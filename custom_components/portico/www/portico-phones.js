/* Portico panel: the Phones part of the Devices tab (UI split P2; design 3.1 and 3.3; DEV-518 Devices: House calls and Phones sections).
 *
 * A SEPARATE module so a bad edit here cannot blank the content tabs: the panel imports it lazily and, when the
 * import fails, simply has no Phones tab. It exports `PhonesController` (what the panel mounts) and the pure helpers
 * the node tests reach (`qrScale`, `formatCountdown`, `validateName`...).
 *
 * The controller owns ONE persistent element (`controller.el`). The panel's `_renderNow()` calls
 * `root.replaceChildren()` on every tab click, theme flip and content change, so the panel re-attaches this element
 * each time; its timers, its resize listener and its blob URL live on the controller, never on a rendered node.
 *
 * Wire (all admin; the panel is open to every household login, so the server's command class is the access control):
 *   portico/phones/list {}                                   -> {header, phones}
 *   portico/phones/enroll {name, allow_http, ha_dial, confirmed_entry_id?}
 *                                                            -> {result: "created", entry_id, qr_token, modules, expires_in,
 *                                                                expires_at, notes, dial_target, created}
 *                                                               | {result: "needs_confirm", entry_id, name} | a typed error
 *   portico/phones/cancel {entry_id}                         -> {result: "cancelled", removed}
 *   portico/phones/revoke {entry_id, confirm: true}          -> {result: "revoked", legacy}      (UI split P3)
 *   portico/phones/set {entry_id, patch}                     -> {result: "updated", phone: <row>}  patch: {doors?, away_ring?, house_receive?}
 *   portico/phones/make_panel {entry_id}                     -> {result: "updated", phone: <row>}  (DEV-807: makes an enrolled phone a panel that owns its pages; no screen, no slug;
 *                                                               a typed error: legacy_phone, already_panel, not_portico_user)
 *   portico/doors {}                                         -> {doors: [{door_id, name, ...}]}  (any user may ask; the door choices)
 *   GET /api/portico/enroll/qr/<token>                       the QR as a PNG (one pixel per module, 4-module quiet zone),
 *                                                            fetched ONCE as a blob with the user's own bearer header.
 *   GET /api/portico/enroll/text/<token>                     the text the QR encodes (text/plain), fetched only when an administrator
 *                                                            presses "Copy enrolment text"; held in memory for the page, never rendered unless
 *                                                            the clipboard is refused
 * The QR is never a websocket frame (HA logs every frame at DEBUG) and never in a URL the browser navigates to.
 *
 * All user-supplied text (a phone's name, an address) goes in through textContent, never as markup (a test greps this
 * file for every HTML-string setter).
 */

export const QR_QUIET = 4;           // the quiet zone, in modules, on each side (the PNG carries it)
export const QR_K_MIN = 3;           // below 3 device pixels a module the phone's camera cannot read it
export const QR_K_MAX = 6;
export const QR_CHROME_PX = 240;     // the height the panel's own chrome takes (toolbar, header, title): what the QR must leave
export const NARROW_PX = 720;        // below this the QR stacks above its text and the list becomes cards
export const NAME_MAX = 24;       // the owner, 2026-10-23: longer names make a QR the app's decoder often cannot find
export const DENSE_MODULES = 89;     // version 18 and up: see the note on the QR page
export const QR_PATH = "/api/portico/enroll/qr/";
export const TEXT_PATH = "/api/portico/enroll/text/";
export const COUNTDOWN_STEP_MS = 10000;
export const STATUS_ENTITIES = ["sensor.call_state", "sensor.listen_leg"];    // the optional read-only gate status line (design 3.2)

export const NOTE_TEXT = {
  replaces_earlier_qr: "This phone already had a QR waiting. The earlier QR no longer works.",
  reenroll_long_lived: "This phone was enrolled before one-time codes. Its old access keeps working until this QR is scanned, then it is replaced.",
  fcm_skipped: "The push ring notification (Firebase) values were unusable, so this QR leaves them out. The phone will not register for the push ring notification until they are fixed in the Home Assistant dialog.",
  manual_tailscale: "Manual Tailscale mode: this QR carries no Tailscale key. Join the phone to your tailnet yourself, or it cannot connect away from home.",
};

export const WHERE_TEXT = [
  ["In Home Assistant, Settings > Devices & services > Portico", "the doors (gate, front door), their connections and the call audio, the push ring notification, the Firebase key and the Tailscale credentials. These dialogs stay as the Advanced / recovery path."],
  ["In this panel", "phones, the push ring notification and the app's settings (this tab), screens, pages, and the ring and call rules."],
];

const WHERE_LINKS = [["/config/integrations/integration/portico", "Portico in Home Assistant"]];

/* ---- pure helpers ---------------------------------------------------------------------------------------------- */

/* The QR's size on screen, or null when it cannot be drawn readably. `availableCss` is the CSS pixels the QR may take
 * (a square), `modules` the QR's modules per side WITHOUT the quiet zone, `dpr` the device pixel ratio. A module is an
 * INTEGER number `k` of DEVICE pixels, 3 to 6 (a fractional scale blurs a module edge and a phone camera misreads it):
 * k = clamp(floor(available_device_px / (modules + 8)), 3, 6); the CSS width is (modules + 8) * k / dpr. */
export function qrScale(availableCss, modules, dpr) {
  const ratio = dpr > 0 ? dpr : 1;
  if (!(modules > 0) || !(availableCss > 0)) return null;
  const total = modules + 2 * QR_QUIET;
  const k = Math.min(QR_K_MAX, Math.floor(Math.floor(availableCss * ratio) / total));
  if (k < QR_K_MIN) return null;
  return { k, css: (total * k) / ratio, device: total * k, total };
}

/* "9 min 50 s": the time left, rounded UP to ten seconds (the text changes every ten). */
export function formatCountdown(seconds) {
  const s = Math.max(0, Math.ceil(seconds / 10) * 10);
  const m = Math.floor(s / 60);
  const r = s % 60;
  if (m === 0) return `${r} s`;
  return r === 0 ? `${m} min` : `${m} min ${r} s`;
}

/* The name as the server will read it: stripped, 1 to NAME_MAX characters (by code point, as the server counts). */
export function validateName(text) {
  const name = typeof text === "string" ? text.trim() : "";
  const length = Array.from(name).length;
  if (length < 1) return { ok: false, name, error: "The phone needs a name." };
  if (length > NAME_MAX) return { ok: false, name, error: `The name is longer than ${NAME_MAX} characters.` };
  return { ok: true, name };
}

export function localTime(iso, locale) {
  const d = new Date(iso);
  if (!iso || Number.isNaN(d.getTime())) return "";
  return d.toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit" });
}

export function localDateTime(iso, locale) {
  const d = new Date(iso);
  if (!iso || Number.isNaN(d.getTime())) return "";
  return d.toLocaleString(locale, { dateStyle: "medium", timeStyle: "short" });
}

/* One row's state as words. */
export function stateText(row) {
  if (row.state === "pending") return row.expired ? "Code expired: make a new code" : "Waiting for the first scan";
  if (row.state === "compromised") return "Flagged: its code was used twice, make a new code";
  return "Enrolled";
}

export function doorsText(doors) {
  if (doors === null || doors === undefined) return "All doors";
  const n = Array.isArray(doors) ? doors.length : 0;
  return n === 1 ? "1 door" : `${n} doors`;
}

/* DEV-807 panel mode (own-pages design): a panel is a phone that owns its pages, its Ring pages and its roster hiding like any phone;
 * no screen is declared and nothing is typed. A legacy `panel_screen` on a row is ignored. */
/* Whether the row offers "Make a panel": a phone entry (a legacy row cannot be a panel), signed in, not a panel yet. */
export function canMakePanel(row) {
  return !!row && !row.legacy && row.state === "enrolled" && row.panel !== true;
}
export function panelText(row) {
  if (!row || row.panel !== true) return "";
  return "Panel. It has its own pages and Ring pages, chosen in its section like a phone's; the doors that ring it come from the door Talk elements on its pages.";
}

