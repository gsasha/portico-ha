// Pages unified, P-B: the panel's Pages tab for a schema 6 document (a library of Content and Ring pages, no sidebar, no "used by"
// chooser), the tab bar [Elements, Pages, Devices, Settings], the per-kind preview, the delete guard, and, for a schema 4/5
// document, the old tabs untouched plus the admin-only "Check migration" dry run and its confirm page. The pure edits (`logic`) and
// the panel in jsdom with a fake `hass`. Protocol-level; NOT a visual check.
import assert from "assert";
import fs from "fs";
import { loadPanel, editorHass, makeLoaded, sleep, DOOR_ID } from "./panel_loader.mjs";

const { dom, Panel, doc } = loadPanel();
const L = Panel.logic;
const win = dom.window;
const throwsEdit = (fn, re) => assert.throws(fn, (e) => e instanceof L.EditError && re.test(e.message), String(re));

const TALK_DOOR = { id: "el-20000001", kind: "talk", name: "Front door", options: { door: "http://10.6.0.7:8091" } };
const TALK_PHONE = { id: "talk-phone-phone1", kind: "talk", name: "Alice's phone", managed: true, options: { phone: "phone1" } };
const TALK_GONE = { id: "el-20000009", kind: "talk", name: "Old door", options: { door: "gone" }, target_removed: true };
const GATE = { id: "el-10000001", kind: "button", name: "Gate lock", entity_id: "lock.gate", icon: "lock-open", options: { action: "lock_open" } };
const LAMP = { id: "el-10000003", kind: "button", name: "Lamp", entity_id: "script.lamp", icon: "lightbulb", options: { action: "script_run" } };
const CAM = { id: "el-10000008", kind: "camera", name: "Gate cam", entity_id: "camera.gate", options: {} };
const ELS = [TALK_DOOR, TALK_PHONE, GATE, LAMP, CAM];
const listReply = (elements) => ({ schema_version: 1, rev: "r1", elements, used_by: {}, kinds: {}, icons: [], palette: [], limits: {} });

const q = (p, sel) => p.shadowRoot.querySelector(sel);
const qa = (p, sel) => Array.from(p.shadowRoot.querySelectorAll(sel));
const act = (p, name) => q(p, `[data-act="${name}"]`);
const click = (node) => { assert.ok(node, "clicked something that is not there"); node.dispatchEvent(new win.Event("click", { bubbles: true })); };
function setValue(node, value, type = "input") { assert.ok(node); node.value = value; node.dispatchEvent(new win.Event(type, { bubbles: true })); }
const sec = (p, id) => q(p, `.pagesection[data-page="${id}"]`);
const secq = (p, id, sel) => sec(p, id).querySelector(sel);
const tabs = (p) => qa(p, "nav.tabs button").map((b) => b.textContent);

/* A schema 6 document: a content page, a Ring page of the front door's Talk element and one of Alice's phone's, phone1 shows the content
 * page, the kitchen screen has nothing. */
function v6Loaded() {
  const loaded = makeLoaded();
  loaded.document = {
    schema_version: 6, home_name: "My Home",
    pages: [
      { id: "home", label: "Home", kind: "content", blocks: [{ id: "home", title: "Home", cameras: [{ id: "gate", label: "Gate", entity_id: "camera.gate" }],
        actions: [{ id: "open-gate", label: "Open the gate", kind: "lock_open", entity_id: "lock.gate", confirm: true }] }] },
      { id: "front-ring", label: "Front ring", kind: "ring", talk: TALK_DOOR.id, cameras: null,
        blocks: [{ id: "ring-btns", title: "Doors", cameras: [], actions: [{ id: "ring-gate", element: GATE.id }] }] },
      { id: "night-ring", label: "Night ring", kind: "ring", talk: TALK_PHONE.id, cameras: ["camera.gate"], blocks: [] },
    ],
    devices: { phone1: { pages: [{ page: "home" }], roster_hide: [] }, kitchen: { pages: [], roster_hide: [] } },
    dashboards: {}, endpoints: {},
  };
  loaded.orphans = [];
  loaded.resolved = {};
  return loaded;
}

const REPORT = {
  ok: true, can_apply: true, doc_rev: "rev1", schema: { from: 4, to: 6 }, safety_copy: ".storage/portico.content.v4",
  counts: { endpoints: 2, library_pages: 3, content_pages: 2, ring_pages_new: 1, ring_assignments: 2, rules_converted_by_caller_kind: { door: 1 }, rules_unused: 0,
            talk_elements_to_create: 0, dropped_dangling_buttons: 0, endpoints_equal: 2, endpoints_unequal: 0 },
  ring_pages: [{ id: "ring-1", label: "Gate ring", talk: "el-20000001", cameras: null, ring_buttons: ["open-gate"], call_buttons: [], from_rules: ["template:r1"], assigned_to: ["phone1"] }],
  talk_elements_to_create: [], blocked_by: [], dropped: [], unused_rules: [],
  per_endpoint: [{ endpoint: "phone1", kind: "phone", equal: true, rev_before: "a", rev_after: "a", callers: 1, pages: ["home"], differences: [] },
                 { endpoint: "kitchen", kind: "dashboard", equal: true, rev_before: "b", rev_after: "b", callers: 1, pages: ["home"], differences: [] }],
  problems: [],
};

