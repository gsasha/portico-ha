// DEV-760 P2: camera references in the panel. The pure edit functions (add / detach / re-point, the schema number, the
// validation that no longer calls a reference "not a camera"), then the panel in jsdom with a fake `hass` that answers
// portico/elements/list: the camera dialog's "From Elements / Define here" switch and its live preview, the saved
// `{element: ...}` item, the badge, Detach, the "Element removed" tile, label/size/live overrides, the rule and screen
// camera pickers, and a grep that no HTML-string setter appears. Protocol-level; NOT a real browser.
import assert from "assert";
import fs from "fs";
import { loadPanel, editorHass, makeLoaded, sleep } from "./panel_loader.mjs";

const { dom, Panel, doc } = loadPanel();
const L = Panel.logic;
const win = dom.window;
const throwsEdit = (fn, re) => assert.throws(fn, (e) => e instanceof L.EditError && re.test(e.message), String(re));

const GATE = { id: "el-00000001", kind: "camera", name: "Front gate", entity_id: "camera.gate", options: { size: "big" } };
const GARAGE = { id: "el-00000002", kind: "camera", name: "Garage", entity_id: "camera.garage", options: {} };
const STREAM = { id: "el-00000003", kind: "camera", name: "Stream only", options: { stream: "rtsp://x/y" } };
const BUTTON = { id: "el-00000004", kind: "button", name: "Open gate", entity_id: "lock.gate", options: { action: "lock_unlock" } };
const ELS = [GATE, GARAGE, STREAM, BUTTON];
const listReply = (elements) => ({ schema_version: 1, rev: "r1", elements, used_by: {}, kinds: { camera: { label: "Camera" }, button: { label: "Button" } }, icons: [], palette: [], limits: {} });

const q = (p, sel) => p.shadowRoot.querySelector(sel);
const qa = (p, sel) => Array.from(p.shadowRoot.querySelectorAll(sel));
const act = (p, name) => q(p, `[data-act="${name}"]`);
const click = (node) => { assert.ok(node, "clicked something that is not there"); node.dispatchEvent(new win.Event("click", { bubbles: true })); };
function setValue(node, value, type = "input") {
  assert.ok(node, "set a value on something that is not there");
  node.value = value;
  node.dispatchEvent(new win.Event(type, { bubbles: true }));
}
const FORM = '[data-dialog="cameras"]';
const pageOf = (d) => d.template.pages[0];
const camsOf = (d) => L.pageCameras(pageOf(d));

async function mount(opts = {}) {
  const loaded = opts.loaded || makeLoaded();
  const f = editorHass({ "portico/elements/list": () => listReply(opts.elements || ELS), ...(opts.handlers || {}) }, { loaded });
  const panel = doc.createElement("portico-panel");
  panel._previewMs = 10;
  panel.hass = f.hass;
  doc.body.appendChild(panel);
  await sleep(60);
  return { f, panel };
}
const withRef = (extra = {}) => {
  const loaded = makeLoaded();
  loaded.document.template.pages[0].blocks[0].cameras.push({ id: "front-gate", element: GATE.id, ...extra });
  return loaded;
};

