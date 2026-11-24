// UI split P2 and P3: the Phones tab (portico-phones.js) inside the panel: the persistent element, the tab bar in every phase,
// the list, the Enrol page, the QR fetched as a blob with the user's own bearer, the integer-scale rule, the countdown.
// Protocol-level, in jsdom with a fake `hass`; NOT a visual check (a real browser is the acceptance test).
import assert from "assert";
import fs from "fs";
import { loadPanel, makeHass, editorHass, importPhones, makeLoaded, sleep } from "./panel_loader.mjs";

const PHONES = await importPhones();
const { qrScale, formatCountdown, validateName, doorsText, onOff, stateText, NOTE_TEXT, QR_PATH, TEXT_PATH,
  revokeConsequences, statusLine, pickedDoors, sameDoors, houseHint } = PHONES;

// ---- 1. the QR scale: an INTEGER number of DEVICE pixels per module -------------------------------------------------
{
  // the rule: k = clamp(floor(available_device_px / (modules + 8)), 3, 6); css width = (modules + 8) * k / dpr
  for (const modules of [57, 85, 89, 93]) {
    for (const dpr of [1, 1.25, 1.5, 2, 2.625, 3]) {
      for (let avail = 100; avail <= 1400; avail += 37) {
        const s = qrScale(avail, modules, dpr);
        const expectK = Math.min(6, Math.floor(Math.floor(avail * dpr) / (modules + 8)));
        if (expectK < 3) { assert.strictEqual(s, null, `${modules}/${dpr}/${avail}: too small to draw`); continue; }
        assert.ok(s, `${modules}/${dpr}/${avail}`);
        assert.strictEqual(s.k, expectK);
        assert.ok(Number.isInteger(s.k) && s.k >= 3 && s.k <= 6, "k is an integer from 3 to 6");
        assert.ok(Number.isInteger(s.device), "the image is a whole number of DEVICE pixels");
        assert.strictEqual(s.device, (modules + 8) * s.k);
        assert.ok(Math.abs(s.css - (modules + 8) * s.k / dpr) < 1e-9, "css width = (modules + 8) * k / dpr");
        assert.ok(Math.abs(s.css * dpr - s.device) < 1e-9, "css px * dpr is exactly whole device pixels");
        assert.ok(s.device <= Math.floor(avail * dpr) || s.k === 3 && false, "it never exceeds what is available");
      }
    }
  }
  // named viewports: the laptop of the design (1280 x 768, ~240 px of chrome), the phone, a hi-dpi laptop, a zoomed one
  assert.strictEqual(qrScale(768 - 240, 85, 1).k, 5, "1280x768 at 100%: k=5, 465 px, not 6 (558 px does not fit)");
  assert.strictEqual(qrScale(768 - 240, 85, 1).css, 465);
  assert.strictEqual(qrScale(1080 - 240, 85, 1).k, 6, "a 1080p screen reaches k=6");
  assert.strictEqual(qrScale(358, 85, 2.625).k, 6, "a 390 px phone (dpr 2.625) can draw k=6: 212 css px");
  assert.ok(Math.abs(qrScale(358, 85, 2.625).css - 93 * 6 / 2.625) < 1e-9);
  assert.strictEqual(qrScale(270, 85, 1), null, "270 css px at dpr 1 is k=2: no QR, say so");
  assert.strictEqual(qrScale(279, 85, 1).k, 3, "k=3 is the smallest drawn");
  assert.strictEqual(qrScale(278, 85, 1), null);
  assert.strictEqual(qrScale(465, 85, 1.5).k, 6 > Math.floor(Math.floor(465 * 1.5) / 93) ? Math.floor(Math.floor(465 * 1.5) / 93) : 6);
  assert.strictEqual(qrScale(0, 85, 1), null);
  assert.strictEqual(qrScale(500, 0, 1), null);
  assert.strictEqual(qrScale(500, 85, 0).k, 5, "a missing dpr is 1");
  assert.strictEqual(qrScale(1400, 85, 1).k, 6, "never above 6");
  console.log("PASS: the QR scale is an integer number of device pixels, 3 to 6, for every viewport and pixel ratio");
}

// ---- 2. small pure helpers ------------------------------------------------------------------------------------------
{
  assert.strictEqual(formatCountdown(600), "10 min");
  assert.strictEqual(formatCountdown(599.4), "10 min");
  assert.strictEqual(formatCountdown(591), "10 min");
  assert.strictEqual(formatCountdown(590), "9 min 50 s");
  assert.strictEqual(formatCountdown(61), "1 min 10 s");
  assert.strictEqual(formatCountdown(9), "10 s");
  assert.strictEqual(formatCountdown(0), "0 s");
  assert.strictEqual(formatCountdown(-5), "0 s");
  assert.deepStrictEqual(validateName("  Pixel  "), { ok: true, name: "Pixel" }, "stripped");
  assert.strictEqual(PHONES.NAME_MAX, 24, "the limit is 24 (Alice, 2026-10-23)");
  assert.strictEqual(validateName("   ").ok, false);
  assert.strictEqual(validateName("").ok, false);
  assert.strictEqual(validateName("x".repeat(24)).ok, true);
  assert.strictEqual(validateName("x".repeat(25)).ok, false);
  assert.strictEqual(validateName("ט".repeat(24)).ok, true, "24 characters, counted as the server counts them");
  assert.strictEqual(validateName("😀".repeat(24)).ok, true, "by code point, not UTF-16 unit");
  assert.strictEqual(validateName(undefined).ok, false);
  assert.strictEqual(doorsText(null), "All doors");
  assert.strictEqual(doorsText(["a"]), "1 door");
  assert.strictEqual(doorsText(["a", "b"]), "2 doors");
  assert.strictEqual(onOff(true), "On");
  assert.strictEqual(onOff(false), "Off");
  assert.strictEqual(onOff(null), "Not known");
  assert.match(stateText({ state: "pending", expired: true }), /make a new code/);
  assert.match(stateText({ state: "pending", expired: false }), /Waiting/);
  assert.match(stateText({ state: "compromised" }), /Flagged/);
  assert.strictEqual(stateText({ state: "enrolled" }), "Enrolled");
  console.log("PASS: the name, countdown and row helpers");
}

// ---- 3. no innerHTML anywhere in the Phones module (static) ---------------------------------------------------------
{
  const src = fs.readFileSync(new URL("../portico-phones.js", import.meta.url), "utf8");
  for (const bad of ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"]) {
    assert.ok(!src.includes(bad), `portico-phones.js must not use ${bad}`);
  }
  const panelSrc = fs.readFileSync(new URL("../portico-panel.js", import.meta.url), "utf8");
  assert.ok(!panelSrc.includes("innerHTML"), "the panel never used it and still does not");
  console.log("PASS: user text only goes in through textContent (no innerHTML in either file)");
}

// ---- fixtures -------------------------------------------------------------------------------------------------------

const HEADER = { push_entry: true, tailscale_mode: "auto", tailscale_complete: true, enrol_here: true, ha_dial: "10.6.0.5:443",
  pending: 1, pending_cap: 5, cap_reached: false };
const ROWS = [
  { entry_id: "e-alice", legacy: false, name: "Alice's Pixel", state: "enrolled", expired: false, code_expires_in: null,
    enrolled_at: "2026-10-08T10:00:00+00:00", last_connection: "2026-10-22T09:30:00+00:00", push_registered: true,
    doors: null, away_ring: true, house_receive: false },
  { entry_id: "e-yael", legacy: false, name: "Yael <b>bold</b>", state: "pending", expired: false, code_expires_in: 300,
    enrolled_at: "2026-10-23T07:00:00+00:00", last_connection: null, push_registered: false, doors: ["d1", "d2"],
    away_ring: null, house_receive: true },
  { entry_id: "e-old", legacy: false, name: "Lost code", state: "pending", expired: true, code_expires_in: null,
    enrolled_at: "2026-10-22T07:00:00+00:00", last_connection: null, push_registered: null, doors: null, away_ring: null,
    house_receive: false },
  { entry_id: "legacy-abc", legacy: true, name: "Old Samsung", state: "enrolled", expired: false, code_expires_in: null,
    enrolled_at: "2026-09-01T10:00:00+00:00", last_connection: null, push_registered: null, doors: null, away_ring: null,
    house_receive: false },
];
const DOORS = [{ door_id: "d1", name: "Gate" }, { door_id: "d2", name: "Front door" }, { door_id: "d3", name: "Garage" }];
const patchToRow = (patch) => ({ ...("doors" in patch ? { doors: patch.doors } : {}), ...("away_ring" in patch ? { away_ring: patch.away_ring } : {}),
  ...("house_receive" in patch ? { house_receive: patch.house_receive } : {}) });
const CREATED = { result: "created", entry_id: "e-new", qr_token: "TOKEN-0123456789", modules: 85, expires_in: 600,
  expires_at: "2026-10-23T08:10:00+00:00", notes: ["manual_tailscale"], dial_target: "10.6.0.5:443", created: true };

