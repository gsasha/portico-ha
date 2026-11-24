// The camera picker shows each feed's picture size and frame rate (a spec chip). The pure chip text (`logic.specChip`)
// and the panel in jsdom with a fake `hass` that answers `portico/cameras/info`: which cameras it asks for (only the
// ones on screen, in batches, once), what the chips and the picker's options say, and that a failing command leaves
// the picker working. Protocol-level; NOT a visual check (see panel_harness.html).
import assert from "assert";
import { loadPanel, editorHass, makeLoaded, sleep, STATES } from "./panel_loader.mjs";

const { dom, Panel, doc } = loadPanel();
const win = dom.window;
const L = Panel.logic;
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

async function mount(fake) {
  const panel = doc.createElement("portico-panel");
  panel._previewMs = 10;
  panel.hass = fake.hass;
  doc.body.appendChild(panel);
  await sleep(50);
  return panel;
}

/* The server's answer, as camera_info.py gives it for the cameras in `table`: id -> [width, height, fps, source]. */
const TABLE = {
  "camera.gate": [1600, 1200, 20, "attributes"],
  "camera.garage": [1920, 1080, null, "still"],
  "camera.zebra": [null, null, null, "unavailable"],
};
const infoHandler = (table = TABLE) => (msg) => ({
  cameras: msg.entity_ids.map((id) => {
    const [width, height, fps, source] = table[id] || [640, 480, null, "still"];
    return { entity_id: id, width, height, fps, source };
  }),
});
const asked = (f) => f.ofType("portico/cameras/info").flatMap((m) => m.entity_ids);

// 1. the chip text: size, frame rate or "fps unknown", the dash while loading or on failure, the tooltip says where it is from
{
  const ok = L.specChip({ width: 1600, height: 1200, fps: 20, source: "attributes" });
  assert.strictEqual(ok.text, "1600x1200 · 20 fps");
  assert.strictEqual(ok.state, "ok");
  assert.ok(ok.title.includes("stream attributes") && ok.title.includes("about 1.9 MP"), ok.title);
  assert.ok(!ok.title.includes("does not report"), "a known frame rate needs no apology");
  const still = L.specChip({ width: 1920, height: 1080, fps: null, source: "still" });
  assert.strictEqual(still.text, "1920x1080 · fps unknown");
  assert.ok(still.title.includes("picture the camera returns now"), still.title);
  assert.ok(still.title.includes("Home Assistant does not report this feed's frame rate"), still.title);
  assert.ok(still.title.includes("about 2.1 MP"), still.title);
  assert.strictEqual(L.specChip({ width: 3840, height: 2160, fps: 12.5, source: "attributes" }).text, "3840x2160 · 12.5 fps");
  // DEV-766: the live stream's size wins over the still's, and the tooltip says so
  const live = L.specChip({ width: 640, height: 480, fps: null, source: "still", stream: { width: 2560, height: 1440 } });
  assert.strictEqual(live.text, "2560x1440 · fps unknown");
  assert.ok(live.title.includes("live stream") && live.title.includes("about 3.7 MP"), live.title);
  assert.ok(!live.title.includes("picture the camera returns now"), live.title);
  assert.strictEqual(L.specChip({ width: 640, height: 480, fps: null, source: "still", stream: { width: 0, height: 9 } }).text, "640x480 · fps unknown", "a bad stream falls back to the still");
  assert.strictEqual(L.specChip({ width: null, height: null, fps: null, source: "failed", stream: { width: 1280, height: 720 } }).text, "1280x720 · fps unknown");
  assert.ok(L.specChip({ width: 4000, height: 3000, fps: null, source: "still" }).title.includes("about 12 MP"), "large pictures drop the decimal");
  // Frigate's measured rate (DEV-663): the chip shows it, the tooltip names the source and says it is the detect stream's
  const fr = L.specChip({ width: 1280, height: 720, fps: 14.5, source: "attributes", fps_source: "frigate-stats",
                          detect: { fps: 5, width: 1280, height: 720 } });
  assert.strictEqual(fr.text, "1280x720 · 14.5 fps");
  assert.ok(fr.title.includes("Frigate's measured rate") && fr.title.includes("detect stream") && fr.title.includes("not necessarily the recording stream"), fr.title);
  assert.ok(fr.title.includes("set to 1280x720 at 5 fps"), fr.title);
  assert.ok(!fr.title.includes("does not report"), fr.title);
  const frNoDetect = L.specChip({ width: 1280, height: 720, fps: 15, source: "still", fps_source: "frigate-stats" });
  assert.ok(frNoDetect.title.includes("Frigate's measured rate") && !frNoDetect.title.includes("is set to"), frNoDetect.title);
  assert.ok(!ok.title.includes("Frigate"), "a rate from Home Assistant does not claim Frigate");
  const frUnknown = L.specChip({ width: 1280, height: 720, fps: null, source: "attributes", fps_source: "frigate-stats", detect: { fps: 5, width: 1, height: 1 } });
  assert.strictEqual(frUnknown.text, "1280x720 · fps unknown");
  assert.ok(!frUnknown.title.includes("Frigate"), "no rate, no Frigate claim");
  for (const info of [undefined, null]) {
    const loading = L.specChip(info);
    assert.deepStrictEqual([loading.text, loading.state], ["—", "loading"]);
  }
  for (const info of [{ failed: true }, { source: "failed", width: null, height: null, fps: null }, { source: "unavailable", width: null, height: null, fps: null }]) {
    const none = L.specChip(info);
    assert.deepStrictEqual([none.text, none.state], ["—", "none"]);
  }
  assert.ok(L.specChip({ source: "unavailable" }).title.includes("unavailable"));
  // a size that is not a positive number is a dash, never "NaNxNaN"; a bad frame rate is "fps unknown", never guessed
  assert.strictEqual(L.specChip({ width: "1600", height: 1200, fps: 20 }).text, "—");
  assert.strictEqual(L.specChip({ width: 0, height: 1200, fps: 20 }).text, "—");
  assert.strictEqual(L.specChip({ width: 640, height: 480, fps: 0 }).text, "640x480 · fps unknown");
  assert.strictEqual(L.specChip({ width: 640, height: 480, fps: "20" }).text, "640x480 · fps unknown");
  console.log("PASS: the spec chip says size and fps, 'fps unknown' when not reported, a dash while loading or on failure");
}

