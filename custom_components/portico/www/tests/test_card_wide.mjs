// The wide-screen layout (main camera 2/3, other cameras at the right, buttons at the bottom) and the kiosk back
// rail (the tablet's standard left-edge back control, replacing the old Close "x"). jsdom has no layout engine, so: the DECISIONS and the ARITHMETIC are pure functions tested with numbers,
// and the DOM and CSS are tested as text. NOT a visual check (see test_card_visual.html ?wide).
import assert from "assert";
import crypto from "crypto";
import { loadCard, sleep, makeHass, CONTENT } from "./card_loader.mjs";

const { dom, PorticoCard } = loadCard();
const doc = dom.window.document;
const sha = (t) => crypto.createHash("sha256").update(t).digest("hex").slice(0, 16);

const cam = (id, size, extra = {}) => ({ id, label: id.toUpperCase(), entity_id: `camera.${id}`, size, available: true, name: id, ...extra });
const act = (id, extra = {}) => ({ id, label: `L-${id}`, kind: "script_run", entity_id: `script.${id}`, confirm: false, available: true, name: id, ...extra });
const blk = (id, title, cameras = [], actions = []) => ({ id, title, cameras, actions });
const withBlocks = (blocks) => ({ ...CONTENT, pages: [{ id: "home", label: "Home", blocks }] });
const q = (card, sel) => card.shadowRoot.querySelector(sel);
const qa = (card, sel) => Array.from(card.shadowRoot.querySelectorAll(sel));

async function mount(config, content, box) {
  const fake = makeHass({ doors: [], content });
  const card = doc.createElement("portico-card");
  doc.body.appendChild(card);
  card.setConfig({ _pollMs: 20, _snapshotMs: 30, ...config });
  if (box) card._box = { w: box[0], h: box[1] }; // what the ResizeObserver reports (jsdom has no layout)
  card.hass = fake.hass;
  await sleep(40);
  return { card, fake };
}

const RAIL = PorticoCard.W_RAIL;
const SIZES = [[1280, 800], [1920, 1200], [1024, 600], [800, 480]];
const geo = (w, h, nCams, nButtons, extra = {}) => PorticoCard.wideGeometry({
  w, h, fullpage: true, nMain: nCams ? 1 : 0, nSide: Math.max(0, nCams - 1), nButtons, hasTitle: true, extras: 0, ...extra });