// ---- 1. pure: placing, the label rule, refusals ----------------------------------------------------------------------
{
  const d = makeLoaded().document;
  const r = L.addCameraRef(d, "template", "home", GATE, { label: "Front gate", size: "auto", live: false });
  assert.deepStrictEqual(camsOf(r).at(-1), { id: "front-gate", element: GATE.id }, "the name equal to the element's, no size, no live: nothing but the reference");
  assert.ok(!("entity_id" in camsOf(r).at(-1)));
  const o = L.addCameraRef(d, "template", "home", GATE, { label: "Gate (street)", size: "small", live: true });
  assert.deepStrictEqual(camsOf(o).at(-1), { id: "front-gate", element: GATE.id, label: "Gate (street)", size: "small", live: true });
  const none = L.addCameraRef(d, "template", "home", GATE, { label: "" });
  assert.deepStrictEqual(camsOf(none).at(-1), { id: "front-gate", element: GATE.id, label: "" }, "an empty name is kept: no name on the tile");
  const twice = L.addCameraRef(r, "template", "home", GATE, {});
  assert.deepStrictEqual(camsOf(twice).map((c) => c.id), ["gate", "front-gate", "front-gate-2"], "the same element twice gets two ids");
  throwsEdit(() => L.addCameraRef(d, "template", "home", STREAM, {}), /stream only/);
  throwsEdit(() => L.addCameraRef(d, "template", "home", BUTTON, {}), /stream only|Pick an element/);
  throwsEdit(() => L.addCameraRef(d, "template", "home", null, {}), /Pick an element/);
  throwsEdit(() => L.addCameraRef(d, "template", "home", { ...GATE, id: "gate" }, {}), /Pick an element/);
  assert.ok(L.placeableCamera(GATE) && !L.placeableCamera(STREAM) && !L.placeableCamera(BUTTON) && !L.placeableCamera(null));
  assert.ok(L.isRef({ element: "el-1" }) && !L.isRef({ entity_id: "camera.a" }) && !L.isRef(null));
  console.log("PASS: addCameraRef stores only the reference and the overrides; stream-only and non-camera elements are refused");
}

// ---- 2. pure: the schema number --------------------------------------------------------------------------------------
{
  const inline = makeLoaded().document;
  assert.strictEqual(L.editorDocument(inline).schema_version, 4, "an inline-only document is still sent as 4");
  assert.strictEqual(L.docHasRefs(inline), false);
  const ref = L.addCameraRef(inline, "template", "home", GATE, {});
  assert.strictEqual(L.editorDocument(ref).schema_version, 5);
  const ruleRef = L.clone(inline); ruleRef.template.rules = [{ id: "r", caller: "kind:door", cameras: [GATE.id], ring_buttons: [], call_buttons: [] }];
  assert.strictEqual(L.editorDocument(ruleRef).schema_version, 5, "an element id in a rule's cameras");
  const scr = L.clone(inline); scr.dashboards = { k: { name: "K", ha_user_id: "u", cameras: [GATE.id] } };
  assert.strictEqual(L.editorDocument(scr).schema_version, 5, "an element id in a screen's cameras");
  const scr2 = L.clone(inline); scr2.dashboards = { k: { name: "K", ha_user_id: "u", cameras: ["camera.gate"] } };
  assert.strictEqual(L.editorDocument(scr2).schema_version, 4);
  const back = L.detachCamera(ref, "template", "home", "front-gate", GATE);
  assert.strictEqual(L.editorDocument(back).schema_version, 4, "after the last detach the document is plain 4 again");
  assert.strictEqual(L.SCHEMA_VERSION, 4);
  assert.strictEqual(L.SCHEMA_VERSION_REFS, 5);
  console.log("PASS: schema 5 only when a reference exists anywhere; otherwise 4, byte-for-byte as before");
}

// ---- 3. pure: validation does not flag a reference ------------------------------------------------------------------
{
  const ref = L.addCameraRef(makeLoaded().document, "template", "home", GATE, { size: "small", live: true });
  assert.deepStrictEqual(L.validate(ref), [], "a reference tile is not 'not a camera'");
  const bad = L.clone(ref); camsOf(bad).at(-1).element = "front-gate";
  assert.ok(L.validate(bad).some((p) => /element must be an element id/.test(p)));
  const bad2 = L.clone(ref); camsOf(bad2).at(-1).size = "huge"; camsOf(bad2).at(-1).live = "yes"; camsOf(bad2).at(-1).label = "x".repeat(200);
  assert.strictEqual(L.validate(bad2).length, 3);
  const inlineBad = L.clone(ref); camsOf(inlineBad)[0].entity_id = "light.x";
  assert.ok(L.validate(inlineBad).some((p) => /is not a camera/.test(p)), "an inline tile is still checked");
  const withRule = L.clone(ref); withRule.template.rules = [{ id: "r", caller: "kind:door", cameras: [GATE.id, "camera.gate"], ring_buttons: [], call_buttons: [] }];
  assert.deepStrictEqual(L.validate(withRule), [], "a rule camera may be an element id");
  withRule.template.rules[0].cameras.push("light.x");
  assert.ok(L.validate(withRule).some((p) => /rule camera is not a camera/.test(p)));
  const scr = L.clone(ref); scr.dashboards = { k: { name: "K", ha_user_id: "u2", cameras: [GATE.id] } };
  assert.deepStrictEqual(L.validate(scr, null, { users: new Set(["u2"]) }), []);
  console.log("PASS: validation accepts references in pages, rules and screens and still flags a real mistake");
}

