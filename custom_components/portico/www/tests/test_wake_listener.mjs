// The every-page wake listener (Devices D3b, DEVICES 4.5 K1/K2): WakeListener, at the end of portico-card.js, asks
// portico/kiosk/hello once, and only for a user with a declared screen mounts a hidden body-level card that obeys the
// screen's `wake` and draws the ordinary ring overlay without any dashboard card mounted. jsdom, the websocket mocked.
// NOT a real wall panel: Fully Kiosk's screensaver and the chime past the autoplay rule are unseen (see the PR).
import assert from "assert";
import { loadCard, sleep, makeHass } from "./card_loader.mjs";

const { dom, PorticoCard } = loadCard();
const win = dom.window;
const doc = win.document;
const WakeListener = PorticoCard.WakeListener;
assert.ok(WakeListener, "PorticoCard.WakeListener is exported");

const AUDIO = { InboundAudio: 0, OutboundAudio: 0, DirectCapable: true };
const FRONT = { door_id: "vto.example", endpoint_id: "dahua:vto.example", door_ref: "d-1", driver: "dahua_sip", capabilities: AUDIO, name: "Front door", camera_entity_id: null, available: true, away_media: false };
const SIDE = { door_id: "side.example", endpoint_id: "dahua:side.example", door_ref: "d-2", driver: "dahua_sip", capabilities: AUDIO, name: "Side door", camera_entity_id: null, available: true, away_media: false };
const content = (wake, endpoint = "kitchen") => ({
  endpoint, kind: "dashboard", home_name: "Home", rev: "r1",
  pages: [{ id: "home", label: "Home", cameras: [], actions: [] }],
  callers: {
    "dahua:vto.example": { name: "Front door", kind: "door", cameras: [], ring_buttons: [], call_buttons: [] },
    "dahua:side.example": { name: "Side door", kind: "door", cameras: [], ring_buttons: [], call_buttons: [] },
    "*": { name: null, kind: null, cameras: [], ring_buttons: [], call_buttons: [] },
  },
  roster: [],
  ...(wake === undefined ? {} : { wake }),
});
const overlays = () => doc.querySelectorAll("[data-portico-overlay]");
const houses = () => doc.querySelectorAll("[data-portico-house]");
const wakeHosts = () => doc.querySelectorAll("[data-portico-wake-host]");

/** A hass for one logged-in user: makeHass plus portico/kiosk/hello and any number of subscribers per type. */
function setup({ wake, declared = ["kitchen"], helloError, user = { id: "u-kitchen" } } = {}) {
  const fake = makeHass({ doors: [FRONT, SIDE], content: content(wake) });
  const conn = fake.hass.connection;
  const subs = {};
  const send = conn.sendMessagePromise;
  conn.sendMessagePromise = async (msg) => {
    if (msg.type === "portico/kiosk/hello") {
      fake.sent.push(msg);
      if (helloError) throw helloError;
      return { user: { id: user.id, name: "Kitchen" }, declared, endpoint_ok: declared.length > 0 };
    }
    return send(msg);
  };
  const sub = conn.subscribeMessage;
  conn.subscribeMessage = async (cb, msg, o) => {
    (subs[msg.type] = subs[msg.type] || []).push(cb);
    const un = await sub(cb, msg, o);
    return () => { subs[msg.type] = subs[msg.type].filter((x) => x !== cb); un(); };
  };
  fake.hass.user = user;
  fake.all = (event) => {
    const type = event.type === "content_changed" ? "portico/content/subscribe" : "portico/calls/subscribe";
    for (const cb of [...(subs[type] || [])]) cb(event);
  };
  fake.subCount = () => Object.values(subs).reduce((n, l) => n + l.length, 0);
  return fake;
}

let current = null;
const listener = (fake, extra = {}) => {
  current = fake.hass;
  return new WakeListener({ getHass: () => current, tryMs: 5, watchMs: 40, ...extra });
};
const ring = (fake, door, call = "c1") =>
  fake.all({ type: "call_state_changed", door_id: door.door_id, call_id: call, state: "ringing", prev_state: "idle" });
const ended = (fake, door, call = "c1") =>
  fake.all({ type: "call_state_changed", door_id: door.door_id, call_id: call, state: "ended", prev_state: "ringing" });
const houseRing = (fake, call = "h1", extra = {}) =>
  fake.all({ type: "house_call", call_id: call, state: "ringing", from: "ep-alice", to: "kitchen", ...extra });

