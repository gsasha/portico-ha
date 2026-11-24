// M6 K3: the Screens tab and the Ring & call tab. The state model and validation (pure `logic`), the save
// payloads, and the two tabs in jsdom with a fake `hass`. Protocol-level; NOT a visual check.
import assert from "assert";
import { loadPanel, editorHass, makeLoaded, sleep, DOOR_ID, STATES } from "./panel_loader.mjs";

const { dom, Panel, doc } = loadPanel();
const L = Panel.logic;
const win = dom.window;
const throwsEdit = (fn, re) => assert.throws(fn, (e) => e instanceof L.EditError && re.test(e.message), String(re));

const USERS = [
  { id: "u-admin", name: "Alice", is_admin: true, kiosk_open: false },
  { id: "u1", name: "Alice phone user", is_admin: false, kiosk_open: false },
  { id: "u2", name: "Tablet", is_admin: false, kiosk_open: true },
  { id: "u3", name: "Hall tablet", is_admin: false, kiosk_open: false },
];

/* The shared fixture plus users, a stored screen and two rules. */
function k3Loaded() {
  const loaded = makeLoaded();
  loaded.ha_users = USERS.map((u) => ({ ...u }));
  loaded.document.dashboards = { kitchen: { name: "Kitchen tablet", ha_user_id: "u2", room: "Kitchen", cameras: ["camera.gate"] } };
  loaded.document.template.pages[0].blocks[0].actions.push({ id: "open-hall", label: "Hall door", kind: "lock_open", entity_id: "lock.front", confirm: true });
  loaded.document.template.rules = [
    { id: "any-door", caller: "kind:door", cameras: null, ring_buttons: ["open-gate"], call_buttons: [] },
  ];
  loaded.doc_rev = "rev1";
  return loaded;
}
const kstate = () => L.createState(k3Loaded());
const names = (list) => list.map((x) => x.value);

// 1. addScreen: slug from the name or typed, the slug rule, collisions, the user, the room
{
  const s = kstate();
  const taken = L.takenEndpointIds(s);
  assert.ok(taken.has("kitchen") && taken.has("phone1") && taken.has(DOOR_ID), "screens, phones and doors share one id space");
  const r = L.addScreen(s.draft, { name: "  Hall tablet ", ha_user_id: "u3", room: " Hall " }, taken);
  assert.strictEqual(r.slug, "hall-tablet");
  assert.deepStrictEqual(r.doc.dashboards["hall-tablet"], { name: "Hall tablet", ha_user_id: "u3", room: "Hall", cameras: [] });
  assert.ok(!("hall-tablet" in s.draft.dashboards), "the old document is untouched");
  assert.strictEqual(L.addScreen(s.draft, { name: "Another", slug: "wall_2", ha_user_id: "u3" }, taken).slug, "wall_2");
  assert.deepStrictEqual(L.addScreen(s.draft, { name: "No room", ha_user_id: "u3" }, taken).doc.dashboards["no-room"],
    { name: "No room", ha_user_id: "u3", cameras: [] }, "an empty room is left out");
  // a name that makes a taken slug gets a suffix; a typed slug that is taken is refused
  assert.strictEqual(L.addScreen(s.draft, { name: "Kitchen", ha_user_id: "u3" }, taken).slug, "kitchen-2");
  throwsEdit(() => L.addScreen(s.draft, { name: "X", slug: "kitchen", ha_user_id: "u3" }, taken), /already used/);
  throwsEdit(() => L.addScreen(s.draft, { name: "X", slug: "phone1", ha_user_id: "u3" }, taken), /already used/);
  for (const bad of ["Has Space", "UPPER", "-lead", "x".repeat(33), "a/b", "a.b"]) {
    throwsEdit(() => L.addScreen(s.draft, { name: "X", slug: bad, ha_user_id: "u3" }, taken), /1-32 characters/);
  }
  throwsEdit(() => L.addScreen(s.draft, { name: "", ha_user_id: "u3" }, taken), /needs a name/);
  throwsEdit(() => L.addScreen(s.draft, { name: "X".repeat(41), ha_user_id: "u3" }, taken), /longer than 40/);
  throwsEdit(() => L.addScreen(s.draft, { name: "X", ha_user_id: "" }, taken), /Pick the Home Assistant user/);
  throwsEdit(() => L.addScreen(s.draft, { name: "X", ha_user_id: "u3", room: "R".repeat(41) }, taken), /room is longer/);
  console.log("PASS: a screen needs a name, a valid free address and a user; the address comes from the name");
}