// 2. the chosen camera rows of a block show the chip, asked once for the cameras on screen
{
  const f = editorHass({ "portico/cameras/info": infoHandler() });
  const panel = await mount(f);
  assert.deepStrictEqual(asked(f), ["camera.gate"], "only the camera in the block on screen is asked about");
  const chip = q(panel, '.pagedetail .row[data-tile="gate"] [data-spec="camera.gate"]');
  assert.ok(chip, "the camera row has a chip");
  assert.strictEqual(chip.textContent, "1600x1200 · 20 fps");
  assert.ok(chip.title.includes("stream attributes"));
  // a re-render (a tab, an edit) costs no call
  click(act(panel, "tab-devices"));
  click(act(panel, "tab-pages"));
  assert.strictEqual(f.ofType("portico/cameras/info").length, 1, "known cameras are not asked again");
  assert.strictEqual(q(panel, '.pagedetail [data-spec="camera.gate"]').textContent, "1600x1200 · 20 fps", "and the chip is still there after the redraw");
  console.log("PASS: a block's camera rows show the chip; the cameras on screen are asked once");
  panel.remove();
}

// 3. the picker: options say their spec once known; only the cameras listed are asked; the chosen feed has a chip
{
  const f = editorHass({ "portico/cameras/info": infoHandler() });
  const panel = await mount(f);
  click(act(panel, "add-camera"));
  await sleep(10);
  assert.deepStrictEqual(asked(f).sort(), ["camera.garage", "camera.gate", "camera.zebra"].sort(), "the cameras in the open picker are asked about");
  assert.strictEqual(f.ofType("portico/cameras/info").length, 2, "one call for the page's camera row, one for the picker's others");
  assert.deepStrictEqual(f.ofType("portico/cameras/info")[1].entity_ids.sort(), ["camera.garage", "camera.zebra"], "the row's camera is not asked twice");
  const optionText = (value) => q(panel, `${FORM} option[value="${value}"]`).textContent;
  assert.strictEqual(optionText("camera.gate"), "Gate camera (camera.gate) — 1600x1200 · 20 fps");
  assert.strictEqual(optionText("camera.garage"), "Garage camera (camera.garage) — 1920x1080 · fps unknown");
  assert.strictEqual(optionText("camera.zebra"), "camera.zebra", "an unavailable camera's option keeps its plain name");
  assert.ok(q(panel, `${FORM} option[value="camera.garage"]`).title.includes("picture the camera returns now"), "the option's tooltip says where it comes from");
  const choice = q(panel, `${FORM} [data-spec-for="target"]`);
  assert.ok(choice.hidden, "nothing chosen yet, no chip");
  setValue(q(panel, `${FORM} [data-field="target"]`), "camera.garage", "change");
  assert.ok(!choice.hidden);
  assert.strictEqual(choice.textContent, "1920x1080 · fps unknown");
  assert.ok(choice.title.includes("does not report this feed's frame rate"));
  setValue(q(panel, `${FORM} [data-field="target"]`), "camera.zebra", "change");
  assert.strictEqual(choice.textContent, "—", "an unavailable camera is a dash");
  assert.ok(choice.title.includes("unavailable"));
  // the label still follows the choice (the option text is longer now; the name comes from data-name)
  assert.strictEqual(q(panel, `${FORM} [data-field="label"]`).value, "camera.zebra");
  setValue(q(panel, `${FORM} [data-field="target"]`), "camera.garage", "change");
  assert.strictEqual(q(panel, `${FORM} [data-field="label"]`).value, "Garage camera", "the name is the camera's, without the spec");
  click(act(panel, "dialog-ok"));
  click(act(panel, "save"));
  await sleep(40);
  const saved = f.ofType("portico/content/save")[0].document.template.pages[0].blocks[0].cameras;
  assert.deepStrictEqual(saved.map((c) => [c.label, c.entity_id]), [["Gate", "camera.gate"], ["Garage camera", "camera.garage"]], "no spec text leaks into the document");
  assert.ok(!JSON.stringify(f.ofType("portico/content/save")[0]).includes("fps"), "nothing about fps is saved");
  console.log("PASS: the picker's options and the chosen feed show the spec; the saved document is unchanged");
  panel.remove();
}

