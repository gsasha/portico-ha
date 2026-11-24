// D-20261112 phone page: "Add first page" on a phone or screen with no page, and the QR page's next-step line.
// The pure edits (`logic`) and the panel in jsdom with a fake `hass`. Protocol-level; NOT a visual check.
import assert from "assert";
import { loadPanel, editorHass, makeLoaded, sleep } from "./panel_loader.mjs";

const { dom, Panel, doc } = loadPanel();
const L = Panel.logic;
const win = dom.window;
const throwsEdit = (fn, re) => assert.throws(fn, (e) => e instanceof L.EditError && re.test(e.message), String(re));

const T_FRONT = { id: "el-20000001", kind: "talk", name: "Front door", options: { door: "http://10.6.0.7:8091" } };
const T_SIDE = { id: "el-20000002", kind: "talk", name: "Side door", options: { door: "side-1" } };
const T_PHONE = { id: "talk-phone-phone1", kind: "talk", name: "Alice's phone", managed: true, options: { phone: "phone1" } };
const T_KITCHEN = { id: "talk-screen-kitchen", kind: "talk", name: "Kitchen tablet", managed: true, options: { screen: "kitchen" } };
const T_HALL = { id: "talk-screen-hall", kind: "talk", name: "Hall tablet", managed: true, options: { screen: "hall" } };
const ELS = [T_FRONT, T_SIDE, T_PHONE, T_KITCHEN, T_HALL];
const MAP = new Map(ELS.map((e) => [e.id, e]));
const listReply = (elements) => ({ schema_version: 1, rev: "r1", elements, used_by: {}, kinds: {}, icons: [], palette: [], limits: {} });

const q = (p, sel) => p.shadowRoot.querySelector(sel);
const qa = (p, sel) => Array.from(p.shadowRoot.querySelectorAll(sel));
const act = (p, name) => q(p, `[data-act="${name}"]`);
const click = (node) => { assert.ok(node, "clicked something that is not there"); node.dispatchEvent(new win.Event("click", { bubbles: true })); };
function setValue(node, value, type = "change") { assert.ok(node); node.value = value; node.dispatchEvent(new win.Event(type, { bubbles: true })); }

const page = (id, label, ...talks) => ({ id, label, kind: "content", blocks: [{ id: "b", title: label, cameras: [], actions: talks.map((t) => ({ id: `a-${t}`, element: t })) }] });
const ringPage = (id, talk) => ({ id, label: id, kind: "ring", talk, cameras: null, blocks: [] });

/* phone1 shows `home`; kitchen shows `front` (the front door's Talk element); hall shows nothing. */
function doc6() {
  return {
    schema_version: 6, home_name: "My Home",
    pages: [page("home", "Home"), page("front", "Front", T_FRONT.id), page("side", "Side", T_SIDE.id), ringPage("front-ring", T_FRONT.id), ringPage("front-night", T_FRONT.id),
            ringPage("alice-ring", T_PHONE.id)],
    devices: { phone1: { pages: [{ page: "home" }], ring: { [T_FRONT.id]: "front-ring" }, roster_hide: [] },
               kitchen: { pages: [{ page: "front" }], ring: {}, roster_hide: [] }, hall: { pages: [], ring: {}, roster_hide: [] } },
    dashboards: { kitchen: { name: "Kitchen tablet", ha_user_id: "u2", cameras: [] }, hall: { name: "Hall tablet", ha_user_id: "u3", cameras: [] } },
    endpoints: {},
  };
}

// ---- 5. the panel: Devices tab, schema 6 ---------------------------------------------------------------------------------------------------
const PHONE_ROWS = [{ entry_id: "e-1", legacy: false, name: "Alice's phone", state: "enrolled", expired: false, code_expires_in: null, enrolled_at: "2026-10-08T07:00:00+00:00",
  last_connection: "2026-11-09T07:00:00+00:00", push_registered: true, doors: null, away_ring: null, house_receive: false }];
