// The editor's live video settings: a Live checkbox on every camera tile (the additive boolean `live`, stored only when on),
// the advisory warning when more than 2 tiles of a page are live, the document's `show_fps`, the client-side validation that
// mirrors the server, and the preview showing live tiles as such. Protocol-level, jsdom; NOT a visual check.
import assert from "assert";
import { loadPanel, editorHass, makeLoaded, sleep, STATES } from "./panel_loader.mjs";

const { dom, Panel, doc } = loadPanel();
const win = dom.window;
const L = Panel.logic;
const fresh = () => L.createState(makeLoaded());
const throwsEdit = (fn, re) => assert.throws(fn, (e) => e instanceof L.EditError && re.test(e.message));
const q = (p, sel) => p.shadowRoot.querySelector(sel);
const qa = (p, sel) => Array.from(p.shadowRoot.querySelectorAll(sel));
const act = (p, name) => q(p, `[data-act="${name}"]`);
const click = (node) => { assert.ok(node, "clicked something that is not there"); node.dispatchEvent(new win.Event("click", { bubbles: true })); };
function setChecked(node, on) {
  assert.ok(node, "ticked something that is not there");
  node.checked = on;
  node.dispatchEvent(new win.Event("change", { bubbles: true }));
}
function setValue(node, value, type = "input") {
  node.value = value;
  node.dispatchEvent(new win.Event(type, { bubbles: true }));
}
async function mount(fake) {
  const panel = doc.createElement("portico-panel");
  panel._previewMs = 10;
  panel.hass = fake.hass;
  doc.body.appendChild(panel);
  await sleep(50);
  return panel;
}

/* A loaded document whose template page has one block of five cameras (none live). */
function camerasLoaded(extra = {}) {
  const loaded = makeLoaded();
  const page = loaded.document.template.pages[0];
  page.blocks = [{ id: "home", title: "Home", cameras: ["c0", "c1", "c2", "c3", "c4"].map((id) => ({ id, label: `Cam ${id}`, entity_id: `camera.${id}`, ...(extra[id] || {}) })), actions: [] }];
  return loaded;
}
const STATES5 = { ...STATES, ...Object.fromEntries(["c0", "c1", "c2", "c3", "c4"].map((id) => [`camera.${id}`, { state: "idle", attributes: { friendly_name: `Cam ${id}` } }])) };
const WARN = '[data-state="too-many-live"]';
const liveBox = (p, id) => q(p, `[data-tile="${id}"] [data-field="camera-live"]`);
const savedCameras = (f) => f.ofType("portico/content/save").at(-1).document.template.pages[0].blocks[0].cameras;

