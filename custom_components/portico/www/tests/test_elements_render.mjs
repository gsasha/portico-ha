// The shared Elements renderers (DEV-760 E3): per-kind drawing on fake states, live update, the inert preview button,
// the unknown-kind fallback, text-only output for hostile strings, parity of the palette / default icons / ask-first set
// with the card, the panel and the two contract files, and a grep that no HTML-string setter appears. jsdom, not a
// real browser or a real Home Assistant frontend: that is not covered here.
import assert from "assert";
import fs from "fs";
import { JSDOM } from "jsdom";
import * as R from "../portico-elements-render.js";

const dom = new JSDOM("<!doctype html><html><body></body></html>", { url: "http://localhost/", pretendToBeVisual: true });
const doc = dom.window.document;
const here = (p) => new URL(p, import.meta.url);
const read = (p) => fs.readFileSync(here(p), "utf8");
const el = (kind, extra = {}) => ({ id: "el-0000abcd", kind, name: "Thing", ...extra });
const st = (state, attributes = {}) => ({ state, attributes });

// 1. status: text and colour follow the states map, live
{
  const e = el("status", { entity_id: "lock.front", options: { states: { locked: { label: "Bolted", color: "teal" }, unlocked: { color: "red" } } } });
  const h = R.renderElement(doc, e, { states: { "lock.front": st("locked") } });
  assert.strictEqual(h.el.querySelector(".pe-value").textContent, "Bolted");
  assert.strictEqual(h.el.dataset.color, "teal");
  assert.strictEqual(h.el.style.background, "rgb(0, 121, 107)");
  h.update({ "lock.front": st("unlocked") });
  assert.strictEqual(h.el.querySelector(".pe-value").textContent, "Unlocked", "default label when the rule has only a colour");
  assert.strictEqual(h.el.dataset.color, "red");
  h.update({ "lock.front": st("unavailable") });
  assert.strictEqual(h.el.dataset.color, "neutral");
  assert.strictEqual(h.el.querySelector(".pe-value").textContent, "Unavailable");
  h.update({});
  assert.strictEqual(h.el.querySelector(".pe-value").textContent, "Entity not found");
  assert.strictEqual(h.el.dataset.state, "missing");
  assert.strictEqual(h.el.dataset.color, "error");
  h.update({ "lock.front": st("weird_state") });
  assert.strictEqual(h.el.querySelector(".pe-value").textContent, "weird_state", "an unmapped state is shown raw");
  const dark = R.renderElement(doc, e, { dark: true, states: { "lock.front": st("locked") } });
  assert.strictEqual(dark.el.style.background, "rgb(128, 203, 196)", "dark theme palette");
  h.destroy();
  h.update({ "lock.front": st("locked") });                       // after destroy: ignored, no throw
  assert.strictEqual(h.el.querySelector(".pe-value").textContent, "weird_state");
  console.log("PASS: status text and colour follow the states map, live; missing, unavailable, raw and dark");
}

// 2. status by attribute
{
  const e = el("status", { entity_id: "sensor.t", options: { attribute: "temperature" } });
  const h = R.renderElement(doc, e, { states: { "sensor.t": st("ok", { temperature: 21.5 }) } });
  assert.strictEqual(h.el.querySelector(".pe-value").textContent, "21.5");
  h.update({ "sensor.t": st("ok", {}) });
  assert.strictEqual(h.el.querySelector(".pe-value").textContent, "No value");
  h.update({ "sensor.t": st("ok", { temperature: { a: 1 } }) });
  assert.strictEqual(h.el.querySelector(".pe-value").textContent, "No value", "an object attribute is not dumped");
  console.log("PASS: a status can show an attribute; absent or structured gives 'No value'");
}

// 3. chip: compact, short name, same data
{
  const e = el("chip", { name: "Garage door opener", entity_id: "cover.g", options: { short: "Garage" } });
  const h = R.renderElement(doc, e, { states: { "cover.g": st("open") } });
  assert.ok(h.el.classList.contains("pe-chiptile"));
  assert.strictEqual(h.el.querySelector(".pe-name").textContent, "Garage");
  assert.strictEqual(h.el.querySelector(".pe-value").textContent, "Open");
  assert.strictEqual(h.el.dataset.color, "amber");
  const cut = R.renderElement(doc, el("chip", { name: "A very long element name", entity_id: "cover.g" }), {});
  assert.strictEqual(cut.el.querySelector(".pe-name").textContent, "A very long ", "name cut to 12 when short is absent");
  assert.ok(R.MAX_SHORT === 12);
  console.log("PASS: a chip is the compact form of a status (short name, same value and colour)");
}

