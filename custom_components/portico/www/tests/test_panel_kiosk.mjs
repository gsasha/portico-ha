// M6 K3: the kiosk route of the panel (/portico/kiosk/<slug>): it draws the card and nothing else, says hello,
// degrades with a clear line when the card or the server part is missing. Protocol-level; NOT a visual check.
import assert from "assert";
import { loadPanel, makeHass, editorHass, sleep } from "./panel_loader.mjs";

const q = (p, sel) => p.shadowRoot.querySelector(sel);
const text = (p) => p.shadowRoot.textContent;
const kids = (p) => Array.from(p.shadowRoot.children).map((n) => n.localName);

/* A window with a stand-in for K2's card: records how it was configured. */
function world({ card = true, url } = {}) {
  const w = loadPanel({ url });
  const win = w.dom.window;
  const defineCard = () => {
    win.customElements.define("portico-card", class extends win.HTMLElement {
      setConfig(c) { this.config = c; }
      set hass(h) { this.hassSeen = h; this.hassSets = (this.hassSets || 0) + 1; }
    });
  };
  if (card) defineCard();
  return { ...w, win, defineCard };
}

const HELLO_OK = { user: { id: "u2", name: "Kitchen" }, declared: ["kitchen"], endpoint_ok: true };
const helloFake = (reply = HELLO_OK, opts = {}) => makeHass({ "portico/kiosk/hello": typeof reply === "function" ? reply : () => reply }, { user: { is_admin: false }, ...opts });

/* The panel imports the card file itself through panel._importCard(url); a test replaces that (jsdom has no module loader). */
async function mount(w, fake, route, { cardWaitMs = 30, importCard } = {}) {
  const panel = w.doc.createElement("portico-panel");
  panel._cardWaitMs = cardWaitMs;
  w.imports = [];
  panel._importCard = (url) => { w.imports.push(url); return importCard ? importCard(url) : Promise.resolve(); };
  if (route !== undefined) panel.route = route;
  panel.hass = fake.hass;
  w.doc.body.appendChild(panel);
  await sleep(120);      // two card waits (30 ms each) when the card is missing: before and after the panel's own import
  return panel;
}
const KIOSK = { prefix: "/portico", path: "/kiosk/kitchen" };

// 1. the route parser (pure)
{
  const { Panel } = loadPanel();
  const T = Panel.logic.kioskTarget;
  assert.deepStrictEqual(T({ prefix: "/portico", path: "/kiosk/kitchen" }), { slug: "kitchen" });
  assert.deepStrictEqual(T({ prefix: "/portico", path: "/kiosk/kitchen/" }), { slug: "kitchen" });
  assert.deepStrictEqual(T({ prefix: "/portico", path: "/kiosk/wall_2-a" }), { slug: "wall_2-a" });
  assert.deepStrictEqual(T({ prefix: "/portico", path: "/kiosk" }), { slug: null }, "the kiosk route with no screen");
  assert.deepStrictEqual(T({ prefix: "/portico", path: "/kiosk/" }), { slug: null });
  assert.deepStrictEqual(T({ prefix: "/portico", path: "/kiosk/Bad Slug" }), { slug: null }, "not a valid address");
  assert.deepStrictEqual(T({ prefix: "/portico", path: "/kiosk/%E0%A4%A" }), { slug: null }, "a broken escape is not a crash");
  assert.deepStrictEqual(T({ prefix: "/portico", path: "/kiosk/a/b" }), null, "only one segment is a screen");
  assert.strictEqual(T({ prefix: "/portico", path: "" }), null);
  assert.strictEqual(T({ prefix: "/portico", path: "/other" }), null);
  assert.strictEqual(T(null, "/portico"), null);
  assert.deepStrictEqual(T(null, "/portico/kiosk/kitchen"), { slug: "kitchen" }, "no route yet: the browser's own path");
  assert.strictEqual(T(null, "/lovelace/kiosk/kitchen"), null, "another panel's path is not ours");
  assert.strictEqual(T(null, "/portico-other/kiosk/kitchen"), null);
  assert.strictEqual(T(null, undefined), null);
  console.log("PASS: the kiosk route is /portico/kiosk/<valid slug>; anything else is the editor");
  const L = Panel.logic.kioskLook;
  assert.deepStrictEqual(L(undefined), { bg: "none", backPath: "" }, "nothing configured: a plain page, no back route (DEV-836)");
  assert.deepStrictEqual(L({ background_url: "https://pics.example.com/a.jpg?v=1", back_path: "/lovelace/home" }),
                         { bg: 'url("https://pics.example.com/a.jpg?v=1")', backPath: "/lovelace/home" });
  assert.strictEqual(L({ background_url: "/local/bg.jpg" }).bg, 'url("/local/bg.jpg")', "a same-origin path is fine");
  for (const bad of ['javascript:alert(1)', '//evil.com/x.jpg', 'https://x/a")b', "https://x/a b", "data:image/png;base64,AA", 5, null])
    assert.strictEqual(L({ background_url: bad }).bg, "none", `background rejected: ${JSON.stringify(bad)}`);
  for (const bad of ["//evil", "http://x", "x/y", "/a b", "/\\evil", 5, null])
    assert.strictEqual(L({ back_path: bad }).backPath, "", `back path rejected: ${JSON.stringify(bad)}`);
  console.log("PASS: the kiosk look comes from the server and only a safe URL or route is used");
}

