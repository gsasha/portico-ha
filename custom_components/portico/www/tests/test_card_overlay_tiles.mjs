// The ring overlay's camera tiles (the kitchen tablet's kiosk screen): a big camera has a row of its own, small ones are half the
// width, two per row (Alice: the small views were tiny on the tablet). The phone layout is not this overlay and is untouched.
import assert from "assert";
import { loadCard, sleep, makeHass, CONTENT, DOOR_NO_AUDIO } from "./card_loader.mjs";

const { dom, PorticoCard } = loadCard();
assert.ok(PorticoCard, "portico-card was not registered");
const doc = dom.window.document;
const door = DOOR_NO_AUDIO;
const overlay = () => doc.querySelector("[data-portico-overlay]");
const cam = (id, size) => ({ id, label: id, entity_id: `camera.${id}`, available: true, size });

async function main() {
  const cameras = [cam("gate", "big"), cam("drive", "small"), cam("street", "small"), cam("yard", "small")];
  const content = { ...CONTENT,
    callers: { [door.endpoint_id]: { name: "Gate", kind: "door", cameras, ring_buttons: [], call_buttons: [] } } };
  const fake = makeHass({ doors: [door], content });
  const card = doc.createElement("portico-card");
  doc.body.appendChild(card);
  card.setConfig({ _pollMs: 20, _snapshotMs: 30, _ringSnapshotMs: 20, endpoint: "kitchen-tablet", takeover: true, fullpage: true });
  card.hass = fake.hass;
  await sleep(60);
  const ev = (state, call) => fake.fire({ type: "call_state_changed", door_id: door.door_id, call_id: call, state });
  {
    ev("ringing", "s1");
    const pics = Array.from(overlay().shadowRoot.querySelectorAll(".pics .pic"));
    assert.deepStrictEqual(pics.map((p) => p.classList.contains("small")), [false, true, true, true]);
    const css = overlay().shadowRoot.querySelector("style").textContent;
    assert.ok(/\.pics \.pic\.small \{[^}]*calc\(50% - 4px\)/.test(css), "small tiles are half a row");
    assert.ok(/\.pics \{[^}]*flex-flow: row wrap/.test(css), "tiles wrap into rows");
    ev("ended", "s1");
    card.remove();
    console.log("PASS: overlay camera tiles: big alone on a row, small ones two per row");
  }
  // a camera with no size (an old server) stays a big tile
  {
    const fake2 = makeHass({ doors: [door], content: { ...CONTENT, callers: { [door.endpoint_id]: { name: "Gate", kind: "door",
      cameras: [{ id: "gate", label: "gate", entity_id: "camera.gate", available: true }], ring_buttons: [], call_buttons: [] } } } });
    const c2 = doc.createElement("portico-card");
    doc.body.appendChild(c2);
    c2.setConfig({ _pollMs: 20, endpoint: "kitchen-tablet", takeover: true });
    c2.hass = fake2.hass;
    await sleep(60);
    fake2.fire({ type: "call_state_changed", door_id: door.door_id, call_id: "n1", state: "ringing" });
    assert.ok(!overlay().shadowRoot.querySelector(".pic.small"), "no size: not small");
    c2.remove();
    console.log("PASS: a camera without a size stays a full-row tile");
  }
  process.exit(0);
}
main().catch((e) => { console.error(e); process.exit(1); });
