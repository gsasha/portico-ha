// Portico Elements: the shared renderers (DEV-760 E3; portico_elements_design.md section 6).
//
// A pure ES module. Each renderer draws one stored element (camera, button, status, chip) with DOM builders only and
// returns a handle { el, update(states), destroy() }:
//   el       the element's node (detached; the host appends it)
//   update   redraw from an HA states map { "light.x": { state, attributes } } (hass.states); safe to call often
//   destroy  drop the references; the module holds no timer except the full-size camera window's, and no listener that reaches HA
// Nothing here runs an action, places a call or writes to HA (D8): a button is drawn disabled, inert and labelled as a
// preview. No HTML-string setter is used anywhere (text goes in with textContent, nodes with createElement): the
// grep test in tests/test_elements_render.mjs fails the build if one appears. Element names, labels, states and
// attribute values are user or device text and reach the page only as text.
//
// A new kind needs only a row in RENDERERS (the extension table) and, on the server, a row in elements.KIND_SPECS.
// An unknown kind draws a neutral tile and never throws. Parity with the card and the panel (the palette, the default
// icon per button action, the ask-first set) is pinned by tests/test_elements_render.mjs so a preview cannot drift
// from what the screens draw today.

/* The button palette (contract/palette_v1.json): { name: { light: [fill, text], dark: [fill, text] } }. */
export const PALETTE = {
  blue: { light: ["#1565C0", "#FFFFFF"], dark: ["#90CAF9", "#0D2B4E"] },
  green: { light: ["#2E7D32", "#FFFFFF"], dark: ["#A5D6A7", "#0F3A14"] },
  amber: { light: ["#8A5A00", "#FFFFFF"], dark: ["#FFD54F", "#3D2A00"] },
  red: { light: ["#C62828", "#FFFFFF"], dark: ["#EF9A9A", "#5A0E0E"] },
  purple: { light: ["#6A1B9A", "#FFFFFF"], dark: ["#CE93D8", "#3B0F52"] },
  teal: { light: ["#00796B", "#FFFFFF"], dark: ["#80CBC4", "#00332D"] },
  pink: { light: ["#AD1457", "#FFFFFF"], dark: ["#F48FB1", "#4F0A27"] },
  grey: { light: ["#546E7A", "#FFFFFF"], dark: ["#B0BEC5", "#1F2A30"] },
};

/* The icon of a button that names none (the panel's table; the card's is the first six). */
export const KIND_ICON = {
  portico_open: "mdi:door-open",
  lock_open: "mdi:lock-open-variant",
  button_press: "mdi:gesture-tap-button",
  script_run: "mdi:script-text-play",
  scene_activate: "mdi:palette",
  call: "mdi:phone-in-talk",
  lock_lock: "mdi:lock",
  lock_unlock: "mdi:lock-open-variant",
  cover_open: "mdi:garage-open",
  cover_close: "mdi:garage",
  talk: "mdi:account-voice",
};

/* Actions the screens ask a second tap for by default (the panel's KINDS[...].confirm). The preview shows the mark. */
export const ASKS_FIRST = new Set(["portico_open", "lock_open", "lock_unlock", "cover_open"]);

export const PREVIEW_NOTE = "Preview: does not run";
export const MAX_SHORT = 12;

/* What a state reads as when the element's own `states` map has no rule for it. A colour is a palette name or
 * "neutral"; the element's own rule always wins. Chosen to say little: only well-known HA states are worded. */
export const DEFAULT_STATES = {
  on: { label: "On", color: "green" },
  off: { label: "Off", color: "grey" },
  open: { label: "Open", color: "amber" },
  opening: { label: "Opening", color: "amber" },
  closed: { label: "Closed", color: "green" },
  closing: { label: "Closing", color: "amber" },
  locked: { label: "Locked", color: "green" },
  unlocked: { label: "Unlocked", color: "red" },
  home: { label: "Home", color: "green" },
  not_home: { label: "Away", color: "grey" },
  unavailable: { label: "Unavailable", color: "neutral" },
  unknown: { label: "Unknown", color: "neutral" },
};

const NOT_FOUND_TEXT = "Entity not found";
const NO_VALUE_TEXT = "No value";
const NEUTRAL = { light: ["#ECEFF1", "#37474F"], dark: ["#2A2F33", "#CFD8DC"] };
const ERROR = { light: ["#FFEBEE", "#B71C1C"], dark: ["#4A1515", "#FFCDD2"] };

const SLUG_RE = /^[a-z0-9]+(-[a-z0-9]+)*$/;

function node(doc, tag, cls, text) {
  const n = doc.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = text;
  return n;
}

const DEFAULT_STATUS_ICON = "mdi:information-outline";

