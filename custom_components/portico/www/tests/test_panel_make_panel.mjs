// DEV-807 panel mode, own pages: the Devices tab's "Make a panel" (portico/phones/make_panel: no slug, no screen), the panel's section
// (the Pages and Ring page selectors of a phone, keyed by its phone endpoint id), the one-time "Copy pages from the old screen" button
// and a former panel screen's row. jsdom with a fake `hass`; NOT a visual check.
import assert from "assert";
import { loadPanel, editorHass, makeLoaded, sleep } from "./panel_loader.mjs";
import { importPhones } from "./panel_loader.mjs";

const PH = await importPhones();
const { canMakePanel, panelText, revokeConsequences } = PH;

// ---- 1. the pure helpers --------------------------------------------------------------------------------------------------------
{
  assert.strictEqual(PH.suggestPanelSlug, undefined, "no slug helpers any more: a panel has no screen address");
  const base = { legacy: false, state: "enrolled" };
  assert.strictEqual(canMakePanel(base), true);
  assert.strictEqual(canMakePanel({ ...base, legacy: true }), false, "a legacy row is never offered it");
  assert.strictEqual(canMakePanel({ ...base, state: "pending" }), false);
  assert.strictEqual(canMakePanel({ ...base, panel: true, panel_screen: "x" }), false);
  assert.strictEqual(canMakePanel(null), false);
  assert.match(panelText({ panel: true, panel_screen: "kp-app" }), /own pages/);
  assert.ok(!/kp-app/.test(panelText({ panel: true, panel_screen: "kp-app" })), "the old screen is not named in the row text");
  assert.strictEqual(panelText({}), "");
  assert.ok(!revokeConsequences({ state: "enrolled", panel: true, panel_screen: "kp-app" }).some((l) => /kp-app|panel screen/i.test(l)), "revoke no longer takes a screen with the panel");
  console.log("PASS: who is offered the action, the panel row text, no panel-screen line on revoke");
}

// ---- 2. the Devices tab ---------------------------------------------------------------------------------------------------------
const { dom, doc } = loadPanel();
const win = dom.window;
const q = (p, sel) => p.shadowRoot.querySelector(sel);
const qa = (p, sel) => Array.from(p.shadowRoot.querySelectorAll(sel));
const act = (p, name) => q(p, `[data-act="${name}"]`);
const click = (node) => { assert.ok(node, "clicked something that is not there"); node.dispatchEvent(new win.Event("click", { bubbles: true })); };
const type = (node, value) => { assert.ok(node); node.value = value; node.dispatchEvent(new win.Event("input", { bubbles: true })); };
const sent = (f, t) => f.sent.filter((m) => m.type === t);

const page = (id, label) => ({ id, label, kind: "content", blocks: [{ id: "b", title: label, cameras: [], actions: [] }] });
const ringPage = (id, talk) => ({ id, label: id, kind: "ring", talk, cameras: null, blocks: [] });
const T_KP = { id: "talk-screen-kitchen-panel-app", kind: "talk", name: "Kitchen panel", managed: true, options: { screen: "kitchen-panel-app" } };
const listReply = (elements) => ({ schema_version: 1, rev: "r1", elements, used_by: {}, kinds: {}, icons: [], palette: [], limits: {} });

const ROW = (o) => ({ entry_id: "e-1", legacy: false, name: "Alice's phone", state: "enrolled", expired: false, code_expires_in: null,
  enrolled_at: "2026-10-08T07:00:00+00:00", last_connection: "2026-11-09T07:00:00+00:00", push_registered: true, doors: null, away_ring: null, house_receive: false, ...o });

function doc6(withPanel, ownPages = false) {
  const d = { schema_version: 6, home_name: "My Home", pages: [page("home", "Home"), ringPage("kp-ring", T_KP.id)],
    devices: { phone1: { pages: [{ page: "home" }], ring: {}, roster_hide: [] } }, dashboards: {}, endpoints: {} };
  if (withPanel) {
    d.devices["kitchen-panel-app"] = { pages: [{ page: "home" }], ring: {}, roster_hide: [] };          // the former screen's row
    d.dashboards["kitchen-panel-app"] = { name: "Kitchen panel", ha_user_id: "u9", cameras: [] };
    if (ownPages) d.devices["kp-phone"] = { pages: [{ page: "home" }], ring: {}, roster_hide: [] };
  }
  return d;
}