async function mount(document) {
  const loaded = makeLoaded();
  loaded.document = document;
  loaded.orphans = [];
  loaded.resolved = {};
  loaded.endpoints.push({ id: "hall", kind: "dashboard", name: "Hall tablet", label: "Hall tablet", ha_user_id: "u3", cameras: [], room: null, available: true, reason: null });
  const f = editorHass({ "portico/elements/list": () => listReply(ELS), "portico/phones/list": () => ({ header: {}, phones: PHONE_ROWS }), "portico/house_calls/get": () => ({ enabled: false, live: false, push_entry: true, phones_receiving: 1, active_calls: 0 }),
    "portico/content/preview": () => ({ endpoint: "x", kind: "phone", rev: "r", pages: [], roster: [], callers: {} }) }, { loaded });
  const panel = doc.createElement("portico-panel");
  panel._previewMs = 10;
  panel.hass = f.hass;
  doc.body.appendChild(panel);
  await sleep(80);
  panel._pickTab("devices");
  await sleep(40);
  return { f, panel };
}

// ---- 1. pure ----
{
  const d = doc6();
  d.devices.phone1.pages = [];
  const r = L.addFirstPage(d, "phone1", "Dina's phone", MAP);
  assert.strictEqual(r.doc.pages.length, d.pages.length + 1, "one new library page");
  const made = r.doc.pages.find((p) => p.id === r.pageId);
  assert.strictEqual(made.label, "Dina's phone");
  assert.strictEqual(made.kind, "content");
  assert.deepStrictEqual(L.devicePageIds(r.doc, "phone1"), [r.pageId], "it is the device's Home");
  assert.deepStrictEqual(r.doc.devices.phone1.ring, d.devices.phone1.ring, "the ring assignment is left alone");
  assert.strictEqual(d.pages.length, 6, "the old document is untouched");
  // a device with no entry at all (a just-enrolled phone)
  const n = L.addFirstPage(d, "fresh", "New", MAP);
  assert.deepStrictEqual(L.devicePageIds(n.doc, "fresh"), [n.pageId]);
  // a device that has a page is refused; two devices of one name get two pages
  throwsEdit(() => L.addFirstPage(d, "kitchen", "Kitchen tablet", MAP), /already has a page/);
  const two = L.addFirstPage(r.doc, "hall", "Dina's phone", MAP);
  assert.notStrictEqual(two.pageId, r.pageId);
  // the library limit refuses the whole step
  const big = doc6();
  for (let i = 0; i < 40; i++) big.pages.push(page(`p${i}`, `P${i}`));
  let refused = false;
  try { L.addFirstPage(big, "hall", "Hall tablet", MAP); } catch (e) { refused = e instanceof L.EditError; }
  if (refused) assert.strictEqual(L.devicePageIds(big, "hall").length, 0);
  console.log("PASS: addFirstPage is one pure step, owned by the device, ring untouched");
}

// ---- 2. the panel ----
{
  const d = doc6();
  d.devices.phone1.pages = [];
  const { f, panel } = await mount(d);
  const sec = q(panel, '[data-device="phone"][data-phone="e-1"]');
  const prompt = sec.querySelector('[data-state="no-device-pages"]');
  assert.ok(prompt && /This phone has no page yet/.test(prompt.textContent), "the prompt in the phone's section");
  const hallPrompt = q(panel, '[data-device="screen"][data-screen="kitchen"] [data-selector="pages"]');
  assert.ok(!hallPrompt.querySelector('[data-state="no-device-pages"]'), "a screen with a page has no prompt");
  assert.match(q(panel, '[data-device="screen"][data-screen="hall"] [data-state="no-device-pages"]').textContent, /This screen has no page yet/);
  const saves = () => f.ofType("portico/content/save").length;
  click(prompt.querySelector('[data-act="add-first-page"]'));
  await sleep(40);
  const st = panel._state;
  const ids = L.devicePageIds(st.draft, "phone1");
  assert.strictEqual(ids.length, 1);
  assert.strictEqual(st.pageId, ids[0], "opened in the editor");
  assert.strictEqual(panel._tab, "pages");
  assert.strictEqual(L.pagesOf(st.draft, L.LIBRARY).find((p) => p.id === ids[0]).label, "Alice's phone");
  assert.strictEqual(saves(), 0, "it is a draft edit; the Save bar writes it");
  panel.remove();
  console.log("PASS: the prompt, the one-click action and the editor opening");
}

// ---- 3. the QR page names the next step ----
{
  const { panel } = await mount(doc6());
  assert.match(panel._phones.nextStep(), /give it a page/);
  panel.remove();
  console.log("PASS: next-step text offered for schema 6");
}
console.log("ALL PASS test_panel_first_page");
process.exit(0);
