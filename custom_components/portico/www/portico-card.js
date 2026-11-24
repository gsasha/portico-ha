/**
 * Portico Lovelace card (contract 3).
 *
 * Design doc §9.7, the content design (automation/portico_content_design.md §6) and the editor design
 * (automation/portico_editor_design.md §3.3-3.4, §5). Registered as a Lovelace resource: a static path the
 * integration serves plus frontend.add_extra_js_url (__init__.py), so it installs with the HACS package.
 *
 * Contract 3 only. The first thing every card does is `portico/version`: any answer but `{contract: 3}`
 * (or an `unknown_command`) shows one line and a Reload button and nothing else. Then `portico/doors`
 * (the door list), `portico/calls/subscribe` (call-state snapshots and events, per door) and
 * `portico/content/subscribe` (`content_changed` for the card's endpoint), then `portico/content/get`.
 * Every call command carries the door's `door_id`.
 *
 * The card shows ONE endpoint's document (`pages`, `callers`). Config `endpoint: <slug>` names a screen
 * (or, for an administrator, any phone or screen); a card without `endpoint` shows the viewing user's OWN
 * endpoint (what a phone user's card always did). An administrator without `endpoint` is refused by the
 * server (`unauthorized`): the card says "name a screen with endpoint:". There is no `profile:` any more.
 *
 * Three call states: idle, ringing, in-call (video pane + hang up / open). Answering needs getUserMedia,
 * which requires HTTPS or localhost: a stated prerequisite (§9.9), not silently swallowed if it fails.
 * Pictures come over the same websocket (portico/content/snapshot); a button is run by id
 * (portico/content/run) and a lock or gate asks for a second tap first. Nothing here names a service or an
 * entity: the card sends ids from the document, as the app does.
 *
 * Ring takeover (config `takeover: true`): a ring opens a full-viewport overlay drawn from
 * `callers[<the ringing door's endpoint_id>]` (name, camera stills, ring buttons; call buttons in a call),
 * Answer (only for a door that has audio), Dismiss, and a chime; it closes itself when the ring ends, when
 * somebody else answers (after saying so), or after 120 s of nobody answering. Stills come through
 * portico/content/snapshot every 1.5 s while the overlay shows, one request per camera in flight. The
 * overlay is attached to document.body, so no ancestor's CSS transform can break its `position: fixed`.
 * It cannot wake a sleeping Fully Kiosk screen or stop its screensaver: that stays with the existing
 * automation. `fullpage: true` fills the viewport (the kiosk page /portico/kiosk/<slug>).
 *
 * A second ring while this card is in a call (or answering one), in EVERY mode, is refused with a one-line
 * notice: the first call keeps its PeerConnection and microphone (R7-19), and its own `ended` tears it down.
 *
 * Live video (portico/content/live): a camera tile with `live` (or that the viewer tapped live) plays WebRTC video, video only
 * and muted, in a <video> over its still; the still stays underneath and is the fallback for any failure. A tap on a tile
 * switches it between live and still for this viewer (localStorage). A tile completely off screen, or a hidden page, streams
 * nothing; the ring overlay's first camera goes live at a ring. `show_fps` draws a frames-per-second label on live tiles.
 *
 * Status: protocol behaviour is tested headless (www/tests, jsdom, Node) with the websocket and WebRTC
 * mocked. NOT verified in a real browser or on a real tablet: the look, the overlay over Fully's
 * screensaver, the chime past the autoplay rule.
 */

const STATE_IDLE = "idle";
const STATE_RINGING = "ringing";
const STATE_IN_CALL = "in-call";

// transport-rs/src/driver AudioTopology serialises as a number; NoAudio is 3.
const AUDIO_NONE = 3;
const DOOR_KINDS = new Set(["portico_open", "lock_open"]);

// Screen wake (Devices D3, DEVICES-01..10). The server sends `wake: {doors: [door ref...], screen_call}` with content/get and
// content_changed for a schema 6 SCREEN only; a phone, an old schema or an unmigrated store sends none, and then nothing here
// filters (the old behaviour: every ring takes over). A door ref is matched the way the server's Endpoint.names_door does
// (a bare device_id, or `<provider>:<device_id>`), never by string equality with the endpoint id.
function normWake(w) {
  if (!w || typeof w !== "object" || !Array.isArray(w.doors)) return undefined;
  return { doors: w.doors.filter((r) => typeof r === "string"), screen_call: w.screen_call !== false,
           path: typeof w.path === "string" ? w.path : null };
}

// The door's provider and device_id from `endpoint_id` ("<provider>:<device_id>", split at the FIRST colon); without a
// namespaced endpoint_id the door_id is the bare device_id and no namespaced ref can name it.
function doorNames(door, ref) {
  if (!door || typeof ref !== "string") return false;
  const eid = typeof door.endpoint_id === "string" ? door.endpoint_id : "";
  const at = eid.indexOf(":");
  const provider = at > 0 ? eid.slice(0, at) : null;
  const deviceId = at > 0 ? eid.slice(at + 1) : (door.door_id || null);
  if (!deviceId) return false;
  const i = ref.indexOf(":");
  return ref === deviceId || (i >= 0 && !!provider && ref.slice(0, i) === provider && ref.slice(i + 1) === deviceId);
}

// May a ring at `door` take this screen over? No wake at all = yes (old behaviour).
function wakeAllowsDoor(wake, door) {
  if (!wake) return true;
  return wake.doors.some((ref) => doorNames(door, ref));
}

// Who shows the overlay / sounds the chime for a call (Devices D3b). The every-page wake listener (WakeListener, the end of this file)
// runs a body-level card of its own, so a page that also mounts a card would show the same ring twice: the first card to claim a
// call id (`door:<call_id>` or `house:<call_id>`) owns it, the other stays silent. A claim lasts until its card is detached.
const CLAIMS = new Map();
const CLAIMS_MAX = 64;
function claimCall(key, card) {
  const owner = CLAIMS.get(key);
  if (owner !== undefined && owner !== card) return false;
  if (owner === undefined) {
    CLAIMS.set(key, card);
    if (CLAIMS.size > CLAIMS_MAX) CLAIMS.delete(CLAIMS.keys().next().value);
  }
  return true;
}
function releaseClaims(card) {
  for (const [k, v] of Array.from(CLAIMS)) if (v === card) CLAIMS.delete(k);
}

// K2 (DEVICES 4.5): the dashboard path of the view that holds a screen's card. A mounted card with `path:` remembers it here (per
// browser, per screen) because the listener runs on pages where no card is mounted. Only an in-app path is ever kept or used.
const WAKE_PATH_PREFIX = "portico-wake-path:";
function wakePathGet(slug) {
  if (!slug) return null;
  try { return validBackPath(window.localStorage.getItem(WAKE_PATH_PREFIX + slug)); } catch (err) { return null; }
}
function wakePathSet(slug, path) {
  const ok = validBackPath(path);
  if (!slug || !ok) return;
  try { window.localStorage.setItem(WAKE_PATH_PREFIX + slug, ok); } catch (err) { /* blocked storage: no K2 from this browser */ }
}

const RING_TIMEOUT_MS = 120000;
const ANSWERED_ELSEWHERE_MS = 3000;
// Failsafe for an overlay held for a call this card did not answer (a lost `ended` must not leave it on the wall): ask the
// server for the door's call state every FAILSAFE_POLL_MS; two answers in a row with no active call close it. The hard cap
// only applies when the server has not answered for FAILSAFE_STALE_MS (a call can last longer than any ring).
const FAILSAFE_POLL_MS = 1500;
const FAILSAFE_IDLE_POLLS = 2;
const FAILSAFE_CAP_MS = 120000;
const FAILSAFE_STALE_MS = 10000;
const FAILSAFE_PROBE_TIMEOUT_MS = 4000;
const FAILSAFE_LOG_EVERY_MS = 2000;
const ALREADY_ANSWERED_TEXT = "Someone else already answered this call";
const DEVICE_BUSY_TEXT = "The door station is busy with another call. Try again in a moment.";
const REFUSED_TEXT = "Answering from Portico is switched off at the door";
const CALL_ENDED_TEXT = "That call has ended";
const UNKNOWN_DOOR_TEXT = "Portico does not know this door any more";
const NAME_A_SCREEN_TEXT = "Name a screen with endpoint: in this card (this login is not an enrolled phone).";
const VERSION_TEXT = "Portico is being updated; this page works again after Home Assistant restarts.";
const SNAPSHOT_EVERY_MS = 4000;
const RING_SNAPSHOT_EVERY_MS = 1500;
const ARM_MS = 4000;
const NOTICE_MS = 8000;

// Blocks (portico blocks design 6.5). The palette is contract/palette_v1.json ({name: {light|dark: [fill, text]}}); a node
// test pins this copy equal to the file. KIND_ICON is the icon of a button that names none (the panel and the app agree).
const PALETTE = {
  blue: { light: ["#1565C0", "#FFFFFF"], dark: ["#90CAF9", "#0D2B4E"] },
  green: { light: ["#2E7D32", "#FFFFFF"], dark: ["#A5D6A7", "#0F3A14"] },
  amber: { light: ["#8A5A00", "#FFFFFF"], dark: ["#FFD54F", "#3D2A00"] },
  red: { light: ["#C62828", "#FFFFFF"], dark: ["#EF9A9A", "#5A0E0E"] },
  purple: { light: ["#6A1B9A", "#FFFFFF"], dark: ["#CE93D8", "#3B0F52"] },
  teal: { light: ["#00796B", "#FFFFFF"], dark: ["#80CBC4", "#00332D"] },
  pink: { light: ["#AD1457", "#FFFFFF"], dark: ["#F48FB1", "#4F0A27"] },
  grey: { light: ["#546E7A", "#FFFFFF"], dark: ["#B0BEC5", "#1F2A30"] },
};
const KIND_ICON = {
  portico_open: "mdi:door-open",
  lock_open: "mdi:lock-open-variant",
  button_press: "mdi:gesture-tap-button",
  script_run: "mdi:script-text-play",
  scene_activate: "mdi:palette",
  call: "mdi:phone-in-talk",
};
const BIG_STILL_WIDTH = 960; // a big tile asks for this many pixels; a small one sends no width (the server's default)

// Wide layout (the wall tablet). Pure functions: jsdom has no layout engine, so the decisions and the arithmetic
// are testable here and the DOM only receives the numbers.
const WIDE_MIN_W = 720;   // a card at least this wide (CSS px) draws the 2-column layout
const W_PAD = 16;         // ha-card padding
const W_GAP = 10;         // grid gap
const W_LABEL = 24;       // a camera tile's caption row
const W_TITLE = 34;       // the block title row
const W_BTN = 48;         // a button row
const W_BTN_MIN = 150;    // the narrow grid's minmax(150px, 1fr): the same grid is used under the cameras
const W_EXTRA = 60;       // a compact (second and later) block in fullpage mode: one 48px row + gap
const W_HOUSE = 58;       // fullpage: the Everyone row (M7 3d): one 48px key + the gap above it; 0 while house calls are off
const W_PEEK = 40;        // fullpage: when more content follows (later blocks, the Everyone row), the first screen leaves this much of it showing, so the page reads as scrollable
const W_MORE_COLS = 3;    // fullpage: a later block's camera tiles, this many per row
// fullpage: the left edge is the back rail's (see BACK_RAIL_STYLE): 10px margin + the 64px button + 10px clear gap. The
// content starts to its right, so the rail never overlaps the main camera, the side cameras or the buttons.
const W_RAIL = 84;
const BACK_RAIL_SIZE = 64;
const BACK_RAIL_MARGIN = 10;
// The back rail's default target: none (DEV-836; the old default was an estate route). A card config key `back_path`
// names an in-app route ("/lovelace/home"); the kiosk page gets it from the `kiosk_back_path` option (see portico-panel.js).
// With none, the rail uses the layered fallback (history back, else close).
const DEFAULT_BACK_PATH = "";

function wideDecision(w, h, fullpage) {
  // A fullpage card has a real box (the viewport): wide AND wider than tall. A Lovelace card's height is made of its
  // own content, so a height test would be circular: there the width alone decides.
  if (!(w >= WIDE_MIN_W)) return false;
  return fullpage ? w > h : true;
}

// Fullpage (the wall tablet) sizes touch targets and the title to the screen instead of the 48px floor: a button row is
// about 9% of the height (72px at 800), clamped 48..80, so the controls get a real share of the screen and the camera takes what is left; the title row is a legible 22px. A Lovelace card keeps the floor.
function fullpageBtnH(h) { return Math.max(W_BTN, Math.min(80, Math.max(48, Math.round(h * 0.09)))); }
const W_TITLE_FULL = 38;

// o: {w, h, fullpage, nMain (0 or 1), nSide (cameras after the first), nButtons, hasTitle, extras (later blocks, fullpage only),
//     house (0 or 1: the Everyone row under the blocks, fullpage only)}
// Fullpage: the buttons, the title and the later blocks' rows span `rowW` (the camera group's width, or the width the
// button columns need if that is more), in `btnCols` equal columns, so they line up with the picture and spread over it
// instead of hugging the left edge. The row count comes from the full content width, as before.
function wideGeometry(o) {
  const cw = o.w - (o.fullpage ? W_RAIL + W_PAD : 2 * W_PAD);
  const titleH = o.hasTitle ? (o.fullpage ? W_TITLE_FULL : W_TITLE) : 0;
  const bh = o.fullpage ? fullpageBtnH(o.h) : W_BTN;
  const extraH = o.fullpage ? bh + 12 : W_EXTRA;
  const perRow = Math.max(1, Math.floor((cw + W_GAP) / (W_BTN_MIN + W_GAP)));
  const rows = o.nButtons ? Math.ceil(o.nButtons / perRow) : 0;
  const cols = rows ? Math.ceil(o.nButtons / rows) : 0;
  const btnH = rows ? rows * bh + (rows - 1) * W_GAP + W_GAP : 0; // includes the gap above the row
  const houseH = o.fullpage && o.house ? W_HOUSE : 0;
  const peek = o.fullpage && ((o.extras || 0) > 0 || o.house) ? W_PEEK : 0;
  const hasSide = o.nSide > 0;
  const hasMain = o.nMain !== 0;
  let mw = hasSide ? (cw - W_GAP) * 2 / 3 : cw;
  // The page scrolls (2026-11-12): the first block alone is fitted to the screen, so the main camera and its buttons are
  // whole on the first screen; every later block and the Everyone row sit below it and scroll into view.
  const extrasShown = o.fullpage ? (o.extras || 0) : 0;
  if (o.fullpage) {
    const avail = o.h - 2 * W_PAD - titleH - btnH - peek - (hasMain ? W_LABEL : 0);
    mw = Math.min(mw, Math.max(avail, 60) * 16 / 9);
  }
  const mh = mw * 9 / 16;
  // The side column is at most a third (mw / 2 is half of the main's 2/3). Side cameras keep 16:9 and share the main
  // tile's height, so with several of them the column hugs them (narrower than a third) and the group stays centred.
  let sh = 0;
  if (hasSide) {
    const t = (mh + W_LABEL - (o.nSide - 1) * W_GAP) / o.nSide;
    sh = Math.max(0, Math.min(mw / 2 * 9 / 16, t - W_LABEL));
  }
  const sw = hasSide ? sh * 16 / 9 : 0;
  const sideTotal = hasSide ? o.nSide * (sh + W_LABEL) + (o.nSide - 1) * W_GAP : 0;
  const height = 2 * W_PAD + titleH + (hasMain ? mh + W_LABEL : 0) + btnH; // the first screen's content; later blocks and the house row scroll
  const groupW = mw + (hasSide ? W_GAP + sw : 0);
  const rowW = Math.min(cw, Math.max(hasMain ? groupW : 0, cols * W_BTN_MIN + Math.max(0, cols - 1) * W_GAP));
  return { mw, mh, sw, sh, sideW: sh * 16 / 9, sideTotal, btnRows: rows, btnCols: cols, btnH, btnRowH: bh, extraH, titleH,
           houseH, extrasShown, height, groupW, rowW };
}

// A later block in fullpage mode (the Door row...): its camera tiles are W_MORE_COLS to a row across the width the first block
// uses (rowW), 16:9, with their captions. Sizes are computed up front, so nothing shifts when the pictures load.
function wideMoreGeometry(rowW) {
  const tw = Math.max(120, Math.floor((rowW - (W_MORE_COLS - 1) * W_GAP) / W_MORE_COLS));
  return { tw, th: Math.round(tw * 9 / 16), cols: W_MORE_COLS };
}

// A back_path is an in-app route: one leading "/", then not "/" (that is "//host"), no backslash ("/\\host" is "//host" to a
// browser), no control character or space, no scheme. Anything else is not navigated to. Returns the path or null.
function validBackPath(p) {
  if (typeof p !== "string" || p.length < 1 || p.length > 512) return null;
  if (p[0] !== "/" || p[1] === "/" || p[1] === "\\") return null;
  if (/[\\\s\u0000-\u001f\u007f]/.test(p)) return null;
  return p;
}

