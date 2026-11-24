// UI split P4, P5, P6: the Settings tab (portico-settings.js) inside the panel: the tab and its fallback, the read-only door status,
// the door switches (a draft, a server check, a confirmation page, one message with confirm), the push ring notification preferences.
// Protocol-level, in jsdom with a fake `hass`; NOT a visual check (a real browser is the acceptance test).
import assert from "assert";
import fs from "fs";
import { loadPanel, editorHass, importSettings, sleep } from "./panel_loader.mjs";

const SETTINGS = await importSettings();
const { statusRows, hubText, uiPatch, uiDraftOf, uiConsequenceLines, liveAwayParse, liveAwayText, driverText, ERROR_TEXT } = SETTINGS;

// ---- fixtures -------------------------------------------------------------------------------------------------------

const GATE_STATUS = {
  entry_id: "e-gate", name: "Gate", driver: "gate_sip_server", loaded: true, entry_state: "loaded", away_media: true, connected: true,
  transport_available: true, driver_available: true, call_state: "idle", outbound_state: "idle", can_dial: true,
  settings: null, gate_sip: null, listen_state: null,
};
const FRONT_STATUS = {
  entry_id: "e-front", name: "Front <b>door</b>", driver: "dahua_sip", loaded: true, entry_state: "loaded", away_media: true, connected: true,
  transport_available: true, driver_available: null, call_state: "idle", outbound_state: "idle", can_dial: false,
  settings: null, gate_sip: null, listen_state: null,
};
const UI = { push_entry: true, running: true, ring_enabled: true, ring_window: 45, ring_window_min: 10, ring_window_max: 120, new_phones_enabled: false,
  test_phones: [], phones: [{ reg_id: "r1", name: "Alice's Pixel", away_ring: true }, { reg_id: "r2", name: "Yael <i>phone</i>", away_ring: false }] };

function handlersFor(state = {}) {
  state.status = state.status || { hub: { process: "running", wheel_version: "0.6.2", min_wheel_version: "0.5.0", doors_attached: 2, restarted_once: false },
    doors: [GATE_STATUS, FRONT_STATUS], as_of: "2026-10-24T08:00:00+00:00" };
  state.ui = state.ui || JSON.parse(JSON.stringify(UI));
  state.uiSet = state.uiSet || ((msg) => {
    if (msg.validate_only) return { result: "valid", changes: Object.keys(msg.patch), settings: state.ui };
    if (Object.keys(msg.patch).some((k) => k !== "ring_window") && msg.confirm !== true) throw { code: "confirmation_required", message: "x" };
    state.ui = { ...state.ui, ...msg.patch };
    return { result: "applied", changes: Object.keys(msg.patch), settings: JSON.parse(JSON.stringify(state.ui)) };
  });
  return {
    state,
    handlers: {
      "portico/door_status": () => state.status,
      "portico/ui_settings/get": () => state.ui,
      "portico/ui_settings/set": (msg) => state.uiSet(msg),
      // the Phones tab's own calls, so opening that tab in a test does not fail
      "portico/phones/list": () => ({ header: {}, phones: [] }),
      "portico/house_calls/get": () => ({ enabled: false, live: false, push_entry: true, phones_receiving: 0, active_calls: 0, audio_bridge: true }),
    },
  };
}

