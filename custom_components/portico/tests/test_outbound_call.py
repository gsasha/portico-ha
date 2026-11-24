# ruff: noqa: F811 - test parameters named after the imported fixtures
"""P2 of the Portico blocks series: the Python half of the dial call id (inert until a driver dials).

A call Portico itself places gets an `out-<16 hex>` id from the Go hub (P1). This file proves, at three levels
(the tracker, the door runtime, and the whole hub with real entities), that such a call

* is remembered from the dial result, so `webrtc_offer` for it is accepted, and a dial result WITHOUT a call id
  behaves exactly as it always did;
* is never a visitor ring: no change to the call-state sensor (the entity the kitchen takeover keys on), no
  `portico_intercom_call` bus event, no provider transition (so no push, no `portico/calls` event to phones, no
  listen-leg watch input);
* has its own state (`dialing` / `in_call` / `ended`) that a future Talk can observe;
* and that the `CanDial` capability reads False unless a driver says exactly True.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from homeassistant.const import EVENT_STATE_CHANGED
from pytest_homeassistant_custom_component.common import async_capture_events

from custom_components.portico.door.call_state import (
    CallStateTracker,
    OutboundTransition,
    is_outbound_event,
)
from custom_components.portico.door.provider_impl import EVENT_INTERCOM_CALL
from custom_components.portico.door.transport import TransportError

from .test_hub import _provider, _setup_two
from .test_observer import DAHUA_HOST, _custom_integrations, _sensor_id, fake_transport  # noqa: F401 - fixtures
from .test_transport_provider import _add_device

OUT = "out-0123456789abcdef"
WIRE_STATES = ("ringing", "answered", "active", "ended")


# ---- the tracker -------------------------------------------------------------------------------------------

def _watched(tracker: CallStateTracker):
    """Every visitor-side hook of the tracker, recorded."""
    plain, transitions, outbound = [], [], []
    tracker.add_listener(lambda: plain.append(tracker.state))
    tracker.add_transition_listener(transitions.append)
    tracker.add_outbound_listener(outbound.append)
    return plain, transitions, outbound


@pytest.mark.parametrize("event", [
    {"call_id": OUT, "state": "ringing"},
    {"call_id": "out-x", "state": "ringing", "device_id": "dev"},
    {"call_id": "c1", "state": "ringing", "direction": "outbound"},
    {"call_id": "c1", "state": "ringing", "outbound": True},
    {"call_id": None, "state": "ringing", "outbound": True},
])
def test_what_is_an_outbound_event(event):
    assert is_outbound_event(event)


@pytest.mark.parametrize("event", [
    {"call_id": "c1", "state": "ringing"},
    {"call_id": "dial-10.3.0.20", "state": "answered"},      # a driver's own dial id (dahua) stays as it was
    {"call_id": "outbound-1", "state": "ringing"},           # the prefix is `out-`, not `out`
    {"call_id": "xout-1", "state": "ringing"},
    {"call_id": None, "state": "ended"},
    {"call_id": "c1", "state": "ringing", "direction": "inbound"},
    {"call_id": "c1", "state": "ringing", "outbound": False},
    {"call_id": 7, "state": "ringing"},
    {"state": "ringing"},
])
def test_what_is_not_an_outbound_event(event):
    assert not is_outbound_event(event)


def test_an_outbound_call_never_moves_the_visitor_state_or_fires_a_visitor_hook():
    tracker = CallStateTracker("dev", "entry")
    plain, transitions, outbound = _watched(tracker)
    for state in WIRE_STATES:
        tracker.apply_event({"call_id": OUT, "state": state})
        assert (tracker.state, tracker.call_id) == ("idle", None)
    assert plain == [] and transitions == []
    assert [(t.prev_state, t.state) for t in outbound] == [
        ("idle", "dialing"), ("dialing", "in_call"), ("in_call", "in_call"), ("in_call", "ended")]
    assert all(isinstance(t, OutboundTransition) and t.call_id == OUT and t.device_id == "dev" and
               t.entry_id == "entry" for t in outbound)


def test_the_outbound_call_has_its_own_state_a_talk_can_read():
    tracker = CallStateTracker("dev")
    assert (tracker.outbound_call_id, tracker.outbound_state) == (None, "idle")
    tracker.apply_event({"call_id": OUT, "state": "ringing"})
    assert (tracker.outbound_call_id, tracker.outbound_state) == (OUT, "dialing")
    tracker.apply_event({"call_id": OUT, "state": "answered"})
    assert (tracker.outbound_call_id, tracker.outbound_state) == (OUT, "in_call")
    tracker.apply_event({"call_id": OUT, "state": "ended"})
    assert (tracker.outbound_call_id, tracker.outbound_state) == (None, "idle")


def test_a_visitor_ring_and_an_outbound_call_do_not_disturb_each_other():
    tracker = CallStateTracker("dev")
    plain, transitions, outbound = _watched(tracker)
    tracker.apply_event({"call_id": "v1", "state": "ringing"})
    tracker.apply_event({"call_id": OUT, "state": "ringing"})
    tracker.apply_event({"call_id": OUT, "state": "answered"})
    assert (tracker.state, tracker.call_id) == ("ringing", "v1")        # the visitor is still ringing
    tracker.apply_event({"call_id": "v1", "state": "ended"})
    assert (tracker.state, tracker.call_id) == ("idle", None)
    assert (tracker.outbound_call_id, tracker.outbound_state) == (OUT, "in_call")   # ending v1 left ours alone
    tracker.apply_event({"call_id": OUT, "state": "ended"})
    assert plain == ["ringing", "idle"] and [t.call_id for t in transitions] == ["v1", "v1"]
    assert [t.state for t in outbound] == ["dialing", "in_call", "ended"]


def test_a_late_event_for_an_ended_outbound_call_is_dropped():
    tracker = CallStateTracker("dev")
    _, _, outbound = _watched(tracker)
    tracker.apply_event({"call_id": OUT, "state": "ringing"})
    tracker.apply_event({"call_id": OUT, "state": "ended"})
    tracker.apply_event({"call_id": OUT, "state": "answered"})
    assert tracker.outbound_state == "idle" and [t.state for t in outbound] == ["dialing", "ended"]


def test_the_end_of_another_outbound_call_leaves_the_tracked_one():
    tracker = CallStateTracker("dev")
    tracker.apply_event({"call_id": OUT, "state": "answered"})
    tracker.apply_event({"call_id": "out-other", "state": "ended"})
    assert (tracker.outbound_call_id, tracker.outbound_state) == (OUT, "in_call")


def test_an_unknown_outbound_state_is_ignored():
    tracker = CallStateTracker("dev")
    _, _, outbound = _watched(tracker)
    tracker.apply_event({"call_id": OUT, "state": "some-future-state"})
    assert outbound == [] and tracker.outbound_state == "idle"


def test_end_outbound_call_ends_the_tracked_call_and_is_a_noop_without_one():
    tracker = CallStateTracker("dev")
    _, _, outbound = _watched(tracker)
    tracker.end_outbound_call()
    assert outbound == []
    tracker.apply_event({"call_id": OUT, "state": "answered"})
    tracker.end_outbound_call()
    assert [t.state for t in outbound] == ["in_call", "ended"] and tracker.outbound_call_id is None


def test_a_raising_outbound_listener_cannot_stop_the_state_change():
    tracker = CallStateTracker("dev")
    tracker.add_outbound_listener(lambda t: 1 / 0)
    seen = []
    tracker.add_outbound_listener(seen.append)
    tracker.apply_event({"call_id": OUT, "state": "ringing"})
    assert tracker.outbound_state == "dialing" and len(seen) == 1


def test_a_removed_outbound_listener_hears_nothing():
    tracker = CallStateTracker("dev")
    seen = []
    tracker.add_outbound_listener(seen.append)
    tracker.remove_outbound_listener(seen.append)
    tracker.apply_event({"call_id": OUT, "state": "ringing"})
    assert seen == []


def test_golden_a_dial_style_id_without_the_prefix_is_still_a_visitor_style_call():
    """No behaviour change for any id the hub does not mint as `out-`: today's dahua `dial-<host>` call is
    tracked exactly as before."""
    tracker = CallStateTracker("dev")
    plain, transitions, outbound = _watched(tracker)
    tracker.apply_event({"call_id": "dial-dev", "state": "answered"})
    assert (tracker.state, tracker.call_id) == ("answered", "dial-dev")
    tracker.apply_event({"call_id": "dial-dev", "state": "ended"})
    assert plain == ["answered", "idle"] and len(transitions) == 2 and outbound == []
    assert tracker.outbound_state == "idle"


# ---- the door runtime and the provider ---------------------------------------------------------------------

@pytest.mark.parametrize("method", ["ringing", "call_state_changed"])
@pytest.mark.parametrize("extra", [{}, {"device_id": "10.3.0.20"}, {"device_id": ""}])
async def test_a_door_given_an_outbound_notification_makes_no_visitor_change(hass, method, extra):
    """The matrix: ringing/answered/active/ended with an `out-` id, by either notification method and stamped
    or unstamped with the device id, produces no state, no bus event, no provider transition."""
    _, runtime = _add_device(hass, device_id="10.3.0.20")
    bus = async_capture_events(hass, EVENT_INTERCOM_CALL)
    transitions = []
    from custom_components.portico.door.provider_impl import DATA_TRANSPORT_PROVIDER
    from custom_components.portico.door.const import DOMAIN, DRIVER_ID
    hass.data[DRIVER_ID][DATA_TRANSPORT_PROVIDER].subscribe(transitions.append)
    plain = []
    runtime.call_state.add_listener(lambda: plain.append(1))
    for state in WIRE_STATES:
        runtime.handle_notification(method, {"call_id": OUT, "state": state, **extra})
        assert (runtime.call_state.state, runtime.call_state.call_id) == ("idle", None)
    await hass.async_block_till_done()
    assert bus == [] and transitions == [] and plain == []
    assert runtime.call_state.outbound_state == "idle"          # the whole sequence ended


async def test_an_outbound_ringing_notification_lands_in_the_outbound_state(hass):
    _, runtime = _add_device(hass, device_id="10.3.0.20")
    runtime.handle_notification("ringing", {"call_id": OUT, "device_id": "10.3.0.20"})
    assert (runtime.call_state.outbound_call_id, runtime.call_state.outbound_state) == (OUT, "dialing")
    runtime.handle_notification("call_state_changed", {"call_id": OUT, "state": "active"})
    assert runtime.call_state.outbound_state == "in_call"


async def test_a_visitor_ring_through_the_same_door_is_unchanged(hass):
    """Golden: the same notifications with a visitor id still ring, still fire the bus event."""
    _, runtime = _add_device(hass, device_id="10.3.0.20")
    bus = async_capture_events(hass, EVENT_INTERCOM_CALL)
    runtime.handle_notification("ringing", {"call_id": "c1", "device_id": "10.3.0.20"})
    assert (runtime.call_state.state, runtime.call_state.call_id) == ("ringing", "c1")
    await hass.async_block_till_done()
    assert [e.data["state"] for e in bus] == ["ringing"]


async def test_an_outbound_notification_for_the_wrong_device_is_still_dropped(hass, caplog):
    _, runtime = _add_device(hass, device_id="10.3.0.20")
    runtime.handle_notification("ringing", {"call_id": OUT, "device_id": "10.3.0.99"})
    assert runtime.call_state.outbound_state == "idle"
    assert "dropping a ringing for device" in caplog.text


async def test_a_door_that_loses_its_transport_ends_the_outbound_call_too(hass):
    _, runtime = _add_device(hass, device_id="10.3.0.20")
    seen = []
    runtime.call_state.add_outbound_listener(seen.append)
    runtime.handle_notification("call_state_changed", {"call_id": OUT, "state": "answered"})
    runtime.transport_gone()
    assert runtime.call_state.outbound_state == "idle" and [t.state for t in seen] == ["in_call", "ended"]


async def test_dial_result_with_a_call_id_is_remembered_so_webrtc_offer_is_accepted(hass):
    _, runtime = _add_device(hass, device_id="10.3.0.20")
    runtime.client.dial = AsyncMock(return_value={"ok": True, "call_id": OUT})
    runtime.client.webrtc_offer = AsyncMock(return_value={"sdp": "answer"})
    provider = _provider(hass)
    assert await provider.dial("10.3.0.20", "10.9.0.1", 5060) == {"ok": True, "call_id": OUT}
    assert runtime._hub.noted == [(OUT, "10.3.0.20")]           # the hub is told which door owns the id
    assert await provider.webrtc_offer(OUT, "offer") == {"sdp": "answer"}
    runtime.client.webrtc_offer.assert_awaited_once_with(OUT, "offer")


async def test_golden_dial_result_without_a_call_id_is_exactly_as_before(hass):
    """The current Go hub replies `{ok: true}` to a dial: nothing is remembered, and an offer for any id
    is `unknown_call`, as it always was."""
    _, runtime = _add_device(hass, device_id="10.3.0.20")
    runtime.client.dial = AsyncMock(return_value={"ok": True})
    runtime.client.webrtc_offer = AsyncMock(return_value={"sdp": "answer"})
    provider = _provider(hass)
    assert await provider.dial("10.3.0.20", "10.9.0.1", 5060) == {"ok": True}
    assert runtime._hub.noted == []
    assert provider._call_doors == {}
    for result in ({"ok": True, "call_id": ""}, {"ok": True, "call_id": 7}, {"ok": True, "call_id": None}, None):
        runtime.client.dial = AsyncMock(return_value=result)
        assert await provider.dial("10.3.0.20", "10.9.0.1", 5060) == result
    assert runtime._hub.noted == [] and provider._call_doors == {}
    with pytest.raises(TransportError) as err:
        await provider.webrtc_offer(OUT, "offer")
    assert err.value.code == "unknown_call"
    runtime.client.webrtc_offer.assert_not_awaited()


async def test_hangup_of_an_outbound_call_ends_only_the_outbound_call(hass):
    _, runtime = _add_device(hass, device_id="10.3.0.20")
    runtime.client.dial = AsyncMock(return_value={"ok": True, "call_id": OUT})
    runtime.client.hangup = AsyncMock(return_value={"ok": True})
    provider = _provider(hass)
    await provider.dial("10.3.0.20", "10.9.0.1", 5060)
    runtime.handle_notification("call_state_changed", {"call_id": OUT, "state": "answered"})
    runtime.handle_notification("ringing", {"call_id": "v1", "device_id": "10.3.0.20"})
    await provider.hangup(OUT, "bye")
    assert runtime.call_state.outbound_state == "idle"
    assert (runtime.call_state.state, runtime.call_state.call_id) == ("ringing", "v1")


# ---- the whole hub, real entities: what the takeover automation can see --------------------------------------

async def test_outbound_call_through_the_hub_produces_no_visitor_ring_anywhere(hass, fake_transport):
    """The takeover proof. The kitchen takeover is driven by a door's call-state sensor reaching `ringing` (and by
    the ring events built on the same tracker). Dial through the real hub, then let the (fake) Go side report
    ringing -> answered -> active -> ended for the `out-` id, stamped and unstamped. Every state the sensor, or
    any other entity, ever takes is recorded: none is `ringing`, none changes at all, and no ring event is fired
    or delivered."""
    observer, dahua = await _setup_two(hass)
    go = fake_transport.instances[0]
    sensor = _sensor_id(hass, dahua)
    observer_sensor = _sensor_id(hass, observer)

    async def dial(device_id, host, port):
        go.requests.append(("dial", (device_id, host, port)))
        return {"ok": True, "call_id": OUT}

    go.dial = dial
    states, bus, transitions = [], async_capture_events(hass, EVENT_INTERCOM_CALL), []
    hass.bus.async_listen(EVENT_STATE_CHANGED, lambda e: states.append((e.data["entity_id"], e.data["new_state"])))
    _provider(hass).subscribe(transitions.append)
    before = {s: hass.states.get(s) for s in (sensor, observer_sensor)}

    assert (await _provider(hass).dial(DAHUA_HOST, "10.0.0.9", 5060))["call_id"] == OUT
    for stamp in ({}, {"device_id": ""}, {"device_id": DAHUA_HOST}):      # unstamped, empty, stamped
        for state in ("ringing", "answered", "active", "ended"):
            go.notify("call_state_changed", {"call_id": OUT, "state": state, **stamp})
        go.notify("ringing", {"call_id": OUT, **stamp})                    # and an outbound `ringing` notification
    await hass.async_block_till_done()

    assert [s for _, s in states if s is not None and s.state == "ringing"] == []
    assert states == []                                    # no entity changed at all
    assert hass.states.get(sensor) is before[sensor] and hass.states.get(observer_sensor) is before[observer_sensor]
    assert hass.states.get(sensor).state == "idle" and hass.states.get(sensor).attributes["call_id"] is None
    assert bus == [] and transitions == []
    assert dahua.runtime_data.call_state.outbound_state == "idle"


async def test_outbound_call_is_followed_through_the_hub_for_a_talk(hass, fake_transport):
    observer, dahua = await _setup_two(hass)
    go = fake_transport.instances[0]

    async def dial(device_id, host, port):
        return {"ok": True, "call_id": OUT}

    go.dial = dial
    tracker = dahua.runtime_data.call_state
    seen = []
    tracker.add_outbound_listener(seen.append)
    await _provider(hass).dial(DAHUA_HOST, "10.0.0.9", 5060)
    go.notify("call_state_changed", {"call_id": OUT, "state": "ringing"})
    go.notify("call_state_changed", {"call_id": OUT, "state": "answered"})
    assert (tracker.outbound_call_id, tracker.outbound_state) == (OUT, "in_call")
    assert observer.runtime_data.call_state.outbound_state == "idle"         # the other door never hears it
    assert (await _provider(hass).webrtc_offer(OUT, "offer")) == {"sdp": f"answer-for-{OUT}"}
    go.notify("call_state_changed", {"call_id": OUT, "state": "ended"})
    assert [t.state for t in seen] == ["dialing", "in_call", "ended"]
    assert tracker.outbound_state == "idle"


async def test_golden_a_visitor_ring_through_the_hub_still_rings_the_sensor(hass, fake_transport):
    observer, dahua = await _setup_two(hass)
    go = fake_transport.instances[0]
    sensor = _sensor_id(hass, dahua)
    bus = async_capture_events(hass, EVENT_INTERCOM_CALL)
    go.ring("dh-1", device_id=DAHUA_HOST)
    await hass.async_block_till_done()
    assert hass.states.get(sensor).state == "ringing" and hass.states.get(sensor).attributes["call_id"] == "dh-1"
    assert [e.data["state"] for e in bus] == ["ringing"]


# ---- the CanDial capability ----------------------------------------------------------------------------------

@pytest.mark.parametrize("capabilities,expected", [
    (None, False),
    ({}, False),
    ({"DirectCapable": True, "HasRelay": True}, False),
    ({"CanDial": False}, False),
    ({"CanDial": None}, False),
    ({"CanDial": "true"}, False),
    ({"CanDial": 1}, False),
    ({"can_dial": True}, False),                    # the wire name is the Go field name, verbatim
    ({"CanDial": True}, True),
])
async def test_can_dial_is_true_only_for_an_explicit_true(hass, capabilities, expected):
    _, runtime = _add_device(hass, device_id="10.3.0.20")
    runtime.capabilities = capabilities
    assert runtime.can_dial is expected


async def test_no_driver_the_hub_configures_today_can_dial(hass, fake_transport):
    observer, dahua = await _setup_two(hass)
    assert observer.runtime_data.capabilities is not None and dahua.runtime_data.capabilities is not None
    assert "CanDial" not in observer.runtime_data.capabilities and "CanDial" not in dahua.runtime_data.capabilities
    assert observer.runtime_data.can_dial is False and dahua.runtime_data.can_dial is False
