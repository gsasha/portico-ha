// The editor panel in jsdom with a fake `hass`: load, pickers, save / conflict / invalid, the restart message,
// the debounced preview, "Not attached", escaping of user text, subscription, the read-only view.
// Protocol-level; NOT a visual check (see panel_harness.html).
import assert from "assert";
import { loadPanel, editorHass, makeHass, makeLoaded, sleep, STATES } from "./panel_loader.mjs";

const { dom, Panel, doc } = loadPanel();
assert.ok(Panel, "portico-panel was not registered");
const win = dom.window;

async function mount(fake, { previewMs = 20 } = {}) {
  const panel = doc.createElement("portico-panel");
  panel._previewMs = previewMs;
  panel.hass = fake.hass;
  doc.body.appendChild(panel);
  await sleep(40);
  return panel;
}
const q = (p, sel) => p.shadowRoot.querySelector(sel);
const qa = (p, sel) => Array.from(p.shadowRoot.querySelectorAll(sel));
const act = (p, name) => q(p, `[data-act="${name}"]`);
const click = (node) => { assert.ok(node, "clicked something that is not there"); node.dispatchEvent(new win.Event("click", { bubbles: true })); };
function setValue(node, value, type = "input") {
  node.value = value;
  node.dispatchEvent(new win.Event(type, { bubbles: true }));
}
const text = (p) => p.shadowRoot.textContent;
const labelsOf = (p, sel) => qa(p, sel).map((n) => n.textContent);

async function addCamera(panel, entity, label) {
  click(act(panel, "add-camera"));
  const sel = q(panel, '[data-dialog="cameras"] [data-field="target"]');
  setValue(sel, entity, "change");
  if (label !== undefined) setValue(q(panel, '[data-dialog="cameras"] [data-field="label"]'), label);
  click(act(panel, "dialog-ok"));
}

