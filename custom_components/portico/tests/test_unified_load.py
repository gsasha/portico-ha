# ruff: noqa: F811 - test parameters named after the imported fixtures
"""The one integration with its three entry kinds (S-2, STRUCT-07, PROV-01, STATE-04, Q4, Q5; the fold, DEV-686).

`fixtures/core.config_entries.json` is a redacted copy of the SHAPE of the box's real `core.config_entries` as it was
BEFORE the first split (its header says what is real and what is fake): an OLD-FORMAT observer entry (`data.driver`,
no `entry_type`), a disabled old-format `dahua_sip` entry with its phone rows and Tailscale fields, and the push
entry. Loading it checks what a restart does to an old-format door entry in the unified integration: it is refused
with one line and swept on removal, exactly as before the fold, and never taken for a door. The door of today (an
`entry_type: "door"` entry) is loaded in `test_door_and_push_entries_load_in_one_integration` and in the migration
tests (`test_unify_migration_integration.py`).
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.portico as ui_package
from custom_components.portico import tailscale_approve
from custom_components.portico.config_flow import DOMAIN
from custom_components.portico.provider import API_VERSION, DATA_PROVIDERS
from custom_components.portico.push import const

from .driver_part import DRIVER_DOMAIN, GATE_SIP_URL, _observer_entry, fake_transport  # noqa: F401
from .fake_google import service_account_info
from .test_push_registry import _custom_integrations, _setup_all, google  # noqa: F401 - fixtures by name

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "core.config_entries.json"
OBSERVER_ID = "01JSPLITFX0BSERVER00000001"
DAHUA_ID = "01JSPLITFX0DAHUASIP00000002"
PUSH_ID = "01JSPLITFX0PUSHENTRY0000003"
SECRET = "tskey-app-kFAKEclientCNTRL-SENTINELSECRETxyz0123456789"


def _fixture_entries(hass) -> dict[str, MockConfigEntry]:
    """The fixture's portico entries, added to hass as the box has them."""
    out = {}
    for raw in json.loads(FIXTURE.read_text(encoding="utf-8"))["data"]["entries"]:
        entry = MockConfigEntry(
            domain=raw["domain"], entry_id=raw["entry_id"], title=raw["title"], unique_id=raw["unique_id"],
            data=raw["data"], options=raw["options"], version=raw["version"], minor_version=raw["minor_version"],
            source=raw["source"], disabled_by=ConfigEntryDisabler(raw["disabled_by"]) if raw["disabled_by"] else None)
        entry.add_to_hass(hass)
        out[raw["entry_id"]] = entry
    return out


def _credentials(hass_storage, google) -> None:
    hass_storage[const.CREDENTIALS_STORE_KEY] = {
        "version": 1, "minor_version": 1, "key": const.CREDENTIALS_STORE_KEY,
        "data": service_account_info(google.token_uri)}


def _oauth_store(hass_storage, entry_id: str, secret: str = SECRET) -> str:
    key = f"{tailscale_approve.STORE_KEY_PREFIX}{entry_id}"
    hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": {"client_secret": secret}}
    return key


def _peer_token_store(hass_storage, entry_id: str) -> str:
    key = f"portico.gate_sip_peer.{entry_id}"
    hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": {"peer_token": "FAKE-peer-token-123"}}
    return key


# ---- one integration: a door and the push entry, in either order ----------------------------------------------


@pytest.mark.parametrize("push_first", [False, True], ids=["door_first", "push_first"])
async def test_door_and_push_entries_load_in_one_integration(hass, hass_storage, hass_ws_client, google,
                                                              fake_transport, push_first):
    door, push = await _setup_all(hass, hass_storage, google, push_first=push_first)
    assert door.domain == push.domain == DOMAIN
    assert door.data["entry_type"] == "door" and push.data["entry_type"] == "push"
    assert door.state is ConfigEntryState.LOADED and push.state is ConfigEntryState.LOADED
    assert DOMAIN in hass.config.components and DRIVER_DOMAIN not in hass.config.components

    registry = hass.data[DOMAIN][DATA_PROVIDERS]
    provider = registry.provider(DRIVER_DOMAIN)
    assert provider is not None and provider.API_VERSION == API_VERSION == 1
    assert provider is hass.data[DRIVER_DOMAIN]["provider"]           # the object async_setup made, not a second one
    assert [d.id for d in registry.doors()] == [GATE_SIP_URL]

    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "portico/version"})
    result = (await client.receive_json())["result"]
    assert result["providers"] == [{"domain": DRIVER_DOMAIN, "api_version": 1}]      # `provider` ids stay (the door id)