/* handlers for the three phones commands; `state` is mutable so a test can change what the server says. */
function phonesHandlers(state = {}) {
  state.header = state.header || { ...HEADER };
  state.rows = state.rows || ROWS.map((r) => ({ ...r }));
  state.enroll = state.enroll || (() => ({ ...CREATED }));
  state.cancel = state.cancel || (() => ({ result: "cancelled", removed: true }));
  state.doors = state.doors || DOORS;
  state.revoke = state.revoke || (() => ({ result: "revoked", legacy: false }));
  state.set = state.set || ((msg) => {            // a server that keeps what it was told
    const i = state.rows.findIndex((r) => r.entry_id === msg.entry_id);
    state.rows[i] = { ...state.rows[i], ...patchToRow(msg.patch) };
    return { result: "updated", phone: state.rows[i] };
  });
  return {
    state,
    handlers: {
      "portico/phones/list": () => ({ header: state.header, phones: state.rows }),
      "portico/phones/enroll": (msg) => state.enroll(msg),
      "portico/phones/cancel": (msg) => state.cancel(msg),
      "portico/phones/revoke": (msg) => state.revoke(msg),
      "portico/phones/set": (msg) => state.set(msg),
      "portico/doors": () => ({ doors: state.doors }),
    },
  };
}

/* The bearer-header fetch: `hass.fetchWithAuth` (the frontend's) when given, else `hass.auth` and the window's fetch. */
const ENROL_TEXT = '{"ha_url":"https://x","code":"SECRET-CODE","phone_name":"Yael","enroll_id":"abc"}';
function qrFetchFakes(win, { status = 200 } = {}) {
  const log = { fetches: [], created: [], revoked: [], refreshes: 0 };
  win.URL.createObjectURL = (blob) => { log.created.push(blob); return `blob:fake/${log.created.length}`; };
  win.URL.revokeObjectURL = (url) => { log.revoked.push(url); };
  const respond = () => ({ ok: status === 200, status, blob: async () => ({ type: "image/png", size: 1578 }), text: async () => ENROL_TEXT });
  log.fetchWithAuth = async (path, init) => { log.fetches.push({ via: "fetchWithAuth", path, init }); return respond(); };
  win.fetch = async (path, init) => { log.fetches.push({ via: "fetch", path, init }); return respond(); };
  return log;
}

async function mount(opts = {}) {
  const w = loadPanel({ phones: opts.phones, settings: opts.settings, elements: opts.elements });
  const win = w.dom.window;
  const doc = w.doc;
  const ph = phonesHandlers(opts.state || {});
  const log = qrFetchFakes(win, opts);
  const handlers = { ...ph.handlers, ...(opts.handlers || {}) };
  const fake = editorHass(handlers, { user: opts.user, states: opts.states, loaded: opts.loaded });
  if (opts.withFetchWithAuth !== false) fake.hass.fetchWithAuth = log.fetchWithAuth;
  if (opts.auth) fake.hass.auth = opts.auth(log);
  const panel = doc.createElement("portico-panel");
  panel._previewMs = 20;
  panel.hass = fake.hass;
  doc.body.appendChild(panel);
  await sleep(60);
  return { ...w, win, panel, fake, log, state: ph.state, ctl: () => panel._phones };
}
const q = (p, sel) => p.shadowRoot.querySelector(sel);
const qa = (p, sel) => Array.from(p.shadowRoot.querySelectorAll(sel));
const act = (p, name) => q(p, `[data-act="${name}"]`);
const field = (p, name) => q(p, `[data-field="${name}"]`);
const text = (p) => p.shadowRoot.textContent;
const click = (win, node) => { assert.ok(node, "clicked something that is not there"); node.dispatchEvent(new win.Event("click", { bubbles: true })); };
const type = (win, node, value, ev = "input") => { assert.ok(node, "no such field"); node.value = value; node.dispatchEvent(new win.Event(ev, { bubbles: true })); };
const types = (fake, t) => fake.sent.filter((m) => m.type === t);

async function openPhones(m) {
  click(m.win, act(m.panel, "tab-devices"));
  await sleep(20);
}
async function toEnrol(m, name) {
  await openPhones(m);
  click(m.win, act(m.panel, "ph-enrol"));
  if (name !== undefined) type(m.win, field(m.panel, "ph-name"), name);
}

// ---- 4. the tab: present for an administrator, absent without the module, and the content tabs never depend on it ----------
{
  const m = await mount();
  assert.deepStrictEqual(qa(m.panel, "header .tab").map((b) => b.textContent), ["Elements", "Pages", "Devices", "Ring & call", "Settings", "Status"]);
  assert.ok(act(m.panel, "tab-devices"));
  assert.strictEqual(types(m.fake, "portico/phones/list").length, 0, "nothing is asked until the tab is opened");
  await openPhones(m);
  assert.strictEqual(types(m.fake, "portico/phones/list").length, 1, "the list is read when the tab opens");
  // Devices is a content tab (the Screens section is a draft): the Save bar stays, and sub-pages of the Phones part hide it
  for (const there of ["undo", "discard", "save"]) assert.ok(act(m.panel, there), `${there} is shown on the Devices tab (the Screens draft)`);
  assert.strictEqual(q(m.panel, ".page").dataset.devview, "list");
  assert.ok(q(m.panel, '[data-section="screens"]'), "the Screens section is on the same tab");
  assert.ok(q(m.panel, '[data-view="list"]'), "the Phones section");
  const heads = qa(m.panel, ".devices h2").map((h) => h.textContent);
  assert.ok(heads.indexOf("Phones") >= 0 && heads.indexOf("Phones") < heads.indexOf("Screens"), `order: ${heads}`);
  click(m.win, act(m.panel, "ph-enrol"));
  assert.strictEqual(q(m.panel, ".page").dataset.devview, "enrol", "a sub-page marks the page so the Screens section and the Save bar are hidden");
  click(m.win, act(m.panel, "ph-back") || act(m.panel, "ph-cancel") || qa(m.panel, ".ph-acts button").pop());
  await sleep(20);
  assert.strictEqual(q(m.panel, ".page").dataset.devview, "list");
  assert.ok(q(m.panel, '[data-block="where"]'), "the 'Where is everything' block is folded into the Phones section");
  assert.match(q(m.panel, '[data-block="where"]').textContent, /the doors \(gate, front door\)/);
  assert.doesNotMatch(q(m.panel, '[data-block="where"]').textContent, /Portico Intercom/);
  assert.match(q(m.panel, '[data-block="where"]').textContent, /this panel/);
  // back to a content tab: the header is the content header again
  click(m.win, act(m.panel, "tab-pages"));
  assert.ok(act(m.panel, "save") && act(m.panel, "undo") && act(m.panel, "discard"));
  console.log("PASS: the Devices tab has House calls, Phones and Screens in that order; sub-pages hide the Screens section and the Save bar");
}
{
  // a non-administrator never gets the module, the tab or a phones call
  const m = await mount({ user: { is_admin: false }, handlers: { "portico/content/get": () => ({ endpoint: "e", kind: "phone", pages: [], callers: {}, roster: [] }) } });
  assert.strictEqual(act(m.panel, "tab-devices"), null);
  assert.strictEqual(m.ctl(), null);
  assert.strictEqual(types(m.fake, "portico/phones/list").length, 0);
  console.log("PASS: a non-administrator gets no Phones tab and no phones call");
}
{
  // the module fails to load: no Phones tab, the content tabs work exactly as before
  const m = await mount({ phones: false, settings: false, elements: false });
  assert.deepStrictEqual(qa(m.panel, "header .tab").map((b) => b.textContent), ["Pages", "Devices", "Ring & call"]);
  assert.strictEqual(m.ctl(), null);
  click(m.win, act(m.panel, "tab-devices"));
  assert.ok(act(m.panel, "add-screen"), "the Screens tab still works");
  click(m.win, act(m.panel, "tab-pages"));
  assert.ok(act(m.panel, "save"));
  console.log("PASS: when the Phones module cannot be imported the tab is absent and the content tabs are unharmed");
}
{
  // the URL the panel imports: the hashed one from HA's panel config, else the bare path
  const w = loadPanel();
  const seen = [];
  const panel = w.doc.createElement("portico-panel");
  panel._importPhones = (url) => { seen.push(url); return Promise.reject(new Error("no")); };
  panel.panel = { config: { phones_module: "/portico/portico-phones.js?v=abc123def456" } };
  panel.hass = editorHass({}).hass;
  w.doc.body.appendChild(panel);
  await sleep(40);
  assert.deepStrictEqual(seen, ["/portico/portico-phones.js?v=abc123def456"]);
  const bare = w.doc.createElement("portico-panel");
  const seen2 = [];
  bare._importPhones = (url) => { seen2.push(url); return Promise.reject(new Error("no")); };
  bare.hass = editorHass({}).hass;
  w.doc.body.appendChild(bare);
  await sleep(40);
  assert.deepStrictEqual(seen2, ["/portico/portico-phones.js"]);
  console.log("PASS: the panel imports the content-hashed module URL from its config, the bare path otherwise");
}

