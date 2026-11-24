# ruff: noqa: F811 - test parameters named after the imported fixtures
"""`capabilities_changed {device_id, capabilities}`: the gate observer's CanDial follows gate-sip's runtime dial switch
(`POST /peer/dial`) without a reload of the entry (2026-11-09: switched on after the entry loaded, `can_dial` stayed False
and the app's Talk button stayed disabled until a manual reload).

A fake Go transport (the driver part's, driver_part.py) stands in for the driver flipping `dialEnabled`: it sends the
notification the Go hub sends. Pinned: Python stores the new set (`can_dial`, the provider's door), tells the door-set
hooks (the gate watch, the house roster), tells every content subscriber to refetch, and fails closed when it goes off;
a malformed, foreign or unchanged notification does nothing.
"""
from __future__ import annotations

import pytest

from custom_components.portico.content import get_content_store
from custom_components.portico.provider import DATA_PROVIDERS, DOMAIN

from .driver_part import GATE_SIP_URL, fake_transport  # noqa: F401
from .test_door_through_the_ui_part import _custom_integrations, _setup_door  # noqa: F401 - fixtures by name

pytestmark = pytest.mark.asyncio


def _caps(runtime, **over):
    return {**(runtime.capabilities or {}), **over}


async def _rig(hass):
    entry = await _setup_door(hass)
    runtime = entry.runtime_data
    registry = hass.data[DOMAIN][DATA_PROVIDERS]
    hooks, announced = [], []
    registry.add_doors_hook(lambda: hooks.append([d.can_dial for d in registry.doors()]))
    get_content_store(hass).add_home_listener(lambda: announced.append(1))
    from .driver_part import FakeGoTransport
    return entry, runtime, registry, FakeGoTransport.instances[-1], hooks, announced


async def test_dial_switched_on_after_the_entry_loaded_reaches_python(hass, fake_transport):
    entry, runtime, registry, go, hooks, announced = await _rig(hass)
    assert runtime.can_dial is False and [d.can_dial for d in registry.doors()] == [False]
    go.notify("capabilities_changed", {"device_id": GATE_SIP_URL, "capabilities": _caps(runtime, CanDial=True)})
    assert runtime.can_dial is True
    assert [d.can_dial for d in registry.doors()] == [True]
    assert hooks == [[True]]                    # the door-set hooks ran (the gate watch lives on this)
    assert len(announced) == 1                  # and the content subscribers were told to refetch


async def test_dial_switched_off_fails_closed(hass, fake_transport):
    entry, runtime, registry, go, hooks, announced = await _rig(hass)
    go.notify("capabilities_changed", {"device_id": GATE_SIP_URL, "capabilities": _caps(runtime, CanDial=True)})
    off = {k: v for k, v in runtime.capabilities.items() if k != "CanDial"}      # Go omits CanDial unless true
    go.notify("capabilities_changed", {"device_id": GATE_SIP_URL, "capabilities": off})
    assert runtime.can_dial is False and [d.can_dial for d in registry.doors()] == [False]
    assert hooks == [[True], [False]] and len(announced) == 2


async def test_an_unchanged_foreign_or_malformed_report_does_nothing(hass, fake_transport):
    entry, runtime, registry, go, hooks, announced = await _rig(hass)
    before = dict(runtime.capabilities)
    go.notify("capabilities_changed", {"device_id": GATE_SIP_URL, "capabilities": dict(before)})          # unchanged
    go.notify("capabilities_changed", {"device_id": "another", "capabilities": _caps(runtime, CanDial=True)})  # not routed
    go.notify("capabilities_changed", {"device_id": GATE_SIP_URL, "capabilities": "yes"})                    # malformed
    go.notify("capabilities_changed", {"device_id": GATE_SIP_URL})
    assert runtime.capabilities == before and runtime.can_dial is False
    assert hooks == [] and announced == []


async def test_can_dial_is_exactly_true_only(hass, fake_transport):
    entry, runtime, registry, go, hooks, announced = await _rig(hass)
    go.notify("capabilities_changed", {"device_id": GATE_SIP_URL, "capabilities": _caps(runtime, CanDial="yes")})
    assert runtime.can_dial is False


async def test_a_door_that_left_stops_hearing_the_listener(hass, fake_transport):
    entry, runtime, registry, go, hooks, announced = await _rig(hass)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert runtime._capabilities_listeners == []
