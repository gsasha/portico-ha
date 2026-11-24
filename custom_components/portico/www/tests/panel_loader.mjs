// Loads the REAL portico-panel.js into a jsdom window and builds a fake `hass` for it.
import { JSDOM } from "jsdom";
import fs from "fs";

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export function loadPanel(opts = {}) {
  const dom = new JSDOM("<!doctype html><html><body></body></html>", { url: opts.url || "http://localhost/", pretendToBeVisual: true });
  const src = fs.readFileSync(new URL("../portico-panel.js", import.meta.url), "utf8");
  const run = new dom.window.Function("window", "document", "customElements", "HTMLElement", src + "\nreturn customElements.get('portico-panel');");
  const Panel = run(dom.window, dom.window.document, dom.window.customElements, dom.window.HTMLElement);
  // The Phones tab is a separate ES module the panel imports lazily (UI split P2). jsdom's Function cannot resolve a URL,
  // so the loader hands the panel the real file through a data: URL; `opts.phones === false` makes the import fail.
  Panel.prototype._importPhones = opts.phones === false
    ? () => Promise.reject(new Error("the Phones module is not served"))
    : () => importPhones();
  // The Settings tab (UI split P4) is the same: its own module, handed over as a data: URL; `opts.settings === false` fails it.
  Panel.prototype._importSettings = opts.settings === false
    ? () => Promise.reject(new Error("the Settings module is not served"))
    : () => importSettings();
  // The Elements tab (DEV-760 E4) is the same, plus the shared renderer module it draws with; `opts.elements === false`
  // fails the tab module, `opts.elementsRender === false` fails the renderer (either leaves no Elements tab).
  Panel.prototype._importElements = opts.elements === false
    ? () => Promise.reject(new Error("the Elements module is not served"))
    : () => importElements();
  Panel.prototype._importElementsRender = opts.elementsRender === false
    ? () => Promise.reject(new Error("the Elements renderer is not served"))
    : () => importElementsRender();
  // The panel opens on Elements when its module loads (the first tab). The other tests were written for the Pages start, so
  // by default the loader marks a tab as already chosen; `opts.openOnElements` leaves the real behaviour for the tests of it.
  if (!opts.openOnElements) {
    const ensure = Panel.prototype._ensureElements;
    Panel.prototype._ensureElements = function () { this._tabChosen = true; return ensure.call(this); };
  }
  return { dom, Panel, doc: dom.window.document };
}

/* The real portico-elements.js and portico-elements-render.js as modules. */
export function importElements() {
  const src = fs.readFileSync(new URL("../portico-elements.js", import.meta.url), "utf8");
  return import("data:text/javascript;base64," + Buffer.from(src, "utf8").toString("base64"));
}
export function importElementsRender() {
  const src = fs.readFileSync(new URL("../portico-elements-render.js", import.meta.url), "utf8");
  return import("data:text/javascript;base64," + Buffer.from(src, "utf8").toString("base64"));
}

/* The real portico-settings.js as a module (imported once per process). */
export function importSettings() {
  const src = fs.readFileSync(new URL("../portico-settings.js", import.meta.url), "utf8");
  return import("data:text/javascript;base64," + Buffer.from(src, "utf8").toString("base64"));
}

/* The real portico-phones.js as a module (imported once per process). */
export function importPhones() {
  const src = fs.readFileSync(new URL("../portico-phones.js", import.meta.url), "utf8");
  return import("data:text/javascript;base64," + Buffer.from(src, "utf8").toString("base64"));
}

/* The wire of the server with blocks (B2): every page `{id, label, blocks}` plus the derived flat mirror
 * `cameras`/`actions` (only the kinds a schema-3 build knows), which the panel must ignore and drop. */
const V3_KINDS = ["portico_open", "lock_open", "button_press", "script_run", "scene_activate", "call"];
export const withMirror = (page) => ({
  ...page,
  cameras: page.blocks.flatMap((b) => b.cameras.map((c) => ({ id: c.id, label: c.label, entity_id: c.entity_id }))),
  actions: page.blocks.flatMap((b) => b.actions.filter((a) => V3_KINDS.includes(a.kind))),
});

const tpl = () => ({
  pages: [withMirror({
    id: "home", label: "Home",
    blocks: [{
      id: "home", title: "Home",
      cameras: [{ id: "gate", label: "Gate", entity_id: "camera.gate" }],
      actions: [{ id: "open-gate", label: "Open the gate", kind: "lock_open", entity_id: "lock.gate", icon: "gate-open", confirm: true }],
    }],
  })],
  rules: [], roster: ["*"],
});

export const DOOR_ID = "portico_intercom:http://10.6.0.7:8091";

/* A resolved page as the server's `content/get` / `content/preview` serve it: `blocks` (resolved) plus the flat mirror. */
export function resolvedPage(page) {
  return {
    ...page,
    cameras: page.blocks.flatMap((b) => b.cameras),
    actions: page.blocks.flatMap((b) => b.actions.filter((a) => V3_KINDS.includes(a.kind))),
  };
}

/* What the server's resolver makes of a DRAFT page (a stand-in, enough for the preview): effective camera sizes
 * (explicit, else the first big and the rest small), every action available except a talk button, which is off with
 * the reason `talk_off`; `door_available` when the block has a door. */