// 3b. status and chip draw any mdi icon with ha-icon; unavailable or undrawable falls back to the default glyph
{
  for (const kind of ["status", "chip"]) {
    const h = R.renderElement(doc, el(kind, { icon: "lock-open-variant", entity_id: "lock.front" }), { states: { "lock.front": st("locked") } });
    const icon = () => h.el.querySelector("ha-icon").getAttribute("icon");
    assert.strictEqual(icon(), "mdi:lock-open-variant");
    h.update({});
    assert.strictEqual(icon(), "mdi:information-outline", "element not found: default glyph");
    h.update({ "lock.front": st("unlocked") });
    assert.strictEqual(icon(), "mdi:lock-open-variant", "back when it exists");
    const prefixed = R.renderElement(doc, el(kind, { icon: "mdi:thermometer", entity_id: "sensor.t" }), { states: { "sensor.t": st("20") } });
    assert.strictEqual(prefixed.el.querySelector("ha-icon").getAttribute("icon"), "mdi:thermometer");
    for (const bad of ["Bad Name", "../x", "x\"><img src=x>", "", 5]) {
      const b = R.renderElement(doc, el(kind, { icon: bad, entity_id: "sensor.t" }), { states: { "sensor.t": st("1") } });
      assert.strictEqual(b.el.querySelector("ha-icon").getAttribute("icon"), "mdi:information-outline", `undrawable ${String(bad)}`);
      assert.strictEqual(b.el.querySelector("img"), null);
    }
    const nostate = R.renderElement(doc, el(kind, { icon: "gate", entity_id: "sensor.t" }), {});
    assert.strictEqual(nostate.el.querySelector("ha-icon").getAttribute("icon"), "mdi:information-outline");
  }
  console.log("PASS: status and chip draw their own mdi icon; unavailable or undrawable shows the default glyph");
}

// 4. button: inert preview
{
  const e = el("button", { name: "Open gate", icon: "gate", options: { action: "cover_open", color: "blue" }, entity_id: "cover.gate" });
  let clicks = 0;
  const h = R.renderElement(doc, e, {});
  doc.body.append(h.el);
  const b = h.el.querySelector("button");
  b.addEventListener("click", () => clicks++);
  b.click();
  assert.ok(b.disabled && b.getAttribute("aria-disabled") === "true");
  assert.strictEqual(clicks, 0, "a disabled button runs nothing");
  assert.strictEqual(h.el.querySelectorAll("button").length, 1);
  assert.strictEqual(b.querySelector("ha-icon").getAttribute("icon"), "mdi:gate", "own icon");
  assert.strictEqual(b.dataset.color, "blue");
  assert.ok(h.el.textContent.includes(R.PREVIEW_NOTE));
  assert.ok(h.el.querySelector(".pe-asks"), "cover_open asks twice");
  const plain = R.renderElement(doc, el("button", { options: { action: "script_run" }, entity_id: "script.x" }), {});
  assert.strictEqual(plain.el.querySelector("ha-icon").getAttribute("icon"), "mdi:script-text-play", "default icon of the action");
  assert.ok(!plain.el.querySelector(".pe-asks"));
  assert.strictEqual(plain.el.querySelector("button").dataset.color, undefined, "no colour unless named");
  const badIcon = R.renderElement(doc, el("button", { icon: "x\"><img src=x>", options: { action: "lock_lock" }, entity_id: "lock.a" }), {});
  assert.strictEqual(badIcon.el.querySelector("ha-icon").getAttribute("icon"), "mdi:lock", "a non-slug icon falls back to the default");
  h.update({ "cover.gate": st("open") });                    // a button ignores states
  assert.ok(b.disabled);
  h.destroy();
  assert.ok(!doc.body.contains(h.el), "destroy removes the node");
  console.log("PASS: a button preview is disabled and inert, default icon, ask-twice mark, bad icon falls back");
}

// 5. camera
{
  const e = el("camera", { name: "Gate cam", entity_id: "camera.gate" });
  let t = 0;
  const h = R.renderElement(doc, e, { now: () => ++t, states: { "camera.gate": st("idle", { entity_picture: "/api/camera_proxy/camera.gate?token=abc" }) } });
  const img = () => h.el.querySelector("img");
  assert.strictEqual(img().getAttribute("src"), "/api/camera_proxy/camera.gate?token=abc");
  assert.strictEqual(img().alt, "Gate cam");
  h.el.querySelector(".pe-refresh").click();
  assert.strictEqual(img().getAttribute("src"), "/api/camera_proxy/camera.gate?token=abc&_=1", "refresh re-reads the same picture");
  h.update({ "camera.gate": st("idle", { entity_picture: "javascript:alert(1)" }) });
  assert.ok(!img() && h.el.textContent.includes("No picture"), "a non-http picture address is not loaded");
  h.update({ "camera.gate": st("idle", { entity_picture: "//evil.example/x.png" }) });
  assert.ok(!img(), "a protocol-relative address is not loaded");
  h.update({});
  assert.ok(h.el.textContent.includes("Entity not found"));
  const stream = R.renderElement(doc, el("camera", { name: "Yard", options: { stream: "rtsp://10.3.1.2:554/main" } }), {});
  assert.ok(!stream.el.querySelector("img"));
  assert.ok(stream.el.textContent.includes("10.3.1.2:554/main") && stream.el.textContent.includes("Stream only"));
  assert.ok(!stream.el.textContent.includes("rtsp://"));
  console.log("PASS: camera draws the entity picture, refreshes, refuses unsafe addresses; a stream-only camera is a tile");
}