async function main() {
  // 1. the wide/narrow switch
  {
    const d = PorticoCard.wideDecision;
    assert.strictEqual(d(1280, 800, true), true);
    assert.strictEqual(d(720, 400, true), true, "720 is the threshold, inclusive");
    assert.strictEqual(d(719, 400, true), false);
    assert.strictEqual(d(800, 1280, true), false, "a portrait fullpage screen stays narrow");
    assert.strictEqual(d(800, 800, true), false, "square is not wider than tall");
    assert.strictEqual(d(900, 3000, false), true, "a Lovelace card: width alone (its height is its content)");
    assert.strictEqual(d(500, 100, false), false);
    assert.strictEqual(d(0, 0, true), false, "no box yet (jsdom, first paint) is narrow");
    console.log("PASS: wide switch: >= 720px and (fullpage: wider than tall); a card's height never counts; no box is narrow");
  }

  // 2. the arithmetic fits every supported tablet, for 1, 3 and 5 camera blocks, with the back rail's width reserved
  {
    for (const [w, h] of SIZES) {
      for (const n of [1, 3, 5]) {
        for (const nb of [0, 3, 8]) {
          const g = geo(w, h, n, nb);
          const tag = `${w}x${h} ${n} cams ${nb} buttons`;
          assert.ok(g.height <= h + 0.001, `${tag}: total ${g.height} > ${h}`);
          assert.ok(g.groupW <= w - RAIL - 16 + 0.001, `${tag}: group wider than the card minus the rail`);
          // the group is centred in the space right of the rail: its left edge is never left of the rail's reserved width
          assert.ok(RAIL + (w - RAIL - 16 - g.groupW) / 2 >= RAIL - 0.001, `${tag}: group starts left of the rail's edge`);
          assert.ok(Math.abs(g.mh - g.mw * 9 / 16) < 0.001, `${tag}: main keeps 16:9`);
          if (n > 1) {
            assert.ok(g.sideTotal <= g.mh + 24 + 0.001, `${tag}: side column ${g.sideTotal} taller than the main tile`);
            assert.ok(g.sh > 0, `${tag}: side cameras visible`);
            assert.ok(Math.abs(g.sideW - g.sh * 16 / 9) < 0.001, `${tag}: side cameras keep 16:9`);
            assert.ok(g.sw <= g.mw / 2 + 0.001 && Math.abs(g.sw - g.sideW) < 0.001);
          } else {
            assert.strictEqual(g.sw, 0);
          }
        }
      }
    }
    console.log("PASS: sizing: main + side + buttons + title fit with the rail beside them at 1280x800, 1920x1200, 1024x600, 800x480 for 1/3/5 cameras");
  }

  // 3. proportions: 2/3 for the main camera when width-limited; evenly shared side column
  {
    const g = geo(1280, 800, 3, 4);
    assert.ok(Math.abs(g.mw - (1280 - RAIL - 16 - 10) * 2 / 3) < 0.001, "main is 2/3 of the width");
    const g2 = geo(1280, 800, 2, 4);
    assert.ok(Math.abs(g2.sw - g2.mw / 2) < 0.001 && Math.abs(g2.groupW - (1280 - RAIL - 16)) < 0.001, "one side camera: the remaining third, group fills the width");
    assert.ok(g.sw < g.mw / 2 && Math.abs(g.sw - g.sideW) < 0.001, "two or more side cameras: the column hugs them, never wider than a third");
    const g5 = geo(800, 480, 5, 3);
    const two = geo(800, 480, 2, 3);
    assert.ok(g5.sh < two.sh, "more cameras scale each down");
    assert.ok(Math.abs(g5.sideTotal - (g5.mh + 24)) < 0.5, "four side cameras share the main tile's height evenly");
    const solo = geo(1280, 800, 1, 4);
    assert.ok(solo.mw > g.mw, "a lone camera takes the whole width");
    console.log("PASS: sizing: main = 2/3, side = 1/3, side cameras share the main height evenly, a lone camera takes the width");
  }

  // 4. a short screen is limited by HEIGHT, not width, and the group is still inside the screen
  {
    const g = geo(1920, 600, 3, 4);
    assert.ok(g.mw < (1920 - RAIL - 16 - 10) * 2 / 3, "height-limited: narrower than 2/3 of the width");
    assert.ok(g.height <= 600.001);
    const tall = geo(1920, 1200, 3, 4);
    assert.ok(Math.abs(tall.mw - (1920 - RAIL - 16 - 10) * 2 / 3) < 0.001, "tall enough: width-limited");
    console.log("PASS: sizing: main is limited by the available height on a short screen, by the width otherwise");
  }

  // 5. later blocks in fullpage: all drawn below the first screen (the page scrolls); the first block is fitted alone
  {
    const a = geo(1280, 800, 3, 4, { extras: 2 });
    assert.strictEqual(a.extrasShown, 2);
    assert.ok(a.height <= 800.001);
    const b = geo(800, 480, 3, 4, { extras: 6 });
    assert.strictEqual(b.extrasShown, 6, "no later block is dropped for lack of height");
    assert.ok(b.height <= 480.001, "the first screen still fits");
    const c = geo(800, 480, 3, 4, { extras: 0 });
    assert.ok(c.extrasShown === 0 && c.height <= 480.001);
    assert.ok(b.btnH === c.btnH && b.btnRows >= 1, "the first block's button rows are always reserved");
    assert.ok(c.mw >= b.mw, "with nothing below, the camera takes the whole screen; with more below it leaves a peek");
    const m = PorticoCard.wideMoreGeometry(1000);
    assert.ok(m.cols === 3 && m.tw >= (1000 - 20) / 3 - 1 && m.th === Math.round(m.tw * 9 / 16), "tiles: three per row, 16:9");
    console.log("PASS: sizing: later blocks are all shown below the first screen, in rows of three 16:9 tiles; the first block's buttons always have their rows");
  }

  // 6. the back path rule (pure) and the layered way out
  {
    const v = PorticoCard.validBackPath;
    assert.strictEqual(v("/kitchen-panel"), "/kitchen-panel");
    assert.strictEqual(v("/kitchen-panel/screen"), "/kitchen-panel/screen");
    assert.strictEqual(v("/"), "/");
    assert.strictEqual(v("/a/b?x=1#y"), "/a/b?x=1#y");
    for (const bad of ["//evil", "//evil.com/x", "http://x", "https://x/y", "javascript:alert(1)", "", "kitchen-panel", "x/y",
                       "/\\evil", "/ok path", "/a\nb", "/a\tb", "\\\\host", " /x", "/x\u0000", null, undefined, 5, {}, "/" + "a".repeat(600)]) {
      assert.strictEqual(v(bad), null, `rejected: ${JSON.stringify(bad)}`);
    }
    const t = PorticoCard.backTarget;
    assert.strictEqual(PorticoCard.DEFAULT_BACK_PATH, "", "no estate route is built in (DEV-836)");
    assert.strictEqual(t({}), null, "no key: no back path, the layered fallback");
    assert.strictEqual(t(undefined), null);
    assert.strictEqual(t({ back_path: "/lovelace/kitchen" }), "/lovelace/kitchen");
    assert.strictEqual(t({ back_path: "" }), null, "empty: no back path, the layered fallback");
    assert.strictEqual(t({ back_path: null }), null);
    assert.strictEqual(t({ back_path: "//evil" }), null, "an invalid value is never navigated to, and never silently replaced");
    assert.strictEqual(t({ back_path: "http://x" }), null);
    assert.strictEqual(PorticoCard.closeAction(true, 5), "fully");
    assert.strictEqual(PorticoCard.closeAction(true, 1), "fully");
    assert.strictEqual(PorticoCard.closeAction(false, 2), "back");
    assert.strictEqual(PorticoCard.closeAction(false, 1), "home");
    assert.strictEqual(PorticoCard.closeAction(false, 0), "home");
    console.log("PASS: back path: one leading '/', never //host, a scheme, a backslash or a control character; default and off; layered way out unchanged");
  }

  // 7. NARROW is the same drawing as before (hashes re-pinned for live video: the tiles gained the tap/live attributes and the card the live CSS; nothing else moved)
  {
    const content = withBlocks([blk("b1", "Entrance", [cam("gate", "big"), cam("yard", "small"), cam("lawn", "small")],
      [act("go", { color: "green", icon: "door" }), act("lights", { show_label: false })]), blk("b2", "Back", [cam("x", "big")], [act("y")])]);
    const { card } = await mount({}, content);
    const sections = qa(card, "section.block").map((s) => s.outerHTML).join("\n");
    const style = q(card, "style").textContent;
    if (process.env.GOLDEN) console.log("GOLDEN", sha(sections), sha(style));
    assert.ok(!card.hasAttribute("wide"));
    assert.ok(!/wide|compact|back-rail/.test(sections + style), "no wide classes in the narrow drawing");
    assert.strictEqual(sha(sections), "b4ea402bc839a13f", "narrow block DOM unchanged");
    assert.strictEqual(sha(style), "dc5eda44710981df", "narrow style text unchanged");
    assert.ok(!q(card, "#back-rail") && !q(card, ".close"), "no rail in a normal card");
    console.log("PASS: narrow card: block DOM and style text are byte-identical to before; no back rail");
    card.remove();
  }

  // 8. the wide DOM: main first camera whatever its size, others at the right, buttons last; focus order
  {
    const content = withBlocks([blk("b1", "Entrance", [cam("gate", "small"), cam("yard", "big"), cam("lawn", "small"), cam("shed", "big", { available: false })],
      [act("go", { color: "green", icon: "door", confirm: true }), act("lights", { show_label: false, color: "blue" }), act("off")])]);
    const { card, fake } = await mount({ fullpage: true }, content, [1280, 800]);
    assert.ok(card.hasAttribute("wide"));
    const sec = q(card, "section.block.wide");
    assert.ok(sec, "wide section");
    assert.deepStrictEqual(Array.from(sec.children).map((c) => c.className), ["block-title", "wide-grid", "acts blk"]);
    const main = qa(card, ".wide-main .cam");
    assert.strictEqual(main.length, 1);
    assert.strictEqual(main[0].querySelector("img").dataset.camera, "gate", "the FIRST camera is main, whatever its size");
    assert.ok(main[0].classList.contains("big"));
    assert.deepStrictEqual(qa(card, ".wide-side .cam").map((t) => t.querySelector("img").dataset.camera), ["yard", "lawn", "shed"]);
    assert.ok(qa(card, ".wide-side .cam").every((t) => t.classList.contains("small")), "every other camera is small, even a 'big' one");
    // document order = focus order: main camera, side cameras, buttons
    const order = Array.from(sec.querySelectorAll("img[data-camera], button.act")).map((n) => n.dataset.camera || n.dataset.action);
    assert.deepStrictEqual(order, ["gate", "yard", "lawn", "shed", "go", "lights", "off"]);
    // buttons exactly as B4 draws them
    const [go, lights] = qa(card, "button.act");
    assert.ok(go.classList.contains("c-green") && go.querySelector("ha-icon").getAttribute("icon") === "mdi:door");
    assert.strictEqual(lights.getAttribute("aria-label"), "L-lights");
    assert.ok(!lights.querySelector(".t"), "show_label false: icon only");
    go.click(); await sleep(5);
    assert.ok(go.classList.contains("armed") || qa(card, "button.act")[0].classList.contains("armed"), "ask-first still arms");
    // every camera keeps its content description; the unavailable one says so
    assert.ok(qa(card, "img[data-camera]").every((i) => i.alt));
    // inline geometry is on the section
    assert.ok(/--w-mw: 780px/.test(sec.getAttribute("style")), sec.getAttribute("style"));
    // the main camera asks for the large still, the others the default
    await sleep(80);
    const widths = Object.fromEntries(fake.ofType("portico/content/snapshot").map((m) => [m.camera_id, m.width]));
    assert.strictEqual(widths.gate, 960, "the main camera asks for the large still");
    assert.strictEqual(widths.yard, undefined, "a side camera asks for the default width, even if the server said big");
    console.log("PASS: wide DOM: first camera main regardless of size, others right, buttons last in focus order, B4 buttons, large still for main only");
    card.remove();
  }

  // 9. wide CSS text and the single-camera / no-camera / lovelace cases
  {
    const content = withBlocks([blk("b1", "Solo", [cam("gate", "big")], [act("go")]), blk("b2", "Only buttons", [], [act("a1")])]);
    const { card } = await mount({}, content, [1000, 300]);
    assert.ok(card.hasAttribute("wide"), "a Lovelace card 1000 wide is wide whatever its height");
    assert.ok(q(card, ".wide-grid.solo"), "one camera: no side column");
    assert.ok(!q(card, ".wide-side"));
    assert.strictEqual(qa(card, "section.block.wide").length, 2, "non-fullpage: every block gets the full layout");
    assert.ok(!qa(card, ".compact-row").length);
    assert.ok(!q(card, "#back-rail"), "no rail in a Lovelace card, wide or not");
    const css = q(card, "style").textContent;
    assert.ok(css.includes(".wide-grid") && css.includes(".wide-side .cam .pic"));
    assert.ok(!css.includes(":host([fullpage][wide])") || !card.hasAttribute("fullpage"), "no fullpage rules needed");
    card.remove();
    // a narrow Lovelace column
    const m = await mount({}, content, [400, 900]);
    assert.ok(!m.card.hasAttribute("wide") && !q(m.card, ".wide-grid"));
    m.card.remove();
    console.log("PASS: wide card in Lovelace: width decides, every block full layout, a lone camera has no side column, no rail");
  }

  // 10. fullpage: later blocks below the first screen with big tiles, the first never loses buttons; a resize redraws
  {
    const blocks = [blk("b1", "Main", [cam("a", "big"), cam("b", "small")], [act("go"), act("go2")]),
                    blk("b2", "Two", [cam("c", "big"), cam("e", "small"), cam("f", "small")], [act("c1")]), blk("b3", "Three", [], [act("d1")])];
    const { card } = await mount({ fullpage: true }, withBlocks(blocks), [1280, 800]);
    assert.ok(q(card, "section.block.wide:not(.more) .wide-grid"));
    assert.strictEqual(qa(card, "section.block.more").length, 2, "later blocks drawn below the first screen");
    assert.strictEqual(qa(card, "section.block.more .more-cams .cam").length, 3, "every camera of a later block is drawn");
    assert.ok(q(card, "section.block.more .more-cams .cam img").alt, "tiles keep their description");
    assert.ok(qa(card, "section.block.more .more-cams .cam .label").length === 3, "tiles keep their captions");
    assert.ok(qa(card, "section.block:not(.more) button.act").length === 2);
    const mst = q(card, "section.block.more").getAttribute("style");
    const tw = parseFloat(/--w-tw:\s*([\d.]+)px/.exec(mst)[1]);
    const gw = parseFloat(/--w-gw:\s*([\d.]+)px/.exec(mst)[1]);
    assert.ok(tw >= (gw - 20) / 3 - 1 && tw >= 300, `tiles are a third of the row (${tw}px of ${gw}px)`);
    const css = q(card, "style").textContent;
    assert.ok(!css.includes(":host([fullpage][wide]) { overflow: hidden; }"), "wide fullpage scrolls");
    assert.ok(/:host\(\[fullpage\]\) \{[^}]*overflow: auto/.test(css) && css.includes("position: fixed") && css.includes("scrollbar-width: none"), "a fixed scroller, no scrollbar");
    assert.ok(/:host\(\[fullpage\]\) \{ background: transparent;/.test(css) && /:host\(\[fullpage\]\) ha-card \{[^}]*background: transparent/.test(css) && !css.includes("meda_portrait"), "the kiosk card is transparent; the page behind it carries the picture");
    assert.ok(!/min-height: 100%[^}]*height: \d+px/.test(css));
    // a tiny screen: 800x480 still draws; resize to portrait goes narrow
    card._setBox(800, 480);
    assert.ok(card.hasAttribute("wide"));
    card._setBox(480, 800);
    assert.ok(!card.hasAttribute("wide") && !q(card, ".wide-grid") && qa(card, "section.block").length === 3, "portrait: back to the narrow drawing");
    console.log("PASS: fullpage: first block full layout, later blocks below with big tiles, scrolling page on the main screen's background; a resize switches the drawing");
    card.remove();
  }

  // 11. the back rail: fullpage only, the tablet's own rail (64px round button, 10px from the left, centred, label Back);
  //     the old Close "x" and its 56px top band are gone
  {
    const content = withBlocks([blk("b1", "Main", [cam("a", "big")], [act("go")])]);
    for (const box of [[1280, 800], [480, 800]]) {
      const { card } = await mount({ fullpage: true }, content, box);
      const r = q(card, "button#back-rail");
      assert.ok(r, "the rail in fullpage, wide or narrow");
      assert.strictEqual(qa(card, "button#back-rail").length, 1);
      assert.strictEqual(r.getAttribute("aria-label"), "Back");
      assert.strictEqual(r.getAttribute("title"), "Back");
      const st = r.style;
      assert.strictEqual(st.position, "fixed");
      assert.strictEqual(st.left, "10px");
      assert.strictEqual(st.top, "calc(50% - 32px)", "vertically centred");
      assert.strictEqual(st.width, "64px"); assert.strictEqual(st.height, "64px");
      assert.strictEqual(st.minWidth, "64px"); assert.strictEqual(st.minHeight, "64px");
      assert.strictEqual(st.borderRadius, "50%");
      assert.strictEqual(st.background, "rgba(0, 0, 0, 0.55)");
      assert.strictEqual(st.color, "rgb(255, 255, 255)");
      assert.strictEqual(st.zIndex, "4");
      assert.ok(r.getAttribute("style").includes("will-change:transform"));
      assert.ok(r.querySelector("svg path"), "the arrow icon");
      assert.ok(r.querySelector("svg").getAttribute("style").includes("width:34px"));
      assert.ok(!r.querySelector("ha-ripple, mwc-ripple"), "the original rail has no ripple");
      const css = q(card, "style").textContent;
      assert.ok(css.includes(`:host([fullpage]) ha-card { padding-left: ${RAIL}px;`), "the rail's width is reserved on the left, narrow too");
      assert.ok(!css.includes("padding-top: 56px") && !css.includes(".close"), "the top band and its style are gone");
      assert.ok(!q(card, "#close-btn") && !q(card, ".close"), "the x is gone");
      assert.ok(!card.shadowRoot.innerHTML.includes("&#x2715;") && !card.shadowRoot.textContent.includes("\u2715"));
      assert.ok(10 + 64 <= RAIL, "the rail ends before the reserved width does");
      assert.ok(Array.from(card.shadowRoot.children).filter((n) => n.tagName !== "STYLE").pop() === r, "the rail is last in focus order");
      card.remove();
    }
    console.log("PASS: back rail: fullpage only, 64px round, 10px from the left edge, centred, label Back, width reserved; the x and its band are gone");
  }

  // 12. what the rail does: SPA navigation to the back path, assign as the fallback, the layered way out as the last
  {
    const content = withBlocks([blk("b1", "Main", [cam("a", "big")], [act("go")])]);
    const w = dom.window;
    const push = w.history.pushState.bind(w.history);
    const calls = [];
    const events = [];
    w.addEventListener("location-changed", (e) => events.push(e));
    const spy = (name) => (...a) => { calls.push([name, ...a]); };
    // pushState + location-changed
    {
      const { card } = await mount({ fullpage: true, back_path: "/lovelace/home" }, content, [1280, 800]);
      w.history.pushState = (...a) => { calls.push(["push", ...a]); push(...a); };
      q(card, "#back-rail").click();
      assert.deepStrictEqual(calls[0].slice(0, 4), ["push", null, "", "/lovelace/home"], "the configured back path");
      assert.strictEqual(events.length, 1, "HA is told with a location-changed event");
      assert.ok(events[0].bubbles && events[0].composed);
      assert.strictEqual(w.location.pathname, "/lovelace/home", "the address moved without a reload");
      card.remove();
    }
    // a configured path
    {
      calls.length = 0; events.length = 0;
      const { card } = await mount({ fullpage: true, back_path: "/lovelace/kitchen" }, content, [1280, 800]);
      q(card, "#back-rail").click();
      assert.strictEqual(calls[0][3], "/lovelace/kitchen");
      assert.strictEqual(events.length, 1);
      card.remove();
    }
    // an invalid configured path is never navigated to: layered fallback (here no Fully, history length 1 or more)
    {
      calls.length = 0; events.length = 0;
      const { card } = await mount({ fullpage: true, back_path: "//evil.example" }, content, [1280, 800]);
      let loaded = 0;
      w.fully = { loadStartURL: () => { loaded++; } };
      q(card, "#back-rail").click();
      assert.strictEqual(calls.length, 0, "no pushState to //evil.example");
      assert.strictEqual(events.length, 0);
      assert.strictEqual(loaded, 1, "the layered fallback ran (Fully's start URL)");
      delete w.fully;
      card.remove();
    }
    // no path configured ("") also uses the layered fallback
    {
      calls.length = 0; events.length = 0;
      const { card } = await mount({ fullpage: true, back_path: "" }, content, [1280, 800]);
      let loaded = 0;
      w.fully = { loadStartURL: () => { loaded++; } };
      q(card, "#back-rail").click();
      assert.strictEqual(calls.length, 0);
      assert.strictEqual(loaded, 1);
      delete w.fully;
      card.remove();
    }
    // pushState throws -> location.assign(path)
    {
      calls.length = 0; events.length = 0;
      const { card } = await mount({ fullpage: true, back_path: "/lovelace/home" }, content, [1280, 800]);
      w.history.pushState = () => { throw new Error("nope"); };
      const origErr = console.error; console.error = () => {};
      card._assign = spy("assign");
      q(card, "#back-rail").click();
      console.error = origErr;
      assert.deepStrictEqual(calls, [["assign", "/lovelace/home"]], "falls back to location.assign(path)");
      assert.strictEqual(events.length, 0);
      // both throw -> the layered way out
      calls.length = 0;
      card._assign = (p) => { calls.push(["assign", p]); if (p !== "/") throw new Error("x"); };
      let loaded = 0;
      w.fully = { loadStartURL: () => { loaded++; } };
      console.error = () => {};
      q(card, "#back-rail").click();
      console.error = origErr;
      assert.strictEqual(loaded, 1, "both navigations failed: Fully's start URL");
      delete w.fully;
      card.remove();
    }
    w.history.pushState = push;
    console.log("PASS: rail click: pushState + location-changed to the back path (default or configured), assign if pushState fails, layered way out if both fail, an invalid or empty path never navigated");
  }

  // 7. the wall tablet's controls (2026-11-12 "layout is broken": a strip of tiny controls under a camera that took the height)
  {
    const g = geo(1280, 800, 1, 2, { extras: 1 });
    assert.ok(g.btnRowH >= 64 && g.btnRowH <= 80, `1280x800: touch targets sized to the screen (${g.btnRowH}px)`);
    assert.ok(g.titleH >= 36, "a legible title row");
    assert.ok(g.height <= 800.001 && g.extrasShown === 1, "title + camera + buttons fit the first screen; the Door block follows below");
    assert.ok(g.btnH >= 800 * 0.1, "the controls get a real share of the height");
    assert.ok(g.mw >= 900 && Math.abs(g.mh - g.mw * 9 / 16) < 0.001, "the camera keeps 16:9 and still takes what is left");
    assert.ok(g.btnCols === 2 && g.btnRows === 1 && Math.abs(g.rowW - g.groupW) < 0.001, "two buttons share one row across the picture's width");
    for (const [w, h] of SIZES) {
      for (const nb of [0, 2, 8]) {
        const x = geo(w, h, 1, nb, { extras: 1 });
        assert.ok(x.btnRowH >= 48, `${w}x${h}: buttons never under 48px`);
        assert.ok(x.rowW <= w - RAIL - 16 + 0.001 && x.height <= h + 0.001, `${w}x${h} ${nb} buttons: rows inside the screen`);
      }
    }
    // a Lovelace card keeps its 48px rows
    const l = PorticoCard.wideGeometry({ w: 1280, h: 800, fullpage: false, nMain: 1, nSide: 0, nButtons: 2, hasTitle: true, extras: 0 });
    assert.strictEqual(l.btnRowH, 48);
    // the DOM: the vars the CSS needs are set on the first block and on the later one
    const { card } = await mount({ fullpage: true }, withBlocks([blk("a", "Gate", [cam("g", "big")], [act("x"), act("y")]),
      blk("b", "Door", [cam("d", "small")], [act("o")])]), [1280, 800]);
    for (const b of ["a", "b"]) {
      const st = q(card, `section[data-block="${b}"]`).getAttribute("style");
      assert.ok(/--w-gw:/.test(st) && /--w-bh:\s*\d+/.test(st) && /--w-cols:/.test(st), `block ${b}: sizing vars set`);
    }
    const css = q(card, "style").textContent;
    assert.ok(/fullpage\]\[wide\]\) \.acts\.blk \.act\s*\{[^}]*height: var\(--w-bh\)/.test(css), "buttons use the screen-sized height");
    assert.ok(/fullpage\]\[wide\]\) \.block\.wide > \.acts\.blk\s*\{[^}]*var\(--w-gw\)[^}]*repeat\(var\(--w-cols/.test(css), "button grid spans the picture, equal columns");
    card.remove();
    console.log("PASS: wall tablet: controls sized to the screen (>= 48px, ~72px at 800), share of height reserved, spread across the picture, camera takes the rest");
  }

  console.log("ALL_CARD_WIDE_TESTS_PASSED");
}

main().catch((err) => {
  console.error("TEST FAILED:", err);
  process.exit(1);
});
