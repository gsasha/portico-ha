// Portico Elements tab (DEV-760 E4; portico_elements_design.md section 5): the READ-ONLY list of stored elements with a
// live preview of each, drawn by the shared renderer (portico-elements-render.js). E5 adds Add / Edit / Rename / Remove
// with a live preview BEFORE save (the draft is drawn by the same renderer; `validate_only` asks the server to check it
// without writing). Buttons stay inert previews: nothing on this page runs an action.
//
// A pure ES module the panel imports lazily, like the Phones and Settings tabs: a failed import leaves no Elements tab
// and breaks nothing. The panel hands in the renderer module it imported from its config (`elements_render_module`),
// so this file imports nothing. Wire (administrators only, the server checks): portico/elements/list {} ->
// {schema_version, rev, elements: [{id, kind, name, icon?, entity_id?, options}], used_by, kinds: {kind: {label, ...}}, ...}.
//
// Live previews: the panel calls setHass(hass) from its `set hass` (HA is expected to call it at each state change but
// that is UNVERIFIED for a custom panel), and as a fallback, while the tab is open, the controller also subscribes to
// `state_changed` on hass.connection and patches its own copy of the states. Whichever arrives first wins; both are
// idempotent. All server and device text reaches the page with textContent only; no HTML-string setter is used anywhere
// (a test greps this file).

export const KIND_ORDER = ["talk", "button", "camera", "status", "chip"];
export const ERROR_TEXT = {
  unauthorized: "Only a Home Assistant administrator can see the elements.",
  unknown_command: "This Home Assistant has not loaded the Elements commands yet. Restart Home Assistant to load them.",
  newer_schema: "The elements file was written by a newer Portico. Update the integration before using this page.",
};
export const EMPTY_TEXT = "No elements yet. Use the Add button in a kind's box below; elements you add will be listed there, grouped by kind.";
export const SUMMARY_TEXT = "Named, reusable pieces of UI. A button here is only a preview: it never runs.";

/* Every server error code, as a sentence for the person. `invalid` carries the server's own (value-free) text. */
export const ERROR_MESSAGES = {
  ...ERROR_TEXT,
  invalid: "That element is not valid.",
  name_taken: "That name is already used by another element. Pick a different name.",
  conflict: "The elements changed since this page read them (another session saved first).",
  unknown_element: "That element no longer exists. Reload the list.",
  kind_locked: "An element's kind cannot change. Remove it and add a new one.",
  too_many: "There are too many elements. Remove one first.",
  in_use: "Screens use this element. Remove it from those screens first.",
  in_use_action: "Screens use this button, so its action cannot change. Remove it from those screens first.",
  in_use_stream: "Screens use this camera, so it needs a camera entity. Remove it from those screens first.",
  migration_missing: "The stored elements could not be migrated. See the Home Assistant log.",
  unknown: "Something went wrong on the server. See the Home Assistant log.",
};
export const RELOAD_CODES = ["conflict", "unknown_element"];
const TARGET_ACTIONS = ["portico_open", "call", "talk"];
const TALK_DEFAULT_ICON = "phone";
const SIZE_CHOICES = ["auto", "big", "small"];
const FIELD_LABELS = { entity_id: "Entity", small_entity_id: "Small stream (optional)", stream: "Stream address (rtsp:// or rtsps://, no password)", size: "Size", action: "Action",
  door: "Intercom door", group: "Calls a group (instead of a door, a phone or a screen)", target: "Target", color: "Colour", short: "Short name (shown on a chip)", attribute: "Attribute (show this instead of the state)" };
const MIN_DEBOUNCE_MS = 350;
const ICON_RE = /^[a-z0-9]+(-[a-z0-9]+)*$/; // the server's rule for a free mdi name (status, chip)
const ICON_MAX = 64;

/* ---- the draft: a flat, form-shaped copy of an element ---- */

export function emptyDraft(kind) {
  return { kind, name: "", icon: "", entity_id: "", small_entity_id: "", stream: "", size: "", action: "", target: "", door: "", group: "", color: "", short: "", attribute: "",
    source: "entity", states: [], extra: {} };
}

export function elementToDraft(element) {
  const o = (element && element.options) || {};
  const d = emptyDraft(element.kind);
  d.name = element.name || "";
  d.icon = element.icon || "";
  d.entity_id = element.entity_id || "";
  d.small_entity_id = typeof o.small_entity_id === "string" ? o.small_entity_id : "";
  d.source = !element.entity_id && o.stream ? "stream" : "entity";
  for (const k of ["stream", "size", "action", "target", "door", "group", "color", "short", "attribute"]) if (typeof o[k] === "string") d[k] = o[k];
  d.states = Object.entries(o.states && typeof o.states === "object" ? o.states : {})
    .map(([state, rule]) => ({ state, label: (rule && rule.label) || "", color: (rule && rule.color) || "" }));
  for (const [k, v] of Object.entries(o)) if (!(k in d) && k !== "states" && typeof v === "string") d.extra[k] = v;
  return d;
}

/* The fields the form shows for this draft, in order, from the kind's own field list. Pure. */
export function visibleFields(spec, draft) {
  const names = ((spec && spec.fields) || []).map((f) => f.name);
  const both = names.includes("entity_id") && names.includes("stream");
  const action = ((spec && spec.actions) || []).find((a) => a.action === draft.action);
  return names.filter((n) => {
    if (both && n === "entity_id") return draft.source === "entity";
    if (both && n === "stream") return draft.source === "stream";
    if (n === "small_entity_id") return draft.source === "entity";       // a stream address has no small side (DEV-768)
    if (n === "entity_id" && spec.actions) return !!(action && action.entity_domain);
    if (n === "target") return TARGET_ACTIONS.includes(draft.action);
    if (names.includes("group") && draft.group && (n === "door" || n === "screen" || n === "phone")) return false;   // a Talk element names exactly one target
    return true;
  });
}

/* The element the server stores for a draft: only the visible fields, empty values left out. `id` for an edit. Pure. */
export function draftToElement(spec, draft, id) {
  const out = { kind: draft.kind, name: draft.name };
  if (id) out.id = id;
  if (draft.icon) out.icon = draft.icon.replace(/^mdi:/, "");
  const options = {};
  const known = new Set(["entity_id", "small_entity_id", "stream", "size", "action", "target", "door", "group", "color", "short", "attribute", "states"]);
  for (const n of visibleFields(spec, draft)) {
    if (n === "entity_id") { if (draft.entity_id) out.entity_id = draft.entity_id; }
    else if (n === "states") {
      const states = {};
      for (const r of draft.states) {
        if (!r.state) continue;
        const rule = {};
        if (r.label) rule.label = r.label;
        if (r.color) rule.color = r.color;
        states[r.state] = rule;
      }
      if (Object.keys(states).length) options.states = states;
    } else if (known.has(n)) { if (draft[n]) options[n] = draft[n]; }
    else if (draft.extra[n]) options[n] = draft.extra[n];
  }
  // DEV-768: a lone stream is "the stream" and is stored as entity_id; filling only the Small picker stores it there
  if (options.small_entity_id) {
    if (!out.entity_id) { out.entity_id = options.small_entity_id; delete options.small_entity_id; }
    else if (out.entity_id === options.small_entity_id) delete options.small_entity_id;
  }
  out.options = options;
  return out;
}

/* Where a server `field` (envelope or options.*) shows in the form. */
export function fieldKey(field) {
  if (typeof field !== "string") return "";
  const k = field.replace(/^options\./, "");
  return k === "states" || /^[a-z_]+$/.test(k) ? k : "";
}

export function nameClash(elements, name, ownId) {
  const key = String(name || "").trim().toLowerCase();
  if (!key) return false;
  return (elements || []).some((e) => e && e.id !== ownId && typeof e.name === "string" && e.name.trim().toLowerCase() === key);
}