// 6. unknown kind and bad input never throw; hostile text stays text
{
  for (const bad of [el("gauge"), null, undefined, 5, {}, { kind: "status", options: null, name: 5 }, el("status", { options: { states: "no" } })]) {
    const h = R.renderElement(doc, bad, { states: { "x.y": st("on") } });
    assert.ok(h.el && typeof h.update === "function" && typeof h.destroy === "function");
    h.update({}); h.destroy();
  }
  assert.ok(R.renderElement(doc, el("gauge"), {}).el.textContent.includes("Unknown kind"));
  const hostile = `<img src=x onerror=window.__pwn=1><script>window.__pwn=1</script>`;
  const mk = [
    el("status", { name: hostile, entity_id: "sensor.a", options: { states: { on: { label: hostile } } } }),
    el("chip", { name: hostile, entity_id: "sensor.a", options: { short: hostile } }),
    el("button", { name: hostile, options: { action: "script_run" }, entity_id: "script.a" }),
    el("camera", { name: hostile, entity_id: "camera.a" }),
    el("nope", { name: hostile }),
  ];
  for (const e of mk) {
    const h = R.renderElement(doc, e, { states: { "sensor.a": st("on", { x: hostile }), "camera.a": st("idle", {}) } });
    doc.body.append(h.el);
    assert.strictEqual(h.el.querySelectorAll("script").length, 0);
    assert.ok([...h.el.querySelectorAll("img")].every((i) => i.getAttribute("src") !== "x"), `${e.kind}: no injected img`);
    h.destroy();
  }
  assert.ok(!dom.window.__pwn, "nothing executed");
  const s = R.renderElement(doc, mk[0], { states: { "sensor.a": st("on") } });
  assert.ok(s.el.querySelector(".pe-name").textContent === hostile && s.el.querySelector(".pe-value").textContent === hostile, "shown verbatim as text");
  console.log("PASS: unknown or malformed input never throws; hostile names and labels stay text");
}

// 7. extension table: a new kind needs only a renderer
{
  R.RENDERERS.dial = (d, element) => { const n = d.createElement("div"); n.textContent = "dial " + element.name; return { el: n, update() {}, destroy() {} }; };
  const h = R.renderElement(doc, el("dial"), {});
  assert.strictEqual(h.el.textContent, "dial Thing");
  delete R.RENDERERS.dial;
  assert.ok(R.renderElement(doc, el("dial"), {}).el.textContent.includes("Unknown kind"));
  assert.deepStrictEqual(Object.keys(R.RENDERERS).sort(), ["button", "camera", "chip", "status", "talk"]);
  // a renderer that throws falls back, not up
  R.RENDERERS.boom = () => { throw new Error("x"); };
  assert.ok(R.renderElement(doc, el("boom"), {}).el.textContent.includes("Unknown kind"));
  delete R.RENDERERS.boom;
  console.log("PASS: the extension table takes a new kind with one renderer; a throwing renderer falls back");
}

