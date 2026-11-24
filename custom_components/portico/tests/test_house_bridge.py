"""The house-call media glue (M7 3b-S; design 15.4, 15.12, 15.16): the call model of 3a joined to the hub's two-leg bridge, all
with a FAKE hub (`FakeHouseProvider`) and fake connections. Nothing here carries audio; the one thing these tests cannot
say is whether real audio is intelligible, which no offline test can (it needs two phones and the 0.6.0 wheel).

The gate (everything below is inert unless both hold): house calls are switched on (the settings entry's option, here
`async_setup_house(enabled=True)`) AND a registered provider answers `house_bridge_supported()` true. With a plain
version-1 provider, or one that says no, every new path answers `not_supported` and the call is the 3a call."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from .. import away_media, house, house_bridge
from ..house import HOUSE_PREFIX, async_stop_house
from ..provider import API_VERSION, ProviderRegistry
from .fake_house_provider import FakeHouseProvider, HubError
from .fake_provider import FakeProvider
from .house_env import build_house
from .test_endpoints import _providers
from .test_push_registry import _custom_integrations  # noqa: F401 - autouse fixture by name

pytestmark = pytest.mark.asyncio

CALLER, CALLEE = "alice", "dina"


class MediaConn:
    """A websocket connection as the bridge sees it: a user, the removers HA calls on close, the events sent."""

    def __init__(self, user_id: str) -> None:
        self.user = SimpleNamespace(id=user_id, is_admin=False, name=user_id)
        self.subscriptions: dict = {}
        self.events: list[tuple[int, dict]] = []

    def send_event(self, msg_id: int, event: dict) -> None:
        self.events.append((msg_id, event))

    def sent(self) -> list[dict]:
        return [event for _id, event in self.events]


class World:
    def __init__(self, h, hub: FakeHouseProvider | None) -> None:
        self.h = h
        self.hub = hub
        self.reg = h.registry
        self.conns: dict[str, MediaConn] = {}

    def conn(self, who: str) -> MediaConn:
        if who not in self.conns:
            self.conns[who] = MediaConn(self.h.users[who])
        return self.conns[who]

    def call(self, caller=CALLER, callee=CALLEE) -> str:
        result = self.h.dial(caller, callee)
        assert result.ok, result
        return result.call_id

    def answer(self, call_id: str, who=CALLEE) -> None:
        result, code = self.reg.answer(call_id, self.h.users[who])
        assert code is None, code

    def answered(self, caller=CALLER, callee=CALLEE) -> str:
        call_id = self.call(caller, callee)
        self.answer(call_id, callee)
        return call_id

    def declare(self, call_id: str, who: str, *, msg_id: int, codec: str = "pcmu", sdp: str | None = None):
        """One end's `portico/house/media_open` (a coroutine: the first end waits for the second)."""
        return self.reg.bridge.attach(self.conn(who), msg_id, self.h.users[who], call_id, codec=codec, sdp=sdp)

    async def connect(self, call_id: str, caller=CALLER, callee=CALLEE, **kw):
        """Both ends declare frame legs; returns (caller's reply, callee's reply)."""
        first = asyncio.ensure_future(self.declare(call_id, caller, msg_id=11, **kw))
        await asyncio.sleep(0)
        assert not first.done(), "the first end must wait for the second"
        second = await self.declare(call_id, callee, msg_id=22, **kw)
        return await first, second

    def events(self, who: str) -> list[tuple]:
        return self.h.conns[who].house()


@pytest.fixture
async def world(hass, hass_storage):
    h = await build_house(hass, hass_storage)
    hub = FakeHouseProvider()
    _providers(hass).register(hub)
    for endpoint in (CALLER, CALLEE, "kitchen"):
        h.online(endpoint)
    for conn in h.conns.values():
        conn.clear()
    return World(h, hub)


@pytest.fixture
async def plain_world(hass, hass_storage):
    """The same house with NO bridge provider at all: the door provider is a plain version-1 `FakeProvider`."""
    h = await build_house(hass, hass_storage)
    for endpoint in (CALLER, CALLEE, "kitchen"):
        h.online(endpoint)
    for conn in h.conns.values():
        conn.clear()
    return World(h, None)