/* What to tell the person about a failed call. The server's `invalid` text is value-free; nothing else is passed on raw. */
export function errorMessage(err, classify) {
  const code = err && err.code;
  if (code === "invalid") {
    const t = err.message || err.error?.message;
    return typeof t === "string" && t && t !== "invalid" ? `That element is not valid: ${t}.` : ERROR_MESSAGES.invalid;
  }
  if (code && Object.prototype.hasOwnProperty.call(ERROR_MESSAGES, code)) return ERROR_MESSAGES[code];
  return classify ? classify(err).message : ((err && err.message) || "Something went wrong.");
}

/* The entity list of the panel's own pickers (portico-panel.js `entityOptions`), used when the panel hands none in. */
export function entityOptionsFallback(states, domain, filter) {
  const needle = String(filter || "").trim().toLowerCase();
  const out = [];
  for (const [id, st] of Object.entries(states || {})) {
    if (domain && id.split(".")[0] !== domain) continue;
    const name = (st && st.attributes && st.attributes.friendly_name) || id;
    if (needle && !id.toLowerCase().includes(needle) && !String(name).toLowerCase().includes(needle)) continue;
    out.push({ value: id, label: name === id ? id : `${name} (${id})`, name: String(name) });
  }
  return out.sort((a, b) => a.name.localeCompare(b.name) || a.value.localeCompare(b.value));
}

/* ---- the camera picker's picture sizes (DEV-760, D-20261109-b0a3) ----
 * `portico/cameras/info` (camera_info.py) reads the size of the still a camera returns now; the picker shows it beside
 * the name so the main and the sub stream of one camera can be told apart. HA has no resolution attribute of its own. */
export const CAMERA_BATCH = 20;             // the command takes at most this many ids
export const CAMERA_TTL_MS = 10 * 60 * 1000;
export const CAMERA_RETRY_MS = 60 * 1000;   // a failed read is tried again after this
const CAMERA_STREAM_SUFFIX = /_(main|sub\d*|high_resolution_channel|medium_resolution_channel|low_resolution_channel|high|medium|low|hd|sd|fluent|clear|balanced)$/;

/* The camera a stream belongs to: the entity id without a trailing stream word (`camera.gate_intercom_sub` -> `gate_intercom`). */
export function cameraFamily(entityId) {
  const id = String(entityId || "");
  const name = id.slice(id.indexOf(".") + 1);
  const base = name.replace(CAMERA_STREAM_SUFFIX, "");
  return base || name;
}

/* The picture size of one `cameras/info` row as text. `info` is a row, undefined while it is being asked (shows an
 * ellipsis) or anything else when the server had nothing (shows "unknown"). Pure. */
export function resolutionText(info) {
  if (info === undefined) return "\u2026";
  const live = liveSize(info);
  const w = live ? live.width : info && info.width, h = live ? live.height : info && info.height;
  return Number.isInteger(w) && Number.isInteger(h) && w > 0 && h > 0 ? `${w}x${h}` : "unknown";
}

/* The size of the camera's LIVE stream when the server read it (`row.stream = {width, height}`, DEV-766), else null. For a
 * Frigate camera the still is the 640x480 detect frame while the live stream is the main restream, so this is what the
 * owner is choosing. Pure. */
export function liveSize(info) {
  const s = info && info.stream;
  if (!s || typeof s !== "object") return null;
  const w = s.width, h = s.height;
  return Number.isInteger(w) && Number.isInteger(h) && w > 0 && h > 0 ? { width: w, height: h } : null;
}

export const SIZE_TITLE_LIVE = "Size of the camera's live stream, read from the stream itself. The snapshot a phone receives may be smaller.";
export const SIZE_TITLE_STILL = "Size of the picture the camera returns now (a snapshot); a live stream may differ.";
export function sizeTitle(info) { return liveSize(info) ? SIZE_TITLE_LIVE : SIZE_TITLE_STILL; }

/* The two streams of a camera element (DEV-768): `big` is its entity_id, `small` the optional options.small_entity_id.
 * Both are "" when absent. Pure. */
export function cameraStreams(el) {
  const big = el && typeof el.entity_id === "string" ? el.entity_id : "";
  const o = el && el.options && typeof el.options === "object" ? el.options : {};
  const small = typeof o.small_entity_id === "string" ? o.small_entity_id : "";
  return { big, small };
}

/* Whether a state change of `id` concerns the element: its entity_id or, for a camera, its small stream. Pure. */
function touches(el, id) { const { big, small } = cameraStreams(el); return big === id || (!!small && small === id); }

/* The caption line of the small stream under a camera element's preview: "camera.x_sub - 640x480 (live stream)"; "" when
 * the element has no small stream. `info` as in `resolutionText`. Pure. */
export function cameraSmallCaption(el, info) {
  if (!el || el.kind !== "camera") return "";
  const { small } = cameraStreams(el);
  if (!small) return "";
  const t = resolutionText(info);
  if (t === "\u2026") return `${small} - ...`;
  return t === "unknown" ? `${small} - size unknown` : `${small} - ${t} (${liveSize(info) ? "live stream" : "snapshot"})`;
}

/* The caption under a camera element's preview. `el` is the element ({kind, entity_id, stream}); `info` as in
 * `resolutionText`. Returns the text, or "" for an element that is no camera or has nothing to show yet. A stream-only
 * camera says "stream" and is never probed. Pure. */
export function cameraCaption(el, info) {
  if (!el || el.kind !== "camera") return "";
  const ent = typeof el.entity_id === "string" ? el.entity_id : "";
  if (!ent) return el.options && typeof el.options.stream === "string" && el.options.stream ? "stream" : "";
  const t = resolutionText(info);
  if (t === "\u2026") return `${ent} - ...`;
  return t === "unknown" ? `${ent} - size unknown` : `${ent} - ${t} (${liveSize(info) ? "live stream" : "snapshot"})`;
}

/* Options sorted by camera family, then the largest picture first inside a family; sizes not known go last. Pure.
 * `sizeOf(entity_id)` returns the pixel count or 0. */
export function sortCameraOptions(opts, sizeOf) {
  const key = (o) => cameraFamily(o.value);
  return opts.slice().sort((a, b) =>
    key(a).localeCompare(key(b)) || (sizeOf(b.value) - sizeOf(a.value)) || String(a.name).localeCompare(String(b.name)) || a.value.localeCompare(b.value));
}

/* ---- Talk element (DEV-518): the intercom door picker ----
 * `reply.talk_doors` is `[{door_id, name, driver, can_dial, available}]` from the server (the `portico/doors` facts). Each choice
 * says the door's driver and whether a press could dial it now, like the camera picker's resolutions. A stored door that is no
 * longer listed stays selectable as "door removed" (the server keeps the reference; saving the element unchanged still works). */
export function doorState(door) {
  if (!door) return "door removed";
  if (door.can_dial === true) return door.available === false ? "can dial, not connected" : "can dial now";
  return door.available === false ? "cannot dial now, not connected" : "cannot dial now";
}
export function doorChoices(doors, current) {
  const list = Array.isArray(doors) ? doors.filter((d) => d && typeof d.door_id === "string") : [];
  const out = list.map((d) => ({ value: d.door_id, label: `${d.name || d.door_id} \u2014 ${d.driver || "unknown driver"}, ${doorState(d)}` }));
  if (current && !list.some((d) => d.door_id === current)) out.unshift({ value: current, label: `${current} \u2014 ${doorState(null)}` });
  return out;
}
/* The one-line "door removed" note for a Talk element on the list, or "". */
export function doorNote(element, doors) {
  if (!element || element.kind !== "talk") return "";
  const id = element.options && element.options.door;
  const list = Array.isArray(doors) ? doors : [];
  return list.some((d) => d && d.door_id === id) ? "" : "door removed";
}

