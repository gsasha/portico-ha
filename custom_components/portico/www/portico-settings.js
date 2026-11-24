/* Portico panel: the Settings tab (UI split P4, P5, P6; design 5.2).
 *
 * A SEPARATE module, like the Phones tab: the panel imports it lazily and, when the import fails, simply has no Settings tab,
 * so a bad edit here cannot blank the content tabs. It exports `SettingsController` (what the panel mounts) and the pure
 * helpers the node tests reach (`statusRows`, `uiPatch`, `uiConsequenceLines`...).
 *
 * The controller owns ONE persistent element (`controller.el`): the panel's `_renderNow()` calls `root.replaceChildren()` on
 * every tab click and theme flip, so the panel re-attaches this element each time and the draft survives.
 *
 * Wire (all admin: the panel is open to every household login, so the server's command class is the access control; the server
 * checks `confirm` itself, whatever this page did):
 *   portico/door_status {}                 -> {hub, doors: [{entry_id, name, driver, connected, call_state, ...}], as_of}   (P4, read only)
 *   portico/ui_settings/get {}             -> {ring_enabled, ring_window, new_phones_enabled, test_phones, phones, ...}      (P6)
 *   portico/ui_settings/set {patch, confirm?, validate_only?}
 *                                          -> {result, changes, settings}
 *
 * NEVER here: a peer token, a password, a key or a URL. The page cannot send one (the server refuses any key outside its list).
 * Door settings are not edited here any more (the gate observer's switches went with the observer, DEV-518 C5; a SIP-server door
 * keeps its own in the Home Assistant Reconfigure dialog). Nothing is switched on by this page by itself: a value changes when the administrator
 * presses Apply on the confirmation page, which is the only place `confirm: true` is sent.
 *
 * All text from the server (a door's name, a phone's name, a driver's reason) goes in through textContent, never
 * as markup (a test greps this file for every HTML-string setter).
 */

export const DRIVER_TEXT = { dahua_sip: "Dahua SIP", unifi_protect: "UniFi Protect" };

export const ERROR_TEXT = {
  bad_patch: "Nothing valid to change. Reload the page and try again.",
  confirmation_required: "That change has to be confirmed. Reload the page and try again.",
  no_push_entry: "Portico's settings entry is missing, so these settings have nowhere to be kept. Add Portico under Settings > Devices & services.",
  push_not_loaded: "The push ring notification is not running, so the phones it knows cannot be listed. Fix its credentials in the Home Assistant dialog first.",
  unknown_phone: "One of the ticked phones is not registered for the push ring notification any more. Reload the page.",
  unknown: "Something went wrong on the server while doing that. The Home Assistant log has the details.",
};

/* ---- pure helpers ------------------------------------------------------------------------------------------------ */

export function yesNo(value) {
  return value === true ? "Yes" : value === false ? "No" : "Not known";
}

export function driverText(driver) {
  return DRIVER_TEXT[driver] || (typeof driver === "string" && driver ? driver : "unknown driver");
}

/* The lines of a door's status card as [label, text] pairs. `door` is one entry of door_status. Read only. */
export function statusRows(door) {
  const rows = [];
  rows.push(["Driver", driverText(door.driver)]);
  rows.push(["Home Assistant entry", door.loaded ? "Loaded" : `Not running (${String(door.entry_state || "unknown").replace(/_/g, " ")})`]);
  rows.push(["Connection", door.connected ? "Connected" : "Not connected"]);
  if (door.loaded) {
    rows.push(["Hub link", yesNo(door.transport_available)]);
    rows.push(["Door reachable", door.driver_available === null || door.driver_available === undefined ? "Not reported" : yesNo(door.driver_available)]);
    rows.push(["Call state", door.call_state || "Not known"]);
    rows.push(["Talk (outbound) state", door.outbound_state || "Not known"]);
    rows.push(["Can place a call", yesNo(door.can_dial)]);
  }
  return rows;
}

export function hubText(hub) {
  if (!hub) return "Not known";
  const bits = [`process ${hub.process || "unknown"}`];
  if (hub.wheel_version) bits.push(`wheel ${hub.wheel_version}`);
  if (Number.isFinite(hub.doors_attached)) bits.push(hub.doors_attached === 1 ? "1 door attached" : `${hub.doors_attached} doors attached`);
  if (hub.restarted_once) bits.push("restarted once since it started");
  return bits.join(", ");
}