// 2. the kiosk page is the card and nothing else; hello first; no editor traffic
{
  const w = world();
  const fake = helloFake();
  const panel = await mount(w, fake, KIOSK);
  assert.deepStrictEqual(kids(panel), ["style", "portico-card"], "the style and the card: no header, no tabs, no wrapper");
  const card = q(panel, "portico-card");
  assert.strictEqual(card.getAttribute("endpoint"), "kitchen");
  assert.ok(card.hasAttribute("fullpage"));
  assert.deepStrictEqual(card.config, { type: "custom:portico-card", endpoint: "kitchen", takeover: true, fullpage: true });
  assert.strictEqual(card.hassSeen, fake.hass, "the card gets hass");
  assert.deepStrictEqual(fake.sent.map((m) => m.type), ["portico/kiosk/hello"], "hello only: no document, no subscription");
  assert.deepStrictEqual(fake.sent[0], { type: "portico/kiosk/hello", endpoint: "kitchen" });
  assert.strictEqual(fake.subscribes(), 0);
  assert.ok(panel.hasAttribute("kiosk"), "the host page is marked as the kiosk page");
  const css = q(panel, "style").textContent;
  assert.ok(/:host\(\[kiosk\]\) \{[^}]*var\(--portico-kiosk-bg, none\) center \/ cover no-repeat/.test(css), "the page behind the transparent card carries the configured background (no URL in code)");
  assert.strictEqual(q(panel, "header"), null);
  assert.strictEqual(q(panel, "button"), null);
  // a later hass reaches the card
  const next = { ...fake.hass, tick: 1 };
  panel.hass = next;
  assert.strictEqual(card.hassSeen, next);
  console.log("PASS: the kiosk route draws only <portico-card endpoint fullpage>, after kiosk/hello, with no editor traffic");
  panel.remove();
}

// 3. an administrator at the kiosk URL (the laptop check) gets the same page; the editor does not appear
{
  const w = world();
  const fake = helloFake(HELLO_OK, { user: { is_admin: true } });
  const panel = await mount(w, fake, KIOSK);
  assert.deepStrictEqual(kids(panel), ["style", "portico-card"]);
  assert.strictEqual(fake.ofType("portico/content/document").length, 0);
  console.log("PASS: an administrator opening the kiosk URL gets the screen, not the editor");
  panel.remove();
}

// 4. the kiosk is found from the browser's path when HA has not given a route; the editor path stays the editor
{
  const w = world({ url: "http://localhost/portico/kiosk/kitchen" });
  const panel = await mount(w, helloFake());
  assert.deepStrictEqual(kids(panel), ["style", "portico-card"]);
  panel.remove();

  const e = world();
  const fake = editorHass();
  const editor = await mount(e, fake, { prefix: "/portico", path: "" });
  assert.ok(q(editor, "header .tab"), "the editor at /portico");
  assert.strictEqual(fake.ofType("portico/kiosk/hello").length, 0);
  assert.strictEqual(q(editor, "portico-card"), null);
  console.log("PASS: the browser's path is the fallback for the route; /portico is the editor");
  editor.remove();
}

// 5. the card element is missing: a clear line, no chrome, no crash; and it mounts when it arrives within the wait
{
  const w = world({ card: false });
  const panel = await mount(w, helloFake(), KIOSK, { cardWaitMs: 30 });
  assert.deepStrictEqual(kids(panel), ["style", "div"]);
  assert.strictEqual(q(panel, "[data-kiosk]").dataset.kiosk, "message");
  assert.ok(text(panel).includes(w.Panel.logic.KIOSK_NO_CARD_TEXT));
  assert.strictEqual(q(panel, "button"), null);
  panel.remove();

  const late = world({ card: false });
  const p2 = await mount(late, helloFake(), KIOSK, { cardWaitMs: 500 });
  assert.strictEqual(q(p2, "[data-kiosk]").dataset.kiosk, "loading");
  late.defineCard();
  await sleep(30);
  assert.deepStrictEqual(kids(p2), ["style", "portico-card"], "the card arrived within the wait");
  console.log("PASS: a missing card element is a clear message; a card that loads late is still mounted");
  p2.remove();
}

