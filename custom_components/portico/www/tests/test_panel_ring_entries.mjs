// Ring pages per Talk element (D-20261122-4aec, decisions D-20261122-5b24), R-C: the Pages tab has one entry per ringable Talk element
// (door, phone, screen; never a group), each Default (computed, nothing stored) or Edited (a stored Ring page), plus "Removed"; Edit seeds a
// page, Reset deletes it; the Devices tab has no Ring pages selector. While any devices[*].ring map is non-empty (the legacy form) the old
// assignment is read-only and a banner points at the migration dry run. Protocol-level in jsdom; NOT a visual check.
import assert from "assert";
import fs from "fs";
import { loadPanel, editorHass, makeLoaded, sleep } from "./panel_loader.mjs";

const { dom, Panel, doc } = loadPanel();
const L = Panel.logic;
const win = dom.window;
const throwsEdit = (fn, re) => assert.throws(fn, (e) => e instanceof L.EditError && re.test(e.message), String(re));
const q = (p, sel) => p.shadowRoot.querySelector(sel);
const qa = (p, sel) => Array.from(p.shadowRoot.querySelectorAll(sel));
const click = (n) => { assert.ok(n, "clicked something that is not there"); n.dispatchEvent(new win.Event("click", { bubbles: true })); };
const listReply = (elements) => ({ schema_version: 1, rev: "r1", elements, used_by: {}, kinds: {}, icons: [], palette: [], limits: {} });

const FRONT = { id: "el-20000001", kind: "talk", name: "Front door", options: { door: "http://10.0.0.7:8091" } };
const SIDE = { id: "el-20000002", kind: "talk", name: "Side door", options: { door: "side-1" } };
const FRONT2 = { id: "el-20000003", kind: "talk", name: "Front door again", options: { door: "http://10.0.0.7:8091" } };
const PHONE = { id: "talk-phone-phone1", kind: "talk", name: "Alice's phone", managed: true, options: { phone: "phone1" } };
const KITCHEN = { id: "talk-screen-kitchen", kind: "talk", name: "Kitchen tablet", managed: true, options: { screen: "kitchen" } };
const GONE = { id: "talk-screen-old", kind: "talk", name: "Old tablet", managed: true, target_removed: true, options: { screen: "old" } };
const EVERYONE = { id: "el-20000009", kind: "talk", name: "Everyone", options: { group: "everyone" } };
const OPEN_FRONT = { id: "el-30000001", kind: "button", name: "Open front", entity_id: "", options: { action: "portico_open", target: "http://10.0.0.7:8091" } };
const OPEN_SIDE = { id: "el-30000002", kind: "button", name: "Open side", entity_id: "", options: { action: "portico_open", target: "side-1" } };
const LOCK = { id: "el-30000003", kind: "button", name: "Gate lock", entity_id: "lock.gate", options: { action: "lock_open" } };
const LAMP = { id: "el-30000004", kind: "button", name: "Lamp", entity_id: "light.lamp", options: { action: "button_press" } };
const ELS = [FRONT, SIDE, PHONE, KITCHEN, GONE, EVERYONE, OPEN_FRONT, OPEN_SIDE, LOCK, LAMP];
const MAP = new Map(ELS.map((e) => [e.id, e]));

const content = (id) => ({ id, label: id, kind: "content", blocks: [{ id, title: id, cameras: [], actions: [] }] });
/* No device holds a ring map. */
const doc6 = () => ({ schema_version: 6, home_name: "My Home", pages: [content("home")],
  devices: { phone1: { pages: [{ page: "home" }], ring: {}, roster_hide: [] } }, dashboards: {}, endpoints: {} });

