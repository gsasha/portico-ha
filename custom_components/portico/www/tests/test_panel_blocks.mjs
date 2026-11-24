// The editor's blocks (DEV-658 B3; blocks design 5): the pure part (`logic`) and the panel in jsdom with a fake `hass`.
// Blocks, camera sizes, button colours and icons, the Talk row, the split prompt, the home name, and the wire rules:
// schema_version 4 on every save and preview, and NO flat `cameras`/`actions` on any page. Protocol-level; NOT a visual
// check (see panel_harness.html).
import assert from "assert";
import fs from "fs";
import { loadPanel, editorHass, makeLoaded, sleep, STATES, DOOR_ID, withMirror } from "./panel_loader.mjs";

const { dom, Panel, doc } = loadPanel();
const win = dom.window;
const L = Panel.logic;
const fresh = () => L.createState(makeLoaded());
const throwsEdit = (fn, re) => assert.throws(fn, (e) => e instanceof L.EditError && re.test(e.message));
const q = (p, sel) => p.shadowRoot.querySelector(sel);
const qa = (p, sel) => Array.from(p.shadowRoot.querySelectorAll(sel));
const act = (p, name) => q(p, `[data-act="${name}"]`);
const click = (node) => { assert.ok(node, "clicked something that is not there"); node.dispatchEvent(new win.Event("click", { bubbles: true })); };
function setValue(node, value, type = "input") {
  assert.ok(node, "set a value on something that is not there");
  node.value = value;
  node.dispatchEvent(new win.Event(type, { bubbles: true }));
}
function setChecked(node, on) {
  node.checked = on;
  node.dispatchEvent(new win.Event("change", { bubbles: true }));
}
const key = (node, name) => node.dispatchEvent(new win.KeyboardEvent("keydown", { key: name, bubbles: true, cancelable: true }));
const text = (p) => p.shadowRoot.textContent;
const labelsOf = (p, sel) => qa(p, sel).map((n) => n.textContent);
const rgb = (css) => { const n = doc.createElement("div"); n.style.background = css; return n.style.background; };

async function mount(fake, { previewMs = 10 } = {}) {
  const panel = doc.createElement("portico-panel");
  panel._previewMs = previewMs;
  panel.hass = fake.hass;
  doc.body.appendChild(panel);
  await sleep(50);
  return panel;
}

const CONTRACT_DIRS = ["/src/contract/", new URL("../../../../contract/", import.meta.url).pathname];
function contract(name) {
  for (const dir of CONTRACT_DIRS) if (fs.existsSync(dir + name)) return JSON.parse(fs.readFileSync(dir + name, "utf8"));
  throw new Error(`contract/${name} is not reachable from here (looked in ${CONTRACT_DIRS.join(", ")})`);
}

const STATES2 = { ...STATES, "cover.gate": { state: "closed", attributes: { friendly_name: "Gate cover" } },
                  "cover.garage": { state: "closed", attributes: { friendly_name: "Garage cover" } } };

/* A loaded document whose template page has two blocks: "Gate" (a door, two cameras, an open-door button and a Talk
 * button) and "House" (one coloured, text-less script button). The flat mirror in it is STALE on purpose. */
function blocksLoaded() {
  const loaded = makeLoaded();
  const page = loaded.document.template.pages[0];
  page.blocks = [
    { id: "gate", title: "Gate", door: "http://10.6.0.7:8091",
      cameras: [{ id: "gate", label: "Gate", entity_id: "camera.gate" }, { id: "yard", label: "Yard", entity_id: "camera.garage", size: "small" }],
      actions: [{ id: "open-door", label: "Open the door", kind: "portico_open", icon: "door-sliding-open", color: "green", confirm: true },
                { id: "talk", label: "Talk", kind: "talk", icon: "account-voice", confirm: false }] },
    { id: "house", title: "House", cameras: [],
      actions: [{ id: "lights", label: "Lights", kind: "script_run", entity_id: "script.evening", icon: "lightbulb", color: "amber", show_label: false, confirm: false }] },
  ];
  return loaded;
}

const mkCams = (n) => Array.from({ length: n }, (_, i) => ({ id: `c${i}`, label: `Cam ${i}`, entity_id: `camera.c${i}` }));
const mkActs = (n) => Array.from({ length: n }, (_, i) => ({ id: `a${i}`, label: `Act ${i}`, kind: "button_press", entity_id: "button.garage", confirm: false }));
const homeBlock = (draft) => L.pagesOf(draft, "template")[0].blocks[0];
const blockIds = (draft, pageId = "home") => L.pagesOf(draft, "template").find((p) => p.id === pageId).blocks.map((b) => b.id);

// 1. block operations: add, rename, move, delete, each undoable; the rules on titles and the limit
{
  const s = fresh();
  assert.deepStrictEqual(L.pagesOf(s.draft, "template")[0].blocks.map((b) => b.title), ["Home"], "a migrated page is one block named after the page");
  const r = L.addBlock(s.draft, "template", "home", "  Garden ");
  assert.strictEqual(r.blockId, "garden");
  assert.deepStrictEqual(L.pagesOf(r.doc, "template")[0].blocks.map((b) => b.title), ["Home", "Garden"]);
  assert.deepStrictEqual(L.pagesOf(r.doc, "template")[0].blocks[1], { id: "garden", title: "Garden", cameras: [], actions: [] });
  throwsEdit(() => L.addBlock(r.doc, "template", "home", "GARDEN"), /already called/);
  throwsEdit(() => L.addBlock(s.draft, "template", "home", "   "), /needs a title/);
  throwsEdit(() => L.addBlock(s.draft, "template", "home", "x".repeat(41)), /longer than 40/);
  throwsEdit(() => L.addBlock(s.draft, "template", "home", "a\nb"), /control characters/);
  throwsEdit(() => L.addBlock(s.draft, "template", "nope", "X"), /not editable here/);
  let doc = s.draft;
  for (let i = 2; i <= 8; i++) doc = L.addBlock(doc, "template", "home", `Block ${i}`).doc;
  assert.strictEqual(blockIds(doc).length, 8);
  throwsEdit(() => L.addBlock(doc, "template", "home", "one too many"), /At most 8 blocks/);

  const renamed = L.renameBlock(r.doc, "template", "home", "garden", "Back yard");
  assert.deepStrictEqual(L.pagesOf(renamed, "template")[0].blocks.map((b) => [b.id, b.title]), [["home", "Home"], ["garden", "Back yard"]], "the id stays");
  assert.ok(L.renameBlock(r.doc, "template", "home", "garden", "garden"), "its own title in another case is not a clash");
  throwsEdit(() => L.renameBlock(r.doc, "template", "home", "garden", "home"), /already called/);
  throwsEdit(() => L.renameBlock(r.doc, "template", "home", "garden", ""), /needs a title/);
  throwsEdit(() => L.renameBlock(r.doc, "template", "home", "ghost", "Z"), /gone/);

  assert.deepStrictEqual(blockIds(L.moveBlock(r.doc, "template", "home", "garden", -1)), ["garden", "home"]);
  assert.deepStrictEqual(blockIds(L.moveBlock(r.doc, "template", "home", "garden", 1)), ["home", "garden"], "the last one stays last");
  assert.deepStrictEqual(blockIds(L.deleteBlock(r.doc, "template", "home", "home")), ["garden"]);
  assert.deepStrictEqual(blockIds(L.deleteBlock(L.deleteBlock(r.doc, "template", "home", "home"), "template", "home", "garden")), [], "a page may hold no block");
  throwsEdit(() => L.deleteBlock(r.doc, "template", "home", "ghost"), /gone/);

  // every operation is one Undo step
  const ops = [
    (d) => L.addBlock(d, "template", "home", "Orchard").doc,
    (d) => L.renameBlock(d, "template", "home", "home", "Front"),
    (d) => L.moveBlock(d, "template", "home", "garden", -1),
    (d) => L.deleteBlock(d, "template", "home", "garden"),
    (d) => L.setBlockDoor(d, "template", "home", "home", "http://10.6.0.7:8091"),
    (d) => L.setCameraSize(d, "template", "home", "gate", "big"),
    (d) => L.setButtonColor(d, "template", "home", "open-gate", "red"),
    (d) => L.setShowLabel(d, "template", "home", "open-gate", false),
    (d) => L.moveTileToBlock(d, "template", "home", "cameras", "gate", "garden"),
    (d) => L.setHomeName(d, "Elsewhere"),
  ];
  const t = fresh();
  t.draft = L.addBlock(t.draft, "template", "home", "Garden").doc;
  t.loaded = { ...t.loaded, document: L.clone(t.draft) };
  for (const op of ops) {
    const before = L.clone(t.draft);
    L.commit(t, op(t.draft));
    assert.ok(!L.same(t.draft, before), "the operation changed the draft");
    assert.ok(L.undo(t));
    assert.ok(L.same(t.draft, before), "Undo restores the draft exactly");
  }
  console.log("PASS: blocks are added, renamed, moved and deleted with unique titles, the 8 limit, and each is one Undo step");
}

