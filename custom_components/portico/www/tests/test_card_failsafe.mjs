// The kitchen-tablet overlay after a real gate call (gate_sip_observer, answered on a phone through the claim, ended by the phone).
// Alice: "the call window on the kitchen tablet did not close after a call" (2026-11-12 15:09, gate call 22 s).
//
// The ids of the real sequence, from the code: the transport's observer mints ONE id per call (`gso-<ns hex>-<n>-<rnd>`,
// transport-rs/src/driver/gatesipobserver/mod.rs mint_call_id) and uses it for `ringing`, `answered` and `ended`; the Python
// provider's `answer` adds a second `answered` for the same id (provider_impl.answer); the tracker's `ended` carries the tracker's
// call id (call_state.apply_event). So the ids are consistent and the events a connected card gets are
//   ringing gso-X, answered gso-X, answered gso-X, ended gso-X.
// THE STUCK VIEW (Alice's photo of the tablet): not the dark ring overlay but the light kiosk PAGE (title, live camera, the back rail).
// The overlay closed fine on `ended`; the automation had loaded the page for the call and nothing took the tablet back, live
// video running. So the card now leaves the page when a call it was brought up for is over (tests 7-9).
// What those cannot cover, and what the failsafe covers, is an `ended` the card never gets (a dropped socket, a page that opened
// after the answer, anything unforeseen): the card asks the server and lets go by itself.
import assert from "assert";
import { loadCard, sleep, makeHass, CONTENT, DOOR_NO_AUDIO } from "./card_loader.mjs";

const { dom, PorticoCard } = loadCard();
assert.ok(PorticoCard, "portico-card was not registered");
const doc = dom.window.document;
const overlays = () => doc.querySelectorAll("[data-portico-overlay]");
const door = DOOR_NO_AUDIO;
const GSO = "gso-18d2f4a1c3b2e10-7-9f3a11c2";
const ev = (fake, state, call) => fake.fireAll({ type: "call_state_changed", door_id: door.door_id, call_id: call, state });
const cam = (id) => ({ id, label: id, entity_id: `camera.${id}`, available: true, size: "big" });
const content = () => ({
  ...CONTENT,
  wake: { doors: [door.door_id], screen_call: true },
  callers: { [door.endpoint_id]: { name: "Gate", kind: "door", cameras: [cam("gate")], ring_buttons: [], call_buttons: [] },
             "*": { name: null, kind: null, cameras: [], ring_buttons: [], call_buttons: [] } },
});

// console.info lines of the cards
const logs = [];
dom.window.console.info = (...a) => logs.push(a.join(" "));
const origInfo = console.info;
console.info = (...a) => logs.push(a.join(" "));

// the server's word on the door, as the probe's snapshot
let server = null;
const probe = () => [{ type: "call_state_changed", door_id: door.door_id, call_id: server ? server.call : null, state: server ? server.state : "idle" }];

async function kiosk(cfg = {}, opts = {}) {
  logs.length = 0;
  const fake = makeHass({ doors: [door], content: content(), probe: opts.noProbe ? undefined : probe });
  const mk = (extra) => {
    const c = doc.createElement("portico-card");
    doc.body.appendChild(c);
    c.setConfig({ _pollMs: 20, _snapshotMs: 30, _ringSnapshotMs: 20, endpoint: "kitchen-tablet", takeover: true,
                  _failsafePollMs: 20, _failsafeProbeMs: 30, _failsafeLogMs: 0, ...cfg, ...extra });
    c.hass = fake.hass;
    return c;
  };
  const cards = [mk({ fullpage: true }), mk({ _ghost: true })];
  await sleep(80);
  return { fake, cards };
}
const done = (cards) => cards.forEach((c) => c.remove());

