// Pages unified, P-C: the Devices tab's Pages and Ring pages selectors for a phone and a screen,
// the ownership of Talk elements (a door Talk element may be on several screens, D-20261122-42b1), "Wakes on".
// The pure edits (`logic`) and the panel in jsdom with a fake `hass`. Protocol-level; NOT a visual check.
import assert from "assert";
import { loadPanel, editorHass, makeLoaded, sleep } from "./panel_loader.mjs";

const { dom, Panel, doc } = loadPanel();
const L = Panel.logic;
const win = dom.window;
const throwsEdit = (fn, re) => assert.throws(fn, (e) => e instanceof L.EditError && re.test(e.message), String(re));

const T_FRONT = { id: "el-20000001", kind: "talk", name: "Front door", options: { door: "http://10.6.0.7:8091" } };
const T_SIDE = { id: "el-20000002", kind: "talk", name: "Side door", options: { door: "side-1" } };
const T_PHONE = { id: "talk-phone-phone1", kind: "talk", name: "Alice's phone", managed: true, options: { phone: "phone1" } };
const T_KITCHEN = { id: "talk-screen-kitchen", kind: "talk", name: "Kitchen tablet", managed: true, options: { screen: "kitchen" } };
const T_HALL = { id: "talk-screen-hall", kind: "talk", name: "Hall tablet", managed: true, options: { screen: "hall" } };
const ELS = [T_FRONT, T_SIDE, T_PHONE, T_KITCHEN, T_HALL];
const MAP = new Map(ELS.map((e) => [e.id, e]));
const listReply = (elements) => ({ schema_version: 1, rev: "r1", elements, used_by: {}, kinds: {}, icons: [], palette: [], limits: {} });

const q = (p, sel) => p.shadowRoot.querySelector(sel);
const qa = (p, sel) => Array.from(p.shadowRoot.querySelectorAll(sel));
const act = (p, name) => q(p, `[data-act="${name}"]`);
const click = (node) => { assert.ok(node, "clicked something that is not there"); node.dispatchEvent(new win.Event("click", { bubbles: true })); };
function setValue(node, value, type = "change") { assert.ok(node); node.value = value; node.dispatchEvent(new win.Event(type, { bubbles: true })); }

const page = (id, label, ...talks) => ({ id, label, kind: "content", blocks: [{ id: "b", title: label, cameras: [], actions: talks.map((t) => ({ id: `a-${t}`, element: t })) }] });
const ringPage = (id, talk) => ({ id, label: id, kind: "ring", talk, cameras: null, blocks: [] });

/* phone1 shows `home`; kitchen shows `front` (the front door's Talk element); hall shows nothing. */
function doc6() {
  return {
    schema_version: 6, home_name: "My Home",
    pages: [page("home", "Home"), page("front", "Front", T_FRONT.id), page("side", "Side", T_SIDE.id), ringPage("front-ring", T_FRONT.id), ringPage("front-night", T_FRONT.id),
            ringPage("alice-ring", T_PHONE.id)],
    devices: { phone1: { pages: [{ page: "home" }], ring: { [T_FRONT.id]: "front-ring" }, roster_hide: [] },
               kitchen: { pages: [{ page: "front" }], ring: {}, roster_hide: [] }, hall: { pages: [], ring: {}, roster_hide: [] } },
    dashboards: { kitchen: { name: "Kitchen tablet", ha_user_id: "u2", cameras: [] }, hall: { name: "Hall tablet", ha_user_id: "u3", cameras: [] } },
    endpoints: {},
  };
}

// ---- 1. pure: the page list --------------------------------------------------------------------------------------------------------
{
  const d = doc6();
  const a = L.addDevicePage(d, "hall", "home", MAP);
  assert.deepStrictEqual(a.devices.hall.pages, [{ page: "home" }]);
  assert.deepStrictEqual(d.devices.hall.pages, [], "the old document is untouched");
  const b = L.addDevicePage(a, "hall", "side", MAP);
  assert.deepStrictEqual(L.devicePageIds(b, "hall"), ["home", "side"], "added at the end; the first is Home");
  assert.deepStrictEqual(L.devicePageIds(L.moveDevicePage(b, "hall", "side", -1), "hall"), ["side", "home"]);
  assert.deepStrictEqual(L.devicePageIds(L.moveDevicePage(b, "hall", "home", -1), "hall"), ["home", "side"], "the first cannot go up");
  assert.deepStrictEqual(L.devicePageIds(L.removeDevicePage(b, "hall", "home"), "hall"), ["side"]);
  throwsEdit(() => L.addDevicePage(b, "hall", "home", MAP), /already in the list/);
  throwsEdit(() => L.addDevicePage(d, "hall", "front-ring", MAP), /belongs to a Talk element and is not listed on a device/);
  throwsEdit(() => L.addDevicePage(d, "hall", "nope", MAP), /Pick a page/);
  throwsEdit(() => L.removeDevicePage(d, "hall", "home"), /not in the list/);
  // a device with no entry yet starts empty
  const e = L.addDevicePage(d, "new-phone", "home", MAP);
  assert.deepStrictEqual(e.devices["new-phone"], { pages: [{ page: "home" }], roster_hide: [] });
  // the twelve-page limit
  let big = doc6();
  for (let i = 0; i < 12; i++) { big.pages.push(page(`p${i}`, `P${i}`)); big = L.addDevicePage(big, "hall", `p${i}`, MAP); }
  throwsEdit(() => L.addDevicePage(big, "hall", "home", MAP), /at most 12 pages/);
  console.log("PASS: add, remove, reorder; Home is first; a Ring page, a duplicate and the limit are refused");
}