/* What revoking a phone destroys, as the confirmation page lists it (design 3.1). */
export function revokeConsequences(row) {
  const out = [
    "Its Home Assistant login: every token the phone holds stops working at once.",
    "Its Home Assistant user, when Portico made it (named \"Portico - <phone>\"). Any other user keeps its account and loses only the tokens enrolment made.",
    "Its push ring notification registration: the phone stops getting rings.",
    "Its own screen settings (the content override for this phone).",
  ];
  if (row && row.state !== "enrolled") out.push("The QR code that is still waiting, if there is one.");
  out.push("Not deleted: its Tailscale node. Home Assistant shows a notice that names it; delete it in the Tailscale admin console.");
  return out;
}

/* The sentence under a phone's House calls tick box. House calls are always on (D-20261122-aafb: who is called is set per action),
 * so the phone's own setting is the only switch. */
export function houseHint() {
  return "With this ticked, this phone can ring and be rung by other phones and screens.";
}

/* The gate status line from `hass.states`, or "" when neither sensor exists (a different install). Read-only. */
export function statusLine(states) {
  const bits = [];
  const call = states && states["sensor.call_state"];
  const leg = states && states["sensor.listen_leg"];
  if (call && typeof call.state === "string") bits.push(`Call state: ${call.state}`);
  if (leg && typeof leg.state === "string") bits.push(`Listen leg: ${leg.state}`);
  return bits.join(". ");
}

/* The door ids a form would send: the ticked ones, in the order the list shows them, then any ticked id the list does not
 * have (the server refuses those and says so). null = every door. */
export function pickedDoors(all, picked, doorList) {
  if (all) return null;
  const known = (doorList || []).map((d) => d.id);
  return [...known.filter((id) => picked.has(id)), ...Array.from(picked).filter((id) => !known.includes(id))];
}

export function sameDoors(a, b) {
  if (a === null || a === undefined || b === null || b === undefined) return (a ?? null) === (b ?? null);
  return a.length === b.length && a.every((id) => b.includes(id));
}

export function onOff(value) {
  return value === true ? "On" : value === false ? "Off" : "Not known";
}

const STYLE = `
  .phones, .phones .ph-body { display: grid; gap: 12px; }
  .phones a { color: var(--primary-color); }
  .phones .ph-card { background: var(--card-background-color); border: 1px solid var(--divider-color);
                     border-radius: var(--ha-card-border-radius, 12px); padding: 12px; }
  .phones h2 { font-size: 17px; font-weight: 500; margin: 0 0 6px; }
  .phones .ph-where ul { margin: 4px 0 0; padding-left: 20px; }
  .phones .ph-where li { margin: 2px 0; }
  .phones .ph-bar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
  .phones .ph-sum { color: var(--secondary-text-color); font-size: 13px; }
  .phones table { width: 100%; border-collapse: collapse; }
  .phones th { text-align: left; font-size: 12px; font-weight: 500; text-transform: uppercase; letter-spacing: .06em;
               color: var(--secondary-text-color); padding: 4px 8px; }
  .phones td { padding: 8px; border-top: 1px solid var(--divider-color); vertical-align: top; overflow-wrap: anywhere; }
  .phones td.ph-name { font-weight: 500; }
  .phones .ph-chip { display: inline-block; font-size: 12px; padding: 1px 8px; border-radius: 10px;
                     border: 1px solid var(--divider-color); color: var(--secondary-text-color); }
  .phones .ph-chip.ok { color: var(--success-color, #2e7d32); border-color: var(--success-color, #2e7d32); }
  .phones .ph-chip.warn { color: var(--warning-color, #b26a00); border-color: var(--warning-color, #b26a00); }
  .phones .ph-chip.bad { color: var(--error-color, #db4437); border-color: var(--error-color, #db4437); }
  .phones .ph-form { display: grid; gap: 10px; max-width: 520px; }
  .phones .ph-form label { display: grid; gap: 2px; font-size: 13px; color: var(--secondary-text-color); }
  .phones .ph-form label.ph-check { display: flex; gap: 6px; align-items: center; }
  .phones .ph-acts { display: flex; flex-wrap: wrap; gap: 8px; }
  .phones .ph-qrwrap { display: flex; flex-wrap: wrap; gap: 16px; align-items: flex-start; }
  .phones .ph-qrbox { background: #fff; color: #000; color-scheme: light; filter: none; forced-color-adjust: none;
                      line-height: 0; display: inline-block; flex: 0 0 auto; }
  .phones .ph-qrbox img { display: block; image-rendering: pixelated; image-rendering: crisp-edges;
                          background: #fff; filter: none; }
  .phones .ph-qrslot { flex: 0 1 auto; min-width: 0; max-width: 100%; }
  .phones .ph-qrtext { flex: 1 1 280px; min-width: 0; display: grid; gap: 8px; }
  .phones .ph-qrtext p { margin: 0; }
  .phones .ph-qrnone { padding: 12px; line-height: 1.4; max-width: 320px; background: var(--secondary-background-color); color: var(--primary-text-color); }
  .phones .ph-count { font-size: 18px; font-weight: 500; }
  .phones .ph-notes { margin: 0; padding-left: 20px; display: grid; gap: 4px; }
  .phones .ph-list { margin: 4px 0; padding-left: 20px; display: grid; gap: 4px; }
  .phones .ph-door { display: flex; gap: 6px; align-items: center; }
  .phones .ph-sub { margin-left: 22px; display: grid; gap: 4px; }
  .phones .ph-menu { position: relative; display: inline-block; }
  .phones .ph-dots { background: none; border: none; border-radius: 50%; width: 36px; height: 36px; padding: 6px; cursor: pointer;
                     color: var(--secondary-text-color); display: inline-flex; align-items: center; justify-content: center; }
  .phones .ph-dots:hover, .phones .ph-dots[aria-expanded="true"] { background: var(--secondary-background-color); }
  .phones .ph-dots svg { width: 22px; height: 22px; fill: currentColor; }
  .phones .ph-pop { position: absolute; right: 0; top: 100%; z-index: 5; min-width: 150px; display: grid; padding: 4px 0;
                    background: var(--card-background-color); border: 1px solid var(--divider-color); border-radius: 8px;
                    box-shadow: 0 4px 14px rgba(0,0,0,.25); }
  .phones .ph-pop[hidden] { display: none; }
  .phones .ph-pop button { border: none; background: none; text-align: left; padding: 10px 16px; border-radius: 0; cursor: pointer;
                           color: var(--primary-text-color); font: inherit; }
  .phones .ph-pop button:hover, .phones .ph-pop button:focus { background: var(--secondary-background-color); }
  .phones .ph-pop button.danger { color: var(--error-color, #db4437); }
  .phones .ph-sections { display: grid; gap: 12px; margin-top: 10px; }
  .phones .ph-section { background: var(--secondary-background-color); min-width: 0; }
  .phones .ph-head { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 12px; }
  .phones .ph-head h2 { margin: 0; overflow-wrap: anywhere; }
  .phones .ph-grow { flex: 1 1 auto; }
  .phones .ph-facts { margin: 4px 0; }
  .phones dl.ph-grid { display: grid; grid-template-columns: max-content 1fr; gap: 2px 14px; margin: 4px 0 8px; font-size: 13px; }
  .phones dl.ph-grid dt { color: var(--secondary-text-color); }
  .phones dl.ph-grid dd { margin: 0; overflow-wrap: anywhere; }
  .phones td.ph-actions { text-align: right; white-space: nowrap; width: 1%; }
  .phones details.ph-where summary { cursor: pointer; font-size: 13px; color: var(--secondary-text-color); }
  .phones button.danger { color: var(--error-color, #db4437); border-color: var(--error-color, #db4437); }
  @media (max-width: 720px) {
    .phones thead { display: none; }
    .phones table, .phones tbody, .phones tr, .phones td { display: block; width: auto; }
    .phones tr { border: 1px solid var(--divider-color); border-radius: 10px; margin-bottom: 8px; padding: 4px 0;
                 background: var(--secondary-background-color); }
    .phones td { border-top: none; padding: 3px 10px; }
    .phones td::before { content: attr(data-label) ": "; color: var(--secondary-text-color); font-size: 12px; }
    .phones td.ph-name::before, .phones td.ph-name-less::before { content: none; }
    .phones td.ph-name-less:empty { display: none; }
  }
`;

