# ruff: noqa: F811 - test parameters named after the imported fixtures
"""The migrated storage, loaded by HA's real harness and the real unified integration (design section 6).

The offline script is run on the old-shape fixtures (`fixtures/unify/`, see `test_unify_migration.py`), and what it WROTE
is what HA loads: `core.config_entries` through `ConfigEntries.async_initialize()`, `core.entity_registry` through the
registry's own loader, the peer token through the private Store at its unchanged key. Then the domain is entered the way
HA enters it, `async_setup_component(hass, "portico")` (calling a migrator directly would miss an ordering problem), and
the test asserts that the door entry LOADS, that `sensor.call_state` and `sensor.listen_leg` are the same entities under
the same ids (no `_2`), that the transport is configured with the stored peer token, and that the old integration folder
is really gone (`IntegrationNotFound`). Only the Go hub at the process boundary is faked.

A negative control runs the same storage UNMIGRATED through the new code to show what the script is for: the door entry
is an orphan, and a door added again by hand takes `sensor.call_state_2`.

The migrated storage must be in `hass_storage` before the `hass` fixture builds the registries, so the fixture below is
requested before `hass`, and this module imports no autouse fixture (an autouse one would build `hass` first).
"""
from __future__ import annotations

import importlib.util
import json
import os
import time
from pathlib import Path

import pytest
from homeassistant.config_entries import ConfigEntryState

from homeassistant.helpers import entity_registry as er
from homeassistant.loader import IntegrationNotFound, async_get_integration
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .driver_part import fake_transport  # noqa: F401 - fixture by name

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "unify"
SCRIPT = Path(__file__).resolve().parent.parent.parent.parent / "scripts" / "migrate_unify.py"
DOOR_ID = "01M5A7DXY2YXMJYQ0HZ9FXNWSS"
PEER_KEY = f"portico.gate_sip_peer.{DOOR_ID}"
PEER_TOKEN = "FAKE-peer-token-0123456789"