// ---- 5. the persistent element survives every re-render ------------------------------------------------------------------
{
  const m = await mount();
  await openPhones(m);
  const el = m.ctl().el;
  assert.ok(el.isConnected && m.panel.shadowRoot.contains(el));
  click(m.win, act(m.panel, "ph-enrol"));
  type(m.win, field(m.panel, "ph-name"), "Half typed");
  const input = field(m.panel, "ph-name");
  // everything that makes the panel call _renderNow(): _render, a theme flip, a content change, a tab round trip, _load
  m.panel._render();
  assert.strictEqual(m.ctl().el, el);
  assert.ok(el.isConnected && m.panel.shadowRoot.contains(el), "re-attached after replaceChildren");
  assert.strictEqual(field(m.panel, "ph-name"), input, "the very same input node: nothing was rebuilt");
  assert.strictEqual(input.value, "Half typed", "what was typed survives");
  m.fake.hass.themes = { darkMode: true };
  m.panel.hass = m.fake.hass;                                                         // set hass -> _themeCheck -> _render
  assert.ok(m.panel.shadowRoot.contains(el) && field(m.panel, "ph-name").value === "Half typed");
  m.fake.fire({ type: "content_changed" });
  await sleep(40);
  assert.ok(m.panel.shadowRoot.contains(el) && field(m.panel, "ph-name").value === "Half typed", "a content change redraws the panel, not the form");
  click(m.win, act(m.panel, "tab-pages"));
  assert.ok(!m.panel.shadowRoot.contains(el), "off the tab the element is simply not placed");
  click(m.win, act(m.panel, "tab-devices"));
  await sleep(20);
  assert.strictEqual(m.ctl().el, el);
  assert.ok(m.panel.shadowRoot.contains(el));
  assert.strictEqual(field(m.panel, "ph-name").value, "Half typed", "and it is as it was left");
  console.log("PASS: the persistent element survives _render, a theme flip, a content change and a tab round trip, with its form");
}

