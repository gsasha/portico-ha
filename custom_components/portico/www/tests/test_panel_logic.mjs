// The editor's pure part (portico-panel.js `logic`): state model, edits, validation, orphan moves, request
// builders. No DOM. NOT a visual check.
import assert from "assert";
import fs from "fs";
import { loadPanel, makeLoaded, DOOR_ID, STATES } from "./panel_loader.mjs";

const { Panel } = loadPanel();
const L = Panel.logic;
const fresh = () => L.createState(makeLoaded());
const throwsEdit = (fn, re) => assert.throws(fn, (e) => e instanceof L.EditError && re.test(e.message));

// 1. move and slug
{
  assert.deepStrictEqual(L.move(["a", "b", "c"], 0, 2), ["b", "c", "a"]);
  assert.deepStrictEqual(L.move(["a", "b", "c"], 2, 0), ["c", "a", "b"]);
  assert.deepStrictEqual(L.move(["a", "b"], 1, 9), ["a", "b"], "clamped, same order");
  assert.deepStrictEqual(L.move(["a", "b"], 5, 0), ["a", "b"], "out of range is a no-op");
  assert.strictEqual(L.slug("Open the Gate!", new Set()), "open-the-gate");
  assert.strictEqual(L.slug("Open the Gate!", new Set(["open-the-gate"])), "open-the-gate-2");
  assert.strictEqual(L.slug("???", new Set(), "page"), "page");
  assert.ok(/^[a-z0-9][a-z0-9_-]{0,31}$/.test(L.slug("x".repeat(80), new Set())), "ids fit the server's grammar");
  console.log("PASS: move() and slug() behave");
}

// 2. adding pages, cameras, buttons
{
  const s = fresh();
  const r = L.addPage(s.draft, "template", "  Garden  ");
  assert.strictEqual(r.pageId, "garden");
  assert.strictEqual(L.pagesOf(r.doc, "template").at(-1).label, "Garden", "label trimmed");
  assert.strictEqual(L.pagesOf(s.draft, "template").length, 1, "the input document is not touched");
  // a new page of a phone must not take a household page's id (it would replace it)
  const own = L.addPage(s.draft, "phone1", "Home");
  assert.strictEqual(own.pageId, "home-2");
  throwsEdit(() => L.addPage(s.draft, "template", "   "), /needs a name/);
  throwsEdit(() => L.addPage(s.draft, "template", "x".repeat(41)), /longer than 40/);
  let doc = s.draft;
  for (let i = 0; i < 11; i++) doc = L.addPage(doc, "template", `P${i}`).doc;
  throwsEdit(() => L.addPage(doc, "template", "one too many"), /At most 12 pages/);

  const cam = L.addCamera(s.draft, "template", "home", "camera.garage", "Garage");
  assert.deepStrictEqual(L.pageCameras(L.pagesOf(cam, "template")[0]).at(-1), { id: "garage", label: "Garage", entity_id: "camera.garage" });
  throwsEdit(() => L.addCamera(s.draft, "template", "home", "lock.gate", "x"), /Pick a camera/);
  // a camera's name may be empty (it then draws no name); a page's and a button's may not
  const bare = L.addCamera(s.draft, "template", "home", "camera.garage", "   ");
  assert.deepStrictEqual(L.pageCameras(L.pagesOf(bare, "template")[0]).at(-1), { id: "garage", label: "", entity_id: "camera.garage" });
  assert.deepStrictEqual(L.validate(bare), [], "an empty camera name is a valid document");
  throwsEdit(() => L.addCamera(s.draft, "template", "home", "camera.garage", "x".repeat(41)), /longer than 40/);
  const cleared = L.updateTile(cam, "template", "home", "cameras", "garage", { label: "" });
  assert.strictEqual(L.pageCameras(L.pagesOf(cleared, "template")[0]).at(-1).label, "");
  throwsEdit(() => L.updateTile(cam, "template", "home", "cameras", "garage", { label: "y".repeat(41) }), /longer than 40/);
  throwsEdit(() => L.renamePage(s.draft, "template", "home", ""), /needs a name/);
  throwsEdit(() => L.addAction(s.draft, "template", "home", { kind: "lock_open", target: "lock.front", label: " " }), /needs a name/);
  assert.strictEqual(L.cameraName({ label: "Garage", entity_id: "camera.garage" }), "Garage");
  assert.strictEqual(L.cameraName({ label: "", entity_id: "camera.garage" }), "camera.garage");
  assert.strictEqual(L.cameraName({ label: "", entity_id: "camera.garage" }, { "camera.garage": { attributes: { friendly_name: "Garage cam" } } }), "Garage cam");

  const lock = L.addAction(s.draft, "template", "home", { kind: "lock_open", target: "lock.front", label: "Front door", icon: "door-open", confirm: true });
  assert.deepStrictEqual(L.pageActions(L.pagesOf(lock, "template")[0]).at(-1),
    { id: "front-door", label: "Front door", kind: "lock_open", entity_id: "lock.front", icon: "door-open", confirm: true });
  const noicon = L.addAction(s.draft, "template", "home", { kind: "script_run", target: "script.evening", label: "Evening", icon: "none", confirm: false });
  assert.ok(!("icon" in L.pageActions(L.pagesOf(noicon, "template")[0]).at(-1)), "icon none is not stored");
  const door = L.addAction(s.draft, "template", "home", { kind: "portico_open", target: "http://10.6.0.7:8091", label: "Gate relay", confirm: true });
  assert.strictEqual(L.pageActions(L.pagesOf(door, "template")[0]).at(-1).device_id, "http://10.6.0.7:8091");
  throwsEdit(() => L.addAction(s.draft, "template", "home", { kind: "lock_open", target: "light.kitchen", label: "x" }), /Pick a lock/);
  throwsEdit(() => L.addAction(s.draft, "template", "home", { kind: "switch_toggle", target: "switch.x", label: "x" }), /Pick what the button does/);
  throwsEdit(() => L.addAction(s.draft, "template", "home", { kind: "call", target: "x", label: "x" }), /Pick what the button does/);
  throwsEdit(() => L.addAction(s.draft, "template", "home", { kind: "portico_open", target: "", label: "x" }), /Pick the door/);
  // ids stay unique within the page
  let twice = L.addAction(s.draft, "template", "home", { kind: "lock_open", target: "lock.front", label: "Open the gate", confirm: true });
  assert.deepStrictEqual(L.pageActions(L.pagesOf(twice, "template")[0]).map((a) => a.id), ["open-gate", "open-the-gate"]);
  twice = L.addAction(twice, "template", "home", { kind: "lock_open", target: "lock.front", label: "Open the gate", confirm: true });
  assert.strictEqual(new Set(L.pageActions(L.pagesOf(twice, "template")[0]).map((a) => a.id)).size, 3);
  console.log("PASS: pages, cameras and buttons are added with unique valid ids; bad input is refused with a reason");
}

