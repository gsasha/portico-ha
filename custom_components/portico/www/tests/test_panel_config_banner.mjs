// DEV-824 P5: the editor's file-configuration banner (portico/config/status): pending hand edits, a failed write, a refused or deferred
// reload, and the quiet "file configuration off" note; read on load and again after content_changed; absent when all is well, when the
// server lacks the command, and for a non-administrator. Protocol-level, in jsdom with a fake `hass`; NOT a visual check.
import assert from "assert";
import { loadPanel, editorHass, sleep } from "./panel_loader.mjs";

const OK = { enabled: true, dir_exists: true, pending: [], pending_total: 0, last_write_error: null, last_reload: null };

async function mount(status, opts = {}) {
  const w = loadPanel();
  const state = { status };
  const handlers = status === undefined ? {} : { "portico/config/status": () => (state.status instanceof Error ? (() => { throw state.status; })() : JSON.parse(JSON.stringify(state.status))) };
  const fake = editorHass(handlers, { user: opts.user });
  const panel = w.doc.createElement("portico-panel");
  panel._previewMs = 20;
  panel.hass = fake.hass;
  w.doc.body.appendChild(panel);
  await sleep(80);
  return { ...w, panel, fake, state };
}
const q = (m, sel) => m.panel.shadowRoot.querySelector(sel);
const qa = (m, sel) => Array.from(m.panel.shadowRoot.querySelectorAll(sel));
const calls = (m) => m.fake.sent.filter((x) => x.type === "portico/config/status").length;

// 1. all well: asked once, no banner at all
{
  const m = await mount(OK);
  assert.strictEqual(calls(m), 1, "read once on load");
  assert.ok(!q(m, '[data-state="config-files"]'), "nothing to say, nothing shown");
  console.log("PASS: a healthy file configuration shows no banner");
}

// 2. pending hand edits: listed, capped, with the exact total; text is data, never markup
{
  const m = await mount({ ...OK, pending: ["pages/home.yaml", "elements/<b>x</b>.yaml", "a", "b", "c"], pending_total: 8 });
  const box = q(m, '[data-state="config-pending"]');
  assert.ok(box && /8 configuration files are edited by hand and not yet reloaded/.test(box.textContent), box && box.textContent);
  assert.ok(/portico\.reload_config/.test(box.textContent));
  assert.strictEqual(box.querySelectorAll("li").length, 6, "five paths and an 'and N more' line");
  assert.ok(/and 3 more/.test(box.textContent));
  assert.ok(box.textContent.includes("elements/<b>x</b>.yaml") && !box.querySelector("b"), "a path is text");
  assert.ok(box.classList.contains("warn"), "a warning, not an error");
  const one = await mount({ ...OK, pending: ["pages/home.yaml"], pending_total: 1 });
  assert.ok(/1 configuration file is edited by hand/.test(q(one, '[data-state="config-pending"]').textContent));
  console.log("PASS: pending hand edits are listed, capped, counted and escaped");
}

// 3. a failed write
{
  const m = await mount({ ...OK, last_write_error: { count: 2, errors: [{ path: "pages/home.yaml", code: "PermissionError" }, { path: "", code: "write" }] } });
  const box = q(m, '[data-state="config-not-written"]');
  assert.ok(box && box.classList.contains("err") && /2 configuration files could not be written/.test(box.textContent), box && box.textContent);
  assert.ok(/The last save is live/.test(box.textContent) && /pages\/home\.yaml \(PermissionError\)/.test(box.textContent));
  console.log("PASS: a failed file write is an error that says the save is live");
}

// 4. a refused reload: file:line:col, capped
{
  const errors = Array.from({ length: 7 }, (_, i) => ({ file: `elements/e${i}.yaml`, line: i + 2, col: 3, message: `bad <i>${i}</i>` }));
  const m = await mount({ ...OK, last_reload: { at: "2026-11-23T04:00:00+00:00", ok: false, applied: false, deferred: false, error_total: 25, errors } });
  const box = q(m, '[data-state="config-reload-errors"]');
  assert.ok(box && box.classList.contains("err") && /refused \(25 errors\); nothing was applied/.test(box.textContent), box && box.textContent);
  assert.ok(box.textContent.includes("elements/e0.yaml:2:3 bad <i>0</i>") && !box.querySelector("i"));
  assert.strictEqual(box.querySelectorAll("li").length, 6);
  assert.ok(/and 20 more/.test(box.textContent));
  console.log("PASS: a refused reload lists file:line:col, capped, with the total");
}

// 5. deferred, and a good reload says nothing
{
  const m = await mount({ ...OK, last_reload: { at: "x", ok: true, applied: false, deferred: true, error_total: 0, errors: [] } });
  const box = q(m, '[data-state="config-reload-deferred"]');
  assert.ok(box && box.classList.contains("warn") && /deferred: a call is live/.test(box.textContent));
  const good = await mount({ ...OK, last_reload: { at: "x", ok: true, applied: true, deferred: false, error_total: 0, errors: [] } });
  assert.ok(!q(good, '[data-state="config-files"]'));
  console.log("PASS: a deferred reload is a warning; a good one is silent");
}

// 6. off: a quiet note, not an error and not a warning
{
  const off = await mount({ ...OK, enabled: false });
  const note = q(off, '[data-state="config-off"]');
  assert.ok(note && note.classList.contains("note") && !note.classList.contains("err") && !note.classList.contains("warn") && /off/.test(note.textContent));
  const nodir = await mount({ ...OK, dir_exists: false });
  const note2 = q(nodir, '[data-state="config-off"]');
  assert.ok(note2 && note2.classList.contains("note") && /portico\.export_config/.test(note2.textContent));
  assert.ok(!q(nodir, '[data-state="config-pending"]') && !q(nodir, '[data-state="config-reload-errors"]'));
  console.log("PASS: file configuration off, or no folder, is a quiet note");
}

// 7. it shows on every tab, and is re-read after content_changed
{
  const m = await mount(OK);
  assert.ok(!q(m, '[data-state="config-files"]'));
  m.state.status = { ...OK, pending: ["pages/home.yaml"], pending_total: 1 };
  m.fake.fire({ type: "content_changed" });
  await sleep(80);
  assert.strictEqual(calls(m), 2, "read again after content_changed");
  assert.ok(q(m, '[data-state="config-pending"]'), "the new state is shown");
  const tab = qa(m, '[data-act^="tab-"]').find((b) => b.dataset.act !== "tab-pages");
  assert.ok(tab, "another tab exists");
  tab.dispatchEvent(new m.dom.window.Event("click", { bubbles: true }));
  await sleep(60);
  assert.ok(q(m, '[data-state="config-pending"]'), "still shown on " + tab.dataset.act);
  m.state.status = OK;
  m.fake.fire({ type: "content_changed" });
  await sleep(80);
  assert.ok(!q(m, '[data-state="config-files"]'), "gone once the files are reloaded");
  console.log("PASS: read on load and after content_changed, shown on every tab, cleared when resolved");
}

// 8. an old server (no command), a failing read, a non-administrator: no banner, the editor works
{
  const none = await mount(undefined);
  assert.ok(q(none, '[data-act="save"]') && !q(none, '[data-state="config-files"]'), "unknown command: the editor as before");
  const broke = await mount(new Error("boom"));
  assert.ok(q(broke, '[data-act="save"]') && !q(broke, '[data-state="config-files"]'));
  const user = await mount(OK, { user: { is_admin: false } });
  assert.strictEqual(calls(user), 0, "a non-administrator never asks");
  console.log("PASS: no command, a failed read or a non-administrator: no banner, no error");
}
process.exit(0);
