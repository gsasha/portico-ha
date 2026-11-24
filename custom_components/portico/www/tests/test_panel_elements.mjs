// DEV-760 E4: the read-only Elements tab (portico-elements.js) inside the panel: the tab and its fallbacks, groups by kind,
// the empty and error states, live previews on a states change (setHass) and on a state_changed event (the fallback while
// HA's `set hass` timing is unverified), admin only, and a grep that no HTML-string setter appears. jsdom with a fake
// `hass`; NOT a real browser or a real Home Assistant frontend.
import assert from "assert";
import fs from "fs";
import { loadPanel, editorHass, importElements, sleep } from "./panel_loader.mjs";

const EL = await importElements();
const { groupByKind, EMPTY_TEXT } = EL;

const ELEMENTS = [
  { id: "el-00000001", kind: "status", name: "Front <b>door</b>", entity_id: "lock.front", options: { states: { locked: { label: "Bolted", color: "teal" } } } },
  { id: "el-00000002", kind: "camera", name: "Gate cam", entity_id: "camera.gate", options: {} },
  { id: "el-00000003", kind: "button", name: "Open gate", entity_id: "lock.gate", options: { action: "lock_unlock", color: "blue" } },
  { id: "el-00000004", kind: "status", name: "Porch light", entity_id: "light.porch", options: {} },
  { id: "el-00000005", kind: "chip", name: "Garage", entity_id: "cover.garage", options: {} },
  { id: "el-00000006", kind: "hologram", name: "Future thing", options: {} },
];
const KINDS = { camera: { label: "Camera" }, button: { label: "Button" }, status: { label: "Status" }, chip: { label: "Chip" } };
const reply = (elements, o = {}) => ({ schema_version: 1, rev: o.rev || "r1", elements, used_by: o.used_by || {}, kinds: o.kinds || KINDS, icons: o.icons || [], palette: o.palette || [], limits: o.limits || {} });
const states = (extra = {}) => ({
  "lock.front": { state: "locked", attributes: {} },
  "camera.gate": { state: "idle", attributes: { entity_picture: "/api/camera_proxy/camera.gate?token=t" } },
  "lock.gate": { state: "locked", attributes: {} },
  "light.porch": { state: "on", attributes: {} },
  "cover.garage": { state: "closed", attributes: {} },
  ...extra,
});

async function mount(opts = {}) {
  const w = loadPanel({ elements: opts.elements, elementsRender: opts.elementsRender, openOnElements: opts.openOnElements });
  const doc = w.doc;
  const calls = { list: 0 };
  const listeners = new Set();
  const handlers = {
    "portico/elements/list": () => { calls.list++; if (opts.listError) throw opts.listError; return reply(opts.list || ELEMENTS, opts); },
    ...(opts.handlers || {}),
    "portico/phones/list": () => ({ header: {}, phones: [] }),
    "portico/door_status": () => ({ hub: {}, doors: [], as_of: "x" }),
    "portico/door_settings/get": () => ({ doors: [], helpers: [], effects: {} }),
    "portico/ui_settings/get": () => ({ push_entry: true, running: true, ring_enabled: true, ring_window: 45, ring_window_min: 10, ring_window_max: 120, new_phones_enabled: false, test_phones: [], phones: [] }),
    "portico/house_calls/get": () => ({ enabled: false, live: false, push_entry: true, phones_receiving: 0, active_calls: 0, audio_bridge: true }),
  };
  const fake = editorHass(handlers, { user: opts.user, states: states(opts.states) });
  if (opts.events !== false) {
    fake.hass.connection.subscribeEvents = async (cb, type) => {
      assert.strictEqual(type, "state_changed");
      const h = { cb };
      listeners.add(h);
      return () => listeners.delete(h);
    };
  }
  const panel = doc.createElement("portico-panel");
  panel._previewMs = 20;
  panel.hass = fake.hass;
  doc.body.appendChild(panel);
  await sleep(60);
  return { ...w, win: w.dom.window, panel, fake, calls, listeners, ctl: () => panel._elements,
    emit: (entity_id, new_state) => { for (const h of Array.from(listeners)) h.cb({ event_type: "state_changed", data: { entity_id, new_state } }); },
    // what HA does on a change: a NEW hass object with a NEW states map
    setStates: (patch) => { const next = { ...fake.hass, states: { ...fake.hass.states, ...patch } }; panel.hass = next; fake.hass = next; } };
}
const q = (p, sel) => p.shadowRoot.querySelector(sel);
const qa = (p, sel) => Array.from(p.shadowRoot.querySelectorAll(sel));
const act = (p, name) => q(p, `[data-act="${name}"]`);
const click = (win, node) => { assert.ok(node, "clicked something that is not there"); node.dispatchEvent(new win.Event("click", { bubbles: true })); };
const tabs = (p) => qa(p, "header .tab").map((b) => b.textContent);
async function openElements(m) { click(m.win, act(m.panel, "tab-elements")); await sleep(40); }
const rowOf = (p, id) => q(p, `.el-row[data-element="${id}"]`);
const valueOf = (p, id) => rowOf(p, id).querySelector(".pe-value").textContent;

// ---- 0. DEV-518: the Talk element's door picker data and its draft round trip -------------------------------------------------
{
  const doors = [{ door_id: "gate-obs", name: "Gate observer", driver: "gate_sip_observer", can_dial: true, available: true },
    { door_id: "bell", name: "Doorbell", driver: "ha_event", can_dial: false, available: true },
    { door_id: "gate-srv", name: "Gate server", driver: "gate_sip_server", can_dial: true, available: false }];
  const c = EL.doorChoices(doors, "gone");
  assert.strictEqual(c[0].value, "gone");
  assert.ok(/door removed/.test(c[0].label), "a stored door that is no longer listed stays selectable and says so");
  assert.ok(/gate_sip_observer, can dial now/.test(c.find((x) => x.value === "gate-obs").label));
  assert.ok(/ha_event, cannot dial now/.test(c.find((x) => x.value === "bell").label));
  assert.ok(/can dial, not connected/.test(c.find((x) => x.value === "gate-srv").label));
  assert.deepStrictEqual(EL.doorChoices(null, "").length, 0);
  assert.strictEqual(EL.doorNote({ kind: "talk", options: { door: "gone" } }, doors), "door removed");
  assert.strictEqual(EL.doorNote({ kind: "talk", options: { door: "bell" } }, doors), "");
  assert.strictEqual(EL.doorNote({ kind: "button", options: {} }, doors), "");
  const spec = { fields: [{ name: "door", type: "door", required: true }] };
  const draft = EL.elementToDraft({ id: "el-0000aaaa", kind: "talk", name: "T", icon: "phone", options: { door: "bell" } });
  assert.strictEqual(draft.door, "bell");
  assert.deepStrictEqual(EL.draftToElement(spec, draft, "el-0000aaaa"), { kind: "talk", name: "T", id: "el-0000aaaa", icon: "phone", options: { door: "bell" } });
  assert.deepStrictEqual(EL.KIND_ORDER, ["talk", "button", "camera", "status", "chip"], "Alice: talk, button, camera, status, chip");
  console.log("PASS: the Talk element's door picker shows driver and can-dial, keeps a removed door, and round-trips the draft");
}

// ---- 1. groups by kind, in a fixed order, unknown kinds last --------------------------------------------------------------
{
  const g = groupByKind(ELEMENTS, KINDS);
  assert.deepStrictEqual(g.map((x) => x.kind), ["button", "camera", "status", "chip", "hologram"]);
  assert.deepStrictEqual(g.map((x) => x.label), ["Button", "Camera", "Status", "Chip", "hologram"], "an unlisted kind is labelled by its own name");
  assert.deepStrictEqual(g[2].elements.map((e) => e.id), ["el-00000001", "el-00000004"], "the list order is kept inside a group");
  assert.deepStrictEqual(groupByKind(null, null), []);
  assert.deepStrictEqual(groupByKind([null, 3, { name: "x" }], {}).map((x) => x.kind), ["unknown"]);
  console.log("PASS: elements group by kind in a fixed order; unknown kinds come last; junk never throws");
}