/* The mdi icon name of an element or a button action: the element's own (any short slug), else the action default. */
export function iconName(element) {
  const given = typeof element.icon === "string" ? element.icon.replace(/^mdi:/, "") : "";
  if (SLUG_RE.test(given)) return `mdi:${given}`;
  const action = element.options && element.options.action;
  if (element.kind === "camera") return "mdi:cctv";
  if (element.kind === "talk") return "mdi:phone";
  if (element.kind === "status" || element.kind === "chip") return DEFAULT_STATUS_ICON;
  return KIND_ICON[action] || "mdi:gesture-tap";
}

function haIcon(doc, name) {
  const icon = doc.createElement("ha-icon");
  icon.setAttribute("icon", name);
  return icon;
}

/* [fill, text] for a colour name in the theme: a palette name, "neutral", or "error". */
export function colorPair(name, dark) {
  const theme = dark ? "dark" : "light";
  if (name === "error") return ERROR[theme];
  if (Object.prototype.hasOwnProperty.call(PALETTE, name)) return PALETTE[name][theme];
  return NEUTRAL[theme];
}

function paint(n, pair) {
  n.style.background = pair[0];
  n.style.color = pair[1];
  n.style.borderColor = pair[0];
}

/* The states map entry for a state: the element's own rule, else the default word, else the raw state. */
export function stateRule(options, state) {
  const own = options && options.states && Object.prototype.hasOwnProperty.call(options.states, state) ? options.states[state] : null;
  const dflt = Object.prototype.hasOwnProperty.call(DEFAULT_STATES, state) ? DEFAULT_STATES[state] : null;
  const label = (own && own.label) || (dflt && dflt.label) || String(state);
  const color = (own && own.color) || (dflt && dflt.color) || "neutral";
  return { label, color };
}

/* What a status or chip shows for one entity: { text, color, missing }. */
export function statusView(element, states) {
  const options = element.options || {};
  const entity = states && element.entity_id && Object.prototype.hasOwnProperty.call(states, element.entity_id)
    ? states[element.entity_id] : null;
  if (!entity || typeof entity !== "object") return { text: NOT_FOUND_TEXT, color: "error", missing: true };
  if (options.attribute) {
    const attrs = entity.attributes && typeof entity.attributes === "object" ? entity.attributes : {};
    const value = Object.prototype.hasOwnProperty.call(attrs, options.attribute) ? attrs[options.attribute] : null;
    if (value === null || value === undefined || typeof value === "object") return { text: NO_VALUE_TEXT, color: "neutral", missing: false };
    return { text: String(value), color: "neutral", missing: false };
  }
  const rule = stateRule(options, String(entity.state));
  return { text: rule.label, color: rule.color, missing: false };
}

/* A picture address a camera element may load: HA's own proxy path or an http(s) URL. Nothing else (no javascript:). */
function safePicture(value) {
  if (typeof value !== "string" || !value) return "";
  if (value.startsWith("/") && !value.startsWith("//")) return value;
  return /^https?:\/\//i.test(value) ? value : "";
}

function withStamp(path, stamp) {
  return stamp ? path + (path.includes("?") ? "&" : "?") + "_=" + stamp : path;
}