// 4. the filter narrows the list; a camera not yet asked about is asked when it appears (not before)
{
  const states = { ...STATES };
  for (let n = 0; n < 25; n++) states[`camera.cam${String(n).padStart(2, "0")}`] = { state: "idle", attributes: { friendly_name: `Cam ${String(n).padStart(2, "0")}` } };
  const f = editorHass({ "portico/cameras/info": infoHandler() }, { states });
  const panel = await mount(f);
  assert.deepStrictEqual(asked(f), ["camera.gate"]);
  click(act(panel, "add-camera"));
  await sleep(10);
  const batches = f.ofType("portico/cameras/info").map((m) => m.entity_ids.length);
  assert.ok(batches.every((n) => n <= L.SPEC_BATCH), `no call over ${L.SPEC_BATCH} ids: ${batches}`);
  const options = qa(panel, `${FORM} option[data-cam]`);
  assert.strictEqual(options.length, 28);
  const before = asked(f).length;
  assert.strictEqual(before, 1 + L.SPEC_BATCH, "the row's camera and the first 20 options of the picker, not all 28");
  assert.ok(!asked(f).includes("camera.zebra"), "an option past the first 20 (camera.zebra sorts after Cam 24) is not asked about yet");
  setValue(q(panel, `${FORM} [data-field="filter"]`), "zebra");
  await sleep(10);
  assert.deepStrictEqual(f.ofType("portico/cameras/info").pop().entity_ids, ["camera.zebra"], "typing in the filter asks about what it reveals, and only that");
  setValue(q(panel, `${FORM} [data-field="filter"]`), "Gate");
  await sleep(10);
  assert.strictEqual(asked(f).length, before + 1, "a camera already known is not asked again");
  console.log("PASS: only the cameras visible in the picker are asked, in batches of at most 20, and the filter asks lazily");
  panel.remove();
}

