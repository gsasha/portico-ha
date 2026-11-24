"""DEV-824 P3: the outbound half (config_sync.py): the files follow the editor's saves, `portico_config_changed`, the initial export.

HA-harness session. Pinned: the first export then `reload_config` shows zero changes (and P1's prove_roundtrip is clean); the export
refuses a directory that has files unless `overwrite`; an editor save writes only the files that differ and fires the event with
paths only; no diff, no write, no event; the apply never writes a file or fires the event and what it adopted is not a pending
edit; a hand edit is left alone and reported; a write failure never fails the save; the option switches writing off; path hygiene
(a symlinked root or file); and no secret or `open_relay_action` ever reaches a file.
"""
from __future__ import annotations

import json
import os
from unittest.mock import patch

import pytest
from homeassistant.helpers import issue_registry as ir

from .. import config_apply, config_sync, config_yaml as cy
from ..config_flow import DOMAIN
from ..config_service import async_setup_config_service, read_tree
from ..elements_commands import async_setup_elements_commands
from ..ui_settings import async_setup_ui_settings_commands
from .test_config_apply import (CAMERA, _edited, _settings, _tree, _with_element, world)  # noqa: F401 - fixture by name
from .test_push_registry import _custom_integrations  # noqa: F401 - autouse fixture by name

pytestmark = pytest.mark.asyncio


@pytest.fixture
def root(world, hass):
    async_setup_config_service(hass)
    config_sync.async_setup_config_sync(hass)
    return hass.config.path("portico")


async def _export(hass, **data):
    return await hass.services.async_call(DOMAIN, "export_config", data, blocking=True, return_response=True)


async def _settle(hass):
    state = config_sync._state(hass)
    while state.task is not None and not state.task.done():
        await state.task
    await hass.async_block_till_done()


async def _camera(world, name="Yard cam"):
    """A new camera element (the server mints its id); returns the id."""
    return (await world.elements.async_set({"kind": "camera", "name": name, "entity_id": "camera.yard"}, world.elements.rev))["element"]["id"]


def _disk(root):
    files, errors = read_tree(root)
    assert errors == []
    return files


# ---- the initial export ----------------------------------------------------------------------------------------------------------

async def test_a_dry_run_export_lists_the_files_and_writes_nothing(world, hass, root):
    reply = await _export(hass, dry_run=True)
    assert reply["ok"] and reply["written"] is False and "portico.yaml" in reply["files"] and "index.yaml" in reply["files"]
    assert not os.path.exists(root) and world.events == []


async def test_export_writes_the_first_files_and_reload_config_round_trips_with_zero_changes(world, hass, root):
    reply = await _export(hass)
    assert reply["ok"] and reply["written"] and set(reply["added"]) == set(_tree(world))
    await hass.async_block_till_done()
    assert len(world.events) == 1 and world.events[0].data["source"] == "export"
    assert _disk(root) == _tree(world)
    assert cy.prove_roundtrip(world.content.document(), [e for e in world.elements.stored() if not e.get("managed")],
                              _settings(world)) == []
    files, errors = read_tree(root)
    dry = await config_apply.reload_config(hass, files, errors, dry_run=True)
    assert dry["ok"] and dry["changed"] is False and dry["errors"] == []
    report = dry["report"]
    assert report["elements"] == {"added": [], "changed": [], "removed": []} and report["pages"]["changed"] == []
    assert report["deletions"] == [] and report["sensitive"] == [] and not any(r["changed"] for r in report["per_device"])
    assert dry["report"]["settings"]["changed"] == []


async def test_export_refuses_a_directory_that_already_has_files_unless_overwrite(world, hass, root):
    await _export(hass)
    again = await _export(hass)
    assert again["ok"] is False and again["errors"][0]["code"] == "exists" and again["written"] is False
    with open(os.path.join(root, "pages", "home.yaml"), "a", encoding="utf-8") as handle:
        handle.write("# a hand edit\n")
    forced = await _export(hass, overwrite=True)
    assert forced["ok"] and "pages/home.yaml" in forced["changed"]
    assert _disk(root) == _tree(world)