// The path the rail goes to: undefined -> the default; "" / null / false -> none configured (the layered fallback);
// a string that fails validBackPath -> null too (never navigate to something unexpected).
function backTarget(config) {
  const v = config ? config.back_path : undefined;
  if (v === undefined) return validBackPath(DEFAULT_BACK_PATH);
  return validBackPath(v);
}

// The way out when there is no back path, or navigating to it failed. All three stay on this origin.
function closeAction(hasFully, historyLength) {
  if (hasFully) return "fully";
  if (historyLength > 1) return "back";
  return "home";
}

// ---- House calls on the kiosk page (M7 3d, design 15.14) -------------------------------------------------------------
// Everything below is dormant while the server's house-call model is off: the roster then has no `everyone` entry, the
// card draws no key, and no `house_call` event ever arrives. No audio here (3b): the page sends the commands and shows
// the server's states.
const HOUSE_END_MS = 4000;      // an outcome stays this long ("Everyone declined"), or until it is closed
const HOUSE_STALE_MS = 60000;   // a ring or answer the server never ended (lost event): let go of the screen after this
const HOUSE_GROUP = "everyone";

// The Everyone row's style, added to the page only when the server sent the key (so the page is unchanged while it is off).
const HOUSE_CSS = `
        .acts.blk.house-row { grid-template-columns: 1fr; margin-top: 10px; }
        .house-row .act { height: 48px; overflow: hidden; white-space: nowrap; }
        .house-row .act .sub { font-size: 13px; opacity: .9; text-overflow: ellipsis; overflow: hidden; }`;

// The roster's Everyone entry, or null: only on a fullpage (kiosk) card, only when the server sent one (it omits the key
// while house calls are off, and for an endpoint that may not call everyone). Nothing else is ever inferred.
function houseEntry(content, fullpage) {
  if (!fullpage || !content || !Array.isArray(content.roster)) return null;
  const e = content.roster.find((r) => r && r.kind === "group" && r.id === HOUSE_GROUP && typeof r.action_id === "string");
  return e || null;
}

function phonesText(n) {
  return n === 1 ? "1 phone" : `${n} phones`;
}

// The key's words. Ready: "Everyone (2 phones)". Off: the server's reason in words.
function houseKeyText(entry, busyHere) {
  if (busyHere) return { label: "Everyone", sub: "You are on a call", disabled: true };
  if (entry.available) {
    const n = Number.isInteger(entry.count) ? entry.count : 0;
    return { label: n > 0 ? `Everyone (${phonesText(n)})` : "Everyone", sub: "", disabled: false };
  }
  const why = {
    no_receivers: "No phone is set to receive house calls",
    busy: "You are on a call",
  }[entry.reason] || "Not available right now";
  return { label: "Everyone", sub: why, disabled: true };
}

// A refusal of the placement (the content/run error code) in words.
function houseRefusalText(code) {
  switch (code) {
    case "busy": return "You are already on a call.";
    case "not_callable": return "No phone is set to receive house calls right now.";
    case "forbidden": return "This screen may not call everyone.";
    case "place_forbidden": return "A house call is placed from the screen itself.";
    case "target_offline": return "That phone is not reachable.";
    case "target_dnd": return "That phone has Do Not Disturb on.";
    case "not_available": return "House calls are not switched on.";
    case "rate_limited": return "Wait a moment, then try again.";
    case "unauthorized":
    case "unknown_action": return "This screen may not place that call.";
    default: return "The call could not be placed.";
  }
}

// What an ended house call says (design 10 and 14.6), by the reason the server gave.
function houseOutcomeText(st, name) {
  const who = name || "Someone";
  switch (st.reason) {
    case "declined": return st.group ? "Everyone declined" : `${who} declined`;
    case "no_answer": return st.group ? "No one answered" : `${who} did not answer`;
    case "busy": return st.group ? "Everyone is busy" : `${who} is on a call`;
    case "dnd": return `${who} has Do Not Disturb on`;
    case "error": return "Could not connect";
    case "refused": return st.text || "The call could not be placed.";
    default: return "The call ended";
  }
}

const houseLive = (st) => !!st && st.state !== "ended";

// The overlay's state machine, pure. `cur` is null or {callId, dir: "out"|"in", state: placing|ringing|answered|active|ended,
// peer (an endpoint id), group, count, reason, text}; `ev` is a `house_call` event {call_id, state, from, to, reason?};
// `own` is this screen's endpoint id. Returns the next state (null = nothing to show). Rules:
//  - an event for a call other than the one shown is ignored while that one is live; a late `ended` of anything we do not
//    show is ignored; a finished outcome gives way to a new call;
//  - the callee's ring that ends before the person answered (answered elsewhere, cancelled, no answer, declined by me) is
//    silent: the screen just lets go;
//  - the caller's own cancel is silent; every other end of our call shows its outcome.
function houseReduce(cur, ev, own) {
  if (!ev || ev.type !== "house_call" || typeof ev.call_id !== "string" || !ev.call_id) return cur;
  const out = ev.from === own;
  if (!out && ev.to !== own) return cur;                       // not about this screen
  const placing = !!cur && cur.state === "placing" && !cur.callId && out;
  const same = !!cur && (cur.callId === ev.call_id || placing);
  if (cur && !same && houseLive(cur)) return cur;             // a live call is never displaced by a stranger's event
  const base = same ? cur : null;
  if (base && base.state === "ended") return cur;            // late event for a finished call
  if (ev.state === "ended") {
    if (!base) return cur;
    if (base.dir === "in" && base.state === "ringing") return null;      // the ring ended before we answered: silent
    if (ev.reason === "cancelled" || ev.reason === "answered_elsewhere") return null;
    return { ...base, callId: ev.call_id, state: "ended", reason: ev.reason || "hangup" };
  }
  if (ev.state !== "ringing" && ev.state !== "answered" && ev.state !== "active") return cur;
  const next = base ? { ...base } : { callId: ev.call_id, dir: out ? "out" : "in", peer: null, group: false, count: 0, reason: null };
  next.callId = ev.call_id;
  next.dir = out ? "out" : "in";
  next.state = ev.state;
  if (!out) next.peer = ev.from;
  else if (ev.to === HOUSE_GROUP) { next.group = true; next.peer = null; }
  else next.peer = ev.to;
  return next;
}

// The overlay's sizes for a screen of w x h CSS pixels: the title, the status line and one row of buttons stack inside the
// screen (the overlay is above the page, so the page's own geometry is not touched) and two buttons fit side by side.
function houseOverlayGeometry(w, h) {
  const pad = 16;
  const gap = 12;
  const small = h < 560;
  const titleSize = Math.max(22, Math.min(34, Math.floor(h * 0.06)));
  const statusSize = small ? 16 : 20;
  const btnH = small ? 56 : 72;
  const btnW = Math.max(120, Math.min(260, Math.floor((w - 2 * pad - gap) / 2)));
  const total = 2 * pad + Math.ceil(titleSize * 1.3) + gap + Math.ceil(statusSize * 1.4) + 2 * gap + btnH;
  return { pad, gap, titleSize, statusSize, btnH, btnW, total };
}

// Fullpage (the kiosk route): the back rail. Look copied from the estate's own back-rail card
// (its `mount()`): a 64px round button, 10px from the left edge, vertically centred, rgba(0,0,0,.55) with a
// white 34px arrow, no ripple, `will-change: transform` (its own compositor layer: without it that WebView sometimes
// paints a plain fixed element as a sliver), z-index 4. Same inline-style list as the original.
const BACK_RAIL_STYLE = [
  "position:fixed", "top:calc(50% - 32px)", `left:${BACK_RAIL_MARGIN}px`, "z-index:4", "will-change:transform",
  "box-sizing:border-box", "margin:0", "padding:0",
  `width:${BACK_RAIL_SIZE}px`, `height:${BACK_RAIL_SIZE}px`, `min-width:${BACK_RAIL_SIZE}px`, `min-height:${BACK_RAIL_SIZE}px`,
  "border:none", "border-radius:50%", "background:rgba(0,0,0,0.55)", "color:#fff",
  "display:flex", "align-items:center", "justify-content:center", "cursor:pointer",
  "-webkit-tap-highlight-color:transparent",
].join(";");
const BACK_RAIL_ICON = '<svg viewBox="0 0 24 24" style="width:34px;height:34px;fill:currentColor">'
  + '<path d="M20 11H7.8l5.6-5.6L12 4l-8 8 8 8 1.4-1.4L7.8 13H20v-2z"/></svg>';
// The kiosk card is TRANSPARENT (the owner, 2026-11-12): the page that hosts it (portico-panel.js, the kiosk route's host element) wears the
// wall tablet's main-screen background, so the card sets no background of its own and takes the theme's light-on-black text
// colours of the `screensaver_black` theme (repos/portal/screensaver_black.yaml: #e8eaed / #9aa0a6). Surfaces that sit on the
// picture (captions, notices) get a dark translucent plate or a shadow so they stay legible on a busy picture; the card's
// own element is the scroller, and the page behind it does not scroll, so the picture stays put.
const FULLPAGE_CSS = `
        :host([fullpage]) { background: transparent; color: #e8eaed; --primary-text-color: #e8eaed; --secondary-text-color: #9aa0a6;
                            --divider-color: rgba(232, 234, 237, .35);
                            -webkit-overflow-scrolling: touch; overscroll-behavior: contain; touch-action: pan-y; scrollbar-width: none; }
        :host([fullpage])::-webkit-scrollbar { display: none; width: 0; height: 0; }
        :host([fullpage]) ha-card { padding-left: ${W_RAIL}px; padding-bottom: 28px; background: transparent; --ha-card-background: transparent;
                                    color: #e8eaed; }
        :host([fullpage]) .block-title { text-shadow: 0 1px 4px #000, 0 0 14px rgba(0, 0, 0, .9); }
        :host([fullpage]) .cam .label { box-sizing: border-box; height: ${W_LABEL}px; line-height: ${W_LABEL}px; padding: 0 8px; margin-top: 0;
                                        white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
                                        background: rgba(0, 0, 0, .55); border-radius: 0 0 8px 8px; }
        :host([fullpage]) .muted, :host([fullpage]) .notice, :host([fullpage]) .error { text-shadow: 0 1px 4px #000, 0 0 10px rgba(0, 0, 0, .9); }
        :host([fullpage]) .tab { background: rgba(0, 0, 0, .55); }`;

// The wide drawing (container >= 720px). Added only when wide, so the narrow style text is unchanged.
const WIDE_CSS = `
        .block.wide { margin-top: 0; }
        .wide-grid { display: grid; grid-template-columns: var(--w-mw) var(--w-sw, 0px); gap: 10px; justify-content: center; align-items: start; }
        .wide-grid.solo { grid-template-columns: var(--w-mw); }
        .wide-main .cam .pic { width: var(--w-mw); }
        .wide-side { display: flex; flex-direction: column; gap: 10px; height: var(--w-sideh); }
        .wide-side .cam .pic { height: var(--w-sh); width: calc(var(--w-sh) * 16 / 9); aspect-ratio: auto; }
        .block.wide > .acts.blk { margin-top: 10px; }
        .block.wide.more { margin-top: 22px; }
        .more-cams { display: grid; grid-template-columns: repeat(3, var(--w-tw)); gap: 10px; justify-content: start; }
        .more-cams .cam .pic { width: var(--w-tw); height: var(--w-th); aspect-ratio: auto; }`;

// The wall tablet's drawing (fullpage AND wide), on top of WIDE_CSS: the title, the button rows and the later blocks' rows
// span the camera group's width (--w-gw, set by _drawWideBlock from wideGeometry), buttons are --w-bh tall (sized to the
// screen, >= 48px) in --w-cols equal columns. Every size here is the one wideGeometry reserved, so nothing shifts or clips.
const FULLPAGE_WIDE_CSS = `
        :host([fullpage][wide]) ha-card { display: flex; flex-direction: column; justify-content: flex-start; }
        :host([fullpage][wide]) .block.wide > .block-title { box-sizing: border-box; width: var(--w-gw); margin: 0 auto; height: 38px;
                                                              padding-bottom: 8px; font-size: 22px; line-height: 30px; font-weight: 500;
                                                              white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        :host([fullpage][wide]) .block.wide > .acts.blk { width: var(--w-gw); margin: 10px auto 0; gap: 10px;
                                                          grid-template-columns: repeat(var(--w-cols, 1), minmax(0, 1fr)); }
        :host([fullpage][wide]) .acts.blk .act { height: var(--w-bh); min-height: var(--w-bh); padding: 0 12px; font-size: 20px;
                                                  --mdc-icon-size: 28px; }
        :host([fullpage][wide]) .more-cams { width: var(--w-gw); margin: 0 auto; }`;

function blockPaletteCss() {
  let light = "";
  let dark = "";
  for (const [name, c] of Object.entries(PALETTE)) {
    light += `.act.c-${name} { --pt-fill: ${c.light[0]}; --pt-text: ${c.light[1]}; }\n`;
    dark += `:host([dark]) .act.c-${name} { --pt-fill: ${c.dark[0]}; --pt-text: ${c.dark[1]}; }\n`;
  }
  return light + dark;
}

function iconNameOf(action) {
  const given = typeof action.icon === "string" ? action.icon.replace(/^mdi:/, "") : "";
  if (/^[a-z0-9]+(-[a-z0-9]+)*$/.test(given)) return `mdi:${given}`;
  return KIND_ICON[action.kind] || "mdi:gesture-tap";
}

// The websocket contracts the card speaks (portico/version -> {contract}).
const SUPPORTED_CONTRACTS = [3];

// A device that cannot do an action at all (the transport's `not_supported`
// IPC error, relayed unchanged as the websocket error code: e.g.
// gate_sip_observer's Answer/Dial/OpenRelay, design doc §4.2a) is not a
// failure to debug: show the server's reason instead of the generic text.
function describeError(err, fallback) {
  if (err && err.code === "device_busy") return DEVICE_BUSY_TEXT;
  // The door does not let Portico answer (gate_sip_observer with peer claims switched off; not because
  // someone else answered): the ring stays.
  if (err && err.code === "refused") return REFUSED_TEXT;
  if (err && err.code === "unknown_door") return UNKNOWN_DOOR_TEXT;
  if (err && err.code === "not_supported") {
    const reason = String(err.message || "This device cannot do that from Home Assistant.");
    return reason.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
  }
  return fallback;
}

function contentErrorText(err, screen) {
  switch (err && err.code) {
    case "unauthorized":
      return screen
        ? "This Home Assistant user is not set up for this screen (Portico > Screens)."
        : NAME_A_SCREEN_TEXT;
    case "unknown_endpoint":
      return "No screen has that id. Check the card's `endpoint`.";
    default:
      return "Could not load this content.";
  }
}

function actionResultText(label, err) {
  const code = err && err.code;
  if (code === "rate_limited") return `“${label}” ran a moment ago. Wait a moment.`;
  if (code === "unavailable") return `“${label}”: that is unavailable right now.`;
  if (code === "unauthorized" || code === "unknown_action") return `“${label}”: not allowed for this phone.`;
  if (code === "not_supported") return `“${label}”: ${String((err && err.message) || "not supported")}`;
  return `“${label}”: Home Assistant could not run it.`;
}