// 8. parity with the card, the panel and the contract files
function literal(src, name) {
  const m = src.match(new RegExp(`\\nconst ${name} = ([\\[{][\\s\\S]*?\\n[\\]}]);`));
  assert.ok(m, `${name} not found`);
  return new Function("TALK_KIND_TEXT", `return (${m[1]});`)("");
}
{
  const card = read("../portico-card.js");
  const panel = read("../portico-panel.js");
  // the node runner copies only www/ and mounts the repo at /src; a plain checkout finds the files by relative path
  const contractFile = (name) => {
    const f = [`/src/contract/${name}`, new URL(`../../../../contract/${name}`, import.meta.url).pathname].find((x) => fs.existsSync(x));
    assert.ok(f, `contract/${name} is reachable from the test`);
    return JSON.parse(fs.readFileSync(f, "utf8"));
  };
  const contract = contractFile("palette_v1.json");
  const icons = contractFile("icons_v1.json");

  // palette: equal to the card's, the panel's and the contract's
  const cardPalette = literal(card, "PALETTE");
  assert.deepStrictEqual(R.PALETTE, cardPalette, "palette equals the card's");
  const panelPalette = literal(panel, "PALETTE");
  assert.deepStrictEqual(R.PALETTE, Object.fromEntries(panelPalette.map((c) => [c.name, { light: [c.light.fill, c.light.text], dark: [c.dark.fill, c.dark.text] }])), "palette equals the panel's");
  assert.deepStrictEqual(R.PALETTE, Object.fromEntries(contract.colors.map((c) => [c.name, { light: [c.light.fill, c.light.text], dark: [c.dark.fill, c.dark.text] }])), "palette equals contract/palette_v1.json");

  // default icon per action: equal to the panel's table, and the card's table is a subset of it
  const panelIcons = literal(panel, "KIND_ICON");
  assert.deepStrictEqual(R.KIND_ICON, panelIcons, "default icons equal the panel's");
  const cardIcons = literal(card, "KIND_ICON");
  for (const [k, v] of Object.entries(cardIcons)) assert.strictEqual(R.KIND_ICON[k], v, `card default icon ${k}`);

  // the ask-first set equals the panel's KINDS[...].confirm (minus call/talk, never asked)
  const kinds = literal(panel, "KINDS");
  assert.deepStrictEqual([...R.ASKS_FIRST].sort(), Object.keys(kinds).filter((k) => kinds[k].confirm).sort(), "ask-first set equals the panel's");

  // every icon the contract offers is accepted as an own icon; the default icons that the contract lists are named the same way
  for (const name of icons.icons) {
    assert.strictEqual(R.iconName({ kind: "button", icon: name, options: { action: "script_run" } }), `mdi:${name}`);
    assert.strictEqual(R.iconName({ kind: "button", icon: `mdi:${name}` }), `mdi:${name}`);
  }
  console.log("PASS: palette, default icons and ask-first set match the card, the panel and the contract files");
}

// 9. the tile the panel and the card draw for a button vs. this module's: same icon and same palette colours
{
  const card = read("../portico-card.js");
  // the card's own iconNameOf rule: a slug icon wins, otherwise the kind's default (same as iconName for a button)
  const m = card.match(/function iconNameOf\(action\) \{([\s\S]*?)\n\}/);
  assert.ok(m, "iconNameOf not found");
  const iconNameOf = new Function("KIND_ICON", `return function (action) {${m[1]}\n};`)(literal(card, "KIND_ICON"));
  for (const action of Object.keys(literal(card, "KIND_ICON"))) {
    for (const icon of [undefined, "gate", "mdi:lock", "Bad Icon", "x\"><b>"]) {
      assert.strictEqual(R.iconName({ kind: "button", icon, options: { action } }), iconNameOf({ kind: action, icon }), `${action} / ${icon}`);
    }
  }
  console.log("PASS: icon choice for a button equals the card's iconNameOf for every shared action");
}

// 9b. DEV-518: a Talk element draws an inert handset button
{
  const h = R.renderElement(doc, el("talk", { name: "Gate talk", icon: "phone", options: { door: "gate-sip-server" } }));
  const b = h.el.querySelector("button");
  assert.ok(b.disabled && b.dataset.action === "talk", "inert, never dials");
  assert.strictEqual(b.querySelector("ha-icon").getAttribute("icon"), "mdi:phone");
  assert.strictEqual(b.textContent, "Gate talk");
  assert.strictEqual(R.iconName({ kind: "talk" }), "mdi:phone", "no icon of its own: a handset");
  assert.ok(!h.el.querySelector(".pe-asks"), "never ask-first");
  console.log("PASS: a Talk element renders as an inert handset button");
}

