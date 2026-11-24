// The card as a declared screen on contract 3 (M6 K2, E4): config `endpoint`, `fullpage`, the overlay built from
// `callers`, the door -> callers key (`door.endpoint_id`), the 1.5 s stills with their in-flight guard, the
// second-ring refusal (R7-19), the version probe (R7-02). Also pins a phone user's card WITHOUT `endpoint` (its own
// endpoint, the same callers). jsdom, nothing mocked that the card does not itself call. NOT a visual check.
import assert from "assert";
import { loadCard, sleep, makeHass } from "./card_loader.mjs";

const { dom, PorticoCard, state } = loadCard();
assert.ok(PorticoCard, "portico-card was not registered");
const doc = dom.window.document;

const AUDIO = { InboundAudio: 0, OutboundAudio: 0, DirectCapable: true };
const NO_AUDIO = { InboundAudio: 3, OutboundAudio: 3, DirectCapable: false };
const FRONT = { door_id: "vto.example", endpoint_id: "dahua:vto.example", door_ref: "d-1", driver: "dahua_sip", capabilities: AUDIO, name: "Front door", camera_entity_id: null, available: true, away_media: false };
const GATE = { door_id: "gate.example", endpoint_id: "gate_sip:gate.example", door_ref: "d-2", driver: "gate_sip_observer", capabilities: NO_AUDIO, name: "Vehicle gate", camera_entity_id: null, available: true, away_media: false };

const act = (id, label, kind = "portico_open", extra = {}) => ({ id, label, kind, confirm: false, available: true, name: label, ...extra });
const cam = (id, label, available = true) => ({ id, label, entity_id: "camera." + id, available, name: label });

function screenContent(over = {}) {
  const home = { id: "home", label: "Home", cameras: [cam("p-yard", "Yard")], actions: [act("a-evening", "Evening", "scene_activate")] };
  const second = { id: "garden", label: "Garden", cameras: [cam("p-lawn", "Lawn")], actions: [act("a-water", "Water", "script_run")] };
  return {
    endpoint: "kitchen", kind: "dashboard", home_name: "Home", rev: "r1",
    pages: [home, second],
    callers: {
      "dahua:vto.example": {
        name: "Front door", kind: "door",
        cameras: [{ id: "c-aaaaaaaaaaaa", label: "Front door", name: "Front cam", available: true }],
        ring_buttons: [act("a-front", "Open front", "lock_open", { confirm: true })],
        call_buttons: [act("a-call-open", "Open during call")],
      },
      "gate_sip:gate.example": {
        name: "Vehicle gate", kind: "door",
        cameras: [{ id: "c-bbbbbbbbbbbb", label: "Vehicle gate", name: "Gate cam", available: true },
                  { id: "c-cccccccccccc", label: "Vehicle gate 2", name: "Gate cam 2", available: false }],
        ring_buttons: [act("a-gate", "Open gate")],
        call_buttons: [],
      },
      "*": { name: null, kind: null, cameras: [], ring_buttons: [], call_buttons: [] },
    },
    roster: [],
    ...over,
  };
}

/** The shared contract-3 fake (card_loader.mjs), with this file's default doors and screen document. */
function makeScreenHass({ doors = [FRONT, GATE], content = screenContent(), ...rest } = {}) {
  return makeHass({ doors, content, ...rest });
}

async function mount(config, opts) {
  const fake = makeScreenHass(opts);
  const card = doc.createElement("portico-card");
  doc.body.appendChild(card);
  card.setConfig({ _pollMs: 20, _snapshotMs: 30, _ringSnapshotMs: 20, ...config });
  card.hass = fake.hass;
  await sleep(50);
  return { card, fake };
}
const SCREEN = { endpoint: "kitchen", takeover: true, fullpage: true };
const overlay = () => doc.querySelector("[data-portico-overlay]");
const oroot = () => overlay() && overlay().shadowRoot;
const obtn = () => Array.from(oroot().querySelectorAll("button")).map((b) => b.textContent);
const q = (card, sel) => card.shadowRoot.querySelector(sel);
const qa = (card, sel) => Array.from(card.shadowRoot.querySelectorAll(sel));
const ring = (fake, door, call = "c1") =>
  fake.fire({ type: "call_state_changed", door_id: door.door_id, call_id: call, state: "ringing", prev_state: "idle" });
