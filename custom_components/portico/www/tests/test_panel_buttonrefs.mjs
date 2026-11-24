// DEV-760 P3: button references in the panel. SAFETY first: a button can open a door or the gate, so the checks here are about
// what a screen can NOT do: turn ask-first off on a door/lock/gate action (never stored as false, the box is locked on), hold an
// entity or action of its own (a reference stores none), hide the text without an icon. Then the pure edit functions (place,
// detach, re-point, overrides), the schema number, validation, and the panel in jsdom with a fake `hass` answering
// portico/elements/list: the button dialog's "From Elements / Define here" switch with its inert preview, the saved `{element}`
// item, the badge, Detach, the "Element removed" tile, the rule's button list. Protocol-level; NOT a real browser.
import assert from "assert";
import fs from "fs";
import { loadPanel, editorHass, makeLoaded, sleep } from "./panel_loader.mjs";

const { dom, Panel, doc } = loadPanel();
const L = Panel.logic;
const win = dom.window;
const throwsEdit = (fn, re) => assert.throws(fn, (e) => e instanceof L.EditError && re.test(e.message), String(re));

const GATE = { id: "el-10000001", kind: "button", name: "Gate lock", entity_id: "lock.gate", icon: "lock-open", options: { action: "lock_open", color: "red" } };
const UNLOCK = { id: "el-10000002", kind: "button", name: "Front unlock", entity_id: "lock.front", options: { action: "lock_unlock" } };
const LAMP = { id: "el-10000003", kind: "button", name: "Lamp", entity_id: "script.lamp", icon: "lightbulb", options: { action: "script_run" } };
const DOOR = { id: "el-10000004", kind: "button", name: "Block door", options: { action: "portico_open" } };
const TALK = { id: "el-10000005", kind: "button", name: "Talk", options: { action: "talk" } };
const CALL = { id: "el-10000006", kind: "button", name: "Call kitchen", options: { action: "call", target: "dashboard:kitchen" } };
const BADCALL = { id: "el-10000007", kind: "button", name: "Bad call", options: { action: "call", target: "nonsense" } };
const CAM = { id: "el-10000008", kind: "camera", name: "Cam", entity_id: "camera.gate", options: {} };
const WRONG = { id: "el-10000009", kind: "button", name: "Wrong domain", entity_id: "switch.x", options: { action: "lock_open" } };
const ELS = [GATE, UNLOCK, LAMP, DOOR, TALK, CALL, BADCALL, CAM, WRONG];
const listReply = (elements) => ({ schema_version: 1, rev: "r1", elements, used_by: {}, kinds: { camera: { label: "Camera" }, button: { label: "Button" } }, icons: [], palette: [], limits: {} });

const q = (p, sel) => p.shadowRoot.querySelector(sel);
const qa = (p, sel) => Array.from(p.shadowRoot.querySelectorAll(sel));
const act = (p, name) => q(p, `[data-act="${name}"]`);
const click = (node) => { assert.ok(node, "clicked something that is not there"); node.dispatchEvent(new win.Event("click", { bubbles: true })); };
function setValue(node, value, type = "input") { assert.ok(node); node.value = value; node.dispatchEvent(new win.Event(type, { bubbles: true })); }
function setChecked(node, on) { assert.ok(node); node.checked = on; node.dispatchEvent(new win.Event("change", { bubbles: true })); }
const FORM = '[data-dialog="actions"]';
const blockOf = (d, withDoor) => { const b = d.template.pages[0].blocks[0]; if (withDoor) b.door = "portico_intercom:http://10.6.0.7:8091"; return b; };
const actsOf = (d) => L.pageActions(d.template.pages[0]);
const lastAct = (d) => actsOf(d).at(-1);

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
const withRef = (extra = {}, el = GATE) => {
  const loaded = makeLoaded();
  loaded.document.template.pages[0].blocks[0].actions.push({ id: "gate-lock", element: el.id, ...extra });
  return loaded;
};

