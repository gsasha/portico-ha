// Loads the REAL portico-card.js into a jsdom window, with the browser APIs it calls mocked.
// Shared by every test_card*.mjs.
import { JSDOM } from "jsdom";
import fs from "fs";

/** A fake IntersectionObserver (jsdom has none): `state.io` lists the observers the card made. A test decides what is on
 *  screen with `state.io.show(pred, bool)`: every observed node `pred` accepts is reported with `isIntersecting: bool`. */
function makeIO(window) {
  const list = [];
  class FakeIO {
    constructor(callback, options) { this.callback = callback; this.options = options; this.targets = new Set(); this.disconnects = 0; list.push(this); }
    observe(node) { this.targets.add(node); }
    unobserve(node) { this.targets.delete(node); }
    disconnect() { this.targets.clear(); this.disconnects++; }
  }
  list.show = (pred, isIntersecting) => {
    for (const io of list) {
      const entries = [...io.targets].filter((n) => pred(n)).map((target) => ({ target, isIntersecting }));
      if (entries.length) io.callback(entries, io);
    }
  };
  list.byKey = (key) => (n) => n.dataset && n.dataset.liveKey === key;
  list.all = () => () => true;
  window.IntersectionObserver = FakeIO;
  return list;
}

export function loadCard({ withIO = false } = {}) {
  const dom = new JSDOM("<!doctype html><html><body></body></html>", { url: "http://localhost/", pretendToBeVisual: true });
  // jsdom's media elements do not implement play/pause (they log "not implemented"); a <video> here records them.
  dom.window.HTMLVideoElement.prototype.play = function play() { this._plays = (this._plays || 0) + 1; this._paused = false; return Promise.resolve(); };
  dom.window.HTMLVideoElement.prototype.pause = function pause() { this._paused = true; };
  Object.defineProperty(dom.window.HTMLVideoElement.prototype, "paused", { configurable: true, get() { return this._paused !== false; } });
  global.window = dom.window;
  global.document = dom.window.document;
  global.HTMLElement = dom.window.HTMLElement;
  global.customElements = dom.window.customElements;
  // Node 21+ makes `navigator` a read-only global getter; define it instead of assigning.
  Object.defineProperty(global, "navigator", { value: dom.window.navigator, configurable: true, writable: true });

  const state = { getUserMediaCalls: 0, lastPC: null, localStream: null, trackOnRemoteDescription: null, pcs: [], io: null, stats: null };
  if (withIO) state.io = makeIO(dom.window);
  const fakeTrack = { stop: () => {} };
  global.navigator.mediaDevices = {
    getUserMedia: async () => {
      state.getUserMediaCalls++;
      state.localStream = { getTracks: () => [fakeTrack], isLocal: true };
      return state.localStream;
    },
  };
  class FakeRTCPeerConnection {
    constructor() {
      this.iceGatheringState = "complete";
      this.connectionState = "new";
      this.localDescription = { sdp: "v=0 fake-offer-sdp" };
      this.transceivers = [];
      this.tracks = [];
      this.addedCandidates = [];
      this._prev = state.lastPC;
      state.lastPC = this;
      state.pcs.push(this);
    }
    addTrack(track) { this.tracks.push(track); }
    // A live view's connection is told apart by addTransceiver: it must not displace `state.lastPC`, the call's connection.
    addTransceiver(kind, init) {
      this.transceivers.push({ kind, init });
      this.isLive = true;
      if (state.lastPC === this) state.lastPC = this._prev;
    }
    async createOffer() {
      // the plain string for the call's connection; an SDP with one media line per transceiver for a live one
      if (!this.transceivers.length) return { type: "offer", sdp: "v=0 fake-offer-sdp" };
      const lines = ["v=0"];
      for (const t of this.transceivers) lines.push(`m=${t.kind} 9 UDP/TLS/RTP/SAVPF 96`, `a=${(t.init && t.init.direction) || "sendrecv"}`);
      return { type: "offer", sdp: lines.join("\r\n") + "\r\n" };
    }
    async addIceCandidate(c) { this.addedCandidates.push(c); }
    async getStats() { return new Map(Object.entries(state.stats ? state.stats(this) : {})); }
    emitCandidate(c) { if (this.onicecandidate) this.onicecandidate({ candidate: c }); }
    emitState(s) { this.connectionState = s; if (this.onconnectionstatechange) this.onconnectionstatechange(); }
    async setLocalDescription(d) { this.localDescription = d; }
    async setRemoteDescription(d) {
      this.remoteDescription = d;
      // a real connection fires ontrack from here; a test names the stream it should deliver
      if (state.trackOnRemoteDescription) this.emitTrack(state.trackOnRemoteDescription);
    }
    emitTrack(stream) { if (this.ontrack) this.ontrack({ streams: [stream] }); }
    addEventListener() {}
    removeEventListener() {}
    close() { this.closed = true; }
  }
  global.RTCPeerConnection = FakeRTCPeerConnection;

  const src = fs.readFileSync(new URL("../portico-card.js", import.meta.url), "utf8");
  const run = new dom.window.Function(
    "window", "document", "customElements", "HTMLElement", "navigator", "RTCPeerConnection", "console", "IntersectionObserver",
    src + "\nreturn customElements.get('portico-card');",
  );
  const PorticoCard = run(dom.window, dom.window.document, dom.window.customElements, dom.window.HTMLElement,
    global.navigator, FakeRTCPeerConnection, console, withIO ? dom.window.IntersectionObserver : undefined);
  return { dom, PorticoCard, state };
}

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** A fake hass whose websocket answers the contract-3 commands the card sends. */
export function makeHass({ doors, content, contentError, snapshotError, runError, answerError, runResult, houseError,
                           version = { contract: 3, ui_version: 1, providers: [] }, versionError, snapshot, live, probe } = {}) {
  const sent = [];
  const probes = [];
  const callCbs = [];            // every card's calls subscription (`subs` keeps only the last)
  const probeUnsubs = { count: 0 };
  const subs = {}; // subscription type -> event callback
  // Live video (portico/content/live): `live` is absent for a server without the command (the card must fall back to stills
  // silently; the command is then not recorded in `sent`, so the older tests' exact lists are untouched) or an object:
  //   error (msg) -> an error to reject the command with | sessionId (n) -> the id of the n-th session (default `sess-<n>`)
  //   socket: false = the connection exposes no raw socket | sessionEvent: true = the id also comes as a {type:"session"} event
  // The result passes the fake socket first and only then resolves the subscription: the real library's order.
  const liveSessions = [];
  const liveSent = [];
  const socketListeners = new Set();
  let nextCommandId = 100;
  const connection = {
    socket: !live || live.socket === false ? undefined : {
      addEventListener: (type, fn) => { if (type === "message") socketListeners.add(fn); },
      removeEventListener: (type, fn) => { if (type === "message") socketListeners.delete(fn); },
    },
    sendMessagePromise: async (msg) => {
      sent.push(msg);
      switch (msg.type) {
        case "portico/content/live_ice": return {};
        case "portico/content/live_close": {
          const s = liveSessions.find((x) => x.id === msg.session_id);
          const open = !!s && !s.closedByClient;
          if (s) s.closedByClient = true;
          return { closed: open };
        }
        case "portico/version":
          if (versionError) throw versionError;
          return version;
        case "portico/doors": return { doors: doors || [] };
        case "portico/content/get":
          if (contentError) throw contentError;
          return content || CONTENT;
        case "portico/content/snapshot":
          if (snapshot) return snapshot(msg);
          if (snapshotError) throw snapshotError(msg);
          return { content_type: "image/jpeg", content: "QUJD" }; // "ABC"
        case "portico/content/run":
          if (runError) throw runError(msg);
          return { ran: true, ...(runResult ? runResult(msg) : {}) };
        case "portico/house/answer":
        case "portico/house/decline":
        case "portico/house/hangup":
          if (houseError) { const e = houseError(msg); if (e) throw e; }
          return {};
        case "portico/open_relay": return { ok: true };
        case "portico/answer":
          if (answerError) throw answerError(msg);
          return { mode: "relay" };
        case "portico/webrtc_offer": return { sdp: "v=0 fake-answer-sdp" };
        case "portico/hangup": return { ok: true };
        default: throw new Error("unexpected message: " + JSON.stringify(msg));
      }
    },
    subscribeMessage: async (cb, msg, options) => {
      if (msg.type === "portico/content/live") {
        msg.id = ++nextCommandId;          // the real library writes the command id into the message as it sends it
        liveSent.push(msg);
        if (live) sent.push(msg);
        if (!live) throw { code: "unknown_command", message: "Unknown command." };
        const failure = live.error && live.error(msg);
        if (failure) throw failure;
        const session = { id: live.sessionId ? live.sessionId(liveSessions.length) : `sess-${liveSessions.length}`, msg, cb, options,
                          unsubscribed: 0, closedByClient: false };
        liveSessions.push(session);
        if (live.resultDelayMs) await new Promise((r) => setTimeout(r, live.resultDelayMs));
        await Promise.resolve();
        const frame = JSON.stringify({ id: msg.id, type: "result", success: true, result: { session_id: session.id, stream: "main" } });
        for (const fn of [...socketListeners]) fn({ data: frame });
        if (live.sessionEvent) cb({ type: "session", session_id: session.id });
        return () => { session.unsubscribed++; return Promise.resolve(); };
      }
      if (msg.type === "portico/calls/subscribe" && cb.porticoProbe) {
        // a calls subscription marked `porticoProbe` is the card's failsafe probe (it asks for the snapshot and lets go): the first stays the
        // one `fire` reaches. `probe(msg)` -> the snapshot events to answer with (absent = the server never answers), or throws.
        probes.push(msg);
        if (probe) {
          const events = probe(msg);
          await Promise.resolve();
          setTimeout(() => events.forEach((e) => cb({ snapshot: true, ...e })), 0);
        }
        return () => { probeUnsubs.count++; };
      }
      sent.push(msg);
      subs[msg.type] = cb;
      if (msg.type === "portico/calls/subscribe") callCbs.push(cb);
      return () => { delete subs[msg.type]; };
    },
  };
  return {
    hass: { connection, states: {} },
    sent,
    probes,
    probeUnsubs,
    liveSent,
    liveSessions,
    socketListeners,
    // an event of the n-th live session, as the server sends it
    liveEvent: (n, event) => liveSessions[n].cb(event),
    ofType: (t) => sent.filter((m) => m.type === t),
    // a call event comes on portico/calls/subscribe, a content_changed on portico/content/subscribe (WS-04)
    fire: (event) => {
      const cb = subs[event.type === "content_changed" ? "portico/content/subscribe" : "portico/calls/subscribe"];
      if (!cb) throw new Error("nobody subscribed for " + event.type);
      cb(event);
    },
    // a call event to EVERY card that subscribed (a kiosk page has two: the panel's and the listener's), as the server sends it
    fireAll: (event) => callCbs.forEach((cb) => cb(event)),
    hasSubscriber: () => Object.keys(subs).length > 0,
    subscribed: (type) => (type ? !!subs[type] : Object.keys(subs).length === 2),
  };
}