async def test_a_door_gets_the_door_sensors_not_the_away_ring_sensor(hass, hass_storage, google, fake_transport):
    """The sensor platform dispatches a door BEFORE the push ring notification fall-through (design 1.2): the door has
    `sensor.call_state` and no `sensor.portico_push_ring_notification` of its own."""
    door, push = await _setup_all(hass, hass_storage, google, push_first=False)
    entities = er.async_entries_for_config_entry(er.async_get(hass), door.entry_id)
    assert sorted(e.unique_id for e in entities) == [f"{door.entry_id}_call_state"]
    assert hass.states.get("sensor.call_state") is not None
    away = er.async_entries_for_config_entry(er.async_get(hass), push.entry_id)
    # DEV-811 P4: the settings entry also hosts the house-wide call entities (`sensor.portico_calls`, `binary_sensor.portico_call_active`)
    assert sorted(e.unique_id for e in away if e.domain == "sensor") == sorted(
        [f"{push.entry_id}_away_ring", f"{push.entry_id}_calls", f"{push.entry_id}_live_sessions"])   # DEV-805
    assert [e.unique_id for e in away if e.domain == "binary_sensor"] == [f"{push.entry_id}_call_active"]


async def test_no_scan_of_the_domains_entries_takes_a_door_for_a_phone_host_or_a_donor(hass, hass_storage, google,
                                                                                        fake_transport):
    """A door entry carries `data.driver`: every scan over the domain's entries must skip it (design 1.2)."""
    from custom_components.portico import enrollment_adopt, house, phone_entry

    door, push = await _setup_all(hass, hass_storage, google, push_first=False)
    assert door.data["driver"] == "gate_sip_observer"
    assert enrollment_adopt._door_entries(hass) == []                      # not a donor of enrollment state
    assert [e for e, _p in phone_entry.phone_rows(hass)] == []             # not a host of legacy phone rows
    assert phone_entry.phone_entries(hass) == []
    assert house.house_option_on(hass) is True                            # always on (D-20261122-aafb); a door entry changes nothing
    hass.config_entries.async_update_entry(door, data={**door.data, "enrolled_phones": [
        {"ha_user_id": "u1", "name": "On a door", "enroll_id": "e1"}]})
    assert [e for e, _p in phone_entry.phone_rows(hass)] == []             # even a stray row on a door is not read


# ---- the three real entry shapes ---------------------------------------------------------------------------


async def test_the_two_old_driver_entries_are_refused_with_one_line_each_and_the_push_entry_serves(
        hass, hass_storage, hass_ws_client, google, monkeypatch, caplog):
    """A restart with `custom_components/portico` the UI part only: the observer entry (`data.driver`) is refused
    with one ERROR line, the disabled `dahua_sip` entry is not set up at all (and would be refused the same way),
    the push entry serves, and no process is spawned."""
    spawned = []

    async def _no_process(*args, **kwargs):
        spawned.append(args)
        raise AssertionError("the UI part must never spawn a process")

    monkeypatch.setattr("asyncio.create_subprocess_exec", _no_process)
    entries = _fixture_entries(hass)
    _credentials(hass_storage, google)
    caplog.set_level(logging.ERROR, logger="custom_components.portico")
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()

    observer, dahua, push = entries[OBSERVER_ID], entries[DAHUA_ID], entries[PUSH_ID]
    assert observer.state is ConfigEntryState.SETUP_ERROR
    assert dahua.state is ConfigEntryState.NOT_LOADED and dahua.disabled_by is ConfigEntryDisabler.USER
    assert push.state is ConfigEntryState.LOADED
    ours = [r.getMessage() for r in caplog.records if r.name.startswith("custom_components.portico")
            and r.levelno >= logging.ERROR]
    refusals = [m for m in ours if OBSERVER_ID in m]
    assert refusals == [
        f'Portico: entry "Portico (gate_sip_observer)" ({OBSERVER_ID}) is an old-format door entry (it has a driver '
        "but no entry kind). Delete it and add the door under Settings > Add integration > Portico > Add a door"]
    assert not [m for m in ours if "FAKE-" in m or SECRET in m]                # no secret in any line

    # the disabled one, refused by the same code if it were enabled: one line, False, nothing started
    caplog.clear()
    assert await ui_package.async_setup_entry(hass, dahua) is False
    (line,) = [r.getMessage() for r in caplog.records if DAHUA_ID in r.getMessage()]
    assert "old-format door entry" in line and "Add a door" in line

    # the push ring notification serves: its sensor, the websocket, and nothing was spawned
    assert hass.states.get("sensor.portico_push_ring_notification").state == "ok"
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "portico/version"})
    assert (await client.receive_json())["result"] == {
        "contract": 3, "ui_version": 1, "providers": [{"domain": DRIVER_DOMAIN, "api_version": 1}]}
    assert spawned == []