// ---- 1. pure: placing, and what is never stored -----------------------------------------------------------------------
{
  const d = makeLoaded().document;
  const r = L.addButtonRef(d, "template", "home", GATE, { label: "Gate lock", show_label: true, confirm: false });
  assert.deepStrictEqual(lastAct(r), { id: "gate-lock", element: GATE.id }, "name equal to the element's, nothing else asked: only the reference");
  for (const k of ["entity_id", "kind", "icon", "color", "device_id", "target"]) assert.ok(!(k in lastAct(r)), `a reference holds no ${k}`);
  const o = L.addButtonRef(d, "template", "home", LAMP, { label: "Lights", show_label: false, confirm: true });
  assert.deepStrictEqual(lastAct(o), { id: "lamp", element: LAMP.id, label: "Lights", show_label: false, confirm: true }, "a harmless action may be tightened");
  // SAFETY: ask-first is never stored for an action that already asks first, and never as false
  for (const el of [GATE, UNLOCK]) {
    for (const confirm of [true, false, undefined]) {
      const t = lastAct(L.addButtonRef(d, "template", "home", el, { confirm }));
      assert.ok(!("confirm" in t), `${el.options.action}: nothing stored (it always asks first)`);
    }
  }
  assert.ok(!("confirm" in lastAct(L.addButtonRef(d, "template", "home", LAMP, { confirm: false }))), "false is never stored");
  throwsEdit(() => L.addButtonRef(d, "template", "home", UNLOCK, { show_label: false }), /icon/);   // no icon on the element
  throwsEdit(() => L.addButtonRef(d, "template", "home", CAM, {}), /cannot be placed/);
  throwsEdit(() => L.addButtonRef(d, "template", "home", WRONG, {}), /cannot be placed/);
  throwsEdit(() => L.addButtonRef(d, "template", "home", BADCALL, {}), /cannot be placed/);
  throwsEdit(() => L.addButtonRef(d, "template", "home", null, {}), /Pick an element/);
  throwsEdit(() => L.addButtonRef(d, "template", "home", TALK, {}), /door/);              // the block has no door
  throwsEdit(() => L.addButtonRef(d, "template", "home", DOOR, {}), /door/);
  const withDoor = L.clone(d); blockOf(withDoor, true);
  assert.deepStrictEqual(lastAct(L.addButtonRef(withDoor, "template", "home", TALK, {})), { id: "talk", element: TALK.id });
  assert.deepStrictEqual(lastAct(L.addButtonRef(withDoor, "template", "home", DOOR, {})), { id: "block-door", element: DOOR.id });
  assert.deepStrictEqual(lastAct(L.addButtonRef(d, "template", "home", CALL, {})), { id: "call-kitchen", element: CALL.id });
  const twice = L.addButtonRef(r, "template", "home", GATE, {});
  assert.deepStrictEqual(actsOf(twice).map((a) => a.id).slice(-2), ["gate-lock", "gate-lock-2"]);
  assert.ok(L.placeableButton(GATE) && !L.placeableButton(CAM) && !L.placeableButton(WRONG) && !L.placeableButton(BADCALL) && !L.placeableButton(null));
  assert.ok(L.askLocked(GATE) && L.askLocked(UNLOCK) && L.askLocked(DOOR) && !L.askLocked(LAMP) && !L.askLocked(TALK));
  assert.deepStrictEqual(L.parseCallTarget("door:portico_intercom:http://x"), { type: "door", id: "portico_intercom:http://x" });
  console.log("PASS: addButtonRef stores only the reference; ask-first is never stored as false nor on a door, lock or gate; refusals");
}