// 2. editing a screen: name, user, room, cameras; the address never changes; delete
{
  const s = kstate();
  let doc = L.updateScreen(s.draft, "kitchen", { name: " Kitchen wall ", room: "" });
  assert.deepStrictEqual(doc.dashboards.kitchen, { name: "Kitchen wall", ha_user_id: "u2", cameras: ["camera.gate"] }, "an empty room removes the key");
  doc = L.updateScreen(doc, "kitchen", { ha_user_id: "u3" });
  assert.strictEqual(doc.dashboards.kitchen.ha_user_id, "u3");
  throwsEdit(() => L.updateScreen(doc, "kitchen", { ha_user_id: "" }), /Pick the Home Assistant user/);
  throwsEdit(() => L.updateScreen(doc, "nope", { name: "x" }), /gone/);
  doc = L.addScreenCamera(doc, "kitchen", "camera.garage");
  assert.deepStrictEqual(doc.dashboards.kitchen.cameras, ["camera.gate", "camera.garage"]);
  throwsEdit(() => L.addScreenCamera(doc, "kitchen", "camera.garage"), /already in the list/);
  throwsEdit(() => L.addScreenCamera(doc, "kitchen", "light.kitchen"), /Pick a camera/);
  doc = L.removeScreenCamera(doc, "kitchen", "camera.gate");
  assert.deepStrictEqual(doc.dashboards.kitchen.cameras, ["camera.garage"]);
  const many = { ...s.draft, dashboards: { kitchen: { ...s.draft.dashboards.kitchen, cameras: Array.from({ length: 24 }, (_, i) => `camera.c${i}`) } } };
  throwsEdit(() => L.addScreenCamera(many, "kitchen", "camera.extra"), /At most 24/);

  // delete: refused while a call button names the screen; otherwise its own pages are kept (Not attached)
  const withCall = L.clone(s.draft);
  withCall.template.pages[0].blocks[0].actions.push({ id: "call-kitchen", label: "Call kitchen", kind: "call", target: { type: "dashboard", id: "kitchen" } });
  assert.strictEqual(L.callersOfScreen(withCall, "kitchen").length, 1);
  throwsEdit(() => L.deleteScreen(withCall, "kitchen"), /call button still calls this screen/);
  const withPages = L.clone(s.draft);
  withPages.overrides.kitchen = { pages: [{ id: "home", label: "Home", blocks: [{ id: "home", title: "Home", cameras: [], actions: [] }] }], rules: [], roster_hide: [] };
  const gone = L.deleteScreen(withPages, "kitchen");
  assert.ok(!("kitchen" in gone.dashboards));
  assert.ok("kitchen" in gone.overrides, "its pages stay in the document");
  console.log("PASS: a screen can be edited and deleted; the address is fixed; a called screen cannot be deleted");
}

// 3. validation: slug rule, user must exist, id clashes, camera entities
{
  const doc = k3Loaded().document;
  const ctx = { users: new Set(USERS.map((u) => u.id)), otherIds: new Set(["phone1", DOOR_ID]) };
  assert.deepStrictEqual(L.validate(doc, null, ctx), []);
  const bad = L.clone(doc);
  bad.dashboards = {
    "Bad Slug": { name: "A", ha_user_id: "u1", cameras: [] },
    phone1: { name: "B", ha_user_id: "u1", cameras: [] },
    ghost: { name: "C", ha_user_id: "u-deleted", cameras: [] },
    nouser: { name: "D", ha_user_id: "", cameras: [] },
    cams: { name: "E", ha_user_id: "u1", cameras: ["light.x"], room: "R".repeat(41) },
  };
  const problems = L.validate(bad, null, ctx).join("\n");
  assert.match(problems, /Screen "A": the address must be 1-32 characters/);
  assert.match(problems, /Screen "B": the address "phone1" is already used by a phone or a door/);
  assert.match(problems, /Screen "C": its Home Assistant user no longer exists/);
  assert.match(problems, /Screen "D": pick the Home Assistant user/);
  assert.match(problems, /Screen "E": the room is longer than 40/);
  assert.match(problems, /Screen "E": a room camera is not a camera/);
  // without a user list (older server part) the user is not checked, the rest still is
  assert.ok(!/no longer exists/.test(L.validate(bad, null, { users: null, otherIds: ctx.otherIds }).join("\n")));
  console.log("PASS: validation: the slug rule, the user must exist, no clash with a phone or door, real cameras");
}

// 4. the picker: non-administrators only, the open-now marker, the current user kept even when gone or an administrator
{
  const s = kstate();
  const opts = L.screenUserOptions(s, null);
  assert.deepStrictEqual(names(opts), ["u3", "u2", "u1"].sort((a, b) => USERS.find((u) => u.id === a).name.localeCompare(USERS.find((u) => u.id === b).name)));
  assert.ok(!opts.some((o) => o.value === "u-admin"), "an administrator is never offered");
  assert.strictEqual(opts.find((o) => o.value === "u2").label, "Tablet (open now)");
  assert.strictEqual(opts.find((o) => o.value === "u3").label, "Hall tablet");
  assert.strictEqual(L.screenUserOptions(s, "u-gone").at(-1).label, "u-gone (no such user)");
  assert.strictEqual(L.screenUserOptions(s, "u-admin").at(-1).label, "Alice (administrator)");
  console.log("PASS: the user picker lists non-administrators with the open-now marker and keeps a stale choice visible");
}

// 5. the draft's screens show in the rows at once; a deleted stored one leaves them; the server's other rows stay
{
  const s = kstate();
  const before = L.rowsOf(s, ["dashboard"]).map((r) => r.id);
  assert.deepStrictEqual(before, ["kitchen"]);
  const added = L.addScreen(s.draft, { name: "Hall", ha_user_id: "u3" }, L.takenEndpointIds(s));
  L.commit(s, added.doc);
  assert.deepStrictEqual(L.rowsOf(s, ["dashboard"]).map((r) => r.id), ["kitchen", "hall"]);
  const order = L.allRows(s).map((r) => r.kind);
  assert.ok(order.indexOf("door") > order.lastIndexOf("dashboard"), "doors stay after the screens, as the registry orders them");
  L.commit(s, L.deleteScreen(s.draft, "kitchen"));
  assert.deepStrictEqual(L.rowsOf(s, ["dashboard"]).map((r) => r.id), ["hall"]);
  assert.strictEqual(L.rowName(L.rowsOf(s, ["dashboard"])[0]), "Hall");
  console.log("PASS: rows follow the draft: a new screen appears at once, a deleted one goes");
}