// ---- 3. pure: ownership, conflicts, wakes ----------------------------------------------------------------------------------------------
{
  const d = doc6();
  const parties = L.talkPartiesOf(MAP);
  assert.deepStrictEqual(L.doorOwners(d, parties), { [T_FRONT.id]: ["kitchen"] }, "only screens own; a phone does not");
  assert.strictEqual(L.wakesOnText(d, "kitchen", MAP), "Wakes on: Front door, Kitchen tablet (its own call element)");
  assert.strictEqual(L.wakesOnText(d, "hall", MAP), "Wakes on: Hall tablet (its own call element)");
  assert.strictEqual(L.wakesOnText(d, "hall", new Map([[T_FRONT.id, T_FRONT]])), "Wakes on: nothing (no Talk element on its pages)");
  // the same element on two screens: allowed, and each screen wakes on it
  const shared = doc6();
  shared.devices.hall.pages = [{ page: "front" }];
  assert.deepStrictEqual(L.doorOwners(shared, parties)[T_FRONT.id], ["hall", "kitchen"]);
  assert.strictEqual(L.wakesOnText(shared, "kitchen", MAP), "Wakes on: Front door, Kitchen tablet (its own call element)");
  assert.strictEqual(L.wakesOnText(shared, "hall", MAP), "Wakes on: Front door, Hall tablet (its own call element)");
  assert.strictEqual(L.addDevicePage(doc6(), "hall", "front", MAP).devices.hall.pages.length, 1, "adding the page the kitchen holds is not refused");
  // two different elements for the same door are two elements: allowed
  const two = doc6();
  two.pages.push(page("front2", "Front 2", "el-20000003"));
  const m2 = new Map([...MAP, ["el-20000003", { id: "el-20000003", kind: "talk", name: "Front door 2", options: { door: T_FRONT.options.door } }]]);
  assert.strictEqual(L.addDevicePage(two, "hall", "front2", m2).devices.hall.pages.length, 1);
  // unknown elements (the list has not loaded): nothing is claimed, the server stays the authority
  assert.doesNotThrow(() => L.addDevicePage(doc6(), "hall", "front", null));
  console.log("PASS: ownership by own pages; an element on two screens wakes both");
}


// ---- 4. pure: the retired per-device ring helpers are gone; a stale `ring` map (a restored document) is carried, never edited ------------
{
  const d = doc6();
  for (const name of ["legacyRing", "legacyRingRows", "ringRowsFor", "setDeviceRing", "setRingTalk"]) assert.strictEqual(L[name], undefined, name);
  const e = L.addDevicePage(d, "phone1", "side", MAP);
  assert.deepStrictEqual(e.devices.phone1.ring, { [T_FRONT.id]: "front-ring" }, "the stale map passes through untouched");
  assert.deepStrictEqual(L.devicesUsingPage(d, "front-ring"), [], "a Ring page has no device user: it belongs to its Talk element");
  console.log("PASS: the legacy ring helpers are gone and a stale ring map does not crash an edit");
}

// ---- 5. the panel: Devices tab, schema 6 ---------------------------------------------------------------------------------------------------
const PHONE_ROWS = [{ entry_id: "e-1", legacy: false, name: "Alice's phone", state: "enrolled", expired: false, code_expires_in: null, enrolled_at: "2026-10-08T07:00:00+00:00",
  last_connection: "2026-11-09T07:00:00+00:00", push_registered: true, doors: null, away_ring: null, house_receive: false }];