/* A Talk element that calls a group (everyone, phones or panels): the three words and one line each (group-call design 2 and 3). */
export const GROUP_CHOICES = [
  { value: "everyone", label: "Everyone - every phone and panel that can receive a call", note: "Calls everyone" },
  { value: "phones", label: "All phones - every phone that can receive a call, panels left out", note: "Calls all phones" },
  { value: "panels", label: "All panels - the other wall panels that can receive a call", note: "Calls all panels" },
];
/* The list row's one line for a group Talk element ("Calls everyone"), or "". */
export function groupNote(element) {
  const found = element && element.kind === "talk" && element.options ? GROUP_CHOICES.find((c) => c.value === element.options.group) : null;
  return found ? found.note : "";
}

/* The elements grouped by kind: known kinds in KIND_ORDER first, then any other kind in the order met. Pure. */
export function groupByKind(elements, kinds) {
  const groups = new Map();
  for (const e of Array.isArray(elements) ? elements : []) {
    if (!e || typeof e !== "object") continue;
    const kind = typeof e.kind === "string" && e.kind ? e.kind : "unknown";
    if (!groups.has(kind)) groups.set(kind, []);
    groups.get(kind).push(e);
  }
  const names = [...KIND_ORDER.filter((k) => groups.has(k)), ...[...groups.keys()].filter((k) => !KIND_ORDER.includes(k))];
  return names.map((kind) => {
    const spec = kinds && typeof kinds === "object" && Object.prototype.hasOwnProperty.call(kinds, kind) ? kinds[kind] : null;
    const label = spec && typeof spec.label === "string" && spec.label ? spec.label : kind;
    return { kind, label, elements: groups.get(kind) };
  });
}

const STYLE = `
  .elements, .elements .el-body { display: grid; gap: 12px; }
  .elements .el-sum { color: var(--secondary-text-color); font-size: 13px; margin: 0; }
  .elements h2 { font-size: 17px; font-weight: 500; margin: 0 0 6px; }
  .elements .el-group { background: var(--card-background-color); border: 1px solid var(--divider-color);
                        border-radius: var(--ha-card-border-radius, 12px); padding: 12px; }
  .elements .el-ghead { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 6px; }
  .elements .el-ghead h2 { margin: 0; }
  .elements .el-rows { display: grid; gap: 8px; }
  .elements .el-row { display: grid; grid-template-columns: minmax(150px, 240px) 1fr; gap: 12px; align-items: center; }
  .elements .el-meta { display: grid; gap: 2px; font-size: 14px; overflow-wrap: anywhere; }
  .elements .el-kind, .elements .el-ent { color: var(--secondary-text-color); font-size: 12px; }
  .elements .el-msg.err { color: var(--error-color, #db4437); }
  .elements .pe-tile { border: 1px solid var(--divider-color); border-radius: 10px; padding: 8px 10px; display: flex;
                       flex-wrap: wrap; gap: 6px; align-items: center; font-size: 14px; }
  .elements .pe-camera { display: grid; }
  .elements .pe-pic img { max-width: 100%; max-height: 140px; border-radius: 6px; }
  .elements .pe-sub { color: inherit; opacity: .75; font-size: 12px; width: 100%; }
  .elements .pe-value { margin-left: auto; font-weight: 500; }
  .elements .el-row { grid-template-columns: minmax(150px, 240px) 1fr auto; }
  .elements .el-row.el-row-pair { grid-template-columns: minmax(260px, 460px) minmax(120px, 1fr) auto; }
  @media (max-width: 640px) { .elements .el-row.el-row-pair { grid-template-columns: 1fr; } }
  .elements .el-acts { display: flex; gap: 6px; flex-wrap: wrap; }
  .elements .el-notice { color: var(--primary-text-color); background: var(--secondary-background-color); padding: 8px 12px; border-radius: 8px; margin: 0; }
  .elements .el-form { display: grid; grid-template-columns: minmax(260px, 1fr) minmax(200px, 320px); gap: 16px; align-items: start; }
  .elements .el-fields { display: grid; gap: 10px; }
  .elements .el-field { display: grid; gap: 4px; font-size: 14px; }
  .elements .el-field input, .elements .el-field select { font: inherit; padding: 6px 8px; max-width: 100%; box-sizing: border-box; }
  .elements .el-err { color: var(--error-color, #db4437); font-size: 12px; min-height: 0; }
  .elements .el-ok { color: var(--secondary-text-color); font-size: 12px; }
  .elements .el-previewbox { position: sticky; top: 8px; display: grid; gap: 6px; }
  .elements .el-staterow { display: grid; grid-template-columns: 1fr 1fr 1fr auto; gap: 6px; }
  .elements .el-buttons { display: flex; gap: 8px; flex-wrap: wrap; }
  .elements .el-used { margin: 0; padding-left: 20px; }
  @media (max-width: 720px) { .elements .el-row, .elements .el-form { grid-template-columns: 1fr; } }
`;

export class ElementsController {
  /* opts: doc (the panel's document), getHass (the current hass), classify (the panel's classifyError), render (the
   * imported portico-elements-render.js module: needs renderElement). */
  constructor(opts) {
    this.doc = opts.doc;
    this.getHass = opts.getHass;
    this.render = opts.render;
    this.entityOptions = opts.entityOptions || entityOptionsFallback;   // the panel's own list, passed in
    this.validateMs = Number.isFinite(opts.validateMs) ? opts.validateMs : MIN_DEBOUNCE_MS;
    this.view = { mode: "list" };     // list | form | remove
    this.notice = "";
    this.preview = null;              // { element, handle } of the form's live preview
    this._vtimer = null;
    this.classify = opts.classify || ((err) => ({ kind: "other", message: (err && err.message) || "Something went wrong." }));
    this.reply = null;           // the last good elements/list reply
    this.error = "";
    this.loading = false;
    this.handles = [];           // [{ element, handle }] of the previews now on the page
    this.states = {};            // our copy of hass.states, patched by state_changed events
    this.dark = false;
    this._gen = 0;
    this._camInfo = new Map();   // entity_id -> { row | null, at }: the picture sizes already read
    this._camPending = new Set();
    this._camWatch = new Set();  // [{ sel, fill }] of the camera pickers waiting for sizes
    this._unsub = null;
    this._subGen = 0;
    this.disposed = false;

    const doc = this.doc;
    this.el = doc.createElement("section");
    this.el.className = "elements";
    this.el.dataset.elements = "";
    const style = doc.createElement("style");
    style.textContent = STYLE;
    this.body = doc.createElement("div");
    this.body.className = "el-body";
    this.el.append(style, this.body);
    this._readStates();
    this._draw();
  }

  attach(parent) {
    if (this.el.parentNode !== parent) parent.append(this.el);
  }

  /* The tab was opened: the list is read again and the live feed starts. */
  show() {
    this.load();
    this._subscribe();
  }

  hide() {
    this._unsubscribe();
  }

  dispose() {
    this.disposed = true;
    this._gen++;
    this._unsubscribe();
    this._clearValidate();
    this._destroyHandles();
    this._destroyPreview();
  }

  errText(err) {
    const code = err && err.code;
    if (code && Object.prototype.hasOwnProperty.call(ERROR_TEXT, code)) return ERROR_TEXT[code];
    return this.classify(err).message;
  }

  /* ---- reading ---- */

  async load() {
    const gen = ++this._gen;
    this.loading = true;
    if (this.view.mode === "list") this._draw();
    let reply = null;
    let error = "";
    try {
      reply = await this.getHass().callWS({ type: "portico/elements/list" });
      if (!reply || typeof reply !== "object" || !Array.isArray(reply.elements)) {
        reply = null;
        error = "The server's answer was not what this page expects.";
      }
    } catch (err) {
      error = this.errText(err);
    }
    if (gen !== this._gen || this.disposed) return false;
    this.loading = false;
    if (reply) { this.reply = reply; this.error = ""; } else this.error = error;
    if (this.view.mode === "list") this._draw();
    else if (this.view.mode === "form") this._refreshForm();     // the open form keeps the draft; only the checks follow the new list
    return !error;
  }