/* ---- the controller -------------------------------------------------------------------------------------------- */

export class PhonesController {
  /* opts: doc (the panel's document), getHass (the current hass), classify (the panel's classifyError), and for tests
   * win, now (monotonic ms), fetch, setTimer / clearTimer. */
  constructor(opts) {
    this.doc = opts.doc;
    this.win = opts.win || opts.doc.defaultView;
    this.getHass = opts.getHass;
    this.classify = opts.classify || ((err) => ({ kind: "other", message: (err && err.message) || "Something went wrong." }));
    this._now = opts.now || (() => (this.win.performance ? this.win.performance.now() : Date.now()));
    this._fetch = opts.fetch || ((...a) => this.win.fetch(...a));
    this._setTimer = opts.setTimer || ((fn, ms) => this.win.setTimeout(fn, ms));
    this._clearTimer = opts.clearTimer || ((id) => this.win.clearTimeout(id));
    this.locale = opts.locale;
    this.canPreview = opts.canPreview || (() => false);       // the panel: can this phone's screen be previewed?
    this.previewPhone = opts.previewPhone || (() => false);   // the panel: open the Pages tab on this phone
    this.onView = opts.onView || (() => {});                  // the panel: the page changed (list or a sub-page), so it can hide what does not belong
    this.aboveList = opts.aboveList || (() => null);          // the panel: the "Push ring notification and app" block (DEVICES-09), drawn above the phone list (schema 4/5 only)
    this.onPanelMade = opts.onPanelMade || (() => {});        // the panel: a panel was made, so the rows changed (it reloads, or marks itself stale)
    this.nextStep = opts.nextStep || (() => "");              // the panel: the sentence on the QR page about giving the new phone a page (schema 6), or ""
    this.deviceBody = opts.deviceBody || (() => null);        // the panel: one phone's own settings (its Pages and Ring pages selectors), a node or null (schema 6)
    this.perDevice = false;      // schema 6: one section per phone; The panel sets it with setPerDevice.
    this.panelForm = null;       // DEV-807: {row, busy, error} while the "Make a panel" page is open
    this.view = "list";          // list | enrol | confirm | qr | manage | revoke | panel
    this.data = null;            // {header, phones}
    this.listError = "";
    this.loading = false;
    this.form = { name: "", allow_http: false, ha_dial: "" };
    this.formError = "";
    this.fieldError = "";        // the form field the server named
    this.busy = false;
    this.confirm = null;         // {entry_id, name}
    this.manage = null;          // the phone being managed (P3): see openManage
    this.notice = "";            // a line the list shows once ("Revoked Pixel.")
    this.qr = null;              // see _startQr
    this._timer = null;
    this._resizeBound = null;
    this._gen = 0;
    this.disposed = false;

    const doc = this.doc;
    this.el = doc.createElement("section");
    this.el.className = "phones";
    this.el.dataset.phones = "";
    const style = doc.createElement("style");
    style.textContent = STYLE;
    this.body = doc.createElement("div");
    this.body.className = "ph-body";
    this.el.append(style, this.body);
    // Escape leaves the revoke confirmation without revoking.
    this.el.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape" && this.view === "revoke" && this.manage && !this.manage.busy) this.cancelRevoke();
    });
    this._draw();
  }

  /* The panel calls this after every render: put the persistent element where the new page wants it. */
  attach(parent) {
    if (this.el.parentNode !== parent) parent.append(this.el);
  }

  /* The panel says which layout the document wants: schema 6 (one section per phone, House calls on the Settings tab) or the
   * older one. A change redraws; the same value does nothing. */
  setPerDevice(on) {
    on = on === true;
    if (this.perDevice === on) return;
    this.perDevice = on;
    this._draw(true);
  }

  /* The tab was opened. The list is read every time (the phone list changes under the page: a phone redeems its code). */
  show() {
    if (this.view === "list") this.load();
  }

  dispose() {
    this.disposed = true;
    this._gen++;
    this._stopTimer();
    this._unbindResize();
    this._revoke();
    this.qr = null;
  }

  /* ---- reading the list ---- */

  async load() {
    const gen = ++this._gen;
    this.loading = true;
    if (this.view === "list") this._draw();
    let reply;
    try {
      reply = await this.getHass().callWS({ type: "portico/phones/list" });
    } catch (err) {
      if (gen !== this._gen || this.disposed) return false;
      this.loading = false;
      this.listError = this.classify(err).message;
      if (this.view === "list") this._draw();
      return false;
    }
    if (gen !== this._gen || this.disposed) return false;
    this.loading = false;
    this.listError = "";
    this.data = reply && typeof reply === "object" ? reply : { header: {}, phones: [] };
    if (this.view === "list") this._draw();
    return true;
  }

  get header() {
    return (this.data && this.data.header) || {};
  }

  /* ---- the Enrol page ---- */

  openEnrol(name) {
    this._gen++;                 // a list read in flight must not redraw over the form
    this.notice = "";
    // The remembered address is the suggestion; a blank field later FORGETS it (the server's rule), so it is shown.
    this.form = { name: name || "", allow_http: false, ha_dial: this.header.ha_dial || "" };
    this.formError = "";
    this.fieldError = "";
    this.view = "enrol";
    this._draw();
  }

  back() {
    this._leaveQr();
    this.manage = null;
    this.panelForm = null;
    this.view = "list";
    this._draw();
    this.load();
  }

  /* ---- DEV-807: make a panel (it owns its pages; no screen, no address) ---- */

  openMakePanel(row) {
    if (this.view !== "list" || !this.perDevice || !canMakePanel(row)) return false;
    this._gen++;
    this.notice = "";
    this.panelForm = { row, busy: false, error: "", field: "" };
    this.view = "panel";
    this._draw();
    return true;
  }

  async makePanel() {
    const f = this.panelForm;
    if (!f || f.busy || this.view !== "panel") return false;
    f.busy = true;
    f.error = "";
    f.field = "";
    this._draw();
    let reply;
    try {
      reply = await this.getHass().callWS({ type: "portico/phones/make_panel", entry_id: f.row.entry_id });
    } catch (err) {
      if (this.disposed || this.panelForm !== f) return false;
      f.busy = false;
      f.error = this.classify(err).message;
      f.field = (err && err.error && err.error.field) || (err && err.field) || "";
      this._draw();
      return false;
    }
    if (this.disposed || this.panelForm !== f) return false;
    if (!reply || reply.result !== "updated" || !reply.phone) {
      f.busy = false;
      f.error = "The server answered in a way this page does not understand. Reload the page.";
      this._draw();
      return false;
    }
    this.panelForm = null;
    this.notice = `${f.row.name} is now a panel. Choose its pages and its Ring pages in its section on this tab.`;
    try { this.onPanelMade(); } catch (_err) { /* the panel reloads on its own content event anyway */ }
    this.back();
    return true;
  }

  /* ---- managing one phone (UI split P3): doors, push ring notification, house calls, preview, revoke ---- */

  openManage(row) {
    this._gen++;
    this.notice = "";
    this.manage = {
      row, busy: false, error: "", done: "", doorList: null, doorsError: "",
      all: row.doors === null || row.doors === undefined,
      picked: new Set(Array.isArray(row.doors) ? row.doors : []),
      away: row.away_ring === true, house: row.house_receive === true,
    };
    this.view = "manage";
    this._draw();
    this._loadDoors(this.manage);
  }

  /* The door choices are the doors Portico has NOW (`portico/doors`, any class), because the server refuses an id it
   * does not know. */
  async _loadDoors(m) {
    try {
      const reply = await this.getHass().callWS({ type: "portico/doors" });
      if (this.manage !== m || this.disposed) return;
      m.doorList = ((reply && reply.doors) || []).map((d) => ({ id: String(d.door_id), name: String(d.name || d.door_id) }));
    } catch (err) {
      if (this.manage !== m || this.disposed) return;
      m.doorsError = this.classify(err).message;
    }
    if (this.view === "manage") this._draw();
  }

  /* Only what changed goes in the patch. */
  _patch(m) {
    const patch = {};
    const doors = pickedDoors(m.all, m.picked, m.doorList);
    if (!sameDoors(doors, m.row.doors)) patch.doors = doors;
    if (m.row.away_ring !== null && m.row.away_ring !== undefined && m.away !== (m.row.away_ring === true)) patch.away_ring = m.away;
    if (!m.row.legacy && m.house !== (m.row.house_receive === true)) patch.house_receive = m.house;
    return patch;
  }

  async saveManage() {
    const m = this.manage;
    if (!m || m.busy) return false;
    if (m.doorList === null && !m.all) {
      m.error = "The door list has not loaded yet.";
      this._draw();
      return false;
    }
    const patch = this._patch(m);
    if (!Object.keys(patch).length) {
      m.error = "";
      m.done = "Nothing changed.";
      this._draw();
      return false;
    }
    m.busy = true;
    m.error = "";
    m.done = "";
    this._draw();
    let reply;
    try {
      reply = await this.getHass().callWS({ type: "portico/phones/set", entry_id: m.row.entry_id, patch });
    } catch (err) {
      if (this.disposed || this.manage !== m) return false;
      m.busy = false;
      m.error = this.classify(err).message;
      this._draw();
      return false;
    }
    if (this.disposed || this.manage !== m) return false;
    m.busy = false;
    if (!reply || reply.result !== "updated" || !reply.phone) {
      m.error = "The server answered in a way this page does not understand. Reload the page.";
      this._draw();
      return false;
    }
    const row = reply.phone;
    if (this.data && Array.isArray(this.data.phones)) {
      this.data.phones = this.data.phones.map((r) => (r.entry_id === row.entry_id ? row : r));
    }
    m.row = row;
    m.all = row.doors === null || row.doors === undefined;
    m.picked = new Set(Array.isArray(row.doors) ? row.doors : []);
    m.away = row.away_ring === true;
    m.house = row.house_receive === true;
    m.done = "Saved.";
    this._draw();
    return true;
  }

  previewAsPhone() {
    const m = this.manage;
    if (m) this.previewPhone(m.row.name);
  }

  askRevoke() {
    const m = this.manage;
    if (!m || m.busy) return;
    m.error = "";
    this.view = "revoke";
    this._draw();
    const keep = this.body.querySelector('[data-act="ph-revoke-no"]');
    if (keep && typeof keep.focus === "function") keep.focus();       // the safe button has the focus, not the destructive one
  }

  /* Delete from a row's menu: the same revoke page, with the same words, reached without the Edit page. Keeping the phone
   * goes back to the list. */
  askRevokeRow(row) {
    if (this.view !== "list") return;
    this._gen++;
    this.notice = "";
    this.manage = { row, busy: false, error: "", done: "", doorList: null, doorsError: "", all: true, picked: new Set(), away: false, house: false, fromList: true };
    this.askRevoke();
  }

  cancelRevoke() {
    if (!this.manage) return;
    if (this.manage.fromList) {
      this.manage = null;
      this.view = "list";
      this._draw();
      this.load();
      return;
    }
    this.view = "manage";
    this._draw();
  }

  /* The server refuses without `confirm: true`; this page sends it only from the confirmation page's button. */
  async revoke() {
    const m = this.manage;
    if (!m || m.busy || this.view !== "revoke") return false;
    m.busy = true;
    m.error = "";
    this._draw();
    try {
      await this.getHass().callWS({ type: "portico/phones/revoke", entry_id: m.row.entry_id, confirm: true });
    } catch (err) {
      if (this.disposed || this.manage !== m) return false;
      m.busy = false;
      if (err && err.code === "unknown_phone") {                       // already gone (revoked in another tab)
        this.manage = null;
        this.notice = `${m.row.name} was already gone.`;
        this.back();
        return true;
      }
      m.error = this.classify(err).message;
      this._draw();
      return false;
    }
    if (this.disposed) return false;
    this.manage = null;
    this.notice = `Revoked ${m.row.name}.`;
    this.back();
    return true;
  }

  async create(confirmedEntryId) {
    if (this.busy) return false;
    const checked = validateName(this.form.name);
    if (!checked.ok) {
      this.formError = checked.error;
      this.fieldError = "name";
      this.view = "enrol";
      this._draw();
      return false;
    }
    this.busy = true;
    this.formError = "";
    this.fieldError = "";
    this._drawBusy();
    const msg = { type: "portico/phones/enroll", name: checked.name, allow_http: !!this.form.allow_http,
      ha_dial: (this.form.ha_dial || "").trim() };
    if (confirmedEntryId) msg.confirmed_entry_id = confirmedEntryId;
    let reply;
    try {
      reply = await this.getHass().callWS(msg);
    } catch (err) {
      if (this.disposed) return false;
      this.busy = false;
      this.formError = this.classify(err).message;
      this.fieldError = (err && err.error && err.error.field) || (err && err.field) || "";
      this.view = "enrol";
      this._draw();
      return false;
    }
    if (this.disposed) return false;
    this.busy = false;
    if (reply && reply.result === "needs_confirm") {
      this.confirm = { entry_id: String(reply.entry_id || ""), name: String(reply.name || "") };
      this.view = "confirm";
      this._draw();
      return false;
    }
    if (!reply || reply.result !== "created") {
      this.formError = "The server answered in a way this page does not understand. Reload the page.";
      this.view = "enrol";
      this._draw();
      return false;
    }
    this._startQr(reply, checked.name, confirmedEntryId);
    return true;
  }

  /* ---- the QR page ---- */

  _startQr(reply, name, confirmedEntryId) {
    this._leaveQr();
    this.qr = {
      reply, name, confirmedEntryId: confirmedEntryId || null,
      start: this._now(),          // a monotonic clock: a wall-clock change cannot move the countdown
      blobUrl: "", fetching: true, fetchError: "", expired: false, scale: undefined, cancelling: false, error: "",
    };
    this.view = "qr";
    this._draw();
    this._bindResize();
    this._tick();
    this._fetchQr(this.qr);
  }

  async _fetchQr(qr) {
    try {
      const blob = await this._fetchBlob(qr.reply.qr_token);
      if (this.qr !== qr || this.disposed) return;
      qr.blobUrl = this.win.URL.createObjectURL(blob);
    } catch (err) {
      if (this.qr !== qr || this.disposed) return;
      qr.fetchError = fetchErrorText(err);
    }
    qr.fetching = false;
    if (this.qr === qr && !qr.expired) this._drawQrBox();
  }

  /* The user's OWN bearer: `hass.fetchWithAuth` when the installed frontend has it (UNVERIFIED which versions do), else
   * the access token from `hass.auth`, refreshed first when it has expired. */
  async _fetchBlob(token) {
    return (await this._fetchAuth(QR_PATH + encodeURIComponent(token))).blob();
  }

  async _fetchText(token) {
    return (await this._fetchAuth(TEXT_PATH + encodeURIComponent(token))).text();
  }

  async _fetchAuth(path) {
    const hass = this.getHass();
    let resp;
    if (hass && typeof hass.fetchWithAuth === "function") {
      resp = await hass.fetchWithAuth(path);
    } else {
      const auth = hass && hass.auth;
      if (!auth || !auth.data) throw new Error("no login to fetch the QR with");
      if (auth.expired && typeof auth.refreshAccessToken === "function") await auth.refreshAccessToken();
      resp = await this._fetch(path, { headers: { Authorization: `Bearer ${auth.data.access_token}` }, cache: "no-store", credentials: "omit" });
    }
    if (!resp || !resp.ok) {
      const err = new Error(`HTTP ${resp && resp.status}`);
      err.status = resp && resp.status;
      throw err;
    }
    return resp;
  }

  /* "Copy enrolment text": the text the QR encodes, for the app's Settings paste field (a wall tablet that cannot scan).
   * Fetched now, with the user's own bearer; it is a secret like the QR, so it is kept only in `qr.text` (dropped with the
   * page) and shown in a read-only field only when the clipboard refuses. */
  async copyText() {
    const qr = this.qr;
    if (!qr || qr.expired || qr.copying) return false;
    qr.copying = true;
    qr.copyState = "";
    qr.text = "";
    this._drawCopy();
    let text = "";
    try {
      text = await this._fetchText(qr.reply.qr_token);
    } catch (err) {
      if (this.qr !== qr || this.disposed) return false;
      qr.copying = false;
      qr.copyState = "failed";
      qr.copyError = err && err.status === 404 ? "The text is no longer available: it expired or was cancelled. Use New code."
        : err && err.status === 403 ? "Only an administrator can copy the enrolment text."
        : `The text could not be loaded (${(err && err.message) || "network error"}).`;
      this._drawCopy();
      return false;
    }
    if (this.qr !== qr || this.disposed) return false;
    qr.copying = false;
    let copied = false;
    try {
      const nav = this.win.navigator;
      if (nav && nav.clipboard && typeof nav.clipboard.writeText === "function") {
        await nav.clipboard.writeText(text);
        copied = true;
      }
    } catch (_err) { /* refused (insecure page, no permission): the visible field below */ }
    if (this.qr !== qr || this.disposed) return false;
    if (copied) {
      qr.copyState = "copied";
      qr.text = "";
    } else {
      qr.copyState = "manual";
      qr.text = text;
    }
    this._drawCopy();
    return copied;
  }

  _drawCopy() {
    const qr = this.qr;
    const slot = this.body.querySelector('[data-slot="copy"]');
    if (!qr || !slot) return;
    slot.replaceChildren();
    if (qr.expired) return;
    const btn = this.body.querySelector('[data-act="ph-copytext"]');
    if (btn) btn.disabled = !!qr.copying;
    if (qr.copyState === "copied") {
      const ok = this._h("p", "hint", "Copied. Paste it into the app's Settings, in the enrolment field.");
      ok.dataset.state = "ph-copied";
      slot.append(ok);
    } else if (qr.copyState === "failed") {
      const bad = this._h("div", "msg err", qr.copyError);
      bad.dataset.state = "ph-copy-error";
      slot.append(bad);
    } else if (qr.copyState === "manual" && qr.text) {
      slot.append(this._h("p", "hint", "The browser would not copy it. Select the text below and copy it yourself."));
      const area = this.doc.createElement("textarea");
      area.readOnly = true;
      area.rows = 4;
      area.value = qr.text;
      area.dataset.field = "ph-enrol-text";
      area.setAttribute("aria-label", "Enrolment text");
      area.style.width = "100%";
      area.addEventListener("focus", () => { try { area.select(); } catch (_err) { /* ignore */ } });
      slot.append(area);
      try { area.focus(); } catch (_err) { /* ignore */ }
    }
  }

  remaining() {
    if (!this.qr) return 0;
    return Math.max(0, this.qr.reply.expires_in - (this._now() - this.qr.start) / 1000);
  }

  _tick() {
    this._stopTimer();
    const qr = this.qr;
    if (!qr || this.disposed) return;
    const left = this.remaining();
    if (left <= 0) {
      if (!qr.expired) {
        qr.expired = true;
        this._revoke();
        this._draw();
      }
      return;
    }
    this._drawCountdown();
    const next = left <= COUNTDOWN_STEP_MS / 1000 ? left * 1000 + 20 : COUNTDOWN_STEP_MS;
    this._timer = this._setTimer(() => this._tick(), next);
  }

  _stopTimer() {
    if (this._timer !== null) this._clearTimer(this._timer);
    this._timer = null;
  }

  _revoke() {
    if (this.qr) { this.qr.text = ""; this.qr.copyState = ""; }
    if (this.qr && this.qr.blobUrl) {
      try { this.win.URL.revokeObjectURL(this.qr.blobUrl); } catch (_err) { /* already gone */ }
      this.qr.blobUrl = "";
    }
  }

  _leaveQr() {
    this._stopTimer();
    this._unbindResize();
    this._revoke();
    this.qr = null;
  }

  async cancel() {
    const qr = this.qr;
    if (!qr || qr.cancelling) return false;
    qr.cancelling = true;
    qr.error = "";
    try {
      await this.getHass().callWS({ type: "portico/phones/cancel", entry_id: qr.reply.entry_id });
    } catch (err) {
      if (this.disposed) return false;
      qr.cancelling = false;
      // The server holds no live QR for this phone (it expired, or was scanned): there is nothing left to cancel.
      if (err && err.code === "unknown_phone") return this.back();
      qr.error = this.classify(err).message;
      this._draw();
      return false;
    }
    if (this.disposed) return false;
    this.back();
    return true;
  }

  /* New code: the same name and address again; the server reuses the waiting phone and the earlier QR dies. */
  async newCode() {
    const qr = this.qr;
    if (!qr) return false;
    this.form = { name: qr.name, allow_http: this.form.allow_http, ha_dial: this.form.ha_dial };
    return this.create(qr.confirmedEntryId);
  }

  /* ---- re-evaluated on resize and zoom ---- */

  _bindResize() {
    if (this._resizeBound) return;
    this._resizeBound = () => this._drawQrBox();
    this.win.addEventListener("resize", this._resizeBound);
  }

  _unbindResize() {
    if (!this._resizeBound) return;
    this.win.removeEventListener("resize", this._resizeBound);
    this._resizeBound = null;
  }

  /* What the QR may take, in CSS pixels, and the device pixel ratio. Overridable in tests. */
  measure() {
    const w = this.win;
    const dpr = w.devicePixelRatio || 1;
    const boxW = this.el.clientWidth || w.innerWidth || 0;
    const stacked = boxW < NARROW_PX;
    const across = stacked ? boxW : Math.floor(boxW * 0.55);
    const high = (w.innerHeight || 0) - QR_CHROME_PX;
    return { available: high > 0 ? Math.min(across, high) : across, dpr, stacked, viewport: boxW };
  }

  /* ---- drawing ---- */

  _h(tag, cls, text) {
    const node = this.doc.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  /* A three-dots menu (mdi:dots-vertical) with its own small popup: Home Assistant's menu elements are not something this
   * module can count on. The trigger carries aria-haspopup and aria-expanded; the popup is a role="menu" of role="menuitem"
   * buttons. Enter or Space on the trigger, or a click, opens it and puts the focus on the first item; Up and Down move,
   * Escape or Tab closes it (Escape returns the focus to the trigger), and so does a click anywhere else. */
  _menu(label, act, items) {
    const doc = this.doc;
    const wrap = this._h("span", "ph-menu");
    wrap.dataset.menu = act;
    const trigger = this._h("button", "ph-dots");
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
    const pop = this._h("div", "ph-pop");
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
      const b = this._h("button", item.danger ? "danger" : "", item.label);
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
        const root = wrap.getRootNode ? wrap.getRootNode() : this.doc;
        const cur = buttons.indexOf(root.activeElement);
        const i = cur < 0 ? (ev.key === "ArrowDown" ? 0 : buttons.length - 1) : (cur + (ev.key === "ArrowDown" ? 1 : -1) + buttons.length) % buttons.length;
        if (buttons[i]) buttons[i].focus();
      }
    });
    wrap.append(trigger, pop);
    return wrap;
  }

  _btn(label, onClick, { act, cls, disabled } = {}) {
    const b = this._h("button", cls || "", label);
    b.type = "button";
    if (act) b.dataset.act = act;
    if (disabled) b.disabled = true;
    else b.addEventListener("click", onClick);
    return b;
  }

  /* silent: do not tell the panel the view changed (the panel redraws the sections from inside its own render). */
  _draw(silent) {
    if (this.disposed) return;
    const body = this.body;
    body.replaceChildren();
    const view = this.view;
    if (!["list", "qr", "manage", "revoke", "panel"].includes(this.view)) body.append(this._where());   // not while scanning, nor on a phone's own pages; the list holds it itself, folded
    if (!this.perDevice && this.view === "list") {
      const app = this.aboveList();          // the push ring notification and app settings: a persistent element of the Settings module
      if (app) body.append(app);
    }
    if (this.view === "enrol") body.append(this._enrolPage());
    else if (this.view === "confirm") body.append(this._confirmPage());
    else if (this.view === "qr") body.append(this._qrPage());
    else if (this.view === "manage" && this.manage) body.append(this._managePage());
    else if (this.view === "revoke" && this.manage) body.append(this._revokePage());
    else if (this.view === "panel" && this.panelForm) body.append(this._panelPage());
    else body.append(this._listPage());
    if (this.view === "qr") {
      this._drawQrBox();
      this._drawCountdown();
    }
    this.el.dataset.phonesView = view;
    if (!silent) this.onView(view);
  }

  _where(folded) {
    const card = this._h(folded ? "details" : "div", folded ? "ph-where" : "ph-card ph-where");
    card.dataset.block = "where";
    card.append(this._h(folded ? "summary" : "h2", null, "Where is everything"));
    const list = this._h("ul");
    for (const [where, what] of WHERE_TEXT) list.append(this._h("li", null, `${where}: ${what}`));
    card.append(list);
    const links = this._h("div", "ph-sum");
    for (const [href, text] of WHERE_LINKS) {
      const a = this._h("a", null, text);
      a.setAttribute("href", href);
      links.append(a, " ");
    }
    card.append(links);
    return card;
  }

  _summary() {
    const h = this.header;
    const bits = [];
    if (h.push_entry === false) bits.push("Portico's settings entry is missing.");
    else {
      const mode = h.tailscale_mode ? `Tailscale mode: ${h.tailscale_mode}${h.tailscale_complete ? "" : " (not finished)"}.` : "Tailscale is not set up.";
      bits.push(mode);
      if (typeof h.pending === "number") bits.push(`${h.pending} of ${h.pending_cap} QR codes waiting.`);
      if (h.ha_dial) bits.push(`Phones dial ${h.ha_dial} over Tailscale.`);
    }
    return bits.join(" ");
  }

  _listPage() {
    const card = this._listCard();
    if (!this.perDevice) card.append(this._where(true));
    return card;
  }

  _listCard() {
    const doc = this.doc;
    const card = this._h("div", "ph-card");
    card.dataset.view = "list";
    card.append(this._h("h2", null, this.perDevice ? "Devices" : "Phones"));
    const bar = this._h("div", "ph-bar");
    bar.append(this._btn("Enrol a device", () => this.openEnrol(), { act: "ph-enrol", cls: "primary" }));
    bar.append(this._btn(this.loading ? "Reading..." : "Refresh", () => { this.notice = ""; this.load(); }, { act: "ph-refresh", disabled: this.loading }));
    const sum = this._h("span", "ph-sum", this.data ? this._summary() : "");
    sum.dataset.field = "ph-summary";
    bar.append(sum);
    card.append(bar);
    const status = statusLine(this.getHass() && this.getHass().states);
    if (status) {
      // Optional and read-only (design 3.2): the call state and the listen leg as Home Assistant reports them.
      const line = this._h("p", "ph-sum", `Gate: ${status}.`);
      line.dataset.field = "ph-status";
      card.append(line);
    }
    if (this.notice) {
      const note = this._h("div", "msg ok", this.notice);
      note.dataset.state = "ph-notice";
      card.append(note);
    }
    if (this.listError) {
      const err = this._h("div", "msg err", this.listError);
      err.dataset.state = "ph-error";
      card.append(err);
    }
    if (!this.data) {
      if (!this.listError) card.append(this._h("p", "empty", "Reading the phone list..."));
      return card;
    }
    const phones = this.data.phones || [];
    if (!phones.length) {
      card.append(this._h("p", "empty", "No device is enrolled yet. Use Enrol a device."));
      return card;
    }
    if (this.perDevice) {
      // Schema 6: not a table. The card is only the top (Enrol, Refresh, notices); each phone is its own section below it.
      const sections = this._h("div", "ph-sections");
      sections.dataset.role = "phone-sections";
      const sorted = phones.slice().sort((a, b) => String(a.name).localeCompare(String(b.name)));
      for (const row of sorted) sections.append(this._phoneSection(row));
      card.dataset.view = "list";
      card.append(sections);
      return card;
    }
    const table = doc.createElement("table");
    const head = doc.createElement("thead");
    const hr = doc.createElement("tr");
    for (const t of ["Phone", "State", "Enrolled", "Last connection", "App registered", "Doors", "Push ring notification", "House calls", ""]) {
      hr.append(this._h("th", null, t));
    }
    head.append(hr);
    table.append(head);
    const tbody = doc.createElement("tbody");
    for (const row of phones) tbody.append(this._phoneRow(row));
    table.append(tbody);
    card.append(table);
    return card;
  }

  /* The menu a phone has, on its row (before schema 6) and in its section header (schema 6). */
  _phoneMenu(row) {
    const items = [{ label: "Edit", act: "ph-row-manage", run: () => this.openManage(row) }];
    if (!row.legacy && (row.state !== "enrolled")) items.push({ label: "New code", act: "ph-row-newcode", run: () => this.openEnrol(row.name) });
    if (this.perDevice && canMakePanel(row)) items.push({ label: "Make a panel", act: "ph-row-panel", run: () => this.openMakePanel(row) });
    items.push({ label: "Delete", act: "ph-row-delete", danger: true, run: () => this.askRevokeRow(row) });
    return this._menu(`Actions for ${row.name}`, "ph-row-menu", items);
  }

  /* Schema 6: one section for one phone. Header: its name, the kind chip, the menu on the right. Under it: what the list row
   * used to say (state, last connection...), then the phone's own settings from the panel (Pages, Ring pages). */
  _phoneSection(row) {
    const sec = this._h("section", "ph-card ph-section");
    sec.dataset.device = "phone";
    sec.dataset.phone = row.entry_id;
    const head = this._h("div", "ph-head");
    head.append(this._h("h2", "ph-title", row.name));
    const kind = this._h("span", "ph-chip", "Phone");
    kind.dataset.chip = "kind";
    head.append(kind);
    if (row.panel === true) {
      const pc = this._h("span", "ph-chip ok", "Panel");
      pc.dataset.chip = "panel";
      head.append(pc);
    }
    const grow = this._h("span", "ph-grow");
    head.append(grow, this._phoneMenu(row));
    sec.append(head);
    const state = this._h("p", "ph-facts");
    const chip = this._h("span", "ph-chip " + (row.state === "enrolled" ? "ok" : row.state === "compromised" || row.expired ? "bad" : "warn"), stateText(row));
    chip.dataset.chip = row.expired ? "expired" : row.state;
    state.append(chip);
    sec.append(state);
    const facts = this._h("dl", "ph-grid");
    const fact = (label, value) => { facts.append(this._h("dt", null, label), this._h("dd", null, value)); };
    fact("Enrolled", localDateTime(row.enrolled_at, this.locale) || "Not known");
    fact("Last connection", row.last_connection ? localDateTime(row.last_connection, this.locale) : "Never");
    fact("App registered", row.push_registered === true ? "Registered" : row.push_registered === false ? "Not registered" : "Not known");
    fact("Doors", doorsText(row.doors));
    fact("Push ring notification", onOff(row.away_ring));
    fact("House calls", onOff(row.house_receive));
    sec.append(facts);
    const body = this.deviceBody(row);
    if (body) sec.append(body);
    return sec;
  }

  _phoneRow(row) {
    const tr = this.doc.createElement("tr");
    tr.dataset.phone = row.entry_id;
    const cell = (label, value, cls) => {
      const td = this._h("td", cls || "", value);
      td.dataset.label = label;
      tr.append(td);
      return td;
    };
    cell("Phone", row.name, "ph-name");
    const state = this._h("td");
    state.dataset.label = "State";
    const chip = this._h("span", "ph-chip " + (row.state === "enrolled" ? "ok" : row.state === "compromised" || row.expired ? "bad" : "warn"), stateText(row));
    chip.dataset.chip = row.expired ? "expired" : row.state;
    state.append(chip);
    tr.append(state);
    cell("Enrolled", localDateTime(row.enrolled_at, this.locale) || "Not known");
    cell("Last connection", row.last_connection ? localDateTime(row.last_connection, this.locale) : "Never");
    cell("App registered", row.push_registered === true ? "Registered" : row.push_registered === false ? "Not registered" : "Not known");
    cell("Doors", doorsText(row.doors));
    cell("Push ring notification", onOff(row.away_ring));
    cell("House calls", onOff(row.house_receive));
    const acts = this._h("td", "ph-name-less ph-actions");
    acts.dataset.label = "Actions";
    acts.append(this._phoneMenu(row));
    tr.append(acts);
    return tr;
  }

  _enrolPage() {
    const h = this.header;
    const card = this._h("div", "ph-card");
    card.dataset.view = "enrol";
    card.append(this._h("h2", null, "Enrol a device"));
    const blocked = this._blocker();
    if (blocked) {
      const box = this._h("div", "msg err", blocked);
      box.dataset.state = "ph-blocked";
      card.append(box, this._btn("Back", () => this.back(), { act: "ph-back" }));
      return card;
    }
    card.append(this._h("p", "hint", "The phone scans a QR code that works once. The phone gets its own Home Assistant login; nothing is shared with the people of the house."));
    const form = this._h("div", "ph-form");
    const nameLabel = this._h("label", null, `Phone name (1 to ${NAME_MAX} characters)`);
    const name = this.doc.createElement("input");
    name.type = "text";
    name.maxLength = NAME_MAX;      // the server stops at 24 after stripping
    name.value = this.form.name;
    name.dataset.field = "ph-name";
    name.addEventListener("input", () => { this.form.name = name.value; });
    nameLabel.append(name);
    const dialLabel = this._h("label", null, "Address the phone dials over Tailscale (optional; empty = automatic)");
    const dial = this.doc.createElement("input");
    dial.type = "text";
    dial.value = this.form.ha_dial;
    dial.placeholder = "10.6.0.5:443";
    dial.dataset.field = "ph-ha-dial";
    dial.addEventListener("input", () => { this.form.ha_dial = dial.value; });
    dialLabel.append(dial);
    const remembered = this._h("span", "hint", h.ha_dial
      ? `Remembered from the last phone: ${h.ha_dial}. Empty this field to forget it.`
      : "Nothing is remembered yet.");
    dialLabel.append(remembered);
    const httpLabel = this._h("label", "ph-check");
    const http = this.doc.createElement("input");
    http.type = "checkbox";
    http.checked = !!this.form.allow_http;
    http.dataset.field = "ph-allow-http";
    http.addEventListener("change", () => { this.form.allow_http = http.checked; });
    httpLabel.append(http, "Allow plain http on my own network");
    form.append(nameLabel, dialLabel, httpLabel);
    if (this.formError) {
      const err = this._h("div", "msg err", this.formError);
      err.dataset.state = "ph-error";
      if (this.fieldError) err.dataset.errorField = this.fieldError;
      form.append(err);
    }
    const acts = this._h("div", "ph-acts");
    acts.append(this._btn(this.busy ? "Creating..." : "Create the QR", () => this.create(), { act: "ph-create", cls: "primary", disabled: this.busy }));
    acts.append(this._btn("Back", () => this.back(), { act: "ph-back" }));
    form.append(acts);
    card.append(form);
    return card;
  }

  /* Why the form cannot be used, from the list's header; "" when it can (the server stays the authority). */
  _blocker() {
    const h = this.header;
    if (h.push_entry === false) return "Portico's settings entry is missing, so phones cannot be enrolled. Add Portico under Settings > Devices & services.";
    if (h.enrol_here === false) {
      if (h.tailscale_mode === "approve") return "This install enrols phones in approve mode, which needs your approval on Tailscale's own page for each phone. Do it in the Home Assistant dialog: Settings > Devices & services > Portico > Configure > Enroll a device.";
      return "Finish Tailscale setup in the Home Assistant dialog first (Settings > Devices & services > Portico > Configure). Then come back here.";
    }
    return "";
  }

  _drawBusy() {
    const button = this.body.querySelector('[data-act="ph-create"]');
    if (button) { button.disabled = true; button.textContent = "Creating..."; }
  }

  _confirmPage() {
    const card = this._h("div", "ph-card");
    card.dataset.view = "confirm";
    card.append(this._h("h2", null, "This phone is already enrolled"));
    const who = this.confirm ? this.confirm.name : "";
    card.append(this._h("p", null, `A phone named "${who}" is already enrolled. Enrolling again gives it a new QR; once the new QR is scanned, the old access is replaced. Continue?`));
    const acts = this._h("div", "ph-acts");
    acts.append(this._btn("Enrol it again", () => this.create(this.confirm && this.confirm.entry_id), { act: "ph-confirm-yes", cls: "primary" }));
    acts.append(this._btn("Back", () => { this.view = "enrol"; this._draw(); }, { act: "ph-confirm-no" }));
    card.append(acts);
    return card;
  }

  _qrPage() {
    const qr = this.qr;
    const r = qr.reply;
    const card = this._h("div", "ph-card");
    card.dataset.view = "qr";
    card.append(this._h("h2", null, `QR for ${qr.name}`));
    const wrap = this._h("div", "ph-qrwrap");
    const box = this._h("div", "ph-qrslot");
    box.dataset.slot = "qr";
    const text = this._h("div", "ph-qrtext");
    const count = this._h("p", "ph-count");
    count.dataset.field = "ph-countdown";
    count.setAttribute("aria-live", "off");
    text.append(count);
    const until = this._h("p", "hint", qr.expired ? "" : `Works once, until ${localTime(r.expires_at, this.locale)}. Open the Portico app on the phone and scan this code.`);
    until.dataset.field = "ph-until";
    text.append(until);
    if (r.dial_target) {
      const addr = this._h("p", null, `Address in the QR: ${r.dial_target}. The phone must be able to reach it (over Tailscale or the tunnel).`);
      addr.dataset.field = "ph-address";
      text.append(addr);
    }
    if (r.modules >= DENSE_MODULES) {
      // Found by decoding with ZXing 3.5.3 (the app's decoder): codes of 89 modules and up (a long name, or a long address or key)
      // are often not found by its detector, at any scale; 85 modules (a name up to about 36) read every time tried.
      const dense = this._h("p", "hint", "This code is denser than usual because of the long name. If the app does not read it, enrol again with a shorter name.");
      dense.dataset.state = "ph-dense";
      text.append(dense);
    }
    const next = this.nextStep();
    if (next) {
      const step = this._h("p", "hint", next);
      step.dataset.field = "ph-next-step";
      text.append(step);
    }
    const notes = this._h("ul", "ph-notes");
    notes.dataset.field = "ph-notes";
    for (const code of r.notes || []) {
      const li = this._h("li", null, NOTE_TEXT[code] || `Note: ${code}`);
      li.dataset.note = code;
      notes.append(li);
    }
    if ((r.notes || []).length) text.append(notes);
    if (qr.error) {
      const err = this._h("div", "msg err", qr.error);
      err.dataset.state = "ph-error";
      text.append(err);
    }
    if (!qr.expired) {
      {
        const copy = this._h("div", "ph-copy");
        const row = this._h("div", "ph-acts");
        row.append(this._btn("Copy enrolment text", () => this.copyText(), { act: "ph-copytext", disabled: !!qr.copying }));
        copy.append(row);
        copy.append(this._h("p", "hint", "For a tablet that cannot scan: paste it into the app's Settings. Valid 10 minutes, works once. Treat it like a password. It holds no Home Assistant token."));
        const out = this._h("div", null);
        out.dataset.slot = "copy";
        copy.append(out);
        text.append(copy);
      }
    }
    const acts = this._h("div", "ph-acts");
    acts.append(this._btn("Cancel", () => this.cancel(), { act: "ph-cancel", disabled: qr.cancelling || qr.expired }));
    acts.append(this._btn("New code", () => this.newCode(), { act: "ph-newcode", disabled: this.busy }));
    acts.append(this._btn("Done", () => this.back(), { act: "ph-done" }));
    text.append(acts);
    wrap.append(box, text);
    card.append(wrap);
    return card;
  }

  _check(label, checked, onChange, { field, disabled } = {}) {
    const wrap = this._h("label", "ph-door");
    const box = this.doc.createElement("input");
    box.type = "checkbox";
    box.checked = !!checked;
    if (disabled) box.disabled = true;
    if (field) box.dataset.field = field;
    box.addEventListener("change", () => onChange(box.checked));
    wrap.append(box, label);
    return wrap;
  }

  _managePage() {
    const m = this.manage;
    const row = m.row;
    const card = this._h("div", "ph-card");
    card.dataset.view = "manage";
    card.append(this._h("h2", null, row.name));
    card.append(this._h("p", "ph-sum", `${stateText(row)}${row.legacy ? ". Enrolled before phone entries: only its push ring notification can be changed here." : ""}`));
    const form = this._h("div", "ph-form");

    // doors
    const doors = this._h("div");
    doors.append(this._h("h2", null, "Doors this phone may open"));
    doors.append(this._check("All doors", m.all, (v) => { m.all = v; this._draw(); }, { field: "ph-doors-all", disabled: row.legacy }));
    if (!m.all) {
      const sub = this._h("div", "ph-sub");
      sub.dataset.field = "ph-doors";
      if (m.doorsError) {
        const e = this._h("div", "msg err", m.doorsError);
        e.dataset.state = "ph-doors-error";
        sub.append(e);
      } else if (m.doorList === null) {
        sub.append(this._h("span", "hint", "Reading the doors..."));
      } else {
        const known = new Set(m.doorList.map((d) => d.id));
        for (const d of m.doorList) {
          sub.append(this._check(d.name, m.picked.has(d.id), (v) => { if (v) m.picked.add(d.id); else m.picked.delete(d.id); },
            { field: `ph-door:${d.id}` }));
        }
        // A stored door that Portico no longer has stays ticked and named, so it can be unticked; the server refuses it.
        for (const id of Array.from(m.picked).filter((id) => !known.has(id))) {
          sub.append(this._check(`${id} (Portico does not have this door now: untick it)`, true,
            (v) => { if (v) m.picked.add(id); else m.picked.delete(id); }, { field: `ph-door:${id}` }));
        }
        if (!m.doorList.length) sub.append(this._h("span", "hint", "Portico has no doors right now."));
        if (!m.picked.size) sub.append(this._h("span", "hint", "No door ticked: this phone could open none."));
      }
      doors.append(sub);
    }
    form.append(doors);

    // push ring notification
    const ring = this._h("div");
    ring.append(this._h("h2", null, "Push ring notification"));
    const registered = row.away_ring !== null && row.away_ring !== undefined;
    ring.append(this._check("Ring this phone when someone calls the gate and it is away from home", m.away, (v) => { m.away = v; },
      { field: "ph-away", disabled: !registered }));
    if (!registered) ring.append(this._h("span", "hint", "Open the Portico app on this phone once so it registers; the switch is here after that."));
    form.append(ring);

    // house calls
    if (!row.legacy) {
      const house = this._h("div");
      house.append(this._h("h2", null, "House calls"));
      house.append(this._check("Receive house calls in Portico", m.house, (v) => { m.house = v; }, { field: "ph-house" }));
      const hint = this._h("span", "hint", houseHint());
      hint.dataset.field = "ph-house-hint";
      house.append(hint);
      form.append(house);
    }

    if (m.error) {
      const err = this._h("div", "msg err", m.error);
      err.dataset.state = "ph-error";
      form.append(err);
    }
    if (m.done) {
      const ok = this._h("div", "msg ok", m.done);
      ok.dataset.state = "ph-saved";
      form.append(ok);
    }
    const acts = this._h("div", "ph-acts");
    acts.append(this._btn(m.busy ? "Saving..." : "Save", () => this.saveManage(), { act: "ph-save", cls: "primary", disabled: m.busy }));
    if (!row.legacy && this.canPreview(row.name)) {
      acts.append(this._btn("Preview its screen", () => this.previewAsPhone(), { act: "ph-preview" }));
    }
    acts.append(this._btn("Back", () => this.back(), { act: "ph-back" }));
    form.append(acts);
    card.append(form);

    const danger = this._h("div");
    danger.append(this._h("h2", null, "Remove this phone"));
    danger.append(this._btn("Revoke this phone...", () => this.askRevoke(), { act: "ph-revoke", cls: "danger", disabled: m.busy }));
    card.append(danger);
    return card;
  }

  _panelPage() {
    const f = this.panelForm;
    const card = this._h("div", "ph-card");
    card.dataset.view = "panel";
    card.append(this._h("h2", null, `Make "${f.row.name}" a panel`));
    card.append(this._h("p", "hint", "A panel is a phone that stays on a wall. It keeps its login, its push registration and its house calls, and it has its own pages and Ring pages, chosen in its section like a phone's (one page is the usual case). The doors that ring it come from the door Talk elements on its pages; every door may be answered from it."));
    const form = this._h("div", "ph-form");
    if (f.error) {
      const err = this._h("div", "msg err", f.error);
      err.dataset.state = "ph-error";
      if (f.field) err.dataset.errorField = f.field;
      form.append(err);
    }
    const acts = this._h("div", "ph-acts");
    acts.append(this._btn(f.busy ? "Making..." : "Make a panel", () => this.makePanel(), { act: "ph-panel-make", cls: "primary", disabled: f.busy }));
    acts.append(this._btn("Back", () => this.back(), { act: "ph-back", disabled: f.busy }));
    form.append(acts);
    card.append(form);
    return card;
  }

  _revokePage() {
    const m = this.manage;
    const card = this._h("div", "ph-card");
    card.dataset.view = "revoke";
    card.setAttribute("role", "alertdialog");
    card.setAttribute("aria-label", `Revoke ${m.row.name}`);
    card.append(this._h("h2", null, `Revoke "${m.row.name}"?`));
    card.append(this._h("p", null, "This cannot be undone. The phone has to be enrolled again with a new QR. Revoking destroys:"));
    const list = this._h("ul", "ph-list");
    list.dataset.field = "ph-consequences";
    for (const line of revokeConsequences(m.row)) list.append(this._h("li", null, line));
    card.append(list);
    if (m.error) {
      const err = this._h("div", "msg err", m.error);
      err.dataset.state = "ph-error";
      card.append(err);
    }
    const acts = this._h("div", "ph-acts");
    // The safe button is first, so Tab from the card reaches it before the destructive one.
    acts.append(this._btn("Keep this phone", () => this.cancelRevoke(), { act: "ph-revoke-no", disabled: m.busy }));
    acts.append(this._btn(m.busy ? "Revoking..." : "Revoke it", () => this.revoke(), { act: "ph-revoke-yes", cls: "danger", disabled: m.busy }));
    card.append(acts);
    return card;
  }

  _drawCountdown() {
    const count = this.body.querySelector('[data-field="ph-countdown"]');
    if (!count || !this.qr) return;
    const left = this.remaining();
    count.textContent = this.qr.expired || left <= 0 ? "This code has expired. Use New code." : `${formatCountdown(left)} left`;
  }

  /* The QR itself: an image at an integer device-pixel scale, or the reason there is none. */
  _drawQrBox() {
    const qr = this.qr;
    const slot = this.body.querySelector('[data-slot="qr"]');
    if (!qr || !slot) return;
    slot.replaceChildren();
    if (qr.expired) return;
    if (qr.fetchError) {
      slot.append(this._h("div", "ph-qrnone", qr.fetchError));
      return;
    }
    if (qr.fetching || !qr.blobUrl) {
      slot.append(this._h("div", "ph-qrnone", "Loading the QR..."));
      return;
    }
    const m = this.measure();
    const scale = qrScale(m.available, qr.reply.modules, m.dpr);
    qr.scale = scale;
    if (!scale) {
      const note = this._h("div", "ph-qrnone", "This window is too small to draw the QR so a phone can read it. Scan it from another screen: open this page on a laptop or a larger window.");
      note.dataset.state = "ph-too-small";
      slot.append(note);
      return;
    }
    const img = this.doc.createElement("img");
    img.alt = "Enrolment QR code";
    img.src = qr.blobUrl;
    img.draggable = false;
    img.style.width = `${scale.css}px`;
    img.style.height = `${scale.css}px`;
    img.style.imageRendering = "pixelated";
    img.dataset.k = String(scale.k);
    const holder = this._h("div", "ph-qrbox");
    holder.append(img);
    slot.append(holder);
    if (m.stacked) {
      const narrow = this._h("p", "hint", "On a narrow screen like this one, scan the QR from another screen.");
      narrow.dataset.state = "ph-narrow";
      narrow.style.maxWidth = `${scale.css}px`;
      slot.append(narrow);
    }
  }
}

function fetchErrorText(err) {
  const status = err && err.status;
  if (status === 403) return "Only an administrator can show the QR.";
  if (status === 404) return "The QR is no longer available: it expired or was cancelled. Use New code.";
  return `The QR could not be loaded (${(err && err.message) || "network error"}). Use New code to try again.`;
}