/* "rtsp://host:port/path" shown as host and path (the store refuses credentials, query and fragment). */
function streamLabel(stream) {
  return String(stream).replace(/^rtsps?:\/\//i, "");
}

function handle(root, update) {
  let alive = true;
  return {
    el: root,
    update(states) { if (alive) update(states || {}); },
    destroy() { alive = false; if (root.parentNode) root.parentNode.removeChild(root); },
  };
}


/* ---- the full-size camera window (click a camera picture) ----
 * A modal dialog with the clicked stream as large as the window allows (aspect kept). A camera with two streams gets a
 * Big / Small switch. The dialog plays the REAL live stream: a WebRTC session negotiated over Home Assistant's own
 * `camera/webrtc/offer` websocket subscription (the same API HA's camera dialog uses; the panel is admin, so it may call it), but it first tries the panel command `portico/camera/webrtc_offer` (Frigate's own go2rtc answers in ~10 ms; HA's 2 to 3 s) and falls back to HA's on any error. The note under the picture says which path was used.
 * Only when there is no hass connection, no RTCPeerConnection, or the session fails does it fall back to a still picture
 * (re-read every VIEWER_REFRESH_MS from the entity's current entity_picture, the camera_proxy token rotates), and then the
 * note under the picture says so. Escape, the backdrop and Close shut it (and end the live session); focus goes back to the
 * picture that opened it. The only timers in this module are the still refresh and the connect timeout; close() clears them. */
export const VIEWER_REFRESH_MS = 3000;
export const LIVE_CONNECT_MS = 12000;
export const LIVE_FRAME_MS = 40000;   // after the connection is up: a camera may need a while for its next key frame
const LIVE_GATHER_MS = 1500;

/* The frame-rate readout of the live window: a port of the app's FpsMeter (portico-app core/.../live/FpsMeter.kt). `--` until
 * 2 s of video have been watched, then `N fps`, `0 fps` after 2 s without a new frame. The rate is read over a sliding 4 s window of
 * (time, total frames decoded) readings, and the shown integer moves only when the measured rate is a full 1.0 fps away from it, so a
 * steady stream draws a steady number. A total or clock that goes back starts a new window. Pure: no clock of its own. */
export const FPS_POLL_MS = 1000;
export const FPS_WINDOW_MS = 4000;
export const FPS_MIN_SPAN_MS = 2000;
export const FPS_STALL_MS = 2000;
export const FPS_HYSTERESIS = 1.0;
export function createFpsMeter() {
  let times = [];
  let totals = [];
  let lastFrameAt = -1;
  let shown = null;
  const reset = () => { times = []; totals = []; lastFrameAt = -1; shown = null; };
  const sample = (nowMs, totalFrames) => {
    if (times.length && (nowMs < times[times.length - 1] || totalFrames < totals[totals.length - 1])) reset();
    if (!times.length) { times.push(nowMs); totals.push(totalFrames); return; }
    if (nowMs === times[times.length - 1]) return;
    if (totalFrames > totals[totals.length - 1]) lastFrameAt = nowMs;
    times.push(nowMs); totals.push(totalFrames);
    while (times.length > 2 && times[1] <= nowMs - FPS_WINDOW_MS) { times.shift(); totals.shift(); }
    if (lastFrameAt >= 0 && nowMs - lastFrameAt >= FPS_STALL_MS) {
      shown = 0;
      times = [nowMs]; totals = [totalFrames];   // measure afresh once frames return
      return;
    }
    const span = nowMs - times[0];
    if (span < FPS_MIN_SPAN_MS) return;
    const delta = totalFrames - totals[0];
    if (delta <= 0) return;
    const raw = delta * 1000 / span;
    if (shown === null || Math.abs(raw - shown) >= FPS_HYSTERESIS) shown = Math.round(raw);
  };
  return { sample, reset, label: () => (shown === null ? "--" : `${shown} fps`) };
}

/* Ask Frigate's go2rtc (through the Portico panel command) for the answer to `sdp`. Returns the answer SDP, or "" for any
 * failure. An entity that is not a Frigate stream is remembered for the page's life so the next open skips the attempt. */
export const FRIGATE_ASK_MS = 6000;
const notFrigate = new Set();
export function askFrigate(win, conn, entityId, sdp) {
  if (notFrigate.has(entityId) || typeof conn.sendMessagePromise !== "function") return Promise.resolve("");
  return new Promise((resolve) => {
    const t = win.setTimeout(() => resolve(""), FRIGATE_ASK_MS);
    let r;
    try { r = conn.sendMessagePromise({ type: "portico/camera/webrtc_offer", entity_id: entityId, sdp }); } catch (_e) { win.clearTimeout(t); resolve(""); return; }
    Promise.resolve(r).then((res) => {
      win.clearTimeout(t);
      resolve(res && typeof res.answer === "string" && res.answer ? res.answer : "");
    }, (err) => {
      win.clearTimeout(t);
      if (err && err.code === "not_frigate_stream") notFrigate.add(entityId);
      resolve("");
    });
  });
}

/* One live WebRTC session to a camera entity over HA's `camera/webrtc/offer`. Returns { stop, framesDecoded() }. onState(state, detail) is
 * called with "connecting", "via" (detail = "Frigate" or "HA": which path answered the offer), "live" (the first video track is attached and the connection is up) or "failed" (detail = why).
 * The video element gets the MediaStream as srcObject. Never throws. */
export function startLiveStream(win, hass, entityId, video, onState) {
  let stopped = false;
  let pc = null;
  let unsub = null;
  let timer = null;
  let sessionId = "";
  let settled = false;
  const pendingRemote = [];
  let connected = false;   // the WebRTC connection is up
  let decoded = false;     // and the video element has a decoded frame: only then is it "live"
  const maybeLive = () => { if (connected && decoded) done("live"); };
  if (video) video.addEventListener("loadeddata", () => { decoded = true; maybeLive(); });
  const done = (state, detail) => { if (stopped || settled && state !== "failed") return; if (state === "live" || state === "failed") { settled = true; if (timer !== null) win.clearTimeout(timer); timer = null; } onState(state, detail); };
  const stop = () => {
    if (stopped) return;
    stopped = true;
    if (timer !== null) win.clearTimeout(timer);
    timer = null;
    if (pc) { pc.onicecandidate = null; pc.ontrack = null; pc.onconnectionstatechange = null; try { pc.close(); } catch (_e) { /* gone */ } }
    pc = null;
    if (video && video.srcObject) { try { video.pause(); } catch (_e) { /* gone */ } video.srcObject = null; }
    const u = unsub;
    unsub = null;
    if (u) { try { const r = u(); if (r && typeof r.catch === "function") r.catch(() => {}); } catch (_e) { /* connection gone */ } }
  };
  // the running total of video frames decoded (inbound-rtp), or null when the browser cannot say; never throws
  const framesDecoded = async () => {
    try {
      if (stopped || !pc || typeof pc.getStats !== "function") return null;
      const report = await pc.getStats();
      let total = null;
      report.forEach((r) => { if (r && r.type === "inbound-rtp" && (r.kind === "video" || r.mediaType === "video") && typeof r.framesDecoded === "number") total = r.framesDecoded; });
      return total;
    } catch (_e) { return null; }
  };
  const fail = (why) => { if (stopped) return; done("failed", why); stop(); };
  const conn = hass && hass.connection;
  if (!conn || typeof conn.subscribeMessage !== "function" || typeof win.RTCPeerConnection !== "function") {
    Promise.resolve().then(() => fail("no live connection to Home Assistant in this browser"));
    return { stop, framesDecoded };
  }
  (async () => {
    try {
      done("connecting");
      pc = new win.RTCPeerConnection();
      pc.addTransceiver("video", { direction: "recvonly" });
      pc.ontrack = (ev) => {
        if (stopped || !video) return;
        const stream = (ev.streams && ev.streams[0]) || (typeof win.MediaStream === "function" && ev.track ? new win.MediaStream([ev.track]) : null);
        if (!stream) return;
        video.srcObject = stream;
        const p = video.play();
        if (p && typeof p.catch === "function") p.catch(() => {});
      };
      pc.onconnectionstatechange = () => {
        if (stopped || !pc) return;
        if (pc.connectionState === "connected") {
          connected = true;
          if (!decoded && !settled) {
            if (timer !== null) win.clearTimeout(timer);
            timer = win.setTimeout(() => fail("the connection is up but this browser shows no video from the camera (a codec it cannot decode?)"), LIVE_FRAME_MS);
            onState("waiting");
          }
          maybeLive();
        }
        else if (pc.connectionState === "failed") fail("the WebRTC connection failed");
      };
      pc.onicecandidate = (ev) => {
        if (stopped || !sessionId || !ev || !ev.candidate) return;
        const c = typeof ev.candidate.toJSON === "function" ? ev.candidate.toJSON() : { candidate: ev.candidate.candidate, sdpMid: ev.candidate.sdpMid, sdpMLineIndex: ev.candidate.sdpMLineIndex };
        try { const r = conn.sendMessagePromise({ type: "camera/webrtc/candidate", entity_id: entityId, session_id: sessionId, candidate: c }); if (r && r.catch) r.catch(() => {}); } catch (_e) { /* nothing to do */ }
      };
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      // wait briefly for the host candidates so one offer carries them (HA has no STUN/TURN to add on this LAN)
      await new Promise((resolve) => {
        if (pc.iceGatheringState === "complete") { resolve(); return; }
        const t = win.setTimeout(resolve, LIVE_GATHER_MS);
        pc.addEventListener("icegatheringstatechange", () => { if (pc && pc.iceGatheringState === "complete") { win.clearTimeout(t); resolve(); } });
      });
      if (stopped) return;
      // First choice: Frigate's own go2rtc answers in ~10 ms where Home Assistant's built-in one needs 2 to 3 s (it spawns
      // ffmpeg). Any failure at all (not a Frigate stream, Frigate down, too slow) falls through to Home Assistant's offer.
      const frigateAnswer = await askFrigate(win, conn, entityId, pc.localDescription.sdp);
      if (stopped) return;
      timer = win.setTimeout(() => fail("no video arrived in time"), LIVE_CONNECT_MS);
      if (frigateAnswer) {
        try {
          await pc.setRemoteDescription({ type: "answer", sdp: frigateAnswer });
          if (stopped) return;
          onState("via", "Frigate");
          return;
        } catch (_e) { /* the answer was refused: fall back to Home Assistant's own */ }
        if (stopped) return;
      }
      onState("via", "HA");
      const sub = await conn.subscribeMessage((event) => {
        if (stopped || !event) return;
        if (event.type === "session" && typeof event.session_id === "string") sessionId = event.session_id;
        else if (event.type === "answer" && typeof event.answer === "string") {
          Promise.resolve(pc && pc.setRemoteDescription({ type: "answer", sdp: event.answer })).then(() => {
            for (const c of pendingRemote.splice(0)) pc && pc.addIceCandidate(c).catch(() => {});
          }).catch(() => fail("the camera's answer was refused"));
        } else if (event.type === "candidate" && event.candidate) {
          const c = typeof event.candidate === "string" ? { candidate: event.candidate, sdpMid: "0" } : event.candidate;
          if (pc && pc.remoteDescription) pc.addIceCandidate(c).catch(() => {}); else pendingRemote.push(c);
        } else if (event.type === "error") fail(event.message || event.code || "the camera refused the live session");
      }, { type: "camera/webrtc/offer", entity_id: entityId, offer: pc.localDescription.sdp }, { resubscribe: false });
      if (stopped) { try { sub(); } catch (_e) { /* gone */ } return; }
      unsub = sub;
    } catch (err) {
      fail((err && (err.message || err.code)) || "could not start the live session");
    }
  })();
  return { stop, framesDecoded };
}

export function openCameraViewer(doc, spec) {
  const streams = (spec.streams || []).filter((s) => s && typeof s.entity_id === "string" && s.entity_id);
  if (!streams.length) return null;
  const win = doc.defaultView;
  const opener = doc.activeElement;
  const dark = !!spec.dark;
  const getStates = typeof spec.getStates === "function" ? spec.getStates : () => ({});
  const now = typeof spec.now === "function" ? spec.now : () => Date.now();
  let index = Math.min(Math.max(spec.index | 0, 0), streams.length - 1);
  const back = node(doc, "div", "pe-viewer-backdrop");
  back.style.cssText = "position:fixed;inset:0;z-index:10000;background:rgba(0,0,0,.72);display:flex;align-items:center;justify-content:center;padding:12px;box-sizing:border-box";
  const dlg = node(doc, "div", "pe-viewer");
  dlg.setAttribute("role", "dialog");
  dlg.setAttribute("aria-modal", "true");
  dlg.setAttribute("aria-label", `${spec.name || "Camera"} full size`);
  dlg.style.cssText = `display:flex;flex-direction:column;gap:8px;max-width:100%;max-height:100%;padding:10px;box-sizing:border-box;border-radius:10px;background:var(--card-background-color, ${dark ? "#1c1c1c" : "#fff"});color:var(--primary-text-color, ${dark ? "#e1e1e1" : "#212121"})`;
  const bar = node(doc, "div", "pe-viewer-bar");
  bar.style.cssText = "display:flex;flex-wrap:wrap;gap:8px;align-items:center";
  const title = node(doc, "strong", "pe-viewer-title", spec.name || "Camera");
  title.style.cssText = "flex:1 1 auto;overflow-wrap:anywhere";
  bar.append(title);
  const switches = [];
  if (streams.length > 1) {
    streams.forEach((s, i) => {
      const b = node(doc, "button", "pe-viewer-switch", s.label || s.entity_id);
      b.type = "button";
      b.dataset.stream = String(s.label || "").toLowerCase();
      b.addEventListener("click", () => { index = i; draw(); });
      switches.push(b);
      bar.append(b);
    });
  }
  const close = node(doc, "button", "pe-viewer-close", "Close");
  close.type = "button";
  close.setAttribute("aria-label", "Close full size view");
  bar.append(close);
  const frame = node(doc, "div", "pe-viewer-frame");
  frame.style.cssText = "flex:1 1 auto;min-height:0;display:flex;align-items:center;justify-content:center";
  const video = node(doc, "video", "pe-viewer-video");
  video.muted = true;
  video.autoplay = true;
  video.playsInline = true;
  video.setAttribute("muted", "");
  video.setAttribute("playsinline", "");
  video.hidden = true;
  const VIDEO_CSS = "display:block;max-width:calc(100vw - 48px);max-height:calc(100vh - 130px);object-fit:contain;border-radius:6px;background:#000";
  // While the live stream connects the video is kept in the page (so it keeps decoding) but invisible: the still picture shows instead.
  const VIDEO_STASH = `${VIDEO_CSS};position:absolute;width:1px;height:1px;opacity:0;pointer-events:none`;
  video.style.cssText = VIDEO_CSS;
  const img = node(doc, "img", "pe-viewer-img");
  img.style.cssText = "display:block;max-width:calc(100vw - 48px);max-height:calc(100vh - 130px);object-fit:contain;border-radius:6px";
  const none = node(doc, "div", "pe-none");
  none.hidden = true;
  // `hidden` alone loses to the inline `display:block` above, so every show/hide sets the display too
  const setShown = (el, on) => { el.hidden = !on; el.style.display = on ? "block" : "none"; };
  frame.style.position = "relative";
  frame.append(video, img, none);
  const fpsLabel = node(doc, "div", "pe-viewer-fps", "");
  fpsLabel.style.cssText = "position:absolute;left:8px;bottom:8px;padding:2px 6px;border-radius:4px;background:rgba(0,0,0,.6);color:#fff;font:12px/1.3 monospace;pointer-events:none";
  fpsLabel.hidden = true;
  frame.append(fpsLabel);
  const note = node(doc, "div", "pe-sub", "");
  note.style.cssText = "font-size:12px;opacity:.75";
  dlg.append(bar, frame, note);
  back.append(dlg);
  let timer = null;
  let live = null;
  let mode = "connecting";   // "connecting" | "live" | "still"
  const getHass = typeof spec.getHass === "function" ? spec.getHass : () => null;
  const picture = () => {
    const states = getStates() || {};
    const id = streams[index].entity_id;
    const ent = Object.prototype.hasOwnProperty.call(states, id) ? states[id] : null;
    return safePicture(ent && ent.attributes && ent.attributes.entity_picture);
  };
  const load = () => {
    const src = picture();
    setShown(img, !!src);
    none.hidden = !!src;
    none.textContent = src ? "" : "No picture";
    if (src) img.src = withStamp(src, String(now()));
  };
  const stopStill = () => { if (timer !== null) win.clearInterval(timer); timer = null; };
  let fpsTimer = null;
  const stopFps = () => { if (fpsTimer !== null) win.clearInterval(fpsTimer); fpsTimer = null; fpsLabel.hidden = true; fpsLabel.textContent = ""; };
  // the measured decoded frame rate (and the picture size when known), about once a second, while the live stream plays
  const startFps = (session) => {
    stopFps();
    const meter = createFpsMeter();
    let busy = false;
    const paint = () => {
      const w = video.videoWidth | 0;
      const h = video.videoHeight | 0;
      fpsLabel.textContent = `${w > 0 && h > 0 ? `${w}x${h} ` : ""}${meter.label()}`;
      fpsLabel.hidden = false;
    };
    const tickFps = async () => {
      if (busy) return;
      busy = true;
      const total = await session.framesDecoded();
      busy = false;
      if (live !== session || mode !== "live") return;
      if (total !== null) meter.sample(Math.round(win.performance && win.performance.now ? win.performance.now() : Date.now()), total);
      paint();
    };
    paint();
    fpsTimer = win.setInterval(tickFps, FPS_POLL_MS);
  };
  const stopLive = () => { stopFps(); if (live) live.stop(); live = null; };
  const useStill = (why) => {
    mode = "still";
    stopLive();
    setShown(video, false);
    note.textContent = `${streams[index].entity_id} - NOT live: ${why}. Showing a still picture, refreshed every ${VIEWER_REFRESH_MS / 1000} s`;
    note.dataset.mode = "still";
    load();
    stopStill();
    timer = win.setInterval(load, VIEWER_REFRESH_MS);
  };
  const draw = () => {
    const s = streams[index];
    img.alt = `${spec.name || "Camera"}${streams.length > 1 ? ` (${s.label})` : ""}`;
    switches.forEach((b, i) => { b.setAttribute("aria-pressed", String(i === index)); b.style.fontWeight = i === index ? "700" : "400"; });
    stopStill();
    stopLive();
    none.hidden = true;
    // the live stream takes seconds to deliver its first picture (a camera's next key frame): show the latest still meanwhile
    const still = picture();
    setShown(img, !!still);
    if (still) img.src = withStamp(still, String(now()));
    video.hidden = false;
    video.style.cssText = VIDEO_STASH;
    mode = "connecting";
    note.dataset.mode = "connecting";
    note.textContent = `${s.entity_id} - connecting to the live stream (latest picture shown meanwhile)...`;
    const mine = s.entity_id;
    let via = "";
    const viaText = () => (via ? ` (via ${via})` : "");
    live = startLiveStream(win, getHass(), mine, video, (state, detail) => {
      if (streams[index].entity_id !== mine) return;
      if (state === "via") { via = detail; note.dataset.via = String(detail).toLowerCase(); note.textContent = `${mine} - connecting to the live stream${viaText()} (latest picture shown meanwhile)...`; }
      else if (state === "live") { mode = "live"; setShown(img, false); video.style.cssText = VIDEO_CSS; note.dataset.mode = "live"; note.textContent = `${mine} - live stream${viaText()}`; startFps(live); }
      else if (state === "waiting") { note.textContent = `${mine} - connected${viaText()}, waiting for the camera's first picture...`; }
      else if (state === "failed") useStill(detail || "the live stream could not start");
    });
  };
  img.addEventListener("error", () => { none.hidden = false; none.textContent = "No picture"; img.hidden = true; });
  const shut = () => {
    stopStill();
    stopLive();
    doc.removeEventListener("keydown", onKey, true);
    if (back.parentNode) back.parentNode.removeChild(back);
    if (opener && typeof opener.focus === "function") { try { opener.focus(); } catch (_e) { /* gone */ } }
  };
  const onKey = (ev) => {
    if (ev.key === "Escape") { ev.preventDefault(); ev.stopPropagation(); shut(); return; }
    if (ev.key === "Tab") {                                   // keep focus inside the dialog
      const items = [...switches, close];
      const at = items.indexOf(doc.activeElement);
      const next = ev.shiftKey ? (at <= 0 ? items.length - 1 : at - 1) : (at === items.length - 1 ? 0 : at + 1);
      ev.preventDefault();
      items[next].focus();
    }
  };
  close.addEventListener("click", shut);
  back.addEventListener("click", (ev) => { if (ev.target === back) shut(); });
  doc.addEventListener("keydown", onKey, true);
  doc.body.append(back);
  draw();
  close.focus();
  return { el: back, close: shut, mode: () => mode };
}

function cameraRenderer(doc, element, ctx) {
  const root = node(doc, "div", `pe-tile pe-camera pe-${ctx.size}`);
  root.dataset.element = element.id || "";
  const frame = node(doc, "div", "pe-pic");
  const caption = node(doc, "div", "pe-name", element.name);
  const sub = node(doc, "div", "pe-sub");
  frame.style.minHeight = "96px";
  root.append(frame, caption, sub);
  let stamp = "";
  let latest = {};
  // the line inside the card: the entity, and (when the host supplies it) the size and live/snapshot source
  const setSub = () => {
    const t = element.entity_id && typeof ctx.caption === "function" ? ctx.caption(element.entity_id) : "";
    sub.textContent = t || element.entity_id || "";
    sub.dataset.caption = t ? "info" : "plain";
  };
  let shown = null;     // what the frame now shows; the picture is rebuilt only when this changes (no flicker on a state push)
  const draw = () => {
    let key;
    let src = "";
    let entity = null;
    if (!element.entity_id) key = "stream";
    else {
      entity = Object.prototype.hasOwnProperty.call(latest, element.entity_id) ? latest[element.entity_id] : null;
      if (!entity) key = "missing";
      else {
        src = safePicture(entity.attributes && entity.attributes.entity_picture);
        key = src ? `pic:${withStamp(src, stamp)}` : "nopic";
      }
    }
    if (key === shown) return;
    shown = key;
    frame.replaceChildren();
    if (key === "stream") {
      frame.append(haIcon(doc, iconName(element)), node(doc, "div", "pe-none", "Stream only: no picture here"));
      sub.textContent = streamLabel((element.options && element.options.stream) || "");
      return;
    }
    setSub();
    if (key === "missing") { frame.append(node(doc, "div", "pe-none pe-error", NOT_FOUND_TEXT)); return; }
    if (key === "nopic") { frame.append(node(doc, "div", "pe-none", "No picture")); return; }
    const img = node(doc, "img");
    img.alt = element.name || "Camera";
    img.src = withStamp(src, stamp);
    img.addEventListener("error", () => { img.remove(); frame.append(node(doc, "div", "pe-none", "No picture")); });
    // click, Enter or Space opens the full-size window (the pair passes both streams; a lone camera shows just itself)
    const view = ctx.viewer || { streams: [{ label: "", entity_id: element.entity_id }], index: 0, getStates: () => latest };
    img.setAttribute("role", "button");
    img.tabIndex = 0;
    img.title = "Open full size";
    img.style.cursor = "zoom-in";
    img.setAttribute("aria-label", `Open ${element.name || "camera"}${view.streams.length > 1 && view.streams[view.index].label ? ` (${view.streams[view.index].label})` : ""} full size`);
    const open = () => openCameraViewer(doc, { name: element.name, streams: view.streams, index: view.index, getStates: view.getStates, getHass: ctx.getHass, dark: ctx.dark, now: ctx.now });
    img.addEventListener("click", open);
    img.addEventListener("keydown", (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); open(); } });
    const refresh = node(doc, "button", "pe-refresh", "Refresh");
    refresh.type = "button";
    refresh.addEventListener("click", () => { stamp = String(ctx.now()); draw(); }); // re-reads the same picture; no call to HA
    frame.append(img, refresh);
  };
  draw();
  const h = handle(root, (states) => { latest = states; draw(); });
  h.refreshCaptions = () => { if (element.entity_id && shown && shown !== "stream" && shown !== "missing") setSub(); };
  return h;
}