// 6. the save payload: screens go in document.dashboards, base_doc_rev is the loaded one, no extra keys
{
  const s = kstate();
  L.commit(s, L.addScreen(s.draft, { name: "Hall", ha_user_id: "u3", room: "Hall" }, L.takenEndpointIds(s)).doc);
  const req = L.saveRequest(s);
  assert.deepStrictEqual(Object.keys(req).sort(), ["base_doc_rev", "document", "type"]);
  assert.strictEqual(req.type, "portico/content/save");
  assert.strictEqual(req.base_doc_rev, "rev1");
  assert.deepStrictEqual(req.document.dashboards.hall, { name: "Hall", ha_user_id: "u3", room: "Hall", cameras: [] });
  assert.deepStrictEqual(req.document.dashboards.kitchen, s.loaded.document.dashboards.kitchen, "the stored screen is carried unchanged");
  assert.strictEqual(L.kioskUrl("hall", "http://192.168.7.20:8123"), "http://192.168.7.20:8123/portico/kiosk/hall");
  assert.strictEqual(L.kioskUrl("hall", "https://haos.example.net"), "https://haos.example.net/portico/kiosk/hall");
  console.log("PASS: a screen is saved in document.dashboards through content/save with base_doc_rev");
}

// 6b. the kiosk URL is built from the origin it is given (design 5.2: the tablet's own origin), and from nothing else
{
  assert.strictEqual(L.kioskUrl("kitchen-tablet", "http://10.9.8.7:8123"), "http://10.9.8.7:8123/portico/kiosk/kitchen-tablet");
  assert.strictEqual(L.kioskUrl("kitchen-tablet", "https://ha.example.org:8443"), "https://ha.example.org:8443/portico/kiosk/kitchen-tablet");
  assert.strictEqual(L.kioskUrl("a", "http://[fd00::5]:8123"), "http://[fd00::5]:8123/portico/kiosk/a");
  // hostile or unusable input: no URL at all, never a half-built one
  for (const bad of ["null", "", "ftp://x", "javascript:alert(1)", "http://x/", "http://x/path", "http://u:p@x", "http://x y", "https://x?a=1", "http://x#f", "http://", "//x", "http://x:123456", undefined, null, 5]) {
    assert.strictEqual(L.kioskUrl("hall", bad), null, String(bad));
  }
  for (const bad of ["Hall", "a/b", "a b", "../x", "", "a?b", "a#b", "<s>", "x".repeat(33), undefined, null]) {
    assert.strictEqual(L.kioskUrl(bad, "http://192.168.7.20:8123"), null, String(bad));
  }
  assert.ok(!("KIOSK_ORIGIN" in L), "no fixed origin is exported");
  for (const t of [L.KIOSK_ADDRESS_HINT, L.KIOSK_NO_ORIGIN_TEXT]) assert.ok(!/clear\s*web\s*storage/i.test(t), "never suggest clearing web storage");
  console.log("PASS: the kiosk URL is built from the given origin; hostile origins and slugs give no URL");
}