// 10. no HTML-string setter in the module (the repo's XSS-safe pattern; the grep test)
{
  const src = read("../portico-elements-render.js").split("\n").filter((l) => !l.trim().startsWith("//")).join("\n");
  for (const re of [/innerHTML/, /outerHTML/, /insertAdjacentHTML/, /document\.write/, /\bsrcdoc\b/, /createContextualFragment/, /DOMParser/, /\beval\s*\(/, /new Function/, /setAttribute\(\s*["']on/i]) {
    assert.ok(!re.test(src), `forbidden pattern ${re} in portico-elements-render.js`);
  }
  assert.ok(!/addEventListener\(\s*["'](?!click|error|keydown|icegatheringstatechange|loadeddata)/.test(src), "only click, error, keydown, icegatheringstatechange and loadeddata listeners exist");
  assert.ok(!/\b(callService|callWS|fetch|XMLHttpRequest)\b/.test(src), "no service call, no one-shot WS call, no network fetch (the live window only subscribes camera/webrtc/offer and asks portico/camera/webrtc_offer)");
  assert.strictEqual((src.match(/\bsetInterval\b/g) || []).length, 2, "the full-size window has two intervals: the still-fallback refresh and the live fps poll");
  assert.strictEqual((src.match(/\bsubscribeMessage\b/g) || []).filter(Boolean).length >= 1, true);
  assert.ok(!/type:\s*"(?!camera\/webrtc\/(offer|candidate)|portico\/camera\/webrtc_offer)[a-z_]+\/[a-z_\/]+"/.test(src), "the only HA message types are camera/webrtc/offer, camera/webrtc/candidate and portico/camera/webrtc_offer");
  console.log("PASS: no HTML-string setter, no service call or fetch; the live window sends only camera/webrtc/offer and /candidate");
}

{
  const doc = new JSDOM("<!doctype html><body></body>").window.document;
  const st = { "camera.a": { state: "idle", attributes: { entity_picture: "/api/camera_proxy/camera.a?token=1" } }, "camera.b": { state: "idle", attributes: {} } };
  const one = R.renderPreview(doc, { id: "e1", kind: "camera", name: "A", entity_id: "camera.a", options: {} }, { states: st });
  assert.ok(!one.el.querySelector(".pe-pair"), "a one-stream camera is unchanged");
  const two = R.renderPreview(doc, { id: "e2", kind: "camera", name: "A", entity_id: "camera.a", options: { small_entity_id: "camera.b" } }, { states: st });
  assert.strictEqual(two.el.querySelectorAll(".pe-pair-cell").length, 2);
  assert.strictEqual(two.el.querySelectorAll("img").length, 1, "Small has no picture here: its tile says so, Big still draws");
  assert.match(two.el.querySelector('[data-stream="small"]').textContent, /No picture/);
  two.update({ ...st, "camera.b": { state: "idle", attributes: { entity_picture: "/api/camera_proxy/camera.b?token=2" } } });
  assert.strictEqual(two.el.querySelectorAll("img").length, 2, "update reaches the Small tile");
  two.destroy();
  console.log("PASS: renderPreview draws one labelled picture per stream, leaves a one-stream camera alone");
}

// full-size camera window
{
  const dom2 = new JSDOM("<!doctype html><body><div id=h></div></body>", { pretendToBeVisual: true });
  const doc = dom2.window.document;
  const st = { "camera.a": { state: "idle", attributes: { entity_picture: "/api/camera_proxy/camera.a?token=1" } }, "camera.b": { state: "idle", attributes: { entity_picture: "/api/camera_proxy/camera.b?token=2" } } };
  const two = R.renderPreview(doc, { id: "e2", kind: "camera", name: "Yard", entity_id: "camera.a", options: { small_entity_id: "camera.b" } }, { states: st });
  doc.getElementById("h").append(two.el);
  const small = two.el.querySelector('[data-stream="small"] img');
  assert.strictEqual(small.getAttribute("role"), "button");
  assert.strictEqual(small.tabIndex, 0);
  assert.match(small.getAttribute("aria-label"), /Open Yard \(Small\) full size/);
  small.focus();
  small.dispatchEvent(new dom2.window.KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  const dlg = doc.querySelector('[role="dialog"][aria-modal="true"]');
  assert.ok(dlg, "Enter opens the window");
  await new Promise((r) => setTimeout(r, 20));    // no hass here: the live attempt fails at once and the still fallback shows
  assert.match(dlg.querySelector(".pe-sub").textContent, /NOT live: no live connection/, "the dialog says plainly that it is a still");
  assert.ok(dlg.querySelector("img").getAttribute("src").startsWith("/api/camera_proxy/camera.b?token=2&_="), "opens on the clicked stream");
  assert.deepStrictEqual([...dlg.querySelectorAll(".pe-viewer-switch")].map((b) => b.textContent), ["Big", "Small"]);
  dlg.querySelector('[data-stream="big"]').click();
  await new Promise((r) => setTimeout(r, 20));
  assert.ok(dlg.querySelector("img").getAttribute("src").startsWith("/api/camera_proxy/camera.a?"), "switch to Big");
  two.update({ ...st, "camera.a": { state: "idle", attributes: { entity_picture: "/api/camera_proxy/camera.a?token=NEW" } } });
  await new Promise((r) => setTimeout(r, R.VIEWER_REFRESH_MS + 300));
  assert.ok(dlg.querySelector("img").getAttribute("src").includes("token=NEW"), "the refresh reads the current picture token");
  doc.dispatchEvent(new dom2.window.KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
  assert.ok(!doc.querySelector('[role="dialog"]'), "Escape closes");
  assert.strictEqual(doc.activeElement, small, "focus returns to the picture");
  small.click();
  const back = doc.querySelector(".pe-viewer-backdrop");
  assert.ok(back);
  back.querySelector(".pe-viewer").click();
  assert.ok(doc.querySelector(".pe-viewer-backdrop"), "a click inside does not close");
  back.click();
  assert.ok(!doc.querySelector(".pe-viewer-backdrop"), "backdrop click closes");
  const one = R.renderPreview(doc, { id: "e1", kind: "camera", name: "Solo", entity_id: "camera.a", options: {} }, { states: st });
  doc.getElementById("h").append(one.el);
  one.el.querySelector("img").click();
  await new Promise((r) => setTimeout(r, 20));
  assert.strictEqual(doc.querySelectorAll(".pe-viewer-switch").length, 0, "one stream: no switch");
  doc.querySelector(".pe-viewer-close").click();
  assert.ok(!doc.querySelector(".pe-viewer-backdrop"), "Close button closes");
  console.log("PASS: clicking a camera picture opens the full-size window (switch, refresh, Escape, backdrop, Close, focus back)");
}

// full-size window: the REAL live stream over camera/webrtc/offer
{
  const dom3 = new JSDOM("<!doctype html><body><div id=h></div></body>", { pretendToBeVisual: true });
  const win = dom3.window;
  const doc = win.document;
  const made = [];
  class FakePC {
    constructor() { this.iceGatheringState = "complete"; this.connectionState = "new"; this.closed = false; this.remote = null; made.push(this); }
    addTransceiver(kind, o) { this.tr = [kind, o]; }
    addEventListener() {}
    async createOffer() { return { type: "offer", sdp: "v=0 OFFER" }; }
    async setLocalDescription(d) { this.localDescription = d; }
    async setRemoteDescription(d) { this.remote = d; this.remoteDescription = d; }
    async addIceCandidate(c) { (this.ice = this.ice || []).push(c); }
    close() { this.closed = true; }
    async getStats() { const f = (this.fakeFrames = (this.fakeFrames || 0) + 25); return new Map([["a", { type: "inbound-rtp", kind: "video", framesDecoded: f }], ["b", { type: "inbound-rtp", kind: "audio", framesDecoded: 99999 }]]); }
  }
  win.RTCPeerConnection = FakePC;
  win.HTMLMediaElement.prototype.play = function () { this._played = true; return Promise.resolve(); };
  win.HTMLMediaElement.prototype.pause = function () {};
  const subs = [];
  let unsubbed = 0;
  const sent = [];
  const hass = { connection: {
    subscribeMessage: async (cb, msg, opts) => { subs.push({ cb, msg, opts }); return () => { unsubbed++; }; },
    sendMessagePromise: async (m) => { sent.push(m); return {}; },
  } };
  const st = { "camera.a": { state: "idle", attributes: { entity_picture: "/api/camera_proxy/camera.a?token=1" } }, "camera.b": { state: "idle", attributes: { entity_picture: "/api/camera_proxy/camera.b?token=2" } } };
  const tick = () => new Promise((r) => setTimeout(r, 20));
  const two = R.renderPreview(doc, { id: "e2", kind: "camera", name: "Yard", entity_id: "camera.a", options: { small_entity_id: "camera.b" } }, { states: st, getHass: () => hass });
  doc.getElementById("h").append(two.el);
  two.el.querySelector('[data-stream="big"] img').click();
  await tick();
  assert.strictEqual(subs.length, 1, "one subscription");
  assert.deepStrictEqual({ type: subs[0].msg.type, entity_id: subs[0].msg.entity_id, offer: subs[0].msg.offer }, { type: "camera/webrtc/offer", entity_id: "camera.a", offer: "v=0 OFFER" });
  assert.deepStrictEqual(made[0].tr, ["video", { direction: "recvonly" }]);
  const dlg = doc.querySelector('[role="dialog"]');
  const video = dlg.querySelector("video");
  assert.ok(!video.hidden, "the video element is the picture");
  assert.ok(!dlg.querySelector("img").hidden && dlg.querySelector("img").getAttribute("src").startsWith("/api/camera_proxy/camera.a?"), "the latest still shows while live is starting");
  assert.strictEqual(video.style.opacity, "0", "the connecting video is stashed invisible, not shown black");
  assert.match(dlg.querySelector(".pe-sub").textContent, /connecting to the live stream/);
  assert.deepStrictEqual(sent[0], { type: "portico/camera/webrtc_offer", entity_id: "camera.a", sdp: "v=0 OFFER" }, "Frigate is asked first (an empty reply falls back to HA)");
  subs[0].cb({ type: "session", session_id: "S1" });
  subs[0].cb({ type: "answer", answer: "v=0 ANSWER" });
  subs[0].cb({ type: "candidate", candidate: { candidate: "candidate:1 1 udp 1 1.2.3.4 5 typ host", sdpMid: "0", sdpMLineIndex: 0 } });
  await tick();
  assert.deepStrictEqual(made[0].remote, { type: "answer", sdp: "v=0 ANSWER" });
  assert.strictEqual(made[0].ice.length, 1);
  const stream = { id: "ms" };
  made[0].ontrack({ streams: [stream] });
  assert.strictEqual(video.srcObject, stream, "the track feeds the video element");
  made[0].connectionState = "connected";
  made[0].onconnectionstatechange();
  assert.match(dlg.querySelector(".pe-sub").textContent, /waiting for the camera.s first picture/, "connected is not live until a frame is decoded");
  video.dispatchEvent(new win.Event("loadeddata"));
  assert.match(dlg.querySelector(".pe-sub").textContent, /camera\.a - live stream \(via HA\)$/);
  assert.ok(dlg.querySelector("img").hidden && dlg.querySelector("img").style.display === "none", "live hides the still (display too, not just the attribute)");
  assert.strictEqual(video.style.opacity, "", "and shows the video");
  assert.ok(!/still|NOT live/.test(dlg.querySelector(".pe-sub").textContent));
  // the fps label: shown only while live, '--' until 2 s are watched, then the decoded rate (25 frames per poll = 25 fps)
  const fps = dlg.querySelector(".pe-viewer-fps");
  assert.ok(fps && !fps.hidden && fps.textContent === "--", "label shows -- right after going live");
  Object.defineProperty(video, "videoWidth", { value: 1600 });
  Object.defineProperty(video, "videoHeight", { value: 1200 });
  await new Promise((r) => setTimeout(r, 3600));
  assert.strictEqual(fps.textContent, "1600x1200 25 fps", "resolution and measured rate: " + fps.textContent);
  // local candidates are sent once the session id is known
  made[0].onicecandidate({ candidate: { toJSON: () => ({ candidate: "candidate:9", sdpMid: "0", sdpMLineIndex: 0 }) } });
  assert.deepStrictEqual(sent.filter((m) => m.type === "camera/webrtc/candidate")[0], { type: "camera/webrtc/candidate", entity_id: "camera.a", session_id: "S1", candidate: { candidate: "candidate:9", sdpMid: "0", sdpMLineIndex: 0 } });
  // the Small switch ends that session and starts another
  dlg.querySelector('[data-stream="small"]').click();
  await tick();
  assert.ok(made[0].closed && unsubbed === 1, "the first session is closed and unsubscribed");
  assert.strictEqual(subs[1].msg.entity_id, "camera.b");
  // an error from HA drops to the labelled still
  subs[1].cb({ type: "error", code: "webrtc_offer_failed", message: "no stream" });
  assert.match(dlg.querySelector(".pe-sub").textContent, /NOT live: no stream\. Showing a still picture/);
  assert.ok(video.hidden && video.style.display === "none" && !dlg.querySelector("img").hidden && dlg.querySelector("img").style.display === "block");
  assert.ok(fps.hidden, "the label is hidden once the stream is off (still fallback)");
  doc.querySelector(".pe-viewer-close").click();
  assert.ok(made[1].closed && unsubbed === 2, "Close ends the session");
  console.log("PASS: the full-size window negotiates a real WebRTC live stream (offer/answer/candidates), switches streams, and falls back to a labelled still on error");
}
// the steadied fps meter (port of the app's FpsMeter)
{
  const m = R.createFpsMeter();
  assert.strictEqual(m.label(), "--");
  m.sample(0, 0); m.sample(1000, 25);
  assert.strictEqual(m.label(), "--", "under 2 s watched");
  m.sample(2000, 50);
  assert.strictEqual(m.label(), "25 fps");
  m.sample(3000, 75); m.sample(4000, 101); m.sample(5000, 126);   // a 26 fps burst second moves the 4 s average < 1.0: stays 25
  assert.strictEqual(m.label(), "25 fps", "hysteresis holds the shown number");
  for (let i = 1; i <= 6; i++) m.sample(5000 + i * 1000, 126 + i * 30);
  assert.strictEqual(m.label(), "30 fps", "a full fps away moves it");
  m.sample(12000, 306); m.sample(14000, 306);
  assert.strictEqual(m.label(), "0 fps", "no frame for 2 s");
  m.sample(15000, 336); m.sample(17000, 396);
  assert.strictEqual(m.label(), "30 fps", "recovers");
  m.sample(18000, 5);
  assert.strictEqual(m.label(), "--", "a counter that went back restarts");
  console.log("PASS: createFpsMeter steadies the fps readout (window, hysteresis, stall, reset)");
}

// full-size window: Frigate's go2rtc answers first; every failure falls back to HA's own offer
{
  const dom4 = new JSDOM("<!doctype html><body><div id=h></div></body>", { pretendToBeVisual: true });
  const win = dom4.window;
  const doc = win.document;
  const made = [];
  class FakePC {
    constructor() { this.iceGatheringState = "complete"; this.connectionState = "new"; this.closed = false; made.push(this); }
    addTransceiver() {}
    addEventListener() {}
    async createOffer() { return { type: "offer", sdp: "v=0 OFFER" }; }
    async setLocalDescription(d) { this.localDescription = d; }
    async setRemoteDescription(d) { if (this.refuse) throw new Error("bad sdp"); this.remote = d; this.remoteDescription = d; }
    async addIceCandidate() {}
    close() { this.closed = true; }
  }
  win.RTCPeerConnection = FakePC;
  win.HTMLMediaElement.prototype.play = function () { return Promise.resolve(); };
  win.HTMLMediaElement.prototype.pause = function () {};
  const subs = [];
  const asked = [];
  let reply = async () => ({ answer: "v=0 FRIGATE" });
  const hass = { connection: {
    subscribeMessage: async (cb, msg) => { subs.push({ cb, msg }); return () => {}; },
    sendMessagePromise: async (m) => { asked.push(m); return reply(m); },
  } };
  const st = { "camera.f": { state: "idle", attributes: { entity_picture: "/api/camera_proxy/camera.f?token=1" } } };
  const tick = () => new Promise((r) => setTimeout(r, 20));
  const open = async (id) => {
    const r = R.renderPreview(doc, { id: "x" + id, kind: "camera", name: "Cam", entity_id: id }, { states: { [id]: st["camera.f"] }, getHass: () => hass });
    doc.getElementById("h").append(r.el);
    r.el.querySelector("img").click();
    await tick();
    return doc.querySelector('[role="dialog"]');
  };
  const connect = (pc, dlg) => {
    pc.ontrack({ streams: [{ id: "ms" }] });
    pc.connectionState = "connected";
    pc.onconnectionstatechange();
    dlg.querySelector("video").dispatchEvent(new win.Event("loadeddata"));
  };
  // 1. Frigate answers: no HA subscription at all, the note says so
  let dlg = await open("camera.f");
  assert.strictEqual(subs.length, 0, "Frigate answered: HA's camera/webrtc/offer is never subscribed");
  assert.deepStrictEqual(asked[0], { type: "portico/camera/webrtc_offer", entity_id: "camera.f", sdp: "v=0 OFFER" });
  assert.deepStrictEqual(made[0].remote, { type: "answer", sdp: "v=0 FRIGATE" });
  assert.strictEqual(dlg.querySelector(".pe-sub").dataset.via, "frigate");
  assert.match(dlg.querySelector(".pe-sub").textContent, /connecting to the live stream \(via Frigate\)/);
  connect(made[0], dlg);
  assert.match(dlg.querySelector(".pe-sub").textContent, /camera\.f - live stream \(via Frigate\)$/);
  doc.querySelector(".pe-viewer-close").click();
  // 2. not a Frigate stream: HA's offer is used, and the next open skips the ask
  reply = async () => { const e = new Error("no"); e.code = "not_frigate_stream"; throw e; };
  dlg = await open("camera.g");
  assert.strictEqual(subs.length, 1, "falls back to camera/webrtc/offer");
  assert.strictEqual(subs[0].msg.entity_id, "camera.g");
  assert.match(dlg.querySelector(".pe-sub").textContent, /\(via HA\)/);
  subs[0].cb({ type: "answer", answer: "v=0 HA" });
  await tick();
  connect(made[made.length - 1], dlg);
  assert.match(dlg.querySelector(".pe-sub").textContent, /camera\.g - live stream \(via HA\)$/);
  doc.querySelector(".pe-viewer-close").click();
  const before = asked.length;
  dlg = await open("camera.g");
  assert.strictEqual(asked.length, before, "a not_frigate_stream entity is not asked again");
  assert.strictEqual(subs.length, 2);
  doc.querySelector(".pe-viewer-close").click();
  // 3. any other error (Frigate down, a thrown call) falls back too
  for (const [id, fn] of [["camera.h", async () => { const e = new Error("x"); e.code = "frigate_failed"; throw e; }], ["camera.i", async () => ({})], ["camera.j", () => { throw new Error("sync"); }]]) {
    reply = fn;
    const n = subs.length;
    dlg = await open(id);
    assert.strictEqual(subs.length, n + 1, `${id}: HA's offer is used after a Frigate failure`);
    assert.match(dlg.querySelector(".pe-sub").textContent, /\(via HA\)/);
    doc.querySelector(".pe-viewer-close").click();
  }
  // 4. an answer the browser refuses falls back to HA's offer on the same connection
  reply = async () => ({ answer: "v=0 BAD" });
  const nBefore = subs.length;
  const prevCtor = win.RTCPeerConnection;
  win.RTCPeerConnection = class extends FakePC { constructor() { super(); this.refuse = true; } };
  dlg = await open("camera.k");
  assert.strictEqual(subs.length, nBefore + 1, "a refused Frigate answer falls back to HA's");
  assert.match(dlg.querySelector(".pe-sub").textContent, /\(via HA\)/);
  doc.querySelector(".pe-viewer-close").click();
  win.RTCPeerConnection = prevCtor;
  console.log("PASS: the live window asks Frigate first (note: via Frigate) and falls back to HA's offer on not_frigate_stream, an error, an empty or refused answer (note: via HA)");
}
process.exit(0);