  /* ---- live states ---- */

  _readStates() {
    const hass = this.getHass && this.getHass();
    this.states = hass && hass.states && typeof hass.states === "object" ? { ...hass.states } : {};
    this.dark = !!(hass && hass.themes && hass.themes.darkMode);
  }

  /* The panel's `set hass` calls this. HA replaces hass.states at each change, so a fresh copy is the truth. */
  setHass(hass) {
    if (this.disposed) return;
    const dark = !!(hass && hass.themes && hass.themes.darkMode);
    this.states = hass && hass.states && typeof hass.states === "object" ? { ...hass.states } : {};
    if (dark !== this.dark) { this.dark = dark; this._draw(); this._repaintPreview(); return; }      // a theme flip repaints the palette colours
    this._push(null);
  }

  /* Fallback feed: a state_changed event carries {entity_id, new_state}; null new_state means removed. */
  _onStateChanged(event) {
    if (this.disposed || !event) return;
    const data = event.data || event;
    const id = data && data.entity_id;
    if (typeof id !== "string") return;
    if (data.new_state) this.states[id] = data.new_state;
    else delete this.states[id];
    this._push(id);
  }

  _push(entityId) {
    if (this.preview && (entityId === null || touches(this.preview.element, entityId))) {
      try { this.preview.handle.update(this.states); } catch (_err) { /* the preview must not break the page */ }
    }
    for (const { element, handle } of this.handles) {
      if (entityId !== null && !touches(element, entityId)) continue;
      try { handle.update(this.states); } catch (_err) { /* one bad preview must not stop the others */ }
    }
  }

  async _subscribe() {
    if (this._unsub || this.disposed) return;
    const hass = this.getHass();
    const conn = hass && hass.connection;
    if (!conn || typeof conn.subscribeEvents !== "function") return;     // setHass alone then
    const gen = ++this._subGen;
    try {
      const unsub = await conn.subscribeEvents((ev) => this._onStateChanged(ev), "state_changed");
      if (gen !== this._subGen || this._unsub || this.disposed) { try { unsub(); } catch (_err) { /* gone */ } return; }
      this._unsub = unsub;
    } catch (_err) {
      this._unsub = null;     // no fallback feed; setHass still works
    }
  }

  _unsubscribe() {
    this._subGen++;
    const unsub = this._unsub;
    this._unsub = null;
    if (unsub) { try { unsub(); } catch (_err) { /* the connection is gone */ } }
  }

  /* ---- drawing ---- */

  _destroyHandles() {
    for (const { handle } of this.handles) { try { handle.destroy(); } catch (_err) { /* already gone */ } }
    this.handles = [];
  }

  _t(tag, cls, text) {
    const n = this.doc.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }

  _draw() {
    this._destroyHandles();
    if (this.view.mode === "remove") { this._drawRemove(); return; }
    if (this.view.mode === "form") return;          // the form draws itself; a list refresh must not wipe the draft
    const body = this.body;
    body.replaceChildren();
    body.append(this._t("p", "el-sum", SUMMARY_TEXT));
    if (this.notice) {
      const n = this._t("p", "el-notice", this.notice);
      n.dataset.elements = "notice";
      body.append(n);
    }
    if (this.error) {
      const msg = this._t("p", "el-msg err", this.error);
      msg.dataset.elements = "error";
      body.append(msg);
    }
    if (!this.reply) {
      if (this.loading) body.append(this._t("p", "el-msg", "Loading..."));
      return;
    }
    const groups = groupByKind(this.reply.elements, this.reply.kinds);
    if (!groups.length) {
      const empty = this._t("p", "el-msg", EMPTY_TEXT);
      empty.dataset.elements = "empty";
      body.append(empty);
    }
    // a box for every kind the server can create (even with no elements, so it can be added to), then any other kind met
    const kinds = this._kinds();
    const have = Object.keys(kinds);
    const known = [...KIND_ORDER.filter((k) => have.includes(k)), ...have.filter((k) => !KIND_ORDER.includes(k))];
    const byKind = new Map(groups.map((g) => [g.kind, g]));
    const boxes = [...known.map((k) => byKind.get(k) || { kind: k, label: (kinds[k] && typeof kinds[k].label === "string" && kinds[k].label) || k, elements: [] }),
      ...groups.filter((g) => !known.includes(g.kind))];
    for (const g of boxes) {
      const box = this._t("div", "el-group");
      box.dataset.kind = g.kind;
      const head = this._t("div", "el-ghead");
      head.append(this._t("h2", null, `${g.label} (${g.elements.length})`));
      if (known.includes(g.kind)) {
        const add = this._btn(`Add ${String(g.label).toLowerCase()}`, "add", () => this.openAdd(g.kind));
        add.dataset.kind = g.kind;
        head.append(add);
      }
      box.append(head);
      const rows = this._t("div", "el-rows");
      for (const e of g.elements) rows.append(this._row(e));
      box.append(rows);
      body.append(box);
    }
    this._askCaptionCameras(this.reply.elements);
  }

  /* One batched read for every camera element's entity on the list (the shared cache skips what is known). */
  _askCaptionCameras(elements) {
    const ids = [];
    for (const e of elements || []) {
      if (!e || e.kind !== "camera") continue;
      const { big, small } = cameraStreams(e);
      for (const id of [big, small]) if (id && !ids.includes(id)) ids.push(id);
    }
    if (ids.length) this._askCameras(ids);
  }

  /* The line inside a camera card: "camera.x - 2560x1440 (live stream)" or "... (snapshot)" when the stream's size is not
   * known; "" for an entity not asked about yet or no entity (the card then shows the bare entity_id). */
  _streamCaption(id) {
    if (typeof id !== "string" || !id) return "";
    return cameraCaption({ kind: "camera", entity_id: id }, this._capInfo({ entity_id: id }));
  }

  /* What the renderer needs besides the states: the card caption, and the hass for the live full-size window. */
  _renderCtx() {
    return { size: "preview", dark: this.dark, states: this.states, caption: (id) => this._streamCaption(id), getHass: () => (this.getHass ? this.getHass() : null) };
  }

  _capInfo(element, small) {
    const id = small ? cameraStreams(element).small : element && element.entity_id;
    if (typeof id !== "string" || !id) return undefined;
    const have = this._camInfo.get(id);
    return have ? have.row : undefined;
  }