async def test_adopt_runs_against_the_real_entry_shapes(hass, hass_storage, google, caplog):
    """S-1's adoption on the fixture: the push entry ends with the observer's Tailscale answer and ha_dial, the
    OAuth client secret copied under its own id (readable by the new code), one INFO line, no secret in the log."""
    entries = _fixture_entries(hass)
    _credentials(hass_storage, google)
    _oauth_store(hass_storage, OBSERVER_ID)
    caplog.set_level(logging.INFO, logger="custom_components.portico")
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    push = entries[PUSH_ID]
    assert push.state is ConfigEntryState.LOADED

    assert push.data["tailscale_mode"] == "approve"
    assert push.data["tailscale_client_id"] == "kFAKEclientCNTRL"
    assert push.data["tailscale_redirect_uri"].endswith(tailscale_approve.CALLBACK_PATH)
    assert push.data["ha_dial"] == "10.6.0.5:443"
    assert "enrolled_phones" not in push.data                      # MIGR-01: phones re-enroll, they are not copied
    assert await tailscale_approve.OAuthAppStore(hass, PUSH_ID).async_load_secret() == SECRET
    assert await tailscale_approve.OAuthAppStore(hass, OBSERVER_ID).async_load_secret() == SECRET   # source kept

    from custom_components.portico.enrollment_adopt import oauth_copy_is_readable

    assert await oauth_copy_is_readable(hass) is True
    lines = [r.getMessage() for r in caplog.records if "adopted enrollment settings" in r.getMessage()]
    assert len(lines) == 1 and "oauth_app_secret" in lines[0] and SECRET not in caplog.text

    # the M5.3b gate's third check: "Enroll a device" opens the PHONE form, not the Tailscale question
    result = await hass.config_entries.options.async_init(PUSH_ID)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "enroll_phone"})
    assert result["step_id"] == "phone_details"


# ---- STRUCT-05, STRUCT-07: the UI part alone ----------------------------------------------------------------


async def test_ui_part_loads_without_driver_part_and_registers_panel_and_card(hass, hass_client, hass_ws_client,
                                                                              monkeypatch):
    """No door configured: the integration loads, the card and the panel are registered, enrollment and content
    answer, and every door command names an unknown door (the built-in door provider is always registered, so the
    `not_available` of "no provider at all" cannot happen)."""
    registered = AsyncMock()
    monkeypatch.setattr("homeassistant.components.panel_custom.async_register_panel", registered)
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()

    from homeassistant.components.frontend import DATA_EXTRA_MODULE_URL

    card_js = Path(ui_package.__file__).parent / "www" / "portico-card.js"
    card_hash = hashlib.sha256(card_js.read_bytes()).hexdigest()[:12]
    assert f"{ui_package._CARD_URL_PATH}?v={card_hash}" in hass.data[DATA_EXTRA_MODULE_URL]
    registered.assert_awaited_once()
    assert registered.await_args.kwargs["frontend_url_path"] == "portico"
    assert registered.await_args.kwargs["webcomponent_name"] == "portico-panel"
    # the websocket client first: HA 2026.10 refuses to set websocket_api up once hass_client() has frozen the router
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "portico/doors"})
    assert (await ws.receive_json())["result"] == {"doors": []}
    await ws.send_json({"id": 2, "type": "portico/dial", "door_id": "anything"})
    assert (await ws.receive_json())["error"]["code"] == "unknown_door"
    await ws.send_json({"id": 3, "type": "portico/content/document"})
    assert (await ws.receive_json())["success"]                  # the editor's data answers with no door at all

    client = await hass_client()
    for path, marker in ((ui_package._CARD_URL_PATH, "portico-card"), (ui_package._PANEL_URL_PATH, "portico-panel")):
        resp = await client.get(path)
        assert resp.status == 200 and marker in await resp.text()


