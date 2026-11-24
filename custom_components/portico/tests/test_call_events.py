# ruff: noqa: F811 - test parameters named after the imported fixtures
"""Every ring and call as an HA event (DEV-811 P4): `portico_call`, `portico_call_stats`, `sensor.portico_calls` and
`binary_sensor.portico_call_active`. Fakes only: the fake provider's transitions, the house env, no network, no wheel."""
from __future__ import annotations

import logging
import time
from datetime import timedelta

import pytest
from homeassistant.core import callback
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_capture_events, async_fire_time_changed

from ..call_events import (
    EVENT_CALL,
    EVENT_CALL_STATS,
    PRUNE_INTERVAL_S,
    STATS_DELAY_S,
    CallEvents,
    add_stats_part,
    async_setup_call_events,
    get_call_events,
)
from ..house import REASON_NO_ANSWER
from ..provider import CallTransition
from .house_env import build_house
from .test_endpoints import _provide
from .test_push_registry import (  # noqa: F401 - fixtures by name
    _custom_integrations,
    _setup_all,
    fake_transport,
    google,
)

pytestmark = pytest.mark.asyncio


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _rig(hass, *, ring_only=False):
    """The fake provider with one door `gate-1` named Gate, and a CallEvents on a clock the test moves."""
    caps = {"InboundAudio": 3, "OutboundAudio": 3} if ring_only else {"HasRelay": True}
    fake = _provide(hass, "fake", "gate-1", name="Gate", capabilities=caps)
    clock = Clock()
    events = CallEvents(hass, clock=clock)
    events.start()
    hass.data["portico"]["call_events"] = events
    return fake, events, clock


def _phases(captured, call_id=None):
    return [e.data["phase"] for e in captured if call_id is None or e.data["call_id"] == call_id]


def _ended(captured, call_id):
    (ended,) = [e.data for e in captured if e.data["call_id"] == call_id and e.data["phase"] == "ended"]
    return ended


async def test_a_door_call_fires_one_event_per_phase_and_one_end(hass):
    fake, events, _clock = _rig(hass)
    got = async_capture_events(hass, EVENT_CALL)
    fake.ring("gate-1", "c1")
    fake.state("c1", "ringing")                       # a repeated ringing is a normal tracker transition: not a second event
    fake.state("c1", "answered")
    fake.state("c1", "active")
    fake.state("c1", "ended")
    fake.state("c1", "ended")                         # nor a second end
    await hass.async_block_till_done()
    assert _phases(got) == ["ringing", "answered", "active", "ended"]
    first = got[0].data
    assert first["kind"] == "door" and first["direction"] == "in" and first["door_id"] == "gate-1"
    assert first["door_name"] == "Gate" and first["call_id"] == "c1" and first["at"].endswith("+00:00")
    ended = _ended(got, "c1")
    assert ended["outcome"] == "answered" and "talk_s" in ended and "ring_s" in ended
    for event in got:                                 # omitted, never null: no field without a source is present
        assert None not in event.data.values()
        assert not {"path", "media", "codec", "from", "to", "answered_by"} & set(event.data)


async def test_a_ring_nobody_answers_is_an_event_and_ends_missed(hass):
    fake, _events, _clock = _rig(hass)
    got = async_capture_events(hass, EVENT_CALL)
    fake.ring("gate-1", "c1")
    fake.state("c1", "ended")
    await hass.async_block_till_done()
    assert _phases(got) == ["ringing", "ended"]
    ended = _ended(got, "c1")
    assert ended["outcome"] == "missed" and "talk_s" not in ended


async def test_a_ring_only_door_can_only_have_rung(hass):
    fake, _events, _clock = _rig(hass, ring_only=True)
    got = async_capture_events(hass, EVENT_CALL)
    fake.ring("gate-1", "c1")
    fake.state("c1", "ended")
    await hass.async_block_till_done()
    assert _ended(got, "c1")["outcome"] == "rang"


async def test_id_less_idle_and_unseen_transitions_fire_nothing(hass):
    _fake, events, _clock = _rig(hass)
    got = async_capture_events(hass, EVENT_CALL)
    now = time.time()
    events.on_door_transition(CallTransition("gate-1", None, "idle", "ringing", None, now))        # no call id
    events.on_door_transition(CallTransition("gate-1", "c9", "ringing", "ended", "c9", now))       # never saw it start
    events.on_door_transition(CallTransition("gate-1", "c9", "idle", "ended", None, now))          # ended after idle
    events.on_door_transition(CallTransition("gate-1", None, "idle", "unavailable", None, now))    # a door leaving
    await hass.async_block_till_done()
    assert got == [] and events.state == "idle"