// Escape a provider-supplied string for the innerHTML templates in _render (device ids, drivers,
// error text). The content path uses el()/textContent and does not need this.
function esc(value) {
  return String(value).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

// ---- live video of a camera tile (portico/content/live, live_ice, live_close) ---------------------------------------------
// WebRTC, VIDEO ONLY: one receive-only video line, no audio line and no microphone, in a peer connection of its own (the
// call's `_pc` and its <audio> element are never touched). The stills stay underneath as the poster and are the fallback
// for ANY failure: nothing here shows an error text.
const LIVE_RETRY_MS = [2000, 5000, 15000];   // after a failure of an established or half-made connection; then stills until a tap/visibility change
const LIVE_CONNECT_MS = 20000;               // no first frame by then: a failure
const LIVE_DISCONNECT_MS = 5000;             // an ICE "disconnected" that does not recover by then: a failure
const LIVE_FPS_MS = 1000;
// An error answer to the command itself (before the result) is final: asking again would get the same answer.
const LIVE_FINAL_CODES = new Set(["unauthorized", "unknown_endpoint", "unknown_camera", "unavailable", "too_many_live",
                                  "invalid_format", "live_unavailable", "unknown_command"]);
const LIVE_STORE_PREFIX = "portico-card:live:";

function liveStoreGet(key) {
  try {
    const v = window.localStorage.getItem(LIVE_STORE_PREFIX + key);
    return v === "1" ? true : v === "0" ? false : null;
  } catch (err) { return null; }
}

function liveStoreSet(key, value) {      // value: true, false, or null (forget: the document's setting decides again)
  try {
    if (value === null) window.localStorage.removeItem(LIVE_STORE_PREFIX + key);
    else window.localStorage.setItem(LIVE_STORE_PREFIX + key, value ? "1" : "0");
  } catch (err) { /* private mode or blocked storage: the choice lasts until the page is closed */ }
}

function liveSizeOf(tileSize) {          // the tile's drawn size decides the stream: a big tile the main one, anything smaller the small one
  return tileSize === "big" ? "big" : "small";
}

// Sends the live command as a subscription and hands back its RESULT, which Home Assistant's websocket library drops for a
// subscription (it resolves with the unsubscribe function only). The result frame is read from the socket as it passes
// (`connection.socket`, matched on the command id the library writes into `msg.id` as it sends); a server that sends the
// session id as a first `{type: "session", session_id}` event is understood too. If neither shows, the result resolves with
// a null session id when the subscription is confirmed: the answer and candidates still work, only live_ice and live_close
// cannot be sent (the unsubscribe still ends the session on the server).
function openLive(connection, msg, onEvent) {
  let settle = null;
  const result = new Promise((resolve, reject) => { settle = { resolve, reject }; });
  const socket = connection.socket;
  let listener = null;
  const detach = () => {
    if (listener && socket) { try { socket.removeEventListener("message", listener); } catch (err) { /* gone */ } }
    listener = null;
  };
  if (socket && typeof socket.addEventListener === "function") {
    listener = (ev) => {
      let frames;
      try { frames = JSON.parse(ev.data); } catch (err) { return; }
      for (const f of Array.isArray(frames) ? frames : [frames]) {
        if (f && f.id === msg.id && f.type === "result") {
          detach();
          if (f.success && f.result && typeof f.result.session_id === "string") settle.resolve(f.result);
        }
      }
    };
    socket.addEventListener("message", listener);
  }
  let unsub = null;
  let closed = false;
  const callUnsub = () => {
    const u = unsub;
    unsub = null;
    if (!u) return;
    try { const r = u(); if (r && typeof r.catch === "function") r.catch(() => {}); } catch (err) { /* connection gone */ }
  };
  let pending;
  try {
    pending = Promise.resolve(connection.subscribeMessage((event) => {
      if (event && event.type === "session" && typeof event.session_id === "string") {
        settle.resolve({ session_id: event.session_id, stream: event.stream });
        return;
      }
      onEvent(event);
    }, msg, { resubscribe: false }));
  } catch (err) {
    pending = Promise.reject(err);
  }
  pending.then((u) => {
    unsub = u;
    detach();
    settle.resolve({ session_id: null, stream: null });     // a no-op when the real result was already read
    if (closed) callUnsub();
  }, (err) => { detach(); settle.reject(err); });
  return {
    result,
    // the socket listener stays until the result (or a failure) arrives: a session closed before its id was known is still closed by name
    unsubscribe() { closed = true; callUnsub(); },
  };
}

class LiveView {
  // o: {key, cameraId, size ("big"|"small"), connection () -> the websocket connection, scope {endpoint?}, retryMs [..],
  //     connectMs, disconnectMs, fpsMs, wantFps, onState (), onFps ()}
  constructor(o) {
    this._o = o;
    this.key = o.key;
    this.cameraId = o.cameraId;
    this.size = o.size;
    this.state = "idle";     // idle | starting | playing | retrying | failed | stopped
    this.fps = null;
    this.wantFps = !!o.wantFps;
    this.opens = 0;          // how many sessions this view has opened (a test reads it)
    this._attempt = 0;
    this._retry = 0;
    this._pc = null;
    this._handle = null;
    this._sessionId = null;
    this._pending = [];       // local candidates found before the session id was known
    this._chain = Promise.resolve();
    this._timers = { retry: null, connect: null, disconnect: null, fps: null };
    this._frames = 0;
    this._frameToken = 0;
    this._frameAt = 0;
    // The one <video> of this tile for the view's whole life: a re-render of the card moves it into the new tile, it is
    // never recreated, so the picture does not restart. Muted and inline so a browser lets it play without a gesture.
    const v = document.createElement("video");
    v.muted = true;
    v.defaultMuted = true;
    v.setAttribute("muted", "");
    v.autoplay = true;
    v.playsInline = true;
    v.setAttribute("playsinline", "");
    v.setAttribute("aria-hidden", "true");
    v.disablePictureInPicture = true;
    v.style.opacity = "0";
    v.addEventListener("playing", () => this._onPlaying());
    this.video = v;
  }

  start() {
    if (this.state === "starting" || this.state === "playing" || this.state === "retrying") return;
    this._retry = 0;
    this._open();
  }

  stop() {
    this.state = "stopped";
    for (const k of Object.keys(this._timers)) this._clear(k);
    this._stopMeter();
    this._teardown();
    this._release();
  }

  setWantFps(on) {
    on = !!on;
    if (on === this.wantFps) return;
    this.wantFps = on;
    if (on) { if (this.state === "playing") this._startMeter(); } else { this._stopMeter(); this.fps = null; }
  }

  _clear(name) {
    if (this._timers[name]) { clearTimeout(this._timers[name]); clearInterval(this._timers[name]); this._timers[name] = null; }
  }

  _notify() { try { this._o.onState && this._o.onState(this); } catch (err) { console.error("portico-card: live state callback failed", err); } }

  _release() {
    const v = this.video;
    v.style.opacity = "0";
    try { v.pause(); } catch (err) { /* nothing playing */ }
    v.srcObject = null;
  }

  // Ends the connection of the current attempt: HA's websocket subscription, the server session (live_close, once per
  // session id) and the peer connection. Every callback of that attempt is stale from here on.
  _teardown() {
    this._attempt++;
    this._clear("connect");
    this._clear("disconnect");
    const handle = this._handle;
    const sessionId = this._sessionId;
    const pc = this._pc;
    this._handle = null;
    this._sessionId = null;
    this._pending = [];
    this._pc = null;
    if (handle) handle.unsubscribe();
    if (sessionId) this._sendClose(sessionId);
    if (pc) {
      pc.onicecandidate = null;
      pc.ontrack = null;
      pc.onconnectionstatechange = null;
      try { pc.close(); } catch (err) { /* already closed */ }
    }
  }

  _sendClose(sessionId) {
    const conn = this._o.connection();
    if (!conn) return;
    try {
      const p = conn.sendMessagePromise({ type: "portico/content/live_close", session_id: sessionId });
      if (p && typeof p.catch === "function") p.catch(() => {});
    } catch (err) { /* the socket is gone: the server closes the session with it */ }
  }

  async _open() {
    this._teardown();
    this._release();
    const attempt = this._attempt;
    this.state = "starting";
    this.fps = null;
    this._notify();
    const conn = this._o.connection();
    if (!conn || typeof RTCPeerConnection === "undefined") { this._fail(attempt, false); return; }
    let pc;
    try {
      pc = new RTCPeerConnection();
      this._pc = pc;
      pc.addTransceiver("video", { direction: "recvonly" });       // video only: there is no audio line in this offer
      pc.ontrack = (ev) => this._onTrack(attempt, ev);
      pc.onicecandidate = (ev) => this._onLocalCandidate(attempt, ev);
      pc.onconnectionstatechange = () => this._onPcState(attempt);
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      if (attempt !== this._attempt) return;
      const sdp = (pc.localDescription && pc.localDescription.sdp) || offer.sdp;
      const msg = { type: "portico/content/live", camera_id: this.cameraId, sdp, size: this.size, ...(this._o.scope || {}) };
      this._timers.connect = setTimeout(() => this._fail(attempt, true), this._o.connectMs || LIVE_CONNECT_MS);
      this.opens++;
      const handle = openLive(conn, msg, (event) => this._onEvent(attempt, event));
      this._handle = handle;
      handle.result.then((res) => this._onResult(attempt, res, handle), (err) => {
        if (attempt === this._attempt) this._fail(attempt, !(err && LIVE_FINAL_CODES.has(err.code)));
      });
    } catch (err) {
      this._fail(attempt, false);
    }
  }

  _onResult(attempt, res, handle) {
    if (attempt !== this._attempt) {
      // torn down while the result was on its way: the unsubscribe closes it, the explicit close makes sure
      if (res && res.session_id) this._sendClose(res.session_id);
      return;
    }
    this._sessionId = res.session_id || null;
    if (!this._sessionId) { this._pending = []; return; }
    const queued = this._pending;
    this._pending = [];
    for (const c of queued) this._sendCandidate(c);
  }

  _onLocalCandidate(attempt, ev) {
    if (attempt !== this._attempt) return;
    let candidate;
    if (!ev || !ev.candidate) candidate = { candidate: "" };                    // end of candidates
    else if (typeof ev.candidate.toJSON === "function") candidate = ev.candidate.toJSON();
    else candidate = { candidate: ev.candidate.candidate, sdpMid: ev.candidate.sdpMid, sdpMLineIndex: ev.candidate.sdpMLineIndex };
    if (this._sessionId) this._sendCandidate(candidate);
    else this._pending.push(candidate);
  }

  _sendCandidate(candidate) {
    const conn = this._o.connection();
    if (!conn || !this._sessionId) return;
    try {
      const p = conn.sendMessagePromise({ type: "portico/content/live_ice", session_id: this._sessionId, candidate });
      if (p && typeof p.catch === "function") p.catch(() => {});
    } catch (err) { /* nothing to do: the picture simply may not connect */ }
  }

  _onEvent(attempt, event) {
    if (attempt !== this._attempt || !event) return;
    const pc = this._pc;
    if (event.type === "answer" && pc && typeof event.sdp === "string") {
      this._chain = Promise.resolve(pc.setRemoteDescription({ type: "answer", sdp: event.sdp })).catch(() => { this._fail(attempt, true); });
    } else if (event.type === "candidate" && pc && event.candidate) {
      this._chain = this._chain.then(() => pc.addIceCandidate(event.candidate)).catch(() => { /* one bad candidate is not a failure */ });
    } else if (event.type === "error") {
      this._fail(attempt, true);
    }
  }

  _onTrack(attempt, ev) {
    if (attempt !== this._attempt || !ev) return;
    let stream = ev.streams && ev.streams[0];
    if (!stream && ev.track && typeof MediaStream !== "undefined") stream = new MediaStream([ev.track]);
    if (!stream) return;
    this.video.srcObject = stream;
    this.play();
  }

  play() {
    let p;
    try { p = this.video.play(); } catch (err) { return; }
    if (p && typeof p.catch === "function") p.catch(() => {});
  }

  _onPcState(attempt) {
    if (attempt !== this._attempt || !this._pc) return;
    const st = this._pc.connectionState;
    if (st === "failed" || st === "closed") this._fail(attempt, true);
    else if (st === "disconnected") {
      if (!this._timers.disconnect) this._timers.disconnect = setTimeout(() => this._fail(attempt, true), this._o.disconnectMs || LIVE_DISCONNECT_MS);
    } else if (st === "connected") this._clear("disconnect");
  }

  _onPlaying() {
    if (this.state !== "starting") return;
    this.state = "playing";
    this._retry = 0;
    this._clear("connect");
    this.video.style.opacity = "1";
    if (this.wantFps) this._startMeter();
    this._notify();
  }

  _fail(attempt, retriable) {
    if (attempt !== this._attempt || this.state === "stopped") return;
    this._stopMeter();
    this._teardown();
    this._release();
    const delays = this._o.retryMs || LIVE_RETRY_MS;
    if (!retriable || this._retry >= delays.length) {
      this.state = "failed";
      this._notify();
      return;
    }
    this.state = "retrying";
    this._notify();
    this._timers.retry = setTimeout(() => {
      this._timers.retry = null;
      if (this.state === "retrying") this._open();
    }, delays[this._retry++]);
  }

  // ---- the frame rate (shown only when the document asks for it) ----
  _startMeter() {
    this._stopMeter();
    const every = this._o.fpsMs || LIVE_FPS_MS;
    const v = this.video;
    const token = ++this._frameToken;
    if (typeof v.requestVideoFrameCallback === "function") {
      this._frames = 0;
      this._frameAt = performance.now();
      const onFrame = () => {
        if (token !== this._frameToken) return;
        this._frames++;
        v.requestVideoFrameCallback(onFrame);
      };
      v.requestVideoFrameCallback(onFrame);
      this._timers.fps = setInterval(() => {
        const now = performance.now();
        const seconds = Math.max((now - this._frameAt) / 1000, 0.001);
        this.fps = this._frames / seconds;
        this._frames = 0;
        this._frameAt = now;
        this._paint();
      }, every);
    } else {
      let last = null;
      this._timers.fps = setInterval(async () => {
        const pc = this._pc;
        if (!pc || typeof pc.getStats !== "function") return;
        try {
          const report = await pc.getStats();
          if (token !== this._frameToken) return;
          let fps = null;
          report.forEach((r) => {
            if (r.type === "inbound-rtp" && (r.kind === "video" || r.mediaType === "video")) {
              if (typeof r.framesPerSecond === "number") fps = r.framesPerSecond;
              else if (typeof r.framesDecoded === "number") {
                const now = performance.now();
                if (last) fps = ((r.framesDecoded - last.frames) * 1000) / Math.max(now - last.at, 1);
                last = { frames: r.framesDecoded, at: now };
              }
            }
          });
          if (fps !== null) { this.fps = fps; this._paint(); }
        } catch (err) { /* stats are a courtesy */ }
      }, every);
    }
  }

  _stopMeter() {
    this._frameToken++;
    this._clear("fps");
  }

  _paint() { try { this._o.onFps && this._o.onFps(this); } catch (err) { console.error("portico-card: live fps callback failed", err); } }
}

class PorticoCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._config = { takeover: false };
    // Did anyone use the kiosk page itself (not the ring overlay, which lives in document.body)? A page the automation opened for a
    // call, and nobody touched, is handed back when the call is over (_leaveAfterCall).
    this._touched = false;
    this._leaveArmed = false;
    this.addEventListener("pointerdown", () => { this._touched = true; }, true);
    this._state = STATE_IDLE;
    this._doors = [];
    this._activeDoorId = null;
    this._activeCallId = null;
    this._pc = null;
    this._localStream = null;
    this._remoteStream = null;
    // The caller's voice. One element for the card's life, deliberately outside every render subtree
    // (_render replaces the shadow root's innerHTML, the overlay replaces its children): a re-render
    // must never recreate it or restart playback. Never inserted in a page; a detached audio element plays.
    // Only the REMOTE stream is ever attached here, never the local mic (it would echo).
    this._remoteAudio = document.createElement("audio");
    this._remoteAudio.autoplay = true;
    this._remoteAudio.playsInline = true;
    this._audioBlocked = false;
    this._unsubscribers = [];
    this._hass = null;
    this._error = null;

    // content view
    this._content = null;
    this._contentError = null;
    this._pictures = {};
    this._inflight = new Set();
    this._lastSnapshotAt = 0;
    this._armed = null;
    this._armTimer = null;
    this._notice = null;
    this._noticeTimer = null;
    this._pollTimer = null;

    // wide layout: the host's box (ResizeObserver, or getBoundingClientRect at render)
    this._box = { w: 0, h: 0 };
    this._resizeObserver = null;

    // screen mode
    this._pageId = null;
    this._versionNotice = null;
    this._ringCallId = null;    // the call id of the last ring this card claimed (CLAIMS)
    this._wake = undefined;     // normWake() of the server's `wake`; undefined = none sent = no filtering

    // ring takeover
    this._answering = false;
    this._elsewhere = false;
    this._elsewhereTimer = null;
    this._hiddenFor = null;
    this._ringTimer = null;
    this._chimeTimer = null;
    this._audioCtx = null;
    this._soundBlocked = false;
    this._overlayHost = null;
    this._overlayRoot = null;

    // live video: key ("page:<camera>|<size>" or "ring:<camera>|big") -> {view: LiveView, surface}. Kept on the card, never in a
    // rendered subtree: _render replaces the shadow root's innerHTML, the views and their <video> elements survive it.
    this._live = new Map();
    this._liveSlots = [];
    this._liveIO = null;
    this._liveVisible = new Map();
    this._liveChoices = new Map();      // this page's copy of the viewer's taps: they still count when storage is blocked
    this._onVisibility = () => this._reconcileLive();
    this._onPageHide = () => this._closeAllLive();
    this._onPageShow = (ev) => { if (ev && ev.persisted) this._syncLive(); };

    // house calls (M7 3d): null while there is nothing to show; the host element exists only while there is
    this._house = null;
    this._houseHost = null;
    this._houseRoot = null;
    this._houseTimer = null;
    this._houseRev = null;
    this._houseChime = false;
  }

  setConfig(config) {
    const before = this._config && this._config.endpoint;
    this._config = { takeover: false, ...(config || {}) };
    if (!this._config._ghost && this._config.endpoint) wakePathSet(this._config.endpoint, this._config.path);
    if (this._hass && this._content !== null && before !== this._config.endpoint) {
      // another screen's page assignment: the old wake is not this screen's
      this._wake = undefined;
      this._loadContent();
    }
    if (this._config.fullpage) this.setAttribute("fullpage", "");
    else this.removeAttribute("fullpage");
  }

  set hass(hass) {
    const firstRun = this._hass === null;
    this._hass = hass;
    if (hass && hass.themes && hass.themes.darkMode) this.setAttribute("dark", "");
    else this.removeAttribute("dark");
    if (firstRun) {
      this._init();
    }
  }

  connectedCallback() {
    document.addEventListener("visibilitychange", this._onVisibility);
    window.addEventListener("pagehide", this._onPageHide);
    window.addEventListener("pageshow", this._onPageShow);
    this._observeBox();
    this._render();
    // A re-parent (masonry column change, edit mode) fires disconnect+connect on the same element with
    // _hass still set, so set hass never runs _init again: rebuild what disconnectedCallback tore down.
    if (this._hass && !this._unsubscribers.length && !this._initRunning) this._init();
  }

  disconnectedCallback() {
    document.removeEventListener("visibilitychange", this._onVisibility);
    window.removeEventListener("pagehide", this._onPageHide);
    window.removeEventListener("pageshow", this._onPageShow);
    this._closeAllLive();
    if (this._liveIO) { this._liveIO.disconnect(); this._liveIO = null; }
    this._liveVisible.clear();
    if (this._resizeObserver) { this._resizeObserver.disconnect(); this._resizeObserver = null; }
    this._releaseSubscriptions();
    this._teardownCall();
    this._stopPolling();
    this._stopChime();
    clearTimeout(this._ringTimer);
    clearTimeout(this._elsewhereTimer);
    clearTimeout(this._armTimer);
    clearTimeout(this._noticeTimer);
    this._removeOverlay();
    this._failsafeStop();
    releaseClaims(this);
    clearTimeout(this._houseTimer);
    this._houseTimer = null;
    this._house = null;
    this._houseChime = false;
    this._removeHouse();
  }

  _observeBox() {
    if (this._resizeObserver || typeof ResizeObserver === "undefined") return;
    this._resizeObserver = new ResizeObserver((entries) => {
      const last = entries && entries[entries.length - 1];
      if (last && last.contentRect) this._setBox(last.contentRect.width, last.contentRect.height);
    });
    this._resizeObserver.observe(this);
  }

  // Takes the host's size; redraws only when the layout it implies changed (a Lovelace card's height follows its
  // content, so only its width counts there).
  _setBox(w, h) {
    const before = this._box;
    this._box = { w: Math.round(w), h: Math.round(h) };
    const changed = this._box.w !== before.w || (!!this._config.fullpage && this._box.h !== before.h);
    if (changed && this._content && this._blocksOf(this._currentPage())) this._render();
  }

  _isWide() {
    return wideDecision(this._box.w, this._box.h, !!this._config.fullpage);
  }

  async _init() {
    this._initRunning = true;
    try {
      await this._initInner();
    } finally {
      this._initRunning = false;
    }
  }

  _releaseSubscriptions() {
    const held = this._unsubscribers;
    this._unsubscribers = [];
    for (const unsubscribe of held) {
      try { unsubscribe(); } catch (err) { console.error("portico-card: unsubscribe failed", err); }
    }
  }

  async _subscribe(msg) {
    try {
      const unsubscribe = await this._hass.connection.subscribeMessage((event) => this._handleEvent(event), msg);
      if (this.isConnected) {
        this._unsubscribers.push(unsubscribe);
      } else {
        unsubscribe(); // detached during the await: disconnectedCallback had nothing to release
      }
    } catch (err) {
      console.error(`portico-card: ${msg.type} failed`, err);
    }
  }

  async _initInner() {
    if (!(await this._probeVersion())) {
      this._render();
      return;
    }
    try {
      const result = await this._hass.connection.sendMessagePromise({ type: "portico/doors" });
      this._doors = result.doors || [];
    } catch (err) {
      console.error("portico-card: portico/doors failed", err);
      this._doors = [];
    }
    this._render();

    if (this._config._ghost) {
      // The wake listener's card: it must know its `wake` BEFORE the call snapshot arrives (a call already ringing is
      // replayed on subscribe, and an absent wake means "do not wake" here).
      await this._subscribe({ type: "portico/content/subscribe", ...this._scope() });
      if (!this.isConnected) return;
      await this._loadContent();
      if (!this.isConnected) return;
      await this._subscribe({ type: "portico/calls/subscribe" });
      if (this.isConnected) this._startPolling();
      return;
    }
    await this._subscribe({ type: "portico/calls/subscribe" });
    await this._subscribe({ type: "portico/content/subscribe", ...this._scope() });
    // connectedCallback runs _init again on re-attach
    if (!this.isConnected) return;
    await this._loadContent();
    if (this.isConnected) this._startPolling();
  }

  // ---- screen mode -----------------------------------------------------------------------

  _isScreen() {
    return !!(this._config && this._config.endpoint);
  }

  // The content request's scope: a card naming an endpoint sends it; without one the server answers with
  // the viewing user's own endpoint (an administrator gets `unauthorized`: see contentErrorText).
  _scope() {
    return this._isScreen() ? { endpoint: this._config.endpoint } : {};
  }

  // false = the server speaks a contract this page does not (or has no such command): say so, do nothing else.
  async _probeVersion() {
    this._versionNotice = null;
    try {
      const r = await this._hass.connection.sendMessagePromise({ type: "portico/version" });
      const contract = r && r.contract;
      if (SUPPORTED_CONTRACTS.includes(contract)) return true;
      this._versionNotice = VERSION_TEXT;
    } catch (err) {
      if (!err || err.code !== "unknown_command") {
        console.error("portico-card: portico/version failed", err);
        return true; // a transient failure is not a version answer: carry on
      }
      this._versionNotice = VERSION_TEXT;
    }
    return false;
  }

  _reloadPage() {
    window.location.reload();
  }

  _pagesOf() {
    const c = this._content || {};
    if (Array.isArray(c.pages) && c.pages.length) return c.pages;
    return [{ id: "home", label: "", cameras: [], actions: [] }];
  }

  // A page's cameras across its blocks. The server stopped sending the flat `cameras`/`actions` beside `blocks` in B8;
  // a page with no `blocks` is an OLD server's reply and is read as the flat form exactly as before. A reply from this
  // server never needs that branch.
  _blocksOf(page) {
    return page && Array.isArray(page.blocks) ? page.blocks : null;
  }

  _pageCameras(page) {
    const blocks = this._blocksOf(page);
    return blocks ? blocks.flatMap((b) => b.cameras || []) : (page.cameras || []);
  }

  _pageActions(page) {
    const blocks = this._blocksOf(page);
    return blocks ? blocks.flatMap((b) => b.actions || []) : (page.actions || []);
  }

  _currentPage() {
    const pages = this._pagesOf();
    return pages.find((p) => p.id === this._pageId) || pages[0];
  }

  // The `callers` entry for the door that is ringing: the door's endpoint_id (portico/doors) keys it.
  _callerFor(doorId) {
    if (!this._content || !this._content.callers) return null;
    const door = this._doors.find((d) => d.door_id === doorId);
    const key = door && door.endpoint_id;
    if (!key || !Object.prototype.hasOwnProperty.call(this._content.callers, key)) return null;
    const caller = this._content.callers[key];
    return caller && typeof caller === "object" ? caller : null;
  }

  _inCallHere() {
    return this._state === STATE_IN_CALL || this._answering || !!this._pc;
  }

  // ---- content view ----------------------------------------------------------------------

  // Every camera / action of every page (the ring's fallbacks when the ringing door has no `callers` entry).
  _allCameras() {
    return this._pagesOf().flatMap((p) => this._pageCameras(p));
  }

  _allActions() {
    return this._pagesOf().flatMap((p) => this._pageActions(p));
  }

  async _loadContent() {
    try {
      this._content = await this._hass.connection.sendMessagePromise({
        type: "portico/content/get",
        ...this._scope(),
      });
      this._contentError = null;
      this._wake = normWake(this._content && this._content.wake);
      if (!this._config._ghost && this._content && this._content.endpoint) wakePathSet(this._content.endpoint, this._config.path);
    } catch (err) {
      console.error("portico-card: portico/content/get failed", err);
      this._contentError = contentErrorText(err, this._isScreen());
    }
    this._render();
    this._pollPictures(true);
  }

  _startPolling() {
    if (this._pollTimer) return;
    this._pollTimer = setInterval(() => this._pollPictures(false), this._config._pollMs || 1000);
  }

  _stopPolling() {
    clearInterval(this._pollTimer);
    this._pollTimer = null;
  }

  _ringCameraId() {
    const cams = this._allCameras();
    const wanted = this._config.ring_camera;
    if (wanted && cams.some((c) => c.id === wanted)) return wanted;
    const first = cams.find((c) => c.available);
    return first ? first.id : null;
  }

  _overlayShowing() {
    return !!this._overlayHost;
  }

  _pollPictures(force) {
    if (!this._hass || !this._content) return;
    if (typeof document !== "undefined" && document.visibilityState === "hidden") return;
    if (this._config._ghost && !this._overlayShowing()) return;   // the wake listener's card draws no page: no pictures to keep
    const callerNow = this._overlayShowing() ? this._callerFor(this._activeDoorId) : null;
    const ringing = this._overlayShowing()
      && (this._state === STATE_RINGING || (!!callerNow && (this._state === STATE_IN_CALL || this._elsewhere)));
    const every = ringing
      ? (this._config._ringSnapshotMs || RING_SNAPSHOT_EVERY_MS)
      : (this._config._snapshotMs || SNAPSHOT_EVERY_MS);
    const now = Date.now();
    if (!force && now - this._lastSnapshotAt < every) return;
    this._lastSnapshotAt = now;
    let ids;
    let widths = null;
    if (ringing && callerNow) {
      ids = (callerNow.cameras || []).filter((c) => c.available && !this._liveShowing(c.id)).map((c) => c.id);
    } else if (ringing) {
      const id = this._ringCameraId();
      ids = id ? [id] : [];
    } else {
      const cams = this._pageCameras(this._currentPage()).filter((c) => c.available);
      ids = cams.filter((c) => !this._liveShowing(c.id)).map((c) => c.id);
      const pageBlocks = this._blocksOf(this._currentPage());
      if (pageBlocks) {
        widths = {};
        if (this._isWide()) {
          // wide: each block's FIRST camera is the large one, whatever size the server gave it
          for (const b of pageBlocks) {
            const first = (b.cameras || [])[0];
            if (first && first.available) widths[first.id] = BIG_STILL_WIDTH;
          }
        } else {
          for (const c of cams) if (c.size === "big") widths[c.id] = BIG_STILL_WIDTH;
        }
      }
    }
    for (const id of ids) this._snapshot(id, widths ? widths[id] : undefined);
  }

  async _snapshot(cameraId, width) {
    if (this._inflight.has(cameraId)) return;
    this._inflight.add(cameraId);
    try {
      const r = await this._hass.connection.sendMessagePromise({
        type: "portico/content/snapshot",
        camera_id: cameraId,
        ...(width ? { width } : {}),
        ...this._scope(),
      });
      this._pictures[cameraId] = `data:${r.content_type};base64,${r.content}`;
    } catch (err) {
      // rate_limited: the picture we hold is still the newest; anything else, it must not look live.
      if (!err || err.code !== "rate_limited") delete this._pictures[cameraId];
    } finally {
      this._inflight.delete(cameraId);
    }
    this._applyPictures(this.shadowRoot);
    if (this._overlayRoot) this._applyPictures(this._overlayRoot);
  }

  _applyPictures(root) {
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll("img[data-camera]").forEach((img) => {
      const src = this._pictures[img.dataset.camera];
      const holder = img.parentElement;
      if (src) {
        if (img.getAttribute("src") !== src) img.setAttribute("src", src);
        img.style.display = "";
        if (holder) holder.classList.remove("nopic");
      } else {
        img.removeAttribute("src");
        img.style.display = "none";
        if (holder) holder.classList.add("nopic");
      }
    });
  }

  _contentView() {
    const wrap = el("div", "content");
    if (this._versionNotice) {
      wrap.append(el("div", "error", this._versionNotice));
      const b = el("button", "act", "Reload");
      b.id = "reload-btn";
      b.addEventListener("click", () => this._reloadPage());
      wrap.append(b);
      return wrap;
    }
    if (this._contentError) {
      wrap.append(el("div", "error", this._contentError));
      return wrap;
    }
    if (!this._content) {
      wrap.append(el("div", "muted", "Loading…"));
      return wrap;
    }
    const screen = this._isScreen();
    const blocks = this._blocksOf(this._currentPage());
    const { cameras = [], actions = [] } = blocks ? {} : this._currentPage();
    const pages = this._pagesOf();
    if (pages.length > 1) {
      const current = this._currentPage();
      const tabs = el("div", "tabs");
      for (const p of pages) {
        const t = el("button", "tab" + (p === current ? " on" : ""), p.label || p.id);
        t.dataset.page = p.id;
        t.addEventListener("click", () => {
          this._pageId = p.id;
          this._render();
          this._pollPictures(true);
        });
        tabs.append(t);
      }
      wrap.append(tabs);
    }
    if (blocks) {
      let drawn = 0;
      const wide = this._isWide();
      blocks.forEach((b, i) => {
        drawn += wide ? this._drawWideBlock(wrap, b, i, blocks.length) : this._drawBlock(wrap, b);
      });
      const everyone = this._everyoneRow();
      if (everyone) wrap.append(everyone);
      if (!drawn) {
        wrap.append(el("div", "muted", screen ? "Nothing is set up for this screen yet (Portico > Screens)."
                                              : "Nothing chosen for this phone yet (open Portico in the sidebar)."));
      }
      return wrap;
    }
    if (cameras.length) {
      const grid = el("div", "cams");
      for (const c of cameras) grid.append(this._cameraTile(c));
      wrap.append(grid);
    }
    if (actions.length) {
      const grid = el("div", "acts");
      for (const a of actions) {
        const armed = this._armed === a.id;
        const b = el("button", "act" + (armed ? " armed" : ""), armed ? `Tap again: ${a.label}` : a.label);
        b.disabled = !a.available;
        b.dataset.action = a.id;
        b.addEventListener("click", () => this._pressAction(a));
        grid.append(b);
      }
      wrap.append(grid);
    }
    if (!cameras.length && !actions.length) {
      wrap.append(el("div", "muted", screen ? "Nothing is set up for this screen yet (Portico > Screens)."
                                          : "Nothing chosen for this phone yet (open Portico in the sidebar)."));
    }
    return wrap;
  }

  _cameraTile(c, size) {
    const tile = el("div", "cam" + (size ? ` ${size}` : "") + (c.available ? "" : " off"));
    const frame = el("div", "pic nopic");
    const img = el("img");
    img.dataset.camera = c.id;
    img.alt = c.label || "Camera"; // an empty name is the owner's choice: no caption, but the picture keeps an accessible name
    frame.append(img, el("span", "none", c.available ? "No picture" : "Camera unavailable"));
    tile.append(frame);
    if (size && c.available && this._liveSupported()) {
      // a block tile: it can play live (the document's setting, or this viewer's last tap) and a tap switches it
      const mode = this._liveMode(c);
      this._markLiveSlot(frame, "page", c, liveSizeOf(size), mode);
      tile.classList.add("tap");
      tile.setAttribute("role", "button");
      tile.setAttribute("aria-pressed", mode ? "true" : "false");
      tile.tabIndex = 0;
      tile.addEventListener("click", () => this._toggleLive(c, liveSizeOf(size)));
      tile.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); this._toggleLive(c, liveSizeOf(size)); }
      });
    }
    if (c.label) tile.append(el("div", "label", c.label));
    return tile;
  }

  // One block: its title, its cameras (the server's effective size: big = a row of its own, small = half a row; no
  // toggle and no zoom here) and its buttons (talk is never drawn on the card, design D7). Returns how many
  // cameras and buttons it drew.
  _drawBlock(wrap, block) {
    const cameras = block.cameras || [];
    const actions = (block.actions || []).filter((a) => a.kind !== "talk");
    const sec = el("section", "block");
    sec.dataset.block = block.id;
    if (block.title) sec.append(el("h3", "block-title", block.title));
    if (cameras.length) {
      const grid = el("div", "cams blk");
      for (const c of cameras) grid.append(this._cameraTile(c, c.size === "small" ? "small" : "big"));
      sec.append(grid);
    }
    if (actions.length) sec.append(this._blockButtons(actions));
    if (block.title || cameras.length || actions.length) wrap.append(sec);
    return cameras.length + actions.length;
  }

  // A block's buttons: icon, label per show_label, palette colour, ask-first text. Shared by the narrow and the wide
  // drawing so the two cannot drift.
  _blockButtons(actions) {
    const grid = el("div", "acts blk");
    for (const a of actions) {
      const armed = this._armed === a.id;
      const iconOnly = a.show_label === false && !armed; // an armed button always says what the second tap does
      const named = Object.prototype.hasOwnProperty.call(PALETTE, a.color);
      const tone = named ? ` c-${a.available ? a.color : "grey"}` : ""; // disabled is grey whatever the colour
      const b = el("button", "act" + tone + (armed ? " armed" : ""));
      const icon = document.createElement("ha-icon");
      icon.setAttribute("icon", iconNameOf(a));
      b.append(icon);
      if (iconOnly) b.setAttribute("aria-label", a.label);
      else b.append(el("span", "t", armed ? `Tap again: ${a.label}` : a.label));
      b.title = a.label;
      b.disabled = !a.available;
      b.dataset.action = a.id;
      b.addEventListener("click", () => this._pressAction(a));
      grid.append(b);
    }
    return grid;
  }

  // The wide drawing of one block (container >= 720px). The block's FIRST camera is the main one whatever `size` the
  // server gave it; every other camera goes to the right column. In fullpage mode the first block gets the full
  // layout sized to the screen; later blocks are drawn below it with large tiles, and the page scrolls.
  _drawWideBlock(wrap, block, index, total) {
    const cameras = block.cameras || [];
    const actions = (block.actions || []).filter((a) => a.kind !== "talk");
    const fullpage = !!this._config.fullpage;
    const sec = el("section", "block wide" + (fullpage && index > 0 ? " more" : ""));
    sec.dataset.block = block.id;
    if (fullpage && index > 0) {
      // a later block: full tiles in rows of three below the first screen (the page scrolls)
      const gw = this._wideGeo ? this._wideGeo.rowW : this._box.w - W_RAIL - W_PAD;
      const m = wideMoreGeometry(gw);
      if (this._wideGeo) this._setWideVars(sec, this._wideGeo);
      sec.style.setProperty("--w-tw", `${m.tw}px`);
      sec.style.setProperty("--w-th", `${m.th}px`);
      if (block.title) sec.append(el("h3", "block-title", block.title));
      if (cameras.length) {
        const grid = el("div", "more-cams");
        for (const c of cameras) grid.append(this._cameraTile(c, "small"));
        sec.append(grid);
      }
      if (actions.length) sec.append(this._blockButtons(actions));
      if (block.title || cameras.length || actions.length) wrap.append(sec);
      return cameras.length + actions.length;
    }
    const g = wideGeometry({ w: this._box.w, h: this._box.h, fullpage, nMain: cameras.length ? 1 : 0,
                             nSide: Math.max(0, cameras.length - 1), nButtons: actions.length,
                             hasTitle: !!block.title, extras: fullpage ? total - 1 : 0, house: this._houseEntry() ? 1 : 0 });
    this._wideGeo = g;
    this._setWideVars(sec, g);
    if (block.title) sec.append(el("h3", "block-title", block.title));
    if (cameras.length) {
      const grid = el("div", "wide-grid" + (cameras.length > 1 ? "" : " solo"));
      const main = el("div", "wide-main");
      main.append(this._cameraTile(cameras[0], "big"));
      grid.append(main);
      if (cameras.length > 1) {
        const side = el("div", "wide-side");
        for (const c of cameras.slice(1)) side.append(this._cameraTile(c, "small"));
        grid.append(side);
      }
      sec.append(grid);
    }
    if (actions.length) sec.append(this._blockButtons(actions));
    if (block.title || cameras.length || actions.length) wrap.append(sec);
    return cameras.length + actions.length;
  }

  _setWideVars(sec, g) {
    const px = (n) => `${Math.floor(n * 100) / 100}px`;
    sec.style.setProperty("--w-mw", px(g.mw));
    sec.style.setProperty("--w-sw", px(g.sw));
    sec.style.setProperty("--w-sh", px(g.sh));
    sec.style.setProperty("--w-sideh", px(g.sideTotal));
    if (!this._config.fullpage) return; // a Lovelace card's page stays byte-identical
    sec.style.setProperty("--w-gw", px(g.rowW));
    sec.style.setProperty("--w-bh", px(g.btnRowH));
    sec.style.setProperty("--w-cols", String(Math.max(1, g.btnCols)));
  }

  async _pressAction(action) {
    if (!action.available) return;
    if (action.confirm && this._armed !== action.id) {
      this._armed = action.id;
      clearTimeout(this._armTimer);
      this._armTimer = setTimeout(() => {
        this._armed = null;
        this._render();
      }, this._config._armMs || ARM_MS);
      this._render();
      return;
    }
    this._armed = null;
    clearTimeout(this._armTimer);
    try {
      await this._hass.connection.sendMessagePromise({
        type: "portico/content/run",
        action_id: action.id,
        ...this._scope(),
      });
      this._say(`“${action.label}”: done.`);
    } catch (err) {
      console.error("portico-card: portico/content/run failed", err);
      this._say(actionResultText(action.label, err));
    }
  }

  _say(text) {
    this._notice = text;
    clearTimeout(this._noticeTimer);
    this._noticeTimer = setTimeout(() => {
      this._notice = null;
      this._render();
    }, this._config._noticeMs || NOTICE_MS);
    this._render();
  }

  // ---- failsafe for a call this card did not answer ------------------------------------------

  // Let go of the call this card holds: the shared tail of every way the overlay closes (an `ended` event, the idle poll, the cap).
  _letGoOfCall() {
    this._teardownCall();
    this._endRing();
    this._state = STATE_IDLE;
    this._activeDoorId = null;
    this._activeCallId = null;
    // the call is over: an overlay kept open for a call someone else took goes with it
    this._elsewhere = false;
    clearTimeout(this._elsewhereTimer);
    this._failsafeStop();
    this._leaveAfterCall();
  }

  // The HA automation opens /portico/kiosk/<slug> on the tablet when the gate rings, and nothing else ever takes the tablet back: with
  // the overlay gone, the page itself (live cameras and all) stayed on the wall after the call (2026-11-12). The mounted kiosk card
  // that was brought up for a call, and that nobody used, leaves when the call is over (the back rail's way out). A page somebody touched
  // is theirs and stays. The listener's hidden card never navigates.
  _leaveAfterCall() {
    const armed = this._leaveArmed;
    this._leaveArmed = false;
    if (!armed || this._touched || !this._config.fullpage || this._config._ghost) return;
    this._logLeave();
    try { this._goBack(); } catch (err) { console.error("portico-card: leaving the kiosk after the call failed", err); }
  }

  _logLeave() {
    try { console.info("portico-card: the call is over and the kiosk page was opened for it; leaving the page"); } catch (err) { /* no console */ }
  }

  // Which path closed the overlay and the call ids seen, on the browser console (rate limited): provable from the tablet.
  _logClose(path, ids) {
    const now = Date.now();
    if (this._lastCloseLog && now - this._lastCloseLog < (this._config._failsafeLogMs ?? FAILSAFE_LOG_EVERY_MS)) return;
    this._lastCloseLog = now;
    try { console.info(`portico-card: overlay closed by ${path}; door ${this._activeDoorId || "-"}; call ids: ${ids}; held ${this._activeCallId || "-"}`); } catch (err) { /* no console */ }
  }

  // Does the overlay hold a call this card did not answer (a ring, or one someone else took)?
  _failsafeWanted() {
    return !!this._config.takeover && !!this._overlayHost && !!this._activeDoorId && !this._answering
      && (this._elsewhere || this._state === STATE_RINGING);
  }

  _failsafeSync() {
    if (!this._failsafeWanted()) { this._failsafeStop(); return; }
    if (this._fs) return;
    const now = Date.now();
    this._fs = { since: now, lastOk: now, idle: 0, busy: false, timer: null, ids: new Set() };
    if (this._activeCallId) this._fs.ids.add(this._activeCallId);
    this._fs.timer = setInterval(() => this._failsafeTick(), this._config._failsafePollMs || FAILSAFE_POLL_MS);
  }

  _failsafeStop() {
    if (this._fs) clearInterval(this._fs.timer);
    this._fs = null;
  }

  // A new ring while the overlay is open is a new call: its clock and idle count start again.
  _failsafeNewCall() {
    if (!this._fs) return;
    this._fs.since = Date.now();
    this._fs.idle = 0;
    if (this._activeCallId) this._fs.ids.add(this._activeCallId);
  }

  async _failsafeTick() {
    const fs = this._fs;
    if (!fs || fs.busy) return;
    if (!this._failsafeWanted()) { this._failsafeStop(); return; }
    fs.busy = true;
    const doorId = this._activeDoorId;
    const callAtStart = this._activeCallId;
    let snap = null;
    try { snap = await this._probeDoor(doorId); } catch (err) { snap = null; }
    fs.busy = false;
    if (this._fs !== fs) return;                               // closed or restarted while the answer was on its way
    if (!this._failsafeWanted() || this._activeDoorId !== doorId) return;
    const now = Date.now();
    if (snap) {
      fs.lastOk = now;
      if (snap.call_id) fs.ids.add(snap.call_id);
      const live = snap.state === "ringing" || snap.state === "answered" || snap.state === "active";
      if (live || this._activeCallId !== callAtStart) {
        fs.idle = 0;
      } else if (++fs.idle >= (this._config._failsafeIdlePolls || FAILSAFE_IDLE_POLLS)) {
        this._logClose("idle poll", [...fs.ids].join(","));
        this._letGoOfCall();
        this._render();
        return;
      }
    } else if (now - fs.lastOk >= (this._config._failsafeStaleMs ?? FAILSAFE_STALE_MS)
        && now - fs.since >= (this._config._failsafeCapMs ?? FAILSAFE_CAP_MS)) {
      // the server cannot be asked: the longest a ring or a call this card is only watching may hold the screen
      this._logClose("cap", [...fs.ids].join(","));
      this._letGoOfCall();
      this._render();
    }
  }

  // The door's call state as the server holds it now: the snapshot a fresh `portico/calls/subscribe` opens with, read and let
  // go. null when the server did not answer in time (never read as "no call").
  _probeDoor(doorId) {
    return new Promise((resolve) => {
      let unsubscribe = null;
      let done = false;
      const finish = (value) => {
        if (done) return;
        done = true;
        clearTimeout(timer);
        if (unsubscribe) { try { unsubscribe(); } catch (err) { /* already gone */ } }
        resolve(value);
      };
      const timer = setTimeout(() => finish(null), this._config._failsafeProbeMs || FAILSAFE_PROBE_TIMEOUT_MS);
      let pending;
      try {
        const onEvent = (event) => {
          if (event && event.type === "call_state_changed" && event.snapshot && event.door_id === doorId) finish(event);
        };
        onEvent.porticoProbe = true;                // a marker for the test harness only
        pending = this._hass.connection.subscribeMessage(onEvent, { type: "portico/calls/subscribe" });
      } catch (err) { finish(null); return; }
      Promise.resolve(pending).then((un) => {
        if (done) { try { un(); } catch (err) { /* already gone */ } } else unsubscribe = un;
      }, () => finish(null));
    });
  }

  // ---- events ------------------------------------------------------------------------------

  _handleEvent(event) {
    if (event.type === "content_changed") {
      // ours (or an install-wide change: endpoint null); a revision we already hold needs no refetch
      const ownId = this._config.endpoint || (this._content && this._content.endpoint);
      const mine = event.endpoint === undefined || event.endpoint === null || !ownId || event.endpoint === ownId;
      const same = event.rev && this._content && event.rev === this._content.rev;
      // the event carries the screen's wake too: take it at once (a refetch follows when the revision is new)
      if (mine && event.endpoint) this._wake = normWake(event.wake);
      if (mine && !same) this._loadContent();
      return;
    }
    if (event.type === "house_call") {
      this._onHouseEvent(event);
      return;
    }
    if (event.type === "house_roster_changed") {
      // who can be rung changed: re-read the roster (the Everyone count) when the revision is new
      if (this._config.fullpage && event.rev !== this._houseRev) {
        this._houseRev = event.rev;
        this._loadContent();
      }
      return;
    }
    if (event.type !== "call_state_changed") return;
    const door = this._doors.find((d) => d.door_id === event.door_id);
    if (!door) return; // an event for a door this card doesn't know about: ignore, don't guess
    const takeover = !!this._config.takeover;

    if (event.state === "ended" || event.state === "idle" || event.state === "unavailable") {
      // Contract 3: a call ends with `ended` (carrying its call id; there is no idle echo after it). An `idle`
      // snapshot (a resubscribe) or an `unavailable` one (the door left) ends whatever this card held for the door.
      door.available = event.state !== "unavailable";
      const ours = event.door_id === this._activeDoorId;
      const sameCall = event.state !== "ended" || !event.call_id || !this._activeCallId
        || event.call_id === this._activeCallId;
      if (ours && sameCall) {
        this._logClose("ended event", `${event.state} ${event.call_id || "-"}`);
        this._letGoOfCall();
      } else if (ours) {
        this._logClose("ended event ignored (other call id)", `event ${event.call_id} held ${this._activeCallId}`);
      }
      this._render();
      return;
    }
    door.available = true;

    if (event.state === "ringing" && takeover && !this._ringAllowed(door)) {
      // D3: this screen contains no Talk element for that door: the ring does not wake it
      return;
    }
    if (event.state === "ringing" && this._inCallHere()) {
      // R7-19: a ring while this card is in a call (or answering one) changes nothing: the live call keeps its
      // PeerConnection and microphone, the live call's own `ended` ends it. Say so, in one line.
      if (event.call_id !== this._activeCallId) {
        const who = (this._callerFor(event.door_id) || {}).name || door.name || "Another door";
        this._say(`${who} is ringing, but you are already on a call.`);
      }
      return;
    }
    if (event.state === "ringing") {
      const fresh = event.call_id !== this._activeCallId;
      if (fresh) {
        // a still from the last visitor must not pass for this one
        const caller = this._callerFor(event.door_id);
        for (const c of (caller && caller.cameras) || []) delete this._pictures[c.id];
      }
      this._activeDoorId = event.door_id;
      this._activeCallId = event.call_id;
      this._state = STATE_RINGING;
      this._failsafeNewCall();
      this._error = null;
      this._answering = false;
      this._elsewhere = false;
      if (takeover) {
        if (fresh) {
          this._hiddenFor = null;
          this._touched = false;
        }
        this._leaveArmed = true;
        this._startRing();
      } else {
        this._playRingtone();
      }
    } else if (event.state === "answered" || event.state === "active") {
      if (event.snapshot && takeover && !this._activeDoorId && !this._answering && this._ringAllowed(door)
          && typeof event.call_id === "string" && event.call_id) {
        // The page opened (the HA automation loads it at the ring) after someone else took the call: the snapshot is the only
        // word this card gets. Hold the call as "answered elsewhere", so the overlay shows and the `ended` (or the idle poll) closes it.
        this._activeDoorId = event.door_id;
        this._activeCallId = event.call_id;
        this._ringCallId = event.call_id;
        this._leaveArmed = true;
        this._state = STATE_IDLE;
        this._elsewhere = true;
        clearTimeout(this._elsewhereTimer);
      } else if (event.door_id === this._activeDoorId) {
        // The server's id for the live call is the one its `ended` will carry: follow it (a card that kept the ring's id
        // would ignore that `ended` and never let go of the screen).
        if (typeof event.call_id === "string" && event.call_id && event.call_id !== this._activeCallId) {
          if (this._hiddenFor === this._activeCallId) this._hiddenFor = event.call_id;
          this._activeCallId = event.call_id;
        }
        if (takeover && this._state === STATE_RINGING && !this._answering) {
          // Somebody else took it. The overlay stays for the whole call (the cameras are what the screen is for) and goes
          // when the call ends (the `ended` branch above): no timer here.
          this._endRing();
          this._state = STATE_IDLE;
          this._elsewhere = true;
          clearTimeout(this._elsewhereTimer);
        } else if (!this._elsewhere) {
          // (a later `active` for a call already marked as someone else's must not turn it into one of ours)
          this._state = STATE_IN_CALL;
        }
      }
    }
    this._render();
  }

  // ---- live video of a tile ---------------------------------------------------------------------------------------

  _liveSupported() {
    return typeof RTCPeerConnection !== "undefined";
  }

  // This viewer's choice for a camera if there is one (a tap), else the document's `live`. The choice is kept per browser
  // (localStorage), per screen: it is only stored while it differs from the document, so a changed document still reaches
  // a viewer who has not chosen.
  _liveStoreKey(cameraId) {
    return `${this._config.endpoint || (this._content && this._content.endpoint) || ""}:${cameraId}`;
  }

  _liveMode(camera) {
    const k = this._liveStoreKey(camera.id);
    const own = this._liveChoices.has(k) ? this._liveChoices.get(k) : liveStoreGet(k);
    return own === null || own === undefined ? camera.live === true : own;
  }

  _toggleLive(camera, size) {
    const entry = this._live.get(`page:${camera.id}|${size}`);
    const mode = this._liveMode(camera);
    if (mode && entry && entry.view.state === "failed") {
      entry.view.start();            // it should be live but gave up: a tap asks again instead of switching it off
      return;
    }
    const next = !mode;
    const k = this._liveStoreKey(camera.id);
    const choice = next === (camera.live === true) ? null : next;
    this._liveChoices.set(k, choice);       // null: follow the document again
    liveStoreSet(k, choice);
    this._render();
  }

  // Marks the picture frame of a tile as a place a live video can play. The words of the attributes are read back by
  // _syncLive after every render, so the views never depend on the DOM they were first drawn in.
  _markLiveSlot(frame, surface, camera, size, mode) {
    frame.dataset.liveKey = `${surface}:${camera.id}|${size}`;
    frame.dataset.liveCamera = camera.id;
    frame.dataset.liveSize = size;
    frame.dataset.liveSurface = surface;
    frame.dataset.liveMode = mode ? "1" : "0";
    if (mode && this._content && this._content.show_fps === true) frame.append(el("span", "fps", "--"));
  }

  _liveShowing(cameraId) {
    for (const { view } of this._live.values()) if (view.cameraId === cameraId && view.state === "playing") return true;
    return false;
  }

  // After every render: find the tiles that can play live, watch them for visibility, and reconcile the sessions with them.
  _syncLive() {
    if (!this.shadowRoot) return;
    const slots = [];
    for (const root of [this.shadowRoot, this._overlayRoot]) {
      if (!root || !root.querySelectorAll) continue;
      root.querySelectorAll("[data-live-key]").forEach((node) => {
        slots.push({ node, key: node.dataset.liveKey, cameraId: node.dataset.liveCamera, size: node.dataset.liveSize,
                     surface: node.dataset.liveSurface, mode: node.dataset.liveMode === "1" });
      });
    }
    this._liveSlots = slots;
    const keys = new Set(slots.map((slot) => slot.key));
    for (const key of Array.from(this._liveVisible.keys())) if (!keys.has(key)) this._liveVisible.delete(key);
    if (typeof IntersectionObserver !== "undefined") {
      if (!this._liveIO) {
        this._liveIO = new IntersectionObserver((entries) => {
          let changed = false;
          for (const e of entries) {
            const key = e.target && e.target.dataset && e.target.dataset.liveKey;
            if (key && this._liveVisible.get(key) !== e.isIntersecting) { this._liveVisible.set(key, e.isIntersecting); changed = true; }
          }
          if (changed) this._reconcileLive();
        }, { threshold: 0 });
      }
      this._liveIO.disconnect();       // the nodes are new after a render: watch these
      for (const slot of slots) this._liveIO.observe(slot.node);
    }
    this._reconcileLive();
  }

  _liveWanted(slot) {
    if (!this.isConnected || !this._hass || !this._content || !slot.mode) return false;
    if (typeof document !== "undefined" && document.visibilityState === "hidden") return false;
    if (slot.surface === "page" && this._overlayHost) return false;         // the ring covers the page: nothing to stream behind it
    if (this._liveIO && this._liveVisible.get(slot.key) !== true) return false;     // completely off screen (or not yet seen): no stream
    return true;
  }

  _reconcileLive() {
    const present = new Set();
    const wantFps = !!(this._content && this._content.show_fps === true);
    for (const slot of this._liveSlots) {
      present.add(slot.key);
      let entry = this._live.get(slot.key);
      if (this._liveWanted(slot)) {
        if (!entry) {
          const o = this._config;
          const view = new LiveView({
            key: slot.key, cameraId: slot.cameraId, size: slot.size, scope: this._scope(), wantFps,
            connection: () => (this._hass ? this._hass.connection : null),
            retryMs: o._liveRetryMs, connectMs: o._liveConnectMs, disconnectMs: o._liveDisconnectMs, fpsMs: o._liveFpsMs,
            onState: () => this._paintLive(),
            onFps: () => this._paintLive(),
          });
          entry = { view, surface: slot.surface };
          this._live.set(slot.key, entry);
          view.start();
        }
        entry.view.setWantFps(wantFps);
        const video = entry.view.video;
        if (video.parentNode !== slot.node) {
          slot.node.appendChild(video);              // the same element in the new tile: no new session, no renegotiation
          if (entry.view.state === "playing" && video.paused) entry.view.play();
        }
      } else if (entry) {
        this._closeLive(slot.key);
      }
    }
    for (const key of Array.from(this._live.keys())) if (!present.has(key)) this._closeLive(key);
    this._paintLive();
  }

  // The frame-rate label of every live tile: '--' until the first measurement.
  _paintLive() {
    for (const slot of this._liveSlots) {
      const label = slot.node.querySelector && slot.node.querySelector(".fps");
      if (!label) continue;
      const entry = this._live.get(slot.key);
      const fps = entry ? entry.view.fps : null;
      label.textContent = fps === null || fps === undefined || !Number.isFinite(fps) ? "--" : `${Math.round(fps)} fps`;
    }
  }

  _closeLive(key) {
    const entry = this._live.get(key);
    if (!entry) return;
    this._live.delete(key);
    entry.view.stop();
    if (entry.view.video.parentNode) entry.view.video.parentNode.removeChild(entry.view.video);
  }

  _closeAllLive() {
    for (const key of Array.from(this._live.keys())) this._closeLive(key);
  }

  // ---- house calls (M7 3d) ---------------------------------------------------------------------------------------

  _houseEntry() {
    return houseEntry(this._content, !!this._config.fullpage);
  }

  _houseOwn() {
    return this._config.endpoint || (this._content && this._content.endpoint) || null;
  }

  _houseName(endpointId) {
    const r = this._content && Array.isArray(this._content.roster)
      ? this._content.roster.find((e) => e && e.id === endpointId) : null;
    return r && typeof r.name === "string" ? r.name : "";
  }

  // The Everyone key, or null when the server sent none (house calls off, or this screen may not): then nothing is drawn.
  _everyoneRow() {
    const entry = this._houseEntry();
    if (!entry) return null;
    const words = houseKeyText(entry, houseLive(this._house));
    const row = el("div", "acts blk house-row");
    const b = el("button", "act" + ` c-${words.disabled ? "grey" : "green"}`);
    const icon = document.createElement("ha-icon");
    icon.setAttribute("icon", "mdi:phone-in-talk");
    b.append(icon, el("span", "t", words.label));
    if (words.sub) b.append(el("span", "sub", words.sub));
    b.title = words.sub ? `${words.label}: ${words.sub}` : words.label;
    b.id = "everyone-btn";
    b.disabled = words.disabled;
    b.dataset.action = entry.action_id;
    b.addEventListener("click", () => this._pressEveryone());
    row.append(b);
    return row;
  }

  // No confirm step (CB-9): one press places the call. The overlay shows at once ("Calling everyone"), the server's events
  // carry it on, a refusal replaces it with the words for its code.
  async _pressEveryone() {
    const entry = this._houseEntry();
    if (!entry || !entry.available || houseLive(this._house)) return;
    this._setHouse({ callId: null, dir: "out", state: "placing", peer: null, group: true,
                     count: Number.isInteger(entry.count) ? entry.count : 0, reason: null });
    try {
      const r = await this._hass.connection.sendMessagePromise({
        type: "portico/content/run", action_id: entry.action_id, ...this._scope() });
      const cur = this._house;
      if (cur && cur.state === "placing" && !cur.callId && r && typeof r.call_id === "string") {
        this._setHouse({ ...cur, callId: r.call_id });
      }
    } catch (err) {
      console.error("portico-card: placing Everyone failed", err);
      this._setHouse({ callId: null, dir: "out", state: "ended", peer: null, group: true, count: 0,
                       reason: "refused", text: houseRefusalText(err && err.code) });
    }
  }

  _onHouseEvent(event) {
    if (!this._config.fullpage && !this._config._ghost) return;
    const own = this._houseOwn();
    if (!own) return;
    // D3: a house call to this screen always wakes it (screen_call); a wake that says otherwise silences an incoming one
    if (this._wake && !this._wake.screen_call && event.to === own && event.from !== own) return;
    // the listener's card: an incoming call only, and only with a wake that says screen_call (no wake = not for this page)
    if (this._config._ghost && (!this._wake || !this._wake.screen_call || event.to !== own || event.from === own)) return;
    const next = houseReduce(this._house, event, own);
    if (next !== this._house) this._setHouse(next);
  }

  // The one place the state changes: chime, timers, redraw.
  _setHouse(next) {
    const prev = this._house;
    this._house = next;
    const ringingIn = !!next && next.dir === "in" && next.state === "ringing"
      && (!next.callId || claimCall(`house:${next.callId}`, this));
    if (ringingIn && !this._houseChime) {
      this._houseChime = true;
      this._wakeNavigate();
      this._startChime();
    } else if (!ringingIn && this._houseChime) {
      this._houseChime = false;
      this._stopChime();
    }
    clearTimeout(this._houseTimer);
    this._houseTimer = null;
    if (next && next.state === "ended") {
      this._houseTimer = setTimeout(() => this._setHouse(null), this._config._houseEndMs || HOUSE_END_MS);
    } else if (next && (next.state === "ringing" || next.state === "answered" || next.state === "placing")) {
      const id = next.callId;
      this._houseTimer = setTimeout(() => {
        if (this._house && this._house.callId === id && houseLive(this._house)) this._setHouse(null);
      }, this._config._houseStaleMs || HOUSE_STALE_MS);
    }
    if (prev !== next) this._render();
  }

  // The caller's Cancel, or Hang up in a call: tell the server, let go of the screen at once (the server's own end event for
  // a call we no longer show is ignored).
  async _houseHangup() {
    const cur = this._house;
    if (!cur || !cur.callId) return;
    const callId = cur.callId;
    this._setHouse(null);
    try {
      await this._hass.connection.sendMessagePromise({ type: "portico/house/hangup", call_id: callId });
    } catch (err) {
      console.error("portico-card: portico/house/hangup failed", err);
    }
  }

  async _houseDecline() {
    const cur = this._house;
    if (!cur || !cur.callId) return;
    const callId = cur.callId;
    this._setHouse(null);
    try {
      await this._hass.connection.sendMessagePromise({ type: "portico/house/decline", call_id: callId });
    } catch (err) {
      console.error("portico-card: portico/house/decline failed", err);
    }
  }

  // Answer a call from a phone. No audio yet (3b): the server moves the call to `answered` and the events show the rest.
  async _houseAnswer() {
    const cur = this._house;
    if (!cur || !cur.callId || cur.dir !== "in" || cur.state !== "ringing") return;
    const callId = cur.callId;
    try {
      await this._hass.connection.sendMessagePromise({ type: "portico/house/answer", call_id: callId, client_kind: "browser" });
      const now = this._house;
      if (now && now.callId === callId && now.state === "ringing") this._setHouse({ ...now, state: "answered" });
    } catch (err) {
      console.error("portico-card: portico/house/answer failed", err);
      const now = this._house;
      if (now && now.callId === callId) {
        this._setHouse(null);
        this._say(err && err.code === "already_answered" ? "Someone else already answered this call" : CALL_ENDED_TEXT);
      }
    }
  }

  // The door outranks the house: while a door rings or is in a call (this card's overlay or state) the house overlay is hidden.
  _houseWanted() {
    if ((!this._config.fullpage && !this._config._ghost) || !this._house) return false;
    if (this._config._ghost && this._house.dir !== "in") return false;
    if (this._house.callId && !claimCall(`house:${this._house.callId}`, this)) return false;
    if (this._overlayWanted()) return false;
    return this._state === STATE_IDLE;
  }

  _removeHouse() {
    if (this._houseHost && this._houseHost.parentNode) this._houseHost.parentNode.removeChild(this._houseHost);
    this._houseHost = null;
    this._houseRoot = null;
  }

  _houseWords() {
    const st = this._house;
    const name = this._houseName(st.peer);
    if (st.state === "ended") return { title: houseOutcomeText(st, name), status: "", buttons: ["close"] };
    if (st.dir === "in") {
      const who = name || "A phone";
      if (st.state === "ringing") return { title: `${who} is calling`, status: "", buttons: ["answer", "decline"] };
      if (st.state === "answered") return { title: `${who} is calling`, status: "Connecting…", buttons: ["hangup"] };
      return { title: `In a call with ${who}`, status: "", buttons: ["hangup"] };
    }
    if (st.state === "answered") return { title: `${name || "Someone"} answered`, status: "Connecting…", buttons: ["hangup"] };
    if (st.state === "active") return { title: `In a call with ${name || "someone"}`, status: "", buttons: ["hangup"] };
    const calling = st.group ? "Calling everyone…" : `Calling ${name || "…"}`;
    const status = st.state === "ringing" && st.group && st.count > 0 ? `Ringing ${phonesText(st.count)}` : "";
    return { title: calling, status, buttons: [st.callId ? "cancel" : "cancel-wait"] };
  }

  _renderHouse() {
    if (!this._houseWanted()) {
      this._removeHouse();
      return;
    }
    if (!this._houseHost) {
      const host = document.createElement("div");
      host.setAttribute("data-portico-house", "");
      host.style.cssText = "position:fixed;top:0;left:0;right:0;bottom:0;z-index:2147482000";
      this._houseRoot = host.attachShadow({ mode: "open" });
      document.body.appendChild(host);
      this._houseHost = host;
    }
    const w = this._box.w || (window.innerWidth || 1280);
    const h = this._box.h || (window.innerHeight || 800);
    const g = houseOverlayGeometry(w, h);
    const words = this._houseWords();
    const root = this._houseRoot;
    root.replaceChildren();
    const style = document.createElement("style");
    style.textContent = `
      .shade { position: absolute; inset: 0; background: #111; color: #fff; display: flex; flex-direction: column;
               align-items: center; justify-content: center; padding: ${g.pad}px; box-sizing: border-box; overflow: hidden;
               font-family: sans-serif; text-align: center; }
      h1 { margin: 0 0 ${g.gap}px; font-size: ${g.titleSize}px; font-weight: 500; line-height: 1.3; }
      .status { font-size: ${g.statusSize}px; line-height: 1.4; color: #ddd; min-height: ${Math.ceil(g.statusSize * 1.4)}px; }
      .buttons { display: flex; gap: ${g.gap}px; justify-content: center; margin-top: ${2 * g.gap}px; }
      button { height: ${g.btnH}px; width: ${g.btnW}px; border-radius: 12px; border: none; font-size: 20px; cursor: pointer; color: #fff; }
      button[disabled] { opacity: .5; cursor: default; }
      .answer { background: #2e7d32; } .decline, .hangup, .cancel { background: #c62828; } .close { background: #555; }
    `;
    const shade = el("div", "shade");
    shade.append(el("h1", null, words.title), el("div", "status", words.status));
    const buttons = el("div", "buttons");
    const add = (kind, text, fn, disabled) => {
      const b = el("button", kind, text);
      b.id = `house-${kind}-btn`;
      if (disabled) b.disabled = true;
      else b.addEventListener("click", fn);
      buttons.append(b);
    };
    for (const kind of words.buttons) {
      if (kind === "answer") add("answer", "Answer", () => this._houseAnswer());
      else if (kind === "decline") add("decline", "Decline", () => this._houseDecline());
      else if (kind === "hangup") add("hangup", "Hang up", () => this._houseHangup());
      else if (kind === "cancel") add("cancel", "Cancel", () => this._houseHangup());
      else if (kind === "cancel-wait") add("cancel", "Cancel", null, true);
      else add("close", "Close", () => this._setHouse(null));
    }
    shade.append(buttons);
    root.append(style, shade);
  }

  // ---- ring takeover -----------------------------------------------------------------------

  // May a ring at `door` take this card over? A mounted card: no wake = yes (the old behaviour). The wake listener's card
  // (config `_ghost`): only a wake that names the door; no wake and an empty one never wake it.
  _ringAllowed(door) {
    if (this._config._ghost && !this._wake) return false;
    return wakeAllowsDoor(this._wake, door);
  }

  // K2: bring the dashboard view that holds this screen's card to the front (listener card only; no path = nothing moves).
  _wakeNavigate() {
    if (!this._config._ghost) return;
    const path = validBackPath(this._wake && this._wake.path) || wakePathGet(this._config.endpoint);
    if (!path) return;
    const bare = path.split(/[?#]/)[0];
    if (window.location && window.location.pathname === bare) return;
    try {
      window.history.pushState(null, "", path);
      window.dispatchEvent(new window.CustomEvent("location-changed", { bubbles: true, composed: true }));
    } catch (err) { console.error("portico-card: wake navigation failed", err); }
  }

  _startRing() {
    // a card that already holds the overlay for this call owns the ring: this one neither sounds nor navigates
    this._ringCallId = this._activeCallId;      // kept past the call's end: the "answered elsewhere" notice is still this call's
    if (this._ringCallId && !claimCall(`door:${this._ringCallId}`, this)) return;
    this._wakeNavigate();
    this._startChime();
    clearTimeout(this._ringTimer);
    const callId = this._activeCallId;
    this._ringTimer = setTimeout(() => {
      // nobody answered for the whole window: let go of the screen (the call itself is not touched)
      if (this._activeCallId === callId && this._state === STATE_RINGING) {
        this._hiddenFor = callId;
        this._stopChime();
        this._say("Nobody answered.");
      }
    }, this._config._ringTimeoutMs || RING_TIMEOUT_MS);
    this._pollPictures(true);
  }

  _endRing() {
    clearTimeout(this._ringTimer);
    this._ringTimer = null;
    this._stopChime();
  }

  _canAnswer(device) {
    const cap = device && device.capabilities;
    return !!cap && typeof cap.OutboundAudio === "number" && cap.OutboundAudio !== AUDIO_NONE;
  }

  _overlayWanted() {
    if (!this._config.takeover) return false;
    let wanted = false;
    if (this._elsewhere) wanted = true;
    else if (this._state === STATE_RINGING) wanted = this._hiddenFor !== this._activeCallId;
    else if (this._state === STATE_IN_CALL) wanted = this._hiddenFor !== this._activeCallId;
    // one overlay per call, whichever card claimed it first (a mounted card and the listener's card may both see it)
    return wanted && (!this._ringCallId || claimCall(`door:${this._ringCallId}`, this));
  }

  _dismiss() {
    this._touched = true;                    // closing the overlay is asking for the page behind it: it is theirs now
    this._hiddenFor = this._activeCallId;
    this._elsewhere = false;
    this._stopChime();
    this._render();
  }

  _startChime() {
    if (this._chimeTimer) return;
    try {
      const AC = (typeof window !== "undefined") && (window.AudioContext || window.webkitAudioContext);
      if (!AC) return;
      const ctx = this._audioCtx || (this._audioCtx = new AC());
      if (ctx.resume) ctx.resume().catch(() => {});
      this._soundBlocked = ctx.state === "suspended";
      const beep = () => {
        try {
          const o = ctx.createOscillator();
          const g = ctx.createGain();
          o.frequency.value = 880;
          g.gain.value = 0.2;
          o.connect(g);
          g.connect(ctx.destination);
          o.start();
          o.stop(ctx.currentTime + 0.25);
        } catch (_err) {
          /* a chime that cannot play is not an error */
        }
      };
      beep();
      this._chimeTimer = setInterval(beep, this._config._chimeMs || 1500);
    } catch (_err) {
      /* no audio on this device */
    }
  }

  _stopChime() {
    clearInterval(this._chimeTimer);
    this._chimeTimer = null;
  }

  _ensureOverlay() {
    if (this._overlayHost) return;
    const host = document.createElement("div");
    host.setAttribute("data-portico-overlay", "");
    host.style.cssText = "position:fixed;top:0;left:0;right:0;bottom:0;z-index:2147483000";
    this._overlayRoot = host.attachShadow({ mode: "open" });
    // The first touch is what lets a browser start sound (autoplay rule): use it.
    host.addEventListener("pointerdown", () => {
      if (this._audioCtx && this._audioCtx.resume) {
        this._audioCtx.resume().then(() => {
          if (this._soundBlocked) {
            this._soundBlocked = false;
            this._renderOverlay();
          }
        }).catch(() => {});
      }
    });
    document.body.appendChild(host);
    this._overlayHost = host;
  }

  _removeOverlay() {
    if (this._overlayHost && this._overlayHost.parentNode) this._overlayHost.parentNode.removeChild(this._overlayHost);
    this._overlayHost = null;
    this._overlayRoot = null;
  }

  _renderOverlay() {
    if (!this._overlayWanted()) {
      this._removeOverlay();
      this._failsafeStop();
      this._syncLive();
      return;
    }
    const opening = !this._overlayHost;
    this._ensureOverlay();
    this._failsafeSync();
    const root = this._overlayRoot;
    root.replaceChildren();
    const style = document.createElement("style");
    style.textContent = `
      .shade { position: absolute; inset: 0; background: #111; color: #fff; display: flex; flex-direction: column;
               align-items: center; padding: 16px; box-sizing: border-box; overflow: auto; font-family: sans-serif; }
      h1 { margin: 8px 0 12px; font-size: 28px; font-weight: 500; text-align: center; }
      .pics { width: 100%; display: flex; flex-flow: row wrap; justify-content: center; gap: 8px; }
      .pics .pic { flex: 0 0 min(100%, 900px); width: min(100%, 900px); }
      .pics .pic.small { flex: 0 0 calc(50% - 4px); width: calc(50% - 4px); }
      .pic { position: relative; width: min(100%, 900px); aspect-ratio: 16 / 9; background: #000; border-radius: 8px; overflow: hidden; }
      .pic img { width: 100%; height: 100%; object-fit: contain; display: block; }
      .pic video { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: contain; display: block; z-index: 1; }
      .pic .fps { position: absolute; right: 8px; bottom: 8px; z-index: 2; font-size: 13px; line-height: 1; padding: 3px 8px;
                  border-radius: 8px; background: rgba(0, 0, 0, .6); color: #fff; pointer-events: none; }
      .pic .none { position: absolute; inset: 0; display: none; align-items: center; justify-content: center; color: #aaa; }
      .pic.nopic .none { display: flex; }
      .buttons { display: flex; flex-wrap: wrap; gap: 12px; justify-content: center; margin-top: 16px; width: 100%; }
      button { padding: 18px 28px; border-radius: 12px; border: none; font-size: 20px; cursor: pointer; color: #fff; min-width: 140px; }
      .hear { background: #ef6c00; }
      .answer { background: #2e7d32; } .open { background: #1565c0; } .hangup, .dismiss { background: #c62828; }
      .dismiss { background: #555; } .open.armed { background: #ef6c00; }
      .msg { margin-top: 12px; font-size: 18px; color: #ddd; text-align: center; max-width: 700px; }
      .hint { margin-top: 10px; font-size: 14px; color: #ffcc80; }
      .err { margin-top: 10px; color: #ff8a80; }
    `;
    const shade = el("div", "shade");
    const device = this._doors.find((d) => d.door_id === this._activeDoorId);
    const inCall = this._state === STATE_IN_CALL;
    const caller = this._callerFor(this._activeDoorId);
    const who = caller && caller.name;
    shade.append(el("h1", null, this._elsewhere ? "Answered elsewhere"
      : (inCall ? (who ? `In a call with ${who}` : "In a call") : (who ? `${who} is calling` : "Someone is at the door"))));

    // Someone else took a call this screen was ringing for: the cameras stay (no door buttons) until it ends. When the race
    // was lost at Answer the call is forgotten (no door): then it is the short notice with Close only.
    const watching = this._elsewhere && !!this._activeDoorId;
    if (!this._elsewhere || watching) {
      let frame = el("div", "pic nopic");
      // A known caller shows its own cameras (one still each); only an unknown one falls to the old door-camera rule.
      const callerCams = caller && Array.isArray(caller.cameras) ? caller.cameras : [];
      const camId = caller ? null : this._ringCameraId();
      if (callerCams.length) {
        frame = el("div", "pics");
        callerCams.forEach((c, n) => {
          // The server's effective size: a big camera has a row of its own, a small one half a row (two per row).
          const one = el("div", "pic nopic " + (c.size === "small" ? "small" : "big"));
          const img = el("img");
          img.dataset.camera = c.id;
          img.alt = c.label;
          one.append(img, el("span", "none", c.available ? "No picture" : "Camera unavailable"));
          if (n === 0 && c.available && this._liveSupported()) this._markLiveSlot(one, "ring", c, "big", true);   // a ring: live by default
          frame.append(one);
        });
      } else if (camId) {
        const img = el("img");
        img.dataset.camera = camId;
        img.alt = "Camera";
        frame.append(img, el("span", "none", "No picture"));
      } else {
        const url = this._cameraImageUrl(this._activeDoorId || "");
        if (url) {
          const img = el("img");
          img.setAttribute("src", url);
          frame.classList.remove("nopic");
          frame.append(img);
        } else {
          frame.append(el("span", "none", "No camera"));
        }
      }
      shade.append(frame);

      const buttons = el("div", "buttons");
      const openButtons = watching ? [] : caller
        ? ((inCall ? caller.call_buttons : caller.ring_buttons) || [])
        : this._allActions().filter((a) => DOOR_KINDS.has(a.kind));
      for (const a of openButtons) {
        const armed = this._armed === a.id;
        const b = el("button", "open" + (armed ? " armed" : ""), armed ? `Tap again: ${a.label}` : a.label);
        b.disabled = !a.available;
        b.dataset.action = a.id;
        b.addEventListener("click", () => this._pressAction(a));
        buttons.append(b);
      }
      if (!watching && !caller && !openButtons.length && this._activeDoorId) {
        const b = el("button", "open", "Open");
        b.id = "open-btn";
        b.addEventListener("click", () => this._openRelay(this._activeDoorId));
        buttons.append(b);
      }
      if (!watching && !inCall && this._canAnswer(device)) {
        const b = el("button", "answer", "Answer");
        b.id = "answer-btn";
        b.addEventListener("click", () => this._answer(this._activeDoorId, this._activeCallId));
        buttons.append(b);
      }
      if (inCall && this._audioBlocked) buttons.append(this._hearButton());
      if (inCall) {
        const b = el("button", "hangup", "Hang up");
        b.id = "hangup-btn";
        b.addEventListener("click", () => this._hangup());
        buttons.append(b);
      }
      const d = el("button", "dismiss", "Dismiss");
      d.id = "dismiss-btn";
      d.addEventListener("click", () => this._dismiss());
      buttons.append(d);
      shade.append(buttons);

      if (!watching && !inCall && !this._canAnswer(device)) {
        shade.append(el("div", "msg", "This door cannot be answered from here. Answer in the Portico or intercom app."));
      }
      if (this._soundBlocked) shade.append(el("div", "hint", "Touch the screen once to turn the ring sound on."));
    } else {
      const d = el("button", "dismiss", "Close");
      d.id = "dismiss-btn";
      d.addEventListener("click", () => this._dismiss());
      const buttons = el("div", "buttons");
      buttons.append(d);
      shade.append(buttons);
    }
    if (this._notice) shade.append(el("div", "msg", this._notice));
    if (this._error) shade.append(el("div", "err", this._error));
    root.append(style, shade);
    this._applyPictures(root);
    this._syncLive();
    // A screen's first still is asked for the moment the overlay exists (the ring's own poll ran before it did).
    if (opening) this._pollPictures(true);
  }

  // ---- calls (unchanged behaviour) ----------------------------------------------------------

  async _answer(doorId, callId) {
    this._answering = true;
    // a retry starts clean: an earlier "busy" notice must not linger over the call
    this._error = null;
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
    } catch (err) {
      // §9.9's own stated prerequisite — HTTPS or localhost — surfaced
      // to the person looking at the card, not just the console.
      console.error("portico-card: getUserMedia failed", err);
      this._error = "Microphone access failed — answering needs HTTPS (or localhost), per the design doc's own prerequisite.";
      this._answering = false;
      this._render();
      return;
    }
    this._localStream = stream;

    const pc = new RTCPeerConnection();
    this._pc = pc;
    for (const track of stream.getTracks()) {
      pc.addTrack(track, stream);
    }
    pc.ontrack = (ev) => {
      if (this._pc !== pc) return; // a track of a call already torn down
      const stream = ev.streams[0];
      if (stream && stream !== this._remoteStream) {
        this._remoteStream = stream;
        this._playRemote();
      }
      this._render();
    };

    try {
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      await this._waitForIceGatheringComplete(pc);

      const modeResult = await this._hass.connection.sendMessagePromise({
        type: "portico/answer",
        door_id: doorId,
        call_id: callId,
        client_kind: "browser",
      });
      if (modeResult.mode !== "relay") {
        // A browser can only ever be answered in relay mode (§9.6) —
        // anything else means something upstream disagrees with this
        // card's own assumption, worth failing loudly on rather than
        // silently pretending to handle.
        throw new Error(`unexpected answer mode for a browser client: ${modeResult.mode}`);
      }

      const offerResult = await this._hass.connection.sendMessagePromise({
        type: "portico/webrtc_offer",
        call_id: callId,
        door_id: doorId,
        sdp: pc.localDescription.sdp,
      });
      await pc.setRemoteDescription({ type: "answer", sdp: offerResult.sdp });
    } catch (err) {
      console.error("portico-card: answer failed", err);
      if (err && err.code === "already_answered") {
        // Another client holds (or ended) this call: nothing to retry.
        this._teardownCall();
        this._callEndedAfterClaim(ALREADY_ANSWERED_TEXT);
        this._render();
        return;
      }
      if (err && err.code === "not_available") {
        // The call is gone, or the door cannot be answered now: nothing to retry.
        this._teardownCall();
        this._callEndedAfterClaim(CALL_ENDED_TEXT);
        this._render();
        return;
      }
      this._error = describeError(err, "Answering failed — see the browser console.");
      this._answering = false;
      this._teardownCall();
      this._render();
      return;
    }

    this._endRing();
    this._state = STATE_IN_CALL;
    this._render();
  }

  _callEndedAfterClaim(text) {
    this._answering = false;
    this._endRing();
    this._state = STATE_IDLE;
    // forget the call, so a late `answered`/`idle` event for it cannot put the card in a call
    this._activeDoorId = null;
    this._activeCallId = null;
    this._error = text;
    if (this._config.takeover) {
      // the overlay says "Answered elsewhere" for a moment, then lets go
      this._elsewhere = true;
      clearTimeout(this._elsewhereTimer);
      this._elsewhereTimer = setTimeout(() => {
        this._elsewhere = false;
        this._error = null;
        this._render();
      }, this._config._elsewhereMs || ANSWERED_ELSEWHERE_MS);
    }
  }

  _waitForIceGatheringComplete(pc) {
    // Matches internal/sfu.Session's own non-trickle design on the Go
    // side (§9.6's package doc) — one offer/answer round trip is
    // enough, appropriate for this design's tailnet-only, no-STUN/TURN
    // reality (§4.8). portico/webrtc_ice exists for a future trickle
    // path but isn't needed by this card's own good-path flow.
    if (pc.iceGatheringState === "complete") return Promise.resolve();
    return new Promise((resolve) => {
      const check = () => {
        if (pc.iceGatheringState === "complete") {
          pc.removeEventListener("icegatheringstatechange", check);
          resolve();
        }
      };
      pc.addEventListener("icegatheringstatechange", check);
    });
  }

  async _hangup() {
    if (this._activeCallId) {
      try {
        await this._hass.connection.sendMessagePromise({
          type: "portico/hangup",
          call_id: this._activeCallId,
          reason: "user_hangup",
        });
      } catch (err) {
        console.error("portico-card: hangup failed", err);
      }
    }
    this._teardownCall();
    this._endRing();
    this._state = STATE_IDLE;
    this._activeDoorId = null;
    this._activeCallId = null;
    this._render();
  }

  async _openRelay(doorId) {
    try {
      await this._hass.connection.sendMessagePromise({ type: "portico/open_relay", door_id: doorId });
    } catch (err) {
      console.error("portico-card: open_relay failed", err);
      this._error = describeError(err, "Open failed — see the browser console.");
      this._render();
    }
  }

  _teardownCall() {
    this._answering = false;
    if (this._pc) {
      this._pc.close();
      this._pc = null;
    }
    if (this._localStream) {
      for (const track of this._localStream.getTracks()) track.stop();
      this._localStream = null;
    }
    this._remoteStream = null;
    this._stopRemote();
  }

  // Attach the caller's stream to the persistent audio element and start it. The Answer press is the user
  // gesture; if the browser still refuses, show "Tap to hear" and let the next tap play (never an alert).
  _playRemote() {
    const audio = this._remoteAudio;
    audio.srcObject = this._remoteStream;
    this._audioBlocked = false;
    this._startRemote();
  }

  _startRemote() {
    const audio = this._remoteAudio;
    let p;
    try { p = audio.play(); } catch (err) { p = Promise.reject(err); }
    if (p && p.then) {
      p.then(() => {
        if (this._audioBlocked && audio.srcObject === this._remoteStream) {
          this._audioBlocked = false;
          this._render();
        }
      }, () => {
        if (audio.srcObject && audio.srcObject === this._remoteStream && !this._audioBlocked) {
          this._audioBlocked = true;
          this._render();
        }
      });
    }
  }

  _stopRemote() {
    const audio = this._remoteAudio;
    if (audio && audio.srcObject) {
      try { audio.pause(); } catch (err) { /* nothing playing */ }
      audio.srcObject = null;
    }
    this._audioBlocked = false;
  }

  _hearButton() {
    const b = el("button", "hear", "Tap to hear");
    b.id = "hear-btn";
    b.addEventListener("click", () => this._startRemote());
    return b;
  }

  _playRingtone() {
    // Autoplay-exemption caveat (§9.7's own note): browsers commonly
    // block audio until a user gesture unlocks the page. A rejected
    // play() is expected sometimes, not treated as an error.
    const audio = this.shadowRoot.querySelector("#ringtone");
    if (audio) {
      audio.currentTime = 0;
      audio.play().catch(() => {});
    }
  }

  _cameraImageUrl(doorId) {
    if (!this._hass) return "";
    // The door's own camera entity, as portico/doors reports it; no entity, no video (never a guess).
    const entityId = (this._doors.find((d) => d.door_id === doorId) || {}).camera_entity_id;
    if (!entityId) return "";
    const token = this._hass.states[entityId]?.attributes?.access_token;
    if (!token) return "";
    return `/api/camera_proxy_stream/${entityId}?token=${token}`;
  }

  // ---- render --------------------------------------------------------------------------------

  _render() {
    if (!this.shadowRoot) return;
    // measure once at render when the observer has not (or cannot) report; jsdom has no layout and answers 0
    if (!this._box.w && this.getBoundingClientRect) {
      const r = this.getBoundingClientRect();
      if (r.width > 0) this._box = { w: Math.round(r.width), h: Math.round(r.height) };
    }
    const wide = this._isWide() && !!this._content && !!this._blocksOf(this._currentPage());
    if (wide) this.setAttribute("wide", "");
    else this.removeAttribute("wide");
    this._wideGeo = undefined;

    const style = `
      <style>
        :host { display: block; }
        :host([fullpage]) { position: fixed; top: 0; left: 0; right: 0; bottom: 0; overflow: auto;
                            background: var(--primary-background-color, #111); }
        :host([fullpage]) ha-card { border-radius: 0; box-shadow: none; min-height: 100%; box-sizing: border-box; }
        ha-card { padding: 16px; }
        .tabs { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 10px; }
        .tab { padding: 10px 18px; font-size: 16px; background: transparent; color: var(--primary-text-color, #000);
               border: 1px solid var(--divider-color, #888); }
        .tab.on { background: var(--primary-color, #2196f3); color: var(--text-primary-color, #fff); }
        .ringing { text-align: center; }
        .ringing h2 { margin: 0 0 16px; }
        .buttons { display: flex; gap: 8px; justify-content: center; }
        button { padding: 8px 16px; border-radius: 8px; border: none; cursor: pointer; font-size: 14px; }
        .answer { background: #4caf50; color: white; }
        .decline, .hangup { background: #f44336; color: white; }
        .open { background: #2196f3; color: white; }
        .video { width: 100%; border-radius: 8px; margin-bottom: 12px; }
        .error { color: #f44336; font-size: 13px; margin-top: 8px; }
        .notice { font-size: 14px; margin-top: 8px; }
        .muted { opacity: .7; font-size: 13px; }
        .cams { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 10px; }
        .cam .pic { position: relative; aspect-ratio: 16 / 9; background: #000; border-radius: 8px; overflow: hidden; }
        .cam .pic img { width: 100%; height: 100%; object-fit: cover; display: block; }
        .cam .pic .none { position: absolute; inset: 0; display: none; align-items: center; justify-content: center; color: #bbb; font-size: 13px; }
        .cam .pic.nopic .none { display: flex; }
        .cam.off { opacity: .6; }
        .cam.tap { cursor: pointer; }
        .pic video { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; display: block; z-index: 1; }
        .pic .fps { position: absolute; right: 6px; bottom: 6px; z-index: 2; font-size: 11px; line-height: 1; padding: 2px 6px;
                    border-radius: 8px; background: rgba(0, 0, 0, .6); color: #fff; pointer-events: none; }
        .cam .label { font-weight: 500; padding-top: 4px; }
        .acts { display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 10px; margin-top: 12px; }
        .act { padding: 16px; font-size: 16px; background: var(--primary-color, #2196f3); color: var(--text-primary-color, #fff); }
        .act.armed { background: #ef6c00; }
        .act[disabled] { opacity: .5; cursor: default; }
        .block { margin-top: 14px; }
        .block:first-child { margin-top: 0; }
        .block-title { margin: 0 0 8px; font-size: 18px; font-weight: 500; }
        .cams.blk { grid-template-columns: 1fr 1fr; }
        .cams.blk .cam.big { grid-column: 1 / -1; }
        .acts.blk { grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); }
        .acts.blk .act { min-height: 48px; min-width: 48px; display: flex; align-items: center; justify-content: center; gap: 8px; }
        .acts.blk .act[class*="c-"] { background: var(--pt-fill); color: var(--pt-text); }
        .acts.blk .act.armed { background: #ef6c00; color: #fff; }
        ${blockPaletteCss()}${this._config.fullpage ? FULLPAGE_CSS : ""}${wide ? WIDE_CSS : ""}${wide && this._config.fullpage ? FULLPAGE_WIDE_CSS : ""}${this._houseEntry() ? HOUSE_CSS : ""}
      </style>
    `;

    // In takeover mode a ring is shown by the overlay; the card itself keeps showing the dashboard view.
    const shown = this._config.takeover ? STATE_IDLE : this._state;
    let body;
    if (shown === STATE_RINGING) {
      const device = this._doors.find((d) => d.door_id === this._activeDoorId);
      const imgUrl = this._cameraImageUrl(this._activeDoorId);
      body = `
        <div class="ringing">
          <h2>${device ? esc(device.name || device.door_id) : "Someone"} is calling</h2>
          ${imgUrl ? `<img class="video" src="${esc(imgUrl)}" />` : ""}
          <div class="buttons">
            <button class="answer" id="answer-btn">Answer</button>
            <button class="decline" id="decline-btn">Decline</button>
          </div>
        </div>
        <audio id="ringtone" loop src="/local/portico-ringtone.mp3"></audio>
      `;
    } else if (shown === STATE_IN_CALL) {
      const imgUrl = this._cameraImageUrl(this._activeDoorId);
      body = `
        <div class="ringing">
          ${imgUrl ? `<img class="video" src="${esc(imgUrl)}" />` : ""}
          <div class="buttons">
            <button class="open" id="open-btn">Open</button>
            ${this._audioBlocked ? `<button class="hear" id="hear-btn" style="background:#ef6c00;color:white">Tap to hear</button>` : ""}
            <button class="hangup" id="hangup-btn">Hang up</button>
          </div>
        </div>
      `;
    } else {
      body = `<div id="content-slot"></div>`;
    }

    this.shadowRoot.innerHTML = `
      ${style}
      <ha-card>
        ${body}
        ${this._error ? `<div class="error">${esc(this._error)}</div>` : ""}
        <div id="notice-slot"></div>
      </ha-card>
      ${this._config.fullpage ? `<button id="back-rail" aria-label="Back" title="Back" style="${BACK_RAIL_STYLE}">${BACK_RAIL_ICON}</button>` : ""}
    `;
    const slot = this.shadowRoot.querySelector("#content-slot");
    if (slot) slot.replaceWith(this._config._ghost ? document.createElement("span") : this._contentView());
    const noticeSlot = this.shadowRoot.querySelector("#notice-slot");
    if (noticeSlot && this._notice) noticeSlot.replaceWith(el("div", "notice", this._notice));

    this._attachListeners();
    this._applyPictures(this.shadowRoot);
    this._renderOverlay();
    this._renderHouse();
  }

  _attachListeners() {
    const answerBtn = this.shadowRoot.querySelector("#answer-btn");
    if (answerBtn) {
      answerBtn.addEventListener("click", () => this._answer(this._activeDoorId, this._activeCallId));
    }
    const declineBtn = this.shadowRoot.querySelector("#decline-btn");
    if (declineBtn) {
      declineBtn.addEventListener("click", () => this._hangup());
    }
    const hangupBtn = this.shadowRoot.querySelector("#hangup-btn");
    if (hangupBtn) {
      hangupBtn.addEventListener("click", () => this._hangup());
    }
    const rail = this.shadowRoot.querySelector("#back-rail");
    if (rail) rail.addEventListener("click", () => this._goBack());
    const hearBtn = this.shadowRoot.querySelector("#hear-btn");
    if (hearBtn) hearBtn.addEventListener("click", () => this._startRemote());
    const openBtn = this.shadowRoot.querySelector("#open-btn");
    if (openBtn) {
      openBtn.addEventListener("click", () => this._openRelay(this._activeDoorId));
    }
  }

  // The kiosk page has no browser chrome. The rail goes to the back path the way HA's own navigation does it (SPA:
  // pushState + location-changed, no page reload), then location.assign; the layered way out (Fully's start URL, history,
  // "/") is only for a page with no back path or when both navigations fail. Same origin only: an absolute or
  // cross-origin URL would log the panel out.
  _goBack() {
    const path = backTarget(this._config);
    if (path) {
      try {
        window.history.pushState(null, "", path);
        window.dispatchEvent(new window.CustomEvent("location-changed", { bubbles: true, composed: true }));
        return;
      } catch (err) { console.error("portico-card: pushState to the back path failed", err); }
      try { this._assign(path); return; } catch (err) { console.error("portico-card: location.assign failed", err); }
    } else if (this._config && this._config.back_path) {
      console.warn("portico-card: back_path is not an in-app path starting with a single '/'; using the fallback");
    }
    this._leave();
  }

  _assign(path) { window.location.assign(path); }   // a seam: jsdom's location.assign cannot be replaced

  _leave() {
    const fully = typeof window !== "undefined" ? window.fully : undefined;
    const hasFully = !!fully && typeof fully.loadStartURL === "function";
    const how = closeAction(hasFully, window.history ? window.history.length : 0);
    if (how === "fully") {
      try { fully.loadStartURL(); return; } catch (err) { console.error("portico-card: fully.loadStartURL failed", err); }
      if (window.history && window.history.length > 1) { window.history.back(); return; }
    } else if (how === "back") { window.history.back(); return; }
    this._assign("/");
  }

  static wideMoreGeometry(rowW) { return wideMoreGeometry(rowW); }
  static wideDecision(w, h, fullpage) { return wideDecision(w, h, fullpage); }
  static wideGeometry(o) { return wideGeometry(o); }
  static closeAction(hasFully, historyLength) { return closeAction(hasFully, historyLength); }
  static validBackPath(p) { return validBackPath(p); }
  static backTarget(c) { return backTarget(c); }
  static get DEFAULT_BACK_PATH() { return DEFAULT_BACK_PATH; }
  static get W_RAIL() { return W_RAIL; }
  static houseEntry(c, f) { return houseEntry(c, f); }
  static houseKeyText(e, b) { return houseKeyText(e, b); }
  static houseRefusalText(c) { return houseRefusalText(c); }
  static houseOutcomeText(s, n) { return houseOutcomeText(s, n); }
  static houseReduce(c, e, o) { return houseReduce(c, e, o); }
  static houseOverlayGeometry(w, h) { return houseOverlayGeometry(w, h); }
  static get W_HOUSE() { return W_HOUSE; }

  getCardSize() {
    return 6;
  }

  static getStubConfig() {
    return { takeover: false };
  }
}

