// Live video on the card (portico/content/live, live_ice, live_close): a camera tile with `live` (or tapped live) plays WebRTC
// video, video only, over its still. jsdom, with the websocket, RTCPeerConnection, IntersectionObserver and the <video>
// element faked (card_loader.mjs). This proves the card's LOGIC (the offer, the signalling, the sessions' lives, the fallback);
// it does NOT prove a real WebView plays the video.
import assert from "assert";
import { loadCard, sleep, makeHass } from "./card_loader.mjs";

const { dom, PorticoCard, state } = loadCard({ withIO: true });
assert.ok(PorticoCard, "portico-card was not registered");
const doc = dom.window.document;

const cam = (id, label, extra = {}) => ({ id, label, entity_id: "camera." + id, name: label, available: true, ...extra });
function page(cameras, extra = {}) {
  return {
    endpoint: "ep-alice", kind: "phone", home_name: "Home", rev: "r1",
    pages: [{ id: "home", label: "Home", blocks: [{ id: "b1", title: "Cameras", cameras, actions: [] }] }],
    callers: {}, roster: [], ...extra,
  };
}

/** Mounts a card on a fake hass. `live: null` = a server without the command. The tiles count as on screen unless `visible: false`. */
async function mount({ config = {}, content, live = {}, doors = [], visible = true, settle = 60 } = {}) {
  dom.window.localStorage.clear();
  state.pcs.length = 0;
  const fake = makeHass({ doors, content, live: live === null ? undefined : live });
  const card = doc.createElement("portico-card");
  doc.body.appendChild(card);
  card.setConfig({ _pollMs: 20, _snapshotMs: 30, _ringSnapshotMs: 20, _liveRetryMs: [30, 60], _liveFpsMs: 40, ...config });
  card.hass = fake.hass;
  await sleep(settle);
  if (visible) { state.io.show(state.io.all(), true); await sleep(40); }
  return { card, fake };
}
const type = (fake, t) => fake.sent.filter((m) => m.type === t);
const cmds = (fake) => type(fake, "portico/content/live");
const closes = (fake) => type(fake, "portico/content/live_close");
const ices = (fake) => type(fake, "portico/content/live_ice");
const livePCs = () => state.pcs.filter((p) => p.isLive);
const videos = (card) => Array.from(card.shadowRoot.querySelectorAll(".pic video"));
const open = (fake) => fake.liveSessions.filter((s) => !s.unsubscribed).length;
const tile = (card, id) => Array.from(card.shadowRoot.querySelectorAll(".cam")).find((t) => t.querySelector(`img[data-camera="${id}"]`));
const fresh = (n) => ({ streams: [{ id: "stream-" + n }] });
const playing = (video) => video.dispatchEvent(new dom.window.Event("playing"));
const hide = (hidden) => {
  Object.defineProperty(doc, "visibilityState", { configurable: true, get: () => (hidden ? "hidden" : "visible") });
  doc.dispatchEvent(new dom.window.Event("visibilitychange"));
};