// 7. rules: add, the caller choices, the page-set buttons, limits, order, delete
{
  const s = kstate();
  assert.deepStrictEqual(names(L.ruleCallerChoices(s, "template")), ["kind:phone", "kind:dashboard", DOOR_ID], "the kind that has a rule is left out");
  assert.deepStrictEqual(names(L.ruleCallerChoices(s, "phone1")), ["kind:door", "kind:phone", "kind:dashboard", DOOR_ID], "another holder, another list");
  assert.deepStrictEqual(L.ruleButtonChoices(s.draft, "template").map((c) => c.id), ["open-gate", "open-hall"]);
  // a phone's own page joins the household's buttons
  const own = L.addAction(L.addPage(s.draft, "phone1", "Mine").doc, "phone1", "mine", { kind: "button_press", target: "button.garage", label: "Garage", confirm: false });
  assert.deepStrictEqual(L.ruleButtonChoices(own, "phone1").map((c) => c.id), ["open-gate", "open-hall", "garage-2"], "the id is unique across the whole document: an orphan override already has \"garage\"");
  assert.deepStrictEqual(L.ruleButtonChoices(own, "template").map((c) => c.id), ["open-gate", "open-hall"], "the household set never sees a phone's button");

  const r = L.addRule(s.draft, "phone1", { caller: DOOR_ID, label: "Gate", cameras: ["camera.gate"], ring_buttons: ["open-gate", "open-gate"], call_buttons: ["open-hall"] });
  assert.strictEqual(r.ruleId, "gate");
  assert.deepStrictEqual(L.rulesOf(r.doc, "phone1")[0],
    { id: "gate", caller: DOOR_ID, cameras: ["camera.gate"], ring_buttons: ["open-gate"], call_buttons: ["open-hall"] }, "duplicate ids collapse");
  assert.deepStrictEqual(L.rulesOf(r.doc, "template"), L.rulesOf(s.draft, "template"), "the other holder is untouched");
  const own2 = L.addRule(s.draft, "template", { caller: "kind:phone", label: "Any phone" }).doc;
  assert.deepStrictEqual(L.rulesOf(own2, "template")[1], { id: "any-phone", caller: "kind:phone", cameras: null, ring_buttons: [], call_buttons: [] });

  throwsEdit(() => L.addRule(s.draft, "template", { caller: "kind:door", label: "Again" }), /already a rule for that caller/);
  throwsEdit(() => L.addRule(s.draft, "template", { caller: "kind:window" }), /door, a phone or a screen/);
  throwsEdit(() => L.addRule(s.draft, "template", { caller: "" }), /Pick who the rule is for/);
  throwsEdit(() => L.addRule(s.draft, "template", { caller: "kind:phone", ring_buttons: ["nope"] }), /not on this page set/);
  throwsEdit(() => L.addRule(s.draft, "template", { caller: "kind:phone", cameras: ["light.x"] }), /Pick a camera/);
  const nine = Array.from({ length: 9 }, (_, i) => `b${i}`);
  const big = L.clone(s.draft);
  big.template.pages[0].blocks[0].actions.push(...nine.map((id) => ({ id, label: id, kind: "button_press", entity_id: "button.garage", confirm: false })));
  throwsEdit(() => L.addRule(big, "template", { caller: "kind:phone", ring_buttons: nine }), /At most 8 ring buttons/);
  const twelve = L.clone(s.draft);
  twelve.template.rules = Array.from({ length: 12 }, (_, i) => ({ id: `r${i}`, caller: `kind:x${i}`.replace("kind:", ""), cameras: null, ring_buttons: [], call_buttons: [] }));
  throwsEdit(() => L.addRule(twelve, "template", { caller: "kind:phone" }), /At most 12 rules/);

  // update, move, delete
  let doc = L.addRule(s.draft, "template", { caller: "kind:phone", label: "Any phone" }).doc;
  doc = L.updateRule(doc, "template", "any-phone", { cameras: [], call_buttons: ["open-hall"] });
  assert.deepStrictEqual(L.rulesOf(doc, "template")[1], { id: "any-phone", caller: "kind:phone", cameras: [], ring_buttons: [], call_buttons: ["open-hall"] });
  doc = L.updateRule(doc, "template", "any-phone", { cameras: null });
  assert.strictEqual(L.rulesOf(doc, "template")[1].cameras, null, "back to the caller's own cameras");
  throwsEdit(() => L.updateRule(doc, "template", "nope", { cameras: null }), /gone/);
  assert.deepStrictEqual(L.rulesOf(L.moveRule(doc, "template", "any-phone", -1), "template").map((x) => x.id), ["any-phone", "any-door"]);
  assert.deepStrictEqual(L.rulesOf(L.moveRule(doc, "template", "any-phone", 1), "template").map((x) => x.id), ["any-door", "any-phone"], "the end stays the end");
  assert.deepStrictEqual(L.rulesOf(L.deleteRule(doc, "template", "any-phone"), "template").map((x) => x.id), ["any-door"]);
  assert.deepStrictEqual(L.toggleId(["a"], "b"), ["a", "b"]);
  assert.deepStrictEqual(L.toggleId(["a", "b"], "a"), ["b"]);
  console.log("PASS: rules: caller choices, the page-set buttons, limits, update, order and delete");
}

// 8. a rule button the page set lost does not block unrelated edits, but validation reports it
{
  const s = kstate();
  const doc = L.clone(s.draft);
  doc.template.rules[0].ring_buttons = ["open-gate", "lost-button"];
  const kept = L.updateRule(doc, "template", "any-door", { ring_buttons: ["open-gate", "lost-button", "open-hall"] });
  assert.deepStrictEqual(L.rulesOf(kept, "template")[0].ring_buttons, ["open-gate", "lost-button", "open-hall"]);
  assert.match(L.validate(doc).join("\n"), /still uses the button "lost-button"/);
  // structural problems the server would refuse
  const bad = L.clone(s.draft);
  bad.template.rules = [
    { id: "a", caller: "kind:window", cameras: null, ring_buttons: [], call_buttons: [] },
    { id: "a", caller: "", cameras: ["light.x"], ring_buttons: [], call_buttons: [] },
  ];
  const out = L.validate(bad).join("\n");
  assert.match(out, /rule "a": the caller kind must be a door, a phone or a screen/);
  assert.match(out, /rule "a": the rule id is missing or used twice/);
  assert.match(out, /rule "a": it needs a caller/);
  assert.match(out, /rule "a": a rule camera is not a camera/);
  console.log("PASS: validation reports rule problems; a lost button does not block editing the rest");
}

// 9. the preview request carries the caller; the stated resolution order is in the hint
{
  const s = kstate();
  s.sel = "ep:kitchen";
  assert.deepStrictEqual(L.previewRequest(s, DOOR_ID), { type: "portico/content/preview", document: s.draft, endpoint: "kitchen", caller: DOOR_ID });
  assert.ok(!("caller" in L.previewRequest(s, "*")), "an unknown caller asks for everything: the reply always carries \"*\"");
  assert.ok(!("caller" in L.previewRequest(s)));
  const hint = L.RULE_ORDER_HINT;
  const pos = ["own rule for the calling door", "own rule for that kind", "household rule for the calling door", "household rule for that kind", "first rule in the list wins"].map((t) => hint.indexOf(t));
  assert.ok(pos.every((p) => p >= 0) && pos.slice(0, 4).every((p, i, a) => !i || p > a[i - 1]), "the four steps are stated in the server's order");
  console.log("PASS: the preview names the caller; the resolution order is stated");
}

