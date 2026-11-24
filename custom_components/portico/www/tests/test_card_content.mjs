// The card's content view and ring takeover (content design §6): protocol-level, jsdom, nothing mocked
// that the card does not itself call. NOT a visual check (see test_card_visual.html).
import assert from "assert";
import { loadCard, sleep, makeHass, CONTENT, DOOR_NO_AUDIO, DOOR_AUDIO } from "./card_loader.mjs";

const { dom, PorticoCard, state } = loadCard();
assert.ok(PorticoCard, "portico-card was not registered");
const doc = dom.window.document;

async function mount(config, hassOpts) {
  const fake = makeHass(hassOpts);
  const card = doc.createElement("portico-card");
  doc.body.appendChild(card);
  card.setConfig({ _pollMs: 20, _snapshotMs: 30, _ringSnapshotMs: 20, ...config });
  card.hass = fake.hass;
  await sleep(40);
  return { card, fake };
}
const overlay = () => doc.querySelector("[data-portico-overlay]");
const overlayRoot = () => overlay() && overlay().shadowRoot;
const q = (card, sel) => card.shadowRoot.querySelector(sel);
const qa = (card, sel) => Array.from(card.shadowRoot.querySelectorAll(sel));
const ring = (fake, door, call = "c1", extra = {}) =>
  fake.fire({ type: "call_state_changed", door_id: door.door_id, call_id: call, state: "ringing", ...extra });