// ---- 6. the list --------------------------------------------------------------------------------------------------------------
{
  const m = await mount();
  await openPhones(m);
  const rows = qa(m.panel, "[data-phone]");
  assert.deepStrictEqual(rows.map((r) => r.dataset.phone), ["e-alice", "e-yael", "e-old", "legacy-abc"]);
  const row = (id) => q(m.panel, `[data-phone="${id}"]`);
  assert.match(row("e-alice").textContent, /Alice's Pixel/);
  assert.match(row("e-alice").textContent, /Enrolled/);
  assert.match(row("e-alice").textContent, /Registered/);
  assert.match(row("e-alice").textContent, /All doors/);
  assert.match(row("e-yael").textContent, /Waiting for the first scan/);
  assert.match(row("e-yael").textContent, /2 doors/);
  assert.match(row("e-old").textContent, /Code expired: make a new code/);
  assert.strictEqual(row("e-old").querySelector("[data-chip]").dataset.chip, "expired");
  assert.match(row("legacy-abc").textContent, /Old Samsung/);
  // a New code button for a phone still waiting, never for an enrolled or legacy one
  assert.ok(row("e-yael").querySelector('[data-act="ph-row-newcode"]') && row("e-old").querySelector('[data-act="ph-row-newcode"]'));
  assert.strictEqual(row("e-alice").querySelector('[data-act="ph-row-newcode"]'), null);
  assert.strictEqual(row("legacy-abc").querySelector('[data-act="ph-row-newcode"]'), null);
  // the summary line from the header
  assert.match(field(m.panel, "ph-summary").textContent, /Tailscale mode: auto/);
  assert.match(field(m.panel, "ph-summary").textContent, /1 of 5 QR codes waiting/);
  assert.match(field(m.panel, "ph-summary").textContent, /10\.6\.0\.5:443/);
  // user text is text: a name that is markup stays inert
  assert.strictEqual(qa(m.panel, "b").length, 0, "no <b> element came out of a phone's name");
  assert.ok(row("e-yael").textContent.includes("Yael <b>bold</b>"));
  // narrow-width rule: the stylesheet turns table rows into cards and labels every cell
  const css = q(m.panel, ".phones style").textContent;
  assert.match(css, /@media \(max-width: 720px\)/);
  assert.match(css, /td::before \{ content: attr\(data-label\)/);
  assert.ok(qa(m.panel, "[data-phone] td").every((td) => td.dataset.label !== undefined), "every cell carries its label for the card layout");
  assert.match(q(m.panel, "style").textContent, /\.tabs \{ display: flex; gap: 8px; overflow-x: auto/, "the tab bar scrolls");
  console.log("PASS: the list shows every row's state, doors and registration; a hostile name stays text; rows become cards when narrow");
}
{
  // a list that cannot be read says so with the panel's own words; Refresh asks again
  let fail = true;
  const m = await mount({ handlers: { "portico/phones/list": () => { if (fail) throw { code: "unknown", message: "boom" }; return { header: HEADER, phones: [] }; } } });
  await openPhones(m);
  assert.match(q(m.panel, '[data-state="ph-error"]').textContent, /Something went wrong on the server/);
  fail = false;
  click(m.win, act(m.panel, "ph-refresh"));
  await sleep(20);
  assert.strictEqual(q(m.panel, '[data-state="ph-error"]'), null);
  assert.match(text(m.panel), /No device is enrolled yet/);
  console.log("PASS: a failed list is a message, and Refresh recovers");
}
{
  // a server part that does not have the commands yet says what to do
  const m = await mount({ handlers: { "portico/phones/list": () => { throw { code: "unknown_command", message: "Unknown command." }; } } });
  await openPhones(m);
  assert.match(q(m.panel, '[data-state="ph-error"]').textContent, /Restart Home Assistant/);
  console.log("PASS: an old server part says restart Home Assistant");
}

// ---- 7. the Enrol page ------------------------------------------------------------------------------------------------------
{
  const m = await mount();
  await toEnrol(m);
  const dial = field(m.panel, "ph-ha-dial");
  assert.strictEqual(dial.value, "10.6.0.5:443", "ha_dial is PREFILLED with the remembered value");
  assert.match(text(m.panel), /Remembered from the last phone: 10\.6\.0\.5:443\. Empty this field to forget it\./);
  assert.strictEqual(field(m.panel, "ph-name").value, "");
  assert.strictEqual(field(m.panel, "ph-name").maxLength, 24, "the input stops at 24 characters");
  assert.strictEqual(field(m.panel, "ph-allow-http").checked, false);
  // an empty or over-long name is refused here and nothing is sent
  click(m.win, act(m.panel, "ph-create"));
  assert.match(q(m.panel, '[data-state="ph-error"]').textContent, /needs a name/);
  assert.strictEqual(types(m.fake, "portico/phones/enroll").length, 0);
  type(m.win, field(m.panel, "ph-name"), "x".repeat(25));
  click(m.win, act(m.panel, "ph-create"));
  assert.match(q(m.panel, '[data-state="ph-error"]').textContent, /longer than 24/);
  assert.strictEqual(types(m.fake, "portico/phones/enroll").length, 0);
  // stripped, and exactly the wire the server expects
  type(m.win, field(m.panel, "ph-name"), "  Yael's Pixel  ");
  type(m.win, field(m.panel, "ph-allow-http"), "", "change");
  click(m.win, act(m.panel, "ph-create"));
  await sleep(30);
  assert.deepStrictEqual(types(m.fake, "portico/phones/enroll"), [
    { type: "portico/phones/enroll", name: "Yael's Pixel", allow_http: false, ha_dial: "10.6.0.5:443" }]);
  console.log("PASS: the Enrol page prefills ha_dial, strips and checks the name, and sends exactly the documented message");
}
{
  // a blank ha_dial is sent as a blank (that FORGETS it); the box ticked is sent as allow_http: true
  const m = await mount();
  await toEnrol(m, "Pixel");
  type(m.win, field(m.panel, "ph-ha-dial"), "");
  const box = field(m.panel, "ph-allow-http");
  box.checked = true;
  box.dispatchEvent(new m.win.Event("change", { bubbles: true }));
  click(m.win, act(m.panel, "ph-create"));
  await sleep(30);
  assert.deepStrictEqual(types(m.fake, "portico/phones/enroll")[0], { type: "portico/phones/enroll", name: "Pixel", allow_http: true, ha_dial: "" });
  console.log("PASS: a blank ha_dial is sent blank (forget), plain http is sent when ticked");
}
{
  // with no remembered address the field is empty and says so
  const m = await mount({ state: { header: { ...HEADER, ha_dial: null } } });
  await toEnrol(m, "Pixel");
  assert.strictEqual(field(m.panel, "ph-ha-dial").value, "");
  assert.match(text(m.panel), /Nothing is remembered yet/);
  console.log("PASS: nothing remembered, nothing prefilled");
}
{
  // a phone's name that is markup is sent as typed and drawn as text
  const m = await mount();
  await toEnrol(m, "<img src=x onerror=a()>");
  click(m.win, act(m.panel, "ph-create"));
  await sleep(40);
  assert.strictEqual(types(m.fake, "portico/phones/enroll")[0].name, "<img src=x onerror=a()>");
  assert.strictEqual(qa(m.panel, "img").filter((i) => i.getAttribute("src") === "x").length, 0, "no element was made from the name");
  assert.ok(text(m.panel).includes("QR for <img src=x onerror=a()>"));
  console.log("PASS: a name that is markup is text on the QR page");
}
{
  // blocked: approve mode and an unfinished Tailscale setup say what to do, with no form
  for (const [header, re] of [
    [{ ...HEADER, tailscale_mode: "approve", enrol_here: false }, /approve mode/],
    [{ ...HEADER, tailscale_mode: null, tailscale_complete: false, enrol_here: false }, /Finish Tailscale setup in the Home Assistant dialog/],
    [{ ...HEADER, tailscale_mode: "auto", tailscale_complete: false, enrol_here: false }, /Finish Tailscale setup in the Home Assistant dialog/],
    [{ ...HEADER, push_entry: false, tailscale_mode: null, enrol_here: false }, /settings entry is missing/],
  ]) {
    const m = await mount({ state: { header } });
    await toEnrol(m);
    assert.match(q(m.panel, '[data-state="ph-blocked"]').textContent, re);
    assert.strictEqual(field(m.panel, "ph-name"), null, "no form when it cannot be used");
    assert.ok(act(m.panel, "ph-back"));
  }
  console.log("PASS: approve mode and an unfinished Tailscale answer say what to do in the Home Assistant dialog");
}
{
  // the server's refusals, in the panel's words, with the field kept
  const cases = [
    [{ code: "invalid_ha_dial", message: "x", field: "ha_dial" }, /not one the phone can dial/, "ha_dial"],
    [{ code: "pending_cap", message: "x" }, /Five phones are already waiting/, ""],
    [{ code: "needs_tailscale_setup", message: "x" }, /Finish the Tailscale setup/, ""],
    [{ code: "needs_approval", message: "x" }, /approve mode/, ""],
    [{ code: "no_ha_url", message: "x" }, /no address to put in the QR/, ""],
    [{ code: "no_https_url", message: "x" }, /plain http/, ""],
    [{ code: "tailscale_mint_failed", message: "x" }, /did not give a key/, ""],
    [{ code: "no_push_entry", message: "x" }, /settings entry is missing/, ""],
    [{ code: "bad_name", message: "x", field: "phone_name" }, /1 to 24 characters/, "phone_name"],
    [{ code: "unknown", message: "x" }, /Something went wrong on the server/, ""],
    [{ code: "unauthorized", message: "Unauthorized" }, /Only an administrator/, ""],
    [{ code: "unknown_command", message: "Unknown command." }, /Restart Home Assistant/, ""],
  ];
  for (const [error, re, fieldName] of cases) {
    const m = await mount({ state: { enroll: () => { throw error; } } });
    await toEnrol(m, "Pixel");
    click(m.win, act(m.panel, "ph-create"));
    await sleep(30);
    const box = q(m.panel, '[data-state="ph-error"]');
    assert.ok(box, error.code);
    assert.match(box.textContent, re, error.code);
    assert.strictEqual(box.dataset.errorField || "", fieldName, error.code);
    assert.strictEqual(field(m.panel, "ph-name").value, "Pixel", "the form keeps what was typed");
    assert.strictEqual(q(m.panel, "img"), null);
  }
  console.log("PASS: every typed error of the server has its own words in the panel, with the field it names");
}

// ---- 8. needs_confirm ------------------------------------------------------------------------------------------------------------
{
  const asked = [];
  const m = await mount({ state: { enroll: (msg) => {
    asked.push(msg);
    return msg.confirmed_entry_id ? { ...CREATED, entry_id: "e-alice", created: false, notes: ["reenroll_long_lived", "manual_tailscale"] }
      : { result: "needs_confirm", entry_id: "e-alice", name: "Alice's Pixel" };
  } } });
  await toEnrol(m, "Alice's Pixel");
  click(m.win, act(m.panel, "ph-create"));
  await sleep(30);
  assert.ok(q(m.panel, '[data-view="confirm"]'), "a confirmation, not an error");
  assert.strictEqual(q(m.panel, '[data-state="ph-error"]'), null);
  assert.match(text(m.panel), /A phone named "Alice's Pixel" is already enrolled/);
  assert.strictEqual(q(m.panel, "img"), null, "no QR yet");
  // No: back to the form, nothing re-sent
  click(m.win, act(m.panel, "ph-confirm-no"));
  assert.ok(field(m.panel, "ph-name"));
  assert.strictEqual(asked.length, 1);
  click(m.win, act(m.panel, "ph-create"));
  await sleep(30);
  click(m.win, act(m.panel, "ph-confirm-yes"));
  await sleep(40);
  assert.strictEqual(asked.length, 3);
  assert.strictEqual(asked[2].confirmed_entry_id, "e-alice", "re-sent with the id of the phone named");
  assert.ok(!("confirmed_entry_id" in asked[0]) && !("confirmed_entry_id" in asked[1]));
  assert.ok(q(m.panel, '[data-view="qr"]'), "then the QR page");
  assert.match(q(m.panel, '[data-field="ph-notes"]').textContent, /enrolled before one-time codes/);
  // New code keeps the confirmation it was given
  click(m.win, act(m.panel, "ph-newcode"));
  await sleep(40);
  assert.strictEqual(asked.length, 4);
  assert.strictEqual(asked[3].confirmed_entry_id, "e-alice");
  console.log("PASS: needs_confirm names the phone, and confirming re-sends with its id (also for New code)");
}

// ---- 9. the QR page: the blob fetch with the user's own bearer ---------------------------------------------------------------------
async function qrWorld(opts = {}) {
  const m = await mount(opts);
  const ctl = () => m.ctl();
  await openPhones(m);
  click(m.win, act(m.panel, "ph-enrol"));
  type(m.win, field(m.panel, "ph-name"), opts.name || "Yael's Pixel");
  // a laptop: 1280 x 768 at 100%, the box the panel gives the tab is 1200 px wide
  m.win.innerWidth = opts.width || 1280;
  m.win.innerHeight = opts.height || 768;
  m.win.devicePixelRatio = opts.dpr || 1;
  Object.defineProperty(ctl().el, "clientWidth", { configurable: true, get: () => opts.box || (opts.width || 1280) - 80 });
  click(m.win, act(m.panel, "ph-create"));
  await sleep(40);
  return m;
}
{
  const m = await qrWorld();
  assert.ok(q(m.panel, '[data-view="qr"]'));
  assert.deepStrictEqual(m.log.fetches.map((f) => [f.via, f.path]), [["fetchWithAuth", `${QR_PATH}TOKEN-0123456789`]],
    "fetched once, by the frontend's own authenticated fetch");
  const img = q(m.panel, ".ph-qrbox img");
  assert.ok(img, "the QR is drawn");
  assert.strictEqual(img.getAttribute("src"), "blob:fake/1", "from an object URL");
  assert.strictEqual(m.log.created.length, 1);
  assert.ok(!text(m.panel).includes("TOKEN-0123456789") && !img.outerHTML.includes("TOKEN"), "the token is in no visible text or attribute");
  // 1280x768 at 100%: k=5, 465 css px, image-rendering pixelated, on a white box
  assert.strictEqual(img.dataset.k, "5");
  assert.strictEqual(img.style.width, "465px");
  assert.strictEqual(img.style.height, "465px");
  assert.strictEqual(img.style.imageRendering, "pixelated");
  const css = q(m.panel, ".phones style").textContent;
  assert.match(css, /\.ph-qrbox \{ background: #fff; color: #000; color-scheme: light; filter: none; forced-color-adjust: none;/, "white, never inverted");
  assert.match(css, /image-rendering: pixelated/);
  assert.strictEqual(img.alt, "Enrolment QR code");
  // the text beside it
  assert.match(q(m.panel, '[data-field="ph-address"]').textContent, /Address in the QR: 10\.6\.0\.5:443/);
  assert.match(q(m.panel, '[data-field="ph-notes"]').textContent, /Manual Tailscale mode: this QR carries no Tailscale key/);
  assert.match(q(m.panel, '[data-field="ph-until"]').textContent, /Works once, until \d\d:\d\d/);
  assert.strictEqual(field(m.panel, "ph-countdown").getAttribute("aria-live"), "off");
  assert.match(field(m.panel, "ph-countdown").textContent, /^(10 min|9 min 50 s) left$/);
  assert.ok(act(m.panel, "ph-cancel") && act(m.panel, "ph-newcode"));
  assert.strictEqual(q(m.panel, '[data-state="ph-narrow"]'), null, "no narrow-screen note on a laptop");
  assert.strictEqual(q(m.panel, '[data-block="where"]'), null, "the QR page gives its room to the QR");
  console.log("PASS: the QR is fetched once as a blob through fetchWithAuth and drawn pixelated at k=5 on white beside its text");
}
{
  // the fallback: no hass.fetchWithAuth -> hass.auth.data.access_token, with the bearer header, after a refresh when expired
  let auth;
  const m = await qrWorld({
    withFetchWithAuth: false,
    auth: (log) => {
      auth = {
        data: { access_token: "OLD-ACCESS-TOKEN" }, expired: true,
        refreshAccessToken: async () => { log.refreshes++; auth.data.access_token = "NEW-ACCESS-TOKEN"; auth.expired = false; },
      };
      return auth;
    },
  });
  assert.strictEqual(m.log.refreshes, 1, "an expired token is refreshed first");
  assert.strictEqual(m.log.fetches.length, 1);
  const f = m.log.fetches[0];
  assert.strictEqual(f.via, "fetch");
  assert.strictEqual(f.path, `${QR_PATH}TOKEN-0123456789`);
  assert.strictEqual(f.init.headers.Authorization, "Bearer NEW-ACCESS-TOKEN", "the user's own bearer, the refreshed one");
  assert.strictEqual(f.init.cache, "no-store");
  assert.strictEqual(f.init.credentials, "omit");
  assert.ok(!f.path.includes("NEW-ACCESS") && !JSON.stringify(m.fake.sent).includes("ACCESS-TOKEN"), "the bearer is never in a URL or a websocket frame");
  assert.ok(q(m.panel, ".ph-qrbox img"));
  console.log("PASS: without fetchWithAuth the panel uses hass.auth's access token, refreshed when expired, as a bearer header");
}
{
  // an unexpired token is not refreshed; no login at all is a message, not a crash
  const m = await qrWorld({ withFetchWithAuth: false, auth: (log) => ({ data: { access_token: "TOKEN-A" }, expired: false, refreshAccessToken: async () => { log.refreshes++; } }) });
  assert.strictEqual(m.log.refreshes, 0);
  assert.strictEqual(m.log.fetches[0].init.headers.Authorization, "Bearer TOKEN-A");
  const n = await qrWorld({ withFetchWithAuth: false });
  assert.match(q(n.panel, ".ph-qrnone").textContent, /The QR could not be loaded/);
  assert.strictEqual(q(n.panel, ".ph-qrbox img"), null);
  console.log("PASS: a live token is used as it is; no login is a message");
}
{
  // 403 and 404 are explained; no image
  for (const [status, re] of [[403, /Only an administrator can show the QR/], [404, /no longer available/], [500, /could not be loaded/]]) {
    const m = await qrWorld({ status });
    assert.match(q(m.panel, ".ph-qrnone").textContent, re, String(status));
    assert.strictEqual(q(m.panel, ".ph-qrbox img"), null);
    assert.ok(act(m.panel, "ph-newcode"), "New code is there to try again");
  }
  console.log("PASS: 403 / 404 / errors of the QR fetch are said, never drawn");
}

{
  // a dense code (89 modules and up) carries the hint; the usual 85 does not
  const dense = await qrWorld({ state: { enroll: () => ({ ...CREATED, modules: 89 }) } });
  assert.match(q(dense.panel, '[data-state="ph-dense"]').textContent, /shorter name/);
  const usual = await qrWorld();
  assert.strictEqual(q(usual.panel, '[data-state="ph-dense"]'), null);
  console.log("PASS: a code of 89 modules or more says a shorter name reads more reliably");
}

// ---- 9b. "Copy enrolment text" -------------------------------------------------------------------------------------------------------
{
  const m = await qrWorld();
  const clip = [];
  Object.defineProperty(m.win.navigator, "clipboard", { configurable: true, value: { writeText: async (t) => { clip.push(t); } } });
  assert.ok(!JSON.stringify(m.log.fetches).includes("/text/"), "the text is not fetched until the button is pressed");
  assert.ok(!text(m.panel).includes("SECRET-CODE"), "the text is not on the page before the press");
  assert.match(text(m.panel), /Valid 10 minutes, works once\. Treat it like a password\. It holds no Home Assistant token/);
  click(m.win, act(m.panel, "ph-copytext"));
  await sleep(40);
  assert.deepStrictEqual(m.log.fetches.map((f) => f.path).slice(1), [`/api/portico/enroll/text/TOKEN-0123456789`], "fetched with the user's own bearer path");
  assert.deepStrictEqual(clip, [ENROL_TEXT], "exactly the served text goes to the clipboard");
  assert.ok(q(m.panel, '[data-state="ph-copied"]'));
  assert.ok(!text(m.panel).includes("SECRET-CODE") && !q(m.panel, '[data-field="ph-enrol-text"]'), "copied: nothing secret is left on the page");
  assert.ok(!JSON.stringify(m.fake.sent).includes("SECRET-CODE"), "never a websocket frame");
  console.log("PASS: Copy enrolment text fetches the text on demand and writes exactly it to the clipboard");
}
{
  // the clipboard refuses: a visible read-only field, selected
  const m = await qrWorld();
  Object.defineProperty(m.win.navigator, "clipboard", { configurable: true, value: { writeText: async () => { throw new Error("denied"); } } });
  click(m.win, act(m.panel, "ph-copytext"));
  await sleep(40);
  const area = field(m.panel, "ph-enrol-text");
  assert.ok(area && area.readOnly && area.value === ENROL_TEXT, "a read-only field holds the text");
  assert.ok(!q(m.panel, '[data-state="ph-copied"]'));
  click(m.win, act(m.panel, "ph-done"));
  assert.ok(!q(m.panel, '[data-field="ph-enrol-text"]'), "leaving the page drops it");
  console.log("PASS: a refused clipboard falls back to a read-only field with the text");
}
{
  // no clipboard API at all, and a 404 / 403 are said
  const m = await qrWorld();
  click(m.win, act(m.panel, "ph-copytext"));
  await sleep(40);
  assert.ok(field(m.panel, "ph-enrol-text"), "no navigator.clipboard: the field");
  for (const [status, re] of [[404, /no longer available/], [403, /Only an administrator/]]) {
    const n = await qrWorld();
    n.fake.hass.fetchWithAuth = async (path) => (path.includes("/text/") ? { ok: false, status } : { ok: true, status: 200, blob: async () => ({}) });
    click(n.win, act(n.panel, "ph-copytext"));
    await sleep(40);
    assert.match(q(n.panel, '[data-state="ph-copy-error"]').textContent, re, String(status));
    assert.ok(!q(n.panel, '[data-field="ph-enrol-text"]'));
  }
  console.log("PASS: no clipboard API shows the field; 403 and 404 of the text fetch are said");
}

// ---- 10. the scale on screen: laptop, phone, zoom, resize ---------------------------------------------------------------------------
{
  const cases = [
    // [name, innerWidth, innerHeight, dpr, box clientWidth, expected k or null, css px]
    ["laptop 1280x768 @100%", 1280, 768, 1, 1200, 5, 465],
    // browser zoom changes devicePixelRatio AND the CSS viewport (a 1280x768 screen at 125% is 1024x614 CSS px)
    ["laptop 1280x768 screen, zoom 125% (1024x614 css, dpr 1.25)", 1024, 614, 1.25, 944, 5, 93 * 5 / 1.25],
    ["laptop 1280x768 screen, zoom 150% (853x512 css, dpr 1.5)", 853, 512, 1.5, 773, 4, 93 * 4 / 1.5],
    ["laptop 1280x768 screen, zoom 200% (640x384 css, dpr 2)", 640, 384, 2, 560, 3, 93 * 3 / 2],
    ["laptop 1280x768 screen, zoom 300% (427x256 css, dpr 3): too small", 427, 256, 3, 347, null, null],
    ["laptop 1280x768 screen, zoom 50% (2560x1536 css, dpr 0.5)", 2560, 1536, 0.5, 2480, 6, 93 * 6 / 0.5],
    ["laptop 1920x1080 @100%", 1920, 1080, 1, 1800, 6, 558],
    ["phone 390x844 dpr 2.625", 390, 844, 2.625, 358, 6, 93 * 6 / 2.625],
    ["tablet 800x1280 dpr 2", 800, 1280, 2, 760, 6, 93 * 6 / 2],
    ["a small window 400x400 @100%", 400, 400, 1, 360, null, null],
  ];
  for (const [name, w, h, dpr, box, k, css] of cases) {
    const m = await qrWorld({ width: w, height: h, dpr, box });
    const img = q(m.panel, ".ph-qrbox img");
    if (k === null) {
      assert.strictEqual(img, null, `${name}: no QR below k=3`);
      assert.match(q(m.panel, '[data-state="ph-too-small"]').textContent, /Scan it from another screen/, name);
    } else {
      assert.ok(img, name);
      assert.strictEqual(Number(img.dataset.k), k, name);
      assert.ok(Math.abs(parseFloat(img.style.width) - css) < 1e-6, `${name}: ${img.style.width} vs ${css}`);
      assert.ok(Number.isInteger(Math.round(parseFloat(img.style.width) * dpr * 1e6) / 1e6), `${name}: whole device pixels`);
    }
    if (w < 720 && img) assert.ok(q(m.panel, '[data-state="ph-narrow"]'), `${name}: a narrow screen is told to scan from another screen`);
  }
  console.log("PASS: the QR is drawn at the integer scale each viewport allows; below k=3 it says 'scan from another screen'");
}
{
  // re-evaluated on resize and zoom
  const m = await qrWorld();
  const img0 = q(m.panel, ".ph-qrbox img");
  assert.strictEqual(img0.dataset.k, "5");
  m.win.innerHeight = 1080;
  m.win.dispatchEvent(new m.win.Event("resize"));
  assert.strictEqual(q(m.panel, ".ph-qrbox img").dataset.k, "6", "a taller window gives a bigger module");
  m.win.devicePixelRatio = 1.5;                                  // the browser zoomed to 150%
  m.win.innerWidth = 853;
  Object.defineProperty(m.ctl().el, "clientWidth", { configurable: true, get: () => 800 });
  m.win.innerHeight = 512;
  m.win.dispatchEvent(new m.win.Event("resize"));
  const z = q(m.panel, ".ph-qrbox img");
  assert.ok(z, "still drawn at 150% zoom");
  assert.ok(Math.abs(parseFloat(z.style.width) * 1.5 - Number(z.dataset.k) * 93) < 1e-6, "whole device pixels at the new ratio");
  m.win.innerHeight = 300;
  m.win.dispatchEvent(new m.win.Event("resize"));
  assert.strictEqual(q(m.panel, ".ph-qrbox img"), null, "squeezed below k=3: the QR goes, the reason appears");
  assert.ok(q(m.panel, '[data-state="ph-too-small"]'));
  m.win.innerHeight = 1080;
  m.win.dispatchEvent(new m.win.Event("resize"));
  assert.ok(q(m.panel, ".ph-qrbox img"), "and comes back");
  // leaving the QR page releases the listener
  click(m.win, act(m.panel, "ph-done"));
  await sleep(20);
  m.win.innerHeight = 400;
  m.win.dispatchEvent(new m.win.Event("resize"));
  assert.strictEqual(q(m.panel, ".ph-qrbox img"), null);
  console.log("PASS: the scale is re-evaluated on resize and zoom, and the listener goes when the page does");
}

// ---- 11. the countdown ---------------------------------------------------------------------------------------------------------------------
{
  const m = await qrWorld();
  const ctl = m.ctl();
  // a controllable monotonic clock and timer; the panel's own timers are replaced from here on
  let now = ctl.qr.start;
  const timers = [];
  ctl._now = () => now;
  ctl._setTimer = (fn, ms) => { timers.push({ fn, ms }); return timers.length; };
  ctl._clearTimer = () => {};
  const tick = () => ctl._tick();
  tick();
  assert.strictEqual(field(m.panel, "ph-countdown").textContent, "10 min left");
  assert.strictEqual(timers.at(-1).ms, 10000, "the text is rewritten every 10 seconds");
  now += 135 * 1000;
  tick();
  assert.strictEqual(field(m.panel, "ph-countdown").textContent, "7 min 50 s left");
  now += 430 * 1000;                                              // 565 s gone: 35 s left
  tick();
  assert.strictEqual(field(m.panel, "ph-countdown").textContent, "40 s left");
  now += 29 * 1000;                                                // 6 s left: the next wake is at the end, not 10 s later
  tick();
  assert.strictEqual(field(m.panel, "ph-countdown").textContent, "10 s left");
  assert.ok(timers.at(-1).ms < 7000 && timers.at(-1).ms >= 6000, `wakes when the code ends: ${timers.at(-1).ms}`);
  assert.strictEqual(m.log.revoked.length, 0);
  now += 6 * 1000 + 1;                                            // +1 ms: start is a fractional performance.now(), and 600 s of float additions can land a hair short of the end
  tick();
  assert.match(field(m.panel, "ph-countdown").textContent, /This code has expired/);
  assert.strictEqual(q(m.panel, ".ph-qrbox img"), null, "the QR is gone at expiry");
  assert.deepStrictEqual(m.log.revoked, ["blob:fake/1"], "and its object URL is revoked");
  assert.strictEqual(act(m.panel, "ph-cancel").disabled, true, "nothing to cancel any more");
  assert.ok(act(m.panel, "ph-newcode") && !act(m.panel, "ph-newcode").disabled);
  const stopped = timers.length;
  tick();
  assert.strictEqual(timers.length, stopped, "no timer once it has expired");
  console.log("PASS: the countdown runs on a monotonic clock, rewrites its text every 10 s, and the QR and blob go at expiry");
}
{
  // a wall-clock change cannot move it: the countdown never reads Date
  const src = fs.readFileSync(new URL("../portico-phones.js", import.meta.url), "utf8");
  const remaining = src.slice(src.indexOf("remaining() {"), src.indexOf("_tick() {"));
  assert.ok(!remaining.includes("Date"), "remaining() reads the monotonic clock only");
  console.log("PASS: the countdown does not read the wall clock");
}

// ---- 12. Cancel, New code, Done; blob hygiene ---------------------------------------------------------------------------------------------------
{
  const m = await qrWorld();
  click(m.win, act(m.panel, "ph-cancel"));
  await sleep(40);
  assert.deepStrictEqual(types(m.fake, "portico/phones/cancel"), [{ type: "portico/phones/cancel", entry_id: "e-new" }]);
  assert.deepStrictEqual(m.log.revoked, ["blob:fake/1"], "the blob URL is revoked at cancel");
  assert.ok(q(m.panel, '[data-view="list"]'), "back to the list");
  assert.ok(types(m.fake, "portico/phones/list").length >= 2, "and the list is read again");
  assert.strictEqual(m.ctl().qr, null);
  console.log("PASS: Cancel tells the server, drops the QR and its blob, and goes back to a fresh list");
}
{
  // a cancel the server says is already moot (the phone scanned it) still ends the page; any other failure stays visible
  const m = await qrWorld({ state: { cancel: () => { throw { code: "unknown_phone", message: "x" }; } } });
  click(m.win, act(m.panel, "ph-cancel"));
  await sleep(40);
  assert.ok(q(m.panel, '[data-view="list"]'));
  const n = await qrWorld({ state: { cancel: () => { throw { code: "unknown", message: "x" }; } } });
  click(n.win, act(n.panel, "ph-cancel"));
  await sleep(40);
  assert.ok(q(n.panel, '[data-view="qr"]'), "the QR is still live on the server: it stays on screen");
  assert.match(q(n.panel, '[data-state="ph-error"]').textContent, /Something went wrong on the server/);
  assert.ok(q(n.panel, ".ph-qrbox img"));
  console.log("PASS: a moot cancel ends the page, a failed one keeps the QR and says so");
}
{
  // New code: the same name and address again, a new token, the old blob released
  let n = 0;
  const m = await qrWorld({ state: { enroll: () => ({ ...CREATED, qr_token: `TOKEN-${++n}`, created: n === 1 }) } });
  click(m.win, act(m.panel, "ph-newcode"));
  await sleep(50);
  const sent = types(m.fake, "portico/phones/enroll");
  assert.strictEqual(sent.length, 2);
  assert.deepStrictEqual({ ...sent[1] }, { ...sent[0] }, "the same message");
  assert.deepStrictEqual(m.log.fetches.map((f) => f.path), [`${QR_PATH}TOKEN-1`, `${QR_PATH}TOKEN-2`]);
  assert.deepStrictEqual(m.log.revoked, ["blob:fake/1"], "the earlier QR's blob is released");
  assert.strictEqual(q(m.panel, ".ph-qrbox img").getAttribute("src"), "blob:fake/2");
  console.log("PASS: New code asks again for the same phone and swaps the image and its blob");
}
{
  // Done and dispose release everything
  const m = await qrWorld();
  click(m.win, act(m.panel, "ph-done"));
  await sleep(20);
  assert.deepStrictEqual(m.log.revoked, ["blob:fake/1"]);
  const k = await qrWorld();
  k.panel.remove();                                              // the page goes away: disconnectedCallback
  assert.deepStrictEqual(k.log.revoked, ["blob:fake/1"], "removing the panel revokes the blob");
  assert.strictEqual(k.panel._phones, null);
  console.log("PASS: Done and removing the panel release the blob URL and the timers");
}
{
  // a created reply whose code the server lost before the fetch finished is a message, not a stuck page
  const m = await qrWorld({ status: 404 });
  assert.match(q(m.panel, ".ph-qrnone").textContent, /Use New code/);
  console.log("PASS: a QR that expired before it was fetched says to use New code");
}

// ---- 13. the tab bar in the error and needs-restart phases ------------------------------------------------------------------------------------------
{
  const m = await mount({ handlers: { "portico/content/document": () => { throw { code: "internal_error", message: "the content store is broken" }; } } });
  assert.strictEqual(m.panel._phase, "error");
  assert.match(text(m.panel), /the content store is broken/);
  assert.deepStrictEqual(qa(m.panel, "header .tab").map((b) => b.textContent), ["Content", "Devices", "Settings", "Status", "Elements"], "the bar is there in the error phase");
  click(m.win, act(m.panel, "tab-devices"));
  await sleep(30);
  assert.ok(q(m.panel, '[data-view="list"]'), "Phones is reachable with the content store broken");
  assert.strictEqual(qa(m.panel, "[data-phone]").length, 4);
  assert.ok(!text(m.panel).includes("the content store is broken"));
  click(m.win, act(m.panel, "tab-content"));
  assert.match(text(m.panel), /the content store is broken/, "and the failure is one click away");
  console.log("PASS: with the content store broken the tab bar is shown and Phones works");
}
{
  const m = await mount({ handlers: { "portico/content/document": () => { throw { code: "unknown_command", message: "Unknown command." }; } } });
  assert.strictEqual(m.panel._phase, "needs_restart");
  assert.ok(q(m.panel, '[data-state="needs-restart"]'));
  assert.deepStrictEqual(qa(m.panel, "header .tab").map((b) => b.textContent), ["Content", "Devices", "Settings", "Status", "Elements"]);
  click(m.win, act(m.panel, "tab-devices"));
  await sleep(30);
  assert.ok(q(m.panel, '[data-view="list"]'));
  console.log("PASS: in the needs-restart phase the tab bar is shown and Phones works");
}
{
  // a server on another contract: needs_restart from the probe; the bar is there and the tab says what the server answers
  const m = await mount({ handlers: { "portico/version": () => ({ contract: 2 }), "portico/phones/list": () => { throw { code: "unknown_command", message: "x" }; } } });
  assert.strictEqual(m.panel._phase, "needs_restart");
  assert.deepStrictEqual(qa(m.panel, "header .tab").map((b) => b.textContent), ["Content", "Devices", "Settings", "Status", "Elements"]);
  click(m.win, act(m.panel, "tab-devices"));
  await sleep(30);
  assert.match(q(m.panel, '[data-state="ph-error"]').textContent, /Restart Home Assistant/);
  console.log("PASS: on a contract mismatch the bar is shown too");
}
{
  // without the module the error phase is what it always was: no bar
  const m = await mount({ phones: false, settings: false, elements: false, handlers: { "portico/content/document": () => { throw { code: "internal_error", message: "broken" }; } } });
  assert.strictEqual(qa(m.panel, "header .tab").length, 0);
  assert.match(text(m.panel), /broken/);
  console.log("PASS: with no Phones module the error phase is unchanged");
}

// ---- 15. UI split P3: managing a phone (doors, push ring notification, house calls), the status line --------------------------------------------------------------------
async function toManage(m, id) {
  await openPhones(m);
  click(m.win, q(m.panel, `[data-phone="${id}"] [data-act="ph-row-manage"]`));
  await sleep(20);
}
{
  assert.deepStrictEqual(revokeConsequences({ state: "enrolled" }).length, 5);
  assert.strictEqual(revokeConsequences({ state: "pending" }).length, 6, "a waiting QR is listed only for a phone that has one");
  assert.match(revokeConsequences({ state: "enrolled" }).join(" "), /Home Assistant user/);
  assert.match(revokeConsequences({ state: "enrolled" }).join(" "), /Tailscale node/);
  assert.strictEqual(statusLine({}), "");
  assert.strictEqual(statusLine(undefined), "");
  assert.strictEqual(statusLine({ "sensor.call_state": { state: "idle" } }), "Call state: idle");
  assert.strictEqual(statusLine({ "sensor.call_state": { state: "ringing" }, "sensor.listen_leg": { state: "up" } }), "Call state: ringing. Listen leg: up");
  const list = [{ id: "a" }, { id: "b" }, { id: "c" }];
  assert.strictEqual(pickedDoors(true, new Set(["a"]), list), null);
  assert.deepStrictEqual(pickedDoors(false, new Set(["c", "a", "x"]), list), ["a", "c", "x"], "list order, then an id the list does not have");
  assert.ok(sameDoors(null, undefined) && sameDoors(["a", "b"], ["b", "a"]) && !sameDoors(null, []) && !sameDoors(["a"], ["a", "b"]));
  console.log("PASS: revoke consequences, the status line and the door helpers");
}
{
  // the status line: read-only, from hass.states, absent on an install without the sensors
  const none = await mount();
  await openPhones(none);
  assert.strictEqual(field(none.panel, "ph-status"), null);
  const some = await mount({ states: { "sensor.call_state": { state: "ringing", attributes: {} }, "sensor.listen_leg": { state: "up", attributes: {} } } });
  await openPhones(some);
  assert.strictEqual(field(some.panel, "ph-status").textContent, "Gate: Call state: ringing. Listen leg: up.");
  assert.strictEqual(types(some.fake, "portico/phones/set").length, 0);
  console.log("PASS: the optional gate status line reads hass.states and writes nothing");
}
{
  // opening a phone: the doors it has now, the choices from portico/doors, the switches from its row
  const m = await mount();
  await toManage(m, "e-yael");
  assert.ok(q(m.panel, '[data-view="manage"]'));
  assert.strictEqual(types(m.fake, "portico/doors").length, 1, "the door choices are the doors Portico has now");
  assert.strictEqual(q(m.panel, '[data-block="where"]'), null, "no 'where is everything' block on a phone's own page");
  assert.match(q(m.panel, '[data-view="manage"] h2').textContent, /Yael <b>bold<\/b>/);
  assert.strictEqual(qa(m.panel, "b").length, 0);
  assert.strictEqual(field(m.panel, "ph-doors-all").checked, false, "Yael has two doors: not 'all'");
  assert.strictEqual(field(m.panel, "ph-door:d1").checked, true);
  assert.strictEqual(field(m.panel, "ph-door:d2").checked, true);
  assert.strictEqual(field(m.panel, "ph-door:d3").checked, false);
  assert.strictEqual(field(m.panel, "ph-away").disabled, true, "no registration yet: the push ring notification cannot be switched");
  assert.match(text(m.panel), /Open the Portico app on this phone once/);
  assert.strictEqual(field(m.panel, "ph-house").checked, true);
  assert.match(field(m.panel, "ph-house-hint").textContent, /this phone can ring and be rung/);
  // nothing changed: Save sends nothing
  click(m.win, act(m.panel, "ph-save"));
  await sleep(10);
  assert.strictEqual(types(m.fake, "portico/phones/set").length, 0);
  assert.match(text(m.panel), /Nothing changed/);
  console.log("PASS: a phone's page shows its doors, push ring notification and house calls, and sends nothing when nothing changed");
}
{
  // saving sends only what changed, and the list shows the reply
  const m = await mount();
  await toManage(m, "e-alice");
  assert.strictEqual(field(m.panel, "ph-doors-all").checked, true);
  assert.strictEqual(field(m.panel, "ph-doors"), null, "all doors: no list");
  assert.strictEqual(field(m.panel, "ph-away").checked, true);
  assert.strictEqual(field(m.panel, "ph-away").disabled, false);
  click(m.win, field(m.panel, "ph-doors-all"));
  field(m.panel, "ph-doors-all").checked = false;
  field(m.panel, "ph-doors-all").dispatchEvent(new m.win.Event("change", { bubbles: true }));
  await sleep(10);
  for (const id of ["d3", "d1"]) {
    const box = field(m.panel, `ph-door:${id}`);
    box.checked = true;
    box.dispatchEvent(new m.win.Event("change", { bubbles: true }));
  }
  const away = field(m.panel, "ph-away");
  away.checked = false;
  away.dispatchEvent(new m.win.Event("change", { bubbles: true }));
  click(m.win, act(m.panel, "ph-save"));
  await sleep(20);
  const sent = types(m.fake, "portico/phones/set");
  assert.deepStrictEqual(sent, [{ type: "portico/phones/set", entry_id: "e-alice", patch: { doors: ["d1", "d3"], away_ring: false } }],
    "doors in the list's order, the push ring notification, and NOT house_receive (unchanged)");
  assert.match(text(m.panel), /Saved\./);
  assert.strictEqual(field(m.panel, "ph-away").checked, false);
  click(m.win, act(m.panel, "ph-back"));
  await sleep(20);
  assert.match(q(m.panel, '[data-phone="e-alice"]').textContent, /2 doors/);
  console.log("PASS: Save sends only the changed fields; the reply is what the page and the list then show");
}
{
  // a refusal shows the panel's words and keeps the form; an unknown stored door stays visible so it can be unticked
  const rows = ROWS.map((r) => ({ ...r }));
  rows[0].doors = ["d1", "gone"];
  const m = await mount({ state: { rows, set: () => { throw { code: "unknown_door", message: "x", field: "doors" }; } } });
  await toManage(m, "e-alice");
  assert.ok(field(m.panel, "ph-door:gone"), "a stored door Portico no longer has is shown");
  assert.match(field(m.panel, "ph-door:gone").parentNode.textContent, /does not have this door now/);
  const house = field(m.panel, "ph-house");
  house.checked = true;
  house.dispatchEvent(new m.win.Event("change", { bubbles: true }));
  click(m.win, act(m.panel, "ph-save"));
  await sleep(20);
  assert.match(q(m.panel, '[data-state="ph-error"]').textContent, /not a door Portico has now/);
  assert.strictEqual(field(m.panel, "ph-house").checked, true, "what was ticked stays ticked");
  assert.strictEqual(act(m.panel, "ph-save").disabled, false, "and the form can be fixed and sent again");
  console.log("PASS: a refused save keeps the form and says why; a vanished stored door is shown so it can be unticked");
}
{
  // door list unreadable: the form says so and does not send doors it cannot check
  const m = await mount({ handlers: { "portico/doors": () => { throw { code: "internal_error", message: "doors down" }; } } });
  await toManage(m, "e-yael");
  assert.match(q(m.panel, '[data-state="ph-doors-error"]').textContent, /doors down/);
  console.log("PASS: an unreadable door list is a message");
}
{
  // a legacy row: no doors, no house calls, the push ring notification only when it has one; no preview
  const m = await mount();
  await toManage(m, "legacy-abc");
  assert.strictEqual(field(m.panel, "ph-doors-all").disabled, true);
  assert.strictEqual(field(m.panel, "ph-house"), null);
  assert.match(text(m.panel), /only its push ring notification can be changed here/);
  assert.strictEqual(act(m.panel, "ph-preview"), null);
  console.log("PASS: a legacy row offers only what the server will accept");
}
{
  // preview: only when the content document has exactly one phone of that name
  const w = await mount({ state: { rows: [{ ...ROWS[0], name: "Alice's phone" }, ...ROWS.slice(1)] } });
  await toManage(w, "e-alice");
  assert.ok(act(w.panel, "ph-preview"), "the content document has one phone of that name");
  const before = types(w.fake, "portico/content/preview").length;
  click(w.win, act(w.panel, "ph-preview"));
  await sleep(120);
  assert.ok(q(w.panel, "[data-act='save']"), "now on a content tab");
  assert.strictEqual(w.panel._tab, "pages");
  assert.strictEqual(w.panel._state.sel, "ep:phone1", "the phone's own endpoint is selected");
  assert.ok(types(w.fake, "portico/content/preview").length > before, "and the existing preview command ran");
  assert.strictEqual(types(w.fake, "portico/content/preview").at(-1).endpoint, "phone1");
  const none = await mount();
  await toManage(none, "e-alice");
  assert.strictEqual(act(none.panel, "ph-preview"), null, "Alice's Pixel is not in the content document: no link");
  // two phones of one name in the document: which one is meant cannot be told, so no link
  const twins = makeLoaded();
  twins.endpoints.push({ ...twins.endpoints[0], id: "phone2", ha_user_id: "u9" });
  const dup = await mount({ loaded: twins, state: { rows: [{ ...ROWS[0], name: "Alice's phone" }, ...ROWS.slice(1)] } });
  await toManage(dup, "e-alice");
  assert.strictEqual(act(dup.panel, "ph-preview"), null, "two phones share the name: no link");
  console.log("PASS: Preview opens the existing content preview for the one phone of that name, and is absent otherwise");
}
{
  // revoke: a confirmation page that lists what it destroys; the safe button first and focused; Escape leaves; nothing is sent until the button
  const m = await mount();
  await toManage(m, "e-alice");
  click(m.win, act(m.panel, "ph-revoke"));
  assert.ok(q(m.panel, '[data-view="revoke"]'));
  assert.strictEqual(types(m.fake, "portico/phones/revoke").length, 0, "asking is not revoking");
  const items = qa(m.panel, '[data-field="ph-consequences"] li').map((li) => li.textContent);
  assert.ok(items.length === 5 && items.some((t) => /Home Assistant user/.test(t)) && items.some((t) => /push ring notification registration/.test(t)));
  const buttons = qa(m.panel, '[data-view="revoke"] [data-act]').map((b) => b.dataset.act);
  assert.deepStrictEqual(buttons, ["ph-revoke-no", "ph-revoke-yes"], "the safe button comes first");
  assert.strictEqual(m.panel.shadowRoot.activeElement, act(m.panel, "ph-revoke-no"), "and has the focus");
  q(m.panel, ".phones").dispatchEvent(new m.win.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  assert.ok(q(m.panel, '[data-view="manage"]'), "Escape goes back to the phone's page");
  assert.strictEqual(types(m.fake, "portico/phones/revoke").length, 0);
  click(m.win, act(m.panel, "ph-revoke"));
  click(m.win, act(m.panel, "ph-revoke-no"));
  assert.ok(q(m.panel, '[data-view="manage"]'));
  assert.strictEqual(types(m.fake, "portico/phones/revoke").length, 0);
  console.log("PASS: revoke asks first: the consequences are listed, nothing is sent, Escape and Keep leave");
}
{
  // revoke through: the command carries the confirmation, the list is read again and says so
  const m = await mount();
  await toManage(m, "e-yael");
  click(m.win, act(m.panel, "ph-revoke"));
  click(m.win, act(m.panel, "ph-revoke-yes"));
  await sleep(30);
  assert.deepStrictEqual(types(m.fake, "portico/phones/revoke"), [{ type: "portico/phones/revoke", entry_id: "e-yael", confirm: true }]);
  assert.ok(q(m.panel, '[data-view="list"]'));
  assert.match(q(m.panel, '[data-state="ph-notice"]').textContent, /Revoked Yael <b>bold<\/b>\./);
  assert.ok(types(m.fake, "portico/phones/list").length >= 2, "the list is read again");
  console.log("PASS: revoke sends confirm: true from the confirmation page only, then shows the list again");
}
{
  // revoke refused: the page stays on the confirmation with the words; a phone already gone is not an error
  const m = await mount({ state: { revoke: () => { throw { code: "unauthorized", message: "no" }; } } });
  await toManage(m, "e-alice");
  click(m.win, act(m.panel, "ph-revoke"));
  click(m.win, act(m.panel, "ph-revoke-yes"));
  await sleep(20);
  assert.ok(q(m.panel, '[data-view="revoke"]'));
  assert.match(q(m.panel, '[data-state="ph-error"]').textContent, /Only an administrator/);
  assert.strictEqual(act(m.panel, "ph-revoke-yes").disabled, false);
  const gone = await mount({ state: { revoke: () => { throw { code: "unknown_phone", message: "x" }; } } });
  await toManage(gone, "e-alice");
  click(gone.win, act(gone.panel, "ph-revoke"));
  click(gone.win, act(gone.panel, "ph-revoke-yes"));
  await sleep(30);
  assert.match(q(gone.panel, '[data-state="ph-notice"]').textContent, /already gone/);
  console.log("PASS: a refused revoke stays on the confirmation; an already-gone phone is a notice");
}
{
  // the module never builds a revoke without the confirmation page: the only caller of the command is revoke(), which needs the page
  const src = fs.readFileSync(new URL("../portico-phones.js", import.meta.url), "utf8");
  assert.strictEqual((src.match(/portico\/phones\/revoke"/g) || []).length, 1, "named once as a message type, in revoke()");
  const m = await mount();
  await toManage(m, "e-alice");
  assert.strictEqual(await m.ctl().revoke(), false, "revoke() outside the confirmation view does nothing");
  assert.strictEqual(types(m.fake, "portico/phones/revoke").length, 0);
  console.log("PASS: revoke() does nothing unless the confirmation page is showing");
}

// ---- 16. D-20261122-aafb: no global House calls switch ----------------------------------------------------------------------------------------------
{
  assert.match(houseHint(), /this phone can ring and be rung/);
  const m = await mount();
  await openPhones(m);
  assert.ok(!q(m.panel, '[data-block="house"]') && !field(m.panel, "ph-house-toggle"), "no House calls card on the Phones tab");
  assert.strictEqual(types(m.fake, "portico/house_calls/get").length, 0, "the removed command is never asked");
  const src = fs.readFileSync(new URL("../portico-phones.js", import.meta.url), "utf8");
  assert.ok(!/house_calls\/(get|set)/.test(src.replace(/\/\*[\s\S]*?\*\//g, "")), "no code names the removed commands");
  console.log("PASS: there is no House calls switch; the per-phone tick box stays");
}

// ---- 17. Devices tab: the 3-dots menu on a phone row (Edit, Delete) ---------------------------------------------------------------------------------
{
  const m = await mount();
  await openPhones(m);
  const rowOf = (id) => q(m.panel, `[data-phone="${id}"]`);
  const trig = rowOf("e-alice").querySelector('[data-act="ph-row-menu"]');
  const pop = () => rowOf("e-alice").querySelector('[role="menu"]');
  assert.ok(trig && trig.getAttribute("aria-haspopup") === "menu" && trig.getAttribute("aria-expanded") === "false" && trig.getAttribute("aria-label"));
  assert.ok(trig.querySelector("svg path"), "the mdi:dots-vertical glyph");
  assert.strictEqual(pop().hidden, true);
  assert.deepStrictEqual(Array.from(pop().querySelectorAll('[role="menuitem"]')).map((b) => b.textContent), ["Edit", "Delete"], "an enrolled phone: Edit and Delete");
  assert.deepStrictEqual(Array.from(rowOf("e-yael").querySelectorAll('[role="menuitem"]')).map((b) => b.textContent), ["Edit", "New code", "Delete"]);
  assert.strictEqual(rowOf("e-alice").querySelector('[data-act="ph-row-manage"]').getAttribute("role"), "menuitem", "the row itself has no loose Manage button");
  // open: expanded, focus on the first item
  click(m.win, trig);
  assert.strictEqual(pop().hidden, false);
  assert.strictEqual(trig.getAttribute("aria-expanded"), "true");
  assert.strictEqual(m.panel.shadowRoot.activeElement, pop().querySelector("button"));
  // arrow keys move, Escape closes and returns the focus to the trigger
  const key = (node, k) => node.dispatchEvent(new m.win.KeyboardEvent("keydown", { key: k, bubbles: true, cancelable: true }));
  key(m.panel.shadowRoot.activeElement, "ArrowDown");
  assert.strictEqual(m.panel.shadowRoot.activeElement.textContent, "Delete");
  key(m.panel.shadowRoot.activeElement, "ArrowDown");
  assert.strictEqual(m.panel.shadowRoot.activeElement.textContent, "Edit", "wraps");
  key(m.panel.shadowRoot.activeElement, "Escape");
  assert.strictEqual(pop().hidden, true);
  assert.strictEqual(trig.getAttribute("aria-expanded"), "false");
  assert.strictEqual(m.panel.shadowRoot.activeElement, trig);
  // click-away closes
  click(m.win, trig);
  assert.strictEqual(pop().hidden, false);
  q(m.panel, ".page").dispatchEvent(new m.win.Event("click", { bubbles: true, composed: true }));      // a real click is composed: it reaches the document across the shadow root
  assert.strictEqual(pop().hidden, true, "a click elsewhere closes it");
  // Edit opens the existing edit page
  click(m.win, trig);
  click(m.win, rowOf("e-alice").querySelector('[data-act="ph-row-manage"]'));
  await sleep(20);
  assert.ok(q(m.panel, '[data-view="manage"]'), "Edit opens the per-phone edit page");
  click(m.win, act(m.panel, "ph-back"));
  await sleep(20);
  // Delete: the existing revoke confirmation, reached from the row; keeping returns to the list; nothing is sent
  click(m.win, rowOf("e-yael").querySelector('[data-act="ph-row-menu"]'));
  click(m.win, rowOf("e-yael").querySelector('[data-act="ph-row-delete"]'));
  assert.ok(q(m.panel, '[data-view="revoke"]'), "Delete shows the revoke confirmation");
  assert.match(text(m.panel), /Revoke "Yael's Galaxy"\?|Revoke "/);
  assert.strictEqual(types(m.fake, "portico/phones/revoke").length, 0);
  assert.strictEqual(q(m.panel, ".page").dataset.devview, "revoke");
  click(m.win, act(m.panel, "ph-revoke-no"));
  await sleep(20);
  assert.ok(q(m.panel, '[data-view="list"]'), "Keep returns to the list");
  assert.strictEqual(types(m.fake, "portico/phones/revoke").length, 0);
  click(m.win, rowOf("e-yael").querySelector('[data-act="ph-row-menu"]'));
  click(m.win, rowOf("e-yael").querySelector('[data-act="ph-row-delete"]'));
  click(m.win, act(m.panel, "ph-revoke-yes"));
  await sleep(30);
  assert.deepStrictEqual(types(m.fake, "portico/phones/revoke"), [{ type: "portico/phones/revoke", entry_id: "e-yael", confirm: true }]);
  console.log("PASS: a phone row has a 3-dots menu (aria, keys, click-away) with Edit and Delete; Delete reuses the revoke confirmation");
}

console.log("ALL_PANEL_PHONES_TESTS_PASSED");
process.exit(0);      // the panels made above keep their picture timers; the process would otherwise wait for them