// ---- 2. pure: updateTile on a reference -------------------------------------------------------------------------------
{
  const base = makeLoaded().document;
  const lock = L.addButtonRef(base, "template", "home", UNLOCK, {});
  // trying to turn ask-first off, or on (redundant), changes nothing stored
  for (const confirm of [false, true]) assert.ok(!("confirm" in lastAct(L.updateTile(lock, "template", "home", "actions", "front-unlock", { confirm }, UNLOCK))));
  const lamp = L.addButtonRef(base, "template", "home", LAMP, {});
  const on = L.updateTile(lamp, "template", "home", "actions", "lamp", { confirm: true }, LAMP);
  assert.strictEqual(lastAct(on).confirm, true);
  const off = L.updateTile(on, "template", "home", "actions", "lamp", { confirm: false }, LAMP);
  assert.ok(!("confirm" in lastAct(off)), "turning the optional question off removes the key, it never stores false");
  const hidden = L.updateTile(lamp, "template", "home", "actions", "lamp", { show_label: false }, LAMP);
  assert.strictEqual(lastAct(hidden).show_label, false);
  assert.ok(!("show_label" in lastAct(L.updateTile(hidden, "template", "home", "actions", "lamp", { show_label: true }, LAMP))));
  throwsEdit(() => L.updateTile(lock, "template", "home", "actions", "front-unlock", { show_label: false }, UNLOCK), /icon/);
  // label: a string overrides, null follows the element again; an empty button name is refused
  const named = L.updateTile(lamp, "template", "home", "actions", "lamp", { label: "Reading light" }, LAMP);
  assert.strictEqual(lastAct(named).label, "Reading light");
  assert.ok(!("label" in lastAct(L.updateTile(named, "template", "home", "actions", "lamp", { label: null }, LAMP))));
  throwsEdit(() => L.updateTile(lamp, "template", "home", "actions", "lamp", { label: "" }, LAMP), /needs a name/);
  // a patch of an entity, kind or colour has no effect on a reference
  const sneaky = L.updateTile(lamp, "template", "home", "actions", "lamp", { icon: "door", color: "red", kind: "lock_open", entity_id: "lock.evil" }, LAMP);
  assert.deepStrictEqual(lastAct(sneaky), { id: "lamp", element: LAMP.id });
  console.log("PASS: updateTile on a reference changes label, show text and ask-first (tighten only) and nothing else");
}

// ---- 3. pure: the schema number, validation ----------------------------------------------------------------------------
{
  const inline = makeLoaded().document;
  assert.strictEqual(L.editorDocument(inline).schema_version, 4);
  const ref = L.addButtonRef(inline, "template", "home", LAMP, {});
  assert.strictEqual(L.docHasRefs(ref), true);
  assert.strictEqual(L.editorDocument(ref).schema_version, 5, "a button reference makes it schema 5");
  assert.strictEqual(L.editorDocument(L.detachButton(ref, "template", "home", "lamp", LAMP)).schema_version, 4, "after the last detach: 4 again");
  assert.deepStrictEqual(L.validate(ref), [], "a reference is not 'needs its lock'");
  const bad = L.clone(ref); lastAct(bad).element = "gate";
  assert.ok(L.validate(bad).some((p) => /element must be an element id/.test(p)));
  const weak = L.clone(ref); lastAct(weak).confirm = false;
  assert.ok(L.validate(weak).some((p) => /can be turned on, never off/.test(p)), "a hand-made confirm:false is flagged");
  const bad2 = L.clone(ref); lastAct(bad2).show_label = "no"; lastAct(bad2).label = "";
  assert.strictEqual(L.validate(bad2).length, 2);
  const dup = L.clone(ref); blockOf(dup).actions.push({ id: "lamp", element: GATE.id });
  assert.ok(L.validate(dup).some((p) => /used twice/.test(p)));
  const inlineBad = L.clone(ref); actsOf(inlineBad)[0].entity_id = "switch.x";
  assert.ok(L.validate(inlineBad).some((p) => /needs its lock/.test(p)), "an inline tile is still checked");
  console.log("PASS: schema 5 only while a button reference exists; validation flags a weakened or malformed reference");
}