// 3. editing a tile, reorder, remove
{
  const s = fresh();
  const doc = L.addAction(s.draft, "template", "home", { kind: "scene_activate", target: "scene.movie", label: "Movie", confirm: false });
  const ids = (d) => L.pageActions(L.pagesOf(d, "template")[0]).map((a) => a.id);
  assert.deepStrictEqual(ids(L.moveTile(doc, "template", "home", "actions", "movie", -1)), ["movie", "open-gate"]);
  assert.deepStrictEqual(ids(L.moveTile(doc, "template", "home", "actions", "movie", 1)), ["open-gate", "movie"], "last stays last");
  const edited = L.updateTile(doc, "template", "home", "actions", "open-gate", { label: "Gate", icon: "none", confirm: false });
  const a = L.pageActions(L.pagesOf(edited, "template")[0])[0];
  assert.deepStrictEqual([a.label, "icon" in a, a.confirm], ["Gate", false, false]);
  throwsEdit(() => L.updateTile(doc, "template", "home", "actions", "open-gate", { label: "" }), /needs a name/);
  assert.deepStrictEqual(ids(L.removeTile(doc, "template", "home", "actions", "open-gate")), ["movie"]);
  // a stored call button keeps confirm false whatever is sent
  const withCall = L.clone(doc);
  L.pagesOf(withCall, "template")[0].blocks[0].actions.push({ id: "ring", label: "Ring", kind: "call", target: { type: "phone", id: "p" }, confirm: false });
  const kept = L.updateTile(withCall, "template", "home", "actions", "ring", { confirm: true, label: "Ring Dina" });
  assert.strictEqual(L.pageActions(L.pagesOf(kept, "template")[0]).find((x) => x.id === "ring").confirm, false);
  console.log("PASS: tiles can be edited, reordered and removed; a call button never gains confirm");
}