async function mount(opts = {}) {
  const w = loadPanel({ phones: opts.phones, settings: opts.settings, elements: opts.elements });
  const win = w.dom.window;
  const doc = w.doc;
  const sv = handlersFor(opts.state || {});
  const fake = editorHass({ ...sv.handlers, ...(opts.handlers || {}) }, { user: opts.user });
  const panel = doc.createElement("portico-panel");
  panel._previewMs = 20;
  panel.hass = fake.hass;
  doc.body.appendChild(panel);
  await sleep(60);
  return { ...w, win, panel, fake, state: sv.state, ctl: () => panel._settings };
}
const q = (p, sel) => p.shadowRoot.querySelector(sel);
const qa = (p, sel) => Array.from(p.shadowRoot.querySelectorAll(sel));
const act = (p, name) => q(p, `[data-act="${name}"]`);
const text = (p) => p.shadowRoot.textContent;
const click = (win, node) => { assert.ok(node, "clicked something that is not there"); node.dispatchEvent(new win.Event("click", { bubbles: true })); };
const types = (fake, t) => fake.sent.filter((m) => m.type === t);
const fieldIn = (card, name) => card.querySelector(`[data-field="${name}"]`);
const tick = (win, node, value) => { assert.ok(node, "no such field"); node.checked = value; node.dispatchEvent(new win.Event("change", { bubbles: true })); };
const setValue = (win, node, value, ev = "input") => { assert.ok(node, "no such field"); node.value = value; node.dispatchEvent(new win.Event(ev, { bubbles: true })); };

async function openSettings(m) {
  click(m.win, act(m.panel, "tab-settings"));
  await sleep(30);
}
async function openStatus(m) {
  click(m.win, act(m.panel, "tab-status"));
  await sleep(30);
}
// DEVICES-09: the push ring notification and the app's settings live on the Devices tab, above the phone list
async function openDevices(m) {
  click(m.win, act(m.panel, "tab-devices"));
  await sleep(40);
}

// ---- 1. the pure helpers ----------------------------------------------------------------------------------------------
{
  assert.strictEqual(driverText("dahua_sip"), "Dahua SIP");
  assert.strictEqual(driverText("x"), "x");
  assert.strictEqual(driverText("gate_sip_observer"), "gate_sip_observer", "the retired driver has no special name");
  for (const gone of ["doorPatch", "draftOf", "applyTick", "tickBlocked", "consequenceLines", "houseAfterNote", "patchReloads", "DOOR_KEYS", "LISTEN_KEYS", "HOUSE_KEY"]) {
    assert.ok(!(gone in SETTINGS), `${gone} is gone with the gate observer's controls`);
  }
  console.log("PASS: the observer's door-edit helpers are gone");
}