def _script():
    spec = importlib.util.spec_from_file_location("migrate_unify", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _config_dir(root: Path) -> Path:
    storage = root / ".storage"
    storage.mkdir(parents=True)
    for name in ("core.config_entries", "core.entity_registry"):
        target = storage / name
        target.write_text((FIXTURES / f"{name}.old.json").read_text("utf-8"), encoding="utf-8")
        old = time.time() - 3600
        os.utime(target, (old, old))
    (storage / PEER_KEY).write_text(json.dumps({"version": 1, "minor_version": 1, "key": PEER_KEY,
                                                "data": {"peer_token": PEER_TOKEN}}, indent=2), encoding="utf-8")
    (storage / "portico_intercom").mkdir()
    return root


def _only_the_door_and_its_rows(entries_doc: dict, registry_doc: dict) -> None:
    """The push and phone entries need their own credentials and would fail their setup here; this test is about the
    door. Foreign rows are kept (the registry must hold them untouched)."""
    entries_doc["data"]["entries"] = [e for e in entries_doc["data"]["entries"] if e["entry_id"] == DOOR_ID]
    registry_doc["data"]["entities"] = [r for r in registry_doc["data"]["entities"] if r["config_entry_id"] == DOOR_ID]


def _hass_storage_from(hass_storage, config: Path) -> None:
    for name in ("core.config_entries", "core.entity_registry"):
        doc = json.loads((config / ".storage" / name).read_text("utf-8"))
        if name == "core.config_entries":
            registry = json.loads((config / ".storage" / "core.entity_registry").read_text("utf-8"))
            _only_the_door_and_its_rows(doc, registry)
        else:
            entries = json.loads((config / ".storage" / "core.config_entries").read_text("utf-8"))
            _only_the_door_and_its_rows(entries, doc)
        hass_storage[name] = doc
    hass_storage[PEER_KEY] = json.loads((config / ".storage" / PEER_KEY).read_text("utf-8"))


@pytest.fixture
def migrated(hass_storage, tmp_path):
    """The old fixtures, migrated by the real script, loaded into `hass_storage`. Request it BEFORE `hass`."""
    config = _config_dir(tmp_path / "config")
    backup = tmp_path / "backup"
    backup.mkdir()
    for name in ("core.config_entries", "core.entity_registry", PEER_KEY):
        (backup / name).write_bytes((config / ".storage" / name).read_bytes())
    (backup / "portico_intercom").mkdir()
    assert _script().main(["--config", str(config), "--backup-dir", str(backup)]) == 0
    _hass_storage_from(hass_storage, config)
    return config


@pytest.fixture
def unmigrated(hass_storage, tmp_path):
    config = _config_dir(tmp_path / "config")
    _hass_storage_from(hass_storage, config)
    return config


async def _load_the_entity_registry_from_storage(hass) -> None:
    """The harness builds the registries with a store that never reads (`StoreWithoutWriteLoad`), so a registry file put
    in `hass_storage` is ignored. Build the entity registry again, now that the patch is gone, with the real store
    class: it reads `hass_storage` the way HA reads `.storage/core.entity_registry` at start."""
    hass.data.pop(er.DATA_REGISTRY)
    await er.async_get(hass).async_load()
    assert er.async_get(hass).entities, "the migrated registry file was not read"


async def _prepare(hass, tmp_path, monkeypatch) -> None:
    """What a start does before the domain: http (the static paths), the config path in a temp dir (the hub's socket
    directory), then the two core stores read from storage."""
    from homeassistant.components.frontend import DATA_EXTRA_MODULE_URL

    monkeypatch.setattr(hass.config, "path", lambda *parts: str(tmp_path.joinpath(*parts)))
    assert await async_setup_component(hass, "http", {})
    hass.data[DATA_EXTRA_MODULE_URL] = set()
    await _load_the_entity_registry_from_storage(hass)
    await hass.config_entries.async_initialize()


async def _enter(hass, enable_custom_integrations, tmp_path, monkeypatch):
    """Everything a normal start does for this domain, nothing more, through HA's own entry points."""
    await _prepare(hass, tmp_path, monkeypatch)
    await _start(hass)


async def _start(hass) -> None:
    assert await async_setup_component(hass, "portico", {})
    await hass.async_block_till_done()


async def test_the_old_integration_is_missing_from_the_loader(migrated, hass, enable_custom_integrations):
    with pytest.raises(IntegrationNotFound):
        await async_get_integration(hass, "portico_intercom")


async def test_the_migrated_door_loads_with_the_same_entities_and_ids_and_no_underscore_2(
        migrated, hass, enable_custom_integrations, fake_transport, tmp_path, monkeypatch):
    await _prepare(hass, tmp_path, monkeypatch)
    registry = er.async_get(hass)
    before = {row.unique_id: (row.entity_id, row.id) for row in registry.entities.values()}
    assert sorted(before) == [f"{DOOR_ID}_call_state", f"{DOOR_ID}_listen_leg"]
    assert [before[u][0] for u in sorted(before)] == ["sensor.call_state", "sensor.listen_leg"]

    await _start(hass)
    registry = er.async_get(hass)

    (door,) = hass.config_entries.async_entries("portico")
    assert door.entry_id == DOOR_ID and door.state is ConfigEntryState.LOADED
    assert door.data["entry_type"] == "door" and door.data["driver"] == "gate_sip_observer"

    after = {row.unique_id: (row.entity_id, row.id) for row in registry.entities.values()}
    assert after == before                                              # same entities: same ids, same registry row ids
    assert {row.platform for row in registry.entities.values()} == {"portico"}
    assert hass.states.get("sensor.call_state").state == "idle"
    # the listen-leg sensor of the retired observer is no longer created; its registry row stays (orphaned), not renumbered
    assert not [e for e in hass.states.async_entity_ids("sensor") if e.endswith("_2")]
    assert len(registry.entities) == 2


async def test_the_transport_is_configured_from_the_migrated_entry_and_the_stored_peer_token(
        migrated, hass, enable_custom_integrations, fake_transport, tmp_path, monkeypatch):
    await _enter(hass, enable_custom_integrations, tmp_path, monkeypatch)
    ((driver, config),) = fake_transport.instances[-1].configured
    assert driver == "gate_sip_observer"
    assert "peer_token" not in config and "listen_enabled" not in config               # the listen leg is gone (DEV-518 C5)
    assert config["device_id"] == "http://gate-sip.example:8091"
    assert "entry_type" not in config and "gate_sip_listen" not in config               # Python's own keys stay home
    provider = hass.data["portico_intercom"]["provider"]
    (door,) = provider.doors()
    assert door["id"] == "http://gate-sip.example:8091" and door["provider"] == "portico_intercom"


async def test_a_ring_reaches_the_same_sensor(migrated, hass, enable_custom_integrations, fake_transport, tmp_path,
                                              monkeypatch):
    from pytest_homeassistant_custom_component.common import async_capture_events

    await _enter(hass, enable_custom_integrations, tmp_path, monkeypatch)
    events = async_capture_events(hass, "portico_intercom_call")
    go = fake_transport.instances[-1]
    go.ring("gso-9", device_id="http://gate-sip.example:8091")
    await hass.async_block_till_done()
    assert hass.states.get("sensor.call_state").state == "ringing"
    assert [(e.data["state"], e.data["call_id"]) for e in events] == [("ringing", "gso-9")]


async def test_unmigrated_storage_leaves_an_orphan_and_a_door_added_again_takes_underscore_2(
        unmigrated, hass, enable_custom_integrations, fake_transport, tmp_path, monkeypatch):
    """The negative control: what the script prevents."""
    await _enter(hass, enable_custom_integrations, tmp_path, monkeypatch)
    (orphan,) = hass.config_entries.async_entries("portico_intercom")
    assert orphan.entry_id == DOOR_ID and orphan.state is not ConfigEntryState.LOADED
    assert hass.states.get("sensor.call_state") is None or hass.states.get("sensor.call_state").state != "idle"

    again = MockConfigEntry(
        domain="portico", unique_id="gate_sip_observer:http://gate-sip.example:8091-again",
        data={"entry_type": "door", "driver": "gate_sip_observer", "gate_sip_url": "http://gate-sip.example:8091",
              "interval": 1.0, "device_id": "http://gate-sip.example:8091-again",
              "existing_camera_entity": "camera.gate_intercom", "open_relay_action": []})
    again.add_to_hass(hass)
    assert await hass.config_entries.async_setup(again.entry_id)
    await hass.async_block_till_done()
    ids = sorted(e.entity_id for e in er.async_entries_for_config_entry(er.async_get(hass), again.entry_id))
    assert ids == ["sensor.call_state_2"]