// ---- 4. pure: detach, re-point, the label override, names -----------------------------------------------------------
{
  const base = makeLoaded().document;
  const plain = L.addCameraRef(base, "template", "home", GATE, {});
  const det = camsOf(L.detachCamera(plain, "template", "home", "front-gate", GATE)).at(-1);
  assert.deepStrictEqual(det, { id: "front-gate", label: "Front gate", entity_id: "camera.gate", size: "big" }, "name and size come from the element");
  const ov = L.addCameraRef(base, "template", "home", GATE, { label: "Street", size: "small", live: true });
  assert.deepStrictEqual(camsOf(L.detachCamera(ov, "template", "home", "front-gate", GATE)).at(-1),
    { id: "front-gate", label: "Street", entity_id: "camera.gate", size: "small", live: true }, "this tile's overrides win");
  const empty = L.addCameraRef(base, "template", "home", GARAGE, { label: "" });
  assert.deepStrictEqual(camsOf(L.detachCamera(empty, "template", "home", "garage", GARAGE)).at(-1), { id: "garage", label: "", entity_id: "camera.garage" });
  assert.strictEqual(L.validate(L.detachCamera(plain, "template", "home", "front-gate", GATE)).length, 0);
  throwsEdit(() => L.detachCamera(plain, "template", "home", "front-gate", null), /element is gone/);
  throwsEdit(() => L.detachCamera(plain, "template", "home", "front-gate", GARAGE), /element is gone/);
  throwsEdit(() => L.detachCamera(plain, "template", "home", "front-gate", STREAM), /element is gone/);
  throwsEdit(() => L.detachCamera(base, "template", "home", "gate", GATE), /not from an element/);
  const moved = L.repointCamera(ov, "template", "home", "front-gate", GARAGE);
  assert.deepStrictEqual(camsOf(moved).at(-1), { id: "front-gate", element: GARAGE.id, label: "Street", size: "small", live: true }, "overrides stay");
  throwsEdit(() => L.repointCamera(ov, "template", "home", "front-gate", STREAM), /Pick a camera element/);
  // label override: a string sets it, null follows the element again
  const s1 = L.updateTile(plain, "template", "home", "cameras", "front-gate", { label: "Street" });
  assert.strictEqual(camsOf(s1).at(-1).label, "Street");
  const s2 = L.updateTile(s1, "template", "home", "cameras", "front-gate", { label: null });
  assert.ok(!("label" in camsOf(s2).at(-1)));
  const s3 = L.updateTile(plain, "template", "home", "cameras", "front-gate", { size: "small", live: true });
  assert.deepStrictEqual(camsOf(s3).at(-1), { id: "front-gate", element: GATE.id, size: "small", live: true });
  // names
  const els = new Map(ELS.map((e) => [e.id, e]));
  assert.strictEqual(L.cameraName({ id: "a", element: GATE.id }, {}, els), "Front gate");
  assert.strictEqual(L.cameraName({ id: "a", element: GATE.id, label: "Street" }, {}, els), "Street");
  assert.strictEqual(L.cameraName({ id: "a", element: "el-deadbeef" }, {}, els), "Element removed");
  assert.strictEqual(L.cameraName({ id: "a", element: "el-deadbeef" }, {}, null), "el-deadbeef", "list unknown: the id, not a claim that it is gone");
  assert.strictEqual(L.cameraName({ id: "g", label: "", entity_id: "camera.gate" }, { "camera.gate": { attributes: { friendly_name: "Gate camera" } } }), "Gate camera");
  // screens: an element id is accepted once
  const sc = L.clone(base); sc.dashboards = { k: { name: "K", ha_user_id: "u", room: "", cameras: [] } };
  const sc2 = L.addScreenCamera(sc, "k", GATE.id);
  assert.deepStrictEqual(sc2.dashboards.k.cameras, [GATE.id]);
  throwsEdit(() => L.addScreenCamera(sc2, "k", GATE.id), /already/);
  throwsEdit(() => L.addScreenCamera(sc, "k", "light.x"), /Pick a camera/);
  console.log("PASS: detach copies the element and keeps overrides and the id; re-point; the label override; names; screen element ids");
}