export const CONTENT = {
  endpoint: "ep-alice",
  kind: "phone",
  home_name: "Home",
  rev: "r1",
  pages: [{
    id: "home",
    label: "Home",
    cameras: [
      { id: "gate", label: "Gate", entity_id: "camera.gate", name: "Gate camera", available: true },
      { id: "garage", label: "Garage", entity_id: "camera.garage", name: "Garage", available: false },
    ],
    actions: [
      { id: "open-front", label: "Front door", kind: "lock_open", entity_id: "lock.front", confirm: true, available: true, name: "Front door lock" },
      { id: "open-gate", label: "Gate relay", kind: "portico_open", door_id: "gate.example", confirm: true, available: true, name: "Gate" },
      { id: "evening", label: "Evening", kind: "scene_activate", entity_id: "scene.evening", confirm: false, available: true, name: "Evening" },
    ],
  }],
  callers: {},
  roster: [],
};

const door = (extra) => ({ door_ref: "d-x", camera_entity_id: null, available: true, away_media: false, ...extra });
export const DOOR_NO_AUDIO = door({ door_id: "gate.example", endpoint_id: "gate_sip:gate.example", name: "Vehicle gate", driver: "gate_sip_observer", capabilities: { InboundAudio: 3, OutboundAudio: 3, DirectCapable: false } });
export const DOOR_AUDIO = door({ door_id: "vto.example", endpoint_id: "dahua:vto.example", name: "Front door", driver: "dahua_sip", capabilities: { InboundAudio: 0, OutboundAudio: 0, DirectCapable: true } });