customElements.define("portico-card", PorticoCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: "portico-card",
  name: "Portico",
  description: "What a phone or a screen shows (cameras, buttons), and a door ring on a wall tablet.",
});

// ---- the every-page wake listener (Devices D3b, DEVICES 4.5 K1/K2) ---------------------------------------------------
//
// the owner's rule for Screens: "The panel will activate itself when a call is received that dials a talk element contained in it."
// This file is the module frontend.add_extra_js_url loads on EVERY Home Assistant page, so the listener lives here: a screen's
// ring must not depend on its dashboard tab being the visible one (HA mounts only the current view's cards).
//
// What it does, once per page load, for a logged-in user:
//   1. portico/kiosk/hello (no endpoint) -> `declared`, the screens declared for this HA user. None (a phone's login, an
//      administrator, any other user) or any error (portico missing, another contract): it stops, and holds no subscription.
//      The server keeps that command bounded (one memory entry per user, 32 at most); it is asked once per page load.
//   2. For the first declared screen it mounts a hidden <portico-card> (config `_ghost`) in a body-level host. That card is the
//      ordinary card: it reads the screen's `wake` from content/get and content_changed, obeys it, and draws the ordinary ring
//      overlay and chime on document.body, so nothing of the overlay is copied. Differences, all in the card and keyed on `_ghost`:
//      it draws no page and fetches no page pictures, an ABSENT wake means "never" (the old behaviour is a mounted card's), and
//      it shows a house call only when it is for this screen and wake.screen_call is on.
//   3. K2: when a ring wakes it, it navigates (pushState + location-changed) to the dashboard path of the view holding the
//      screen's card: wake.path if the server ever sends one, else the `path:` a mounted card of that screen last remembered
//      in this browser. No path, nothing navigates and the overlay shows on the current page.
//   4. A mounted card showing the same call is not doubled: the first card to claim the call id owns it (CLAIMS above).
//   5. It stands down on pagehide and when the HA user is gone (logout), checked every WAKE_WATCH_MS.
// Not seen on a real wall panel: Fully Kiosk's screensaver, the chime past the autoplay rule (see the PR).
const WAKE_HASS_TRIES = 120;      // wait up to 60 s for HA to hand over `hass` (500 ms apart), then give up
const WAKE_WATCH_MS = 10000;