async def test_a_replaced_ring_ends_the_old_call_superseded_and_a_late_event_does_not_close_the_new_one(hass):
    fake, events, _clock = _rig(hass)
    got = async_capture_events(hass, EVENT_CALL)
    fake.ring("gate-1", "c1")
    fake.ring("gate-1", "c2")                          # prev_call_id c1: the tracker replaced it
    await hass.async_block_till_done()
    assert _ended(got, "c1")["outcome"] == "superseded"
    assert events.state == "ringing" and [c["call_id"] for c in events.live_calls()] == ["c2"]
    # the superseded call's own late end (and a late ring) must not touch c2
    now = time.time()
    events.on_door_transition(CallTransition("gate-1", "c1", "ringing", "ended", "c1", now))
    events.on_door_transition(CallTransition("gate-1", "c1", "ringing", "ringing", "c1", now))
    await hass.async_block_till_done()
    assert _phases(got, "c1") == ["ringing", "ended"] and _phases(got, "c2") == ["ringing"]
    assert events.state == "ringing"


async def test_outbound_phases_and_outcome(hass):
    fake, events, _clock = _rig(hass)
    got = async_capture_events(hass, EVENT_CALL)
    fake.outbound("gate-1", "out-1", "dialing")
    assert events.state == "idle"                       # dialing is not a ring
    fake.outbound("gate-1", "out-1", "in_call", "dialing")
    assert events.state == "talking"
    fake.outbound("gate-1", "out-1", "ended", "in_call")
    fake.outbound("gate-1", "out-2", "dialing")
    fake.outbound("gate-1", "out-2", "ended", "dialing")
    await hass.async_block_till_done()
    assert _phases(got, "out-1") == ["ringing", "active", "ended"]
    assert got[0].data["kind"] == "outbound" and got[0].data["direction"] == "out"
    assert _ended(got, "out-1")["outcome"] == "answered" and _ended(got, "out-2")["outcome"] == "missed"
    assert events.state == "idle"


async def test_the_sensor_states(hass):
    fake, events, _clock = _rig(hass)
    assert events.state == "idle"
    fake.ring("gate-1", "c1")                           # the first ring: set built from the event's own id
    assert events.state == "ringing"
    fake.state("c1", "answered")
    assert events.state == "talking"
    fake.state("c1", "ended")
    assert events.state == "idle"
    assert events.last_call["outcome"] == "answered" and "phase" not in events.last_call


async def test_the_pruner_asks_the_source_and_never_drops_a_call_it_still_holds(hass):
    fake, events, clock = _rig(hass)
    got = async_capture_events(hass, EVENT_CALL)
    fake.ring("gate-1", "c1")
    fake.state("c1", "answered")
    clock.now += 11 * 60                                # a call longer than 10 minutes, silent: still held by its source
    assert events.prune() == [] and events.state == "talking"
    # an outbound call silent for 5 minutes: the source holds it under `outbound_call_id`
    fake.outbound("gate-1", "out-1", "dialing")
    fake.outbound("gate-1", "out-1", "in_call", "dialing")
    fake._states["gate-1"]["outbound_call_id"] = "out-1"
    clock.now += 5 * 60
    assert events.prune() == []
    # the source no longer holds c1: a recent event protects it, an old one does not
    fake._states["gate-1"].update(call_id=None, state="idle")
    clock.now += 1
    events._live["c1"].last_mono = clock.now - (PRUNE_INTERVAL_S - 1)
    assert "c1" not in events.prune()
    clock.now += 5
    assert events.prune() == ["c1"]
    await hass.async_block_till_done()
    assert _ended(got, "c1")["outcome"] == "lost"
    assert events.state == "talking"                    # the outbound call is still there
    fake._states["gate-1"].pop("outbound_call_id")      # and now its source lets go too
    clock.now += 61
    assert events.prune() == ["out-1"] and events.state == "idle"


async def test_a_raising_bus_listener_does_not_stop_the_call_path(hass):
    fake, events, _clock = _rig(hass)

    @callback
    def _boom(_event):
        raise RuntimeError("listener failed")

    hass.bus.async_listen(EVENT_CALL, _boom)
    got = async_capture_events(hass, EVENT_CALL)
    fake.ring("gate-1", "c1")
    fake.state("c1", "ended")
    await hass.async_block_till_done()
    assert _phases(got) == ["ringing", "ended"] and events.state == "idle"


async def test_the_logbook_line_is_written_once_per_ended_call(hass):
    fake, _events, _clock = _rig(hass)
    log = async_capture_events(hass, "logbook_entry")
    fake.ring("gate-1", "c1")
    fake.state("c1", "answered")
    await hass.async_block_till_done()
    assert log == []
    fake.state("c1", "ended")
    await hass.async_block_till_done()
    (line,) = log
    assert line.data["name"] == "Portico call" and line.data["message"].startswith("Gate: answered")


async def test_stats_fire_five_seconds_after_ended_only_when_parts_arrived(hass):
    fake, _events, _clock = _rig(hass)
    got = async_capture_events(hass, EVENT_CALL_STATS)
    fake.ring("gate-1", "c1")
    fake.state("c1", "ended")                           # no parts for c1: nothing, ever
    fake.ring("gate-1", "c2")
    fake.state("c2", "answered")
    fake.state("c2", "ended")
    add_stats_part(hass, "c2", {"uplink_frames": 120})
    add_stats_part(hass, "c2", {"downlink_frames": 118})
    await hass.async_block_till_done()
    assert got == []                                    # `ended` never waits for it, and the delay has not passed
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=STATS_DELAY_S + 1))
    await hass.async_block_till_done()
    (event,) = got
    assert event.data == {"call_id": "c2", "uplink_frames": 120, "downlink_frames": 118, "door_id": "gate-1"}