// ---- jsdom: the two tabs ----------------------------------------------------------------------------------

async function mount(fake, { previewMs = 20 } = {}) {
  const panel = doc.createElement("portico-panel");
  panel._previewMs = previewMs;
  panel.hass = fake.hass;
  doc.body.appendChild(panel);
  await sleep(40);
  return panel;
}
const q = (p, sel) => p.shadowRoot.querySelector(sel);
const qa = (p, sel) => Array.from(p.shadowRoot.querySelectorAll(sel));
const act = (p, name) => q(p, `[data-act="${name}"]`);
const click = (node) => { assert.ok(node, "clicked something that is not there"); node.dispatchEvent(new win.Event("click", { bubbles: true })); };
function setValue(node, value, type = "input") {
  assert.ok(node, "no such field");
  node.value = value;
  node.dispatchEvent(new win.Event(type, { bubbles: true }));
}
const text = (p) => p.shadowRoot.textContent;
const fakeK3 = (extra = {}, opts = {}) => editorHass(extra, { ...opts, loaded: opts.loaded || k3Loaded() });

async function main() {
  // 10. the Screens tab: the list, the picker, the kiosk URL; add a screen and Save
  {
    const f = fakeK3();
    const panel = await mount(f);
    assert.deepStrictEqual(qa(panel, "header .tab").map((b) => b.textContent), ["Elements", "Pages", "Devices", "Ring & call", "Settings", "Status"]);
    assert.ok(act(panel, "tab-devices"), "UI split P2: the Phones tab");
    const previewsBefore = f.ofType("portico/content/preview").length;      // a preview the Pages tab asked for before the click (the extra tab modules load while the panel mounts)
    click(act(panel, "tab-devices"));
    assert.deepStrictEqual(qa(panel, "[data-screen]").map((n) => n.dataset.screen), ["kitchen"]);
    assert.strictEqual(q(panel, '[data-field="screen-name"]').value, "Kitchen tablet");
    assert.strictEqual(q(panel, '[data-field="screen-room"]').value, "Kitchen");
    assert.strictEqual(q(panel, '[data-field="screen-user"]').value, "u2");
    assert.strictEqual(q(panel, '[data-field="kiosk-url"]').value, "http://localhost/portico/kiosk/kitchen");
    assert.strictEqual(q(panel, '[data-field="kiosk-url"]').readOnly, true);
    assert.deepStrictEqual(qa(panel, '[data-field="screen-user"] option').map((o) => o.textContent),
      ["Alice phone user", "Hall tablet", "Tablet (open now)"], "no administrator in the picker; the open-now marker");
    assert.deepStrictEqual(qa(panel, "[data-camera]").map((n) => n.dataset.camera), ["camera.gate"]);
    assert.strictEqual(f.ofType("portico/content/preview").length, previewsBefore, "the Screens tab asks for no preview");
    // Devices tab: the Screens section has its heading, the explanatory sentence, and a 3-dots menu (Edit, Delete) on the row
    assert.ok(q(panel, '[data-section="screens"] h2'), "a Screens section heading");
    assert.match(q(panel, '[data-section="screens"]').textContent, /A screen is a Home Assistant panel you can put in other UI \(for example a dashboard tab\)\. It wakes itself when a call dials a Talk element it contains\./);
    const sm = q(panel, '[data-screen="kitchen"] [data-act="screen-menu"]');
    assert.ok(sm && sm.getAttribute("aria-haspopup") === "menu" && sm.getAttribute("aria-expanded") === "false");
    assert.deepStrictEqual(qa(panel, '[data-screen="kitchen"] [role="menuitem"]').map((b) => b.textContent), ["Edit", "Delete"]);
    click(sm);
    assert.strictEqual(sm.getAttribute("aria-expanded"), "true");
    click(sm);
    assert.strictEqual(sm.getAttribute("aria-expanded"), "false", "the trigger toggles it");
    assert.strictEqual(q(panel, '[data-screen="kitchen"] [data-act="delete-screen"]').textContent, "Delete");
    assert.strictEqual(q(panel, '.screendetail [data-act="delete-screen"]'), null, "delete lives in the row menu only");

    // add one: the address is made from the name, the user is required
    click(act(panel, "add-screen"));
    assert.ok(text(panel).includes("A screen needs a name"), "a name is required");
    setValue(q(panel, '[data-field="new-screen-name"]'), "Hall tablet");
    click(act(panel, "add-screen"));
    assert.ok(text(panel).includes("Pick the Home Assistant user"), "a user is required");
    setValue(q(panel, '[data-field="new-screen-name"]'), "Hall tablet");
    setValue(q(panel, '[data-field="new-screen-user"]'), "u3", "change");
    setValue(q(panel, '[data-field="new-screen-room"]'), "Hall");
    click(act(panel, "add-screen"));
    assert.deepStrictEqual(qa(panel, "[data-screen]").map((n) => n.dataset.screen), ["kitchen", "hall-tablet"]);
    assert.strictEqual(q(panel, '[data-field="kiosk-url"]').value, "http://localhost/portico/kiosk/hall-tablet", "the new screen is selected");
    // a room camera
    setValue(q(panel, '[data-field="screen-camera-add-filter"]'), "garage");
    setValue(q(panel, '[data-field="screen-camera-add"]'), "camera.garage", "change");
    assert.deepStrictEqual(qa(panel, "[data-camera]").map((n) => n.dataset.camera), ["camera.garage"]);
    assert.strictEqual(q(panel, '[data-status]').dataset.status, "dirty");

    click(act(panel, "save"));
    await sleep(40);
    const saves = f.ofType("portico/content/save");
    assert.strictEqual(saves.length, 1);
    assert.strictEqual(saves[0].base_doc_rev, "rev1");
    assert.deepStrictEqual(saves[0].document.dashboards["hall-tablet"], { name: "Hall tablet", ha_user_id: "u3", room: "Hall", cameras: ["camera.garage"] });
    assert.deepStrictEqual(saves[0].document.dashboards.kitchen, k3Loaded().document.dashboards.kitchen);
    assert.ok(text(panel).includes("Saved."));
    console.log("PASS: the Screens tab lists, picks the user, shows the kiosk URL, adds a screen and saves it");
    panel.remove();
  }

  // 11. edit, copy, edit-its-pages, delete
  {
    const f = fakeK3();
    const panel = await mount(f);
    click(act(panel, "tab-devices"));
    setValue(q(panel, '[data-field="screen-name"]'), "Kitchen wall", "change");
    setValue(q(panel, '[data-field="screen-user"]'), "u3", "change");
    setValue(q(panel, '[data-field="screen-room"]'), "", "change");
    assert.ok(text(panel).includes("Screen: Kitchen wall"));
    click(act(panel, "undo"));
    click(act(panel, "undo"));
    click(act(panel, "undo"));
    assert.strictEqual(q(panel, '[data-status]').dataset.status, "saved", "three edits, three undos");

    // copy: the clipboard gets the URL; with no clipboard the address is selected and the user told
    let copied = null;
    Object.defineProperty(win.navigator, "clipboard", { configurable: true, value: { writeText: async (t) => { copied = t; } } });
    click(act(panel, "copy-url"));
    await sleep(10);
    assert.strictEqual(copied, "http://localhost/portico/kiosk/kitchen");
    assert.ok(text(panel).includes("Address copied."));
    Object.defineProperty(win.navigator, "clipboard", { configurable: true, value: { writeText: async () => { throw new Error("denied"); } } });
    click(act(panel, "copy-url"));
    await sleep(10);
    assert.ok(text(panel).includes("Could not copy automatically"));

    // the screen's pages are edited on the Pages tab
    click(act(panel, "edit-screen-pages"));
    assert.ok(text(panel).includes("Pages of: Kitchen tablet"));
    click(act(panel, "tab-devices"));

    // delete
    click(act(panel, "delete-screen"));
    assert.deepStrictEqual(qa(panel, "[data-screen]").map((n) => n.dataset.screen), []);
    assert.ok(text(panel).includes("Add a screen to start."));
    click(act(panel, "save"));
    await sleep(40);
    assert.deepStrictEqual(f.ofType("portico/content/save")[0].document.dashboards, {});
    console.log("PASS: edit with Undo, copy the URL (and the no-clipboard fallback), open its pages, delete");
    panel.remove();
  }

  // 12. a stale user blocks Save with a clear line; a call button blocks a delete
  {
    const loaded = k3Loaded();
    loaded.document.dashboards.kitchen.ha_user_id = "u-deleted";
    const f = fakeK3({}, { loaded });
    const panel = await mount(f);
    click(act(panel, "tab-devices"));
    assert.ok(text(panel).includes("its Home Assistant user no longer exists"));
    assert.ok(text(panel).includes("u-deleted (no such user)"), "the stale choice stays visible in the picker");
    setValue(q(panel, '[data-field="screen-name"]'), "Kitchen b", "change");
    assert.strictEqual(act(panel, "save").disabled, true, "the problem is blocking");
    setValue(q(panel, '[data-field="screen-user"]'), "u2", "change");
    assert.strictEqual(act(panel, "save").disabled, false);
    panel.remove();

    const loaded2 = k3Loaded();
    loaded2.document.template.pages[0].blocks[0].actions.push({ id: "call-k", label: "Call kitchen", kind: "call", target: { type: "dashboard", id: "kitchen" } });
    const g = fakeK3({}, { loaded: loaded2 });
    const panel2 = await mount(g);
    click(act(panel2, "tab-devices"));
    click(act(panel2, "delete-screen"));
    assert.ok(text(panel2).includes("A call button still calls this screen"));
    assert.deepStrictEqual(qa(panel2, "[data-screen]").map((n) => n.dataset.screen), ["kitchen"], "nothing was deleted");
    console.log("PASS: a vanished user blocks Save with a clear line; a called screen cannot be deleted");
    panel2.remove();
  }

  // 13. escaping: names reach the page as text only
  {
    const loaded = k3Loaded();
    const evil = '<img src=x onerror="window.__pwned=1">';
    loaded.ha_users.push({ id: "u9", name: evil, is_admin: false, kiosk_open: false });
    loaded.document.dashboards.kitchen.name = evil;
    loaded.document.dashboards.kitchen.room = evil;
    loaded.document.template.rules.push({ id: "evil", caller: evil.slice(0, 20), cameras: null, ring_buttons: [], call_buttons: [] });
    const f = fakeK3({}, { loaded });
    const panel = await mount(f);
    click(act(panel, "tab-devices"));
    assert.strictEqual(qa(panel, "img").length, 0, "no element was created from a name");
    assert.ok(text(panel).includes(evil));
    assert.strictEqual(q(panel, '[data-field="screen-room"]').value, evil);
    click(act(panel, "tab-calls"));
    assert.strictEqual(qa(panel, "img").length, 0);
    assert.strictEqual(win.__pwned, undefined);
    console.log("PASS: screen names, rooms, users and rule callers are rendered as text");
    panel.remove();
  }

  // 14. an older server part (no ha_users): said clearly, nothing crashes
  {
    const loaded = k3Loaded();
    delete loaded.ha_users;
    const f = fakeK3({}, { loaded });
    const panel = await mount(f);
    click(act(panel, "tab-devices"));
    assert.ok(q(panel, '[data-state="users-missing"]'));
    assert.ok(text(panel).includes("Restart Home Assistant"));
    console.log("PASS: a server part that lists no HA users says to restart Home Assistant");
    panel.remove();
  }

  // 15. Ring & call: the rules of the household, add / edit / delete, the explained order, the ring preview
  {
    const ringReply = (msg) => ({
      endpoint: msg.endpoint, kind: "dashboard", rev: "r", pages: [],
      callers: {
        [DOOR_ID]: {
          name: "Gate", kind: "door",
          cameras: [{ id: "c-1", label: "Gate", name: "Gate camera", available: true }],
          ring_buttons: [{ id: "open-gate", label: "Open the gate", kind: "lock_open", name: "Gate lock", available: true }],
          call_buttons: [],
        },
        "*": { name: null, kind: null, cameras: [], ring_buttons: [], call_buttons: [] },
      },
      roster: [],
    });
    const f = fakeK3({ "portico/content/preview": ringReply });
    const panel = await mount(f);
    click(act(panel, "tab-calls"));
    assert.ok(q(panel, '[data-hint="rule-order"]').textContent.includes("household rule for the calling door"));
    assert.deepStrictEqual(qa(panel, "[data-rule]").map((n) => n.dataset.rule), ["any-door"]);
    assert.deepStrictEqual(qa(panel, '[data-field="new-rule-caller"] option').map((o) => o.value), ["kind:phone", "kind:dashboard", DOOR_ID]);
    assert.deepStrictEqual(qa(panel, '[data-list="ring_buttons"] input').filter((i) => i.checked).map((i) => i.dataset.button), ["open-gate"]);

    // a rule for the gate door, with the Hall door as a ring button and one camera
    setValue(q(panel, '[data-field="new-rule-caller"]'), DOOR_ID, "change");
    click(act(panel, "add-rule"));
    assert.deepStrictEqual(qa(panel, "[data-rule]").map((n) => n.dataset.rule), ["any-door", "gate"]);
    assert.ok(text(panel).includes("Rule for: Door: Gate"));
    setValue(q(panel, '[data-field="rule-cameras-mode"]'), "these", "change");
    setValue(q(panel, '[data-field="rule-camera-add"]'), "camera.gate", "change");
    const ring = qa(panel, '[data-list="ring_buttons"] input[data-button="open-hall"]')[0];
    ring.checked = true;
    ring.dispatchEvent(new win.Event("change", { bubbles: true }));
    const call = qa(panel, '[data-list="call_buttons"] input[data-button="open-gate"]')[0];
    call.checked = true;
    call.dispatchEvent(new win.Event("change", { bubbles: true }));
    await sleep(60);

    // the preview was asked for the draft, for the gate as the caller
    const last = f.ofType("portico/content/preview").at(-1);
    assert.strictEqual(last.caller, DOOR_ID);
    assert.deepStrictEqual(last.document.template.rules[1],
      { id: "gate", caller: DOOR_ID, cameras: ["camera.gate"], ring_buttons: ["open-hall"], call_buttons: ["open-gate"] });
    const ringBox = q(panel, "[data-ring]");
    assert.ok(ringBox.textContent.includes("Gate is calling") && ringBox.textContent.includes("Gate camera") && ringBox.textContent.includes("Open the gate"));

    // "an unknown caller" draws nothing and asks without a caller
    setValue(q(panel, '[data-field="preview-caller"]'), "*", "change");
    await sleep(60);
    assert.ok(!("caller" in f.ofType("portico/content/preview").at(-1)));
    assert.ok(q(panel, "[data-ring]").textContent.includes("shows nothing"));

    // save: rules go in the document the way content/save wants them
    click(act(panel, "save"));
    await sleep(40);
    const save = f.ofType("portico/content/save")[0];
    assert.strictEqual(save.base_doc_rev, "rev1");
    assert.deepStrictEqual(save.document.template.rules.map((r) => r.id), ["any-door", "gate"]);

    // a phone's own rules: the same tab, the other holder
    click(q(panel, '[data-sel="ep:phone1"]'));
    assert.ok(text(panel).includes("This phone's or screen's own rules come before the household's."));
    assert.deepStrictEqual(qa(panel, "[data-rule]").map((n) => n.dataset.rule), []);
    // delete a household rule from the household view
    click(q(panel, '[data-sel="template"]'));
    click(q(panel, '[data-rule="any-door"] [data-act="delete-rule"]'));
    assert.deepStrictEqual(qa(panel, "[data-rule]").map((n) => n.dataset.rule), []);
    console.log("PASS: Ring & call: add a rule, choose cameras and buttons, the preview for a chosen caller, save, delete");
    panel.remove();
  }

  // 16. a rule that names a button the page set lost is reported and blocks Save; the Not attached list has no rules view
  {
    const loaded = k3Loaded();
    loaded.document.template.rules[0].ring_buttons = ["open-gate", "lost-button"];
    const f = fakeK3({}, { loaded });
    const panel = await mount(f);
    click(act(panel, "tab-calls"));
    assert.ok(text(panel).includes('still uses the button "lost-button"'));
    assert.strictEqual(act(panel, "save").disabled, true);
    assert.ok(!qa(panel, "[data-sel^='orphan:']").length, "Not attached has no Ring & call view");
    // the stale one is shown (checked) so it can be unticked
    const stale = qa(panel, '[data-list="ring_buttons"] input[data-button="lost-button"]')[0];
    assert.ok(stale && stale.checked);
    stale.checked = false;
    stale.dispatchEvent(new win.Event("change", { bubbles: true }));
    assert.strictEqual(act(panel, "save").disabled, false, "unticking it clears the problem");
    console.log("PASS: a rule still using a lost button is reported, shown and fixable");
    panel.remove();
  }
}