def frames(legs):
    return {leg["side"]: leg for leg in legs}


# ---- the gate: no bridge available, nothing new happens ----------------------------------------------------------------

async def test_with_a_version_1_provider_and_no_bridge_media_open_is_not_supported_and_the_call_is_the_3a_call(plain_world):
    w = plain_world
    assert w.reg.bridge.supported() is False and w.reg.bridge.provider() is None
    call_id = w.answered()
    result, code = await w.declare(call_id, CALLER, msg_id=5)
    assert (result, code) == (None, "not_supported")
    assert w.conn(CALLER).events == [] and w.conn(CALLER).subscriptions == {}
    # 3a: nothing opened a leg, so the audio deadline ends it in error and nothing else happens
    await w.h.advance(11)
    assert w.events(CALLER)[-1] == ("ended", CALLEE, "error")
    assert w.reg.bridge._calls == {}


@pytest.mark.parametrize("how", ["says_no", "raises"])
async def test_a_provider_that_says_no_or_cannot_tell_is_no_bridge(world, how):
    if how == "says_no":
        world.hub.supported = False
    else:
        world.hub.supported_raises = RuntimeError("hub gone")
    call_id = world.answered()
    assert world.reg.bridge.supported() is False
    assert await world.declare(call_id, CALLER, msg_id=5) == (None, "not_supported")
    assert world.hub.opened == [] and world.hub.closed == []


async def test_the_capability_is_asked_every_time_not_remembered(world):
    call_id = world.answered()
    world.hub.supported = False
    assert await world.declare(call_id, CALLER, msg_id=5) == (None, "not_supported")
    world.hub.supported = True                       # the hub restarted onto a wheel that has it
    first, second = await world.connect(call_id)
    assert first[1] is None and second[1] is None
    # and it is asked again for the next call: a hub that went back to an old wheel is no bridge any more
    world.reg.hangup(call_id, world.h.users[CALLER])
    world.hub.supported = False
    second_call = world.answered()
    assert await world.declare(second_call, CALLER, msg_id=9) == (None, "not_supported")


async def test_the_registry_finds_the_bridge_provider_among_providers_that_have_none(hass, world):
    registry = _providers(hass)
    assert registry.house_provider() is world.hub
    world.hub.supported = False
    assert registry.house_provider() is None
    plain = FakeProvider("plain")
    registry.register(plain)
    assert registry.house_provider() is None          # a provider with none of the optional methods is simply not one


async def test_the_provider_api_version_stays_1_so_a_version_1_provider_keeps_working(hass):
    from custom_components.portico import provider as ui_provider

    assert API_VERSION == 1 == ui_provider.API_VERSION
    registry = ProviderRegistry(hass)
    old = FakeProvider("old")                         # a version-1 provider with none of the optional methods
    old.add_door("d1")
    registry.register(old)
    assert registry.house_provider() is None
    new = FakeHouseProvider("new")
    registry.register(new)
    assert registry.house_provider() is new


# ---- placing and answering opens nothing; the second declaration opens one bridge ------------------------------------

async def test_placing_and_answering_open_no_bridge_by_themselves(world):
    call_id = world.call()
    assert world.hub.opened == []
    world.answer(call_id)
    assert world.hub.opened == [] and world.hub.closed == []


async def test_a_call_that_is_not_answered_has_no_media(world):
    call_id = world.call()
    # answered at once, not after the 15 s a waiting end would be held: nothing was registered to wait for
    assert await asyncio.wait_for(world.declare(call_id, CALLER, msg_id=5), 1) == (None, "unknown_call")
    assert await asyncio.wait_for(world.declare(call_id, CALLEE, msg_id=5), 1) == (None, "unknown_call")
    assert world.hub.opened == [] and world.reg.bridge._calls == {}


