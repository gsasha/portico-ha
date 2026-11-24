// Panel owns its pages (own-pages design): the editor's mirror of the server's ownership rules (`panels` = [{id, user, name}], the phone
// endpoint rows with `panel: true`): the panel's phone id owns door Talk elements on its own pages, a FORMER panel screen (its login is a
// panel's) does not, a group call element may sit on a panel's pages, and "Rings for these doors". Pure edits only; NOT a visual check.
import assert from "assert";
import { loadPanel } from "./panel_loader.mjs";

const { Panel } = loadPanel();
const L = Panel.logic;
const throwsEdit = (fn, re) => assert.throws(fn, (e) => e instanceof L.EditError && re.test(e.message), String(re));

const T_FRONT = { id: "el-20000001", kind: "talk", name: "Front door", options: { door: "gate-1" } };
const T_SIDE = { id: "el-20000002", kind: "talk", name: "Side door", options: { door: "side-1" } };
const T_GROUP = { id: "el-20000003", kind: "talk", name: "Call all", options: { group: "everyone" } };
const MAP = new Map([T_FRONT, T_SIDE, T_GROUP].map((e) => [e.id, e]));
const PANELS = [{ id: "kp-phone", user: "u9", name: "Kitchen panel" }];
const page = (id, label, ...talks) => ({ id, label, kind: "content", blocks: [{ id: "b", title: label, cameras: [], actions: talks.map((t) => ({ id: `a-${t}`, element: t })) }] });

/* kp-phone is the panel; kitchen-panel-app its former screen (same login); hall a browser screen. */
function doc6() {
  return {
    schema_version: 6, pages: [page("home", "Home"), page("front", "Front", T_FRONT.id), page("side", "Side", T_SIDE.id), page("group", "Group", T_GROUP.id)],
    devices: { "kp-phone": { pages: [{ page: "front" }], ring: {}, roster_hide: [] }, "kitchen-panel-app": { pages: [{ page: "front" }], ring: {}, roster_hide: [] },
               hall: { pages: [{ page: "side" }], ring: {}, roster_hide: [] } },
    dashboards: { "kitchen-panel-app": { name: "Kitchen panel", ha_user_id: "u9", cameras: [] }, hall: { name: "Hall tablet", ha_user_id: "u3", cameras: [] } },
    endpoints: {},
  };
}

{
  const d = doc6();
  const parties = L.talkPartiesOf(MAP);
  assert.deepStrictEqual(L.ownerKeys(d, []), ["kitchen-panel-app", "hall"], "no panels: every declared screen owns, as before");
  assert.deepStrictEqual(L.ownerKeys(d, PANELS), ["hall", "kp-phone"], "a former panel screen is not an owner; the panel's phone id is");
  assert.deepStrictEqual(L.doorOwners(d, parties), { [T_FRONT.id]: ["kitchen-panel-app"], [T_SIDE.id]: ["hall"] }, "without the panel list: unchanged");
  assert.deepStrictEqual(L.doorOwners(d, parties, PANELS), { [T_FRONT.id]: ["kp-phone"], [T_SIDE.id]: ["hall"] });
  assert.strictEqual(L.wakesOnText(d, "kp-phone", MAP, PANELS), "Wakes on: Front door");
  assert.strictEqual(L.ringsForText(d, "kp-phone", MAP, PANELS), "Rings for these doors: Front door");
  assert.strictEqual(L.ringsForText(d, "kitchen-panel-app", MAP, PANELS), "Rings for these doors: none (no door Talk element on its pages)", "the former screen wakes for nothing");
  assert.strictEqual(L.ringsForText(doc6(), "hall", MAP, PANELS), "Rings for these doors: Side door");
  console.log("PASS: ownership mirror: the panel phone id owns, a former screen does not");
}

{
  // a door element put on a browser screen that the panel holds is allowed, and both own it (D-20261122-42b1)
  const d = doc6();
  const both = L.addDevicePage(d, "hall", "front", MAP, PANELS);
  assert.deepStrictEqual(L.doorOwners(both, L.talkPartiesOf(MAP), PANELS)[T_FRONT.id], ["hall", "kp-phone"]);
  const two = [...PANELS, { id: "pl-phone", user: "u8", name: "Playroom panel" }];
  assert.deepStrictEqual(L.devicePageIds(L.addDevicePage(d, "pl-phone", "front", MAP, two), "pl-phone"), ["front"]);
  assert.strictEqual(L.addDevicePage(d, "pl-phone", "home", MAP, two).devices["pl-phone"].pages.length, 1, "an unrelated page is fine");
  console.log("PASS: a second owner of a door Talk element is allowed");
}

{
  // a group call element: a panel is a screen for it; a plain phone is not
  const d = doc6();
  d.devices["ph-1"] = { pages: [], ring: {}, roster_hide: [] };
  L.setPanelHosts(PANELS);                // the editor sets it from the endpoint rows on every render
  throwsEdit(() => L.addDevicePage(d, "ph-1", "group", MAP, PANELS), /group call/);
  assert.deepStrictEqual(L.devicePageIds(L.addDevicePage(d, "kp-phone", "group", MAP, PANELS), "kp-phone"), ["front", "group"], "allowed on the panel");
  throwsEdit(() => L.addDevicePage(d, "kp-phone", "group", MAP, []), /group call/);   // not known as a panel: refused (the server decides in the end)
  // placing a group button on a page: a page a panel shows is fine, a page a plain phone shows is not
  const withGroup = (page_) => ({ ...doc6(), pages: [...doc6().pages, page_] });
  const blank = { id: "blank", label: "Blank", kind: "content", blocks: [{ id: "b", title: "B", cameras: [], actions: [] }] };
  const onPanel = withGroup(blank); onPanel.devices["kp-phone"].pages = [{ page: "blank" }];
  const onPhone = withGroup(blank); onPhone.devices["ph-1"] = { pages: [{ page: "blank" }], ring: {}, roster_hide: [] };
  const groupEl = { ...T_GROUP, options: { ...T_GROUP.options }, kind: "talk" };
  assert.ok(L.addButtonRef(onPanel, "@library", "blank", groupEl, null, null), "a group button goes on a panel's page");
  throwsEdit(() => L.addButtonRef(onPhone, "@library", "blank", groupEl, null, null), /group call button/);
  assert.strictEqual(L.addDevicePage(d, "hall", "group", MAP, PANELS).devices.hall.pages.length, 2, "a screen as before");
  console.log("PASS: a group call button may be put on a panel's page and not on a plain phone's");
}

console.log("ALL PASS test_panel_own_pages");
process.exit(0);
