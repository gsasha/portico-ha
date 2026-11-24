// Screen wake (Devices D3, DEVICES-01..10): a takeover card opens the ring overlay only for a door its server `wake` names
// (matched with the names_door rule: bare device_id or <provider>:<device_id>, never the endpoint id as a string), a house call
// to itself always wakes it, an absent `wake` (phone, old schema) filters nothing, an empty one wakes for nothing, and the
// wake is re-read on content_changed and when the card is pointed at another screen. jsdom, the websocket mocked.
// NOT a real wall panel: see the PR for what stays unseen.
import assert from "assert";
import { loadCard, sleep, makeHass } from "./card_loader.mjs";

const { dom, PorticoCard } = loadCard();
assert.ok(PorticoCard, "portico-card was not registered");
const doc = dom.window.document;

const AUDIO = { InboundAudio: 0, OutboundAudio: 0, DirectCapable: true };
const FRONT = { door_id: "vto.example", endpoint_id: "dahua:vto.example", door_ref: "d-1", driver: "dahua_sip", capabilities: AUDIO, name: "Front door", camera_entity_id: null, available: true, away_media: false };
const SIDE = { door_id: "side.example", endpoint_id: "dahua:side.example", door_ref: "d-2", driver: "dahua_sip", capabilities: AUDIO, name: "Side door", camera_entity_id: null, available: true, away_media: false };
const content = (wake, endpoint = "kitchen", rev = "r1") => ({
  endpoint, kind: "dashboard", home_name: "Home", rev,
  pages: [{ id: "home", label: "Home", cameras: [], actions: [] }],
  callers: {
    "dahua:vto.example": { name: "Front door", kind: "door", cameras: [], ring_buttons: [], call_buttons: [] },
    "dahua:side.example": { name: "Side door", kind: "door", cameras: [], ring_buttons: [], call_buttons: [] },
    "*": { name: null, kind: null, cameras: [], ring_buttons: [], call_buttons: [] },
  },
  roster: [],
  ...(wake === undefined ? {} : { wake }),
});
const overlay = () => doc.querySelector("[data-portico-overlay]");
const houseOverlay = () => doc.querySelector("[data-portico-house]");
const ring = (fake, door, call = "c1") =>
  fake.fire({ type: "call_state_changed", door_id: door.door_id, call_id: call, state: "ringing", prev_state: "idle" });
const ended = (fake, door, call = "c1") =>
  fake.fire({ type: "call_state_changed", door_id: door.door_id, call_id: call, state: "ended", prev_state: "ringing" });

async function mount(wake, config = {}) {
  const fake = makeHass({ doors: [FRONT, SIDE], content: content(wake) });
  const card = doc.createElement("portico-card");
  doc.body.appendChild(card);
  card.setConfig({ _pollMs: 20, _snapshotMs: 30, _ringSnapshotMs: 20, endpoint: "kitchen", takeover: true, fullpage: true, ...config });
  card.hass = fake.hass;
  await sleep(50);
  return { card, fake };
}

async function expectRing(fake, door, shown, why) {
  ring(fake, door);
  await sleep(20);
  assert.strictEqual(!!overlay(), shown, why);
  ended(fake, door);
  await sleep(20);
  assert.ok(!overlay(), "the overlay is gone after the call ends");
}