class WakeListener {
  constructor(opts = {}) {
    this._getHass = opts.getHass || (() => {
      const ha = document.querySelector("home-assistant");
      return ha && ha.hass ? ha.hass : null;
    });
    this._tryMs = opts.tryMs || 500;
    this._watchMs = opts.watchMs || WAKE_WATCH_MS;
    this._tries = opts.tries || WAKE_HASS_TRIES;
    this._started = false;
    this._runId = 0;
    this._tryTimer = null;
    this._watchTimer = null;
    this._host = null;
    this._card = null;
    this._userId = null;
    this._onHide = () => this.stop();
    this._onShow = (ev) => { if (ev && ev.persisted) this.start(); };
  }

  get card() { return this._card; }
  get running() { return this._started; }

  start() {
    if (this._started) return;
    this._started = true;
    const run = ++this._runId;
    window.addEventListener("pagehide", this._onHide);
    window.addEventListener("pageshow", this._onShow);
    let left = this._tries;
    const attempt = async () => {
      this._tryTimer = null;
      if (run !== this._runId) return;
      const hass = this._getHass();
      if (!hass || !hass.user || !hass.connection) {
        if (--left > 0) this._tryTimer = setTimeout(attempt, this._tryMs);
        return;
      }
      await this._begin(hass, run);
    };
    attempt();
  }