function buttonRenderer(doc, element, ctx) {
  const options = element.options || {};
  const root = node(doc, "div", "pe-tile pe-button");
  root.dataset.element = element.id || "";
  const b = node(doc, "button", "pe-btn");
  b.type = "button";
  b.disabled = true; // inert: no listener, no action, ever (D8)
  b.setAttribute("aria-disabled", "true");
  b.dataset.action = options.action || "";
  const named = Object.prototype.hasOwnProperty.call(PALETTE, options.color) ? options.color : "";
  if (named) { b.dataset.color = named; paint(b, colorPair(named, ctx.dark)); }
  b.append(haIcon(doc, iconName(element)), node(doc, "span", "pe-t", element.name));
  b.title = element.name;
  root.append(b);
  if (ASKS_FIRST.has(options.action)) root.append(node(doc, "span", "pe-chip pe-asks", "Requires confirmation"));
  root.append(node(doc, "div", "pe-sub", PREVIEW_NOTE));
  return handle(root, () => {}); // a button has no state to follow
}

/* A Talk element (DEV-518): a handset button for one intercom door. Inert like every preview: it never dials. */
function talkRenderer(doc, element) {
  const root = node(doc, "div", "pe-tile pe-button pe-talk");
  root.dataset.element = element.id || "";
  const b = node(doc, "button", "pe-btn");
  b.type = "button";
  b.disabled = true; // inert: no listener, no call, ever (D8)
  b.setAttribute("aria-disabled", "true");
  b.dataset.action = "talk";
  b.append(haIcon(doc, iconName(element)), node(doc, "span", "pe-t", element.name));
  b.title = element.name;
  root.append(b, node(doc, "div", "pe-sub", PREVIEW_NOTE));
  return handle(root, () => {});
}