// 5b. the card element is missing: the panel imports the card file itself (HA's own loader fails silently)
{
  const w = world({ card: false });
  const panel = await mount(w, helloFake(), KIOSK, { importCard: async () => { w.defineCard(); } });
  assert.strictEqual(w.imports.length, 1, "imported once");
  assert.ok(/^\/portico\/portico-card\.js\?v=\d+$/.test(w.imports[0]), "the card URL with a cache-busting value: " + w.imports[0]);
  assert.deepStrictEqual(kids(panel), ["style", "portico-card"], "the card is drawn after the panel loaded it");
  assert.strictEqual(q(panel, "portico-card").getAttribute("endpoint"), "kitchen");
  panel.remove();

  // the import throws: the real error and the URL are the message, still no chrome
  const bad = world({ card: false });
  const p2 = await mount(bad, helloFake(), KIOSK, { importCard: async () => { throw new TypeError("Failed to fetch dynamically imported module"); } });
  assert.deepStrictEqual(kids(p2), ["style", "div"]);
  assert.strictEqual(q(p2, "button"), null);
  const t = text(p2);
  assert.ok(t.includes("TypeError: Failed to fetch dynamically imported module"), t);
  assert.ok(t.includes(bad.imports[0]) && t.includes("/portico/portico-card.js?v="), "the URL tried is shown");
  assert.ok(!t.includes(bad.Panel.logic.KIOSK_NO_CARD_TEXT), "not the generic sentence");
  p2.remove();

  // the import throws "already defined" because HA's loader won the race: no error, the card is drawn
  const race = world({ card: false });
  const p3 = await mount(race, helloFake(), KIOSK, { importCard: async () => { race.defineCard(); throw new Error('the name "portico-card" has already been defined'); } });
  assert.deepStrictEqual(kids(p3), ["style", "portico-card"]);
  p3.remove();

  // the file loads but defines nothing: the generic sentence plus the URL
  const nodef = world({ card: false });
  const p4 = await mount(nodef, helloFake(), KIOSK, {});
  assert.strictEqual(nodef.imports.length, 1);
  assert.ok(text(p4).includes(nodef.Panel.logic.KIOSK_NO_CARD_TEXT) && text(p4).includes(nodef.imports[0]));
  p4.remove();

  // already defined: no import at all
  const have = world();
  const p5 = await mount(have, helloFake(), KIOSK, {});
  assert.strictEqual(have.imports.length, 0, "no import when the card is already defined");
  assert.deepStrictEqual(kids(p5), ["style", "portico-card"]);
  p5.remove();
  console.log("PASS: a missing card is imported by the panel; an import error shows its real text and URL; no import when defined");
}

// 6. the server says no: not declared, an old server part (unknown_command and an old-shaped reply), another error
{
  const w = world();
  const no = await mount(w, helloFake({ user: { id: "u2", name: "K" }, declared: [], endpoint_ok: false }), KIOSK);
  assert.deepStrictEqual(kids(no), ["style", "div"], "no card for a login the server does not know");
  assert.ok(text(no).includes('This login has no Portico screen "kitchen"'));
  no.remove();

  const old = await mount(world(), makeHass({}, { user: { is_admin: false } }), KIOSK);
  assert.ok(text(old).includes(world().Panel.logic.KIOSK_RESTART_TEXT), "unknown_command: restart Home Assistant");
  assert.strictEqual(q(old, "portico-card"), null);
  old.remove();

  const shape = await mount(world(), helloFake({ user: { id: "u2" }, declared: ["kitchen"] }), KIOSK);
  assert.ok(text(shape).includes("Portico is being updated"), "an old-shaped reply is the same line");
  shape.remove();

  const other = await mount(world(), helloFake(() => { throw { code: "home_assistant_error", message: "boom" }; }), KIOSK);
  assert.ok(text(other).includes("boom"));
  other.remove();
  console.log("PASS: not declared, unknown_command and an old-shaped reply each give a clear line and no card");
}

// 7. a missing or invalid address
{
  for (const path of ["/kiosk", "/kiosk/Bad Slug"]) {
    const w = world();
    const fake = helloFake();
    const panel = await mount(w, fake, { prefix: "/portico", path });
    assert.ok(text(panel).includes(w.Panel.logic.KIOSK_NO_SLUG_TEXT), path);
    assert.strictEqual(fake.sent.length, 0, "nothing is asked of the server");
    assert.strictEqual(q(panel, "portico-card"), null);
    panel.remove();
  }
  console.log("PASS: /portico/kiosk with no valid screen address says so and asks nothing");
}