async function main() {
  // 1. the pure part: `live` is stored only when on; a non-boolean is refused; the count and the threshold
  {
    const s = fresh();
    const on = L.setCameraLive(s.draft, "template", "home", "gate", true);
    assert.strictEqual(L.pagesOf(on, "template")[0].blocks[0].cameras[0].live, true);
    const off = L.setCameraLive(on, "template", "home", "gate", false);
    assert.ok(!("live" in L.pagesOf(off, "template")[0].blocks[0].cameras[0]), "off removes the key: absent is false");
    assert.deepStrictEqual(off, s.draft, "on and off again is the same document");
    throwsEdit(() => L.updateTile(s.draft, "template", "home", "cameras", "gate", { live: "yes" }), /on or off/);
    throwsEdit(() => L.updateTile(s.draft, "template", "home", "cameras", "gate", { live: 1 }), /on or off/);
    throwsEdit(() => L.updateTile(s.draft, "template", "home", "cameras", "nope", { live: true }), /gone/);
    // it is for cameras only: a button patch ignores it
    const a = L.updateTile(s.draft, "template", "home", "actions", "open-gate", { live: true });
    assert.ok(!("live" in L.pagesOf(a, "template")[0].blocks[0].actions[0]), "a button never gets live");
    // the threshold: more than 2
    assert.strictEqual(L.LIVE_WARN_AFTER, 2);
    const four = camerasLoaded();
    let d = four.document;
    const counts = [];
    for (const id of ["c0", "c1", "c2", "c3"]) {
      d = L.setCameraLive(d, "template", "home", id, true);
      counts.push([L.liveCount(L.pagesOf(d, "template")[0]), L.tooManyLive(L.pagesOf(d, "template")[0])]);
    }
    assert.deepStrictEqual(counts, [[1, false], [2, false], [3, true], [4, true]], "2 is fine, 3 is too many");
    // counted across the blocks of one page
    const two = L.clone(d);
    const pg = L.pagesOf(two, "template")[0];
    pg.blocks = [{ id: "a", title: "A", cameras: pg.blocks[0].cameras.slice(0, 2), actions: [] }, { id: "b", title: "B", cameras: pg.blocks[0].cameras.slice(2, 3), actions: [] }];
    assert.strictEqual(L.liveCount(pg), 3);
    assert.strictEqual(L.tooManyLive(pg), true);
    console.log("PASS: live is stored only when on, refused when not a boolean, for cameras only; more than 2 live tiles on a page is too many");
  }

  // 2. show_fps: stored only when on
  {
    const s = fresh();
    assert.strictEqual(L.setShowFps(s.draft, true).show_fps, true);
    assert.ok(!("show_fps" in L.setShowFps(L.setShowFps(s.draft, true), false)));
    assert.ok(!("show_fps" in L.saveRequest(s).document), "a document without it goes out without it");
    console.log("PASS: show_fps is stored only when on");
  }

  // 3. validation mirrors the server's: a strict boolean for live and show_fps
  {
    const s = fresh();
    assert.deepStrictEqual(L.validate(L.setShowFps(L.setCameraLive(s.draft, "template", "home", "gate", true), true)), []);
    const bad = L.clone(s.draft);
    L.pagesOf(bad, "template")[0].blocks[0].cameras[0].live = "yes";
    assert.ok(L.validate(bad).some((p) => /Live setting that is not on or off/.test(p)), L.validate(bad).join("|"));
    const zero = L.clone(s.draft);
    L.pagesOf(zero, "template")[0].blocks[0].cameras[0].live = 0;
    assert.ok(L.validate(zero).some((p) => /Live setting that is not on or off/.test(p)), "0 is not false");
    const fps = L.clone(s.draft);
    fps.show_fps = "yes";
    assert.ok(L.validate(fps).some((p) => /Show frame rate must be on or off/.test(p)));
    fps.show_fps = false;
    assert.deepStrictEqual(L.validate(fps), [], "false is a boolean: valid (the server stores nothing for it)");
    console.log("PASS: validation refuses a live or show_fps that is not a boolean, as the server does");
  }

  // 4. the UI: a checkbox on every camera tile, the setting is the tile's state, the save carries `live` only when on
  {
    const f = editorHass({}, { loaded: camerasLoaded({ c1: { live: true } }), states: STATES5 });
    const panel = await mount(f);
    assert.strictEqual(qa(panel, '.block [data-field="camera-live"]').length, 5, "every camera tile has one");
    assert.deepStrictEqual(qa(panel, '.block [data-field="camera-live"]').map((n) => n.checked), [false, true, false, false, false], "it shows the tile's state");
    for (const n of qa(panel, '.block [data-field="camera-live"]')) assert.ok(n.getAttribute("aria-label"), "it has an accessible name");
    assert.ok(q(panel, '[data-hint="live"]').textContent.length > 20, "a short hint says what Live does");
    assert.ok(/fallback/.test(q(panel, '[data-hint="live"]').textContent));
    setChecked(liveBox(panel, "c0"), true);
    assert.strictEqual(q(panel, "[data-status]").dataset.status, "dirty");
    setChecked(liveBox(panel, "c1"), false);
    click(act(panel, "save"));
    await sleep(40);
    const cams = savedCameras(f);
    assert.deepStrictEqual(cams.map((c) => [c.id, c.live]), [["c0", true], ["c1", undefined], ["c2", undefined], ["c3", undefined], ["c4", undefined]]);
    for (const c of cams) assert.ok(c.live === true || !("live" in c), "stored only when true");
    console.log("PASS: every camera tile has a Live checkbox showing its state; a save carries live only when on");
    panel.remove();
  }

  // 5. the warning: absent at 2, present above 2, gone when one comes off; it is only advice
  {
    const f = editorHass({}, { loaded: camerasLoaded({ c0: { live: true } }), states: STATES5 });
    const panel = await mount(f);
    assert.ok(!q(panel, WARN));
    setChecked(liveBox(panel, "c1"), true);
    assert.ok(!q(panel, WARN), "two live tiles: no warning");
    setChecked(liveBox(panel, "c2"), true);
    const w = q(panel, WARN);
    assert.ok(w, "three: the warning");
    assert.strictEqual(w.textContent, L.LIVE_WARN_TEXT);
    assert.ok(/overload the tablet or the camera server/.test(w.textContent) && /Show frame rate/.test(w.textContent) && /take cameras off live/.test(w.textContent));
    setChecked(liveBox(panel, "c3"), true);
    setChecked(liveBox(panel, "c4"), true);
    assert.ok(q(panel, WARN) && qa(panel, WARN).length === 1, "five: still one warning, and no cap");
    assert.strictEqual(qa(panel, '.block [data-field="camera-live"]').filter((n) => n.checked).length, 5, "nothing stops the fifth");
    assert.notStrictEqual(act(panel, "save").disabled, true, "advice does not block the save");
    setChecked(liveBox(panel, "c4"), false);
    setChecked(liveBox(panel, "c3"), false);
    setChecked(liveBox(panel, "c2"), false);
    assert.ok(!q(panel, WARN), "back to two: the warning goes");
    click(act(panel, "undo"));
    assert.ok(q(panel, WARN), "undo brings the third back and the warning with it");
    console.log("PASS: the warning shows above 2 live tiles of a page and not at 2; it blocks nothing");
    panel.remove();
  }

  // 6. the add and edit forms carry the setting too
  {
    const f = editorHass({}, { loaded: camerasLoaded({ c1: { live: true } }), states: STATES5 });
    const panel = await mount(f);
    click(q(panel, '[data-tile="c1"] [data-act="edit-tile"]'));
    assert.strictEqual(q(panel, '[data-dialog="cameras"] [data-field="live"]').checked, true, "the form shows the tile's state");
    setChecked(q(panel, '[data-dialog="cameras"] [data-field="live"]'), false);
    click(act(panel, "dialog-ok"));
    assert.strictEqual(liveBox(panel, "c1").checked, false, "saved from the form");
    click(act(panel, "add-camera"));
    const pick = q(panel, '[data-dialog="cameras"] select');
    const option = Array.from(pick.options).find((o) => o.value === "camera.garage") || Array.from(pick.options).find((o) => o.value);
    pick.value = option.value;
    pick.dispatchEvent(new win.Event("change", { bubbles: true }));
    setChecked(q(panel, '[data-dialog="cameras"] [data-field="live"]'), true);
    click(act(panel, "dialog-ok"));
    const added = L.pagesOf(panel._state.draft, "template")[0].blocks[0].cameras.at(-1);
    assert.strictEqual(added.live, true, "a camera added with Live on is live");
    console.log("PASS: the add and edit forms show and set Live");
    panel.remove();
  }

  // 7. show_fps: no box on the Pages tab (it is on the Settings tab); the document field still round-trips untouched
  {
    const loaded = camerasLoaded();
    loaded.document.show_fps = true;
    const f2 = editorHass({}, { loaded, states: STATES5 });
    const p2 = await mount(f2);
    assert.ok(!q(p2, '[data-field="show-fps"]'), "no frame-rate box on the Pages tab");
    assert.strictEqual(p2._state.draft.show_fps, true, "a stored setting stays in the draft");
    for (const sel of ["ep:phone1", "ep:kitchen", "template"]) {
      click(q(p2, `[data-sel="${sel}"]`));
      assert.ok(!q(p2, '[data-field="show-fps"]'), `${sel}: no box`);
    }
    setChecked(liveBox(p2, "c0"), true);
    click(act(p2, "save"));
    await sleep(40);
    assert.strictEqual(f2.ofType("portico/content/save").at(-1).document.show_fps, true, "an edit elsewhere keeps show_fps");
    // a document without it goes out byte-identical to before
    const f3 = editorHass({}, { loaded: camerasLoaded(), states: STATES5 });
    const p3 = await mount(f3);
    setChecked(liveBox(p3, "c0"), true);
    setChecked(liveBox(p3, "c0"), false);
    assert.strictEqual(q(p3, "[data-status]").dataset.status, "saved", "a Live on and off again leaves the document as it was: nothing to save");
    assert.deepStrictEqual(L.saveRequest(p3._state).document, L.saveRequest(L.createState(camerasLoaded())).document, "and nothing different on the wire");
    assert.ok(!("show_fps" in L.saveRequest(p3._state).document));
    console.log("PASS: show_fps has no box on the Pages tab and still round-trips with the document");
    p2.remove(); p3.remove();
  }

  // 8. the preview shows live tiles as such
  {
    const f = editorHass({}, { loaded: camerasLoaded({ c0: { live: true }, c2: { live: true } }), states: STATES5 });
    const panel = await mount(f);
    await sleep(60);
    const live = qa(panel, "[data-preview] .cam.live");
    assert.strictEqual(live.length, 2);
    assert.deepStrictEqual(live.map((n) => n.querySelector(".livebadge").textContent), ["LIVE", "LIVE"]);
    assert.strictEqual(qa(panel, "[data-preview] .cam").length, 5);
    assert.strictEqual(qa(panel, "[data-preview] .livebadge").length, 2, "only the live ones");
    setChecked(liveBox(panel, "c3"), true);
    await sleep(60);
    assert.strictEqual(qa(panel, "[data-preview] .livebadge").length, 3, "the preview follows the checkbox");
    console.log("PASS: the preview marks live tiles LIVE and follows the checkbox");
    panel.remove();
  }
  console.log("ALL_PANEL_LIVE_TESTS_PASSED");
}

main().catch((err) => { console.error("TEST FAILED:", err); process.exit(1); });
