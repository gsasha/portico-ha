# ruff: noqa: F811 - test parameters named after the imported fixtures
"""M7 step 3a-0 (HC-5): the gate and the entrance door can ring AT THE SAME TIME, as the UI part sees it.

Over REAL driver-part doors (the gate: a `gate_sip_observer`; the entrance: a `dahua_sip`), the transport faked as
everywhere (driver_part.py): the `portico/calls/subscribe` snapshots and live events carry the right `door_id`,
`call_id` and `prev_state` for each door when both ring, answer and end interleaved; one door's end never ends the
other's call; a hangup or an answer addressed with the OTHER door's call id is refused/routed correctly; a door that
leaves mid-call goes `ended` + `unavailable` for that door alone; and the push fan-out (the notifier) is per door:
each ring carries its own `door_ref` and `call_id`, an answered/ended of one door never reaches the other's call, and
the per-door ring cap does not couple the two doors.

Nothing here changes production code. What is NOT here: the app's `another_call` handling of a second ring, and any
server-side notion of a phone being busy (there is none yet; 3a adds the roster and the ClaimLedger).
"""
from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock

from homeassistant.setup import async_setup_component

from custom_components.portico.config_flow import DOMAIN
from custom_components.portico.provider import DATA_PROVIDERS, CallTransition

from .driver_part import (  # noqa: F401 - fixtures by name
    DAHUA_HOST,
    DRIVER_DOMAIN,
    GATE_SIP_URL,
    _dahua_entry,
    _observer_entry,
    fake_transport,
    get_provider,
)
from .test_push_notifier import ENTRY, ENTRY_B, _kinds, _phone, _settle, google, rig  # noqa: F401 - fixtures by name
from .test_push_registry import _custom_integrations  # noqa: F401

GATE = GATE_SIP_URL
ENTRANCE = DAHUA_HOST


async def _setup_two_doors(hass):
    """The gate and the entrance (driver part, one hub), then the UI part, the way a restart orders them."""
    observer, dahua = _observer_entry(hass), _dahua_entry(hass)
    assert await hass.config_entries.async_setup(observer.entry_id)      # sets the whole domain up: both entries
    await hass.async_block_till_done()
    assert observer.state.value == dahua.state.value == "loaded"
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    return observer, dahua


def _registry(hass):
    return hass.data[DOMAIN][DATA_PROVIDERS]


async def _subscribe(hass, hass_ws_client):
    client = await hass_ws_client(hass)

    async def recv():
        return await asyncio.wait_for(client.receive_json(), timeout=5)

    await client.send_json({"id": 1, "type": "portico/calls/subscribe"})
    assert (await recv())["success"]

    async def events(n):
        out = []
        for _ in range(n):
            msg = await recv()
            assert msg["type"] == "event", msg
            out.append(msg["event"])
        return out

    return client, recv, events


def _brief(events):
    return [(e["door_id"], e["call_id"], e["state"], e.get("prev_state"), e.get("snapshot", False)) for e in events]


async def test_the_registry_lists_both_doors_and_routes_each_call_to_the_one_provider(hass, fake_transport):
    await _setup_two_doors(hass)
    assert sorted(d.id for d in _registry(hass).doors()) == sorted([GATE, ENTRANCE])
    go = fake_transport.instances[-1]
    go.ring("gate-1", device_id=GATE)
    go.ring("door-1", device_id=ENTRANCE)
    await hass.async_block_till_done()
    assert _registry(hass).for_call("gate-1") is get_provider(hass) is _registry(hass).for_call("door-1")
    assert _registry(hass).for_call("ghost") is None


async def test_the_subscribe_snapshots_name_each_door_idle(hass, hass_ws_client, fake_transport):
    await _setup_two_doors(hass)
    _, _, events = await _subscribe(hass, hass_ws_client)
    got = _brief(await events(2))
    assert sorted(got) == sorted([(GATE, None, "idle", None, True), (ENTRANCE, None, "idle", None, True)])


async def test_the_subscribe_snapshots_carry_each_doors_own_ringing_call(hass, hass_ws_client, fake_transport):
    await _setup_two_doors(hass)
    go = fake_transport.instances[-1]
    go.ring("gate-1", device_id=GATE)
    go.ring("door-1", device_id=ENTRANCE)
    go.state("door-1", "answered")
    await hass.async_block_till_done()
    _, _, events = await _subscribe(hass, hass_ws_client)
    got = _brief(await events(2))
    assert sorted(got) == sorted([(GATE, "gate-1", "ringing", None, True), (ENTRANCE, "door-1", "answered", None, True)])