function statusRenderer(doc, element, ctx) {
  const compact = ctx.size === "compact";
  const options = element.options || {};
  const root = node(doc, "div", `pe-tile ${compact ? "pe-chiptile" : "pe-status"}`);
  root.dataset.element = element.id || "";
  const icon = haIcon(doc, DEFAULT_STATUS_ICON);
  const name = node(doc, "span", "pe-name",
    compact ? (options.short || String(element.name).slice(0, MAX_SHORT)) : element.name);
  const value = node(doc, "span", "pe-value");
  root.append(icon, name, value);
  const draw = (states) => {
    const view = statusView(element, states);
    value.textContent = view.text;
    root.dataset.color = view.color;
    root.dataset.state = view.missing ? "missing" : "ok";
    // an unavailable element shows the default glyph, never a custom icon next to "not found"
    icon.setAttribute("icon", view.missing ? DEFAULT_STATUS_ICON : iconName(element));
    paint(root, colorPair(view.color, ctx.dark));
  };
  draw({});
  return handle(root, draw);
}

function unknownRenderer(doc, element, ctx) {
  const root = node(doc, "div", "pe-tile pe-unknown");
  root.dataset.element = (element && element.id) || "";
  root.append(node(doc, "div", "pe-name", (element && typeof element.name === "string" && element.name) || "Element"),
    node(doc, "div", "pe-sub", "Unknown kind: not drawn"));
  paint(root, colorPair("neutral", ctx.dark));
  return handle(root, () => {});
}