async function main() {
  // 1. the real sequence, ids as the observer sends them: closed by the `ended` event, and the log says so
  {
    server = null;
    const { fake, cards } = await kiosk();
    ev(fake, "ringing", GSO);
    assert.strictEqual(overlays().length, 1, "the ring opens one overlay");
    ev(fake, "answered", GSO);            // the observer's (poll saw inCall)
    ev(fake, "answered", GSO);            // and the provider's own, at the claim
    assert.strictEqual(overlays().length, 1, "open while the phone talks");
    ev(fake, "ended", GSO);
    await sleep(30);
    assert.strictEqual(overlays().length, 0, "closed by the ended event");
    assert.ok(logs.some((l) => l.includes("ended event") && l.includes(GSO)), "logged: " + JSON.stringify(logs));
    done(cards);
    console.log("PASS: the real sequence (same id throughout) closes on `ended` and logs the path and the id");
  }

  // 2. the tablet's page opens AFTER the answer (the HA automation loads it at the ring, a tablet needs seconds): the only
  //    word is the subscribe snapshot `answered`. Before the fix this card ignored it (no overlay, the kiosk page stayed up);
  //    now it holds the call as someone else's and the `ended` closes it.
  {
    server = { state: "answered", call: GSO };
    const fake = makeHass({ doors: [door], content: content(), probe });
    logs.length = 0;
    const mk = (extra) => {
      const c = doc.createElement("portico-card");
      doc.body.appendChild(c);
      c.setConfig({ _pollMs: 20, _snapshotMs: 30, _ringSnapshotMs: 20, endpoint: "kitchen-tablet", takeover: true,
                    _failsafePollMs: 20, _failsafeProbeMs: 30, _failsafeLogMs: 0, ...extra });
      c.hass = fake.hass;
      return c;
    };
    const cards = [mk({ fullpage: true }), mk({ _ghost: true })];
    await sleep(80);
    fake.fireAll({ type: "call_state_changed", snapshot: true, door_id: door.door_id, call_id: GSO, state: "answered" });
    assert.strictEqual(overlays().length, 1, "a page opened mid-call shows the call once (one overlay for two cards)");
    ev(fake, "ended", GSO);
    server = null;
    await sleep(30);
    assert.strictEqual(overlays().length, 0, "and closes when it ends");
    done(cards);
    console.log("PASS: a page opened after the answer holds the call as answered elsewhere and closes at its end");
  }

  // 3. a lost `ended`: the idle poll closes the overlay after two answers with no active call, and logs it with the ids
  {
    server = { state: "answered", call: GSO };
    const { fake, cards } = await kiosk();
    ev(fake, "ringing", GSO); ev(fake, "answered", GSO);
    assert.strictEqual(overlays().length, 1);
    await sleep(120);
    assert.strictEqual(overlays().length, 1, "the server says the call is active: stays open");
    server = null;                        // the call is over on the server; its `ended` never reaches this card
    await sleep(150);
    assert.strictEqual(overlays().length, 0, "closed by the idle poll");
    assert.ok(logs.some((l) => l.includes("idle poll") && l.includes(GSO)), "logged: " + JSON.stringify(logs));
    assert.ok(fake.probes.length >= 2, "it asked the server");
    assert.ok(fake.probeUnsubs.count >= 1, "and let go of each probe subscription");
    done(cards);
    console.log("PASS: a lost `ended` is made good by the idle poll (and it is logged)");
  }

  // 4. one idle answer between active ones does not close it, and a call that goes on is never closed (even past the cap)
  {
    server = { state: "active", call: GSO };
    const { fake, cards } = await kiosk({ _failsafeCapMs: 100, _failsafeStaleMs: 60 });
    ev(fake, "ringing", GSO); ev(fake, "answered", GSO);
    await sleep(70);
    // exactly one idle answer: wait for the next probe the card sends (not a wall-clock guess, which let a loaded host
    // fit two idle polls into the old 25 ms), answer it idle, and make the server active again before the one after it
    const sent = fake.probes.length;
    server = null;
    for (let i = 0; i < 500 && fake.probes.length === sent; i++) await sleep(1);
    assert.ok(fake.probes.length > sent, "the card probed again");
    server = { state: "active", call: GSO };
    await sleep(300);
    assert.strictEqual(overlays().length, 1, "still open: a call is genuinely active, the cap is not the mechanism");
    ev(fake, "ended", GSO);
    await sleep(30);
    assert.strictEqual(overlays().length, 0);
    done(cards);
    console.log("PASS: no close during an active call (one idle blip, and well past the cap with the server answering)");
  }

  // 5. the server cannot be asked: the cap closes it, and not before
  {
    server = null;
    const { fake, cards } = await kiosk({ _failsafeCapMs: 250, _failsafeStaleMs: 80 }, { noProbe: true });
    ev(fake, "ringing", GSO); ev(fake, "answered", GSO);
    await sleep(150);
    assert.strictEqual(overlays().length, 1, "before the cap it stays");
    await sleep(300);
    assert.strictEqual(overlays().length, 0, "closed by the cap");
    assert.ok(logs.some((l) => l.includes("cap") && l.includes(GSO)), "logged: " + JSON.stringify(logs));
    done(cards);
    console.log("PASS: with no answer from the server the cap closes the overlay");
  }

  // 6. a new ring while the overlay is open restarts the failsafe's idea of the call, and the next ring works
  {
    server = { state: "ringing", call: "gso-next" };
    const { fake, cards } = await kiosk();
    ev(fake, "ringing", GSO); ev(fake, "ended", GSO);
    await sleep(30);
    ev(fake, "ringing", "gso-next");
    await sleep(100);
    assert.strictEqual(overlays().length, 1, "the ring the server still has stays");
    server = null;
    await sleep(150);
    assert.strictEqual(overlays().length, 0, "and goes when the server has none");
    done(cards);
    console.log("PASS: a ring the server still holds is not closed; one it does not hold is");
  }

  // 7. THE PHOTO: the overlay closes and the kiosk PAGE (back rail, live tiles) must go with it: the card walks the back rail's way out
  {
    server = null;
    const pushed = [];
    const realPush = dom.window.history.pushState.bind(dom.window.history);
    dom.window.history.pushState = (a, b, path) => { pushed.push(path); return realPush(a, b, path); };
    const { fake, cards } = await kiosk({ back_path: "/screen" });
    ev(fake, "ringing", GSO); ev(fake, "answered", GSO);
    assert.strictEqual(pushed.length, 0, "nothing navigates during the call");
    ev(fake, "ended", GSO);
    await sleep(30);
    assert.strictEqual(overlays().length, 0, "overlay closed");
    assert.deepStrictEqual(pushed, ["/screen"], "and the page behind it is left, once, for the back path");
    assert.ok(logs.some((l) => l.includes("leaving the page")), "logged");
    done(cards);
    console.log("PASS: after the call the kiosk page itself is handed back (the stuck view of the photo)");

    // 8. a page somebody touched stays; so does one whose overlay was dismissed
    pushed.length = 0;
    const b = await kiosk({ back_path: "/screen" });
    b.cards[0].dispatchEvent(new dom.window.Event("pointerdown", { bubbles: true, composed: true }));
    ev(b.fake, "ringing", GSO + "2");
    ev(b.fake, "ended", GSO + "2");
    await sleep(30);
    // a ring after the touch re-arms (a fresh ring clears it), so touch DURING the call:
    ev(b.fake, "ringing", GSO + "3"); ev(b.fake, "answered", GSO + "3");
    b.cards[0].dispatchEvent(new dom.window.Event("pointerdown", { bubbles: true, composed: true }));
    ev(b.fake, "ended", GSO + "3");
    await sleep(30);
    assert.deepStrictEqual(pushed, ["/screen"], "only the untouched call left (the first); the touched one stayed");
    done(b.cards);
    pushed.length = 0;
    const c = await kiosk({ back_path: "/screen" });
    ev(c.fake, "ringing", GSO + "4"); ev(c.fake, "answered", GSO + "4");
    doc.querySelector("[data-portico-overlay]").shadowRoot.querySelector("#dismiss-btn").click();
    ev(c.fake, "ended", GSO + "4");
    await sleep(30);
    assert.deepStrictEqual(pushed, [], "a dismissed overlay means the page is wanted: stays");
    done(c.cards);
    console.log("PASS: a touched page, or a dismissed overlay, is not taken away");

    // 9. late page load (snapshot answered) then the end: also handed back; idle-poll path too
    pushed.length = 0;
    server = { state: "answered", call: GSO };
    const d = await kiosk({ back_path: "/screen" });
    d.fake.fireAll({ type: "call_state_changed", snapshot: true, door_id: door.door_id, call_id: GSO, state: "answered" });
    assert.strictEqual(overlays().length, 1);
    server = null;
    await sleep(200);
    assert.strictEqual(overlays().length, 0, "idle poll closed it");
    assert.deepStrictEqual(pushed, ["/screen"], "and handed the page back");
    done(d.cards);
    dom.window.history.pushState = realPush;
    console.log("PASS: a page opened mid-call is handed back when the idle poll closes it");
  }

  console.info = origInfo;
  process.exit(0);
}
main().catch((e) => { console.error(e); process.exit(1); });