# ---- Q4: the legacy sweep and its OAuth-secret backstop ----------------------------------------------------


async def _remove_refused_observer(hass, hass_storage, google, monkeypatch, *, push_has_copy: bool):
    entries = _fixture_entries(hass)
    _credentials(hass_storage, google)
    observer_store = _oauth_store(hass_storage, OBSERVER_ID)
    peer_store = _peer_token_store(hass_storage, OBSERVER_ID)
    # make the push entry hold no copy unless the test says so: the adoption would make one at its setup, so the
    # observer's answer is stripped of what the adopt needs (the adopt then has nothing to copy from)
    if push_has_copy:
        _oauth_store(hass_storage, PUSH_ID, secret="tskey-app-kFAKEclientCNTRL-ANOTHERCOPYxyz0123456789")
    else:
        for entry_id in (OBSERVER_ID, DAHUA_ID):        # no complete answer anywhere: nothing to adopt
            hass.config_entries.async_update_entry(entries[entry_id], data={
                k: v for k, v in entries[entry_id].data.items() if not k.startswith("tailscale_")})
    removed = []
    real = ui_package.async_remove_entry

    async def spy(hass_, entry):
        removed.append(entry.entry_id)
        await real(hass_, entry)

    monkeypatch.setattr(ui_package, "async_remove_entry", spy)
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    observer = entries[OBSERVER_ID]
    assert observer.state is ConfigEntryState.SETUP_ERROR
    assert peer_store in hass_storage and observer_store in hass_storage
    await hass.config_entries.async_remove(OBSERVER_ID)
    await hass.async_block_till_done()
    return removed, observer_store, peer_store


async def test_removing_a_refused_door_entry_keeps_the_oauth_store_when_the_push_entry_has_none(
        hass, hass_storage, google, monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="custom_components.portico")
    removed, observer_store, peer_store = await _remove_refused_observer(
        hass, hass_storage, google, monkeypatch, push_has_copy=False)
    # HA DOES call async_remove_entry for an entry whose setup returned False (proved here, not assumed): the entry
    # was in SETUP_ERROR when the removal began (the helper asserts it) and the spy saw the call
    assert removed == [OBSERVER_ID]
    assert peer_store not in hass_storage                    # the peer-token store always goes
    assert observer_store in hass_storage                    # the OAuth secret stays: no copy under the push entry
    assert (await tailscale_approve.OAuthAppStore(hass, OBSERVER_ID).async_load_secret()) == SECRET
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and "kept" in r.getMessage()]
    assert warnings == [f"Portico: kept .storage/{observer_store}: the push entry holds no copy of the Tailscale "
                        "OAuth-app secret yet"]
    assert SECRET not in caplog.text


async def test_removing_a_refused_door_entry_removes_the_oauth_store_when_the_push_entry_has_a_copy(
        hass, hass_storage, google, monkeypatch):
    removed, observer_store, peer_store = await _remove_refused_observer(
        hass, hass_storage, google, monkeypatch, push_has_copy=True)
    assert removed == [OBSERVER_ID]
    assert peer_store not in hass_storage and observer_store not in hass_storage
    assert f"{tailscale_approve.STORE_KEY_PREFIX}{PUSH_ID}" in hass_storage      # the push entry's copy is untouched


