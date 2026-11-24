# ruff: noqa: F811 - test parameters named after the imported fixtures
"""M7 step 3a-0 (HC-5): the gate and the entrance door can ring AT THE SAME TIME.

The driver part's side of "two doors at once", pinned with the code as it is (no production change): each door has
its own `CallStateTracker`; the hub routes a notification by `device_id` (or by the call id it learned); the provider
reports every transition with its door's id; one door going idle, unavailable or away never touches the other's call;
the one arbitration (`answer`) holds one lock PER DOOR; a hangup goes to the call's own door. The mixed pair used
here is the estate's: the gate (a `gate_sip_observer`, relay answer) and the entrance (a `dahua_sip` door, direct
capable), on the one hub, the transport and its socket faked exactly as test_hub.py fakes them.

What is NOT here: the phone's own "you are already on a call" (the app's, `another_call`) and any server-side busy
rule for a phone. The server has none today: a second door can be answered while the first is answered; the first
test that says so is marked CURRENT and is the one 3a (the ClaimLedger) is expected to change.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from homeassistant.core import callback

from custom_components.portico.door.call_state import CallStateTracker
from custom_components.portico.door.provider_impl import EVENT_INTERCOM_CALL
from custom_components.portico.door.transport import TransportError

from .test_hub import _provider, _setup_two
from .test_observer import (  # noqa: F401 - fixtures by name
    DAHUA_HOST,
    GATE_SIP_URL,
    _custom_integrations,
    _sensor_id,
    fake_transport,
)

GATE = GATE_SIP_URL        # the gate: a gate_sip_observer, answered through the transport (relay)
ENTRANCE = DAHUA_HOST      # the entrance: a dahua_sip door, direct capable


async def _both_ring(hass, fake_transport):
    observer, dahua = await _setup_two(hass)
    go = fake_transport.instances[0]
    go.ring("gate-1", device_id=GATE)
    go.ring("door-1", device_id=ENTRANCE)
    await hass.async_block_till_done()
    return observer, dahua, go


# ---- the trackers -----------------------------------------------------------------------------------------------


async def test_two_trackers_share_no_state_not_even_the_ended_call_memory():
    gate, door = CallStateTracker("gate", "e1"), CallStateTracker("door", "e2")
    gate.apply_event({"call_id": "c1", "state": "ringing"})
    gate.apply_event({"call_id": "c1", "state": "ended"})
    assert gate._ended_call_ids is not door._ended_call_ids
    # a call id the gate ended is not "ended" for the entrance: its own ring with that id is accepted
    door.apply_event({"call_id": "c1", "state": "ringing"})
    assert (door.call_id, door.state) == ("c1", "ringing")
    assert (gate.call_id, gate.state) == (None, "idle")


async def test_one_transition_listener_on_two_trackers_sees_which_door_each_change_is_about():
    gate, door = CallStateTracker("gate", "e1"), CallStateTracker("door", "e2")
    seen = []
    for tracker in (gate, door):
        tracker.add_transition_listener(seen.append)
    gate.apply_event({"call_id": "g1", "state": "ringing"})
    door.apply_event({"call_id": "d1", "state": "ringing"})
    door.apply_event({"call_id": "d1", "state": "answered"})
    gate.apply_event({"call_id": "g1", "state": "ended"})
    assert [(t.device_id, t.entry_id, t.call_id, t.prev_state, t.state) for t in seen] == [
        ("gate", "e1", "g1", "idle", "ringing"),
        ("door", "e2", "d1", "idle", "ringing"),
        ("door", "e2", "d1", "ringing", "answered"),
        ("gate", "e1", "g1", "ringing", "ended"),   # the gate's end names the gate's call, not the door's
    ]
    assert (door.call_id, door.state) == ("d1", "answered")


async def test_the_plain_listeners_of_one_tracker_never_fire_for_the_other_door():
    gate, door = CallStateTracker("gate"), CallStateTracker("door")
    fired = []
    gate.add_listener(lambda: fired.append("gate"))
    door.add_listener(lambda: fired.append("door"))
    door.apply_event({"call_id": "d1", "state": "ringing"})
    gate.apply_event({"call_id": "g1", "state": "ringing"})
    door.apply_event({"call_id": "d1", "state": "ended"})
    assert fired == ["door", "gate", "door"]


# ---- the hub and the doors ----------------------------------------------------------------------------------------


async def test_both_doors_ring_at_once_each_keeps_its_own_call_and_sensor(hass, fake_transport):
    observer, dahua, go = await _both_ring(hass, fake_transport)

    assert (observer.runtime_data.call_state.call_id, observer.runtime_data.call_state.state) == ("gate-1", "ringing")
    assert (dahua.runtime_data.call_state.call_id, dahua.runtime_data.call_state.state) == ("door-1", "ringing")
    for entry, call in ((observer, "gate-1"), (dahua, "door-1")):
        sensor = hass.states.get(_sensor_id(hass, entry))
        assert sensor.state == "ringing" and sensor.attributes.get("call_id") == call
    provider = _provider(hass)
    assert provider.call_state(GATE) == {"door_id": GATE, "call_id": "gate-1", "state": "ringing"}
    assert provider.call_state(ENTRANCE) == {"door_id": ENTRANCE, "call_id": "door-1", "state": "ringing"}


async def test_interleaved_ring_answer_end_reports_each_transition_with_its_door_and_call(hass, fake_transport):
    observer, dahua = await _setup_two(hass)
    go = fake_transport.instances[0]
    provider = _provider(hass)
    seen: list[dict] = []
    provider.subscribe(seen.append)
    bus: list[dict] = []
    hass.bus.async_listen(EVENT_INTERCOM_CALL, callback(lambda event: bus.append(dict(event.data))))

    go.ring("gate-1", device_id=GATE)
    go.ring("door-1", device_id=ENTRANCE)
    go.state("door-1", "answered")
    go.state("gate-1", "answered")
    go.state("door-1", "ended")
    go.state("gate-1", "active")
    go.state("gate-1", "ended")
    await hass.async_block_till_done()

    expected = [
        (GATE, "gate-1", "idle", "ringing"),
        (ENTRANCE, "door-1", "idle", "ringing"),
        (ENTRANCE, "door-1", "ringing", "answered"),
        (GATE, "gate-1", "ringing", "answered"),
        (ENTRANCE, "door-1", "answered", "ended"),
        (GATE, "gate-1", "answered", "active"),
        (GATE, "gate-1", "active", "ended"),
    ]
    assert [(t["door_id"], t["call_id"], t["prev_state"], t["state"]) for t in seen] == expected
    # the bus event (fired by the driver whether or not anything listens) carries the same door and call
    assert [(e["device_id"], e["call_id"], e["prev_state"], e["state"]) for e in bus] == expected
    assert observer.runtime_data.call_state.state == dahua.runtime_data.call_state.state == "idle"


async def test_one_doors_end_never_ends_the_other_doors_call(hass, fake_transport):
    observer, dahua, go = await _both_ring(hass, fake_transport)
    go.state("gate-1", "answered")
    go.state("door-1", "ended")
    await hass.async_block_till_done()

    assert dahua.runtime_data.call_state.state == "idle"
    gate_state = observer.runtime_data.call_state
    assert (gate_state.call_id, gate_state.state) == ("gate-1", "answered")
    assert hass.states.get(_sensor_id(hass, observer)).state == "answered"
    assert hass.states.get(_sensor_id(hass, dahua)).state == "idle"
    # and the other way round: the entrance rings again while the gate is still answered
    go.ring("door-2", device_id=ENTRANCE)
    go.state("gate-1", "ended")
    await hass.async_block_till_done()
    assert (dahua.runtime_data.call_state.call_id, dahua.runtime_data.call_state.state) == ("door-2", "ringing")
    assert observer.runtime_data.call_state.state == "idle"


async def test_a_late_event_of_an_ended_call_changes_neither_door(hass, fake_transport):
    observer, dahua, go = await _both_ring(hass, fake_transport)
    go.state("gate-1", "ended")
    go.state("door-1", "answered")
    # the Go side still follows the gate's call for a moment: stamped, and unstamped (routed by the learned call id)
    go.state("gate-1", "answered")
    go.state("gate-1", "active", device_id="")
    await hass.async_block_till_done()
    assert observer.runtime_data.call_state.state == "idle"
    assert (dahua.runtime_data.call_state.call_id, dahua.runtime_data.call_state.state) == ("door-1", "answered")


async def test_one_door_going_unavailable_ends_only_its_own_call(hass, fake_transport):
    observer, dahua, go = await _both_ring(hass, fake_transport)
    seen: list[dict] = []
    _provider(hass).subscribe(seen.append)

    go.availability(False, "gate-sip down", device_id=GATE)       # the gate's driver can no longer see its calls
    await hass.async_block_till_done()

    assert observer.runtime_data.call_state.state == "idle"
    assert (dahua.runtime_data.call_state.call_id, dahua.runtime_data.call_state.state) == ("door-1", "ringing")
    assert [(t["door_id"], t["call_id"], t["state"]) for t in seen] == [(GATE, "gate-1", "ended")]


async def test_a_door_leaving_mid_call_ends_only_its_call_and_the_other_door_carries_on(hass, fake_transport):
    observer, dahua, go = await _both_ring(hass, fake_transport)
    seen: list[dict] = []
    _provider(hass).subscribe(seen.append)

    assert await hass.config_entries.async_unload(observer.entry_id)
    await hass.async_block_till_done()

    assert [(t["door_id"], t["call_id"], t["state"]) for t in seen] == [(GATE, "gate-1", "ended")]
    assert [d["id"] for d in _provider(hass).doors()] == [ENTRANCE]
    assert _provider(hass).call_state(GATE) is None
    assert _provider(hass).call_state(ENTRANCE) == {"door_id": ENTRANCE, "call_id": "door-1", "state": "ringing"}
    go.state("door-1", "answered")
    await hass.async_block_till_done()
    assert dahua.runtime_data.call_state.state == "answered"


async def test_the_transport_dying_ends_every_doors_call_once_each(hass, fake_transport):
    observer, dahua, go = await _both_ring(hass, fake_transport)
    seen: list[dict] = []
    _provider(hass).subscribe(seen.append)
    for entry in (observer, dahua):
        entry.runtime_data.transport_gone()
    await hass.async_block_till_done()
    assert sorted((t["door_id"], t["call_id"], t["state"]) for t in seen) == [
        (GATE, "gate-1", "ended"), (ENTRANCE, "door-1", "ended")]


# ---- answer, hangup, dial ---------------------------------------------------------------------------------------


async def test_current_a_second_door_can_be_answered_while_the_first_is_answered(hass, fake_transport):
    """CURRENT. The server has no busy rule for a phone: the gate answered through the transport (relay) and the
    entrance answered direct are both granted, back to back. "You are already on a call" is the app's today; a
    server-side rule (3a's ClaimLedger) would change this test, not the per-door tracking it also pins."""
    observer, dahua, go = await _both_ring(hass, fake_transport)
    go.answer = AsyncMock(return_value={})
    provider = _provider(hass)

    first = await provider.answer(GATE, "gate-1", client_kind="app", claimant="phone-1")
    second = await provider.answer(ENTRANCE, "door-1", client_kind="app", claimant="phone-1")

    assert (first["verdict"], first["mode"]) == ("answered", "relay")      # the observer is not direct capable
    assert (second["verdict"], second["mode"]) == ("answered", "direct")   # a dahua door is: only the tracker moves
    assert go.answer.await_args_list[0].args[0] == GATE and len(go.answer.await_args_list) == 1
    assert observer.runtime_data.call_state.state == dahua.runtime_data.call_state.state == "answered"


async def test_the_answer_lock_is_per_door_a_slow_answer_of_one_door_does_not_hold_up_the_other(hass, fake_transport):
    observer, dahua, go = await _both_ring(hass, fake_transport)
    gate_release = asyncio.Event()
    asked: list[str] = []

    async def answer(device_id, host, port):
        asked.append(device_id)
        if device_id == GATE:
            await gate_release.wait()
        return {}

    go.answer = answer
    provider = _provider(hass)
    # an untracked task: hass.async_block_till_done() would wait for it, and it waits for gate_release
    slow = asyncio.ensure_future(provider.answer(GATE, "gate-1", client_kind="browser", claimant="a"))
    for _ in range(20):
        await asyncio.sleep(0)
    assert asked == [GATE] and not slow.done()

    other = await asyncio.wait_for(
        provider.answer(ENTRANCE, "door-1", client_kind="browser", claimant="b"), timeout=5)   # relay as well
    assert (other["verdict"], other["mode"]) == ("answered", "relay")
    assert not slow.done() and asked == [GATE, ENTRANCE]
    assert dahua.runtime_data.call_state.state == "answered" and observer.runtime_data.call_state.state == "ringing"

    gate_release.set()
    assert (await slow)["verdict"] == "answered"
    assert observer.runtime_data.call_state.state == "answered"


async def test_two_claimants_of_the_same_door_are_still_arbitrated_by_that_doors_lock(hass, fake_transport):
    """The per-door lock still does its job with a second door in flight: of two claimants of the gate exactly one
    is granted and the other reads `answered` inside the lock; the entrance's own claim is independent."""
    observer, dahua, go = await _both_ring(hass, fake_transport)
    go.answer = AsyncMock(return_value={})
    provider = _provider(hass)
    results = await asyncio.gather(
        provider.answer(GATE, "gate-1", client_kind="browser", claimant="a"),
        provider.answer(GATE, "gate-1", client_kind="browser", claimant="b"),
        provider.answer(ENTRANCE, "door-1", client_kind="app", claimant="c"),
    )
    assert sorted(r["verdict"] for r in results[:2]) == ["answered", "taken_elsewhere"]
    assert results[2]["verdict"] == "answered"
    assert go.answer.await_count == 1


async def test_answering_a_door_with_the_other_doors_call_id_is_not_available_and_changes_nothing(hass, fake_transport):
    observer, dahua, go = await _both_ring(hass, fake_transport)
    go.answer = AsyncMock(return_value={})
    provider = _provider(hass)
    assert (await provider.answer(ENTRANCE, "gate-1", client_kind="browser", claimant="a"))["verdict"] == "not_available"
    assert (await provider.answer(GATE, "door-1", client_kind="browser", claimant="a"))["verdict"] == "not_available"
    go.answer.assert_not_awaited()
    assert observer.runtime_data.call_state.state == dahua.runtime_data.call_state.state == "ringing"


async def test_hangup_goes_to_the_calls_own_door_and_ends_only_that_call(hass, fake_transport):
    observer, dahua, go = await _both_ring(hass, fake_transport)
    go.hangup = AsyncMock(return_value={"ok": True})
    provider = _provider(hass)
    seen: list[dict] = []
    provider.subscribe(seen.append)

    await provider.hangup("door-1", "user_hangup")

    go.hangup.assert_awaited_once_with("door-1", "user_hangup")
    assert dahua.runtime_data.call_state.state == "idle"
    assert (observer.runtime_data.call_state.call_id, observer.runtime_data.call_state.state) == ("gate-1", "ringing")
    assert [(t["door_id"], t["call_id"], t["state"]) for t in seen] == [(ENTRANCE, "door-1", "ended")]
    await provider.hangup("gate-1", "user_hangup")
    assert observer.runtime_data.call_state.state == "idle"
    with pytest.raises(TransportError) as err:
        await provider.hangup("ghost", "x")
    assert err.value.code == "unknown_call"


async def test_a_dial_out_of_one_door_leaves_the_other_doors_visitor_untouched(hass, fake_transport):
    """A call Portico places on the entrance (the hub's `out-` id) is tracked on the side: the gate's ring, its
    sensor and its snapshot are unaffected, and the entrance's own visitor state stays idle."""
    observer, dahua = await _setup_two(hass)
    go = fake_transport.instances[0]

    async def dial(device_id, host, port):
        return {"call_id": "out-0123456789abcdef"}

    go.dial = dial
    provider = _provider(hass)
    go.ring("gate-1", device_id=GATE)
    result = await provider.dial(ENTRANCE, "10.9.9.9", 4000)
    out = result["call_id"]
    go.state(out, "ringing", device_id="")        # an outbound transition carries no device id: routed by the learned id
    go.state(out, "answered", device_id="")
    await hass.async_block_till_done()

    tracker = dahua.runtime_data.call_state
    assert (tracker.outbound_call_id, tracker.outbound_state) == (out, "in_call")
    assert (tracker.call_id, tracker.state) == (None, "idle")
    gate_state = observer.runtime_data.call_state
    assert (gate_state.call_id, gate_state.state, gate_state.outbound_call_id) == ("gate-1", "ringing", None)
    assert provider.call_state(ENTRANCE) == {"door_id": ENTRANCE, "call_id": None, "state": "idle",
                                             "outbound_state": "in_call", "outbound_call_id": out}
    assert provider.call_state(GATE) == {"door_id": GATE, "call_id": "gate-1", "state": "ringing"}
    # the gate's visitor leaving does not end the entrance's outbound call
    go.state("gate-1", "ended")
    await hass.async_block_till_done()
    assert tracker.outbound_state == "in_call"


# ---- DEV-806: the late hangup, and Talk's `busy` announced ------------------------------------------------------


async def test_a_late_hangup_for_a_call_the_driver_released_is_success(hass, fake_transport):
    observer, dahua, go = await _both_ring(hass, fake_transport)
    provider = _provider(hass)
    go.hangup = AsyncMock(side_effect=TransportError("not_implemented", "Phase 1: keepalive/hangup tracking not built yet"))
    # the driver released the call (its audio leg closed): the tracker has ended it already
    observer.runtime_data.call_state.apply_event({"call_id": "gate-1", "state": "ended"})
    assert await provider.hangup("gate-1", "user_hangup") == {"ok": True}
    # a call still live in the tracker keeps the transport's error; a call nobody owns stays unknown_call
    with pytest.raises(TransportError) as err:
        await provider.hangup("door-1", "user_hangup")
    assert err.value.code == "not_implemented"
    with pytest.raises(TransportError) as err:
        await provider.hangup("ghost", "x")
    assert err.value.code == "unknown_call"


async def test_a_hangup_of_a_claimed_but_unanswered_gate_sip_server_call_is_success(hass, fake_transport):
    # DEV-518: the hub ends only a call it answered; a ringing one it reports `not_implemented`. Not an error here.
    from ..door.gate_sip_listen import SERVER_DRIVER
    observer, dahua, go = await _both_ring(hass, fake_transport)
    provider = _provider(hass)
    go.hangup = AsyncMock(side_effect=TransportError("not_implemented", "x"))
    assert observer.runtime_data.call_state.state == "ringing"
    with pytest.raises(TransportError):                                   # other driver: keeps the error
        await provider.hangup("gate-1", "user_hangup")
    hass.config_entries.async_update_entry(observer, data={**observer.data, "driver": SERVER_DRIVER})
    assert await provider.hangup("gate-1", "user_hangup") == {"ok": True}
    assert observer.runtime_data.call_state.state == "ringing"            # nothing was ended


async def test_a_visitor_transition_sends_a_revless_content_changed_once(hass, fake_transport):
    from custom_components.portico.content import get_content_store

    seen: list[int] = []
    store = get_content_store(hass)
    store.add_live_listener(lambda: seen.append(1))
    observer, dahua = await _setup_two(hass)
    go = fake_transport.instances[0]
    go.ring("gate-1", device_id=GATE)
    await hass.async_block_till_done()
    assert len(seen) == 1                      # ringing
    go.ring("gate-1", device_id=GATE)          # a repeated ring is no transition
    await hass.async_block_till_done()
    assert len(seen) == 1
    observer.runtime_data.call_state.apply_event({"call_id": "gate-1", "state": "ended"})
    assert len(seen) == 2                      # ended