/* The extension table: kind -> (doc, element, ctx) -> { el, update, destroy }. A new kind adds a row here. */
export const RENDERERS = {
  camera: cameraRenderer,
  button: buttonRenderer,
  talk: talkRenderer,
  status: statusRenderer,
  chip: (doc, element, ctx) => statusRenderer(doc, element, { ...ctx, size: "compact" }),
};

/* Draw one element. ctx: { states?, size?: "preview" | "full" | "compact", dark?, now? }. Never throws: a bad element or
 * a renderer that fails draws the neutral tile. */
export function renderElement(doc, element, ctx = {}) {
  const c = { size: "preview", dark: false, now: () => Date.now(), ...ctx };
  const kind = element && typeof element === "object" ? element.kind : undefined;
  let out;
  try {
    const draw = typeof kind === "string" && Object.prototype.hasOwnProperty.call(RENDERERS, kind) ? RENDERERS[kind] : unknownRenderer;
    out = draw(doc, element, c);
  } catch (_err) {
    out = unknownRenderer(doc, element && typeof element === "object" ? element : {}, c);
  }
  if (c.states) out.update(c.states);
  return out;
}

/* A camera with two streams (entity_id = Big, options.small_entity_id = Small) drawn as two labelled pictures side by side
 * (wrapping to a stack on a narrow screen); anything else is exactly renderElement. The Small tile is the same element with
 * entity_id = the small stream and no small option, so the one camera picture path is reused. A tile that cannot be drawn
 * shows the "could not be drawn" line for that tile only. Same handle shape as renderElement: { el, update, destroy }. */