/* The patch for the push ring notification card against the server's values. */
export function uiPatch(ui, draft) {
  const patch = {};
  if (draft.ring_enabled !== ui.ring_enabled) patch.ring_enabled = draft.ring_enabled;
  if (draft.ring_window !== ui.ring_window) patch.ring_window = draft.ring_window;
  if (draft.new_phones_enabled !== ui.new_phones_enabled) patch.new_phones_enabled = draft.new_phones_enabled;
  const was = (ui.test_phones || []).slice().sort();
  const now = Array.from(draft.test_phones).sort();
  if (was.length !== now.length || was.some((id, i) => id !== now[i])) patch.test_phones = Array.from(draft.test_phones);
  const base = String(draft.live_rtsp_base || "").trim();
  if (base !== (ui.live_rtsp_base || "")) patch.live_rtsp_base = base;
  const subsWas = (ui.live_sub_streams || []).slice().sort();
  const subsNow = liveSubNames(draft.live_sub_streams).sort();
  if (subsWas.length !== subsNow.length || subsWas.some((n, i) => n !== subsNow[i])) patch.live_sub_streams = liveSubNames(draft.live_sub_streams);
  const awayWas = ui.live_away_streams || {};
  const awayNow = liveAwayParse(draft.live_away_streams).map;
  if (JSON.stringify(sortedAway(awayWas)) !== JSON.stringify(sortedAway(awayNow))) patch.live_away_streams = awayNow;
  return patch;
}

function sortedAway(map) {
  return Object.keys(map).sort().map((k) => [k, map[k].main, map[k].sub || ""]);
}

/* DEV-715: the per-camera away addresses typed as `camera.porch rtsp://main [rtsp://sub]`, entries separated by spaces, commas or
   new lines (a token starting with `camera.` begins an entry). Returns {map, problem}; the server checks again. */