async def test_interleaved_events_on_the_stream_carry_the_right_door_call_and_previous_state(hass, hass_ws_client, fake_transport):
    await _setup_two_doors(hass)
    go = fake_transport.instances[-1]
    _, _, events = await _subscribe(hass, hass_ws_client)
    await events(2)                                          # the two idle snapshots

    go.ring("gate-1", device_id=GATE)
    go.ring("door-1", device_id=ENTRANCE)
    go.state("door-1", "answered")
    go.state("gate-1", "answered")
    go.state("gate-1", "ended")
    go.state("door-1", "active")
    go.state("door-1", "ended")
    got = _brief(await events(7))
    assert got == [
        (GATE, "gate-1", "ringing", "idle", False),
        (ENTRANCE, "door-1", "ringing", "idle", False),
        (ENTRANCE, "door-1", "answered", "ringing", False),
        (GATE, "gate-1", "answered", "ringing", False),
        (GATE, "gate-1", "ended", "answered", False),         # the gate's end names the gate's call
        (ENTRANCE, "door-1", "active", "answered", False),    # and the entrance's call carried on
        (ENTRANCE, "door-1", "ended", "active", False),
    ]


async def test_a_hangup_by_call_id_over_the_websocket_ends_only_that_doors_call(hass, hass_ws_client, fake_transport, caplog):
    observer, dahua = await _setup_two_doors(hass)
    go = fake_transport.instances[-1]
    go.hangup = AsyncMock(return_value={"ok": True})
    go.ring("gate-1", device_id=GATE)
    go.ring("door-1", device_id=ENTRANCE)
    await hass.async_block_till_done()
    client, recv, events = await _subscribe(hass, hass_ws_client)
    await events(2)

    with caplog.at_level(logging.INFO):
        await client.send_json({"id": 2, "type": "portico/hangup", "call_id": "gate-1", "reason": "user"})
        seen = [await recv(), await recv()]
    assert any(m["id"] == 2 and m["success"] for m in seen), seen
    go.hangup.assert_awaited_once_with("gate-1", "user")
    assert any("hangup requested call_id=gate-1 reason=user" in r.getMessage() for r in caplog.records)
    ended = [m["event"] for m in seen if m["type"] == "event"]
    assert _brief(ended) == [(GATE, "gate-1", "ended", "ringing", False)]
    assert observer.runtime_data.call_state.state == "idle"
    assert (dahua.runtime_data.call_state.call_id, dahua.runtime_data.call_state.state) == ("door-1", "ringing")


async def test_answering_a_door_with_the_other_doors_call_id_is_refused_and_nothing_moves(hass, hass_ws_client, fake_transport):
    observer, dahua = await _setup_two_doors(hass)
    go = fake_transport.instances[-1]
    go.answer = AsyncMock(return_value={})
    go.ring("gate-1", device_id=GATE)
    go.ring("door-1", device_id=ENTRANCE)
    await hass.async_block_till_done()
    client = await hass_ws_client(hass)

    await client.send_json({"id": 1, "type": "portico/answer", "door_id": ENTRANCE, "call_id": "gate-1",
                            "client_kind": "browser"})
    resp = await asyncio.wait_for(client.receive_json(), timeout=5)
    assert resp["success"] is False and resp["error"]["code"] == "not_available"
    go.answer.assert_not_awaited()
    assert observer.runtime_data.call_state.state == dahua.runtime_data.call_state.state == "ringing"

    await client.send_json({"id": 2, "type": "portico/answer", "door_id": GATE, "call_id": "gate-1",
                            "client_kind": "browser"})
    resp = await asyncio.wait_for(client.receive_json(), timeout=5)
    assert resp["success"] is True and resp["result"]["mode"] == "relay"
    assert observer.runtime_data.call_state.state == "answered" and dahua.runtime_data.call_state.state == "ringing"