async def test_export_overwrite_removes_stray_files_in_the_managed_directories_only(world, hass, root):
    await _export(hass)
    os.makedirs(os.path.join(root, "elements"), exist_ok=True)
    with open(os.path.join(root, "elements", "el-stray.yaml"), "w", encoding="utf-8") as handle:
        handle.write("id: el-stray\n")
    with open(os.path.join(root, "README.md"), "w", encoding="utf-8") as handle:
        handle.write("mine\n")
    dry = await _export(hass, dry_run=True, overwrite=True)
    assert dry["would_remove"] == ["elements/el-stray.yaml"]
    done = await _export(hass, overwrite=True)
    assert done["removed"] == ["elements/el-stray.yaml"] and os.path.exists(os.path.join(root, "README.md"))


async def test_no_secret_and_no_open_relay_action_is_ever_in_a_file(world, hass, root):
    await _export(hass)
    text = "\n".join(_disk(root).values())
    assert "open_relay_action" not in text and "password" not in text.lower() and "token" not in text.lower()
    # a tree that would carry one is never written
    bad = {**world.content.document(), "open_relay_action": {"service": "x"}}
    with patch.object(type(world.content), "document", lambda self: bad):
        with pytest.raises(ValueError):
            config_sync.snapshot(hass)


# ---- the editor save path ---------------------------------------------------------------------------------------------------------

async def test_a_save_writes_only_the_files_that_differ_and_fires_the_event_with_paths_only(world, hass, root):
    await _export(hass)
    await hass.async_block_till_done()
    world.events.clear()
    cid = await _camera(world)
    config_sync.async_schedule(hass)
    await _settle(hass)
    assert len(world.events) == 1
    data = world.events[0].data
    assert data["source"] == "editor" and f"elements/{cid}.yaml" in data["added"] and "index.yaml" in data["changed"]
    assert data["pending"] == [] and data["doc_rev"] == world.content.doc_rev
    assert "Yard cam" not in json.dumps(dict(data))              # paths and a summary: never file contents
    assert _disk(root) == _tree(world)
    world.events.clear()
    config_sync.async_schedule(hass)                             # nothing changed: no diff, no write, no event
    await _settle(hass)
    assert world.events == []


async def test_deleting_an_element_removes_its_file(world, hass, root):
    cid = await _camera(world)
    await _export(hass)
    assert f"elements/{cid}.yaml" in _disk(root)
    await hass.async_block_till_done()
    world.events.clear()
    await world.elements.async_delete(cid, world.elements.rev)
    config_sync.async_schedule(hass)
    await _settle(hass)
    assert f"elements/{cid}.yaml" not in _disk(root) and world.events[0].data["removed"] == [f"elements/{cid}.yaml"]


async def test_the_websocket_saves_trigger_the_files_and_a_validate_only_does_not(world, hass, root, hass_ws_client):
    async_setup_elements_commands(hass)
    async_setup_ui_settings_commands(hass)
    await _export(hass)
    await hass.async_block_till_done()
    world.events.clear()
    client = await hass_ws_client(hass)
    element = {"kind": "camera", "name": "Yard cam", "entity_id": "camera.yard", "options": {}}
    await client.send_json({"id": 1, "type": "portico/elements/set", "element": element, "base_rev": world.elements.rev,
                            "validate_only": True})
    assert (await client.receive_json())["success"]
    await _settle(hass)
    assert world.events == []
    await client.send_json({"id": 2, "type": "portico/elements/set", "element": element, "base_rev": world.elements.rev})
    created = await client.receive_json()
    assert created["success"]
    cid = created["result"]["element"]["id"]
    await _settle(hass)
    assert f"elements/{cid}.yaml" in _disk(root) and len(world.events) == 1
    await client.send_json({"id": 3, "type": "portico/ui_settings/set", "patch": {"ring_window": 60}, "confirm": True})
    assert (await client.receive_json())["success"]
    await _settle(hass)
    assert "window: 60" in _disk(root)["portico.yaml"] and len(world.events) == 2
    await client.send_json({"id": 4, "type": "portico/elements/delete", "element_id": cid, "base_rev": world.elements.rev})
    assert (await client.receive_json())["success"]
    await _settle(hass)
    assert f"elements/{cid}.yaml" not in _disk(root)


