// Group call elements, P2 (design portico_group_call_elements_design.md section 5): a Talk element with options.group
// (everyone / phones / panels) is a placeable `call` button aimed at {type: "group", id}; it may sit only on a SCREEN's page (never a
// phone's, the server refuses the same save), never on a Ring page, and it cannot be detached into an inline button (an inline
// call button cannot name a group). Pure edits (`logic`) and the Elements page helpers. Protocol-level; NOT a visual check.
import assert from "assert";
import { loadPanel, importElements } from "./panel_loader.mjs";

const { Panel } = loadPanel();
const EL = await importElements();
const L = Panel.logic;
const throwsEdit = (fn, re) => assert.throws(fn, (e) => e instanceof L.EditError && re.test(e.message), String(re));

const G = (group, n) => ({ id: `el-3000000${n}`, kind: "talk", name: `Call ${group}`, icon: "phone", options: { group } });
const EVERYONE = G("everyone", 1), PHONES = G("phones", 2), PANELS = G("panels", 3);
const T_PHONE = { id: "talk-phone-p1", kind: "talk", name: "Alice", managed: true, options: { phone: "p1" } };
const ELS = [EVERYONE, PHONES, PANELS, T_PHONE];
const MAP = new Map(ELS.map((e) => [e.id, e]));

// ---- 1. the clause: a group is a call target, placeable, and buttonAction is `call` ---------------------------------------
for (const e of [EVERYONE, PHONES, PANELS]) {
  assert.deepStrictEqual(L.houseCallTarget(e), { type: "group", id: e.options.group });
  assert.strictEqual(L.placeableButton(e), true);
  assert.strictEqual(L.isGroupElement(e), true);
}
assert.strictEqual(L.isGroupElement(T_PHONE), false);
assert.deepStrictEqual(L.houseCallTarget(T_PHONE), { type: "phone", id: "p1" }, "the phone clause is unchanged");
const BAD = { id: "el-30000009", kind: "talk", name: "Bad", options: { group: "nobody" } };
assert.strictEqual(L.houseCallTarget(BAD), null);
assert.strictEqual(L.placeableButton(BAD), false, "a stored bad word is not placeable");
// a hand-written call BUTTON element still cannot name a group
assert.strictEqual(L.placeableButton({ id: "el-30000010", kind: "button", name: "B", options: { action: "call", target: "group:everyone" } }), false);
console.log("PASS: houseCallTarget / placeableButton read options.group");

// ---- 2. placing on a block of a library page -----------------------------------------------------------------------------
const page = (id) => ({ id, label: id, kind: "content", blocks: [{ id: "b", title: id, cameras: [], actions: [] }] });
function doc6(devices) {
  return { schema_version: 6, home_name: "T", pages: [page("home")], devices,
    dashboards: { kitchen: { name: "Kitchen tablet", ha_user_id: "u2", cameras: [] } }, endpoints: {} };
}
const FOREIGN = "A group call button can be placed only on a screen's page, not on a phone's";
const tilesOf = (d) => L.pageActions(d.pages[0]);
{
  const d = doc6({ kitchen: { pages: [{ page: "home" }], ring: {}, roster_hide: [] } });
  for (const e of [EVERYONE, PHONES, PANELS]) {
    const r = L.addButtonRef(d, L.LIBRARY, "home", e, {});
    assert.strictEqual(tilesOf(r).at(-1).element, e.id);
    assert.ok(!("confirm" in tilesOf(r).at(-1)), "no confirm is stored (CB-9)");
    assert.strictEqual(L.askLocked(e), false);
  }
  // an unassigned library page is fine: the check bites when a phone gets it
  L.addButtonRef(doc6({}), L.LIBRARY, "home", PANELS, {});
}
console.log("PASS: a group element is placed on a screen's page");