// 5. the command fails (an older server, a dropped link): chips are a dash, the picker still works, the failure is not retried at once
{
  const f = editorHass({ "portico/cameras/info": () => { throw { code: "unknown_command", message: "Unknown command." }; } });
  const panel = await mount(f);
  const row = q(panel, '.pagedetail [data-spec="camera.gate"]');
  assert.strictEqual(row.textContent, "—");
  assert.strictEqual(row.dataset.specState, "none");
  click(act(panel, "add-camera"));
  await sleep(10);
  assert.strictEqual(qa(panel, `${FORM} option[value^="camera."]`).length, 3, "the list is complete");
  assert.strictEqual(q(panel, `${FORM} option[value="camera.gate"]`).textContent, "Gate camera (camera.gate)", "options keep their plain text");
  setValue(q(panel, `${FORM} [data-field="target"]`), "camera.garage", "change");
  assert.strictEqual(q(panel, `${FORM} [data-spec-for="target"]`).textContent, "—");
  click(act(panel, "dialog-ok"));
  assert.deepStrictEqual(qa(panel, ".pagedetail .row[data-tile] .label").map((n) => n.textContent).slice(0, 2), ["Gate", "Garage camera"], "the camera was added");
  const calls = f.ofType("portico/cameras/info").length;
  click(act(panel, "tab-devices"));
  click(act(panel, "tab-pages"));
  assert.strictEqual(f.ofType("portico/cameras/info").length, calls, "a failed ask is not repeated on every redraw");
  assert.strictEqual(panel._phase, "ready");
  // a reply with no row for a camera asked is a failure for that camera only
  const g = editorHass({ "portico/cameras/info": () => ({ cameras: [{ entity_id: "camera.gate", width: 800, height: 600, fps: 5, source: "attributes" }] }) });
  const p2 = await mount(g);
  click(act(p2, "add-camera"));
  await sleep(10);
  assert.strictEqual(q(p2, `${FORM} option[value="camera.gate"]`).textContent, "Gate camera (camera.gate) — 800x600 · 5 fps");
  assert.strictEqual(q(p2, `${FORM} option[value="camera.garage"]`).textContent, "Garage camera (camera.garage)");
  console.log("PASS: when the command fails the chips are dashes and the picker works");
  panel.remove();
  p2.remove();
}

// 6. a malformed reply is a failure, not an exception; a camera missing from hass.states is not asked about
{
  const f = editorHass({ "portico/cameras/info": () => ({ cameras: "nope" }) });
  const panel = await mount(f);
  assert.strictEqual(q(panel, '.pagedetail [data-spec="camera.gate"]').textContent, "—");
  panel.remove();

  const loaded = makeLoaded();
  loaded.document.template.pages[0].blocks[0].cameras.push({ id: "gone", label: "Gone", entity_id: "camera.removed" });
  const g = editorHass({ "portico/cameras/info": infoHandler() }, { loaded });
  const p2 = await mount(g);
  assert.deepStrictEqual(asked(g), ["camera.gate"], "a camera that is no longer in Home Assistant is not asked about");
  const gone = q(p2, '.pagedetail [data-spec="camera.removed"]');
  assert.strictEqual(gone.textContent, "—");
  assert.ok(gone.title.includes("unavailable"));
  console.log("PASS: a bad reply and a camera that is gone both leave a dash");
  p2.remove();
}

// 7. the Screens tab: the room camera rows and the add-a-camera picker show the spec too
{
  const loaded = makeLoaded();
  loaded.ha_users = [{ id: "u2", name: "Tablet", is_admin: false, kiosk_open: false }];
  loaded.document.dashboards = { kitchen: { name: "Kitchen tablet", ha_user_id: "u2", room: "Kitchen", cameras: ["camera.gate"] } };
  const f = editorHass({ "portico/cameras/info": infoHandler() }, { loaded });
  const panel = await mount(f);
  click(act(panel, "tab-devices"));
  await sleep(10);
  assert.strictEqual(q(panel, '[data-camera="camera.gate"] [data-spec="camera.gate"]').textContent, "1600x1200 · 20 fps");
  assert.strictEqual(q(panel, '[data-field="screen-camera-add"] option[value="camera.garage"]').textContent, "Garage camera (camera.garage) — 1920x1080 · fps unknown");
  console.log("PASS: the Screens tab's room cameras and picker show the spec");
  panel.remove();
}

// 8. the editing form of a chosen camera shows the chip; a non-administrator's panel never asks
{
  const f = editorHass({ "portico/cameras/info": infoHandler() });
  const panel = await mount(f);
  click(act(panel, "edit-tile"));
  assert.strictEqual(q(panel, `${FORM} [data-spec="camera.gate"]`).textContent, "1600x1200 · 20 fps");
  panel.remove();

  const g = editorHass({ "portico/cameras/info": infoHandler() }, { user: { is_admin: false } });
  const p2 = await mount(g);
  assert.strictEqual(g.ofType("portico/cameras/info").length, 0, "only an administrator asks");
  await p2._askCameras(["camera.gate"]);       // the command is admin only: the panel does not even try for anyone else
  assert.strictEqual(g.ofType("portico/cameras/info").length, 0, "asked directly, a non-administrator's panel still sends nothing");
  p2.remove();
  console.log("PASS: the edit form shows the chip; a non-administrator never asks");
}

console.log("ALL_PANEL_CAMSPEC_TESTS_PASSED");