// 13. the address shown and copied follows the origin the panel was opened at (LAN http, tunnel https), with no 10.6.0.5 anywhere
async function originFlow(pageUrl, want) {
  const w = loadPanel({ url: pageUrl });
  const d = w.dom.window.document;
  const panel = d.createElement("portico-panel");
  panel._previewMs = 20;
  panel.hass = editorHass({}, { loaded: k3Loaded() }).hass;
  d.body.appendChild(panel);
  await sleep(40);
  panel.shadowRoot.querySelector('[data-act="tab-devices"]').dispatchEvent(new w.dom.window.Event("click", { bubbles: true }));
  const field = panel.shadowRoot.querySelector('[data-field="kiosk-url"]');
  assert.strictEqual(field.value, want);
  let copied = null;
  Object.defineProperty(w.dom.window.navigator, "clipboard", { configurable: true, value: { writeText: async (t) => { copied = t; } } });
  panel.shadowRoot.querySelector('[data-act="copy-url"]').dispatchEvent(new w.dom.window.Event("click", { bubbles: true }));
  await sleep(10);
  assert.strictEqual(copied, want, "Copy copies exactly what is shown");
  assert.ok(panel.shadowRoot.textContent.includes("Address copied."));
  assert.ok(!panel.shadowRoot.innerHTML.includes("10.6.0.5"), "no fixed address in the page");
  panel.remove();
}
{
  await originFlow("http://192.168.7.20:8123/portico/screens", "http://192.168.7.20:8123/portico/kiosk/kitchen");
  await originFlow("https://haos.example.net/portico/screens", "https://haos.example.net/portico/kiosk/kitchen");
  // an opaque origin ("null": a sandboxed or data: page) gives no URL and says why
  const w = loadPanel({ url: "about:blank" });
  const d = w.dom.window.document;
  const panel = d.createElement("portico-panel");
  panel._previewMs = 20;
  panel.hass = editorHass({}, { loaded: k3Loaded() }).hass;
  d.body.appendChild(panel);
  await sleep(40);
  panel.shadowRoot.querySelector('[data-act="tab-devices"]').dispatchEvent(new w.dom.window.Event("click", { bubbles: true }));
  assert.ok(!panel.shadowRoot.querySelector('[data-field="kiosk-url"]'), "no address field");
  assert.ok(!panel.shadowRoot.querySelector('[data-act="copy-url"]'), "no Copy button");
  assert.ok(panel.shadowRoot.textContent.includes(L.KIOSK_NO_ORIGIN_TEXT));
  panel.remove();
  console.log("PASS: the kiosk address follows the page origin (http LAN, https), Copy copies what is shown, an opaque origin says so");
}

// 14. grep guard: no fixed kiosk host remains in the served files (the dial placeholder in the phone UI is a different thing)
{
  const fs = await import("fs");
  const www = new URL("../", import.meta.url);
  for (const f of fs.readdirSync(www).filter((n) => n.endsWith(".js"))) {
    const src = fs.readFileSync(new URL(f, www), "utf8");
    assert.ok(!/10\.6\.0\.5:8123/.test(src), `${f}: a hard-coded kiosk origin`);
    assert.ok(!/KIOSK_ORIGIN\s*=/.test(src), `${f}: a fixed KIOSK_ORIGIN`);
    assert.ok(!/clearWebstorage/i.test(src), `${f}: names Fully's clearWebstorage`);
  }
  console.log("PASS: no hard-coded kiosk origin in www/");
}

main().then(() => console.log("ALL_PANEL_SCREENS_TESTS_PASSED")).catch((err) => { console.error(err); process.exit(1); });