async function mount(opts = {}) {
  const loaded = opts.loaded || v6Loaded();
  const ring = (msg) => {
    const caller = msg.caller;
    return { endpoint: msg.endpoint, kind: "phone", home_name: "x", rev: "r", pages: [], roster: [],
             callers: caller ? { [caller]: { name: "Gate", kind: "door", cameras: [{ id: "c-1", label: "Gate", name: "Gate cam", available: true }],
               ring_buttons: [{ id: "ring-gate", label: "Gate lock", name: "Gate lock", kind: "lock_open", available: true }], call_buttons: [] } } : {} };
  };
  const f = editorHass({
    "portico/elements/list": () => listReply(opts.elements || ELS),
    "portico/content/preview": (msg) => (opts.preview ? opts.preview(msg) : ring(msg)),
    ...(opts.handlers || {}),
  }, { loaded, user: opts.user });
  const panel = doc.createElement("portico-panel");
  panel._previewMs = 10;
  panel.hass = f.hass;
  doc.body.appendChild(panel);
  await sleep(80);
  return { f, panel };
}

// ---- 1. pure: the model switch, editorDocument and the save keep schema 6 ---------------------------------------------------------
{
  const d = v6Loaded().document;
  assert.ok(L.isPagesModel(d) && !L.isPagesModel(makeLoaded().document));
  const e = L.editorDocument(d);
  assert.strictEqual(e.schema_version, 6, "a schema 6 document stays 6");
  assert.ok(!("template" in e) && !("overrides" in e), "no template or overrides are invented");
  assert.strictEqual(e.pages[1].cameras, null, "a Ring page keeps its own cameras (null = the caller's)");
  assert.deepStrictEqual(e.pages[2].cameras, ["camera.gate"]);
  assert.strictEqual(e.pages[1].talk, TALK_DOOR.id);
  const flat = L.clone(d);
  flat.pages[0].cameras = [{ id: "x" }]; flat.pages[0].actions = [{ id: "y" }];
  assert.ok(!("cameras" in L.editorDocument(flat).pages[0]) && !("actions" in L.editorDocument(flat).pages[0]), "a content page's flat lists are dropped");
  const state = L.createState({ ...v6Loaded(), document: d, doc_rev: "r" });
  assert.strictEqual(L.saveRequest(state).document.schema_version, 6);
  assert.ok(L.isDirty(state) === false, "a loaded schema 6 document is not dirty");
  assert.ok(L.isElementId("talk-phone-phone1") && L.isElementId("talk-screen-kitchen") && !L.isElementId("talk-tablet-x"), "managed Talk ids are element ids");
  console.log("PASS: a schema 6 document stays 6 through the editor and the save, Ring pages keep their camera");
}