async function expectRing(fake, door, shown, why, call = "c1") {
  ring(fake, door, call);
  await sleep(25);
  assert.strictEqual(overlays().length, shown ? 1 : 0, why);
  ended(fake, door, call);
  await sleep(25);
  assert.strictEqual(overlays().length, 0, "the overlay is gone with the call");
}

async function main() {
  // 1. no declared screen -> no subscription, no host; an error from hello -> nothing either
  {
    const fake = setup({ declared: [] });
    const l = listener(fake);
    l.start();
    await sleep(60);
    assert.strictEqual(fake.ofType("portico/kiosk/hello").length, 1, "hello is asked exactly once");
    assert.strictEqual(fake.ofType("portico/kiosk/hello")[0].endpoint, undefined, "hello is asked with no endpoint");
    assert.strictEqual(fake.subCount(), 0, "no subscription for a user with no screen");
    assert.strictEqual(wakeHosts().length, 0, "no body-level host");
    assert.strictEqual(fake.ofType("portico/content/get").length, 0, "no content read either");
    l.stop();
    const bad = setup({ helloError: { code: "unknown_command", message: "no" } });
    const l2 = listener(bad);
    l2.start();
    await sleep(60);
    assert.strictEqual(bad.subCount(), 0, "a failing hello leaves no subscription");
    assert.strictEqual(wakeHosts().length, 0);
    l2.stop();
    // hass not handed over yet: it waits, bounded, and asks nobody
    const none = setup({});
    current = null;
    const l3 = new WakeListener({ getHass: () => current, tryMs: 2, tries: 5 });
    l3.start();
    await sleep(40);
    assert.strictEqual(none.ofType("portico/kiosk/hello").length, 0, "no hass, no hello");
    current = none.hass;
    await sleep(30);
    assert.strictEqual(none.ofType("portico/kiosk/hello").length, 0, "it gave up after its bounded tries");
    l3.stop();
    console.log("PASS: no declared screen / hello error / no hass -> no subscription, no host, hello at most once");
  }

  // 2. match / mismatch / empty / absent, with NO dashboard card mounted
  {
    const fake = setup({ wake: { doors: ["vto.example"], screen_call: true } });
    const l = listener(fake);
    l.start();
    await sleep(60);
    assert.strictEqual(wakeHosts().length, 1, "one hidden host for the declared screen");
    assert.strictEqual(fake.ofType("portico/content/get")[0].endpoint, "kitchen", "it reads the DECLARED screen's content");
    assert.ok(fake.ofType("portico/content/subscribe").length === 1 && fake.ofType("portico/calls/subscribe").length === 1);
    const order = fake.sent.map((m) => m.type);
    assert.ok(order.indexOf("portico/content/get") < order.indexOf("portico/calls/subscribe"), "the wake is read before the call snapshot can arrive");
    await expectRing(fake, FRONT, true, "a door named by wake.doors opens the overlay with no card mounted");
    await expectRing(fake, SIDE, false, "a door not named does not");
    // a refreshed wake arrives on content_changed
    fake.all({ type: "content_changed", endpoint: "kitchen", rev: "r1", wake: { doors: ["dahua:side.example"], screen_call: true } });
    await expectRing(fake, SIDE, true, "content_changed moves the wake");
    await expectRing(fake, FRONT, false, "and the old door no longer wakes it");
    fake.all({ type: "content_changed", endpoint: "kitchen", rev: "r1", wake: { doors: [], screen_call: true } });
    await expectRing(fake, FRONT, false, "an empty wake never wakes");
    await expectRing(fake, SIDE, false, "an empty wake never wakes (2)");
    assert.strictEqual(fake.ofType("portico/content/snapshot").length, 0, "the hidden card fetched no page pictures");
    l.stop();

    const absent = setup({ wake: undefined });
    const la = listener(absent);
    la.start();
    await sleep(60);
    await expectRing(absent, FRONT, false, "an ABSENT wake wakes nothing body-level (a mounted card would; the listener never)");
    absent.all({ type: "content_changed", endpoint: "kitchen", rev: "r1" });
    await expectRing(absent, FRONT, false, "a content_changed without wake keeps it that way");
    la.stop();
    console.log("PASS: match wakes with no mounted card; mismatch, empty and absent wake never");
  }

  // 3. house calls: to this screen with screen_call -> overlay; screen_call off, no wake, another screen, outgoing -> none
  {
    const fake = setup({ wake: { doors: [], screen_call: true } });
    const l = listener(fake);
    l.start();
    await sleep(60);
    houseRing(fake);
    await sleep(25);
    assert.strictEqual(houses().length, 1, "a house call to this screen opens the house overlay body-level");
    fake.all({ type: "house_call", call_id: "h1", state: "ended", from: "ep-alice", to: "kitchen", reason: "cancelled" });
    await sleep(25);
    assert.strictEqual(houses().length, 0, "it closes with the call");
    houseRing(fake, "h2", { to: "hall" });
    fake.all({ type: "house_call", call_id: "h3", state: "ringing", from: "kitchen", to: "ep-alice" });
    await sleep(25);
    assert.strictEqual(houses().length, 0, "another screen's call and a call the screen placed itself are not shown by the listener");
    fake.all({ type: "content_changed", endpoint: "kitchen", rev: "r1", wake: { doors: [], screen_call: false } });
    houseRing(fake, "h4");
    await sleep(25);
    assert.strictEqual(houses().length, 0, "screen_call false: a house call does not wake it");
    l.stop();
    const none = setup({ wake: undefined });
    const ln = listener(none);
    ln.start();
    await sleep(60);
    houseRing(none, "h5");
    await sleep(25);
    assert.strictEqual(houses().length, 0, "absent wake: a house call does not wake it either");
    ln.stop();
    console.log("PASS: a house call wakes the screen only when it is for it and wake.screen_call is on");
  }

  // 4. de-dup: a mounted card on the same screen + the listener's card -> one overlay, and it closes with the call
  {
    for (const mountFirst of [true, false]) {
      const fake = setup({ wake: { doors: ["vto.example"], screen_call: true } });
      const mount = async () => {
        const card = doc.createElement("portico-card");
        doc.body.appendChild(card);
        card.setConfig({ _pollMs: 20, _snapshotMs: 30, _ringSnapshotMs: 20, endpoint: "kitchen", takeover: true, fullpage: true });
        card.hass = fake.hass;
        await sleep(50);
        return card;
      };
      let card;
      const l = listener(fake);
      if (mountFirst) { card = await mount(); l.start(); await sleep(60); } else { l.start(); await sleep(60); card = await mount(); }
      assert.strictEqual(wakeHosts().length, 1);
      assert.strictEqual(fake.subCount(), 4, "both cards subscribe (content and calls)");
      ring(fake, FRONT, "dup");
      await sleep(30);
      assert.strictEqual(overlays().length, 1, `one ring overlay for one call (mounted ${mountFirst ? "first" : "second"})`);
      ended(fake, FRONT, "dup");
      await sleep(30);
      assert.strictEqual(overlays().length, 0, "it closes with the call");
      // a house call: one overlay too
      houseRing(fake, "hd");
      await sleep(30);
      assert.strictEqual(houses().length, 1, "one house overlay for one house call");
      fake.all({ type: "house_call", call_id: "hd", state: "ended", from: "ep-alice", to: "kitchen", reason: "cancelled" });
      await sleep(30);
      assert.strictEqual(houses().length, 0);
      // answered by somebody else: one notice, not two
      ring(fake, FRONT, "dup2");
      await sleep(30);
      assert.strictEqual(overlays().length, 1, "a second call: one overlay");
      fake.all({ type: "call_state_changed", door_id: FRONT.door_id, call_id: "dup2", state: "answered", prev_state: "ringing" });
      await sleep(30);
      assert.strictEqual(overlays().length, 1, "answered elsewhere: one notice, not two");
      ended(fake, FRONT, "dup2");
      await sleep(30);
      assert.ok(overlays().length <= 1, "and still at most one after it ends (the notice fades on its own timer)");
      card.remove();
      l.stop();
    }
    console.log("PASS: a mounted card and the listener's card show a call once (either mounted first), closed with the call");
  }

  // 5. K2: navigate only with a configured path
  {
    const fake = setup({ wake: { doors: ["vto.example"], screen_call: true } });
    const pushes = [];
    const realPush = win.history.pushState.bind(win.history);
    win.history.pushState = (a, b, url) => { pushes.push(url); realPush(a, b, url); };
    let located = 0;
    win.addEventListener("location-changed", () => { located++; });
    // no path ever configured -> the overlay opens on this page and nothing navigates
    const l = listener(fake);
    l.start();
    await sleep(60);
    await expectRing(fake, FRONT, true, "no path: the overlay still opens on the current page");
    assert.strictEqual(pushes.length, 0, "no path configured: nothing navigates");
    assert.strictEqual(located, 0);
    // a mounted card with `path` remembers it for the listener
    const card = doc.createElement("portico-card");
    doc.body.appendChild(card);
    card.setConfig({ endpoint: "kitchen", takeover: false, path: "/lovelace-kitchen/wall" });
    card.remove();
    ring(fake, FRONT, "k2");
    await sleep(25);
    assert.deepStrictEqual(pushes, ["/lovelace-kitchen/wall"], "a configured path is navigated to once, before the overlay");
    assert.strictEqual(located, 1, "with a location-changed event");
    assert.strictEqual(overlays().length, 1);
    assert.strictEqual(win.location.pathname, "/lovelace-kitchen/wall");
    ended(fake, FRONT, "k2");
    await sleep(25);
    // already on that view: no second navigation
    ring(fake, FRONT, "k3");
    await sleep(25);
    assert.strictEqual(pushes.length, 1, "already on the view: nothing to navigate");
    ended(fake, FRONT, "k3");
    await sleep(25);
    // a mismatching door does not navigate
    win.history.pushState(null, "", "/lovelace/other");
    pushes.length = 0;
    ring(fake, SIDE, "k4");
    await sleep(25);
    assert.strictEqual(pushes.length, 0, "a ring that does not wake the screen does not move the page");
    ended(fake, SIDE, "k4");
    // a hostile path is never used
    win.localStorage.setItem("portico-wake-path:kitchen", "//evil.example/x");
    ring(fake, FRONT, "k5");
    await sleep(25);
    assert.strictEqual(pushes.length, 0, "a protocol-relative path is not navigated to");
    ended(fake, FRONT, "k5");
    // a server-sent wake.path wins over the remembered one
    win.localStorage.setItem("portico-wake-path:kitchen", "/lovelace-kitchen/wall");
    fake.all({ type: "content_changed", endpoint: "kitchen", rev: "r1", wake: { doors: ["vto.example"], screen_call: true, path: "/dash/talk" } });
    ring(fake, FRONT, "k6");
    await sleep(25);
    assert.deepStrictEqual(pushes, ["/dash/talk"], "wake.path from the server, when sent, is the target");
    ended(fake, FRONT, "k6");
    win.history.pushState = realPush;
    l.stop();
    console.log("PASS: K2 navigates only with a valid configured path, once, and only for a ring that wakes the screen");
  }

  // 6. cleanup: stop(), pagehide, logout
  {
    const fake = setup({ wake: { doors: ["vto.example"], screen_call: true } });
    const l = listener(fake);
    l.start();
    await sleep(60);
    ring(fake, FRONT, "z1");
    await sleep(25);
    assert.strictEqual(overlays().length, 1);
    assert.ok(fake.subCount() > 0);
    l.stop();
    assert.strictEqual(wakeHosts().length, 0, "stop removes the host");
    assert.strictEqual(overlays().length, 0, "and an overlay that was up");
    assert.strictEqual(fake.subCount(), 0, "and every subscription");
    assert.ok(!l.running);

    const f2 = setup({ wake: { doors: ["vto.example"], screen_call: true } });
    const l2 = listener(f2);
    l2.start();
    await sleep(60);
    win.dispatchEvent(new win.Event("pagehide"));
    await sleep(20);
    assert.strictEqual(wakeHosts().length, 0, "pagehide stands the listener down");
    assert.strictEqual(f2.subCount(), 0);

    const f3 = setup({ wake: { doors: ["vto.example"], screen_call: true } });
    const l3 = listener(f3);
    l3.start();
    await sleep(60);
    assert.strictEqual(wakeHosts().length, 1);
    current = { ...f3.hass, user: undefined };           // logout: HA has no user any more
    await sleep(100);
    assert.strictEqual(wakeHosts().length, 0, "logout stands it down");
    assert.strictEqual(f3.subCount(), 0);
    const f4 = setup({ wake: { doors: ["vto.example"], screen_call: true } });
    const l4 = listener(f4);
    l4.start();
    await sleep(60);
    current = { ...f4.hass, user: { id: "someone-else" } };
    await sleep(100);
    assert.strictEqual(wakeHosts().length, 0, "a different user ends it too");
    // start twice is one listener
    const f5 = setup({ wake: { doors: [], screen_call: true } });
    const l5 = listener(f5);
    l5.start(); l5.start();
    await sleep(60);
    assert.strictEqual(wakeHosts().length, 1, "start() twice makes one host");
    assert.strictEqual(f5.ofType("portico/kiosk/hello").length, 1);
    l5.stop();
    console.log("PASS: stop, pagehide and logout remove the host and every subscription; start is idempotent");
  }

  console.log("test_wake_listener: all passed");
  process.exit(0);
}
main().catch((e) => { console.error(e); process.exit(1); });