export function resolveDraftPage(page) {
  return resolvedPage({
    id: page.id, label: page.label,
    blocks: page.blocks.map((b) => ({
      id: b.id, title: b.title,
      ...(b.door ? { door: b.door, door_available: true } : {}),
      cameras: b.cameras.map((c, n) => ({ ...c, size: c.size === "big" || c.size === "small" ? c.size : n === 0 ? "big" : "small", name: c.label, available: true })),
      actions: b.actions.map((a) => (a.kind === "talk"
        ? { ...a, name: a.label, available: false, reason: "talk_off" }
        : { ...a, name: a.label, available: true })),
    })),
  });
}

export function makeLoaded() {
  return {
    document: {
      schema_version: 4, home_name: "My Home", template: tpl(),
      overrides: {
        phone1: { pages: [], rules: [], roster_hide: [] },
        "old-phone-user-id-123": {
          pages: [withMirror({ id: "home", label: "Home", blocks: [{ id: "home", title: "Home", cameras: [],
            actions: [{ id: "garage", label: "Garage", kind: "button_press", entity_id: "button.garage", confirm: false }] }] })],
          rules: [], roster_hide: [],
        },
      },
      dashboards: {}, endpoints: {},
    },
    doc_rev: "rev1",
    resolved: {
      phone1: { endpoint: "phone1", kind: "phone", pages: [resolvedPage({
        id: "home", label: "Home",
        blocks: [{
          id: "home", title: "Home",
          cameras: [{ id: "gate", label: "Gate", entity_id: "camera.gate", size: "big", name: "Gate camera", available: true }],
          actions: [{ id: "open-gate", label: "Open the gate", kind: "lock_open", entity_id: "lock.gate", icon: "gate-open", confirm: true, name: "Gate lock", available: true }],
        }],
      })] },
    },
    endpoints: [
      { id: "phone1", kind: "phone", name: "Alice's phone", label: "Alice's phone (enrolled 2026-10-08)", ha_user_id: "u1", cameras: [], room: null, available: true, reason: null },
      { id: "kitchen", kind: "dashboard", name: "Kitchen tablet", label: "Kitchen tablet", ha_user_id: "u2", cameras: [], room: "Kitchen", available: true, reason: null },
      { id: DOOR_ID, kind: "door", name: "Gate", label: "Gate", ha_user_id: null, cameras: ["camera.gate"], room: null, available: true, reason: null },
    ],
    orphans: ["old-phone-user-id-123"],
    ha_users: [],
  };
}

export const STATES = {
  "camera.gate": { state: "idle", attributes: { friendly_name: "Gate camera", entity_picture: "/api/camera_proxy/camera.gate" } },
  "camera.garage": { state: "idle", attributes: { friendly_name: "Garage camera" } },
  "camera.zebra": { state: "idle", attributes: {} },
  "lock.gate": { state: "locked", attributes: { friendly_name: "Gate lock" } },
  "lock.front": { state: "locked", attributes: { friendly_name: "Front door" } },
  "light.kitchen": { state: "on", attributes: { friendly_name: "Kitchen light" } },
  "script.evening": { state: "off", attributes: { friendly_name: "Evening" } },
  "scene.movie": { state: "off", attributes: { friendly_name: "Movie" } },
  "button.garage": { state: "unknown", attributes: { friendly_name: "Garage button" } },
};

/* handlers: type -> (msg) => result | throws {code, message}. Every call is recorded in `sent`. */
export function makeHass(handlers = {}, opts = {}) {
  const sent = [];
  const live = new Set();
  let subscribes = 0;
  const probes = [];
  const subMsgs = [];
  const hass = {
    states: opts.states || STATES,
    user: opts.user || { is_admin: true },
    callWS: async (msg) => {
      if (msg.type === "portico/version") {      // the contract probe: kept apart from `sent` so traffic assertions stay about content
        probes.push(msg);
        const v = handlers[msg.type];
        if (!v) return { contract: 3, ui_version: 1, providers: [] };
        return typeof v === "function" ? v(msg) : v;
      }
      sent.push(JSON.parse(JSON.stringify(msg)));
      const h = handlers[msg.type];
      if (!h) throw { code: "unknown_command", message: "Unknown command." };
      return typeof h === "function" ? h(msg) : h;
    },
    connection: {
      subscribeMessage: async (cb, msg) => {
        subscribes++;
        subMsgs.push(msg);
        await sleep(5);
        const handle = { cb };
        live.add(handle);
        return () => live.delete(handle);
      },
    },
  };
  return {
    hass, sent, live, probes, subMsgs, subscribes: () => subscribes,
    ofType: (t) => sent.filter((m) => m.type === t),
    fire: (event) => { for (const h of Array.from(live)) h.cb(event); },
  };
}

export function editorHass(overrides = {}, opts = {}) {
  const loaded = opts.loaded || makeLoaded();
  let calls = 0;
  const handlers = {
    "portico/content/document": () => { calls++; return JSON.parse(JSON.stringify(loaded)); },
    "portico/content/preview": (msg) => ({
      endpoint: msg.endpoint, kind: "phone", home_name: msg.document.home_name, rev: "r",
      pages: [...((msg.document.template || {}).pages || []), ...(((msg.document.overrides || {})[msg.endpoint] || {}).pages || [])]
        .map(resolveDraftPage),
      callers: {}, roster: [],
    }),
    "portico/content/save": () => ({ doc_rev: "rev2" }),
    ...overrides,
  };
  const f = makeHass(handlers, opts);
  f.documentCalls = () => calls;
  return f;
}
