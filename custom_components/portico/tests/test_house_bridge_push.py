"""3b-S beside 3c: the media bridge and the push ring notification (push) on one call. A `push_only` phone has no open socket, so it can
never be an attached bridge leg; and whatever ends a bridged call still fires the 3c push hooks (`house_ended`, `house_left`).
Fakes only (a fake push ring notification, a fake hub); no audio."""
from __future__ import annotations

import asyncio

import pytest

from ..config_flow import DOMAIN
from ..push import const
from .fake_house_provider import FakeHouseProvider
from .house_env import build_house
from .test_endpoints import _providers
from .test_house_bridge import MediaConn, World
from .test_house_push import FakePush
from .test_push_registry import _custom_integrations  # noqa: F401 - autouse fixture by name

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def world(hass, hass_storage):
    """alice and the kitchen are online; dina (receive on, a push registration) is away, i.e. `push_only`."""
    h = await build_house(hass, hass_storage, phones=(("alice", True), ("dina", True)))
    push = FakePush(h.users["alice"], h.users["dina"])
    hass.data[DOMAIN][const.DATA_PUSH] = push
    hub = FakeHouseProvider()
    _providers(hass).register(hub)
    h.online("alice")
    h.online("kitchen")
    for conn in h.conns.values():
        conn.clear()
    w = World(h, hub)
    w.push = push
    return w


async def test_a_push_only_callee_has_no_socket_so_it_is_no_leg_and_the_deadline_ends_the_call(world):
    h = world.h
    assert h.registry.presence("dina")["state"] == "push_only"
    call_id = h.dial("kitchen", "dina").call_id
    assert ("ring", call_id) == world.push.log[0][:2]
    # still ringing: a push-only phone asking for audio is not in an answered call
    assert await asyncio.wait_for(world.declare(call_id, "dina", msg_id=5), 1) == (None, "unknown_call")
    world.answer(call_id, "dina")                      # the app woke, answered (its socket is not subscribed to calls)
    caller = asyncio.ensure_future(world.declare(call_id, "kitchen", msg_id=6))
    await asyncio.sleep(0)
    assert not caller.done() and world.hub.opened == []          # one end alone never opens a bridge
    await h.advance(11)
    assert (await caller) == (None, "unknown_call")
    assert world.hub.opened == [] and world.hub.closed == []
    assert world.push.log[-1] == ("ended", call_id)              # and the 3c hook for the end still fired


async def test_a_bridged_call_that_ends_closes_the_bridge_and_fires_the_push_hooks(world):
    h = world.h
    call_id = h.dial("kitchen", "dina").call_id                  # dina is away: rung by notification
    world.answer(call_id, "dina")                                # her app woke and answered ...
    h.online("dina")                                             # ... and opened its socket: now it can carry audio
    await world.connect(call_id, caller="kitchen", callee="dina")
    world.hub.leg_state(call_id, "to", "first_frame")
    assert ("answered", call_id, "dina") in world.push.log
    ok, _ = world.reg.hangup(call_id, h.users["dina"])
    assert ok
    await h.hass.async_block_till_done()
    assert world.hub.closed == [call_id]
    assert world.push.log[-1] == ("ended", call_id)


async def test_a_lost_bridge_leg_fires_the_push_end_too(world):
    h = world.h
    call_id = h.dial("kitchen", "dina").call_id
    world.answer(call_id, "dina")
    h.online("dina")
    await world.connect(call_id, caller="kitchen", callee="dina")
    world.hub.leg_state(call_id, "from", "lost", reason="lost")
    await h.hass.async_block_till_done()
    assert world.hub.closed == [call_id] and world.push.log[-1] == ("ended", call_id)


async def test_everyone_with_an_away_phone_bridges_only_the_phone_that_answered(world):
    h = world.h
    call_id = h.everyone().call_id
    kinds = [e[0] for e in world.push.log]
    assert "ring" in kinds                                       # dina is rung by notification
    world.answer(call_id, "alice")                               # the online phone wins
    first = asyncio.ensure_future(world.declare(call_id, "kitchen", msg_id=3))
    await asyncio.sleep(0)
    await world.declare(call_id, "alice", msg_id=4)
    await first
    ((_, legs),) = world.hub.opened
    assert [leg["owner"] for leg in legs] == ["kitchen", "alice"]
    assert await world.declare(call_id, "dina", msg_id=5) == (None, "unknown_call")    # the push-only loser gets no leg
    assert ("answered", call_id, "alice") in world.push.log      # the away phone's notification is dismissed (3c)