// ---- 5. the dialog: default From Elements, the list, the preview, the saved reference ---------------------------------
{
  const { f, panel } = await mount();
  click(act(panel, "add-camera"));
  await sleep(30);
  const sw = qa(panel, `${FORM} [data-field="camera-source"] button`);
  assert.deepStrictEqual(sw.map((b) => b.textContent), ["From Elements", "Define here"]);
  assert.strictEqual(sw[0].getAttribute("aria-pressed"), "true", "From Elements is the default while a placeable camera element exists");
  assert.deepStrictEqual(qa(panel, `${FORM} .elrow`).map((n) => n.dataset.element), [GATE.id, GARAGE.id], "only entity-backed camera elements are offered (no stream-only, no button)");
  assert.ok(!q(panel, `${FORM} [data-field="target"]`), "the entity select is the other mode's");
  assert.ok(!q(panel, `${FORM} [data-slot="element-preview"]`) && q(panel, FORM).textContent.includes("Pick a camera element to see it here"), "nothing chosen: no preview yet");
  click(q(panel, `${FORM} .elrow[data-element="${GATE.id}"]`));
  assert.ok(q(panel, `${FORM} [data-slot="element-preview"] .pe-camera`), "the E3 renderer draws the chosen camera");
  assert.ok(q(panel, `${FORM} [data-slot="element-preview"]`).textContent.includes("Front gate"));
  assert.strictEqual(q(panel, `${FORM} [data-field="label"]`).value, "Front gate", "the name is prefilled with the element's");
  assert.ok(q(panel, FORM).textContent.includes("Defined on the Elements page"));
  setValue(q(panel, `${FORM} [data-field="element-filter"]`), "gara");
  assert.deepStrictEqual(qa(panel, `${FORM} .elrow`).map((n) => n.dataset.element), [GARAGE.id], "the filter narrows the list");
  setValue(q(panel, `${FORM} [data-field="element-filter"]`), "");
  setValue(q(panel, `${FORM} [data-field="size"]`), "small", "change");
  click(act(panel, "dialog-ok"));
  const row = q(panel, '.row[data-tile="front-gate"]');
  assert.ok(row, "the tile is on the page");
  assert.strictEqual(row.dataset.element, GATE.id);
  assert.strictEqual(row.querySelector('[data-badge="element"]').textContent, "element", "the element badge");
  assert.strictEqual(row.querySelector(".label").textContent, "Front gate");
  assert.ok(row.textContent.includes("camera.gate"), "the entity comes from the element");
  assert.ok(row.querySelector('[data-act="detach-tile"]'));
  click(act(panel, "save"));
  await sleep(50);
  const save = f.ofType("portico/content/save").at(-1);
  assert.strictEqual(save.document.schema_version, 5);
  assert.deepStrictEqual(save.document.template.pages[0].blocks[0].cameras.at(-1), { id: "front-gate", element: GATE.id, size: "small" });
  assert.ok(f.ofType("portico/elements/list").length >= 1);
  console.log("PASS: the camera dialog defaults to From Elements, previews with the renderer and saves {element} at schema 5");
  panel.remove();
}

// ---- 6. the dialog: Define here is unchanged and saves an inline camera at schema 4 ---------------------------------------
{
  const { f, panel } = await mount();
  click(act(panel, "add-camera"));
  await sleep(20);
  click(q(panel, `${FORM} [data-source="define"]`));
  assert.ok(q(panel, `${FORM} [data-field="target"]`), "the entity select");
  assert.ok(q(panel, FORM).textContent.includes("Used only on this screen"));
  setValue(q(panel, `${FORM} [data-field="target"]`), "camera.garage", "change");
  click(act(panel, "dialog-ok"));
  click(act(panel, "save"));
  await sleep(50);
  const save = f.ofType("portico/content/save").at(-1);
  assert.strictEqual(save.document.schema_version, 4);
  assert.deepStrictEqual(save.document.template.pages[0].blocks[0].cameras.at(-1), { id: "garage", label: "Garage camera", entity_id: "camera.garage" });
  console.log("PASS: Define here saves an inline camera, schema 4");
  panel.remove();
}