async def test_one_door_leaving_mid_call_sends_ended_and_unavailable_for_it_alone(hass, hass_ws_client, fake_transport):
    observer, dahua = await _setup_two_doors(hass)
    go = fake_transport.instances[-1]
    go.ring("gate-1", device_id=GATE)
    go.ring("door-1", device_id=ENTRANCE)
    await hass.async_block_till_done()
    _, _, events = await _subscribe(hass, hass_ws_client)
    await events(2)

    assert await hass.config_entries.async_unload(observer.entry_id)
    await hass.async_block_till_done()
    got = _brief(await events(2))
    assert got == [(GATE, "gate-1", "ended", "ringing", False), (GATE, None, "unavailable", None, True)]
    assert _registry(hass).for_call("gate-1") is None
    assert _registry(hass).for_call("door-1") is get_provider(hass)
    go.state("door-1", "answered")
    assert _brief(await events(1)) == [(ENTRANCE, "door-1", "answered", "ringing", False)]


# ---- push fan-out per door (the notifier over two trackers) ---------------------------------------------------------


def _messages(google, reg=None):
    out = []
    for m in google.sent_messages():
        value = m.get("fid") or m.get("token")
        if reg is None or value == reg:
            out.append((m["data"]["kind"], m["data"]["call_id"], m["data"]["door_ref"]))
    return out


async def test_push_two_doors_ringing_at_once_each_ring_carries_its_own_door_ref_and_call(rig):
    phone = await _phone(rig.registry, 1)
    rig.tracker(ENTRY).apply_event({"call_id": "gate-1", "state": "ringing"})
    rig.tracker(ENTRY_B).apply_event({"call_id": "door-1", "state": "ringing"})
    await _settle(rig)
    got = _messages(rig.google, phone["registration"])
    assert sorted(got) == sorted([("ring", "gate-1", ENTRY), ("ring", "door-1", ENTRY_B)])


async def test_push_answer_and_end_of_one_door_never_reach_the_other_doors_call(rig):
    phone = await _phone(rig.registry, 1)
    gate, door = rig.tracker(ENTRY), rig.tracker(ENTRY_B)
    gate.apply_event({"call_id": "gate-1", "state": "ringing"})
    door.apply_event({"call_id": "door-1", "state": "ringing"})
    await _settle(rig)
    gate.apply_event({"call_id": "gate-1", "state": "answered"})
    await _settle(rig)
    door.apply_event({"call_id": "door-1", "state": "ended"})       # the entrance's visitor gives up, unanswered
    await _settle(rig)
    got = _messages(rig.google, phone["registration"])
    assert ("answered", "gate-1", ENTRY) in got and ("ended", "door-1", ENTRY_B) in got
    assert ("ended", "gate-1", ENTRY) not in got and ("answered", "door-1", ENTRY_B) not in got
    assert len(got) == 4
    gate.apply_event({"call_id": "gate-1", "state": "ended"})       # the phone heard `answered`: no `ended` after it
    await _settle(rig)
    assert len(_messages(rig.google, phone["registration"])) == 4


async def test_push_the_per_door_ring_cap_does_not_couple_the_two_doors(rig):
    """A door's rings are limited to one per 5 s; the other door ringing in the same second is not a ring of the
    first and is not skipped by its cap."""
    await _phone(rig.registry, 1)
    gate, door = rig.tracker(ENTRY), rig.tracker(ENTRY_B)
    gate.apply_event({"call_id": "gate-1", "state": "ringing"})
    gate.apply_event({"call_id": "gate-2", "state": "ringing"})     # a second gate ring within 5 s: skipped by the gate's cap
    door.apply_event({"call_id": "door-1", "state": "ringing"})     # the entrance's first ring: sent
    await _settle(rig)
    rings = sorted(call for kind, call in _kinds(rig.google) if kind == "ring")
    assert rings == ["door-1", "gate-1"]
    assert rig.events.skips == ["device_rate"]


async def test_push_a_new_ring_on_one_door_does_not_finish_the_other_doors_call(rig):
    """`ringing` for a new call replaces the tracked call of ITS door only (prev_call_id is per tracker)."""
    phone = await _phone(rig.registry, 1)
    gate, door = rig.tracker(ENTRY), rig.tracker(ENTRY_B)
    door.apply_event({"call_id": "door-1", "state": "ringing"})
    await _settle(rig)
    rig.notifier._device_rings.clear()
    gate.apply_event({"call_id": "gate-1", "state": "ringing"})
    await _settle(rig)
    door.apply_event({"call_id": "door-1", "state": "answered"})
    await _settle(rig)
    got = _messages(rig.google, phone["registration"])
    assert ("ended", "door-1", ENTRY_B) not in got
    assert ("answered", "door-1", ENTRY_B) in got and ("ring", "gate-1", ENTRY) in got