async def test_both_ends_declare_frame_legs_and_exactly_one_bridge_opens(world):
    call_id = world.answered()
    (caller_reply, caller_code), (callee_reply, callee_code) = await world.connect(call_id, codec="l16")
    assert caller_code is None and callee_code is None
    ((opened_id, legs),) = world.hub.opened
    assert opened_id == call_id and [leg["side"] for leg in legs] == ["from", "to"]
    assert legs[0] == {"side": "from", "kind": "frames", "owner": CALLER, "codec": "l16"}
    assert legs[1] == {"side": "to", "kind": "frames", "owner": CALLEE, "codec": "l16"}
    assert caller_reply == {"kind": "frames", "codec": "l16", "frame_bytes": 1280, "frame_ms": 40}
    assert callee_reply == caller_reply
    assert len(world.hub.legs) == 2 and world.hub.closed == []
    # each end's command id now holds its subscription (HA calls it when the connection closes)
    assert set(world.conn(CALLER).subscriptions) == {11} and set(world.conn(CALLEE).subscriptions) == {22}


async def test_the_pcmu_default_reply_carries_the_sizes_the_hub_gave(world):
    call_id = world.answered()
    reply, _ = (await world.connect(call_id))[0]
    assert reply == {"kind": "frames", "codec": "pcmu", "frame_bytes": 320, "frame_ms": 40}


async def test_the_first_end_to_ask_waits_and_the_second_opens_it(world):
    call_id = world.answered()
    first = asyncio.ensure_future(world.declare(call_id, CALLEE, msg_id=7))        # the callee asks first
    await asyncio.sleep(0)
    assert not first.done() and world.hub.opened == []
    second = await world.declare(call_id, CALLER, msg_id=8)
    assert second[1] is None and (await first)[1] is None
    assert len(world.hub.opened) == 1


async def test_an_end_cannot_declare_twice(world):
    call_id = world.answered()
    first = asyncio.ensure_future(world.declare(call_id, CALLER, msg_id=5))
    await asyncio.sleep(0)
    assert await world.declare(call_id, CALLER, msg_id=6) == (None, "already_open")
    await world.declare(call_id, CALLEE, msg_id=7)
    assert (await first)[1] is None
    assert await world.declare(call_id, CALLER, msg_id=9) == (None, "already_open")
    assert len(world.hub.opened) == 1


async def test_a_stranger_and_a_loser_learn_nothing_and_get_no_leg(world):
    h = world.h
    call_id = h.everyone().call_id
    world.answer(call_id, CALLEE)                      # dina wins; alice stops ringing
    assert await world.declare(call_id, CALLER, msg_id=5) == (None, "unknown_call")       # the loser
    stranger = world.reg.bridge.attach(MediaConn("u-nobody"), 5, "u-nobody", call_id, codec="pcmu", sdp=None)
    assert await stranger == (None, "unknown_call")
    assert world.hub.opened == []


async def test_one_answerer_wins_on_everyone_and_only_the_winner_is_bridged(world):
    h = world.h
    call_id = h.everyone().call_id
    first = world.reg.answer(call_id, h.users[CALLEE])
    second = world.reg.answer(call_id, h.users[CALLER])
    assert first[1] is None and second == (None, "already_answered")
    first_decl = asyncio.ensure_future(world.declare(call_id, "kitchen", msg_id=3))
    await asyncio.sleep(0)
    both = await world.declare(call_id, CALLEE, msg_id=4)
    assert both[1] is None and (await first_decl)[1] is None
    ((_, legs),) = world.hub.opened
    assert [(leg["side"], leg["owner"]) for leg in legs] == [("from", "kitchen"), ("to", CALLEE)]
    assert await world.declare(call_id, CALLER, msg_id=5) == (None, "unknown_call")
    assert len(world.hub.opened) == 1


async def test_two_phones_racing_to_answer_open_one_bridge(world):
    h = world.h
    call_id = h.everyone().call_id
    results = [world.reg.answer(call_id, h.users[who]) for who in (CALLER, CALLEE)]
    assert sorted(code or "ok" for _r, code in results) == ["already_answered", "ok"]
    winner = CALLER if results[0][1] is None else CALLEE
    first = asyncio.ensure_future(world.declare(call_id, "kitchen", msg_id=3))
    await asyncio.sleep(0)
    await world.declare(call_id, winner, msg_id=4)
    await first
    assert len(world.hub.opened) == 1 and world.hub.opened[0][1][1]["owner"] == winner


# ---- audio ------------------------------------------------------------------------------------------------------------