// 8. navigating inside the panel between the editor and the kiosk, and between two screens
{
  const w = world();
  const fake = {
    ...editorHass({ "portico/kiosk/hello": (m) => ({ user: { id: "u", name: "n" }, declared: [m.endpoint], endpoint_ok: true }) }, { user: { is_admin: true } }),
  };
  const panel = await mount(w, fake, { prefix: "/portico", path: "" });
  assert.ok(q(panel, "header .tab"));
  panel.route = { prefix: "/portico", path: "/kiosk/kitchen" };
  await sleep(60);
  assert.deepStrictEqual(kids(panel), ["style", "portico-card"]);
  assert.strictEqual(q(panel, "portico-card").getAttribute("endpoint"), "kitchen");
  panel.route = { prefix: "/portico", path: "/kiosk/hall" };
  await sleep(60);
  assert.strictEqual(q(panel, "portico-card").getAttribute("endpoint"), "hall");
  assert.deepStrictEqual(fake.ofType("portico/kiosk/hello").map((m) => m.endpoint), ["kitchen", "hall"]);
  panel.route = { prefix: "/portico", path: "/kiosk/hall" };
  await sleep(30);
  assert.strictEqual(fake.ofType("portico/kiosk/hello").length, 2, "the same route again changes nothing");
  panel.route = { prefix: "/portico", path: "" };
  await sleep(60);
  assert.ok(q(panel, "header .tab"), "back to the editor");
  assert.strictEqual(q(panel, "portico-card"), null);
  console.log("PASS: moving between /portico and /portico/kiosk/<slug> switches the page");
  panel.remove();
}

// 9. hello is sent again on every connect (the server keeps who is open in memory only)
{
  const w = world();
  const fake = helloFake();
  const listeners = [];
  fake.hass.connection.addEventListener = (name, cb) => listeners.push([name, cb]);
  fake.hass.connection.removeEventListener = (name, cb) => { const i = listeners.findIndex((l) => l[0] === name && l[1] === cb); if (i >= 0) listeners.splice(i, 1); };
  const panel = await mount(w, fake, KIOSK);
  assert.deepStrictEqual(listeners.map((l) => l[0]), ["ready"]);
  listeners[0][1]();
  await sleep(10);
  assert.strictEqual(fake.ofType("portico/kiosk/hello").length, 2, "a reconnect says hello again");
  assert.deepStrictEqual(kids(panel), ["style", "portico-card"], "and the card stays");
  panel.remove();
  assert.deepStrictEqual(listeners, [], "the listener is removed with the page");
  console.log("PASS: kiosk/hello is re-sent when the connection is ready again, and the listener is released");
}

// 10. a page that was waiting for a restarted server tries again on the next connect
{
  const w = world();
  let up = false;
  const fake = helloFake(() => { if (!up) throw { code: "unknown_command", message: "Unknown command." }; return HELLO_OK; });
  let ready = null;
  fake.hass.connection.addEventListener = (_n, cb) => { ready = cb; };
  fake.hass.connection.removeEventListener = () => {};
  const panel = await mount(w, fake, KIOSK);
  assert.ok(text(panel).includes("Portico is being updated"));
  up = true;
  ready();
  await sleep(60);
  assert.deepStrictEqual(kids(panel), ["style", "portico-card"], "the screen comes back by itself after the restart");
  console.log("PASS: a kiosk waiting for the server part draws the screen once the server answers");
  panel.remove();
}

// 11. a card that refuses its settings does not take the page down
{
  const w = loadPanel();
  const win = w.dom.window;
  win.customElements.define("portico-card", class extends win.HTMLElement {
    setConfig() { throw new Error("no endpoint"); }
  });
  const panel = await mount({ ...w, win }, helloFake(), KIOSK);
  assert.ok(text(panel).includes("The Portico card did not accept its settings: no endpoint"));
  console.log("PASS: a card that rejects its configuration is a message");
  panel.remove();
}

// 12. the contract probe on the kiosk route: contract 3 is probed once, then hello; another contract is one line and no hello
{
  const w = world();
  const ok = helloFake();
  const p1 = await mount(w, ok, KIOSK);
  assert.deepStrictEqual(ok.probes, [{ type: "portico/version" }]);
  assert.deepStrictEqual(kids(p1), ["style", "portico-card"]);
  p1.remove();
  const w2 = world();
  const old = helloFake(HELLO_OK, {});
  const f = makeHass({ "portico/version": () => ({ contract: 2 }), "portico/kiosk/hello": () => HELLO_OK }, { user: { is_admin: false } });
  const p2 = await mount(w2, f, KIOSK);
  assert.ok(text(p2).includes("needs Portico contract 3"));
  assert.strictEqual(f.sent.length, 0, "no hello to a server on another contract");
  assert.ok(!q(p2, "portico-card"), "no card");
  void old;
  console.log("PASS: the kiosk route probes the contract and says so when it is not 3");
  p2.remove();
}

console.log("ALL_PANEL_KIOSK_TESTS_PASSED");
