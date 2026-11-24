// The card's classic behaviour on contract 3 (no `endpoint`, no takeover): the handshake, doors, the two
// subscriptions, the ring / answer / hang up / open flows with door ids, the typed errors, the call-state
// snapshots and `ended`, the second-ring refusal in this plain mode (R7-19). jsdom, WebRTC mocked.
import assert from "assert";
import { loadCard, sleep, makeHass, DOOR_AUDIO, DOOR_NO_AUDIO } from "./card_loader.mjs";

const { dom, PorticoCard, state } = loadCard();
assert.ok(PorticoCard, "portico-card custom element was not registered");
const doc = dom.window.document;

async function mount(opts, config = {}) {
  const fake = makeHass(opts);
  const card = doc.createElement("portico-card");
  doc.body.appendChild(card);
  card.setConfig(config);
  card.hass = fake.hass;
  await sleep(25);
  return { card, fake };
}
const ev = (door, extra) => ({ type: "call_state_changed", door_id: door.door_id, ...extra });
const q = (card, sel) => card.shadowRoot.querySelector(sel);

async function main() {
  // 1. the handshake: version first, then doors, then the two subscriptions, then the content
  {
    const { card, fake } = await mount({ doors: [DOOR_AUDIO] });
    assert.deepStrictEqual(fake.sent.map((m) => m.type).slice(0, 5), [
      "portico/version", "portico/doors", "portico/calls/subscribe", "portico/content/subscribe", "portico/content/get",
    ]);
    assert.deepStrictEqual(fake.ofType("portico/content/get"), [{ type: "portico/content/get" }], "no endpoint: the user's own");
    assert.deepStrictEqual(fake.ofType("portico/content/subscribe"), [{ type: "portico/content/subscribe" }]);
    assert.ok(fake.subscribed(), "both subscriptions are held");
    assert.ok(fake.sent.every((m) => !("profile" in m) && !("device_id" in m)), "nothing from contract 2");
    console.log("PASS: init probes the version, reads doors, holds calls/subscribe and content/subscribe, asks content/get {}");
    card.remove();
    assert.ok(!fake.hasSubscriber(), "both subscriptions are released on removal");
  }

  // 2. a ring, Answer (door_id on answer and offer), in-call, hang up
  {
    const { card, fake } = await mount({ doors: [DOOR_AUDIO] });
    fake.fire(ev(DOOR_AUDIO, { call_id: "call-1", state: "ringing", prev_state: "idle" }));
    await sleep(5);
    assert.strictEqual(card._state, "ringing");
    const answerBtn = q(card, "#answer-btn");
    assert.ok(answerBtn, "ringing state should render an #answer-btn");
    assert.ok(q(card, ".ringing h2").textContent.includes("Front door"), "the door's name, not its id");
    answerBtn.click();
    await sleep(30);
    assert.strictEqual(state.getUserMediaCalls, 1);
    const answer = fake.ofType("portico/answer");
    assert.deepStrictEqual(answer, [{ type: "portico/answer", door_id: "vto.example", call_id: "call-1", client_kind: "browser" }]);
    assert.deepStrictEqual(fake.ofType("portico/webrtc_offer"),
      [{ type: "portico/webrtc_offer", call_id: "call-1", door_id: "vto.example", sdp: "v=0 fake-offer-sdp" }]);
    assert.deepStrictEqual(state.lastPC.remoteDescription, { type: "answer", sdp: "v=0 fake-answer-sdp" });
    assert.strictEqual(card._state, "in-call");
    console.log("PASS: Answer drives answer {door_id} -> webrtc_offer {door_id} -> setRemoteDescription and reaches in-call");

    q(card, "#hangup-btn").click();
    await sleep(20);
    assert.deepStrictEqual(fake.ofType("portico/hangup"), [{ type: "portico/hangup", call_id: "call-1", reason: "user_hangup" }]);
    assert.strictEqual(card._state, "idle");
    assert.strictEqual(state.lastPC.closed, true, "the RTCPeerConnection is closed on hangup");
    console.log("PASS: hang up sends portico/hangup, closes the peer connection, returns to idle");
    card.remove();
  }

  // 3. not_supported: the server's reason, not the generic text; open_relay carries door_id
  {
    const reason = "gate_sip_observer: Answer: not_supported (observer mode has no audio; answer in the existing app)";
    const { card, fake } = await mount({
      doors: [{ ...DOOR_NO_AUDIO, capabilities: { DirectCapable: false, OutboundAudio: 1 } }],
      answerError: () => ({ code: "not_supported", message: reason }),
    });
    fake.fire(ev(DOOR_NO_AUDIO, { call_id: "gso-1", state: "ringing" }));
    await sleep(5);
    q(card, "#answer-btn").click();
    await sleep(30);
    assert.strictEqual(card._error, reason);
    assert.ok(fake.ofType("portico/webrtc_offer").length === 0, "no webrtc_offer after not_supported");
    assert.strictEqual(card._state, "ringing", "the card stays on the ring");
    console.log("PASS: not_supported on answer is shown as the server's reason, no offer sent");

    card._hass.connection.sendMessagePromise = async (msg) => {
      fake.sent.push(msg);
      if (msg.type === "portico/open_relay") throw { code: "not_supported", message: reason };
      throw new Error("unexpected");
    };
    await card._openRelay("gate.example");
    assert.strictEqual(card._error, reason);
    assert.deepStrictEqual(fake.ofType("portico/open_relay"), [{ type: "portico/open_relay", door_id: "gate.example" }]);
    card._hass.connection.sendMessagePromise = async (msg) => { throw { code: "open_relay_failed", message: "boom" }; };
    await card._openRelay("gate.example");
    assert.strictEqual(card._error, "Open failed — see the browser console.");
    console.log("PASS: open_relay sends door_id; not_supported shows the reason, other errors the generic text");
    card.remove();
  }

  // 4. the answer errors of contract 3
  const CAP = { DirectCapable: false, OutboundAudio: 1 };
  {
    // already_answered: clear message, the ring is released, no retry, a late `answered` is ignored
    const door = { ...DOOR_NO_AUDIO, capabilities: CAP };
    const { card, fake } = await mount({ doors: [door], answerError: () => ({ code: "already_answered", message: "x" }) });
    fake.fire(ev(door, { call_id: "aa-1", state: "ringing" }));
    await sleep(5);
    q(card, "#answer-btn").click();
    await sleep(30);
    assert.strictEqual(card._error, "Someone else already answered this call");
    assert.ok(card.shadowRoot.textContent.includes("Someone else already answered this call"));
    assert.strictEqual(card._state, "idle", "ring released");
    assert.strictEqual(q(card, "#answer-btn"), null, "no retry offered");
    assert.ok(fake.ofType("portico/webrtc_offer").length === 0);
    fake.fire(ev(door, { call_id: "aa-1", state: "answered", prev_state: "ringing" }));
    assert.strictEqual(card._state, "idle", "a late answered event is ignored");
    console.log("PASS: already_answered shows the clear message, releases the ring, offers no retry");
    card.remove();
  }
  {
    // refused (was claims_disabled): its own message; the ring and Answer stay
    const door = { ...DOOR_NO_AUDIO, capabilities: CAP };
    const { card, fake } = await mount({ doors: [door], answerError: () => ({ code: "refused", message: "x" }) });
    fake.fire(ev(door, { call_id: "cd-1", state: "ringing" }));
    await sleep(5);
    q(card, "#answer-btn").click();
    await sleep(30);
    assert.ok(card._error.includes("Answering from Portico is switched off at the door"), card._error);
    assert.ok(!card.shadowRoot.textContent.includes("Someone else already answered"));
    assert.strictEqual(card._state, "ringing", "the ring is kept");
    assert.ok(q(card, "#answer-btn"), "Answer is still offered");
    console.log("PASS: refused says Portico answering is off, keeps the ring and Answer");
    card.remove();
  }
  {
    // not_available: the call is gone: "call ended", ring released, no retry
    const door = { ...DOOR_NO_AUDIO, capabilities: CAP };
    const { card, fake } = await mount({ doors: [door], answerError: () => ({ code: "not_available", message: "x" }) });
    fake.fire(ev(door, { call_id: "na-1", state: "ringing" }));
    await sleep(5);
    q(card, "#answer-btn").click();
    await sleep(30);
    assert.strictEqual(card._error, "That call has ended");
    assert.ok(card.shadowRoot.textContent.includes("That call has ended"));
    assert.strictEqual(card._state, "idle");
    assert.strictEqual(q(card, "#answer-btn"), null);
    assert.strictEqual(state.lastPC.closed, true, "the half-built PeerConnection is closed");
    console.log("PASS: not_available says the call ended, releases the ring, offers no retry");
    card.remove();
  }
  {
    // unknown_door: worded, ring kept
    const door = { ...DOOR_NO_AUDIO, capabilities: CAP };
    const { card, fake } = await mount({ doors: [door], answerError: () => ({ code: "unknown_door", message: "x" }) });
    fake.fire(ev(door, { call_id: "ud-1", state: "ringing" }));
    await sleep(5);
    q(card, "#answer-btn").click();
    await sleep(30);
    assert.ok(card._error.includes("does not know this door"), card._error);
    assert.ok(!card._error.includes("Answering failed"));
    console.log("PASS: unknown_door is worded");
    card.remove();
  }
  {
    // device_busy: a clear message, the ring and Answer stay, the retry works
    const door = { ...DOOR_NO_AUDIO, capabilities: CAP };
    let busy = true;
    const { card, fake } = await mount({ doors: [door], answerError: () => (busy ? { code: "device_busy", message: "SIP 486 busy here <b>" } : null) });
    const real = card._hass.connection.sendMessagePromise;
    card._hass.connection.sendMessagePromise = async (msg) => {
      if (msg.type === "portico/answer" && !busy) { fake.sent.push(msg); return { mode: "relay" }; }
      return real(msg);
    };
    fake.fire(ev(door, { call_id: "db-1", state: "ringing" }));
    await sleep(5);
    q(card, "#answer-btn").click();
    await sleep(30);
    assert.strictEqual(card._error, "The door station is busy with another call. Try again in a moment.");
    assert.ok(!card.shadowRoot.textContent.includes("486"), "server text is not shown");
    assert.strictEqual(card._state, "ringing");
    assert.strictEqual(card._activeCallId, "db-1");
    busy = false;
    q(card, "#answer-btn").click();
    await sleep(30);
    assert.strictEqual(fake.ofType("portico/answer").length, 2, "retry sends a second answer");
    assert.strictEqual(card._error, null);
    console.log("PASS: device_busy shows the clear message, keeps the ring and the Answer button, retry works");
    card.remove();
  }

  // 5. contract-3 call states: snapshots on subscribe, `ended` (no idle echo), `unavailable` (the door left)
  {
    const door = { ...DOOR_AUDIO, door_id: "10.3.0.30" };
    const { card, fake } = await mount({ doors: [door] });
    fake.fire(ev(door, { snapshot: true, call_id: null, state: "idle" }));
    assert.strictEqual(card._state, "idle");
    assert.strictEqual(card._activeDoorId, null);
    console.log("PASS: an idle snapshot on subscribe leaves the card idle");

    fake.fire(ev(door, { snapshot: true, call_id: "live-1", state: "ringing" }));
    assert.strictEqual(card._state, "ringing");
    assert.strictEqual(card._activeCallId, "live-1");
    assert.ok(q(card, "#answer-btn"), "a ringing snapshot shows the live ring");
    console.log("PASS: a ringing snapshot shows the ring that is still live");

    fake.fire(ev(door, { call_id: "old-call", state: "ended", prev_state: "ringing" }));
    assert.strictEqual(card._state, "ringing", "the end of ANOTHER call changes nothing");
    fake.fire(ev(door, { call_id: "live-1", state: "ended", prev_state: "ringing" }));
    assert.strictEqual(card._state, "idle", "`ended` for the live call ends the ring: there is no idle echo");
    assert.strictEqual(card._activeDoorId, null);
    assert.strictEqual(card._activeCallId, null);
    console.log("PASS: ended (alone) returns the card to idle; another call's ended is ignored");

    fake.fire(ev(door, { call_id: "live-2", state: "ringing", prev_state: "idle" }));
    fake.fire(ev(door, { call_id: "live-2", state: "answered", prev_state: "ringing" }));
    assert.strictEqual(card._state, "in-call");
    fake.fire(ev(door, { call_id: "live-2", state: "ended", prev_state: "answered" }));
    assert.strictEqual(card._state, "idle");
    console.log("PASS: an answered call ends on ended");

    fake.fire(ev(door, { call_id: "live-3", state: "ringing", prev_state: "idle" }));
    fake.fire(ev(door, { snapshot: true, call_id: null, state: "unavailable" }));
    assert.strictEqual(card._state, "idle", "a door that left ends its ring");
    assert.strictEqual(card._doors[0].available, false);
    fake.fire(ev(door, { snapshot: true, call_id: null, state: "idle" }));
    assert.strictEqual(card._doors[0].available, true, "and is available again when it returns");
    console.log("PASS: an unavailable snapshot ends the door's ring; its return marks it available");
    fake.fire({ type: "call_state_changed", door_id: "never-heard-of-it", call_id: "z", state: "ringing" });
    assert.strictEqual(card._state, "idle", "an unknown door is ignored");
    fake.fire({ type: "house_call", door_id: door.door_id });
    assert.strictEqual(card._state, "idle", "an unknown event type is ignored (M7 reserves house_call)");
    card.remove();
  }

  // 6. the classic ring view shows the door's own camera entity, never a guessed id
  {
    dom.window.HTMLMediaElement.prototype.play = () => Promise.resolve();   // jsdom does not implement play()
    const front = { ...DOOR_AUDIO, door_id: "front-1", camera_entity_id: "camera.portico_dahua_sip_camera" };
    const side = { ...DOOR_AUDIO, door_id: "side-1", camera_entity_id: null };
    const { card, fake } = await mount({ doors: [front, side] });
    card._hass.states = {
      "camera.portico_dahua_sip_camera": { attributes: { access_token: "tok" } },
      "camera.portico_side_1": { attributes: { access_token: "guess" } },
    };
    fake.fire(ev(front, { call_id: "cam-1", state: "ringing" }));
    await sleep(5);
    const video = q(card, "img.video");
    assert.ok(video, "a door with a camera entity shows video");
    assert.strictEqual(video.getAttribute("src"), "/api/camera_proxy_stream/camera.portico_dahua_sip_camera?token=tok");
    fake.fire(ev(front, { call_id: "cam-1", state: "ended" }));
    fake.fire(ev(side, { call_id: "cam-2", state: "ringing" }));
    await sleep(5);
    assert.strictEqual(q(card, "img.video"), null, "no camera entity: no video, no guessed id");
    console.log("PASS: the ring view uses the door's camera_entity_id and shows nothing without one");
    card.remove();
  }

  // 7. R7-19 in the plain (no endpoint) mode: a second ring while in a call is refused, the live call keeps its PeerConnection
  {
    const { card, fake } = await mount({ doors: [DOOR_AUDIO, DOOR_NO_AUDIO] });
    fake.fire(ev(DOOR_AUDIO, { call_id: "f1", state: "ringing" }));
    await sleep(5);
    q(card, "#answer-btn").click();
    await sleep(30);
    const livePC = state.lastPC;
    assert.ok(livePC && !livePC.closed && card._pc === livePC && card._state === "in-call", "the first call is live");
    fake.fire(ev(DOOR_NO_AUDIO, { call_id: "g1", state: "ringing" }));
    await sleep(5);
    assert.strictEqual(card._pc, livePC, "the PeerConnection is still the first call's");
    assert.ok(!livePC.closed, "and is not closed");
    assert.strictEqual(card._activeDoorId, DOOR_AUDIO.door_id);
    assert.strictEqual(card._activeCallId, "f1");
    assert.strictEqual(card._state, "in-call");
    assert.ok(q(card, ".notice").textContent.includes("Vehicle gate is ringing, but you are already on a call."));
    fake.fire(ev(DOOR_NO_AUDIO, { call_id: "g1", state: "ended" }));
    assert.ok(!livePC.closed && card._pc === livePC, "the refused door ending changes nothing");
    fake.fire(ev(DOOR_AUDIO, { call_id: "f1", state: "ended" }));
    assert.ok(livePC.closed && card._pc === null, "the live call's ended tears it down (peer connection and microphone)");
    assert.strictEqual(card._localStream, null);
    fake.fire(ev(DOOR_NO_AUDIO, { call_id: "g2", state: "ringing" }));
    assert.strictEqual(card._activeCallId, "g2", "the next ring is accepted once free");
    console.log("PASS: R7-19, plain mode: a second ring in a call is refused with a line; the live call is kept and torn down by its own ended");
    card.remove();
  }

  // 8. an administrator's card without `endpoint` is refused by the server: one clear line
  {
    const { card } = await mount({ doors: [], contentError: { code: "unauthorized", message: "only an enrolled Portico phone, or an admin naming one, may do this" } });
    const text = q(card, ".error").textContent;
    assert.ok(text.includes("Name a screen with endpoint:"), text);
    console.log("PASS: unauthorized without `endpoint` says to name a screen with endpoint:");
    card.remove();
  }

  // 9. the caller's voice: the remote stream goes to ONE persistent audio element, never the local mic
  {
    const hearIn = (card) => q(card, "#hear-btn");
    // jsdom has no media: count what the card asks of the element
    const stub = (card, playImpl) => {
      const a = card._remoteAudio;
      a.plays = 0;
      a.pauses = 0;
      a.play = () => { a.plays++; return playImpl(); };
      a.pause = () => { a.pauses++; };
      return a;
    };
    const answerCall = async (card, fake) => {
      fake.fire(ev(DOOR_AUDIO, { call_id: "a1", state: "ringing", prev_state: "idle" }));
      await sleep(5);
      q(card, "#answer-btn").click();
      await sleep(30);
    };
    const remote = { remote: true };

    // 9a. attaches once, survives re-renders, local stream never attached, teardown clears it
    {
      const { card, fake } = await mount({ doors: [DOOR_AUDIO] });
      const audio = stub(card, () => Promise.resolve());
      await answerCall(card, fake);
      assert.strictEqual(audio.srcObject == null, true, "nothing to play before a track arrives");
      state.lastPC.emitTrack(remote);
      await sleep(5);
      assert.strictEqual(audio.srcObject, remote, "the remote stream is attached");
      assert.notStrictEqual(audio.srcObject, state.localStream);
      assert.ok(!audio.srcObject.isLocal, "the microphone stream is never played back");
      assert.strictEqual(audio.autoplay, true);
      assert.strictEqual(audio.plays, 1);
      for (let i = 0; i < 3; i++) card._render();
      state.lastPC.emitTrack(remote); // a second ontrack for the same stream
      await sleep(5);
      assert.strictEqual(card._remoteAudio, audio, "the element is the same one after re-renders");
      assert.strictEqual(audio.srcObject, remote);
      assert.strictEqual(audio.plays, 1, "re-rendering does not restart playback");
      assert.strictEqual(hearIn(card), null, "no tap-to-hear when play() worked");
      q(card, "#hangup-btn").click();
      await sleep(20);
      assert.strictEqual(audio.srcObject, null, "hang up detaches the stream");
      assert.ok(audio.pauses >= 1, "and pauses the element");
      console.log("PASS: the remote stream is attached once to a persistent audio element, survives re-renders, never the mic, cleared on hang up");
      card.remove();
    }

    // 9b. the track arrives while setRemoteDescription runs (the real order), before in-call
    {
      state.trackOnRemoteDescription = remote;
      const { card, fake } = await mount({ doors: [DOOR_AUDIO] });
      const audio = stub(card, () => Promise.resolve());
      await answerCall(card, fake);
      state.trackOnRemoteDescription = null;
      assert.strictEqual(card._state, "in-call");
      assert.strictEqual(audio.srcObject, remote);
      assert.strictEqual(audio.plays, 1);
      fake.fire(ev(DOOR_AUDIO, { call_id: "a1", state: "ended" }));
      await sleep(5);
      assert.strictEqual(audio.srcObject, null, "the call ending detaches the stream");
      console.log("PASS: a track delivered during setRemoteDescription plays; the call ending clears it");
      card.remove();
    }

    // 9c. play() rejected: a visible Tap to hear, one tap plays and clears it; no alert
    {
      const { card, fake } = await mount({ doors: [DOOR_AUDIO] });
      let allow = false;
      const audio = stub(card, () => (allow ? Promise.resolve() : Promise.reject(new Error("NotAllowedError"))));
      let alerts = 0;
      dom.window.alert = () => { alerts++; };
      await answerCall(card, fake);
      state.lastPC.emitTrack(remote);
      await sleep(10);
      const btn = hearIn(card);
      assert.ok(btn, "a rejected play() shows the Tap to hear control");
      assert.ok(btn.textContent.includes("Tap to hear"));
      card._render();
      assert.ok(hearIn(card), "it survives a re-render");
      allow = true;
      hearIn(card).click();
      await sleep(10);
      assert.strictEqual(audio.plays, 2, "the tap calls play() again");
      assert.strictEqual(hearIn(card), null, "success clears the control");
      assert.strictEqual(audio.srcObject, remote);
      assert.strictEqual(alerts, 0, "never an alert");
      console.log("PASS: a rejected play() shows Tap to hear; the tap plays and clears it; no alert");
      card.remove();
    }

    // 9d. rejected, then the call is hung up: the control goes with it
    {
      const { card, fake } = await mount({ doors: [DOOR_AUDIO] });
      const audio = stub(card, () => Promise.reject(new Error("NotAllowedError")));
      await answerCall(card, fake);
      state.lastPC.emitTrack(remote);
      await sleep(10);
      assert.ok(hearIn(card));
      q(card, "#hangup-btn").click();
      await sleep(20);
      assert.strictEqual(hearIn(card), null);
      assert.strictEqual(audio.srcObject, null);
      assert.strictEqual(card._audioBlocked, false);
      console.log("PASS: hang up clears a pending Tap to hear");
      card.remove();
    }

    // 9e. takeover overlay: the control is in the overlay's in-call view and works from there
    {
      const { card, fake } = await mount({ doors: [DOOR_AUDIO] }, { takeover: true });
      let allow = false;
      const audio = stub(card, () => (allow ? Promise.resolve() : Promise.reject(new Error("NotAllowedError"))));
      const root = () => doc.querySelector("[data-portico-overlay]").shadowRoot;
      fake.fire(ev(DOOR_AUDIO, { call_id: "a1", state: "ringing", prev_state: "idle" }));
      await sleep(5);
      root().querySelector("#answer-btn").click();
      await sleep(30);
      state.lastPC.emitTrack(remote);
      await sleep(10);
      assert.ok(root().querySelector("#hear-btn"), "the overlay offers Tap to hear");
      allow = true;
      root().querySelector("#hear-btn").click();
      await sleep(10);
      assert.strictEqual(audio.plays, 2);
      assert.strictEqual(root().querySelector("#hear-btn"), null);
      root().querySelector("#hangup-btn").click();
      await sleep(20);
      assert.strictEqual(audio.srcObject, null);
      console.log("PASS: in the takeover overlay Tap to hear plays and clears; hang up detaches");
      card.remove();
    }
  }

  console.log("ALL_CARD_TESTS_PASSED");
}

main().catch((err) => {
  console.error("TEST FAILED:", err);
  process.exit(1);
});