// 4. the household / override view
{
  const s = fresh();
  let doc = L.addPage(s.draft, "template", "Garden").doc;                // template: home, garden
  let eff = L.effectivePages(doc, "phone1");
  assert.deepStrictEqual(eff.map((e) => [e.page.id, e.src]), [["home", "household"], ["garden", "household"]]);
  doc = L.overridePage(doc, "phone1", "garden");
  assert.deepStrictEqual(L.effectivePages(doc, "phone1").map((e) => [e.page.id, e.src]), [["home", "household"], ["garden", "override"]]);
  doc = L.addPage(doc, "phone1", "Pool").doc;
  doc = L.addPage(doc, "phone1", "Deck").doc;
  assert.deepStrictEqual(L.effectivePages(doc, "phone1").map((e) => e.page.id), ["home", "garden", "pool", "deck"]);
  // only the phone's own pages move; the override of a household page keeps the household's place
  doc = L.movePage(doc, "phone1", "deck", -1);
  assert.deepStrictEqual(L.effectivePages(doc, "phone1").map((e) => e.page.id), ["home", "garden", "deck", "pool"]);
  doc = L.movePage(doc, "phone1", "garden", 1);
  assert.deepStrictEqual(L.effectivePages(doc, "phone1").map((e) => e.page.id), ["home", "garden", "deck", "pool"], "an override does not move");
  // removing the override gives the household page back
  doc = L.deletePage(doc, "phone1", "garden");
  assert.strictEqual(L.effectivePages(doc, "phone1").find((e) => e.page.id === "garden").src, "household");
  throwsEdit(() => L.renamePage(doc, "phone1", "home", "X"), /not editable here/);
  throwsEdit(() => L.overridePage(doc, "template", "home"), /Only a household page/);
  // template changes: the phone's edits never touch the template list
  assert.deepStrictEqual(L.pagesOf(doc, "template").map((p) => p.id), ["home", "garden"]);
  const t = L.movePage(doc, "template", "garden", -1);
  assert.deepStrictEqual(L.pagesOf(t, "template").map((p) => p.id), ["garden", "home"]);
  console.log("PASS: a phone's view merges household and own pages in the server's order");
}

// 5. dirty, undo, discard
{
  const s = fresh();
  assert.strictEqual(L.isDirty(s), false);
  // key order and cloning never make a document dirty
  s.draft = L.clone(s.loaded.document);
  s.draft.template = { roster: s.draft.template.roster, rules: s.draft.template.rules, pages: s.draft.template.pages };
  assert.strictEqual(L.isDirty(s), false, "same content in another key order is not a change");
  L.commit(s, L.addPage(s.draft, "template", "Garden").doc);
  assert.strictEqual(L.isDirty(s), true);
  L.commit(s, L.addPage(s.draft, "template", "Pool").doc);
  assert.strictEqual(L.pagesOf(s.draft, "template").length, 3);
  assert.ok(L.undo(s));
  assert.strictEqual(L.pagesOf(s.draft, "template").length, 2);
  assert.ok(L.undo(s));
  assert.strictEqual(L.isDirty(s), false, "undone back to the loaded document");
  assert.strictEqual(L.undo(s), false, "nothing left to undo");
  L.commit(s, L.addPage(s.draft, "template", "Garden").doc);
  L.discard(s);
  assert.strictEqual(L.isDirty(s), false);
  assert.strictEqual(s.undo.length, 0);
  for (let i = 0; i < 60; i++) L.commit(s, L.clone(s.draft));
  assert.strictEqual(s.undo.length, 50, "the undo stack is 50 deep");
  console.log("PASS: dirty tracking, undo (50 deep) and discard");
}