// ---- 2. pure: add / delete / ring settings / blocks -------------------------------------------------------------------------------
{
  const d = v6Loaded().document;
  const nameOf = (id) => ({ phone1: "Alice's phone", kitchen: "Kitchen tablet" }[id] || id);
  throwsEdit(() => L.addLibraryPage(d, "", "X"), /Choose the kind/);
  throwsEdit(() => L.addLibraryPage(d, "ring", "Back ring", TALK_PHONE.id), /appears by itself/);
  const c = L.addLibraryPage(d, "content", "Garden");
  assert.deepStrictEqual(c.doc.pages.at(-1), { id: "garden", label: "Garden", kind: "content", blocks: [{ id: "garden", title: "Garden", cameras: [], actions: [] }] });
  assert.strictEqual(d.pages.length, 3, "the old document is untouched");

  // delete: refused with the devices named while one shows the page or answers with it
  throwsEdit(() => L.deleteLibraryPage(d, "home", nameOf), /still used by Alice's phone/);
  // a Ring page has no device user (R-D): the stale per-device map of a restored document does not hold it
  assert.deepStrictEqual(L.deleteLibraryPage(d, "front-ring", nameOf).pages.map((p) => p.id), ["home", "night-ring"]);
  assert.deepStrictEqual(L.deleteLibraryPage(d, "night-ring", nameOf).pages.map((p) => p.id), ["home", "front-ring"]);
  // a leftover ("Not attached") entry is no user of a page and loses only the reference when the page goes
  L.setLeftoverKeys(["old-key"]);
  const withLeft = { ...d, devices: { ...d.devices, "old-key": { pages: [{ page: "night-ring" }, { page: "home" }], ring: {}, roster_hide: ["z"] } } };
  assert.strictEqual(L.devicesUsingPage(withLeft, "night-ring").length, 0);
  const pruned = L.deleteLibraryPage(withLeft, "night-ring", nameOf);
  assert.deepStrictEqual(pruned.devices["old-key"], { pages: [{ page: "home" }], ring: {}, roster_hide: ["z"] });
  throwsEdit(() => L.deleteLibraryPage(withLeft, "home", nameOf), /still used by Alice's phone/);
  L.setLeftoverKeys([]);
  assert.strictEqual(L.shownOnText(d, "home", nameOf), "Shown on: Alice's phone");
  assert.strictEqual(L.shownOnText(d, "night-ring", nameOf), "Not shown on any device yet");

  // Ring page: Talk and camera
  assert.strictEqual(L.setRingTalk, undefined, "a Ring page's Talk element is fixed by its entry");
  assert.deepStrictEqual(L.setRingCamera(d, "front-ring", "camera.garage").pages[1].cameras, ["camera.garage"]);
  assert.deepStrictEqual(L.setRingCamera(d, "front-ring", CAM.id).pages[1].cameras, [CAM.id]);
  assert.deepStrictEqual(L.setRingCamera(d, "front-ring", "").pages[1].cameras, []);
  assert.strictEqual(L.setRingCamera(d, "night-ring", null).pages[2].cameras, null);
  throwsEdit(() => L.setRingCamera(d, "front-ring", "light.kitchen"), /Pick a camera/);

  // blocks: when, and what a Ring page refuses
  assert.strictEqual(L.setBlockWhen(d, "front-ring", "ring-btns", "ring").pages[1].blocks[0].when, "ring");
  assert.ok(!("when" in L.setBlockWhen(L.setBlockWhen(d, "front-ring", "ring-btns", "call"), "front-ring", "ring-btns", "").pages[1].blocks[0]), "both = no key");
  throwsEdit(() => L.setBlockWhen(d, "front-ring", "ring-btns", "later"), /Shown must be/);
  throwsEdit(() => L.setBlockWhen(d, "home", "home", "ring"), /not a Ring page/);
  throwsEdit(() => L.addButtonRef(d, L.LIBRARY, "front-ring", TALK_DOOR, { label: "Talk" }, "ring-btns"), /cannot hold a Talk button/);
  throwsEdit(() => L.addAction(d, L.LIBRARY, "front-ring", { kind: "talk", label: "Talk", blockId: "ring-btns" }), /cannot hold a Talk button/);
  throwsEdit(() => L.addCamera(d, L.LIBRARY, "front-ring", "camera.gate", "Gate", "ring-btns"), /camera is chosen on the page/);
  throwsEdit(() => L.addCameraRef(d, L.LIBRARY, "front-ring", CAM, { label: "x" }, "ring-btns"), /camera is chosen on the page/);
  const placed = L.addButtonRef(d, L.LIBRARY, "front-ring", LAMP, { label: "Lamp" }, "ring-btns");
  assert.deepStrictEqual(placed.pages[1].blocks[0].actions.at(-1), { id: "lamp", element: LAMP.id }, "a button element is placed on a Ring page");
  // ids stay unique across the whole library
  assert.ok(L.allActionIds(d).has("open-gate") && L.allActionIds(d).has("ring-gate"));
  const second = L.addAction(d, L.LIBRARY, "home", { kind: "button_press", target: "button.garage", label: "Open gate", blockId: "home" });
  assert.notStrictEqual(L.pageActions(second.pages[0]).at(-1).id, "open-gate", "a new button's id is unique in the library");
  console.log("PASS: adding, deleting (guarded), the Ring page's Talk, camera and when, and what a Ring page refuses");
}

// ---- 3. pure: validation, Talk required, no Talk button, buttons only -----------------------------------------------------------
{
  const d = v6Loaded().document;
  assert.deepStrictEqual(L.validate(d, null, { elements: new Map(ELS.map((e) => [e.id, e])) }), []);
  const bad = L.clone(d);
  bad.pages[1].talk = "";
  bad.pages[1].blocks[0].actions.push({ id: "talk-in-ring", element: TALK_DOOR.id });
  bad.pages[1].blocks.push({ id: "cams", title: "Cams", cameras: [{ id: "k", label: "K", entity_id: "camera.gate" }], actions: [] });
  bad.pages[1].blocks[0].when = "later";
  bad.pages[2].cameras = ["light.x"];
  bad.pages[0].blocks[0].when = "ring";
  const problems = L.validate(bad, null, { elements: new Map(ELS.map((e) => [e.id, e])) }).join("\n");
  assert.match(problems, /Front ring": a Ring page needs its Talk element/);
  assert.match(problems, /cannot hold a Talk button/);
  assert.match(problems, /hold buttons only/);
  assert.match(problems, /Shown must be/);
  assert.match(problems, /Night ring": the camera is not a camera/);
  assert.match(problems, /only a Ring page's blocks say when/);
  // a managed Talk element is a Talk element even before the element list is known
  const managed = L.clone(d);
  managed.pages[1].blocks[0].actions.push({ id: "m", element: "talk-phone-phone1" });
  assert.match(L.validate(managed, null, { elements: null }).join("\n"), /cannot hold a Talk button/);
  console.log("PASS: validation: a Ring page needs a Talk element, no Talk button, buttons only, when is ring/call");
}

// ---- 4. pure: assumePage and the preview request --------------------------------------------------------------------------------
{
  const d = v6Loaded().document;
  const before = JSON.stringify(d);
  const a = L.assumePage(d, "kitchen", "home");
  assert.deepStrictEqual(a.devices.kitchen.pages, [{ page: "home" }]);
  assert.strictEqual(JSON.stringify(d), before, "the draft is not changed");
  assert.deepStrictEqual(L.assumePage(d, "phone1", "home").devices.phone1.pages, [{ page: "home" }], "already shown: unchanged");
  assert.deepStrictEqual(L.assumePage(d, "kitchen", "night-ring"), d, "a Ring page is the Talk element's: nothing is assumed on a device");
  assert.strictEqual(L.assumePage(d, "phone1", "missing"), d);
  const s = L.createState({ ...v6Loaded(), doc_rev: "r" });
  s.pageId = "night-ring";
  s.draft = L.editorDocument(s.draft);
  assert.strictEqual(L.previewEndpoint(s), "phone1", "no device shows it: the first phone");
  s.pageId = "home";
  assert.strictEqual(L.previewEndpoint(s), "phone1", "the device that shows the page");
  s.previewAs = "kitchen";
  assert.strictEqual(L.previewEndpoint(s), "kitchen", "the choice wins");
  const req = L.previewRequest(s, DOOR_ID);
  assert.strictEqual(req.endpoint, "kitchen");
  assert.strictEqual(req.caller, DOOR_ID);
  assert.deepStrictEqual(req.document.devices.kitchen.pages, [{ page: "home" }], "the request carries the assumption");
  assert.strictEqual(L.isDirty(s), false, "nothing was changed in the draft");
  assert.deepStrictEqual(L.talkCaller([{ id: DOOR_ID, kind: "door" }], TALK_DOOR), DOOR_ID);
  assert.strictEqual(L.talkCaller([], TALK_DOOR), null);
  assert.strictEqual(L.talkCaller([], TALK_PHONE), "phone1");
  console.log("PASS: assumePage and the preview request: the page is taken as assigned, the draft is untouched");
}

// ---- 5. schema 6 panel: the tab bar and the Pages tab ------------------------------------------------------------------------
{
  const { f, panel } = await mount();
  const t = tabs(panel);
  assert.deepStrictEqual(t.slice(-4), ["Pages", "Devices", "Settings", "Status"], `tabs: ${t}`);
  assert.ok(!t.includes("Ring & call") && t.indexOf("Elements") < t.indexOf("Pages"), `order: ${t}`);
  assert.ok(q(panel, '[data-tab="pages-unified"]'), "the unified Pages tab");
  assert.ok(!q(panel, "nav.side"), "no sidebar, no household/phone/screen chooser");
  assert.ok(!qa(panel, "select").some((s) => /used by/i.test(s.getAttribute("aria-label") || "")), "no 'used by' chooser");
  // ONE SECTION PER PAGE, stacked, no list/strip/cards/open/back: editor on the left, preview on the right
  assert.ok(!q(panel, ".pagestrip, .pagelist, .pagecard, .pagegrid, [data-role=\"page-selector\"]"), "no page list, strip or card grid");
  assert.ok(!act(panel, "open-page") && !act(panel, "back-to-pages"), "nothing to open, nothing to go back from");
  const sections = qa(panel, ".pagesection");
  assert.deepStrictEqual(sections.map((c) => c.dataset.page), ["home", "front-ring", "night-ring"]);
  assert.deepStrictEqual(sections.map((c) => c.querySelector("h2").textContent), ["Home", "Front ring", "Night ring"], "each section is titled with the page name");
  for (const c of sections) {
    assert.ok(c.querySelector(".sectionhead [data-badge=kind]") && (c.dataset.page !== "home" || c.querySelector('.pagedetail [data-act="delete-page"]')) && c.querySelector('.pagedetail [data-hint="shown-on"]'), "the editor is in every section");
    const cols = c.querySelector(".sectioncols");
    assert.ok(cols.children[0].classList.contains("pagedetail") && cols.children[1].classList.contains("previewcol"), "editor on the left, preview on the right");
    assert.ok(cols.querySelector('.previewcol [data-field="preview-as"]') && cols.querySelector(".previewcol [data-section-preview]"), "the preview column has 'Preview as'");
  }
  assert.ok(q(panel, '[data-form="new-library-page"]') && q(panel, '[data-tab="pages-unified"]').contains(q(panel, '[data-form="new-library-page"]')), "Add a page stays");
  assert.ok(!q(panel, '[data-field="home-name"]'), "no Home name field");
  assert.ok(!q(panel, '[data-field="show-fps"]'), "the frame-rate box is on the Settings tab now");
  await sleep(120);
  const reqs = f.ofType("portico/content/preview");
  assert.strictEqual(reqs.length, 3, "one request per page");
  assert.deepStrictEqual(reqs.map((r) => r.endpoint), ["phone1", "phone1", "phone1"], "the default device");
  const ringReq = reqs.find((r) => r.caller);
  assert.strictEqual(ringReq.caller, DOOR_ID, "a Ring page previews as its Talk element's party");
  assert.ok(secq(panel, "front-ring", "[data-ring]"), "the Ring section draws the ring frame");
  assert.match(secq(panel, "front-ring", "[data-ring]").textContent, /Gate is calling/);
  const home = sec(panel, "home");
  assert.strictEqual(home.querySelector('[data-badge="kind"]').textContent, "Content page");
  assert.strictEqual(home.querySelector('[data-hint="shown-on"]').textContent, "Shown on: Alice's phone (enrolled 2026-10-08)");
  assert.match(home.querySelector('[data-hint="shown-on-warning"]').textContent, /changes it on every device listed/);
  assert.ok(home.querySelector('[data-field="block-door"]') && home.querySelector('[data-act="add-camera"]'), "a Content page: door, cameras, buttons");
  assert.ok(!home.querySelector('[data-field="block-when"]') && !home.querySelector('[data-field="ring-talk"]'));
  assert.deepStrictEqual(Array.from(home.querySelectorAll('[data-field="preview-as"] option')).map((o) => o.value), ["phone1", "kitchen"]);

  // the Ring editor
  const ring = sec(panel, "front-ring");
  assert.strictEqual(ring.querySelector("[data-badge=kind]").textContent, "Ring page");
  assert.match(ring.querySelector('[data-hint="shown-on"]').textContent, /^Rings on: every device called by Front door/);
  assert.ok(!ring.querySelector('[data-act="delete-page"]'), "a Ring page is reset on its entry, never deleted");
  assert.match(secq(panel, "night-ring", '[data-hint="shown-on"]').textContent, /^Rings on: every device called by Alice's phone/);
  const talk = ring.querySelector('[data-field="ring-talk"]');
  assert.ok(talk, "the Talk element line");
  assert.ok(talk.tagName !== "SELECT" && /Front door.*fixed/.test(talk.textContent), "the Talk element is fixed text: a Ring page is the page of its Talk element");
  const cam = ring.querySelector('[data-field="ring-camera"]');
  assert.strictEqual(cam.dataset.value, "@own");
  assert.match(cam.textContent, /caller's own cameras/);
  assert.ok(ring.querySelector('[data-field="ring-camera-pick-source"]') && ring.querySelector('[data-field="ring-camera-pick-element"]'), "the Content page's camera chooser (From Elements / Define here)");
  assert.ok(!ring.querySelector('select[data-field="ring-camera"]'), "no bespoke camera select");
  assert.match(ring.querySelector('[data-badge="kind"]').className, /ring/);
  assert.match(ring.querySelector('[data-hint="rings-when"]').textContent, /^Rings when: /);
  assert.match(ring.textContent, /every device called by this Talk element rings with/);
  assert.ok(!ring.querySelector('[data-field="block-when"]') && !ring.querySelector('[data-field="block-door"]'), "no per-block 'when' selector, no door");
  assert.ok(!ring.querySelector('[data-act="add-camera"]') && ring.querySelector('[data-act="add-button"]'), "buttons only");
  // the Ring preview: caller = the door, on the first device that answers with it (phone1)
  const last = reqs.find((r) => r.caller);
  assert.strictEqual(last.endpoint, "phone1");
  assert.ok(!("ring" in (last.document.devices.phone1 || {})) || Object.keys(last.document.devices.phone1.ring).length === 0, "no per-device ring assumption in the request");
  assert.match(ring.querySelector("[data-ring]").textContent, /Gate is calling/);
  assert.match(ring.querySelector('[data-field="preview-caller-fixed"]').textContent, /Call from: Gate/);
  { const heads = Array.from(ring.querySelectorAll("[data-section-preview] h3")).map((h) => h.textContent);
    assert.ok(heads.includes("Buttons") && heads.filter((t) => /button/i.test(t)).length === 1, `one list titled Buttons (${heads})`);
    assert.ok(!/Ring buttons|Call buttons|Buttons shown with the call/i.test(ring.textContent), "no ring/call split anywhere on a Ring page");
    assert.ok(Array.from(ring.querySelectorAll("h3")).some((h) => h.textContent === "Buttons"), "the editor heading is Buttons"); }
  { const fx = ring.querySelectorAll('[data-section-preview] [data-fixed="answer"], [data-section-preview] [data-fixed="decline"]');
    assert.deepStrictEqual(Array.from(fx).map((x) => x.textContent), ["Answer", "Decline"], "the ring preview draws the two fixed call buttons");
    assert.ok(Array.from(fx).every((x) => x.getAttribute("aria-disabled") === "true" && x.tagName !== "BUTTON"), "inert");
    assert.ok(!sec(panel, "home").querySelector('[data-section-preview] [data-fixed]'), "a Content preview has neither"); }
  assert.ok(!sec(panel, "home").querySelector('[data-field="preview-caller-fixed"]'));

  // change the when of a block, the camera: the draft is dirty, the document updated
  setValue(secq(panel, "front-ring", '[data-field="ring-camera-pick-element"]'), CAM.id, "change");
  await sleep(30);
  assert.deepStrictEqual(panel._state.draft.pages[1].cameras, [CAM.id]);
  secq(panel, "front-ring", '[data-act="ring-camera-own"]').click();
  await sleep(30);
  assert.strictEqual(panel._state.draft.pages[1].cameras, null);

  // a Ring page with a removed Talk element is kept, and says so
  panel.remove();
  console.log("PASS: schema 6 panel: tab bar, list with badges and 'Shown on', Content and Ring editors, the ring preview");
}

// ---- 6. schema 6 panel: Add page asks the kind; a Ring page needs a Talk element; delete is refused with the devices ----------------
{
  const { f, panel } = await mount();
  assert.ok(!q(panel, '[data-field="new-page-kind"]') && !q(panel, '[data-field="new-page-talk"]'), "Add page asks no kind and no Talk element");
  setValue(q(panel, '[data-field="new-page"]'), "Garden");
  click(act(panel, "add-page"));
  await sleep(40);
  assert.strictEqual(panel._state.draft.pages.at(-1).kind, "content");
  assert.ok(sec(panel, "garden") && qa(panel, ".pagesection").length === 4, "the new page has its section at the end");
  // delete a page a device uses: refused, the device is named
  click(secq(panel, "home", '[data-act="delete-page"]'));
  await sleep(30);
  assert.match(q(panel, '[data-state="message"]').textContent, /still used by Alice's phone/);
  assert.ok(panel._state.draft.pages.some((p) => p.id === "home"), "nothing was deleted");
  // delete a page nobody uses
  click(secq(panel, "garden", '[data-act="delete-page"]'));
  await sleep(30);
  assert.ok(!panel._state.draft.pages.some((p) => p.id === "garden"));
  assert.ok(!sec(panel, "garden") && sec(panel, "home"), "deleting the page removes its section only");
  setValue(q(panel, '[data-field="new-page"]'), "Terrace");
  click(act(panel, "add-page"));
  await sleep(40);
  // save: schema 6, the whole library, base rev
  click(act(panel, "save"));
  await sleep(60);
  const saved = f.ofType("portico/content/save").at(-1);
  assert.strictEqual(saved.document.schema_version, 6);
  assert.ok(!("template" in saved.document) && !("overrides" in saved.document));
  assert.strictEqual(saved.base_doc_rev, "rev1");
  panel.remove();
  console.log("PASS: Add page makes a Content page, delete is refused with the devices, the save is schema 6");
}

// ---- 7. a Ring page whose Talk element is gone; no Talk elements; Talk button not offered on a Ring page ------------------------------
{
  const loaded = v6Loaded();
  loaded.document.pages[1].talk = "el-20000077";
  loaded.document.pages[2].blocks = [{ id: "night-btns", title: "Doors", cameras: [], actions: [] }];
  const { panel } = await mount({ loaded });
  await sleep(40);
  assert.strictEqual(sec(panel, "front-ring").dataset.ringState, "removed", "the entry says so");
  assert.match(sec(panel, "front-ring").textContent, /gone or has nothing to ring for/);
  const ringSec = sec(panel, "night-ring");
  // a Ring page's Add button dialog never offers a Talk element or the Talk kind
  click(ringSec.querySelector('[data-act="add-button"]'));
  await sleep(30);
  assert.ok(!sec(panel, "home").querySelector('[data-dialog="actions"]'), "the dialog opens in its own section only");
  const form = q(panel, '[data-dialog="actions"]');
  click(Array.from(form.querySelectorAll("button")).find((b) => /Define here/.test(b.textContent)));
  await sleep(30);
  const kinds = Array.from(q(panel, '[data-dialog="actions"]').querySelectorAll('[data-field="kind"] option')).map((o) => o.value);
  assert.ok(kinds.length && !kinds.includes("talk"), "no Talk kind on a Ring page");
  click(Array.from(q(panel, '[data-dialog="actions"]').querySelectorAll("button")).find((b) => /From Elements/.test(b.textContent)));
  await sleep(30);
  const names = qa(panel, '[data-dialog="actions"] .elrow').map((b) => b.textContent);
  assert.ok(names.some((n) => n.startsWith("Gate lock")) && !names.some((n) => /Front door|Alice's phone/.test(n)), `elements offered: ${names}`);
  panel.remove();
  console.log("PASS: a removed Talk element is shown, a Ring page is offered no Talk element");
}

// ---- 7b. sections: an empty library, at most 2 previews in flight, errors per section, per-section debounce -------------------------
{
  const empty = v6Loaded();
  empty.document.pages = [];
  empty.document.devices = { phone1: { pages: [], ring: {}, roster_hide: [] } };
  const e = await mount({ loaded: empty });
  assert.ok(!qa(e.panel, ".pagesection[data-page]").length && /No pages yet/.test(q(e.panel, '[data-role="page-sections"]').textContent), "the calm empty state");
  assert.ok(q(e.panel, '[data-form="new-library-page"]'), "with Add page");
  e.panel.remove();

  const loaded = v6Loaded();
  for (let i = 0; i < 4; i++) loaded.document.pages.push({ id: `extra-${i}`, label: `Extra ${i}`, kind: "content", blocks: [] });
  let inflight = 0, peak = 0;
  const m = await mount({ loaded, handlers: { "portico/content/preview": async (msg) => {
    if (msg.document.pages.find((p) => p.id === "night-ring") && msg.caller === undefined && msg.document.devices.phone1.pages.some((x) => x.page === "extra-1")) throw { code: "invalid", message: "Nope." };
    inflight++; peak = Math.max(peak, inflight); await sleep(15); inflight--;
    return { endpoint: msg.endpoint, kind: "phone", home_name: "x", rev: "r", pages: [], callers: {}, roster: [] };
  } } });
  await sleep(300);
  assert.strictEqual(m.f.ofType("portico/content/preview").length, 7, "one request per page");
  assert.ok(peak <= 2, `at most 2 previews in flight, saw ${peak}`);
  assert.match(secq(m.panel, "extra-1", "[data-section-preview]").textContent, /Nope/, "a refusal is shown in its own section");
  assert.strictEqual(secq(m.panel, "home", "h2").textContent, "Home");
  m.panel.remove();
  console.log("PASS: sections: empty state, one request per page with at most 2 in flight, a refusal stays in its section");
}

// ---- 8. the old tab ids land on the unified tabs ---------------------------------------------------------------------------------------
{
  const { panel } = await mount();
  panel._pickTab("calls");
  await sleep(20);
  assert.strictEqual(panel._tab, "pages");
  panel._pickTab("screens");
  await sleep(20);
  assert.strictEqual(panel._tab, "devices");
  panel._pickTab("phones");
  assert.strictEqual(panel._tab, "devices");
  panel.remove();
  console.log("PASS: calls, screens and phones land on Pages and Devices in a schema 6 document");
}

// ---- 9. schema 4/5: the old tabs exactly, the banner, the dry run, the confirm page -------------------------------------------------
{
  const { f, panel } = await mount({ loaded: makeLoaded(), handlers: { "portico/content/pages_migrate": (msg) => (msg.validate_only ? REPORT : { ...REPORT, applied: true, doc_rev: "rev9" }) } });
  const t = tabs(panel);
  assert.deepStrictEqual(t.slice(-5), ["Pages", "Devices", "Ring & call", "Settings", "Status"], `legacy tabs: ${t}`);
  assert.ok(q(panel, "nav.side") && !q(panel, '[data-tab="pages-unified"]'), "the old Pages tab with its sidebar");
  const banner = q(panel, '[data-state="migration-banner"]');
  assert.ok(banner, "a calm banner for the administrator");
  assert.match(banner.textContent, /Pages and Ring & call will be unified: run the migration dry run/);
  assert.strictEqual(f.ofType("portico/content/pages_migrate").length, 0, "nothing is asked until the button is pressed");
  click(act(panel, "check-migration"));
  await sleep(60);
  const calls = f.ofType("portico/content/pages_migrate");
  assert.strictEqual(calls.length, 1);
  assert.strictEqual(calls[0].validate_only, true, "the check is a dry run");
  const dlg = q(panel, '[data-dialog="migration"]');
  assert.ok(dlg && dlg.getAttribute("role") === "dialog");
  assert.match(dlg.textContent, /every phone and screen gets the same result/);
  assert.strictEqual(dlg.querySelector('[data-count="library_pages"] td:last-child').textContent, "3");
  assert.match(dlg.textContent, /Alice's phone \(enrolled 2026-10-08\): same result/);
  assert.match(dlg.textContent, /Gate ring/);
  assert.ok(!act(panel, "migration-apply").disabled, "can_apply: the button is enabled");
  // the apply is not automatic: the first press only shows the confirm page, naming what will change
  click(act(panel, "migration-apply"));
  await sleep(30);
  assert.strictEqual(f.ofType("portico/content/pages_migrate").length, 1, "still only the dry run");
  const confirm = q(panel, '[data-dialog="migration"]');
  assert.strictEqual(confirm.dataset.phase, "confirm");
  assert.match(confirm.querySelector('[data-list="will-change"]').textContent, /schema 6 \(from 4\)/);
  assert.match(confirm.textContent, /\.storage\/portico\.content\.v4/);
  click(act(panel, "migration-back"));
  await sleep(20);
  assert.strictEqual(q(panel, '[data-dialog="migration"]').dataset.phase, "report");
  click(act(panel, "migration-apply"));
  await sleep(20);
  click(act(panel, "migration-confirm"));
  await sleep(80);
  const all = f.ofType("portico/content/pages_migrate");
  assert.strictEqual(all.length, 2);
  assert.deepStrictEqual([all[1].validate_only, all[1].base_doc_rev], [false, "rev1"], "the apply carries the doc_rev of the dry run");
  assert.ok(!q(panel, '[data-dialog="migration"]'), "the dialog is gone");
  assert.strictEqual(f.documentCalls() >= 2, true, "the editor reloads the document after the apply");
  panel.remove();

  // can_apply false: the Apply button is disabled and no apply can be sent
  const bad = { ...REPORT, ok: false, can_apply: false, problems: ["the resolved document differs before and after for: phone1"],
                per_endpoint: [{ ...REPORT.per_endpoint[0], equal: false, differences: ["callers.*"] }] };
  const m2 = await mount({ loaded: makeLoaded(), handlers: { "portico/content/pages_migrate": () => bad } });
  click(act(m2.panel, "check-migration"));
  await sleep(60);
  assert.ok(act(m2.panel, "migration-apply").disabled, "Apply is disabled unless can_apply");
  assert.match(q(m2.panel, '[data-dialog="migration"]').textContent, /DIFFERENT \(callers\.\*\)/);
  assert.match(q(m2.panel, '[data-dialog="migration"]').textContent, /differs before and after/);
  click(act(m2.panel, "migration-apply"));
  await sleep(20);
  m2.panel._applyMigration();
  await sleep(20);
  assert.ok(m2.f.ofType("portico/content/pages_migrate").every((m) => m.validate_only === true), "no apply was ever sent");
  m2.panel.remove();

  // an unsaved draft: the apply works on the saved content, so it waits
  const m3 = await mount({ loaded: makeLoaded(), handlers: { "portico/content/pages_migrate": () => REPORT } });
  m3.panel._state.draft.home_name = "Changed";
  m3.panel._render();
  click(act(m3.panel, "check-migration"));
  await sleep(60);
  assert.ok(act(m3.panel, "migration-apply").disabled, "unsaved changes: Apply waits");
  m3.panel.remove();

  // a refused apply (the document moved): said plainly, nothing reloaded as success
  const m4 = await mount({ loaded: makeLoaded(), handlers: { "portico/content/pages_migrate": (msg) => { if (msg.validate_only) return REPORT; throw { code: "conflict", message: "the document changed since the dry run" }; } } });
  click(act(m4.panel, "check-migration"));
  await sleep(60);
  click(act(m4.panel, "migration-apply"));
  await sleep(20);
  click(act(m4.panel, "migration-confirm"));
  await sleep(60);
  assert.match(q(m4.panel, '[data-state="migration-error"]').textContent, /Nothing was migrated; run the check again/);
  m4.panel.remove();

  // an old server part without the command
  const m5 = await mount({ loaded: makeLoaded() });
  click(act(m5.panel, "check-migration"));
  await sleep(60);
  assert.match(q(m5.panel, '[data-state="migration-error"]').textContent, /newer Portico server part/);
  m5.panel.remove();
  console.log("PASS: schema 4/5: old tabs untouched, banner, dry run, report, confirm page, disabled Apply, conflict, never automatic");
}

// ---- 10. the banner is for administrators only: a non-administrator gets the read-only view, not the editor -------------------------
{
  const { panel } = await mount({ loaded: makeLoaded(), user: { is_admin: false }, handlers: { "portico/content/get": () => ({ pages: [{ id: "home", label: "Home", blocks: [] }], cameras: [], actions: [] }) } });
  assert.ok(!q(panel, '[data-state="migration-banner"]') && !q(panel, '[data-act="check-migration"]'));
  panel.remove();
  const src = fs.readFileSync(new URL("../portico-panel.js", import.meta.url), "utf8");
  assert.strictEqual((src.match(/validate_only: false/g) || []).length, 1, "exactly one place sends an apply");
  for (const bad of ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "srcdoc"]) assert.ok(!src.includes(bad), `no ${bad}`);
  console.log("PASS: no banner or button without the administrator; one place sends an apply; no HTML-string setters");
}

console.log("ALL PASS test_panel_pages_unified");
process.exit(0);