async def test_the_stats_event_has_a_logbook_description(hass):
    from ..logbook import async_describe_events

    described = {}
    async_describe_events(hass, lambda domain, event_type, fn: described.update({event_type: (domain, fn)}))
    domain, fn = described[EVENT_CALL_STATS]
    from homeassistant.core import Event

    out = fn(Event(EVENT_CALL_STATS, {"call_id": "c2", "uplink_frames": 120}))
    assert domain == "portico" and out["message"] == "audio numbers: uplink_frames 120"
    assert EVENT_CALL not in described                  # one line per ended call is written by the helper, not four rows


# ---- house calls: only the call-level moments ---------------------------------------------------------------------


async def test_house_calls_fire_call_level_events_only(hass, hass_storage):
    house = await build_house(hass, hass_storage)
    events = async_setup_call_events(hass)
    got = async_capture_events(hass, EVENT_CALL)
    house.online("alice")
    house.online("dina")
    house.online("kitchen")
    # "Everyone" from the kitchen rings both phones; the first answer wins, the loser's end is per endpoint: no `ended`
    result = house.everyone("kitchen")
    assert result.ok
    await hass.async_block_till_done()
    assert _phases(got) == ["ringing"] and got[0].data["kind"] == "house"
    assert got[0].data["from"] == "Kitchen" and got[0].data["to"] == "everyone" and "direction" not in got[0].data
    assert events.state == "ringing"
    reply, code = house.registry.answer(result.call_id, house.user("alice"))
    assert code is None
    await hass.async_block_till_done()
    assert _phases(got) == ["ringing", "answered"] and got[1].data["answered_by"] == "Alice"
    assert events.state == "talking"
    assert house.registry.mark_active(result.call_id)
    ok, code = house.registry.hangup(result.call_id, house.user("alice"))
    await hass.async_block_till_done()
    assert _phases(got) == ["ringing", "answered", "active", "ended"]
    ended = _ended(got, result.call_id)
    assert ended["outcome"] == "hangup" and "end_reason" not in ended and events.state == "idle"


async def test_a_group_decline_does_not_end_the_house_call_and_a_ring_timeout_ends_no_answer(hass, hass_storage):
    house = await build_house(hass, hass_storage)
    events = async_setup_call_events(hass)
    got = async_capture_events(hass, EVENT_CALL)
    for endpoint in ("alice", "dina", "kitchen"):
        house.online(endpoint)
    result = house.everyone("kitchen")
    ok, code = house.registry.decline(result.call_id, house.user("alice"))   # one member declined: per-endpoint only
    assert ok
    await hass.async_block_till_done()
    assert _phases(got) == ["ringing"] and events.state == "ringing"
    await house.advance(60)
    assert _ended(got, result.call_id)["outcome"] == REASON_NO_ANSWER and events.state == "idle"


async def test_the_house_pruner_drops_a_call_the_registry_no_longer_holds(hass, hass_storage):
    house = await build_house(hass, hass_storage)
    events = async_setup_call_events(hass)
    house.online("alice")
    house.online("kitchen")
    result = house.dial("kitchen", "alice")
    assert result.ok and events.state == "ringing"
    house.registry._calls.clear()                       # the registry lost it without an end
    events._clock = lambda: time.monotonic() + 3600
    assert events.prune() == [result.call_id] and events.state == "idle"


# ---- the entities, through a real settings entry ----------------------------------------------------------------------


async def test_the_entities_follow_a_real_door_call(hass, hass_storage, google, fake_transport):
    await _setup_all(hass, hass_storage, google)
    sensor, binary = "sensor.portico_calls", "binary_sensor.portico_call_active"
    assert hass.states.get(sensor).state == "idle" and hass.states.get(binary).state == "off"
    assert get_call_events(hass) is not None
    got = async_capture_events(hass, EVENT_CALL)
    transport = fake_transport.instances[-1]
    transport.ring("c1")
    await hass.async_block_till_done()
    assert hass.states.get(sensor).state == "ringing" and hass.states.get(binary).state == "on"
    assert hass.states.get(sensor).attributes["calls"][0]["call_id"] == "c1"
    transport.state("c1", "answered")
    await hass.async_block_till_done()
    assert hass.states.get(sensor).state == "talking"
    transport.state("c1", "ended")
    await hass.async_block_till_done()
    assert hass.states.get(sensor).state == "idle" and hass.states.get(binary).state == "off"
    assert hass.states.get(sensor).attributes["last_call"]["outcome"] == "answered"
    assert _phases(got) == ["ringing", "answered", "ended"]
