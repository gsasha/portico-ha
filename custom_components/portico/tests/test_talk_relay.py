# ruff: noqa: F811 - test parameters named after the imported fixtures
"""The Talk relay (M7 P3-B, `portico_house_calls_design.md` 15.15 (d)): `talk_state` to the phone that placed a Talk call.

Everything here is INERT on a real install: no door reports `CanDial`, so a Talk press is refused before it reaches
the relay and nothing is ever placed. A FAKE door that does report it stands in, over a real websocket as the phones.
What is pinned:

- the sequence the placing phone sees on `portico/calls/subscribe`: `dialing` (call_id null: the id does not exist yet),
  `in_call`, `ended` (no reason when the call simply ends); a dial that fails ends with the error's code, as one of the
  reasons the app maps (busy, not_connected, failed, not_available, no_answer, timeout), and no call id;
- nobody else sees any of it: not a second phone, not a dashboard login, not an administrator; a visitor ring during
  the talk still reaches everyone as `call_state_changed`; an outbound call nobody here placed is relayed to no one;
- the races: ended before dialing, an end for another call, a repeated or regressing state, the placing phone leaving
  mid-call (hung up after the grace; back inside the grace keeps the call), leaving before the dial returns, a cancelled
  dial, the watchdog;
- the real driver part: an outbound change reaches the relay through the provider seam and touches no visitor state, no
  sensor input, no bus event.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from .. import talk_relay
from ..config_flow import DOMAIN
from ..provider import DATA_PROVIDERS
from ..talk_relay import EVENT_TYPE, get_talk_relay
from .driver_part import _add_device
from .fake_provider import FakeProvider
from .test_content_wire_blocks import _doc4, _entrance, _fake, _garden, _save
from .test_editor_commands import PHONE_ID, _client, world  # noqa: F401 - fixtures by name
from .test_push_registry import _phone_user
from .test_talk_gate import DOOR, _arm, _ready

pytestmark = pytest.mark.asyncio

CALL = "out-0123456789abcdef"
GATE = "gate-1"


# ---- helpers ---------------------------------------------------------------------------------------------------------

def _next(client):
    """HA wants strictly increasing message ids per connection."""
    client._mid = getattr(client, "_mid", 0) + 1
    return client._mid


async def _barrier(client):
    """Everything the server queued for this connection before now: send a command, read up to its result."""
    msg_id = _next(client)
    await client.send_json({"id": msg_id, "type": "portico/version"})
    events = []
    while True:
        frame = await asyncio.wait_for(client.receive_json(), 5)
        if frame.get("id") == msg_id and frame.get("type") == "result":
            return events
        events.append(frame)


async def _subscribe(client):
    await client.send_json({"id": _next(client), "type": "portico/calls/subscribe"})
    first = await asyncio.wait_for(client.receive_json(), 5)
    assert first["type"] == "result" and first["success"], first
    return await _barrier(client)      # the door snapshots


async def _call(client, type_, _unused=None, **body):
    """Send one command; (its result frame, the events that arrived before it)."""
    msg_id = _next(client)
    await client.send_json({"id": msg_id, "type": type_, **body})
    events = []
    while True:
        frame = await asyncio.wait_for(client.receive_json(), 5)
        if frame.get("id") == msg_id and frame.get("type") == "result":
            return frame, events
        events.append(frame)


def _talks(frames):
    return [f["event"] for f in frames if f.get("type") == "event" and f["event"].get("type") == EVENT_TYPE]


def _states(frames):
    return [(e["state"], e["call_id"]) for e in _talks(frames)]


def _script_dial(world, *, steps=("in_call",), call_id=CALL, gate=None):
    """The fake door's dial as the real hub does it: the driver's transitions for the minted id first (the door
    answered), then the reply carrying the id. `gate` (an asyncio.Event) holds the reply back."""
    fake = _fake(world)

    async def dial(door_id, host, port):
        fake._record("dial", door_id, host, port)
        fake._owner[call_id] = door_id
        for state in steps:
            fake.outbound(door_id, call_id, state)
        if gate is not None:
            await gate.wait()
        return {"ok": True, "call_id": call_id}

    fake.dial = dial
    return fake


async def _second_phone(world, hass_ws_client, name="Ana", enroll_id="ep-ana"):
    user, token = await _phone_user(world.hass, name=f"{name} phone user")
    (entry,) = [e for e in world.hass.config_entries.async_entries(DOMAIN) if e.data.get("enrolled_phones")]
    world.hass.config_entries.async_update_entry(entry, data={
        **entry.data, "enrolled_phones": [*entry.data["enrolled_phones"],
                                          {"ha_user_id": user.id, "name": name, "enroll_id": enroll_id}]})
    return await hass_ws_client(world.hass, access_token=token), user


async def _press(client, _unused, world):
    world.store._last_run.clear()
    return await _call(client, "portico/content/run", action_id="talk")


async def _world_ready(world, hass_ws_client, **arm):
    await _ready(world, **arm)
    phone = await _client(world, hass_ws_client, "phone")
    await _subscribe(phone)
    relay = get_talk_relay(world.hass)
    return phone, relay


# ---- the placing phone's own stream -----------------------------------------------------------------------------------

async def test_the_placing_phone_hears_dialing_in_call_and_ended_in_order(world, hass_ws_client):
    phone, relay = await _world_ready(world, hass_ws_client)
    _script_dial(world)
    result, events = await _press(phone, 20, world)
    assert result["success"], result
    assert result["result"]["call_id"] == CALL
    # `dialing` before anything: the call id does not exist yet (the dial blocks until the door answers)
    assert _states(events) == [("dialing", None), ("in_call", CALL)]
    first = _talks(events)[0]
    assert first == {"type": "talk_state", "door_id": DOOR.split(":", 1)[1], "call_id": None, "state": "dialing"}
    _fake(world).outbound(GATE, CALL, "ended", "in_call")
    later = await _barrier(phone)
    assert _talks(later) == [{"type": "talk_state", "door_id": GATE, "call_id": CALL, "state": "ended"}]
    assert relay._by_door == {}


async def test_a_call_that_simply_ends_has_no_reason_and_the_other_states_have_none(world, hass_ws_client):
    phone, _relay = await _world_ready(world, hass_ws_client)
    _script_dial(world)
    _result, events = await _press(phone, 20, world)
    assert all("reason" not in e for e in _talks(events))
    _fake(world).outbound(GATE, CALL, "ended", "in_call")
    (ended,) = _talks(await _barrier(phone))
    assert "reason" not in ended


async def test_a_second_phone_on_the_same_door_sees_nothing_at_all(world, hass_ws_client):
    phone, _relay = await _world_ready(world, hass_ws_client)
    other, _user = await _second_phone(world, hass_ws_client)
    await _subscribe(other)
    _script_dial(world)
    await _press(phone, 20, world)
    _fake(world).outbound(GATE, CALL, "ended", "in_call")
    assert await _barrier(other) == []       # not a talk_state and not a call_state_changed: nothing
    assert _states(await _barrier(phone)) == [("ended", CALL)]


async def test_a_dashboard_login_and_an_administrator_see_nothing(world, hass_ws_client):
    phone, _relay = await _world_ready(world, hass_ws_client)
    tablet = await _client(world, hass_ws_client, "tablet")
    admin = await _client(world, hass_ws_client, "admin")
    await _subscribe(tablet)
    await _subscribe(admin)
    _script_dial(world)
    await _press(phone, 20, world)
    _fake(world).outbound(GATE, CALL, "ended", "in_call")
    assert await _barrier(tablet) == [] and await _barrier(admin) == []


async def test_two_streams_of_the_placing_phone_both_hear_it(world, hass_ws_client):
    phone, _relay = await _world_ready(world, hass_ws_client)
    again = await _client(world, hass_ws_client, "phone")           # the same phone user, a second connection
    await _subscribe(again)
    _script_dial(world)
    await _press(phone, 20, world)
    assert _states(await _barrier(again)) == [("dialing", None), ("in_call", CALL)]


async def test_a_visitor_ring_during_a_talk_still_reaches_everyone_and_is_not_a_talk_state(world, hass_ws_client):
    phone, _relay = await _world_ready(world, hass_ws_client)
    other, _user = await _second_phone(world, hass_ws_client)
    await _subscribe(other)
    _script_dial(world)
    await _press(phone, 20, world)
    await _barrier(phone)
    _fake(world).ring(GATE, "visitor-1")             # the bell, as today
    for client in (phone, other):
        frames = await _barrier(client)
        assert _talks(frames) == []
        (event,) = [f["event"] for f in frames if f["event"]["type"] == "call_state_changed"]
        assert (event["call_id"], event["state"]) == ("visitor-1", "ringing")


async def test_an_outbound_call_nobody_here_placed_is_relayed_to_no_one(world, hass_ws_client):
    """The old app's dial, `portico/dial`, another integration: an `out-` id with no Talk placement is nobody's to tell."""
    phone, relay = await _world_ready(world, hass_ws_client)
    other, _user = await _second_phone(world, hass_ws_client)
    await _subscribe(other)
    fake = _fake(world)
    for state in ("dialing", "in_call", "ended"):
        fake.outbound(GATE, "out-someone-else", state)
    assert await _barrier(phone) == [] and await _barrier(other) == []
    assert relay._by_door == {}


async def test_today_nothing_changes_a_door_without_candial_refuses_and_the_relay_holds_nothing(world, hass_ws_client):
    await _save(world, _doc4(_entrance(), _garden()))          # the door reports no CanDial, as every door today
    phone = await _client(world, hass_ws_client, "phone")
    await _subscribe(phone)
    relay = get_talk_relay(world.hass)
    result, events = await _press(phone, 20, world)
    assert result["success"] is False and result["error"]["code"] == "not_available"
    assert events == [] and relay._by_door == {} and _fake(world).calls_to("dial") == []
    assert await _barrier(phone) == []


# ---- a dial that fails ------------------------------------------------------------------------------------------------------

class _Refused(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code
        self.message = f"the gate said {code}"


async def test_a_refused_dial_ends_with_the_codes_reason_and_no_call_id(world, hass_ws_client):
    phone, relay = await _world_ready(world, hass_ws_client)
    _fake(world).raise_on["dial"] = _Refused("busy")
    result, events = await _press(phone, 20, world)
    assert result["success"] is False and result["error"]["code"] == "busy"
    assert _talks(events) == [
        {"type": "talk_state", "door_id": GATE, "call_id": None, "state": "dialing"},
        {"type": "talk_state", "door_id": GATE, "call_id": None, "state": "ended", "reason": "busy"}]
    assert relay._by_door == {}


async def test_a_dial_that_raises_without_a_code_ends_failed(world, hass_ws_client):
    phone, _relay = await _world_ready(world, hass_ws_client)
    _fake(world).raise_on["dial"] = RuntimeError("boom")
    _result, events = await _press(phone, 20, world)
    assert _talks(events)[-1]["reason"] == "failed"


async def test_a_dial_with_no_call_id_ends_failed(world, hass_ws_client):
    phone, _relay = await _world_ready(world, hass_ws_client)

    async def dial(door_id, host, port):
        return {"ok": True}

    _fake(world).dial = dial
    result, events = await _press(phone, 20, world)
    assert result["success"] is False and result["error"]["code"] == "failed"
    assert [(e["state"], e.get("reason")) for e in _talks(events)] == [("dialing", None), ("ended", "failed")]


async def test_after_a_failed_dial_the_next_press_starts_clean(world, hass_ws_client):
    phone, relay = await _world_ready(world, hass_ws_client)
    fake = _fake(world)
    fake.raise_on["dial"] = _Refused("not_connected")
    await _press(phone, 20, world)
    del fake.raise_on["dial"]
    _script_dial(world)
    result, events = await _press(phone, 21, world)
    assert result["success"] and _states(events) == [("dialing", None), ("in_call", CALL)]
    assert list(relay._by_door) == [("fake", GATE)]


async def test_a_cancelled_dial_tells_the_phone_and_forgets_the_placement(world, hass_ws_client):
    phone, relay = await _world_ready(world, hass_ws_client)
    started = asyncio.Event()

    async def dial(door_id, host, port):
        started.set()
        await asyncio.sleep(60)

    _fake(world).dial = dial
    task = asyncio.ensure_future(world.store.async_run(
        world.phone.id, "S", "talk", caller_user_id=world.phone.id, endpoint_id=PHONE_ID))
    await started.wait()
    assert list(relay._by_door) == [("fake", GATE)]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    frames = await _barrier(phone)
    assert [(e["state"], e.get("reason")) for e in _talks(frames)] == [("dialing", None), ("ended", "failed")]        # a cancel is `failed` on the wire: only the app's known reasons are sent
    assert relay._by_door == {}


# ---- races: what the tracker can say out of order ------------------------------------------------------------------------------

async def test_an_end_before_dialing_is_not_ours_and_does_not_bind_the_placement(world, hass_ws_client):
    """An `ended` for an earlier call (the tracker passes one with no call tracked) arrives while the dial is pending:
    the phone is not told it, and the placement still follows the real call afterwards."""
    phone, _relay = await _world_ready(world, hass_ws_client)
    gate = asyncio.Event()
    _script_dial(world, steps=(), gate=gate)
    fake = _fake(world)
    store = world.store
    store._last_run.clear()
    task = asyncio.ensure_future(_call(phone, "portico/content/run", action_id="talk"))
    await asyncio.sleep(0.05)
    fake.outbound(GATE, "out-earlier-call", "ended")
    fake.outbound(GATE, CALL, "in_call", "dialing")
    gate.set()
    result, events = await task
    assert result["success"]
    assert _states(events) == [("dialing", None), ("in_call", CALL)]


async def test_an_end_for_another_call_while_ours_is_going_is_ignored(world, hass_ws_client):
    phone, relay = await _world_ready(world, hass_ws_client)
    _script_dial(world)
    await _press(phone, 20, world)
    fake = _fake(world)
    fake.outbound(GATE, "out-other", "ended")
    fake.outbound(GATE, "out-other", "in_call")
    assert _talks(await _barrier(phone)) == []
    assert list(relay._by_door) == [("fake", GATE)]
    fake.outbound(GATE, CALL, "ended", "in_call")
    assert _states(await _barrier(phone)) == [("ended", CALL)]


async def test_a_repeated_or_regressing_state_is_not_sent_twice(world, hass_ws_client):
    phone, _relay = await _world_ready(world, hass_ws_client)
    _script_dial(world, steps=("dialing", "dialing", "in_call", "in_call", "dialing"))
    _result, events = await _press(phone, 20, world)
    # the synthesised `dialing` stands for the driver's own; a driver that does report one adds nothing; no step back
    assert _states(events) == [("dialing", None), ("in_call", CALL)]


async def test_nothing_is_sent_after_the_end(world, hass_ws_client):
    phone, relay = await _world_ready(world, hass_ws_client)
    _script_dial(world)
    await _press(phone, 20, world)
    fake = _fake(world)
    fake.outbound(GATE, CALL, "ended", "in_call")
    fake.outbound(GATE, CALL, "in_call")
    fake.outbound(GATE, CALL, "ended")
    assert _states(await _barrier(phone)) == [("ended", CALL)]
    assert relay._by_door == {}


async def test_the_end_arriving_before_the_dial_returns_finishes_the_placement(world, hass_ws_client):
    """The door answered and hung up before the reply was read: the phone hears in_call then ended, and the late
    `placed` finds nothing to do (no hang-up of a call that is over)."""
    phone, relay = await _world_ready(world, hass_ws_client)
    _script_dial(world, steps=("in_call", "ended"))
    result, events = await _press(phone, 20, world)
    assert result["success"]
    assert _states(events) == [("dialing", None), ("in_call", CALL), ("ended", CALL)]
    assert relay._by_door == {}
    await asyncio.sleep(0.05)
    assert _fake(world).calls_to("hangup") == []


# ---- the placing phone leaving ---------------------------------------------------------------------------------------------------

async def test_the_placing_phone_leaving_mid_call_hangs_the_call_up_after_the_grace(world, hass_ws_client):
    phone, relay = await _world_ready(world, hass_ws_client)
    relay._drop_grace = 0.1
    _script_dial(world)
    await _press(phone, 20, world)
    await phone.close()
    await world.hass.async_block_till_done()
    assert _fake(world).calls_to("hangup") == []                    # not at once: the app may be back inside the grace
    await asyncio.sleep(0.4)
    await world.hass.async_block_till_done()
    assert _fake(world).calls_to("hangup") == [((CALL, "link_lost"), {})]


async def test_the_phone_back_inside_the_grace_keeps_its_call(world, hass_ws_client):
    phone, relay = await _world_ready(world, hass_ws_client)
    relay._drop_grace = 0.3
    _script_dial(world)
    await _press(phone, 20, world)
    await phone.close()
    await world.hass.async_block_till_done()
    back = await _client(world, hass_ws_client, "phone")
    await _subscribe(back)
    await asyncio.sleep(0.6)
    assert _fake(world).calls_to("hangup") == []
    _fake(world).outbound(GATE, CALL, "ended", "in_call")
    assert _states(await _barrier(back)) == [("ended", CALL)]      # and the new stream hears the rest


async def test_coming_back_cancels_the_pending_hangup_and_a_stream_check_guards_it_again(world, hass_ws_client):
    phone, relay = await _world_ready(world, hass_ws_client)
    relay._drop_grace = 5
    _script_dial(world)
    await _press(phone, 20, world)
    (placement,) = relay._by_door.values()
    assert placement.drop_timer is None
    await phone.close()
    await world.hass.async_block_till_done()
    assert placement.drop_timer is not None                         # scheduled, 5 s away
    back = await _client(world, hass_ws_client, "phone")
    await _subscribe(back)
    assert placement.drop_timer is None                             # the phone is back: the hang-up is cancelled, not just late
    # and if a timer ever did fire with a stream open, it still hangs nothing up
    relay._drop(placement)
    await world.hass.async_block_till_done()
    assert _fake(world).calls_to("hangup") == []


async def test_another_phone_staying_connected_does_not_keep_the_call_alive(world, hass_ws_client):
    phone, relay = await _world_ready(world, hass_ws_client)
    other, _user = await _second_phone(world, hass_ws_client)
    await _subscribe(other)
    relay._drop_grace = 0.1
    _script_dial(world)
    await _press(phone, 20, world)
    await phone.close()
    await asyncio.sleep(0.4)
    await world.hass.async_block_till_done()
    assert len(_fake(world).calls_to("hangup")) == 1


async def test_the_phone_leaving_before_the_dial_returns_hangs_up_once_the_id_is_known(world, hass_ws_client):
    phone, relay = await _world_ready(world, hass_ws_client)
    relay._drop_grace = 0.1
    gate = asyncio.Event()
    _script_dial(world, steps=(), gate=gate)
    world.store._last_run.clear()
    await phone.send_json({"id": _next(phone), "type": "portico/content/run", "action_id": "talk"})
    await asyncio.sleep(0.05)
    await phone.close()
    await asyncio.sleep(0.4)                       # the grace ran out with no id to hang up
    assert _fake(world).calls_to("hangup") == []
    gate.set()
    await asyncio.sleep(0.1)
    await world.hass.async_block_till_done()
    assert _fake(world).calls_to("hangup") == [((CALL, "link_lost"), {})]


async def test_a_phone_that_never_subscribed_has_its_call_hung_up_after_the_grace(world, hass_ws_client):
    await _ready(world)
    relay = get_talk_relay(world.hass)
    relay._drop_grace = 0.1
    _script_dial(world)
    phone = await _client(world, hass_ws_client, "phone")           # no calls/subscribe: nobody could ever hear the call
    await _press(phone, 20, world)
    await asyncio.sleep(0.4)
    await world.hass.async_block_till_done()
    assert _fake(world).calls_to("hangup") == [((CALL, "link_lost"), {})]


async def test_a_failing_hangup_is_swallowed(world, hass_ws_client):
    phone, relay = await _world_ready(world, hass_ws_client)
    relay._drop_grace = 0.05
    _script_dial(world)
    await _press(phone, 20, world)
    _fake(world).raise_on["hangup"] = _Refused("unknown_call")
    await phone.close()
    await asyncio.sleep(0.3)
    await world.hass.async_block_till_done()
    assert relay._by_door and len(_fake(world).calls_to("hangup")) == 1       # tried once; nothing raised into the loop


async def test_the_watchdog_ends_a_call_nothing_ended(world, hass_ws_client):
    phone, relay = await _world_ready(world, hass_ws_client)
    relay._max_seconds = 0.1
    _script_dial(world)
    await _press(phone, 20, world)
    # the timer was armed at placing with the old value; re-arm it the way a short-lived install would have
    for placement in relay._by_door.values():
        for timer in placement.timers:
            timer.cancel()
        placement.timers[:] = [world.hass.loop.call_later(0.1, relay._expire, placement)]
    await asyncio.sleep(0.4)
    await world.hass.async_block_till_done()
    frames = await _barrier(phone)
    assert [(e["state"], e.get("reason")) for e in _talks(frames)] == [("ended", "timeout")]
    assert relay._by_door == {} and _fake(world).calls_to("hangup") == [((CALL, "link_lost"), {})]


# ---- the seam and the relay itself --------------------------------------------------------------------------------------------------

async def test_a_connection_with_no_user_is_nobodys_phone(world):
    relay = get_talk_relay(world.hass)
    assert relay.attach(SimpleNamespace(user=None), 1) is None
    assert relay.attach(SimpleNamespace(), 2) is None
    assert relay._streams == []


async def test_a_dead_socket_does_not_stop_the_call_or_the_other_streams(world, hass_ws_client):
    phone, relay = await _world_ready(world, hass_ws_client)
    broken = SimpleNamespace(user=SimpleNamespace(id=world.phone.id),
                             send_event=lambda *a: (_ for _ in ()).throw(RuntimeError("closed")))
    relay.attach(broken, 77)
    _script_dial(world)
    result, events = await _press(phone, 20, world)
    assert result["success"] and _states(events) == [("dialing", None), ("in_call", CALL)]


async def test_the_registry_drops_a_bad_outbound_transition_and_a_provider_without_the_method_is_unchanged(world):
    registry = world.hass.data[DOMAIN][DATA_PROVIDERS]
    seen = []
    unsubscribe = registry.subscribe_outbound(seen.append)
    fake = _fake(world)
    fake.outbound(GATE, CALL, "ringing")                   # not an outbound state
    fake.outbound(GATE, "", "in_call")                     # no id
    fake._outbound_listeners[0]({"door_id": GATE})         # a missing key
    assert seen == []
    fake.outbound(GATE, CALL, "in_call", "dialing")
    assert [(t.provider, t.door_id, t.call_id, t.state, t.prev_state) for t in seen] == [
        ("fake", GATE, CALL, "in_call", "dialing")]
    unsubscribe()
    fake.outbound(GATE, CALL, "ended", "in_call")
    assert len(seen) == 1

    class Old:             # a provider that predates section 4a: no subscribe_outbound at all
        API_VERSION = 1
        domain = "old"

        def __init__(self):
            self._f = FakeProvider("old")

        def __getattr__(self, name):
            if name == "subscribe_outbound":
                raise AttributeError(name)
            return getattr(self._f, name)

    registry.register(Old())
    assert registry.provider("old") is not None


async def test_a_provider_whose_unsubscribe_does_not_stop_delivery_is_still_ignored_after_unregister(world):
    """The registry's own `alive` check, not the provider's good manners: a late call into the listener it was given."""
    registry = world.hass.data[DOMAIN][DATA_PROVIDERS]
    seen = []
    registry.subscribe_outbound(seen.append)
    fake = _fake(world)
    late = list(fake._outbound_listeners)      # the listeners the registry handed the provider
    assert len(late) >= 1
    registry.unregister("fake")
    for listener in late:
        listener({"door_id": GATE, "call_id": CALL, "prev_state": "idle", "state": "in_call", "at_wall": 1.0})
    assert seen == []


async def test_after_the_provider_unregisters_its_late_outbound_event_is_ignored(world):
    registry = world.hass.data[DOMAIN][DATA_PROVIDERS]
    seen = []
    registry.subscribe_outbound(seen.append)
    fake = _fake(world)
    registry.unregister("fake")
    fake.outbound(GATE, CALL, "in_call")
    assert seen == []


async def test_a_visitor_listener_never_hears_an_outbound_change(world):
    """The visitors' subscription (the phones' stream, the push sender hang off `subscribe`) is a separate list."""
    registry = world.hass.data[DOMAIN][DATA_PROVIDERS]
    visitors = []
    registry.subscribe(visitors.append)
    _fake(world).outbound(GATE, CALL, "in_call", "dialing")
    assert visitors == []


async def test_the_relay_never_raises_into_the_dial(world):
    relay = get_talk_relay(world.hass)
    provider = SimpleNamespace(domain="fake")
    assert relay.placing("ep", None, provider, GATE) is None              # no user to tell
    assert relay.placing("ep", "u", SimpleNamespace(), GATE) is None      # no domain
    first = relay.placing("ep", "u", provider, GATE)
    assert first is not None and relay.placing("ep", "u", provider, GATE) is None    # one placement per door
    relay.placed(None, CALL)
    relay.failed(None, "x")
    relay.failed(first, "x")
    relay.failed(first, "again")           # already finished: nothing
    relay.placed(first, CALL)
    assert relay._by_door == {}


async def test_the_relay_is_created_only_once_the_registry_exists(hass):
    assert get_talk_relay(hass) is None
    assert DOMAIN not in hass.data or talk_relay.DATA_TALK_RELAY not in hass.data[DOMAIN]


async def test_stop_unsubscribes_and_clears(world):
    relay = get_talk_relay(world.hass)
    provider = SimpleNamespace(domain="fake")
    placement = relay.placing("ep", "u", provider, GATE)
    relay.stop()
    assert placement.finished and relay._by_door == {}
    _fake(world).outbound(GATE, CALL, "in_call")          # nobody listens any more
    assert relay._by_door == {}


# ---- the real driver part ----------------------------------------------------------------------------------------------------------------

def _real_world(world, client):
    entry, runtime = _add_device(world.hass, device_id="10.3.0.50", driver="gate_sip_observer",
                                 capabilities={"InboundAudio": 3, "OutboundAudio": 3, "ReportsAvailability": True,
                                               "CanDial": True},
                                 client=client)
    runtime.transport_available = True
    runtime.driver_available = True
    return entry, runtime


async def test_a_real_door_relays_its_outbound_changes_to_the_placing_phone_only(world, hass_ws_client):
    client = AsyncMock()
    entry, runtime = _real_world(world, client)
    await _save(world, _doc4({"id": "front", "title": "Front", "door": "10.3.0.50", "cameras": [],
                              "actions": [{"id": "talk", "label": "Talk", "kind": "talk", "confirm": False}]}))
    phone = await _client(world, hass_ws_client, "phone")
    await _subscribe(phone)
    other, _user = await _second_phone(world, hass_ws_client)
    await _subscribe(other)
    bus = []
    world.hass.bus.async_listen("portico_intercom_call", bus.append)
    tracker = runtime.call_state

    async def dial(door_id, host, port):
        tracker.apply_event({"call_id": CALL, "state": "answered", "device_id": door_id})     # the driver's StateAnswered
        return {"ok": True, "call_id": CALL}

    client.dial.side_effect = dial
    result, events = await _press(phone, 20, world)
    assert result["success"], result
    assert result["result"]["call_id"] == CALL
    assert _states(events) == [("dialing", None), ("in_call", CALL)]
    assert _talks(events)[0]["door_id"] == "10.3.0.50"
    tracker.apply_event({"call_id": CALL, "state": "ended"})
    assert _states(await _barrier(phone)) == [("ended", CALL)]
    # the other phone: nothing, not even a call_state_changed; and no visitor state moved anywhere
    assert await _barrier(other) == []
    assert tracker.state == "idle" and tracker.call_id is None
    await world.hass.async_block_till_done()
    assert bus == []                                   # the bus event of the sensor and the takeover never fired
    assert world.hass.states.async_all("sensor") == []


async def test_a_real_visitor_ring_during_a_real_talk_still_goes_to_everyone(world, hass_ws_client):
    client = AsyncMock()
    entry, runtime = _real_world(world, client)
    await _save(world, _doc4({"id": "front", "title": "Front", "door": "10.3.0.50", "cameras": [],
                              "actions": [{"id": "talk", "label": "Talk", "kind": "talk", "confirm": False}]}))
    phone = await _client(world, hass_ws_client, "phone")
    await _subscribe(phone)
    other, _user = await _second_phone(world, hass_ws_client)
    await _subscribe(other)
    client.dial.return_value = {"ok": True, "call_id": CALL}
    await _press(phone, 20, world)
    await _barrier(phone)
    runtime.call_state.apply_event({"call_id": "v-1", "state": "ringing"})
    for who in (phone, other):
        frames = await _barrier(who)
        assert _talks(frames) == [] and [f["event"]["state"] for f in frames] == ["ringing"]


@pytest.mark.parametrize("code,expected", [
    ("busy", "busy"), ("not_connected", "not_connected"), ("failed", "failed"), ("not_available", "not_available"),
    ("no_answer", "no_answer"), ("timeout", "timeout"), ("gate_busy", "busy"), ("dial_disabled", "not_available"),
    ("dial_failed", "failed"), ("cancelled", "failed"), ("something-new", "failed"), (None, "failed"), ("", "failed")])
async def test_only_the_reasons_the_app_maps_are_ever_sent(code, expected):
    assert talk_relay.wire_reason(code) == expected
    assert expected in talk_relay.WIRE_REASONS