async function main() {
  // 1. wake match / mismatch: a bare device_id ref and a namespaced ref both name the door; the other door does not wake
  {
    const { card, fake } = await mount({ doors: ["vto.example"], screen_call: true });
    await expectRing(fake, FRONT, true, "a bare device_id ref names the door: it wakes");
    await expectRing(fake, SIDE, false, "a door not in wake.doors does not wake the screen");
    card.remove();
    const b = await mount({ doors: ["dahua:side.example"], screen_call: true });
    await expectRing(b.fake, SIDE, true, "a <provider>:<device_id> ref names the door");
    await expectRing(b.fake, FRONT, false, "the other door does not");
    b.card.remove();
    console.log("PASS: a ring wakes the screen only for a door wake.doors names (names_door rule); a non-matching ring does nothing");
  }

  // 2. the names_door rule, not string equality: another provider's `b`, and the endpoint id of a bare ref
  {
    const { card, fake } = await mount({ doors: ["other:vto.example", "dahua:dahua:vto.example", "vto"], screen_call: true });
    await expectRing(fake, FRONT, false, "another provider's namespace, a doubled prefix and a partial id name nothing");
    card.remove();
    console.log("PASS: refs that only look similar do not match");
  }

  // 3. absent wake = today's behaviour exactly: every ring takes over
  {
    const { card, fake } = await mount(undefined);
    await expectRing(fake, FRONT, true, "no wake: the ring takes over");
    await expectRing(fake, SIDE, true, "no wake: any door does");
    card.remove();
    console.log("PASS: an absent wake filters nothing (phones, schema 4/5, unmigrated store)");
  }

  // 4. empty wake (a screen with no Talk element, or only a conflicted one): no door wakes it
  {
    const { card, fake } = await mount({ doors: [], screen_call: true });
    await expectRing(fake, FRONT, false, "an empty wake.doors wakes nothing");
    await expectRing(fake, SIDE, false, "not for any door");
    card.remove();
    console.log("PASS: an empty wake (also what a Talk element in a conflict gives the server) wakes no screen for a door call");
  }

  // 5. a non-takeover card keeps its own inline ring whatever the wake says
  {
    const { card, fake } = await mount({ doors: [], screen_call: true }, { takeover: false, fullpage: false });
    ring(fake, FRONT);
    await sleep(20);
    assert.ok(card.shadowRoot.querySelector("#hangup-btn, #answer-btn, #decline-btn"), "the inline ring UI is shown");
    card.remove();
    console.log("PASS: a non-takeover card is not filtered");
  }

  // 6. house call to itself: always wakes (screen_call), even with no door in wake
  {
    const { card, fake } = await mount({ doors: [], screen_call: true });
    fake.fire({ type: "house_call", call_id: "hso-9", state: "ringing", from: "ep-alice", to: "kitchen" });
    await sleep(20);
    assert.ok(houseOverlay(), "an incoming house call opens the house overlay on a screen with an empty door wake");
    card.remove();
    const c = await mount({ doors: [], screen_call: false });
    c.fake.fire({ type: "house_call", call_id: "hso-9", state: "ringing", from: "ep-alice", to: "kitchen" });
    await sleep(20);
    assert.ok(!houseOverlay(), "a wake that denies screen_call silences an incoming house call");
    c.card.remove();
    const d = await mount(undefined);
    d.fake.fire({ type: "house_call", call_id: "hso-9", state: "ringing", from: "ep-alice", to: "kitchen" });
    await sleep(20);
    assert.ok(houseOverlay(), "no wake: as today");
    d.card.remove();
    console.log("PASS: a house call to this screen wakes it (screen_call); absent wake as today");
  }

  // 7. content_changed carries the new wake: used at once, and the content is refetched for a new revision
  {
    const { card, fake } = await mount({ doors: ["vto.example"], screen_call: true });
    await expectRing(fake, SIDE, false, "side door does not wake at first");
    fake.fire({ type: "content_changed", endpoint: "kitchen", rev: "r1", wake: { doors: ["vto.example", "side.example"], screen_call: true } });
    await expectRing(fake, SIDE, true, "the event's wake is applied at once (same rev: no refetch needed)");
    fake.fire({ type: "content_changed", endpoint: "kitchen", rev: "r1", wake: { doors: [], screen_call: true } });
    await expectRing(fake, FRONT, false, "a shrunk wake stops waking");
    // the refetch path: a new rev re-reads content/get, whose wake is authoritative
    const gets = fake.ofType("portico/content/get").length;
    fake.fire({ type: "content_changed", endpoint: "kitchen", rev: "r2" });
    await sleep(30);
    assert.strictEqual(fake.ofType("portico/content/get").length, gets + 1, "a new revision refetches");
    await expectRing(fake, FRONT, true, "after the refetch the content/get wake (vto.example) is the one in force");
    // another screen's event leaves our wake alone
    fake.fire({ type: "content_changed", endpoint: "other-screen", rev: "x", wake: { doors: [], screen_call: true } });
    await expectRing(fake, FRONT, true, "another endpoint's wake is not ours");
    card.remove();
    console.log("PASS: content_changed updates the wake (event at once, content/get on a new rev); another screen's does not");
  }

  // 8. the wake is dropped when the card is pointed at another screen and re-read
  {
    const { card, fake } = await mount({ doors: [], screen_call: true });
    await expectRing(fake, FRONT, false, "empty wake: no wake");
    const before = fake.ofType("portico/content/get").length;
    card.setConfig({ _pollMs: 20, endpoint: "kitchen2", takeover: true, fullpage: true });
    await sleep(40);
    assert.strictEqual(fake.ofType("portico/content/get").length, before + 1, "a new endpoint refetches");
    assert.strictEqual(fake.ofType("portico/content/get").pop().endpoint, "kitchen2");
    card.remove();
    console.log("PASS: pointing the card at another screen re-reads its wake");
  }

  console.log("test_card_wake: all passed");
  process.exit(0);
}
main().catch((e) => { console.error(e); process.exit(1); });