const AWAY_URL = /^rtsp:\/\/[A-Za-z0-9.-]+(:[0-9]{1,5})?(\/[A-Za-z0-9._~%!$&'()*+,;=:@\/-]*)?$/i;
export function liveAwayParse(text) {
  const tokens = String(text || "").split(/[\s,]+/).filter((t) => t);
  const map = {};
  let cur = null;
  const bad = "Per-camera addresses look like camera.porch rtsp://10.6.0.9:554/main rtsp://10.6.0.9:554/sub : a camera entity, a full rtsp address, an optional second one for the sub stream, no user name, password or query.";
  for (const t of tokens) {
    if (/^camera\.[a-z0-9_]{1,100}$/.test(t)) {
      if (map[t]) return { map: {}, problem: `${t} is listed twice.` };
      cur = map[t] = {};
    } else if (cur && t.length <= 256 && AWAY_URL.test(t) && !cur.sub) {
      if (cur.main) cur.sub = t; else cur.main = t;
    } else return { map: {}, problem: bad };
  }
  if (Object.values(map).some((v) => !v.main) || Object.keys(map).length > 64) return { map: {}, problem: bad };
  return { map, problem: "" };
}

export function liveAwayText(map) {
  return Object.keys(map || {}).sort().map((k) => [k, map[k].main, map[k].sub].filter((x) => x).join(" ")).join(", ");
}

/* What is wrong with the typed address, or "" (empty is fine: it turns the feature off). The server checks again. */
export function liveBaseProblem(text) {
  const t = String(text || "").trim();
  if (!t) return "";
  if (t.length > 128 || !/^rtsp:\/\/[A-Za-z0-9.-]+(:[0-9]{1,5})?\/?$/i.test(t)) {
    return "The address has to look like rtsp://10.6.0.6:8554 : rtsp, a host and an optional port, with no user name, password or path.";
  }
  return "";
}

export function liveSubProblem(text) {
  const names = liveSubNames(text);
  if (names.length > 64 || names.some((n) => !/^[A-Za-z0-9_-]{1,64}$/.test(n))) {
    return "Camera names use letters, digits, _ and - only (at most 64 names).";
  }
  return "";
}

/* The sub-stream names typed in the Settings tab: separated by commas, spaces or new lines, each once. */
export function liveSubNames(text) {
  return Array.from(new Set(String(text || "").split(/[\s,]+/).filter((n) => n)));
}

export function uiDraftOf(ui) {
  return { ring_enabled: ui.ring_enabled === true, ring_window: ui.ring_window, new_phones_enabled: ui.new_phones_enabled === true,
    test_phones: new Set(ui.test_phones || []), live_rtsp_base: ui.live_rtsp_base || "", live_sub_streams: (ui.live_sub_streams || []).join(", "),
    live_away_streams: liveAwayText(ui.live_away_streams) };
}

export function uiConsequenceLines(patch, ui) {
  const out = [];
  const has = (k) => Object.prototype.hasOwnProperty.call(patch, k);
  if (has("ring_enabled")) {
    out.push(patch.ring_enabled
      ? "Push ring notification ON: when a visitor rings, phones with their own push ring notification switched on are notified."
      : "Push ring notification OFF: no phone is notified of a visitor ring while it is away. Phones that are connected still ring in the app. The change applies at once.");
  }
  if (has("ring_window")) out.push(`Ring window ${patch.ring_window} s: a visitor ring is sent to the phones for this long.`);
  if (has("new_phones_enabled")) {
    out.push(patch.new_phones_enabled
      ? "New phones ON: a phone's first registration starts with its push ring notification switched on. Phones already registered keep their own switch."
      : "New phones OFF: a phone's first registration starts with its push ring notification switched off; switch it on for that phone on the Devices tab. Phones already registered keep their own switch.");
  }
  if (has("test_phones")) {
    const names = (ui.phones || []).filter((p) => patch.test_phones.includes(p.reg_id)).map((p) => p.name);
    out.push(patch.test_phones.length
      ? `Test phones (${names.join(", ") || patch.test_phones.length}): while this list is not empty a REAL visitor ring goes only to the listed phones that also have their push ring notification on. Everyone else is not notified.`
      : "Test phones: none. A real visitor ring goes to every phone with its push ring notification on again.");
  }
  if (has("live_rtsp_base")) {
    out.push(patch.live_rtsp_base
      ? `Live video away from home ON, from ${patch.live_rtsp_base}: a phone away from home is told which stream each live camera tile uses. Anyone with a Portico phone on your tailnet that is allowed to reach that port can pull ANY stream on it, not only the cameras shown to them.`
      : "Live video away from home OFF: phones are no longer told any camera stream address.");
  }
  if (has("live_sub_streams")) {
    out.push(patch.live_sub_streams.length
      ? `Cameras with a sub stream (${patch.live_sub_streams.join(", ")}): their Small tiles use the stream called <camera>_sub away from home.`
      : "Cameras with a sub stream: none. Small tiles use the main stream away from home.");
  }
  if (has("live_away_streams")) {
    const names = Object.keys(patch.live_away_streams);
    out.push(names.length
      ? `Own away addresses for ${names.join(", ")}: a phone away from home is given exactly these streams for those cameras, whatever the brand. The same exposure applies: the port they point at is outside Portico's per-camera grant.`
      : "Own away addresses: none. Cameras are back to the Frigate naming (or no stream if the address above is empty).");
  }
  return out;
}

/* ---- styles ------------------------------------------------------------------------------------------------------ */

const LIVE_RTSP_TEXT = "Live video away from home: the address of the camera server's RTSP port (empty = off). Anyone with a Portico phone on your tailnet that is allowed to reach that port can pull ANY stream on it, not only the cameras shown to them.";

const STYLE = `
  .settings, .settings .st-body { display: grid; gap: 12px; }
  .settings .st-card { background: var(--card-background-color); border: 1px solid var(--divider-color);
                       border-radius: var(--ha-card-border-radius, 12px); padding: 12px; }
  .settings h2 { font-size: 17px; font-weight: 500; margin: 0 0 6px; }
  .settings h3 { font-size: 14px; font-weight: 500; margin: 10px 0 4px; }
  .settings a { color: var(--primary-color); }
  .settings .st-sum { color: var(--secondary-text-color); font-size: 13px; }
  .settings .st-grid { display: grid; grid-template-columns: max-content 1fr; gap: 3px 14px; margin: 6px 0; font-size: 14px; }
  .settings .st-grid dt { color: var(--secondary-text-color); }
  .settings .st-grid dd { margin: 0; overflow-wrap: anywhere; }
  .settings .st-chip { display: inline-block; font-size: 12px; padding: 1px 8px; border-radius: 10px;
                       border: 1px solid var(--divider-color); color: var(--secondary-text-color); }
  .settings .st-chip.ok { color: var(--success-color, #2e7d32); border-color: var(--success-color, #2e7d32); }
  .settings .st-chip.bad { color: var(--error-color, #db4437); border-color: var(--error-color, #db4437); }
  .settings .st-form { display: grid; gap: 10px; max-width: 560px; }
  .settings .st-form label { display: grid; gap: 2px; font-size: 13px; color: var(--secondary-text-color); }
  .settings .st-form label.st-check { display: flex; gap: 6px; align-items: center; }
  .settings .st-hint { margin: 0 0 0 22px; }
  .settings .st-acts { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 6px; }
  .settings .st-list { margin: 4px 0; padding-left: 20px; display: grid; gap: 4px; }
  .settings .st-phone { display: flex; gap: 6px; align-items: center; }
  .settings button.danger { color: var(--error-color, #db4437); border-color: var(--error-color, #db4437); }
  @media (max-width: 720px) { .settings .st-grid { grid-template-columns: 1fr; } .settings .st-grid dd { margin-bottom: 4px; } }
`;

/* ---- the controller ---------------------------------------------------------------------------------------------- */

export class SettingsController {
  /* opts: doc (the panel's document), getHass (the current hass), classify (the panel's classifyError). */
  constructor(opts) {
    this.doc = opts.doc;
    this.getHass = opts.getHass;
    this.classify = opts.classify || ((err) => ({ kind: "other", message: (err && err.message) || "Something went wrong." }));
    this.mode = opts.mode === "app" ? "app" : opts.mode === "status" ? "status" : "settings";   // "settings": the Settings tab (the household switches); "status": the Status tab (the read-only readouts: door status, hub); "app": the push ring notification and app block (DEVICES-09; on the Settings tab for schema 6, on the Devices tab before)
    this.below = opts.below || (() => []);   // the panel: persistent elements drawn under the intro (schema 6: House calls, Push ring notification and app)
    this.view = "main";          // main | confirm
    this.status = null;          // door_status's reply
    this.statusError = "";
    this.ui = null;              // ui_settings/get's reply
    this.uiError = "";
    this.uiDraft = null;
    this.fpsApi = opts.fps || null;   // {read, write} from the panel: show_fps is a field of the content document
    this.fps = null;                  // {on} as stored; null = not read (or no api)
    this.fpsDraft = false;
    this.fpsError = "";
    this.fpsNotice = "";
    this.fpsBusy = false;
    this.uiCardError = "";
    this.uiCardNotice = "";
    this.pending = null;         // {patch, lines, error, busy, needsConfirm}
    this.loading = false;
    this._gen = 0;
    this.disposed = false;

    const doc = this.doc;
    this.el = doc.createElement("section");
    this.el.className = "settings";
    this.el.dataset.settings = "";
    const style = doc.createElement("style");
    style.textContent = STYLE;
    this.body = doc.createElement("div");
    this.body.className = "st-body";
    this.el.append(style, this.body);
    // Escape leaves the confirmation page without applying.
    this.el.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape" && this.view === "confirm" && this.pending && !this.pending.busy) this.cancelConfirm();
    });
    this._draw();
  }

  attach(parent) {
    if (this.el.parentNode !== parent) parent.append(this.el);
  }

  /* The tab was opened: everything is read again (a call may have come, a dialog may have saved). A confirmation page in progress stays. */
  show() {
    if (this.view === "main") this.load();
  }

  dispose() {
    this.disposed = true;
    this._gen++;
  }

  errText(err) {
    const code = err && err.code;
    if (code && Object.prototype.hasOwnProperty.call(ERROR_TEXT, code)) return ERROR_TEXT[code];
    return this.classify(err).message;
  }

  /* ---- reading ---- */

  async _read(type) {
    try {
      return { reply: await this.getHass().callWS({ type }) };
    } catch (err) {
      return { error: this.errText(err) };
    }
  }

  async load() {
    const gen = ++this._gen;
    this.loading = true;
    if (this.view === "main") this._draw();
    const app = this.mode === "app";
    const skip = Promise.resolve({ reply: null });
    const [status, ui] = await Promise.all([
      app || this.mode === "settings" ? skip : this._read("portico/door_status"),
      app ? this._read("portico/ui_settings/get") : skip]);
    if (gen !== this._gen || this.disposed) return false;
    this.loading = false;
    const ok = (r, key) => r.reply && typeof r.reply === "object" && (key === null || Array.isArray(r.reply[key]));
    if (app) {
      if (ok(ui, null) && typeof ui.reply.ring_enabled === "boolean") {
        const first = !this.ui;
        this.ui = ui.reply;
        this.uiError = "";
        if (first || !this.uiDraft) this.uiDraft = uiDraftOf(ui.reply);
      } else {
        this.ui = null;
        this.uiError = ui.error || "The server answered in a way this page does not understand. Reload the page.";
      }
      if (this.view === "main") this._draw();
      return true;
    }
    if (this.mode === "status") {
      if (ok(status, "doors")) { this.status = status.reply; this.statusError = ""; }
      else { this.status = null; this.statusError = status.error || "The server answered in a way this page does not understand. Reload the page."; }
      if (this.view === "main") this._draw();
      return true;
    }
    this._loadFps();
    if (this.view === "main") this._draw();
    return true;
  }

  async _loadFps() {
    if (!this.fpsApi) return;
    try {
      const r = await this.fpsApi.read();
      if (this.disposed) return;
      this.fps = { on: r.on === true };
      this.fpsDraft = this.fps.on;
      this.fpsError = "";
    } catch (err) {
      if (this.disposed) return;
      this.fps = null;
      this.fpsError = this.errText(err);
    }
    if (this.view === "main") this._draw();
  }

  async applyFps() {
    if (!this.fpsApi || !this.fps || this.fpsBusy) return false;
    this.fpsBusy = true;
    this.fpsError = "";
    this.fpsNotice = "";
    this._draw();
    try {
      const r = await this.fpsApi.write(this.fpsDraft);
      if (this.disposed) return false;
      this.fps = { on: r.on === true };
      this.fpsDraft = this.fps.on;
      this.fpsNotice = "Applied. The frame rate is " + (this.fps.on ? "shown" : "hidden") + " on live pictures from the next refresh.";
    } catch (err) {
      if (this.disposed) return false;
      this.fpsError = this.errText(err);
    }
    this.fpsBusy = false;
    this._draw();
    return true;
  }

  _fpsCard() {
    const card = this._h("div", "st-card");
    card.dataset.block = "fps-setting";
    card.append(this._h("h2", null, "Live pictures"));
    card.append(this._h("p", "st-sum", "This setting is for the whole household, not for one phone or screen."));
    if (this.fpsError) {
      const err = this._h("div", "msg err", this.fpsError);
      err.dataset.state = "fps-error";
      card.append(err);
    }
    if (!this.fps) return card;
    card.append(this._check("Show the frame rate on live pictures (a small number in the corner of each live tile, to see whether a picture lags)",
      this.fpsDraft, (v) => { this.fpsDraft = v; this.fpsNotice = ""; this._draw(); }, { field: "st-show-fps", disabled: this.fpsBusy }));
    if (this.fpsNotice) {
      const note = this._h("div", "msg ok", this.fpsNotice);
      note.dataset.state = "fps-notice";
      card.append(note);
    }
    const acts = this._h("div", "st-acts");
    const changed = this.fpsDraft !== this.fps.on;
    acts.append(this._btn(this.fpsBusy ? "Working..." : changed ? "Apply" : "No change", () => this.applyFps(), { act: "st-fps-apply", cls: "primary", disabled: !changed || this.fpsBusy }));
    acts.append(this._btn("Discard", () => { this.fpsDraft = this.fps.on; this._draw(); }, { act: "st-fps-reset", disabled: !changed || this.fpsBusy }));
    card.append(acts);
    return card;
  }

  async reviewUi() {
    const ui = this.ui;
    if (!ui || !this.uiDraft || this.pending) return false;
    const patch = uiPatch(ui, this.uiDraft);
    if (!Object.keys(patch).length) return false;
    this.uiCardError = "";
    this.uiCardNotice = "";
    let reply;
    try {
      reply = await this.getHass().callWS({ type: "portico/ui_settings/set", patch, validate_only: true });
    } catch (err) {
      if (this.disposed) return false;
      this.uiCardError = this.errText(err);
      this._draw();
      return false;
    }
    if (this.disposed) return false;
    if (!reply || reply.result !== "valid") {
      this.uiCardError = "The server answered in a way this page does not understand. Reload the page.";
      this._draw();
      return false;
    }
    const needsConfirm = Object.keys(patch).some((k) => k !== "ring_window");
    this.pending = { patch, lines: uiConsequenceLines(patch, ui), error: "", busy: false, needsConfirm };
    if (!needsConfirm) return this.apply();
    this.view = "confirm";
    this._draw();
    const keep = this.body.querySelector('[data-act="st-no"]');
    if (keep && typeof keep.focus === "function") keep.focus();
    return true;
  }

  cancelConfirm() {
    if (this.view !== "confirm" || !this.pending || this.pending.busy) return;
    const failed = !!this.pending.error;
    this.pending = null;
    this.view = "main";
    this._draw();
    if (failed) this.load();         // a refusal (a stale revision, a call in progress) leaves the page reading the truth again
  }

  /* The one place `confirm: true` is sent: from the confirmation page's button, or for a live-only patch the server does not ask it for. */
  async apply() {
    const p = this.pending;
    if (!p || p.busy) return false;
    p.busy = true;
    p.error = "";
    if (this.view === "confirm") this._draw();
    let reply;
    try {
      const msg = { type: "portico/ui_settings/set", patch: p.patch };
      if (p.needsConfirm) msg.confirm = true;
      reply = await this.getHass().callWS(msg);
    } catch (err) {
      if (this.disposed) return false;
      p.busy = false;
      p.error = this.errText(err);
      if (this.view === "confirm") this._draw();
      else this._failLive(p);
      return false;
    }
    if (this.disposed) return false;
    this.pending = null;
    this.view = "main";
    if (reply && reply.settings) {
      this.ui = reply.settings;
      this.uiDraft = uiDraftOf(reply.settings);
    }
    this.uiCardNotice = "Applied. It takes effect at once.";
    this._draw();
    return true;
  }

  _failLive(p) {
    this.uiCardError = p.error;
    this.pending = null;
    this._draw();
  }

  /* ---- drawing ---- */

  _h(tag, cls, text) {
    const node = this.doc.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  _btn(label, onClick, { act, cls, disabled } = {}) {
    const b = this._h("button", cls || "", label);
    b.type = "button";
    if (act) b.dataset.act = act;
    if (disabled) b.disabled = true;
    else b.addEventListener("click", onClick);
    return b;
  }

  _draw() {
    if (this.disposed) return;
    const body = this.body;
    body.replaceChildren();
    if (this.view === "confirm" && this.pending) {
      body.append(this._confirmPage());
      return;
    }
    if (this.mode === "app") {          // the Devices tab's block: only the push ring notification and the app's settings
      body.append(this._uiCard());
      return;
    }
    if (this.mode === "status") {       // the Status tab: only what is read, nothing that changes a setting
      body.append(this._statusIntro());
      body.append(this._statusCards());
      return;
    }
    body.append(this._intro());
    if (this.fpsApi) body.append(this._fpsCard());
    for (const node of this.below()) if (node) body.append(node);
  }

  _intro() {
    const card = this._h("div", "st-card");
    card.dataset.block = "settings-intro";
    card.append(this._h("h2", null, "Settings"));
    card.append(this._h("p", "st-sum", "Household settings: live pictures, house calls, the push ring notification and the app (what the doors are doing is on the Status tab; the phones and screens are on the Devices tab). Nothing here is switched on by itself: a switch changes only when you press Apply. Passwords and keys are never shown or entered here; they stay in the Home Assistant dialogs, which also stay as the way back if this page is ever wrong."));
    const bar = this._h("div", "st-acts");
    bar.append(this._btn(this.loading ? "Reading..." : "Refresh", () => { this.load(); }, { act: "st-refresh", disabled: this.loading }));
    card.append(bar);
    return card;
  }

  _statusIntro() {
    const card = this._h("div", "st-card");
    card.dataset.block = "status-intro";
    card.append(this._h("h2", null, "Status"));
    card.append(this._h("p", "st-sum", "What the doors and the hub are doing right now. Nothing on this tab changes anything; the switches are on the Settings tab."));
    const bar = this._h("div", "st-acts");
    bar.append(this._btn(this.loading ? "Reading..." : "Refresh", () => { this.load(); }, { act: "st-refresh", disabled: this.loading }));
    card.append(bar);
    return card;
  }

  _statusCards() {
    const wrap = this._h("div");
    wrap.dataset.block = "status";
    const head = this._h("div", "st-card");
    head.append(this._h("h2", null, "Door status"));
    if (this.statusError) {
      const err = this._h("div", "msg err", this.statusError);
      err.dataset.state = "status-error";
      head.append(err);
    } else if (!this.status) head.append(this._h("p", "st-sum", this.loading ? "Reading..." : "Not read yet."));
    else {
      const hub = this._h("p", "st-sum", `The hub (the door program Portico runs): ${hubText(this.status.hub)}.`);
      hub.dataset.field = "st-hub";
      head.append(hub);
      if (!this.status.doors.length) head.append(this._h("p", "st-sum", "No door is set up. Add one under Settings > Devices & services > Portico > Add entry."));
    }
    wrap.append(head);
    if (this.status) {
      for (const door of this.status.doors) wrap.append(this._statusCard(door));
    }
    return wrap;
  }

  _statusCard(door) {
    const card = this._h("div", "st-card");
    card.dataset.block = "door-status";
    card.dataset.entry = door.entry_id;
    const title = this._h("h2", null, door.name || "Door");
    title.append(" ", this._h("span", "st-chip " + (door.connected ? "ok" : "bad"), door.connected ? "Connected" : "Not connected"));
    card.append(title);
    const list = this._h("dl", "st-grid");
    for (const [label, text] of statusRows(door)) {
      list.append(this._h("dt", null, label), this._h("dd", null, text));
    }
    card.append(list);
    return card;
  }

  _check(label, checked, onChange, { field, disabled, hint } = {}) {
    const wrap = this._h("div");
    const lab = this._h("label", "st-check");
    const box = this.doc.createElement("input");
    box.type = "checkbox";
    box.checked = !!checked;
    if (field) box.dataset.field = field;
    if (disabled) box.disabled = true;
    box.addEventListener("change", () => onChange(box.checked));
    lab.append(box, this._h("span", null, label));
    wrap.append(lab);
    if (hint) wrap.append(this._h("div", "st-sum st-hint", hint));
    return wrap;
  }

  _uiCard() {
    const card = this._h("div", "st-card");
    card.dataset.block = "ui-settings";
    card.dataset.section = "away-ring-and-app";
    card.append(this._h("h2", null, "Push ring notification and app"));
    card.append(this._h("p", "st-sum", "Who is notified on a phone that is away when a visitor rings. This does not touch the doors, the gate or house calls."));
    if (this.uiError) {
      const err = this._h("div", "msg err", this.uiError);
      err.dataset.state = "ui-error";
      card.append(err);
      return card;
    }
    if (!this.ui || !this.uiDraft) return card;
    const ui = this.ui;
    const d = this.uiDraft;
    if (!ui.push_entry) {
      card.append(this._h("div", "msg err", ERROR_TEXT.no_push_entry));
      return card;
    }
    const form = this._h("div", "st-form");
    form.append(this._check("Push ring notification on", d.ring_enabled, (v) => { this.uiDraft = { ...d, ring_enabled: v }; this._clearUi(); },
      { field: "st-ring-enabled", hint: "The whole push ring notification. Off: no phone is notified while it is away." }));
    const winLabel = this._h("label", null, `Ring window in seconds (${ui.ring_window_min} to ${ui.ring_window_max})`);
    const win = this.doc.createElement("input");
    win.type = "number";
    win.min = String(ui.ring_window_min);
    win.max = String(ui.ring_window_max);
    win.step = "1";
    win.value = String(d.ring_window);
    win.dataset.field = "st-ring-window";
    win.addEventListener("input", () => {
      const n = Number(win.value);
      this.uiDraft = { ...this.uiDraft, ring_window: Number.isInteger(n) && win.value.trim() !== "" ? n : NaN };
      this.uiCardError = "";
      this._drawUiActs(card);
    });
    winLabel.append(win);
    form.append(winLabel);
    form.append(this._check("New phones get the push ring notification", d.new_phones_enabled, (v) => { this.uiDraft = { ...d, new_phones_enabled: v }; this._clearUi(); },
      { field: "st-new-phones", hint: "Whether a phone's first registration starts with its push ring notification on. A phone already registered keeps its own switch." }));
    form.append(this._h("h3", null, "Test phones"));
    form.append(this._h("p", "st-sum", "Empty is normal. While any phone is ticked, a REAL visitor ring goes only to the ticked phones that also have their push ring notification on, so a staged test cannot ring the household."));
    if (!ui.phones.length) form.append(this._h("p", "st-sum", ui.running ? "No phone has registered for the push ring notification yet." : "The push ring notification is not running, so its phones cannot be listed."));
    for (const phone of ui.phones) {
      form.append(this._check(`${phone.name} (push ring notification ${phone.away_ring ? "on" : "off"})`, d.test_phones.has(phone.reg_id), (v) => {
        const next = new Set(d.test_phones);
        if (v) next.add(phone.reg_id); else next.delete(phone.reg_id);
        this.uiDraft = { ...d, test_phones: next };
        this._clearUi();
      }, { field: `st-test-${phone.reg_id}` }));
    }
    form.append(this._h("h3", null, "Live video in the app"));
    form.append(this._h("p", "st-sum", LIVE_RTSP_TEXT));
    form.append(this._textField("Address of the camera server's RTSP port, like rtsp://10.6.0.6:8554 (empty = off)", d.live_rtsp_base, "st-live-rtsp-base",
      (v) => { this.uiDraft = { ...this.uiDraft, live_rtsp_base: v }; this.uiCardError = ""; this._drawUiActs(card); }));
    form.append(this._textField("Cameras that have a sub stream named <camera>_sub (Frigate camera names, separated by commas)", d.live_sub_streams, "st-live-sub-streams",
      (v) => { this.uiDraft = { ...this.uiDraft, live_sub_streams: v }; this.uiCardError = ""; this._drawUiActs(card); }));
    form.append(this._textField("Own away addresses per camera, like camera.porch rtsp://10.6.0.9:554/main rtsp://10.6.0.9:554/sub (sub is optional; any brand; no password; empty = none)", d.live_away_streams, "st-live-away-streams",
      (v) => { this.uiDraft = { ...this.uiDraft, live_away_streams: v }; this.uiCardError = ""; this._drawUiActs(card); }));
    card.append(form);
    this._drawUiActs(card);
    return card;
  }

  _textField(label, value, field, onInput) {
    const wrap = this._h("label", null, label);
    const input = this.doc.createElement("input");
    input.type = "text";
    input.value = value;
    input.dataset.field = field;
    input.autocomplete = "off";
    input.spellcheck = false;
    input.addEventListener("input", () => onInput(input.value));
    wrap.append(input);
    return wrap;
  }

  _clearUi() {
    this.uiCardError = "";
    this.uiCardNotice = "";
    this._draw();
  }

  _drawUiActs(card) {
    for (const old of Array.from(card.querySelectorAll('[data-role="ui-acts"]'))) old.remove();
    const ui = this.ui;
    const d = this.uiDraft;
    const patch = uiPatch(ui, d);
    const winOk = Number.isInteger(d.ring_window) && d.ring_window >= ui.ring_window_min && d.ring_window <= ui.ring_window_max;
    const box = this._h("div");
    box.dataset.role = "ui-acts";
    if (this.uiCardError) {
      const err = this._h("div", "msg err", this.uiCardError);
      err.dataset.state = "ui-card-error";
      box.append(err);
    }
    if (this.uiCardNotice) {
      const note = this._h("div", "msg ok", this.uiCardNotice);
      note.dataset.state = "ui-notice";
      box.append(note);
    }
    if (!winOk) box.append(this._h("p", "st-sum", `The ring window is a whole number from ${ui.ring_window_min} to ${ui.ring_window_max}.`));
    const liveProblem = liveBaseProblem(d.live_rtsp_base) || liveSubProblem(d.live_sub_streams) || liveAwayParse(d.live_away_streams).problem;
    if (liveProblem) {
      const bad = this._h("p", "st-sum", liveProblem);
      bad.dataset.state = "live-rtsp-problem";
      box.append(bad);
    }
    const acts = this._h("div", "st-acts");
    const n = Object.keys(patch).length;
    acts.append(this._btn(n ? `Review ${n === 1 ? "the change" : `${n} changes`}...` : "No change", () => this.reviewUi(),
      { act: "st-ui-review", cls: "primary", disabled: !n || !winOk || !!liveProblem || !!this.pending }));
    acts.append(this._btn("Discard", () => { this.uiDraft = uiDraftOf(this.ui); this._clearUi(); }, { act: "st-ui-reset", disabled: !n }));
    box.append(acts);
    card.append(box);
  }

  _confirmPage() {
    const p = this.pending;
    const card = this._h("div", "st-card");
    card.dataset.view = "confirm";
    card.setAttribute("role", "alertdialog");
    const heading = "Change the push ring notification?";
    card.setAttribute("aria-label", heading);
    card.append(this._h("h2", null, heading));
    const list = this._h("ul", "st-list");
    list.dataset.field = "st-lines";
    for (const line of p.lines) list.append(this._h("li", null, line));
    card.append(list);
    if (p.error) {
      const err = this._h("div", "msg err", p.error);
      err.dataset.state = "confirm-error";
      card.append(err);
    }
    const acts = this._h("div", "st-acts");
    // The safe button is first, so Tab from the card reaches it before the one that changes anything.
    acts.append(this._btn("Keep it as it is", () => this.cancelConfirm(), { act: "st-no", disabled: p.busy }));
    acts.append(this._btn(p.busy ? "Working..." : (p.button || "Apply"), () => this.apply(), { act: "st-yes", cls: "danger", disabled: p.busy }));
    card.append(acts);
    return card;
  }
}