// ---- 2. the tab renders the groups with previews, read-only -----------------------------------------------------------------
{
  const m = await mount();
  assert.deepStrictEqual(tabs(m.panel).slice(0, 1), ["Elements"], "the first tab");
  assert.strictEqual(m.calls.list, 0, "nothing is read before the tab is opened");
  await openElements(m);
  assert.strictEqual(m.calls.list, 1);
  assert.deepStrictEqual(qa(m.panel, ".el-group").map((g) => g.dataset.kind), ["button", "camera", "status", "chip", "hologram"]);
  assert.deepStrictEqual(qa(m.panel, ".el-group h2").map((h) => h.textContent), ["Button (1)", "Camera (1)", "Status (2)", "Chip (1)", "hologram (1)"]);
  assert.strictEqual(qa(m.panel, ".el-row").length, 6);
  assert.strictEqual(valueOf(m.panel, "el-00000001"), "Bolted", "the element's own rule");
  assert.strictEqual(valueOf(m.panel, "el-00000004"), "On");
  assert.ok(rowOf(m.panel, "el-00000002").querySelector("img").getAttribute("src").startsWith("/api/camera_proxy/camera.gate"));
  const btn = rowOf(m.panel, "el-00000003").querySelector("button.pe-btn");
  assert.ok(btn.disabled, "a button preview is inert");
  assert.match(rowOf(m.panel, "el-00000006").textContent, /Unknown kind/);
  // hostile text is text, not markup
  assert.strictEqual(rowOf(m.panel, "el-00000001").querySelector(".el-name").textContent, "Front <b>door</b>");
  assert.strictEqual(qa(m.panel, ".elements b").length, 0);
  // listing alone writes nothing; the controls are Add, and per known-kind row Edit, Rename, Remove (E5)
  assert.ok(act(m.panel, "tab-elements") && q(m.panel, '[data-el-act="add"]'));
  assert.ok(!qa(m.panel, "button").some((b) => b.textContent === "Add element"), "no global Add element button");
  assert.strictEqual(qa(m.panel, '[data-el-act="edit"]').length, 5, "no Edit on the unknown kind");
  assert.strictEqual(qa(m.panel, '[data-el-act="remove"]').length, 6, "every row can be removed");
  assert.deepStrictEqual(m.fake.sent.filter((x) => /elements\/(set|delete)/.test(x.type)), []);
  assert.strictEqual(q(m.panel, "header .grow"), null, "no Undo / Discard / Save header on this tab");
  console.log("PASS: the Elements tab lists the groups with previews and writes nothing by itself");
}

// ---- 3. empty and error states -------------------------------------------------------------------------------------------
{
  const m = await mount({ list: [] });
  await openElements(m);
  const empty = q(m.panel, '[data-elements="empty"]');
  assert.ok(empty);
  assert.strictEqual(empty.textContent, EMPTY_TEXT);
  assert.deepStrictEqual(qa(m.panel, ".el-group").map((g) => g.dataset.kind), ["button", "camera", "status", "chip"], "every kind still has its (empty) box");
  assert.strictEqual(qa(m.panel, '.el-group [data-el-act="add"]').length, qa(m.panel, ".el-group").length, "each empty box keeps its Add button");
  console.log("PASS: no elements shows the empty-state text, and every kind box keeps its Add button");
}
{
  const m = await mount({ listError: { code: "unknown_command", message: "Unknown command." } });
  await openElements(m);
  assert.match(q(m.panel, '[data-elements="error"]').textContent, /Restart Home Assistant/);
  const m2 = await mount({ listError: { code: "unauthorized", message: "x" } });
  await openElements(m2);
  assert.match(q(m2.panel, '[data-elements="error"]').textContent, /administrator/);
  const m3 = await mount({ listError: new Error("socket closed") });
  await openElements(m3);
  assert.ok(q(m3.panel, '[data-elements="error"]').textContent.length > 0);
  assert.strictEqual(qa(m3.panel, ".el-row").length, 0);
  console.log("PASS: a failed read says why (restart, not an admin, other) and draws no rows");
}
{
  // a re-read after an error keeps the old list on screen with the error above it
  const m = await mount();
  await openElements(m);
  const ctl = m.ctl();
  m.fake.hass.callWS = async () => { throw { code: "unknown_command" }; };
  await ctl.load();
  assert.ok(q(m.panel, '[data-elements="error"]'));
  assert.strictEqual(qa(m.panel, ".el-row").length, 6, "the last good list stays");
  console.log("PASS: a failed re-read keeps the last good list under the error");
}

// ---- 4. live: setHass (the panel's `set hass`) and the state_changed fallback ---------------------------------------------
{
  const m = await mount();
  await openElements(m);
  assert.strictEqual(m.listeners.size, 1, "the fallback feed is subscribed while the tab is open");
  // 4a. HA calls `set hass` with new states
  m.setStates({ "lock.front": { state: "unlocked", attributes: {} } });
  assert.strictEqual(valueOf(m.panel, "el-00000001"), "Unlocked");
  // 4b. HA does NOT call `set hass`: only the event arrives
  m.emit("light.porch", { state: "off", attributes: {} });
  assert.strictEqual(valueOf(m.panel, "el-00000004"), "Off");
  assert.strictEqual(valueOf(m.panel, "el-00000001"), "Unlocked", "an event for another entity changes nothing else");
  // an entity that appears and one that is removed
  m.emit("light.porch", null);
  assert.strictEqual(valueOf(m.panel, "el-00000004"), "Entity not found");
  m.emit("light.porch", { state: "on", attributes: {} });
  assert.strictEqual(valueOf(m.panel, "el-00000004"), "On");
  // a later `set hass` carrying the same truth does not undo the event
  m.setStates({});
  assert.strictEqual(valueOf(m.panel, "el-00000004"), "On");
  // junk events never throw
  for (const bad of [undefined, null, {}, { data: {} }, { data: { entity_id: 3 } }]) m.ctl()._onStateChanged(bad);
  console.log("PASS: previews follow both set hass and a state_changed event (the fallback), per entity");
}
{
  // no event API on the connection: setHass alone still works, nothing throws
  const m = await mount({ events: false });
  await openElements(m);
  m.setStates({ "light.porch": { state: "off", attributes: {} } });
  assert.strictEqual(valueOf(m.panel, "el-00000004"), "Off");
  console.log("PASS: without subscribeEvents the tab still follows set hass");
}
{
  // leaving the tab stops the feed; coming back starts one and reads the list again; closing the page drops everything
  const m = await mount();
  await openElements(m);
  assert.strictEqual(m.listeners.size, 1);
  click(m.win, act(m.panel, "tab-pages"));
  await sleep(20);
  assert.strictEqual(m.listeners.size, 0, "no event feed on other tabs");
  await openElements(m);
  assert.strictEqual(m.listeners.size, 1);
  assert.strictEqual(m.calls.list, 2);
  const ctl = m.ctl();
  m.panel.remove();
  assert.strictEqual(m.listeners.size, 0, "closing the panel unsubscribes");
  assert.strictEqual(m.panel._elements, null);
  assert.ok(ctl.disposed);
  console.log("PASS: the event feed lives only while the tab is open");
}
{
  // a theme flip repaints with the dark palette
  const m = await mount();
  await openElements(m);
  const before = rowOf(m.panel, "el-00000003").querySelector("button.pe-btn").style.background;
  const next = { ...m.fake.hass, themes: { darkMode: true } };
  m.panel.hass = next;
  const after = rowOf(m.panel, "el-00000003").querySelector("button.pe-btn").style.background;
  assert.notStrictEqual(after, before);
  console.log("PASS: a theme flip redraws the previews");
}

// ---- 5. admin only; import failures leave the other tabs working ------------------------------------------------------------
{
  const m = await mount({ user: { is_admin: false } });
  assert.strictEqual(act(m.panel, "tab-elements"), null);
  assert.strictEqual(m.ctl(), null);
  assert.strictEqual(m.calls.list, 0);
  assert.strictEqual(m.fake.sent.filter((x) => x.type.startsWith("portico/elements/")).length, 0);
  console.log("PASS: a non-administrator gets no Elements tab and no elements call");
}
// ---- the panel opens on Elements, the first tab (D-20261109-985c); no Overview tab ----------------------------------------
{
  const m = await mount({ openOnElements: true });
  assert.deepStrictEqual(tabs(m.panel), ["Elements", "Pages", "Devices", "Ring & call", "Settings", "Status"], "Elements first, no Overview");
  assert.strictEqual(act(m.panel, "tab-overview"), null, "no Overview tab");
  assert.deepStrictEqual(qa(m.panel, "header .tab.on").map((b) => b.textContent), ["Elements"], "it opens on Elements");
  assert.ok(m.calls.list >= 1, "the list is read");
  click(m.win, act(m.panel, "tab-pages"));
  await sleep(30);
  assert.deepStrictEqual(qa(m.panel, "header .tab.on").map((b) => b.textContent), ["Pages"], "a picked tab is kept");
  console.log("PASS: the panel opens on Elements, the first tab; there is no Overview tab");
  const bad = await mount({ openOnElements: true, elements: false });
  assert.deepStrictEqual(qa(bad.panel, "header .tab.on").map((b) => b.textContent), ["Pages"], "without the Elements module it opens on Pages");
  assert.strictEqual(act(bad.panel, "tab-overview"), null);
  console.log("PASS: without the Elements module the panel opens on Pages");
}
for (const [label, opts] of [["the tab module", { elements: false }], ["the renderer module", { elementsRender: false }]]) {
  const m = await mount(opts);
  assert.strictEqual(act(m.panel, "tab-elements"), null, `${label} failed: no tab`);
  assert.strictEqual(m.ctl(), null);
  assert.deepStrictEqual(tabs(m.panel), ["Pages", "Devices", "Ring & call", "Settings", "Status"], "the other tabs are all there");
  click(m.win, act(m.panel, "tab-devices"));
  await sleep(30);
  assert.ok(q(m.panel, "[data-phones]") || qa(m.panel, "header .tab.on").some((b) => b.textContent === "Devices"), "Phones still opens");
  click(m.win, act(m.panel, "tab-pages"));
  await sleep(30);
  assert.ok(qa(m.panel, "header .tab.on").some((b) => b.textContent === "Pages"), "the content tabs still work");
  console.log(`PASS: when ${label} fails to import there is no Elements tab and the other tabs work`);
}