async def test_downlink_and_uplink_flow_to_the_right_end_only(world):
    call_id = world.answered()
    (_, _), (_, _) = await world.connect(call_id)
    caller, callee = world.conn(CALLER), world.conn(CALLEE)
    caller_leg, callee_leg = sorted(world.hub.legs)[0], sorted(world.hub.legs)[1]
    assert caller_leg.endswith("-from") and callee_leg.endswith("-to")
    world.hub.downlink(callee_leg, "AAAA")
    world.hub.downlink(caller_leg, "BBBB")
    assert caller.events == [(11, {"t": "f", "d": "BBBB"})] and callee.events == [(22, {"t": "f", "d": "AAAA"})]
    # uplink: only the owner of a live frame leg may push, onto its own leg
    world.reg.bridge.uplink(caller, call_id, "up1")
    world.reg.bridge.uplink(callee, call_id, "up2")
    world.reg.bridge.uplink(MediaConn("u-x"), call_id, "evil")
    world.reg.bridge.uplink(caller, "hso-unknown", "none")
    assert world.hub.uplink == [(caller_leg, "up1"), (callee_leg, "up2")]


async def test_no_audio_event_before_the_reply_has_gone_out(world):
    call_id = world.answered()
    await world.connect(call_id)
    caller_leg = sorted(world.hub.legs)[0]
    leg = world.reg.bridge._calls[call_id].legs["from"]
    leg.replied = False                                  # as if the handler had not yet sent its result
    world.hub.downlink(caller_leg, "early")
    assert world.conn(CALLER).events == []
    leg.replied = True
    world.hub.downlink(caller_leg, "CCCC")
    assert world.conn(CALLER).sent() == [{"t": "f", "d": "CCCC"}]
    leg.closed = True                                    # and none once the leg is closed
    world.hub.downlink(caller_leg, "late")
    assert world.conn(CALLER).sent() == [{"t": "f", "d": "CCCC"}]


async def test_the_callees_first_frame_makes_the_call_active_and_stops_the_audio_deadline(world):
    call_id = world.answered()
    await world.connect(call_id)
    world.h.conns[CALLER].clear()
    world.hub.leg_state(call_id, "from", "first_frame")          # the caller's own frame says nothing about the callee
    assert world.events(CALLER) == []
    world.hub.leg_state(call_id, "to", "attached")
    assert world.events(CALLER) == []
    world.hub.leg_state(call_id, "to", "first_frame")
    assert world.events(CALLER) == [("active", CALLEE, None)] and world.events(CALLEE)[-1] == ("active", CALLEE, None)
    await world.h.advance(30)                                    # the 10 s deadline was cancelled
    assert world.events(CALLER) == [("active", CALLEE, None)]
    assert world.hub.closed == []


async def test_no_callee_frame_in_ten_seconds_ends_in_error_and_closes_the_bridge(world):
    call_id = world.answered()
    await world.connect(call_id)
    await world.h.advance(11)
    assert world.events(CALLER)[-1] == ("ended", CALLEE, "error")
    await world.h.hass.async_block_till_done()
    assert world.hub.closed == [call_id]
    assert world.conn(CALLER).sent()[-1] == {"t": "end", "reason": "error"} and world.conn(CALLEE).sent()[-1] == {"t": "end", "reason": "error"}
    assert world.conn(CALLER).subscriptions == {} and world.reg.bridge._calls == {}


# ---- hang-up, goodbye, timeouts -----------------------------------------------------------------------------------------

async def test_a_hangup_closes_the_bridge_once_and_tells_both_ends(world):
    call_id = world.answered()
    await world.connect(call_id)
    world.hub.leg_state(call_id, "to", "first_frame")
    ok, code = world.reg.hangup(call_id, world.h.users[CALLEE])
    assert (ok, code) == (True, None)
    await world.h.hass.async_block_till_done()
    assert world.hub.closed == [call_id]
    assert world.conn(CALLER).sent()[-1] == {"t": "end", "reason": "hangup"}
    assert world.conn(CALLEE).sent()[-1] == {"t": "end", "reason": "hangup"}
    # idempotent: a second hang-up and a late hub state change nothing
    assert world.reg.hangup(call_id, world.h.users[CALLEE]) == (False, "unknown_call")
    world.hub.leg_state(call_id, "to", "lost", reason="lost")
    await world.h.hass.async_block_till_done()
    assert world.hub.closed == [call_id]


