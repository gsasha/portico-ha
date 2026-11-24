// The Pages tab of a schema 6 document, with the REAL document of the live store (fixtures/live_document_v6.json, the result of
// portico/content/document: pages 'home' (content, 2 blocks) and 'ring-c48222d6' (ring, 1 block)). Each page is one SECTION
// titled with the page name: the full editor on the left, the live preview ('Preview as') on the right; the sections are stacked, with
// no list, strip, card grid or open/back. Editing one page rebuilds only its section (another section's focus and selection survive)
// and asks only its preview, debounced, with at most 2 requests in flight overall. Protocol-level in jsdom; NOT a visual check.
import assert from "assert";
import fs from "fs";
import { loadPanel, editorHass, sleep, resolveDraftPage } from "./panel_loader.mjs";

const { dom, Panel, doc } = loadPanel();
const win = dom.window;
const live = () => JSON.parse(fs.readFileSync(new URL("./fixtures/live_document_v6.json", import.meta.url), "utf8"));
const DOOR = "portico_intercom:http://10.0.0.7:8091";
const TALK = { id: "el-e4773c13", kind: "talk", name: "Portico (gate_sip_observer)", options: { door: "http://10.0.0.7:8091" } };
const listReply = (elements) => ({ schema_version: 1, rev: "r1", elements, used_by: {}, kinds: {}, icons: [], palette: [], limits: {} });
const q = (p, sel) => p.shadowRoot.querySelector(sel);
const qa = (p, sel) => Array.from(p.shadowRoot.querySelectorAll(sel));
const sec = (p, id) => q(p, `.pagesection[data-page="${id}"]`);
const click = (n) => { assert.ok(n); n.dispatchEvent(new win.Event("click", { bubbles: true })); };
function setValue(node, value, type = "input") { assert.ok(node); node.value = value; node.dispatchEvent(new win.Event(type, { bubbles: true })); }

/* What the server answers for a schema 6 draft: the endpoint's own content pages, and for a caller the ring frame. */
let inflight = 0, peak = 0;
function server(msg) {
  const dev = (msg.document.devices || {})[msg.endpoint] || {};
  const by = new Map(msg.document.pages.map((p) => [p.id, p]));
  const pages = (dev.pages || []).map((e) => by.get(e.page)).filter((p) => p && p.kind !== "ring").map(resolveDraftPage);
  const ringId = msg.caller && Object.values(dev.ring || {})[0];
  const rp = ringId && by.get(ringId);
  return { endpoint: msg.endpoint, kind: "phone", rev: "r", pages, roster: [],
           callers: msg.caller ? { [msg.caller]: { name: "Portico (gate_sip_observer)", kind: "door", cameras: [{ id: "c1", label: "Gate", name: "Gate main", available: true }],
             ring_buttons: rp ? (rp.blocks || []).flatMap((b) => b.actions) : [], call_buttons: [] } } : {} };
}

/* Wait for the condition, not a fixed time: the previews are debounced, queued (2 in flight) and asked again once the elements list
 * arrives, so on a slow machine a fixed sleep ends before the first frame. Settled = the elements list read, sections drawn, no preview
 * timer, queued or in-flight request, and no section still on 'Preview loads after a moment...'. Bounded: a real hang fails the test. */
async function settled(panel, ms = 10000) {
  const end = Date.now() + ms;
  const ok = () => panel._elList && panel._elList.status !== "idle" && !panel._elList.loading
    && panel.shadowRoot.querySelectorAll(".pagesection").length > 0 && panel._secTimers.size === 0 && panel._secQueue.length === 0 && panel._secInflight === 0
    && !Array.from(panel.shadowRoot.querySelectorAll("[data-section-preview]")).some((h) => /Preview loads/.test(h.textContent));
  while (Date.now() < end) { if (ok()) { await sleep(30); if (ok()) return; } else await sleep(10); }
  assert.fail("the panel's previews did not settle within " + ms + " ms");
}