async def test_a_failed_write_never_fails_the_save_and_raises_an_issue_that_the_next_write_clears(world, hass, root, hass_ws_client):
    async_setup_elements_commands(hass)
    await _export(hass)
    client = await hass_ws_client(hass)
    element = {"kind": "camera", "name": "Yard cam", "entity_id": "camera.yard", "options": {}}
    with patch.object(config_sync, "write_tree", side_effect=OSError("disk full")):
        await client.send_json({"id": 1, "type": "portico/elements/set", "element": element, "base_rev": world.elements.rev})
        created = await client.receive_json()
        assert created["success"]                                  # the save succeeded
        cid = created["result"]["element"]["id"]
        await _settle(hass)
    assert cid in world.elements.elements_by_id()
    assert ir.async_get(hass).async_get_issue(DOMAIN, config_sync.ISSUE_NOT_WRITTEN) is not None
    config_sync.async_schedule(hass)
    await _settle(hass)
    assert ir.async_get(hass).async_get_issue(DOMAIN, config_sync.ISSUE_NOT_WRITTEN) is None
    assert f"elements/{cid}.yaml" in _disk(root)


# ---- the loop break ---------------------------------------------------------------------------------------------------------------

async def test_the_apply_never_writes_a_file_or_fires_the_event_and_adopted_files_are_not_pending(world, hass, root):
    await _export(hass)
    await hass.async_block_till_done()
    world.events.clear()
    files = _with_element(_edited(world), CAMERA)
    for path, text in files.items():                              # an agent edits the files by hand (formatting included)
        with open(os.path.join(root, path), "w", encoding="utf-8") as handle:
            handle.write(text)
    os.makedirs(os.path.join(root, "elements"), exist_ok=True)
    with open(os.path.join(root, "elements", "el-yard-cam.yaml"), "w", encoding="utf-8") as handle:
        handle.write(files["elements/el-yard-cam.yaml"])
    before = {p: (os.stat(os.path.join(root, p)).st_mtime_ns, t) for p, t in _disk(root).items()}
    done = await hass.services.async_call(DOMAIN, "reload_config", {}, blocking=True, return_response=True)
    await hass.async_block_till_done()
    assert done["ok"] and done["applied"]
    assert world.events == []                                      # the apply fires nothing
    after = {p: (os.stat(os.path.join(root, p)).st_mtime_ns, t) for p, t in _disk(root).items()}
    assert after == before                                         # and writes nothing
    assert CAMERA["id"] in world.elements.elements_by_id()
    # an editor save afterwards sees the adopted files as its own, not as pending edits
    await _camera(world, "Yard 2")
    config_sync.async_schedule(hass)
    await _settle(hass)
    assert world.events and world.events[0].data["pending"] == []


async def test_config_apply_source_does_not_write_files_or_fire_the_event():
    import inspect
    source = inspect.getsource(config_apply)
    assert "async_fire" not in source and "write_tree" not in source and "sync_files" not in source


# ---- pending hand edits, the switch, hygiene ----------------------------------------------------------------------------------------

async def test_a_hand_edited_file_is_left_alone_reported_and_cleared_by_a_reload(world, hass, root):
    await _export(hass)
    page = os.path.join(root, "pages", "home.yaml")
    with open(page, encoding="utf-8") as handle:
        edited = handle.read() + "# hand edit\n"
    with open(page, "w", encoding="utf-8") as handle:
        handle.write(edited)
    await hass.async_block_till_done()
    world.events.clear()
    await _camera(world)
    # make the live page differ so the save wants to rewrite it
    files, rev = config_sync.snapshot(hass)
    files["pages/home.yaml"] = files["pages/home.yaml"].replace("label: Home", "label: Home 2")
    with patch.object(config_sync, "_ensure_stores", return_value=False), patch.object(config_sync, "snapshot", return_value=(files, rev)):
        config_sync.async_schedule(hass)
        await _settle(hass)
    with open(page, encoding="utf-8") as handle:
        assert handle.read() == edited                              # left exactly as the author wrote it
    assert world.events[0].data["pending"] == ["pages/home.yaml"]
    assert ir.async_get(hass).async_get_issue(DOMAIN, config_sync.ISSUE_PENDING) is not None
    files2, errors = read_tree(root)
    await config_apply.reload_config(hass, files2, errors, dry_run=False)
    assert ir.async_get(hass).async_get_issue(DOMAIN, config_sync.ISSUE_PENDING) is None