async function mount({ rows, withPanel = false, ownPages = false, handlers = {}, schema6 = true } = {}) {
  const loaded = makeLoaded();
  if (schema6) loaded.document = doc6(withPanel, ownPages);
  loaded.orphans = [];
  loaded.resolved = {};
  if (withPanel) {
    loaded.endpoints.push({ id: "kp-phone", kind: "phone", name: "Kitchen panel", label: "Kitchen panel", ha_user_id: "u9", cameras: [], room: null, available: true, reason: null, panel: true });
    loaded.endpoints.push({ id: "kitchen-panel-app", kind: "dashboard", name: "Kitchen panel", label: "Kitchen panel", ha_user_id: "u9", cameras: [], room: null, available: true, reason: null, app_screen: true });
  }
  const state = { rows };
  const f = editorHass({
    "portico/elements/list": () => listReply([T_KP]),
    "portico/phones/list": () => ({ header: {}, phones: state.rows }),
    "portico/house_calls/get": () => ({ enabled: false, live: false, push_entry: true, phones_receiving: 1, active_calls: 0 }),
    "portico/content/preview": () => ({ endpoint: "x", kind: "phone", rev: "r", pages: [], roster: [], callers: {} }),
    ...handlers,
  }, { loaded });
  const panel = doc.createElement("portico-panel");
  panel._previewMs = 10;
  panel.hass = f.hass;
  doc.body.appendChild(panel);
  await sleep(80);
  panel._pickTab("devices");
  await sleep(40);
  return { f, panel, state };
}
const rowOf = (p, id) => q(p, `[data-device="phone"][data-phone="${id}"]`);
const menuItem = (p, id, name) => rowOf(p, id).querySelector(`[data-act="${name}"]`);

{
  const PLAIN = ROW({});
  const PENDING = ROW({ entry_id: "e-2", name: "Waiting", state: "pending", code_expires_in: 300 });
  const LEGACY = ROW({ entry_id: "legacy-abc", legacy: true, name: "Old Samsung" });
  const PANEL = ROW({ entry_id: "e-4", name: "Kitchen panel", panel: true, panel_screen: "kitchen-panel-app" });
  const { f, panel } = await mount({ rows: [PLAIN, PENDING, LEGACY, PANEL], withPanel: true });
  assert.ok(menuItem(panel, "e-1", "ph-row-panel"), "an enrolled phone entry is offered the action");
  assert.strictEqual(menuItem(panel, "e-1", "ph-row-panel").textContent.trim(), "Make a panel", "no 'screen' in its name");
  assert.strictEqual(menuItem(panel, "e-2", "ph-row-panel"), null, "a phone still waiting for its scan is not");
  assert.strictEqual(menuItem(panel, "legacy-abc", "ph-row-panel"), null, "the Devices tab hides the action for a legacy row");
  assert.strictEqual(menuItem(panel, "e-4", "ph-row-panel"), null, "and for a panel that already is one");
  // the panel's section: a chip, the same two selectors as a phone keyed by the PHONE endpoint id, the wake line, no 'Panel screen' fact
  const prow = rowOf(panel, "e-4");
  assert.ok(prow.querySelector('[data-chip="panel"]'), "the panel chip");
  assert.ok(prow.querySelector('[data-selector="pages"][data-device="kp-phone"]'), "the Pages selector, keyed by its phone endpoint id");
  assert.strictEqual(prow.querySelector('[data-selector="ring-pages"][data-device="kp-phone"]'), null, "no Ring pages selector (D-20261122-4aec)");
  assert.strictEqual(prow.querySelector('[data-selector="pages"][data-device="kitchen-panel-app"]'), null, "never the old screen's key");
  assert.doesNotMatch(prow.textContent, /Panel screen/i, "no 'Panel screen: <slug>' fact and no 'chosen on its screen' hint");
  assert.doesNotMatch(prow.textContent, /Screens section/);
  const rings = prow.querySelector('[data-hint="rings-for"]');
  assert.ok(rings && /^Rings for these doors: none/.test(rings.textContent), "read only: no door Talk element on its pages yet");
  assert.ok(prow.querySelector('[data-selector="pages"]').textContent.includes("Home"), "its first page is Home, as for a phone");
  assert.ok(rowOf(panel, "e-1").querySelector('[data-selector="pages"]'), "an ordinary phone keeps its selectors");
  assert.strictEqual(rowOf(panel, "e-1").querySelector('[data-hint="rings-for"]'), null, "the wake line is the panel's only");
  // the former screen's section: still listed like any screen, marked as former, user fixed, no kiosk address
  const sec = q(panel, '[data-device="screen"][data-screen="kitchen-panel-app"]');
  assert.ok(sec, "the former screen is listed in the Screens section until it is deleted");
  assert.strictEqual(sec.querySelector('[data-chip="kind"]').textContent, "Former panel screen");
  assert.ok(sec.querySelector('[data-act="delete-screen"]'), "its Delete is available");
  assert.match(sec.textContent, /former screen of a panel/);
  console.log("PASS: the action is offered to an enrolled phone entry only; a panel's section has a phone's selectors keyed by its phone id; the former screen is an ordinary row");
}