async def test_the_phones_goodbye_on_its_frame_channel_hangs_the_call_up(world):
    call_id = world.answered()
    await world.connect(call_id)
    world.hub.leg_state(call_id, "to", "first_frame")
    world.reg.bridge.phone_close(MediaConn("u-x"), call_id, "u-x")                   # a stranger's goodbye: nothing
    assert world.events(CALLER)[-1] == ("active", CALLEE, None)
    world.reg.bridge.phone_close(world.conn(CALLEE), call_id, world.h.users[CALLEE])
    assert world.events(CALLER)[-1] == ("ended", CALLEE, "hangup")
    await world.h.hass.async_block_till_done()
    assert world.hub.closed == [call_id]


async def test_the_caller_cancelling_before_it_is_answered_never_opens_a_bridge(world):
    call_id = world.call()
    world.reg.hangup(call_id, world.h.users[CALLER])
    await world.h.hass.async_block_till_done()
    assert world.hub.opened == [] and world.hub.closed == []


async def test_a_callee_that_never_asks_for_audio_leaves_the_first_end_waiting_until_the_deadline(world):
    call_id = world.answered()
    first = asyncio.ensure_future(world.declare(call_id, CALLER, msg_id=5))
    await asyncio.sleep(0)
    await world.h.advance(11)                        # the 10 s audio deadline ends the call
    assert await first == (None, "unknown_call")
    assert world.events(CALLEE)[-1] == ("ended", CALLEE, "error")
    assert world.hub.opened == [] and world.reg.bridge._calls == {}


async def test_cancelling_the_waiting_end_ends_the_call(world):
    call_id = world.answered()
    first = asyncio.ensure_future(world.declare(call_id, CALLER, msg_id=5))
    await asyncio.sleep(0)
    first.cancel()                                   # the phone's socket closed while it waited
    with pytest.raises(asyncio.CancelledError):
        await first
    assert world.events(CALLEE)[-1] == ("ended", CALLEE, "error")
    assert world.reg.bridge._calls == {}


# ---- loss ------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("who", [CALLER, CALLEE])
async def test_an_end_whose_last_subscription_drops_while_talking_ends_the_call_and_closes_the_bridge(world, who):
    call_id = world.answered()
    await world.connect(call_id)
    world.hub.leg_state(call_id, "to", "first_frame")
    world.h.offline(who)
    other = CALLEE if who == CALLER else CALLER
    assert world.events(other)[-1] == ("ended", CALLEE, "error")
    await world.h.hass.async_block_till_done()
    assert world.hub.closed == [call_id]


async def test_without_a_bridge_a_talking_call_is_left_to_the_audio_deadline_as_in_3a(plain_world):
    w = plain_world
    call_id = w.answered()
    w.h.offline(CALLEE)
    assert w.events(CALLER)[-1] == ("answered", CALLEE, None)         # still answered: nothing ended it
    await w.h.advance(11)
    assert w.events(CALLER)[-1] == ("ended", CALLEE, "error")


async def test_the_phones_media_socket_closing_ends_the_call(world):
    call_id = world.answered()
    await world.connect(call_id)
    world.hub.leg_state(call_id, "to", "first_frame")
    world.conn(CALLEE).subscriptions[22]()           # HA calls the remover when the connection closes
    assert world.events(CALLER)[-1] == ("ended", CALLEE, "error")
    await world.h.hass.async_block_till_done()
    assert world.hub.closed == [call_id]


async def test_a_frame_channel_the_hub_closes_on_its_own_ends_the_call(world):
    call_id = world.answered()
    await world.connect(call_id)
    leg_id = sorted(world.hub.legs)[0]
    world.hub.leg_ended(leg_id, "idle")
    assert world.events(CALLEE)[-1] == ("ended", CALLEE, "error")


