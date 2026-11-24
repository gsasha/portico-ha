"""Entry setup/teardown lifecycle — three bugs that stopped the live entry on
2026-10-03 and that no earlier test could see. Plain pytest (no HA harness),
run in its own session like test_transport.py."""

import importlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import custom_components.portico.door as portico
from homeassistant.const import EVENT_HOMEASSISTANT_STOP


@pytest.mark.parametrize("platform", portico.PLATFORMS)
def test_every_platform_is_importable_from_the_integration_root(platform):
    """HA imports custom_components.portico.<platform>; the door implementations
    live in door/entities/, and the entry failed to set up with
    "No module named 'custom_components.portico_intercom.camera'" (the same failure, before the fold)."""
    module = importlib.import_module(f"custom_components.portico.{platform}")
    assert callable(module.async_setup_entry)


class _FakeRuntime:
    instances: list = []

    def __init__(self, hass, entry, hub):
        self.stopped = 0
        self.fail_start = False
        self.ring_only = False      # door/__init__.py reads it to decide on the talk-options update listener
        _FakeRuntime.instances.append(self)

    async def async_start(self):
        pass

    async def async_stop(self):
        self.stopped += 1


def _hass(forward_error=None):
    return SimpleNamespace(
        config_entries=SimpleNamespace(
            async_forward_entry_setups=AsyncMock(side_effect=forward_error)
        ),
        bus=MagicMock(),
        data={},
    )


async def test_failed_setup_stops_the_transport(monkeypatch):
    """A failed setup is never followed by async_unload_entry, so the
    subprocess and its SIP registration leaked on every failure."""
    _FakeRuntime.instances = []
    monkeypatch.setattr(portico, "DoorRuntime", _FakeRuntime)
    with pytest.raises(ModuleNotFoundError):
        await portico.async_setup_door_entry(_hass(ModuleNotFoundError("camera")), MagicMock())
    assert _FakeRuntime.instances[0].stopped == 1


async def test_ha_shutdown_is_the_hubs_one_hook_not_one_per_door(monkeypatch):
    """HA stopping is not an unload; without a stop hook the binary is SIGKILLed with the container and never
    sends its de-register. Since P-3 that hook is the hub's (one per instance, hub.py), not each door's."""
    _FakeRuntime.instances = []
    monkeypatch.setattr(portico, "DoorRuntime", _FakeRuntime)
    hass, entry = _hass(), MagicMock()
    assert await portico.async_setup_door_entry(hass, entry) is True
    hass.bus.async_listen_once.assert_not_called()
    entry.async_on_unload.assert_not_called()