// ---- 7. no placeable element (or the list is unreadable): Define here is the default ----------------------------------------
for (const [name, opts] of [["no camera element", { elements: [BUTTON, STREAM] }], ["list fails", { handlers: { "portico/elements/list": () => { throw { code: "unknown", message: "x" }; } } }]]) {
  const { panel } = await mount(opts);
  click(act(panel, "add-camera"));
  await sleep(20);
  assert.ok(q(panel, `${FORM} [data-field="target"]`), `${name}: the entity select is shown`);
  assert.strictEqual(q(panel, `${FORM} [data-source="define"]`).getAttribute("aria-pressed"), "true");
  click(q(panel, `${FORM} [data-source="elements"]`));
  assert.ok(q(panel, `${FORM} .empty`), `${name}: From Elements says why there is nothing`);
  panel.remove();
}
console.log("PASS: with no placeable element, or no list, the dialog falls back to Define here");

// ---- 8. the placed tile: overrides, detach, open element ----------------------------------------------------------------------
{
  const { f, panel } = await mount({ loaded: withRef() });
  const row = () => q(panel, '.row[data-tile="front-gate"]');
  assert.ok(row().querySelector('[data-badge="element"]'));
  assert.deepStrictEqual(L.validate(panel._state.draft), [], "the loaded reference raises no problem");
  // size and live straight on the row
  setValue(row().querySelector('[data-field="camera-size"]'), "big", "change");
  { const live = row().querySelector('[data-field="camera-live"]'); live.checked = true; live.dispatchEvent(new win.Event("change", { bubbles: true })); }
  assert.deepStrictEqual(camsOf(panel._state.draft).at(-1), { id: "front-gate", element: GATE.id, size: "big", live: true });
  // the Edit dialog: label override, and typing the element's own name removes it again
  click(row().querySelector('[data-act="edit-tile"]'));
  assert.strictEqual(q(panel, `${FORM} [data-field="label"]`).value, "Front gate");
  assert.ok(q(panel, `${FORM} [data-slot="element-preview"] .pe-camera`), "the edit dialog previews the element");
  setValue(q(panel, `${FORM} [data-field="label"]`), "Street");
  click(act(panel, "dialog-ok"));
  assert.strictEqual(camsOf(panel._state.draft).at(-1).label, "Street");
  assert.strictEqual(row().querySelector(".label").textContent, "Street");
  click(row().querySelector('[data-act="edit-tile"]'));
  setValue(q(panel, `${FORM} [data-field="label"]`), "Front gate");
  click(act(panel, "dialog-ok"));
  assert.ok(!("label" in camsOf(panel._state.draft).at(-1)), "the element's own name is not stored as an override");
  // Open element goes to the Elements tab
  click(row().querySelector('[data-act="open-element"]'));
  assert.strictEqual(panel._tab, "elements");
  click(act(panel, "tab-pages"));
  // detach
  click(row().querySelector('[data-act="detach-tile"]'));
  await sleep(30);
  assert.deepStrictEqual(camsOf(panel._state.draft).at(-1), { id: "front-gate", label: "Front gate", entity_id: "camera.gate", size: "big", live: true });
  assert.ok(!row().querySelector('[data-badge="element"]') && !row().querySelector('[data-act="detach-tile"]'), "an inline tile again");
  click(act(panel, "save"));
  await sleep(50);
  const save = f.ofType("portico/content/save").at(-1);
  assert.strictEqual(save.document.schema_version, 4, "no reference left: sent as 4");
  assert.ok(!JSON.stringify(save.document).includes('"element"'));
  console.log("PASS: size, live and name overrides on the placed tile; Open element; Detach converts it to an inline camera");
  panel.remove();
}