async def test_the_option_switches_writing_off_and_no_directory_means_no_writing(world, hass, root):
    config_sync.async_schedule(hass)                                # no directory yet: nothing is created
    await _settle(hass)
    assert not os.path.exists(root) and world.events == []
    await _export(hass)
    await hass.async_block_till_done()
    world.events.clear()
    hass.config_entries.async_update_entry(world.settings, options={**world.settings.options, config_sync.OPTION: False})
    cid = await _camera(world)
    config_sync.async_schedule(hass)
    await _settle(hass)
    assert world.events == [] and f"elements/{cid}.yaml" not in _disk(root)


async def test_a_symlinked_root_or_file_is_never_written_through(world, hass, root, tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    os.symlink(target, root)
    reply = await _export(hass)
    assert reply["ok"] is False and reply["errors"][0]["code"] == "bad_root" and list(target.iterdir()) == []
    os.unlink(root)
    await _export(hass)
    victim = tmp_path / "victim.yaml"
    victim.write_text("keep\n")
    os.unlink(os.path.join(root, "pages", "home.yaml"))
    os.symlink(victim, os.path.join(root, "pages", "home.yaml"))
    result = config_sync.write_tree(root, _tree(world), {})
    assert {"path": "pages/home.yaml", "code": "symlink"} in result["errors"] and victim.read_text() == "keep\n"


async def test_paths_outside_the_layout_are_refused(world, tmp_path):
    result = config_sync.write_tree(str(tmp_path / "r"), {"../evil.yaml": "x", "/abs.yaml": "x", "a/../b.yaml": "x"}, {})
    assert [e["code"] for e in result["errors"]] == ["bad_path"] * 3 and not (tmp_path / "evil.yaml").exists()


# ---- DEV-824: a sync before the stores are loaded must never delete the files --------------------------------------------------------

def _forget_elements(world):
    """What an HA restart leaves before anything has loaded the elements store: loaded flag down, memory empty."""
    world.elements._loaded = False
    world.elements._elements = []


async def test_a_sync_before_the_elements_store_is_loaded_loads_it_and_keeps_the_element_files(world, hass, root):
    await _export(hass)
    await hass.async_block_till_done()
    before = _disk(root)
    assert any(p.startswith("elements/") for p in before)
    _forget_elements(world)
    config_sync.async_schedule(hass)
    await _settle(hass)
    assert _disk(root) == before and _disk(root) == _tree(world)


async def test_the_safety_net_refuses_to_empty_a_managed_directory_when_the_state_looks_unloaded(world, hass, root):
    await _export(hass)
    await hass.async_block_till_done()
    before = _disk(root)
    elements = [p for p in before if p.startswith("elements/")]
    files = {p: t for p, t in before.items() if not p.startswith("elements/")}
    known = {p: config_sync._sha(t) for p, t in before.items()}
    result = config_sync.write_tree(root, files, known)
    assert result["removed"] == [] and result["blocked"] == ["elements"]
    assert _disk(root) == before and elements
    # a directory legitimately emptied, with the state loaded, is removed as before
    assert config_sync.write_tree(root, files, known, state_loaded=True)["removed"] == sorted(elements)


async def test_the_safety_net_raises_an_issue_and_writes_nothing_through_the_sync(world, hass, root):
    await _export(hass)
    await hass.async_block_till_done()
    before = _disk(root)
    state = config_sync._state(hass)
    with patch.object(config_sync, "_ensure_stores", return_value=False), patch.object(config_sync, "snapshot", return_value=({p: t for p, t in before.items() if not p.startswith("elements/")}, "r")):
        await config_sync.sync_files(hass, source="editor")
    assert _disk(root) == before
    assert ir.async_get(hass).async_get_issue(DOMAIN, config_sync.ISSUE_NOT_WRITTEN) is not None and state.write_errors