// ---- 1. pure: the entries ----------------------------------------------------------------------------------------------------------
{
  const d = doc6();
  const { entries, removed } = L.ringEntries(d, MAP);
  assert.deepStrictEqual(entries.map((e) => e.talk), [FRONT.id, SIDE.id, PHONE.id, KITCHEN.id], "doors, phones, screens, by name; no group, no target_removed element");
  assert.ok(entries.every((e) => e.state === "default" && e.page === null && !e.warn));
  assert.deepStrictEqual(removed, []);
  assert.deepStrictEqual(entries.map((e) => e.party), ["door", "door", "phone", "screen"]);
  console.log("PASS: one entry per ringable Talk element, groups and removed targets excluded");
}

// ---- 2. pure: Edit seeds, Reset deletes, the round trip is the original document ----------------------------------------------------------
{
  const d = doc6();
  const before = JSON.stringify(d);
  const seeded = L.editRingEntry(d, FRONT.id, MAP);
  assert.strictEqual(JSON.stringify(d), before, "pure");
  const page = seeded.doc.pages.at(-1);
  assert.deepStrictEqual([page.kind, page.talk, page.cameras, page.label], ["ring", FRONT.id, null, "Front door"]);
  assert.deepStrictEqual(page.blocks, [{ id: "buttons", title: "Buttons", cameras: [], actions: [{ id: "gate-lock", element: LOCK.id }, { id: "open-front", element: OPEN_FRONT.id }] }],
    "this door's open buttons and the lock-open buttons, not another door's open button, not a lamp");
  assert.strictEqual(L.ringEntries(seeded.doc, MAP).entries[0].state, "edited");
  throwsEdit(() => L.editRingEntry(seeded.doc, FRONT.id, MAP), /already has an edited page/);
  assert.deepStrictEqual(L.resetRingEntry(seeded.doc, FRONT.id), d, "Edit then Reset round-trips to the original document");
  throwsEdit(() => L.resetRingEntry(d, FRONT.id), /no edited page/);
  throwsEdit(() => L.editRingEntry(d, EVERYONE.id, MAP), /no ring page to edit/);
  throwsEdit(() => L.editRingEntry(d, GONE.id, MAP), /no ring page to edit/);
  throwsEdit(() => L.editRingEntry(d, "light.x", MAP), /Pick a Talk element/);
  // a phone or screen: an empty page
  const ph = L.editRingEntry(d, PHONE.id, MAP).doc.pages.at(-1);
  assert.deepStrictEqual([ph.talk, ph.cameras, ph.blocks[0].actions], [PHONE.id, null, []]);
  console.log("PASS: Edit seeds from the Elements store, Reset deletes, the round trip is the original");
}

// ---- 3. pure: removed, duplicate Talk elements for one door, the legacy form ------------------------------------------------------------------
{
  const d = L.editRingEntry(doc6(), PHONE.id, MAP).doc;
  const phonePage = d.pages.at(-1).id;
  d.pages.push({ id: "orphan", label: "Orphan", kind: "ring", talk: GONE.id, cameras: null, blocks: [] });
  d.pages.push({ id: "lost", label: "Lost", kind: "ring", talk: "el-deadbeef", cameras: null, blocks: [] });
  const r = L.ringEntries(d, MAP);
  assert.deepStrictEqual(r.removed.map((e) => [e.talk, e.state, e.name]), [[GONE.id, "removed", "Old tablet"], ["el-deadbeef", "removed", "Talk element removed (el-deadbeef)"]]);
  assert.ok(!r.entries.some((e) => e.talk === GONE.id || e.talk === "el-deadbeef"));
  assert.deepStrictEqual(L.resetRingEntry(d, GONE.id).pages.map((p) => p.id), ["home", phonePage, "lost"], "Reset removes a Removed page");
  // two Talk elements for one door: both warn once one holds a page
  const dup = new Map([...MAP, [FRONT2.id, FRONT2]]);
  assert.ok(L.ringEntries(doc6(), dup).entries.every((e) => !e.warn), "no page, no warning");
  const withPage = L.editRingEntry(doc6(), FRONT2.id, dup).doc;
  const warn = L.ringEntries(withPage, dup).entries.filter((e) => e.warn).map((e) => e.talk).sort();
  assert.deepStrictEqual(warn, [FRONT.id, FRONT2.id]);
  // a restored document with a stale per-device `ring` map is read like any other: Edit works, the map is never touched
  const stale = doc6(); stale.devices.phone1.ring = { [FRONT.id]: "x" };
  const edited = L.editRingEntry(stale, FRONT.id, MAP).doc;
  assert.deepStrictEqual(edited.devices.phone1.ring, { [FRONT.id]: "x" });
  assert.strictEqual(L.ringEntries(edited, MAP).entries.find((e) => e.talk === FRONT.id).state, "edited");
  console.log("PASS: removed entries, two Talk elements for one door, a stale ring map does not stop Edit");
}