// 2. cameras and buttons into a chosen block; ids unique per page and stable; sizes; limits
{
  const s = fresh();
  let doc = L.addBlock(s.draft, "template", "home", "Garden").doc;
  doc = L.addCamera(doc, "template", "home", "camera.garage", "Garage", "garden");
  doc = L.addAction(doc, "template", "home", { kind: "script_run", target: "script.evening", label: "Evening", confirm: false, blockId: "garden" });
  const page = L.pagesOf(doc, "template")[0];
  assert.deepStrictEqual(page.blocks[1].cameras, [{ id: "garage", label: "Garage", entity_id: "camera.garage" }]);
  assert.deepStrictEqual(page.blocks[1].actions.map((a) => a.id), ["evening"]);
  assert.deepStrictEqual(page.blocks[0].cameras.map((c) => c.id), ["gate"], "the other block is untouched");
  // no block named: the first one
  const first = L.addCamera(s.draft, "template", "home", "camera.garage", "Garage");
  assert.deepStrictEqual(homeBlock(first).cameras.map((c) => c.id), ["gate", "garage"]);
  // ids are unique across the page's blocks
  const dup = L.addCamera(doc, "template", "home", "camera.gate", "Gate again", "garden");
  assert.deepStrictEqual(L.pageCameras(L.pagesOf(dup, "template")[0]).map((c) => c.id), ["gate", "garage", "gate-2"], "a second camera.gate gets a fresh id");
  const dupAct = L.addAction(doc, "template", "home", { kind: "lock_open", target: "lock.gate", label: "Open the gate", confirm: true, blockId: "garden" });
  assert.deepStrictEqual(L.pageActions(L.pagesOf(dupAct, "template")[0]).map((a) => a.id), ["open-gate", "evening", "open-the-gate"]);
  throwsEdit(() => L.addCamera(doc, "template", "home", "camera.garage", "G", "ghost"), /gone/);
  throwsEdit(() => L.addAction(doc, "template", "home", { kind: "script_run", target: "script.evening", label: "E", blockId: "ghost" }), /gone/);
  const none = L.deleteBlock(L.deleteBlock(doc, "template", "home", "home"), "template", "home", "garden");
  throwsEdit(() => L.addCamera(none, "template", "home", "camera.garage", "G"), /Add a block/);

  // a tile moves between blocks and keeps its id; a door-less open-door button keeps its meaning
  let d2 = L.setBlockDoor(doc, "template", "home", "home", "http://10.6.0.7:8091");
  d2 = L.addAction(d2, "template", "home", { kind: "portico_open", target: "", label: "Open door", confirm: true, blockId: "home" });
  assert.ok(!("device_id" in L.pageActions(L.pagesOf(d2, "template")[0]).find((a) => a.id === "open-door")), "it acts on the block's door");
  const moved = L.moveTileToBlock(d2, "template", "home", "actions", "open-door", "garden");
  const mp = L.pagesOf(moved, "template")[0];
  assert.deepStrictEqual(mp.blocks[1].actions.map((a) => a.id), ["evening", "open-door"], "appended to the other block, same id");
  assert.strictEqual(mp.blocks[1].actions[1].device_id, "http://10.6.0.7:8091", "the door it opened is written in before it moves");
  assert.ok(!mp.blocks[0].actions.some((a) => a.id === "open-door"));
  const cam = L.moveTileToBlock(doc, "template", "home", "cameras", "gate", "garden");
  assert.deepStrictEqual(L.pagesOf(cam, "template")[0].blocks[1].cameras.map((c) => c.id), ["garage", "gate"]);
  assert.ok(L.same(L.moveTileToBlock(doc, "template", "home", "cameras", "gate", "home"), doc), "moving to its own block changes nothing");
  throwsEdit(() => L.moveTileToBlock(doc, "template", "home", "cameras", "nope", "garden"), /gone/);

  // a move up or down stays inside the block
  let two = L.addCamera(doc, "template", "home", "camera.zebra", "Zebra", "home");
  assert.deepStrictEqual(homeBlock(L.moveTile(two, "template", "home", "cameras", "zebra", -1)).cameras.map((c) => c.id), ["zebra", "gate"]);
  assert.deepStrictEqual(homeBlock(L.moveTile(two, "template", "home", "cameras", "zebra", 1)).cameras.map((c) => c.id), ["gate", "zebra"]);
  assert.deepStrictEqual(L.pagesOf(L.moveTile(two, "template", "home", "cameras", "garage", -1), "template")[0].blocks[1].cameras.map((c) => c.id), ["garage"], "the first stays first; no jump to the block above");

  // limits: 12 and 12 per block, 24 and 24 per page
  let full = L.addBlock(s.draft, "template", "home", "Full").doc;
  full = L.clone(full);
  const fb = L.pagesOf(full, "template")[0].blocks[1];
  fb.cameras = mkCams(12);
  fb.actions = mkActs(12);
  throwsEdit(() => L.addCamera(full, "template", "home", "camera.garage", "G", "full"), /At most 12 cameras in a block/);
  throwsEdit(() => L.addAction(full, "template", "home", { kind: "script_run", target: "script.evening", label: "E", blockId: "full" }), /At most 12 buttons in a block/);
  throwsEdit(() => L.moveTileToBlock(full, "template", "home", "cameras", "gate", "full"), /At most 12 cameras in a block/);
  const wide = L.clone(full);
  const wp = L.pagesOf(wide, "template")[0];
  wp.blocks[0].cameras = mkCams(12).map((c) => ({ ...c, id: `d${c.id}` }));
  wp.blocks.push({ id: "third", title: "Third", cameras: [], actions: [] });
  wp.blocks[0].actions = mkActs(12).map((a) => ({ ...a, id: `d${a.id}` }));
  throwsEdit(() => L.addCamera(wide, "template", "home", "camera.garage", "G", "third"), /At most 24 cameras on a page/);
  console.log("PASS: cameras and buttons go to a chosen block, ids stay unique per page and stable across a move, the limits hold");
}

// 3. camera sizes, button colours (palette only), text visibility, icons
{
  const s = fresh();
  let doc = L.setCameraSize(s.draft, "template", "home", "gate", "big");
  assert.strictEqual(homeBlock(doc).cameras[0].size, "big");
  doc = L.setCameraSize(doc, "template", "home", "gate", "small");
  assert.strictEqual(homeBlock(doc).cameras[0].size, "small");
  doc = L.setCameraSize(doc, "template", "home", "gate", "auto");
  assert.ok(!("size" in homeBlock(doc).cameras[0]), "auto is the absence of a size");
  throwsEdit(() => L.setCameraSize(doc, "template", "home", "gate", "huge"), /size is one of/);
  throwsEdit(() => L.setCameraSize(doc, "template", "home", "ghost", "big"), /gone/);

  for (const name of contract("palette_v1.json").colors.map((c) => c.name)) {
    assert.strictEqual(L.pageActions(L.pagesOf(L.setButtonColor(s.draft, "template", "home", "open-gate", name), "template")[0])[0].color, name);
  }
  for (const bad of ["#ff0000", "rgb(1,2,3)", "Blue", "orange", "none ", "red;"]) {
    throwsEdit(() => L.setButtonColor(s.draft, "template", "home", "open-gate", bad), /A colour is one of: blue, green, amber, red, purple, teal, pink, grey/);
  }
  const col = L.setButtonColor(s.draft, "template", "home", "open-gate", "teal");
  assert.ok(!("color" in L.pageActions(L.pagesOf(L.setButtonColor(col, "template", "home", "open-gate", "default"), "template")[0])[0]), "default removes the colour");
  assert.ok(!("color" in L.pageActions(L.pagesOf(L.setButtonColor(col, "template", "home", "open-gate", null), "template")[0])[0]));
  throwsEdit(() => L.addAction(s.draft, "template", "home", { kind: "script_run", target: "script.evening", label: "E", color: "#00ff00" }), /A colour is one of/);

  // text can be hidden only on a button with an icon from the list
  const hidden = L.setShowLabel(s.draft, "template", "home", "open-gate", false);
  assert.strictEqual(L.pageActions(L.pagesOf(hidden, "template")[0])[0].show_label, false);
  assert.ok(!("show_label" in L.pageActions(L.pagesOf(L.setShowLabel(hidden, "template", "home", "open-gate", true), "template")[0])[0]), "shown is the default and is not stored");
  const noicon = L.addAction(s.draft, "template", "home", { kind: "script_run", target: "script.evening", label: "Evening", confirm: false });
  throwsEdit(() => L.setShowLabel(noicon, "template", "home", "evening", false), /Pick an icon from the list before hiding the text/);
  throwsEdit(() => L.addAction(s.draft, "template", "home", { kind: "script_run", target: "script.evening", label: "E", show_label: false }), /Pick an icon/);
  throwsEdit(() => L.updateTile(hidden, "template", "home", "actions", "open-gate", { icon: "none" }), /Pick an icon/);
  throwsEdit(() => L.addAction(s.draft, "template", "home", { kind: "script_run", target: "script.evening", label: "E", icon: "not-in-the-list", show_label: false }), /Pick an icon/);
  assert.ok(L.addAction(s.draft, "template", "home", { kind: "script_run", target: "script.evening", label: "E", icon: "mdi:lightbulb", show_label: false }), "the mdi: prefix is fine");
  // the icon list: 118 names, equal to the contract, every one in exactly one group
  const icons = contract("icons_v1.json").icons;
  assert.deepStrictEqual(L.ICON_NAMES, icons, "the panel's icon list equals contract/icons_v1.json");
  assert.strictEqual(icons.length, 118);
  for (const n of ["door-sliding", "door-sliding-lock", "door-sliding-open", "gate-arrow-left", "gate-arrow-right", "boom-gate", "boom-gate-up", "garage-variant",
                   "garage-open-variant", "garage-lock", "lock-smart", "lock-check", "lock-clock", "lock-alert", "lock-open-check", "account-voice",
                   "microphone-off", "phone-cancel", "doorbell-video"]) assert.ok(L.ICON_NAMES.includes(n), `${n} is one of the 19 new names`);
  const groups = new Map();
  for (const n of L.ICON_NAMES) groups.set(L.iconGroup(n), [...(groups.get(L.iconGroup(n)) || []), n]);
  assert.deepStrictEqual([...groups.keys()].sort(), ["door", "garage", "gate", "lock", "other", "talk"]);
  assert.strictEqual([...groups.values()].flat().length, 118);
  assert.ok(groups.get("door").includes("door-sliding-open") && !groups.get("door").includes("doorbell"), "doorbell is not a door");
  assert.ok(groups.get("garage").includes("garage-lock") && groups.get("gate").includes("boom-gate-up") && groups.get("talk").includes("account-voice"));
  assert.ok(groups.get("lock").includes("lock-open-check"));
  console.log("PASS: sizes, palette-only colours, hidden text needs an icon, the icon list is the contract's 118 names in six groups");
}