async function main() {
  // 1. load: the sidebar, the Pages tab on the template
  {
    const f = editorHass();
    const panel = await mount(f);
    assert.strictEqual(f.ofType("portico/content/document").length, 1);
    assert.deepStrictEqual(qa(panel, "nav [data-sel]").map((b) => b.dataset.sel),
      ["template", "ep:phone1", "ep:kitchen", "orphan:old-phone-user-id-123"]);
    assert.ok(text(panel).includes("Household pages show on every phone; override one phone only to make it differ."));
    assert.deepStrictEqual(labelsOf(panel, ".pagelist [data-page] .txt button"), ["Home"]);
    assert.deepStrictEqual(labelsOf(panel, ".pagedetail .row .label"), ["Gate", "Open the gate"]);
    assert.strictEqual(q(panel, '[data-status]').dataset.status, "saved");
    assert.strictEqual(act(panel, "save").disabled, true, "nothing to save yet");
    console.log("PASS: the editor loads the document, lists the template, phones, screens and Not attached");
    panel.remove();
  }

  // 2. the camera picker is filtered to cameras, with friendly names, and the filter box works
  {
    const f = editorHass();
    const panel = await mount(f);
    click(act(panel, "add-camera"));
    const options = () => qa(panel, '[data-dialog="cameras"] [data-field="target"] option').map((o) => o.value).filter(Boolean);
    assert.deepStrictEqual(options(), ["camera.gate", "camera.garage", "camera.zebra"].sort((a, b) =>
      (STATES[a].attributes.friendly_name || a).localeCompare(STATES[b].attributes.friendly_name || b)));
    assert.ok(!options().some((v) => !v.startsWith("camera.")), "no non-camera entity is offered");
    assert.ok(labelsOf(panel, '[data-dialog="cameras"] option').includes("Garage camera (camera.garage)"));
    setValue(q(panel, '[data-dialog="cameras"] [data-field="filter"]'), "garage");
    assert.deepStrictEqual(options(), ["camera.garage"]);
    // choosing fills the name; the name can still be changed
    setValue(q(panel, '[data-dialog="cameras"] [data-field="target"]'), "camera.garage", "change");
    assert.strictEqual(q(panel, '[data-dialog="cameras"] [data-field="label"]').value, "Garage camera");
    setValue(q(panel, '[data-dialog="cameras"] [data-field="label"]'), "Garage");
    setValue(q(panel, '[data-dialog="cameras"] [data-field="target"]'), "camera.garage", "change");
    assert.strictEqual(q(panel, '[data-dialog="cameras"] [data-field="label"]').value, "Garage", "a typed name is not overwritten");
    console.log("PASS: the camera picker lists only camera.* with friendly names and filters by text");
    panel.remove();
  }

  // 3. add a camera and a button, then Save: the exact message, the reload, "Saved"
  {
    const f = editorHass();
    const panel = await mount(f);
    await addCamera(panel, "camera.garage", "Garage");
    assert.deepStrictEqual(labelsOf(panel, ".pagedetail .row .label"), ["Gate", "Garage", "Open the gate"]);
    assert.strictEqual(q(panel, '[data-status]').dataset.status, "dirty");

    click(act(panel, "add-button"));
    const form = '[data-dialog="actions"]';
    assert.deepStrictEqual(qa(panel, `${form} [data-field="kind"] option`).map((o) => o.value),
      ["portico_open", "lock_open", "lock_lock", "lock_unlock", "cover_open", "cover_close", "button_press", "script_run", "scene_activate", "talk"],
      "the kinds the blocks design allows, Talk last");
    assert.strictEqual(q(panel, `${form} [data-field="kind"] option[value="talk"]`).disabled, false, "Talk can be chosen");
    setValue(q(panel, `${form} [data-field="kind"]`), "script_run", "change");
    assert.ok(qa(panel, `${form} [data-field="target"] option`).every((o) => !o.value || o.value.startsWith("script.")), "entities follow the kind's domain");
    assert.strictEqual(q(panel, `${form} [data-field="confirm"]`).checked, false, "scripts do not ask first by default");
    setValue(q(panel, `${form} [data-field="kind"]`), "lock_open", "change");
    assert.strictEqual(q(panel, `${form} [data-field="confirm"]`).checked, true, "locks do");
    setValue(q(panel, `${form} [data-field="target"]`), "lock.front", "change");
    setValue(q(panel, `${form} [data-field="label"]`), "Front door");
    click(q(panel, `${form} [data-icon="door-open"]`));
    click(act(panel, "dialog-ok"));
    assert.ok(!q(panel, form), "the form closes");

    click(act(panel, "save"));
    await sleep(40);
    const saves = f.ofType("portico/content/save");
    assert.strictEqual(saves.length, 1);
    assert.strictEqual(saves[0].base_doc_rev, "rev1");
    assert.ok(!("drop_orphans" in saves[0]), "no drop list unless an orphan was moved or deleted");
    const page = saves[0].document.template.pages[0];
    assert.deepStrictEqual(page.blocks[0].cameras.map((c) => c.entity_id), ["camera.gate", "camera.garage"]);
    assert.ok(!("cameras" in page) && !("actions" in page), "no flat mirror goes back on save");
    assert.strictEqual(saves[0].document.schema_version, 4);
    assert.deepStrictEqual(page.blocks[0].actions[1], { id: "front-door", label: "Front door", kind: "lock_open", entity_id: "lock.front", icon: "door-open", confirm: true });
    assert.strictEqual(f.ofType("portico/content/document").length, 2, "reloaded after the save");
    assert.ok(text(panel).includes("Saved."));
    console.log("PASS: add a camera and a lock button, Save sends the document with base_doc_rev and reloads");
    panel.remove();
  }

  // 4. a stale base: the panel reloads and says so; the draft is not saved
  {
    const f = editorHass({ "portico/content/save": () => { throw { code: "conflict", message: "the document changed since it was loaded" }; } });
    const panel = await mount(f);
    await addCamera(panel, "camera.garage", "Garage");
    click(act(panel, "save"));
    await sleep(40);
    assert.strictEqual(f.ofType("portico/content/document").length, 2, "reloaded on conflict");
    const msg = q(panel, '[data-state="message"]');
    assert.ok(msg && msg.textContent.includes("Another session saved first"), msg && msg.textContent);
    assert.deepStrictEqual(labelsOf(panel, ".pagedetail .row .label"), ["Gate", "Open the gate"], "shows the saved version");
    assert.strictEqual(q(panel, '[data-status]').dataset.status, "saved");
    console.log("PASS: a conflict reloads the saved version and tells the user another session saved first");
    panel.remove();
  }

  // 5. invalid from the server: the text is shown, the draft stays
  {
    const f = editorHass({ "portico/content/save": () => { throw { code: "invalid", message: "page 'home': camera 2: a camera entity is required" }; } });
    const panel = await mount(f);
    await addCamera(panel, "camera.garage", "Garage");
    click(act(panel, "save"));
    await sleep(40);
    assert.ok(q(panel, '[data-state="message"]').textContent.includes("camera 2: a camera entity is required"));
    assert.strictEqual(f.ofType("portico/content/document").length, 1, "no reload: the work is kept");
    assert.strictEqual(q(panel, '[data-status]').dataset.status, "dirty");
    assert.strictEqual(act(panel, "save").disabled, false, "can try again");
    console.log("PASS: an invalid save shows the server's text and keeps the draft");
    panel.remove();
  }

  // 6. unknown_command: the restart message, no editor
  {
    const f = makeHass({});          // no portico/content/document handler: HA answers unknown_command
    const panel = await mount(f);
    const box = q(panel, '[data-state="needs-restart"]');
    assert.ok(box, "the restart box is shown");
    assert.ok(box.textContent.includes("Restart Home Assistant to enable the editor"), box.textContent);
    assert.ok(!q(panel, ".layout") && !act(panel, "save"), "no editor behind it");
    click(act(panel, "retry"));
    await sleep(20);
    assert.strictEqual(f.ofType("portico/content/document").length, 2, "Check again asks again");
    console.log("PASS: unknown_command on content/document says to restart Home Assistant");
    panel.remove();
  }

  // 7. the preview is debounced (one call for a burst, with the final draft) and a stale reply is dropped
  {
    const f = editorHass();
    const panel = await mount(f, { previewMs: 60 });
    assert.strictEqual(Panel.logic.PREVIEW_DEBOUNCE_MS, 300, "the shipped delay is 300 ms");
    assert.strictEqual(new Panel()._previewMs, 300);
    await sleep(120);
    const base = f.ofType("portico/content/preview").length;
    assert.strictEqual(base, 1, "one preview after the load");
    for (const name of ["A", "B", "C"]) {
      setValue(q(panel, '[data-field="new-page"]'), name);
      click(act(panel, "add-page"));
      await sleep(10);
    }
    assert.strictEqual(f.ofType("portico/content/preview").length, base, "nothing sent inside the window");
    await sleep(150);
    const previews = f.ofType("portico/content/preview");
    assert.strictEqual(previews.length, base + 1, "one call for the burst");
    assert.deepStrictEqual(previews.at(-1).document.template.pages.map((p) => p.label), ["Home", "A", "B", "C"], "carrying the final draft");
    assert.strictEqual(previews.at(-1).endpoint, "phone1", "the template previews as the first phone");
    assert.deepStrictEqual(labelsOf(panel, "[data-preview] [data-act=preview-page]"), ["Home", "A", "B", "C"], "the preview draws the resolved pages");
    assert.ok(q(panel, "[data-preview]").textContent.includes("Empty block."), "and shows the page being edited (a new page starts with one empty block)");
    click(qa(panel, "[data-preview] [data-act=preview-page]")[0]);
    assert.ok(q(panel, "[data-preview]").textContent.includes("Open the gate"), "the preview's own page tabs switch pages");
    console.log("PASS: the preview is debounced, sends the final draft once, via content/preview");
    panel.remove();

    // out-of-order replies: the older one must not overwrite the newer
    let n = 0;
    const g = editorHass({
      "portico/content/preview": async (msg) => {
        const mine = ++n;
        await sleep(mine === 2 ? 5 : 80);      // the second request answers first
        return { endpoint: msg.endpoint, kind: "phone", rev: "r", callers: {}, roster: [],
                 pages: [{ id: "home", label: `reply ${mine}`, cameras: [], actions: [] }, { id: "b", label: "b", cameras: [], actions: [] }] };
      },
    });
    const p2 = await mount(g, { previewMs: 10 });
    await sleep(15);                            // the first preview is in flight
    setValue(q(p2, '[data-field="new-page"]'), "Z");
    click(act(p2, "add-page"));
    await sleep(160);
    assert.strictEqual(g.ofType("portico/content/preview").length, 2);
    const shown = q(p2, "[data-preview]").textContent;
    assert.ok(shown.includes("reply 2"), "the newer reply is shown: " + shown);
    assert.ok(!shown.includes("reply 1"), "the stale reply was dropped: " + shown);
    console.log("PASS: a stale preview reply never overwrites a newer one");
    p2.remove();
  }

  // 8. user text is text, never markup
  {
    const evil = `<img src=x onerror="window.__pwn=1"><script>window.__pwn=2</script>&amp;`;
    const loaded = makeLoaded();
    loaded.document.template.pages[0].label = evil;
    loaded.document.template.pages[0].blocks[0].cameras[0].label = evil;
    loaded.document.template.pages[0].blocks[0].actions[0].label = evil;
    loaded.resolved.phone1.pages[0].label = evil;
    loaded.resolved.phone1.pages[0].blocks[0].actions[0].label = evil;
    loaded.endpoints[0].label = evil;
    const states = { ...STATES, "camera.gate": { state: "idle", attributes: { friendly_name: evil } } };
    const f = editorHass({}, { loaded, states });
    const panel = await mount(f);
    await sleep(60);
    click(act(panel, "tab-devices"));
    click(act(panel, "tab-pages"));
    click(act(panel, "add-camera"));
    assert.ok(text(panel).includes(evil), "the hostile text is on the page as text");
    for (const bad of ["img[src=x]", "script", "[onerror]"]) assert.ok(!q(panel, bad), `no element made from user text: ${bad}`);
    assert.strictEqual(win.__pwn, undefined, "nothing ran");
    // a page name typed by the user as well
    setValue(q(panel, '[data-field="new-page"]'), "<b id=pwn>x</b>");
    click(act(panel, "add-page"));
    assert.ok(text(panel).includes("<b id=pwn>x</b>") && !q(panel, "#pwn") && !q(panel, "b"), "a typed page name is text too");
    console.log("PASS: labels, friendly names and endpoint names are rendered as text");
    panel.remove();
  }

  // 9. "Not attached": move to a phone / delete, saved with drop_orphans
  {
    const f = editorHass();
    const panel = await mount(f);
    click(q(panel, '[data-sel="orphan:old-phone-user-id-123"]'));
    assert.ok(text(panel).includes("Garage (Button)"), "its buttons are listed");
    setValue(q(panel, '[data-field="move-to"]'), "phone1", "change");
    click(act(panel, "orphan-move"));
    assert.ok(!q(panel, '[data-sel="orphan:old-phone-user-id-123"]'), "the entry leaves the sidebar");
    assert.strictEqual(q(panel, '[data-sel="ep:phone1"]').classList.contains("sel"), true, "and the phone is selected");
    assert.ok(text(panel).includes("Garage"));
    click(act(panel, "save"));
    await sleep(40);
    const save = f.ofType("portico/content/save")[0];
    assert.deepStrictEqual(save.drop_orphans, ["old-phone-user-id-123"]);
    assert.ok(!("old-phone-user-id-123" in save.document.overrides));
    assert.strictEqual(save.document.overrides.phone1.pages[0].blocks[0].actions[0].id, "garage");
    panel.remove();

    const g = editorHass();
    const p2 = await mount(g);
    click(q(p2, '[data-sel="orphan:old-phone-user-id-123"]'));
    click(act(p2, "orphan-delete"));
    click(act(p2, "save"));
    await sleep(40);
    const s2 = g.ofType("portico/content/save")[0];
    assert.deepStrictEqual(s2.drop_orphans, ["old-phone-user-id-123"]);
    assert.ok(!("old-phone-user-id-123" in s2.document.overrides));
    p2.remove();

    // Discard brings it back
    const h = editorHass();
    const p3 = await mount(h);
    click(q(p3, '[data-sel="orphan:old-phone-user-id-123"]'));
    click(act(p3, "orphan-delete"));
    assert.ok(!q(p3, '[data-sel="orphan:old-phone-user-id-123"]'));
    click(act(p3, "discard"));
    assert.ok(q(p3, '[data-sel="orphan:old-phone-user-id-123"]'), "Discard restores it");
    p3.remove();
    console.log("PASS: Not attached overrides can be moved to a phone or deleted; Save sends drop_orphans; Discard restores");
  }

  // 10. a phone's own page set: Override a household page, add a page, edit it, Discard, Undo
  {
    const f = editorHass();
    const panel = await mount(f);
    click(q(panel, '[data-sel="ep:phone1"]'));
    assert.ok(text(panel).includes("These pages are added to the household pages"));
    assert.strictEqual(qa(panel, ".pagedetail [data-act=remove-tile]").length, 0, "household tiles are read-only");
    click(act(panel, "override-page"));
    assert.strictEqual(qa(panel, ".pagedetail [data-act=remove-tile]").length, 2, "an overridden page is editable");
    click(qa(panel, ".pagedetail [data-act=remove-tile]")[1]);
    assert.deepStrictEqual(labelsOf(panel, ".pagedetail .row .label"), ["Gate"]);
    click(act(panel, "undo"));
    assert.deepStrictEqual(labelsOf(panel, ".pagedetail .row .label"), ["Gate", "Open the gate"]);
    click(act(panel, "undo"));
    assert.strictEqual(q(panel, '[data-status]').dataset.status, "saved", "undo back to the start");
    setValue(q(panel, '[data-field="new-page"]'), "Pool");
    click(act(panel, "add-page"));
    assert.ok(text(panel).includes("Only here"));
    const name = q(panel, '[data-field="page-label"]');
    setValue(name, "Pool deck", "change");
    assert.deepStrictEqual(labelsOf(panel, ".pagelist [data-page] .txt button"), ["Home", "Pool deck"]);
    click(act(panel, "discard"));
    assert.deepStrictEqual(labelsOf(panel, ".pagelist [data-page] .txt button"), ["Home"]);
    console.log("PASS: a phone overrides a household page, adds its own, and Undo / Discard work");
    panel.remove();
  }

  // 11. client-side problems block Save and show why
  {
    const f = editorHass();
    const panel = await mount(f);
    setValue(q(panel, '[data-field="new-page"]'), "x".repeat(41));
    click(act(panel, "add-page"));
    assert.ok(q(panel, '[data-state="message"]').textContent.includes("longer than 40"), "the edit is refused with a reason");
    assert.strictEqual(q(panel, '[data-status]').dataset.status, "saved", "and changes nothing");
    // a rule that uses a button: removing the button is flagged and Save is off
    const loaded = makeLoaded();
    loaded.document.template.rules = [{ id: "doors", caller: "kind:door", cameras: null, ring_buttons: ["open-gate"], call_buttons: [] }];
    const g = editorHass({}, { loaded });
    const p2 = await mount(g);
    click(qa(p2, ".pagedetail [data-act=remove-tile]")[1]);
    assert.ok(q(p2, '[data-state="problems"]').textContent.includes('rule "doors" still uses the button "open-gate"'));
    assert.strictEqual(act(p2, "save").disabled, true);
    click(act(p2, "undo"));
    assert.ok(!q(p2, '[data-state="problems"]'));
    console.log("PASS: a refused edit says why; a problem the server would refuse disables Save");
    panel.remove();
    p2.remove();
  }

  // 12. content_changed: reload when clean, banner (no reload) when there is unsaved work
  {
    const f = editorHass();
    const panel = await mount(f);
    f.fire({ type: "content_changed", endpoint: "phone1", rev: null });
    await sleep(30);
    assert.strictEqual(f.ofType("portico/content/document").length, 2, "a clean panel reloads");
    await addCamera(panel, "camera.garage", "Garage");
    f.fire({ type: "content_changed", endpoint: "phone1", rev: null });
    await sleep(30);
    assert.strictEqual(f.ofType("portico/content/document").length, 2, "unsaved work is not clobbered");
    assert.ok(q(panel, '[data-state="stale"]'), "but the user is told");
    assert.deepStrictEqual(labelsOf(panel, ".pagedetail .row .label"), ["Gate", "Garage", "Open the gate"]);
    console.log("PASS: content_changed reloads a clean editor and only warns when there is unsaved work");
    panel.remove();
  }

  // 13. subscribes once whatever the order of hass and connect; unsubscribes on disconnect
  {
    const f = editorHass();
    const panel = doc.createElement("portico-panel");
    panel.hass = f.hass;
    doc.body.appendChild(panel);
    await sleep(40);
    assert.strictEqual(f.subscribes(), 1);
    assert.deepStrictEqual(f.subMsgs, [{ type: "portico/content/subscribe" }], "contract 3 subscription: no endpoint, an admin hears every change");
    assert.strictEqual(f.live.size, 1);
    panel.remove();
    assert.strictEqual(f.live.size, 0, "unsubscribed on disconnect");
    doc.body.appendChild(panel);
    await sleep(40);
    assert.strictEqual(f.subscribes(), 2);
    assert.strictEqual(f.live.size, 1, "exactly one live subscription after re-attach");
    panel.remove();
    const g = editorHass();
    const p2 = doc.createElement("portico-panel");
    doc.body.appendChild(p2);
    p2.hass = g.hass;
    await sleep(40);
    assert.strictEqual(g.subscribes(), 1);
    p2.remove();
    console.log("PASS: one subscription, whichever of hass and connect comes first");
  }

  // 14. a user who is not an administrator: read-only, own endpoint, no editor commands
  {
    const f = makeHass({
      "portico/content/get": () => ({ endpoint: "dina-phone", kind: "phone", rev: "x", home_name: "My Home", callers: {}, roster: [],
        pages: [{ id: "home", label: "Home", cameras: [{ id: "g", label: "Gate", entity_id: "camera.gate", available: true }],
                  actions: [{ id: "a", label: "Open", kind: "lock_open", entity_id: "lock.gate", available: true }] }] }),
    }, { user: { is_admin: false } });
    const panel = await mount(f);
    assert.deepStrictEqual(f.sent, [{ type: "portico/content/get" }], "contract 3: no profile, no pages key");
    assert.deepStrictEqual(f.subMsgs, [{ type: "portico/content/subscribe" }], "the read-only view listens for its own endpoint");
    assert.ok(text(panel).includes("Only an administrator can change it."));
    assert.deepStrictEqual(labelsOf(panel, ".cam .label"), ["Gate"]);
    assert.ok(!act(panel, "save") && !act(panel, "add-camera"), "no editing controls");
    panel.remove();
    const g = makeHass({ "portico/content/get": () => { throw { code: "unauthorized", message: "x" }; } }, { user: { is_admin: false } });
    const p2 = await mount(g);
    assert.ok(text(p2).includes("This login has no Portico phone or screen."));
    p2.remove();
    console.log("PASS: a non-administrator sees their own pages read-only, or a clear message");
  }

  // 15. what the app and the card show of the pages (the hint), no preview badge, and the first page defaults to "Home"
  {
    const f = editorHass();
    const panel = await mount(f, { previewMs: 10 });
    await sleep(60);
    const hint = q(panel, '[data-hint="old-clients"]');
    assert.ok(hint, "the Pages tab carries the hint");
    assert.strictEqual(hint.textContent, "The phone app shows every page as a tab in its top line, and the card shows the pages of its endpoint the same way. With one page there are no tabs.");
    assert.ok(!hint.textContent.includes("0.4.3") && !hint.textContent.includes("id home"), "no stale app version or special page");
    assert.strictEqual(qa(panel, "[data-preview] [data-badge]").length, 0, "no page is badged as the one old clients read");
    setValue(q(panel, '[data-field="new-page"]'), "Garden");
    click(act(panel, "add-page"));
    await sleep(80);
    const tabs = qa(panel, "[data-preview] .pagetabs > *");
    assert.deepStrictEqual(tabs.map((n) => n.textContent), ["Home", "Garden"], "the preview tabs are the pages, nothing else");
    panel.remove();

    // no home page: no page is special
    const loaded = makeLoaded();
    loaded.document.template.pages = [{ id: "home", label: "Home", cameras: [], actions: [] }, { id: "test", label: "test", cameras: [], actions: [] }];
    const g = editorHass({}, { loaded });
    const p2 = await mount(g, { previewMs: 10 });
    await sleep(60);
    assert.deepStrictEqual(qa(p2, "[data-preview] .pagetabs > *").map((n) => n.textContent), ["Home", "test"]);
    p2.remove();

    // an empty page set: an empty name creates "Home" (id home); with a home present an empty name is still refused
    const empty = makeLoaded();
    empty.document.template.pages = [];
    const h = editorHass({}, { loaded: empty });
    const p3 = await mount(h, { previewMs: 10 });
    assert.ok(q(p3, '[data-field="new-page"]').placeholder.includes("Home"));
    click(act(p3, "add-page"));
    await sleep(20);
    assert.deepStrictEqual(labelsOf(p3, ".pagelist [data-page] .txt button"), ["Home"]);
    assert.strictEqual(q(p3, ".pagelist [data-page]").dataset.page, "home");
    click(act(p3, "add-page"));
    assert.deepStrictEqual(labelsOf(p3, ".pagelist [data-page] .txt button"), ["Home"], "an empty name adds nothing once there is a home");
    p3.remove();
    console.log("PASS: the Pages tab says what the app and the card show, no page is badged, the first page is Home");
  }

  // 16. the contract probe: contract 3 expected; any other answer is one clear line and no further traffic
  {
    const ok = editorHass();
    const p0 = await mount(ok);
    assert.deepStrictEqual(ok.probes, [{ type: "portico/version" }], "one probe per start");
    assert.ok(q(p0, ".pagedetail, .pagelist"), "contract 3: the editor loads");
    p0.remove();
    for (const [label, handler] of [
      ["contract 2", () => ({ contract: 2 })],
      ["no contract", () => ({})],
      ["unknown_command", () => { throw { code: "unknown_command", message: "Unknown command." }; }],
    ]) {
      const f = editorHass({ "portico/version": handler });
      const panel = await mount(f);
      assert.ok(text(panel).includes("needs Portico contract 3"), `${label}: the one-line message`);
      assert.ok(text(panel).includes("reload this page"), `${label}: says what to do`);
      assert.strictEqual(f.sent.length, 0, `${label}: no document, no content traffic`);
      assert.strictEqual(f.subscribes(), 0, `${label}: nothing subscribed`);
      panel.remove();
    }
    const g = editorHass({ "portico/version": () => { throw { code: "home_assistant_error", message: "gone" }; } });
    const p2 = await mount(g);
    assert.strictEqual(g.documentCalls(), 1, "a probe that fails for another reason does not block the load");
    p2.remove();
    console.log("PASS: the version probe expects contract 3 and says so in one line otherwise");
  }
}
main().then(() => { console.log("ALL_PANEL_EDITOR_TESTS_PASSED"); process.exit(0); }, (e) => { console.error(e); process.exit(1); });