async function mount(extra = {}) {
  const loaded = extra.loaded || live();
  const f = editorHass({
    "portico/elements/list": () => listReply([TALK]),
    "portico/content/preview": async (msg) => { inflight++; peak = Math.max(peak, inflight); await sleep(extra.delay || 5); inflight--; return server(msg); },
  }, { loaded });
  const panel = doc.createElement("portico-panel");
  panel._previewMs = extra.ms || 20;
  panel.hass = f.hass;
  doc.body.appendChild(panel);
  await settled(panel);
  return { f, panel };
}

// ---- 1. the real document: two sections, editor left, preview right, previews really render ---------------------------------------
{
  const { f, panel } = await mount();
  const sections = qa(panel, ".pagesection");
  assert.deepStrictEqual(sections.map((s) => s.dataset.page), ["home", "ring-c48222d6"]);
  assert.deepStrictEqual(sections.map((s) => s.querySelector("h2").textContent), ["Home", "Ring: Portico (gate_sip_observer)"]);
  assert.ok(!q(panel, ".pagecard, .pagegrid, .pagelist, .pagestrip") && !q(panel, '[data-act="open-page"]') && !q(panel, '[data-act="back-to-pages"]'), "no list, strip, cards, open or back");
  const content = sec(panel, "home"), ring = sec(panel, "ring-c48222d6");
  { const lay = q(panel, '.layout[data-tab="pages-unified"]'); assert.ok(lay && lay.classList.contains("one"), "the Pages tab uses the single-column .layout.one (the three-column .layout put it in the 210px sidebar column)"); }
  for (const s of sections) {
    const cols = s.querySelector(".sectioncols");
    assert.ok(cols.children[0].classList.contains("pagedetail") && cols.children[1].classList.contains("previewcol"), "editor left, preview right");
    assert.ok(s.querySelector('[data-badge="kind"]') && (s.dataset.page !== "home" || s.querySelector('[data-act="delete-page"]')) && s.querySelector('[data-hint="shown-on"]'), "kind badge, Shown on, Delete (a Ring page is reset on its entry, never deleted)");
    assert.ok(s.querySelector('.previewcol [data-field="preview-as"]'), "'Preview as'");
  }
  assert.strictEqual(content.querySelectorAll(".blockcard, [data-field=\"block-title\"]").length >= 2, true, "both blocks of the content page are editable");
  assert.strictEqual(content.querySelectorAll('[data-field="block-title"]').length, 2);
  assert.ok(ring.querySelector('[data-field="ring-talk"]') && ring.querySelector('[data-form="ring-camera"]'), "Talk and camera selectors of the Ring page");
  assert.match(ring.querySelector('[data-field="ring-talk"]').textContent, /Portico \(gate_sip_observer\).*fixed/, "the Talk element of a Ring page is fixed text, not a selector");
  assert.ok(!ring.querySelector('select[data-field="ring-talk"]'));
  assert.match(content.querySelector('[data-hint="shown-on"]').textContent, /^Shown on: /);
  assert.match(ring.querySelector('[data-hint="shown-on"]').textContent, /^Rings on: every device called by Portico \(gate_sip_observer\)/);
  assert.ok(!ring.querySelector('[data-act="delete-page"]'));
  // the previews are the page, not 'Nothing to preview yet.'
  assert.ok(content.querySelector("[data-section-preview] .frame"), "the content preview is drawn");
  assert.doesNotMatch(content.querySelector("[data-section-preview]").textContent, /Nothing to preview/);
  assert.match(content.querySelector("[data-section-preview]").textContent, /Gate/);
  assert.match(content.querySelector("[data-section-preview]").textContent, /Door/);
  assert.match(ring.querySelector("[data-section-preview] [data-ring]").textContent, /is calling/);
  assert.match(ring.querySelector('[data-field="preview-caller-fixed"]').textContent, /Call from:/);
  const reqs = f.ofType("portico/content/preview");
  assert.strictEqual(reqs.length, 2, "one request per page");
  assert.strictEqual(reqs.find((r) => r.caller).caller, DOOR, "the Ring page previews as the party of its Talk element");
  assert.strictEqual(reqs.find((r) => !r.caller).document.schema_version, 6);
  panel.remove();
  console.log("PASS: the real v6 document: one section per page, editor left, preview right, both previews render");
}