@pytest.mark.parametrize("leg,reason", [("from", "lost"), ("to", "silence"), ("both", "transport_gone")])
async def test_a_lost_leg_from_the_hub_ends_the_call_in_error_and_closes_the_bridge(world, leg, reason):
    call_id = world.answered()
    await world.connect(call_id)
    world.hub.leg_state(call_id, leg, "lost", reason=reason)
    assert world.events(CALLER)[-1] == ("ended", CALLEE, "error")
    assert world.events(CALLEE)[-1] == ("ended", CALLEE, "error")
    await world.h.hass.async_block_till_done()
    assert world.hub.closed == [call_id]


async def test_hub_states_for_a_call_nobody_knows_are_ignored(world):
    world.hub.leg_state("hso-nope", "to", "lost", reason="lost")
    world.hub.leg_state("hso-nope", "to", "first_frame")
    world.hub.leg_state("hso-nope", "to", "sdp", sdp="x")
    await world.h.hass.async_block_till_done()
    assert world.hub.closed == []


# ---- the bridge failing to open --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("code,reason", [("not_supported", "error"), ("house_open_failed", "error"),
                                          ("too_many_calls", "busy"), ("shutting_down", "error")])
async def test_a_hub_that_refuses_to_open_ends_the_call_and_tells_both_ends(world, code, reason):
    world.hub.open_error = HubError(code, "no")
    call_id = world.answered()
    (first_reply, first_code), (second_reply, second_code) = await world.connect(call_id)
    assert (first_reply, second_reply) == (None, None) and first_code == second_code == code
    assert world.events(CALLER)[-1] == ("ended", CALLEE, reason)
    assert world.reg.bridge._calls == {} and world.hub.legs == {}


async def test_an_error_without_a_code_is_no_transport(world):
    world.hub.open_error = ConnectionError("socket closed")
    call_id = world.answered()
    (_, first_code), (_, second_code) = await world.connect(call_id)
    assert first_code == second_code == "no_transport"
    assert world.events(CALLER)[-1] == ("ended", CALLEE, "error")


async def test_a_hangup_while_the_hub_is_opening_closes_the_bridge_the_hub_made(world):
    world.hub.open_gate = asyncio.Event()
    call_id = world.answered()
    first = asyncio.ensure_future(world.declare(call_id, CALLER, msg_id=5))
    await asyncio.sleep(0)
    second = asyncio.ensure_future(world.declare(call_id, CALLEE, msg_id=6))
    await asyncio.sleep(0.01)
    assert len(world.hub.opened) == 1 and not second.done()
    world.reg.hangup(call_id, world.h.users[CALLER])
    world.hub.open_gate.set()
    assert (await first)[1] == "unknown_call" and (await second)[1] == "unknown_call"
    await world.h.hass.async_block_till_done()
    assert world.hub.closed == [call_id]
    assert world.conn(CALLER).events == [] and world.conn(CALLEE).events == []     # no `end` before a reply: none was sent


async def test_the_declaring_end_closing_its_socket_while_the_hub_opens_does_not_leak_a_bridge(world):
    world.hub.open_gate = asyncio.Event()
    call_id = world.answered()
    first = asyncio.ensure_future(world.declare(call_id, CALLER, msg_id=5))
    await asyncio.sleep(0)
    second = asyncio.ensure_future(world.declare(call_id, CALLEE, msg_id=6))
    await asyncio.sleep(0.01)
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second
    assert (await first)[1] == "unknown_call"
    await world.h.hass.async_block_till_done()
    assert world.hub.closed == [call_id] and world.events(CALLER)[-1] == ("ended", CALLEE, "error")


# ---- a door outranks a house call -----------------------------------------------------------------------------------------

async def test_answering_a_door_call_ends_the_house_call_and_closes_its_bridge(world):
    call_id = world.answered()
    await world.connect(call_id)
    world.hub.leg_state(call_id, "to", "first_frame")
    h = world.h
    h.door.ring("gate-1", "gso-1")
    # the house call is talking: a door that rings leaves it alone, the door shows over it (design 7)
    assert world.events(CALLER)[-1] == ("active", CALLEE, None) and world.hub.closed == []
    reservation = world.reg.reserve_door_answer(h.users[CALLEE], "gso-1")
    assert reservation is not None and not isinstance(reservation, str)
    world.reg.settle_door_answer(reservation, True, "gate-1")      # the provider granted the callee's answer
    assert world.events(CALLER)[-1] == ("ended", CALLEE, "hangup")
    await h.hass.async_block_till_done()
    assert world.hub.closed == [call_id]
    assert world.conn(CALLEE).sent()[-1] == {"t": "end", "reason": "hangup"}
    assert world.conn(CALLER).sent()[-1] == {"t": "end", "reason": "hangup"}