// 4. the palette is the contract's: names, light and dark values
{
  const palette = contract("palette_v1.json");
  assert.deepStrictEqual(L.PALETTE_NAMES, palette.colors.map((c) => c.name));
  assert.strictEqual(L.PALETTE.length, 8);
  assert.deepStrictEqual(L.PALETTE, palette.colors.map((c) => ({ name: c.name, light: c.light, dark: c.dark })), "every fill and text value equals contract/palette_v1.json");
  console.log("PASS: the panel's palette equals contract/palette_v1.json (names, light and dark fill and text)");
}

// 5. new button kinds, Talk, door rules
{
  const s = fresh();
  const add = (spec) => L.pageActions(L.pagesOf(L.addAction(s.draft, "template", "home", spec), "template")[0]).at(-1);
  assert.deepStrictEqual(add({ kind: "lock_lock", target: "lock.front", label: "Lock front", confirm: false }),
    { id: "lock-front", label: "Lock front", kind: "lock_lock", entity_id: "lock.front", confirm: false });
  assert.strictEqual(add({ kind: "lock_unlock", target: "lock.front", label: "Unlock", confirm: L.KINDS.lock_unlock.confirm }).confirm, true, "unlock asks first by default");
  assert.deepStrictEqual([L.KINDS.lock_lock.confirm, L.KINDS.cover_open.confirm, L.KINDS.cover_close.confirm], [false, true, false]);
  assert.strictEqual(add({ kind: "cover_open", target: "cover.gate", label: "Open gate", confirm: true }).entity_id, "cover.gate");
  assert.strictEqual(add({ kind: "cover_close", target: "cover.garage", label: "Close garage", confirm: false }).kind, "cover_close");
  throwsEdit(() => L.addAction(s.draft, "template", "home", { kind: "cover_open", target: "lock.front", label: "x" }), /Pick a cover/);
  throwsEdit(() => L.addAction(s.draft, "template", "home", { kind: "lock_lock", target: "cover.gate", label: "x" }), /Pick a lock/);
  throwsEdit(() => L.addAction(s.draft, "template", "home", { kind: "talk", target: "", label: "Talk", icon: "account-voice" }), /Pick the block's door first/);
  // the open-door button: a door of its own, or the block's
  throwsEdit(() => L.addAction(s.draft, "template", "home", { kind: "portico_open", target: "", label: "Open" }), /Pick the door/);
  const withDoor = L.setBlockDoor(s.draft, "template", "home", "home", "http://10.6.0.7:8091");
  const own = L.addAction(withDoor, "template", "home", { kind: "portico_open", target: "", label: "Open", confirm: true });
  assert.ok(!("device_id" in L.pageActions(L.pagesOf(own, "template")[0]).at(-1)), "the block's door is used, nothing is copied in");
  // Talk to the door (DEV-692 T1): created in a block that has a door; it names no target, never asks first
  {
    const made = L.addAction(withDoor, "template", "home", { kind: "talk", target: "http://other", label: "Talk to gate", icon: "account-voice", confirm: true, color: "blue" });
    assert.deepStrictEqual(L.pageActions(L.pagesOf(made, "template")[0]).at(-1),
      { id: "talk-to-gate", label: "Talk to gate", kind: "talk", icon: "account-voice", confirm: false, color: "blue" },
      "no device_id (the block's door is used), no confirm even when one is asked for");
    assert.deepStrictEqual(L.validate(made), [], "a Talk button in a block with a door is valid");
    throwsEdit(() => L.addAction(withDoor, "template", "home", { kind: "talk", target: "", label: "Talk", show_label: false }), /needs an icon|Pick an icon/);
    assert.strictEqual(L.addAction(withDoor, "template", "home", { kind: "talk", target: "", label: "Talk", icon: "microphone", show_label: false }) !== null, true, "an icon allows hidden text");
    assert.strictEqual(L.KINDS.talk.confirm, false);
    assert.ok(L.KINDS.talk.text.includes("greyed on the phone until the gate allows it"), "the option says what the owner will see");
    // a Talk button created by the editor is still kept out of every ring rule
    assert.ok(!L.ruleButtonChoices(made, "template").some((c) => c.id === "talk-to-gate"), "not offered to a rule");
    const ringed = L.clone(made);
    ringed.template.rules = [{ id: "r", caller: "kind:door", cameras: null, ring_buttons: ["talk-to-gate"], call_buttons: [] }];
    assert.ok(L.validate(ringed).some((p) => /names the Talk button "talk-to-gate"/.test(p)), L.validate(ringed).join("|"));
  }
  const named = L.addAction(withDoor, "template", "home", { kind: "portico_open", target: "http://other", label: "Other", confirm: true });
  assert.strictEqual(L.pageActions(L.pagesOf(named, "template")[0]).at(-1).device_id, "http://other");
  assert.deepStrictEqual(L.validate(own), []);
  // removing the door of a block whose open-door button names none is a problem the save would also refuse
  const lost = L.setBlockDoor(own, "template", "home", "home", "");
  assert.ok(!("door" in homeBlock(lost)));
  assert.ok(L.validate(lost).some((p) => /"Open" needs its door/.test(p)), L.validate(lost).join("|"));

  // a stored Talk button is shown and kept: it needs the block's door, and a ring never names it
  const talkDoc = L.clone(withDoor);
  homeBlock(talkDoc).actions.push({ id: "talk", label: "Talk", kind: "talk", icon: "account-voice", confirm: false });
  assert.deepStrictEqual(L.validate(talkDoc), []);
  const edited = L.updateTile(talkDoc, "template", "home", "actions", "talk", { label: "Talk to gate", color: "blue", confirm: true });
  const tk = homeBlock(edited).actions.at(-1);
  assert.deepStrictEqual([tk.label, tk.color, tk.confirm], ["Talk to gate", "blue", false], "Talk never gains confirm");
  const doorless = L.setBlockDoor(talkDoc, "template", "home", "home", "");
  assert.ok(L.validate(doorless).some((p) => /Talk button "Talk" needs the block to have a door/.test(p)));
  assert.ok(!L.ruleButtonChoices(talkDoc, "template").some((c) => c.id === "talk"), "a Talk button is not offered to a rule");
  const ruled = L.clone(talkDoc);
  ruled.template.rules = [{ id: "r", caller: "kind:door", cameras: null, ring_buttons: ["talk"], call_buttons: [] }];
  assert.ok(L.validate(ruled).some((p) => /names the Talk button "talk"/.test(p)), L.validate(ruled).join("|"));
  console.log("PASS: lock/unlock/gate kinds with their confirm defaults, Talk cannot be added but a stored one is kept, door rules");
}

// 6. the wire: schema_version 4, no flat cameras / actions, a v3 page becomes one block
{
  const loaded = makeLoaded();
  assert.ok("cameras" in loaded.document.template.pages[0] && "actions" in loaded.document.template.pages[0], "the fixture carries the server's flat mirror");
  const s = L.createState(loaded);
  const flatKeys = (d) => [d.template, ...Object.values(d.overrides)].flatMap((h) => h.pages).filter((p) => "cameras" in p || "actions" in p).map((p) => p.id);
  assert.deepStrictEqual(flatKeys(s.draft), [], "the draft holds no flat list");
  assert.deepStrictEqual(flatKeys(s.loaded.document), []);
  assert.strictEqual(L.isDirty(s), false, "dropping the mirror is not a change");
  for (const req of [L.saveRequest(s), L.previewRequest(s)]) {
    assert.strictEqual(req.document.schema_version, 4);
    assert.deepStrictEqual(flatKeys(req.document), []);
  }
  // a stale flat list that got into the draft anyway never reaches the wire (it would win over the blocks)
  s.draft.template.pages[0].cameras = [{ id: "stale", label: "Stale", entity_id: "camera.stale" }];
  s.draft.template.pages[0].actions = [];
  s.draft.overrides["old-phone-user-id-123"].pages[0].cameras = [];
  s.draft.schema_version = 3;
  const save = L.saveRequest(s);
  assert.deepStrictEqual(flatKeys(save.document), []);
  assert.deepStrictEqual(flatKeys(L.previewRequest(s).document), []);
  assert.strictEqual(save.document.schema_version, 4, "schema 4 whatever the draft said");
  assert.strictEqual(L.previewRequest(s).document.schema_version, 4);
  assert.deepStrictEqual(save.document.template.pages[0].blocks[0].cameras.map((c) => c.id), ["gate"], "and the blocks are what is sent");
  assert.strictEqual(s.draft.template.pages[0].cameras.length, 1, "building the request does not touch the draft");

  // an older server (no blocks): a flat page is one block named after the page, as the migration does
  const old = makeLoaded();
  old.document.schema_version = 3;
  old.document.template.pages = [{ id: "front", label: "Front\nyard", cameras: [{ id: "g", label: "G", entity_id: "camera.gate" }],
                                  actions: [{ id: "o", label: "O", kind: "lock_open", entity_id: "lock.gate", confirm: true }] }];
  const o = L.createState(old);
  assert.deepStrictEqual(o.draft.template.pages, [{ id: "front", label: "Front\nyard", blocks: [{ id: "front", title: "Front yard", cameras: [{ id: "g", label: "G", entity_id: "camera.gate" }],
    actions: [{ id: "o", label: "O", kind: "lock_open", entity_id: "lock.gate", confirm: true }] }] }], "one block, titled from the label, nothing lost");
  assert.strictEqual(L.saveRequest(o).document.schema_version, 4);
  assert.strictEqual(L.titleFromLabel("x".repeat(60), "p").length, 40);
  assert.strictEqual(L.titleFromLabel("  \n ", "garden"), "garden");
  // withMirror itself is what the fixture claims
  assert.strictEqual(withMirror({ id: "p", label: "P", blocks: [{ id: "b", title: "B", cameras: [], actions: [{ id: "t", kind: "talk" }, { id: "l", kind: "lock_open" }] }] }).actions.length, 1, "the mirror leaves Talk out");
  console.log("PASS: every save and preview is schema 4 with no flat cameras or actions; a flat (older server) page becomes one block");
}

// 7. validation of blocks, and the home name
{
  const s = fresh();
  const bad = L.clone(s.draft);
  const page = bad.template.pages[0];
  page.blocks = [
    { id: "a", title: "Same", cameras: [], actions: [{ id: "x1", label: "X1", kind: "script_run", entity_id: "script.e", color: "orange", show_label: false }] },
    { id: "b", title: "same", cameras: [{ id: "c1", label: "C", entity_id: "camera.x", size: "huge" }], actions: [] },
    { id: "A", title: "", cameras: [], actions: [] },
    { id: "d", title: "t".repeat(41), door: "", cameras: [], actions: [] },
  ];
  const problems = L.validate(bad);
  for (const re of [/two blocks are called "same"/, /colour that is not in the palette/, /hides its text, so it needs an icon/, /size that is not one of auto, big, small/,
                    /block id is missing or used twice/, /title must be 1-40/, /door is not a Portico door/]) {
    assert.ok(problems.some((p) => re.test(p)), `${re}: ${problems.join(" | ")}`);
  }
  const nine = L.clone(s.draft);
  nine.template.pages[0].blocks = Array.from({ length: 9 }, (_, i) => ({ id: `b${i}`, title: `B${i}`, cameras: [], actions: [] }));
  assert.ok(L.validate(nine).some((p) => /at most 8 blocks/.test(p)));
  const lots = L.clone(s.draft);
  lots.template.pages[0].blocks = [0, 1, 2].map((b) => ({ id: `b${b}`, title: `B${b}`, cameras: mkCams(9).map((c) => ({ ...c, id: `b${b}${c.id}` })), actions: [] }));
  assert.ok(L.validate(lots).some((p) => /at most 24 cameras on a page/.test(p)));
  const dupIds = L.clone(s.draft);
  dupIds.template.pages[0].blocks.push({ id: "second", title: "Second", cameras: [{ id: "gate", label: "G2", entity_id: "camera.gate" }], actions: [] });
  assert.ok(L.validate(dupIds).some((p) => /the id gate is used twice/.test(p)), "ids are unique across the page's blocks");

  // the home name
  assert.strictEqual(L.setHomeName(s.draft, "  My home ").home_name, "My home");
  assert.ok(!("home_name" in L.setHomeName(s.draft, "   ")), "empty removes it");
  throwsEdit(() => L.setHomeName(s.draft, "h".repeat(41)), /longer than 40/);
  throwsEdit(() => L.setHomeName(s.draft, "a b"), /control characters/);
  throwsEdit(() => L.setHomeName(s.draft, "a\tb"), /control characters/);
  const hn = L.clone(s.draft);
  hn.home_name = "n".repeat(41);
  assert.ok(L.validate(hn).some((p) => /home name must be text of up to 40/.test(p)));
  console.log("PASS: block validation mirrors the server (titles, ids, limits, enums, text-less buttons) and so does the home name");
}

// 8. the split prompt: logic
{
  const s = fresh();
  const big = L.clone(s.draft);
  const hb = homeBlock(big);
  hb.door = "http://10.6.0.7:8091";
  hb.cameras = mkCams(15);
  hb.actions = [{ id: "open-door", label: "Open door", kind: "portico_open", confirm: true }, ...mkActs(13)];
  assert.deepStrictEqual(L.overflowBlocks(big).map((o) => [o.owner, o.pageId, o.blockId, o.cameras, o.actions]), [["template", "home", "home", 15, 14]]);
  assert.ok(L.validate(big).some((p) => /at most 12 cameras and 12 buttons in a block/.test(p)), "the server's refusal, said before it is sent");
  const split = L.splitBlock(big, "template", "home", "home");
  assert.strictEqual(split.blockId, "home-2");
  const [first, second] = L.pagesOf(split.doc, "template")[0].blocks;
  assert.deepStrictEqual([first.cameras.length, first.actions.length, second.cameras.length, second.actions.length], [12, 12, 3, 2]);
  assert.strictEqual(second.title, "Home 2");
  assert.deepStrictEqual(second.cameras.map((c) => c.id), ["c12", "c13", "c14"], "the overflow, in order");
  assert.strictEqual(second.door, "http://10.6.0.7:8091", "the new block has the same door");
  assert.deepStrictEqual(L.validate(split.doc), []);
  assert.deepStrictEqual(L.overflowBlocks(split.doc), []);
  assert.strictEqual(L.pageCameras(L.pagesOf(split.doc, "template")[0]).length, 15, "nothing lost");
  throwsEdit(() => L.splitBlock(split.doc, "template", "home", "home"), /within the limits/);
  // titles stay unique and the 8 block limit holds
  const clash = L.addBlock(big, "template", "home", "Home 2").doc;
  assert.strictEqual(L.pagesOf(L.splitBlock(clash, "template", "home", "home").doc, "template")[0].blocks.find((b) => b.id !== "home" && b.title === "Home 3").title, "Home 3");
  let eight = big;
  for (let i = 2; i <= 8; i++) eight = L.addBlock(eight, "template", "home", `Other ${i}`).doc;
  throwsEdit(() => L.splitBlock(eight, "template", "home", "home"), /At most 8 blocks/);
  // an override page or an orphan is found too
  const ov = L.clone(s.draft);
  ov.overrides.phone1.pages = [{ id: "own", label: "Own", blocks: [{ id: "own", title: "Own", cameras: mkCams(13), actions: [] }] }];
  assert.deepStrictEqual(L.overflowBlocks(ov).map((o) => o.owner), ["phone1"]);
  assert.strictEqual(L.pagesOf(L.splitBlock(ov, "phone1", "own", "own").doc, "phone1")[0].blocks.length, 2);
  console.log("PASS: a block over 12 is reported with the server's text and Split moves the overflow to a new block with the same door");
}

// 9. the Pages tab: blocks drawn, edited, saved
async function main() {
  {
    const f = editorHass({}, { loaded: blocksLoaded(), states: STATES2 });
    const panel = await mount(f);
    assert.deepStrictEqual(qa(panel, ".pagedetail .block").map((b) => b.dataset.block), ["gate", "house"]);
    assert.deepStrictEqual(labelsOf(panel, '[data-block="gate"] .row .label'), ["Gate", "Yard", "Open the door", "Talk"]);
    assert.deepStrictEqual(labelsOf(panel, '[data-block="house"] .row .label'), ["Lights"]);
    assert.strictEqual(q(panel, '[data-block="gate"] [data-field="block-title"]').value, "Gate");
    assert.strictEqual(q(panel, '[data-block="gate"] [data-field="block-door"]').value, "http://10.6.0.7:8091");
    assert.deepStrictEqual(qa(panel, '[data-block="gate"] [data-field="block-door"] option').map((o) => [o.value, o.textContent]), [["", "No door"], ["http://10.6.0.7:8091", "Gate"]]);
    assert.strictEqual(q(panel, '[data-block="house"] [data-field="block-door"]').value, "");
    assert.strictEqual(q(panel, '[data-tile="yard"] [data-field="camera-size"]').value, "small");
    assert.strictEqual(q(panel, '[data-tile="gate"] [data-field="camera-size"]').value, "auto");
    assert.ok(q(panel, '[data-tile="open-door"] .sub').textContent.includes("colour green"), "the colour is said in words in the row");
    assert.ok(q(panel, '[data-tile="lights"] .sub').textContent.includes("text hidden"));
    assert.strictEqual(q(panel, '[data-status]').dataset.status, "saved", "the stale flat mirror in the document is not a change");
    // accessibility: every control of a block has a name
    for (const node of qa(panel, ".block select, .block input")) assert.ok(node.getAttribute("aria-label"), `${node.dataset.field} has an accessible name`);
    for (const node of qa(panel, ".block button")) assert.ok(node.textContent.trim() || node.getAttribute("aria-label"), "buttons have text or a label");
    // up / down: the first block cannot go up, the last cannot go down
    assert.strictEqual(q(panel, '[data-block="gate"] [data-act="block-up"]').disabled, true);
    assert.strictEqual(q(panel, '[data-block="house"] [data-act="block-down"]').disabled, true);
    click(q(panel, '[data-block="gate"] [data-act="block-down"]'));
    assert.deepStrictEqual(qa(panel, ".pagedetail .block").map((b) => b.dataset.block), ["house", "gate"]);
    click(act(panel, "undo"));
    assert.deepStrictEqual(qa(panel, ".pagedetail .block").map((b) => b.dataset.block), ["gate", "house"]);
    console.log("PASS: the Pages tab shows a page's blocks in order with door, sizes, colours; the stale flat mirror is not a change");
    panel.remove();
  }

  // add / rename / door / delete / move a tile, then Save: the exact document
  {
    const f = editorHass({}, { loaded: blocksLoaded(), states: STATES2 });
    const panel = await mount(f);
    setValue(q(panel, '[data-field="new-block"]'), "Garden");
    click(act(panel, "add-block"));
    assert.deepStrictEqual(qa(panel, ".pagedetail .block").map((b) => b.dataset.block), ["gate", "house", "garden"]);
    click(act(panel, "add-block"));         // empty: the fallback title
    assert.strictEqual(qa(panel, '.block [data-field="block-title"]')[3].value, "Block 4");
    setValue(q(panel, '[data-field="new-block"]'), "garden");
    click(act(panel, "add-block"));
    assert.ok(text(panel).includes('already called "garden"'), "a clash is said, nothing added");
    assert.strictEqual(qa(panel, ".pagedetail .block").length, 4);
    setValue(q(panel, '[data-block="garden"] [data-field="block-title"]'), "Back yard", "change");
    setValue(q(panel, '[data-block="garden"] [data-field="block-door"]'), "http://10.6.0.7:8091", "change");
    click(q(panel, '[data-block="block-4"] [data-act="delete-block"]'));
    assert.deepStrictEqual(qa(panel, ".pagedetail .block").map((b) => b.dataset.block), ["gate", "house", "garden"]);
    setValue(q(panel, '[data-tile="yard"] [data-field="camera-size"]'), "big", "change");
    setValue(q(panel, '[data-tile="lights"] [data-field="move-to-block"]'), "garden", "change");
    assert.deepStrictEqual(labelsOf(panel, '[data-block="garden"] .row .label'), ["Lights"], "the button moved, same id");
    assert.deepStrictEqual(qa(panel, '[data-block="gate"] [data-tile="yard"] [data-field="move-to-block"] option').map((o) => o.value), ["", "house", "garden"]);
    click(act(panel, "save"));
    await sleep(40);
    const saves = f.ofType("portico/content/save");
    assert.strictEqual(saves.length, 1);
    assert.strictEqual(saves[0].base_doc_rev, "rev1");
    assert.strictEqual(saves[0].document.schema_version, 4);
    const pg = saves[0].document.template.pages[0];
    assert.deepStrictEqual(Object.keys(pg).sort(), ["blocks", "id", "label"], "a page goes out as {id, label, blocks} only");
    assert.deepStrictEqual(pg.blocks.map((b) => [b.id, b.title, b.door || null]), [["gate", "Gate", "http://10.6.0.7:8091"], ["house", "House", null], ["garden", "Back yard", "http://10.6.0.7:8091"]]);
    assert.deepStrictEqual(pg.blocks[0].cameras.map((c) => [c.id, c.size]), [["gate", undefined], ["yard", "big"]]);
    assert.deepStrictEqual(pg.blocks[2].actions.map((a) => a.id), ["lights"]);
    assert.deepStrictEqual(pg.blocks[2].actions[0], { id: "lights", label: "Lights", kind: "script_run", entity_id: "script.evening", icon: "lightbulb", color: "amber", show_label: false, confirm: false });
    for (const holder of [saves[0].document.template, ...Object.values(saves[0].document.overrides)]) {
      for (const p of holder.pages) assert.ok(!("cameras" in p) && !("actions" in p), `page ${p.id}: no flat list on save`);
    }
    assert.strictEqual(f.ofType("portico/content/document").length, 2, "reloaded after the save");
    console.log("PASS: add, rename, door, delete, size and move-to-block save as one schema 4 document with no flat lists");
    panel.remove();
  }

  // the add / edit form: kinds (Talk offered), picker for the new kinds, icon search, colour, text, confirm
  {
    const f = editorHass({}, { loaded: blocksLoaded(), states: STATES2 });
    const panel = await mount(f);
    click(q(panel, '[data-block="house"] [data-act="add-button"]'));
    const form = '[data-block="house"] [data-dialog="actions"]';
    assert.ok(q(panel, form), "the form opens inside the block it was opened from");
    assert.ok(!q(panel, '[data-block="gate"] [data-dialog]'));
    const talk = q(panel, `${form} [data-field="kind"] option[value="talk"]`);
    assert.strictEqual(talk.disabled, false, "Talk can be chosen");
    assert.ok(talk.textContent.includes("greyed on the phone until the gate allows it"));
    setValue(q(panel, `${form} [data-field="kind"]`), "cover_open", "change");
    assert.strictEqual(q(panel, `${form} [data-field="confirm"]`).checked, true, "a gate asks first by default");
    assert.deepStrictEqual(qa(panel, `${form} [data-field="target"] option`).map((o) => o.value).filter(Boolean), ["cover.garage", "cover.gate"].sort((a, b) =>
      STATES2[a].attributes.friendly_name.localeCompare(STATES2[b].attributes.friendly_name)), "only cover.* entities");
    setValue(q(panel, `${form} [data-field="target"]`), "cover.gate", "change");
    assert.strictEqual(q(panel, `${form} [data-field="label"]`).value, "Gate cover");
    setValue(q(panel, `${form} [data-field="label"]`), "Open gate");
    // hidden text without an icon is refused with a reason
    setChecked(q(panel, `${form} [data-field="show-label"]`), false);
    click(act(panel, "dialog-ok"));
    assert.ok(q(panel, form).textContent.includes("Pick an icon from the list before hiding the text"), "the form says why");
    // the icon picker: search, a group, a click, the name
    setValue(q(panel, `${form} [data-field="icon-search"]`), "garage-");
    const shownIcons = () => qa(panel, `${form} [data-icon]`).filter((b) => !b.hidden).map((b) => b.dataset.icon);
    assert.deepStrictEqual(shownIcons(), L.ICON_NAMES.filter((n) => n.includes("garage-")), "search narrows the grid by name");
    assert.ok(shownIcons().includes("garage-variant") && shownIcons().includes("garage-lock"));
    setValue(q(panel, `${form} [data-field="icon-search"]`), "zzzz");
    assert.deepStrictEqual(shownIcons(), []);
    assert.ok(text(panel).includes("No icon matches."));
    setValue(q(panel, `${form} [data-field="icon-search"]`), "");
    assert.strictEqual(shownIcons().length, 119, "all 118 icons and the default");
    assert.deepStrictEqual(qa(panel, `${form} .icongroup h5`).map((h) => h.textContent), ["Door", "Gate", "Garage", "Lock", "Talk", "Other"]);
    setValue(q(panel, `${form} [data-field="icon-search"]`), "garage-open");
    click(q(panel, `${form} [data-icon="garage-open-variant"]`));
    assert.strictEqual(q(panel, `${form} [data-icon="garage-open-variant"]`).getAttribute("aria-checked"), "true");
    assert.strictEqual(q(panel, `${form} [data-icon="garage-open-variant"]`).title, "garage-open-variant", "the name is the tooltip");
    assert.ok(q(panel, `${form} [data-field="icon-name"]`).textContent.includes("garage-open-variant"));
    // the colour picker: Default and eight names, the name is text, a tick marks the choice
    const swatches = qa(panel, `${form} [data-color]`);
    assert.deepStrictEqual(swatches.map((b) => b.dataset.color), ["default", "blue", "green", "amber", "red", "purple", "teal", "pink", "grey"]);
    assert.deepStrictEqual(swatches.map((b) => b.textContent.replace("✓ ", "")), ["Default", "blue", "green", "amber", "red", "purple", "teal", "pink", "grey"], "each swatch says its name");
    assert.strictEqual(swatches[0].getAttribute("aria-checked"), "true");
    for (const sw of swatches.slice(1)) {
      const pal = L.PALETTE.find((c) => c.name === sw.dataset.color);
      assert.strictEqual(sw.style.background, rgb(pal.light.fill), `${sw.dataset.color}: the palette's light fill`);
      assert.strictEqual(sw.style.color, rgb(pal.light.text));
    }
    click(swatches[4]);
    assert.strictEqual(q(panel, `${form} [data-color="red"]`).getAttribute("aria-checked"), "true");
    assert.ok(q(panel, `${form} [data-color="red"]`).textContent.startsWith("✓"), "a tick, not only a colour");
    assert.strictEqual(q(panel, `${form} [data-color="default"]`).getAttribute("aria-checked"), "false");
    click(act(panel, "dialog-ok"));
    assert.ok(!q(panel, form), "the form closes");
    click(act(panel, "save"));
    await sleep(40);
    const added = f.ofType("portico/content/save")[0].document.template.pages[0].blocks[1].actions[1];
    assert.deepStrictEqual(added, { id: "open-gate", label: "Open gate", kind: "cover_open", entity_id: "cover.gate", icon: "garage-open-variant", color: "red", show_label: false, confirm: true });
    console.log("PASS: the button form: new kinds, Talk offered, icon search and groups, palette swatches, hidden text, confirm");
    panel.remove();
  }

  // the Talk form: in a block without a door it is refused with the reason; in a block with one it adds a Talk button
  {
    const f = editorHass({}, { loaded: blocksLoaded(), states: STATES2 });
    const panel = await mount(f);
    click(q(panel, '[data-block="house"] [data-act="add-button"]'));
    let form = '[data-block="house"] [data-dialog="actions"]';
    setValue(q(panel, `${form} [data-field="kind"]`), "talk", "change");
    assert.ok(q(panel, form).textContent.includes("This block has no door yet"), "the form says why");
    assert.ok(!q(panel, `${form} [data-field="target"]`), "Talk has no door picker of its own");
    assert.ok(!q(panel, `${form} [data-field="confirm"]`), "no confirm is offered");
    setValue(q(panel, `${form} [data-field="label"]`), "Talk");
    click(act(panel, "dialog-ok"));
    assert.ok(q(panel, form).textContent.includes("Pick the block's door first"), "refused in a block without a door");
    click(act(panel, "dialog-cancel"));
    click(q(panel, '[data-block="gate"] [data-act="add-button"]'));
    form = '[data-block="gate"] [data-dialog="actions"]';
    setValue(q(panel, `${form} [data-field="kind"]`), "talk", "change");
    assert.ok(q(panel, form).textContent.includes("Talks to this block's door"));
    assert.ok(!q(panel, `${form} [data-field="confirm"]`), "no confirm is offered");
    setValue(q(panel, `${form} [data-field="label"]`), "Talk to gate");
    click(q(panel, `${form} [data-icon="account-voice"]`));
    click(act(panel, "dialog-ok"));
    assert.ok(!q(panel, form), "the form closes");
    assert.ok(q(panel, '[data-tile="talk-to-gate"] [data-talk-reason]').textContent.includes("Talk is off: Off until the gate allows it"), "the editor row shows the reason chip");
    click(act(panel, "save"));
    await sleep(40);
    const gateActions = f.ofType("portico/content/save")[0].document.template.pages[0].blocks[0].actions;
    assert.deepStrictEqual(gateActions.at(-1), { id: "talk-to-gate", label: "Talk to gate", kind: "talk", icon: "account-voice", confirm: false });
    console.log("PASS: a Talk button is created in a block with a door and saved; refused without one; no confirm offered");
    panel.remove();
  }

  // keyboard: the icon grid and the swatches are radio groups with one tab stop and arrow keys
  {
    const f = editorHass({}, { loaded: blocksLoaded(), states: STATES2 });
    const panel = await mount(f);
    click(q(panel, '[data-block="house"] [data-tile="lights"] [data-act="edit-tile"]'));
    const form = '[data-block="house"] [data-dialog="actions"]';
    const icons = q(panel, `${form} [data-field="icon"]`);
    assert.strictEqual(icons.getAttribute("role"), "radiogroup");
    assert.strictEqual(q(panel, `${form} [data-icon="lightbulb"]`).getAttribute("aria-checked"), "true", "the stored icon is chosen");
    assert.deepStrictEqual(qa(panel, `${form} [data-icon]`).filter((b) => b.tabIndex === 0).map((b) => b.dataset.icon), ["lightbulb"], "one tab stop: the chosen icon");
    click(q(panel, `${form} [data-icon="door"]`));
    key(q(panel, `${form} [data-icon="door"]`), "ArrowRight");
    assert.strictEqual(q(panel, `${form} [data-icon="door-open"]`).getAttribute("aria-checked"), "true", "ArrowRight chooses the next icon");
    key(q(panel, `${form} [data-icon="door-open"]`), "ArrowLeft");
    assert.strictEqual(q(panel, `${form} [data-icon="door"]`).getAttribute("aria-checked"), "true");
    key(q(panel, `${form} [data-icon="door"]`), "End");
    assert.strictEqual(q(panel, `${form} [data-icon="doorbell-video"]`).getAttribute("aria-checked"), "true", "End: the last of the 'other' group");
    key(q(panel, `${form} [data-icon="doorbell-video"]`), "Home");
    assert.strictEqual(q(panel, `${form} [data-icon="none"]`).getAttribute("aria-checked"), "true", "Home: the default");
    // the swatches
    const colors = q(panel, `${form} [data-field="color"]`);
    assert.strictEqual(colors.getAttribute("role"), "radiogroup");
    assert.strictEqual(q(panel, `${form} [data-color="amber"]`).getAttribute("aria-checked"), "true", "the stored colour");
    assert.deepStrictEqual(qa(panel, `${form} [data-color]`).filter((b) => b.tabIndex === 0).map((b) => b.dataset.color), ["amber"]);
    key(q(panel, `${form} [data-color="amber"]`), "ArrowRight");
    assert.strictEqual(q(panel, `${form} [data-color="red"]`).getAttribute("aria-checked"), "true");
    key(q(panel, `${form} [data-color="red"]`), "ArrowUp");
    key(q(panel, `${form} [data-color="amber"]`), "ArrowUp");
    key(q(panel, `${form} [data-color="green"]`), "ArrowUp");
    key(q(panel, `${form} [data-color="blue"]`), "ArrowUp");
    assert.strictEqual(q(panel, `${form} [data-color="default"]`).getAttribute("aria-checked"), "true");
    key(q(panel, `${form} [data-color="default"]`), "ArrowLeft");
    assert.strictEqual(q(panel, `${form} [data-color="grey"]`).getAttribute("aria-checked"), "true", "it wraps round");
    // the text-less button keeps its text shown/hidden state in the form
    assert.strictEqual(q(panel, `${form} [data-field="show-label"]`).checked, false);
    setChecked(q(panel, `${form} [data-field="show-label"]`), true);
    click(act(panel, "dialog-ok"));
    const lights = L.pageActions(L.pagesOf(panel._state.draft, "template")[0]).find((a) => a.id === "lights");
    assert.deepStrictEqual([lights.icon, lights.color, "show_label" in lights], [undefined, "grey", false], "none removes the icon, the colour changed, shown is not stored");
    console.log("PASS: icon and colour pickers are keyboard operable radio groups with one tab stop; an edit saves what they show");
    panel.remove();
  }

  // the preview draws blocks, sizes, colours, hidden text, Talk with its reason; dark theme uses the dark values
  for (const dark of [false, true]) {
    const f = editorHass({}, { loaded: blocksLoaded(), states: STATES2 });
    if (dark) f.hass.themes = { darkMode: true };
    const panel = await mount(f);
    await sleep(60);
    const prev = (sel) => qa(panel, `[data-preview] ${sel}`);
    assert.deepStrictEqual(prev(".blockbox .bhead").map((n) => n.textContent), ["Gate", "House"]);
    const cams = prev('[data-block="gate"] .cam');
    assert.deepStrictEqual(cams.map((c) => c.dataset.size), ["big", "small"], "first camera full width, the stored small stays small");
    assert.ok(cams[0].classList.contains("big") && cams[1].classList.contains("small"));
    assert.ok(cams[0].textContent.includes("big"), "the size is said in words as well");
    const tile = (id) => prev(".btn-tile").find((t) => t.textContent.includes(id));
    const open = tile("Open the door");
    const key2 = dark ? "dark" : "light";
    const green = L.PALETTE.find((c) => c.name === "green")[key2];
    assert.strictEqual(open.style.background, rgb(green.fill), `${key2}: the green fill`);
    assert.strictEqual(open.style.color, rgb(green.text));
    assert.ok(open.textContent.includes("requires confirmation"), "confirm is shown in words");
    const lights = prev('[data-block="house"] .btn-tile')[0];
    const amber = L.PALETTE.find((c) => c.name === "amber")[key2];
    assert.strictEqual(lights.style.background, rgb(amber.fill));
    assert.strictEqual(lights.dataset.textHidden, "true");
    assert.ok(!lights.querySelector(".label"), "no visible text");
    assert.strictEqual(lights.querySelector(".sr").textContent, "Lights", "the name stays for screen readers");
    assert.strictEqual(lights.title, "Lights");
    const talk = prev('[data-block="gate"] .btn-tile').find((t) => t.textContent.includes("Talk"));
    assert.ok(talk.classList.contains("off"), "Talk is off");
    assert.ok(talk.textContent.includes("Off until the gate allows it"), "with the server's reason talk_off in words");
    assert.ok(!talk.style.background, "an off button is grey whatever its colour");
    assert.ok(q(panel, '[data-tile="talk"] [data-talk-reason]').textContent.includes("Off until the gate allows it"), "the editor row says it too");
    console.log(`PASS: the preview draws blocks, big/small cameras, ${key2} palette colours, hidden text, asks-first and the Talk reason`);
    panel.remove();
  }

  // a reply whose cameras carry no size: the first is big, the rest small (the auto rule), as the phone does
  {
    const f = editorHass({
      "portico/content/preview": (msg) => ({ endpoint: msg.endpoint, kind: "phone", rev: "r", callers: {}, roster: [], pages: [{
        id: "home", label: "Home", cameras: [], actions: [],
        blocks: [{ id: "b", title: "B", cameras: [1, 2, 3].map((n) => ({ id: `c${n}`, label: `C${n}`, entity_id: "camera.gate", available: true })), actions: [] }] }] }),
    }, { loaded: blocksLoaded(), states: STATES2 });
    const panel = await mount(f);
    await sleep(60);
    assert.deepStrictEqual(qa(panel, "[data-preview] .cam").map((c) => c.dataset.size), ["big", "small", "small"]);
    panel.remove();
    console.log("PASS: a camera without a size is drawn by the auto rule: the first big, the rest small");
  }

  // the preview carries the draft's blocks and schema 4, no flat lists
  {
    const f = editorHass({}, { loaded: blocksLoaded(), states: STATES2 });
    const panel = await mount(f);
    await sleep(60);
    const pre = f.ofType("portico/content/preview").at(-1);
    assert.strictEqual(pre.document.schema_version, 4);
    assert.ok(!("cameras" in pre.document.template.pages[0]) && !("actions" in pre.document.template.pages[0]));
    assert.deepStrictEqual(pre.document.template.pages[0].blocks.map((b) => b.id), ["gate", "house"]);
    setValue(q(panel, '[data-tile="gate"] [data-field="camera-size"]'), "small", "change");
    await sleep(60);
    assert.deepStrictEqual(qa(panel, '[data-preview] [data-block="gate"] .cam').map((c) => c.dataset.size), ["small", "small"], "the preview follows the edit");
    console.log("PASS: the live preview sends the draft's blocks (schema 4, no flat lists) and follows an edit");
    panel.remove();
  }

  // an empty camera name: the owner can clear it, the row names the camera muted, the preview draws no name line, and the
  // save sends label "" (never the entity's name)
  {
    const f = editorHass({
      "portico/content/preview": (msg) => ({ endpoint: msg.endpoint, kind: "phone", rev: "r", callers: {}, roster: [], pages: [{
        id: "home", label: "Home",
        blocks: msg.document.template.pages[0].blocks.map((b) => ({ ...b, cameras: b.cameras.map((c) => ({ ...c, available: true, name: "x" })), actions: [] })) }] }),
    }, { loaded: blocksLoaded(), states: STATES2 });
    const panel = await mount(f);
    await sleep(60);
    assert.ok(qa(panel, "[data-preview] .cam .label").length === 2, "named cameras draw their name");
    click(q(panel, '[data-block="gate"] [data-tile="gate"] [data-act="edit-tile"]'));
    const field = q(panel, '[data-block="gate"] [data-dialog="cameras"] [data-field="label"]');
    assert.strictEqual(field.placeholder, "Leave empty to show no name");
    setValue(field, "");
    click(act(panel, "dialog-ok"));
    assert.strictEqual(L.pageCameras(L.pagesOf(panel._state.draft, "template")[0]).find((c) => c.id === "gate").label, "", "stored empty");
    assert.ok(!q(panel, '[data-state="problems"]'), "no problem is raised for an empty camera name");
    const row = q(panel, '[data-block="gate"] [data-tile="gate"] .label');
    assert.ok(row.classList.contains("muted") && row.textContent === "Gate camera", "the row shows the entity's own name, muted: " + row.textContent);
    await sleep(60);
    const cams = qa(panel, '[data-preview] [data-block="gate"] .cam');
    assert.strictEqual(cams[0].querySelector(".label"), null, "no name line for the unnamed camera");
    assert.ok(cams[1].querySelector(".label"), "the other camera keeps its name");
    assert.strictEqual(cams[0].querySelector("img") ? cams[0].querySelector("img").alt : "Camera", "Camera");
    const pre = f.ofType("portico/content/preview").at(-1);
    assert.strictEqual(pre.document.template.pages[0].blocks[0].cameras[0].label, "", "the draft sent holds the empty label");
    assert.ok(act(panel, "save") && !act(panel, "save").disabled, "an empty camera name can be saved");
    click(act(panel, "save"));
    await sleep(40);
    const saved = f.ofType("portico/content/save").at(-1);
    assert.strictEqual(saved.document.template.pages[0].blocks[0].cameras[0].label, "", "saved as the empty string");
    console.log("PASS: a camera name can be cleared; the row shows the entity name muted, the preview draws no name, the save sends \"\"");
    panel.remove();
  }

  // a rule that names a button of a deleted block blocks the save, like a deleted button
  {
    const loaded = blocksLoaded();
    loaded.document.template.rules = [{ id: "doors", caller: "kind:door", cameras: null, ring_buttons: ["lights"], call_buttons: [] }];
    const f = editorHass({}, { loaded, states: STATES2 });
    const panel = await mount(f);
    click(q(panel, '[data-block="house"] [data-act="delete-block"]'));
    const box = q(panel, '[data-state="problems"]');
    assert.ok(box && box.textContent.includes('rule "doors" still uses the button "lights"'), box && box.textContent);
    assert.strictEqual(act(panel, "save").disabled, true);
    click(act(panel, "undo"));
    assert.ok(!q(panel, '[data-state="problems"]'));
    console.log("PASS: deleting a block whose button a rule names blocks the save and says which rule");
    panel.remove();
  }

  // the split prompt in the panel
  {
    const loaded = blocksLoaded();
    const pg = loaded.document.template.pages[0];
    pg.blocks[0].cameras = mkCams(15);
    pg.blocks[0].actions = [{ id: "open-door", label: "Open", kind: "portico_open", confirm: true }, ...mkActs(13)];
    const f = editorHass({}, { loaded, states: STATES2 });
    const panel = await mount(f);
    const prompt = q(panel, '[data-state="problems"] [data-split="gate"]');
    assert.ok(prompt, "a block over the limit gets a prompt");
    assert.ok(prompt.textContent.includes("15 cameras and 14 buttons"), prompt.textContent);
    assert.ok(q(panel, '[data-state="problems"]').textContent.includes("at most 12 cameras and 12 buttons in a block"));
    assert.strictEqual(act(panel, "save").disabled, true, "Save waits for the split");
    assert.ok(q(panel, '[data-block="gate"] [data-state="block-over-limit"]'), "and the block says so itself");
    click(act(panel, "split-block"));
    assert.ok(!q(panel, '[data-state="problems"]'), "split: no problem left");
    assert.deepStrictEqual(qa(panel, ".pagedetail .block").map((b) => b.dataset.block), ["gate", "gate-2", "house"]);
    assert.deepStrictEqual(labelsOf(panel, '[data-block="gate-2"] .row .label'), ["Cam 12", "Cam 13", "Cam 14", "Act 11", "Act 12"]);
    assert.strictEqual(act(panel, "save").disabled, false);
    click(act(panel, "save"));
    await sleep(40);
    const sent = f.ofType("portico/content/save")[0].document.template.pages[0].blocks;
    assert.deepStrictEqual(sent.map((b) => [b.id, b.cameras.length, b.actions.length, b.door || null]),
      [["gate", 12, 12, "http://10.6.0.7:8091"], ["gate-2", 3, 2, "http://10.6.0.7:8091"], ["house", 0, 1, null]]);
    console.log("PASS: a block over 12 shows the Split prompt, blocks Save, and one click moves the overflow to a new block");
    panel.remove();

    const loaded2 = blocksLoaded();
    loaded2.document.template.pages[0].blocks[1].cameras = mkCams(13);
    const g = editorHass({}, { loaded: loaded2, states: STATES2 });
    const p2 = await mount(g);
    click(q(p2, '[data-block="house"] [data-act="split-block-here"]'));
    assert.deepStrictEqual(qa(p2, ".pagedetail .block").map((b) => b.dataset.block), ["gate", "house", "house-2"], "the prompt inside the block works too");
    p2.remove();
  }

  // the home name: no field on the Pages tab any more; a stored value is kept and saved untouched
  {
    const f = editorHass({}, { loaded: blocksLoaded(), states: STATES2 });
    const panel = await mount(f);
    await sleep(40);
    assert.ok(!q(panel, '[data-field="home-name"]'), "no Home name field: the page's own name is enough");
    assert.strictEqual(panel._state.draft.home_name, "My Home", "a stored home_name stays in the draft");
    click(q(panel, '[data-sel="ep:phone1"]'));
    assert.ok(!q(panel, '[data-field="home-name"]'));
    click(q(panel, '[data-sel="template"]'));
    setValue(q(panel, '[data-field="page-label"]'), "Renamed", "change");
    click(act(panel, "save"));
    await sleep(40);
    assert.strictEqual(f.ofType("portico/content/save")[0].document.home_name, "My Home", "a save still carries it");
    console.log("PASS: no home name field; a stored home_name is kept and saved untouched");
    panel.remove();
  }

  // conflict and the schema error
  {
    let docCalls = 0;
    const second = blocksLoaded();
    second.document.template.pages[0].blocks.push({ id: "newer", title: "Added elsewhere", cameras: [], actions: [] });
    second.doc_rev = "rev9";
    const f = editorHass({
      "portico/content/save": () => { throw { code: "conflict", message: "the document changed since it was loaded" }; },
      "portico/content/document": () => { docCalls++; return JSON.parse(JSON.stringify(docCalls === 1 ? blocksLoaded() : second)); },
    }, { states: STATES2 });
    const panel = await mount(f);
    setValue(q(panel, '[data-block="gate"] [data-field="block-title"]'), "Entrance", "change");
    click(act(panel, "save"));
    await sleep(60);
    assert.strictEqual(f.ofType("portico/content/save")[0].base_doc_rev, "rev1");
    assert.ok(q(panel, '[data-state="message"]').textContent.includes("Another session saved first"));
    assert.deepStrictEqual(qa(panel, ".pagedetail .block").map((b) => b.dataset.block), ["gate", "house", "newer"], "the saved version, with the block added elsewhere");
    assert.strictEqual(q(panel, '[data-block="gate"] [data-field="block-title"]').value, "Gate", "the unsaved rename is gone");
    assert.strictEqual(q(panel, '[data-status]').dataset.status, "saved");
    // and the next save carries the new revision
    f.hass.callWS = ((orig) => async (msg) => (msg.type === "portico/content/save" ? (f.sent.push(JSON.parse(JSON.stringify(msg))), { doc_rev: "rev10" }) : orig(msg)))(f.hass.callWS);
    setValue(q(panel, '[data-block="newer"] [data-field="block-title"]'), "Garden", "change");
    click(act(panel, "save"));
    await sleep(60);
    assert.strictEqual(f.ofType("portico/content/save").at(-1).base_doc_rev, "rev9");
    panel.remove();
    console.log("PASS: a conflict reloads the saved blocks, says so, and the next save uses the new base_doc_rev");
  }
  {
    const f = editorHass({ "portico/content/save": () => { throw { code: "invalid", message: "document: schema_version must be 4; reload the Portico page" }; } },
      { loaded: blocksLoaded(), states: STATES2 });
    const panel = await mount(f);
    setValue(q(panel, '[data-block="gate"] [data-field="block-title"]'), "Entrance", "change");
    click(act(panel, "save"));
    await sleep(40);
    const msg = q(panel, '[data-state="message"]');
    assert.ok(msg.textContent.includes("document: schema_version must be 4; reload the Portico page"), msg.textContent);
    assert.ok(q(panel, '[data-block="gate"] [data-field="block-title"]').value === "Entrance", "the draft stays until the user reloads");
    click(act(panel, "reload"));
    await sleep(40);
    assert.strictEqual(f.ofType("portico/content/document").length, 2, "Reload asks for the document again");
    assert.strictEqual(q(panel, '[data-block="gate"] [data-field="block-title"]').value, "Gate");
    assert.ok(!q(panel, '[data-state="message"]'));
    panel.remove();
    console.log("PASS: the schema_version error is shown verbatim with a Reload button");
  }

  // a flat page from an older server: one block named after the page; and hostile text stays text
  {
    const old = makeLoaded();
    old.document.schema_version = 3;
    const evil = `<img src=x onerror="window.__pwn=1">`;
    old.document.template.pages[0].label = evil;
    delete old.document.template.pages[0].blocks;          // flat only: what an older server serves
    const f = editorHass({}, { loaded: old, states: STATES2 });
    const panel = await mount(f);
    assert.deepStrictEqual(qa(panel, ".pagedetail .block").map((b) => b.dataset.block), ["home"]);
    assert.strictEqual(q(panel, '[data-block="home"] [data-field="block-title"]').value, evil, "named after the page");
    assert.ok(!q(panel, "img[src=x]") && win.__pwn === undefined);
    setValue(q(panel, '[data-field="new-block"]'), "<b id=pwn>x</b>");
    click(act(panel, "add-block"));
    assert.ok(!q(panel, "#pwn") && !q(panel, "b"), "a typed block title is text");
    assert.strictEqual(q(panel, '[data-status]').dataset.status, "dirty");
    click(act(panel, "save"));
    await sleep(40);
    const sent = f.ofType("portico/content/save")[0].document;
    assert.strictEqual(sent.schema_version, 4);
    assert.ok(!("cameras" in sent.template.pages[0]) && sent.template.pages[0].blocks.length === 2);
    console.log("PASS: a flat page (older server) shows as one block named after the page; titles are text, never markup");
    panel.remove();
  }

  // Not attached counts the buttons of its blocks
  {
    const f = editorHass({}, { loaded: blocksLoaded(), states: STATES2 });
    const panel = await mount(f);
    click(act(panel, "tab-pages"));
    click(q(panel, '[data-sel="orphan:old-phone-user-id-123"]'));
    assert.ok(text(panel).includes("Home · 0 cameras, 1 button"));
    assert.ok(text(panel).includes("Button: Garage (Button)"));
    panel.remove();
    console.log("PASS: Not attached lists the buttons of its blocks");
  }

  console.log("ALL_PANEL_BLOCKS_TESTS_PASSED");
}

main().catch((err) => { console.error(err); process.exit(1); });