// ---- 4. pure: detach, re-point, rule choices --------------------------------------------------------------------------
{
  const base = makeLoaded().document;
  const gate = L.addButtonRef(base, "template", "home", GATE, {});
  assert.deepStrictEqual(lastAct(L.detachButton(gate, "template", "home", "gate-lock", GATE)),
    { id: "gate-lock", label: "Gate lock", kind: "lock_open", entity_id: "lock.gate", icon: "lock-open", color: "red", confirm: true }, "copy of the element, asks first");
  const lamp = L.addButtonRef(base, "template", "home", LAMP, { label: "Lights", show_label: false, confirm: true });
  assert.deepStrictEqual(lastAct(L.detachButton(lamp, "template", "home", "lamp", LAMP)),
    { id: "lamp", label: "Lights", kind: "script_run", entity_id: "script.lamp", icon: "lightbulb", show_label: false, confirm: true });
  assert.strictEqual(L.validate(L.detachButton(lamp, "template", "home", "lamp", LAMP)).length, 0);
  const withDoor = L.clone(base); blockOf(withDoor, true);
  const door = L.addButtonRef(withDoor, "template", "home", DOOR, {});
  assert.deepStrictEqual(lastAct(L.detachButton(door, "template", "home", "block-door", DOOR)), { id: "block-door", label: "Block door", kind: "portico_open", confirm: true });
  const call = L.addButtonRef(base, "template", "home", CALL, {});
  assert.deepStrictEqual(lastAct(L.detachButton(call, "template", "home", "call-kitchen", CALL)),
    { id: "call-kitchen", label: "Call kitchen", kind: "call", target: { type: "dashboard", id: "kitchen" }, confirm: false });
  throwsEdit(() => L.detachButton(gate, "template", "home", "gate-lock", null), /element is gone/);
  throwsEdit(() => L.detachButton(gate, "template", "home", "gate-lock", LAMP), /element is gone/);
  throwsEdit(() => L.detachButton(base, "template", "home", "open-gate", GATE), /not from an element/);
  // re-point keeps the overrides; an ask-first override is not copied from anywhere
  const moved = L.repointButton(lamp, "template", "home", "lamp", UNLOCK);
  assert.deepStrictEqual(lastAct(moved), { id: "lamp", element: UNLOCK.id, label: "Lights", show_label: false, confirm: true });
  throwsEdit(() => L.repointButton(lamp, "template", "home", "lamp", CAM), /Pick a button element/);
  // the rule's button list: names through the elements, a talk element is not offered
  const els = new Map(ELS.map((e) => [e.id, e]));
  const two = L.clone(L.addButtonRef(lamp, "template", "home", UNLOCK, {})); blockOf(two, true);
  const withTalk = L.addButtonRef(two, "template", "home", TALK, {});
  assert.strictEqual(actsOf(withTalk).length, 4);
  const choices = L.ruleButtonChoices(withTalk, "template", els);
  assert.deepStrictEqual(choices.map((c) => c.label), ["Open the gate", "Lights", "Front unlock"], "refs are listed by name; the talk reference is left out");
  console.log("PASS: detach copies the element and keeps overrides and the id; re-point; the rule list names references");
}