// 6. validation
{
  const s = fresh();
  assert.deepStrictEqual(L.validate(s.draft), []);
  const bad = L.clone(s.draft);
  bad.template.pages[0].label = "x".repeat(41);
  bad.template.pages[0].blocks[0].cameras.push({ id: "c2", label: "x".repeat(41), entity_id: "light.kitchen" });
  bad.template.pages[0].blocks[0].actions.push({ id: "x", label: "X", kind: "lock_open", entity_id: "switch.x" });
  const problems = L.validate(bad);
  assert.ok(problems.some((p) => /name must be 1-40/.test(p)), problems.join("|"));
  assert.ok(problems.some((p) => /camera's name is at most 40/.test(p)));
  assert.ok(problems.some((p) => /is not a camera/.test(p)));
  assert.ok(problems.some((p) => /needs its lock/.test(p)));
  // a rule that still uses a deleted button blocks the save and names the rule and the button
  const ruled = L.clone(s.draft);
  ruled.template.rules = [{ id: "doors", caller: "kind:door", cameras: null, ring_buttons: ["open-gate"], call_buttons: [] }];
  assert.deepStrictEqual(L.validate(ruled), []);
  const gone = L.removeTile(ruled, "template", "home", "actions", "open-gate");
  const p2 = L.validate(gone);
  assert.strictEqual(p2.length, 1);
  assert.ok(/rule "doors" still uses the button "open-gate"/.test(p2[0]), p2[0]);
  // an override rule may use a household button
  const ov = L.clone(s.draft);
  ov.overrides.phone1.rules = [{ id: "r", caller: "kind:door", cameras: null, ring_buttons: ["open-gate"], call_buttons: [] }];
  assert.deepStrictEqual(L.validate(ov), []);
  console.log("PASS: validation catches bad labels, entities and a rule left without its button");
}

// 7. "Not attached"
{
  const s = fresh();
  const key = "old-phone-user-id-123";
  assert.deepStrictEqual(L.orphanKeys(s), [key]);
  // move into a phone that already has a "home" page: arrives as home-2, marked, nothing replaced
  const withHome = L.clone(s.draft);
  withHome.overrides.phone1.pages = [{ id: "home", label: "Home", blocks: [{ id: "home", title: "Home", cameras: [], actions: [] }] }];
  s.draft = withHome;
  L.moveOrphan(s, key, "phone1");
  assert.deepStrictEqual(L.pagesOf(s.draft, "phone1").map((p) => [p.id, p.label]), [["home", "Home"], ["home-2", "Home (moved)"]]);
  assert.ok(!(key in s.draft.overrides), "the old key leaves the document");
  assert.deepStrictEqual(s.drop, [key], "and is named to be dropped");
  assert.deepStrictEqual(L.orphanKeys(s), []);
  assert.strictEqual(L.pageActions(L.pagesOf(s.draft, "phone1")[1])[0].id, "garage", "its buttons come along");
  // move into the template
  const t = fresh();
  L.moveOrphan(t, key, "template");
  assert.deepStrictEqual(L.pagesOf(t.draft, "template").map((p) => p.id), ["home", "home-2"]);
  assert.deepStrictEqual(t.drop, [key]);
  // undo brings the orphan back and forgets the drop
  L.undo(t);
  assert.deepStrictEqual(t.drop, []);
  assert.deepStrictEqual(L.orphanKeys(t), [key]);
  // delete
  const d = fresh();
  L.deleteOrphan(d, key);
  assert.ok(!(key in d.draft.overrides));
  assert.deepStrictEqual(d.drop, [key]);
  throwsEdit(() => L.deleteOrphan(d, key), /not in the document/);
  // discard forgets the drop list
  L.discard(d);
  assert.deepStrictEqual(d.drop, []);
  assert.ok(key in d.draft.overrides);
  console.log("PASS: a Not attached override moves (fresh ids on a clash) or is deleted, and is named in the drop list");
}

// 8. requests and errors
{
  const s = fresh();
  assert.deepStrictEqual(L.saveRequest(s), { type: "portico/content/save", document: s.draft, base_doc_rev: "rev1" });
  L.deleteOrphan(s, "old-phone-user-id-123");
  const req = L.saveRequest(s);
  assert.deepStrictEqual(req.drop_orphans, ["old-phone-user-id-123"]);
  assert.ok(!("old-phone-user-id-123" in req.document.overrides));
  // preview endpoint: the selected phone; for the template the chosen / first phone
  assert.strictEqual(L.previewEndpoint(s), "phone1");
  s.sel = "ep:kitchen";
  assert.strictEqual(L.previewRequest(s).endpoint, "kitchen");
  s.sel = "template"; s.previewAs = "kitchen";
  assert.strictEqual(L.previewRequest(s).endpoint, "kitchen");
  s.previewAs = "gone";
  assert.strictEqual(L.previewEndpoint(s), "phone1", "a stale choice falls back to the first phone");
  const empty = L.createState({ ...makeLoaded(), endpoints: [] });
  assert.strictEqual(L.previewRequest(empty), null);
  assert.strictEqual(L.classifyError({ code: "unknown_command", message: "Unknown command." }).kind, "needs_restart");
  assert.strictEqual(L.classifyError({ code: "conflict", message: "x" }).kind, "conflict");
  assert.deepStrictEqual(L.classifyError({ code: "invalid", message: "page home: a label is required" }),
    { kind: "invalid", message: "page home: a label is required" });
  assert.strictEqual(L.classifyError({ code: "unauthorized", message: "x" }).kind, "unauthorized");
  assert.strictEqual(L.classifyError(new Error("boom")).message, "boom");
  console.log("PASS: save / preview requests and the error classes");
}

// 9. pickers
{
  const states = { ...STATES };
  const cams = L.entityOptions(states, "camera", "");
  assert.deepStrictEqual(cams.map((c) => c.value), ["camera.gate", "camera.garage", "camera.zebra"].sort((a, b) =>
    (states[a].attributes.friendly_name || a).localeCompare(states[b].attributes.friendly_name || b)), "only cameras, by friendly name");
  assert.ok(cams.every((c) => c.value.startsWith("camera.")), "no light, lock or script in a camera list");
  assert.strictEqual(cams.find((c) => c.value === "camera.gate").label, "Gate camera (camera.gate)");
  assert.strictEqual(cams.find((c) => c.value === "camera.zebra").label, "camera.zebra", "no friendly name: the id alone");
  assert.deepStrictEqual(L.entityOptions(states, "camera", "GARAGE").map((c) => c.value), ["camera.garage"], "filter by name, any case");
  assert.deepStrictEqual(L.entityOptions(states, "camera", "zeb").map((c) => c.value), ["camera.zebra"], "filter by id");
  assert.deepStrictEqual(L.entityOptions(states, "lock", "").map((c) => c.value), ["lock.front", "lock.gate"]);
  assert.deepStrictEqual(L.entityOptions(states, "switch", ""), []);
  // doors: the bare device id after the FIRST colon (a URL keeps its own)
  const st = fresh();
  assert.deepStrictEqual(L.doorOptions(st), [{ value: "http://10.6.0.7:8091", label: "Gate", name: "Gate" }]);
  console.log("PASS: pickers are filtered by domain and text, with friendly names; doors give the bare device id");
}

// 10. the icon list is the app's list
{
  assert.strictEqual(new Set(L.ICON_NAMES).size, L.ICON_NAMES.length, "no duplicates");
  const candidates = ["/src/contract/icons_v1.json", new URL("../../../contract/icons_v1.json", import.meta.url).pathname];
  const file = candidates.find((p) => fs.existsSync(p));
  if (file) {
    assert.deepStrictEqual(L.ICON_NAMES, JSON.parse(fs.readFileSync(file, "utf8")).icons, "ICON_NAMES equals contract/icons_v1.json");
    console.log("PASS: the icon picker's list equals contract/icons_v1.json");
  } else {
    console.log("SKIP: contract/icons_v1.json is not reachable from here");
  }
}
// what the app and the card show of the pages (the hint), and the default name of the first page
{
  const pg = (id) => ({ id, label: id, blocks: [{ id, title: id, cameras: [], actions: [] }] });
  assert.ok(L.PAGES_HINT.includes("every page as a tab") && L.PAGES_HINT.includes("card"), L.PAGES_HINT);
  assert.ok(!/0\.4\.3|id home|"Home"/.test(L.PAGES_HINT), "the hint no longer names a special page or an old app");
  assert.strictEqual(L.oldClientPageId, undefined, "no page is special any more");
  const empty = L.createState({ ...makeLoaded(), document: { schema_version: 3, template: { pages: [], rules: [], roster: ["*"] }, overrides: {}, dashboards: {}, endpoints: {} } });
  assert.strictEqual(L.defaultNewPageLabel(empty.draft, "template"), "Home", "no home yet: the first page is named Home");
  const first = L.addPage(empty.draft, "template", L.defaultNewPageLabel(empty.draft, "template"));
  assert.strictEqual(first.pageId, "home");
  assert.strictEqual(L.defaultNewPageLabel(first.doc, "template"), "", "once there is a home, no default");
  assert.strictEqual(L.defaultNewPageLabel(fresh().draft, "template"), "", "the stock template already has home");
  const garden = L.addPage(empty.draft, "template", "Garden").doc;
  assert.strictEqual(L.defaultNewPageLabel(garden, "template"), "Home", "no home page among others: still Home");
  console.log("PASS: the old-client page, the hint text and the default first-page name");
}
console.log("ALL_PANEL_LOGIC_TESTS_PASSED");