  async _begin(hass, run) {
    let declared = [];
    try {
      const r = await hass.connection.sendMessagePromise({ type: "portico/kiosk/hello" });
      declared = r && Array.isArray(r.declared) ? r.declared.filter((x) => typeof x === "string" && x) : [];
    } catch (err) {
      return;                                   // no portico, another contract, not allowed: nothing to listen for
    }
    if (run !== this._runId || !declared.length) return;
    this._userId = hass.user.id;
    const host = document.createElement("div");
    host.setAttribute("data-portico-wake-host", "");
    host.style.cssText = "display:none";
    const card = document.createElement("portico-card");
    host.appendChild(card);
    document.body.appendChild(host);
    this._host = host;
    this._card = card;
    card.setConfig({ type: "custom:portico-card", endpoint: declared[0], takeover: true, _ghost: true });
    card.hass = hass;
    this._watchTimer = setInterval(() => this._watch(), this._watchMs);
  }

  // HA gives a new `hass` object on every state change; the card only needs the connection and the theme. A missing user
  // (logout) or another one ends the listener.
  _watch() {
    const hass = this._getHass();
    if (!hass || !hass.user || hass.user.id !== this._userId) { this.stop(); return; }
    if (this._card) this._card.hass = hass;
  }

  stop() {
    if (!this._started) return;
    this._started = false;
    this._runId++;
    clearTimeout(this._tryTimer);
    clearInterval(this._watchTimer);
    this._tryTimer = null;
    this._watchTimer = null;
    window.removeEventListener("pagehide", this._onHide);
    window.removeEventListener("pageshow", this._onShow);
    if (this._host && this._host.parentNode) this._host.parentNode.removeChild(this._host);   // disconnectedCallback frees the rest
    this._host = null;
    this._card = null;
    this._userId = null;
  }
}

PorticoCard.WakeListener = WakeListener;
if (typeof document !== "undefined" && document.querySelector("home-assistant") && !window.__porticoWakeListener) {
  window.__porticoWakeListener = new WakeListener();
  window.__porticoWakeListener.start();
}