async def test_a_door_answered_by_someone_else_leaves_the_house_call_up(world):
    call_id = world.answered()
    await world.connect(call_id)
    world.reg.door_claim_granted("u-somebody-else", "gso-1", "gate-1")
    assert world.hub.closed == [] and world.reg._calls[call_id].state == "answered"


# ---- WebRTC legs ----------------------------------------------------------------------------------------------------------------

async def test_a_webrtc_leg_is_declared_with_its_offer_and_the_answer_arrives_as_an_event_after_the_reply(world):
    call_id = world.answered("kitchen", CALLEE)
    first = asyncio.ensure_future(world.declare(call_id, "kitchen", msg_id=3, sdp="v=0 offer"))
    await asyncio.sleep(0)
    second = await world.declare(call_id, CALLEE, msg_id=4)
    assert await first == ({"kind": "webrtc"}, None)
    assert second == ({"kind": "frames", "codec": "pcmu", "frame_bytes": 320, "frame_ms": 40}, None)
    ((_, legs),) = world.hub.opened
    assert legs[0] == {"side": "from", "kind": "webrtc", "owner": "kitchen", "sdp": "v=0 offer"}
    assert legs[1]["kind"] == "frames" and "sdp" not in legs[1]
    kitchen = world.conn("kitchen")
    assert kitchen.events == [] and set(kitchen.subscriptions) == {3}
    world.hub.leg_state(call_id, "from", "sdp", sdp="v=0 answer")
    assert kitchen.sent() == [{"t": "sdp", "sdp": "v=0 answer"}]
    # a phone-side WebRTC leg's socket closing ends the call, like a frame leg's
    kitchen.subscriptions[3]()
    assert world.events(CALLEE)[-1] == ("ended", CALLEE, "error")


async def test_a_webrtc_answer_that_beats_the_reply_is_held_and_sent_right_after_it(world):
    call_id = world.answered("kitchen", CALLEE)
    first = asyncio.ensure_future(world.declare(call_id, "kitchen", msg_id=3, sdp="offer"))
    await asyncio.sleep(0)
    second = asyncio.ensure_future(world.declare(call_id, CALLEE, msg_id=4))
    await asyncio.sleep(0)
    # the hub's answer is already here before the handlers have resumed
    world.hub.leg_state(call_id, "from", "sdp", sdp="early answer")
    await asyncio.gather(first, second)
    await asyncio.sleep(0)
    assert world.conn("kitchen").sent() == [{"t": "sdp", "sdp": "early answer"}]


async def test_a_sdp_state_for_a_frames_leg_is_ignored(world):
    call_id = world.answered()
    await world.connect(call_id)
    world.hub.leg_state(call_id, "from", "sdp", sdp="stray")
    await asyncio.sleep(0)
    assert world.conn(CALLER).events == []


# ---- shutdown and the stay-out rules -------------------------------------------------------------------------------------------

async def test_taking_the_registry_down_closes_every_bridge(hass, world):
    call_id = world.answered()
    await world.connect(call_id)
    async_stop_house(hass)
    await hass.async_block_till_done()
    assert world.hub.closed == [call_id] and world.reg.bridge._calls == {}
    assert world.reg.bridge._unsub is None               # and stopped listening to the hub's leg states


async def test_a_house_call_id_never_reaches_a_doors_verbs_or_tracker(world):
    call_id = world.answered()
    await world.connect(call_id)
    world.hub.leg_state(call_id, "to", "first_frame")
    world.reg.hangup(call_id, world.h.users[CALLER])
    await world.h.hass.async_block_till_done()
    assert call_id.startswith(HOUSE_PREFIX)
    for provider in (world.h.door, world.hub):
        door_verbs = [c for c in provider.calls if c[0] in ("answer", "hangup", "keepalive", "webrtc_offer", "webrtc_answer",
                                                             "webrtc_ice", "media_open", "media_frame", "media_close", "dial")]
        assert door_verbs == [], provider.domain