async def test_removing_the_push_entry_does_not_run_the_legacy_sweep(hass, hass_storage, google):
    """The sweep is for `data.driver` entries only: deleting the push entry removes its own stores and touches
    nothing else (here, the observer's OAuth store survives)."""
    entries = _fixture_entries(hass)
    _credentials(hass_storage, google)
    observer_store = _oauth_store(hass_storage, OBSERVER_ID)
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    await hass.config_entries.async_remove(PUSH_ID)
    await hass.async_block_till_done()
    assert observer_store in hass_storage and entries[PUSH_ID] not in hass.config_entries.async_entries(DOMAIN)


# ---- the door entry's Configure; STATE-04 --------------------------------------------------------------------


async def test_a_door_entry_options_flow_aborts_with_door_entry_moved(hass):
    entries = _fixture_entries(hass)
    for entry_id in (OBSERVER_ID, DAHUA_ID):
        result = await hass.config_entries.options.async_init(entry_id)
        assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "door_entry_moved")
        assert entries[entry_id].options == dict(entries[entry_id].options)           # nothing was written


async def test_away_ring_unique_id_unchanged(hass, hass_storage, google):
    """STATE-04: the push ring notification sensor keeps its entity id and its unique id (`<push entry id>_away_ring`) across
    the split, so history and dashboards keep following it."""
    _fixture_entries(hass)
    _credentials(hass_storage, google)
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    entity = er.async_get(hass).async_get("sensor.portico_push_ring_notification")
    assert entity is not None and entity.unique_id == f"{PUSH_ID}_away_ring" and entity.platform == DOMAIN
    assert entity.config_entry_id == PUSH_ID


def _sha12(name: str) -> str:
    return hashlib.sha256((Path(ui_package.__file__).parent / "www" / name).read_bytes()).hexdigest()[:12]


# ---- the JS URL token is a content hash (setup-level) -----------------------------------------------------


async def test_setup_registers_the_hash_urls_not_the_manifest_version(hass, monkeypatch):
    """The card URL and the panel URL each carry their OWN file's hash; the manifest version appears in neither."""
    from unittest.mock import AsyncMock

    from homeassistant.components.frontend import DATA_EXTRA_MODULE_URL

    hass.data[DATA_EXTRA_MODULE_URL] = set()
    registered = AsyncMock()
    monkeypatch.setattr("homeassistant.components.panel_custom.async_register_panel", registered)
    monkeypatch.setattr(ui_package, "_VERSION", "0.0.0-manifest")
    assert await async_setup_component(hass, "http", {})
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()

    card_url = f"/portico/portico-card.js?v={_sha12('portico-card.js')}"
    panel_url = f"/portico/portico-panel.js?v={_sha12('portico-panel.js')}"
    assert hass.data[DATA_EXTRA_MODULE_URL] == {card_url}
    assert registered.await_args.kwargs["module_url"] == panel_url
    assert "0.0.0-manifest" not in card_url + panel_url
    assert card_url.split("?v=")[1] != panel_url.split("?v=")[1]      # independent tokens


async def test_the_files_are_hashed_off_the_event_loop(hass, monkeypatch):
    """The blocking read runs in the executor, once per file, never inline on the loop."""
    import threading
    from unittest.mock import AsyncMock

    from homeassistant.components.frontend import DATA_EXTRA_MODULE_URL

    hass.data[DATA_EXTRA_MODULE_URL] = set()
    monkeypatch.setattr("homeassistant.components.panel_custom.async_register_panel", AsyncMock())
    loop_thread = threading.get_ident()
    seen = []
    real_token = ui_package._content_token

    def guarded(path):
        assert threading.get_ident() != loop_thread, "the file was read on the event loop"
        seen.append(path.rsplit("/", 1)[1])
        return real_token(path)

    monkeypatch.setattr(ui_package, "_content_token", guarded)
    assert await async_setup_component(hass, "http", {})
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    assert sorted(seen) == ["portico-card.js", "portico-elements-render.js", "portico-elements.js", "portico-panel.js", "portico-phones.js", "portico-settings.js"]      # P2, P4, E3: the tabs' modules too