// ---- 9. an element that was removed ------------------------------------------------------------------------------------------
{
  const loaded = makeLoaded();
  loaded.document.template.pages[0].blocks[0].cameras.push({ id: "old", element: "el-deadbeef", size: "small" });
  const { f, panel } = await mount({ loaded });
  const row = q(panel, '.row[data-tile="old"]');
  assert.strictEqual(row.dataset.state, "element-removed");
  assert.ok(row.classList.contains("dangling"));
  assert.strictEqual(row.querySelector('[data-badge="element"]').textContent, "element removed");
  assert.ok(row.textContent.includes("Element removed"));
  assert.ok(!row.querySelector('[data-act="detach-tile"]') && !row.querySelector('[data-act="edit-tile"]') && !row.querySelector('[data-field="camera-live"]'), "nothing to edit on a removed element");
  assert.deepStrictEqual(L.validate(panel._state.draft), [], "a stored reference whose element is gone does not block the save (the server keeps it)");
  assert.deepStrictEqual(qa(panel, '[data-field="choose-element"] option').map((o) => o.value), ["", GATE.id, GARAGE.id]);
  setValue(row.querySelector('[data-field="choose-element"]'), GARAGE.id, "change");
  await sleep(30);
  assert.deepStrictEqual(camsOf(panel._state.draft).at(-1), { id: "old", element: GARAGE.id, size: "small" }, "choose another keeps the overrides");
  assert.ok(!q(panel, '.row[data-tile="old"]').dataset.state, "the tile is fine again");
  console.log("PASS: a removed element shows a clear tile with Choose another and Remove");
  panel.remove();
}
{
  const loaded = makeLoaded();
  loaded.document.template.pages[0].blocks[0].cameras.push({ id: "old", element: "el-deadbeef" });
  const { panel } = await mount({ loaded });
  click(q(panel, '.row[data-tile="old"] [data-act="remove-tile"]'));
  assert.ok(!q(panel, '.row[data-tile="old"]'), "Remove takes the tile away");
  panel.remove();
  // the list unreadable: the tile is not called removed
  const l2 = makeLoaded();
  l2.document.template.pages[0].blocks[0].cameras.push({ id: "old", element: "el-deadbeef" });
  const m = await mount({ loaded: l2, handlers: { "portico/elements/list": () => { throw { code: "unknown", message: "x" }; } } });
  const r2 = q(m.panel, '.row[data-tile="old"]');
  assert.ok(!r2.dataset.state, "unknown is not the same as removed");
  assert.strictEqual(r2.querySelector('[data-badge="element"]').textContent, "element");
  m.panel.remove();
  console.log("PASS: Remove works on a dangling tile; an unreadable list is not reported as removal");
}