export function renderPreview(doc, element, ctx = {}) {
  const o = element && typeof element === "object" && element.options && typeof element.options === "object" ? element.options : {};
  const small = element && element.kind === "camera" && typeof o.small_entity_id === "string" ? o.small_entity_id : "";
  if (!small) return renderElement(doc, element, ctx);
  const { small_entity_id: _drop, ...rest } = o;
  const wrap = node(doc, "div", "pe-pair");
  wrap.style.cssText = "display:flex;flex-wrap:wrap;gap:8px;align-items:flex-start"; // one line when it fits, wraps when narrow
  const handles = [];
  let latestStates = ctx.states || {};
  const streams = [{ label: "Big", entity_id: element.entity_id || "" }, { label: "Small", entity_id: small }];
  for (const [label, el] of [["Big", { ...element, options: rest }], ["Small", { ...element, entity_id: small, options: rest }]]) {
    const cell = node(doc, "div", "pe-pair-cell");
    cell.dataset.stream = label.toLowerCase();
    cell.style.cssText = "flex:1 1 110px;min-width:0";
    cell.append(node(doc, "div", "pe-sub", label));
    try {
      const h = renderElement(doc, el, { ...ctx, viewer: { streams, index: label === "Big" ? 0 : 1, getStates: () => latestStates } });
      cell.append(h.el);
      handles.push(h);
    } catch (_err) {
      cell.append(node(doc, "span", "el-msg err", "This preview could not be drawn."));
    }
    wrap.append(cell);
  }
  return {
    el: wrap,
    update(states) { latestStates = states || {}; for (const h of handles) { try { h.update(states); } catch (_err) { /* one tile only */ } } },
    refreshCaptions() { for (const h of handles) { try { if (h.refreshCaptions) h.refreshCaptions(); } catch (_err) { /* one tile only */ } } },
    destroy() { for (const h of handles) { try { h.destroy(); } catch (_err) { /* gone */ } } },
  };
}