async def test_the_local_state_strings_match_the_call_model():
    assert (house_bridge._ANSWERED, house_bridge._ACTIVE) == (house.STATE_ANSWERED, house.STATE_ACTIVE)
    assert (house_bridge._END_ERROR, house_bridge._END_HANGUP, house_bridge._END_BUSY) == (
        house.REASON_ERROR, house.REASON_HANGUP, "busy")
    assert house_bridge.CODE_UNKNOWN_CALL == house.CODE_UNKNOWN_CALL
    assert HOUSE_PREFIX == "hso-"
    assert house_bridge.CODECS == away_media.CODECS


async def test_calls_that_never_see_a_bridge_provider_do_not_import_the_driver_part():
    from pathlib import Path

    source = Path(house_bridge.__file__).read_text(encoding="utf-8")
    assert "portico_intercom" not in source.replace("provider_v1", "")


# ---- the registry's validation of what a provider reports -----------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    {"leg": "to", "state": "lost"},                                    # no call id
    {"call_id": "", "leg": "to", "state": "lost"},
    {"call_id": "hso-x", "state": "lost"},                             # no leg
    {"call_id": "hso-x", "leg": "to", "state": "exploded"},            # not a state of the seam
    {"call_id": "hso-x", "leg": "to", "state": "sdp", "sdp": 7},       # mistyped
    {"call_id": "hso-x", "leg": "to", "state": "lost", "reason": 7},
])
async def test_a_malformed_leg_state_is_dropped_and_logged_and_ends_nothing(hass, world, caplog, bad):
    call_id = world.answered()
    await world.connect(call_id)
    bad = {**bad, **({"call_id": call_id} if bad.get("call_id") == "hso-x" else {})}
    for listener in list(world.hub._house_listeners):
        listener(bad)
    assert world.reg._calls[call_id].state == "answered" and world.hub.closed == []
    assert "dropping a bad house leg state" in caplog.text


async def test_a_listener_that_raises_does_not_starve_the_bridge(hass, world, caplog):
    registry = _providers(hass)
    registry.subscribe_house(lambda leg: 1 / 0)
    call_id = world.answered()
    await world.connect(call_id)
    world.hub.leg_state(call_id, "to", "first_frame")
    assert world.events(CALLER)[-1] == ("active", CALLEE, None)        # the bridge still heard it


async def test_a_provider_that_unregistered_is_no_longer_heard(hass, world):
    call_id = world.answered()
    await world.connect(call_id)
    _providers(hass).unregister("bridge")
    world.hub.leg_state(call_id, "to", "lost", reason="lost")           # late notification of a provider that left
    assert world.reg._calls[call_id].state == "answered"
    assert world.reg.bridge.supported() is False                        # and it is no longer a bridge provider


# ---- 3f: the gate is a Talk (dial) target, never a house-bridge leg ---------------------------------------------------------------

async def test_the_gate_is_not_a_house_call_target_and_a_talk_id_is_not_a_house_call(world):
    h = world.h
    result = h.place(CALLER, "door", "fake:gate-1")             # the gate goes through Talk's path in the content store, not here
    assert result.ok is False
    assert h.registry.target_state("door", "fake:gate-1") is None
    for talk_id in ("out-0123456789abcdef", "gso-1", "fake:gate-1"):
        assert await world.declare(talk_id, CALLER, msg_id=5) == (None, "unknown_call")
        world.hub.leg_state(talk_id, "to", "first_frame")        # a stray state for a non-house id changes nothing
        world.hub.leg_state(talk_id, "to", "lost", reason="lost")
    assert world.hub.opened == [] and world.hub.closed == [] and world.reg.bridge._calls == {}


async def test_a_talk_dial_beside_a_house_call_leaves_the_bridge_alone(world):
    call_id = world.answered()
    await world.connect(call_id)
    world.hub.leg_state(call_id, "to", "first_frame")
    # the outbound (Talk) transitions of a door are a separate listener list; none of them reaches the house registry
    world.h.door.calls.clear()
    assert world.reg._calls[call_id].state == "active" and world.hub.closed == []
    assert len(world.hub.opened) == 1