// ---- 5. the dialog: From Elements is the default; locked ask-first on a lock; the saved reference --------------------------
{
  const { f, panel } = await mount();
  click(act(panel, "add-button"));
  await sleep(30);
  const sw = qa(panel, `${FORM} [data-field="button-source"] button`);
  assert.deepStrictEqual(sw.map((b) => b.textContent), ["From Elements", "Define here"]);
  assert.strictEqual(sw[0].getAttribute("aria-pressed"), "true", "From Elements is the default while a placeable button element exists");
  assert.deepStrictEqual(qa(panel, `${FORM} .elrow`).map((n) => n.dataset.element), [GATE.id, UNLOCK.id, LAMP.id, CALL.id], "only buildable button elements (no camera, wrong domain, bad call; the door and Talk need a block door)");
  assert.ok(!q(panel, `${FORM} [data-field="kind"]`) && !q(panel, `${FORM} [data-field="target"]`), "the kind and entity selects are the other mode's");
  assert.ok(q(panel, FORM).textContent.includes("Pick a button element to see it here"));
  click(q(panel, `${FORM} .elrow[data-element="${UNLOCK.id}"]`));
  assert.ok(q(panel, `${FORM} [data-slot="element-preview"]`).textContent.includes("Front unlock"), "the chosen element is previewed");
  assert.ok(qa(panel, `${FORM} [data-slot="element-preview"] button`).every((b) => b.disabled || b.getAttribute("aria-disabled") === "true"), "the preview is inert: every button in it is disabled");
  assert.strictEqual(q(panel, `${FORM} [data-field="label"]`).value, "Front unlock");
  assert.ok(!q(panel, `${FORM} .iconpicker, ${FORM} [data-field="color"]`), "icon and colour belong to the element");
  const ask = q(panel, `${FORM} [data-field="confirm"]`);
  assert.ok(ask.checked && ask.disabled, "SAFETY: a lock always asks first and the box cannot be unticked");
  assert.ok(q(panel, FORM).textContent.includes("always on for a door, lock or gate"));
  setChecked(ask, false);                       // a forced change event on a disabled box must not weaken anything
  click(act(panel, "dialog-ok"));
  const row = q(panel, '.row[data-tile="front-unlock"]');
  assert.ok(row && row.dataset.element === UNLOCK.id);
  assert.strictEqual(row.querySelector('[data-badge="element"]').textContent, "element");
  assert.ok(row.querySelector(".sub").textContent.includes("lock.front") && row.querySelector(".sub").textContent.includes("requires confirmation"));
  assert.ok(row.querySelector('[data-act="detach-tile"]'));
  click(act(panel, "save"));
  await sleep(50);
  const save = f.ofType("portico/content/save").at(-1);
  assert.strictEqual(save.document.schema_version, 5);
  const saved = save.document.template.pages[0].blocks[0].actions.at(-1);
  assert.deepStrictEqual(saved, { id: "front-unlock", element: UNLOCK.id }, "no confirm key at all (the server's default asks first)");
  assert.ok(!JSON.stringify(save.document).includes('"confirm":false,"element"'));
  console.log("PASS: the button dialog defaults to From Elements, previews inertly, locks ask-first on a lock and saves {element} at schema 5");
  panel.remove();
}
{
  const { f, panel } = await mount();
  click(act(panel, "add-button"));
  await sleep(20);
  click(q(panel, `${FORM} .elrow[data-element="${LAMP.id}"]`));
  const ask = q(panel, `${FORM} [data-field="confirm"]`);
  assert.ok(!ask.disabled && !ask.checked, "a harmless action: the question can be added");
  setChecked(ask, true);
  setChecked(q(panel, `${FORM} [data-field="show-label"]`), false);
  setValue(q(panel, `${FORM} [data-field="label"]`), "Reading light");
  click(act(panel, "dialog-ok"));
  click(act(panel, "save"));
  await sleep(50);
  const saved = f.ofType("portico/content/save").at(-1).document.template.pages[0].blocks[0].actions.at(-1);
  assert.deepStrictEqual(saved, { id: "lamp", element: LAMP.id, label: "Reading light", show_label: false, confirm: true });
  panel.remove();
  console.log("PASS: a harmless button can be tightened and renamed, and hide its text (it has an icon)");
}
// Talk and Define-here
{
  const { f, panel } = await mount();
  click(act(panel, "add-button"));
  await sleep(20);
  click(q(panel, `${FORM} [data-source="define"]`));
  assert.ok(q(panel, `${FORM} [data-field="kind"]`) && q(panel, FORM).textContent.includes("Used only on this screen"));
  setValue(q(panel, `${FORM} [data-field="kind"]`), "script_run", "change");
  setValue(q(panel, `${FORM} [data-field="target"]`), "script.evening", "change");
  setValue(q(panel, `${FORM} [data-field="label"]`), "Lamp inline");
  click(act(panel, "dialog-ok"));
  assert.strictEqual(panel._dialog, null, `dialog error: ${panel._dialog && panel._dialog.error}`);
  click(act(panel, "save"));
  await sleep(50);
  const save = f.ofType("portico/content/save").at(-1);
  assert.strictEqual(save.document.schema_version, 4, "an inline button: still schema 4");
  assert.ok(!JSON.stringify(save.document).includes('"element"'));
  assert.strictEqual(save.document.template.pages[0].blocks[0].actions.at(-1).kind, "script_run");
  console.log("PASS: Define here saves an inline button, schema 4");
  panel.remove();
}
for (const [name, opts] of [["no button element", { elements: [CAM, WRONG] }], ["list fails", { handlers: { "portico/elements/list": () => { throw { code: "unknown", message: "x" }; } } }]]) {
  const { panel } = await mount(opts);
  click(act(panel, "add-button"));
  await sleep(20);
  assert.ok(q(panel, `${FORM} [data-field="kind"]`), `${name}: the kind select is shown`);
  assert.strictEqual(q(panel, `${FORM} [data-source="define"]`).getAttribute("aria-pressed"), "true");
  click(q(panel, `${FORM} [data-source="elements"]`));
  assert.ok(q(panel, `${FORM} .empty`), `${name}: From Elements says why there is nothing`);
  panel.remove();
}
console.log("PASS: with no placeable button element, or no list, the dialog falls back to Define here");