async function mount(document) {
  const loaded = makeLoaded();
  loaded.document = document;
  loaded.orphans = [];
  loaded.resolved = {};
  loaded.endpoints.push({ id: "hall", kind: "dashboard", name: "Hall tablet", label: "Hall tablet", ha_user_id: "u3", cameras: [], room: null, available: true, reason: null });
  const f = editorHass({ "portico/elements/list": () => listReply(ELS), "portico/phones/list": () => ({ header: {}, phones: PHONE_ROWS }), "portico/house_calls/get": () => ({ enabled: false, live: false, push_entry: true, phones_receiving: 1, active_calls: 0 }),
    "portico/content/preview": () => ({ endpoint: "x", kind: "phone", rev: "r", pages: [], roster: [], callers: {} }) }, { loaded });
  const panel = doc.createElement("portico-panel");
  panel._previewMs = 10;
  panel.hass = f.hass;
  doc.body.appendChild(panel);
  await sleep(80);
  panel._pickTab("devices");
  await sleep(40);
  return { f, panel };
}
{
  const { f, panel } = await mount(doc6());
  const phoneBlock = q(panel, '[data-device="phone"][data-phone="e-1"]');
  assert.ok(phoneBlock, "a block for the phone");
  assert.ok(phoneBlock.querySelector('[data-selector="pages"][data-device="phone1"]') && !phoneBlock.querySelector('[data-selector="ring-pages-legacy"]'), "no ring list at all (R-D): the stale map is not shown");
  assert.ok(!phoneBlock.querySelector('[data-selector="ring-pages"]') && !phoneBlock.querySelector('select[data-field^="device-ring"]'), "the Ring pages selector is gone");
  // the screen detail
  const sel = (key, name) => q(panel, `[data-device="screen"] [data-selector="${name}"][data-device="${key}"]`);
  assert.ok(sel("kitchen", "pages") && !sel("kitchen", "ring-pages") && !sel("kitchen", "ring-pages-legacy"), "the screen's Pages selector is mounted; it holds no old ring choice, so no ring list");
  assert.match(q(panel, '[data-device="screen"][data-screen="kitchen"] [data-hint="wakes-on"]').textContent, /^Wakes on: Front door, Kitchen tablet \(its own call element\)$/);
  assert.ok(!act(panel, "edit-screen-pages"), "no Pages button in a schema 6 document");
  // add a page to the phone
  const pick = phoneBlock.querySelector('[data-field="device-page-pick"]');
  assert.deepStrictEqual(Array.from(pick.options).map((o) => o.value), ["", "front", "side"], "only content pages not yet listed");
  setValue(pick, "side", "change");
  click(phoneBlock.querySelector('[data-act="device-page-add"]'));
  await sleep(30);
  assert.deepStrictEqual(L.devicePageIds(panel._state.draft, "phone1"), ["home", "side"]);
  assert.ok(q(panel, '[data-status="dirty"]') || q(panel, '[data-state="dirty"]') || panel._state.undo.length, "an edit is a draft change until Save");
  // order, remove
  click(q(panel, '[data-device="phone"][data-phone="e-1"] [data-device-page="side"] [data-act="device-page-up"]'));
  await sleep(30);
  assert.deepStrictEqual(L.devicePageIds(panel._state.draft, "phone1"), ["side", "home"]);
  click(q(panel, '[data-device="phone"][data-phone="e-1"] [data-device-page="home"] [data-act="device-page-remove"]'));
  await sleep(30);
  assert.deepStrictEqual(L.devicePageIds(panel._state.draft, "phone1"), ["side"]);
  // add (a new device starts empty; the normal add control is the only way in)
  assert.ok(!q(panel, '[data-field="device-copy-from"]') && !q(panel, '[data-act="device-pages-copy"]'), "no copy control");
  setValue(q(panel, '[data-device="phone"][data-phone="e-1"] [data-field="device-page-pick"]'), "front", "change");
  click(q(panel, '[data-device="phone"][data-phone="e-1"] [data-act="device-page-add"]'));
  await sleep(30);
  assert.deepStrictEqual(L.devicePageIds(panel._state.draft, "phone1"), ["side", "front"]);
  click(q(panel, '[data-device="phone"][data-phone="e-1"] [data-device-page="side"] [data-act="device-page-remove"]'));
  await sleep(30);
  assert.deepStrictEqual(L.devicePageIds(panel._state.draft, "phone1"), ["front"]);
  // the whole thing goes out in the save as schema 6
  assert.strictEqual(L.saveRequest(panel._state).document.schema_version, 6);
  assert.deepStrictEqual(L.saveRequest(panel._state).document.devices.phone1.pages, [{ page: "front" }]);
  panel.remove();
  console.log("PASS: the phone's and the screen's Pages selector mounts; add, reorder, remove edit the draft; no ring list");
}