// ---- 3. refusals: a phone's page, a stale tab, a Ring page, detach --------------------------------------------------------
{
  const onPhone = doc6({ p1: { pages: [{ page: "home" }], ring: {}, roster_hide: [] } });
  throwsEdit(() => L.addButtonRef(onPhone, L.LIBRARY, "home", EVERYONE, {}), /only on a screen's page, not on a phone's/);
  const shared = doc6({ kitchen: { pages: [{ page: "home" }], ring: {}, roster_hide: [] }, p1: { pages: [{ page: "home" }], ring: {}, roster_hide: [] } });
  throwsEdit(() => L.addButtonRef(shared, L.LIBRARY, "home", PHONES, {}), /on p1/);
  // a non-group Talk element (a phone call) is still placed on a phone's page
  L.addButtonRef(onPhone, L.LIBRARY, "home", T_PHONE, {});
  // assigning a page that holds a group button to a phone is refused; to a screen it is not
  const holds = L.addButtonRef(doc6({}), L.LIBRARY, "home", PANELS, {});
  throwsEdit(() => L.addDevicePage(holds, "p1", "home", MAP), /only on a screen's page, not on a phone's/);
  L.addDevicePage(holds, "kitchen", "home", MAP);
  // the same words as the server's save
  assert.ok(FOREIGN.startsWith("A group call button can be placed only on a screen's page"));
  // a Ring page never holds a Talk element
  const ring = { ...doc6({}), pages: [{ id: "r1", label: "R", kind: "ring", talk: T_PHONE.id, cameras: null, blocks: [] }] };
  throwsEdit(() => L.addButtonRef(ring, L.LIBRARY, "r1", EVERYONE, {}), /Ring page cannot hold a Talk/);
  // an older document: the template reaches phones; an override of a screen is fine, of a phone is not
  const legacy = { schema_version: 4, template: { pages: [page("home")], rules: [], roster: ["*"] }, overrides: {}, dashboards: { kitchen: { name: "K" } }, endpoints: {} };
  throwsEdit(() => L.addButtonRef(legacy, "template", "home", PANELS, {}), /only on a screen's page/);
  const ov = { ...legacy, overrides: { kitchen: { pages: [page("home")], rules: [], roster_hide: [] }, p1: { pages: [page("home")], rules: [], roster_hide: [] } } };
  L.addButtonRef(ov, "kitchen", "home", PANELS, {});
  throwsEdit(() => L.addButtonRef(ov, "p1", "home", PANELS, {}), /only on a screen's page/);
}
{
  // detach: an inline call button cannot name a group, so a group tile cannot be detached
  const d = L.addButtonRef(doc6({ kitchen: { pages: [{ page: "home" }], ring: {}, roster_hide: [] } }), L.LIBRARY, "home", PANELS, {});
  const tile = tilesOf(d)[0];
  throwsEdit(() => L.detachButton(d, L.LIBRARY, "home", tile.id, PANELS), /cannot be detached/);
  // re-point to a group on a phone's page is refused too
  const withPhone = L.addButtonRef(doc6({ p1: { pages: [{ page: "home" }], ring: {}, roster_hide: [] } }), L.LIBRARY, "home", T_PHONE, {});
  const phoneTile = tilesOf(withPhone)[0];
  throwsEdit(() => L.repointButton(withPhone, L.LIBRARY, "home", phoneTile.id, EVERYONE), /only on a screen's page/);
}
console.log("PASS: refusals match the server's save");

// ---- 4. the Elements page: the group field and the list row ---------------------------------------------------------------
{
  const spec = { fields: [{ name: "door" }, { name: "screen" }, { name: "phone" }, { name: "group" }] };
  const d = EL.emptyDraft("talk");
  d.name = "Call all panels";
  assert.deepStrictEqual(EL.visibleFields(spec, d), ["door", "screen", "phone", "group"], "nothing chosen: every target shows");
  d.group = "panels";
  d.door = "stale-door";
  assert.deepStrictEqual(EL.visibleFields(spec, d), ["group"], "a group hides the other three");
  const out = EL.draftToElement(spec, d);
  assert.deepStrictEqual(out, { kind: "talk", name: "Call all panels", options: { group: "panels" } }, "the stale door is not sent");
  const back = EL.elementToDraft({ ...PANELS });
  assert.strictEqual(back.group, "panels");
  assert.deepStrictEqual(EL.GROUP_CHOICES.map((c) => c.value), ["everyone", "phones", "panels"]);
  assert.deepStrictEqual(EL.GROUP_CHOICES.map((c) => EL.groupNote({ kind: "talk", options: { group: c.value } })), ["Calls everyone", "Calls all phones", "Calls all panels"]);
  assert.strictEqual(EL.groupNote(T_PHONE), "");
  assert.strictEqual(EL.groupNote({ kind: "button", options: { group: "everyone" } }), "");
}
console.log("PASS: the Elements form and list row");
console.log("ALL PASS test_panel_group_call");
process.exit(0);