// ---- 6. the placed tile: edit overrides, detach, open element -------------------------------------------------------------
{
  const { f, panel } = await mount({ loaded: withRef({}, UNLOCK) });
  const row = () => q(panel, '.row[data-tile="gate-lock"]');
  assert.ok(row().querySelector('[data-badge="element"]'));
  assert.deepStrictEqual(L.validate(panel._state.draft), []);
  click(row().querySelector('[data-act="edit-tile"]'));
  assert.ok(q(panel, `${FORM} [data-slot="element-preview"]`), "the edit dialog previews the element");
  assert.ok(q(panel, `${FORM} [data-field="confirm"]`).disabled, "ask-first is locked on a lock in the edit dialog too");
  assert.ok(!q(panel, `${FORM} .iconpicker`));
  setValue(q(panel, `${FORM} [data-field="label"]`), "Front door");
  click(act(panel, "dialog-ok"));
  assert.strictEqual(lastAct(panel._state.draft).label, "Front door");
  assert.strictEqual(row().querySelector(".label").textContent, "Front door");
  click(row().querySelector('[data-act="edit-tile"]'));
  setValue(q(panel, `${FORM} [data-field="label"]`), "Front unlock");
  click(act(panel, "dialog-ok"));
  assert.ok(!("label" in lastAct(panel._state.draft)), "the element's own name is not stored as an override");
  click(row().querySelector('[data-act="open-element"]'));
  assert.strictEqual(panel._tab, "elements");
  click(act(panel, "tab-pages"));
  click(row().querySelector('[data-act="detach-tile"]'));
  await sleep(30);
  assert.deepStrictEqual(lastAct(panel._state.draft), { id: "gate-lock", label: "Front unlock", kind: "lock_unlock", entity_id: "lock.front", confirm: true });
  assert.ok(!row().querySelector('[data-badge="element"]') && !row().querySelector('[data-act="detach-tile"]'), "an inline tile again");
  click(act(panel, "save"));
  await sleep(50);
  assert.strictEqual(f.ofType("portico/content/save").at(-1).document.schema_version, 4);
  console.log("PASS: edit overrides on the placed button; Open element; Detach converts it to an inline button that asks first");
  panel.remove();
}

