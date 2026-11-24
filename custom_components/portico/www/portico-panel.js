/* Portico panel: the editor (M6 E5a; editor design 4.1-4.5).
 *
 * The sidebar page /portico. An administrator gets the editor: an Elements tab (opened first), a Pages tab that edits the household template and each phone's or screen's own pages (blocks of cameras
 * and buttons: a title, an optional door, camera sizes, button icons and palette colours; DEV-658 B3; the document
 * is schema 4 and the panel never sends the server's flat cameras/actions mirror), a Devices tab (the House calls switch, the enrolled phones, and the screens: M6 K3 declares a kiosk screen, its HA user, room cameras, its kiosk address)
 * and a Ring & call tab (M6 K3: the rules that say what a ring shows for each caller), with a live preview,
 * Undo, Discard and Save. A user who is not an administrator gets a read-only view of their own endpoint.
 *
 * The kiosk route (M6 K3, design 5.3): opened at /portico/kiosk/<slug> the panel draws NOTHING but
 * <portico-card endpoint="<slug>" fullpage> (after portico/kiosk/hello {endpoint}); no tabs, no header.
 *
 * Wire (editor commands admin, built in E2): portico/content/document {}, portico/content/preview {document,
 * endpoint, caller?}, portico/content/save {document, base_doc_rev, drop_orphans?}; portico/content/subscribe
 * (content_changed, contract 3; an admin naming no endpoint hears every change); portico/version (the panel
 * expects contract 3 and says so in one line otherwise); portico/kiosk/hello {endpoint} (any user, K1/E2). Nothing is computed twice: the preview is the server's resolved record of the DRAFT, drawn
 * here the way the app draws Home. User text only ever goes in through textContent.
 *
 * The pure part (state, edits, validation, request builders) lives in `logic` and is exported as
 * PorticoPanel.logic so the node tests can reach it; the element below only draws it and calls the socket.
 */

/* ---- constants (mirrors of content.py; the server stays the authority) ------------------------------ */

const MAX_LABEL = 40;
const MAX_PAGES = 12;
const MAX_CAMERAS = 24;          // per page, across its blocks
const MAX_ACTIONS = 24;
const MAX_BLOCKS = 8;            // per page
const MAX_BLOCK_CAMERAS = 12;
const MAX_BLOCK_ACTIONS = 12;
const MAX_TITLE = 40;            // a block's title
const MAX_HOME_NAME = 40;
const SCHEMA_VERSION = 4;        // the document schema this panel speaks (blocks); sent on every save and preview
const SCHEMA_VERSION_REFS = 5;   // ... when the document holds an element reference (the server accepts 4 or 5 and stamps 5 only for refs)
// a minted element id, or a managed Talk element's (`talk-phone-<endpoint>`, `talk-screen-<endpoint>`, the server's elements.ID_RE)
const ELEMENT_ID_RE = /^(?:el-[a-z0-9-]{3,40}|talk-(?:screen|phone)-[A-Za-z0-9_-]{1,128})$/;
const SCHEMA_VERSION_PAGES = 6;  // the unified Pages model (library `pages`, `devices`); the panel edits it only when the loaded document IS schema 6
const MAX_LIBRARY_PAGES = 64;    // pages_model.MAX_LIBRARY_PAGES
const LIBRARY = "@library";      // the editing target of the schema 6 page library (cannot be an endpoint id: ids never hold "@")
const PAGE_KIND_TEXT = { content: "Content", ring: "Ring" };
const WHEN_TEXT = { "": "While ringing and during the call", ring: "Only while ringing", call: "Only during the call" };
const CFG_SHOWN = 5;                         // paths and errors the file-configuration banner lists; the rest is a count
const MIGRATION_BANNER_TEXT = "Pages and Ring & call will be unified: run the migration dry run.";
const MAX_RULES = 12;
const MAX_RULE_BUTTONS = 8;
const MAX_ENDPOINT_ID = 160;
const UNDO_DEPTH = 50;
const PREVIEW_DEBOUNCE_MS = 300;
const SECTION_PREVIEW_CONCURRENCY = 2;  // the Pages tab sections: preview requests in flight at once, over all pages
const REFRESH_MS = 10000;
const SPEC_BATCH = 20;            // portico/cameras/info takes at most this many ids
const SPEC_TTL_MS = 10 * 60 * 1000;   // the server keeps a probed size 10 minutes; so does the panel
const SPEC_RETRY_MS = 60 * 1000;      // after a failed ask, wait this long before asking again
const ID_RE = /^[a-z0-9][a-z0-9_-]{0,31}$/;
const TALK_ID_RE = ELEMENT_ID_RE;           // a Ring page's `talk` is any Talk element id (minted or managed)
const ENTITY_RE = /^[a-z0-9_]+\.[a-z0-9_]+$/;

/* Rules (content.py RULE_KINDS) and the caller the server cannot place ("*" in `callers`). */
const RULE_KINDS = ["kind:door", "kind:phone", "kind:dashboard"];
const RULE_KIND_TEXT = { "kind:door": "Any door", "kind:phone": "Any phone", "kind:dashboard": "Any screen" };
const UNKNOWN_CALLER = "*";

/* The address a kiosk screen is opened at (editor design 5.2, final): "the origin of the tablet's start URL, where
 * its logged-in session lives ... Never an origin other than the tablet's own (a different origin logs it out)".
 * The panel is served by the same HA the tablet logs in to, so the origin is the page's own (`location.origin`),
 * never a constant; kioskUrl() takes it as an argument and accepts only a plain http(s) origin. */
const KIOSK_PREFIX = "/portico/kiosk/";
const ORIGIN_RE = /^https?:\/\/(?:[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?|\[[0-9A-Fa-f:.]+\])(?::[0-9]{1,5})?$/;

/* `talk` acts on the block's own door and has no target of its own; the phone greys it until the gate allows it
 * (the server's talk_off), which the preview and the editor row show as a reason. */
const TALK_KIND_TEXT = "Talk to the door (greyed on the phone until the gate allows it)";

/* kind -> entity domain (null: names a Portico door) and whether the button asks first by default.
 * `call` is not offered for new buttons (M7 places calls); one already stored is shown and kept. */
const KINDS = {
  portico_open: { domain: null, confirm: true, text: "Portico door relay" },
  lock_open: { domain: "lock", confirm: true, text: "Lock: open" },
  lock_lock: { domain: "lock", confirm: false, text: "Lock: lock" },
  lock_unlock: { domain: "lock", confirm: true, text: "Lock: unlock" },
  cover_open: { domain: "cover", confirm: true, text: "Gate or garage: open" },
  cover_close: { domain: "cover", confirm: false, text: "Gate or garage: close" },
  button_press: { domain: "button", confirm: false, text: "Button" },
  script_run: { domain: "script", confirm: false, text: "Script" },
  scene_activate: { domain: "scene", confirm: false, text: "Scene" },
  talk: { domain: null, confirm: false, text: TALK_KIND_TEXT },
};
const NO_CONFIRM_KINDS = new Set(["call", "talk"]);      // never asked, never honoured (CB-9)
const KIND_TEXT = { ...Object.fromEntries(Object.entries(KINDS).map(([k, v]) => [k, v.text])), call: "Call", talk: "Talk to the door" };
const KIND_ICON = {
  portico_open: "mdi:door-open",
  lock_open: "mdi:lock-open-variant",
  button_press: "mdi:gesture-tap-button",
  script_run: "mdi:script-text-play",
  scene_activate: "mdi:palette",
  call: "mdi:phone-in-talk",
  lock_lock: "mdi:lock",
  lock_unlock: "mdi:lock-open-variant",
  cover_open: "mdi:garage-open",
  cover_close: "mdi:garage",
  talk: "mdi:account-voice",
};

/* The words for a Talk button's `reason` (the server's talk_state; the preview resolves it live). */
const TALK_REASONS = {
  talk_off: "Off until the gate allows it",
  no_door: "This block has no door",
  not_connected: "The door is not connected",
  busy: "The door is busy",
  phone_only: "Talk works from a phone only",
};

const CAMERA_SIZES = ["auto", "big", "small"];
/* More than this many live tiles on one page draws a warning (advisory: nothing is capped, the server's own guard is far higher). */
const LIVE_WARN_AFTER = 2;
const LIVE_WARN_TEXT = "Several live pictures can overload the tablet or the camera server; turn on Show frame rate (Settings tab) to see whether a picture lags, " +
  "and take cameras off live if it does";
const LIVE_HINT_TEXT = "Live plays live video in the tile; the still picture stays underneath as the fallback. A viewer can tap a tile to switch it between live and still.";
const SIZE_TEXT = { auto: "Automatic (the first is big, the rest small)", big: "Big (full width)", small: "Small (half width)" };

/* The button palette (contract/palette_v1.json; a node test pins the two equal). Light: a solid fill with white text;
 * dark: a pale fill with dark text. Only these eight names exist: a button's colour is one of them or none. */
const PALETTE = [
  { name: "blue", light: { fill: "#1565C0", text: "#FFFFFF" }, dark: { fill: "#90CAF9", text: "#0D2B4E" } },
  { name: "green", light: { fill: "#2E7D32", text: "#FFFFFF" }, dark: { fill: "#A5D6A7", text: "#0F3A14" } },
  { name: "amber", light: { fill: "#8A5A00", text: "#FFFFFF" }, dark: { fill: "#FFD54F", text: "#3D2A00" } },
  { name: "red", light: { fill: "#C62828", text: "#FFFFFF" }, dark: { fill: "#EF9A9A", text: "#5A0E0E" } },
  { name: "purple", light: { fill: "#6A1B9A", text: "#FFFFFF" }, dark: { fill: "#CE93D8", text: "#3B0F52" } },
  { name: "teal", light: { fill: "#00796B", text: "#FFFFFF" }, dark: { fill: "#80CBC4", text: "#00332D" } },
  { name: "pink", light: { fill: "#AD1457", text: "#FFFFFF" }, dark: { fill: "#F48FB1", text: "#4F0A27" } },
  { name: "grey", light: { fill: "#546E7A", text: "#FFFFFF" }, dark: { fill: "#B0BEC5", text: "#1F2A30" } },
];
const PALETTE_NAMES = PALETTE.map((c) => c.name);
const paletteEntry = (name) => PALETTE.find((c) => c.name === name) || null;

/* Control, line and paragraph separator characters: refused in a title and in the home name (content.py). */
const BAD_TEXT = /[\p{Cc}\p{Zl}\p{Zp}]/u;

/* The icon picker's groups, by name. Every icon lands in exactly one (a test pins that). */
const ICON_GROUPS = [
  ["door", "Door", /^door(?!bell)/],
  ["gate", "Gate", /^(gate|boom-gate|fence)/],
  ["garage", "Garage", /^garage/],
  ["lock", "Lock", /^(lock|key|shield-lock|home-lock)/],
  ["talk", "Talk", /^(account-voice|microphone|phone|volume|speaker)/],
  ["other", "Other", /./],
];
function iconGroup(name) {
  return ICON_GROUPS.find(([, , re]) => re.test(name))[0];
}

/* The icons the app bundles (contract/icons_v1.json; a node test pins the two equal). */
const ICON_NAMES = [
  "door", "door-open", "door-closed", "door-closed-lock", "gate", "gate-open", "garage", "garage-open",
  "fence", "lock", "lock-open", "lock-open-variant", "lock-outline", "lock-open-outline",
  "lock-open-variant-outline", "key", "key-variant", "shield-home", "shield-lock", "home", "home-outline",
  "home-lock", "home-export-outline", "bell", "bell-ring", "alarm-light", "doorbell", "lightbulb",
  "lightbulb-on", "lightbulb-outline", "lightbulb-off", "ceiling-light", "lamp", "floor-lamp",
  "wall-sconce", "led-strip-variant", "light-switch", "toggle-switch", "toggle-switch-off", "power",
  "power-plug", "flash", "thermometer", "thermostat", "fan", "air-conditioner", "radiator", "fire",
  "snowflake", "water-boiler", "weather-sunny", "weather-night", "blinds", "blinds-open", "window-shutter",
  "window-shutter-open", "window-open", "window-closed", "curtains", "curtains-closed", "roller-shade",
  "cctv", "camera", "video", "motion-sensor", "phone", "phone-ring", "phone-hangup", "phone-in-talk",
  "phone-voip", "volume-high", "volume-off", "microphone", "script-text", "play", "play-circle", "palette",
  "movie-open", "sofa", "bed", "sleep", "car", "car-electric", "walk", "exit-run", "water", "water-pump",
  "sprinkler", "sprinkler-variant", "flower", "washing-machine", "television", "speaker", "robot-vacuum",
  "coffee", "gesture-tap-button", "gesture-tap", "checkbox-marked-circle", "cog",
  "door-sliding", "door-sliding-lock", "door-sliding-open", "gate-arrow-left", "gate-arrow-right", "boom-gate",
  "boom-gate-up", "garage-variant", "garage-open-variant", "garage-lock", "lock-smart", "lock-check",
  "lock-clock", "lock-alert", "lock-open-check", "account-voice", "microphone-off", "phone-cancel",
  "doorbell-video",
];

/* ---- the pure part ---------------------------------------------------------------------------------- */

class EditError extends Error {}

const clone = (x) => JSON.parse(JSON.stringify(x));

function canon(x) {
  if (Array.isArray(x)) return x.map(canon);
  if (x && typeof x === "object") {
    const out = {};
    for (const k of Object.keys(x).sort()) out[k] = canon(x[k]);
    return out;
  }
  return x;
}
const same = (a, b) => JSON.stringify(canon(a)) === JSON.stringify(canon(b));

/* A new list with the item at `from` moved to `to` (both clamped to the list). */
function move(list, from, to) {
  const out = list.slice();
  if (from < 0 || from >= out.length) return out;
  const dest = Math.max(0, Math.min(out.length - 1, to));
  const [item] = out.splice(from, 1);
  out.splice(dest, 0, item);
  return out;
}

/* An id from free text, unique among `taken` (the old Configure flow's slug rule). */
function slug(text, taken, fallback) {
  let base = String(text || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 24);
  if (!base) base = fallback || "item";
  let candidate = base;
  for (let n = 2; taken.has(candidate); n++) candidate = `${base}-${n}`;
  return candidate;
}

/* ---- blocks: the document's shape (blocks design 3.1) and the wire rules ------------------------------------ */

/* A page's blocks, its cameras and its buttons of every kind. A page with no `blocks` (a resolved legacy page, an
 * older server's reply) answers with its own flat lists. */
const blocksOf = (page) => (page && Array.isArray(page.blocks) ? page.blocks : []);
const pageCameras = (page) => (page && Array.isArray(page.blocks) ? page.blocks.flatMap((b) => b.cameras || []) : (page && page.cameras) || []);
const pageActions = (page) => (page && Array.isArray(page.blocks) ? page.blocks.flatMap((b) => b.actions || []) : (page && page.actions) || []);

/* The title of the block a page's label becomes: control and line characters turned into spaces, cut to the limit
 * (the server's title_from_label). */
function titleFromLabel(label, pageId) {
  const text = [...String(label == null ? "" : label)].map((c) => (BAD_TEXT.test(c) ? " " : c)).join("").trim();
  return [...text].slice(0, MAX_TITLE).join("").trim() || String(pageId || "Page");
}

/* A page as the editor holds it: `{id, label, blocks}` and nothing else. The flat `cameras`/`actions` the server
 * served beside `blocks` until B8 (an OLDER server still does) are DROPPED: the server since B8 refuses a page that
 * carries them ("update the Portico panel"), so a draft must never carry them. A page that came without `blocks` (an
 * older server) becomes one block named after the page, as the migration does. */
function editorPage(page) {
  const out = {};
  // a Ring page's own `cameras` (null = the caller's cameras, or the one chosen) is not a flat list of the old shape
  for (const [key, value] of Object.entries(page || {})) if (!(key === "actions" || (key === "cameras" && pageKind(page) !== "ring"))) out[key] = clone(value);
  if (!Array.isArray(out.blocks) && pageKind(page) === "ring") out.blocks = [];
  else if (!Array.isArray(out.blocks)) {
    out.blocks = [{ id: out.id, title: titleFromLabel(out.label, out.id), cameras: clone((page && page.cameras) || []), actions: clone((page && page.actions) || []) }];
  }
  return out;
}

/* The document in the shape the editor edits and the wire takes: schema 4, every page `{id, label, blocks}`. A pure
 * copy; `saveRequest` and `previewRequest` send exactly this. */
function editorDocument(doc) {
  if (isPagesModel(doc)) {          // schema 6 stays 6: a save keeps the store's model, only the migration moves it
    const six = clone(doc);
    six.pages = (six.pages || []).map(editorPage);
    return six;
  }
  const out = clone(doc || {});
  out.schema_version = docHasRefs(out) ? SCHEMA_VERSION_REFS : SCHEMA_VERSION;
  if (out.template && Array.isArray(out.template.pages)) out.template.pages = out.template.pages.map(editorPage);
  for (const key of Object.keys(out.overrides || {})) {
    const holder = out.overrides[key];
    if (holder && Array.isArray(holder.pages)) holder.pages = holder.pages.map(editorPage);
  }
  return out;
}

/* ---- element references (DEV-760 P2): a camera tile `{id, element, label?, size?, live?}` has no entity of its own ---- */

const isRef = (tile) => !!tile && typeof tile === "object" && typeof tile.element === "string";
const isElementId = (value) => typeof value === "string" && ELEMENT_ID_RE.test(value);
const isCameraEntity = (value) => typeof value === "string" && ENTITY_RE.test(value) && value.split(".")[0] === "camera";
/* A camera element a screen may use: an entity-backed one (a stream-only camera has no snapshot, live path or grant id). */
const placeableCamera = (element) => !!element && element.kind === "camera" && typeof element.entity_id === "string" && element.entity_id !== "";

/* ---- button references (DEV-760 P3). A button tile `{id, element, label?, show_label?, confirm?}` holds NO action, entity or
 * target: the element's, read by the server at every run. SAFETY: a door, lock or gate button (the kinds whose default is to ask
 * first) can never have ask-first turned off, here or by hand: `confirm` is only ever stored as `true`; the server refuses a
 * `false` and never honours one. ---- */

/* A `call` element's target is the string "<type>:<id>" (type: dashboard, door or phone). */
const parseCallTarget = (value) => {
  const m = typeof value === "string" ? /^(dashboard|door|phone):(.+)$/.exec(value) : null;
  return m ? { type: m[1], id: m[2] } : null;
};
/* A Talk ELEMENT (DEV-518) is placed like a button and counts as the "talk" action; it carries its own door (`options.door`), so
 * unlike a button element with the talk action it does not need the block to have one. */
const isTalkElement = (element) => !!element && element.kind === "talk";
/* The three group words of a Talk element's `options.group` (group-call design G1); a group is no endpoint and no door. */
const TALK_GROUPS = ["everyone", "phones", "panels"];
const isGroupElement = (element) => isTalkElement(element) && !!element.options && TALK_GROUPS.includes(element.options.group);
const GROUP_SCREEN_ONLY = "A group call button can be placed only on a screen's page, not on a phone's";
/* A Talk element on a phone or a screen (managed `talk-phone-<id>` / `talk-screen-<id>`, options.phone / options.screen) stands for the
 * existing `call` action aimed at that phone or screen (the server's `_expand_house_call`); it has no door. */
function houseCallTarget(element) {
  const o = isTalkElement(element) && element.options;
  if (o && TALK_GROUPS.includes(o.group)) return { type: "group", id: o.group };     // a group call (everyone / phones / panels): the server's `options.group`
  if (o && typeof o.phone === "string" && o.phone) return { type: "phone", id: o.phone };
  if (o && typeof o.screen === "string" && o.screen) return { type: "dashboard", id: o.screen };
  return null;
}
const buttonAction = (element) => (houseCallTarget(element) ? "call" : isTalkElement(element) ? "talk" : element && element.kind === "button" && element.options && typeof element.options.action === "string" ? element.options.action : "");
/* A button element a screen may use at all (the block's door, for Talk and a door with no target of its own, is checked at placing). */
function placeableButton(element) {
  if (houseCallTarget(element)) return true;
  if (isTalkElement(element)) return !!(element.options && typeof element.options.door === "string" && element.options.door);
  const action = buttonAction(element);
  if (action === "call") return !!parseCallTarget(element.options.target);
  const def = KINDS[action];
  if (!def) return false;
  return def.domain === null || (typeof element.entity_id === "string" && element.entity_id.split(".")[0] === def.domain);
}
/* Does this action ask first by default (a door, a lock, a gate)? Then a reference may not turn that off. */
const askLocked = (element) => !!(KINDS[buttonAction(element)] && KINDS[buttonAction(element)].confirm);
/* Why this button element cannot go into this block, or "". */
function buttonBlockProblem(element, block) {
  const action = buttonAction(element);
  if (action === "talk" && !isTalkElement(element) && !(block && block.door)) return "Talk to the door needs the block to have a door; pick the block's door first";
  if (action === "portico_open" && !(element.options && element.options.target) && !(block && block.door)) return "That door button names no door and the block has none; pick the block's door first";
  return "";
}
/* The inline button a reference stands for (the server's `expand_action`): used by Detach and to describe a tile. */
function inlineButton(tile, element) {
  const o = element.options || {};
  const house = houseCallTarget(element);
  const kind = house ? "call" : isTalkElement(element) ? "talk" : o.action;
  const out = { id: tile.id, label: "label" in tile ? tile.label : element.name, kind };
  if (kind === "call") out.target = house || parseCallTarget(o.target);
  else if (kind === "talk") { if (isTalkElement(element)) out.door = o.door; /* else: the block's door */ }
  else if (KINDS[kind].domain === null) { if (o.target) out.device_id = o.target; }
  else out.entity_id = element.entity_id;
  if (element.icon) out.icon = element.icon;
  if (o.color) out.color = o.color;
  if (tile.show_label === false && usableIcon(element.icon)) out.show_label = false;
  out.confirm = NO_CONFIRM_KINDS.has(kind) ? false : KINDS[kind].confirm || tile.confirm === true;
  return out;
}

/* Does the document hold an element reference anywhere (a page camera, a rule's cameras, a screen's or phone's cameras)?
 * The panel then sends schema 5; without one it sends 4, so an inline-only document is byte-for-byte what it was. */
function docHasRefs(doc) {
  if (isPagesModel(doc)) return true;      // schema 6 always needs the element list (the Talk selector, placed elements)
  const scopes = [(doc && doc.template) || {}, ...Object.values((doc && doc.overrides) || {})];
  for (const scope of scopes) {
    for (const page of (scope && scope.pages) || []) if (pageCameras(page).some(isRef) || pageActions(page).some(isRef)) return true;
    for (const rule of (scope && scope.rules) || []) if (Array.isArray(rule && rule.cameras) && rule.cameras.some(isElementId)) return true;
  }
  for (const key of ["dashboards", "endpoints"]) {
    for (const d of Object.values((doc && doc[key]) || {})) if (d && Array.isArray(d.cameras) && d.cameras.some(isElementId)) return true;
  }
  return false;
}

/* A stable number for an object (a Map, say) so a string can say "the same one as before". */
const objIds = new WeakMap();
let objIdNext = 0;
function objId(o) {
  if (!o || typeof o !== "object") return 0;
  if (!objIds.has(o)) objIds.set(o, ++objIdNext);
  return objIds.get(o);
}

const isPagesModel = (doc) => !!doc && doc.schema_version === SCHEMA_VERSION_PAGES;
const pageKind = (page) => (page && page.kind === "ring" ? "ring" : "content");
const templateOf = (doc) => (doc && doc.template) || {};
const overrideOf = (doc, id) => ((doc && doc.overrides) || {})[id] || {};

/* The editable page list of a target: "template" or an endpoint id. */
function pagesOf(doc, target) {
  if (target === LIBRARY) return (doc && doc.pages) || [];
  return (target === "template" ? templateOf(doc) : overrideOf(doc, target)).pages || [];
}

function withPages(doc, target, fn) {
  const out = clone(doc);
  if (target === LIBRARY) {
    out.pages = fn(out.pages || []);
  } else if (target === "template") {
    out.template = { pages: [], rules: [], roster: ["*"], ...(out.template || {}) };
    out.template.pages = fn(out.template.pages);
  } else {
    out.overrides = out.overrides || {};
    out.overrides[target] = { pages: [], rules: [], roster_hide: [], ...(out.overrides[target] || {}) };
    out.overrides[target].pages = fn(out.overrides[target].pages);
  }
  return out;
}

/* What an endpoint shows, in the server's merge order: the template's pages with a same-id override page
 * laid over each, then the override's own pages. `src`: "household" (inherited, read-only here), "override"
 * (replaces a household page), "own". For the template every page is "own". */
function effectivePages(doc, target) {
  if (target === LIBRARY || target === "template") return pagesOf(doc, "template").map((page) => ({ page, src: "own" }));
  const mine = pagesOf(doc, target);
  const byId = new Map(mine.map((p) => [p.id, p]));
  const template = pagesOf(doc, "template");
  const ids = new Set(template.map((p) => p.id));
  const out = template.map((tp) => (byId.has(tp.id) ? { page: byId.get(tp.id), src: "override" } : { page: tp, src: "household" }));
  for (const p of mine) if (!ids.has(p.id)) out.push({ page: p, src: "own" });
  return out;
}

function takenPageIds(doc, target) {
  if (target === LIBRARY) return new Set(pagesOf(doc, LIBRARY).map((p) => p.id));
  const taken = new Set(pagesOf(doc, "template").map((p) => p.id));
  for (const p of pagesOf(doc, target)) taken.add(p.id);
  return taken;
}

/* What the clients draw today (contract 3): the phone app shows every page of the template as a tab in its top line
 * (only the selected page is drawn; a swipe moves between pages), and the card shows the pages of its endpoint the
 * same way. With one page there are no tabs. No page is special: an old app that read only the page with id "home"
 * is not supported any more. */
const PAGES_HINT = "The phone app shows every page as a tab in its top line, and the card shows the pages of its endpoint the same way. With one page there are no tabs.";

/* A new page's name when the field is left empty: "Home" while the set has no "home" page yet (a convenience for the
 * first page; the id carries no meaning to the clients). Empty otherwise (a name is then required). */
function defaultNewPageLabel(doc, target) {
  return effectivePages(doc, target).some((e) => e.page.id === "home") ? "" : "Home";
}

/* How the editor names a camera for the owner (rows, aria labels, lists): its stored name, or when that is empty the
 * entity's own friendly name (else the entity id), shown muted. Never written into the stored label. `states` is
 * hass.states, optional. */
function cameraName(camera, states, elements) {
  if (isRef(camera)) {         // a reference: its own name if it has one, else the element's; "Element removed" once that is gone
    if (camera.label) return camera.label;
    const element = elements && elements.get(camera.element);
    return element ? element.name : elements ? "Element removed" : camera.element;
  }
  if (camera.label) return camera.label;
  const st = states && states[camera.entity_id];
  return (st && st.attributes && st.attributes.friendly_name) || camera.entity_id || "";
}

function checkLabel(text, what, allowEmpty) {
  const label = String(text == null ? "" : text).trim();
  if (!label && !allowEmpty) throw new EditError(`${what} needs a name`);
  if (label.length > MAX_LABEL) throw new EditError(`${what}'s name is longer than ${MAX_LABEL} characters`);
  return label;
}

function addPage(doc, target, labelText) {
  const label = checkLabel(labelText, "A page");
  if (pagesOf(doc, target).length >= MAX_PAGES) throw new EditError(`At most ${MAX_PAGES} pages`);
  const id = slug(label, takenPageIds(doc, target), "page");
  // a new page starts with one block named after it (what a migrated v3 page is), so the first camera has a home
  const block = { id: slug(label, new Set(), "block"), title: titleFromLabel(label, id), cameras: [], actions: [] };
  const out = withPages(doc, target, (pages) => [...pages, { id, label, blocks: [block] }]);
  return { doc: out, pageId: id };
}

function findPage(doc, target, pageId) {
  const page = pagesOf(doc, target).find((p) => p.id === pageId);
  if (!page) throw new EditError("That page is not editable here");
  return page;
}

function renamePage(doc, target, pageId, labelText) {
  const label = checkLabel(labelText, "A page");
  findPage(doc, target, pageId);
  return withPages(doc, target, (pages) => pages.map((p) => (p.id === pageId ? { ...p, label } : p)));
}

function deletePage(doc, target, pageId) {
  findPage(doc, target, pageId);
  return withPages(doc, target, (pages) => pages.filter((p) => p.id !== pageId));
}

/* Move a page one place up (-1) or down (+1) among the pages the target owns. On an endpoint, a page that
 * replaces a household page keeps the household's place, so only its own pages move. */
function movePage(doc, target, pageId, delta) {
  const templateIds = new Set(pagesOf(doc, "template").map((p) => p.id));
  return withPages(doc, target, (pages) => {
    const movable = pages.map((p, i) => i).filter((i) => target === "template" || target === LIBRARY || !templateIds.has(pages[i].id));
    const at = movable.findIndex((i) => pages[i].id === pageId);
    if (at < 0) return pages;
    const order = move(movable.map((i) => pages[i]), at, at + delta);
    const out = pages.slice();
    movable.forEach((slot, n) => { out[slot] = order[n]; });
    return out;
  });
}

/* Copy a household page into the endpoint's own pages under the same id (it then replaces the household one). */
function overridePage(doc, target, pageId) {
  const source = pagesOf(doc, "template").find((p) => p.id === pageId);
  if (!source || target === "template") throw new EditError("Only a household page can be overridden");
  if (pagesOf(doc, target).some((p) => p.id === pageId)) return doc;
  return withPages(doc, target, (pages) => [...pages, clone(source)]);
}

function editPage(doc, target, pageId, fn) {
  findPage(doc, target, pageId);
  return withPages(doc, target, (pages) => pages.map((p) => (p.id === pageId ? fn(clone(p)) : p)));
}

/* Every button id anywhere in the document (template and every override). A press names a button by id alone, so a new
 * button's id is made unique across the WHOLE document, not just its page. */
function allActionIds(doc) {
  const ids = new Set();
  const holders = [templateOf(doc), { pages: (doc && doc.pages) || [] }, ...Object.values((doc && doc.overrides) || {})];
  for (const holder of holders) for (const page of (holder && holder.pages) || []) for (const a of pageActions(page)) ids.add(a.id);
  return ids;
}

/* All the ids of a page's cameras and buttons: ids are unique per page, across its blocks (D2). */
function tileIds(page) {
  return new Set([...pageCameras(page), ...pageActions(page)].map((t) => t.id));
}

function blockIds(page) {
  return new Set(blocksOf(page).map((b) => b.id));
}

const usableIcon = (icon) => typeof icon === "string" && ICON_NAMES.includes(icon.replace(/^mdi:/, ""));

function checkTitle(text, what) {
  const title = String(text == null ? "" : text).trim();
  if (!title) throw new EditError(`${what} needs a title`);
  if ([...title].length > MAX_TITLE) throw new EditError(`${what}'s title is longer than ${MAX_TITLE} characters`);
  if (BAD_TEXT.test(title)) throw new EditError(`${what}'s title must not contain control characters or line breaks`);
  return title;
}

/* Two blocks of a page may not share a title (the picker would show two "Garden"); case does not count. */
function checkTitleFree(page, title, exceptId) {
  const folded = title.toLocaleLowerCase();
  if (blocksOf(page).some((b) => b.id !== exceptId && String(b.title).toLocaleLowerCase() === folded)) {
    throw new EditError(`Another block on this page is already called "${title}"`);
  }
}

function findBlock(page, blockId) {
  const block = blocksOf(page).find((b) => b.id === blockId);
  if (!block) throw new EditError("That block is gone");
  return block;
}

/* The block a new item goes into: the one named, else the page's first. */
function pickBlock(page, blockId) {
  if (blockId) return findBlock(page, blockId);
  const first = blocksOf(page)[0];
  if (!first) throw new EditError("Add a block to this page first");
  return first;
}

/* ---- blocks ---- */

function addBlock(doc, target, pageId, titleText) {
  const title = checkTitle(titleText, "A block");
  let id = "";
  const out = editPage(doc, target, pageId, (page) => {
    if (blocksOf(page).length >= MAX_BLOCKS) throw new EditError(`At most ${MAX_BLOCKS} blocks on a page`);
    checkTitleFree(page, title, null);
    id = slug(title, blockIds(page), "block");
    page.blocks.push({ id, title, cameras: [], actions: [] });
    return page;
  });
  return { doc: out, blockId: id };
}

function renameBlock(doc, target, pageId, blockId, titleText) {
  const title = checkTitle(titleText, "A block");
  return editPage(doc, target, pageId, (page) => {
    checkTitleFree(page, title, blockId);
    findBlock(page, blockId).title = title;
    return page;
  });
}

function moveBlock(doc, target, pageId, blockId, delta) {
  return editPage(doc, target, pageId, (page) => {
    findBlock(page, blockId);
    const at = page.blocks.findIndex((b) => b.id === blockId);
    page.blocks = move(page.blocks, at, at + delta);
    return page;
  });
}

/* The block and its cameras and buttons go. A rule that names one of its buttons is then a problem `validate` reports
 * (the server refuses that save too), exactly as for a button removed on its own. */
function deleteBlock(doc, target, pageId, blockId) {
  return editPage(doc, target, pageId, (page) => {
    findBlock(page, blockId);
    page.blocks = page.blocks.filter((b) => b.id !== blockId);
    return page;
  });
}

/* A block's door: a Portico device id, or null/"" for none. */
function setBlockDoor(doc, target, pageId, blockId, door) {
  const value = door == null ? "" : String(door);
  if (value.length > 128) throw new EditError("That door's id is too long");
  return editPage(doc, target, pageId, (page) => {
    const block = findBlock(page, blockId);
    if (value) block.door = value; else delete block.door;
    return page;
  });
}

/* The door-less `portico_open` buttons act on their block's door; moving one to another block would change what it
 * opens, so the door is written into the button first. */
function pinDoor(tile, fromBlock) {
  if (tile.kind === "portico_open" && !tile.device_id && fromBlock.door) tile.device_id = fromBlock.door;
}

function moveTileToBlock(doc, target, pageId, listName, tileId, toBlockId) {
  return editPage(doc, target, pageId, (page) => {
    const from = blocksOf(page).find((b) => (b[listName] || []).some((t) => t.id === tileId));
    if (!from) throw new EditError("That item is gone");
    const to = findBlock(page, toBlockId);
    if (to.id === from.id) return page;
    const limit = listName === "cameras" ? MAX_BLOCK_CAMERAS : MAX_BLOCK_ACTIONS;
    if (to[listName].length >= limit) {
      throw new EditError(`At most ${limit} ${listName === "cameras" ? "cameras" : "buttons"} in a block`);
    }
    const tile = from[listName].find((t) => t.id === tileId);
    from[listName] = from[listName].filter((t) => t.id !== tileId);
    if (listName === "actions") pinDoor(tile, from);
    to[listName].push(tile);
    return page;
  });
}

/* The "Split this block" prompt: the cameras and the buttons past the block limit move, in order, to a new block right
 * after it. The new block has the same door, so a door-less open-door button still opens the same door. */
function splitBlock(doc, target, pageId, blockId) {
  let newId = "";
  const out = editPage(doc, target, pageId, (page) => {
    const block = findBlock(page, blockId);
    if (block.cameras.length <= MAX_BLOCK_CAMERAS && block.actions.length <= MAX_BLOCK_ACTIONS) {
      throw new EditError("This block is within the limits; nothing to split");
    }
    if (page.blocks.length >= MAX_BLOCKS) throw new EditError(`At most ${MAX_BLOCKS} blocks on a page; delete or merge one first`);
    const titles = new Set(page.blocks.map((b) => String(b.title).toLocaleLowerCase()));
    let n = 2;
    const part = () => `${[...block.title].slice(0, MAX_TITLE - String(n).length - 1).join("")} ${n}`;
    while (titles.has(part().toLocaleLowerCase())) n++;
    const created = { id: slug(part(), blockIds(page), "block"), title: part(), cameras: block.cameras.splice(MAX_BLOCK_CAMERAS),
                      actions: block.actions.splice(MAX_BLOCK_ACTIONS) };
    if (block.door) created.door = block.door;
    newId = created.id;
    page.blocks.splice(page.blocks.findIndex((b) => b.id === blockId) + 1, 0, created);
    return page;
  });
  return { doc: out, blockId: newId };
}

/* Blocks over the per-block limit, anywhere in the document: [{owner, pageId, pageLabel, blockId, title, cameras,
 * actions}]. The save would be refused for each; the editor offers to split them. */
function overflowBlocks(doc) {
  const out = [];
  const owners = [["template", templateOf(doc)], [LIBRARY, { pages: (doc && doc.pages) || [] }], ...Object.entries((doc && doc.overrides) || {})];
  for (const [owner, holder] of owners) {
    for (const page of (holder && holder.pages) || []) {
      for (const block of blocksOf(page)) {
        const cams = (block.cameras || []).length;
        const acts = (block.actions || []).length;
        if (cams > MAX_BLOCK_CAMERAS || acts > MAX_BLOCK_ACTIONS) {
          out.push({ owner, pageId: page.id, pageLabel: page.label || page.id, blockId: block.id, title: block.title, cameras: cams, actions: acts });
        }
      }
    }
  }
  return out;
}

/* ---- cameras and buttons ---- */

/* A Ring page holds buttons only (its one camera is chosen on the page) and never a Talk element: a ring is not the moment to start a talk. */
function ringNoCamera(page) {
  if (pageKind(page) === "ring") throw new EditError("A Ring page's camera is chosen on the page, not in a block; its blocks hold buttons only");
}
function ringNoTalk(page) {
  if (pageKind(page) === "ring") throw new EditError("A Ring page cannot hold a Talk button; a ring is not the moment to start a talk");
}

function addCamera(doc, target, pageId, entityId, labelText, blockId) {
  if (!ENTITY_RE.test(entityId || "") || entityId.split(".")[0] !== "camera") throw new EditError("Pick a camera");
  const label = checkLabel(labelText, "A camera", true);   // a camera's name may be empty: it then draws no name
  return editPage(doc, target, pageId, (page) => {
    ringNoCamera(page);
    const block = pickBlock(page, blockId);
    if (block.cameras.length >= MAX_BLOCK_CAMERAS) throw new EditError(`At most ${MAX_BLOCK_CAMERAS} cameras in a block`);
    if (pageCameras(page).length >= MAX_CAMERAS) throw new EditError(`At most ${MAX_CAMERAS} cameras on a page`);
    block.cameras.push({ id: slug(entityId.split(".")[1], tileIds(page), "camera"), label, entity_id: entityId });
    return page;
  });
}

/* Place an element (a camera element, entity-backed) on a block: `{id, element, label?, size?, live?}`. The label is kept
 * only when it differs from the element's name (so renaming the element renames the tile); an empty label is kept (no name). */
function addCameraRef(doc, target, pageId, element, spec, blockId) {
  if (!element || !isElementId(element.id)) throw new EditError("Pick an element");
  if (!placeableCamera(element)) throw new EditError("That camera element has no Home Assistant camera behind it (stream only), so it cannot be placed on a screen");
  const wanted = spec && spec.label != null ? checkLabel(spec.label, "A camera", true) : null;
  return editPage(doc, target, pageId, (page) => {
    ringNoCamera(page);
    const block = pickBlock(page, blockId);
    if (block.cameras.length >= MAX_BLOCK_CAMERAS) throw new EditError(`At most ${MAX_BLOCK_CAMERAS} cameras in a block`);
    if (pageCameras(page).length >= MAX_CAMERAS) throw new EditError(`At most ${MAX_CAMERAS} cameras on a page`);
    const tile = { id: slug(element.name, tileIds(page), "camera"), element: element.id };
    if (wanted !== null && wanted !== element.name) tile.label = wanted;
    if (spec && CAMERA_SIZES.includes(spec.size) && spec.size !== "auto") tile.size = spec.size;
    if (spec && spec.live === true) tile.live = true;
    block.cameras.push(tile);
    return page;
  });
}

/* "Detach": the reference becomes an inline camera, a copy of the element's fields with this tile's overrides on top, the
 * same item id (so rules, grants and phone caches do not notice). Mirrors the server's expansion. */
function detachCamera(doc, target, pageId, tileId, element) {
  return editPage(doc, target, pageId, (page) => {
    const { block, tile } = findTile(page, "cameras", tileId);
    if (!isRef(tile)) throw new EditError("That camera is not from an element");
    if (!placeableCamera(element) || element.id !== tile.element) throw new EditError("The element is gone; remove this tile or choose another element");
    const inline = { id: tile.id, label: "label" in tile ? tile.label : element.name, entity_id: element.entity_id };
    const size = tile.size || (element.options && element.options.size);
    if (CAMERA_SIZES.includes(size) && size !== "auto") inline.size = size;
    if (tile.live === true) inline.live = true;
    block.cameras = block.cameras.map((c) => (c.id === tileId ? inline : c));
    return page;
  });
}

/* "Choose another" on a tile whose element was removed: point it at another camera element, keeping its overrides. */
function repointCamera(doc, target, pageId, tileId, element) {
  if (!element || !isElementId(element.id) || !placeableCamera(element)) throw new EditError("Pick a camera element");
  return editPage(doc, target, pageId, (page) => {
    const { tile } = findTile(page, "cameras", tileId);
    if (!isRef(tile)) throw new EditError("That camera is not from an element");
    tile.element = element.id;
    return page;
  });
}

/* Place a button element on a block: `{id, element, label?, show_label?, confirm?}`. Safety: `confirm` is stored only as true, and
 * never for an action that already asks first. The label is kept only when it differs from the element's name. */
/* A group call button may sit only on a SCREEN's page (HC-9; the server refuses the save with the same words). Schema 6: no device that is
 * not a declared screen may show the library page. An older document: the template (it reaches phones) or an override of a key that is no screen. */
/* The panels' phone ids, set by the editor from the endpoint rows (`panel: true`): a panel owns its pages like a phone but is a screen for
 * group calls (own-pages design), so a group call button may sit on its pages. Module state, because the pure edits that place a button are
 * reached from many callers; the editor sets it on every render. */
let PANEL_HOSTS = [];
const setPanelHosts = (list) => { PANEL_HOSTS = Array.isArray(list) ? list : []; };
/* The device keys no endpoint answers to ("Not attached", from `content/document`'s `orphans`): a leftover entry, e.g. a former panel screen,
 * shows nothing on any phone, so it is no phone to refuse a group call button for (the server skips it the same way). */
let LEFTOVER_KEYS = [];
const setLeftoverKeys = (list) => { LEFTOVER_KEYS = Array.isArray(list) ? list : []; };

function groupOnlyOnScreens(doc, target, pageId) {
  const screens = (doc && doc.dashboards) || {};
  const isPanel = (key) => PANEL_HOSTS.some((p) => p.id === key);
  if (target === LIBRARY) {
    const phones = Object.entries((doc && doc.devices) || {}).filter(([key, d]) => !screens[key] && !isPanel(key) && !LEFTOVER_KEYS.includes(key) && ((d && d.pages) || []).some((e) => e && e.page === pageId));
    if (phones.length) throw new EditError(`${GROUP_SCREEN_ONLY} (this page is on ${phones.map(([key]) => key).join(", ")})`);
  } else if (target === "template" || !(screens[target] || isPanel(target))) throw new EditError(GROUP_SCREEN_ONLY);
}

function addButtonRef(doc, target, pageId, element, spec, blockId) {
  if (!element || !isElementId(element.id)) throw new EditError("Pick an element");
  if (!placeableButton(element)) throw new EditError("That button element cannot be placed on a screen (its action, entity or target is missing)");
  const wanted = spec && spec.label != null ? checkLabel(spec.label, "A button") : null;
  return editPage(doc, target, pageId, (page) => {
    if (isTalkElement(element)) ringNoTalk(page);
    if (isGroupElement(element)) groupOnlyOnScreens(doc, target, pageId);
    const block = pickBlock(page, blockId);
    const problem = buttonBlockProblem(element, block);
    if (problem) throw new EditError(problem);
    if (block.actions.length >= MAX_BLOCK_ACTIONS) throw new EditError(`At most ${MAX_BLOCK_ACTIONS} buttons in a block`);
    if (pageActions(page).length >= MAX_ACTIONS) throw new EditError(`At most ${MAX_ACTIONS} buttons on a page`);
    const tile = { id: slug(element.name, new Set([...tileIds(page), ...allActionIds(doc)]), "button"), element: element.id };
    if (wanted !== null && wanted !== element.name) tile.label = wanted;
    if (spec && spec.show_label === false) {
      if (!usableIcon(element.icon)) throw new EditError("Pick an element with an icon before hiding the text: the button needs something to show");
      tile.show_label = false;
    }
    if (spec && spec.confirm === true && !askLocked(element) && !NO_CONFIRM_KINDS.has(buttonAction(element))) tile.confirm = true;
    block.actions.push(tile);
    return page;
  });
}

/* "Detach" a button: an inline copy of the element's fields with this tile's overrides on top, the same id. */
function detachButton(doc, target, pageId, tileId, element) {
  return editPage(doc, target, pageId, (page) => {
    const { block, tile } = findTile(page, "actions", tileId);
    if (!isRef(tile)) throw new EditError("That button is not from an element");
    if (!placeableButton(element) || element.id !== tile.element) throw new EditError("The element is gone; remove this tile or choose another element");
    if (isGroupElement(element)) throw new EditError("A group call button stays tied to its element: it cannot be detached into a button of its own");
    const problem = buttonBlockProblem(element, block);
    if (problem) throw new EditError(problem);
    block.actions = block.actions.map((a) => (a.id === tileId ? inlineButton(tile, element) : a));
    return page;
  });
}

/* "Choose another" for a button whose element was removed: point it at another button element, keeping its overrides. A
 * stored ask-first stays as it is (only ever true); the new action's own default still applies. */
function repointButton(doc, target, pageId, tileId, element) {
  if (!element || !isElementId(element.id) || !placeableButton(element)) throw new EditError("Pick a button element");
  return editPage(doc, target, pageId, (page) => {
    const { block, tile } = findTile(page, "actions", tileId);
    if (!isRef(tile)) throw new EditError("That button is not from an element");
    const problem = buttonBlockProblem(element, block);
    if (problem) throw new EditError(problem);
    if (isGroupElement(element)) groupOnlyOnScreens(doc, target, pageId);
    tile.element = element.id;
    return page;
  });
}

/* spec: {kind, target (an entity id, or a door's device id for portico_open; empty = the block's door), label, icon?,
 * confirm, color?, show_label?, blockId?}. */
function addAction(doc, target, pageId, spec) {
  const kind = KINDS[spec.kind];
  if (!kind) throw new EditError("Pick what the button does");
  const label = checkLabel(spec.label, "A button");
  const tile = { id: "", label, kind: spec.kind };
  if (kind.domain !== null) {
    if (!ENTITY_RE.test(spec.target || "") || spec.target.split(".")[0] !== kind.domain) throw new EditError(`Pick a ${kind.domain}`);
    tile.entity_id = spec.target;
  }
  if (spec.icon && spec.icon !== "none") tile.icon = spec.icon;
  tile.confirm = NO_CONFIRM_KINDS.has(spec.kind) ? false : !!spec.confirm;     // a Talk press is never asked first
  if (spec.color && spec.color !== "default") tile.color = checkColor(spec.color);
  if (spec.show_label === false) tile.show_label = false;
  checkTextHidden(tile);
  return editPage(doc, target, pageId, (page) => {
    if (spec.kind === "talk") ringNoTalk(page);
    const block = pickBlock(page, spec.blockId);
    if (spec.kind === "talk") {
      if (!block.door) throw new EditError("Pick the block's door first: Talk to the door needs it");     // it talks to the block's door, no other
    } else if (kind.domain === null) {
      if (spec.target) tile.device_id = spec.target;
      else if (!block.door) throw new EditError("Pick the door");
    }
    if (block.actions.length >= MAX_BLOCK_ACTIONS) throw new EditError(`At most ${MAX_BLOCK_ACTIONS} buttons in a block`);
    if (pageActions(page).length >= MAX_ACTIONS) throw new EditError(`At most ${MAX_ACTIONS} buttons on a page`);
    tile.id = slug(label, new Set([...tileIds(page), ...allActionIds(doc)]), "button");
    block.actions.push(tile);
    return page;
  });
}

function checkColor(color) {
  if (!PALETTE_NAMES.includes(color)) throw new EditError(`A colour is one of: ${PALETTE_NAMES.join(", ")}`);
  return color;
}

function checkTextHidden(tile) {
  if (tile.show_label === false && !usableIcon(tile.icon)) {
    throw new EditError("Pick an icon from the list before hiding the text: the button needs something to show");
  }
}

/* The tile `tileId` of `listName` in any block of the page, with its block. */
function findTile(page, listName, tileId) {
  for (const block of blocksOf(page)) {
    const tile = (block[listName] || []).find((t) => t.id === tileId);
    if (tile) return { block, tile };
  }
  throw new EditError("That item is gone");
}

/* patch on a camera: {label?, size? ("auto" removes it), live? (true/false; false removes it)}; on a button: {label?, icon? ("none" removes it), confirm?,
 * color? (a palette name; "default"/null removes it), show_label? (true removes it: shown is the default)}. */
function updateTile(doc, target, pageId, listName, tileId, patch, element) {
  return editPage(doc, target, pageId, (page) => {
    const { tile } = findTile(page, listName, tileId);
    if ("label" in patch) {
      if (patch.label === null && isRef(tile)) delete tile.label;      // follow the element's name again
      else tile.label = checkLabel(patch.label, listName === "cameras" ? "A camera" : "A button", listName === "cameras");
    }
    if (listName === "actions" && isRef(tile)) {       // a button reference: label, show text and ask-first only
      if ("show_label" in patch) {
        if (patch.show_label === false) {
          if (!usableIcon(element && element.icon)) throw new EditError("Pick an icon from the list before hiding the text: the button needs something to show");
          tile.show_label = false;
        } else delete tile.show_label;
      }
      if ("confirm" in patch) {      // tighten only: stored as true or not at all, never false, and never on a door, lock or gate action
        if (patch.confirm === true && !(element && (askLocked(element) || NO_CONFIRM_KINDS.has(buttonAction(element))))) tile.confirm = true;
        else delete tile.confirm;
      }
      return page;
    }
    if (listName === "cameras") {
      if ("size" in patch) {
        if (!CAMERA_SIZES.includes(patch.size)) throw new EditError(`A size is one of: ${CAMERA_SIZES.join(", ")}`);
        if (patch.size === "auto") delete tile.size; else tile.size = patch.size;
      }
      if ("live" in patch) {
        if (typeof patch.live !== "boolean") throw new EditError("Live is on or off");
        if (patch.live) tile.live = true; else delete tile.live;       // additive: stored only when on, like the server
      }
      return page;
    }
    if ("icon" in patch) { if (patch.icon && patch.icon !== "none") tile.icon = patch.icon; else delete tile.icon; }
    if ("confirm" in patch && !NO_CONFIRM_KINDS.has(tile.kind)) tile.confirm = !!patch.confirm;
    if ("color" in patch) { if (patch.color && patch.color !== "default") tile.color = checkColor(patch.color); else delete tile.color; }
    if ("show_label" in patch) { if (patch.show_label === false) tile.show_label = false; else delete tile.show_label; }
    checkTextHidden(tile);
    return page;
  });
}

const setCameraSize = (doc, target, pageId, cameraId, size) => updateTile(doc, target, pageId, "cameras", cameraId, { size });
const setCameraLive = (doc, target, pageId, cameraId, live) => updateTile(doc, target, pageId, "cameras", cameraId, { live });
const setButtonColor = (doc, target, pageId, actionId, color) => updateTile(doc, target, pageId, "actions", actionId, { color });
const setShowLabel = (doc, target, pageId, actionId, show) => updateTile(doc, target, pageId, "actions", actionId, { show_label: !!show });

function removeTile(doc, target, pageId, listName, tileId) {
  return editPage(doc, target, pageId, (page) => {
    const { block } = findTile(page, listName, tileId);
    block[listName] = block[listName].filter((t) => t.id !== tileId);
    return page;
  });
}

/* One place up (-1) or down (+1) inside its own block; moveTileToBlock changes the block. */
function moveTile(doc, target, pageId, listName, tileId, delta) {
  return editPage(doc, target, pageId, (page) => {
    const { block } = findTile(page, listName, tileId);
    const at = block[listName].findIndex((t) => t.id === tileId);
    block[listName] = move(block[listName], at, at + delta);
    return page;
  });
}

/* The home name (`document.home_name`, up to MAX_HOME_NAME characters; "" removes it). There is no command of its own:
 * since the Configure step went (E7) it is a field of the document, saved with it. */
function setHomeName(doc, text) {
  const name = String(text == null ? "" : text).trim();
  if ([...name].length > MAX_HOME_NAME) throw new EditError(`The home name is longer than ${MAX_HOME_NAME} characters`);
  if (BAD_TEXT.test(name)) throw new EditError("The home name must not contain control characters or line breaks");
  const out = clone(doc);
  if (name) out.home_name = name; else delete out.home_name;
  return out;
}

/* Show the frame rate on live tiles (`document.show_fps`, a boolean stored only when true, like the server). */
function setShowFps(doc, on) {
  const out = clone(doc);
  if (on) out.show_fps = true; else delete out.show_fps;
  return out;
}

/* How many cameras of a page are set live, and whether that is more than the advisory limit. */
const liveCount = (page) => pageCameras(page).filter((c) => c && c.live === true).length;
const tooManyLive = (page) => liveCount(page) > LIVE_WARN_AFTER;

/* ---- "Not attached" overrides (the server's _merge_moved, mirrored) ---- */

function freshId(wanted, taken) {
  for (let n = 2; ; n++) {
    const candidate = `${wanted.slice(0, 28)}-${n}`;
    if (!taken.has(candidate)) return candidate;
  }
}

/* The orphan's pages and rules added after the target's: an id already taken arrives under a fresh one
 * (`home-2`, label marked "(moved)"); nothing of the target is replaced. `target` is "template" or an endpoint
 * id. The orphan key leaves `overrides`. */
function mergeOrphan(doc, key, target) {
  const orphan = (doc.overrides || {})[key];
  if (!orphan) throw new EditError("That item is not in the document");
  const out = clone(doc);
  delete out.overrides[key];
  const holder = target === "template"
    ? (out.template = { pages: [], rules: [], roster: ["*"], ...(out.template || {}) })
    : ((out.overrides[target] = { pages: [], rules: [], roster_hide: [], ...(out.overrides[target] || {}) }));
  const pages = holder.pages.slice();
  const taken = new Set(pages.map((p) => p.id));
  for (let page of orphan.pages || []) {
    if (taken.has(page.id)) {
      page = { ...page, id: freshId(page.id, taken), label: `${page.label || page.id} (moved)`.slice(0, MAX_LABEL) };
    }
    taken.add(page.id);
    pages.push(clone(page));
  }
  const rules = (holder.rules || []).slice();
  const takenRules = new Set(rules.map((r) => r.id));
  for (let rule of orphan.rules || []) {
    if (takenRules.has(rule.id)) rule = { ...rule, id: freshId(rule.id, takenRules) };
    takenRules.add(rule.id);
    rules.push(clone(rule));
  }
  if (pages.length > MAX_PAGES || rules.length > 12) throw new EditError(`Moving it would pass the limit of ${MAX_PAGES} pages and 12 rules`);
  holder.pages = pages;
  holder.rules = rules;
  if (target !== "template") {
    holder.roster_hide = [...new Set([...(holder.roster_hide || []), ...(orphan.roster_hide || [])])];
  }
  return out;
}

/* ---- the schema 6 page library (Pages unified, P-B): kinds, Ring pages, "shown on", the delete guard ---- */

/* The devices that use a library page: `[{id, via: "pages"}]` (it is in the device's page list). A Ring page belongs to its Talk element, not to a device. */
function devicesUsingPage(doc, pageId) {
  const out = [];
  for (const [id, device] of Object.entries((doc && doc.devices) || {})) {
    if (LEFTOVER_KEYS.includes(id)) continue;     // a "Not attached" entry shows nothing: it is no user of a page (the server agrees)
    if (((device && device.pages) || []).some((e) => e && e.page === pageId)) out.push({ id, via: "pages" });
  }
  return out;
}

/* "Shown on: Alice's phone, Kitchen screen" (design option 12a), computed from the devices, never stored; "" when none. */
function shownOnText(doc, pageId, nameOf) {
  const names = devicesUsingPage(doc, pageId).map((d) => (nameOf ? nameOf(d.id) : d.id));
  return names.length ? `Shown on: ${names.join(", ")}` : "Not shown on any device yet";
}

function addLibraryPage(doc, kind, labelText, talk) {
  if (kind === "ring") throw new EditError("A Ring page appears by itself for each Talk element; press Edit on its entry");
  if (kind !== "content") throw new EditError("Choose the kind of the page first");
  const label = checkLabel(labelText, "A page");
  if (pagesOf(doc, LIBRARY).length >= MAX_LIBRARY_PAGES) throw new EditError(`At most ${MAX_LIBRARY_PAGES} pages`);
  const id = slug(label, takenPageIds(doc, LIBRARY), "page");
  const block = { id: slug(label, new Set(), "block"), title: titleFromLabel(label, id), cameras: [], actions: [] };
  const page = { id, label, kind, blocks: [block] };
  return { doc: withPages(doc, LIBRARY, (pages) => [...pages, page]), pageId: id };
}

/* Delete a library page. A page a device shows is refused and the devices are named (design option 6a/13a). */
function deleteLibraryPage(doc, pageId, nameOf) {
  findPage(doc, LIBRARY, pageId);
  const using = devicesUsingPage(doc, pageId);
  if (using.length) {
    const names = using.map((d) => (nameOf ? nameOf(d.id) : d.id)).join(", ");
    throw new EditError(`This page is still used by ${names}. Take it off ${using.length === 1 ? "that device" : "those devices"} on the Devices tab first.`);
  }
  const next = withPages(doc, LIBRARY, (pages) => pages.filter((p) => p.id !== pageId));
  // The leftover entries keep their other data; only the reference to the deleted page goes (the server prunes the same way).
  const devices = { ...((next && next.devices) || {}) };
  for (const key of LEFTOVER_KEYS) {
    const d = devices[key];
    if (!d) continue;
    devices[key] = { ...d, pages: (d.pages || []).filter((e) => !(e && e.page === pageId)) };
  }
  return { ...next, devices };
}

/* ---- Devices: the Pages and Ring pages selectors, and who owns a Talk element (Pages unified P-C, Devices D2) ---- */

/* `{talk element id: [party kind, party id]}` from the element list (a Map or an array): a Talk element names one of options.door,
 * options.phone, options.screen. */
function talkPartiesOf(elements) {
  const out = new Map();
  const list = elements ? (elements instanceof Map ? [...elements.values()] : Array.isArray(elements) ? elements : Object.values(elements)) : [];
  for (const e of list) {
    if (!isTalkElement(e)) continue;
    const o = e.options || {};
    for (const kind of ["door", "phone", "screen"]) if (typeof o[kind] === "string" && o[kind]) { out.set(e.id, [kind, o[kind]]); break; }
  }
  return out;
}

/* The Talk element ids a device's own content pages hold (as references in a block's buttons), in order, once each. */
function talkIdsOnDevice(doc, key, parties) {
  const byId = new Map(pagesOf(doc, LIBRARY).map((p) => [p.id, p]));
  const out = [];
  for (const entry of ((doc.devices || {})[key] || {}).pages || []) {
    const page = byId.get(entry && entry.page);
    if (!page || pageKind(page) !== "content") continue;
    for (const block of page.blocks || []) {
      for (const item of block.actions || []) {
        if (item && typeof item.element === "string" && parties.has(item.element) && !out.includes(item.element)) out.push(item.element);
      }
    }
  }
  return out;
}

/* The device keys that can own a door Talk element: the declared screens (bar a FORMER panel screen, whose login is a panel's) plus every
 * panel's phone id. `panels` is `[{id, user}]` (the panel phones, from the endpoint rows); none = as before. Mirrors
 * pages_ownership.owner_ids on the server. */
function ownerKeys(doc, panels) {
  const users = new Set((panels || []).map((p) => p.user));
  const keys = Object.entries((doc && doc.dashboards) || {}).filter(([, d]) => !(d && users.has(d.ha_user_id))).map(([slug]) => slug);
  for (const p of panels || []) if (!keys.includes(p.id)) keys.push(p.id);
  return keys;
}

/* `{element id: [owner key...]}` for each DOOR Talk element on the own pages of a screen or a panel (sorted keys). Mirrors
 * pages_ownership.door_owners on the server. */
function doorOwners(doc, parties, panels) {
  const owners = {};
  for (const slug of ownerKeys(doc, panels)) {
    for (const id of talkIdsOnDevice(doc, slug, parties)) {
      if ((parties.get(id) || [])[0] === "door") (owners[id] = owners[id] || []).push(slug);
    }
  }
  for (const id of Object.keys(owners)) owners[id].sort();
  return owners;
}

/* What a screen wakes on, from the draft: `{elements: [door Talk element ids], own: [its own call element ids]}`.
 * A door Talk element on several screens wakes each of them (D-20261122-42b1). */
function wakeOf(doc, key, parties, panels) {
  const owners = doorOwners(doc, parties, panels);
  const on = talkIdsOnDevice(doc, key, parties).filter((id) => (parties.get(id) || [])[0] === "door" && (owners[id] || []).includes(key));      // a former panel screen owns nothing
  return {
    elements: on,
    own: [...parties].filter(([, [kind, ref]]) => kind === "screen" && ref === key).map(([id]) => id).sort(),
  };
}

/* "Wakes on: Front door, Hall screen (its own call element)", or "Wakes on: nothing (no Talk element on its pages)". */
function wakesOnText(doc, key, elements, panels) {
  const parties = talkPartiesOf(elements);
  const w = wakeOf(doc, key, parties, panels);
  const name = (id) => ((elements instanceof Map && elements.get(id)) || {}).name || id;
  const bits = [...w.elements.map(name), ...w.own.map((id) => `${name(id)} (its own call element)`)];
  return bits.length ? `Wakes on: ${bits.join(", ")}` : "Wakes on: nothing (no Talk element on its pages)";
}

/* "Rings for these doors: Front door", for a panel (read only: the doors come from the door Talk elements on its pages; its own call
 * element is not a door), or "... none (no door Talk element on its pages)". */
function ringsForText(doc, key, elements, panels) {
  const parties = talkPartiesOf(elements);
  const w = wakeOf(doc, key, parties, panels);
  const name = (id) => ((elements instanceof Map && elements.get(id)) || {}).name || id;
  return w.elements.length ? `Rings for these doors: ${w.elements.map(name).join(", ")}` : "Rings for these doors: none (no door Talk element on its pages)";
}

const deviceCopy = (doc, key) => {
  const d = ((doc && doc.devices) || {})[key] || {};
  // any other key (a stale `ring` map of a restored document) passes through untouched: the server ignores it
  return { pages: (d.pages || []).map((e) => ({ ...e })), roster_hide: [...(d.roster_hide || [])], ...Object.fromEntries(Object.entries(d).filter(([k]) => !["pages", "roster_hide"].includes(k))) };
};

/* A copy of `doc` with `fn(device)` applied to a copy of device `key` (created empty when it has none yet: a new device starts with no pages). */
function withDevice(doc, key, fn) {
  if (!isPagesModel(doc)) throw new EditError("This document does not use the unified Pages model");
  const out = clone(doc);
  out.devices = out.devices || {};
  const device = deviceCopy(doc, key);
  fn(device);
  out.devices[key] = device;
  return out;
}

const devicePageIds = (doc, key) => ((((doc && doc.devices) || {})[key] || {}).pages || []).map((e) => e.page);

/* Add a content page to a device's list (at the end; the first is Home). A Ring page cannot be listed; a page twice or past MAX_PAGES is refused. A page whose door Talk element another screen holds is fine (D-20261122-42b1). */
function addDevicePage(doc, key, pageId, elements, panels) {
  const page = pagesOf(doc, LIBRARY).find((p) => p.id === pageId);
  if (!page) throw new EditError("Pick a page to add");
  if (!((doc.dashboards || {})[key]) && !(panels || []).some((p) => p.id === key) && elements instanceof Map && pageActions(page).some((a) => isRef(a) && isGroupElement(elements.get(a.element)))) {
    throw new EditError(`${GROUP_SCREEN_ONLY} (this page holds a group call button)`);      // a phone is not a screen; a panel is, for group calls: the server refuses the same save
  }
  if (pageKind(page) !== "content") throw new EditError("A Ring page belongs to a Talk element and is not listed on a device");
  if (devicePageIds(doc, key).includes(pageId)) throw new EditError("That page is already in the list");
  if (devicePageIds(doc, key).length >= MAX_PAGES) throw new EditError(`A device shows at most ${MAX_PAGES} pages`);
  return withDevice(doc, key, (d) => { d.pages.push({ page: pageId }); });
}

/* "Add first page" (D-20261112 phone page): for a device with no page yet, a new empty content page named after the device is made in the
 * library and put in its list, in one pure step (nothing changes when either half is refused). Never a copy of another device's pages and
 * never a template: the page is the device's own to begin with. The Ring page assignment is left alone ("Default (built-in)" is a working ring). */
function addFirstPage(doc, key, deviceName, elements, panels) {
  if (!isPagesModel(doc)) throw new EditError("This document does not use the unified Pages model");
  if (devicePageIds(doc, key).length) throw new EditError("This device already has a page");
  const label = [...String(deviceName || "").trim()].slice(0, MAX_LABEL).join("").trim() || "Home";
  const made = addLibraryPage(doc, "content", label, null);
  return { doc: addDevicePage(made.doc, key, made.pageId, elements, panels), pageId: made.pageId };
}

function removeDevicePage(doc, key, pageId) {
  if (!devicePageIds(doc, key).includes(pageId)) throw new EditError("That page is not in the list");
  return withDevice(doc, key, (d) => { d.pages = d.pages.filter((e) => e.page !== pageId); });
}

function moveDevicePage(doc, key, pageId, delta) {
  const at = devicePageIds(doc, key).indexOf(pageId);
  const to = at + delta;
  if (at < 0 || to < 0 || to >= devicePageIds(doc, key).length) return doc;
  return withDevice(doc, key, (d) => { d.pages = move(d.pages, at, to); });
}

/* ---- Ring pages per Talk element (D-20261122-4aec, decisions D-20261122-5b24) ----
 * A Ring page belongs to a Talk element: at most one is stored, and a Talk element without one rings with the computed default (nothing
 * stored). */

/* The stored Ring pages of a Talk element (normally one). */
const ringPagesFor = (doc, talkId) => pagesOf(doc, LIBRARY).filter((p) => pageKind(p) === "ring" && p.talk === talkId);

const RING_PARTY_KINDS = ["door", "phone", "screen"];

/* The Pages tab entries of the new form: one per ringable Talk element (a door, a phone or a screen; never a group), in the order doors,
 * phones, screens, then by name: `{talk, name, what, party, state: "default"|"edited", page, warn}`; plus the "Removed" entries: a stored
 * Ring page whose Talk element is gone, has lost its target or stands for nobody: `{talk, name, state: "removed", page}`. `warn` is set
 * on both elements when two Talk elements stand for one door and one of them holds a page ("another Talk element ... rings"). */
function ringEntries(doc, elements) {
  const parties = talkPartiesOf(elements);
  const named = (id) => (elements instanceof Map && elements.get(id)) || null;
  const list = elements ? (elements instanceof Map ? [...elements.values()] : Array.isArray(elements) ? elements : Object.values(elements)) : [];
  const ringable = list.filter((e) => isTalkElement(e) && !isGroupElement(e) && e.target_removed !== true && RING_PARTY_KINDS.includes((parties.get(e.id) || [])[0]));
  const rank = (e) => RING_PARTY_KINDS.indexOf(parties.get(e.id)[0]);
  ringable.sort((a, b) => rank(a) - rank(b) || (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
  const sameParty = (a, b) => parties.get(a.id).join("\u0000") === parties.get(b.id).join("\u0000");
  const entries = ringable.map((e) => {
    const page = ringPagesFor(doc, e.id)[0] || null;
    const others = ringable.filter((o) => o.id !== e.id && sameParty(o, e));
    const holders = [e, ...others].filter((x) => ringPagesFor(doc, x.id).length).map((x) => x.id).sort();
    return { talk: e.id, name: e.name, what: elementWhat(e), party: parties.get(e.id)[0], state: page ? "edited" : "default", page,
             warn: others.length > 0 && holders.length > 0 };
  });
  const live = new Set(ringable.map((e) => e.id));
  const removed = pagesOf(doc, LIBRARY).filter((p) => pageKind(p) === "ring" && !live.has(p.talk))
    .map((p) => ({ talk: p.talk, name: named(p.talk) ? named(p.talk).name : `Talk element removed (${p.talk})`, state: "removed", page: p }));
  return { entries, removed };
}

/* The seed of an Edit (Q2): for a door Talk element, the Button elements that open this door (`portico_open` whose target is the door)
 * and every lock-open Button element, as element references in one block "Buttons"; empty for a phone or screen. */
function ringSeedActions(elements, talkId, taken) {
  const parties = talkPartiesOf(elements);
  const [kind, door] = parties.get(talkId) || [];
  if (kind !== "door") return [];
  const list = elements instanceof Map ? [...elements.values()] : Array.isArray(elements) ? elements : Object.values(elements || {});
  const bare = (ref) => { const s = String(ref == null ? "" : ref); const i = s.indexOf(":"); return i > 0 && !s.startsWith("http") ? s.slice(i + 1) : s; };
  const opens = (e) => {
    if (e.kind !== "button" || !placeableButton(e)) return false;
    const o = e.options || {};
    if (o.action === "lock_open") return true;
    return o.action === "portico_open" && !!o.target && (String(o.target) === String(door) || bare(o.target) === bare(door));
  };
  const out = [];
  const used = new Set(taken || []);
  for (const e of list.filter(opens).sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0))) {
    const id = slug(e.name, used, "button");
    used.add(id);
    out.push({ id, element: e.id });
  }
  return out;
}

/* "Edit" on a Default entry: stores a Ring page `{kind: "ring", talk, cameras: null, blocks}` seeded as above. Refused when one exists. */
function editRingEntry(doc, talkId, elements) {
  if (!isPagesModel(doc)) throw new EditError("This document does not use the unified Pages model");
  if (!TALK_ID_RE.test(talkId || "")) throw new EditError("Pick a Talk element");
  const entry = ringEntries(doc, elements).entries.find((e) => e.talk === talkId);
  if (!entry) throw new EditError("That Talk element has no ring page to edit (it is a group, has no target or is removed)");
  if (entry.page) throw new EditError("That Talk element already has an edited page");
  if (pagesOf(doc, LIBRARY).length >= MAX_LIBRARY_PAGES) throw new EditError(`At most ${MAX_LIBRARY_PAGES} pages`);
  const label = checkLabel(entry.name, "A page");
  const id = slug(label, takenPageIds(doc, LIBRARY), "page");
  const actions = ringSeedActions(elements, talkId, allActionIds(doc));
  const block = { id: "buttons", title: "Buttons", cameras: [], actions };
  return { doc: withPages(doc, LIBRARY, (pages) => [...pages, { id, label, kind: "ring", talk: talkId, cameras: null, blocks: [block] }]), pageId: id };
}

/* "Reset to default": deletes the stored Ring page(s) of the Talk element. Nothing else changes. */
function resetRingEntry(doc, talkId) {
  const pages = ringPagesFor(doc, talkId);
  if (!pages.length) throw new EditError("That Talk element has no edited page");
  const ids = new Set(pages.map((p) => p.id));
  return withPages(doc, LIBRARY, (all) => all.filter((p) => !ids.has(p.id)));
}

function ringPageOf(doc, pageId) {
  const page = findPage(doc, LIBRARY, pageId);
  if (pageKind(page) !== "ring") throw new EditError("That is not a Ring page");
  return page;
}

/* The camera of a Ring page: `null` = the caller's own cameras, "" = none, else one camera element or camera entity. */
function setRingCamera(doc, pageId, camera) {
  ringPageOf(doc, pageId);
  let value;
  if (camera === null) value = null;
  else if (camera === "") value = [];
  else if (isElementId(camera) || isCameraEntity(camera)) value = [camera];
  else throw new EditError("Pick a camera");
  return editPage(doc, LIBRARY, pageId, (page) => { page.cameras = value; return page; });
}

/* A Ring block's audience: "" (both), "ring" (only while ringing) or "call" (only during the answered call). */
function setBlockWhen(doc, pageId, blockId, when) {
  ringPageOf(doc, pageId);
  if (when !== "" && when !== "ring" && when !== "call") throw new EditError("Shown must be while ringing, during the call or both");
  return editPage(doc, LIBRARY, pageId, (page) => {
    const block = findBlock(page, blockId);
    if (when) block.when = when; else delete block.when;
    return page;
  });
}

/* The endpoint id of the party a Talk element stands for (what a ring from it is called in `callers`), or null: a door's row
 * id is `<provider>:<device id>`, a phone's or screen's is the endpoint id itself. */
function talkCaller(rows, element) {
  const o = (element && element.options) || {};
  if (typeof o.screen === "string" && o.screen) return o.screen;
  if (typeof o.phone === "string" && o.phone) return o.phone;
  if (typeof o.door === "string" && o.door) {
    const row = (rows || []).find((r) => r.kind === "door" && r.id.slice(r.id.indexOf(":") + 1) === o.door);
    return row ? row.id : null;
  }
  return null;
}

/* ---- the editor state: a loaded document, a draft, an undo stack, the names to drop on save ---- */

function createState(loaded, previous) {
  // Both the loaded document and the draft are held in the editor's shape (schema 4, pages `{id, label, blocks}`, the
  // server's flat mirror dropped), so "is anything changed" compares like with like.
  loaded = { ...loaded, document: editorDocument(loaded.document) };
  const state = {
    loaded,
    draft: clone(loaded.document),
    drop: [],
    undo: [],
    sel: "template",       // "template" | "ep:<endpoint id>" | "orphan:<override key>"
    pageId: null,
    previewAs: null,
    screenId: null,        // Devices tab, Screens section: the screen being edited
    ruleId: null,          // Ring & call tab: the rule being edited
    previewCaller: null,   // Ring & call tab: the caller whose ring screen the preview draws
  };
  if (previous) {
    state.sel = previous.sel;
    state.pageId = previous.pageId;
    state.previewAs = previous.previewAs;
    state.screenId = previous.screenId;
    state.ruleId = previous.ruleId;
    state.previewCaller = previous.previewCaller;
  }
  return state;
}

/* The endpoint rows as the DRAFT sees them: the server's rows, with a screen added, renamed or deleted in the
 * draft (Screens tab) shown at once instead of after the next Save. Only a screen the stored document declares
 * can be dropped here; a screen row the server lists without a stored declaration is left as it is. */
function dashboardRow(slug, d) {
  const name = (d && d.name) || slug;
  return { id: slug, kind: "dashboard", name, label: name, ha_user_id: (d && d.ha_user_id) || null,
           cameras: ((d && d.cameras) || []).slice(), room: (d && d.room) || null, available: true, reason: null };
}

function allRows(state) {
  const loaded = (state.loaded && state.loaded.endpoints) || [];
  const stored = ((state.loaded && state.loaded.document) || {}).dashboards || {};
  const draft = (state.draft && state.draft.dashboards) || {};
  const out = [];
  const have = new Set();
  for (const row of loaded) {
    if (row.kind === "dashboard") {
      if (row.id in stored && !(row.id in draft)) continue;
      if (row.id in draft) { out.push(dashboardRow(row.id, draft[row.id])); have.add(row.id); continue; }
    }
    out.push(row);
  }
  const fresh = Object.keys(draft).filter((slug) => !have.has(slug)).map((slug) => dashboardRow(slug, draft[slug]));
  if (fresh.length) {
    const firstDoor = out.findIndex((r) => r.kind === "door");
    out.splice(firstDoor < 0 ? out.length : firstDoor, 0, ...fresh);
  }
  return out;
}

function rowsOf(state, kinds) {
  return allRows(state).filter((r) => kinds.includes(r.kind));
}

function rowName(row) {
  return row.label || row.name || row.id;
}

function selTarget(sel) {
  return sel === "template" ? "template" : sel.startsWith("ep:") ? sel.slice(3) : null;
}

function orphanKeys(state) {
  const held = isPagesModel(state.draft) ? state.draft.devices || {} : state.draft.overrides || {};      // schema 6: the leftover device entries
  return ((state.loaded && state.loaded.orphans) || []).filter((k) => k in held);
}

/* Is the selection still there after a reload? Else the template. */
function validSel(state, sel) {
  if (sel === "template") return true;
  if (sel.startsWith("ep:")) return rowsOf(state, ["phone", "dashboard"]).some((r) => r.id === sel.slice(3));
  if (sel.startsWith("orphan:")) return orphanKeys(state).includes(sel.slice(7));
  return false;
}

const isDirty = (state) => state.drop.length > 0 || !same(state.draft, state.loaded.document || {});

/* Replace the draft (an edit) and remember the old one for Undo. `drop`: orphan keys to add to the delete list. */
function commit(state, doc, drop) {
  state.undo.push({ draft: state.draft, drop: state.drop });
  if (state.undo.length > UNDO_DEPTH) state.undo.shift();
  state.draft = doc;
  if (drop && drop.length) state.drop = [...new Set([...state.drop, ...drop])];
}

function undo(state) {
  const prev = state.undo.pop();
  if (!prev) return false;
  state.draft = prev.draft;
  state.drop = prev.drop;
  return true;
}

function discard(state) {
  state.draft = clone(state.loaded.document || {});
  state.drop = [];
  state.undo = [];
}

function moveOrphan(state, key, target) {
  commit(state, mergeOrphan(state.draft, key, target), [key]);
}

function deleteOrphan(state, key) {
  const v6 = isPagesModel(state.draft);
  if (!(key in ((v6 ? state.draft.devices : state.draft.overrides) || {}))) throw new EditError("That item is not in the document");
  const doc = clone(state.draft);
  delete (v6 ? doc.devices : doc.overrides)[key];
  commit(state, doc, [key]);
}

/* ---- screens: the declared kiosk screens, `document.dashboards` (M6 K3, design 2 and 4) ---------------- */

const dashboardsOf = (doc) => (doc && doc.dashboards) || {};

/* The URL a kiosk screen opens: `origin` is the page's own (location.origin), `slugText` a screen id. Anything else
 * (an opaque "null" origin, a path, credentials, a slug that is not an id) gives null: no URL is better than a wrong one. */
function kioskUrl(slugText, origin) {
  if (typeof slugText !== "string" || !ID_RE.test(slugText)) return null;
  if (typeof origin !== "string" || !ORIGIN_RE.test(origin)) return null;
  return `${origin}${KIOSK_PREFIX}${slugText}`;
}

/* Every id a screen's slug must not equal: the other endpoints (phones, doors) and the screens already there. */
function takenEndpointIds(state) {
  return new Set(allRows(state).map((r) => r.id));
}

function checkSlug(text, taken) {
  const id = String(text == null ? "" : text).trim();
  if (!ID_RE.test(id)) throw new EditError("A screen's address is 1-32 characters of a-z, 0-9, _ or -, starting with a letter or digit");
  if (taken.has(id)) throw new EditError(`"${id}" is already used by a phone, a screen or a door`);
  return id;
}

function checkRoom(text) {
  const room = String(text == null ? "" : text).trim();
  if (room.length > MAX_LABEL) throw new EditError(`The room is longer than ${MAX_LABEL} characters`);
  return room;
}

function checkUser(userId) {
  if (!userId || typeof userId !== "string") throw new EditError("Pick the Home Assistant user the screen is logged in as");
  return userId;
}

/* spec: {name, slug?, ha_user_id, room?}; `taken`: takenEndpointIds(state). The slug comes from the name when empty. */
function addScreen(doc, spec, taken) {
  const name = checkLabel(spec.name, "A screen");
  const id = spec.slug && String(spec.slug).trim() ? checkSlug(spec.slug, taken) : slug(name, taken, "screen");
  if (!ID_RE.test(id)) throw new EditError("Give the screen an address of a-z, 0-9, _ or -");
  const user = checkUser(spec.ha_user_id);
  const room = checkRoom(spec.room);
  const out = clone(doc);
  out.dashboards = out.dashboards || {};
  out.dashboards[id] = { name, ha_user_id: user, ...(room ? { room } : {}), cameras: [] };
  return { doc: out, slug: id };
}

function findScreen(doc, id) {
  const screen = dashboardsOf(doc)[id];
  if (!screen) throw new EditError("That screen is gone");
  return screen;
}

/* patch: {name?, ha_user_id?, room?} (an empty room removes it). The address never changes: it is the key of the
 * screen's own pages and rules. */
function updateScreen(doc, id, patch) {
  findScreen(doc, id);
  const out = clone(doc);
  const screen = out.dashboards[id];
  if ("name" in patch) screen.name = checkLabel(patch.name, "A screen");
  if ("ha_user_id" in patch) screen.ha_user_id = checkUser(patch.ha_user_id);
  if ("room" in patch) {
    const room = checkRoom(patch.room);
    if (room) screen.room = room; else delete screen.room;
  }
  return out;
}

function addScreenCamera(doc, id, entityId) {
  const screen = findScreen(doc, id);
  if (!isElementId(entityId) && !isCameraEntity(entityId)) throw new EditError("Pick a camera");
  if ((screen.cameras || []).includes(entityId)) throw new EditError("That camera is already in the list");
  if ((screen.cameras || []).length >= MAX_CAMERAS) throw new EditError(`At most ${MAX_CAMERAS} cameras`);
  const out = clone(doc);
  out.dashboards[id].cameras = [...(screen.cameras || []), entityId];
  return out;
}

function removeScreenCamera(doc, id, entityId) {
  const screen = findScreen(doc, id);
  const out = clone(doc);
  out.dashboards[id].cameras = (screen.cameras || []).filter((c) => c !== entityId);
  return out;
}

/* The pages (anywhere in the document) that still hold a `call` button naming this screen: the server refuses a
 * save that leaves one, so the delete is refused here with the page named. */
function callersOfScreen(doc, id) {
  const sets = [["the household pages", templateOf(doc).pages || []]];
  for (const [key, override] of Object.entries(doc.overrides || {})) sets.push([`the pages of ${key}`, override.pages || []]);
  const out = [];
  for (const [where, pages] of sets) {
    for (const page of pages) {
      for (const action of pageActions(page)) {
        if (action.kind === "call" && action.target && action.target.type === "dashboard" && action.target.id === id) {
          out.push(`${where}, page "${page.label || page.id}", button "${action.label}"`);
        }
      }
    }
  }
  return out;
}

/* Delete the declaration. Its own pages and rules stay in the document and show under "Not attached", so
 * declaring the screen again (or moving them) loses nothing; Save writes it. */
function deleteScreen(doc, id) {
  findScreen(doc, id);
  const users = callersOfScreen(doc, id);
  if (users.length) throw new EditError(`A call button still calls this screen (${users[0]}); remove it first`);
  const out = clone(doc);
  delete out.dashboards[id];
  return out;
}

/* The HA users a screen can be logged in as: every non-administrator the server lists, plus the one already
 * chosen (kept, and marked, if it has gone or is an administrator). Sorted by name. */
function screenUserOptions(state, currentId) {
  const users = (state.loaded && state.loaded.ha_users) || [];
  const out = users.filter((u) => u.is_admin === false).map((u) => ({
    value: u.id, label: `${u.name || u.id}${u.kiosk_open ? " (open now)" : ""}`, name: String(u.name || u.id) }));
  out.sort((a, b) => a.name.localeCompare(b.name) || a.value.localeCompare(b.value));
  if (currentId && !out.some((o) => o.value === currentId)) {
    const known = users.find((u) => u.id === currentId);
    out.push({ value: currentId, label: known ? `${known.name || currentId} (administrator)` : `${currentId} (no such user)`,
               name: known ? String(known.name || currentId) : currentId });
  }
  return out;
}

/* ---- rules: what a ring shows for each caller, `template.rules` and `overrides[<id>].rules` (K3, design 2.4) -- */

const RULE_ORDER_HINT = "Which rule applies to a call: 1. this phone's or screen's own rule for the calling door; " +
  "2. its own rule for that kind of caller; 3. the household rule for the calling door; 4. the household rule for " +
  "that kind. Inside a step the first rule in the list wins. With no rule, the ring shows the caller's own cameras " +
  "and, for a door, the door-open and lock buttons of the page set. A caller Portico cannot place (*) shows nothing.";

function rulesOf(doc, target) {
  return (target === "template" ? templateOf(doc) : overrideOf(doc, target)).rules || [];
}

function withRules(doc, target, fn) {
  const out = clone(doc);
  if (target === "template") {
    out.template = { pages: [], rules: [], roster: ["*"], ...(out.template || {}) };
    out.template.rules = fn(out.template.rules);
  } else {
    out.overrides = out.overrides || {};
    out.overrides[target] = { pages: [], rules: [], roster_hide: [], ...(out.overrides[target] || {}) };
    out.overrides[target].rules = fn(out.overrides[target].rules);
  }
  return out;
}

/* The buttons a rule of `target` can name, in page order: the household pages' buttons plus, for a phone or
 * screen, its own pages' (check_rule_buttons). [{id, label, page}] */
function ruleButtonChoices(doc, target, elements) {
  const pages = [...(templateOf(doc).pages || []), ...(target === "template" ? [] : pagesOf(doc, target))];
  const out = [];
  const seen = new Set();
  for (const page of pages) {
    for (const action of pageActions(page)) {
      const element = isRef(action) && elements ? elements.get(action.element) : null;
      if (action.kind === "talk" || buttonAction(element) === "talk" || seen.has(action.id)) continue;      // a ring is not the moment to start a talk
      seen.add(action.id);
      const label = isRef(action) ? action.label || (element ? element.name : action.id) : action.label;
      out.push({ id: action.id, label, page: page.label || page.id });
    }
  }
  return out;
}

/* The callers a new rule can name: the three kinds and each door (a rule by a phone's or screen's id is later).
 * Callers the rule list already has are left out: the second would never apply. */
function ruleCallerChoices(state, target) {
  const used = new Set(rulesOf(state.draft, target).map((r) => r.caller));
  const out = RULE_KINDS.map((k) => ({ value: k, label: RULE_KIND_TEXT[k] }));
  for (const row of rowsOf(state, ["door"])) out.push({ value: row.id, label: `Door: ${rowName(row)}` });
  return out.filter((c) => !used.has(c.value));
}

function ruleCallerLabel(state, caller) {
  if (RULE_KIND_TEXT[caller]) return RULE_KIND_TEXT[caller];
  const row = allRows(state).find((r) => r.id === caller);
  if (row) return `${row.kind === "door" ? "Door" : row.kind === "dashboard" ? "Screen" : "Phone"}: ${rowName(row)}`;
  return caller === UNKNOWN_CALLER ? "Unknown caller (*)" : `${caller} (not found)`;
}

function checkRuleCaller(caller) {
  const text = String(caller == null ? "" : caller);
  if (!text || text !== text.trim()) throw new EditError("Pick who the rule is for");
  if (text.length > MAX_ENDPOINT_ID) throw new EditError("That caller's id is too long");
  if (text.startsWith("kind:") && !RULE_KINDS.includes(text)) throw new EditError("The caller kind must be a door, a phone or a screen");
  return text;
}

/* `keep`: ids the rule already has, allowed even if the page set lost them (the problem list says so; this edit
 * must not be blocked by an unrelated button). */
function checkRuleButtons(doc, target, ids, what, keep) {
  const list = [...new Set(ids || [])];
  if (list.length > MAX_RULE_BUTTONS) throw new EditError(`At most ${MAX_RULE_BUTTONS} ${what}`);
  const known = new Set([...ruleButtonChoices(doc, target).map((c) => c.id), ...(keep || [])]);
  for (const id of list) if (!known.has(id)) throw new EditError(`The button "${id}" is not on this page set`);
  return list;
}

function checkRuleCameras(cameras) {
  if (cameras === null || cameras === undefined) return null;
  if (!Array.isArray(cameras) || cameras.length > MAX_CAMERAS) throw new EditError(`At most ${MAX_CAMERAS} cameras`);
  for (const c of cameras) {
    if (!isElementId(c) && !isCameraEntity(c)) throw new EditError("Pick a camera");
  }
  return [...new Set(cameras)];
}

/* spec: {caller, label?, cameras? (null: the caller's own), ring_buttons?, call_buttons?}. The id comes from `label`. */
function addRule(doc, target, spec) {
  const caller = checkRuleCaller(spec.caller);
  const rules = rulesOf(doc, target);
  if (rules.length >= MAX_RULES) throw new EditError(`At most ${MAX_RULES} rules`);
  if (rules.some((r) => r.caller === caller)) throw new EditError("There is already a rule for that caller here; change it instead");
  const rule = {
    id: slug(spec.label || caller.replace(/^kind:/, ""), new Set(rules.map((r) => r.id)), "rule"),
    caller,
    cameras: checkRuleCameras(spec.cameras === undefined ? null : spec.cameras),
    ring_buttons: checkRuleButtons(doc, target, spec.ring_buttons, "ring buttons"),
    call_buttons: checkRuleButtons(doc, target, spec.call_buttons, "call buttons"),
  };
  return { doc: withRules(doc, target, (list) => [...list, rule]), ruleId: rule.id };
}

function findRule(doc, target, ruleId) {
  const rule = rulesOf(doc, target).find((r) => r.id === ruleId);
  if (!rule) throw new EditError("That rule is gone");
  return rule;
}

/* patch: {cameras?, ring_buttons?, call_buttons?}; the caller of a rule never changes (delete and add instead). */
function updateRule(doc, target, ruleId, patch) {
  const current = findRule(doc, target, ruleId);
  const fields = {};
  if ("cameras" in patch) fields.cameras = checkRuleCameras(patch.cameras);
  if ("ring_buttons" in patch) fields.ring_buttons = checkRuleButtons(doc, target, patch.ring_buttons, "ring buttons", current.ring_buttons);
  if ("call_buttons" in patch) fields.call_buttons = checkRuleButtons(doc, target, patch.call_buttons, "call buttons", current.call_buttons);
  return withRules(doc, target, (list) => list.map((r) => (r.id === ruleId ? { ...r, ...fields } : r)));
}

function deleteRule(doc, target, ruleId) {
  findRule(doc, target, ruleId);
  return withRules(doc, target, (list) => list.filter((r) => r.id !== ruleId));
}

function moveRule(doc, target, ruleId, delta) {
  findRule(doc, target, ruleId);
  return withRules(doc, target, (list) => {
    const at = list.findIndex((r) => r.id === ruleId);
    return move(list, at, at + delta);
  });
}

/* Toggle one button id in a rule's list, appended when added (the list's order is the order drawn). */
function toggleId(list, id) {
  return list.includes(id) ? list.filter((x) => x !== id) : [...list, id];
}

/* ---- validation (the server's normalize_document is the authority; this catches the common ones early) -- */

/* One page's blocks, mirroring content.py (normalize_page, normalize_block, _normalize_items). `here` names the page. */
function validateBlocks(page, here, problems) {
  const blocks = page.blocks;
  if (!Array.isArray(blocks)) { problems.push(`${here}: the page has no blocks list`); return; }
  if (blocks.length > MAX_BLOCKS) problems.push(`${here}: at most ${MAX_BLOCKS} blocks`);
  if (pageCameras(page).length > MAX_CAMERAS) problems.push(`${here}: at most ${MAX_CAMERAS} cameras on a page`);
  if (pageActions(page).length > MAX_ACTIONS) problems.push(`${here}: at most ${MAX_ACTIONS} buttons on a page`);
  const blockSeen = new Set();
  const titles = new Set();
  const ids = new Set();
  for (const block of blocks) {
    const at = `${here}, block "${block.title || block.id}"`;
    if (!ID_RE.test(block.id || "") || blockSeen.has(block.id)) problems.push(`${at}: the block id is missing or used twice`);
    blockSeen.add(block.id);
    const title = String(block.title == null ? "" : block.title).trim();
    if (!title || [...title].length > MAX_TITLE) problems.push(`${at}: the title must be 1-${MAX_TITLE} characters`);
    else if (BAD_TEXT.test(title)) problems.push(`${at}: the title must not contain control characters or line breaks`);
    if (titles.has(title.toLocaleLowerCase())) problems.push(`${here}: two blocks are called "${title}"`);
    titles.add(title.toLocaleLowerCase());
    if (block.door != null && (typeof block.door !== "string" || !block.door || block.door.length > 128)) problems.push(`${at}: the door is not a Portico door`);
    const cameras = block.cameras || [];
    const actions = block.actions || [];
    if (cameras.length > MAX_BLOCK_CAMERAS || actions.length > MAX_BLOCK_ACTIONS) {
      problems.push(`${at}: at most ${MAX_BLOCK_CAMERAS} cameras and ${MAX_BLOCK_ACTIONS} buttons in a block`);
    }
    for (const cam of cameras) {
      if (isRef(cam)) {          // an element reference: the element holds the entity; only the overrides are checked here
        if (!isElementId(cam.element)) problems.push(`${at}: a camera's element must be an element id`);
        if (cam.label != null && (typeof cam.label !== "string" || cam.label.length > MAX_LABEL)) problems.push(`${at}: a camera's name is at most ${MAX_LABEL} characters (empty shows no name)`);
        if (cam.size != null && !CAMERA_SIZES.includes(cam.size)) problems.push(`${at}: "${cameraName(cam)}" has a size that is not one of ${CAMERA_SIZES.join(", ")}`);
        if (cam.live != null && typeof cam.live !== "boolean") problems.push(`${at}: "${cameraName(cam)}" has a Live setting that is not on or off`);
        if (ids.has(cam.id)) problems.push(`${here}: the id ${cam.id} is used twice`);
        ids.add(cam.id);
        continue;
      }
      if (typeof cam.label !== "string" || cam.label.length > MAX_LABEL) problems.push(`${at}: a camera's name is at most ${MAX_LABEL} characters (empty shows no name)`);
      if (!ENTITY_RE.test(cam.entity_id || "") || cam.entity_id.split(".")[0] !== "camera") problems.push(`${at}: "${cameraName(cam)}" is not a camera`);
      if (cam.size != null && !CAMERA_SIZES.includes(cam.size)) problems.push(`${at}: "${cameraName(cam)}" has a size that is not one of ${CAMERA_SIZES.join(", ")}`);
      if (cam.live != null && typeof cam.live !== "boolean") problems.push(`${at}: "${cameraName(cam)}" has a Live setting that is not on or off`);
      if (ids.has(cam.id)) problems.push(`${here}: the id ${cam.id} is used twice`);
      ids.add(cam.id);
    }
    for (const act of actions) {
      if (isRef(act)) {          // a button reference: the element holds the action; only the overrides are checked here
        if (!isElementId(act.element)) problems.push(`${at}: a button's element must be an element id`);
        if (act.label != null && (typeof act.label !== "string" || !act.label.trim() || act.label.length > MAX_LABEL)) problems.push(`${at}: a button needs a name of 1-${MAX_LABEL} characters`);
        if (act.show_label != null && typeof act.show_label !== "boolean") problems.push(`${at}: a button's show-text setting is not on or off`);
        if (act.confirm != null && act.confirm !== true) problems.push(`${at}: requiring confirmation on a button from an element can be turned on, never off`);
        if (ids.has(act.id)) problems.push(`${here}: the id ${act.id} is used twice`);
        ids.add(act.id);
        continue;
      }
      const kind = KINDS[act.kind];
      if (!String(act.label || "").trim() || act.label.length > MAX_LABEL) problems.push(`${at}: a button needs a name of 1-${MAX_LABEL} characters`);
      if (act.kind === "talk") {
        if (!block.door) problems.push(`${at}: the Talk button "${act.label}" needs the block to have a door`);
      } else if (act.kind !== "call") {
        if (!kind) problems.push(`${at}: "${act.label}" has an unknown kind`);
        else if (kind.domain === null ? !(act.device_id || block.door) : !ENTITY_RE.test(act.entity_id || "") || act.entity_id.split(".")[0] !== kind.domain) {
          problems.push(`${at}: "${act.label}" needs its ${kind.domain || "door"}`);
        }
      }
      if (act.color != null && !PALETTE_NAMES.includes(act.color)) problems.push(`${at}: "${act.label}" has a colour that is not in the palette`);
      if (act.show_label === false && !usableIcon(act.icon)) problems.push(`${at}: "${act.label}" hides its text, so it needs an icon from the list`);
      if (ids.has(act.id)) problems.push(`${here}: the id ${act.id} is used twice`);
      ids.add(act.id);
    }
  }
}

/* The schema 6 library (pages_model.py: normalize_library_page / _normalize_ring_page): what the panel can check early. The
 * assignments on `devices` are the Devices tab's and the server's to check. `ctx.elements`: the elements by id, or null. */
function validateLibrary(doc, ctx, problems) {
  const pages = Array.isArray(doc.pages) ? doc.pages : [];
  if (pages.length > MAX_LIBRARY_PAGES) problems.push(`At most ${MAX_LIBRARY_PAGES} pages`);
  const elements = (ctx && ctx.elements) || null;
  const talkLike = (act) => (isRef(act) ? (elements && elements.get(act.element) ? elements.get(act.element).kind === "talk" : String(act.element).startsWith("talk-")) : act.kind === "talk");
  const seen = new Set();
  for (const page of pages) {
    const here = `Page "${page.label || page.id}"`;
    if (!ID_RE.test(page.id || "") || seen.has(page.id)) problems.push(`${here}: the page id is missing or used twice`);
    seen.add(page.id);
    if (!String(page.label || "").trim() || page.label.length > MAX_LABEL) problems.push(`${here}: the name must be 1-${MAX_LABEL} characters`);
    if (page.kind !== undefined && page.kind !== "content" && page.kind !== "ring") problems.push(`${here}: the kind must be Content or Ring`);
    validateBlocks(page, here, problems);
    if (pageKind(page) === "ring") {
      if (!TALK_ID_RE.test(page.talk || "")) problems.push(`${here}: a Ring page needs its Talk element`);
      const cams = page.cameras;
      if (cams !== null && cams !== undefined) {
        if (!Array.isArray(cams) || cams.length > MAX_CAMERAS) problems.push(`${here}: at most ${MAX_CAMERAS} cameras`);
        else if (cams.some((c) => !isElementId(c) && !isCameraEntity(c))) problems.push(`${here}: the camera is not a camera`);
      }
      for (const block of blocksOf(page)) {
        const at = `${here}, block "${block.title || block.id}"`;
        if ((block.cameras || []).length || block.door) problems.push(`${at}: a Ring page's blocks hold buttons only; its camera is chosen on the page`);
        if (block.when != null && !(block.when === "ring" || block.when === "call")) problems.push(`${at}: Shown must be while ringing, during the call or both`);
        for (const act of block.actions || []) if (talkLike(act)) problems.push(`${at}: a Ring page cannot hold a Talk button ("${act.label || act.id}")`);
      }
    } else if (blocksOf(page).some((b) => b.when != null)) {
      problems.push(`${here}: only a Ring page's blocks say when they are shown`);
    }
  }
}

function validate(doc, nameOf, ctx) {
  const name = nameOf || ((id) => id);
  const problems = [];
  if (doc.home_name != null) {
    if (typeof doc.home_name !== "string" || [...doc.home_name.trim()].length > MAX_HOME_NAME) problems.push(`The home name must be text of up to ${MAX_HOME_NAME} characters`);
    else if (BAD_TEXT.test(doc.home_name)) problems.push("The home name must not contain control characters or line breaks");
  }
  if (doc.show_fps != null && typeof doc.show_fps !== "boolean") problems.push("Show frame rate must be on or off");
  if (isPagesModel(doc)) {
    validateLibrary(doc, ctx, problems);
    problems.push(...validateScreens(doc, ctx));
    return problems;
  }
  const sets = [["template", templateOf(doc), "the household pages"]];
  for (const [key, override] of Object.entries(doc.overrides || {})) sets.push([key, override, `${name(key)}'s pages`]);
  const templatePages = templateOf(doc).pages || [];
  for (const [owner, holder, where] of sets) {
    const pages = holder.pages || [];
    if (pages.length > MAX_PAGES) problems.push(`${where}: at most ${MAX_PAGES} pages`);
    const seen = new Set();
    for (const page of pages) {
      const here = `${where}, page "${page.label || page.id}"`;
      if (!ID_RE.test(page.id || "") || seen.has(page.id)) problems.push(`${here}: the page id is missing or used twice`);
      seen.add(page.id);
      if (!String(page.label || "").trim() || page.label.length > MAX_LABEL) problems.push(`${here}: the name must be 1-${MAX_LABEL} characters`);
      validateBlocks(page, here, problems);
    }
    // a rule's ring / call buttons must still exist in the page set it applies to (an override rule: the template's too)
    const available = new Set([...(owner === "template" ? [] : templatePages), ...pages].flatMap((p) => pageActions(p).map((a) => a.id)));
    const talkIds = new Set([...(owner === "template" ? [] : templatePages), ...pages].flatMap((p) => pageActions(p).filter((a) => a.kind === "talk").map((a) => a.id)));
    for (const rule of holder.rules || []) {
      for (const field of ["ring_buttons", "call_buttons"]) {
        for (const id of rule[field] || []) {
          if (!available.has(id)) problems.push(`${where}: the rule "${rule.id}" still uses the button "${id}" (${field.replace("_", " ")}); take it off the rule first`);
          else if (talkIds.has(id)) problems.push(`${where}: the rule "${rule.id}" names the Talk button "${id}"; a ring is not the moment to start a talk (${field.replace("_", " ")})`);
        }
      }
    }
    const rules = holder.rules || [];
    const ruleWhere = owner === "template" ? "the household rules" : `${name(owner)}'s rules`;
    if (rules.length > MAX_RULES) problems.push(`${ruleWhere}: at most ${MAX_RULES} rules`);
    const ruleIds = new Set();
    for (const rule of rules) {
      const here = `${ruleWhere}, rule "${rule.id}"`;
      if (!ID_RE.test(rule.id || "") || ruleIds.has(rule.id)) problems.push(`${here}: the rule id is missing or used twice`);
      ruleIds.add(rule.id);
      const caller = rule.caller;
      if (typeof caller !== "string" || !caller || caller !== caller.trim() || caller.length > MAX_ENDPOINT_ID) problems.push(`${here}: it needs a caller`);
      else if (caller.startsWith("kind:") && !RULE_KINDS.includes(caller)) problems.push(`${here}: the caller kind must be a door, a phone or a screen`);
      for (const field of ["ring_buttons", "call_buttons"]) {
        if ((rule[field] || []).length > MAX_RULE_BUTTONS) problems.push(`${here}: at most ${MAX_RULE_BUTTONS} ${field.replace("_", " ")}`);
      }
      if (rule.cameras !== null && rule.cameras !== undefined) {
        if (!Array.isArray(rule.cameras) || rule.cameras.length > MAX_CAMERAS) problems.push(`${here}: at most ${MAX_CAMERAS} cameras`);
        else if (rule.cameras.some((c) => !isElementId(c) && !isCameraEntity(c))) problems.push(`${here}: a rule camera is not a camera`);
      }
    }
  }
  problems.push(...validateScreens(doc, ctx));
  return problems;
}

/* The declared screens (`dashboards`): the slug rule, a name and room that fit, an HA user that exists, real cameras,
 * and (when `ctx.otherIds` is given) an address no phone or door already has. `ctx.users`: a Set of HA user ids, or
 * null when the server did not list them (an older server part: the user is not checked). */
function validateScreens(doc, ctx) {
  const problems = [];
  const context = ctx || {};
  for (const [id, d] of Object.entries(dashboardsOf(doc))) {
    const where = `Screen "${(d && d.name) || id}"`;
    if (!ID_RE.test(id)) problems.push(`${where}: the address must be 1-32 characters of a-z, 0-9, _ or -`);
    else if (context.otherIds && context.otherIds.has(id)) problems.push(`${where}: the address "${id}" is already used by a phone or a door`);
    if (!d || typeof d !== "object") { problems.push(`${where}: it is not a screen`); continue; }
    if ((d.name || "").length > MAX_LABEL) problems.push(`${where}: the name is longer than ${MAX_LABEL} characters`);
    if ((d.room || "").length > MAX_LABEL) problems.push(`${where}: the room is longer than ${MAX_LABEL} characters`);
    if (!d.ha_user_id) problems.push(`${where}: pick the Home Assistant user it is logged in as`);
    else if (context.users && !context.users.has(d.ha_user_id)) problems.push(`${where}: its Home Assistant user no longer exists; pick another one or delete the screen`);
    const cameras = d.cameras || [];
    if (cameras.length > MAX_CAMERAS) problems.push(`${where}: at most ${MAX_CAMERAS} cameras`);
    if (cameras.some((c) => !isElementId(c) && !isCameraEntity(c))) problems.push(`${where}: a room camera is not a camera`);
  }
  return problems;
}

/* ---- requests and replies ---------------------------------------------------------------------------- */

function saveRequest(state) {
  // `editorDocument` again, at the door: schema_version 4, pages `{id, label, blocks}`, and NO flat cameras/actions on
  // any page (a stale flat list would win over the blocks on the server, blocks design 3.6).
  const msg = { type: "portico/content/save", document: editorDocument(state.draft), base_doc_rev: state.loaded.doc_rev };
  if (state.drop.length) msg.drop_orphans = state.drop.slice();
  return msg;
}

function previewEndpoint(state) {
  if (isPagesModel(state.draft)) return previewEndpointV6(state);
  const target = selTarget(state.sel);
  if (target && target !== "template") return target;
  const rows = rowsOf(state, ["phone", "dashboard"]);
  if (state.previewAs && rows.some((r) => r.id === state.previewAs)) return state.previewAs;
  return rows.length ? rows[0].id : null;
}

/* Schema 6: a page has no landing, so the preview needs a device to be seen on. The choice, else the first device that shows the
 * page, else the first phone, else the first screen. */
function previewEndpointV6(state) {
  const rows = rowsOf(state, ["phone", "dashboard"]).sort((a, b) => (a.kind === b.kind ? 0 : a.kind === "phone" ? -1 : 1));
  if (state.previewAs && rows.some((r) => r.id === state.previewAs)) return state.previewAs;
  const using = new Set(devicesUsingPage(state.draft, state.pageId).map((d) => d.id));
  const hit = rows.find((r) => using.has(r.id));
  return hit ? hit.id : rows.length ? rows[0].id : null;
}

/* The draft with `pageId` taken as assigned to `endpoint`, for the preview only (nothing is saved): a content page joins the
 * device's page list when it is not there; a Ring page needs nothing. Pure. */
function assumePage(doc, endpoint, pageId) {
  const page = ((doc && doc.pages) || []).find((p) => p.id === pageId);
  if (!page || !endpoint) return doc;
  const out = clone(doc);
  out.devices = out.devices || {};
  if (pageKind(page) === "ring") return out;      // a Ring page is the Talk element's: nothing to assume on the device
  const device = (out.devices[endpoint] = { pages: [], roster_hide: [], ...(out.devices[endpoint] || {}) });
  if (!device.pages.some((e) => e.page === pageId)) {
    if (device.pages.length >= MAX_PAGES) device.pages = device.pages.slice(0, MAX_PAGES - 1);
    device.pages = [...device.pages, { page: pageId }];
  }
  return out;
}

/* `caller`: a door's endpoint id narrows the reply's `callers` to it (the Ring & call preview); absent or "*" asks for all. */
function previewRequest(state, caller) {
  const endpoint = previewEndpoint(state);
  if (!endpoint) return null;
  const msg = { type: "portico/content/preview", document: editorDocument(state.draft), endpoint };
  if (isPagesModel(state.draft) && state.pageId) msg.document = assumePage(msg.document, endpoint, state.pageId);
  if (caller && caller !== UNKNOWN_CALLER) msg.caller = caller;
  return msg;
}

/* What the server's refusal means for the panel. */
function classifyError(err) {
  const code = (err && err.code) || "";
  const text = (err && err.message) || "";
  if (code === "unknown_command" || /unknown command/i.test(text)) return { kind: "needs_restart", message: RESTART_TEXT };
  if (code === "conflict") return { kind: "conflict", message: CONFLICT_TEXT };
  // A document of another schema (a tab left open across the update that introduced blocks): the server says so and
  // the only way on is to reload; the editor offers a button for it.
  if (/schema_version must be/i.test(text)) return { kind: "invalid", message: text, reload: true };
  if (code === "invalid") return { kind: "invalid", message: text || "The server did not accept the changes." };
  // Devices D2: the server's own sentence names the element, the screens or the devices
  if (code === "in_use") return { kind: "invalid", message: text || "The server did not accept the changes." };
  if (code === "unauthorized") return { kind: "unauthorized", message: "Only an administrator can edit Portico." };
  // The Phones tab's refusals (UI split P2). The words are the panel's own: the server's text names no menu.
  if (PHONE_ERROR_TEXT[code]) return { kind: code, message: PHONE_ERROR_TEXT[code], field: err && err.field };
  return { kind: "other", message: text || "Something went wrong." };
}

const PHONE_ERROR_TEXT = {
  needs_tailscale_setup: "Tailscale is not set up for enrolment yet. Finish the Tailscale setup in the Home Assistant dialog (Settings > Devices & services > Portico > Configure), then try again.",
  needs_approval: "This install enrols phones in approve mode, which needs your approval on Tailscale's own page for each phone. Use the Home Assistant dialog: Settings > Devices & services > Portico > Configure > Enroll a device.",
  no_ha_url: "Home Assistant has no address to put in the QR. Set the external or internal URL in Settings > System > Network.",
  no_https_url: "Home Assistant's address is plain http. Tick \"Allow plain http on my own network\" if that is what you want, or set an https address in Settings > System > Network.",
  invalid_ha_dial: "That address is not one the phone can dial. Use a private IP address and a port such as 10.6.0.5:443, or leave the field empty.",
  tailscale_mint_failed: "Tailscale did not give a key for this phone. Check the Tailscale API key in the Home Assistant dialog, then try again.",
  no_push_entry: "Portico's settings entry is missing, so phones cannot be enrolled and house calls cannot be switched. Add Portico under Settings > Devices & services.",
  bad_name: "The phone needs a name of 1 to 24 characters.",
  pending_cap: "Five phones are already waiting to be scanned. Cancel one, or wait for its code to expire, then try again.",
  unknown_phone: "That phone is not in the list any more. Reload the list.",
  confirmation_required: "That change has to be confirmed. Reload the page and try again.",
  bad_patch: "Nothing valid to change. Reload the page and try again.",
  unknown_door: "One of the ticked doors is not a door Portico has now. Untick it, or reload the page.",
  legacy_phone: "This phone was enrolled before phone entries, so only its push ring notification can be changed here.",
  no_registration: "The phone's app has not registered for the push ring notification yet. Open the Portico app on the phone once.",
  unknown: "Something went wrong on the server while doing that. The Home Assistant log has the details.",
};

const PHONES_URL_DEFAULT = "/portico/portico-phones.js";
const SETTINGS_URL_DEFAULT = "/portico/portico-settings.js";
const ELEMENTS_URL_DEFAULT = "/portico/portico-elements.js";
const ELEMENTS_RENDER_URL_DEFAULT = "/portico/portico-elements-render.js";

/* ---- the kiosk route (design 5.2 and 5.3) ---- */

const CONTRACT = 3;
const CONTRACT_TEXT = "This page needs Portico contract 3 and Home Assistant is running a different one: update Home Assistant, then reload this page.";

const KIOSK_RESTART_TEXT = "Portico is being updated. This page works again after Home Assistant restarts.";
const KIOSK_NO_CARD_TEXT = "The Portico card is not loaded on this page, so the screen cannot be drawn. Reload the page; if it stays like this, the Portico card file is not being served.";
const KIOSK_CARD_URL = "/portico/portico-card.js";
const KIOSK_ADDRESS_HINT = "Open this on the screen (for Fully Kiosk, as its start URL). It is built from the address you opened Portico at, so it must be the same address the screen is logged in at; a different address logs the screen out. It only works while the browser is logged in as the user above.";
const KIOSK_NO_ORIGIN_TEXT = "This page cannot tell which address it was opened at, so it cannot build the screen's address. Reload Portico from your Home Assistant address.";
/* The dashboard card for a screen. `path` is optional and only used when the card is NOT already in front: see _cardSnippet. */
function cardSnippetText(slug) {
  return [
    "type: custom:portico-card",
    `endpoint: ${slug}`,
    "takeover: true",
    "# Optional: the URL path of the dashboard view that holds this card, for example /lovelace-kitchen/wall.",
    "# When a call wakes this screen from another page, Home Assistant opens this view first. Remove the line to stay on the current page.",
    "path: /lovelace-kitchen/wall",
  ].join("\n");
}
const KIOSK_NO_SLUG_TEXT = "This address names no screen. Use /portico/kiosk/ followed by the screen's address from Portico > Screens.";
const KIOSK_USERS_TEXT = "The server part of Portico is older than this page and does not list Home Assistant users. Restart Home Assistant, then reload.";

/* null: this is not the kiosk route. Else {slug}: the screen's address, or null when the address is missing or
 * not a valid one. `route` is what HA's custom panel element is given ({prefix: "/portico", path: "/kiosk/<slug>"});
 * `pathname` (the browser's) is the fallback while HA has not set `route`. */
function kioskTarget(route, pathname) {
  let rest = null;
  if (route && typeof route.path === "string") rest = route.path;
  else if (typeof pathname === "string" && (pathname === "/portico" || pathname.startsWith("/portico/"))) rest = pathname.slice("/portico".length);
  if (rest === null) return null;
  const m = /^\/kiosk(?:\/([^/?#]*))?\/?$/.exec(rest);
  if (!m) return null;
  let id = m[1] || "";
  try { id = decodeURIComponent(id); } catch (_err) { id = ""; }
  return { slug: ID_RE.test(id) ? id : null };
}

/* What a `kiosk/hello` reply means: {ok} to draw the card, else {ok: false, message, restart?}. A reply without
 * `endpoint_ok` is an older shape: the same line as unknown_command. */
function kioskOutcome(reply, slugText) {
  if (!reply || typeof reply.endpoint_ok !== "boolean") return { ok: false, restart: true, message: KIOSK_RESTART_TEXT };
  if (reply.endpoint_ok) return { ok: true, kiosk: reply.kiosk && typeof reply.kiosk === "object" ? reply.kiosk : {} };
  return { ok: false, message: `This login has no Portico screen "${slugText}". An administrator declares it in Portico > Screens.` };
}

/* The kiosk page's optional look, from the `kiosk` object of the hello reply (the `kiosk_background_url` and `kiosk_back_path`
 * options of the Portico settings entry, DEV-836). Returns {bg, backPath}: `bg` a CSS value for the background image (an
 * http(s) or same-origin path, quoted), else "none"; `backPath` an in-app route or "". Nothing is built in. */
function kioskLook(kiosk) {
  const k = kiosk && typeof kiosk === "object" ? kiosk : {};
  const u = typeof k.background_url === "string" ? k.background_url.trim() : "";
  const okUrl = /^(https?:\/\/|\/(?![\/\\]))[^\s"'()\\\u0000-\u001f\u007f]+$/.test(u);
  const p = typeof k.back_path === "string" ? k.back_path : "";
  const okPath = p.length > 0 && p.length <= 512 && /^\/(?![\/\\])[^\s\\\u0000-\u001f\u007f]*$/.test(p);
  return { bg: okUrl ? `url("${u}")` : "none", backPath: okPath ? p : "" };
}

function kioskError(err) {
  const c = classifyError(err);
  if (c.kind === "needs_restart") return { ok: false, restart: true, message: KIOSK_RESTART_TEXT };
  return { ok: false, message: c.message };
}

const RESTART_TEXT = "The Portico editor needs the newer Portico server part. Restart Home Assistant to enable the editor.";
const CONFLICT_TEXT = "Another session saved first. Your changes were not saved; the editor now shows the saved version.";

/* Entities of one domain from hass.states, with friendly names, filtered by text, by name. */
function entityOptions(states, domain, filter) {
  const needle = String(filter || "").trim().toLowerCase();
  const out = [];
  for (const [id, st] of Object.entries(states || {})) {
    if (id.split(".")[0] !== domain) continue;
    const name = (st && st.attributes && st.attributes.friendly_name) || id;
    if (needle && !id.toLowerCase().includes(needle) && !String(name).toLowerCase().includes(needle)) continue;
    out.push({ value: id, label: name === id ? id : `${name} (${id})`, name: String(name) });
  }
  return out.sort((a, b) => a.name.localeCompare(b.name) || a.value.localeCompare(b.value));
}

/* The spec chip of one camera: what the picker shows so the owner knows which feed he is choosing. `info` is a row of
 * `portico/cameras/info` ({width, height, fps, source}), `undefined` while it is being asked, or `{failed: true}` /
 * `{source: "unavailable"}`. Returns {text, title, state} with state "loading" | "none" | "ok". The frame rate is the
 * server's (`fps_source: "frigate-stats"` marks Frigate's measured rate, whose tooltip says so), or "fps unknown" when
 * nobody reports it: it is never guessed here. */
const SPEC_UNKNOWN_FPS_TEXT = "Home Assistant does not report this feed's frame rate";
const SPEC_FRIGATE_FPS_TEXT = "The frame rate is Frigate's measured rate of this camera's detect stream, not necessarily the recording stream";
function specChip(info) {
  if (info === undefined || info === null) return { text: "\u2014", title: "Reading this feed's size...", state: "loading" };
  const s = info.stream && typeof info.stream === "object" ? info.stream : null;
  const live = s && Number.isInteger(s.width) && Number.isInteger(s.height) && s.width > 0 && s.height > 0;
  const w = live ? s.width : info.width, h = live ? s.height : info.height;
  if (!(Number.isFinite(w) && Number.isFinite(h) && w > 0 && h > 0)) {
    return {
      text: "\u2014", state: "none",
      title: info.source === "unavailable" ? "This camera is unavailable right now, so its size cannot be read." : "Could not read this feed's size.",
    };
  }
  const size = `${w}x${h}`;
  const fps = Number.isFinite(info.fps) && info.fps > 0 ? info.fps : null;
  const mp = (w * h) / 1e6;
  const megapixels = `about ${mp >= 10 ? mp.toFixed(0) : mp.toFixed(1)} MP`;
  const from = live
    ? "Size is that of the camera's live stream, read from the stream itself; the still the phone receives may be smaller."
    : info.source === "attributes"
      ? "Size and frame rate come from the camera's stream attributes in Home Assistant."
      : "Size is that of the picture the camera returns now (the still the phone receives).";
  const fromFrigate = fps !== null && info.fps_source === "frigate-stats";
  const d = info.detect && typeof info.detect === "object" ? info.detect : null;
  const detect = fromFrigate && d && Number.isFinite(d.width) && Number.isFinite(d.height) && Number.isFinite(d.fps)
    ? ` Frigate's detect stream is set to ${d.width}x${d.height} at ${d.fps} fps.` : "";
  return {
    text: fps === null ? `${size} \u00b7 fps unknown` : `${size} \u00b7 ${fps} fps`,
    title: `${from} ${megapixels}.` + (fps === null ? ` ${SPEC_UNKNOWN_FPS_TEXT}.` : "") + (fromFrigate ? ` ${SPEC_FRIGATE_FPS_TEXT}.${detect}` : ""),
    state: "ok",
  };
}

/* The doors a Portico button can open, from the endpoint rows: a door row's id is `<provider>:<device id>`
 * (split on the FIRST colon; a device id can be a URL) and the button stores the bare device id. */
function doorOptions(state) {
  return rowsOf(state, ["door"]).map((r) => ({ value: r.id.slice(r.id.indexOf(":") + 1), label: rowName(r), name: rowName(r) }));
}

const logic = {
  EditError, KINDS, ICON_NAMES, MAX_LABEL, MAX_PAGES, PREVIEW_DEBOUNCE_MS, RESTART_TEXT, CONFLICT_TEXT, CONTRACT, CONTRACT_TEXT,
  PAGES_HINT, defaultNewPageLabel,
  // Pages unified (P-B): the schema 6 library
  SCHEMA_VERSION_PAGES, MAX_LIBRARY_PAGES, LIBRARY, PAGE_KIND_TEXT, WHEN_TEXT, MIGRATION_BANNER_TEXT, isPagesModel, pageKind,
  devicesUsingPage, shownOnText, addLibraryPage, deleteLibraryPage, ringEntries, editRingEntry, resetRingEntry, ringSeedActions, setRingCamera, setBlockWhen, talkCaller,
  assumePage, validateLibrary, previewEndpointV6,
  // Pages unified (P-C): the Devices selectors and the ownership of Talk elements
  talkPartiesOf, talkIdsOnDevice, ownerKeys, setPanelHosts, setLeftoverKeys, doorOwners, wakeOf, wakesOnText, ringsForText, withDevice, devicePageIds, addDevicePage,
  removeDevicePage, moveDevicePage, addFirstPage, ringPagesFor,
  clone, same, move, slug, pagesOf, effectivePages, addPage, renamePage, deletePage, movePage, overridePage,
  cameraName, addCamera, addCameraRef, detachCamera, repointCamera, allActionIds, addButtonRef, detachButton, repointButton, placeableButton, houseCallTarget, isGroupElement, askLocked, parseCallTarget, inlineButton, buttonBlockProblem, isRef, isElementId, placeableCamera, docHasRefs, SCHEMA_VERSION_REFS, addAction, updateTile, removeTile, moveTile, mergeOrphan,
  // blocks (DEV-658 B3)
  SCHEMA_VERSION, MAX_BLOCKS, MAX_BLOCK_CAMERAS, MAX_BLOCK_ACTIONS, MAX_TITLE, MAX_HOME_NAME, PALETTE, PALETTE_NAMES, CAMERA_SIZES,
  TALK_REASONS, ICON_GROUPS, iconGroup, usableIcon, paletteEntry,
  blocksOf, pageCameras, pageActions, titleFromLabel, editorPage, editorDocument,
  addBlock, renameBlock, moveBlock, deleteBlock, setBlockDoor, moveTileToBlock, splitBlock, overflowBlocks,
  setCameraSize, setCameraLive, setButtonColor, setShowLabel, setHomeName, setShowFps, liveCount, tooManyLive, LIVE_WARN_AFTER, LIVE_WARN_TEXT, createState, rowsOf, rowName, selTarget,
  orphanKeys, validSel, isDirty, commit, undo, discard, moveOrphan, deleteOrphan, validate, saveRequest,
  previewEndpoint, previewRequest, classifyError, entityOptions, doorOptions, specChip, SPEC_BATCH, SPEC_TTL_MS, SPEC_RETRY_MS,
  // K3: screens, rules, the kiosk route
  MAX_RULES, MAX_RULE_BUTTONS, RULE_KINDS, RULE_ORDER_HINT, KIOSK_ADDRESS_HINT, KIOSK_NO_ORIGIN_TEXT, KIOSK_RESTART_TEXT, KIOSK_NO_CARD_TEXT, KIOSK_NO_SLUG_TEXT,
  allRows, kioskUrl, cardSnippetText, takenEndpointIds, addScreen, updateScreen, addScreenCamera, removeScreenCamera, deleteScreen,
  callersOfScreen, screenUserOptions, validateScreens,
  rulesOf, ruleButtonChoices, ruleCallerChoices, ruleCallerLabel, addRule, updateRule, deleteRule, moveRule, toggleId,
  kioskTarget, kioskOutcome, kioskError, kioskLook,
};

/* ---- drawing ---------------------------------------------------------------------------------------- */

const STYLE = `
  :host { display: block; background: var(--primary-background-color); color: var(--primary-text-color);
          min-height: 100%; box-sizing: border-box; }
  /* The kiosk route's page (2026-11-12): the wall tablet's main-screen background (the kitchen-panel view "screen": theme
     screensaver_black, a picture from the kiosk_background_url option, size cover, over black). The portico-card on it is transparent. The host fills
     the viewport and does not scroll (the card is the scroller), so the picture stays put. */
  :host([kiosk]) { position: fixed; top: 0; left: 0; right: 0; bottom: 0; color: #e8eaed;
                   background: #000 var(--portico-kiosk-bg, none) center / cover no-repeat scroll; }
  .page { max-width: 1400px; margin: 0 auto; padding: 16px; }
  header { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; }
  h1 { font-size: 24px; font-weight: 400; margin: 0 16px 0 0; }
  .grow { flex: 1 1 auto; }
  .tabs { display: flex; gap: 8px; overflow-x: auto; max-width: 100%; scrollbar-width: thin; }
  .tabs button { flex: 0 0 auto; white-space: nowrap; }
  @media (max-width: 700px) { header .tabs { flex: 1 1 100%; order: 2; } }
  button { font: inherit; color: var(--primary-color); background: none; border: 1px solid var(--divider-color);
           border-radius: 8px; padding: 5px 12px; cursor: pointer; }
  button:hover:not(:disabled) { background: var(--secondary-background-color); }
  button:disabled { opacity: .45; cursor: default; }
  button.primary { background: var(--primary-color); color: var(--text-primary-color, #fff); border-color: var(--primary-color); }
  button.tab.on, button.sel { background: var(--secondary-background-color); font-weight: 500; }
  button.small { padding: 1px 8px; }
  input, select { font: inherit; color: inherit; background: var(--card-background-color); border: 1px solid var(--divider-color);
                  border-radius: 6px; padding: 4px 8px; max-width: 100%; box-sizing: border-box; }
  .status { color: var(--secondary-text-color); margin: 0 8px; }
  .status.dirty { color: var(--warning-color, #b26a00); }
  .msg { padding: 8px 12px; border-radius: 8px; margin: 8px 0; border: 1px solid var(--divider-color); }
  .msg.err { color: var(--error-color, #db4437); border-color: var(--error-color, #db4437); }
  .msg.ok { color: var(--success-color, #2e7d32); }
  .msg ul { margin: 4px 0 0; padding-left: 20px; }
  .restart { max-width: 60ch; margin: 32px auto; text-align: center; font-size: 16px; }
  .layout { display: grid; grid-template-columns: 210px minmax(0, 1fr) 320px; gap: 16px; align-items: start; }
  .layout.two { grid-template-columns: minmax(0, 1fr) 320px; }
  .layout.one { grid-template-columns: minmax(0, 1fr); }
  .msg.note { color: var(--secondary-text-color); background: var(--secondary-background-color); }
  .chip.kindbadge { display: inline-flex; align-items: center; gap: 4px; font-weight: 600; border: 1px solid var(--primary-color); color: var(--primary-color); }
  .chip.kindbadge.ring { background: var(--primary-color); color: var(--text-primary-color, #fff); }
  .chip.kindbadge ha-icon { --mdc-icon-size: 16px; width: 16px; height: 16px; }
  .fixedcall { display: flex; gap: 8px; margin-top: 10px; }
  .fixedbtn { flex: 1; text-align: center; padding: 8px 10px; border-radius: 20px; color: #fff; font-weight: 600; pointer-events: none; user-select: none; }
  .fixedbtn.answer { background: #2e7d32; }
  .fixedbtn.decline { background: #c62828; }
  .sectionhead { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 12px; margin: 0 0 10px; }
  .sectionhead .sectiontitle { margin: 0; }
  .overlay { position: fixed; inset: 0; z-index: 20; background: rgba(0,0,0,.45); display: flex; align-items: flex-start; justify-content: center; overflow: auto; padding: 24px 12px; }
  .modal { background: var(--card-background-color); color: var(--primary-text-color); border-radius: var(--ha-card-border-radius, 12px); padding: 16px; max-width: 640px; width: 100%; box-sizing: border-box; }
  .modal h2 { font-size: 18px; font-weight: 500; margin: 0 0 8px; }
  .modal .acts { display: flex; gap: 8px; justify-content: flex-end; margin-top: 12px; }
  table.migcounts { border-collapse: collapse; margin: 6px 0; }
  table.migcounts td { padding: 2px 12px 2px 0; }
  @media (max-width: 1000px) { .layout, .layout.two { grid-template-columns: 1fr; } }
  .side, .work, .previewcol { background: var(--card-background-color); border: 1px solid var(--divider-color);
           border-radius: var(--ha-card-border-radius, 12px); padding: 12px; }
  .side h3, .work h3, .previewcol h3, h3.section { font-size: 12px; font-weight: 500; text-transform: uppercase;
           letter-spacing: .06em; color: var(--secondary-text-color); margin: 14px 0 6px; }
  .side h3:first-child { margin-top: 0; }
  .side button { display: block; width: 100%; text-align: left; margin-bottom: 4px; overflow-wrap: anywhere; }
  .hint { color: var(--secondary-text-color); font-size: 13px; margin: 0 0 10px; }
  .devices { display: grid; gap: 12px; }
  .devscreens { background: var(--card-background-color); border: 1px solid var(--divider-color);
                border-radius: var(--ha-card-border-radius, 12px); padding: 12px; }
  .devscreens h2 { font-size: 17px; font-weight: 500; margin: 0 0 6px; }
  .devsel { margin: 8px 0; }
  .devsel h3 { font-size: 15px; font-weight: 500; margin: 8px 0 4px; }
  .devicelist { background: none; border: none; padding: 0; }
  .devsections { display: grid; gap: 12px; margin: 8px 0; }
  .devsection { border: 1px solid var(--divider-color); border-radius: 12px; padding: 12px; background: var(--secondary-background-color); min-width: 0; }
  .devhead { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 12px; }
  .devhead .grow { flex: 1 1 auto; }
  .devhead .devtitle { font-size: 17px; font-weight: 500; margin: 0; overflow-wrap: anywhere; }
  .devhead .menu { position: relative; display: inline-block; flex: 0 0 auto; }
  .page[data-devview]:not([data-devview="list"]) .devscreens, .page[data-devview]:not([data-devview="list"]) .contentacts { display: none; }
  .row .menu { position: relative; display: inline-block; flex: 0 0 auto; }
  button.dots { border: none; border-radius: 50%; width: 36px; height: 36px; padding: 6px; color: var(--secondary-text-color);
                display: inline-flex; align-items: center; justify-content: center; }
  button.dots[aria-expanded="true"] { background: var(--secondary-background-color); }
  button.dots svg { width: 22px; height: 22px; fill: currentColor; }
  .menu .pop { position: absolute; right: 0; top: 100%; z-index: 5; min-width: 150px; display: grid; padding: 4px 0;
               background: var(--card-background-color); border: 1px solid var(--divider-color); border-radius: 8px;
               box-shadow: 0 4px 14px rgba(0,0,0,.25); }
  .menu .pop[hidden] { display: none; }
  .menu .pop button { border: none; border-radius: 0; text-align: left; padding: 10px 16px; color: var(--primary-text-color); }
  .menu .pop button.danger { color: var(--error-color, #db4437); }
  .cols { display: grid; grid-template-columns: minmax(180px, 240px) minmax(0, 1fr); gap: 16px; }
  @media (max-width: 700px) { .cols { grid-template-columns: 1fr; } }
  .pagesections { display: flex; flex-direction: column; gap: 20px; margin-top: 12px; }
  .pagesection { border: 1px solid var(--divider-color); border-radius: 12px; padding: 12px; background: var(--secondary-background-color); min-width: 0; }
  .ringsgroup { margin: 18px 0 6px; font-size: 1.1em; }
  .pagesection.ringdefault .pagedetail, .pagesection.ringremoved .pagedetail { display: flex; flex-direction: column; gap: 8px; align-items: flex-start; }
  .pagesection .sectiontitle { font-size: 18px; font-weight: 500; margin: 0 0 10px; overflow-wrap: anywhere; }
  .sectioncols { display: grid; grid-template-columns: minmax(0, 1fr) 320px; gap: 16px; align-items: start; }
  @media (max-width: 1000px) { .sectioncols { grid-template-columns: 1fr; } }
  .row { display: flex; align-items: center; gap: 6px; padding: 6px 8px; border: 1px solid var(--divider-color);
         border-radius: 8px; margin-bottom: 6px; background: var(--secondary-background-color); }
  .row.cur { border-color: var(--primary-color); }
  .row .txt { flex: 1 1 auto; min-width: 0; }
  .row .label { font-weight: 500; overflow-wrap: anywhere; }
  .row .label.muted { font-weight: 400; font-style: italic; color: var(--secondary-text-color); }
  .row .sub { color: var(--secondary-text-color); font-size: 12px; overflow-wrap: anywhere; }
  .row .ic { flex: 0 0 32px; height: 32px; border-radius: 50%; background: var(--primary-color); color: var(--text-primary-color, #fff);
         display: flex; align-items: center; justify-content: center; --mdc-icon-size: 18px; }
  .chip { font-size: 11px; padding: 1px 7px; border-radius: 10px; border: 1px solid var(--divider-color); color: var(--secondary-text-color); }
  .chip.spec { display: inline-block; justify-self: start; white-space: nowrap; margin-top: 2px; }
  .chip.spec[hidden] { display: none; }
  .chip.elbadge { margin-left: 6px; }
  .row.dangling { border-color: var(--error-color, #b3261e); background: color-mix(in srgb, var(--error-color, #b3261e) 10%, transparent); }
  .row.dangling .chip.elbadge { color: var(--error-color, #b3261e); border-color: var(--error-color, #b3261e); }
  .sourceswitch { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; margin: 4px 0; }
  .sourceswitch button.on { background: var(--primary-color); color: var(--text-primary-color, #fff); }
  .elementlist { display: flex; flex-direction: column; gap: 4px; max-height: 220px; overflow: auto; margin: 4px 0; }
  .elementlist .elrow { text-align: left; padding: 6px 8px; border: 1px solid var(--divider-color); border-radius: 6px; background: transparent; color: inherit; cursor: pointer; }
  .elementlist .elrow.on { border-color: var(--primary-color); font-weight: 600; }
  .elpreview { margin: 6px 0; max-width: 320px; }
  .chip.bad { color: var(--error-color, #db4437); border-color: var(--error-color, #db4437); }
  .form { border: 1px dashed var(--divider-color); border-radius: 8px; padding: 10px; margin: 8px 0; display: grid; gap: 8px; }
  .form label { display: grid; gap: 2px; font-size: 13px; color: var(--secondary-text-color); }
  .form .acts { display: flex; gap: 8px; justify-content: flex-end; }
  .empty { color: var(--secondary-text-color); font-style: italic; margin: 4px 0; }
  .grid { display: grid; gap: 10px; }
  .cams { grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); }
  .btns { grid-template-columns: repeat(auto-fill, minmax(120px, 1fr)); }
  .cam { border: 1px solid var(--divider-color); border-radius: 10px; overflow: hidden; background: var(--secondary-background-color); }
  .pic { position: relative; aspect-ratio: 16 / 9; background: #000; display: flex; align-items: center; justify-content: center; }
  .pic img { width: 100%; height: 100%; object-fit: cover; display: block; }
  .pic .none { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
          color: #ddd; font-size: 13px; text-align: center; padding: 6px; }
  .livebadge { position: absolute; top: 6px; left: 6px; font-size: 10px; font-weight: 600; letter-spacing: .06em; padding: 1px 6px;
          border-radius: 8px; background: #c62828; color: #fff; }
  .livebox { display: inline-flex; align-items: center; gap: 4px; font-size: 13px; white-space: nowrap; }
  .msg.warn { color: var(--warning-color, #b26a00); border: 1px solid var(--warning-color, #b26a00); border-radius: 8px; padding: 6px 10px; margin: 8px 0; }
  .cap { padding: 6px 8px; }
  .cap .label { font-weight: 500; overflow-wrap: anywhere; }
  .cap .sub { color: var(--secondary-text-color); font-size: 11px; overflow-wrap: anywhere; }
  .btn-tile { display: flex; flex-direction: column; align-items: center; gap: 4px; padding: 10px 6px; text-align: center;
          border: 1px solid var(--divider-color); border-radius: 10px; background: var(--secondary-background-color); }
  .btn-tile .ic { width: 38px; height: 38px; border-radius: 50%; background: var(--primary-color); color: var(--text-primary-color, #fff);
          display: flex; align-items: center; justify-content: center; --mdc-icon-size: 22px; }
  .btn-tile .label { font-size: 13px; overflow-wrap: anywhere; }
  .off { opacity: .55; }
  .frame { border: 6px solid #333; border-radius: 22px; padding: 10px; background: var(--primary-background-color); max-width: 300px; margin: 0 auto; }
  .frame .title { font-weight: 500; margin-bottom: 6px; }
  .badge { display: inline-block; font-size: 11px; line-height: 1; padding: 3px 6px; border-radius: 9px; background: var(--primary-color); color: var(--text-primary-color, #fff); }
  .frame .pagetabs { display: flex; gap: 4px; flex-wrap: wrap; margin-bottom: 8px; }
  .urlrow { display: flex; gap: 6px; align-items: center; }
  .urlrow input, .urlrow textarea { flex: 1 1 auto; min-width: 0; }
  .urlrow textarea { font-family: monospace; font-size: 12px; }
  .checks { display: grid; gap: 2px; }
  .checks label { display: flex; gap: 6px; align-items: center; font-size: 14px; color: inherit; }
  .danger { color: var(--error-color, #db4437); border-color: var(--error-color, #db4437); }
  [hidden] { display: none !important; }
  .sr { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
  .block { border: 1px solid var(--divider-color); border-radius: 10px; padding: 10px; margin: 10px 0; background: var(--card-background-color); }
  .block.cur { border-color: var(--primary-color); }
  .blockhead { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  .blockhead input { flex: 1 1 140px; min-width: 0; font-weight: 500; }
  .blockhead select { flex: 0 1 180px; min-width: 0; }
  .block h4 { font-size: 12px; font-weight: 500; text-transform: uppercase; letter-spacing: .06em; color: var(--secondary-text-color); margin: 12px 0 4px; }
  .row select { max-width: 150px; }
  .row .swatchdot { display: inline-block; width: 14px; height: 14px; border-radius: 4px; border: 1px solid var(--divider-color); vertical-align: -2px; margin-right: 4px; }
  .swatches, .icongrid { display: flex; flex-wrap: wrap; gap: 6px; }
  .swatch { display: inline-flex; align-items: center; gap: 4px; padding: 3px 10px; border-radius: 14px; border: 2px solid transparent; font-size: 13px; }
  .swatch.none { border-color: var(--divider-color); color: var(--primary-text-color); }
  .swatch[aria-checked="true"], .iconbtn[aria-checked="true"] { outline: 3px solid var(--primary-text-color); outline-offset: 1px; font-weight: 600; }
  .swatch:focus-visible, .iconbtn:focus-visible { outline: 3px solid var(--primary-color); outline-offset: 2px; }
  .iconbtn { width: 38px; height: 38px; padding: 0; display: inline-flex; align-items: center; justify-content: center; --mdc-icon-size: 22px; }
  .iconbtn.wide { width: auto; padding: 0 12px; }
  .pickerlab { display: grid; gap: 4px; }
  .pickerlab .hint { margin: 0; }
  .icongroups { max-height: 220px; overflow: auto; border: 1px solid var(--divider-color); border-radius: 8px; padding: 6px; }
  .icongroup h5 { font-size: 11px; font-weight: 500; text-transform: uppercase; letter-spacing: .06em; color: var(--secondary-text-color); margin: 8px 0 4px; }
  .iconname { min-height: 1.3em; font-size: 12px; color: var(--secondary-text-color); }
  .blockbox { margin: 8px 0 12px; }
  .bhead { font-weight: 600; margin: 4px 0; overflow-wrap: anywhere; }
  .blkcams { grid-template-columns: 1fr 1fr; gap: 6px; }
  .blkcams .cam.big { grid-column: 1 / -1; }
  .btn-tile.colored { border-width: 2px; }
  .btn-tile.colored .chip { color: inherit; border-color: currentColor; }
  .split { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
  .kiosk-msg { box-sizing: border-box; min-height: 100vh; display: flex; align-items: center; justify-content: center;
               text-align: center; padding: 24px; font-size: 20px; max-width: 60ch; margin: 0 auto; }
  portico-card.kiosk { display: block; width: 100%; min-height: 100vh; }
`;

function el(doc, tag, cls, text) {
  const node = doc.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

function withStamp(path, stamp) {
  if (!stamp || path.startsWith("data:")) return path;
  return path + (path.includes("?") ? "&" : "?") + "_=" + stamp;
}

function plural(n, one, many) {
  return `${n} ${n === 1 ? one : many}`;
}

/* A camera element's optional small stream (options.small_entity_id, DEV-768), or "". */
function smallOf(e) {
  const o = e && e.options && typeof e.options === "object" ? e.options : {};
  return typeof o.small_entity_id === "string" ? o.small_entity_id : "";
}

/* The camera line for an element: its stream, or "big + small" when it has both (DEV-768). */
function streamsText(e) {
  const small = smallOf(e);
  return small ? `${e.entity_id || "no entity"} + ${small}` : (e.entity_id || "no entity");
}

/* A one-line "what it points at" for an element in a list: a camera's entity, a button's entity or target. */
function elementWhat(e) {
  if (isGroupElement(e)) return { everyone: "Calls everyone", phones: "Calls all phones", panels: "Calls all panels" }[e.options.group];
  if (e.kind === "talk") return (e.options && (e.options.door || e.options.screen || e.options.phone)) || "no target";
  if (e.kind === "button") {
    const o = e.options || {};
    return e.entity_id || (o.target ? String(o.target) : KIND_TEXT[o.action] || o.action || "button");
  }
  return e.kind === "camera" ? streamsText(e) : (e.entity_id || "no entity");
}

function haIcon(doc, name) {
  const icon = doc.createElement("ha-icon");
  icon.setAttribute("icon", name);
  return icon;
}

const iconOf = (action, element) => (isRef(action) && element ? iconOf({ icon: element.icon, kind: buttonAction(element) }) : action.icon ? `mdi:${String(action.icon).replace(/^mdi:/, "")}` : KIND_ICON[action.kind] || "mdi:gesture-tap");

/* One camera tile of a resolved page (the app's Home tile). `size` ("big" | "small", blocks only): the tile spans the
 * whole row or half of it, and says which in words. */
function cameraTile(doc, camera, picture, stamp, size) {
  const tile = el(doc, "div", "cam" + (size ? ` ${size}` : "") + (camera.available === false ? " off" : ""));
  if (size) tile.dataset.size = size;
  const pic = el(doc, "div", "pic");
  if (picture && camera.available !== false) {
    const img = el(doc, "img");
    img.alt = camera.label || "Camera";
    img.dataset.picture = picture;
    img.src = withStamp(picture, stamp);
    img.addEventListener("error", () => { img.remove(); pic.append(el(doc, "div", "none", "No picture")); });
    pic.append(img);
  } else {
    pic.append(el(doc, "div", "none", camera.available === false ? "Camera unavailable" : "No picture"));
  }
  if (camera.live === true && camera.available !== false) {      // the resolved item's additive flag: this tile plays live video on a screen
    tile.classList.add("live");
    const badge = el(doc, "span", "livebadge", "LIVE");
    badge.dataset.live = "true";
    pic.append(badge);
  }
  tile.append(pic);
  const cap = el(doc, "div", "cap");
  if (camera.label) cap.append(el(doc, "div", "label", camera.label));   // an empty name draws no name line
  cap.append(el(doc, "div", "sub", size ? `${camera.entity_id} · ${size}` : camera.entity_id));
  tile.append(cap);
  return tile;
}

/* The words for a Talk button's reason, or the default one. */
const talkReasonText = (reason) => TALK_REASONS[reason] || TALK_REASONS.talk_off;

/* A palette entry's fill and text for the theme, or null for no colour. */
function paletteColors(name, dark) {
  const entry = paletteEntry(name);
  return entry ? (dark ? entry.dark : entry.light) : null;
}

/* One button as the phone draws it: the icon, the text (unless `show_label` is false; it is then kept for screen
 * readers), the palette colour for the theme (`dark`), grey when unavailable whatever the colour, and the words that
 * say why (so colour is never the only signal). */
function actionTile(doc, action, dark) {
  const off = action.available === false;
  const tile = el(doc, "div", "btn-tile" + (off ? " off" : ""));
  const pal = off ? null : paletteColors(action.color, dark);
  const ic = el(doc, "div", "ic");
  if (action.color) tile.dataset.color = action.color;
  if (pal) {
    tile.classList.add("colored");
    tile.style.background = pal.fill;
    tile.style.color = pal.text;
    tile.style.borderColor = pal.fill;
    ic.style.background = "transparent";
    ic.style.color = pal.text;
  }
  ic.append(haIcon(doc, iconOf(action)));
  tile.append(ic);
  if (action.show_label === false) {
    tile.dataset.textHidden = "true";
    tile.title = action.label;
    tile.append(el(doc, "span", "sr", action.label));
  } else {
    tile.append(el(doc, "div", "label", action.label));
  }
  if (action.kind === "talk") tile.append(el(doc, "span", "chip", talkReasonText(action.reason)));
  else if (off) tile.append(el(doc, "span", "chip bad", "unavailable"));
  if (action.confirm && !NO_CONFIRM_KINDS.has(action.kind)) tile.append(el(doc, "span", "chip", "requires confirmation"));
  return tile;
}

/* One resolved block: its title, the cameras in the phone's grid (a big one spans the row, small ones sit two to a
 * row; the size is the server's effective one), then the buttons. */
function drawBlock(doc, block, pictureOf, stamp, dark) {
  const sec = el(doc, "section", "blockbox");
  sec.dataset.block = block.id;
  const head = el(doc, "div", "bhead", block.title);
  head.setAttribute("role", "heading");
  head.setAttribute("aria-level", "3");
  sec.append(head);
  if (block.door && block.door_available === false) sec.append(el(doc, "span", "chip bad", "door unavailable"));
  const cameras = block.cameras || [];
  const actions = block.actions || [];
  if (cameras.length) {
    const grid = el(doc, "div", "grid blkcams");
    cameras.forEach((camera, n) => {
      const size = camera.size === "big" || camera.size === "small" ? camera.size : n === 0 ? "big" : "small";
      grid.append(cameraTile(doc, camera, pictureOf(camera.entity_id), stamp, size));
    });
    sec.append(grid);
  }
  if (actions.length) {
    const grid = el(doc, "div", "grid btns");
    grid.style.marginTop = "8px";
    for (const action of actions) grid.append(actionTile(doc, action, dark));
    sec.append(grid);
  }
  if (!cameras.length && !actions.length) sec.append(el(doc, "div", "empty", "Empty block."));
  return sec;
}

/* A resolved page as the app's Home draws it: block by block (title, cameras, buttons). A page with no `blocks` (an
 * older server's reply) is drawn flat, cameras above and buttons below. */
function drawPage(doc, page, pictureOf, stamp, dark) {
  const box = el(doc, "div", "pagebox");
  if (Array.isArray(page.blocks)) {
    for (const block of page.blocks) box.append(drawBlock(doc, block, pictureOf, stamp, dark));
    if (!page.blocks.length) box.append(el(doc, "div", "empty", "Nothing on this page yet."));
    return box;
  }
  if (page.cameras.length) {
    const grid = el(doc, "div", "grid cams");
    for (const camera of page.cameras) grid.append(cameraTile(doc, camera, pictureOf(camera.entity_id), stamp));
    box.append(grid);
  }
  if (page.actions.length) {
    const grid = el(doc, "div", "grid btns");
    grid.style.marginTop = "10px";
    for (const action of page.actions) grid.append(actionTile(doc, action, dark));
    box.append(grid);
  }
  if (!page.cameras.length && !page.actions.length) box.append(el(doc, "div", "empty", "Nothing on this page yet."));
  return box;
}

class PorticoPanel extends HTMLElement {
  constructor() {
    super();
    this._root = this.attachShadow({ mode: "open" });
    this._hass = null;
    this._unsub = null;
    this._subscribing = false;
    this._timer = null;
    this._stamp = Date.now();
    this._phase = "loading";     // loading | needs_restart | error | ready | readonly
    this._error = "";
    this._state = null;
    this._tab = "pages";         // the first tab (Elements) takes over once its module has loaded, unless a tab was already picked
    this._tabChosen = false;
    this._msg = null;            // {kind: "ok" | "err", text}
    this._stale = false;         // the stored document changed under an unsaved draft
    this._saving = false;
    this._dialog = null;
    this._camInfo = new Map();   // camera entity id -> {width, height, fps, source, at} | {failed: true, at}
    this._camPending = new Set();
    this._own = null;            // the non-administrator's own pages
    this._preview = null;        // {ok, result | message}
    this._previewPage = null;
    this._previewMs = PREVIEW_DEBOUNCE_MS;
    this._previewTimer = null;
    this._previewGen = 0;
    this._sections = new Map();  // schema 6 Pages tab: page id -> {node, sig, state}, one container per page, rebuilt only when its page changed
    this._secPreviews = new Map();    // page id -> {ok, result, caller | message}: each section's preview
    this._secAs = new Map();     // page id -> the device its "Preview as" names
    this._secKey = new Map();    // page id -> what its last preview request was about (an edit elsewhere asks for nothing)
    this._secTimers = new Map(); // page id -> its debounce timer
    this._secGen = new Map();    // page id -> generation of its newest request (an older reply is dropped)
    this._secQueue = [];         // page ids waiting for a request slot
    this._secInflight = 0;
    this._route = null;          // HA's {prefix, path} for a custom panel
    this._kiosk = null;          // null, or {slug} while the kiosk route is showing
    this._started = false;
    this._contractOk = false;    // the version probe passed for this start: nothing subscribes before it
    this._kioskGen = 0;
    this._kioskMsg = "";
    this._card = null;
    this._cardWaitMs = 4000;
    this._readyBound = null;     // the connection a "ready" listener is bound to
    this._onReady = () => this._hello();
    this._copied = false;
    this._panelInfo = null;      // HA's panel object: `config.phones_module` is the Phones module's hashed URL
    this._phones = null;         // the Phones tab's controller, once its module has loaded (an admin only)
    this._phonesGen = 0;
    this._settings = null;       // the Settings tab's controller (UI split P4 to P6), loaded the same way
    this._status = null;         // the Status tab's controller: the same module in "status" mode (the read-only readouts of what was the Settings tab)
    this._appSettings = null;    // the same module in "app" mode: the "Push ring notification and app" block (DEVICES-09); on the Settings tab for schema 6, in the Devices tab's phone list before
    this._settingsGen = 0;
    this._elements = null;       // the Elements tab controller (DEV-760 E4, read-only), loaded the same way
    this._elementsGen = 0;
    this._elList = { status: "idle", map: null };   // camera references (DEV-760 P2): the elements by id, from portico/elements/list
    this._cfg = null;                                // DEV-824 P5: portico/config/status, the YAML files' state for the banner
    this._elRender = null;       // the shared renderer module, once the Elements tab loaded it (the dialog's live preview)
    this._elPreview = null;      // the dialog's preview handle
    this._pickerSource = {};     // per camera picker (rule / screen): "elements" | "define", unset = automatic
    this._mig = null;            // Pages unified: the migration dialog (legacy documents), see _checkMigration
    this._pageForm = { label: "" };   // the Add page form of the schema 6 Pages tab (a Content page: a Ring page appears by itself per Talk element)
    this._ringAsk = null;        // Ring entries: the Edit / Reset confirmation open on a Talk element, `{talk, what: "edit"|"reset"}`
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (this._elements) this._elements.setHass(hass);      // live previews follow the states (E4)
    if (this._elPreview) { if (this._dialog) this._elPreview.update(hass.states); else this._dropElPreview(); }
    if (first) this._start();
    else if (this._card) this._card.hass = hass;
    this._themeCheck();
  }

  set narrow(_value) {}
  set panel(value) {
    this._panelInfo = value && typeof value === "object" ? value : null;
  }

  /* HA gives a custom panel its sub-path here: {prefix: "/portico", path: "/kiosk/<slug>"}, set again when the
   * browser navigates inside the panel. A change between the editor and the kiosk (or to another screen) restarts. */
  set route(value) {
    this._route = value && typeof value === "object" ? value : null;
    if (!this._started) return;
    const next = this._kioskNow();
    if ((next && next.slug) !== (this._kiosk && this._kiosk.slug) || !next !== !this._kiosk) {
      this._leave();
      this._start();
    }
  }

  get route() {
    return this._route;
  }

  _kioskNow() {
    const loc = this.ownerDocument && this.ownerDocument.defaultView && this.ownerDocument.defaultView.location;
    return kioskTarget(this._route, loc && loc.pathname);
  }

  _start() {
    this._started = true;
    this._kiosk = this._kioskNow();
    if (this._kiosk) {
      this._loadKiosk();
      return;
    }
    this._startEditor();
  }

  /* Contract 3 is expected: ask once per start, and stop with one line when the server speaks anything else.
   * A refusal other than unknown_command (a dropped connection) does not block; the load reports its own error. */
  async _probe() {
    try {
      const r = await this._hass.callWS({ type: "portico/version" });
      return r && r.contract === CONTRACT ? "" : CONTRACT_TEXT;
    } catch (err) {
      return classifyError(err).kind === "needs_restart" ? CONTRACT_TEXT : "";
    }
  }

  async _startEditor() {
    const gen = this._kioskGen;
    const bad = await this._probe();
    if (gen !== this._kioskGen || this._kiosk) return;
    if (bad) {
      this._phase = "needs_restart";
      this._error = bad;
      this._render();
      this._ensurePhones();      // the tab bar is there in this phase too; the tab says what the server answers
      this._ensureSettings();
      this._ensureElements();
      return;
    }
    this._contractOk = true;
    this._ensurePhones();
    this._ensureSettings();
    this._ensureElements();
    this._load();
    this._subscribe();
  }

  /* The Phones tab (UI split P2) is its own module, imported lazily. It does not wait for the content document and a
   * failed import leaves no tab and breaks nothing: the content tabs are drawn by this file alone. */
  async _ensurePhones() {
    if (this._phones || !this._isAdmin || this._kiosk) return;
    const gen = ++this._phonesGen;
    const cfg = this._panelInfo && this._panelInfo.config;
    const url = (cfg && typeof cfg.phones_module === "string" && cfg.phones_module) || PHONES_URL_DEFAULT;
    try {
      const mod = await this._importPhones(url);
      if (gen !== this._phonesGen || this._phones || this._kiosk) return;
      this._phones = new mod.PhonesController({
        doc: this.ownerDocument, getHass: () => this._hass, classify: classifyError,
        canPreview: (name) => this._phoneEndpointId(name) !== null, previewPhone: (name) => this._previewPhone(name),
        onView: (view) => this._devView(view),
        aboveList: () => (this._appSettings && !this._perDevice() ? this._appSettings.el : null),
        deviceBody: (row) => this._phoneDeviceBody(row),
        onPanelMade: () => this._onContentChanged(),          // DEV-807: the new screen is in the content document now
        nextStep: () => (this._perDevice() ? "Next step: once the phone has signed in, give it a page. Its section on this tab will say \"This phone has no page yet\" and offer Add first page." : ""),
      });
    } catch (_err) {
      this._phones = null;       // no House calls or Phones section; everything else works
      return;
    }
    if (this._tab === "devices" || (this._tab === "settings" && this._perDevice())) this._phones.show();
    if (this._appSettings) this._phones._draw();
    if (this._settings) this._settings._draw();          // the Settings tab holds House calls (schema 6)
    this._render();
  }

  _importPhones(url) { return import(url); }

  /* The Phones part draws its own sub-pages (enrol, QR, one phone, revoke, the house calls confirmation) in the Devices tab.
   * While one is open the Screens section and the Save bar are hidden by this one attribute (the style does the hiding);
   * no re-render is needed, so the sub-page keeps its place. */
  _devView(view) {
    const page = this.shadowRoot && this.shadowRoot.querySelector(".page");
    if (page) page.dataset.devview = view;
  }

  /* The Settings tab (UI split P4 to P6) is its own module too, imported the same way and for the same reason: a failed
   * import leaves no Settings tab and breaks nothing. */
  async _ensureSettings() {
    if (this._settings || !this._isAdmin || this._kiosk) return;
    const gen = ++this._settingsGen;
    const cfg = this._panelInfo && this._panelInfo.config;
    const url = (cfg && typeof cfg.settings_module === "string" && cfg.settings_module) || SETTINGS_URL_DEFAULT;
    try {
      const mod = await this._importSettings(url);
      if (gen !== this._settingsGen || this._settings || this._kiosk) return;
      this._settings = new mod.SettingsController({ doc: this.ownerDocument, getHass: () => this._hass, classify: classifyError, fps: this._fpsApi(),
        below: () => (this._perDevice() ? [this._appSettings ? this._appSettings.el : null] : []) });
      this._appSettings = new mod.SettingsController({ doc: this.ownerDocument, getHass: () => this._hass, classify: classifyError, mode: "app" });
      this._status = new mod.SettingsController({ doc: this.ownerDocument, getHass: () => this._hass, classify: classifyError, mode: "status" });
    } catch (_err) {
      this._settings = null;     // no Settings or Status tab and no app block; everything else works
      this._appSettings = null;
      this._status = null;
      return;
    }
    if (this._tab === "status") this._status.show();
    if (this._tab === "settings") { this._settings.show(); if (this._perDevice()) this._appSettings.show(); }
    if (this._tab === "devices") this._appSettings.show();
    if (this._phones) this._phones._draw();          // the block sits inside the Phones list (schema 4/5)
    this._settings._draw();                          // the Settings tab holds the block and House calls (schema 6)
    this._render();
  }

  _importSettings(url) { return import(url); }

  /* Schema 6 lays the Devices tab out as one section per device; House calls and the push ring notification and app go to Settings. */
  _perDevice() {
    return !!(this._state && this._state.draft && isPagesModel(this._state.draft));
  }

  /* The frame-rate switch lives on the Settings tab but is a field of the content document (`show_fps`), so the Settings module
   * gets this pair: `read()` -> {on}, `write(on)` saves the stored document with only that field changed (the same
   * portico/content/save the Save bar sends, at the doc_rev just read; a stale rev is refused by the server). Pages-tab drafts
   * are untouched; the content subscription tells an open editor the document moved. */
  _fpsApi() {
    return {
      read: async () => {
        const loaded = await this._hass.callWS({ type: "portico/content/document" });
        return { on: !!loaded && !!loaded.document && loaded.document.show_fps === true };
      },
      write: async (on) => {
        const loaded = await this._hass.callWS({ type: "portico/content/document" });
        const doc = setShowFps(editorDocument(loaded.document), !!on);
        await this._hass.callWS({ type: "portico/content/save", document: doc, base_doc_rev: loaded.doc_rev });
        return { on: !!on };
      },
    };
  }

  /* The Elements tab (DEV-760 E4, read-only) is its own module too. It draws with the shared renderer module, which the
   * panel imports from its config and hands in; if either import fails there is no Elements tab and nothing else breaks. */
  async _ensureElements() {
    if (this._elements || !this._isAdmin || this._kiosk) return;
    const gen = ++this._elementsGen;
    const cfg = this._panelInfo && this._panelInfo.config;
    const url = (cfg && typeof cfg.elements_module === "string" && cfg.elements_module) || ELEMENTS_URL_DEFAULT;
    const renderUrl = (cfg && typeof cfg.elements_render_module === "string" && cfg.elements_render_module) || ELEMENTS_RENDER_URL_DEFAULT;
    try {
      const [mod, render] = await Promise.all([this._importElements(url), this._importElementsRender(renderUrl)]);
      if (gen !== this._elementsGen || this._elements || this._kiosk) return;
      this._elRender = render;
      this._elements = new mod.ElementsController({ doc: this.ownerDocument, getHass: () => this._hass, classify: classifyError, render, entityOptions });
    } catch (_err) {
      this._elements = null;     // no Elements tab; everything else works
      return;
    }
    if (!this._tabChosen && this._tab === "pages") this._tab = "elements";     // Elements is the first tab and the one the panel opens on
    if (this._tab === "elements") this._elements.show();
    this._render();
  }

  _importElements(url) { return import(url); }
  _importElementsRender(url) { return import(url); }

  /* The controller of the module tab that is open (Settings or Elements), or null on a content tab. The Devices tab is a
   * content tab while the content loads (it holds the Screens draft), but when the content failed it is the Phones part alone. */
  _hosted() {
    if (this._tab === "devices" && this._phase !== "ready") return this._phones;
    if (this._tab === "settings") return this._settings;
    if (this._tab === "status") return this._status;
    if (this._tab === "elements") return this._elements;
    return null;
  }

  /* "Preview as this phone" (UI split P3): the Phones tab asks for the content preview of a phone by its NAME, because
   * the phones command never carries the endpoint id (it is the enrolment's credential). The content document the panel
   * already holds has the ids; a name that two phones share, or that the document does not have (the store is broken,
   * or the phone has no content yet), has no link. */
  _phoneEndpointId(name) {
    if (this._phase !== "ready" || !this._state) return null;
    const rows = rowsOf(this._state, ["phone"]).filter((r) => r.name === name);   // not the label: that carries the enrolment date
    return rows.length === 1 ? rows[0].id : null;
  }

  _previewPhone(name) {
    const id = this._phoneEndpointId(name);
    if (id === null) return false;
    const state = this._state;
    state.sel = `ep:${id}`;
    state.pageId = null;
    state.ruleId = null;
    this._dialog = null;
    this._msg = null;
    this._tabChosen = true; this._tab = "pages";
    this._changed();
    return true;
  }

  /* Drop whatever the current mode holds before the other mode starts. */
  _leave() {
    this._kioskGen++;
    this._contractOk = false;
    this._unbindReady();
    this._card = null;
    this._kioskMsg = "";
    if (this._unsub) {
      this._unsub();
      this._unsub = null;
    }
    this._phase = "loading";
    this._state = null;
  }

  connectedCallback() {
    this._timer = setInterval(() => this._refreshPictures(), REFRESH_MS);
    if (this._kiosk) this._bindReady();
    else if (this._hass && this._contractOk && !this._unsub && !this._subscribing) {
      this._subscribe();
      this._ensurePhones();
      this._ensureSettings();
      this._ensureElements();
    }
  }

  disconnectedCallback() {
    clearInterval(this._timer);
    this._timer = null;
    clearTimeout(this._previewTimer);
    this._previewTimer = null;
    for (const t of this._secTimers.values()) clearTimeout(t);
    this._secTimers.clear();
    this._unbindReady();
    if (this._unsub) {
      this._unsub();
      this._unsub = null;
    }
    // The Phones tab's timers and its QR's blob URL go with the page; a page opened again makes a new controller.
    this._phonesGen++;
    if (this._phones) {
      this._phones.dispose();
      this._phones = null;
    }
    this._settingsGen++;
    if (this._settings) {
      this._settings.dispose();
      this._settings = null;
    }
    if (this._appSettings) {
      this._appSettings.dispose();
      this._appSettings = null;
    }
    if (this._status) {
      this._status.dispose();
      this._status = null;
    }
    this._elementsGen++;
    if (this._elements) {
      this._elements.dispose();
      this._elements = null;
    }
  }

  /* Dark theme? Button colours are drawn with the palette's dark values then (a pale fill, dark text). */
  get _dark() {
    return !!(this._hass && this._hass.themes && this._hass.themes.darkMode);
  }

  /* The editor and its preview redraw when the theme flips, so the swatches and the preview buttons match it. */
  _themeCheck() {
    const dark = this._dark;
    if (this._darkSeen === undefined) { this._darkSeen = dark; return; }
    if (dark === this._darkSeen) return;
    this._darkSeen = dark;
    if (this._phase === "ready" || this._phase === "readonly") this._render();
  }

  get _isAdmin() {
    return !(this._hass && this._hass.user && this._hass.user.is_admin === false);
  }

  /* ---- loading, subscribing ---- */

  async _load() {
    if (!this._isAdmin) return this._loadOwn();
    let loaded;
    try {
      loaded = await this._hass.callWS({ type: "portico/content/document" });
    } catch (err) {
      const c = classifyError(err);
      this._phase = c.kind === "needs_restart" ? "needs_restart" : "error";
      this._error = c.message;
      this._render();
      return false;
    }
    const keep = this._state && this._phase === "ready" ? this._state : null;
    this._state = createState(loaded, keep);
    if (!validSelOf(this._state)) { this._state.sel = "template"; this._state.pageId = null; }
    this._phase = "ready";
    this._error = "";
    this._stale = false;
    this._dialog = null;
    this._render();
    this._schedulePreview();
    this._loadConfigStatus();
    return true;
  }

  /* DEV-824 P5: the state of the YAML files, for the banner. Read-only, best effort: a server without the command (or a failed
   * read) leaves the editor exactly as it was, with no banner. Runs after every load, so a content_changed refreshes it too. */
  async _loadConfigStatus() {
    if (!this._isAdmin || !this._hass || this._kiosk) return;
    let status = null;
    try {
      const reply = await this._hass.callWS({ type: "portico/config/status" });
      status = reply && typeof reply === "object" ? reply : null;
    } catch (_err) { /* no banner */ }
    if (same(status, this._cfg)) return;
    this._cfg = status;
    if (this._phase === "ready") this._render();
  }

  /* The banner: what the file configuration under /config/portico is doing (design 5.2, 5.3, 5.6). One box per state, none when all is
   * well. "Off" and "no directory" are a quiet note, not an error. */
  _configBanner() {
    const cfg = this._cfg;
    if (!this._isAdmin || !cfg || typeof cfg !== "object") return null;
    const doc = this.ownerDocument;
    const wrap = el(doc, "div", "cfgfiles");
    wrap.dataset.state = "config-files";
    const add = (kind, state, text) => { const b = el(doc, "div", `msg ${kind}`, text); b.dataset.state = state; wrap.append(b); return b; };
    const count = (n, one, many) => `${n} ${n === 1 ? one : many}`;
    if (cfg.enabled === false) add("note", "config-off", "File configuration is off, so the editor does not write YAML files and portico.reload_config is refused.");
    else if (cfg.dir_exists === false) add("note", "config-off", "File configuration is off: there is no portico folder in the Home Assistant configuration. Run portico.export_config to create it.");
    const werr = cfg.last_write_error;
    if (werr && werr.count > 0) {
      const box = add("err", "config-not-written", `The last save is live, but ${count(werr.count, "configuration file", "configuration files")} could not be written. The next save tries again.`);
      const list = el(doc, "ul");
      for (const e of (werr.errors || []).slice(0, CFG_SHOWN)) if (e && e.path) list.append(el(doc, "li", null, `${e.path} (${e.code || "error"})`));
      if (list.children.length) box.append(list);
    }
    const pending = Array.isArray(cfg.pending) ? cfg.pending : [];
    const total = Math.max(Number(cfg.pending_total) || 0, pending.length);
    if (total > 0) {
      const box = add("warn", "config-pending", `${count(total, "configuration file is", "configuration files are")} edited by hand and not yet reloaded. The editor leaves them as they are; run portico.reload_config to apply them.`);
      const list = el(doc, "ul");
      for (const path of pending.slice(0, CFG_SHOWN)) list.append(el(doc, "li", null, String(path)));
      if (total > Math.min(pending.length, CFG_SHOWN)) list.append(el(doc, "li", null, `and ${total - Math.min(pending.length, CFG_SHOWN)} more`));
      box.append(list);
    }
    const last = cfg.last_reload;
    if (last && typeof last === "object") {
      if (last.ok === false) {
        const errors = Array.isArray(last.errors) ? last.errors : [];
        const n = Math.max(Number(last.error_total) || 0, errors.length);
        const box = add("err", "config-reload-errors", `The last reload was refused (${count(n, "error", "errors")}); nothing was applied.`);
        const list = el(doc, "ul");
        for (const e of errors.slice(0, CFG_SHOWN)) list.append(el(doc, "li", null, `${e.file || "(tree)"}:${e.line || 1}:${e.col || 1} ${e.message || ""}`.trim()));
        if (n > Math.min(errors.length, CFG_SHOWN)) list.append(el(doc, "li", null, `and ${n - Math.min(errors.length, CFG_SHOWN)} more`));
        box.append(list);
      } else if (last.deferred === true) {
        add("warn", "config-reload-deferred", "The last reload was deferred: a call is live, so nothing was applied. Run portico.reload_config again when the house is idle.");
      }
    }
    return wrap.children.length ? wrap : null;
  }

  async _loadOwn() {
    try {
      const result = await this._hass.callWS({ type: "portico/content/get" });
      this._own = result;
      this._phase = "readonly";
      this._error = "";
    } catch (err) {
      this._phase = "error";
      this._error = "This login has no Portico phone or screen.";
    }
    this._render();
  }

  /* ---- the kiosk route: say hello, then draw the card and nothing else ---- */

  async _loadKiosk() {
    const gen = ++this._kioskGen;
    const target = this._kiosk;
    this._phase = "kiosk";
    this._card = null;
    this._kioskMsg = "";
    this._render();
    if (!target.slug) {
      this._kioskMsg = KIOSK_NO_SLUG_TEXT;
      this._render();
      return;
    }
    this._bindReady();
    const bad = await this._probe();
    if (gen !== this._kioskGen) return;
    if (bad) {
      this._kioskMsg = bad;
      this._render();
      return;
    }
    let outcome;
    try {
      outcome = kioskOutcome(await this._hass.callWS({ type: "portico/kiosk/hello", endpoint: target.slug }), target.slug);
    } catch (err) {
      outcome = kioskError(err);
    }
    if (gen !== this._kioskGen) return;
    if (!outcome.ok) {
      this._kioskMsg = outcome.message;
      this._render();
      return;
    }
    this._kioskLook = kioskLook(outcome.kiosk);
    this.style.setProperty("--portico-kiosk-bg", this._kioskLook.bg);
    await this._whenCard();
    if (gen !== this._kioskGen) return;
    let loadNote = "";
    if (!customElements.get("portico-card")) {
      loadNote = await this._loadCardFile();      // HA's own loader fails silently (console only): load it here and keep the error
      if (gen !== this._kioskGen) return;
    }
    if (!customElements.get("portico-card")) {
      this._kioskMsg = loadNote || KIOSK_NO_CARD_TEXT;
      this._render();
      return;
    }
    try {
      this._card = this._makeCard(target.slug);
    } catch (err) {
      this._kioskMsg = `The Portico card did not accept its settings: ${(err && err.message) || err}`;
    }
    this._render();
  }

  async _whenCard() {
    if (customElements.get("portico-card")) return;
    let timer;
    await Promise.race([
      customElements.whenDefined("portico-card"),
      new Promise((resolve) => { timer = setTimeout(resolve, this._cardWaitMs); }),
    ]);
    clearTimeout(timer);
  }

  /* The card file did not define the element in time: import it ourselves, once. Returns "" when the element is
     defined afterwards, else the message to show, with the real browser error and the URL tried. */
  async _loadCardFile() {
    const url = `${KIOSK_CARD_URL}?v=${Date.now()}`;
    try {
      await this._importCard(url);
    } catch (err) {
      if (customElements.get("portico-card")) return "";     // "already been defined": HA's loader won the race
      const name = (err && err.name) || "Error";
      const detail = (err && err.message) || String(err);
      return `The Portico card file could not be loaded from ${url}. The browser said: ${name}: ${detail}`;
    }
    await this._whenCard();
    if (customElements.get("portico-card")) return "";
    return `${KIOSK_NO_CARD_TEXT} The file ${url} loaded but did not define the card.`;
  }

  _importCard(url) { return import(url); }

  _makeCard(slugText) {
    const card = this.ownerDocument.createElement("portico-card");
    card.className = "kiosk";
    card.setAttribute("endpoint", slugText);
    card.setAttribute("fullpage", "");
    if (typeof card.setConfig === "function") {
      const cfg = { type: "custom:portico-card", endpoint: slugText, takeover: true, fullpage: true };
      if (this._kioskLook && this._kioskLook.backPath) cfg.back_path = this._kioskLook.backPath;
      card.setConfig(cfg);
    }
    card.hass = this._hass;
    return card;
  }

  /* The page sends hello on every connect: the server keeps who is open in memory only (gone at its restart). */
  _hello() {
    if (this._card) {
      Promise.resolve(this._hass.callWS({ type: "portico/kiosk/hello", endpoint: this._kiosk.slug })).catch(() => {});
    } else if (this._kiosk && this._kiosk.slug) {
      this._loadKiosk();      // a page that was waiting for the server part (a restart) tries again
    }
  }

  _bindReady() {
    const conn = this._hass && this._hass.connection;
    if (!conn || typeof conn.addEventListener !== "function" || this._readyBound === conn) return;
    this._unbindReady();
    conn.addEventListener("ready", this._onReady);
    this._readyBound = conn;
  }

  _unbindReady() {
    if (!this._readyBound) return;
    try { this._readyBound.removeEventListener("ready", this._onReady); } catch (_err) { /* gone with the connection */ }
    this._readyBound = null;
  }

  async _subscribe() {
    // hass is set before the element is connected, so both paths arrive here before the await resolves.
    if (this._subscribing || this._unsub) return;
    this._subscribing = true;
    try {
      const unsub = await this._hass.connection.subscribeMessage(
        (event) => { if (event && event.type === "content_changed") this._onContentChanged(); },
        { type: "portico/content/subscribe" },
      );
      if (this.isConnected) this._unsub = unsub;
      else unsub();
    } catch (_err) {
      /* the panel still works without live updates */
    } finally {
      this._subscribing = false;
    }
  }

  _onContentChanged() {
    if (this._saving) return;                       // our own save reloads by itself
    if (this._phase === "ready" && this._state && isDirty(this._state)) {
      this._stale = true;                           // never clobber unsaved work; Save will say conflict
      this._render();
      return;
    }
    this._load();
  }

  /* ---- editing ---- */

  _edit(fn) {
    // fn(state) mutates through the pure ops; an EditError becomes a message and changes nothing
    try {
      fn(this._state);
      this._msg = null;
    } catch (err) {
      if (!(err instanceof EditError)) throw err;
      this._msg = { kind: "err", text: err.message };
    }
    this._changed();
  }

  _changed() {
    this._render();
    this._schedulePreview();
  }

  _problems() {
    const rows = new Map(rowsOf(this._state, ["phone", "dashboard"]).map((r) => [r.id, rowName(r)]));
    const loaded = this._state.loaded || {};
    const ctx = {
      users: Array.isArray(loaded.ha_users) && loaded.ha_users.length ? new Set(loaded.ha_users.map((u) => u.id)) : null,
      otherIds: new Set(allRows(this._state).filter((r) => r.kind !== "dashboard").map((r) => r.id)),
      elements: this._elMap,
    };
    return validate(this._state.draft, (id) => rows.get(id) || id, ctx);
  }

  async _save() {
    const state = this._state;
    if (!state || this._saving || !isDirty(state)) return;
    const problems = this._problems();
    if (problems.length) { this._render(); return; }
    this._saving = true;
    this._msg = null;
    this._render();
    try {
      await this._hass.callWS(saveRequest(state));
      this._saving = false;
      const ok = await this._load();
      if (ok) { this._msg = { kind: "ok", text: "Saved. Phones and screens update at once." }; this._render(); }
    } catch (err) {
      this._saving = false;
      const c = classifyError(err);
      if (c.kind === "conflict") {
        const ok = await this._load();
        if (ok) { this._msg = { kind: "err", text: c.message }; this._render(); }
      } else if (c.kind === "needs_restart") {
        this._phase = "needs_restart";
        this._error = c.message;
        this._render();
      } else {
        this._msg = { kind: "err", text: c.message, reload: !!c.reload };
        this._render();
      }
    }
  }

  /* ---- the preview ---- */

  _schedulePreview() {
    clearTimeout(this._previewTimer);
    if (this._phase !== "ready" || !this._state || this._tab === "devices") return;
    if (this._sectionsMode()) return this._scheduleSections();
    this._previewTimer = setTimeout(() => this._runPreview(), this._previewMs);
  }

  async _runPreview() {
    this._previewTimer = null;
    const state = this._state;
    if (!state) return;
    const gen = ++this._previewGen;
    if (this._problems().length) {
      this._preview = { ok: false, message: "Fix the problems above to see the preview." };
      return this._renderPreview();
    }
    if (isPagesModel(state.draft) && this._ringPreview() && this._ringCaller() === UNKNOWN_CALLER) {
      this._preview = { ok: false, message: "This Ring page's Talk element stands for no door, phone or screen Portico knows, so there is no call to preview." };
      return this._renderPreview();
    }
    const msg = previewRequest(state, this._ringPreview() ? this._ringCaller() : null);
    if (!msg) {
      this._preview = { ok: false, message: "No phone or screen to preview yet." };
      return this._renderPreview();
    }
    try {
      const result = await this._hass.callWS(msg);
      if (gen !== this._previewGen) return;         // a newer edit is already on its way
      this._preview = { ok: true, result };
    } catch (err) {
      if (gen !== this._previewGen) return;
      this._preview = { ok: false, message: classifyError(err).message };
    }
    this._renderPreview();
  }

  /* ---- the Pages tab sections: one preview per page, each debounced on its own ---- */

  /* Schema 6 Pages tab showing? Then the previews are the sections', not the one preview column's. */
  _sectionsMode() {
    const state = this._state;
    return this._tab === "pages" && !!state && isPagesModel(state.draft);
  }

  /* The preview request of one page's section, or why there is none. A Ring page previews as the party its Talk element stands
   * for. `problems`: the draft's problems, read once for all sections. */
  _sectionRequest(page, problems) {
    const state = this._state;
    if (problems.length) return { message: "Fix the problems above to see the preview." };
    let caller = null;
    if (pageKind(page) === "ring") {
      caller = talkCaller(rowsOf(state, ["door"]), this._elMap && this._elMap.get(page.talk)) || UNKNOWN_CALLER;
      if (caller === UNKNOWN_CALLER) return { message: "This Ring page's Talk element stands for no door, phone or screen Portico knows, so there is no call to preview." };
    }
    const msg = previewRequest({ ...state, pageId: page.id, previewAs: this._secAs.get(page.id) || state.previewAs }, caller);
    if (!msg) return { message: "No phone or screen to preview yet." };
    const ep = msg.endpoint;
    const key = JSON.stringify([page, ep, msg.caller || "", (state.draft.devices || {})[ep] || null, (state.draft.dashboards || {})[ep] || null, state.draft.home_name || "", objId(this._elMap)]);
    return { msg, caller, key };
  }

  /* After an edit (or a tab switch): every section whose request would differ from its last one asks again, after its own
   * debounce. A section the edit did not touch asks for nothing. */
  _scheduleSections() {
    const state = this._state;
    const problems = this._problems();
    const live = new Set();
    for (const page of pagesOf(state.draft, LIBRARY)) {
      live.add(page.id);
      const r = this._sectionRequest(page, problems);
      const key = r.message ? `message:${r.message}` : r.key;
      if (this._secKey.get(page.id) === key) continue;
      this._secKey.set(page.id, key);
      clearTimeout(this._secTimers.get(page.id));
      if (r.message) {
        this._secGen.set(page.id, (this._secGen.get(page.id) || 0) + 1);      // a reply in flight is now stale
        this._secPreviews.set(page.id, { ok: false, message: r.message });
        this._fillSection(page.id);
        continue;
      }
      this._secTimers.set(page.id, setTimeout(() => { this._secTimers.delete(page.id); this._enqueueSection(page.id); }, this._previewMs));
    }
    for (const id of Array.from(this._secKey.keys())) if (!live.has(id)) { this._secKey.delete(id); this._secPreviews.delete(id); this._secAs.delete(id); }
  }

  _enqueueSection(id) {
    if (!this._secQueue.includes(id)) this._secQueue.push(id);
    this._pumpSections();
  }

  /* At most SECTION_PREVIEW_CONCURRENCY requests in flight over all sections; a section edited meanwhile asks again and its older reply is dropped. */
  _pumpSections() {
    while (this._secInflight < SECTION_PREVIEW_CONCURRENCY && this._secQueue.length) {
      const id = this._secQueue.shift();
      const state = this._state;
      const page = state && pagesOf(state.draft, LIBRARY).find((p) => p.id === id);
      if (!page || !this._hass) continue;
      const r = this._sectionRequest(page, this._problems());
      if (r.message) { this._secPreviews.set(id, { ok: false, message: r.message }); this._fillSection(id); continue; }
      const gen = (this._secGen.get(id) || 0) + 1;
      this._secGen.set(id, gen);
      this._secInflight++;
      this._hass.callWS(r.msg).then(
        (result) => ({ ok: true, result, caller: r.caller }),
        (err) => ({ ok: false, message: classifyError(err).message }),
      ).then((entry) => {
        this._secInflight--;
        if (this._secGen.get(id) === gen) { this._secPreviews.set(id, entry); this._fillSection(id); }
        this._pumpSections();
      });
    }
  }

  _fillSection(id) {
    const cached = this._sections.get(id);
    const page = this._state && pagesOf(this._state.draft, LIBRARY).find((p) => p.id === id);
    const host = cached && page && cached.node.querySelector("[data-section-preview]");
    if (host) this._fillPreviewHost(host, page);
  }

  _fillPreviewHost(host, page) {
    const doc = this.ownerDocument;
    host.replaceChildren();
    const entry = this._secPreviews.get(page.id);
    if (!entry) { host.append(el(doc, "div", "empty", "Preview loads after a moment...")); return; }
    if (!entry.ok) { host.append(el(doc, "div", "empty", entry.message)); return; }
    if (pageKind(page) === "ring") { host.append(this._ringFrame(entry.result, entry.caller)); return; }
    const shown = (entry.result.pages || []).find((p) => p.id === page.id);
    if (!shown) { host.append(el(doc, "div", "empty", "Nothing to preview yet.")); return; }
    const frame = el(doc, "div", "frame");
    frame.append(drawPage(doc, shown, (id) => this._pictureOf(id), this._stamp, this._dark));
    host.append(frame);
  }

  /* ---- pictures ---- */

  _pictureOf(entityId) {
    const state = this._hass && this._hass.states && this._hass.states[entityId];
    const picture = state && state.attributes && state.attributes.entity_picture;
    if (!picture) return null;
    return this._hass.hassUrl ? this._hass.hassUrl(picture) : picture;
  }

  _refreshPictures() {
    this._stamp = Date.now();
    for (const img of this._root.querySelectorAll("img[data-picture]")) {
      img.src = withStamp(img.dataset.picture, this._stamp);
    }
  }

  /* ---- drawing ---- */

  _btn(label, onClick, { cls, disabled, act, title } = {}) {
    const b = el(this.ownerDocument, "button", cls || "", label);
    b.type = "button";
    if (act) b.dataset.act = act;
    if (title) { b.title = title; b.setAttribute("aria-label", title); }
    if (disabled) b.disabled = true;
    else b.addEventListener("click", onClick);
    return b;
  }

  _render() {
    setPanelHosts(this._panelOwners());           // the pure placement checks see which phones are panels
    setLeftoverKeys(this._state && this._state.loaded ? this._state.loaded.orphans : []);   // ... and which device keys are leftovers
    const root = this._root;
    const active = root && root.activeElement;
    const held = active && { node: active, start: active.selectionStart, end: active.selectionEnd };
    this._renderNow();
    if (held && held.node.isConnected && root.activeElement !== held.node && typeof held.node.focus === "function") {
      // a section that was not rebuilt is moved back into the new tree, which drops its focus: give it back, caret and selection included
      held.node.focus();
      try { if (held.start != null) held.node.setSelectionRange(held.start, held.end); } catch (_) { /* not a text field */ }
    }
    if (this._scrollTo) { const id = this._scrollTo; this._scrollTo = null; this._scrollToSection(id); }
    this._specAfterRender();
    if (this._phase === "ready" && this._elList.status === "idle" && this._hasRefs()) this._loadElementList();     // only a document that holds a reference needs the list at once
  }

  /* ---- element references (DEV-760 P2) ---- */

  /* Read the elements (administrators only; a failure leaves the panel working with inline cameras only). */
  async _loadElementList() {
    if (!this._isAdmin || this._kiosk || !this._hass || this._elList.loading) return;
    this._elList.loading = true;
    let map = null;
    try {
      const reply = await this._hass.callWS({ type: "portico/elements/list" });
      if (reply && Array.isArray(reply.elements)) {
        map = new Map(reply.elements.filter((e) => e && typeof e === "object" && typeof e.id === "string").map((e) => [e.id, e]));
      }
    } catch (_err) { /* no list: the dialog offers "Define here" only */ }
    const before = this._elList.map;
    this._elList = { status: map ? "ready" : "failed", map: map || before };
    if (this._phase === "ready" && !same(before ? [...before.values()] : null, map ? [...map.values()] : null)) { this._render(); this._schedulePreview(); }     // a Ring page's caller comes from its Talk element
  }

  /* The elements as they are right now (a new elements/list call), also stored as the cached list. Null when the read fails:
   * a Detach or Choose another then refuses ("the element is gone") instead of copying a stale value. */
  async _freshElements() {
    try {
      const reply = await this._hass.callWS({ type: "portico/elements/list" });
      if (!reply || !Array.isArray(reply.elements)) return null;
      const map = new Map(reply.elements.filter((e) => e && typeof e === "object" && typeof e.id === "string").map((e) => [e.id, e]));
      this._elList = { status: "ready", map };
      return map;
    } catch (_err) { return null; }
  }

  _hasRefs() { return !!(this._state && this._state.draft && docHasRefs(this._state.draft)); }

  /* The elements by id, or null while unknown (not loaded, or the read failed). */
  get _elMap() { return this._elList.map; }

  _placeable(kind = "camera") { return this._elMap ? [...this._elMap.values()].filter(kind === "camera" ? placeableCamera : placeableButton) : []; }

  _dropElPreview() {
    if (this._elPreview) { try { this._elPreview.destroy(); } catch (_err) { /* gone */ } }
    this._elPreview = null;
  }

  /* Draw an element with the shared renderer (the same picture as on the Elements tab). Without the renderer module: a text line. */
  _elementPreview(element) {
    const doc = this.ownerDocument;
    const slot = el(doc, "div", "elpreview");
    slot.dataset.slot = "element-preview";
    this._dropElPreview();
    if (this._elRender) {
      try {
        const handle = (this._elRender.renderPreview || this._elRender.renderElement)(doc, element, { size: "preview", dark: this._dark, states: this._hass && this._hass.states, getHass: () => this._hass });
        slot.append(handle.el);
        this._elPreview = handle;
        if (element.kind === "camera" && element.entity_id) slot.append(this._specChip(element.entity_id));
        if (element.kind === "camera" && smallOf(element)) slot.append(" ", this._specChip(smallOf(element)));
        return slot;
      } catch (_err) { /* the plain line below */ }
    }
    slot.append(el(doc, "div", "sub", `${element.name} · ${element.kind === "camera" ? streamsText(element) : (element.entity_id || "")}`));
    return slot;
  }

  /* ---- camera spec chips (the picker and the camera rows) ---- */

  /* A chip for one camera entity, painted from what is known; `_askCameras` fills in the rest and repaints. */
  _specChip(entityId) {
    const chip = el(this.ownerDocument, "span", "chip spec");
    chip.dataset.spec = entityId || "";
    this._applySpec(chip);
    return chip;
  }

  _specInfo(entityId) {
    const states = this._hass && this._hass.states;
    if (!states || !states[entityId]) return { source: "unavailable" };
    const have = this._camInfo.get(entityId);
    if (!have) return undefined;
    return have.failed ? { failed: true } : have;
  }

  _applySpec(chip) {
    const id = chip.dataset.spec;
    if (!id) { chip.hidden = true; return; }
    chip.hidden = false;
    const spec = specChip(this._specInfo(id));
    chip.textContent = spec.text;
    chip.title = spec.title;
    chip.dataset.specState = spec.state;
  }

  /* An option of a camera select says its spec once known: "Gate camera (camera.gate) \u2014 1600x1200 \u00b7 20 fps". */
  _applyOptionSpec(option) {
    const spec = specChip(this._specInfo(option.dataset.cam));
    option.textContent = spec.state === "ok" ? `${option.dataset.base} \u2014 ${spec.text}` : option.dataset.base;
    option.title = spec.state === "ok" ? spec.title : "";
  }

  _paintSpecs() {
    for (const chip of this._root.querySelectorAll("[data-spec]")) this._applySpec(chip);
    for (const option of this._root.querySelectorAll("option[data-cam]")) this._applyOptionSpec(option);
  }

  /* Ask for the cameras on screen: every chip, and the first SPEC_BATCH options of an open picker. Nothing is asked
   * for a camera already known (or asked a minute ago and failed), so a re-render costs no call. */
  _specAfterRender() {
    if (!this._root || this._phase !== "ready") return;
    const ids = [];
    for (const chip of this._root.querySelectorAll("[data-spec]")) if (chip.dataset.spec) ids.push(chip.dataset.spec);
    ids.push(...Array.from(this._root.querySelectorAll("option[data-cam]")).slice(0, SPEC_BATCH).map((o) => o.dataset.cam));
    this._askCameras(ids);
  }

  async _askCameras(entityIds) {
    const hass = this._hass;
    if (!hass || !hass.user || !hass.user.is_admin) return;
    const now = Date.now();
    const want = [];
    for (const id of new Set(entityIds)) {
      if (!ENTITY_RE.test(id || "") || id.split(".")[0] !== "camera") continue;
      if (!hass.states || !hass.states[id] || this._camPending.has(id)) continue;
      const have = this._camInfo.get(id);
      if (have && now - have.at < (have.failed ? SPEC_RETRY_MS : SPEC_TTL_MS)) continue;
      want.push(id);
    }
    for (let at = 0; at < want.length; at += SPEC_BATCH) {
      const batch = want.slice(at, at + SPEC_BATCH);
      for (const id of batch) this._camPending.add(id);
      let rows = null;
      try {
        const reply = await hass.callWS({ type: "portico/cameras/info", entity_ids: batch });
        rows = reply && Array.isArray(reply.cameras) ? reply.cameras : null;
      } catch (_err) {
        rows = null;                // an older server, or a dropped connection: the chips stay "\u2014"; the picker works
      }
      const byId = new Map((rows || []).filter((r) => r && typeof r.entity_id === "string").map((r) => [r.entity_id, r]));
      const stamp = Date.now();
      for (const id of batch) {
        this._camPending.delete(id);
        const row = byId.get(id);
        this._camInfo.set(id, row ? { ...row, at: stamp } : { failed: true, at: stamp });
      }
      this._paintSpecs();
    }
  }

  _renderNow() {
    const doc = this.ownerDocument;
    const root = this._root;
    root.replaceChildren();
    const style = doc.createElement("style");
    style.textContent = STYLE;
    if (this._phase === "kiosk") this.setAttribute("kiosk", ""); else this.removeAttribute("kiosk");
    if (this._phase === "kiosk") {
      // The kiosk route: the card and nothing else. No header, no tabs, no `.page` wrapper.
      root.append(style);
      if (this._card) root.append(this._card);
      else {
        const msg = el(doc, "div", "kiosk-msg", this._kioskMsg || "Loading...");
        msg.dataset.kiosk = this._kioskMsg ? "message" : "loading";
        root.append(msg);
      }
      return;
    }
    const page = el(doc, "div", "page");
    root.append(style, page);

    if (this._phase === "needs_restart" || this._phase === "error") {
      // The tab bar is here too (UI split P2): with the content store broken, Phones stays reachable. Without the
      // Phones module the page is what it always was.
      if (this._phones || this._settings || this._elements) page.append(this._failedHeader());
      else page.append(el(doc, "h1", null, "Portico"));
      if (this._hosted()) {
        this._hosted().attach(page);
        return;
      }
      if (this._phase === "error") {
        page.append(el(doc, "p", "msg err", this._error));
        return;
      }
      const box = el(doc, "div", "restart");
      box.dataset.state = "needs-restart";
      box.append(el(doc, "p", null, this._error || RESTART_TEXT));
      box.append(this._btn("Check again", () => this._load(), { act: "retry" }));
      page.append(box);
      return;
    }
    if (this._phase === "loading") {
      page.append(el(doc, "h1", null, "Portico"), el(doc, "p", "empty", "Loading..."));
      return;
    }
    if (this._phase === "readonly") {
      this._renderReadonly(page);
      return;
    }

    const state = this._state;
    const dirty = isDirty(state);
    const header = el(doc, "header");
    header.append(el(doc, "h1", null, "Portico"));
    const hosted = this._hosted();
    const onPhones = !!hosted;
    const v6 = isPagesModel(state.draft);        // the unified Pages model: Elements, Pages, Devices, Settings (Ring & call is a kind of page)
    if (this._phones) this._phones.setPerDevice(v6);
    if (v6 && this._tab === "calls") this._tab = "pages";
    const tabs = [];
    if (this._elements) tabs.push(["elements", "Elements"]);
    tabs.push(["pages", "Pages"], ["devices", "Devices"]);
    if (!v6) tabs.push(["calls", "Ring & call"]);
    if (this._settings) tabs.push(["settings", "Settings"]);
    if (this._status) tabs.push(["status", "Status"]);
    header.append(this._tabBar(tabs));
    const problems = onPhones ? [] : this._problems();
    if (onPhones) {
      // The Phones, Settings and Elements tabs write immediately (Elements is read-only for now) and have no draft: the content header (Undo, Discard, Save) is not shown.
      header.dataset.tab = this._tab;
    } else {
      header.append(el(doc, "span", "grow"));
      const status = el(doc, "span", "status contentacts" + (dirty ? " dirty" : ""), dirty ? "Unsaved changes" : "Saved");
      status.dataset.status = dirty ? "dirty" : "saved";
      header.append(status);
      header.append(this._btn("Undo", () => { undo(state); this._msg = null; this._changed(); }, { cls: "contentacts", disabled: !state.undo.length, act: "undo" }));
      header.append(this._btn("Discard", () => { discard(state); this._msg = null; this._dialog = null; this._changed(); }, { cls: "contentacts", disabled: !dirty, act: "discard" }));
      header.append(this._btn(this._saving ? "Saving..." : "Save", () => this._save(),
        { cls: "primary contentacts", disabled: !dirty || this._saving || problems.length > 0, act: "save" }));
    }
    page.append(header);
    const cfgBanner = this._configBanner();
    if (cfgBanner) page.append(cfgBanner);               // on every tab: the files are about the whole configuration
    if (this._tab === "devices" && this._phones) page.dataset.devview = this._phones.view;

    if (onPhones) {
      hosted.attach(page);
      return;
    }

    if (this._stale) {
      const note = el(doc, "div", "msg err", "Another session changed the saved content. Save will tell you if it clashes; Discard loads the new version.");
      note.dataset.state = "stale";
      page.append(note);
    }
    if (problems.length) {
      const box = el(doc, "div", "msg err", "Fix before saving:");
      box.dataset.state = "problems";
      const list = el(doc, "ul");
      for (const p of problems) list.append(el(doc, "li", null, p));
      box.append(list);
      // a block over the per-block limit (a v3 page can hold 24 of each): one click moves the overflow to a new block
      for (const o of overflowBlocks(state.draft)) {
        const line = el(doc, "div", "split");
        line.dataset.split = o.blockId;
        line.append(el(doc, "span", null, `Block "${o.title}" on page "${o.pageLabel}" holds ${plural(o.cameras, "camera", "cameras")} and ${plural(o.actions, "button", "buttons")}; a block takes at most ${MAX_BLOCK_CAMERAS} of each.`));
        line.append(this._btn("Split this block", () => this._edit((s) => commit(s, splitBlock(s.draft, o.owner, o.pageId, o.blockId).doc)), { act: "split-block" }));
        box.append(line);
      }
      page.append(box);
    }
    if (this._msg) {
      const box = el(doc, "div", `msg ${this._msg.kind}`, this._msg.text);
      box.dataset.state = "message";
      if (this._msg.reload) box.append(" ", this._btn("Reload the editor", () => { this._msg = null; this._load(); }, { act: "reload" }));
      page.append(box);
    }

    if (this._tab === "devices") {
      page.append(this._devicesTab());
    } else if (this._tab === "calls") {
      const layout = el(doc, "div", "layout");
      layout.append(this._sidebar({ orphans: false }), this._callsWork(), this._previewColumn());
      page.append(layout);
      this._renderPreview();
    } else if (v6) {
      page.append(this._libraryTab());
    } else {
      const layout = el(doc, "div", "layout");
      layout.append(this._sidebar(), this._work(), this._previewColumn());
      page.append(layout);
      this._renderPreview();
    }
  }

  /* The tab buttons, in a strip that scrolls sideways when the window is narrow. */
  _tabBar(tabs) {
    const doc = this.ownerDocument;
    const nav = el(doc, "nav", "tabs");
    for (const [id, text] of tabs) {
      nav.append(this._btn(text, () => this._pickTab(id), { cls: "tab" + (this._tab === id ? " on" : ""), act: `tab-${id}` }));
    }
    return nav;
  }

  _pickTab(id) {
    this._tabChosen = true;
    if (this._state && isPagesModel(this._state.draft)) id = id === "calls" ? "pages" : id === "screens" || id === "phones" ? "devices" : id;     // old tab ids land on the unified tabs
    this._tab = id === "content" ? "pages" : id;
    if (id === "devices" || id === "calls" || ((id === "pages" || id === "content") && this._hasRefs())) this._loadElementList();     // the Elements tab may have changed the list
    const own = id === "devices" || (id === "settings" && this._perDevice());     // schema 6: the Settings tab holds House calls and the push ring notification and app
    if (own && this._appSettings) this._appSettings.show();
    if (own && this._phones) this._phones.show();     // the list (and the House calls switch) is read when the tab is opened
    if (id === "settings" && this._settings) this._settings.show();
    if (id === "status" && this._status) this._status.show();
    if (this._elements) {
      if (id === "elements") this._elements.show();         // the list is read and the live feed starts
      else this._elements.hide();
    }
    this._render();
    if (id !== "devices" && id !== "settings" && id !== "status" && id !== "elements") this._schedulePreview();
  }

  /* The header of the error and needs-restart phases once a hosted module (Phones, Settings) is there: a Content tab (the
   * failure) and the tabs that loaded. */
  _failedHeader() {
    const doc = this.ownerDocument;
    const header = el(doc, "header");
    const on = this._hosted() ? this._tab : "content";
    header.dataset.tab = on;
    header.append(el(doc, "h1", null, "Portico"));
    const nav = el(doc, "nav", "tabs");
    const failedTabs = [["content", "Content"]];
    if (this._phones) failedTabs.push(["devices", "Devices"]);
    if (this._settings) failedTabs.push(["settings", "Settings"]);
    if (this._status) failedTabs.push(["status", "Status"]);
    if (this._elements) failedTabs.push(["elements", "Elements"]);
    for (const [id, text] of failedTabs) {
      nav.append(this._btn(text, () => this._pickTab(id), { cls: "tab" + (on === id ? " on" : ""), act: `tab-${id}` }));
    }
    header.append(nav);
    return header;
  }

  /* ---- the sidebar ---- */

  _sidebar({ orphans: withOrphans = true } = {}) {
    const doc = this.ownerDocument;
    const state = this._state;
    const side = el(doc, "nav", "side");
    const entry = (sel, text, sub) => {
      const b = this._btn(text, () => { state.sel = sel; state.pageId = null; state.ruleId = null; this._dialog = null; this._msg = null; this._changed(); },
        { cls: state.sel === sel ? "sel" : "" });
      b.dataset.sel = sel;
      if (sub) b.title = sub;
      return b;
    };
    side.append(el(doc, "h3", null, "Household"));
    side.append(entry("template", "Template"));
    const groups = [["Phones", "phone"], ["Screens", "dashboard"]];
    for (const [title, kind] of groups) {
      const rows = rowsOf(state, [kind]);
      if (!rows.length) continue;
      side.append(el(doc, "h3", null, title));
      for (const r of rows) side.append(entry(`ep:${r.id}`, rowName(r)));
    }
    const orphans = withOrphans ? orphanKeys(state) : [];
    if (orphans.length) {
      side.append(el(doc, "h3", null, "Not attached"));
      for (const key of orphans) side.append(entry(`orphan:${key}`, `Old: ${shortKey(key)}`, key));
    }
    return side;
  }

  /* ---- the work area ---- */

  _work() {
    const doc = this.ownerDocument;
    const state = this._state;
    const work = el(doc, "div", "work");
    if (state.sel.startsWith("orphan:")) return this._orphanWork(work, state.sel.slice(7));
    const target = selTarget(state.sel);
    const rows = rowsOf(state, ["phone", "dashboard"]);
    const name = target === "template" ? "Household template" : rowName(rows.find((r) => r.id === target) || { id: target });
    const banner = this._migrationBanner();
    if (banner) work.append(banner);
    if (this._mig) work.append(this._migrationDialog());
    work.append(el(doc, "h3", null, `Pages of: ${name}`));
    work.append(el(doc, "p", "hint", target === "template"
      ? "Household pages show on every phone; override one phone only to make it differ."
      : "These pages are added to the household pages. A page here with the same id as a household page replaces it."));

    work.append(this._oldClientHint());
    const eff = effectivePages(state.draft, target);
    if (!eff.some((e) => e.page.id === state.pageId)) state.pageId = eff.length ? eff[0].page.id : null;
    const cols = el(doc, "div", "cols");
    cols.append(this._pageList(target, eff));
    cols.append(this._pageDetail(target, eff));
    work.append(cols);
    return work;
  }

  _oldClientHint() {
    const hint = el(this.ownerDocument, "p", "hint", PAGES_HINT);
    hint.dataset.hint = "old-clients";
    return hint;
  }

  _pageList(target, eff) {
    const doc = this.ownerDocument;
    const state = this._state;
    const box = el(doc, "div", "pagelist");
    box.append(el(doc, "h3", null, "Pages"));
    if (!eff.length) box.append(el(doc, "div", "empty", "No pages yet."));
    const ownIds = eff.filter((e) => e.src === "own").map((e) => e.page.id);
    for (const { page, src } of eff) {
      const row = el(doc, "div", "row" + (page.id === state.pageId ? " cur" : ""));
      row.dataset.page = page.id;
      const pick = this._btn(page.label, () => { state.pageId = page.id; this._previewPage = null; this._dialog = null; this._changed(); }, { cls: "small", act: "pick-page" });
      const txt = el(doc, "div", "txt");
      txt.append(pick);
      if (target !== "template") txt.append(el(doc, "div", "sub", src === "household" ? "Household" : src === "override" ? "Overrides the household page" : "Only here"));
      row.append(txt);
      if (src === "own") {
        const at = ownIds.indexOf(page.id);
        row.append(this._btn("▲", () => this._edit((s) => commit(s, movePage(s.draft, target, page.id, -1))), { cls: "small", disabled: at <= 0, act: "page-up", title: "Move up" }));
        row.append(this._btn("▼", () => this._edit((s) => commit(s, movePage(s.draft, target, page.id, 1))), { cls: "small", disabled: at >= ownIds.length - 1, act: "page-down", title: "Move down" }));
      }
      if (src === "household") {
        row.append(this._btn("Override", () => this._edit((s) => { commit(s, overridePage(s.draft, target, page.id)); s.pageId = page.id; }), { cls: "small", act: "override-page", title: "Copy this page here so it can differ" }));
      } else {
        row.append(this._btn("×", () => this._edit((s) => commit(s, deletePage(s.draft, target, page.id))),
          { cls: "small", act: "delete-page", title: src === "override" ? "Remove this override (the household page returns)" : "Delete the page" }));
      }
      box.append(row);
    }
    const add = el(doc, "div", "form");
    const input = el(doc, "input");
    const fallbackLabel = defaultNewPageLabel(state.draft, target);
    input.placeholder = fallbackLabel ? `New page name (empty: ${fallbackLabel})` : "New page name";
    input.maxLength = MAX_LABEL;
    input.dataset.field = "new-page";
    add.append(input);
    const go = () => {
      const label = input.value.trim() ? input.value : fallbackLabel;
      this._edit((s) => { const r = addPage(s.draft, target, label); commit(s, r.doc); s.pageId = r.pageId; });
    };
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
    add.append(this._btn("+ Add page", go, { act: "add-page" }));
    box.append(add);
    return box;
  }

  _pageDetail(target, eff) {
    const doc = this.ownerDocument;
    const state = this._state;
    const box = el(doc, "div", "pagedetail");
    const current = eff.find((e) => e.page.id === state.pageId);
    if (!current) {
      box.append(el(doc, "div", "empty", "Add a page to start."));
      return box;
    }
    const { page, src } = current;
    const editable = src !== "household";
    if (editable) {
      const name = el(doc, "input");
      name.value = page.label;
      name.maxLength = MAX_LABEL;
      name.dataset.field = "page-label";
      name.setAttribute("aria-label", "Page name");
      name.addEventListener("change", () => this._edit((s) => commit(s, renamePage(s.draft, target, page.id, name.value))));
      box.append(el(doc, "h3", null, "Page name"), name);
    } else {
      box.append(el(doc, "h3", null, page.label), el(doc, "p", "hint", "A household page. Use Override to change it for this phone only, or edit it in the template."));
    }

    box.append(el(doc, "h3", null, "Blocks"));
    box.append(el(doc, "p", "hint",
      "A block groups cameras and buttons under a title. In a block the first camera is shown full width and the rest two to a row; " +
      "set a camera to Big or Small to change that. A door lets the block's open-door button leave its door empty."));
    const blocks = blocksOf(page);
    if (tooManyLive(page)) {
      const warn = el(doc, "div", "msg warn", LIVE_WARN_TEXT);
      warn.dataset.state = "too-many-live";
      warn.setAttribute("role", "status");
      box.append(warn);
    }
    if (!blocks.length) box.append(el(doc, "div", "empty", "No blocks on this page."));
    blocks.forEach((block, at) => box.append(this._blockCard(target, page, block, at, blocks.length, editable)));
    if (editable) box.append(this._addBlockForm(target, page));
    return box;
  }

  _addBlockForm(target, page) {
    const doc = this.ownerDocument;
    const form = el(doc, "div", "form");
    form.dataset.form = "new-block";
    const input = el(doc, "input");
    input.maxLength = MAX_TITLE;
    input.dataset.field = "new-block";
    input.setAttribute("aria-label", "New block title");
    const fallback = `Block ${blocksOf(page).length + 1}`;
    input.placeholder = `New block title (empty: ${fallback})`;
    const go = () => this._edit((s) => { commit(s, addBlock(s.draft, target, page.id, input.value.trim() ? input.value : fallback).doc); });
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
    form.append(input, this._btn("+ Add block", go, { act: "add-block" }));
    return form;
  }

  _blockCard(target, page, block, at, count, editable) {
    const doc = this.ownerDocument;
    const state = this._state;
    const card = el(doc, "div", "block");
    const ring = pageKind(page) === "ring";
    card.dataset.block = block.id;
    const head = el(doc, "div", "blockhead");
    const doors = doorOptions(state);
    if (editable) {
      const title = el(doc, "input");
      title.value = block.title;
      title.maxLength = MAX_TITLE;
      title.dataset.field = "block-title";
      title.setAttribute("aria-label", "Block title");
      title.addEventListener("change", () => this._edit((s) => commit(s, renameBlock(s.draft, target, page.id, block.id, title.value))));
      head.append(title);
      if (ring && block.when) {        // the UI no longer offers 'when'; a stored non-default value is kept and shown read-only
        const note = el(doc, "span", "sub", `Stored audience: ${WHEN_TEXT[block.when] || block.when}`);
        note.dataset.note = "block-when";
        head.append(note);
      }
      const door = el(doc, "select");
      door.dataset.field = "block-door";
      door.setAttribute("aria-label", `Door of the block "${block.title}"`);
      const none = el(doc, "option", null, "No door");
      none.value = "";
      door.append(none);
      const known = new Set(doors.map((d) => d.value));
      const options = block.door && !known.has(block.door) ? [...doors, { value: block.door, label: `${block.door} (not found)` }] : doors;
      for (const d of options) {
        const o = el(doc, "option", null, d.label);
        o.value = d.value;
        if (d.value === block.door) o.selected = true;
        door.append(o);
      }
      door.addEventListener("change", () => this._edit((s) => commit(s, setBlockDoor(s.draft, target, page.id, block.id, door.value))));
      if (!ring) head.append(door);       // a Ring page's blocks hold buttons for the call; the door is the Talk element's
      const upd = (fn) => this._edit((s) => commit(s, fn(s.draft)));
      head.append(this._btn("▲", () => upd((d) => moveBlock(d, target, page.id, block.id, -1)), { cls: "small", disabled: at === 0, act: "block-up", title: `Move the block "${block.title}" up` }));
      head.append(this._btn("▼", () => upd((d) => moveBlock(d, target, page.id, block.id, 1)), { cls: "small", disabled: at === count - 1, act: "block-down", title: `Move the block "${block.title}" down` }));
      head.append(this._btn("×", () => upd((d) => deleteBlock(d, target, page.id, block.id)), { cls: "small", act: "delete-block", title: `Delete the block "${block.title}" with its cameras and buttons` }));
    } else {
      head.append(el(doc, "strong", null, block.title));
      if (block.door) head.append(el(doc, "span", "chip", `Door: ${(doors.find((d) => d.value === block.door) || { label: block.door }).label}`));
    }
    card.append(head);

    const over = overflowBlocks({ template: { pages: [{ id: page.id, label: page.label, blocks: [block] }] } });
    if (over.length) {
      const line = el(doc, "div", "msg err split");
      line.dataset.state = "block-over-limit";
      line.append(el(doc, "span", null, `This block holds ${plural(block.cameras.length, "camera", "cameras")} and ${plural(block.actions.length, "button", "buttons")}; at most ${MAX_BLOCK_CAMERAS} of each fit.`));
      if (editable) line.append(this._btn("Split this block", () => this._edit((s) => commit(s, splitBlock(s.draft, target, page.id, block.id).doc)), { act: "split-block-here" }));
      card.append(line);
    }

    for (const listName of ring ? ["actions"] : ["cameras", "actions"]) {
      card.append(el(doc, "h4", null, listName === "cameras" ? "Cameras" : "Buttons"));
      const items = block[listName] || [];
      if (listName === "cameras" && items.length && editable) {
        const hint = el(doc, "div", "sub", LIVE_HINT_TEXT);
        hint.dataset.hint = "live";
        card.append(hint);
      }
      if (!items.length) card.append(el(doc, "div", "empty", listName === "cameras" ? "No cameras." : "No buttons."));
      items.forEach((tile, i) => card.append(this._tileRow(target, page, block, listName, tile, i, items.length, editable)));
      if (this._dialog && this._dialog.list === listName && this._dialog.blockId === block.id && (!this._dialog.pageId || this._dialog.pageId === page.id)) card.append(this._dialogForm(target, page, block));
      else if (editable) {
        card.append(this._btn(listName === "cameras" ? "+ Add camera" : "+ Add button",
          () => { this._dialog = { ...newDialog(listName, this._state, block.id), pageId: page.id }; this._loadElementList(); this._render(); },
          { act: listName === "cameras" ? "add-camera" : "add-button" }));
      }
    }
    return card;
  }

  /* The reason a Talk button is off: the server's `reason` from the latest preview (talk_state), else "off until the
   * gate allows it". */
  _talkReason(pageId, tileId) {
    const result = this._preview && this._preview.ok && this._preview.result;
    const page = result && (result.pages || []).find((p) => p.id === pageId);
    const action = page && pageActions(page).find((a) => a.id === tileId);
    return talkReasonText(action && action.reason);
  }

  _tileRow(target, page, block, listName, tile, at, count, editable) {
    const doc = this.ownerDocument;
    const row = el(doc, "div", "row");
    row.dataset.tile = tile.id;
    const ref = isRef(tile);
    const element = ref && this._elMap ? this._elMap.get(tile.element) || null : null;
    if (listName === "actions") {
      const ic = el(doc, "div", "ic");
      ic.append(haIcon(doc, iconOf(tile, element)));
      row.append(ic);
    }
    const txt = el(doc, "div", "txt");
    const states = this._hass && this._hass.states;
    const usable = listName === "cameras" ? placeableCamera : placeableButton;
    const removed = ref && !!this._elMap && !usable(element);          // the list is known and the element is not in it (or cannot be placed now)
    const entity = ref ? (element && element.entity_id) || "" : tile.entity_id;
    const shown = ref && listName === "actions" && element && placeableButton(element) ? inlineButton(tile, element) : tile;     // what the phone will draw
    if (ref) {
      row.dataset.element = tile.element;
      if (removed) { row.classList.add("dangling"); row.dataset.state = "element-removed"; }
    }
    const unnamed = listName === "cameras" && (ref ? "label" in tile && !tile.label : !tile.label);
    const title = ref ? (listName === "cameras" ? cameraName(tile, states, this._elMap)
      : tile.label || (element ? element.name : this._elMap ? "Element removed" : tile.element)) : tile.label;
    const nameLine = el(doc, "div", unnamed || removed ? "label muted" : "label",
      unnamed ? cameraName(ref ? { ...tile, label: "" } : tile, states, ref ? new Map([[tile.element, element || { name: "" }]]) : undefined) : title);
    if (unnamed) nameLine.title = "No name is shown on the phone";
    txt.append(nameLine);
    if (ref) {
      const badge = el(doc, "span", "chip elbadge", removed ? "element removed" : "element");
      badge.dataset.badge = "element";
      badge.title = removed ? "The element this tile used was removed. It shows nothing on the phone and the kiosk." : `Element: ${element ? element.name : tile.element}. Defined on the Elements page.`;
      txt.append(badge);
    }
    const sub = listName === "cameras"
      ? (removed ? "Element removed: choose another element or remove this tile" : entity || tile.element)
      : removed ? "Element removed or no longer usable: choose another element or remove this tile"
      : [KIND_TEXT[shown.kind] || shown.kind || "Element", shown.entity_id || shown.device_id || (shown.target && shown.target.id) || (shown.kind === "portico_open" ? "the block's door" : ""),
        shown.color ? `colour ${shown.color}` : "", shown.show_label === false ? "text hidden" : "", shown.confirm ? "requires confirmation" : ""].filter(Boolean).join(" · ");
    txt.append(el(doc, "div", "sub", sub));
    if (listName === "cameras" && !removed) txt.append(this._specChip(entity));
    if (shown.kind === "talk") {
      const reason = el(doc, "div", "sub", `Talk is off: ${this._talkReason(page.id, tile.id)}`);
      reason.dataset.talkReason = "";
      txt.append(reason);
    }
    row.append(txt);
    const upd = (fn) => this._edit((s) => commit(s, fn(s.draft)));
    if (editable && removed) {
      const other = this._placeable(listName === "cameras" ? "camera" : "button").filter((e) => listName === "cameras" || !buttonBlockProblem(e, block));
      if (other.length) {
        const pick = el(doc, "select");
        pick.dataset.field = "choose-element";
        pick.setAttribute("aria-label", `Choose another element for "${title}"`);
        const first = el(doc, "option", null, "Choose another...");
        first.value = "";
        pick.append(first);
        for (const e of other) { const o = el(doc, "option", null, `${e.name} (${elementWhat(e)})`); o.value = e.id; pick.append(o); }
        pick.addEventListener("change", async () => {
          if (!pick.value) return;
          const fresh = await this._freshElements();          // re-read, not the cached list
          upd((d) => (listName === "cameras" ? repointCamera : repointButton)(d, target, page.id, tile.id, fresh && fresh.get(pick.value)));
        });
        row.append(pick);
      }
      row.append(this._btn("×", () => upd((d) => removeTile(d, target, page.id, listName, tile.id)), { cls: "small", act: "remove-tile", title: "Remove" }));
    } else if (editable) {
      if (listName === "cameras") {
        const size = el(doc, "select");
        size.dataset.field = "camera-size";
        size.setAttribute("aria-label", `Size of ${listName === "cameras" ? cameraName(tile, states, this._elMap) : tile.label}`);
        for (const value of CAMERA_SIZES) {
          const o = el(doc, "option", null, SIZE_TEXT[value].split(" (")[0]);
          o.value = value;
          o.title = SIZE_TEXT[value];
          if ((tile.size || "auto") === value) o.selected = true;
          size.append(o);
        }
        size.addEventListener("change", () => upd((d) => setCameraSize(d, target, page.id, tile.id, size.value)));
        row.append(size);
        const liveBox = el(doc, "label", "livebox", "Live");
        liveBox.title = LIVE_HINT_TEXT;
        const live = el(doc, "input");
        live.type = "checkbox";
        live.checked = tile.live === true;
        live.dataset.field = "camera-live";
        live.setAttribute("aria-label", `Live video for ${cameraName(tile, states, this._elMap)}`);
        live.addEventListener("change", () => upd((d) => setCameraLive(d, target, page.id, tile.id, live.checked)));
        liveBox.prepend(live);
        row.append(liveBox);
      }
      if (page.blocks.length > 1) {
        const to = el(doc, "select");
        to.dataset.field = "move-to-block";
        to.setAttribute("aria-label", `Move "${listName === "cameras" ? cameraName(tile, states, this._elMap) : tile.label}" to another block`);
        const first = el(doc, "option", null, "Move to...");
        first.value = "";
        to.append(first);
        for (const b of page.blocks) {
          if (b.id === block.id) continue;
          const o = el(doc, "option", null, b.title);
          o.value = b.id;
          to.append(o);
        }
        to.addEventListener("change", () => { if (to.value) upd((d) => moveTileToBlock(d, target, page.id, listName, tile.id, to.value)); });
        row.append(to);
      }
      row.append(this._btn("Edit", () => { this._dialog = { ...editDialog(listName, tile, block.id, this._elMap), pageId: page.id }; this._render(); }, { cls: "small", act: "edit-tile" }));
      if (ref && element) {
        row.append(this._btn("Detach", async () => {
          const fresh = await this._freshElements();          // inline what the element is NOW, not what the cached list said
          upd((d) => (listName === "cameras" ? detachCamera : detachButton)(d, target, page.id, tile.id, fresh ? fresh.get(tile.element) : null));
        },
          { cls: "small", act: "detach-tile", title: `Make this one independent: copy the element's ${listName === "cameras" ? "camera" : "button"} into this tile` }));
        if (this._elements) row.append(this._btn("Open element", () => this._pickTab("elements"), { cls: "small", act: "open-element", title: "Open the Elements page" }));
      }
      row.append(this._btn("▲", () => upd((d) => moveTile(d, target, page.id, listName, tile.id, -1)), { cls: "small", disabled: at === 0, act: "tile-up", title: "Move up" }));
      row.append(this._btn("▼", () => upd((d) => moveTile(d, target, page.id, listName, tile.id, 1)), { cls: "small", disabled: at === count - 1, act: "tile-down", title: "Move down" }));
      row.append(this._btn("×", () => upd((d) => removeTile(d, target, page.id, listName, tile.id)), { cls: "small", act: "remove-tile", title: "Remove" }));
    } else if (listName === "cameras") {
      row.append(el(doc, "span", "chip", SIZE_TEXT[tile.size || "auto"].split(" (")[0]));
      if (tile.live === true) row.append(el(doc, "span", "chip", "Live"));
    }
    return row;
  }

  /* A radio group the keyboard can drive: one tab stop, the arrow keys (and Home/End) move and choose. */
  _radioKeys(group, pick) {
    group.addEventListener("keydown", (event) => {
      const steps = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 };
      const radios = Array.from(group.querySelectorAll('[role="radio"]')).filter((r) => !r.hidden);
      if (!radios.length) return;
      const at = radios.indexOf(event.target);
      let next;
      if (event.key in steps) next = at < 0 ? 0 : (at + steps[event.key] + radios.length) % radios.length;
      else if (event.key === "Home") next = 0;
      else if (event.key === "End") next = radios.length - 1;
      else return;
      event.preventDefault();
      radios[next].focus();
      pick(radios[next].dataset.value);
    });
  }

  /* The colour picker: "Default" and the eight palette names as swatches, each drawn with the palette's fill and text
   * for the theme and carrying its name as text (and a tick when chosen), so colour is never the only cue. */
  _colorPicker(dlg) {
    const doc = this.ownerDocument;
    const group = el(doc, "div", "swatches");
    group.setAttribute("role", "radiogroup");
    group.setAttribute("aria-label", "Button colour");
    group.dataset.field = "color";
    const radios = [];
    for (const value of ["default", ...PALETTE_NAMES]) {
      const b = el(doc, "button", value === "default" ? "swatch none" : "swatch");
      b.type = "button";
      b.setAttribute("role", "radio");
      b.dataset.color = value;
      b.dataset.value = value;
      const pal = paletteColors(value, this._dark);
      if (pal) { b.style.background = pal.fill; b.style.color = pal.text; }
      b.addEventListener("click", () => pick(value));
      radios.push(b);
      group.append(b);
    }
    const refresh = () => {
      const current = dlg.color || "default";
      for (const b of radios) {
        const on = b.dataset.value === current;
        b.setAttribute("aria-checked", on ? "true" : "false");
        b.tabIndex = on ? 0 : -1;
        b.textContent = `${on ? "✓ " : ""}${b.dataset.value === "default" ? "Default" : b.dataset.value}`;
      }
    };
    const pick = (value) => { dlg.color = value; refresh(); };
    this._radioKeys(group, pick);
    refresh();
    return group;
  }

  /* The icon picker: a search box and the icons the app draws, grouped (door, gate, garage, lock, talk, other). The
   * name is the tooltip and is also written under the grid while an icon has focus or the pointer. */
  _iconPicker(dlg) {
    const doc = this.ownerDocument;
    const wrap = el(doc, "div", "iconpicker");
    const search = el(doc, "input");
    search.type = "search";
    search.placeholder = "Search icons...";
    search.dataset.field = "icon-search";
    search.setAttribute("aria-label", "Search icons");
    const caption = el(doc, "div", "iconname");
    caption.setAttribute("aria-live", "polite");
    caption.dataset.field = "icon-name";
    const grid = el(doc, "div", "icongroups");
    grid.setAttribute("role", "radiogroup");
    grid.setAttribute("aria-label", "Icon");
    grid.dataset.field = "icon";
    const names = [...ICON_NAMES];
    if (dlg.icon && dlg.icon !== "none" && !names.includes(dlg.icon)) names.push(dlg.icon);      // a stored icon the list lacks is kept
    const radios = [];
    const mk = (name, text) => {
      const b = el(doc, "button", "iconbtn" + (name === "none" ? " wide" : ""));
      b.type = "button";
      b.setAttribute("role", "radio");
      b.dataset.icon = name;
      b.dataset.value = name;
      b.title = name === "none" ? "The kind's own icon" : name;
      b.setAttribute("aria-label", b.title);
      if (text) b.textContent = text; else b.append(haIcon(doc, `mdi:${name.replace(/^mdi:/, "")}`));
      b.addEventListener("click", () => pick(name));
      b.addEventListener("focus", () => { caption.textContent = b.title; });
      b.addEventListener("mouseover", () => { caption.textContent = b.title; });
      radios.push(b);
      return b;
    };
    const top = el(doc, "div", "icongroup");
    top.dataset.group = "default";
    top.append(mk("none", "Default"));
    grid.append(top);
    const sections = [top];
    for (const [id, label] of ICON_GROUPS) {
      const sec = el(doc, "div", "icongroup");
      sec.dataset.group = id;
      sec.append(el(doc, "h5", null, label));
      const inner = el(doc, "div", "icongrid");
      for (const name of names.filter((n) => iconGroup(n) === id)) inner.append(mk(name));
      if (!inner.children.length) continue;
      sec.append(inner);
      grid.append(sec);
      sections.push(sec);
    }
    const none = el(doc, "div", "empty", "No icon matches.");
    none.hidden = true;
    grid.append(none);
    const current = () => dlg.icon || "none";
    const refresh = () => {
      const needle = search.value.trim().toLowerCase();
      let shown = 0;
      let firstShown = null;
      for (const b of radios) {
        b.hidden = !!needle && !(b.dataset.icon !== "none" && b.dataset.icon.includes(needle));
        if (!b.hidden) { shown++; firstShown = firstShown || b; }
      }
      for (const sec of sections) sec.hidden = Array.from(sec.querySelectorAll('[role="radio"]')).every((r) => r.hidden);
      none.hidden = shown > 0;
      const selected = radios.find((b) => b.dataset.icon === current() && !b.hidden);
      for (const b of radios) {
        b.setAttribute("aria-checked", b.dataset.icon === current() ? "true" : "false");
        b.tabIndex = b === (selected || firstShown) ? 0 : -1;
      }
      caption.textContent = `Icon: ${current() === "none" ? "the kind's own" : current()}`;
    };
    const pick = (name) => { dlg.icon = name; refresh(); };
    this._radioKeys(grid, pick);
    search.addEventListener("input", refresh);
    grid.addEventListener("focusout", () => refresh());
    grid.addEventListener("mouseleave", () => { caption.textContent = `Icon: ${current() === "none" ? "the kind's own" : current()}`; });
    refresh();
    wrap.append(search, grid, caption);
    return wrap;
  }

  /* The add / edit form for a camera or a button. Typing never re-renders (it would drop focus); a change of
   * kind does. */
  _dialogForm(target, page, block) {
    const doc = this.ownerDocument;
    const dlg = this._dialog;
    const state = this._state;
    const form = el(doc, "div", "form");
    form.dataset.dialog = dlg.list;
    const isCamera = dlg.list === "cameras";
    const editing = dlg.mode === "edit";

    form.append(el(doc, "strong", null, `${editing ? "Edit" : "Add"} ${isCamera ? "a camera" : "a button"} (block "${block.title}")`));
    this._dropElPreview();
    // a door or Talk button is offered only where the block has a door for it to act on
    const ringPage = pageKind(page) === "ring";
    const placeable = !editing ? this._placeable(isCamera ? "camera" : "button").filter((e) => isCamera || (!buttonBlockProblem(e, block) && !(ringPage && isTalkElement(e)))) : [];
    const fromElements = !editing && this._dialogSource(dlg) === "elements";
    const refButton = !isCamera && (fromElements || (editing && !!dlg.element));      // a button that is a reference: label, show text, ask first
    if (!editing) form.append(this._sourceSwitch(isCamera ? "camera-source" : "button-source", this._dialogSource(dlg), (value) => { dlg.source = value; dlg.error = ""; this._render(); }, isCamera ? "camera" : "button"));

    let kindDef = null;
    if (!isCamera && !editing && !fromElements) {
      const sel = el(doc, "select");
      sel.dataset.field = "kind";
      for (const [k, v] of Object.entries(KINDS)) {
        if (k === "talk" && ringPage) continue;       // a ring is not the moment to start a talk
        const o = el(doc, "option", null, v.text);
        o.value = k;
        if (dlg.kind === k) o.selected = true;
        sel.append(o);
      }
      sel.addEventListener("change", () => {
        dlg.kind = sel.value; dlg.target = ""; dlg.filter = ""; dlg.confirm = KINDS[sel.value].confirm; this._render();
      });
      const lab = el(doc, "label", null, "What it does");
      lab.append(sel);
      form.append(lab);
    }
    if (!isCamera) kindDef = KINDS[dlg.kind] || { domain: null, confirm: false };

    const labelInput = el(doc, "input");
    let specOfChoice = null;
    if (fromElements) {
      form.append(this._elementPicker(dlg, placeable));
    } else if (!editing) {
      const domain = isCamera ? "camera" : kindDef.domain;
      const select = el(doc, "select");
      select.dataset.field = "target";
      const fill = () => {
        select.replaceChildren();
        const options = domain === null ? doorOptions(state) : entityOptions(this._hass && this._hass.states, domain, dlg.filter);
        const blockDoor = domain === null && block.door ? (options.find((d) => d.value === block.door) || { label: block.door }).label : null;
        const blank = el(doc, "option", null, blockDoor !== null ? `This block's door (${blockDoor})` : options.length ? "Choose..." : "Nothing found");
        blank.value = "";
        select.append(blank);
        for (const opt of options) {
          const o = el(doc, "option", null, opt.label);
          o.value = opt.value;
          o.dataset.name = opt.name;
          if (isCamera) { o.dataset.cam = opt.value; o.dataset.base = opt.label; this._applyOptionSpec(o); }
          if (opt.value === dlg.target) o.selected = true;
          select.append(o);
        }
      };
      if (domain !== null) {
        const filter = el(doc, "input");
        filter.type = "search";
        filter.placeholder = "Filter...";
        filter.value = dlg.filter;
        filter.dataset.field = "filter";
        filter.addEventListener("input", () => {
          dlg.filter = filter.value;
          fill();
          if (isCamera) this._askCameras(Array.from(select.querySelectorAll("option[data-cam]")).slice(0, SPEC_BATCH).map((o) => o.dataset.cam));
        });
        const lab = el(doc, "label", null, isCamera ? "Camera" : `Entity (${domain})`);
        lab.append(filter, select);
        if (isCamera) {                       // the chosen feed's spec, beside the list
          specOfChoice = this._specChip(dlg.target);
          specOfChoice.dataset.specFor = "target";
          lab.append(specOfChoice);
        }
        form.append(lab);
      } else if (dlg.kind === "talk") {
        form.append(el(doc, "div", "sub", block.door ? "Talks to this block's door." : "This block has no door yet: pick one for the block first."));
      } else {
        const lab = el(doc, "label", null, "Door");
        lab.append(select);
        form.append(lab);
      }
      fill();
      select.addEventListener("change", () => {
        dlg.target = select.value;
        if (specOfChoice) {
          specOfChoice.dataset.spec = select.value;
          this._applySpec(specOfChoice);
          this._askCameras([select.value]);
        }
        const chosen = select.selectedOptions[0];
        if (!dlg.labelTouched && chosen && chosen.dataset.name) {
          dlg.label = chosen.dataset.name.slice(0, MAX_LABEL);
          labelInput.value = dlg.label;
        }
      });
    } else if (dlg.element) {          // editing a placement: the element is shown, never edited here
      const element = this._elMap && this._elMap.get(dlg.element);
      if (element) {
        form.append(this._elementPreview(element));
        form.append(el(doc, "div", "sub", `Element: ${element.name} (${elementWhat(element)}). Defined on the Elements page. Change the ${isCamera ? "entity, icon or colour" : "action, entity, icon or colour"} there.`));
      } else {
        form.append(el(doc, "div", "msg err", this._elMap ? "The element was removed." : "The element could not be read just now."));
      }
    } else {
      form.append(el(doc, "div", "sub", isCamera ? dlg.entity : `${KIND_TEXT[dlg.kind] || dlg.kind} · ${dlg.entity}`));
      if (isCamera) form.append(this._specChip(dlg.entity));
    }

    labelInput.value = dlg.label;
    labelInput.maxLength = MAX_LABEL;
    labelInput.dataset.field = "label";
    labelInput.addEventListener("input", () => { dlg.label = labelInput.value; dlg.labelTouched = true; });
    if (isCamera) labelInput.placeholder = "Leave empty to show no name";
    const labLabel = el(doc, "label", null, isCamera ? `Name (up to ${MAX_LABEL} characters; leave empty to show no name)` : `Name (up to ${MAX_LABEL} characters)`);
    labLabel.append(labelInput);
    form.append(labLabel);

    if (isCamera) {
      const size = el(doc, "select");
      size.dataset.field = "size";
      for (const value of CAMERA_SIZES) {
        const o = el(doc, "option", null, SIZE_TEXT[value]);
        o.value = value;
        if ((dlg.size || "auto") === value) o.selected = true;
        size.append(o);
      }
      size.addEventListener("change", () => { dlg.size = size.value; });
      const lab = el(doc, "label", null, "Size on the phone");
      lab.append(size);
      form.append(lab);
      const liveIn = el(doc, "input");
      liveIn.type = "checkbox";
      liveIn.checked = !!dlg.live;
      liveIn.dataset.field = "live";
      liveIn.addEventListener("change", () => { dlg.live = liveIn.checked; });
      const liveLab = el(doc, "label", null, `Live (${LIVE_HINT_TEXT})`);
      liveLab.append(liveIn);
      form.append(liveLab);
    } else if (refButton) {
      // A button from an element: the element owns action, entity, icon and colour. Only the name, show-text and ask-first are this screen's.
      const chosen = this._elMap && this._elMap.get(dlg.element);
      const showText = el(doc, "input");
      showText.type = "checkbox";
      showText.checked = dlg.show_label !== false;
      showText.dataset.field = "show-label";
      showText.addEventListener("change", () => { dlg.show_label = showText.checked; });
      const lab3 = el(doc, "label", null, "Show the name on the button (hidden: the element needs an icon from the list; the name stays for screen readers)");
      lab3.append(showText);
      form.append(lab3);
      if (chosen && !NO_CONFIRM_KINDS.has(buttonAction(chosen))) {
        const locked = askLocked(chosen);
        const confirm = el(doc, "input");
        confirm.type = "checkbox";
        confirm.checked = locked || !!dlg.confirm;
        confirm.disabled = locked;       // SAFETY: a door, lock or gate button always asks first; a screen can add the question, never remove it
        confirm.dataset.field = "confirm";
        confirm.addEventListener("change", () => { dlg.confirm = confirm.checked; });
        const lab2 = el(doc, "label", null, locked ? "Requires confirmation before it runs (always on for a door, lock or gate)" : "Requires confirmation before it runs");
        lab2.append(confirm);
        form.append(lab2);
      }
    } else {
      const iconLab = el(doc, "div", "pickerlab");
      iconLab.append(el(doc, "div", "hint", "Icon (the names the app draws)"), this._iconPicker(dlg));
      form.append(iconLab);
      const colorLab = el(doc, "div", "pickerlab");
      colorLab.append(el(doc, "div", "hint", "Colour (the eight names the app draws)"), this._colorPicker(dlg));
      form.append(colorLab);

      const showText = el(doc, "input");
      showText.type = "checkbox";
      showText.checked = dlg.show_label !== false;
      showText.dataset.field = "show-label";
      showText.addEventListener("change", () => { dlg.show_label = showText.checked; });
      const lab3 = el(doc, "label", null, "Show the name on the button (hidden: it needs an icon from the list; the name stays for screen readers)");
      lab3.append(showText);
      form.append(lab3);
      if (!NO_CONFIRM_KINDS.has(dlg.kind)) {
        const confirm = el(doc, "input");
        confirm.type = "checkbox";
        confirm.checked = !!dlg.confirm;
        confirm.dataset.field = "confirm";
        confirm.addEventListener("change", () => { dlg.confirm = confirm.checked; });
        const lab2 = el(doc, "label", null, "Requires confirmation before it runs");
        lab2.append(confirm);
        form.append(lab2);
      }
    }
    if (dlg.error) form.append(el(doc, "div", "msg err", dlg.error));

    const acts = el(doc, "div", "acts");
    acts.append(this._btn("Cancel", () => { this._dialog = null; this._render(); }, { act: "dialog-cancel" }));
    acts.append(this._btn(editing ? "Save item" : "Add", () => this._submitDialog(target, page), { cls: "primary", act: "dialog-ok" }));
    form.append(acts);
    return form;
  }

  /* "elements" or "define" for the camera dialog: the owner's choice, else From Elements when a placeable camera element exists. */
  _dialogSource(dlg) {
    return dlg.source || (this._placeable(dlg.list === "cameras" ? "camera" : "button").length ? "elements" : "define");
  }

  /* The two-way switch "From Elements / Define here" (camera dialog, rule and screen camera pickers). */
  _sourceSwitch(field, current, onPick, what = "camera") {
    const doc = this.ownerDocument;
    const group = el(doc, "div", "sourceswitch");
    group.setAttribute("role", "group");
    group.setAttribute("aria-label", `Where the ${what} comes from`);
    group.dataset.field = field;
    for (const [value, text] of [["elements", "From Elements"], ["define", "Define here"]]) {
      const b = this._btn(text, () => onPick(value), { cls: current === value ? "small on" : "small", act: `${field}-${value}` });
      b.setAttribute("aria-pressed", current === value ? "true" : "false");
      b.dataset.source = value;
      group.append(b);
    }
    if (current === "define") group.append(el(doc, "span", "sub", "Used only on this screen. Elements can be reused on many."));
    return group;
  }

  /* The list of placeable camera elements (a filter, one row each) with the live preview of the chosen one beneath. */
  _elementPicker(dlg, placeable) {
    const doc = this.ownerDocument;
    const wrap = el(doc, "div", "elementpicker");
    if (!placeable.length) {
      wrap.append(el(doc, "div", "empty", this._elMap
        ? `No ${dlg.list === "cameras" ? "camera" : "button"} elements yet. Add one on the Elements page, or use Define here.`
        : "The elements could not be read just now. Use Define here."));
      return wrap;
    }
    const filter = el(doc, "input");
    filter.type = "search";
    filter.placeholder = "Filter...";
    filter.value = dlg.filter || "";
    filter.dataset.field = "element-filter";
    const list = el(doc, "div", "elementlist");
    list.setAttribute("role", "radiogroup");
    list.setAttribute("aria-label", dlg.list === "cameras" ? "Camera elements" : "Button elements");
    list.dataset.field = "element";
    const fill = () => {
      list.replaceChildren();
      const shown = [];
      const needle = (dlg.filter || "").trim().toLowerCase();
      for (const e of placeable) {
        if (needle && !e.name.toLowerCase().includes(needle) && !elementWhat(e).toLowerCase().includes(needle)) continue;
        const on = e.id === dlg.element;
        const b = el(doc, "button", on ? "elrow on" : "elrow", `${e.name} · ${elementWhat(e)}`);
        if (dlg.list === "cameras" && e.entity_id) { b.append(" ", this._specChip(e.entity_id)); shown.push(e.entity_id); }
        if (dlg.list === "cameras" && smallOf(e)) { b.append(" ", this._specChip(smallOf(e))); shown.push(smallOf(e)); }
        b.type = "button";
        b.setAttribute("role", "radio");
        b.setAttribute("aria-checked", on ? "true" : "false");
        b.dataset.element = e.id;
        b.addEventListener("click", () => {
          dlg.element = e.id;
          if (!dlg.labelTouched) dlg.label = e.name.slice(0, MAX_LABEL);
          if (dlg.list !== "cameras") { dlg.confirm = askLocked(e); dlg.show_label = true; }
          this._render();
        });
        list.append(b);
      }
      if (shown.length) this._askCameras(shown);        // cached sizes cost no call; a row shown for the first time is asked
    };
    filter.addEventListener("input", () => { dlg.filter = filter.value; fill(); });
    fill();
    wrap.append(filter, list);
    const chosen = this._elMap && this._elMap.get(dlg.element);
    if (chosen && (dlg.list === "cameras" ? placeableCamera(chosen) : placeableButton(chosen))) {
      wrap.append(this._elementPreview(chosen));
      wrap.append(el(doc, "div", "sub", `Defined on the Elements page. Change the ${dlg.list === "cameras" ? "entity, icon or colour" : "action, entity, icon or colour"} there.`));
    } else {
      wrap.append(el(doc, "div", "sub", `Pick a ${dlg.list === "cameras" ? "camera" : "button"} element to see it here.`));
    }
    return wrap;
  }

  _submitDialog(target, page) {
    const dlg = this._dialog;
    const state = this._state;
    try {
      let doc;
      if (dlg.mode === "edit") {
        const named = dlg.element && this._elMap && this._elMap.get(dlg.element);
        if (dlg.list === "actions" && dlg.element && !named) throw new EditError("The element was removed; choose another one first");
        doc = updateTile(state.draft, target, page.id, dlg.list, dlg.tileId,
          dlg.list === "cameras" ? { label: named && dlg.label === named.name ? null : dlg.label, size: dlg.size || "auto", live: !!dlg.live }
            : dlg.element ? { label: dlg.label === named.name ? null : dlg.label, confirm: dlg.confirm, show_label: dlg.show_label !== false }
            : { label: dlg.label, icon: dlg.icon || "none", confirm: dlg.confirm, color: dlg.color || "default", show_label: dlg.show_label !== false },
          named || undefined);
      } else if (dlg.list === "actions" && this._dialogSource(dlg) === "elements") {
        const element = this._elMap && this._elMap.get(dlg.element);
        if (!element) throw new EditError("Pick a button element");
        doc = addButtonRef(state.draft, target, page.id, element, { label: dlg.label, show_label: dlg.show_label !== false, confirm: dlg.confirm }, dlg.blockId);
      } else if (dlg.list === "cameras" && this._dialogSource(dlg) === "elements") {
        const element = this._elMap && this._elMap.get(dlg.element);
        if (!element) throw new EditError("Pick a camera element");
        doc = addCameraRef(state.draft, target, page.id, element, { label: dlg.label, size: dlg.size || "auto", live: !!dlg.live }, dlg.blockId);
      } else if (dlg.list === "cameras") {
        doc = addCamera(state.draft, target, page.id, dlg.target, dlg.label, dlg.blockId);
        if ((dlg.size && dlg.size !== "auto") || dlg.live) {
          const added = pageCameras(pagesOf(doc, target).find((p) => p.id === page.id)).at(-1);
          doc = updateTile(doc, target, page.id, "cameras", added.id, { size: dlg.size || "auto", live: !!dlg.live });
        }
      } else {
        doc = addAction(state.draft, target, page.id, { kind: dlg.kind, target: dlg.target, label: dlg.label, icon: dlg.icon, confirm: dlg.confirm,
                                                         color: dlg.color, show_label: dlg.show_label !== false, blockId: dlg.blockId });
      }
      commit(state, doc);
      this._dialog = null;
      this._msg = null;
    } catch (err) {
      if (!(err instanceof EditError)) throw err;
      dlg.error = err.message;
    }
    this._changed();
  }

  /* ---- the Screens tab (K3): declare, edit and delete a kiosk screen ---- */

  /* A filter box and a select of camera entities; `pick(entityId)` runs when one is chosen. */
  _cameraPicker(field, pick, exclude) {
    const doc = this.ownerDocument;
    const placeable = this._placeable();
    const source = this._pickerSource[field] || (placeable.length ? "elements" : "define");
    const wrapper = el(doc, "div", "camerapicker");
    wrapper.append(this._sourceSwitch(`${field}-source`, source, (value) => { this._pickerSource[field] = value; this._render(); }));
    if (source === "elements") {
      wrapper.append(this._elementSelect(field, placeable, pick, exclude));
      return wrapper;
    }
    wrapper.append(this._entityCameraPicker(field, pick, exclude));
    return wrapper;
  }

  /* A filter box and a select of camera elements; `pick(elementId)` runs when one is chosen. */
  _elementSelect(field, placeable, pick, exclude) {
    const doc = this.ownerDocument;
    const lab = el(doc, "label", null, "Add a camera element");
    const filter = el(doc, "input");
    filter.type = "search";
    filter.placeholder = "Filter...";
    filter.dataset.field = `${field}-element-filter`;
    const select = el(doc, "select");
    select.dataset.field = `${field}-element`;
    const fill = () => {
      select.replaceChildren();
      const blank = el(doc, "option", null, placeable.length ? "Choose..." : this._elMap ? "No camera elements yet" : "Elements not available");
      blank.value = "";
      select.append(blank);
      const needle = filter.value.trim().toLowerCase();
      for (const e of placeable) {
        if (exclude.includes(e.id)) continue;
        if (needle && !e.name.toLowerCase().includes(needle) && !`${e.entity_id} ${smallOf(e)}`.toLowerCase().includes(needle)) continue;
        const o = el(doc, "option", null, `${e.name} (${streamsText(e)})`);
        o.value = e.id;
        o.dataset.cam = e.entity_id;
        o.dataset.base = o.textContent;
        this._applyOptionSpec(o);
        select.append(o);
      }
    };
    filter.addEventListener("input", fill);
    select.addEventListener("change", () => { if (select.value) pick(select.value); });
    fill();
    lab.append(filter, select);
    return lab;
  }

  /* How a rule's or a screen's camera entry reads: an entity id as before, an element id by the element's name. */
  _cameraEntry(entry) {
    const states = this._hass && this._hass.states;
    if (isElementId(entry)) {
      const element = this._elMap && this._elMap.get(entry);
      if (element && placeableCamera(element)) return { title: element.name, sub: `Element · ${streamsText(element)}`, entity: element.entity_id, element: true, removed: false };
      return { title: this._elMap ? "Element removed" : entry, sub: this._elMap ? `Element ${entry} was removed` : "Element", entity: "", element: true, removed: !!this._elMap };
    }
    const st = states && states[entry];
    return { title: (st && st.attributes && st.attributes.friendly_name) || entry, sub: entry, entity: entry, element: false, removed: false };
  }

  _cameraEntryRow(entry, onRemove, removeAct) {
    const doc = this.ownerDocument;
    const info = this._cameraEntry(entry);
    const row = el(doc, "div", info.removed ? "row dangling" : "row");
    row.dataset.camera = entry;
    if (info.element) {
      row.dataset.element = entry;
      if (info.removed) row.dataset.state = "element-removed";
    }
    const txt = el(doc, "div", "txt");
    txt.append(el(doc, "div", info.removed ? "label muted" : "label", info.title));
    if (info.element) {
      const badge = el(doc, "span", "chip elbadge", info.removed ? "element removed" : "element");
      badge.dataset.badge = "element";
      txt.append(badge);
    }
    txt.append(el(doc, "div", "sub", info.sub));
    if (info.entity) txt.append(this._specChip(info.entity));
    row.append(txt);
    row.append(this._btn("×", onRemove, { cls: "small", act: removeAct, title: "Remove" }));
    return row;
  }

  _entityCameraPicker(field, pick, exclude) {
    const doc = this.ownerDocument;
    const lab = el(doc, "label", null, "Add a camera");
    const filter = el(doc, "input");
    filter.type = "search";
    filter.placeholder = "Filter...";
    filter.dataset.field = `${field}-filter`;
    const select = el(doc, "select");
    select.dataset.field = field;
    const fill = () => {
      select.replaceChildren();
      const blank = el(doc, "option", null, "Choose...");
      blank.value = "";
      select.append(blank);
      for (const opt of entityOptions(this._hass && this._hass.states, "camera", filter.value)) {
        if (exclude.includes(opt.value)) continue;
        const o = el(doc, "option", null, opt.label);
        o.value = opt.value;
        o.dataset.cam = opt.value;
        o.dataset.base = opt.label;
        this._applyOptionSpec(o);
        select.append(o);
      }
    };
    filter.addEventListener("input", () => {
      fill();
      this._askCameras(Array.from(select.querySelectorAll("option[data-cam]")).slice(0, SPEC_BATCH).map((o) => o.dataset.cam));
    });
    select.addEventListener("change", () => { if (select.value) pick(select.value); });
    fill();
    lab.append(filter, select);
    return lab;
  }

  /* The Devices tab. Schema 6: "Enrol a device" and Refresh on top, then ONE SECTION PER DEVICE, all open and stacked: the phones
   * (alphabetical; drawn by the Phones module with this file's selectors), then the screens (alphabetical), then the Add a screen
   * form. House calls and the push ring notification and app are on the Settings tab. Before schema 6: House calls and Phones (the Phones
   * module's page, which writes at once), then Screens (a draft, saved with the Save bar). While a phone's own page is open the
   * module hides the Screens section and the bar. */
  _devicesTab() {
    const doc = this.ownerDocument;
    const box = el(doc, "div", "devices");
    box.dataset.tab = "devices";
    const v6 = isPagesModel(this._state.draft);
    if (this._phones) {
      this._phones.attach(box);
      if (v6 && this._phones.view === "list") this._phones._draw(true);      // the phone sections hold this render's selectors
    } else box.append(el(doc, "p", "hint", "Phones are not available here: that part of the panel did not load, or this login is not an administrator."));
    if (v6) {
      const section = el(doc, "div", "devscreens devicelist");
      section.dataset.section = "screens";
      section.append(this._screenSections());
      const left = this._leftoverDevices();
      if (left) section.append(left);
      box.append(section);
      return box;
    }
    const section = el(doc, "section", "devscreens");
    section.dataset.section = "screens";
    section.append(this._screensTab());
    box.append(section);
    return box;
  }

  /* Schema 6 "Not attached": the device entries no phone or screen answers to (a removed phone, a screen that was deleted). Each is listed with
   * the pages it still holds, and can be deleted (named in `drop_orphans` on Save); until then it keeps those pages "in use". */
  _leftoverDevices() {
    const state = this._state;
    const keys = orphanKeys(state);
    if (!keys.length) return null;
    const doc = this.ownerDocument;
    const wrap = el(doc, "div", "leftover");
    wrap.dataset.section = "not-attached";
    wrap.append(el(doc, "h2", null, "Not attached"));
    wrap.append(el(doc, "p", "hint", "Old device entries that no phone or screen answers to any more. They show nothing, but a page they hold cannot be deleted. Delete them here; nothing changes until you Save."));
    const byId = new Map(pagesOf(state.draft, LIBRARY).map((p) => [p.id, p]));
    for (const key of keys) {
      const row = el(doc, "div", "row");
      row.dataset.leftover = key;
      const names = devicePageIds(state.draft, key).map((id) => (byId.get(id) || {}).label || id);
      const txt = el(doc, "div", "txt");
      txt.append(el(doc, "span", null, `Old: ${shortKey(key)}${names.length ? ` (pages: ${names.join(", ")})` : ""}`));
      row.append(txt, this._btn("Delete", () => this._edit((s) => deleteOrphan(s, key)), { cls: "small danger", act: "leftover-delete", title: `Delete the old entry ${key}` }));
      wrap.append(row);
    }
    return wrap;
  }

  /* One phone's own settings in its section (schema 6): the two selectors, keyed by the phone's endpoint id. */
  /* DEV-807: the server marks a FORMER panel screen (a declared screen whose login is a panel's) on its endpoint row (`app_screen`). */
  _isAppScreen(id) {
    if (!this._state) return false;
    const loaded = (this._state.loaded && this._state.loaded.endpoints) || [];       // the draft's own rows (dashboardRow) do not carry the flag
    return loaded.some((r) => r && r.kind === "dashboard" && r.id === id && r.app_screen === true);
  }

  /* The panels' phones from the endpoint rows the server sent (`panel: true` on a phone row): `[{id, user, name}]`, what the ownership
   * rules need (a panel owns door Talk elements on its own pages; its former screen does not). */
  _panelOwners() {
    const loaded = (this._state && this._state.loaded && this._state.loaded.endpoints) || [];
    return loaded.filter((r) => r && r.kind === "phone" && r.panel === true).map((r) => ({ id: r.id, user: r.ha_user_id, name: r.name }));
  }

  _phoneDeviceBody(row) {
    if (!this._perDevice()) return null;
    const doc = this.ownerDocument;
    const id = this._phoneEndpointId(row.name);
    const box = el(doc, "div", "devbody");
    if (row.panel === true) {      // own-pages design: a panel owns its pages like a phone (devices[<phone endpoint id>])
      if (id === null) {
        box.append(el(doc, "p", "hint", "Its pages cannot be chosen here yet: the content document has no single phone with this name."));
        return box;
      }
      box.dataset.panel = id;
      box.dataset.endpoint = id;
      box.append(el(doc, "p", "hint", "This phone is a panel. Like a phone, it has its own pages and Ring pages; one page is the usual case."));
      const rings = el(doc, "p", "hint", this._elMap ? ringsForText(this._state.draft, id, this._elMap, this._panelOwners()) : "Rings for these doors: not known yet (the Talk elements are loading)");
      rings.dataset.hint = "rings-for";
      box.append(rings);
      box.append(this._pagesSelector(id, this._state.draft));
      return box;
    }
    if (id === null) {
      box.append(el(doc, "p", "hint", "Its pages cannot be chosen here yet: the content document has no single phone with this name."));
      return box;
    }
    box.dataset.endpoint = id;
    box.append(this._pagesSelector(id, this._state.draft));
    return box;
  }

  /* Schema 6: one section per screen, alphabetical, all open. */
  _screenSections() {
    const doc = this.ownerDocument;
    const state = this._state;
    const wrap = el(doc, "div", "screens");
    wrap.append(el(doc, "h2", null, "Screens"));
    wrap.append(el(doc, "p", "hint", "A screen is a Home Assistant panel you can put in other UI (for example a dashboard tab). It wakes itself when a call dials a Talk element it contains. What it shows, and what a ring shows on it, are chosen in its section. The pages themselves are made on the Pages tab."));
    if (!Array.isArray(state.loaded.ha_users)) {
      const old = el(doc, "div", "msg err", KIOSK_USERS_TEXT);
      old.dataset.state = "users-missing";
      wrap.append(old);
    }
    const screens = Object.entries(dashboardsOf(state.draft)).sort((a, b) => String((a[1] && a[1].name) || a[0]).localeCompare(String((b[1] && b[1].name) || b[0])));
    if (!screens.length) wrap.append(el(doc, "div", "empty", "No screen yet."));
    const list = el(doc, "div", "devsections");
    list.dataset.role = "screen-sections";
    for (const [id, d] of screens) {
      const sec = el(doc, "section", "devsection");
      sec.dataset.device = "screen";
      sec.dataset.screen = id;
      const head = el(doc, "div", "devhead");
      const name = (d && d.name) || id;
      head.append(el(doc, "h2", "devtitle", name));
      const kind = el(doc, "span", "chip");
      kind.dataset.chip = "kind";
      kind.textContent = this._isAppScreen(id) ? "Former panel screen" : "Screen";
      head.append(kind, el(doc, "span", "grow"));
      head.append(this._menuButton(`Actions for ${name}`, "screen-menu", [
        { label: "Edit", act: "edit-screen", run: () => { const f = sec.querySelector('[data-field="screen-name"]'); if (f && typeof f.focus === "function") f.focus(); } },
        { label: "Delete", act: "delete-screen", danger: true, run: () => this._edit((s) => {
          commit(s, deleteScreen(s.draft, id)); s.screenId = null;
        }) },
      ]));
      sec.append(head);
      sec.append(this._screenDetail(id, true));
      list.append(sec);
    }
    wrap.append(list);
    wrap.append(this._addScreenForm());
    return wrap;
  }

  /* A three-dots menu (mdi:dots-vertical) with a small popup of our own, the same pattern as the Phones list (see
   * PhonesController._menu): aria-haspopup / aria-expanded on the trigger, role="menu" with role="menuitem" buttons, arrow
   * keys, Escape (focus back to the trigger), Tab and a click anywhere else close it. */
  _menuButton(label, act, items) {
    const doc = this.ownerDocument;
    const wrap = el(doc, "span", "menu");
    wrap.dataset.menu = act;
    const trigger = el(doc, "button", "dots");
    trigger.type = "button";
    trigger.dataset.act = act;
    trigger.setAttribute("aria-label", label);
    trigger.setAttribute("aria-haspopup", "menu");
    trigger.setAttribute("aria-expanded", "false");
    const NS = "http://www.w3.org/2000/svg";
    const svg = doc.createElementNS(NS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("aria-hidden", "true");
    const path = doc.createElementNS(NS, "path");
    path.setAttribute("d", "M12,16A2,2 0 0,1 14,18A2,2 0 0,1 12,20A2,2 0 0,1 10,18A2,2 0 0,1 12,16M12,10A2,2 0 0,1 14,12A2,2 0 0,1 12,14A2,2 0 0,1 10,12A2,2 0 0,1 12,10M12,4A2,2 0 0,1 14,6A2,2 0 0,1 12,8A2,2 0 0,1 10,6A2,2 0 0,1 12,4Z");
    svg.append(path);
    trigger.append(svg);
    const pop = el(doc, "div", "pop");
    pop.setAttribute("role", "menu");
    pop.setAttribute("aria-label", label);
    pop.hidden = true;
    const buttons = [];
    let away = null;
    const close = (refocus) => {
      pop.hidden = true;
      trigger.setAttribute("aria-expanded", "false");
      if (away) { doc.removeEventListener("click", away, true); away = null; }
      if (refocus && typeof trigger.focus === "function") trigger.focus();
    };
    const open = () => {
      pop.hidden = false;
      trigger.setAttribute("aria-expanded", "true");
      away = (ev) => {
        if (!wrap.isConnected) { close(false); return; }
        const path = typeof ev.composedPath === "function" ? ev.composedPath() : [];
        if (!path.includes(wrap)) close(false);
      };
      doc.addEventListener("click", away, true);
      if (buttons[0] && typeof buttons[0].focus === "function") buttons[0].focus();
    };
    for (const item of items) {
      const b = el(doc, "button", item.danger ? "danger" : "", item.label);
      b.type = "button";
      b.setAttribute("role", "menuitem");
      b.dataset.act = item.act;
      b.addEventListener("click", () => { close(false); item.run(); });
      buttons.push(b);
      pop.append(b);
    }
    trigger.addEventListener("click", () => { if (pop.hidden) open(); else close(true); });
    wrap.addEventListener("keydown", (ev) => {
      if (pop.hidden) return;
      if (ev.key === "Escape") { ev.stopPropagation(); close(true); return; }
      if (ev.key === "Tab") { close(false); return; }
      if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
        ev.preventDefault();
        const root = wrap.getRootNode ? wrap.getRootNode() : doc;
        const cur = buttons.indexOf(root.activeElement);
        const i = cur < 0 ? (ev.key === "ArrowDown" ? 0 : buttons.length - 1) : (cur + (ev.key === "ArrowDown" ? 1 : -1) + buttons.length) % buttons.length;
        if (buttons[i]) buttons[i].focus();
      }
    });
    wrap.append(trigger, pop);
    return wrap;
  }

  _screensTab() {
    const doc = this.ownerDocument;
    const state = this._state;
    const box = el(doc, "div", "screens");
    box.append(el(doc, "h2", null, "Screens"));
    box.append(el(doc, "p", "hint", "A screen is a Home Assistant panel you can put in other UI (for example a dashboard tab). It wakes itself when a call dials a Talk element it contains."));
    box.append(el(doc, "p", "hint", isPagesModel(state.draft)
      ? "What it shows, and what a ring shows on it, are chosen in its details here. The pages themselves are made on the Pages tab."
      : "Its pages and what a ring shows are edited on the Pages and Ring & call tabs."));
    if (!Array.isArray(state.loaded.ha_users)) {
      const old = el(doc, "div", "msg err", KIOSK_USERS_TEXT);
      old.dataset.state = "users-missing";
      box.append(old);
    }
    const screens = Object.entries(dashboardsOf(state.draft));
    if (!screens.some(([id]) => id === state.screenId)) state.screenId = screens.length ? screens[0][0] : null;

    const cols = el(doc, "div", "cols");
    const list = el(doc, "div", "screenlist");
    if (!screens.length) list.append(el(doc, "div", "empty", "No screen yet."));
    for (const [id, d] of screens) {
      const row = el(doc, "div", "row" + (id === state.screenId ? " cur" : ""));
      row.dataset.screen = id;
      const txt = el(doc, "div", "txt");
      txt.append(this._btn((d && d.name) || id, () => { state.screenId = id; this._msg = null; this._render(); }, { cls: "small", act: "pick-screen" }));
      txt.append(el(doc, "div", "sub", id));
      if (isPagesModel(state.draft)) txt.append(this._wakesOn(id));
      row.append(txt);
      row.append(this._menuButton(`Actions for ${(d && d.name) || id}`, "screen-menu", [
        { label: "Edit", act: "edit-screen", run: () => { state.screenId = id; this._msg = null; this._render(); } },
        { label: "Delete", act: "delete-screen", danger: true, run: () => this._edit((s) => {
          commit(s, deleteScreen(s.draft, id)); s.screenId = null;
        }) },
      ]));
      list.append(row);
    }
    list.append(this._addScreenForm());
    cols.append(list);
    cols.append(this._screenDetail());
    box.append(cols);
    return box;
  }

  _addScreenForm() {
    const doc = this.ownerDocument;
    const state = this._state;
    const form = el(doc, "div", "form");
    form.dataset.form = "new-screen";
    form.append(el(doc, "strong", null, "Add a screen"));
    const mk = (labelText, field, placeholder) => {
      const lab = el(doc, "label", null, labelText);
      const input = el(doc, "input");
      input.dataset.field = field;
      if (placeholder) input.placeholder = placeholder;
      input.maxLength = field === "new-screen-slug" ? 32 : MAX_LABEL;
      lab.append(input);
      form.append(lab);
      return input;
    };
    const name = mk(`Name (up to ${MAX_LABEL} characters)`, "new-screen-name", "Kitchen tablet");
    const addr = mk("Address (empty: made from the name)", "new-screen-slug", "kitchen");
    const room = mk("Room (optional)", "new-screen-room", "Kitchen");
    const userLab = el(doc, "label", null, "Home Assistant user it is logged in as");
    const user = el(doc, "select");
    user.dataset.field = "new-screen-user";
    const blank = el(doc, "option", null, "Choose...");
    blank.value = "";
    user.append(blank);
    for (const opt of screenUserOptions(state, null)) {
      const o = el(doc, "option", null, opt.label);
      o.value = opt.value;
      user.append(o);
    }
    userLab.append(user);
    form.append(userLab);
    const go = () => this._edit((s) => {
      const r = addScreen(s.draft, { name: name.value, slug: addr.value, ha_user_id: user.value, room: room.value }, takenEndpointIds(s));
      commit(s, r.doc);
      s.screenId = r.slug;
    });
    const acts = el(doc, "div", "acts");
    acts.append(this._btn("+ Add screen", go, { cls: "primary", act: "add-screen" }));
    form.append(acts);
    return form;
  }

  _screenDetail(forId, inSection) {
    const doc = this.ownerDocument;
    const state = this._state;
    const box = el(doc, "div", "screendetail");
    const id = forId || state.screenId;
    const d = id ? dashboardsOf(state.draft)[id] : null;
    if (!d) {
      box.append(el(doc, "div", "empty", "Add a screen to start."));
      return box;
    }
    const upd = (patch) => this._edit((s) => commit(s, updateScreen(s.draft, id, patch)));
    if (!inSection) box.append(el(doc, "h3", null, `Screen: ${d.name || id}`));
    box.append(el(doc, "div", "sub", `Address: ${id} (it names the screen's pages and rules, so it does not change)`));

    const field = (labelText, node) => { const lab = el(doc, "label", null, labelText); lab.append(node); const wrap = el(doc, "div", "form"); wrap.append(lab); return wrap; };
    const name = el(doc, "input");
    name.value = d.name || "";
    name.maxLength = MAX_LABEL;
    name.dataset.field = "screen-name";
    name.addEventListener("change", () => upd({ name: name.value }));
    box.append(field("Name", name));

    const user = el(doc, "select");
    user.dataset.field = "screen-user";
    if (!d.ha_user_id) { const none = el(doc, "option", null, "Choose..."); none.value = ""; user.append(none); }
    for (const opt of screenUserOptions(state, d.ha_user_id)) {
      const o = el(doc, "option", null, opt.label);
      o.value = opt.value;
      if (opt.value === d.ha_user_id) o.selected = true;
      user.append(o);
    }
    user.addEventListener("change", () => upd({ ha_user_id: user.value }));
    const appScreen = this._isAppScreen(id);
    if (appScreen) user.disabled = true;               // DEV-807: a panel's screen is logged in as its phone's user; changing it would break the pair
    const userBox = field("Home Assistant user it is logged in as (non-administrators; \"open now\" = its kiosk page is open)", user);
    box.append(userBox);

    const room = el(doc, "input");
    room.value = d.room || "";
    room.maxLength = MAX_LABEL;
    room.dataset.field = "screen-room";
    room.addEventListener("change", () => upd({ room: room.value }));
    box.append(field("Room", room));

    box.append(el(doc, "h3", null, "Room cameras"));
    box.append(el(doc, "p", "hint", "What someone sees when this screen calls them."));
    const cams = d.cameras || [];
    if (!cams.length) box.append(el(doc, "div", "empty", "No cameras."));
    for (const entity of cams) {
      box.append(this._cameraEntryRow(entity, () => this._edit((s) => commit(s, removeScreenCamera(s.draft, id, entity))), "remove-screen-camera"));
    }
    box.append(this._cameraPicker("screen-camera-add", (entity) => this._edit((s) => commit(s, addScreenCamera(s.draft, id, entity))), cams));

    if (appScreen) {
      box.append(el(doc, "p", "hint", "This is the former screen of a panel. The panel now has its own pages (in its phone section) and this screen no longer rings or shows on it; it is kept until you delete it from its menu. It has no kiosk address."));
    }
    if (!appScreen) box.append(el(doc, "h3", null, "Kiosk address"));
    if (!appScreen) box.append(el(doc, "p", "hint", KIOSK_ADDRESS_HINT));
    const kioskHref = appScreen ? null : kioskUrl(id, this._pageOrigin());
    if (appScreen) {
      // no kiosk address and no card to paste
    } else if (kioskHref) {
      const urlRow = el(doc, "div", "urlrow");
      const url = el(doc, "input");
      url.readOnly = true;
      url.value = kioskHref;
      url.dataset.field = "kiosk-url";
      url.setAttribute("aria-label", "Kiosk address");
      urlRow.append(url, this._btn("Copy", () => this._copyUrl(kioskHref, "kiosk-url", "Address copied.", id), { act: "copy-url" }));
      box.append(urlRow);
    } else {
      box.append(el(doc, "div", "msg err", KIOSK_NO_ORIGIN_TEXT));
    }

    if (!appScreen) box.append(this._cardSnippet(id));

    if (!isPagesModel(state.draft)) {
      const own = pagesOf(state.draft, id).length;
      box.append(el(doc, "h3", null, "Pages"));
      box.append(el(doc, "p", "hint", `The screen shows the household pages and ${plural(own, "page of its own", "pages of its own")}.`));
      const acts = el(doc, "div", "acts");
      acts.append(this._btn("Edit its pages", () => { state.sel = `ep:${id}`; state.pageId = null; this._tabChosen = true; this._tab = "pages"; this._msg = null; this._changed(); }, { act: "edit-screen-pages" }));
      box.append(acts);
    }
    if (isPagesModel(state.draft)) {
      // P-C: the two selectors (functions of the endpoint key and the draft that return a node), and what the screen wakes on
      box.append(this._wakesOn(id));
      box.append(this._pagesSelector(id, state.draft));
    }
    if (inSection) {
      box.append(el(doc, "p", "hint", "Nothing changes until you Save."));
      return box;
    }
    box.append(el(doc, "p", "hint", "Delete a screen from the menu on its row. Deleting a screen keeps its own pages and rules under Not attached until you delete them there. Nothing changes until you Save."));
    return box;
  }

  /* The origin this page was opened at: the tablet logs in at its own, so the address shown is built from ours. */
  _pageOrigin() {
    const win = this.ownerDocument && this.ownerDocument.defaultView;
    return win && win.location ? win.location.origin : null;
  }

  /* The card to put in a dashboard view, ready to copy. `path` is optional (K2): the URL path of the view that holds the card, so
   * a call that wakes this screen can bring that view to the front even from another page. Without it the ring still shows,
   * on whatever page the screen is on. */
  _cardSnippet(id) {
    const doc = this.ownerDocument;
    const text = cardSnippetText(id);
    const wrap = el(doc, "div", "cardsnippet");
    wrap.append(el(doc, "h3", null, "Card for a dashboard"));
    wrap.append(el(doc, "p", "hint", "Paste this card into a dashboard view (Edit dashboard, Add card, Manual). Set path to the address of that view, so a call can bring it to the front."));
    const row = el(doc, "div", "urlrow");
    const ta = el(doc, "textarea");
    ta.readOnly = true;
    ta.rows = 5;
    ta.value = text;
    ta.dataset.field = "card-snippet";
    ta.setAttribute("aria-label", "Dashboard card for this screen");
    row.append(ta, this._btn("Copy", () => this._copyUrl(text, "card-snippet", "Card copied.", id), { act: "copy-card" }));
    wrap.append(row);
    return wrap;
  }

  async _copyUrl(url, field = "kiosk-url", okText = "Address copied.", screenId = null) {
    const win = this.ownerDocument.defaultView;
    let ok = false;
    try {
      await win.navigator.clipboard.writeText(url);
      ok = true;
    } catch (_err) { /* no clipboard permission on this origin: the address is selected for Ctrl+C */ }
    this._msg = ok ? { kind: "ok", text: okText } : { kind: "err", text: "Could not copy automatically. The text is selected; press Ctrl+C." };
    this._render();
    const scope = screenId ? this._root.querySelector(`[data-device="screen"][data-screen="${screenId}"]`) : null;
    const input = (scope || this._root).querySelector(`[data-field="${field}"]`);
    if (input && !ok) input.select();
  }

  /* ---- Pages unified (P-B): the Pages tab of a schema 6 document ---- */

  _deviceName(id) {
    const row = allRows(this._state).find((r) => r.id === id);
    return row ? rowName(row) : id;
  }

  _talkElements() {
    return this._elMap ? [...this._elMap.values()].filter(isTalkElement).sort((a, b) => a.name.localeCompare(b.name)) : [];
  }

  /* ---- Pages unified (P-C): the Devices tab's two selectors, mounted for a phone and for a screen ----
   * Each takes the endpoint key and the draft and returns a node; they read `draft.devices[key]` = {pages: [{page, as?}]}
   * and write with commit(state, newDoc). A page stores nothing about where it lands: what a device shows is decided here, and only here. */

  _pagesSelector(key, draft) {
    const doc = this.ownerDocument;
    const box = el(doc, "div", "form devsel");
    box.dataset.selector = "pages";
    box.dataset.device = key;
    box.append(el(doc, "h3", null, "Pages"));
    box.append(el(doc, "p", "hint", "The content pages this device shows, in order; the first is its Home. Two devices may show the same page: editing it changes it on both."));
    const library = pagesOf(draft, LIBRARY);
    const byId = new Map(library.map((p) => [p.id, p]));
    const ids = devicePageIds(draft, key);
    if (!ids.length) {
      const isScreen = !!dashboardsOf(draft)[key];
      const isPanel = this._panelOwners().some((p) => p.id === key);
      const none = el(doc, "div", "msg warn", `This ${isScreen ? "screen" : isPanel ? "panel" : "phone"} has no page yet. Until it has one, its app shows an empty Home. Add a first page, or pick an existing page below.`);
      none.dataset.state = "no-device-pages";
      none.setAttribute("role", "status");
      none.append(" ", this._btn("Add first page", () => this._addFirstPage(key), { cls: "primary small", act: "add-first-page", title: "Make an empty page for this device and open it in the editor" }));
      box.append(none);
    }
    ids.forEach((id, at) => {
      const page = byId.get(id);
      const row = el(doc, "div", "row");
      row.dataset.devicePage = id;
      const txt = el(doc, "div", "txt");
      txt.append(el(doc, "span", null, `${at + 1}. ${page ? page.label : `${id} (page removed)`}${at === 0 ? " (Home)" : ""}`));
      row.append(txt);
      row.append(this._btn("Up", () => this._edit((s) => commit(s, moveDevicePage(s.draft, key, id, -1))), { cls: "small", act: "device-page-up", disabled: at === 0, title: `Move ${page ? page.label : id} up` }));
      row.append(this._btn("Down", () => this._edit((s) => commit(s, moveDevicePage(s.draft, key, id, 1))), { cls: "small", act: "device-page-down", disabled: at === ids.length - 1, title: `Move ${page ? page.label : id} down` }));
      row.append(this._btn("×", () => this._edit((s) => commit(s, removeDevicePage(s.draft, key, id))), { cls: "small danger", act: "device-page-remove", title: `Remove ${page ? page.label : id} from this device` }));
      box.append(row);
    });
    const addable = library.filter((p) => pageKind(p) === "content" && !ids.includes(p.id));
    const addRow = el(doc, "div", "acts");
    const pick = el(doc, "select");
    pick.dataset.field = "device-page-pick";
    pick.setAttribute("aria-label", "Page to add");
    const blank = el(doc, "option", null, addable.length ? "Choose a page..." : "Every content page is already here");
    blank.value = "";
    pick.append(blank);
    for (const p of addable) { const o = el(doc, "option", null, p.label); o.value = p.id; pick.append(o); }
    addRow.append(pick, this._btn("Add page", () => this._edit((s) => commit(s, addDevicePage(s.draft, key, pick.value, this._elMap, this._panelOwners()))), { cls: "primary", act: "device-page-add" }));
    box.append(addRow);
    return box;
  }

  /* The device's name as the Devices tab shows it: a screen's declared name, a phone's endpoint row name. */
  _deviceBareName(key) {
    const screen = dashboardsOf(this._state.draft)[key];
    if (screen && screen.name) return screen.name;
    const row = rowsOf(this._state, ["phone"]).find((r) => r.id === key);
    return row ? row.name : key;
  }

  /* "Add first page": one edit of the draft (the new page and the device's list entry together), then the page is opened on the Pages tab.
   * It is saved like any other edit, with the Save bar: the server then validates the whole document at once (content/save). */
  _addFirstPage(key) {
    this._edit((s) => {
      const r = addFirstPage(s.draft, key, this._deviceBareName(key), this._elMap, this._panelOwners());
      commit(s, r.doc);
      s.pageId = r.pageId;
      this._tabChosen = true;
      this._tab = "pages";
      this._scrollTo = r.pageId;
    });
  }

  /* "Wakes on: ..." for a screen, computed from the draft with the server's rule (every screen holding a door Talk element wakes for it). */
  _wakesOn(key) {
    const doc = this.ownerDocument;
    const p = el(doc, "p", "hint", this._elMap ? wakesOnText(this._state.draft, key, this._elMap, this._panelOwners()) : "Wakes on: not known yet (the Talk elements are loading)");
    p.dataset.hint = "wakes-on";
    return p;
  }

  /* The Pages tab (schema 6): "Add page" on top, then ONE SECTION PER PAGE stacked down the tab, each titled with the page name:
   * the page's editor on the left (kind badge, per-kind editor, "Shown on", Delete) and its live preview on the right. A page has
   * no idea where it lands; the Devices tab says which pages a phone or screen shows. Each section is its own container, kept
   * between renders and rebuilt only when its page (or what it shows) changed, so editing one page never touches another's fields. */
  _libraryTab() {
    const doc = this.ownerDocument;
    const state = this._state;
    const pages = pagesOf(state.draft, LIBRARY);
    const layout = el(doc, "div", "layout one");
    layout.dataset.tab = "pages-unified";
    const work = el(doc, "div", "work");
    work.append(el(doc, "h3", null, "Pages"));
    work.append(el(doc, "p", "hint",
      "A Content page is a set of blocks of elements; the Devices tab chooses which a phone or screen shows. What a call shows is the Ring page of its Talk element, listed below under Rings."));
    work.append(this._oldClientHint());
    work.append(this._addPageForm());
    const list = el(doc, "div", "pagesections");
    list.dataset.role = "page-sections";
    const content = pages.filter((p) => pageKind(p) !== "ring");
    if (!content.length && !pages.length) list.append(el(doc, "div", "empty", "No pages yet. Add one to start."));
    const shown = new Set(content.map((p) => p.id));
    const rings = this._elMap ? ringEntries(state.draft, this._elMap) : null;
    if (rings) for (const e of rings.entries) if (e.page) shown.add(e.page.id);
    if (rings) for (const e of rings.removed) shown.add(e.page.id);
    for (const id of Array.from(this._sections.keys())) if (!shown.has(id)) this._sections.delete(id);
    for (const page of content) list.append(this._pageSection(page));
    if (!this._elMap) list.append(el(doc, "div", "empty", "The Talk elements are not available just now, so the Rings entries cannot be listed."));
    if (rings) {
      const head = el(doc, "h2", "ringsgroup", "Rings");
      head.dataset.role = "rings-group";
      list.append(head);
      if (!rings.entries.length) list.append(el(doc, "div", "empty", "No Talk element can ring yet. Add a door, phone or screen Talk element on the Elements tab."));
      for (const e of rings.entries) list.append(e.page ? this._pageSection(e.page, e) : this._defaultRingSection(e));
      if (rings.removed.length) {
        const rh = el(doc, "h2", "ringsgroup", "Removed");
        rh.dataset.role = "rings-removed";
        list.append(rh);
        for (const e of rings.removed) list.append(this._removedRingSection(e));
      }
    }
    work.append(list);
    layout.append(work);
    return layout;
  }

  /* What a section shows, as a string: a section is rebuilt only when this changes (or the document was reloaded). */
  _sectionSig(page) {
    const state = this._state;
    const dlg = this._dialog && this._dialog.pageId === page.id ? this._dialog : null;
    return JSON.stringify([page, shownOnText(state.draft, page.id, (id) => this._deviceName(id)), devicesUsingPage(state.draft, page.id), objId(this._elMap),
                           dlg, this._dark, this._secAs.get(page.id) || "", previewEndpoint({ ...state, pageId: page.id, previewAs: this._secAs.get(page.id) || state.previewAs })]);
  }

  _pageSection(page, entry) {
    const sig = this._sectionSig(page) + (entry ? JSON.stringify([entry.warn, this._ringAsk]) : "");
    const cached = this._sections.get(page.id);
    if (cached && cached.state === this._state && cached.sig === sig) return cached.node;
    const node = this._buildSection(page, entry);
    this._sections.set(page.id, { node, sig, state: this._state });
    return node;
  }

  /* One page's section: its name as the title, the editor on the left, the live preview on the right. */
  _buildSection(page, entry) {
    const doc = this.ownerDocument;
    const section = el(doc, "section", "pagesection");
    section.dataset.page = page.id;
    if (entry) section.dataset.ringState = "edited";
    section.dataset.kind = pageKind(page);
    const kind = pageKind(page);
    const head = el(doc, "div", "sectionhead");
    head.append(el(doc, "h2", "sectiontitle", page.label));
    const badge = el(doc, "span", `chip kindbadge ${kind}`);
    badge.dataset.badge = "kind";
    const icon = el(doc, "ha-icon");
    icon.setAttribute("icon", kind === "ring" ? "mdi:phone-ring" : "mdi:file-document-outline");
    icon.setAttribute("aria-hidden", "true");
    badge.append(icon, el(doc, "span", null, `${PAGE_KIND_TEXT[kind]} page`));
    head.append(badge);
    if (kind === "ring") {
      const t = this._elMap && this._elMap.get(page.talk);
      const rings = el(doc, "span", "sub ringswhen", `Rings when: ${page.talk ? (t ? t.name : this._elMap ? `${page.talk} (removed)` : page.talk) : "no Talk element chosen"}`);
      rings.dataset.hint = "rings-when";
      head.append(rings);
      if (entry) {
        const st = el(doc, "span", "chip", "Edited");
        st.dataset.badge = "ring-state";
        head.append(st);
        if (entry.warn) head.append(this._ringWarn());
        head.append(this._ringAction(entry.talk, "reset", "Reset to default"));
      }
    }
    section.append(head);
    const cols = el(doc, "div", "sectioncols");
    cols.append(this._libDetail(page), this._sectionPreviewColumn(page));
    section.append(cols);
    this._fillPreviewHost(section.querySelector("[data-section-preview]"), page);
    return section;
  }

  /* The preview on the section's right: "Preview as" (a device), the fixed caller of a Ring page, and the frame. */
  _sectionPreviewColumn(page) {
    const doc = this.ownerDocument;
    const state = this._state;
    const col = el(doc, "div", "previewcol");
    col.append(el(doc, "h3", null, "Preview"));
    const rows = rowsOf(state, ["phone", "dashboard"]);
    const view = { ...state, pageId: page.id, previewAs: this._secAs.get(page.id) || state.previewAs };
    if (rows.length) {
      const as = el(doc, "select");
      as.dataset.field = "preview-as";
      as.setAttribute("aria-label", "Preview as");
      for (const r of rows) {
        const o = el(doc, "option", null, `As ${rowName(r)}`);
        o.value = r.id;
        if (previewEndpoint(view) === r.id) o.selected = true;
        as.append(o);
      }
      as.addEventListener("change", () => { this._secAs.set(page.id, as.value); this._scheduleSections(); });
      col.append(as);
    }
    if (pageKind(page) === "ring") {
      const caller = talkCaller(rowsOf(state, ["door"]), this._elMap && this._elMap.get(page.talk)) || UNKNOWN_CALLER;
      const row = allRows(state).find((r) => r.id === caller);
      const line = el(doc, "div", "sub", caller === UNKNOWN_CALLER ? "Call from: unknown (the Talk element stands for nobody Portico knows)" : `Call from: ${row ? rowName(row) : caller}`);
      line.dataset.field = "preview-caller-fixed";
      col.append(line);
    }
    const host = el(doc, "div", "previewhost");
    host.dataset.sectionPreview = "";
    col.append(host);
    return col;
  }

  _scrollToSection(id) {
    const cached = this._sections.get(id);
    if (cached && typeof cached.node.scrollIntoView === "function") cached.node.scrollIntoView({ block: "start" });
  }

  /* Add page: a Content page (a Ring page appears by itself for each Talk element, and is stored when its entry is edited). */
  _addPageForm() {
    const doc = this.ownerDocument;
    const form = this._pageForm;
    const add = el(doc, "div", "form");
    add.dataset.form = "new-library-page";
    add.append(el(doc, "h3", null, "Add a page"));
    const input = el(doc, "input");
    input.value = form.label;
    input.maxLength = MAX_LABEL;
    input.placeholder = "New page name";
    input.dataset.field = "new-page";
    input.setAttribute("aria-label", "New page name");
    input.addEventListener("input", () => { form.label = input.value; });
    add.append(input);
    const go = () => this._edit((s) => {
      const r = addLibraryPage(s.draft, "content", form.label, null);
      commit(s, r.doc);
      s.pageId = r.pageId;
      this._pageForm = { label: "" };
      this._scrollTo = r.pageId;       // the new section comes into view once drawn
    });
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
    add.append(this._btn("+ Add page", go, { act: "add-page" }));
    return add;
  }

  /* ---- the Rings entries of the Pages tab (new form) ---- */

  _ringWarn() {
    const w = el(this.ownerDocument, "span", "sub", "Another Talk element for this door has the page that rings.");
    w.dataset.hint = "ring-duplicate";
    return w;
  }

  /* An Edit / Reset button; the first press opens a confirmation line, the second does it. */
  _ringAction(talk, what, text) {
    return this._btn(text, () => { this._ringAsk = { talk, what }; this._render(); }, { cls: "small", act: `ring-${what}` });
  }

  _ringConfirm(entry, what) {
    const doc = this.ownerDocument;
    const box = el(doc, "div", "msg note");
    box.dataset.state = `ring-${what}-confirm`;
    const page = entry.page;
    const buttons = page ? pageActions(page).length : 0;
    const lost = page && (buttons || page.cameras) ? `${buttons} button${buttons === 1 ? "" : "s"}${page.cameras ? " and its camera choice" : ""}` : "its edits";
    const seed = entry.party === "door" ? ringSeedActions(this._elMap, entry.talk, []).length : 0;
    box.append(el(doc, "span", null, what === "edit"
      ? (entry.party === "door"
        ? `Edit stores a page for ${entry.name} that starts with ${seed} open button${seed === 1 ? "" : "s"} (this door's open buttons and the lock-open buttons from the Elements tab). Every device called by this door then rings with this page: a device whose own pages had none of those buttons will show them.`
        : `Edit stores an empty page for ${entry.name}. Every device it calls then rings with this page instead of the default.`)
      : `Reset to default deletes the page of ${entry.name} (${lost}). The default is computed again; nothing else changes.`), " ");
    const run = () => this._edit((s) => {
      this._ringAsk = null;
      if (what === "edit") { const r = editRingEntry(s.draft, entry.talk, this._elMap); commit(s, r.doc); s.pageId = r.pageId; this._scrollTo = r.pageId; }
      else commit(s, resetRingEntry(s.draft, entry.talk));
    });
    box.append(this._btn(what === "edit" ? "Edit this page" : "Reset to default", run, { cls: "small", act: `ring-${what}-yes` }),
      this._btn("Cancel", () => { this._ringAsk = null; this._render(); }, { cls: "small", act: `ring-${what}-no` }));
    return box;
  }

  /* A Talk element without a stored page: nothing is stored, it rings with the computed default. */
  _defaultRingSection(entry) {
    const doc = this.ownerDocument;
    const section = el(doc, "section", "pagesection ringdefault");
    section.dataset.ringTalk = entry.talk;
    section.dataset.ringState = "default";
    const head = el(doc, "div", "sectionhead");
    head.append(el(doc, "h2", "sectiontitle", entry.name));
    const badge = el(doc, "span", "chip kindbadge ring");
    badge.dataset.badge = "ring-state";
    badge.append(el(doc, "span", null, "Default"));
    head.append(badge, el(doc, "span", "sub", `${{ door: "Door", phone: "Phone", screen: "Screen" }[entry.party]} · ${entry.what}`));
    if (entry.warn) head.append(this._ringWarn());
    section.append(head);
    const body = el(doc, "div", "pagedetail");
    const what = el(doc, "p", "hint", entry.party === "door"
      ? "Rings with Answer and Decline, the door's cameras, and the open buttons of each device's own pages. Nothing is stored."
      : "Rings with Answer and Decline and the caller's cameras. Nothing is stored.");
    what.dataset.hint = "ring-default";
    body.append(what);
    if (this._ringAsk && this._ringAsk.talk === entry.talk && this._ringAsk.what === "edit") body.append(this._ringConfirm(entry, "edit"));
    else body.append(this._ringAction(entry.talk, "edit", "Edit"));
    section.append(body);
    return section;
  }

  /* A stored page whose Talk element is gone or never rings: it never rings (fail closed); Reset removes it. */
  _removedRingSection(entry) {
    const doc = this.ownerDocument;
    const section = el(doc, "section", "pagesection ringremoved");
    section.dataset.ringTalk = entry.talk;
    section.dataset.ringState = "removed";
    section.dataset.page = entry.page.id;
    const head = el(doc, "div", "sectionhead");
    head.append(el(doc, "h2", "sectiontitle", entry.name));
    const badge = el(doc, "span", "chip kindbadge ring");
    badge.dataset.badge = "ring-state";
    badge.append(el(doc, "span", null, "Removed"));
    head.append(badge);
    section.append(head);
    const body = el(doc, "div", "pagedetail");
    body.append(el(doc, "p", "hint", "This Talk element is gone or has nothing to ring for. Its stored page never rings. Reset deletes it."));
    if (this._ringAsk && this._ringAsk.talk === entry.talk && this._ringAsk.what === "reset") body.append(this._ringConfirm(entry, "reset"));
    else body.append(this._ringAction(entry.talk, "reset", "Reset to default"));
    section.append(body);
    return section;
  }

  _libDetail(page) {
    const doc = this.ownerDocument;
    const state = this._state;
    const box = el(doc, "div", "pagedetail");
    const kind = pageKind(page);
    const name = el(doc, "input");
    name.value = page.label;
    name.maxLength = MAX_LABEL;
    name.dataset.field = "page-label";
    name.setAttribute("aria-label", "Page name");
    name.addEventListener("change", () => this._edit((s) => commit(s, renamePage(s.draft, LIBRARY, page.id, name.value))));
    box.append(el(doc, "h3", null, "Page name"), name);
    const entryMode = kind === "ring";
    if (!entryMode) box.append(this._btn("Delete page", () => this._edit((s) => { commit(s, deleteLibraryPage(s.draft, page.id, (id) => this._deviceName(id))); }),
      { cls: "small", act: "delete-page", title: "Delete the page (refused while a device uses it)" }));
    else if (this._ringAsk && this._ringAsk.talk === page.talk && this._ringAsk.what === "reset") {
      const e = ringEntries(state.draft, this._elMap || new Map()).entries.find((x) => x.talk === page.talk);
      box.append(this._ringConfirm(e || { talk: page.talk, name: page.label, page, party: "" }, "reset"));
    }
    const talkName = (this._elMap && this._elMap.get(page.talk) && this._elMap.get(page.talk).name) || page.talk;
    const shown = el(doc, "p", "hint", entryMode ? `Rings on: every device called by ${talkName}` : shownOnText(state.draft, page.id, (id) => this._deviceName(id)));
    shown.dataset.hint = "shown-on";
    box.append(shown);
    if (!entryMode && devicesUsingPage(state.draft, page.id).length) {
      const warn = el(doc, "p", "hint", "Editing this page changes it on every device listed.");
      warn.dataset.hint = "shown-on-warning";
      box.append(warn);
    }
    // Nothing here assigns the page. Which pages a device shows and which Ring page answers which Talk element are chosen on the
    // Devices tab (`_pagesSelector`); a Ring page is the Talk element's and is stored by Edit on its entry.
    if (kind === "ring") box.append(this._ringSettings(page));
    box.append(el(doc, "h3", null, kind === "ring" ? "Buttons" : "Blocks"));
    box.append(el(doc, "p", "hint", kind === "ring"
      ? "This is the page every device called by this Talk element rings with. Answer and Decline are always there; the buttons you add here show with it."
      : "A block groups cameras and buttons under a title. In a block the first camera is shown full width and the rest two to a row; set a camera to Big or Small to change that. A door lets the block's open-door button leave its door empty."));
    const blocks = blocksOf(page);
    if (kind === "content" && tooManyLive(page)) {
      const warn = el(doc, "div", "msg warn", LIVE_WARN_TEXT);
      warn.dataset.state = "too-many-live";
      warn.setAttribute("role", "status");
      box.append(warn);
    }
    if (!blocks.length) box.append(el(doc, "div", "empty", "No blocks on this page."));
    blocks.forEach((block, at) => box.append(this._blockCard(LIBRARY, page, block, at, blocks.length, true)));
    box.append(this._addBlockForm(LIBRARY, page));
    return box;
  }

  /* The Ring page's one camera: the same chooser as a Content page's cameras (From Elements / Define here), limited to one. */
  _ringCamera(page) {
    const doc = this.ownerDocument;
    const box = el(doc, "div", "ringcamera");
    box.dataset.form = "ring-camera";
    box.append(el(doc, "strong", null, "Camera shown with the call"));
    const cams = page.cameras;
    const own = cams === null || cams === undefined;
    const set = (v) => this._edit((s) => commit(s, setRingCamera(s.draft, page.id, v)));
    const cur = el(doc, "div", "msg note");
    cur.dataset.field = "ring-camera";
    cur.dataset.value = own ? "@own" : cams.length === 0 ? "@none" : cams.length === 1 ? cams[0] : "@keep";
    if (own) cur.append("The caller's own cameras (default): the call shows the cameras of whoever is ringing.");
    else if (!cams.length) cur.append("No camera.");
    else if (cams.length > 1) cur.append(`${cams.length} cameras kept from the old rule. Choose one below to replace them.`);
    else { const c = this._cameraEntry(cams[0]); cur.append(`Camera: ${c.title}${c.sub ? ` (${c.sub})` : ""}${c.removed ? " (removed)" : ""}`); }
    box.append(cur);
    const acts = el(doc, "div", "row");
    if (!own) acts.append(this._btn("Use the caller's own cameras", () => set(null), { cls: "small", act: "ring-camera-own" }));
    if (own || cams.length) acts.append(this._btn("No camera", () => set(""), { cls: "small", act: "ring-camera-none" }));
    box.append(acts);
    box.append(this._cameraPicker("ring-camera-pick", (entry) => set(entry), []));
    return box;
  }

  /* A Ring page's own settings: the Talk element (exactly one, required) and the one camera (or the caller's own). */
  _ringSettings(page) {
    const doc = this.ownerDocument;
    const box = el(doc, "div", "form");
    box.dataset.form = "ring-settings";
    const known = !!this._elMap;
    const named = known && this._elMap.get(page.talk);
    const fixed = el(doc, "p", "hint", `Talk element: ${named ? `${named.name} · ${elementWhat(named)}` : page.talk || "none"} (fixed: this is its page)`);
    fixed.dataset.field = "ring-talk";
    box.append(fixed);
    if (page.talk && known && (!named || named.target_removed === true)) {
      const gone = el(doc, "div", "msg err", "Talk element removed. This page never rings.");
      gone.dataset.state = "talk-removed";
      box.append(gone);
    }
    box.append(this._ringCamera(page));
    return box;
  }

  /* ---- the migration to the unified model (administrators; a schema 4/5 document only) ---- */

  _migrationBanner() {
    if (!this._isAdmin || !this._state || isPagesModel(this._state.draft)) return null;
    const doc = this.ownerDocument;
    const box = el(doc, "div", "msg note");
    box.dataset.state = "migration-banner";
    box.append(el(doc, "span", null, MIGRATION_BANNER_TEXT), " ");
    box.append(this._btn("Check migration", () => this._checkMigration(), { act: "check-migration", disabled: !!this._mig && (this._mig.phase === "checking" || this._mig.phase === "applying") }));
    return box;
  }

  /* The dry run: nothing is written (validate_only true). */
  async _checkMigration() {
    if (!this._isAdmin || !this._hass) return;
    this._mig = { phase: "checking" };
    this._render();
    let next;
    try {
      const report = await this._hass.callWS({ type: "portico/content/pages_migrate", validate_only: true });
      next = { phase: "report", report };
    } catch (err) {
      next = { phase: "error", message: classifyError(err).message };
    }
    this._mig = next;
    this._render();
  }

  /* The apply. Only ever called from the confirm page's button, with the doc_rev the dry run returned. */
  async _applyMigration() {
    const mig = this._mig;
    if (!mig || mig.phase !== "confirm" || !mig.report || mig.report.can_apply !== true || (this._state && isDirty(this._state))) return;
    this._mig = { phase: "applying", report: mig.report };
    this._render();
    try {
      await this._hass.callWS({ type: "portico/content/pages_migrate", validate_only: false, base_doc_rev: mig.report.doc_rev });
    } catch (err) {
      const c = classifyError(err);
      this._mig = { phase: "error", message: c.kind === "conflict" ? "The saved content changed since the check. Nothing was migrated; run the check again." : c.message };
      this._render();
      return;
    }
    this._mig = null;
    const ok = await this._load();
    if (ok) { this._msg = { kind: "ok", text: "Migrated. Pages and Ring & call are now one Pages tab; assign pages on the Devices tab." }; this._render(); }
  }

  _migrationDialog() {
    const doc = this.ownerDocument;
    const mig = this._mig;
    const overlay = el(doc, "div", "overlay");
    const box = el(doc, "div", "modal");
    box.setAttribute("role", "dialog");
    box.setAttribute("aria-modal", "true");
    box.setAttribute("aria-label", "Migration to the unified Pages model");
    box.dataset.dialog = "migration";
    box.dataset.phase = mig.phase;
    overlay.append(box);
    const close = () => { this._mig = null; this._render(); };
    box.append(el(doc, "h2", null, "Migration to unified Pages"));
    if (mig.phase === "checking" || mig.phase === "applying") {
      box.append(el(doc, "p", null, mig.phase === "checking" ? "Checking... nothing is changed by this." : "Migrating..."));
      return overlay;
    }
    if (mig.phase === "error") {
      const msg = el(doc, "div", "msg err", mig.message);
      msg.dataset.state = "migration-error";
      box.append(msg, this._btn("Close", close, { act: "migration-close" }));
      return overlay;
    }
    const report = mig.report;
    const nameOf = (id) => this._deviceName(id);
    const counts = report.counts || {};
    if (mig.phase === "confirm") {
      box.append(el(doc, "p", null, "Apply the migration? This changes how every phone and screen gets its pages and rings. It cannot be undone from the panel."));
      const list = el(doc, "ul");
      list.dataset.list = "will-change";
      const line = (text) => list.append(el(doc, "li", null, text));
      line(`The saved content becomes schema ${report.schema && report.schema.to}${report.schema && report.schema.from ? ` (from ${report.schema.from})` : ""}: one library of ${plural(counts.library_pages || 0, "page", "pages")}, and each of ${plural(counts.endpoints || 0, "phone or screen", "phones and screens")} keeps its own list.`);
      line(`${plural(counts.ring_pages_new || 0, "Ring page is", "Ring pages are")} created from the ring rules, with ${plural(counts.ring_assignments || 0, "assignment", "assignments")}; the rules are removed.`);
      if (counts.talk_elements_to_create) line(`${plural(counts.talk_elements_to_create, "Talk element is", "Talk elements are")} created: ${(report.talk_elements_to_create || []).map((t) => t.name || t.id).join(", ")}.`);
      if (counts.dropped_dangling_buttons) line(`${plural(counts.dropped_dangling_buttons, "button that no longer exists is", "buttons that no longer exist are")} left out, as today.`);
      if (report.safety_copy) line(`A copy of the saved content is kept first as ${report.safety_copy}.`);
      line(`Every phone and screen was checked to get the same pages and rings as now (${counts.endpoints_equal} of ${counts.endpoints}).`);
      line("The Ring & call tab goes away; the Pages tab holds both kinds of page.");
      box.append(list);
      const acts = el(doc, "div", "acts");
      acts.append(this._btn("Back", () => { this._mig = { phase: "report", report }; this._render(); }, { act: "migration-back" }));
      acts.append(this._btn("Yes, apply the migration", () => this._applyMigration(), { cls: "primary", act: "migration-confirm", disabled: report.can_apply !== true || isDirty(this._state) }));
      box.append(acts);
      return overlay;
    }
    // phase "report": the dry-run report
    const head = el(doc, "p", report.ok ? "msg ok" : "msg err", report.can_apply ? "The check passed: every phone and screen gets the same result after the migration." : report.ok ? "The check passed, but the migration cannot be applied yet." : "The check found problems. The migration cannot be applied.");
    head.dataset.state = report.can_apply ? "can-apply" : "cannot-apply";
    box.append(head);
    const table = el(doc, "table", "migcounts");
    const COUNT_TEXT = [["endpoints", "Phones and screens"], ["library_pages", "Pages in the library"], ["content_pages", "Content pages"], ["ring_pages_new", "Ring pages created"],
      ["ring_assignments", "Ring assignments"], ["rules_unused", "Rules that never apply"], ["talk_elements_to_create", "Talk elements to create"],
      ["dropped_dangling_buttons", "Buttons dropped (no longer exist)"], ["endpoints_equal", "Devices with the same result"], ["endpoints_unequal", "Devices with a different result"]];
    for (const [key, text] of COUNT_TEXT) {
      if (!(key in counts)) continue;
      const tr = el(doc, "tr");
      tr.dataset.count = key;
      tr.append(el(doc, "td", null, text), el(doc, "td", null, String(counts[key])));
      table.append(tr);
    }
    const byKind = counts.rules_converted_by_caller_kind || {};
    if (Object.keys(byKind).length) {
      const tr = el(doc, "tr");
      tr.dataset.count = "rules_converted_by_caller_kind";
      tr.append(el(doc, "td", null, "Rules converted, by caller"), el(doc, "td", null, Object.entries(byKind).map(([k, n]) => `${k}: ${n}`).join(", ")));
      table.append(tr);
    }
    box.append(table);
    const section = (title, items, render) => {
      if (!items || !items.length) return;
      box.append(el(doc, "h3", "section", title));
      const ul = el(doc, "ul");
      for (const item of items) ul.append(el(doc, "li", null, render(item)));
      box.append(ul);
    };
    section("Ring pages", report.ring_pages, (p) => `${p.label} (${p.talk}): ${new Set([...(p.ring_buttons || []), ...(p.call_buttons || [])]).size} buttons; for ${(p.assigned_to || []).map(nameOf).join(", ") || "nobody"}`);
    section("Talk elements to create", report.talk_elements_to_create, (t) => `${t.name || t.id} (${t.kind || "talk"})`);
    section("Devices", report.per_endpoint, (r) => `${nameOf(r.endpoint)}: ${r.equal ? "same result" : `DIFFERENT (${(r.differences || []).join(", ")})`}`);
    section("Left out", report.dropped, (d) => `${d.id || d.button || JSON.stringify(d)}${d.scope ? ` (${d.scope})` : ""}`);
    section("Rules that never apply", report.unused_rules, (u) => `${u.rule} of ${u.scope}: ${u.why}`);
    section("Problems", report.problems, (p) => p);
    if (report.blocked_by && report.blocked_by.length) section("Blocked by", report.blocked_by, (p) => p);
    if (isDirty(this._state)) box.append(el(doc, "div", "msg warn", "You have unsaved changes. Save or discard them before applying: the migration works on the saved content."));
    const acts = el(doc, "div", "acts");
    acts.append(this._btn("Close", close, { act: "migration-close" }));
    acts.append(this._btn("Apply migration", () => { this._mig = { phase: "confirm", report }; this._render(); },
      { cls: "primary", act: "migration-apply", disabled: report.can_apply !== true || isDirty(this._state) }));
    box.append(acts);
    return overlay;
  }

  /* ---- the Ring & call tab (K3): the rules that say what a ring shows per caller ---- */

  /* Is the preview the ring screen? The Ring & call tab (schema 4/5), or a Ring page open on the Pages tab (schema 6). */
  _ringPreview() {
    if (this._tab === "calls") return true;
    const state = this._state;
    if (this._tab !== "pages" || !state || !isPagesModel(state.draft)) return false;
    const page = pagesOf(state.draft, LIBRARY).find((p) => p.id === state.pageId);
    return !!page && pageKind(page) === "ring";
  }

  _ringCaller() {
    const state = this._state;
    if (isPagesModel(state.draft)) {          // the party the page's Talk element stands for calls
      const page = pagesOf(state.draft, LIBRARY).find((p) => p.id === state.pageId);
      const caller = page && talkCaller(rowsOf(state, ["door"]), this._elMap && this._elMap.get(page.talk));
      return caller || UNKNOWN_CALLER;
    }
    if (state.previewCaller === UNKNOWN_CALLER) return UNKNOWN_CALLER;
    const doors = rowsOf(state, ["door"]);
    if (state.previewCaller && doors.some((r) => r.id === state.previewCaller)) return state.previewCaller;
    return doors.length ? doors[0].id : UNKNOWN_CALLER;
  }

  _callsWork() {
    const doc = this.ownerDocument;
    const state = this._state;
    const work = el(doc, "div", "work");
    if (state.sel.startsWith("orphan:")) {
      work.append(el(doc, "p", "hint", "Pick the household template, a phone or a screen on the left."));
      return work;
    }
    const target = selTarget(state.sel);
    const rows = rowsOf(state, ["phone", "dashboard"]);
    const name = target === "template" ? "Household template" : rowName(rows.find((r) => r.id === target) || { id: target });
    work.append(el(doc, "h3", null, `Ring & call rules of: ${name}`));
    work.append(el(doc, "p", "hint", target === "template"
      ? "Household rules apply to every phone and screen that has no rule of its own for the caller."
      : "This phone's or screen's own rules come before the household's."));
    const order = el(doc, "p", "hint", RULE_ORDER_HINT);
    order.dataset.hint = "rule-order";
    work.append(order);

    const rules = rulesOf(state.draft, target);
    if (!rules.some((r) => r.id === state.ruleId)) state.ruleId = rules.length ? rules[0].id : null;
    const cols = el(doc, "div", "cols");
    const list = el(doc, "div", "rulelist");
    list.append(el(doc, "h3", null, "Rules"));
    if (!rules.length) list.append(el(doc, "div", "empty", "No rules: a ring shows the caller's own cameras and, for a door, its open buttons."));
    const upd = (fn) => this._edit((s) => commit(s, fn(s.draft)));
    rules.forEach((rule, at) => {
      const row = el(doc, "div", "row" + (rule.id === state.ruleId ? " cur" : ""));
      row.dataset.rule = rule.id;
      const txt = el(doc, "div", "txt");
      txt.append(this._btn(ruleCallerLabel(state, rule.caller), () => { state.ruleId = rule.id; this._render(); }, { cls: "small", act: "pick-rule" }));
      row.append(txt);
      row.append(this._btn("▲", () => upd((d) => moveRule(d, target, rule.id, -1)), { cls: "small", disabled: at === 0, act: "rule-up", title: "Move up" }));
      row.append(this._btn("▼", () => upd((d) => moveRule(d, target, rule.id, 1)), { cls: "small", disabled: at === rules.length - 1, act: "rule-down", title: "Move down" }));
      row.append(this._btn("×", () => this._edit((s) => { commit(s, deleteRule(s.draft, target, rule.id)); s.ruleId = null; }), { cls: "small", act: "delete-rule", title: "Delete the rule" }));
      list.append(row);
    });
    list.append(this._addRuleForm(target));
    cols.append(list);
    cols.append(this._ruleDetail(target));
    work.append(cols);
    return work;
  }

  _addRuleForm(target) {
    const doc = this.ownerDocument;
    const state = this._state;
    const form = el(doc, "div", "form");
    form.dataset.form = "new-rule";
    const choices = ruleCallerChoices(state, target);
    if (!choices.length) {
      form.append(el(doc, "div", "empty", "Every caller already has a rule here."));
      return form;
    }
    const lab = el(doc, "label", null, "A rule for");
    const select = el(doc, "select");
    select.dataset.field = "new-rule-caller";
    for (const c of choices) {
      const o = el(doc, "option", null, c.label);
      o.value = c.value;
      select.append(o);
    }
    lab.append(select);
    form.append(lab);
    const go = () => this._edit((s) => {
      const choice = choices.find((c) => c.value === select.value);
      const r = addRule(s.draft, target, { caller: select.value, label: choice ? choice.label.replace(/^Door: /, "") : "" });
      commit(s, r.doc);
      s.ruleId = r.ruleId;
    });
    form.append(this._btn("+ Add rule", go, { act: "add-rule" }));
    return form;
  }

  _ruleDetail(target) {
    const doc = this.ownerDocument;
    const state = this._state;
    const box = el(doc, "div", "ruledetail");
    const rule = rulesOf(state.draft, target).find((r) => r.id === state.ruleId);
    if (!rule) {
      box.append(el(doc, "div", "empty", "Add a rule to start."));
      return box;
    }
    const upd = (patch) => this._edit((s) => commit(s, updateRule(s.draft, target, rule.id, patch)));
    box.append(el(doc, "h3", null, `Rule for: ${ruleCallerLabel(state, rule.caller)}`));

    box.append(el(doc, "h3", null, "Cameras shown"));
    const mode = el(doc, "select");
    mode.dataset.field = "rule-cameras-mode";
    for (const [value, text] of [["own", "The caller's own cameras"], ["these", "Only these cameras"]]) {
      const o = el(doc, "option", null, text);
      o.value = value;
      if ((value === "own") === (rule.cameras === null || rule.cameras === undefined)) o.selected = true;
      mode.append(o);
    }
    mode.addEventListener("change", () => upd({ cameras: mode.value === "own" ? null : [] }));
    box.append(mode);
    if (rule.cameras !== null && rule.cameras !== undefined) {
      if (!rule.cameras.length) box.append(el(doc, "div", "empty", "No cameras: the ring shows none."));
      for (const entity of rule.cameras) {
        box.append(this._cameraEntryRow(entity, () => upd({ cameras: rule.cameras.filter((c) => c !== entity) }), "remove-rule-camera"));
      }
      box.append(this._cameraPicker("rule-camera-add", (entity) => upd({ cameras: [...rule.cameras, entity] }), rule.cameras));
    }

    const choices = ruleButtonChoices(state.draft, target, this._elMap);
    for (const [field, title, hint] of [
      ["ring_buttons", "Ring buttons", "Shown on the ring screen while it rings."],
      ["call_buttons", "Call buttons", "Shown during the call."],
    ]) {
      box.append(el(doc, "h3", null, title));
      box.append(el(doc, "p", "hint", `${hint} Up to ${MAX_RULE_BUTTONS}.`));
      const checks = el(doc, "div", "checks");
      checks.dataset.list = field;
      const have = rule[field] || [];
      const all = [...choices, ...have.filter((id) => !choices.some((c) => c.id === id)).map((id) => ({ id, label: id, page: "missing" }))];
      if (!all.length) checks.append(el(doc, "div", "empty", "This page set has no buttons yet."));
      for (const c of all) {
        const lab = el(doc, "label");
        const box2 = el(doc, "input");
        box2.type = "checkbox";
        box2.checked = have.includes(c.id);
        box2.dataset.button = c.id;
        box2.addEventListener("change", () => upd({ [field]: toggleId(have, c.id) }));
        lab.append(box2, el(doc, "span", null, `${c.label} (${c.page})`));
        checks.append(lab);
      }
      box.append(checks);
    }
    return box;
  }

  /* ---- "Not attached" ---- */

  _orphanWork(work, key) {
    const doc = this.ownerDocument;
    const state = this._state;
    const orphan = (state.draft.overrides || {})[key] || {};
    work.append(el(doc, "h3", null, "Not attached"));
    work.append(el(doc, "p", "hint",
      "These pages belong to a phone or screen that no longer exists (a phone that was enrolled again gets a new id). " +
      "Move them to a phone, into the household template, or delete them. Nothing is lost until you Save."));
    work.append(el(doc, "div", "sub", key));
    const pages = orphan.pages || [];
    if (!pages.length) work.append(el(doc, "div", "empty", "No pages."));
    for (const p of pages) {
      work.append(el(doc, "h3", "section", `${p.label || p.id} · ${plural(pageCameras(p).length, "camera", "cameras")}, ${plural(pageActions(p).length, "button", "buttons")}`));
      const list = el(doc, "div");
      for (const c of pageCameras(p)) list.append(el(doc, "div", "sub", `Camera: ${cameraName(c, this._hass && this._hass.states, this._elMap)} (${isRef(c) ? `element ${c.element}${(() => { const m = this._elMap && this._elMap.get(c.element); return m && smallOf(m) ? ` (${streamsText(m)})` : ""; })()}` : c.entity_id})`));
      for (const a of pageActions(p)) list.append(el(doc, "div", "sub", `Button: ${a.label} (${KIND_TEXT[a.kind] || a.kind})`));
      work.append(list);
    }
    const form = el(doc, "div", "form");
    const to = el(doc, "select");
    to.dataset.field = "move-to";
    const t = el(doc, "option", null, "Into the household template");
    t.value = "template";
    to.append(t);
    for (const r of rowsOf(state, ["phone", "dashboard"])) {
      const o = el(doc, "option", null, `Move to ${rowName(r)}`);
      o.value = r.id;
      to.append(o);
    }
    form.append(to);
    const acts = el(doc, "div", "acts");
    acts.append(this._btn("Move", () => this._edit((s) => { moveOrphan(s, key, to.value); s.sel = to.value === "template" ? "template" : `ep:${to.value}`; s.pageId = null; }), { act: "orphan-move" }));
    acts.append(this._btn("Delete", () => this._edit((s) => { deleteOrphan(s, key); s.sel = "template"; s.pageId = null; }), { act: "orphan-delete" }));
    form.append(acts);
    work.append(form);
    return work;
  }

  /* ---- the preview column ---- */

  _previewColumn() {
    const doc = this.ownerDocument;
    const state = this._state;
    const col = el(doc, "div", "previewcol");
    col.append(el(doc, "h3", null, "Preview"));
    const rows = rowsOf(state, ["phone", "dashboard"]);
    const target = selTarget(state.sel);
    if ((target === "template" || isPagesModel(state.draft)) && rows.length) {
      const as = el(doc, "select");
      as.dataset.field = "preview-as";
      as.setAttribute("aria-label", "Preview as");
      for (const r of rows) {
        const o = el(doc, "option", null, `As ${rowName(r)}`);
        o.value = r.id;
        if (previewEndpoint(state) === r.id) o.selected = true;
        as.append(o);
      }
      as.addEventListener("change", () => { state.previewAs = as.value; this._schedulePreview(); });
      col.append(as);
    }
    if (this._ringPreview() && isPagesModel(state.draft)) {
      const caller = this._ringCaller();
      const row = allRows(state).find((r) => r.id === caller);
      const line = el(doc, "div", "sub", caller === UNKNOWN_CALLER ? "Call from: unknown (the Talk element stands for nobody Portico knows)" : `Call from: ${row ? rowName(row) : caller}`);
      line.dataset.field = "preview-caller-fixed";
      col.append(line);
    } else if (this._tab === "calls") {
      const who = el(doc, "select");
      who.dataset.field = "preview-caller";
      who.setAttribute("aria-label", "Ring screen for a call from");
      const current = this._ringCaller();
      for (const r of rowsOf(state, ["door"])) {
        const o = el(doc, "option", null, `Call from: ${rowName(r)}`);
        o.value = r.id;
        if (current === r.id) o.selected = true;
        who.append(o);
      }
      const unknown = el(doc, "option", null, "Call from: an unknown caller (*)");
      unknown.value = UNKNOWN_CALLER;
      if (current === UNKNOWN_CALLER) unknown.selected = true;
      who.append(unknown);
      who.addEventListener("change", () => { state.previewCaller = who.value; this._schedulePreview(); });
      col.append(who);
    }
    const host = el(doc, "div", "previewhost");
    host.dataset.preview = "";
    col.append(host);
    return col;
  }

  _renderPreview() {
    const host = this._root.querySelector("[data-preview]");
    if (!host) return;
    const doc = this.ownerDocument;
    host.replaceChildren();
    if (!this._preview) { host.append(el(doc, "div", "empty", "Preview loads after a moment...")); return; }
    if (!this._preview.ok) { host.append(el(doc, "div", "empty", this._preview.message)); return; }
    const result = this._preview.result;
    if (this._ringPreview()) { host.append(this._ringFrame(result)); return; }
    const frame = el(doc, "div", "frame");
    frame.append(el(doc, "div", "title", result.home_name || "Home"));
    const pages = result.pages || [];
    const state = this._state;
    let shown = pages.find((p) => p.id === this._previewPage) || pages.find((p) => p.id === (state && state.pageId)) || pages[0];
    if (pages.length > 1) {
      const tabs = el(doc, "div", "pagetabs");
      for (const p of pages) {
        const b = this._btn(p.label, () => { this._previewPage = p.id; this._renderPreview(); }, { cls: "small" + (p === shown ? " sel" : ""), act: "preview-page" });
        tabs.append(b);
      }
      frame.append(tabs);
    }
    if (shown) frame.append(drawPage(doc, shown, (id) => this._pictureOf(id), this._stamp, this._dark));
    else frame.append(el(doc, "div", "empty", "No pages."));
    host.append(frame);
  }

  /* The ring screen for the chosen caller, from the server's resolved `callers` entry of the DRAFT: who is
   * calling, the cameras it would show (named; the real screen draws their stills), its buttons (one list). */
  _ringFrame(result, caller = this._ringCaller()) {
    const doc = this.ownerDocument;
    const frame = el(doc, "div", "frame");
    frame.dataset.ring = "";
    const entry = (result.callers || {})[caller];
    frame.append(el(doc, "div", "title", entry && entry.name ? `${entry.name} is calling` : "An unknown caller"));
    if (!entry) {
      frame.append(el(doc, "div", "empty", "The server does not know this caller yet."));
      return frame;
    }
    // one list: a Ring page's buttons, both audiences merged by id (ring first), as the page's blocks order them
    const seen = new Set();
    const buttons = [...(entry.ring_buttons || []), ...(entry.call_buttons || [])].filter((a) => {
      const id = a && a.id;
      if (id === undefined || id === null) return true;
      if (seen.has(id)) return false;
      seen.add(id);
      return true;
    });
    if (!entry.name && !(entry.cameras || []).length && !buttons.length) {
      frame.append(el(doc, "div", "empty", "A caller Portico cannot place shows nothing."));
      return frame;
    }
    frame.append(el(doc, "h3", "section", "Cameras"));
    const cams = entry.cameras || [];
    if (!cams.length) frame.append(el(doc, "div", "empty", "No cameras."));
    else {
      const grid = el(doc, "div", "grid cams");
      for (const cam of cams) grid.append(cameraTile(doc, { ...cam, entity_id: cam.name || "" }, null, this._stamp));
      frame.append(grid);
      frame.append(el(doc, "div", "sub", "The ring screen draws a still of each, refreshed while it rings."));
    }
    frame.append(el(doc, "h3", "section", "Buttons"));
    if (!buttons.length) frame.append(el(doc, "div", "empty", "None."));
    else {
      const grid = el(doc, "div", "grid btns");
      for (const a of buttons) grid.append(actionTile(doc, a, this._dark));
      frame.append(grid);
    }
    frame.append(this._fixedCallButtons());
    return frame;
  }

  /* The two buttons every ring has, as the app's call row draws them: inert here, not part of the page, not editable. */
  _fixedCallButtons() {
    const doc = this.ownerDocument;
    const row = el(doc, "div", "fixedcall");
    row.dataset.fixed = "call-buttons";
    for (const [kind, text] of [["answer", "Answer"], ["decline", "Decline"]]) {
      const b = el(doc, "span", `fixedbtn ${kind}`, text);
      b.dataset.fixed = kind;
      b.setAttribute("role", "button");
      b.setAttribute("aria-disabled", "true");
      row.append(b);
    }
    return row;
  }

  /* ---- the read-only view for a user who is not an administrator ---- */

  _renderReadonly(page) {
    const doc = this.ownerDocument;
    page.append(el(doc, "h1", null, "Portico"));
    page.append(el(doc, "p", "hint", "What your phone or screen shows besides a call. Only an administrator can change it."));
    const own = this._own || {};
    const pages = own.pages || [{ id: "home", label: "Home", cameras: own.cameras || [], actions: own.actions || [] }];
    for (const p of pages) {
      page.append(el(doc, "h3", "section", p.label));
      page.append(drawPage(doc, p, (id) => this._pictureOf(id), this._stamp, this._dark));
    }
  }
}

function newDialog(list, state, blockId) {
  const kind = "lock_open";
  return { list, mode: "add", kind, filter: "", target: "", element: "", source: null, label: "", labelTouched: false, icon: "none",
           confirm: KINDS[kind].confirm, error: "", tileId: null, entity: "", blockId: blockId || null,
           color: "default", show_label: true, size: "auto", live: false };
}

function editDialog(list, tile, blockId, elements) {
  const element = isRef(tile) ? tile.element : "";
  const named = element && elements && elements.get(element);
  return { list, mode: "edit", kind: tile.kind || "", filter: "", target: "", element, source: null,
           label: isRef(tile) ? ("label" in tile ? tile.label : named ? named.name : "") : tile.label, labelTouched: true,
           icon: tile.icon || "none", confirm: isRef(tile) ? tile.confirm === true : !!tile.confirm, error: "", tileId: tile.id, blockId: blockId || null,
           color: tile.color || "default", show_label: tile.show_label !== false, size: tile.size || "auto", live: tile.live === true,
           entity: tile.entity_id || tile.device_id || (tile.target && tile.target.id) || "" };
}

function validSelOf(state) {
  return validSel(state, state.sel);
}

/* A long id for the sidebar: the first 8 characters. */
function shortKey(key) {
  return key.length > 12 ? `${key.slice(0, 8)}...` : key;
}

PorticoPanel.logic = logic;

if (!customElements.get("portico-panel")) customElements.define("portico-panel", PorticoPanel);