# ---- DEV-704: why the panel needed a hard reload after an update -----------------------------------------------------
# Every registered URL already carries its own file's hash (tests above), so a restart that loads new code hands the
# browser new URLs. What was missing: the files were sent with NO Cache-Control, so a browser could reuse a copy for
# days from its Last-Modified age WITHOUT asking, under a URL that no longer matched the file on disk (a deploy that
# lands after the hash was taken). Now every script is `no-cache`: stored, but revalidated (ETag, 304) on each use.

_SCRIPTS = ("portico-card.js", "portico-panel.js", "portico-phones.js", "portico-settings.js", "portico-elements-render.js", "portico-elements.js")


async def test_every_script_is_sent_no_cache_and_revalidates_with_a_304(hass, hass_client, monkeypatch):
    from homeassistant.components.frontend import DATA_EXTRA_MODULE_URL

    hass.data[DATA_EXTRA_MODULE_URL] = set()
    monkeypatch.setattr("homeassistant.components.panel_custom.async_register_panel", AsyncMock())
    assert await async_setup_component(hass, "http", {})
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()

    client = await hass_client()
    for name in _SCRIPTS:
        for url in (f"/portico/{name}", f"/portico/{name}?v={_sha12(name)}"):
            resp = await client.get(url)
            assert resp.status == 200, url
            assert resp.headers["Cache-Control"] == "no-cache", url
            etag = resp.headers["ETag"]
            assert resp.headers["Last-Modified"]
            assert await resp.read() == (Path(ui_package.__file__).parent / "www" / name).read_bytes()
            again = await client.get(url, headers={"If-None-Match": etag})
            assert again.status == 304, url                  # unchanged: a header-only answer, not the file again


async def test_a_restart_over_changed_files_registers_a_new_url_for_every_module(hass, monkeypatch, tmp_path):
    """A restart is a fresh setup over the files then on disk: the card, the panel, and the Phones and Settings modules
    each get the hash of THEIR file, and changing one file moves that URL and no other."""
    import shutil
    from unittest.mock import MagicMock

    from homeassistant.components.frontend import DATA_EXTRA_MODULE_URL

    real_www = Path(ui_package.__file__).parent / "www"
    www = tmp_path / "www"
    shutil.copytree(real_www, www, ignore=shutil.ignore_patterns("tests"))
    monkeypatch.setattr(ui_package, "__file__", str(tmp_path / "__init__.py"))

    def sha12(name):
        return hashlib.sha256((www / name).read_bytes()).hexdigest()[:12]

    def urls(registered, extra):
        kwargs = registered.await_args.kwargs
        (card,) = extra
        return {"portico-card.js": card, "portico-panel.js": kwargs["module_url"],
                "portico-phones.js": kwargs["config"]["phones_module"],
                "portico-settings.js": kwargs["config"]["settings_module"],
                "portico-elements-render.js": kwargs["config"]["elements_render_module"],
                "portico-elements.js": kwargs["config"]["elements_module"]}

    hass.data[DATA_EXTRA_MODULE_URL] = set()
    registered = AsyncMock()
    monkeypatch.setattr("homeassistant.components.panel_custom.async_register_panel", registered)
    assert await async_setup_component(hass, "http", {})
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    first = urls(registered, hass.data[DATA_EXTRA_MODULE_URL])
    assert first == {name: f"/portico/{name}?v={sha12(name)}" for name in _SCRIPTS}

    # the "restart": new bytes on disk, the panel registered again (the routes of the first run are another process's)
    for name in ("portico-panel.js", "portico-phones.js", "portico-settings.js", "portico-elements-render.js", "portico-elements.js"):
        (www / name).write_bytes((www / name).read_bytes() + b"\n// changed\n")
        hass.http = MagicMock()
        await ui_package._async_register_panel(hass)
        second = urls(registered, hass.data[DATA_EXTRA_MODULE_URL])
        assert second[name] == f"/portico/{name}?v={sha12(name)}" and second[name] != first[name]
        for other in _SCRIPTS:
            if other != name:
                assert second[other] == first[other]          # a file untouched since the last restart keeps its URL
        first = second