// ---- 6. the tab is also there when the content store is broken -------------------------------------------------------------
{
  const w = loadPanel();
  const fake = editorHass({ "portico/elements/list": () => reply(ELEMENTS), "portico/content/document": () => { throw { code: "unknown_command" }; } }, { states: states() });
  const panel = w.doc.createElement("portico-panel");
  panel.hass = fake.hass;
  w.doc.body.appendChild(panel);
  await sleep(60);
  const bar = Array.from(panel.shadowRoot.querySelectorAll("header .tab")).map((b) => b.textContent);
  assert.ok(bar.includes("Content") && bar.includes("Elements"), bar.join());
  panel.shadowRoot.querySelector('[data-act="tab-elements"]').dispatchEvent(new w.dom.window.Event("click", { bubbles: true }));
  await sleep(40);
  assert.strictEqual(panel.shadowRoot.querySelectorAll(".el-row").length, 6);
  console.log("PASS: with the content store broken the Elements tab is still reachable");
}

// ---- 7. no HTML string setter ----------------------------------------------------------------------------------------------
{
  for (const f of ["../portico-elements.js", "../portico-elements-render.js"]) {
    const src = fs.readFileSync(new URL(f, import.meta.url), "utf8").replace(/\/\/.*$/gm, "");
    for (const bad of ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"]) assert.ok(!src.includes(bad), `${f} uses ${bad}`);
  }
  const panelSrc = fs.readFileSync(new URL("../portico-panel.js", import.meta.url), "utf8");
  assert.ok(!/elements[^\n]*innerHTML/i.test(panelSrc));
  console.log("PASS: no HTML-string setter in the Elements tab module");
}

// =================================================== E5: add / edit / rename / remove ===================================================
const FULL_KINDS = {
  camera: { label: "Camera", entity_domain: "camera", fields: [{ name: "entity_id", type: "entity", required: false }, { name: "small_entity_id", type: "entity", required: false }, { name: "stream", type: "stream", required: false }, { name: "size", type: "choice", required: false }] },
  button: { label: "Button", entity_domain: null, fields: [{ name: "action", type: "choice", required: true }, { name: "entity_id", type: "entity", required: false }, { name: "target", type: "text", required: false }, { name: "color", type: "choice", required: false }],
    actions: [{ action: "lock_unlock", entity_domain: "lock" }, { action: "portico_open", entity_domain: null }, { action: "cover_open", entity_domain: "cover" }] },
  status: { label: "Status", entity_domain: null, fields: [{ name: "entity_id", type: "entity", required: true }, { name: "states", type: "states", required: false }, { name: "attribute", type: "text", required: false }] },
  chip: { label: "Chip", entity_domain: null, fields: [{ name: "entity_id", type: "entity", required: true }, { name: "states", type: "states", required: false }, { name: "attribute", type: "text", required: false }, { name: "short", type: "text", required: false }] },
};
const FULL = { kinds: FULL_KINDS, icons: ["door", "lock", "lightbulb"], icon_policy: { camera: "fixed", button: "fixed", status: "mdi", chip: "mdi" }, palette: ["blue", "green", "red"], limits: { max_elements: 200, name: 40, states: 16, state: 40, short: 12, stream: 256 } };
// the kind is fixed once the form is open: switching means leaving it and using that kind's own Add button
const kindTo = (win, panel, kind) => { const c = q(panel, '.el-form [data-el-act="cancel"]'); if (c) click(win, c); click(win, q(panel, `[data-el-act="add"][data-kind="${kind}"]`)); };
const setv = (win, node, value) => { assert.ok(node, "no such field"); node.value = value; node.dispatchEvent(new win.Event(node.tagName === "SELECT" ? "change" : "input", { bubbles: true })); };
const fld = (p, name) => q(p, `.el-form [data-field="${name}"]`);
const fieldNames = (p) => qa(p, ".el-form [data-field-wrap]").map((w) => w.dataset.fieldWrap);
const err = (p, name) => q(p, `.el-form [data-err="${name}"]`).textContent;
const slot = (p) => q(p, ".el-previewslot");

/* A small fake server behind portico/elements/*: validates like the real one in the ways the page can see. */
function fakeServer(initial, extra = {}) {
  const srv = { elements: JSON.parse(JSON.stringify(initial)), rev: 1, sets: [], validates: [], deletes: [], force: extra.force || {} };
  const nameTaken = (el) => srv.elements.some((e) => e.id !== el.id && e.name.trim().toLowerCase() === el.name.trim().toLowerCase());
  srv.handlers = {
    "portico/elements/list": () => ({ ...reply(srv.elements, { ...FULL, rev: `r${srv.rev}`, used_by: srv.used_by || {} }) }),
    "portico/elements/set": (msg) => {
      const el = msg.element;
      if (srv.force.set) throw srv.force.set;
      if (msg.base_rev !== `r${srv.rev}`) throw { code: "conflict", message: "stale" };
      if (!el.name || !el.name.trim()) throw { code: "invalid", field: "name", message: "name must be 1 to 40 characters" };
      if (nameTaken(el)) throw { code: "name_taken", message: "taken" };
      if ((el.kind === "status" || el.kind === "chip") && !el.entity_id) throw { code: "invalid", field: "entity_id", message: "needs an entity" };
      if (srv.retarget && el.id && !msg.confirm) {       // the real server: a used element's entity cannot change without confirm
        const was = srv.elements.find((e) => e.id === el.id);
        if (was && was.entity_id !== el.entity_id) throw { code: "in_use_retarget", message: "used", used_by: [{ endpoint: "template", label: "Home > Gate" }, { endpoint: "kitchen", label: "Kitchen > Doors" }] };
      }
      if (msg.validate_only) { srv.validates.push(msg); return { element: el, rev: `r${srv.rev}`, valid: true }; }
      srv.sets.push(msg);
      const stored = { ...el, id: el.id || `el-${String(900 + srv.sets.length).padStart(8, "0")}` };
      const i = srv.elements.findIndex((e) => e.id === stored.id);
      if (i >= 0) srv.elements[i] = stored; else srv.elements.push(stored);
      srv.rev++;
      return { element: stored, rev: `r${srv.rev}` };
    },
    "portico/elements/delete": (msg) => {
      srv.deletes.push(msg);
      if (srv.force.delete && !msg.confirm) throw srv.force.delete;
      srv.elements = srv.elements.filter((e) => e.id !== msg.element_id);
      srv.rev++;
      return { rev: `r${srv.rev}` };
    },
  };
  return srv;
}
async function mountE5(srv, opts = {}) {
  const m = await mount({ handlers: srv.handlers, ...opts });
  m.ctl().validateMs = 5;
  await openElements(m);
  return m;
}
const BASE = [
  { id: "el-00000001", kind: "status", name: "Front door", entity_id: "lock.front", options: {} },
  { id: "el-00000002", kind: "camera", name: "Gate cam", entity_id: "camera.gate", options: {} },
  { id: "el-00000003", kind: "button", name: "Open gate", entity_id: "lock.gate", options: { action: "lock_unlock", color: "blue" } },
];

// ---- 8. the Add form is built per kind from the reply -----------------------------------------------------------------------
{
  const srv = fakeServer(BASE);
  const m = await mountE5(srv);
  const { win, panel } = m;
  assert.deepStrictEqual(qa(panel, '.elements [data-el-act="add"]').map((b) => b.textContent), ["Add button", "Add camera", "Add status", "Add chip"], "one Add button per kind box, from the reply, in the fixed order; none global");
  assert.deepStrictEqual(qa(panel, '.elements [data-el-act="add"]').map((b) => b.closest(".el-group").dataset.kind), ["button", "camera", "status", "chip"], "each sits in its own kind box, empty boxes included");
  click(win, q(panel, '[data-el-act="add"][data-kind="camera"]'));
  assert.ok(q(panel, '[data-elements="form"]'));
  assert.strictEqual(fld(panel, "kind").textContent, "Camera", "the kind is fixed text on a new element");
  assert.notStrictEqual(fld(panel, "kind").tagName, "SELECT");
  kindTo(win, panel, "camera");
  assert.deepStrictEqual(fieldNames(panel), ["kind", "name", "icon", "source", "entity_id", "small_entity_id", "size"], "camera, entity source: two pickers (DEV-768)");
  assert.deepStrictEqual(Array.from(fld(panel, "entity_id").options).map((o) => o.value), ["", "camera.gate"], "only camera entities");
  setv(win, fld(panel, "source"), "stream");
  assert.deepStrictEqual(fieldNames(panel), ["kind", "name", "icon", "source", "stream", "size"]);
  kindTo(win, panel, "button");
  assert.deepStrictEqual(fieldNames(panel), ["kind", "name", "icon", "action", "entity_id", "color"], "button with a lock action asks for a lock entity");
  assert.deepStrictEqual(Array.from(fld(panel, "entity_id").options).map((o) => o.value), ["", "lock.front", "lock.gate"]);
  assert.deepStrictEqual(Array.from(fld(panel, "color").options).map((o) => o.value), ["", "blue", "green", "red"], "palette from the reply");
  setv(win, fld(panel, "entity_id"), "lock.gate");
  setv(win, fld(panel, "action"), "portico_open");
  assert.deepStrictEqual(fieldNames(panel), ["kind", "name", "icon", "action", "target", "color"], "an action with no entity shows a target instead");
  setv(win, fld(panel, "action"), "cover_open");
  assert.deepStrictEqual(Array.from(fld(panel, "entity_id").options).map((o) => o.value), ["", "cover.garage"], "the domain follows the action");
  kindTo(win, panel, "status");
  assert.deepStrictEqual(fieldNames(panel), ["kind", "name", "icon", "entity_id", "states", "attribute"]);
  kindTo(win, panel, "chip");
  assert.deepStrictEqual(fieldNames(panel), ["kind", "name", "icon", "entity_id", "states", "attribute", "short"]);
  assert.ok(fld(panel, "short").maxLength === 12, "limits come from the reply");
  assert.strictEqual(fld(panel, "name").maxLength, 40);
  console.log("PASS: the Add form per kind (camera, button, status, chip) is built from the reply's kinds, actions, palette and limits");
}

// ---- 9. live preview before save; validate_only; save ------------------------------------------------------------------------
{
  const srv = fakeServer(BASE);
  const m = await mountE5(srv);
  const { win, panel } = m;
  click(win, q(panel, '[data-el-act="add"][data-kind="camera"]'));
  kindTo(win, panel, "status");
  setv(win, fld(panel, "name"), "Porch");
  setv(win, fld(panel, "entity_id"), "light.porch");
  assert.strictEqual(slot(panel).querySelector(".pe-name").textContent, "Porch", "the preview shows the draft before any save");
  assert.strictEqual(slot(panel).querySelector(".pe-value").textContent, "On");
  const previewNode = slot(panel).firstChild;
  setv(win, fld(panel, "attribute"), "");     // an unrelated no-op change keeps the same drawn node
  assert.strictEqual(slot(panel).firstChild, previewNode, "an unchanged draft is not redrawn");
  click(win, q(panel, '[data-el-act="state-add"]'));
  const row = q(panel, ".el-staterow");
  setv(win, row.querySelector('[data-field="state"]'), "on");
  setv(win, row.querySelector('[data-field="label"]'), "Lit");
  setv(win, row.querySelector('[data-field="color"]'), "red");
  assert.strictEqual(slot(panel).querySelector(".pe-value").textContent, "Lit", "state words show in the preview");
  assert.strictEqual(slot(panel).querySelector(".pe-status").dataset.color, "red");
  // the live feed drives the form's preview too
  m.setStates({ "light.porch": { state: "off", attributes: {} } });
  assert.strictEqual(slot(panel).querySelector(".pe-value").textContent, "Off");
  m.emit("light.porch", { state: "on", attributes: {} });
  assert.strictEqual(slot(panel).querySelector(".pe-value").textContent, "Lit");
  assert.strictEqual(fld(panel, "name").value, "Porch", "a states push does not wipe the form");
  // validate_only goes out after the pause, writes nothing, and carries the element
  await sleep(40);
  assert.ok(srv.validates.length >= 1);
  assert.deepStrictEqual(srv.validates.at(-1).element, { kind: "status", name: "Porch", entity_id: "light.porch", options: { states: { on: { label: "Lit", color: "red" } } } });
  assert.strictEqual(srv.validates.at(-1).validate_only, true);
  assert.strictEqual(srv.validates.at(-1).base_rev, "r1");
  assert.strictEqual(srv.sets.length, 0, "nothing is written before Save");
  assert.match(q(panel, '[data-elements="valid"]').textContent, /accepts/);
  // Save
  click(win, act2(panel, "save"));
  await sleep(40);
  assert.strictEqual(srv.sets.length, 1);
  assert.strictEqual(srv.sets[0].validate_only, undefined, "the real save is not validate-only");
  assert.strictEqual(srv.sets[0].element.id, undefined, "a new element has no id; the server mints it");
  assert.strictEqual(srv.sets[0].base_rev, "r1");
  assert.strictEqual(q(panel, '[data-elements="form"]'), null, "back to the list");
  assert.match(q(panel, '[data-elements="notice"]').textContent, /Added "Porch"/);
  assert.ok(qa(panel, ".el-name").some((n) => n.textContent === "Porch"), "the new element is listed");
  console.log("PASS: the preview follows the draft and the live states before save; validate_only writes nothing; Save sends one set");
}
function act2(p, name) { return q(p, `[data-el-act="${name}"]`); }

// ---- 10. server errors beside the field; name_taken; messages ----------------------------------------------------------------
{
  const srv = fakeServer(BASE);
  const m = await mountE5(srv);
  const { win, panel } = m;
  click(win, q(panel, '[data-el-act="add"][data-kind="camera"]'));
  kindTo(win, panel, "status");
  setv(win, fld(panel, "name"), "Hall");
  await sleep(40);
  assert.strictEqual(err(panel, "entity_id"), "", "a field the person has not reached yet is not scolded");
  click(win, act2(panel, "save"));
  await sleep(40);
  assert.match(err(panel, "entity_id"), /not valid: needs an entity/, "the server's invalid shows beside its field");
  assert.strictEqual(srv.sets.length, 0);
  assert.strictEqual(err(panel, "name"), "");
  // a name another element holds: caught on the page at once, in any case and with spaces, and Save does not send
  setv(win, fld(panel, "name"), "  gate CAM ");
  assert.match(err(panel, "name"), /already used/);
  assert.ok(act2(panel, "save").disabled, "Save is off while the name clashes");
  setv(win, fld(panel, "entity_id"), "light.porch");
  await sleep(40);
  assert.match(err(panel, "name"), /already used/, "still the local message after the server's name_taken");
  assert.strictEqual(srv.sets.length, 0);
  // a name_taken only the server knows (another session added it): shown beside the name
  srv.elements.push({ id: "el-0000000a", kind: "status", name: "Other", entity_id: "light.porch", options: {} });
  setv(win, fld(panel, "name"), "Other");
  assert.match(err(panel, "name"), /already used/);
  // empty name on Save
  setv(win, fld(panel, "name"), "");
  click(win, act2(panel, "save"));
  assert.match(err(panel, "name"), /Give the element a name/);
  assert.strictEqual(srv.sets.length, 0);
  console.log("PASS: invalid and name_taken show beside the field; a clashing name blocks Save");
}
{
  const { errorMessage, ERROR_MESSAGES } = EL;
  for (const code of ["name_taken", "conflict", "unknown_element", "kind_locked", "too_many", "newer_schema", "in_use", "in_use_action", "in_use_stream", "migration_missing", "unknown", "unauthorized", "unknown_command"]) {
    const t = errorMessage({ code, message: "raw server text" });
    assert.ok(t.length > 10 && !t.includes("raw server text"), `${code} has its own sentence`);
    assert.strictEqual(t, ERROR_MESSAGES[code]);
  }
  assert.match(errorMessage({ code: "invalid", message: "name must be 1 to 40 characters" }), /name must be 1 to 40/);
  assert.strictEqual(errorMessage({ code: "weird", message: "boom" }, (e) => ({ message: `c:${e.message}` })), "c:boom");
  console.log("PASS: every server error code maps to its own sentence");
}
{
  // conflict on Save: message, a reload button, then a retry with the new rev succeeds and the draft is kept
  const srv = fakeServer(BASE);
  const m = await mountE5(srv);
  const { win, panel } = m;
  click(win, q(panel, '[data-el-act="add"][data-kind="camera"]'));
  kindTo(win, panel, "status");
  setv(win, fld(panel, "name"), "Hall");
  setv(win, fld(panel, "entity_id"), "light.porch");
  await sleep(30);
  srv.rev++;                                   // someone else saved
  click(win, act2(panel, "save"));
  await sleep(30);
  assert.match(q(panel, '[data-elements="form-error"]').textContent, /changed since this page read them/);
  click(win, act2(panel, "reload"));
  await sleep(40);
  assert.strictEqual(q(panel, '[data-elements="form-error"]').textContent, "");
  assert.strictEqual(fld(panel, "name").value, "Hall", "the draft survives the reload");
  click(win, act2(panel, "save"));
  await sleep(40);
  assert.strictEqual(srv.sets.at(-1).base_rev, "r2");
  assert.match(q(panel, '[data-elements="notice"]').textContent, /Added "Hall"/);
  console.log("PASS: a conflict offers a reload that keeps the draft, and the retry saves");
}

// ---- 11. edit and rename -------------------------------------------------------------------------------------------------
{
  const srv = fakeServer(BASE);
  const m = await mountE5(srv);
  const { win, panel } = m;
  click(win, rowOf(panel, "el-00000003").querySelector('[data-el-act="rename"]'));
  assert.notStrictEqual(fld(panel, "kind").tagName, "SELECT", "the kind of an existing element is fixed");
  assert.strictEqual(fld(panel, "name").value, "Open gate");
  assert.strictEqual(fld(panel, "action").value, "lock_unlock");
  assert.strictEqual(fld(panel, "entity_id").value, "lock.gate");
  assert.strictEqual(fld(panel, "color").value, "blue");
  assert.strictEqual(panel.shadowRoot.activeElement && panel.shadowRoot.activeElement.dataset.field, "name", "Rename puts the cursor in the name");
  setv(win, fld(panel, "name"), "Main gate");
  assert.strictEqual(err(panel, "name"), "");
  assert.strictEqual(slot(panel).querySelector(".pe-btn .pe-t").textContent, "Main gate", "the preview shows the new name");
  click(win, act2(panel, "save"));
  await sleep(40);
  assert.deepStrictEqual(srv.sets[0].element, { id: "el-00000003", kind: "button", name: "Main gate", entity_id: "lock.gate", options: { action: "lock_unlock", color: "blue" } }, "a rename is a replace with the same id");
  assert.match(q(panel, '[data-elements="notice"]').textContent, /Saved "Main gate"/);
  assert.strictEqual(rowOf(panel, "el-00000003").querySelector(".el-name").textContent, "Main gate");
  // edit: keeping its own name is not a clash; an icon pick is sent without the mdi: prefix
  click(win, rowOf(panel, "el-00000003").querySelector('[data-el-act="edit"]'));
  setv(win, fld(panel, "icon"), "door");
  assert.strictEqual(err(panel, "name"), "");
  click(win, act2(panel, "save"));
  await sleep(40);
  assert.strictEqual(srv.sets[1].element.icon, "door");
  // server kind_locked-style refusal shows as a sentence, not raw text
  srv.force.set = { code: "kind_locked", message: "raw" };
  click(win, rowOf(panel, "el-00000001").querySelector('[data-el-act="edit"]'));
  click(win, act2(panel, "save"));
  await sleep(30);
  assert.match(q(panel, '[data-elements="form-error"]').textContent, /kind cannot change/);
  click(win, act2(panel, "cancel"));
  assert.strictEqual(q(panel, '[data-elements="form"]'), null);
  console.log("PASS: edit loads the element, rename is a same-id replace, the kind is locked, errors read as sentences");
}

// ---- 12. remove, with and without in_use --------------------------------------------------------------------------------------
{
  const srv = fakeServer(BASE);
  const m = await mountE5(srv);
  const { win, panel } = m;
  click(win, rowOf(panel, "el-00000002").querySelector('[data-el-act="remove"]'));
  assert.match(q(panel, '[data-elements="remove"] h2').textContent, /Remove "Gate cam"\?/);
  assert.strictEqual(act2(panel, "remove-anyway"), null);
  click(win, act2(panel, "cancel"));
  assert.strictEqual(srv.deletes.length, 0, "Cancel removes nothing");
  click(win, rowOf(panel, "el-00000002").querySelector('[data-el-act="remove"]'));
  click(win, act2(panel, "remove-confirm"));
  await sleep(40);
  assert.deepStrictEqual(srv.deletes, [{ type: "portico/elements/delete", element_id: "el-00000002", base_rev: "r1" }], "no confirm on the first try");
  assert.strictEqual(rowOf(panel, "el-00000002"), null);
  assert.match(q(panel, '[data-elements="notice"]').textContent, /Removed "Gate cam"/);
}
{
  // in_use answered by the server: the screens are listed and there is nothing left to press but Close
  const srv = fakeServer(BASE, { force: { delete: { code: "in_use", message: "x", used_by: ["Kitchen <i>panel</i>", { name: "Entrance" }] } } });
  const m = await mountE5(srv);
  const { win, panel } = m;
  click(win, rowOf(panel, "el-00000001").querySelector('[data-el-act="remove"]'));
  click(win, act2(panel, "remove-confirm"));
  await sleep(40);
  assert.deepStrictEqual(qa(panel, ".el-used li").map((n) => n.textContent), ["Kitchen <i>panel</i>", "Entrance"], "screens listed as text");
  assert.strictEqual(qa(panel, ".el-used i").length, 0);
  assert.match(q(panel, '[data-elements="in-use"]').textContent, /In use by these screens; remove those first/);
  assert.strictEqual(act2(panel, "remove-confirm"), null, "the plain Remove is gone");
  assert.strictEqual(act2(panel, "remove-anyway"), null, "there is no Remove anyway");
  assert.strictEqual(qa(panel, "button").filter((b) => /anyway/i.test(b.textContent)).length, 0);
  assert.strictEqual(srv.deletes.length, 1);
  click(win, act2(panel, "cancel"));
  assert.strictEqual(q(panel, '[data-elements="remove"]'), null, "Close leaves the dialog");
  assert.ok(rowOf(panel, "el-00000001"), "the element stays");
  assert.strictEqual(srv.deletes.length, 1, "no second delete, and never a confirm");
  assert.ok(srv.deletes.every((d) => !("confirm" in d)));
}
{
  // used_by already in the list reply: the warning shows before any call
  const srv = fakeServer(BASE);
  srv.used_by = { "el-00000001": ["Kitchen"] };
  const m = await mountE5(srv);
  click(m.win, rowOf(m.panel, "el-00000001").querySelector('[data-el-act="remove"]'));
  assert.ok(!act2(m.panel, "remove-anyway") && !act2(m.panel, "remove-confirm") && act2(m.panel, "cancel"));
  assert.match(q(m.panel, '[data-elements="in-use"]').textContent, /remove those first/);
  assert.strictEqual(srv.deletes.length, 0);
  // a failing delete (conflict) is a sentence with a reload
  srv.used_by = {};
  const srv2 = fakeServer(BASE, { force: { delete: { code: "conflict", message: "raw" } } });
  const m2 = await mountE5(srv2);
  click(m2.win, rowOf(m2.panel, "el-00000001").querySelector('[data-el-act="remove"]'));
  click(m2.win, act2(m2.panel, "remove-confirm"));
  await sleep(40);
  assert.match(q(m2.panel, '[data-elements="remove-error"]').textContent, /changed since/);
  assert.ok(act2(m2.panel, "reload"));
  console.log("PASS: remove asks first; in_use lists the screens, offers no Remove anyway and never sends confirm; failures read as sentences");
}

// ---- 13. pickers: HA's where defined, the panel's list otherwise -------------------------------------------------------------------
{
  const srv = fakeServer(BASE);
  const m = await mountE5(srv);
  click(m.win, q(m.panel, '[data-el-act="add"][data-kind="camera"]'));
  assert.strictEqual(fld(m.panel, "entity_id").tagName, "SELECT", "no ha-entity-picker in this browser: the panel's own list");
  assert.strictEqual(q(m.panel, '.el-form [data-field-wrap="icon"] [data-picker]').dataset.picker, "list");
  // the filter narrows the list
  setv(m.win, q(m.panel, '[data-field="entity_filter"]'), "gate");
  assert.deepStrictEqual(Array.from(fld(m.panel, "entity_id").options).map((o) => o.value), ["", "camera.gate"]);
}
{
  const srv = fakeServer(BASE);
  const m = await mount({ handlers: srv.handlers });
  class Pick extends m.win.HTMLElement {}
  m.win.customElements.define("ha-entity-picker", Pick);
  m.win.customElements.define("ha-icon-picker", class extends m.win.HTMLElement {});
  m.ctl().validateMs = 5;
  await openElements(m);
  click(m.win, q(m.panel, '[data-el-act="add"][data-kind="camera"]'));
  kindTo(m.win, m.panel, "status");
  const ep = fld(m.panel, "entity_id");
  assert.strictEqual(ep.tagName.toLowerCase(), "ha-entity-picker", "defined here, so used");
  assert.ok(ep.hass && ep.hass.states, "it is handed hass");
  setv(m.win, fld(m.panel, "name"), "Porch");
  ep.dispatchEvent(new m.win.CustomEvent("value-changed", { detail: { value: "light.porch" } }));
  assert.strictEqual(slot(m.panel).querySelector(".pe-value").textContent, "On", "its choice drives the preview");
  kindTo(m.win, m.panel, "button");
  assert.deepStrictEqual(fld(m.panel, "entity_id").includeDomains, ["lock"], "domain filter handed to HA's picker");
  // button keeps the restricted list even where HA's picker exists (it offers every mdi icon, the server takes a fixed set)
  assert.strictEqual(fld(m.panel, "icon").tagName, "SELECT");
  assert.deepStrictEqual(Array.from(fld(m.panel, "icon").options).map((o) => o.value), ["", "door", "lock", "lightbulb"]);
  kindTo(m.win, m.panel, "chip");
  const ip = fld(m.panel, "icon");
  assert.strictEqual(ip.tagName.toLowerCase(), "ha-icon-picker", "status and chip use HA's full picker");
  ip.dispatchEvent(new m.win.CustomEvent("value-changed", { detail: { value: "mdi:lock-open-variant" } }));
  assert.strictEqual(m.ctl().view.draft.icon, "lock-open-variant", "any mdi icon, prefix stripped");
  assert.strictEqual(slot(m.panel).querySelector("ha-icon").getAttribute("icon"), "mdi:information-outline", "no entity yet: the default glyph");
  console.log("PASS: ha-entity-picker and ha-icon-picker are used only where the browser defines them; the list is the fallback");
}

// ---- 13b. icon field per kind when the browser has no ha-icon-picker ------------------------------------------------------------
{
  const srv = fakeServer(BASE);
  const m = await mountE5(srv);
  click(m.win, q(m.panel, '[data-el-act="add"][data-kind="camera"]'));
  const wrapOf = () => q(m.panel, '.el-form [data-field-wrap="icon"] [data-picker]');
  assert.strictEqual(wrapOf().dataset.picker, "list", "camera: the fixed list");
  kindTo(m.win, m.panel, "status");
  assert.strictEqual(wrapOf().dataset.picker, "text", "status: free typing");
  const input = fld(m.panel, "icon");
  assert.strictEqual(input.tagName, "INPUT");
  assert.strictEqual(input.maxLength, 64);
  assert.ok(input.pattern.includes("[a-z0-9]"));
  assert.ok(q(m.panel, '.el-form [data-field-wrap="icon"] .el-hint'), "a hint");
  const prev = () => q(m.panel, '.el-form [data-field-wrap="icon"] ha-icon');
  assert.ok(prev().hidden, "nothing typed, no preview");
  setv(m.win, input, "mdi:gate-arrow-right");
  assert.strictEqual(m.ctl().view.draft.icon, "gate-arrow-right");
  assert.strictEqual(prev().getAttribute("icon"), "mdi:gate-arrow-right", "live preview");
  setv(m.win, input, "Bad Name");
  assert.ok(prev().hidden, "a malformed name draws nothing");
  setv(m.win, input, "thermometer");
  kindTo(m.win, m.panel, "chip");
  assert.strictEqual(wrapOf().dataset.picker, "text", "chip: free typing too");
  kindTo(m.win, m.panel, "button");
  assert.strictEqual(wrapOf().dataset.picker, "list", "button: back to the list");
  assert.strictEqual(m.ctl().view.draft.icon, "", "a new form starts without an icon");
  console.log("PASS: the icon field follows the kind (list for button and camera, free mdi name with hint and preview for status and chip)");
}

// ---- 14. buttons stay inert -------------------------------------------------------------------------------------------------
{
  const srv = fakeServer(BASE);
  const m = await mountE5(srv);
  click(m.win, q(m.panel, '[data-el-act="add"][data-kind="camera"]'));
  kindTo(m.win, m.panel, "button");
  setv(m.win, fld(m.panel, "entity_id"), "lock.gate");
  setv(m.win, fld(m.panel, "name"), "Test");
  const b = slot(m.panel).querySelector("button.pe-btn");
  assert.ok(b.disabled);
  click(m.win, b);
  await sleep(20);
  const other = m.fake.sent.filter((x) => !x.type.startsWith("portico/elements/") && !["portico/phones/list", "portico/door_status"].includes(x.type));
  assert.ok(!other.some((x) => /call_service|content\/run|lock|gate/.test(x.type)), "no action ran: " + JSON.stringify(other.map((x) => x.type)));
  assert.strictEqual(srv.sets.length, 0);
  console.log("PASS: a button preview is inert and runs nothing");
}

// ---- 15. camera picture is not rebuilt unless its source changes -------------------------------------------------------------------
{
  const m = await mount();
  await openElements(m);
  const img = () => rowOf(m.panel, "el-00000002").querySelector("img");
  const first = img();
  assert.ok(first);
  m.setStates({ "light.porch": { state: "off", attributes: {} } });
  assert.strictEqual(img(), first, "an unrelated state change keeps the same <img>");
  m.setStates({ "camera.gate": { state: "recording", attributes: { entity_picture: "/api/camera_proxy/camera.gate?token=t" } } });
  assert.strictEqual(img(), first, "the camera's state changed but not its picture address");
  m.emit("camera.gate", { state: "idle", attributes: { entity_picture: "/api/camera_proxy/camera.gate?token=t" } });
  assert.strictEqual(img(), first);
  m.emit("camera.gate", { state: "idle", attributes: { entity_picture: "/api/camera_proxy/camera.gate?token=NEW" } });
  assert.notStrictEqual(img(), first, "a new picture address is a new <img>");
  assert.match(img().getAttribute("src"), /token=NEW/);
  // and in the form's preview, typing a name does not rebuild the camera picture
  const srv = fakeServer(BASE);
  const m2 = await mountE5(srv);
  click(m2.win, q(m2.panel, '[data-el-act="add"][data-kind="camera"]'));
  setv(m2.win, fld(m2.panel, "entity_id"), "camera.gate");
  const pimg = slot(m2.panel).querySelector("img");
  assert.ok(pimg);
  m2.setStates({ "light.porch": { state: "off", attributes: {} } });
  m2.emit("camera.gate", { state: "idle", attributes: { entity_picture: "/api/camera_proxy/camera.gate?token=t" } });
  assert.strictEqual(slot(m2.panel).querySelector("img"), pimg, "form preview: states pushes keep the same <img>");
  console.log("PASS: the camera <img> is rebuilt only when its source changes");
}

// ---- 16. the camera picker shows each camera's picture size (D-20261109-b0a3) ------------------------------------------------
{
  const { cameraFamily, resolutionText, sortCameraOptions } = EL;
  assert.strictEqual(cameraFamily("camera.gate_intercom_sub"), "gate_intercom");
  assert.strictEqual(cameraFamily("camera.gate_intercom"), "gate_intercom");
  assert.strictEqual(cameraFamily("camera.example_intercom_ex_gate_main"), "example_intercom_ex_gate");
  assert.strictEqual(cameraFamily("camera.example_doorbell_ex_entrance_high_resolution_channel"), "example_doorbell_ex_entrance");
  assert.strictEqual(cameraFamily("camera.sub"), "sub", "a name that is only a stream word is kept");
  assert.strictEqual(resolutionText(undefined), "\u2026");
  assert.strictEqual(resolutionText(null), "unknown");
  assert.strictEqual(resolutionText({ width: 2560, height: 1440 }), "2560x1440");
  assert.strictEqual(resolutionText({ width: null, height: null, source: "failed" }), "unknown");
  assert.strictEqual(resolutionText({ width: "<b>", height: 3 }), "unknown");
  const px = { "camera.a_sub": 640 * 360, "camera.a": 2560 * 1440, "camera.b": 0 };
  const sorted = sortCameraOptions(["camera.b", "camera.a_sub", "camera.a"].map((v) => ({ value: v, name: v })), (id) => px[id] || 0);
  assert.deepStrictEqual(sorted.map((o) => o.value), ["camera.a", "camera.a_sub", "camera.b"], "families together, largest first inside one");

  const states = {
    "camera.gate": { state: "idle", attributes: { friendly_name: "Gate" } },
    "camera.gate_sub": { state: "idle", attributes: { friendly_name: "Gate sub" } },
    "camera.dead": { state: "unavailable", attributes: { friendly_name: "Dead" } },
  };
  const srv = fakeServer(BASE);
  const asked = [];
  let release;
  const gate = new Promise((r) => { release = r; });
  srv.handlers["portico/cameras/info"] = async (msg) => {
    asked.push(msg.entity_ids);     // the list asked for camera.gate (its camera element); the picker asks only for the rest
    await gate;
    return { cameras: [
      { entity_id: "camera.gate_sub", width: 640, height: 360, fps: null, source: "still" },
      { entity_id: "camera.gate", width: 2560, height: 1440, fps: null, source: "still" },
      { entity_id: "camera.dead", width: null, height: null, fps: null, source: "unavailable" },
    ] };
  };
  const m = await mountE5(srv, { states });
  click(m.win, q(m.panel, '[data-el-act="add"][data-kind="camera"]'));
  kindTo(m.win, m.panel, "camera");
  const opts = () => Array.from(fld(m.panel, "entity_id").options).filter((o) => o.value).map((o) => [o.value, o.textContent]);
  assert.strictEqual(fld(m.panel, "entity_id").tagName, "SELECT");
  assert.ok(opts().every(([, t]) => t.endsWith("\u2014 \u2026")), "loading: an ellipsis beside each name");
  // the panel's own spec chip also asks for camera.gate (its own cache); the Elements module asks for it once more, then the picker for the rest
  const multi = asked.filter((x) => x.length > 1);
  assert.strictEqual(multi.length, 1, "the picker asks once");
  assert.ok(multi[0].length <= 20 && multi[0].includes("camera.gate_sub") && !multi[0].includes("camera.gate"), "one shared cache in the module: the list's pending read is not asked again");
  assert.ok(asked.some((x) => x.length === 1 && x[0] === "camera.gate"));
  release();
  await sleep(40);
  assert.deepStrictEqual(opts().map(([v]) => v), ["camera.dead", "camera.gate", "camera.gate_sub"] , "family order, then largest first");
  const text = Object.fromEntries(opts());
  assert.match(text["camera.gate"], /\u2014 2560x1440$/);
  assert.match(text["camera.gate_sub"], /\u2014 640x360$/);
  assert.match(text["camera.dead"], /\u2014 unknown$/, "an unavailable camera is unknown, not an error");
  // choosing one still drives the form
  setv(m.win, fld(m.panel, "entity_id"), "camera.gate_sub");
  assert.strictEqual(fld(m.panel, "entity_id").value, "camera.gate_sub");
  // closing and reopening within the cache time asks nothing again
  click(m.win, act2(m.panel, "cancel"));
  click(m.win, q(m.panel, '[data-el-act="add"][data-kind="camera"]'));
  kindTo(m.win, m.panel, "camera");
  await sleep(20);
  assert.strictEqual(asked.length, 3, "kept for the cache time");
  assert.match(Object.fromEntries(opts())["camera.gate"], /2560x1440$/);
  console.log("PASS: the camera picker lists name and picture size, the largest first inside a camera family");
}
{
  // the server fails: every size reads "unknown", the picker still works; HA's own picker is not used for cameras
  const states = { "camera.gate": { state: "idle", attributes: { friendly_name: "Gate" } } };
  const srv = fakeServer(BASE);
  srv.handlers["portico/cameras/info"] = () => { throw { code: "unknown_command", message: "nope" }; };
  const m = await mountE5(srv, { states });
  class Pick extends m.win.HTMLElement {}
  m.win.customElements.define("ha-entity-picker", Pick);
  click(m.win, q(m.panel, '[data-el-act="add"][data-kind="camera"]'));
  kindTo(m.win, m.panel, "camera");
  await sleep(40);
  const sel = fld(m.panel, "entity_id");
  assert.strictEqual(sel.tagName, "SELECT", "cameras use the panel's list even where ha-entity-picker exists");
  assert.match(Array.from(sel.options).find((o) => o.value === "camera.gate").textContent, /\u2014 unknown$/);
  setv(m.win, sel, "camera.gate");
  assert.strictEqual(fld(m.panel, "entity_id").value, "camera.gate");
  console.log("PASS: a failed cameras/info reads 'unknown' and the camera picker still works");
}
// ---- retarget of a used element asks first ----------------------------------------------------------------------------------
{
  const srv = fakeServer(BASE);
  srv.retarget = true;
  const m = await mountE5(srv);
  const { win, panel } = m;
  click(win, rowOf(panel, "el-00000003").querySelector('[data-el-act="edit"]'));
  setv(win, fld(panel, "entity_id"), "lock.front");
  await sleep(40);                                           // the live check reports the question, not an error
  const ask = q(panel, '[data-elements="retarget"]');
  assert.ok(ask, "the form asks");
  assert.match(ask.textContent, /Used on: Home > Gate, Kitchen > Doors\. Apply to all\?/);
  assert.strictEqual(srv.validates.length, 0);
  assert.match(q(panel, '[data-elements="valid"]').textContent, /accepts/, "it is a question, not a mistake");
  click(win, act2(panel, "save"));                           // Save alone does not confirm
  await sleep(40);
  assert.strictEqual(srv.sets.length, 0, "nothing written without the confirm step");
  assert.ok(q(panel, '[data-elements="retarget"]'));
  click(win, act2(panel, "apply-all"));
  await sleep(40);
  assert.strictEqual(srv.sets.length, 1);
  assert.strictEqual(srv.sets[0].confirm, true, "confirm:true only after Apply to all");
  assert.strictEqual(srv.sets[0].element.entity_id, "lock.front");
  console.log("PASS: changing the entity of a used element shows 'Used on: ... Apply to all?' and sends confirm:true only after it");
}
// ---- camera elements show their picture size on the list, in the Edit preview and in the camera dialog ----------------------
{
  const { cameraCaption } = EL;
  assert.strictEqual(cameraCaption({ kind: "camera", entity_id: "camera.g" }, undefined), "camera.g - ...");
  assert.strictEqual(cameraCaption({ kind: "camera", entity_id: "camera.g" }, { width: 2560, height: 1440 }), "camera.g - 2560x1440 (snapshot)");
  assert.strictEqual(cameraCaption({ kind: "camera", entity_id: "camera.g" }, null), "camera.g - size unknown");
  // DEV-766: a Frigate camera's still is the 640x480 detect frame, its live stream the main restream; the caption says which
  const frig = { width: 640, height: 480, source: "still", stream: { width: 2560, height: 1440 } };
  assert.strictEqual(cameraCaption({ kind: "camera", entity_id: "camera.g" }, frig), "camera.g - 2560x1440 (live stream)");
  assert.strictEqual(EL.resolutionText(frig), "2560x1440", "the picker shows the live size, not the still's");
  assert.strictEqual(EL.sizeTitle(frig), EL.SIZE_TITLE_LIVE);
  assert.strictEqual(EL.sizeTitle({ width: 640, height: 480 }), EL.SIZE_TITLE_STILL);
  assert.strictEqual(EL.sizeTitle(undefined), EL.SIZE_TITLE_STILL);
  for (const bad of [null, "2560x1440", [], { width: 0, height: 5 }, { width: 2560.5, height: 1440 }, { width: "2560", height: 1440 }, { height: 9 }, {}]) {
    const row = { width: 640, height: 480, source: "still", stream: bad };
    assert.strictEqual(EL.liveSize(row), null, JSON.stringify(bad));
    assert.strictEqual(cameraCaption({ kind: "camera", entity_id: "camera.g" }, row), "camera.g - 640x480 (snapshot)", "a bad stream value falls back to the still");
  }
  assert.deepStrictEqual(EL.liveSize(frig), { width: 2560, height: 1440 });
  assert.strictEqual(EL.resolutionText({ width: null, height: null, source: "failed", stream: { width: 1280, height: 720 } }), "1280x720", "a stream size stands without a still");
  assert.strictEqual(cameraCaption({ kind: "camera", entity_id: "", options: { stream: "rtsp://h/p" } }, undefined), "stream");
  assert.strictEqual(cameraCaption({ kind: "button", entity_id: "lock.g" }, undefined), "");
  const els = [
    ...BASE,
    { id: "el-00000010", kind: "camera", name: "Garage cam", entity_id: "camera.garage", options: {} },
    { id: "el-00000011", kind: "camera", name: "Stream cam", entity_id: "", options: { stream: "rtsp://h/p" } },
  ];
  const states = {
    "camera.gate": { state: "idle", attributes: { friendly_name: "Gate" } },
    "camera.garage": { state: "idle", attributes: { friendly_name: "Garage" } },
  };
  const srv = fakeServer(els);
  const asked = [];
  let release;
  const gate = new Promise((r) => { release = r; });
  srv.handlers["portico/cameras/info"] = async (msg) => {
    asked.push(msg.entity_ids);
    await gate;
    return { cameras: [{ entity_id: "camera.gate", width: 2560, height: 1440, fps: null, source: "still" }, { entity_id: "camera.garage", width: null, height: null, fps: null, source: "failed" }] };
  };
  const m = await mountE5(srv, { states });
  const cap = (id) => { const n = rowOf(m.panel, id).querySelector(".pe-camera .pe-sub"); return n ? n.textContent : null; };   // the line INSIDE the card
  assert.strictEqual(cap("el-00000002"), "camera.gate - ...", "loading");
  assert.strictEqual(cap("el-00000011"), "h/p", "stream only: the stream address, no probe");
  assert.ok(!rowOf(m.panel, "el-00000002").querySelector(".el-cap"), "no separate caption line after the cards");
  assert.strictEqual(cap("el-00000003"), null, "a button has no caption");
  const batched = asked.filter((x) => x.length > 1);      // the panel's own spec chip may also ask for one camera
  assert.strictEqual(batched.length, 1, "one batched call for all camera elements");
  assert.deepStrictEqual([...batched[0]].sort(), ["camera.garage", "camera.gate"], "the stream-only camera is not in it");
  const callsBefore = asked.length;
  release();
  await sleep(40);
  assert.strictEqual(cap("el-00000002"), "camera.gate - 2560x1440 (snapshot)", "known");
  assert.strictEqual(cap("el-00000010"), "camera.garage - size unknown", "unknown");
  assert.strictEqual(cap("el-00000011"), "h/p");
  // the Edit form's preview header carries the same caption
  click(m.win, rowOf(m.panel, "el-00000002").querySelector('[data-el-act="edit"]'));
  await sleep(30);
  const fcap = slot(m.panel).querySelector(".pe-camera .pe-sub");
  assert.ok(fcap, "caption under the form preview");
  assert.strictEqual(fcap.textContent, "camera.gate - 2560x1440 (snapshot)");
  assert.strictEqual(asked.filter((x) => x.length > 1).length, 1, "no second batched call: the shared cache answers");
  assert.ok(asked.length <= callsBefore + 0, "no call at all for opening the form");
  console.log("PASS: camera elements show their picture size (loading / known / unknown / stream) on the list and in the Edit preview, one batched call");
}

// ---- DEV-768: a camera has a Big and a Small picker; a lone stream is just the stream -----------------------------------------
{
  const spec = FULL_KINDS.camera;
  const d = EL.emptyDraft("camera");
  d.name = "Gate";
  d.entity_id = "camera.gate";
  d.small_entity_id = "camera.gate_sub";
  assert.deepStrictEqual(EL.draftToElement(spec, d), { kind: "camera", name: "Gate", entity_id: "camera.gate", options: { small_entity_id: "camera.gate_sub" } }, "both filled");
  d.small_entity_id = "";
  assert.deepStrictEqual(EL.draftToElement(spec, d), { kind: "camera", name: "Gate", entity_id: "camera.gate", options: {} }, "big only: no small key");
  d.entity_id = "";
  d.small_entity_id = "camera.gate_sub";
  assert.deepStrictEqual(EL.draftToElement(spec, d), { kind: "camera", name: "Gate", entity_id: "camera.gate_sub", options: {} }, "small only is stored as THE stream (entity_id)");
  d.entity_id = "camera.gate_sub";
  assert.deepStrictEqual(EL.draftToElement(spec, d).options, {}, "the same camera twice is one stream");
  d.entity_id = "camera.gate";
  d.small_entity_id = "camera.gate_sub";
  d.source = "stream";
  d.stream = "rtsp://10.6.0.6:8554/gate";
  assert.deepStrictEqual(EL.draftToElement(spec, d).options, { stream: "rtsp://10.6.0.6:8554/gate" }, "a stream address has no small side");
  const back = EL.elementToDraft({ id: "el-0000aaaa", kind: "camera", name: "G", entity_id: "camera.gate", options: { small_entity_id: "camera.gate_sub" } });
  assert.strictEqual(back.small_entity_id, "camera.gate_sub");
  assert.ok(!("small_entity_id" in back.extra), "not parked in the free-form extras");
  console.log("PASS: draft <-> element for the Big/Small camera pickers (both, big only, small only, stream address)");
}
{
  const srv = fakeServer([...BASE, { id: "el-00000010", kind: "camera", name: "Yard cam", entity_id: "camera.gate", options: { small_entity_id: "camera.garage" } }]);
  const m = await mountE5(srv, { states: { "camera.gate": { state: "idle", attributes: {} }, "camera.garage": { state: "idle", attributes: {} } } });
  const { win, panel } = m;
  click(win, q(panel, '[data-el-act="add"][data-kind="camera"]'));
  const wrap = (n) => q(panel, `.el-form [data-field-wrap="${n}"]`);
  assert.ok(wrap("small_entity_id").textContent.includes("Small stream (optional)"));
  assert.ok(wrap("entity_id").textContent.includes("Big stream"));
  assert.ok(wrap("small_entity_id").textContent.includes("Fill only one to always show that one."));
  setv(win, fld(panel, "name"), "Only small");
  setv(win, q(panel, '.el-form [data-field="small_entity_id"]'), "camera.garage");
  click(win, q(panel, '.el-form [data-el-act="save"]'));
  await sleep(60);
  const sent = srv.sets[srv.sets.length - 1];
  assert.ok(sent, "saved");
  assert.strictEqual(sent.element.entity_id, "camera.garage", "the lone small picker is stored as the stream");
  assert.deepStrictEqual(sent.element.options, {});
  // a stream-address camera hides the small picker
  click(win, q(panel, '[data-el-act="add"][data-kind="camera"]'));
  setv(win, fld(panel, "source"), "stream");
  assert.ok(!wrap("small_entity_id"), "no small picker for a stream address");
  console.log("PASS: the camera form shows Big and Small pickers with the hint, and a lone Small is saved as the stream");
}
{
  // DEV-768 follow-up: a camera with both streams names both on the list row, and the small stream's live size is
  // shown even when its still failed (the server row has null width/height but a `stream` size)
  const els = [...BASE, { id: "el-00000020", kind: "camera", name: "Front yard", entity_id: "camera.facade_lower", options: { small_entity_id: "camera.facade_lower_sub" } }];
  const states = { "camera.facade_lower": { state: "recording", attributes: { entity_picture: "/api/camera_proxy/camera.facade_lower?token=a" } }, "camera.facade_lower_sub": { state: "idle", attributes: { entity_picture: "/api/camera_proxy/camera.facade_lower_sub?token=b" } } };
  const srv = fakeServer(els);
  srv.handlers["portico/cameras/info"] = async () => ({ cameras: [
    { entity_id: "camera.facade_lower", width: 640, height: 480, fps: 5, source: "still", stream: { width: 2560, height: 1440 } },
    { entity_id: "camera.facade_lower_sub", width: null, height: null, fps: null, source: "failed", stream: { width: 640, height: 480 } }] });
  const m = await mountE5(srv, { states });
  await sleep(60);
  const row = rowOf(m.panel, "el-00000020");
  assert.strictEqual(row.querySelector('[data-elements="cam-big"]').textContent, "Big: camera.facade_lower");
  assert.strictEqual(row.querySelector('[data-elements="cam-small"]').textContent, "Small: camera.facade_lower_sub");
  const cells = Array.from(row.querySelectorAll(".pe-pair-cell"));
  assert.strictEqual(cells[0].querySelector(".pe-camera .pe-sub").textContent, "camera.facade_lower - 2560x1440 (live stream)", "Big's card carries entity, size and source");
  assert.strictEqual(cells[1].querySelector(".pe-camera .pe-sub").textContent, "camera.facade_lower_sub - 640x480 (live stream)", "a failed still with a live size is not 'unknown'");
  assert.strictEqual(row.querySelectorAll(".el-cap").length, 0, "no caption lines after the row of cards");
  assert.deepStrictEqual(cells.map((c) => c.dataset.stream), ["big", "small"], "one labelled picture per stream");
  assert.deepStrictEqual(cells.map((c) => c.firstChild.textContent), ["Big", "Small"]);
  assert.ok(cells[0].querySelector("img").getAttribute("src").startsWith("/api/camera_proxy/camera.facade_lower?"), "Big draws the entity_id picture");
  assert.ok(cells[1].querySelector("img").getAttribute("src").startsWith("/api/camera_proxy/camera.facade_lower_sub?"), "Small draws the small stream's picture");
  assert.strictEqual(row.querySelectorAll("img").length, 2);
  assert.ok(row.classList.contains("el-row-pair"), "the row gives the two pictures room side by side");
  assert.ok(row.querySelector(".pe-preview, .el-preview .pe-pair"), "both pictures sit in the one preview cell of the row");
  row.querySelectorAll("img")[1].click();
  await sleep(40);   // no live session in this fake hass: the still fallback shows
  const viewer = m.win.document.querySelector('[role="dialog"]');
  assert.ok(viewer && viewer.querySelector("img").getAttribute("src").startsWith("/api/camera_proxy/camera.facade_lower_sub?"), "clicking Small in the list opens it full size");
  viewer.querySelector(".pe-viewer-close").click();
  assert.strictEqual(row.querySelectorAll("img").length, 2);
  click(m.win, row.querySelector('[data-el-act="edit"]'));
  await sleep(60);
  const opt = Array.from(fld(m.panel, "small_entity_id").options).find((o) => o.value === "camera.facade_lower_sub");
  assert.ok(opt && opt.textContent.includes("640x480"), "the Small picker shows the live size, not unknown: " + (opt && opt.textContent));
  const EL2 = await import(new URL("../portico-elements.js", import.meta.url));
  assert.strictEqual(EL2.cameraSmallCaption({ kind: "camera", entity_id: "camera.a" }, undefined), "", "no small stream: no second caption");
  assert.deepStrictEqual(EL2.cameraStreams({ kind: "camera", entity_id: "camera.a", options: { small_entity_id: "camera.b" } }), { big: "camera.a", small: "camera.b" });
  console.log("PASS: a camera with two streams shows both on the list row, captions the small one, and reads its live size");
}
process.exit(0);