  _row(element) {
    const row = this._t("div", "el-row");
    row.dataset.element = typeof element.id === "string" ? element.id : "";
    const preview = this._t("div", "el-preview");
    if (element.kind === "camera" && cameraStreams(element).small) row.classList.add("el-row-pair");   // both pictures side by side in the row
    try {
      const handle = (this.render.renderPreview || this.render.renderElement)(this.doc, element, this._renderCtx());
      preview.append(handle.el);
      this.handles.push({ element, handle });
    } catch (_err) {
      preview.append(this._t("span", "el-msg err", "This preview could not be drawn."));
    }
    const meta = this._t("div", "el-meta");
    meta.append(this._t("strong", "el-name", typeof element.name === "string" && element.name ? element.name : "(unnamed)"),
      this._t("span", "el-kind", typeof element.kind === "string" ? element.kind : "unknown"));
    const streams = element.kind === "camera" ? cameraStreams(element) : null;
    if (streams && streams.small) {              // DEV-768: a camera with two streams names both
      const big = this._t("span", "el-ent", `Big: ${streams.big || "(none)"}`);
      big.dataset.elements = "cam-big";
      const small = this._t("span", "el-ent", `Small: ${streams.small}`);
      small.dataset.elements = "cam-small";
      meta.append(big, small);
    } else if (typeof element.entity_id === "string" && element.entity_id) meta.append(this._t("span", "el-ent", element.entity_id));
    if (groupNote(element)) {
      const note = this._t("span", "el-ent", groupNote(element));
      note.dataset.elements = "talk-group";
      meta.append(note);
    } else if (element.kind === "talk") {
      const found = ((this.reply && this.reply.talk_doors) || []).find((d) => d && d.door_id === (element.options || {}).door);
      const note = this._t("span", "el-ent", found ? `${found.name} \u2014 ${doorState(found)}` : `${(element.options || {}).door || ""} \u2014 ${doorState(null)}`);
      note.dataset.elements = "talk-door";
      meta.append(note);
    }
    const acts = this._t("div", "el-acts");
    const kinds = (this.reply && this.reply.kinds) || {};
    if (typeof element.id === "string" && Object.prototype.hasOwnProperty.call(kinds, element.kind)) {
      acts.append(this._btn("Edit", "edit", () => this.openEdit(element)), this._btn("Rename", "rename", () => this.openEdit(element, "name")));
    }
    if (typeof element.id === "string") acts.append(this._btn("Remove", "remove", () => this.askRemove(element)));
    row.append(preview, meta, acts);
    return row;
  }

  /* ---- E5: add / edit / rename / remove ---- */

  _btn(text, act, fn) {
    const b = this._t("button", null, text);
    b.type = "button";
    b.dataset.elAct = act;
    b.addEventListener("click", fn);
    return b;
  }

  _kinds() { return (this.reply && this.reply.kinds && typeof this.reply.kinds === "object") ? this.reply.kinds : {}; }

  _leave(notice) {
    this._clearValidate();
    this._destroyPreview();
    this.view = { mode: "list" };
    this.notice = notice || "";
    this._draw();
  }

  openAdd(kind) {
    if (!this.reply || typeof kind !== "string" || !this._kinds()[kind]) return;
    this._openForm(null, this._withDefaults(emptyDraft(kind)), null);
  }

  openEdit(element, focus) {
    if (!this.reply || !element || typeof element.id !== "string" || !this._kinds()[element.kind]) return;
    this._openForm(element.id, elementToDraft(element), focus || null);
  }

  _withDefaults(d) {
    const spec = this._kinds()[d.kind] || {};
    if (Array.isArray(spec.actions) && spec.actions.length && !d.action) d.action = spec.actions[0].action;
    if (d.kind === "talk" && !d.icon) d.icon = TALK_DEFAULT_ICON;
    return d;
  }

  _openForm(editing, draft, focus) {
    this._destroyHandles();
    this._clearValidate();
    this.notice = "";
    this.view = { mode: "form", editing, draft, touched: new Set(), submitted: false, server: {}, general: "", code: "",
      valid: false, vseq: 0, saving: false };
    this._drawForm(focus);
    this._scheduleValidate();
  }

  _spec() { return this._kinds()[this.view.draft.kind] || { fields: [] }; }
  _limits() { return (this.reply && this.reply.limits) || {}; }

  _drawForm(focus) {
    const v = this.view;
    const body = this.body;
    body.replaceChildren();
    const orig = v.editing ? (this.reply.elements.find((e) => e.id === v.editing) || null) : null;
    body.append(this._t("h2", null, v.editing ? `Edit ${orig ? orig.name : "element"}` : `Add ${String(this._kinds()[v.draft.kind] && this._kinds()[v.draft.kind].label || v.draft.kind).toLowerCase()}`));
    const form = this._t("div", "el-form");
    form.dataset.elements = "form";
    const left = this._t("div", "el-fields");
    // kind: fixed text (chosen by the per-kind Add button, or the existing element's own kind; the server refuses a change)
    const kinds = this._kinds();
    const kindTxt = this._t("span", "el-kindfix", (kinds[v.draft.kind] && kinds[v.draft.kind].label) || v.draft.kind);
    kindTxt.dataset.field = "kind";
    left.append(this._field("kind", "Kind", kindTxt));
    const name = this.doc.createElement("input");
    name.type = "text";
    name.dataset.field = "name";
    name.setAttribute("aria-label", "Name");
    name.maxLength = this._limits().name || 40;
    name.value = v.draft.name;
    name.addEventListener("input", () => { v.draft.name = name.value; v.touched.add("name"); this._changed(); });
    left.append(this._field("name", "Name (unique)", name));
    this.iconField = this._field("icon", "Icon", this._iconPicker());
    left.append(this.iconField);
    this.fieldsBox = this._t("div", "el-fields");
    left.append(this.fieldsBox);
    const gen = this._t("div", "el-err");
    gen.dataset.elements = "form-error";
    this.genBox = gen;
    const ok = this._t("div", "el-ok");
    ok.dataset.elements = "valid";
    this.okBox = ok;
    const buttons = this._t("div", "el-buttons");
    this.saveBtn = this._btn("Save", "save", () => this._save());
    buttons.append(this.saveBtn, this._btn("Cancel", "cancel", () => this._leave("")));
    left.append(gen, ok, buttons);
    const right = this._t("div", "el-previewbox");
    right.append(this._t("strong", null, "Preview"));
    this.previewSlot = this._t("div", "el-previewslot");
    right.append(this.previewSlot, this._t("div", "el-sum", "Drawn live from the form, before saving. Nothing here runs."));
    form.append(left, right);
    body.append(form);
    this.preview = null;
    this._drawFields();
    this._changed();
    if (focus) { const n = form.querySelector(`[data-field="${focus}"]`); if (n && n.focus) n.focus(); }
  }

  _field(key, title, control, extra) {
    const w = this._t("div", "el-field");
    w.dataset.fieldWrap = key;
    w.append(this._t("span", null, title), control);
    if (extra) w.append(extra);
    const err = this._t("span", "el-err");
    err.dataset.err = key;
    w.append(err);
    return w;
  }

  _select(key, choices, value, blank) {
    const sel = this.doc.createElement("select");
    sel.dataset.field = key;
    sel.setAttribute("aria-label", key);
    if (blank !== undefined) { const o = this._t("option", null, blank); o.value = ""; sel.append(o); }
    for (const c of choices) { const o = this._t("option", null, c.label); o.value = c.value; sel.append(o); }
    sel.value = value;
    return sel;
  }

  _text(key, value, max) {
    const i = this.doc.createElement("input");
    i.type = "text";
    i.dataset.field = key;
    i.setAttribute("aria-label", key);
    if (max) i.maxLength = max;
    i.value = value || "";
    return i;
  }