// ---- 2. editing one page rebuilds only its section and asks only its preview, debounced --------------------------------------------
{
  const { f, panel } = await mount();
  const content = sec(panel, "home"), ring = sec(panel, "ring-c48222d6");
  const ringTitle = ring.querySelector('[data-field="block-title"]');
  ringTitle.focus();
  ringTitle.setSelectionRange(2, 6);
  assert.strictEqual(panel.shadowRoot.activeElement, ringTitle, "the Ring block title has the focus");
  const before = f.ofType("portico/content/preview").length;
  // three quick edits of the content page: one request for that section
  const t0 = content.querySelector('[data-field="block-title"]');
  setValue(t0, "Gate A", "change");
  setValue(sec(panel, "home").querySelector('[data-field="block-title"]'), "Gate B", "change");
  setValue(sec(panel, "home").querySelector('[data-field="block-title"]'), "Gate C", "change");
  assert.strictEqual(panel._state.draft.pages[0].blocks[0].title, "Gate C");
  await sleep(150);
  assert.strictEqual(sec(panel, "ring-c48222d6"), ring, "the Ring section is the very same node");
  assert.notStrictEqual(sec(panel, "home"), content, "the edited section was rebuilt");
  assert.strictEqual(sec(panel, "home").querySelector("h2").textContent, "Home");
  assert.strictEqual(ring.querySelector('[data-field="block-title"]'), ringTitle);
  assert.strictEqual(panel.shadowRoot.activeElement, ringTitle, "the focus survived the edit elsewhere");
  assert.deepStrictEqual([ringTitle.selectionStart, ringTitle.selectionEnd], [2, 6], "and so did the selection");
  const reqs = f.ofType("portico/content/preview").slice(before);
  assert.strictEqual(reqs.length, 1, `one debounced request, for the edited page only (${reqs.length})`);
  assert.ok(!reqs[0].caller, "not the Ring page's");
  assert.match(sec(panel, "home").querySelector("[data-section-preview]").textContent, /Gate C/, "its preview shows the edit");
  // an edit of the Ring page asks the Ring preview, and the content section stays
  const content2 = sec(panel, "home");
  setValue(ring.querySelector('[data-field="block-title"]'), "Call buttons", "change");
  await sleep(120);
  assert.strictEqual(sec(panel, "home"), content2, "the content section stayed");
  const more = f.ofType("portico/content/preview").slice(before + 1);
  assert.strictEqual(more.length, 1);
  assert.strictEqual(more[0].caller, DOOR);
  // 'Preview as' asks only its own section, for the chosen device
  const as = sec(panel, "home").querySelector('[data-field="preview-as"]');
  assert.ok(Array.from(as.options).length >= 2);
  const other = Array.from(as.options).find((o) => o.value !== as.value).value;
  setValue(as, other, "change");
  await sleep(120);
  const last = f.ofType("portico/content/preview").at(-1);
  assert.strictEqual(last.endpoint, other);
  assert.strictEqual(f.ofType("portico/content/preview").length, before + 3);
  panel.remove();
  console.log("PASS: an edit rebuilds only its section, keeps another's focus and selection, and asks only its preview, debounced");
}

// ---- 3. many pages: at most 2 requests in flight overall ----------------------------------------------------------------------------
{
  const loaded = live();
  for (let i = 0; i < 6; i++) loaded.document.pages.push({ id: `extra-${i}`, label: `Extra ${i}`, kind: "content", blocks: [{ id: `extra-${i}`, title: "E", cameras: [], actions: [] }] });
  peak = 0;
  const { f, panel } = await mount({ loaded, delay: 20 });
  await settled(panel);
  assert.strictEqual(qa(panel, ".pagesection").length, 8);
  assert.strictEqual(f.ofType("portico/content/preview").length, 8);
  assert.ok(peak <= 2 && peak >= 1, `peak in flight ${peak}`);
  panel.remove();
  console.log("PASS: eight sections ask eight previews, never more than two at once");
}