async function main() {
  // 1. the offer is ONE receive-only video line and no audio; no microphone; the size comes from the tile
  {
    const { card, fake } = await mount({ content: page([cam("a", "A", { live: true, size: "big" }), cam("b", "B", { live: true, size: "small" }), cam("c", "C", { size: "small" })]) });
    const sent = cmds(fake);
    assert.deepStrictEqual(sent.map((m) => [m.camera_id, m.size]).sort(), [["a", "big"], ["b", "small"]], "only the live tiles ask; Big -> big, Small -> small");
    for (const m of sent) {
      assert.deepStrictEqual(Object.keys(m).sort(), ["camera_id", "id", "sdp", "size", "type"], "no endpoint on the user's own card, nothing else");
      assert.ok(/^m=video /m.test(m.sdp) && /^a=recvonly/m.test(m.sdp), "a receive-only video line");
      assert.strictEqual((m.sdp.match(/^m=/gm) || []).length, 1, "exactly one media line");
      assert.ok(!/m=audio/.test(m.sdp), "NO audio line");
    }
    assert.strictEqual(livePCs().length, 2);
    for (const pc of livePCs()) {
      assert.deepStrictEqual(pc.transceivers, [{ kind: "video", init: { direction: "recvonly" } }]);
      assert.strictEqual(pc.tracks.length, 0, "no local track is ever added");
    }
    assert.strictEqual(state.getUserMediaCalls, 0, "no microphone is asked for");
    assert.strictEqual(fake.liveSessions[0].options.resubscribe, false, "the library must not replay a live offer on reconnect");
    console.log("PASS: the offer is one recvonly video line and no audio line; no microphone; Big asks for big and Small for small");
    card.remove();
  }

  // 2. the answer and the server's candidates are applied; ours go out through live_ice, incl. the end-of-candidates mark
  {
    const { card, fake } = await mount({ content: page([cam("a", "A", { live: true, size: "big" })]) });
    const pc = livePCs()[0];
    fake.liveEvent(0, { type: "answer", sdp: "v=0 answer" });
    await sleep(10);
    assert.deepStrictEqual(pc.remoteDescription, { type: "answer", sdp: "v=0 answer" });
    const c1 = { candidate: "candidate:1 1 udp 1 10.0.0.2 5000 typ host", sdpMid: "0", sdpMLineIndex: 0 };
    fake.liveEvent(0, { type: "candidate", candidate: c1 });
    await sleep(10);
    assert.deepStrictEqual(pc.addedCandidates, [c1]);
    const mine = { candidate: "candidate:2 1 udp 2 10.0.0.9 6000 typ host", sdpMid: "0", sdpMLineIndex: 0 };
    pc.emitCandidate(mine);
    pc.emitCandidate(null);
    await sleep(10);
    assert.deepStrictEqual(ices(fake).map((m) => [m.session_id, m.candidate]),
      [["sess-0", mine], ["sess-0", { candidate: "" }]], "each candidate, then the empty end mark, for the session the result named");
    console.log("PASS: the answer and candidates are applied; local candidates and the end mark go out through live_ice");
    card.remove();
  }

  // 3. candidates found before the result arrives wait for the session id, in order
  {
    const { card, fake } = await mount({ content: page([cam("a", "A", { live: true, size: "big" })]), live: { resultDelayMs: 120 }, settle: 40 });
    await sleep(10);
    const pc = livePCs()[0];
    const early = { candidate: "candidate:3 1 udp 3 10.0.0.9 7000 typ host", sdpMid: "0", sdpMLineIndex: 0 };
    pc.emitCandidate(early);
    pc.emitCandidate(null);
    await sleep(10);
    assert.strictEqual(ices(fake).length, 0, "no session id yet: nothing is sent");
    await sleep(160);
    assert.deepStrictEqual(ices(fake).map((m) => m.candidate), [early, { candidate: "" }], "flushed in order once the id is known");
    console.log("PASS: candidates found before the result are queued and sent in order");
    card.remove();
  }

  // 4. the video: a <video muted playsinline autoplay> in the tile over the still, shown only when it plays
  {
    const { card, fake } = await mount({ content: page([cam("a", "A", { live: true, size: "big" })]) });
    const pc = livePCs()[0];
    const stream = { id: "live-stream" };
    pc.emitTrack(stream);
    const v = videos(card)[0];
    assert.ok(v && v.parentElement.classList.contains("pic") && v.parentElement.querySelector("img[data-camera='a']"), "the video sits in the picture frame beside the still");
    assert.strictEqual(v.srcObject, stream);
    assert.ok(v.muted && v.autoplay && v.playsInline && v.hasAttribute("playsinline") && v.hasAttribute("muted"), "muted, inline, autoplay");
    assert.ok(!v.hasAttribute("controls"));
    assert.strictEqual(v.style.opacity, "0", "hidden until a frame plays: the still is the poster");
    assert.ok(v._plays >= 1, "play() was asked for");
    playing(v);
    assert.strictEqual(v.style.opacity, "1");
    assert.ok(card.shadowRoot.querySelector("img[data-camera='a']"), "the still stays underneath");
    assert.ok(!card._remoteAudio.srcObject, "the call's audio element is never touched");
    // a playing tile stops asking for stills
    await sleep(80);
    const snaps = type(fake, "portico/content/snapshot").length;
    await sleep(120);
    assert.strictEqual(type(fake, "portico/content/snapshot").length, snaps, "no stills are fetched for a tile that plays");
    console.log("PASS: the tile plays a muted inline video over its still, shown once it plays; no stills are fetched meanwhile");
    card.remove();
  }

  // 5. every failure falls back to stills, silently
  {
    // a server without the command
    const { card, fake } = await mount({ content: page([cam("a", "A", { live: true, size: "big" })]), live: null });
    await sleep(120);
    assert.strictEqual(fake.liveSent.length, 1, "asked once, not again");
    assert.ok(card.shadowRoot.querySelector("img[data-camera='a']"));
    assert.ok(!videos(card).some((v) => v.srcObject), "no video plays");
    assert.ok(!/error|fail/i.test(card.shadowRoot.querySelector(".content").textContent), "no error text over the picture");
    assert.ok(type(fake, "portico/content/snapshot").length > 0, "stills keep coming");
    card.remove();
    // each answer to the command itself is final: no retry
    for (const code of ["unauthorized", "unknown_endpoint", "unknown_camera", "unavailable", "too_many_live", "invalid_format", "live_unavailable"]) {
      const m = await mount({ content: page([cam("a", "A", { live: true, size: "big" })]), live: { error: () => ({ code, message: "x" }) } });
      await sleep(120);
      assert.strictEqual(m.fake.liveSent.length, 1, `${code}: no retry`);
      assert.strictEqual(closes(m.fake).length, 0, `${code}: no session, so nothing to close`);
      assert.ok(!/error|fail|live_/i.test(m.card.shadowRoot.querySelector(".content").textContent), `${code}: nothing is said`);
      m.card.remove();
    }
    // an anything-else failure of the command (a lost connection) is retried
    {
      let n = 0;
      const m = await mount({ content: page([cam("a", "A", { live: true, size: "big" })]), live: { error: () => (++n < 2 ? { code: 1, message: "lost" } : null) } });
      await sleep(120);
      assert.strictEqual(m.fake.liveSent.length, 2, "a lost connection is retried");
      assert.strictEqual(open(m.fake), 1);
      m.card.remove();
    }
    console.log("PASS: a missing command and every result error leave the stills alone, say nothing and do not retry");
  }

  // 6. an error event, a failed or closed connection and a stuck connection retry with backoff, then give up on stills
  {
    const { card, fake } = await mount({ content: page([cam("a", "A", { live: true, size: "big" })]) });
    assert.strictEqual(cmds(fake).length, 1);
    fake.liveEvent(0, { type: "error", code: "live_unavailable", message: "x" });
    await sleep(15);
    assert.strictEqual(cmds(fake).length, 1, "not at once");
    await sleep(40);
    assert.strictEqual(cmds(fake).length, 2, "after the first delay");
    assert.strictEqual(closes(fake).length, 1, "the failed session was closed");
    // the second session comes up and plays: the counter starts again
    livePCs()[1].emitTrack(fresh(1));
    playing(videos(card)[0]);
    livePCs()[1].emitState("failed");
    await sleep(15);
    assert.strictEqual(cmds(fake).length, 2);
    await sleep(40);
    assert.strictEqual(cmds(fake).length, 3, "a connection that played and then failed starts from the first delay");
    // now it never plays: the second delay is 60 ms, and after the last delay it stays on stills
    livePCs()[2].emitState("closed");
    await sleep(40);
    assert.strictEqual(cmds(fake).length, 3, "the second delay is longer");
    await sleep(60);
    assert.strictEqual(cmds(fake).length, 4);
    livePCs()[3].emitState("failed");
    await sleep(150);
    assert.strictEqual(cmds(fake).length, 4, "after the last delay it stays on stills");
    assert.strictEqual(open(fake), 0, "every session is closed");
    assert.strictEqual(closes(fake).length, 4, "one live_close per session");
    assert.ok(!videos(card).some((v) => v.srcObject), "no video is left playing");
    // a tap on a tile that should be live but gave up asks again instead of switching it off
    tile(card, "a").click();
    await sleep(40);
    assert.strictEqual(cmds(fake).length, 5, "the tap retries");
    assert.strictEqual(card.shadowRoot.querySelector(".cam").getAttribute("aria-pressed"), "true");
    console.log("PASS: error events and failed connections retry with backoff (and reset after a good run), then stay on stills; a tap retries");
    card.remove();
  }

  // 7. a connection that never plays, and a disconnected one that does not recover, are failures too
  {
    const { card, fake } = await mount({ config: { _liveConnectMs: 200, _liveDisconnectMs: 100, _liveRetryMs: [20] }, content: page([cam("a", "A", { live: true, size: "big" })]) });
    assert.strictEqual(cmds(fake).length, 1);
    await sleep(260);
    assert.strictEqual(cmds(fake).length, 2, "no first frame in time: a failure, retried");
    livePCs()[1].emitTrack(fresh(1));
    playing(videos(card)[0]);
    livePCs()[1].emitState("disconnected");
    livePCs()[1].emitState("connected");
    await sleep(160);
    assert.strictEqual(cmds(fake).length, 2, "a disconnect that recovered is no failure");
    livePCs()[1].emitState("disconnected");
    await sleep(160);
    assert.strictEqual(cmds(fake).length, 3, "a disconnect that did not recover is");
    card.remove();
    console.log("PASS: no first frame in time and an unrecovered disconnect are failures");
  }

  // 8. a tap toggles the tile for this viewer, and the choice is remembered per browser
  {
    const m = await mount({ content: page([cam("a", "A", { size: "big" }), cam("b", "B", { live: true, size: "small" })]) });
    let { card, fake } = m;
    assert.deepStrictEqual(cmds(fake).map((x) => x.camera_id), ["b"], "the document is the default");
    tile(card, "a").click();
    await sleep(40);
    assert.deepStrictEqual(cmds(fake).map((x) => x.camera_id), ["b", "a"], "a tap on a still tile starts it");
    assert.strictEqual(dom.window.localStorage.length, 1, "remembered");
    tile(card, "b").click();
    await sleep(40);
    assert.strictEqual(open(fake), 1, "a tap on a live tile stops it");
    assert.strictEqual(closes(fake).length, 1);
    assert.strictEqual(fake.liveSessions[0].closedByClient, true);
    // a new page load (a new card, the same browser) starts from the choices
    card.remove();
    const again = await mountKeepingStorage(card, fake);
    assert.deepStrictEqual(cmds(again.fake).map((x) => x.camera_id), ["a"], "a is live again and b stays off, as tapped");
    // tapping back to the document's setting forgets the choice
    tile(again.card, "a").click();
    await sleep(30);
    assert.strictEqual(dom.window.localStorage.length, 1, "a is back at the document's setting; b's choice stays");
    again.card.remove();
    // a blocked storage still lets the tap work for the life of the page
    const blocked = await mount({ content: page([cam("a", "A", { size: "big" })]) });
    const storage = Object.getOwnPropertyDescriptor(dom.window, "localStorage");
    Object.defineProperty(dom.window, "localStorage", { configurable: true, get() { throw new Error("blocked"); } });
    tile(blocked.card, "a").click();
    await sleep(40);
    assert.strictEqual(cmds(blocked.fake).length, 1, "the tap worked with the storage blocked");
    tile(blocked.card, "a").click();
    await sleep(30);
    assert.strictEqual(open(blocked.fake), 0);
    Object.defineProperty(dom.window, "localStorage", storage);
    blocked.card.remove();
    console.log("PASS: a tap switches a tile live/still, the choice is remembered in localStorage (and works when it is blocked)");
  }

  // 9. a tile completely off screen is not streamed; it comes back when it intersects
  {
    const { card, fake } = await mount({ content: page([cam("a", "A", { live: true, size: "big" }), cam("b", "B", { live: true, size: "small" })]), visible: false });
    await sleep(40);
    assert.strictEqual(cmds(fake).length, 0, "nothing is streamed before the tiles are seen on screen");
    state.io.show(state.io.byKey("page:a|big"), true);
    await sleep(40);
    assert.deepStrictEqual(cmds(fake).map((x) => x.camera_id), ["a"], "only the tile that intersects");
    assert.ok(state.io.every((io) => io.options.threshold === 0), "threshold 0: any pixel counts");
    state.io.show(state.io.byKey("page:a|big"), false);
    await sleep(30);
    assert.strictEqual(open(fake), 0, "scrolled out of view: the session is closed");
    assert.strictEqual(closes(fake).length, 1);
    assert.strictEqual(livePCs()[0].closed, true);
    assert.ok(card.shadowRoot.querySelector("img[data-camera='a']"), "the still remains");
    state.io.show(state.io.byKey("page:a|big"), true);
    await sleep(40);
    assert.strictEqual(cmds(fake).length, 2);
    assert.strictEqual(open(fake), 1, "back in view: it opens again");
    card.remove();
    console.log("PASS: a tile that is completely off screen is not streamed and reopens when it intersects");
  }

  // 10. a hidden page closes every session; a visible one reopens them
  {
    const { card, fake } = await mount({ content: page([cam("a", "A", { live: true, size: "big" }), cam("b", "B", { live: true, size: "small" })]) });
    assert.strictEqual(open(fake), 2);
    hide(true);
    await sleep(30);
    assert.strictEqual(open(fake), 0);
    assert.strictEqual(closes(fake).length, 2);
    hide(false);
    await sleep(40);
    assert.strictEqual(open(fake), 2, "visible again: both are reopened");
    assert.strictEqual(cmds(fake).length, 4);
    card.remove();
    delete doc.visibilityState;
    console.log("PASS: a hidden page closes every session and a visible one reopens them");
  }

  // 11. a re-render does not reopen a healthy session: the same <video> moves into the new tile
  {
    const { card, fake } = await mount({ content: page([cam("a", "A", { live: true, size: "big" })]) });
    livePCs()[0].emitTrack(fresh(0));
    const v = videos(card)[0];
    playing(v);
    const stream = v.srcObject;
    const before = cmds(fake).length;
    for (let i = 0; i < 4; i++) card._render();
    card._say("a notice");                              // an ordinary re-render source
    fake.fire({ type: "content_changed", endpoint: "ep-alice", rev: "r2" });     // a refetch of the same document
    await sleep(60);
    card._render();
    assert.strictEqual(cmds(fake).length, before, "no new live command");
    assert.strictEqual(fake.liveSessions[0].unsubscribed, 0, "never unsubscribed");
    assert.strictEqual(closes(fake).length, 0, "never closed");
    assert.strictEqual(livePCs().length, 1);
    assert.strictEqual(livePCs()[0].closed, undefined);
    assert.strictEqual(videos(card).length, 1);
    assert.strictEqual(videos(card)[0], v, "the very same <video> element");
    assert.strictEqual(v.srcObject, stream, "still attached to the same stream");
    assert.strictEqual(v.style.opacity, "1");
    assert.ok(!v.paused, "and playing");
    // a size change (the layout moved the tile to another size) is another session: the old one closes, a new one opens
    card._content.pages[0].blocks[0].cameras[0].size = "small";
    card._render();
    state.io.show(state.io.all(), true);          // a real observer reports every node it is given
    await sleep(40);
    assert.strictEqual(closes(fake).length, 1);
    assert.deepStrictEqual(cmds(fake).map((m) => m.size), ["big", "small"]);
    assert.strictEqual(open(fake), 1);
    card.remove();
    console.log("PASS: re-rendering keeps the session and its video; a size change replaces it");
  }

  // 12. tearing down closes every session once: removal of the card, the page unloading, a tile that leaves, a document change
  {
    const { card, fake } = await mount({ content: page([cam("a", "A", { live: true, size: "big" }), cam("b", "B", { live: true, size: "small" }), cam("c", "C", { live: true, size: "small" })]) });
    assert.strictEqual(open(fake), 3);
    card.remove();
    await sleep(20);
    assert.strictEqual(open(fake), 0, "every subscription is released");
    assert.deepStrictEqual(closes(fake).map((m) => m.session_id).sort(), ["sess-0", "sess-1", "sess-2"], "one live_close per session, none twice");
    assert.ok(livePCs().every((pc) => pc.closed === true));
    // the page unloading
    const m = await mount({ content: page([cam("a", "A", { live: true, size: "big" })]) });
    dom.window.dispatchEvent(new dom.window.Event("pagehide"));
    await sleep(20);
    assert.strictEqual(open(m.fake), 0);
    assert.strictEqual(closes(m.fake).length, 1);
    m.card.remove();
    await sleep(20);
    assert.strictEqual(closes(m.fake).length, 1, "removing the card after the page hid does not close it twice");
    // a document change that takes `live` off a tile
    const d = await mount({ content: page([cam("a", "A", { live: true, size: "big" })]) });
    const changed = page([cam("a", "A", { size: "big" })], { rev: "r2" });
    d.card._content = changed;
    d.card._render();
    await sleep(30);
    assert.strictEqual(open(d.fake), 0, "live taken off in the editor: the tile closes");
    assert.strictEqual(closes(d.fake).length, 1);
    d.card.remove();
    // a tile that leaves with its page
    const p = await mount({ content: { ...page([cam("a", "A", { live: true, size: "big" })]), pages: [
      { id: "home", label: "Home", blocks: [{ id: "b1", title: "T", cameras: [cam("a", "A", { live: true, size: "big" })], actions: [] }] },
      { id: "two", label: "Two", blocks: [{ id: "b2", title: "T2", cameras: [cam("z", "Z", { size: "big" })], actions: [] }] }] } });
    assert.strictEqual(open(p.fake), 1);
    Array.from(p.card.shadowRoot.querySelectorAll("button.tab")).find((b) => b.dataset.page === "two").click();
    await sleep(30);
    assert.strictEqual(open(p.fake), 0, "the page changed: the tile that left is closed");
    assert.strictEqual(closes(p.fake).length, 1);
    p.card.remove();
    console.log("PASS: every session ends once: card removal, page unload, a tile taken off live, a tile that left with its page");
  }

  // 13. no raw socket, or a server that names the session in an event: still works
  {
    const a = await mount({ content: page([cam("a", "A", { live: true, size: "big" })]), live: { socket: false, sessionEvent: true } });
    livePCs()[0].emitCandidate({ candidate: "c" });
    a.card.remove();
    await sleep(20);
    assert.strictEqual(ices(a.fake)[0].session_id, "sess-0", "the id from a session event");
    assert.strictEqual(closes(a.fake).length, 1);
    const b = await mount({ content: page([cam("a", "A", { live: true, size: "big" })]), live: { socket: false } });
    livePCs()[0].emitCandidate({ candidate: "c" });
    await sleep(10);
    assert.strictEqual(ices(b.fake).length, 0, "no session id to name: nothing is sent");
    b.fake.liveEvent(0, { type: "answer", sdp: "v=0 a" });
    await sleep(10);
    assert.deepStrictEqual(livePCs()[0].remoteDescription, { type: "answer", sdp: "v=0 a" }, "the answer is applied all the same");
    b.card.remove();
    await sleep(20);
    assert.strictEqual(b.fake.liveSessions[0].unsubscribed, 1, "the unsubscribe still ends the session");
    assert.strictEqual(closes(b.fake).length, 0);
    console.log("PASS: the session id may come in an event; without any, the video still connects and the unsubscribe closes it");
  }

  // 14. frame rate: shown only when the document asks; '--' until known; requestVideoFrameCallback where it exists
  {
    const proto = dom.window.HTMLVideoElement.prototype;
    proto.requestVideoFrameCallback = function requestVideoFrameCallback(cb) { (this._rvfc = this._rvfc || []).push(cb); return this._rvfc.length; };
    const frames = (v, n) => { for (let i = 0; i < n; i++) { const list = v._rvfc; v._rvfc = []; for (const cb of list) cb(); } };
    const off = await mount({ content: page([cam("a", "A", { live: true, size: "big" })]) });
    assert.ok(!off.card.shadowRoot.querySelector(".fps"), "no label unless show_fps");
    off.card.remove();
    const { card, fake } = await mount({ content: page([cam("a", "A", { live: true, size: "big" }), cam("s", "S", { size: "small" })], { show_fps: true }) });
    const labels = () => Array.from(card.shadowRoot.querySelectorAll(".fps")).map((n) => n.textContent);
    assert.deepStrictEqual(labels(), ["--"], "a label on the live tile only, '--' until known");
    livePCs()[0].emitTrack(fresh(0));
    const v = videos(card)[0];
    playing(v);
    frames(v, 8);
    await sleep(70);
    assert.ok(/^[1-9]\d* fps$/.test(labels()[0]), `measured from the frames: ${labels()[0]}`);
    card._render();                                     // a re-render keeps the number
    assert.ok(/^[1-9]\d* fps$/.test(labels()[0]), "kept across a re-render");
    await sleep(100);                                   // no frames now
    assert.strictEqual(labels()[0], "0 fps", "a stalled picture reads 0");
    assert.ok(card.shadowRoot.querySelector(".fps").textContent !== "", "textContent only");
    card.remove();
    delete proto.requestVideoFrameCallback;
    // getStats where there is no requestVideoFrameCallback
    state.stats = () => ({ r1: { type: "inbound-rtp", kind: "video", framesPerSecond: 23.6 }, r2: { type: "outbound-rtp", kind: "audio" } });
    const s = await mount({ content: page([cam("a", "A", { live: true, size: "big" })], { show_fps: true }) });
    livePCs()[0].emitTrack(fresh(0));
    playing(videos(s.card)[0]);
    await sleep(100);
    assert.strictEqual(s.card.shadowRoot.querySelector(".fps").textContent, "24 fps", "getStats framesPerSecond");
    s.card.remove();
    state.stats = null;
    console.log("PASS: the frame rate label shows only with show_fps, reads '--' until known, then the measured rate (frame callback or getStats)");
  }

  // 15. the ring: the caller's first camera goes live by default, with the same fallback; the page behind it waits
  {
    const GATE = { door_id: "gate.example", endpoint_id: "gate_sip:gate.example", door_ref: "d-2", driver: "gate_sip_observer", capabilities: { InboundAudio: 3, OutboundAudio: 3, DirectCapable: false }, name: "Vehicle gate", camera_entity_id: null, available: true, away_media: false };
    const content = page([cam("p-yard", "Yard", { live: true, size: "big" })], {
      endpoint: "kitchen", kind: "dashboard",
      callers: { "gate_sip:gate.example": { name: "Vehicle gate", kind: "door",
        cameras: [{ id: "c-bbbbbbbbbbbb", label: "Vehicle gate", name: "Gate cam", available: true }, { id: "c-cccccccccccc", label: "Gate 2", name: "Gate cam 2", available: true }],
        ring_buttons: [], call_buttons: [] } },
    });
    const { card, fake } = await mount({ config: { endpoint: "kitchen", takeover: true, fullpage: true }, content, doors: [GATE] });
    assert.deepStrictEqual(cmds(fake).map((m) => m.camera_id), ["p-yard"], "the page tile is live before a ring");
    assert.strictEqual(cmds(fake)[0].endpoint, "kitchen", "a screen names its endpoint");
    fake.fire({ type: "call_state_changed", door_id: GATE.door_id, call_id: "r1", state: "ringing", prev_state: "idle" });
    await sleep(60);
    state.io.show(state.io.all(), true);
    await sleep(60);
    const host = doc.querySelector("[data-portico-overlay]");
    assert.ok(host, "the overlay is up");
    assert.deepStrictEqual(cmds(fake).map((m) => [m.camera_id, m.size]), [["p-yard", "big"], ["c-bbbbbbbbbbbb", "big"]], "the caller's FIRST camera only");
    assert.ok(host.shadowRoot.querySelector(".pic video"), "its video sits in the overlay's picture frame");
    assert.strictEqual(fake.liveSessions[0].unsubscribed, 1, "the page tile behind the overlay was let go");
    // the overlay re-renders while ringing: the ring's session is kept
    const ringVideo = host.shadowRoot.querySelector("video");
    card._render();
    card._renderOverlay();
    await sleep(30);
    assert.strictEqual(cmds(fake).length, 2);
    assert.strictEqual(doc.querySelector("[data-portico-overlay]").shadowRoot.querySelector("video"), ringVideo);
    // the ring ends: its session closes and the page tile comes back
    fake.fire({ type: "call_state_changed", door_id: GATE.door_id, call_id: "r1", state: "ended", prev_state: "ringing" });
    await sleep(60);
    state.io.show(state.io.all(), true);
    await sleep(60);
    assert.ok(!doc.querySelector("[data-portico-overlay]"));
    assert.strictEqual(fake.liveSessions[1].unsubscribed, 1, "the ring's session is closed when the overlay goes");
    assert.deepStrictEqual(cmds(fake).map((m) => m.camera_id), ["p-yard", "c-bbbbbbbbbbbb", "p-yard"]);
    card.remove();
    // a failure at a ring is silent too
    const bad = await mount({ config: { endpoint: "kitchen", takeover: true, fullpage: true }, content, doors: [GATE], live: null });
    bad.fake.fire({ type: "call_state_changed", door_id: GATE.door_id, call_id: "r2", state: "ringing", prev_state: "idle" });
    await sleep(60);
    state.io.show(state.io.all(), true);
    await sleep(60);
    const o = doc.querySelector("[data-portico-overlay]");
    assert.ok(o && o.shadowRoot.querySelector("img[data-camera='c-bbbbbbbbbbbb']"), "the still is all there is");
    assert.ok(!/error|fail/i.test(o.shadowRoot.querySelector(".shade").textContent));
    bad.card.remove();
    console.log("PASS: a ring brings the caller's first camera live (same fallback), keeps it across re-renders, closes it with the overlay");
  }

  // 16. a call answered while the ring's picture is live keeps both, on connections of their own
  {
    const DOOR = { door_id: "vto.example", endpoint_id: "dahua:vto.example", door_ref: "d-1", driver: "dahua_sip", capabilities: { InboundAudio: 0, OutboundAudio: 0, DirectCapable: true }, name: "Front door", camera_entity_id: null, available: true, away_media: false };
    const content = page([cam("a", "A", { size: "big" })], {
      callers: { "dahua:vto.example": { name: "Front door", kind: "door", cameras: [{ id: "c-aaaaaaaaaaaa", label: "Front", name: "F", available: true }], ring_buttons: [], call_buttons: [] } },
    });
    const { card, fake } = await mount({ config: { takeover: true }, content, doors: [DOOR] });
    fake.fire({ type: "call_state_changed", door_id: DOOR.door_id, call_id: "k1", state: "ringing", prev_state: "idle" });
    await sleep(40);
    state.io.show(state.io.all(), true);
    await sleep(40);
    assert.strictEqual(open(fake), 1, "the ring's picture is live");
    const livePC = livePCs()[0];
    livePC.emitTrack({ id: "live-video" });
    const liveVideo = doc.querySelector("[data-portico-overlay]").shadowRoot.querySelector("video");
    playing(liveVideo);
    doc.querySelector("[data-portico-overlay]").shadowRoot.querySelector("#answer-btn").click();
    await sleep(60);
    assert.ok(card._pc && !card._pc.isLive && card._pc !== livePC, "the call has its own connection");
    assert.ok(state.getUserMediaCalls === 1 && state.localStream, "and the microphone");
    assert.strictEqual(open(fake), 1, "answering did not touch the live session");
    assert.strictEqual(cmds(fake).length, 1, "and asked for nothing new");
    assert.ok(!livePC.closed && liveVideo.srcObject && liveVideo.style.opacity === "1", "nor its connection or its video");
    assert.strictEqual(doc.querySelector("[data-portico-overlay]").shadowRoot.querySelector("video"), liveVideo, "the same video through the state change");
    assert.ok(!card._remoteAudio.srcObject || card._remoteAudio.srcObject !== liveVideo.srcObject, "the live video is never given to the call's audio element");
    card._teardownCall();                              // the call ends: the live view carries on until the ring does
    assert.strictEqual(card._pc, null);
    assert.ok(!livePC.closed && open(fake) === 1, "the end of the call does not end the live view");
    card.remove();
    await sleep(20);
    assert.strictEqual(open(fake), 0);
    console.log("PASS: a call has a connection of its own; the live view uses none of the call's and survives the answer");
  }
  // 17. a tile that goes while its result is still on the way: the late session is still closed, once
  {
    const { card, fake } = await mount({ content: page([cam("a", "A", { live: true, size: "big" })]), live: { resultDelayMs: 150 }, settle: 40 });
    await sleep(30);
    assert.strictEqual(cmds(fake).length, 1, "the offer is in flight");
    state.io.show(state.io.byKey("page:a|big"), false);          // scrolled away before the answer to the command came
    await sleep(40);
    assert.strictEqual(closes(fake).length, 0, "no session id yet: nothing to name");
    await sleep(260);
    assert.deepStrictEqual(closes(fake).map((m) => m.session_id), ["sess-0"], "closed by name once the id arrived");
    assert.strictEqual(fake.liveSessions[0].unsubscribed, 1);
    assert.strictEqual(open(fake), 0);
    assert.strictEqual(cmds(fake).length, 1, "and it did not come back");
    card.remove();
    console.log("PASS: a session whose tile went before its result arrived is closed by name, once");
  }
  console.log("ALL_CARD_LIVE_TESTS_PASSED");
}

// a second page load in the same browser: the same localStorage, a new card
async function mountKeepingStorage(oldCard, oldFake) {
  const fake = makeHass({ content: page([cam("a", "A", { size: "big" }), cam("b", "B", { live: true, size: "small" })]), live: {} });
  void oldCard; void oldFake;
  state.pcs.length = 0;
  const card = doc.createElement("portico-card");
  doc.body.appendChild(card);
  card.setConfig({ _pollMs: 20, _snapshotMs: 30, _liveRetryMs: [30, 60] });
  card.hass = fake.hass;
  await sleep(60);
  state.io.show(state.io.all(), true);
  await sleep(40);
  return { card, fake };
}

main().catch((err) => { console.error("TEST FAILED:", err); process.exit(1); });