  /* The fields that depend on the kind and on other fields; rebuilt when those change, never while typing. */
  _drawFields() {
    const v = this.view;
    const box = this.fieldsBox;
    box.replaceChildren();
    const spec = this._spec();
    const d = v.draft;
    const names = (spec.fields || []).map((f) => f.name);
    if (names.includes("entity_id") && names.includes("stream")) {
      const sel = this._select("source", [{ value: "entity", label: "A Home Assistant camera" }, { value: "stream", label: "A stream address" }], d.source);
      sel.addEventListener("change", () => { d.source = sel.value; this._drawFields(); this._changed(); });
      box.append(this._field("source", "Picture from", sel));
    }
    const required = new Set((spec.fields || []).filter((f) => f.required).map((f) => f.name));
    for (const n of visibleFields(spec, d)) {
      const title = (FIELD_LABELS[n] || n) + (required.has(n) ? " *" : "");
      let w;
      const twoStreams = names.includes("small_entity_id");        // the camera kind: two pickers, Big and Small
      if (n === "entity_id") w = this._field(n, twoStreams ? "Big stream" : title, this._entityPicker(this._domainFor(spec, d), d.entity_id, (val) => this._set(n, val)));
      else if (n === "small_entity_id") {
        const hint = this._t("div", "el-hint", "Fill only one to always show that one.");
        hint.dataset.field = "streams_hint";
        w = this._field(n, title, this._entityPicker("camera", d.small_entity_id, (val) => this._set(n, val), "small_entity_id"), hint);
      }
      else if (n === "action") {
        const sel = this._select(n, (spec.actions || []).map((a) => ({ value: a.action, label: a.action })), d.action);
        sel.addEventListener("change", () => this._onAction(sel.value));
        w = this._field(n, title, sel);
      } else if (n === "door") {
        const sel = this._select(n, doorChoices(this.reply && this.reply.talk_doors, d.door), d.door, "Choose a door...");
        sel.addEventListener("change", () => this._set(n, sel.value));
        w = this._field(n, title, sel);
      } else if (n === "group") {
        const sel = this._select(n, GROUP_CHOICES.map((c) => ({ value: c.value, label: c.label })), d.group, "(none)");
        sel.addEventListener("change", () => { this._set(n, sel.value); this._drawFields(); });      // a group hides the door, screen and phone fields
        w = this._field(n, title, sel);
      } else if (n === "size") {
        const sel = this._select(n, SIZE_CHOICES.map((c) => ({ value: c, label: c })), d.size, "(default)");
        sel.addEventListener("change", () => this._set(n, sel.value));
        w = this._field(n, title, sel);
      } else if (n === "color") {
        const sel = this._select(n, (this.reply.palette || []).map((c) => ({ value: c, label: c })), d.color, "(default)");
        sel.addEventListener("change", () => this._set(n, sel.value));
        w = this._field(n, title, sel);
      } else if (n === "states") w = this._field(n, "State words and colours (optional)", this._statesEditor());
      else {
        const known = Object.prototype.hasOwnProperty.call(d, n) && n !== "extra";
        const input = this._text(n, known ? d[n] : d.extra[n], n === "short" ? this._limits().short : n === "stream" ? this._limits().stream : 0);
        input.addEventListener("input", () => this._set(n, input.value, known ? null : "extra"));
        w = this._field(n, title, input);
      }
      box.append(w);
    }
    this._showErrors();
  }

  _domainFor(spec, d) {
    if (Array.isArray(spec.actions)) {
      const a = spec.actions.find((x) => x.action === d.action);
      return a ? a.entity_domain || null : null;
    }
    return spec.entity_domain || null;
  }

  _onAction(action) {
    const d = this.view.draft;
    d.action = action;
    const dom = this._domainFor(this._spec(), d);
    if (d.entity_id && dom && d.entity_id.split(".")[0] !== dom) d.entity_id = "";     // a lock is not a cover
    if (!TARGET_ACTIONS.includes(action)) d.target = "";
    this.view.touched.add("action");
    this._drawFields();
    this._changed();
  }

  _set(key, value, bag) {
    const v = this.view;
    if (bag === "extra") v.draft.extra[key] = value; else v.draft[key] = value;
    v.touched.add(key);
    this._changed();
  }

  _statesEditor() {
    const v = this.view;
    const box = this._t("div", "el-states");
    const palette = (this.reply.palette || []).map((c) => ({ value: c, label: c }));
    const draw = () => {
      box.replaceChildren();
      v.draft.states.forEach((row, i) => {
        const r = this._t("div", "el-staterow");
        r.dataset.row = String(i);
        const st = this._text("state", row.state, this._limits().state);
        st.placeholder = "state (e.g. on)";
        const lb = this._text("label", row.label, this._limits().state);
        lb.placeholder = "word to show";
        const co = this._select("color", palette, row.color, "(default)");
        for (const [inp, k] of [[st, "state"], [lb, "label"]]) inp.addEventListener("input", () => { row[k] = inp.value; v.touched.add("states"); this._changed(); });
        co.addEventListener("change", () => { row.color = co.value; v.touched.add("states"); this._changed(); });
        r.append(st, lb, co, this._btn("Remove state", "state-remove", () => { v.draft.states.splice(i, 1); v.touched.add("states"); draw(); this._changed(); }));
        box.append(r);
      });
      const add = this._btn("Add a state", "state-add", () => { v.draft.states.push({ state: "", label: "", color: "" }); draw(); this._changed(); });
      add.disabled = v.draft.states.length >= (this._limits().states || 16);
      box.append(add);
    };
    draw();
    return box;
  }

  /* HA's own pickers where this browser defines them (a custom panel often cannot rely on that); else the panel's list. */
  _native(tag) {
    const win = this.doc.defaultView;
    try { return win && win.customElements && win.customElements.get(tag) ? this.doc.createElement(tag) : null; } catch (_err) { return null; }
  }

  /* Ask for the sizes of the cameras not yet known (or stale), at most CAMERA_BATCH per call, one call at a time; then
   * `done()`. A failure leaves the sizes "unknown". Never throws. */
  async _askCameras(ids) {
    const hass = this.getHass && this.getHass();
    if (!hass || typeof hass.callWS !== "function") return;
    const now = Date.now();
    const want = ids.filter((id) => !this._camPending.has(id) && !(this._camInfo.has(id) && now - this._camInfo.get(id).at < (this._camInfo.get(id).row ? CAMERA_TTL_MS : CAMERA_RETRY_MS)));
    for (let at = 0; at < want.length && !this.disposed; at += CAMERA_BATCH) {
      const batch = want.slice(at, at + CAMERA_BATCH);
      for (const id of batch) this._camPending.add(id);
      let rows = null;
      try {
        const reply = await hass.callWS({ type: "portico/cameras/info", entity_ids: batch });
        rows = reply && Array.isArray(reply.cameras) ? reply.cameras : null;
      } catch (_err) {
        rows = null;              // an older server or a dropped connection: "unknown", and the picker still works
      }
      const byId = new Map((rows || []).filter((r) => r && typeof r.entity_id === "string").map((r) => [r.entity_id, r]));
      const stamp = Date.now();
      for (const id of batch) {
        this._camPending.delete(id);
        const row = byId.get(id);
        this._camInfo.set(id, { row: row && ((Number.isInteger(row.width) && Number.isInteger(row.height)) || liveSize(row)) ? row : null, at: stamp });
      }
      this._camRefill();
    }
  }

  /* Redraw the camera pickers still on the page (a picker is rebuilt by a kind change while a read is under way). */
  _camRefill() {
    if (this.disposed) return;
    for (const h of this.handles) { try { if (h.handle && h.handle.refreshCaptions) h.handle.refreshCaptions(); } catch (_err) { /* one tile only */ } }
    if (this.preview && this.preview.handle && this.preview.handle.refreshCaptions) { try { this.preview.handle.refreshCaptions(); } catch (_err) { /* gone */ } }
    for (const w of Array.from(this._camWatch)) {
      if (!w.sel.isConnected) { this._camWatch.delete(w); continue; }
      w.fill();
    }
  }

  _camSize(id) {
    const have = this._camInfo.get(id);
    if (!have || !have.row) return 0;
    const live = liveSize(have.row);
    return live ? live.width * live.height : have.row.width * have.row.height;
  }

  _camText(id) {
    const have = this._camInfo.get(id);
    return resolutionText(have ? have.row : undefined);
  }