async function main() {
  // 1. the content view: get with the endpoint, tiles, buttons, snapshots only for available cameras
  {
    const { card, fake } = await mount({ endpoint: "ep-alice" }, { doors: [DOOR_NO_AUDIO], content: CONTENT });
    assert.deepStrictEqual(fake.ofType("portico/content/get"), [{ type: "portico/content/get", endpoint: "ep-alice" }]);
    assert.strictEqual(qa(card, ".cam").length, 2);
    assert.deepStrictEqual(qa(card, "button.act").map((b) => b.textContent), ["Front door", "Gate relay", "Evening"]);
    await sleep(60);
    const snaps = fake.ofType("portico/content/snapshot");
    assert.ok(snaps.length >= 1, "a picture was asked for");
    assert.ok(snaps.every((m) => m.camera_id === "gate" && m.endpoint === "ep-alice" && !("profile" in m)), "only the available camera, with the endpoint, by id");
    assert.strictEqual(q(card, "img[data-camera=gate]").getAttribute("src"), "data:image/jpeg;base64,QUJD");
    assert.ok(q(card, "img[data-camera=garage]").parentElement.classList.contains("nopic"), "an unavailable camera shows no picture");
    console.log("PASS: content view asks for the endpoint, draws tiles and buttons, snapshots only available cameras by id");
    card.remove();
  }

  // 2. content_changed reloads; the ids sent are ids, never a service or an entity
  {
    const { card, fake } = await mount({}, { doors: [DOOR_NO_AUDIO], content: CONTENT });
    fake.fire({ type: "content_changed", rev: null });
    await sleep(20);
    assert.strictEqual(fake.ofType("portico/content/get").length, 2);
    for (const m of fake.sent) {
      assert.ok(!("entity_id" in m) && !("service" in m) && !("domain" in m), "no entity or service in " + JSON.stringify(m));
    }
    console.log("PASS: content_changed refetches; no request carries an entity or a service");
    card.remove();
  }

  // 3. error words
  for (const [err, expect] of [
    [{ code: "unauthorized", message: "x" }, "Name a screen with endpoint:"],
    [{ code: "unknown_endpoint", message: "x" }, "`endpoint`"],
    [{ code: "internal_error", message: "x" }, "Could not load"],
  ]) {
    const { card } = await mount({}, { doors: [], contentError: err });
    assert.ok(q(card, ".error").textContent.includes(expect), `${err.code} -> ${q(card, ".error").textContent}`);
    card.remove();
  }
  console.log("PASS: content errors say what to fix (name a screen, unknown endpoint, anything else)");

  // 4. a plain button runs at once by id, with no profile key, and says so
  {
    const { card, fake } = await mount({}, { doors: [], content: CONTENT });
    qa(card, "button.act").find((b) => b.textContent === "Evening").click();
    await sleep(10);
    assert.deepStrictEqual(fake.ofType("portico/content/run"), [{ type: "portico/content/run", action_id: "evening" }]);
    assert.ok(q(card, ".notice").textContent.includes("done"));
    console.log("PASS: a plain button runs at once, by id, and reports done");
    card.remove();
  }

  // 5. a lock or gate needs a second tap, and the arming expires
  {
    const { card, fake } = await mount({ _armMs: 60 }, { doors: [], content: CONTENT });
    const front = () => qa(card, "button.act").find((b) => b.dataset.action === "open-front");
    front().click();
    await sleep(5);
    assert.strictEqual(fake.ofType("portico/content/run").length, 0, "the first tap sends nothing");
    assert.strictEqual(front().textContent, "Tap again: Front door");
    await sleep(90);
    assert.strictEqual(front().textContent, "Front door", "the arming expired");
    front().click();
    await sleep(5);
    front().click();
    await sleep(10);
    assert.deepStrictEqual(fake.ofType("portico/content/run").map((m) => m.action_id), ["open-front"]);
    console.log("PASS: a lock asks for a second tap, which expires; the second tap runs it once");
    card.remove();
  }

  // 6. a refused run is worded
  {
    const { card } = await mount({}, { doors: [], content: CONTENT, runError: () => ({ code: "rate_limited", message: "x" }) });
    qa(card, "button.act").find((b) => b.textContent === "Evening").click();
    await sleep(10);
    assert.ok(q(card, ".notice").textContent.includes("a moment ago"));
    console.log("PASS: a rate-limited press is worded, not a stack trace");
    card.remove();
  }

  // 7. ring takeover: overlay on the body (not in the card), door buttons only, no Answer for a device without audio
  {
    const { card, fake } = await mount({ takeover: true }, { doors: [DOOR_NO_AUDIO], content: CONTENT });
    assert.ok(!overlay(), "no overlay before a ring");
    ring(fake, DOOR_NO_AUDIO);
    await sleep(40);
    assert.ok(overlay(), "the ring opens the overlay");
    assert.strictEqual(overlay().parentNode, doc.body, "attached to document.body, outside every ancestor");
    assert.ok(!card.shadowRoot.contains(overlay()), "not inside the card");
    assert.strictEqual(overlay().style.position, "fixed", "fixed over the viewport");
    assert.ok(overlay().style.top === "0px" && overlay().style.bottom === "0px" && overlay().style.left === "0px" && overlay().style.right === "0px", "covering all four edges");
    const buttons = Array.from(overlayRoot().querySelectorAll("button")).map((b) => b.textContent);
    assert.deepStrictEqual(buttons, ["Front door", "Gate relay", "Dismiss"], "only the door kinds, then Dismiss; no Answer");
    assert.ok(overlayRoot().textContent.includes("cannot be answered from here"));
    assert.ok(fake.ofType("portico/content/snapshot").some((m) => m.camera_id === "gate"), "the ring camera is fetched");
    await sleep(60);
    assert.ok(overlayRoot().querySelector("img[data-camera=gate]").getAttribute("src").startsWith("data:image/jpeg"));
    console.log("PASS: a ring opens a body-level fixed overlay with the door buttons and a picture, and no Answer without audio");

    // the door button asks twice, then runs by id
    const gateBtn = () => Array.from(overlayRoot().querySelectorAll("button")).find((b) => b.dataset.action === "open-gate");
    gateBtn().click();
    await sleep(5);
    assert.strictEqual(fake.ofType("portico/content/run").length, 0);
    gateBtn().click();
    await sleep(10);
    assert.deepStrictEqual(fake.ofType("portico/content/run").map((m) => m.action_id), ["open-gate"]);
    console.log("PASS: opening from the overlay asks twice, then runs by id");

    // Dismiss hides it; a repeat of the same ring does not reopen it; a new call does
    overlayRoot().querySelector("#dismiss-btn").click();
    assert.ok(!overlay(), "Dismiss closes it");
    ring(fake, DOOR_NO_AUDIO, "c1");
    assert.ok(!overlay(), "the same call ringing again stays dismissed");
    ring(fake, DOOR_NO_AUDIO, "c2");
    assert.ok(overlay(), "a new call opens it again");
    console.log("PASS: Dismiss closes it for that call only");

    // idle closes it
    fake.fire({ type: "call_state_changed", door_id: DOOR_NO_AUDIO.door_id, call_id: "c2", state: "ended", prev_state: "ringing" });
    assert.ok(!overlay(), "the call ending closes the overlay");
    console.log("PASS: the call ending closes the overlay");
    card.remove();
  }

  // 8. answered elsewhere: says so and STAYS for the whole call (the cameras are what the screen is for), no door buttons,
  //    and lets go when the call ends (the timer that used to close it after a few seconds is gone)
  {
    const { card, fake } = await mount({ takeover: true, _elsewhereMs: 60 }, { doors: [DOOR_NO_AUDIO], content: CONTENT });
    ring(fake, DOOR_NO_AUDIO);
    fake.fire({ type: "call_state_changed", door_id: DOOR_NO_AUDIO.door_id, call_id: "c1", state: "answered" });
    assert.ok(overlay(), "still there");
    assert.ok(overlayRoot().textContent.includes("Answered elsewhere"));
    assert.ok(!Array.from(overlayRoot().querySelectorAll("button")).some((b) => b.dataset.action), "no door buttons on a call someone else took");
    await sleep(90);
    assert.ok(overlay(), "still there during the call, long past the old timer");
    fake.fire({ type: "call_state_changed", door_id: DOOR_NO_AUDIO.door_id, call_id: "c1", state: "ended" });
    assert.ok(!overlay(), "closes when the call ends");
    console.log("PASS: answered elsewhere is said, the overlay stays for the call and closes when it ends");
    card.remove();
  }

  // 9. nobody answers: lets go after the window
  {
    const { card, fake } = await mount({ takeover: true, _ringTimeoutMs: 50 }, { doors: [DOOR_NO_AUDIO], content: CONTENT });
    ring(fake, DOOR_NO_AUDIO);
    assert.ok(overlay());
    await sleep(90);
    assert.ok(!overlay(), "closed after the timeout");
    console.log("PASS: the overlay closes after the no-answer window");
    card.remove();
  }

  // 10. a device with audio gets Answer; answering ourselves is not 'answered elsewhere'
  {
    const { card, fake } = await mount({ takeover: true }, { doors: [DOOR_AUDIO], content: CONTENT });
    ring(fake, DOOR_AUDIO, "v1");
    const answer = overlayRoot().querySelector("#answer-btn");
    assert.ok(answer, "Answer is offered when the device has audio");
    answer.click();
    await sleep(30);
    assert.strictEqual(state.getUserMediaCalls, 1);
    fake.fire({ type: "call_state_changed", door_id: DOOR_AUDIO.door_id, call_id: "v1", state: "answered" });
    assert.ok(overlay() && overlayRoot().textContent.includes("In a call"), "our own answer is a call, not 'elsewhere'");
    assert.ok(overlayRoot().querySelector("#hangup-btn"));
    console.log("PASS: Answer only for a device with audio; our own answer is not 'answered elsewhere'");
    card.remove();
  }

  // 10b. answer refused with already_answered: say so, release the overlay, offer no retry
  {
    const { card, fake } = await mount(
      { takeover: true, _elsewhereMs: 60 },
      { doors: [DOOR_AUDIO], content: CONTENT, answerError: () => ({ code: "already_answered", message: "this call was already answered or has ended" }) }
    );
    ring(fake, DOOR_AUDIO, "aa1");
    overlayRoot().querySelector("#answer-btn").click();
    await sleep(30);
    assert.ok(overlayRoot().textContent.includes("Someone else already answered this call"));
    assert.ok(!overlayRoot().textContent.includes("Answering failed"));
    assert.strictEqual(overlayRoot().querySelector("#answer-btn"), null, "no retry on a call that is gone");
    assert.ok(!fake.ofType("portico/webrtc_offer").length, "no offer sent");
    fake.fire({ type: "call_state_changed", door_id: DOOR_AUDIO.door_id, call_id: "aa1", state: "answered" });
    assert.ok(!overlayRoot().textContent.includes("In a call"), "a late answered event does not make it a call");
    await sleep(90);
    assert.strictEqual(overlay(), null, "the overlay lets go by itself");
    console.log("PASS: already_answered in the takeover overlay says so, offers no retry, and closes itself");
    card.remove();
  }

  // 10c. answer refused with device_busy: say so, keep the overlay and the Answer button
  {
    const { card, fake } = await mount(
      { takeover: true },
      { doors: [DOOR_AUDIO], content: CONTENT, answerError: () => ({ code: "device_busy", message: "SIP 486" }) }
    );
    ring(fake, DOOR_AUDIO, "db1");
    overlayRoot().querySelector("#answer-btn").click();
    await sleep(30);
    assert.ok(overlayRoot().textContent.includes("The door station is busy with another call. Try again in a moment."));
    assert.ok(!overlayRoot().textContent.includes("Answering failed"));
    assert.ok(overlayRoot().querySelector("#answer-btn"), "Answer stays available to retry");
    assert.ok(overlay(), "the overlay is not dismissed");
    console.log("PASS: device_busy in the takeover overlay says so and keeps the ring and Answer");
    card.remove();
  }

  // 11. the chime: starts on a ring, stops on dismiss, and no audio at all is harmless
  {
    let beeps = 0;
    dom.window.AudioContext = class {
      constructor() { this.state = "suspended"; this.currentTime = 0; this.destination = {}; }
      resume() { this.state = "running"; return Promise.resolve(); }
      createOscillator() { beeps++; return { frequency: {}, connect() {}, start() {}, stop() {} }; }
      createGain() { return { gain: {}, connect() {} }; }
    };
    const { card, fake } = await mount({ takeover: true, _chimeMs: 20 }, { doors: [DOOR_NO_AUDIO], content: CONTENT });
    ring(fake, DOOR_NO_AUDIO);
    await sleep(70);
    assert.ok(beeps >= 2, "a chime repeats while it rings (" + beeps + ")");
    overlayRoot().querySelector("#dismiss-btn").click();
    const after = beeps;
    await sleep(60);
    assert.strictEqual(beeps, after, "Dismiss stops the chime");
    card.remove();
    delete dom.window.AudioContext;
    const again = await mount({ takeover: true }, { doors: [DOOR_NO_AUDIO], content: CONTENT });
    ring(again.fake, DOOR_NO_AUDIO, "c9");
    assert.ok(overlay(), "no AudioContext: the overlay still opens, silently");
    again.card.remove();
    console.log("PASS: the chime plays while ringing, stops on Dismiss, and a missing AudioContext is harmless");
  }

  // 12. without takeover nothing leaves the card (the legacy behaviour)
  {
    const { card, fake } = await mount({}, { doors: [DOOR_NO_AUDIO] });
    ring(fake, DOOR_NO_AUDIO);
    assert.ok(!overlay(), "no overlay without takeover");
    assert.ok(q(card, "#answer-btn"), "the ring is drawn inside the card as before");
    card.remove();
    console.log("PASS: without takeover the ring stays inside the card");
  }

  // 13. takeover with no door button anywhere still opens the door through the door's relay
  {
    const bare = { ...CONTENT, pages: [{ id: "home", label: "Home", cameras: [], actions: [{ id: "evening", label: "Evening", kind: "scene_activate", confirm: false, available: true, name: "Evening" }] }] };
    const { card, fake } = await mount({ takeover: true }, { doors: [DOOR_NO_AUDIO], content: bare });
    ring(fake, DOOR_NO_AUDIO);
    overlayRoot().querySelector("#open-btn").click();
    await sleep(10);
    assert.deepStrictEqual(fake.ofType("portico/open_relay"), [{ type: "portico/open_relay", door_id: "gate.example" }]);
    card.remove();
    console.log("PASS: takeover without door buttons falls back to the door relay");
  }

  // 14. leaving the page removes the overlay and stops everything
  {
    const { card, fake } = await mount({ takeover: true }, { doors: [DOOR_NO_AUDIO], content: CONTENT });
    ring(fake, DOOR_NO_AUDIO);
    assert.ok(overlay());
    card.remove(); // disconnectedCallback
    assert.ok(!overlay(), "no orphan overlay stays on the page");
    assert.ok(!fake.hasSubscriber(), "the subscription is released");
    const n = fake.ofType("portico/content/snapshot").length;
    await sleep(80);
    assert.strictEqual(fake.ofType("portico/content/snapshot").length, n, "polling stopped");
    console.log("PASS: removing the card removes the overlay, the subscription and the polling");
  }

  // 15. a DOM re-attach (masonry re-parenting) rebuilds the subscription and polling
  {
    const { card, fake } = await mount({ takeover: true }, { doors: [DOOR_NO_AUDIO], content: CONTENT });
    const parent = card.parentNode;
    parent.removeChild(card);
    assert.ok(!fake.hasSubscriber(), "released on detach");
    parent.appendChild(card);
    await sleep(60);
    assert.ok(fake.hasSubscriber(), "re-subscribed on re-attach");
    const n = fake.ofType("portico/content/snapshot").length;
    await sleep(80);
    assert.ok(fake.ofType("portico/content/snapshot").length > n, "polling resumed");
    ring(fake, DOOR_NO_AUDIO);
    assert.ok(overlay(), "a ring after re-attach opens the overlay");
    card.remove();
    console.log("PASS: re-attaching the card resubscribes and resumes polling");
  }

  // 16. detached while subscribeMessage is in flight: the subscription is released, nothing polls
  {
    const fake = makeHass({ doors: [DOOR_NO_AUDIO], content: CONTENT });
    let live = 0;
    fake.hass.connection.subscribeMessage = async () => { await sleep(30); live++; return () => { live--; }; };
    const card = doc.createElement("portico-card");
    doc.body.appendChild(card);
    card.setConfig({ _pollMs: 20 });
    card.hass = fake.hass;
    await sleep(5);
    card.remove();
    await sleep(80);
    assert.strictEqual(live, 0, "the late subscription was released");
    assert.strictEqual(fake.ofType("portico/content/snapshot").length, 0, "no polling on a detached card");
    console.log("PASS: a card detached during subscribe leaks nothing");
  }

  // ---- blocks (portico blocks design 6.5): a page with `blocks` is drawn as blocks; a page without them is the flat form

  const cam = (id, size, extra = {}) => ({ id, label: id.toUpperCase(), entity_id: `camera.${id}`, size, available: true, name: id, ...extra });
  const act = (id, extra = {}) => ({ id, label: `L-${id}`, kind: "script_run", entity_id: `script.${id}`, confirm: false, available: true, name: id, ...extra });
  // a content reply whose first page holds the given blocks, as the server sends it since B8: blocks and NO flat lists
  const withBlocks = (blocks, extra = {}) => ({
    ...CONTENT,
    pages: [{ id: "home", label: "Home", blocks }],
    ...extra,
  });
  const blk = (id, title, cameras = [], actions = []) => ({ id, title, cameras, actions });
  const tileClasses = (card) => qa(card, ".cam").map((t) => `${t.querySelector("img").dataset.camera}:${t.classList.contains("small") ? "small" : t.classList.contains("big") ? "big" : "?"}`);
  const sizeOf = (fake) => Object.fromEntries(fake.ofType("portico/content/snapshot").map((m) => [m.camera_id, m.width]));

  // 17. one camera: a block with a title and one full-width tile; the picture is asked for at 960
  {
    const { card, fake } = await mount({}, { doors: [], content: withBlocks([blk("b1", "Entrance", [cam("gate", "big")])]) });
    assert.strictEqual(qa(card, "section.block").length, 1);
    assert.strictEqual(q(card, "section.block h3.block-title").textContent, "Entrance", "the title is a heading, set as text");
    assert.deepStrictEqual(tileClasses(card), ["gate:big"]);
    await sleep(60);
    const snaps = fake.ofType("portico/content/snapshot");
    assert.ok(snaps.length >= 1 && snaps.every((m) => m.width === 960 && m.camera_id === "gate"), JSON.stringify(snaps));
    assert.ok(q(card, "img[data-camera=gate]").getAttribute("src").startsWith("data:image/jpeg"), "the picture lands in the tile");
    console.log("PASS: blocks: one camera is one big tile under its title, asked for at width 960");
    card.remove();
  }

  // 18. two cameras (big then small), three, and an odd count: the sizes the server sent are the sizes drawn
  {
    const cases = [
      [["a:big", "b:small"], [cam("a", "big"), cam("b", "small")]],
      [["a:big", "b:small", "c:small"], [cam("a", "big"), cam("b", "small"), cam("c", "small")]],
      [["a:big", "b:small", "c:small", "d:small"], [cam("a", "big"), cam("b", "small"), cam("c", "small"), cam("d", "small")]],
      [["a:small", "b:small", "c:big"], [cam("a", "small"), cam("b", "small"), cam("c", "big")]],
      [["a:small", "b:small"], [cam("a", "small"), cam("b", "small")]],
    ];
    for (const [expect, cameras] of cases) {
      const { card, fake } = await mount({}, { doors: [], content: withBlocks([blk("b1", "T", cameras)]) });
      assert.deepStrictEqual(tileClasses(card), expect);
      await sleep(60);
      const w = sizeOf(fake);
      for (const c of cameras) {
        if (c.size === "big") assert.strictEqual(w[c.id], 960, `${c.id} is big: width 960`);
        else assert.ok(Object.prototype.hasOwnProperty.call(w, c.id) && w[c.id] === undefined, `${c.id} is small: asked for, with no width`);
      }
      // the layout rules that make big a row of its own and small half a row
      const css = card.shadowRoot.querySelector("style").textContent;
      assert.ok(/\.cams\.blk\s*\{[^}]*grid-template-columns:\s*1fr 1fr/.test(css), "two columns");
      assert.ok(/\.cams\.blk \.cam\.big\s*\{[^}]*grid-column:\s*1 \/ -1/.test(css), "a big tile spans the row");
      card.remove();
    }
    console.log("PASS: blocks: sizes from the server are honoured for 2, 3, odd and small-first camera lists; width only on big");
  }

  // 19. no size toggle and no zoom: a tap on a tile never changes its size and sends nothing but polling (since the live video
  // work a tap switches the tile between live and still for this viewer: that is pinned in test_card_live.mjs)
  {
    const { card, fake } = await mount({}, { doors: [], content: withBlocks([blk("b1", "T", [cam("a", "big"), cam("b", "small")])]) });
    await sleep(40);
    const n = fake.sent.length;
    for (const t of qa(card, ".cam")) { t.click(); t.querySelector("img").click(); }
    for (const t of qa(card, ".cam")) t.dispatchEvent(new dom.window.WheelEvent("wheel", { deltaY: -100, ctrlKey: true, bubbles: true }));
    assert.deepStrictEqual(tileClasses(card), ["a:big", "b:small"]);
    assert.ok(fake.sent.slice(n).every((m) => m.type === "portico/content/snapshot"), "nothing but polling is sent");
    console.log("PASS: blocks: a tap on a tile does not toggle its size and nothing zooms");
    card.remove();
  }

  // 20. several blocks: titles in order, each with its own tiles and buttons; a block's cameras all poll
  {
    const content = withBlocks([
      blk("b1", "Front", [cam("a", "big")], [act("x1")]),
      blk("b2", "Garden", [cam("b", "big"), cam("c", "small")], [act("x2"), act("x3")]),
    ]);
    const { card, fake } = await mount({}, { doors: [], content });
    assert.deepStrictEqual(qa(card, "section.block").map((s) => s.dataset.block), ["b1", "b2"]);
    assert.deepStrictEqual(qa(card, ".block-title").map((h) => h.textContent), ["Front", "Garden"]);
    assert.deepStrictEqual(qa(card, "section.block:nth-of-type(2) button.act").map((b) => b.dataset.action), ["x2", "x3"]);
    await sleep(60);
    assert.deepStrictEqual(Object.keys(sizeOf(fake)).sort(), ["a", "b", "c"]);
    // the flat mirror is not drawn a second time
    assert.strictEqual(qa(card, ".cam").length, 3);
    assert.strictEqual(qa(card, "button.act").length, 3);
    console.log("PASS: blocks: several blocks draw in order, each its own, and the flat mirror is not drawn twice");
    card.remove();
  }

  // 21. buttons: icon, label, colour, show_label, accessible name, touch size, confirm as before
  {
    const content = withBlocks([blk("b1", "T", [], [
      act("p", { label: "Pay", color: "blue", icon: "door-sliding-lock" }),
      act("q", { label: "Quiet", color: "red", icon: "gate-arrow-left", show_label: false }),
      act("r", { label: "Plain", kind: "lock_open", entity_id: "lock.x", confirm: true }),
      act("s", { label: "Off", color: "green", available: false }),
      act("t", { label: "Odd", color: "chartreuse", icon: "Not A Name!" }),
      act("u", { label: "Talk to door", kind: "talk", device_id: "gate.example", available: false, color: "teal" }),
    ])]);
    const { card, fake } = await mount({ _armMs: 60 }, { doors: [], content });
    const btn = (id) => qa(card, "button.act").find((b) => b.dataset.action === id);
    assert.deepStrictEqual(qa(card, "button.act").map((b) => b.dataset.action), ["p", "q", "r", "s", "t"], "talk is not drawn");
    assert.strictEqual(btn("p").querySelector("ha-icon").getAttribute("icon"), "mdi:door-sliding-lock", "a new (+19) icon name is drawn");
    assert.strictEqual(btn("q").querySelector("ha-icon").getAttribute("icon"), "mdi:gate-arrow-left");
    assert.strictEqual(btn("p").textContent, "Pay");
    assert.ok(btn("p").classList.contains("c-blue"));
    assert.strictEqual(btn("q").textContent, "", "show_label false: no text");
    assert.strictEqual(btn("q").getAttribute("aria-label"), "Quiet", "the label is the content description");
    assert.strictEqual(btn("q").title, "Quiet");
    assert.strictEqual(btn("r").querySelector("ha-icon").getAttribute("icon"), "mdi:lock-open-variant", "no icon: the kind's");
    assert.ok(!/\bc-/.test(btn("r").className), "no colour: the theme's button");
    assert.ok(btn("s").disabled && btn("s").classList.contains("c-grey") && !btn("s").classList.contains("c-green"), "disabled is grey");
    assert.ok(!/\bc-/.test(btn("t").className), "an unknown colour name draws as no colour");
    assert.strictEqual(btn("t").querySelector("ha-icon").getAttribute("icon"), "mdi:script-text-play", "an unusable icon name falls back to the kind's");
    const css = card.shadowRoot.querySelector("style").textContent;
    assert.ok(/\.acts\.blk \.act\s*\{[^}]*min-height:\s*48px[^}]*min-width:\s*48px/.test(css), "touch targets are at least 44px");
    // confirm behaves as today, and an armed icon-only button says what the second tap does
    btn("r").click();
    await sleep(5);
    assert.strictEqual(fake.ofType("portico/content/run").length, 0);
    assert.strictEqual(btn("r").textContent, "Tap again: Plain");
    btn("q").click();
    await sleep(5);
    assert.deepStrictEqual(fake.ofType("portico/content/run").map((m) => m.action_id), ["q"], "a plain button runs at once, by id");
    assert.ok(q(card, ".notice").textContent.length > 0);
    await sleep(90);
    assert.strictEqual(btn("r").textContent, "Plain", "the arming expired");
    btn("r").click();
    await sleep(5);
    btn("r").click();
    await sleep(10);
    assert.deepStrictEqual(fake.ofType("portico/content/run").map((m) => m.action_id), ["q", "r"]);
    console.log("PASS: blocks: buttons draw icon, label, palette colour, show_label, accessible name; talk hidden; confirm as before");
    card.remove();
  }

  // 22. an icon-only armed button shows its text; unavailable button runs nothing
  {
    const content = withBlocks([blk("b1", "T", [], [act("q", { label: "Quiet", kind: "lock_open", entity_id: "lock.q", confirm: true, icon: "lock-smart", show_label: false }),
      act("z", { label: "Z", available: false })])]);
    const { card, fake } = await mount({}, { doors: [], content });
    const btn = (id) => qa(card, "button.act").find((b) => b.dataset.action === id);
    btn("q").click();
    await sleep(5);
    assert.strictEqual(btn("q").textContent, "Tap again: Quiet");
    btn("z").click();
    await sleep(5);
    assert.strictEqual(fake.ofType("portico/content/run").length, 0);
    console.log("PASS: blocks: an armed icon-only button says what the second tap does; an unavailable one runs nothing");
    card.remove();
  }

  // 23. the palette: the card's copy equals contract/palette_v1.json, light and dark; dark follows hass.themes.darkMode
  {
    const fs = (await import("fs")).default;
    const file = ["/src/contract/palette_v1.json", new URL("../../../../../contract/palette_v1.json", import.meta.url).pathname].find((f) => fs.existsSync(f));
    assert.ok(file, "contract/palette_v1.json is reachable from the test");
    const palette = JSON.parse(fs.readFileSync(file, "utf8")).colors;
    const content = withBlocks([blk("b1", "T", [], palette.map((c) => act(c.name, { color: c.name })))]);
    const { card } = await mount({}, { doors: [], content });
    const css = card.shadowRoot.querySelector("style").textContent;
    for (const c of palette) {
      assert.ok(css.includes(`.act.c-${c.name} { --pt-fill: ${c.light.fill}; --pt-text: ${c.light.text}; }`), `light ${c.name}`);
      assert.ok(css.includes(`:host([dark]) .act.c-${c.name} { --pt-fill: ${c.dark.fill}; --pt-text: ${c.dark.text}; }`), `dark ${c.name}`);
    }
    assert.strictEqual((css.match(/\.act\.c-/g) || []).length, palette.length * 2, "no colour beyond the palette");
    assert.ok(!card.hasAttribute("dark"), "light by default");
    card.hass = { ...card._hass, themes: { darkMode: true } };
    assert.ok(card.hasAttribute("dark"), "dark follows hass.themes.darkMode");
    card.hass = { ...card._hass, themes: { darkMode: false } };
    assert.ok(!card.hasAttribute("dark"));
    console.log("PASS: blocks: the palette equals contract/palette_v1.json in light and dark and follows the HA theme");
    card.remove();
  }

  // 24. an older server (no `blocks`): the flat form exactly as before, no width, no sections
  {
    const { card, fake } = await mount({}, { doors: [], content: CONTENT });
    assert.strictEqual(qa(card, "section.block").length, 0);
    assert.strictEqual(qa(card, ".cams.blk, .acts.blk, ha-icon").length, 0);
    assert.strictEqual(qa(card, ".cam.big, .cam.small").length, 0);
    await sleep(60);
    assert.ok(fake.ofType("portico/content/snapshot").every((m) => !("width" in m)), "no width on the flat form");
    // a page that has blocks: [] draws nothing from the flat arrays and says so
    const { card: c2 } = await mount({}, { doors: [], content: { ...CONTENT, pages: [{ ...CONTENT.pages[0], blocks: [] }] } });
    assert.strictEqual(qa(c2, "button.act").length, 0);
    assert.ok(q(c2, ".muted").textContent.startsWith("Nothing chosen"));
    console.log("PASS: blocks: a reply without blocks falls back to the flat form, with no width; an empty block list says so");
    card.remove();
    c2.remove();
  }

  // 25. tabs with blocks: each page draws its own blocks and only the current page is polled
  {
    const content = withBlocks([blk("b1", "One", [cam("a", "big")])]);
    content.pages.push({ id: "two", label: "Two", blocks: [blk("b2", "Two", [cam("z", "big")], [act("zz")])], cameras: [cam("z", "big")], actions: [act("zz")] });
    const { card, fake } = await mount({}, { doors: [], content });
    await sleep(50);
    assert.deepStrictEqual(Object.keys(sizeOf(fake)), ["a"]);
    qa(card, ".tab").find((t) => t.dataset.page === "two").click();
    await sleep(60);
    assert.deepStrictEqual(qa(card, ".block-title").map((h) => h.textContent), ["Two"]);
    assert.ok(Object.keys(sizeOf(fake)).includes("z"));
    console.log("PASS: blocks: tabs switch the blocks drawn and the cameras polled");
    card.remove();
  }

  // 26. the ring paths do not change with blocks: no width on a ring still, the door buttons are the flat mirror's
  {
    const content = withBlocks([blk("b1", "T", [cam("gate", "big")], [
      act("open-gate", { label: "Gate relay", kind: "portico_open", device_id: "gate.example", confirm: true }),
      act("talk", { label: "Talk", kind: "talk", device_id: "gate.example", available: false })])]);
    const { card, fake } = await mount({ takeover: true }, { doors: [DOOR_NO_AUDIO], content });
    await sleep(30);
    ring(fake, DOOR_NO_AUDIO);
    const n = fake.ofType("portico/content/snapshot").length;
    await sleep(60);
    assert.ok(overlay(), "the ring opens the overlay");
    const buttons = Array.from(overlayRoot().querySelectorAll("button")).map((b) => b.textContent);
    assert.deepStrictEqual(buttons, ["Gate relay", "Dismiss"], "the ring shows the door buttons, never Talk");
    const during = fake.ofType("portico/content/snapshot").slice(n);
    assert.ok(during.length >= 1 && during.every((m) => !("width" in m)), "ring snapshots carry no width");
    assert.strictEqual(overlayRoot().querySelectorAll("ha-icon").length, 0, "the overlay is not redrawn as blocks");
    card.remove();
    // and a door with a `callers` entry: its stills are asked for as before, with no width, big or not
    const withCallers = withBlocks([blk("b1", "T", [cam("gate", "big")])], {
      callers: { "gate_sip:gate.example": { name: "Vehicle gate", kind: "door", cameras: [{ id: "c-bbbbbbbbbbbb", label: "Gate", name: "g", available: true }],
                                            ring_buttons: [act("a-gate", { label: "Open gate" })], call_buttons: [] } },
    });
    const m2 = await mount({ takeover: true }, { doors: [DOOR_NO_AUDIO], content: withCallers });
    await sleep(30);
    ring(m2.fake, DOOR_NO_AUDIO);
    const n2 = m2.fake.ofType("portico/content/snapshot").length;
    await sleep(60);
    const ringSnaps = m2.fake.ofType("portico/content/snapshot").slice(n2);
    assert.ok(ringSnaps.length >= 1 && ringSnaps.every((m) => m.camera_id === "c-bbbbbbbbbbbb" && !("width" in m)), JSON.stringify(ringSnaps));
    m2.card.remove();
    console.log("PASS: blocks: the ring overlay is unchanged: door buttons from the blocks, no Talk, no width, with or without a callers entry");
  }

  // 27. B8: a reply that has blocks never needs the flat fallback; a stale flat list beside blocks is not read
  {
    const reply = withBlocks([blk("b1", "Front", [cam("gate", "big"), cam("yard", "small")], [act("go")]),
                              blk("b2", "Back", [cam("lawn", "big")], [act("lights")])]);
    assert.ok(reply.pages.every((p) => !("cameras" in p) && !("actions" in p)), "the reply has no flat lists at all");
    const { card, fake } = await mount({}, { doors: [], content: reply });
    assert.deepStrictEqual(qa(card, "section.block h3.block-title").map((h) => h.textContent), ["Front", "Back"]);
    assert.deepStrictEqual(tileClasses(card), ["gate:big", "yard:small", "lawn:big"]);
    assert.strictEqual(qa(card, "button.act").length, 2);
    await sleep(60);
    assert.deepStrictEqual([...new Set(fake.ofType("portico/content/snapshot").map((m) => m.camera_id))].sort(), ["gate", "lawn", "yard"]);
    // the whole-document readers (the ring's fallback when no callers entry names the door) read the blocks too
    assert.deepStrictEqual(card._allCameras().map((c) => c.id), ["gate", "yard", "lawn"]);
    assert.deepStrictEqual(card._allActions().map((a) => a.id), ["go", "lights"]);
    // a stale flat form beside blocks (a server from before B8 that was edited by hand, say) is ignored, not merged
    const stale = { ...reply, pages: [{ ...reply.pages[0], cameras: [cam("ghost", "big")], actions: [act("ghost-act")] }] };
    const m2 = await mount({}, { doors: [], content: stale });
    assert.deepStrictEqual(tileClasses(m2.card), ["gate:big", "yard:small", "lawn:big"], "blocks win; the flat list is not drawn");
    assert.ok(!qa(m2.card, "button.act").some((b) => b.textContent.includes("ghost")));
    assert.deepStrictEqual(m2.card._allCameras().map((c) => c.id), ["gate", "yard", "lawn"]);
    console.log("PASS: blocks: a blocks-only reply draws, polls and answers the ring fallback without any flat list; a stale flat list is ignored");
    card.remove();
    m2.card.remove();
  }

  // 28. an empty camera name draws no name at all: no label node, a neutral accessible name on the picture
  {
    const reply = withBlocks([blk("b1", "Front", [{ ...cam("gate", "big"), label: "" }, cam("yard", "small")], [])]);
    const { card } = await mount({}, { doors: [], content: reply });
    const tiles = qa(card, ".cam");
    assert.strictEqual(tiles.length, 2);
    assert.strictEqual(tiles[0].querySelector(".label"), null, "no name element for an empty name");
    assert.strictEqual(tiles[0].children.length, 1, "the tile is the picture and nothing else (no padded empty line)");
    assert.strictEqual(tiles[0].querySelector("img").alt, "Camera");
    assert.strictEqual(tiles[1].querySelector(".label").textContent, "YARD");
    assert.strictEqual(tiles[1].querySelector("img").alt, "YARD");
    console.log("PASS: a camera with an empty name draws no name element; its picture keeps the alt text Camera");
    card.remove();
  }

  console.log("ALL_CARD_CONTENT_TESTS_PASSED");
}

main().catch((err) => {
  console.error("TEST FAILED:", err);
  process.exit(1);
});