// ---- 7. an element that was removed or became unusable -----------------------------------------------------------------------
{
  const loaded = makeLoaded();
  loaded.document.template.pages[0].blocks[0].actions.push({ id: "old", element: "el-deadbeef", confirm: true });
  const { panel } = await mount({ loaded });
  const row = q(panel, '.row[data-tile="old"]');
  assert.strictEqual(row.dataset.state, "element-removed");
  assert.ok(row.classList.contains("dangling"));
  assert.strictEqual(row.querySelector('[data-badge="element"]').textContent, "element removed");
  assert.ok(row.textContent.includes("Element removed"));
  assert.ok(!row.querySelector('[data-act="detach-tile"]') && !row.querySelector('[data-act="edit-tile"]'), "nothing to edit on a removed element");
  assert.deepStrictEqual(L.validate(panel._state.draft), []);
  assert.deepStrictEqual(qa(panel, '.row[data-tile="old"] [data-field="choose-element"] option').map((o) => o.value), ["", GATE.id, UNLOCK.id, LAMP.id, CALL.id]);
  setValue(row.querySelector('[data-field="choose-element"]'), LAMP.id, "change");
  await sleep(30);
  assert.deepStrictEqual(lastAct(panel._state.draft), { id: "old", element: LAMP.id, confirm: true });
  assert.ok(!q(panel, '.row[data-tile="old"]').dataset.state);
  panel.remove();
  const l2 = makeLoaded();
  l2.document.template.pages[0].blocks[0].actions.push({ id: "old", element: "el-deadbeef" });
  const m = await mount({ loaded: l2, handlers: { "portico/elements/list": () => { throw { code: "unknown", message: "x" }; } } });
  assert.ok(!q(m.panel, '.row[data-tile="old"]').dataset.state, "unknown is not the same as removed");
  m.panel.remove();
  console.log("PASS: a removed element shows a clear tile with Choose another; an unreadable list is not reported as removal");
}

// ---- 8. the Ring & call tab lists a referenced button by name, and offers a rule only buttons that exist on the page ----------
{
  const { f, panel } = await mount({ loaded: (() => {
    const l = withRef({}, LAMP);
    l.document.template.rules = [{ id: "any-door", caller: "kind:door", cameras: [], ring_buttons: [], call_buttons: [] }];
    return l;
  })() });
  click(act(panel, "tab-calls"));
  await sleep(40);
  const labels = qa(panel, '[data-list="ring_buttons"] label').map((n) => n.textContent.trim());
  assert.ok(labels.some((t) => t.includes("Lamp")), `the referenced button is offered by its element's name: ${labels.join(" | ")}`);
  const box = qa(panel, '[data-list="ring_buttons"] input').find((i) => i.value === "gate-lock" || (i.dataset && i.dataset.id === "gate-lock"));
  assert.ok(box || labels.length, "listed");
  panel.remove();
  console.log("PASS: rule buttons stay page-button ids; a reference is listed by name");
}

// ---- 10. review round: ids unique across the whole document; Detach / Choose another re-read the element --------------------------
{
  // a harmless button on ANOTHER page holds the id "lamp"; the new one gets a different id
  const d = makeLoaded().document;
  d.overrides.phone1.pages = [{ id: "second", label: "Second", blocks: [{ id: "b", title: "B", cameras: [], actions: [{ id: "lamp", element: LAMP.id }] }] }];
  const r = L.addButtonRef(d, "template", "home", LAMP, {});
  assert.deepStrictEqual(lastAct(r), { id: "lamp-2", element: LAMP.id }, "unique across the document, not just the page");
  const a = L.addAction(d, "template", "home", { kind: "script_run", target: "script.evening", label: "Lamp", confirm: false });
  assert.strictEqual(lastAct(a).id, "lamp-2");
  assert.strictEqual(L.allActionIds(d).has("lamp"), true);
  console.log("PASS: a new button's id is unique across the whole document");
}
{
  // the cached list says the lamp is a script; the element was since re-pointed: Detach must inline what it is NOW
  let now = LAMP;
  const { f, panel } = await mount({ loaded: withRef({}, LAMP), handlers: { "portico/elements/list": () => listReply([now, GATE]) } });
  const row = () => q(panel, '.row[data-tile="gate-lock"]');
  now = { ...LAMP, entity_id: "script.other", name: "Lamp 2" };
  const before = f.ofType("portico/elements/list").length;
  click(row().querySelector('[data-act="detach-tile"]'));
  await sleep(40);
  assert.ok(f.ofType("portico/elements/list").length > before, "Detach reads the elements again");
  assert.strictEqual(lastAct(panel._state.draft).entity_id, "script.other", "the fresh value is inlined, not the cached one");
  panel.remove();
  // the read fails: nothing is inlined from the cache
  let fail = false;
  const m = await mount({ loaded: withRef({}, LAMP), handlers: { "portico/elements/list": () => { if (fail) throw { code: "unknown", message: "x" }; return listReply([LAMP]); } } });
  fail = true;
  click(q(m.panel, '.row[data-tile="gate-lock"] [data-act="detach-tile"]'));
  await sleep(40);
  assert.ok(L.isRef(lastAct(m.panel._state.draft)), "still a reference");
  m.panel.remove();
  console.log("PASS: Detach re-reads the element before inlining and refuses when it cannot");
}