  _entityPicker(domain, value, onChange, fieldName = "entity_id") {
    // HA's own picker cannot show a picture size, so cameras always use the panel's list
    const native = domain === "camera" ? null : this._native("ha-entity-picker");
    if (native) {
      native.dataset.field = fieldName;
      native.dataset.picker = "native";
      native.hass = this.getHass();
      native.value = value || "";
      if (domain) native.includeDomains = [domain];
      native.addEventListener("value-changed", (ev) => onChange((ev.detail && ev.detail.value) || ""));
      return native;
    }
    const wrap = this._t("div", "el-pick");
    wrap.dataset.picker = "list";
    const filter = this.doc.createElement("input");
    filter.type = "search";
    filter.placeholder = "Filter...";
    filter.dataset.field = "entity_filter";
    filter.setAttribute("aria-label", "Filter entities");
    const sel = this.doc.createElement("select");
    sel.dataset.field = fieldName;
    sel.setAttribute("aria-label", fieldName === "small_entity_id" ? "Small stream" : "Entity");
    const fill = () => {
      sel.replaceChildren();
      // the panel's own list filters by one domain; a kind that takes any entity needs the same list unfiltered
      let opts = (domain ? this.entityOptions : entityOptionsFallback)(this.states, domain, filter.value);
      const cams = domain === "camera";
      if (cams) opts = sortCameraOptions(opts, (id) => this._camSize(id));
      const blank = this._t("option", null, opts.length ? "Choose..." : "Nothing found");
      blank.value = "";
      sel.append(blank);
      if (value && !opts.some((o) => o.value === value)) { const o = this._t("option", null, value); o.value = value; sel.append(o); }
      for (const o of opts) {
        const n = this._t("option", null, cams ? `${o.label} \u2014 ${this._camText(o.value)}` : o.label);
        n.value = o.value;
        if (cams) { const have = this._camInfo.get(o.value); n.title = sizeTitle(have ? have.row : undefined); }
        sel.append(n);
      }
      sel.value = value || "";
    };
    filter.addEventListener("input", fill);
    sel.addEventListener("change", () => { value = sel.value; onChange(sel.value); });
    fill();
    if (domain === "camera") {
      const ids = (this.entityOptions)(this.states, domain, "").map((o) => o.value);
      this._camWatch.add({ sel, fill });
      this._askCameras(ids);
    }
    wrap.append(filter, sel);
    return wrap;
  }

  /* "mdi" (status and chip: any Material Design icon name) or "fixed" (button, camera: the server's list). The server says so in
     icon_policy; an older server that does not is read as the same split. */
  _iconMode() {
    const kind = this.view.draft.kind;
    const policy = this.reply.icon_policy;
    if (policy && typeof policy[kind] === "string") return policy[kind] === "mdi" ? "mdi" : "fixed";
    return kind === "status" || kind === "chip" ? "mdi" : "fixed";
  }

  /* The kind changed: the icon control is the one the new kind's policy asks for. */
  _drawIcon() {
    const field = this.iconField;
    if (!field) return;
    const old = field.querySelector("[data-field=icon], [data-picker]");
    const root = old && old.closest ? (old.closest("[data-picker]") || old) : old;
    if (root && root.parentNode === field) root.replaceWith(this._iconPicker());
  }

  _iconPicker() {
    const v = this.view;
    const free = this._iconMode() === "mdi";
    const native = free ? this._native("ha-icon-picker") : null; // HA's picker offers every mdi icon: only where the server takes one
    if (native) {
      native.dataset.field = "icon";
      native.dataset.picker = "native";
      native.value = v.draft.icon ? `mdi:${v.draft.icon}` : "";
      native.addEventListener("value-changed", (ev) => this._set("icon", String((ev.detail && ev.detail.value) || "").replace(/^mdi:/, "")));
      return native;
    }
    if (free) return this._iconTextInput();
    const wrap = this._t("div", "el-pick");
    wrap.dataset.picker = "list";
    const filter = this.doc.createElement("input");
    filter.type = "search";
    filter.placeholder = "Search icons...";
    filter.dataset.field = "icon_filter";
    filter.setAttribute("aria-label", "Search icons");
    const sel = this.doc.createElement("select");
    sel.dataset.field = "icon";
    sel.setAttribute("aria-label", "Icon");
    const fill = () => {
      const needle = filter.value.trim().toLowerCase();
      sel.replaceChildren();
      const blank = this._t("option", null, "(the kind's default)");
      blank.value = "";
      sel.append(blank);
      const cur = v.draft.icon;
      const names = (this.reply.icons || []).filter((n) => !needle || n.includes(needle) || n === cur);
      if (cur && !names.includes(cur)) names.unshift(cur);
      for (const n of names) { const o = this._t("option", null, n); o.value = n; sel.append(o); }
      sel.value = cur || "";
    };
    filter.addEventListener("input", fill);
    sel.addEventListener("change", () => this._set("icon", sel.value));
    fill();
    wrap.append(filter, sel);
    return wrap;
  }

  /* No ha-icon-picker in this browser: type any mdi name, with a hint and a live preview drawn by ha-icon. */
  _iconTextInput() {
    const v = this.view;
    const wrap = this._t("div", "el-pick");
    wrap.dataset.picker = "text";
    const input = this.doc.createElement("input");
    input.type = "text";
    input.dataset.field = "icon";
    input.setAttribute("aria-label", "Icon");
    input.maxLength = ICON_MAX;
    input.pattern = "[a-z0-9]+(-[a-z0-9]+)*";
    input.placeholder = "e.g. lock-open-variant";
    input.autocomplete = "off";
    input.value = v.draft.icon || "";
    const prev = this.doc.createElement("ha-icon");
    prev.dataset.iconPreview = "1";
    const show = () => {
      const name = input.value.trim().replace(/^mdi:/, "");
      if (name && ICON_RE.test(name) && name.length <= ICON_MAX) { prev.setAttribute("icon", `mdi:${name}`); prev.hidden = false; }
      else { prev.removeAttribute("icon"); prev.hidden = true; }
    };
    input.addEventListener("input", () => {
      this._set("icon", input.value.trim().replace(/^mdi:/, ""));
      show();
    });
    show();
    const hint = this._t("span", "el-hint", "Any Material Design Icons name, for example lock-open-variant (see pictogrammers.com/library/mdi). Leave empty for the default.");
    wrap.append(input, prev, hint);
    return wrap;
  }

  /* ---- preview, checks, errors ---- */

  _currentElement() {
    const v = this.view;
    return draftToElement(this._spec(), v.draft, v.editing);
  }

  _destroyPreview() {
    if (this.preview) { try { this.preview.handle.destroy(); } catch (_err) { /* gone */ } }
    this.preview = null;
  }

  /* Redraw the preview from the draft. Rebuilt only when the element (or the theme) changed, so a camera picture is not
   * thrown away by an unrelated keystroke. */
  _repaintPreview() {
    if (this.view.mode !== "form" || !this.previewSlot) return;
    const element = this._currentElement();
    const key = JSON.stringify([element, this.dark]);
    if (this.preview && this.preview.key === key) return;
    this._destroyPreview();
    this.previewSlot.replaceChildren();
    try {
      const handle = (this.render.renderPreview || this.render.renderElement)(this.doc, element, this._renderCtx());
      this.previewSlot.append(handle.el);
      if (element.kind === "camera") {
        const { big, small } = cameraStreams(element);
        this._askCameras([big, small].filter(Boolean));
      }
      this.preview = { element, handle, key };
    } catch (_err) {
      this.previewSlot.append(this._t("span", "el-msg err", "This preview could not be drawn."));
    }
  }

  _local() {
    const v = this.view;
    const out = {};
    const name = v.draft.name.trim();
    if (nameClash(this.reply.elements, name, v.editing)) out.name = ERROR_MESSAGES.name_taken;
    else if (v.submitted && !name) out.name = "Give the element a name.";
    return out;
  }

