// Schema 6 "Not attached": a leftover device entry (a deleted former panel screen) is listed on the Devices tab and can be deleted
// (named in drop_orphans on Save), and it is no phone for the group-call placement check. Protocol-level in jsdom; NOT a visual check.
import assert from "assert";
import { loadPanel, editorHass, makeLoaded, sleep } from "./panel_loader.mjs";

const { dom, Panel, doc } = loadPanel();
const L = Panel.logic;
const win = dom.window;

const T_GROUP = { id: "el-20000003", kind: "talk", name: "Call phones", options: { group: "phones" } };
const page = (id, label, ...talks) => ({ id, label, kind: "content", blocks: [{ id: "b", title: label, cameras: [], actions: talks.map((t) => ({ id: `a-${t}`, element: t })) }] });
const listReply = (elements) => ({ schema_version: 1, rev: "r1", elements, used_by: {}, kinds: {}, icons: [], palette: [], limits: {} });

function doc6() {
  return {
    schema_version: 6, home_name: "My Home", pages: [page("playroom", "Playroom"), page("g", "Group")],
    devices: { "panel-phone": { pages: [{ page: "playroom" }], ring: {}, roster_hide: [] }, "playroom-panel-app": { pages: [{ page: "playroom" }], ring: {}, roster_hide: [] } },
    dashboards: {}, endpoints: {},
  };
}
const state6 = () => { const loaded = makeLoaded(); loaded.document = doc6(); loaded.orphans = ["playroom-panel-app"]; return L.createState(loaded); };

{
  const s = state6();
  assert.deepStrictEqual(L.orphanKeys(s), ["playroom-panel-app"], "a schema 6 leftover device entry is listed");
  L.deleteOrphan(s, "playroom-panel-app");
  assert.ok(!("playroom-panel-app" in s.draft.devices) && "panel-phone" in s.draft.devices);
  assert.deepStrictEqual(L.saveRequest(s).drop_orphans, ["playroom-panel-app"], "the delete is named to the server");
  assert.strictEqual(L.saveRequest(s).document.devices["panel-phone"].pages[0].page, "playroom", "the panel keeps its pages in the same request");
  console.log("PASS: schema 6 leftover device entry: listed, deleted, named in drop_orphans");
}

{
  // the group call button on a library page that a leftover entry also holds
  const loaded = makeLoaded(); loaded.document = doc6(); loaded.document.devices["panel-phone"].pages = [{ page: "playroom" }]; loaded.orphans = ["playroom-panel-app"];
  L.setPanelHosts([{ id: "panel-phone", user: "u9", name: "Playroom Panel" }]);
  L.setLeftoverKeys([]);
  assert.throws(() => L.addButtonRef(loaded.document, L.LIBRARY || "@library", "playroom", T_GROUP, {}), /only be placed on a screen|group call/i, "without the leftover list the entry counts as a phone (the old behaviour)");
  L.setLeftoverKeys(["playroom-panel-app"]);
  const out = L.addButtonRef(loaded.document, L.LIBRARY || "@library", "playroom", T_GROUP, {});
  assert.ok(out.pages.find((p) => p.id === "playroom").blocks[0].actions.some((a) => a.element === T_GROUP.id), "a leftover entry is no phone: the button is placed");
  L.setLeftoverKeys([]);
  console.log("PASS: a leftover device entry is no phone for the group call placement check");
}

{
  const loaded = makeLoaded();
  loaded.document = doc6(); loaded.orphans = ["playroom-panel-app"]; loaded.resolved = {};
  const f = editorHass({ "portico/elements/list": () => listReply([T_GROUP]), "portico/phones/list": () => ({ header: {}, phones: [] }),
    "portico/house_calls/get": () => ({ enabled: false, live: false, push_entry: true, phones_receiving: 0, active_calls: 0 }),
    "portico/content/preview": () => ({ endpoint: "x", kind: "phone", rev: "r", pages: [], roster: [], callers: {} }) }, { loaded });
  const panel = doc.createElement("portico-panel");
  panel._previewMs = 10;
  panel.hass = f.hass;
  doc.body.appendChild(panel);
  await sleep(80);
  panel._pickTab("devices");
  await sleep(40);
  const row = panel.shadowRoot.querySelector('[data-leftover="playroom-panel-app"]');
  assert.ok(row, "the Devices tab lists the leftover entry under Not attached");
  row.querySelector('[data-act="leftover-delete"]').dispatchEvent(new win.Event("click", { bubbles: true }));
  await sleep(20);
  assert.ok(!panel.shadowRoot.querySelector('[data-leftover="playroom-panel-app"]'), "deleted in the draft");
  panel.shadowRoot.querySelector('[data-act="save"]') && panel.shadowRoot.querySelector('[data-act="save"]').dispatchEvent(new win.Event("click", { bubbles: true }));
  await sleep(40);
  const save = f.ofType("portico/content/save").at(-1);
  assert.ok(save, "Save sent");
  assert.deepStrictEqual(save.drop_orphans, ["playroom-panel-app"]);
  assert.ok(!("playroom-panel-app" in save.document.devices));
  console.log("PASS: the Devices tab Not attached row deletes the leftover entry on Save");
}
process.exit(0);