async function waitFor(fn, ms = 5000) { const end = Date.now() + ms; while (Date.now() < end) { if (fn()) { await sleep(30); return; } await sleep(10); } assert.fail("timed out"); }

async function mount({ document, elements = ELS, handlers = {} }) {
  const loaded = makeLoaded();
  loaded.document = document; loaded.orphans = []; loaded.resolved = {};
  const f = editorHass({ "portico/elements/list": () => listReply(elements), ...handlers }, { loaded });
  const panel = doc.createElement("portico-panel");
  panel._previewMs = 20;
  panel.hass = f.hass;
  doc.body.appendChild(panel);
  await waitFor(() => panel._elList && panel._elList.status !== "idle" && !panel._elList.loading && q(panel, '[data-tab="pages-unified"]'));
  return { f, panel };
}

// ---- 4. panel, new form: Default entries, Edit with confirmation, Reset, Removed, no Devices selector ------------------------------------
{
  const d = doc6();
  d.pages.push({ id: "orphan", label: "Orphan", kind: "ring", talk: GONE.id, cameras: null, blocks: [] });
  const { panel } = await mount({ document: d });
  const sections = () => qa(panel, ".pagesection");
  assert.ok(!q(panel, '[data-state="ring-migration-banner"]'), "no banner in the new form");
  assert.ok(!q(panel, '[data-field="new-page-kind"]'), "Add page asks no kind");
  const defaults = qa(panel, '.pagesection[data-ring-state="default"]');
  assert.deepStrictEqual(defaults.map((n) => n.dataset.ringTalk), [FRONT.id, SIDE.id, PHONE.id, KITCHEN.id]);
  assert.match(defaults[0].textContent, /Default/);
  assert.match(defaults[0].textContent, /Nothing is stored/);
  assert.ok(q(panel, '[data-role="rings-group"]') && q(panel, '[data-role="rings-removed"]'));
  assert.strictEqual(qa(panel, '.pagesection[data-ring-state="removed"]').length, 1, "the orphan is under Removed");
  assert.ok(q(panel, '.pagesection[data-ring-state="removed"] [data-act="ring-reset"]'));
  assert.ok(!q(panel, '.pagesection[data-ring-state="removed"] [data-field="block-title"]'), "a removed entry has no editor and no preview");
  // Edit: asks first, stores nothing until confirmed
  click(defaults[0].querySelector('[data-act="ring-edit"]'));
  await sleep(30);
  const confirm = q(panel, '[data-state="ring-edit-confirm"]');
  assert.ok(confirm);
  assert.match(confirm.textContent, /starts with 2 open buttons/);
  assert.ok(!panel._state.draft.pages.some((p) => p.kind === "ring" && p.talk === FRONT.id), "nothing stored before the confirmation");
  click(q(panel, '[data-act="ring-edit-no"]'));
  await sleep(30);
  assert.ok(!q(panel, '[data-state="ring-edit-confirm"]'));
  click(q(panel, `.pagesection[data-ring-talk="${FRONT.id}"] [data-act="ring-edit"]`));
  await sleep(30);
  click(q(panel, '[data-act="ring-edit-yes"]'));
  await sleep(60);
  const stored = panel._state.draft.pages.find((p) => p.kind === "ring" && p.talk === FRONT.id);
  assert.ok(stored && stored.blocks[0].actions.length === 2);
  const edited = q(panel, `.pagesection[data-page="${stored.id}"]`);
  assert.ok(edited && edited.dataset.ringState === "edited" && edited.querySelector('[data-badge="ring-state"]').textContent === "Edited");
  assert.ok(edited.querySelector('[data-act="ring-reset"]') && !edited.querySelector('[data-act="delete-page"]'), "Reset to default instead of Delete");
  assert.ok(!qa(panel, '.pagesection[data-ring-state="default"]').some((n) => n.dataset.ringTalk === FRONT.id), "no Default entry for it any more");
  assert.match(edited.querySelector('[data-hint="shown-on"]').textContent, /^Rings on: every device called by Front door$/);
  // Reset: confirmation names what is lost
  click(edited.querySelector('[data-act="ring-reset"]'));
  await sleep(30);
  assert.match(q(panel, '[data-state="ring-reset-confirm"]').textContent, /deletes the page of Front door \(2 buttons\)/);
  click(q(panel, '[data-act="ring-reset-yes"]'));
  await sleep(60);
  assert.ok(!panel._state.draft.pages.some((p) => p.talk === FRONT.id), "back to Default: nothing stored");
  assert.ok(q(panel, `.pagesection[data-ring-state="default"][data-ring-talk="${FRONT.id}"]`));
  // Reset a Removed page
  click(q(panel, '.pagesection[data-ring-state="removed"] [data-act="ring-reset"]'));
  await sleep(30);
  click(q(panel, '[data-act="ring-reset-yes"]'));
  await sleep(60);
  assert.ok(!panel._state.draft.pages.some((p) => p.id === "orphan"));
  assert.ok(!q(panel, '[data-role="rings-removed"]'));
  assert.ok(sections().length >= 5);
  // the Devices tab: no Ring pages selector, no legacy list
  click(qa(panel, "nav.tabs button").find((b) => b.textContent === "Devices"));
  await sleep(60);
  assert.ok(!q(panel, '[data-selector="ring-pages"]') && !q(panel, '[data-selector="ring-pages-legacy"]') && !q(panel, '[data-field^="device-ring"]'));
  panel.remove();
  console.log("PASS: panel, new form: Default and Edited entries, Edit and Reset ask first, Removed, no Devices selector");
}