  _showErrors() {
    const v = this.view;
    if (v.mode !== "form" || !this.body) return;
    const local = this._local();
    const keys = new Set([...Object.keys(local), ...Object.keys(v.server)]);
    for (const n of this.body.querySelectorAll("[data-err]")) keys.add(n.dataset.err);
    for (const n of this.body.querySelectorAll("[data-err]")) {
      const k = n.dataset.err;
      n.textContent = local[k] || ((v.submitted || v.touched.has(k)) && v.server[k]) || "";
    }
    // a server error for a field the form does not show goes to the general line
    const orphan = Object.keys(v.server).filter((k) => !this.body.querySelector(`[data-err="${k}"]`)).map((k) => v.server[k]);
    this.genBox.replaceChildren();
    const lines = [...(v.general ? [v.general] : []), ...orphan];
    if (lines.length) this.genBox.append(this._t("span", null, lines.join(" ")));
    if (RELOAD_CODES.includes(v.code)) this.genBox.append(" ", this._btn("Reload the list", "reload", () => this._reloadForm()));
    if (v.retarget) {
      const names = v.retarget.map((u) => this._usedText(u)).join(", ");
      const ask = this._t("div", "el-msg warn");
      ask.dataset.elements = "retarget";
      ask.append(this._t("span", null, `Used on: ${names || "other screens"}. Apply to all? Changing what this points at changes every one of them.`), " ",
        this._btn("Apply to all", "apply-all", () => this._save(true)));
      this.genBox.append(ask);
    }
    this.okBox.textContent = v.valid && !Object.keys(local).length ? "The server accepts this element." : "";
    this.saveBtn.disabled = v.saving || Object.keys(local).some((k) => k === "name" && nameClash(this.reply.elements, v.draft.name.trim(), v.editing));
  }

  _changed() {
    const v = this.view;
    if (v.mode !== "form") return;
    v.valid = false;
    this._repaintPreview();
    this._showErrors();
    this._scheduleValidate();
  }

  _refreshForm() {
    this._showErrors();
    this._scheduleValidate();
  }

  _clearValidate() {
    const win = this.doc.defaultView;
    if (this._vtimer !== null && win) win.clearTimeout(this._vtimer);
    this._vtimer = null;
    if (this.view && this.view.vseq !== undefined) this.view.vseq++;
  }

  _scheduleValidate() {
    const win = this.doc.defaultView;
    if (this.disposed || this.view.mode !== "form" || !win) return;
    if (this._vtimer !== null) win.clearTimeout(this._vtimer);
    this._vtimer = win.setTimeout(() => { this._vtimer = null; this._validate(); }, this.validateMs);
  }

  /* Server-side check of the draft, nothing written (portico/elements/set with validate_only). */
  async _validate() {
    const v = this.view;
    if (v.mode !== "form" || !this.reply || this.disposed) return;
    const seq = ++v.vseq;
    try {
      await this.getHass().callWS({ type: "portico/elements/set", element: this._currentElement(), base_rev: this.reply.rev, validate_only: true });
      if (seq !== v.vseq || this.view !== v) return;
      v.server = {}; v.general = ""; v.code = ""; v.valid = true; v.retarget = null;
    } catch (err) {
      if (seq !== v.vseq || this.view !== v) return;
      this._mapServer(err);
    }
    this._showErrors();
  }

  _mapServer(err) {
    const v = this.view;
    const code = (err && err.code) || "";
    const text = errorMessage(err, this.classify);
    v.valid = false;
    v.server = {}; v.general = ""; v.code = code; v.retarget = null;
    if (code === "in_use_retarget") {       // not a mistake in the form: a question. The screens that use it are named; Save asks again with confirm
      v.valid = true;
      v.retarget = Array.isArray(err.used_by) ? err.used_by : [];
      return;
    }
    if (code === "name_taken") v.server.name = text;
    else if (code === "invalid") {
      const key = fieldKey(err.field);
      if (key) v.server[key] = text; else v.general = text;
    } else v.general = text;
  }

  async _reloadForm() {
    const v = this.view;
    await this.load();
    if (this.view !== v || this.disposed) return;
    v.general = ""; v.code = ""; v.server = {};
    this._showErrors();
    this._scheduleValidate();
  }

  async _save(confirm = false) {
    const v = this.view;
    if (v.mode !== "form" || v.saving || !this.reply) return;
    v.submitted = true;
    if (!v.draft.name.trim() || nameClash(this.reply.elements, v.draft.name, v.editing)) {
      this._showErrors();
      const n = this.body.querySelector('[data-field="name"]');
      if (n && n.focus) n.focus();
      return;
    }
    this._clearValidate();
    v.saving = true;
    this._showErrors();
    const element = this._currentElement();
    try {
      await this.getHass().callWS({ type: "portico/elements/set", element, base_rev: this.reply.rev, ...(confirm === true ? { confirm: true } : {}) });
    } catch (err) {
      v.saving = false;
      if (this.view !== v || this.disposed) return;
      this._mapServer(err);
      this._showErrors();
      return;
    }
    v.saving = false;
    if (this.disposed) return;
    this._leave(`${v.editing ? "Saved" : "Added"} "${element.name.trim()}".`);
    await this.load();
  }

  /* ---- remove ---- */

  askRemove(element) {
    if (!this.reply || !element || typeof element.id !== "string") return;
    const known = (this.reply.used_by && this.reply.used_by[element.id]) || [];
    this._destroyHandles();
    this.notice = "";
    this.view = { mode: "remove", element, usedBy: Array.isArray(known) ? known : [], inUse: Array.isArray(known) && known.length > 0, busy: false, error: "", code: "" };
    this._drawRemove();
  }

  _usedText(item) {
    if (typeof item === "string") return item;
    if (item && typeof item === "object") {
      for (const k of ["name", "title", "label", "screen", "id"]) if (typeof item[k] === "string" && item[k]) return item[k];
    }
    return "A screen";
  }

  _drawRemove() {
    const v = this.view;
    const body = this.body;
    body.replaceChildren();
    const box = this._t("div", "el-group");
    box.dataset.elements = "remove";
    box.append(this._t("h2", null, `Remove "${v.element.name}"?`));
    if (v.inUse) {
      const w = this._t("p", null, "In use by these screens; remove those first:");
      w.dataset.elements = "in-use";
      const ul = this._t("ul", "el-used");
      for (const u of v.usedBy) ul.append(this._t("li", null, this._usedText(u)));
      box.append(w, ul);
    } else box.append(this._t("p", null, "This removes the element from the list. Nothing else changes."));
    if (v.error) {
      const e = this._t("p", "el-msg err", v.error);
      e.dataset.elements = "remove-error";
      box.append(e);
      if (RELOAD_CODES.includes(v.code)) box.append(this._btn("Reload the list", "reload", async () => { await this.load(); this._leave(""); }));
    }
    const row = this._t("div", "el-buttons");
    if (!v.inUse) {
      // An element a screen uses is never removable from here (the server refuses it): the list above is the whole answer.
      const go = this._btn("Remove", "remove-confirm", () => this._doRemove());
      go.disabled = v.busy;
      row.append(go);
    }
    row.append(this._btn(v.inUse ? "Close" : "Cancel", "cancel", () => this._leave("")));
    box.append(row);
    body.append(box);
  }

  async _doRemove() {
    const v = this.view;
    if (v.mode !== "remove" || v.busy || !this.reply) return;
    v.busy = true;
    v.error = "";
    this._drawRemove();
    try {
      await this.getHass().callWS({ type: "portico/elements/delete", element_id: v.element.id, base_rev: this.reply.rev });
    } catch (err) {
      v.busy = false;
      if (this.view !== v || this.disposed) return;
      v.code = (err && err.code) || "";
      if (v.code === "in_use") { v.inUse = true; v.usedBy = Array.isArray(err.used_by) ? err.used_by : []; v.error = ""; }
      else v.error = errorMessage(err, this.classify);
      this._drawRemove();
      return;
    }
    if (this.disposed) return;
    this._leave(`Removed "${v.element.name}".`);
    await this.load();
  }
}