// ---- 10. the Screens tab and the Ring & call tab ------------------------------------------------------------------------------
function screensLoaded() {
  const loaded = makeLoaded();
  loaded.ha_users = [{ id: "u2", name: "Tablet", is_admin: false, kiosk_open: false }];
  loaded.document.dashboards = { kitchen: { name: "Kitchen tablet", ha_user_id: "u2", room: "Kitchen", cameras: ["camera.gate"] } };
  loaded.document.template.rules = [{ id: "any-door", caller: "kind:door", cameras: [], ring_buttons: ["open-gate"], call_buttons: [] }];
  return loaded;
}
{
  const { f, panel } = await mount({ loaded: screensLoaded() });
  click(act(panel, "tab-devices"));
  await sleep(40);       // the elements are read when the tab opens
  assert.strictEqual(q(panel, '[data-field="screen-camera-add-source"] [data-source="elements"]').getAttribute("aria-pressed"), "true");
  assert.deepStrictEqual(qa(panel, '[data-field="screen-camera-add-element"] option').map((o) => o.value), ["", GATE.id, GARAGE.id]);
  assert.ok(!q(panel, '[data-field="screen-camera-add"]'), "the entity select is the other mode's");
  setValue(q(panel, '[data-field="screen-camera-add-element"]'), GATE.id, "change");
  assert.deepStrictEqual(panel._state.draft.dashboards.kitchen.cameras, ["camera.gate", GATE.id]);
  const row = q(panel, `.row[data-camera="${GATE.id}"]`);
  assert.ok(row.querySelector('[data-badge="element"]') && row.textContent.includes("Front gate") && row.textContent.includes("camera.gate"));
  assert.deepStrictEqual(qa(panel, '[data-field="screen-camera-add-element"] option').map((o) => o.value), ["", GARAGE.id], "one already in the list is not offered again");
  click(q(panel, '[data-field="screen-camera-add-source"] [data-source="define"]'));
  assert.ok(q(panel, '[data-field="screen-camera-add"]'), "Define here: the entity select as before");
  click(act(panel, "save"));
  await sleep(50);
  const save = f.ofType("portico/content/save").at(-1);
  assert.strictEqual(save.document.schema_version, 5);
  assert.deepStrictEqual(save.document.dashboards.kitchen.cameras, ["camera.gate", GATE.id]);
  console.log("PASS: the screen camera picker switches, accepts an element id and saves it");
  panel.remove();
}
{
  const l = screensLoaded();
  l.document.dashboards.kitchen.cameras = ["el-deadbeef"];
  const { panel } = await mount({ loaded: l });
  click(act(panel, "tab-devices"));
  await sleep(40);       // the elements are read when the tab opens
  const row = q(panel, '.row[data-camera="el-deadbeef"]');
  assert.strictEqual(row.dataset.state, "element-removed");
  assert.ok(row.textContent.includes("Element removed"));
  assert.deepStrictEqual(L.validate(panel._state.draft, null, { users: new Set(["u2"]) }), []);
  panel.remove();
  console.log("PASS: a screen camera whose element was removed shows the removed row");
}
{
  const { f, panel } = await mount({ loaded: screensLoaded() });
  click(act(panel, "tab-calls"));
  await sleep(40);       // the elements are read when the tab opens
  setValue(q(panel, '[data-field="rule-cameras-mode"]'), "these", "change");
  assert.ok(q(panel, '[data-field="rule-camera-add-element"]'), "the rule picker defaults to From Elements");
  setValue(q(panel, '[data-field="rule-camera-add-element"]'), GARAGE.id, "change");
  assert.deepStrictEqual(panel._state.draft.template.rules[0].cameras, [GARAGE.id]);
  const row = q(panel, `.row[data-camera="${GARAGE.id}"]`);
  assert.ok(row.querySelector('[data-badge="element"]') && row.textContent.includes("Garage"));
  click(q(panel, '[data-field="rule-camera-add-source"] [data-source="define"]'));
  setValue(q(panel, '[data-field="rule-camera-add"]'), "camera.gate", "change");
  assert.deepStrictEqual(panel._state.draft.template.rules[0].cameras, [GARAGE.id, "camera.gate"], "elements and entities mix in one list");
  await sleep(60);
  assert.strictEqual(f.ofType("portico/content/preview").at(-1).document.schema_version, 5, "the preview request carries 5 too");
  click(q(panel, `.row[data-camera="${GARAGE.id}"] [data-act="remove-rule-camera"]`));
  assert.deepStrictEqual(panel._state.draft.template.rules[0].cameras, ["camera.gate"]);
  console.log("PASS: the ring-rule camera picker switches, accepts element ids, mixes with entities");
  panel.remove();
}

// ---- 11. no HTML-string setters -----------------------------------------------------------------------------------------------
{
  const src = fs.readFileSync(new URL("../portico-panel.js", import.meta.url), "utf8");
  for (const bad of ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "srcdoc"]) {
    assert.ok(!src.includes(bad), `portico-panel.js must not use ${bad}`);
  }
  // a hostile element name is text, never markup
  const hostile = { ...GATE, name: "<img src=x onerror=alert(1)>" };
  const { panel } = await mount({ elements: [hostile] });
  click(act(panel, "add-camera"));
  await sleep(20);
  assert.ok(!q(panel, `${FORM} img[src="x"]`), "the name is not parsed as markup");
  assert.ok(q(panel, `${FORM} .elrow`).textContent.includes("<img"));
  panel.remove();
  console.log("PASS: no HTML-string setter; element names are text");
}
console.log("ALL PASS test_panel_camrefs");
process.exit(0);