// ---- 5b. the layout: one section per device, phones then screens, House calls and the push ring notification on Settings -------------------------------
{
  const { panel } = await mount(doc6());
  const tab = q(panel, '[data-tab="devices"]');
  const secs = Array.from(tab.querySelectorAll('section[data-device]'));
  assert.deepStrictEqual(secs.map((x) => `${x.dataset.device}:${x.dataset.phone || x.dataset.screen}`), ["phone:e-1", "screen:hall", "screen:kitchen"], "phones first, then screens, each alphabetical");
  assert.ok(!tab.querySelector("details"), "nothing is collapsed");
  assert.ok(!tab.querySelector("table") && !tab.querySelector('[data-block="house"]') && !tab.querySelector('[data-block="ui-settings"]'), "no table, no House calls, no push ring notification block on Devices");
  const bar = tab.querySelector('[data-act="ph-enrol"]');
  assert.ok(bar && tab.querySelector('[data-act="ph-refresh"]') && (bar.compareDocumentPosition(secs[0]) & 4), "Enrol a device and Refresh come first");
  for (const sec of secs) {
    assert.ok(sec.querySelector(".ph-head, .devhead").textContent.match(/Phone|Screen/), "a kind chip");
    assert.ok(sec.querySelector('[data-selector="pages"]') && !sec.querySelector('[data-selector="ring-pages"]'), "the Pages selector only");
  }
  const menuItems = (sec) => Array.from(sec.querySelectorAll('[role="menuitem"]')).map((b) => b.textContent);
  assert.deepStrictEqual(menuItems(secs[0]), ["Edit", "Make a panel", "Delete"], "an enrolled phone entry: Edit, Make a panel (DEV-807), Delete");
  assert.deepStrictEqual(menuItems(secs[1]), ["Edit", "Delete"]);
  assert.match(secs[0].textContent, /Last connection/);
  assert.ok(secs[2].querySelector('[data-field="card-snippet"]') && secs[2].querySelector('[data-field="screen-name"]') && secs[2].querySelector('[data-hint="wakes-on"]'), "a screen: snippet, name, wakes on");
  assert.ok(secs[1].querySelector('[data-field="card-snippet"]'), "every screen has its own snippet");
  // Settings: House calls and the push ring notification and app
  panel._pickTab("settings");
  await sleep(60);
  assert.ok(!q(panel, '[data-block="house"]'), "no House calls switch on the Settings tab (D-20261122-aafb)");
  assert.ok(q(panel, '[data-block="ui-settings"]'), "Push ring notification and app is on the Settings tab");
  panel.remove();
  console.log("PASS: Devices is Enrol and Refresh, then one section per phone and screen; House calls and the push ring notification are on Settings");
}

// ---- 6. the panel: adding a page the other screen holds is allowed, no banner, "Wakes on" lists it on both ------------------------------------
{
  const { panel } = await mount(doc6());
  // hall adds the page that holds the front door's Talk element, which the kitchen holds already
  const hall = () => q(panel, '[data-device="screen"] [data-selector="pages"][data-device="hall"]');
  panel._state.screenId = "hall";
  panel._render();
  await sleep(20);
  setValue(hall().querySelector('[data-field="device-page-pick"]'), "front", "change");
  click(hall().querySelector('[data-act="device-page-add"]'));
  await sleep(30);
  assert.ok(!q(panel, '[data-state="message"]') || !/already on screen/.test(q(panel, '[data-state="message"]').textContent), "no refusal");
  assert.deepStrictEqual(L.devicePageIds(panel._state.draft, "hall"), ["front"]);
  assert.ok(!q(panel, '[data-state="talk-conflicts"]'), "no banner");
  panel.remove();

  const shared = doc6();
  shared.devices.hall.pages = [{ page: "front" }];
  const m = await mount(shared);
  assert.ok(!q(m.panel, '[data-state="talk-conflicts"]'), "no banner for an element on two screens");
  assert.match(q(m.panel, '[data-device="screen"][data-screen="kitchen"] [data-hint="wakes-on"]').textContent, /^Wakes on: Front door, Kitchen tablet/, "both wake");
  m.panel.remove();
  console.log("PASS: a page the other screen holds can be added; no refusal, no banner, both screens wake");
}

// ---- 7. schema 4/5: the Devices tab is as it was -------------------------------------------------------------------------------------------
{
  const f = editorHass({ "portico/elements/list": () => listReply(ELS) }, { loaded: makeLoaded() });
  const panel = doc.createElement("portico-panel");
  panel.hass = f.hass;
  doc.body.appendChild(panel);
  await sleep(80);
  panel._pickTab("devices");
  await sleep(40);
  assert.ok(!q(panel, '[data-selector="pages"]') && !q(panel, '[data-section="phone-pages"]') && !q(panel, '[data-state="talk-conflicts"]'));
  panel.remove();
  console.log("PASS: a schema 4/5 document keeps the Devices tab untouched");
}

console.log("ALL PASS test_panel_devices_selectors");
process.exit(0);