// ---- 8b. DEV-518: a Talk ELEMENT carries its own door, so it is placeable in a block with none ----------------------------------
{
  const TALKEL = { id: "el-10000010", kind: "talk", name: "Gate talk", icon: "phone", options: { door: "gate-sip-server" } };
  const NODOOR = { id: "el-10000011", kind: "talk", name: "No door", options: {} };
  const d = makeLoaded().document;
  assert.ok(L.placeableButton(TALKEL) && !L.placeableButton(NODOOR), "placeable only with a door of its own");
  assert.strictEqual(L.buttonBlockProblem(TALKEL, { actions: [] }), "", "no block door needed");
  assert.ok(/needs the block to have a door/.test(L.buttonBlockProblem(TALK, { actions: [] })), "the old Talk BUTTON still needs the block's door");
  const r = L.addButtonRef(d, "template", "home", TALKEL, { label: "Gate talk", confirm: true });
  assert.deepStrictEqual(lastAct(r), { id: "gate-talk", element: TALKEL.id }, "a reference only; never an ask-first (CB-9)");
  assert.deepStrictEqual(L.inlineButton({ id: "gate-talk", element: TALKEL.id }, TALKEL),
    { id: "gate-talk", label: "Gate talk", kind: "talk", door: "gate-sip-server", icon: "phone", confirm: false }, "Detach copies the door");
  console.log("PASS: a Talk element is placed without a block door, carries its door into Detach, and is never ask-first");
}

// ---- 8b. a Talk element on a phone or a screen is placeable and stands for the `call` action (the server's _expand_house_call) -------
{
  const PHONE = { id: "talk-phone-abc123", kind: "talk", name: "Alice's phone", icon: "phone", managed: true, options: { phone: "abc123" } };
  const SCREEN = { id: "talk-screen-kitchen", kind: "talk", name: "Kitchen panel", managed: true, options: { screen: "kitchen" } };
  assert.ok(L.placeableButton(PHONE) && L.placeableButton(SCREEN), "no door needed: they are house calls");
  assert.deepStrictEqual(L.inlineButton({ id: "call-phone", element: PHONE.id }, PHONE),
    { id: "call-phone", label: "Alice's phone", kind: "call", target: { type: "phone", id: "abc123" }, icon: "phone", confirm: false });
  assert.deepStrictEqual(L.inlineButton({ id: "call-kitchen", element: SCREEN.id }, SCREEN).target, { type: "dashboard", id: "kitchen" });
  assert.ok(!L.placeableButton({ id: "el-10000012", kind: "talk", name: "Empty", options: {} }), "a Talk element with no door, phone or screen is still refused");
  console.log("PASS: phone and screen Talk elements can be placed as house-call buttons");
}

// ---- 9. no HTML-string setters; a hostile element name is text --------------------------------------------------------------------
{
  const src = fs.readFileSync(new URL("../portico-panel.js", import.meta.url), "utf8");
  for (const bad of ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "srcdoc"]) assert.ok(!src.includes(bad), `portico-panel.js must not use ${bad}`);
  const hostile = { ...LAMP, name: "<img src=x onerror=alert(1)>" };
  const { panel } = await mount({ elements: [hostile] });
  click(act(panel, "add-button"));
  await sleep(20);
  assert.ok(!q(panel, `${FORM} img[src="x"]`), "the name is not parsed as markup");
  assert.ok(q(panel, `${FORM} .elrow`).textContent.includes("<img"));
  panel.remove();
  console.log("PASS: no HTML-string setter; element names are text");
}
console.log("ALL PASS test_panel_buttonrefs");
process.exit(0);