// ---- 5. panel, a restored document that still has ring maps: read like any other, no banner, entries offered ------------------------------
{
  const d = JSON.parse(fs.readFileSync(new URL("./fixtures/live_document_v6.json", import.meta.url), "utf8"));
  const loaded = d.document ? d : { document: d };
  const fx = makeLoaded();
  fx.document = loaded.document; fx.orphans = loaded.orphans || []; fx.resolved = loaded.resolved || {};
  assert.ok(Object.values(fx.document.devices || {}).some((x) => x.ring && Object.keys(x.ring).length), "the fixture still holds a per-device ring map");
  const f = editorHass({ "portico/elements/list": () => listReply([FRONT]) }, { loaded: fx });
  const panel = doc.createElement("portico-panel");
  panel._previewMs = 20;
  panel.hass = f.hass;
  doc.body.appendChild(panel);
  await waitFor(() => panel._elList && panel._elList.status !== "idle" && !panel._elList.loading && q(panel, '[data-tab="pages-unified"]'));
  assert.ok(!q(panel, '[data-state="ring-migration-banner"]') && !q(panel, '[data-selector="ring-pages-legacy"]'), "no banner, no old list");
  assert.ok(q(panel, '[data-role="rings-group"]') && q(panel, '[data-ring-state]'), "the Rings entries are offered");
  assert.ok(!f.ofType("portico/content/ring_pages_migrate").length && !f.ofType("portico/content/save").length, "no call, nothing saved");
  panel.remove();
  console.log("PASS: panel, a document with a stale ring map: read like any other");
}
console.log("ALL PASS test_panel_ring_entries");