{
  // no migration button any more: the panel has its own pages, with or without a legacy `panel_screen` on the row
  for (const row of [ROW({ entry_id: "e-4", name: "Kitchen panel", panel: true, panel_screen: "kitchen-panel-app" }), ROW({ entry_id: "e-4", name: "Kitchen panel", panel: true }), ROW({})]) {
    const { panel } = await mount({ rows: [row], withPanel: true });
    assert.strictEqual(act(panel, "adopt-pages"), null, "no Copy pages from the old screen button");
    assert.strictEqual(q(panel, '[data-hint="adopt-pages"]'), null);
  }
  console.log("PASS: there is no migration button");
}

{
  // the action: the page, the call, the refresh
  const rows = [ROW({})];
  const { f, panel, state } = await mount({ rows, handlers: {
    "portico/phones/make_panel": (msg) => {
      state.rows = [{ ...state.rows[0], panel: true }];
      return { result: "updated", phone: state.rows[0] };
    } } });
  const before = f.documentCalls();
  click(menuItem(panel, "e-1", "ph-row-panel"));
  await sleep(10);
  assert.strictEqual(q(panel, '[data-field="ph-panel-slug"]'), null, "no slug field: a panel has no screen address");
  assert.ok(q(panel, '[data-view="panel"]'), "the make-a-panel page");
  assert.strictEqual(q(panel, ".page").dataset.devview, "panel", "a sub-page: the Screens section and the Save bar are hidden");
  assert.doesNotMatch(q(panel, '[data-view="panel"]').textContent, /copy|another device/i, "no copy-from-another-device option");
  click(act(panel, "ph-panel-make"));
  await sleep(60);
  const calls = sent(f, "portico/phones/make_panel");
  assert.strictEqual(calls.length, 1);
  assert.deepStrictEqual(Object.keys(calls[0]).filter((k) => k !== "id").sort(), ["entry_id", "type"], "entry_id only");
  assert.strictEqual(calls[0].entry_id, "e-1");
  assert.strictEqual(sent(f, "portico/phones/make_panel_screen").length, 0, "never the old command");
  assert.strictEqual(q(panel, ".page").dataset.devview, "list", "back to the list");
  assert.ok(rowOf(panel, "e-1").querySelector('[data-chip="panel"]'), "the list was read again: the row is a panel now");
  assert.strictEqual(menuItem(panel, "e-1", "ph-row-panel"), null, "and no longer offers the action");
  assert.match(q(panel, ".phones").textContent, /is now a panel/);
  console.log("PASS: make a panel sends entry_id only and refreshes the list");
}

{
  // a refusal stays on the page with the server's words; a second press while busy sends nothing
  const rows = [ROW({})];
  let n = 0;
  const { f, panel } = await mount({ rows, handlers: {
    "portico/phones/make_panel": () => { n++; throw { code: "not_portico_user", message: "Only a phone whose Home Assistant user Portico made can be a panel", error: { code: "not_portico_user" } }; } } });
  click(menuItem(panel, "e-1", "ph-row-panel"));
  await sleep(10);
  click(act(panel, "ph-panel-make"));
  await sleep(40);
  assert.strictEqual(n, 1);
  assert.ok(q(panel, '[data-view="panel"]'), "still on the page");
  const err = q(panel, '[data-state="ph-error"]');
  assert.ok(err && err.textContent.length > 0, "the refusal is shown");
  click(act(panel, "ph-back"));
  await sleep(40);
  assert.strictEqual(q(panel, ".page").dataset.devview, "list");
  assert.ok(menuItem(panel, "e-1", "ph-row-panel"), "nothing changed: the phone still offers the action");
  console.log("PASS: a refusal stays on the page with the server's message; Back leaves nothing changed");
}

{
  // the older layout (schema 4): no Pages model, no app screen, so no action
  const { panel } = await mount({ rows: [ROW({})], schema6: false });
  const row = q(panel, '[data-phone="e-1"]');
  assert.ok(row, "the phone is listed");
  assert.strictEqual(row.querySelector('[data-act="ph-row-panel"]'), null);
  console.log("PASS: the pre-schema-6 layout offers no panel action");
}

{
  // a panel with no page: the plain no-page hint (no old-screen wording), and the former screen can be deleted at once
  const { panel } = await mount({ rows: [ROW({ entry_id: "e-4", name: "Kitchen panel", panel: true, panel_screen: "kitchen-panel-app" })], withPanel: true });
  const msg = rowOf(panel, "e-4").querySelector('[data-state="no-device-pages"]');
  assert.ok(msg && /This panel has no page yet/.test(msg.textContent) && !/old screen/.test(msg.textContent), "the plain hint next to Add first page");
  const sec = q(panel, '[data-device="screen"][data-screen="kitchen-panel-app"]');
  click(sec.querySelector('[data-act="delete-screen"]'));
  await sleep(20);
  assert.doesNotMatch(panel.shadowRoot.textContent, /still supplies the pages/);
  console.log("PASS: no fallback hint, and deleting a former screen is not refused");
}

console.log("ALL PASS test_panel_make_panel");
process.exit(0);
