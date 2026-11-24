# ruff: noqa: F811 - test parameters named after the imported fixtures
"""M7 P3-B: the driver part hands the changes of a call Portico placed to the UI part's talk relay, through the provider
seam (`subscribe_outbound`, contract/provider_v1.md section 4a), and through nothing else.

What is pinned: the dict's shape; an outbound change reaches an outbound subscriber and NO visitor listener, entity,
bus event or push input; a visitor ring reaches no outbound subscriber; a detached door stops being heard; a raising
subscriber is skipped; and through the whole hub, two doors, the change carries the door it belongs to. HA-harness session.
"""
from __future__ import annotations

import pytest
from homeassistant.const import EVENT_STATE_CHANGED
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.portico.door.const import DOMAIN, DRIVER_ID
from custom_components.portico.door.provider_impl import DATA_TRANSPORT_PROVIDER, EVENT_INTERCOM_CALL

from .test_hub import _provider, _setup_two
from .test_observer import DAHUA_HOST, _custom_integrations, _sensor_id, fake_transport  # noqa: F401 - fixtures
from .test_transport_provider import _add_device

OUT = "out-0123456789abcdef"


def _provider_of(hass):
    return hass.data[DRIVER_ID][DATA_TRANSPORT_PROVIDER]


async def test_an_outbound_change_reaches_an_outbound_subscriber_as_a_plain_dict(hass):
    entry, runtime = _add_device(hass, device_id="10.3.0.20")
    heard = []
    _provider_of(hass).subscribe_outbound(heard.append)
    runtime.handle_notification("ringing", {"call_id": OUT, "device_id": "10.3.0.20"})
    runtime.handle_notification("call_state_changed", {"call_id": OUT, "state": "answered"})
    runtime.handle_notification("call_state_changed", {"call_id": OUT, "state": "ended"})
    assert [(h["door_id"], h["call_id"], h["state"], h["prev_state"]) for h in heard] == [
        ("10.3.0.20", OUT, "dialing", "idle"), ("10.3.0.20", OUT, "in_call", "dialing"), ("10.3.0.20", OUT, "ended", "in_call")]
    assert all(set(h) == {"door_id", "call_id", "prev_state", "state", "at_wall"} for h in heard)
    assert all(isinstance(h["at_wall"], float) for h in heard)


async def test_an_outbound_change_reaches_no_visitor_listener_entity_or_bus_event(hass):
    entry, runtime = _add_device(hass, device_id="10.3.0.20")
    visitors, states = [], []
    _provider_of(hass).subscribe(visitors.append)
    _provider_of(hass).subscribe_outbound(lambda raw: None)
    bus = async_capture_events(hass, EVENT_INTERCOM_CALL)
    hass.bus.async_listen(EVENT_STATE_CHANGED, states.append)
    for state in ("ringing", "answered", "active", "ended"):
        runtime.handle_notification("call_state_changed", {"call_id": OUT, "state": state})
    await hass.async_block_till_done()
    assert visitors == [] and bus == [] and states == []
    assert (runtime.call_state.state, runtime.call_state.call_id) == ("idle", None)


async def test_a_visitor_ring_reaches_no_outbound_subscriber(hass):
    entry, runtime = _add_device(hass, device_id="10.3.0.20")
    heard = []
    _provider_of(hass).subscribe_outbound(heard.append)
    runtime.handle_notification("ringing", {"call_id": "c1", "device_id": "10.3.0.20"})
    runtime.handle_notification("call_state_changed", {"call_id": "c1", "state": "answered"})
    runtime.handle_notification("call_state_changed", {"call_id": "c1", "state": "ended"})
    assert heard == []


async def test_unsubscribing_twice_is_harmless_and_a_raising_subscriber_is_skipped(hass):
    entry, runtime = _add_device(hass, device_id="10.3.0.20")
    heard = []

    def boom(raw):
        raise RuntimeError("no")

    _provider_of(hass).subscribe_outbound(boom)
    unsub = _provider_of(hass).subscribe_outbound(heard.append)
    runtime.handle_notification("call_state_changed", {"call_id": OUT, "state": "answered"})
    assert len(heard) == 1
    unsub()
    unsub()
    runtime.handle_notification("call_state_changed", {"call_id": OUT, "state": "ended"})
    assert len(heard) == 1


async def test_a_detached_door_is_no_longer_heard_and_an_attached_one_is_listened_to_once(hass):
    entry, runtime = _add_device(hass, device_id="10.3.0.20")
    provider = _provider_of(hass)
    provider.attach(entry)                       # a second attach of the same entry is a no-op
    heard = []
    provider.subscribe_outbound(heard.append)
    runtime.handle_notification("call_state_changed", {"call_id": OUT, "state": "answered"})
    assert len(heard) == 1                       # once, not twice
    provider.detach(entry)
    runtime.handle_notification("call_state_changed", {"call_id": OUT, "state": "ended"})
    assert len(heard) == 1
    assert runtime.call_state._outbound_listeners == []


async def test_the_hub_routes_an_outbound_change_to_its_own_door_only(hass, fake_transport):
    observer, dahua = await _setup_two(hass)
    go = fake_transport.instances[0]

    async def dial(device_id, host, port):
        return {"ok": True, "call_id": OUT}

    go.dial = dial
    heard = []
    _provider(hass).subscribe_outbound(heard.append)
    states = []
    hass.bus.async_listen(EVENT_STATE_CHANGED, states.append)
    await _provider(hass).dial(DAHUA_HOST, "10.0.0.9", 5060)
    go.notify("call_state_changed", {"call_id": OUT, "state": "answered"})     # unstamped: routed by the dial's call id
    go.notify("call_state_changed", {"call_id": OUT, "state": "ended"})
    await hass.async_block_till_done()
    assert [(h["door_id"], h["state"]) for h in heard] == [(DAHUA_HOST, "in_call"), (DAHUA_HOST, "ended")]
    assert states == [] and observer.runtime_data.call_state.outbound_state == "idle"
    assert hass.states.get(_sensor_id(hass, dahua)).state == "idle"
