"""DEV-824 P5: `portico/config/status`, what the editor's banner reads (config_service.py, config_sync.status, config_apply.record_reload).

HA-harness session. Pinned: admin only and read-only; the file configuration off, or no directory, is a plain state (not an error);
a hand edit and a hand-made file are pending until a reload adopts them; a failed write is reported until the next good write; the last
real reload's result (errors as file:line:col, capped; deferred; applied) is remembered in memory, and a dry run does not replace it.
"""
from __future__ import annotations

import os

import pytest
from unittest.mock import patch

from .. import config_apply, config_sync
from ..config_flow import DOMAIN
from ..config_service import async_setup_config_service, read_tree
from ..editor_commands import async_setup_editor_commands
from ..elements_commands import async_setup_elements_commands
from .test_config_apply import world  # noqa: F401 - fixture by name
from .test_push_registry import _custom_integrations  # noqa: F401 - autouse fixture by name
from .test_config_sync import _export, _settle

pytestmark = pytest.mark.asyncio


@pytest.fixture
def root(world, hass):
    async_setup_config_service(hass)
    async_setup_editor_commands(hass)
    config_sync.async_setup_config_sync(hass)
    return hass.config.path("portico")


async def _status(hass, hass_ws_client):
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "portico/config/status"})
    reply = await client.receive_json()
    assert reply["success"], reply
    return reply["result"]


async def _reload(hass, **data):
    return await hass.services.async_call(DOMAIN, "reload_config", data, blocking=True, return_response=True)


async def test_no_directory_is_a_quiet_state_and_nothing_is_created(world, hass, root, hass_ws_client):
    result = await _status(hass, hass_ws_client)
    assert result == {"enabled": True, "dir_exists": False, "pending": [], "pending_total": 0, "last_write_error": None, "last_reload": None}
    assert not os.path.exists(root)


async def test_the_option_off_is_reported_and_no_file_is_read(world, hass, root, hass_ws_client):
    await _export(hass)
    hass.config_entries.async_update_entry(world.settings, options={**world.settings.options, config_sync.OPTION: False})
    result = await _status(hass, hass_ws_client)
    assert result["enabled"] is False and result["dir_exists"] is True and result["pending"] == []


async def test_a_clean_tree_has_nothing_pending_and_a_hand_edit_is_pending_until_a_reload(world, hass, root, hass_ws_client):
    await _export(hass)
    await hass.async_block_till_done()
    assert (await _status(hass, hass_ws_client))["pending"] == []
    page = os.path.join(root, "pages", "home.yaml")
    with open(page, "a", encoding="utf-8") as handle:
        handle.write("# hand edit\n")
    with open(os.path.join(root, "pages", "made-by-hand.yaml"), "w", encoding="utf-8") as handle:
        handle.write("id: made-by-hand\n")
    with open(os.path.join(root, "README.md"), "w", encoding="utf-8") as handle:
        handle.write("not a layout file")
    result = await _status(hass, hass_ws_client)
    assert result["pending"] == ["pages/home.yaml", "pages/made-by-hand.yaml"] and result["pending_total"] == 2
    os.unlink(os.path.join(root, "pages", "made-by-hand.yaml"))        # an invalid hand-made file would refuse the reload
    files, errors = read_tree(root)
    await config_apply.reload_config(hass, files, errors, dry_run=False)
    assert (await _status(hass, hass_ws_client))["pending"] == []


async def test_the_pending_list_is_capped_but_the_total_is_exact(world, hass, root, hass_ws_client):
    await _export(hass)
    for n in range(config_sync.STATUS_CAP + 5):
        with open(os.path.join(root, "pages", f"p{n:03}.yaml"), "w", encoding="utf-8") as handle:
            handle.write("x: 1\n")
    result = await _status(hass, hass_ws_client)
    assert len(result["pending"]) == config_sync.STATUS_CAP and result["pending_total"] == config_sync.STATUS_CAP + 5


async def test_a_failed_write_is_reported_until_the_next_good_write(world, hass, root, hass_ws_client):
    async_setup_elements_commands(hass)
    await _export(hass)
    with patch.object(config_sync, "write_tree", side_effect=OSError("disk full")):
        config_sync.async_schedule(hass)
        await _settle(hass)
    failed = (await _status(hass, hass_ws_client))["last_write_error"]
    assert failed == {"count": 1, "errors": [{"path": "", "code": "write"}]}
    config_sync.async_schedule(hass)
    await _settle(hass)
    assert (await _status(hass, hass_ws_client))["last_write_error"] is None


async def test_the_last_reload_is_remembered_with_capped_errors_and_a_dry_run_does_not_replace_it(world, hass, root, hass_ws_client):
    await _export(hass)
    await hass.async_block_till_done()
    assert (await _status(hass, hass_ws_client))["last_reload"] is None
    ok = await _reload(hass)
    assert ok["ok"]
    seen = (await _status(hass, hass_ws_client))["last_reload"]
    assert seen["ok"] is True and seen["errors"] == [] and seen["deferred"] is False and seen["at"].endswith("+00:00")
    for n in range(config_apply.REPORT_ERRORS_CAP + 3):
        with open(os.path.join(root, "elements", f"bad{n:03}.yaml"), "w", encoding="utf-8") as handle:
            handle.write("kind: [unclosed\n")
    bad = await _reload(hass)
    assert not bad["ok"]
    seen = (await _status(hass, hass_ws_client))["last_reload"]
    assert seen["ok"] is False and seen["applied"] is False
    assert len(seen["errors"]) == config_apply.REPORT_ERRORS_CAP and seen["error_total"] >= config_apply.REPORT_ERRORS_CAP + 3
    assert set(seen["errors"][0]) == {"file", "line", "col", "message"} and seen["errors"][0]["file"].startswith("elements/bad")
    await _reload(hass, dry_run=True)
    assert (await _status(hass, hass_ws_client))["last_reload"]["at"] == seen["at"]


async def test_a_deferred_reload_is_reported_as_deferred():
    class _Hass:
        data: dict = {}
    hass = _Hass()
    config_apply.record_reload(hass, {"ok": True, "applied": False, "deferred": True, "dry_run": False, "errors": []})
    last = config_apply.last_reload(hass)
    assert last["ok"] is True and last["deferred"] is True and last["applied"] is False


async def test_a_missing_directory_refusal_is_the_last_reload_too(world, hass, root, hass_ws_client):
    await _reload(hass)
    seen = (await _status(hass, hass_ws_client))["last_reload"]
    assert seen["ok"] is False and seen["errors"][0]["message"].startswith("there is no portico directory")