// contract 3: a call ends with `ended` carrying its call id (no idle echo)
const ended = (fake, door, call = "c1") =>
  fake.fire({ type: "call_state_changed", door_id: door.door_id, call_id: call, state: "ended", prev_state: "ringing" });

async function main() {
  // 1. a screen: version first, then doors, the two subscriptions, then content/get {endpoint} and nothing named profile
  {
    const { card, fake } = await mount(SCREEN, {});
    const types = fake.sent.map((m) => m.type);
    assert.strictEqual(types[0], "portico/version", "the probe goes first");
    assert.deepStrictEqual(types.slice(0, 5), ["portico/version", "portico/doors", "portico/calls/subscribe", "portico/content/subscribe", "portico/content/get"]);
    assert.deepStrictEqual(fake.ofType("portico/content/subscribe"), [{ type: "portico/content/subscribe", endpoint: "kitchen" }]);
    assert.deepStrictEqual(fake.ofType("portico/content/get"), [{ type: "portico/content/get", endpoint: "kitchen" }]);
    assert.ok(fake.sent.every((m) => !("profile" in m)), "no request names a profile");
    assert.ok(fake.subscribed(), "both subscriptions are held");
    assert.deepStrictEqual(qa(card, ".cam .label").map((e) => e.textContent), ["Yard"], "the first page");
    assert.deepStrictEqual(qa(card, "button.act").map((b) => b.textContent), ["Evening"]);
    assert.deepStrictEqual(qa(card, "button.tab").map((b) => b.textContent), ["Home", "Garden"], "paged by label");
    assert.ok(card.hasAttribute("fullpage"), "fullpage is on the host");
    await sleep(60);
    const snaps = fake.ofType("portico/content/snapshot");
    assert.ok(snaps.length >= 1 && snaps.every((m) => m.camera_id === "p-yard" && m.endpoint === "kitchen" && !("profile" in m)),
      "stills of the visible page only, by id, with the endpoint");
    assert.strictEqual(q(card, "img[data-camera=p-yard]").getAttribute("src"), "data:image/jpeg;base64,QUJD");
    console.log("PASS: a screen probes the version, reads doors, subscribes twice, reads content/get {endpoint}, draws the first page and its stills");

    qa(card, "button.tab").find((b) => b.dataset.page === "garden").click();
    await sleep(60);
    assert.deepStrictEqual(qa(card, ".cam .label").map((e) => e.textContent), ["Lawn"]);
    assert.deepStrictEqual(qa(card, "button.act").map((b) => b.textContent), ["Water"]);
    assert.ok(fake.ofType("portico/content/snapshot").some((m) => m.camera_id === "p-lawn" && m.endpoint === "kitchen"));
    // a refetch keeps the page chosen
    fake.fire({ type: "content_changed", endpoint: "kitchen", rev: "r2" });
    await sleep(30);
    assert.deepStrictEqual(qa(card, ".cam .label").map((e) => e.textContent), ["Lawn"], "the chosen page survives a refresh");
    console.log("PASS: a page tab switches the page and its stills; a refresh keeps it");

    // a button runs by id with the endpoint, never a profile
    qa(card, "button.act")[0].click();
    await sleep(10);
    assert.deepStrictEqual(fake.ofType("portico/content/run"), [{ type: "portico/content/run", action_id: "a-water", endpoint: "kitchen" }]);
    console.log("PASS: a page button runs by id with the endpoint");
    card.remove();
  }

  // 2. content_changed: ours and the install-wide one refetch; another endpoint's and an unchanged rev do not
  {
    const { card, fake } = await mount(SCREEN, {});
    const gets = () => fake.ofType("portico/content/get").length;
    assert.strictEqual(gets(), 1);
    fake.fire({ type: "content_changed", endpoint: "other-screen", rev: null });
    await sleep(15);
    assert.strictEqual(gets(), 1, "another endpoint's change is not ours");
    fake.fire({ type: "content_changed", endpoint: "kitchen", rev: "r1" });
    await sleep(15);
    assert.strictEqual(gets(), 1, "a revision we hold needs no refetch");
    fake.fire({ type: "content_changed", endpoint: "kitchen", rev: "r2" });
    await sleep(15);
    assert.strictEqual(gets(), 2, "ours, new revision: refetch");
    fake.fire({ type: "content_changed", endpoint: null, rev: null });
    await sleep(15);
    assert.strictEqual(gets(), 3, "an install-wide change (endpoint null): refetch");
    console.log("PASS: content_changed refetches for this endpoint (and the install-wide one), not for another's or the same rev");
    card.remove();
  }

  // 3. the door -> callers key, and the overlay built from the entry
  {
    const { card, fake } = await mount(SCREEN, {});
    assert.ok(!overlay(), "no overlay before a ring");
    ring(fake, FRONT, "f1");
    await sleep(40);
    assert.ok(overlay() && overlay().parentNode === doc.body, "a body-level overlay");
    assert.strictEqual(oroot().querySelector("h1").textContent, "Front door is calling");
    assert.deepStrictEqual(obtn(), ["Open front", "Answer", "Dismiss"], "ring buttons of THIS door, then Answer (it has audio), Dismiss");
    assert.strictEqual(oroot().querySelectorAll("img[data-camera]").length, 1);
    assert.strictEqual(oroot().querySelector("img[data-camera]").dataset.camera, "c-aaaaaaaaaaaa");
    await sleep(60);
    assert.ok(oroot().querySelector("img[data-camera]").getAttribute("src").startsWith("data:image/jpeg"), "the still arrived");
    ended(fake, FRONT, "f1");
    await sleep(10);
    assert.ok(!overlay(), "the ring ending closes it");

    ring(fake, GATE, "g1");
    await sleep(40);
    assert.strictEqual(oroot().querySelector("h1").textContent, "Vehicle gate is calling");
    assert.deepStrictEqual(obtn(), ["Open gate", "Dismiss"], "the other door's entry; no Answer without audio");
    assert.deepStrictEqual(Array.from(oroot().querySelectorAll("img[data-camera]")).map((i) => i.dataset.camera), ["c-bbbbbbbbbbbb", "c-cccccccccccc"]);
    const snaps = fake.ofType("portico/content/snapshot").filter((m) => m.camera_id.startsWith("c-"));
    assert.ok(snaps.every((m) => m.camera_id === "c-aaaaaaaaaaaa" || m.camera_id === "c-bbbbbbbbbbbb"), "an unavailable camera is not asked for");
    assert.ok(snaps.every((m) => m.endpoint === "kitchen"));
    assert.ok(oroot().textContent.includes("Camera unavailable"));
    console.log("PASS: the ringing door's endpoint_id picks its callers entry: name, cameras, ring buttons");
    ended(fake, GATE, "g1");
    card.remove();
  }

  // 3b. no endpoint_id, an endpoint_id the callers do not have, or an Object.prototype name: the old door-camera rule
  for (const endpoint_id of [undefined, "nobody:here", "__proto__", "constructor"]) {
    const door = { ...FRONT, endpoint_id };
    const { card, fake } = await mount(SCREEN, { doors: [door] });
    ring(fake, door, "x1");
    await sleep(30);
    assert.strictEqual(oroot().querySelector("h1").textContent, "Someone is at the door", `endpoint_id ${endpoint_id}: no entry`);
    // the old rule: the first available camera of the document's pages, and the DOOR_KINDS of its actions
    assert.strictEqual(oroot().querySelector("img[data-camera]").dataset.camera, "p-yard");
    assert.ok(!obtn().includes("Open front"), "no other door's buttons");
    assert.ok(obtn().includes("Evening") === false, "only the door kinds of the actions");
    card.remove();
  }
  console.log("PASS: a door with no callers entry falls back to the old door-camera rule, never to another door's entry");

  // 4. in a call the overlay shows the call buttons; a button runs by id with the endpoint; a lock asks twice
  {
    const { card, fake } = await mount({ ...SCREEN, _armMs: 500 }, {});
    ring(fake, FRONT, "f1");
    await sleep(30);
    const front = () => Array.from(oroot().querySelectorAll("button")).find((b) => b.dataset.action === "a-front");
    front().click();
    await sleep(5);
    assert.strictEqual(fake.ofType("portico/content/run").length, 0, "a confirm button: the first tap sends nothing");
    front().click();
    await sleep(10);
    assert.deepStrictEqual(fake.ofType("portico/content/run"), [{ type: "portico/content/run", action_id: "a-front", endpoint: "kitchen" }]);
    oroot().querySelector("#answer-btn").click();
    await sleep(40);
    assert.strictEqual(oroot().querySelector("h1").textContent, "In a call with Front door");
    assert.deepStrictEqual(obtn(), ["Open during call", "Hang up", "Dismiss"], "the call buttons, not the ring buttons");
    assert.ok(state.lastPC && !state.lastPC.closed, "a PeerConnection is live");
    console.log("PASS: ring buttons confirm and run by id with the endpoint; in a call the call buttons replace them");
    card.remove();
  }

  // 5. stills: every ring tick, one request per camera in flight
  {
    let release = null;
    let calls = 0;
    const gate = new Promise((r) => { release = r; });
    const { card, fake } = await mount({ ...SCREEN, _ringSnapshotMs: 20 }, {
      snapshot: async (m) => {
        if (m.camera_id === "c-aaaaaaaaaaaa") { calls++; await gate; }
        return { content_type: "image/jpeg", content: "QUJD" };
      },
    });
    ring(fake, FRONT, "f1");
    await sleep(150);
    assert.strictEqual(calls, 1, "one request in flight for the camera: the next tick waits for the answer");
    release();
    await sleep(80);
    assert.ok(calls >= 3, `after the answer the stills continue (${calls})`);
    console.log("PASS: stills refresh on the ring cadence with one request per camera in flight");
    card.remove();
  }
  {
    // the cadence default is 1.5 s while ringing (and slower otherwise)
    const { card, fake } = await mount({ endpoint: "kitchen", takeover: true, _ringSnapshotMs: undefined }, {});
    ring(fake, FRONT, "f1");
    await sleep(60);
    const n = fake.ofType("portico/content/snapshot").filter((m) => m.camera_id === "c-aaaaaaaaaaaa").length;
    assert.strictEqual(n, 1, "the first still at once, the next one only 1.5 s later");
    console.log("PASS: the default ring cadence is 1.5 s");
    card.remove();
  }

  // 6. the second ring while in a call is refused (R7-19); the live call keeps its PeerConnection
  {
    const { card, fake } = await mount(SCREEN, {});
    ring(fake, FRONT, "f1");
    await sleep(30);
    oroot().querySelector("#answer-btn").click();
    await sleep(40);
    const livePC = state.lastPC;
    assert.ok(livePC && !livePC.closed && card._pc === livePC, "the first call is live");
    ring(fake, GATE, "g1");
    await sleep(20);
    assert.strictEqual(card._pc, livePC, "the PeerConnection is still the first call's");
    assert.ok(!livePC.closed, "and is not closed");
    assert.strictEqual(card._activeDoorId, FRONT.door_id);
    assert.strictEqual(card._activeCallId, "f1");
    assert.strictEqual(card._state, "in-call");
    assert.strictEqual(oroot().querySelector("h1").textContent, "In a call with Front door", "the overlay still shows the live call");
    assert.ok(oroot().textContent.includes("Vehicle gate is ringing, but you are already on a call."), "one line says why");
    // the refused door's own `ended` does not end the live call; the live call's `ended` does, and frees the card
    ended(fake, GATE, "g1");
    await sleep(10);
    assert.ok(!livePC.closed && card._pc === livePC, "the refused door ending changes nothing");
    ended(fake, FRONT, "f1");
    await sleep(10);
    assert.ok(livePC.closed && card._pc === null, "the live call's ended tears the call down");
    assert.strictEqual(card._localStream, null, "and releases the microphone");
    ring(fake, GATE, "g2");
    await sleep(30);
    assert.strictEqual(oroot().querySelector("h1").textContent, "Vehicle gate is calling", "the next ring is accepted once free");
    console.log("PASS: a second ring while in a call is refused with one line; the live call is kept, then torn down by its own ended");
    // a ring is also refused while the answer is still being set up
    card.remove();
  }
  {
    // while the Answer is under way (microphone prompt open): same rule
    const { card, fake } = await mount(SCREEN, {});
    let grant = null;
    const real = global.navigator.mediaDevices.getUserMedia;
    global.navigator.mediaDevices.getUserMedia = () => new Promise((r) => { grant = () => r({ getTracks: () => [{ stop() {} }] }); });
    ring(fake, FRONT, "f1");
    await sleep(30);
    oroot().querySelector("#answer-btn").click();
    await sleep(10);
    ring(fake, GATE, "g1");
    await sleep(10);
    assert.strictEqual(card._activeCallId, "f1", "a ring during the answer does not take the card over");
    global.navigator.mediaDevices.getUserMedia = real;
    grant();
    await sleep(40);
    card.remove();
    console.log("PASS: a ring while the answer is being set up is refused too");
  }

  // 7. the version probe: contract 3 works; any other contract or an unknown command shows one line and a Reload
  for (const [label, opts] of [
    ["contract 2", { version: { contract: 2 } }],
    ["contract 4", { version: { contract: 4 } }],
    ["no contract field", { version: {} }],
    ["unknown_command", { versionError: { code: "unknown_command", message: "x" } }],
  ]) {
    const { card, fake } = await mount(SCREEN, opts);
    assert.strictEqual(q(card, ".error").textContent, "Portico is being updated; this page works again after Home Assistant restarts.", label);
    assert.deepStrictEqual(fake.sent.map((m) => m.type), ["portico/version"], `${label}: nothing but the probe was sent`);
    assert.ok(!fake.hasSubscriber(), `${label}: nothing subscribed`);
    let reloads = 0;
    card._reloadPage = () => { reloads++; };
    q(card, "#reload-btn").click();
    assert.strictEqual(reloads, 1, `${label}: the button reloads the page`);
    card.remove();
  }
  {
    // the probe guards EVERY mode: the plain card (no endpoint) too
    const { card, fake } = await mount({}, { version: { contract: 2 } });
    assert.ok(q(card, "#reload-btn"), "a card without endpoint shows the line and Reload");
    assert.deepStrictEqual(fake.sent.map((m) => m.type), ["portico/version"]);
    card.remove();
  }
  {
    const { card, fake } = await mount(SCREEN, { versionError: new Error("socket closed") });
    assert.ok(!q(card, "#reload-btn") && fake.ofType("portico/content/get").length === 1, "a transient failure is not a version answer");
    card.remove();
  }
  console.log("PASS: the version probe, in every mode: contract 3 works; another contract or an unknown command is one line and a Reload button, nothing else runs");

  // 8. error words in screen mode
  for (const [err, expect] of [
    [{ code: "unauthorized", message: "x" }, "not set up for this screen"],
    [{ code: "unknown_endpoint", message: "x" }, "`endpoint`"],
    [{ code: "internal_error", message: "x" }, "Could not load"],
  ]) {
    const { card } = await mount(SCREEN, { contentError: err });
    assert.ok(q(card, ".error").textContent.includes(expect), `${err.code} -> ${q(card, ".error").textContent}`);
    card.remove();
  }
  console.log("PASS: screen content errors say what to fix");

  // 9. user text is text: names, labels, the refusal line
  {
    const HOSTILE = `<img src=x onerror=window.__pwn=1><b>x</b>"&'`;
    const content = screenContent();
    content.pages[0].label = HOSTILE;
    content.pages[1].label = HOSTILE + "2";
    content.pages[0].cameras[0].label = HOSTILE;
    content.pages[0].actions[0].label = HOSTILE;
    content.callers["dahua:vto.example"].name = HOSTILE;
    content.callers["dahua:vto.example"].cameras[0].label = HOSTILE;
    content.callers["dahua:vto.example"].ring_buttons[0].label = HOSTILE;
    content.callers["gate_sip:gate.example"].name = HOSTILE + "g";
    const { card, fake } = await mount(SCREEN, { content });
    const bad = (root) => root.querySelectorAll("b, script, img[onerror], img[src=x]").length;
    assert.strictEqual(bad(card.shadowRoot), 0, "the idle page carries no injected element");
    assert.strictEqual(qa(card, "button.tab")[0].textContent, HOSTILE);
    ring(fake, FRONT, "f1");
    await sleep(30);
    assert.strictEqual(bad(oroot()), 0, "the ring overlay carries none");
    assert.strictEqual(oroot().querySelector("h1").textContent, `${HOSTILE} is calling`);
    oroot().querySelector("#answer-btn").click();
    await sleep(40);
    ring(fake, GATE, "g1");
    await sleep(20);
    assert.strictEqual(bad(oroot()), 0, "nor does the refusal line");
    assert.ok(oroot().textContent.includes(`${HOSTILE}g is ringing, but you are already on a call.`));
    assert.strictEqual(global.window.__pwn, undefined);
    console.log("PASS: names, labels and the refusal line reach the DOM as text");
    card.remove();
  }

  // 10. a card WITHOUT `endpoint` is the viewing user's OWN endpoint: content/get {}, no profile anywhere
  {
    const own = screenContent({ endpoint: "ep-alice", kind: "phone" });
    const { card, fake } = await mount({ takeover: true }, { content: own });
    await sleep(60);
    assert.deepStrictEqual(fake.ofType("portico/content/get"), [{ type: "portico/content/get" }]);
    assert.deepStrictEqual(fake.ofType("portico/content/subscribe"), [{ type: "portico/content/subscribe" }]);
    assert.ok(fake.ofType("portico/content/snapshot").every((m) => !("profile" in m) && !("endpoint" in m)), "snapshots carry no scope");
    assert.ok(fake.sent.every((m) => !("profile" in m)));
    assert.deepStrictEqual(qa(card, ".cam .label").map((e) => e.textContent), ["Yard"]);
    assert.deepStrictEqual(qa(card, "button.tab").map((b) => b.textContent), ["Home", "Garden"], "a phone's pages page too");
    assert.ok(!card.hasAttribute("fullpage"));
    assert.strictEqual(card.getCardSize(), 6);
    // content_changed: its own endpoint id (learned from the document) refetches; another's does not
    fake.fire({ type: "content_changed", endpoint: "ep-other", rev: "r9" });
    await sleep(20);
    assert.strictEqual(fake.ofType("portico/content/get").length, 1, "another endpoint's change is not ours");
    fake.fire({ type: "content_changed", endpoint: "ep-alice", rev: "r9" });
    await sleep(20);
    assert.strictEqual(fake.ofType("portico/content/get").length, 2);
    // the ring is drawn from `callers`, keyed by the door's endpoint_id, in this mode too
    ring(fake, FRONT, "f1");
    await sleep(30);
    assert.strictEqual(oroot().querySelector("h1").textContent, "Front door is calling");
    assert.deepStrictEqual(obtn(), ["Open front", "Answer", "Dismiss"]);
    assert.strictEqual(oroot().querySelector("img[data-camera]").dataset.camera, "c-aaaaaaaaaaaa");
    await sleep(40);
    assert.ok(fake.ofType("portico/content/snapshot").some((m) => m.camera_id === "c-aaaaaaaaaaaa" && !("endpoint" in m)));
    console.log("PASS: a card without `endpoint` asks content/get {} (the user's own endpoint) and draws the callers entry of the ringing door");
    // R7-19 in this mode: a second ring while in a call is refused
    oroot().querySelector("#answer-btn").click();
    await sleep(40);
    const livePC = state.lastPC;
    ring(fake, GATE, "g1");
    await sleep(10);
    assert.strictEqual(card._activeCallId, "f1", "the second ring does not take the card over");
    assert.strictEqual(card._pc, livePC);
    assert.ok(!livePC.closed);
    console.log("PASS: the second-ring refusal holds in the endpoint-less mode too");
    card.remove();
  }

  // 11. fullpage is a host attribute that follows the config
  {
    const card = doc.createElement("portico-card");
    card.setConfig({ endpoint: "kitchen", fullpage: true });
    assert.ok(card.hasAttribute("fullpage"));
    card.setConfig({ endpoint: "kitchen" });
    assert.ok(!card.hasAttribute("fullpage"));
    console.log("PASS: fullpage follows the config");
  }

  console.log("ALL_CARD_SCREEN_TESTS_PASSED");
}

main().catch((err) => {
  console.error("TEST FAILED:", err);
  process.exit(1);
});