// ---- 2. the words: what each setting does, said before it is applied -----------------------------------------------------------
{
  const ui = uiConsequenceLines({ test_phones: ["r1"] }, UI).join("\n");
  assert.match(ui, /Alice's Pixel/);
  assert.match(ui, /REAL visitor ring goes only to the listed phones/);
  assert.match(uiConsequenceLines({ test_phones: [] }, UI).join("\n"), /every phone with its push ring notification on again/);
  assert.match(uiConsequenceLines({ ring_enabled: false }, UI).join("\n"), /no phone is notified/i);
  assert.match(uiConsequenceLines({ new_phones_enabled: true }, UI).join("\n"), /first registration starts with its push ring notification switched on/);
  console.log("PASS: every setting states what it does before it applies");
}

// ---- 3. the status text -------------------------------------------------------------------------------------------------
{
  const rows = Object.fromEntries(statusRows(FRONT_STATUS));
  assert.strictEqual(rows["Driver"], "Dahua SIP");
  assert.strictEqual(rows["Connection"], "Connected");
  assert.strictEqual(rows["Call state"], "idle");
  assert.strictEqual(rows["Door reachable"], "Not reported");
  assert.ok(![...Object.keys(rows)].some((k) => /Peer token|gate-sip|Listen leg|Set here/.test(k)), "no observer rows");
  const down = Object.fromEntries(statusRows({ ...FRONT_STATUS, loaded: false, entry_state: "setup_error", connected: false }));
  assert.strictEqual(down["Home Assistant entry"], "Not running (setup error)");
  assert.strictEqual(down["Connection"], "Not connected");
  assert.match(hubText({ process: "running", wheel_version: "0.6.2", doors_attached: 1, restarted_once: true }), /process running, wheel 0\.6\.2, 1 door attached, restarted once/);
  assert.strictEqual(hubText(null), "Not known");
  console.log("PASS: the status rows name what the door and the hub report and show nothing for a missing part");
}

// ---- 4. no HTML string setter, and no way to send a secret ----------------------------------------------------------------
{
  const src = fs.readFileSync(new URL("../portico-settings.js", import.meta.url), "utf8");
  for (const bad of ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"]) {
    assert.ok(!src.includes(bad), `portico-settings.js must not use ${bad}`);
  }
  assert.ok(!/peer_token\s*[:=]/.test(src.replace(/peer_token_set/g, "")), "the module never builds a peer_token field");
  console.log("PASS: user text only goes in through textContent; the module has no field for a peer token");
}

// ---- 5. the tab ---------------------------------------------------------------------------------------------------------------
{
  const m = await mount();
  assert.deepStrictEqual(qa(m.panel, "header .tab").map((b) => b.textContent), ["Elements", "Pages", "Devices", "Ring & call", "Settings", "Status"]);
  assert.strictEqual(types(m.fake, "portico/door_status").length, 0, "nothing is asked until the tab is opened");
  await openSettings(m);
  assert.strictEqual(types(m.fake, "portico/door_settings/get").length, 0, "the page no longer reads door settings");
  assert.strictEqual(types(m.fake, "portico/door_status").length, 0, "the Settings tab no longer reads the door status");
  assert.strictEqual(q(m.panel, '[data-block="door-status"]'), null, "no status readout on the Settings tab");
  assert.strictEqual(q(m.panel, '[data-field="st-hub"]'), null);
  assert.strictEqual(types(m.fake, "portico/ui_settings/get").length, 0, "the Settings tab no longer reads the phone and app settings");
  assert.strictEqual(q(m.panel, '[data-block="ui-settings"]'), null, "no push ring notification or app block on the Settings tab");
  assert.ok(!/Test phones|Push ring notification|Live video/.test(text(m.panel)), "Settings holds no phone or app wording");
  assert.strictEqual(q(m.panel, "header").dataset.tab, "settings");
  for (const gone of ["undo", "discard", "save"]) assert.strictEqual(act(m.panel, gone), null, `${gone} is not shown on the Settings tab`);
  assert.strictEqual(q(m.panel, "header .status"), null);
  assert.match(text(m.panel), /Nothing here is switched on by itself/);
  assert.match(text(m.panel), /Passwords and keys are never shown or entered here/);
  assert.strictEqual(q(m.panel, '[data-block="door-card"]'), null, "no door controls on the Settings tab");
  // status: a card per door, the markup in a name stays text
  await openStatus(m);
  assert.strictEqual(types(m.fake, "portico/door_status").length, 1);
  assert.strictEqual(q(m.panel, "header").dataset.tab, "status");
  assert.ok(q(m.panel, '[data-block="status-intro"]'));
  assert.strictEqual(q(m.panel, '[data-block="door-card"]'), null, "no door switches on the Status tab");
  assert.strictEqual(q(m.panel, '[data-field="st-port"]'), null);
  assert.strictEqual(act(m.panel, "st-review"), null);
  const cards = qa(m.panel, '[data-block="door-status"]');
  assert.strictEqual(cards.length, 2);
  assert.strictEqual(cards[1].querySelector("h2").firstChild.textContent, "Front <b>door</b>");
  assert.strictEqual(cards[1].querySelector("b"), null, "a door's name is text, never markup");
  assert.match(q(m.panel, '[data-field="st-hub"]').textContent, /process running, wheel 0\.6\.2, 2 doors attached/);
  click(m.win, act(m.panel, "tab-settings"));
  await sleep(30);
  // the Devices tab holds the push ring notification and app block, above the phone list, and reads it when opened
  await openDevices(m);
  const blk = q(m.panel, '[data-block="ui-settings"]');
  assert.ok(blk, "the Push ring notification and app block is on the Devices tab");
  assert.match(blk.querySelector("h2").textContent, /Push ring notification and app/);
  assert.strictEqual(types(m.fake, "portico/ui_settings/get").length, 1);
  assert.ok(blk.compareDocumentPosition(act(m.panel, "ph-enrol")) & 4, "the block comes before the phone list's controls");
  assert.ok(blk.querySelector('[data-field="st-live-rtsp-base"]'), "and so does the live video block");
  // back to a content tab and back: the same element, the header is the content header again
  const el = m.ctl().el;
  click(m.win, act(m.panel, "tab-pages"));
  assert.ok(act(m.panel, "save"));
  await openSettings(m);
  assert.strictEqual(m.ctl().el, el, "the persistent element survives the re-render");
  assert.strictEqual(types(m.fake, "portico/door_settings/get").length, 0);
  console.log("PASS: the Settings tab and the Status tab are last, hides Undo/Discard/Save, reads when opened, and draws a card per door");
}
{
  const m = await mount({ user: { is_admin: false }, handlers: { "portico/content/get": () => ({ endpoint: "e", kind: "phone", pages: [], callers: {}, roster: [] }) } });
  assert.strictEqual(act(m.panel, "tab-settings"), null);
  assert.strictEqual(act(m.panel, "tab-status"), null);
  assert.strictEqual(m.ctl(), null);
  for (const t of ["portico/door_status", "portico/door_settings/get", "portico/ui_settings/get"]) assert.strictEqual(types(m.fake, t).length, 0);
  console.log("PASS: a non-administrator gets no Settings tab and no settings call");
}
{
  const m = await mount({ settings: false, elements: false });
  assert.deepStrictEqual(qa(m.panel, "header .tab").map((b) => b.textContent), ["Pages", "Devices", "Ring & call"]);
  assert.strictEqual(m.ctl(), null);
  click(m.win, act(m.panel, "tab-devices"));
  assert.ok(act(m.panel, "add-screen"), "the Screens tab still works");
  click(m.win, act(m.panel, "tab-devices"));
  await sleep(20);
  assert.ok(act(m.panel, "ph-enrol"), "and so does the Phones tab");
  console.log("PASS: when the Settings module cannot be imported the tab is absent and everything else is unharmed");
}
{
  const urls = [];
  const w = loadPanel();
  const panel = w.doc.createElement("portico-panel");
  panel._importSettings = (url) => { urls.push(url); return Promise.reject(new Error("no")); };
  panel.panel = { config: { settings_module: "/portico/portico-settings.js?v=abc123def456" } };
  panel.hass = editorHass({}).hass;
  w.doc.body.appendChild(panel);
  await sleep(40);
  assert.deepStrictEqual(urls, ["/portico/portico-settings.js?v=abc123def456"]);
  const bare = w.doc.createElement("portico-panel");
  const urls2 = [];
  bare._importSettings = (url) => { urls2.push(url); return Promise.reject(new Error("no")); };
  bare.hass = editorHass({}).hass;
  w.doc.body.appendChild(bare);
  await sleep(40);
  assert.deepStrictEqual(urls2, ["/portico/portico-settings.js"]);
  console.log("PASS: the panel imports the content-hashed Settings module from its config, the bare path otherwise");
}
{
  // the tab bar is there when the content store is broken
  const m = await mount({ handlers: { "portico/content/document": () => { throw { code: "internal_error", message: "store broken" }; } } });
  assert.ok(act(m.panel, "tab-settings") && act(m.panel, "tab-content"), "the failed phase keeps the tab bar");
  click(m.win, act(m.panel, "tab-settings"));
  await sleep(30);
  assert.ok(q(m.panel, '[data-block="settings-intro"]'), "and the Settings page works there");
  assert.strictEqual(q(m.panel, "header").dataset.tab, "settings");
  console.log("PASS: with the content store broken the Settings tab is still reachable");
}

// ---- 5b. Status holds the readouts, Settings the controls, nothing twice ------------------------------------------------------
{
  const m = await mount({});
  await openSettings(m);
  assert.strictEqual(q(m.panel, '[data-block="claims"]'), null, "no claims section: the claims switch is gone");
  await openStatus(m);
  assert.strictEqual(q(m.panel, '[data-block="claims-card"]'), null);
  assert.strictEqual(act(m.panel, "st-claims"), null, "no controls on Status");
  click(m.win, act(m.panel, "tab-status"));
  assert.strictEqual(m.panel._hosted(), m.panel._status);
  console.log("PASS: the Status tab shows the door status with no control; the controls stay on Settings");
}
{
  // a deep link / old state of "settings" still works, and a failed import leaves neither tab
  const m = await mount({ settings: false, elements: false });
  assert.strictEqual(act(m.panel, "tab-status"), null);
  assert.strictEqual(m.panel._status, null);
  console.log("PASS: with the module missing there is no Status tab either");
}

// ---- 6. a read that fails leaves the other cards working ------------------------------------------------------------------------
{
  const m = await mount({ handlers: { "portico/door_status": () => { throw { code: "unknown", message: "boom" }; } } });
  await openStatus(m);
  assert.match(q(m.panel, '[data-state="status-error"]').textContent, /Something went wrong on the server/);
  await openSettings(m);
  assert.strictEqual(q(m.panel, '[data-state="status-error"]'), null, "a status failure is not shown on Settings");
  assert.ok(q(m.panel, '[data-block="settings-intro"]'), "the Settings page still draws");
  console.log("PASS: a failed read says why on its own card and leaves the others working");
}

// ---- 8. the push ring notification preferences ----------------------------------------------------------------------------------------------------------
{
  const m = await mount();
  await openDevices(m);
  assert.match(text(m.panel), /Push ring notification/);
  assert.match(text(m.panel), /does not touch the doors, the gate or house calls/);
  const ui = q(m.panel, '[data-block="ui-settings"]');
  assert.strictEqual(ui.querySelector('[data-field="st-ring-enabled"]').checked, true);
  assert.strictEqual(ui.querySelector('[data-field="st-new-phones"]').checked, false);
  assert.strictEqual(ui.querySelector('[data-field="st-ring-window"]').value, "45");
  assert.strictEqual(ui.querySelector('[data-field="st-test-r2"]').parentNode.textContent.includes("Yael <i>phone</i>"), true, "a phone's name is text");
  assert.strictEqual(ui.querySelector("i"), null);
  // the ring window alone: checked by the server, applied with no confirmation page and no confirm key
  setValue(m.win, ui.querySelector('[data-field="st-ring-window"]'), "60");
  click(m.win, act(m.panel, "st-ui-review"));
  await sleep(30);
  const sets = types(m.fake, "portico/ui_settings/set");
  assert.deepStrictEqual(sets, [{ type: "portico/ui_settings/set", patch: { ring_window: 60 }, validate_only: true }, { type: "portico/ui_settings/set", patch: { ring_window: 60 } }]);
  assert.ok(!q(m.panel, '[data-view="confirm"]'));
  assert.strictEqual(m.state.ui.ring_window, 60);
  assert.match(q(m.panel, '[data-state="ui-notice"]').textContent, /takes effect at once/);
  console.log("PASS: the ring window alone is checked and applied with no confirmation");
}
{
  const ok = liveAwayParse("camera.porch rtsp://10.6.0.9:554/ch1 rtsp://10.6.0.9:554/ch2, camera.yard rtsp://cam.lan/live");
  assert.deepStrictEqual(ok, { problem: "", map: { "camera.porch": { main: "rtsp://10.6.0.9:554/ch1", sub: "rtsp://10.6.0.9:554/ch2" }, "camera.yard": { main: "rtsp://cam.lan/live" } } });
  assert.strictEqual(liveAwayText(ok.map), "camera.porch rtsp://10.6.0.9:554/ch1 rtsp://10.6.0.9:554/ch2, camera.yard rtsp://cam.lan/live");
  for (const bad of ["rtsp://10.6.0.9/a", "camera.porch", "camera.porch rtsp://u:p@10.6.0.9/a", "camera.porch rtsp://10.6.0.9/a?x=1", "light.x rtsp://10.6.0.9/a",
    "camera.a rtsp://h/a rtsp://h/b rtsp://h/c", "camera.a rtsp://h/a camera.a rtsp://h/b"]) assert.ok(liveAwayParse(bad).problem, bad);
  assert.strictEqual(liveAwayParse("").problem, "");
  const UI0 = { ring_enabled: true, ring_window: 45, new_phones_enabled: true, test_phones: [] };
  assert.deepStrictEqual(uiPatch(UI0, { ...uiDraftOf(UI0), live_away_streams: "camera.yard rtsp://cam.lan/live" }), { live_away_streams: { "camera.yard": { main: "rtsp://cam.lan/live" } } });
  const UI1 = { ...UI0, live_away_streams: ok.map };
  assert.deepStrictEqual(uiPatch(UI1, uiDraftOf(UI1)), {}, "untouched: no patch");
  assert.deepStrictEqual(uiPatch(UI1, { ...uiDraftOf(UI1), live_away_streams: "" }), { live_away_streams: {} });
  console.log("PASS: per-camera away addresses are parsed, diffed and cleared in the page the way the server checks them");
}
{
  const m = await mount();
  await openDevices(m);
  const ui = () => q(m.panel, '[data-block="ui-settings"]');
  tick(m.win, ui().querySelector('[data-field="st-ring-enabled"]'), false);
  tick(m.win, ui().querySelector('[data-field="st-test-r1"]'), true);
  assert.strictEqual(types(m.fake, "portico/ui_settings/set").length, 0, "ticking sends nothing");
  click(m.win, act(m.panel, "st-ui-review"));
  await sleep(20);
  const page = q(m.panel, '[data-view="confirm"]');
  assert.ok(page);
  assert.match(page.querySelector("h2").textContent, /Change the push ring notification\?/);
  const lines = q(m.panel, '[data-field="st-lines"]').textContent;
  assert.match(lines, /Push ring notification OFF/);
  assert.match(lines, /REAL visitor ring goes only to the listed phones/);
  click(m.win, act(m.panel, "st-yes"));
  await sleep(30);
  const sets = types(m.fake, "portico/ui_settings/set");
  assert.strictEqual(sets.length, 2);
  assert.deepStrictEqual(sets[1], { type: "portico/ui_settings/set", patch: { ring_enabled: false, test_phones: ["r1"] }, confirm: true });
  assert.deepStrictEqual(m.state.ui.test_phones, ["r1"]);
  assert.strictEqual(q(m.panel, '[data-block="ui-settings"] [data-field="st-test-r1"]').checked, true, "the card shows what the server now holds");
  // untick the last test phone: the list is empty again
  tick(m.win, ui().querySelector('[data-field="st-test-r1"]'), false);
  assert.deepStrictEqual(uiPatch(m.state.ui, { ...uiDraftOf(m.state.ui), test_phones: new Set() }), { test_phones: [] });
  console.log("PASS: the push ring notification and the test phones go through a confirmation page that names who is reached, and send confirm once");
}
{
  const m = await mount({ state: { uiSet: (msg) => { if (msg.validate_only) throw { code: "unknown_phone", message: "x" }; return {}; } } });
  await openDevices(m);
  tick(m.win, q(m.panel, '[data-block="ui-settings"] [data-field="st-test-r1"]'), true);
  click(m.win, act(m.panel, "st-ui-review"));
  await sleep(20);
  assert.match(q(m.panel, '[data-state="ui-card-error"]').textContent, /not registered for the push ring notification any more/);
  assert.ok(!q(m.panel, '[data-view="confirm"]'));
  const none = await mount({ state: { ui: { ...UI, push_entry: false } } });
  await openDevices(none);
  assert.match(q(none.panel, '[data-block="ui-settings"]').textContent, /settings entry is missing/);
  const off = await mount({ state: { ui: { ...UI, running: false, phones: [] } } });
  await openDevices(off);
  assert.match(q(off.panel, '[data-block="ui-settings"]').textContent, /push ring notification is not running, so its phones cannot be listed/);
  console.log("PASS: a refusal is said on the card; a missing settings entry and a stopped push ring notification are said");
}

// ---- 8b. live video away from home (DEV-713 S1b-1) -------------------------------------------------------------------------------------------
{
  const { liveBaseProblem, liveSubProblem, liveSubNames } = SETTINGS;
  assert.strictEqual(liveBaseProblem(""), "");
  assert.strictEqual(liveBaseProblem(" rtsp://10.6.0.6:8554 "), "");
  assert.strictEqual(liveBaseProblem("rtsp://go2rtc.lan"), "");
  for (const bad of ["http://10.6.0.6:8554", "rtsp://user:pw@10.6.0.6:8554", "rtsp://10.6.0.6:8554/cam", "10.6.0.6:8554", "rtsp://", "rtsp://[::1]:8554"]) {
    assert.match(liveBaseProblem(bad), /rtsp:\/\/10\.6\.0\.6:8554/, bad);
  }
  assert.deepStrictEqual(liveSubNames("gate_intercom, gate_intercom  doorbell\nfacade-lower"), ["gate_intercom", "doorbell", "facade-lower"]);
  assert.strictEqual(liveSubProblem("gate_intercom, doorbell"), "");
  assert.match(liveSubProblem("gate/intercom"), /letters, digits/);
  // untouched card: no live keys in the patch, and an unset server value is an empty draft
  assert.deepStrictEqual(uiPatch(UI, uiDraftOf(UI)), {});
  assert.strictEqual(uiDraftOf(UI).live_rtsp_base, "");
  assert.strictEqual(uiDraftOf({ ...UI, live_rtsp_base: "rtsp://10.6.0.6:8554", live_sub_streams: ["gate_intercom"] }).live_sub_streams, "gate_intercom");
  console.log("PASS: the live-video address and sub-stream names are checked in the page the way the server checks them");
}
{
  const m = await mount();
  await openDevices(m);
  const ui = () => q(m.panel, '[data-block="ui-settings"]');
  assert.match(ui().textContent, /Live video away from home: the address of the camera server's RTSP port \(empty = off\)\. Anyone with a Portico phone on your tailnet that is allowed to reach that port can pull ANY stream on it, not only the cameras shown to them\./);
  const base = ui().querySelector('[data-field="st-live-rtsp-base"]');
  assert.strictEqual(base.value, "");
  // a bad address stops the review button and says why; nothing is sent
  setValue(m.win, base, "rtsp://user:pw@10.6.0.6:8554");
  assert.ok(q(m.panel, '[data-state="live-rtsp-problem"]'));
  assert.strictEqual(act(m.panel, "st-ui-review").disabled, true);
  setValue(m.win, ui().querySelector('[data-field="st-live-rtsp-base"]'), "rtsp://10.6.0.6:8554");
  setValue(m.win, ui().querySelector('[data-field="st-live-sub-streams"]'), "gate_intercom, doorbell");
  assert.strictEqual(act(m.panel, "st-ui-review").disabled, false);
  assert.strictEqual(types(m.fake, "portico/ui_settings/set").length, 0, "typing sends nothing");
  click(m.win, act(m.panel, "st-ui-review"));
  await sleep(20);
  const page = q(m.panel, '[data-view="confirm"]');
  assert.ok(page, "turning it on is confirmed");
  const lines = q(m.panel, '[data-field="st-lines"]').textContent;
  assert.match(lines, /Live video away from home ON, from rtsp:\/\/10\.6\.0\.6:8554/);
  assert.match(lines, /can pull ANY stream on it, not only the cameras shown to them/);
  assert.match(lines, /gate_intercom, doorbell/);
  click(m.win, act(m.panel, "st-yes"));
  await sleep(30);
  const sets = types(m.fake, "portico/ui_settings/set");
  assert.strictEqual(sets.length, 2);
  assert.deepStrictEqual(sets[1], { type: "portico/ui_settings/set", patch: { live_rtsp_base: "rtsp://10.6.0.6:8554", live_sub_streams: ["gate_intercom", "doorbell"] }, confirm: true });
  assert.strictEqual(ui().querySelector('[data-field="st-live-rtsp-base"]').value, "rtsp://10.6.0.6:8554", "the card shows what the server now holds");
  // emptying the address turns it off, and that is confirmed too
  setValue(m.win, ui().querySelector('[data-field="st-live-rtsp-base"]'), "");
  assert.deepStrictEqual(uiPatch(m.state.ui, { ...uiDraftOf(m.state.ui), live_rtsp_base: "" }), { live_rtsp_base: "" });
  click(m.win, act(m.panel, "st-ui-review"));
  await sleep(20);
  assert.match(q(m.panel, '[data-field="st-lines"]').textContent, /Live video away from home OFF/);
  console.log("PASS: live video away from home is edited on the Settings tab, confirmed, and sent as live_rtsp_base / live_sub_streams");
}

// ---- 9. everything sent is a command this module owns, with keys it owns ------------------------------------------------------------------------
{
  const m = await mount();
  await openSettings(m);
  const allowed = new Set(["portico/door_status", "portico/ui_settings/get", "portico/ui_settings/set",
    "portico/content/document", "portico/content/preview", "portico/content/get", "portico/cameras/info", "portico/elements/list", "portico/config/status"]);      // the last six are the content tabs' own (elements/list: camera references, DEV-760 P2; config/status: the YAML files' banner, DEV-824 P5, a read)
  for (const msg of m.fake.sent) assert.ok(allowed.has(msg.type), `unexpected command ${msg.type}`);
  for (const t of ["portico/door_settings/get", "portico/door_settings/set"]) assert.strictEqual(types(m.fake, t).length, 0, t);
  for (const t of ["portico/phones/enroll", "portico/phones/revoke", "portico/house_calls/set", "portico/dial", "portico/open_relay"]) assert.strictEqual(types(m.fake, t).length, 0, t);
  console.log("PASS: the Settings tab sends only its own commands, with only the keys it owns");
}

// ---- 10. every server error code has a text here -----------------------------------------------------------------------------------------------
{
  for (const code of ["bad_patch", "confirmation_required", "no_push_entry", "push_not_loaded", "unknown_phone", "unknown"]) {
    assert.ok(ERROR_TEXT[code] && ERROR_TEXT[code].length > 20, code);
  }
  console.log("PASS: every refusal the server can send has a text in the page's own words");
}
// ---- 12. the frame-rate switch (moved here from the Pages tab): a field of the content document, saved with portico/content/save ------------------
{
  const m = await mount({});
  await openSettings(m);
  const card = () => q(m.panel, '[data-block="fps-setting"]');
  assert.ok(card(), "a Live pictures card on the Settings tab");
  const box = () => fieldIn(card(), "st-show-fps");
  assert.strictEqual(box().checked, false);
  assert.match(card().textContent, /Show the frame rate on live pictures/);
  assert.strictEqual(card().querySelector('[data-act="st-fps-apply"]').disabled, true, "nothing to apply yet");
  tick(m.win, box(), true);
  assert.strictEqual(types(m.fake, "portico/content/save").length, 0, "ticking alone writes nothing");
  click(m.win, card().querySelector('[data-act="st-fps-apply"]'));
  await sleep(40);
  const saves = types(m.fake, "portico/content/save");
  assert.strictEqual(saves.length, 1);
  assert.strictEqual(saves[0].document.show_fps, true);
  assert.strictEqual(saves[0].base_doc_rev, "rev1", "the doc_rev just read");
  assert.match(card().textContent, /Applied/);
  assert.ok(!q(m.panel, '[data-field="show-fps"]'), "and no box on the Pages tab");
  console.log("PASS: the frame-rate switch is on the Settings tab: tick, Apply, one content/save with show_fps");
}
console.log("ALL_PANEL_SETTINGS_TESTS_PASSED");
process.exit(0);      // the panels made above keep their picture timers; the process would otherwise wait for them
