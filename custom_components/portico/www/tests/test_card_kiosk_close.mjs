// The kitchen-tablet kiosk screen (/portico/kiosk/<slug>): the ring overlay stays open DURING a call someone else took
// (the cameras are the point) and closes by itself when the call is over.
// Reproduces Alice's report after a real gate call: the overlay neither closed when he answered on his phone nor when the
// call ended. Causes: (1) the later `active` event turned "answered elsewhere" into a phantom "in a call" of this card;
// (2) the card kept the RING's call id and ignored an `ended` that carries the live call's id. jsdom, websocket mocked.
import assert from "assert";
import { loadCard, sleep, makeHass, CONTENT, DOOR_NO_AUDIO } from "./card_loader.mjs";

const { dom, PorticoCard } = loadCard();
assert.ok(PorticoCard, "portico-card was not registered");
const doc = dom.window.document;
const overlays = () => doc.querySelectorAll("[data-portico-overlay]");
const overlay = () => overlays()[0] || null;
const door = DOOR_NO_AUDIO;
const ev = (fake, state, call) => fake.fire({ type: "call_state_changed", door_id: door.door_id, call_id: call, state });
const cam = (id, size) => ({ id, label: id, entity_id: `camera.${id}`, available: true, size });
const content = (cameras) => ({
  ...CONTENT,
  wake: { doors: [door.door_id], screen_call: true },
  callers: { [door.endpoint_id]: { name: "Gate", kind: "door", cameras, ring_buttons: [], call_buttons: [] },
             "*": { name: null, kind: null, cameras: [], ring_buttons: [], call_buttons: [] } },
});

// the kiosk page: the panel's fullpage card AND the every-page listener's hidden card, both on the same endpoint
async function kiosk(cameras, order) {
  const fake = makeHass({ doors: [door], content: content(cameras) });
  const mk = (cfg) => {
    const c = doc.createElement("portico-card");
    doc.body.appendChild(c);
    c.setConfig({ _pollMs: 20, _snapshotMs: 30, _ringSnapshotMs: 20, endpoint: "kitchen-tablet", takeover: true, ...cfg });
    c.hass = fake.hass;
    return c;
  };
  const cards = order === "ghost-first" ? [mk({ _ghost: true }), mk({ fullpage: true })] : [mk({ fullpage: true }), mk({ _ghost: true })];
  await sleep(80);
  return { fake, cards };
}

async function main() {
  // 1. the whole call, with the same call id throughout and with the live call carrying another id than the ring did
  for (const order of ["panel-first", "ghost-first"]) {
    for (const [ring, live] of [["c1", "c1"], ["c1", "c9"]]) {
      const { fake, cards } = await kiosk([cam("gate", "big")], order);
      ev(fake, "ringing", ring);
      assert.strictEqual(overlays().length, 1, `${order}: one overlay at the ring`);
      ev(fake, "answered", live);
      assert.strictEqual(overlays().length, 1, `${order}: still open after it was answered elsewhere`);
      ev(fake, "active", live);
      await sleep(150);
      assert.strictEqual(overlays().length, 1, `${order}: open during the call`);
      const text = overlay().shadowRoot.textContent;
      assert.ok(text.includes("Answered elsewhere"), "says it is someone else's call");
      assert.ok(!overlay().shadowRoot.querySelector("#hangup-btn") && !overlay().shadowRoot.querySelector("#answer-btn"), "no Answer or Hang up on a call that is not ours");
      assert.ok(overlay().shadowRoot.querySelector("img[data-camera=gate]"), "the camera is drawn during the call");
      ev(fake, "ended", live);
      await sleep(30);
      assert.strictEqual(overlays().length, 0, `${order} ring ${ring} live ${live}: closed when the call ended`);
      cards.forEach((c) => c.remove());
    }
  }
  console.log("PASS: kiosk (panel card + listener card): open during a call answered elsewhere, closed at its end (also when the live call's id differs from the ring's)");

  // 2. the ring ends unanswered (caller gives up): closed
  {
    const { fake, cards } = await kiosk([cam("gate", "big")], "panel-first");
    ev(fake, "ringing", "u1");
    assert.strictEqual(overlays().length, 1);
    ev(fake, "ended", "u1");
    await sleep(30);
    assert.strictEqual(overlays().length, 0, "closed when the ring ends unanswered");
    cards.forEach((c) => c.remove());
    console.log("PASS: a ring that ends unanswered closes the overlay");
  }

  // 3. the next ring after a call that was kept open shows again, and a stale `active` after `ended` opens nothing
  {
    const { fake, cards } = await kiosk([cam("gate", "big")], "panel-first");
    ev(fake, "ringing", "a1"); ev(fake, "answered", "a1"); ev(fake, "ended", "a1");
    await sleep(30);
    assert.strictEqual(overlays().length, 0);
    ev(fake, "active", "a1");
    assert.strictEqual(overlays().length, 0, "a late event of the ended call opens nothing");
    ev(fake, "ringing", "a2");
    assert.strictEqual(overlays().length, 1, "the next ring takes the screen again");
    assert.ok(overlay().shadowRoot.querySelector("#dismiss-btn"));
    ev(fake, "ended", "a2");
    await sleep(30);
    assert.strictEqual(overlays().length, 0);
    cards.forEach((c) => c.remove());
    console.log("PASS: after an ended call a late event opens nothing, and the next ring shows");
  }

  // 4. Dismiss during the call keeps it closed until it ends
  {
    const { fake, cards } = await kiosk([cam("gate", "big")], "panel-first");
    ev(fake, "ringing", "d1"); ev(fake, "answered", "d1");
    overlay().shadowRoot.querySelector("#dismiss-btn").click();
    assert.strictEqual(overlays().length, 0, "Close closes it");
    ev(fake, "active", "d1");
    await sleep(30);
    assert.strictEqual(overlays().length, 0, "and it stays closed while the call goes on");
    ev(fake, "ended", "d1");
    cards.forEach((c) => c.remove());
    console.log("PASS: Close during the call stays closed");
  }

  process.exit(0);
}
main().catch((e) => { console.error(e); process.exit(1); });
