"""House calls ring AWAY phones by notification (M7 3c; design 5, 14.6, 15.5): `push_only` presence, who is rung, when the
registry calls the sender, and that the sender's failures never touch a call. Registry-level tests use a FAKE push ring notification (a
stand-in manager that logs what it is asked); the last tests drive the REAL manager and sender over the local fake Google.
Nothing is sent anywhere; nothing here rings a real phone."""
from __future__ import annotations

import pytest
from homeassistant.helpers.dispatcher import async_dispatcher_send

from ..call_endpoints import REASON_DND, REASON_NO_RECEIVERS, REASON_OFFLINE, REASON_PUSH_ONLY, REASON_RECEIVE_OFF
from ..config_flow import DOMAIN
from ..content import get_content_store
from ..endpoints import get_endpoint_registry
from ..house import CODE_BUSY, CODE_NOT_CALLABLE, CODE_TARGET_DND, CODE_TARGET_OFFLINE, async_stop_house, get_house, house_from_ref
from ..phone_entry import async_set_house_receive, phone_entries
from ..push import const, payload
from .house_env import build_house
from .test_push_registry import _custom_integrations, _manager, _push_entry, google  # noqa: F401 - fixtures by name

pytestmark = pytest.mark.asyncio

KITCHEN_REF = house_from_ref("kitchen")


class FakePush:
    """What the house-call registry asks of the push ring notification, logged. `users` are the HA users it can reach by notification."""

    def __init__(self, *users: str) -> None:
        self.users = set(users)
        self.log: list[tuple] = []
        self.raise_on: set[str] = set()

    def _maybe(self, what: str) -> None:
        if what in self.raise_on:
            raise RuntimeError(f"{what} bug")

    def house_has_push(self, user_id):
        self._maybe("has_push")
        return user_id in self.users

    def house_ring(self, call_id, from_ref, callees, ring_s):
        self.log.append(("ring", call_id, from_ref, dict(callees), ring_s))
        self._maybe("ring")
        return set(callees)

    def house_answered(self, call_id, winner):
        self.log.append(("answered", call_id, winner))
        self._maybe("answered")

    def house_left(self, call_id, endpoint_id, *, notify):
        self.log.append(("left", call_id, endpoint_id, notify))
        self._maybe("left")

    def house_ended(self, call_id):
        self.log.append(("ended", call_id))
        self._maybe("ended")

    def kinds(self):
        return [entry[0] for entry in self.log]


@pytest.fixture
async def house(hass, hass_storage):
    """alice and the kitchen screen are online; dina and bob (receive on) are away; zed has receive off. All four phones
    have a push registration."""
    h = await build_house(hass, hass_storage, phones=(("alice", True), ("dina", True), ("bob", True), ("zed", False)))
    push = FakePush(h.users["alice"], h.users["dina"], h.users["bob"], h.users["zed"])
    hass.data[DOMAIN][const.DATA_PUSH] = push
    h.online("alice")
    h.online("kitchen")
    for conn in h.conns.values():
        conn.clear()
    h.push = push
    return h


def _state(h, who):
    return h.registry.target_state("phone", who)


# ---- presence and callability: `push_only` -------------------------------------------------------------------------

async def test_an_away_phone_with_receive_on_and_a_registration_is_push_only_and_callable(house):
    assert house.registry.presence("dina") == {"state": "push_only", "dnd": False}
    state = _state(house, "dina")
    assert (state.available, state.reason) == (True, REASON_PUSH_ONLY)
    assert house.registry.presence("alice")["state"] == "online"
    assert (_state(house, "alice").available, _state(house, "alice").reason) == (True, None)


async def test_without_a_registration_the_same_phone_is_offline_as_it_was_in_3a(house):
    house.push.users.discard(house.users["dina"])
    assert house.registry.presence("dina")["state"] == "offline"
    state = _state(house, "dina")
    assert (state.available, state.reason) == (False, REASON_OFFLINE)
    assert house.dial("kitchen", "dina").code == CODE_TARGET_OFFLINE
    assert house.push.log == []


async def test_without_an_away_ring_at_all_nothing_is_push_only(house, hass):
    del hass.data[DOMAIN][const.DATA_PUSH]
    assert house.registry.presence("dina")["state"] == "offline"
    assert (_state(house, "dina").available, _state(house, "dina").reason) == (False, REASON_OFFLINE)
    assert house.dial("kitchen", "dina").code == CODE_TARGET_OFFLINE


async def test_receive_off_is_never_push_only_or_rung(house):
    state = _state(house, "zed")
    assert (state.available, state.reason) == (False, REASON_RECEIVE_OFF)
    assert house.dial("kitchen", "zed").code == CODE_NOT_CALLABLE
    house.everyone()
    assert house.push.log[0][3] == {"dina": house.users["dina"], "bob": house.users["bob"]}        # never zed


async def test_a_screen_is_never_push_only(house):
    house.offline("kitchen")
    house.push.users.add(house.users["kitchen"])                      # even with a registration under its login
    assert house.dial("alice", "kitchen").code == CODE_TARGET_OFFLINE
    assert house.registry.presence("kitchen")["state"] == "offline"


async def test_a_push_bug_reads_as_not_reachable_and_never_breaks_the_roster(house):
    house.push.raise_on.add("has_push")
    assert house.registry.presence("dina")["state"] == "offline"
    assert house.dial("kitchen", "dina").code == CODE_TARGET_OFFLINE
    house.registry.roster_rev()


async def test_a_door_ringing_makes_an_away_phone_busy_and_no_push_goes(house):
    house.door.ring("gate-1", "g1")
    assert house.dial("kitchen", "dina").code == CODE_BUSY and house.push.log == []
    assert _state(house, "dina").reason == "busy"


async def test_the_call_button_reads_push_only_through_content(house, hass):
    store = get_content_store(hass)
    dina = store.call_state({"label": "Dina", "target": {"type": "phone", "id": "dina"}})
    assert (dina["available"], dina["reason"]) == (True, REASON_PUSH_ONLY)
    alice = store.call_state({"label": "Alice", "target": {"type": "phone", "id": "alice"}})
    assert (alice["available"], alice["reason"]) == (True, None)
    house.push.users.clear()
    gone = store.call_state({"label": "Dina", "target": {"type": "phone", "id": "dina"}})
    assert (gone["available"], gone["reason"]) == (False, REASON_OFFLINE)


# ---- placing: who is rung, and how ---------------------------------------------------------------------------------

async def test_a_one_to_one_call_to_an_away_phone_rings_it_by_notification_with_an_opaque_ref(house):
    placed = house.dial("kitchen", "dina")
    assert placed.ok
    assert house.push.log == [("ring", placed.call_id, KITCHEN_REF, {"dina": house.users["dina"]}, 45)]
    assert KITCHEN_REF != "kitchen" and "kitchen" not in KITCHEN_REF and KITCHEN_REF.startswith("e-")
    # the caller's own screen is told it rings (the 3a events are unchanged)
    assert house.conns["kitchen"].house() == [("ringing", "dina", None)]


async def test_a_phone_with_an_open_subscription_is_rung_by_the_socket_and_not_pushed(house):
    house.online("dina")
    placed = house.dial("kitchen", "dina")
    assert placed.ok and house.push.log == []
    assert house.registry.presence("dina")["state"] == "online"


async def test_the_ring_window_pushed_is_the_registrys_own(hass, hass_storage):
    h = await build_house(hass, hass_storage, phones=(("alice", True), ("dina", True)))
    push = FakePush(h.users["dina"])
    hass.data[DOMAIN][const.DATA_PUSH] = push
    h.registry._ring_s = 30
    h.online("alice")
    assert h.dial("alice", "dina").ok and push.log[0][4] == 30


async def test_do_not_disturb_is_never_rung_by_either_path(house):
    house.online("dina", dnd=True)                                     # reported, and the connection is open
    assert house.dial("kitchen", "dina").code == CODE_TARGET_DND and house.push.log == []
    house.everyone()
    assert all(entry[0] != "ring" or "dina" not in entry[3] for entry in house.push.log)
    assert house.conns["dina"].house() == []                           # and the socket was not rung either
    # a DND report dies with its connection (HC-4): the phone is then simply away, and rung by notification
    house.offline("dina")
    assert house.registry.presence("dina") == {"state": "push_only", "dnd": False}


async def test_everyone_rings_the_online_phones_by_socket_and_the_away_ones_by_one_push_call(house):
    placed = house.everyone()
    assert placed.ok
    assert house.push.log == [("ring", placed.call_id, KITCHEN_REF,
                               {"dina": house.users["dina"], "bob": house.users["bob"]}, 45)]
    assert house.conns["alice"].house() == [("ringing", "alice", None)]    # alice is on the socket
    entry = house.registry.group_entries(get_endpoint_registry(house.hass).get("kitchen"))[0]
    assert entry["count"] == 0 or entry["reason"] is not None            # the screen is now on a call


async def test_the_everyone_count_includes_push_only_phones_and_drops_without_the_away_ring(house, hass):
    kitchen = get_endpoint_registry(hass).get("kitchen")
    assert house.registry.group_entries(kitchen)[0]["count"] == 3             # alice online, dina and bob by push
    house.push.users.discard(house.users["bob"])
    assert house.registry.group_entries(kitchen)[0]["count"] == 2
    del hass.data[DOMAIN][const.DATA_PUSH]
    entry = house.registry.group_entries(kitchen)[0]
    assert entry["count"] == 1 and entry["available"] is True                # alice only, exactly as in 3a
    house.offline("alice")
    entry = house.registry.group_entries(kitchen)[0]
    assert (entry["available"], entry["reason"], entry["count"]) == (False, REASON_NO_RECEIVERS, 0)


async def test_everyone_to_nobody_but_push_only_phones_is_placed(house):
    house.offline("alice")
    house.push.users.discard(house.users["alice"])                 # alice has no registration: simply offline
    placed = house.everyone()
    assert placed.ok and set(house.push.log[0][3]) == {"dina", "bob"}


async def test_a_caller_is_never_pushed_its_own_call(house):
    house.offline("alice")
    house.online("dina")
    placed = house.dial("dina", "alice")                               # alice is away now: pushed; dina (caller) is not
    assert placed.ok and house.push.log[0][3] == {"alice": house.users["alice"]}


# ---- the chain: answered, declined, cancelled, displaced ----------------------------------------------------------

async def test_a_pushed_callee_that_comes_online_and_answers_settles_the_ring(house):
    placed = house.dial("kitchen", "dina")
    house.online("dina")                                               # she tapped the notification; the app subscribed
    result, code = house.registry.answer(placed.call_id, house.users["dina"])
    assert code is None
    assert house.push.log[1:] == [("answered", placed.call_id, "dina")]
    house.registry.hangup(placed.call_id, house.users["kitchen"])
    assert house.push.kinds() == ["ring", "answered", "ended"]


async def test_first_answer_wins_and_the_losers_are_told_through_the_sender(house):
    placed = house.everyone()
    result, code = house.registry.answer(placed.call_id, house.users["alice"])      # the socket phone answers first
    assert code is None and house.push.log[1] == ("answered", placed.call_id, "alice")


async def test_a_declined_one_to_one_is_left_quietly_then_ended(house):
    placed = house.dial("kitchen", "dina")
    house.online("dina")
    ok, code = house.registry.decline(placed.call_id, house.users["dina"])
    assert ok and house.push.log[1:] == [("left", placed.call_id, "dina", False), ("ended", placed.call_id)]


async def test_a_declined_fan_out_leaves_the_others_ringing(house):
    placed = house.everyone()
    house.online("dina")
    house.registry.decline(placed.call_id, house.users["dina"])
    assert house.push.log[1:] == [("left", placed.call_id, "dina", False)]
    house.registry.hangup(placed.call_id, house.users["kitchen"])
    assert house.push.log[-1] == ("ended", placed.call_id)


@pytest.mark.parametrize("how", ["cancel", "no_answer", "stop"])
async def test_a_call_that_is_cancelled_unanswered_or_torn_down_ends_the_ring_once(house, how):
    placed = house.dial("kitchen", "dina")
    if how == "cancel":
        house.registry.hangup(placed.call_id, house.users["kitchen"])
    elif how == "no_answer":
        await house.advance(46)
    else:
        house.registry.stop()
    assert house.push.log[1:] == [("ended", placed.call_id)]
    house.registry.hangup(placed.call_id, house.users["kitchen"])      # a repeat does nothing
    assert house.push.kinds() == ["ring", "ended"]


async def test_a_door_ringing_dismisses_a_pushed_fan_out_callee_and_then_the_call_ends(house):
    placed = house.everyone()
    house.door.ring("gate-1", "g1")
    lefts = [entry for entry in house.push.log if entry[0] == "left"]
    assert {entry[2] for entry in lefts} == {"dina", "bob"} and all(entry[3] is True for entry in lefts)
    assert house.push.log[-1] == ("ended", placed.call_id) and house.push.kinds().count("ended") == 1


async def test_a_door_ringing_ends_a_pushed_one_to_one_with_one_dismiss(house):
    house.dial("alice", "dina")
    house.door.ring("gate-1", "g1")
    assert house.push.kinds() == ["ring", "ended"]


async def test_a_pushed_callee_that_answers_a_door_leaves_the_house_ring(house):
    placed = house.everyone()
    house.registry.door_claim_granted(house.users["dina"], "g1")
    assert ("left", placed.call_id, "dina", True) in house.push.log


async def test_a_pushed_callee_whose_connection_drops_is_dismissed(house):
    placed = house.everyone()
    house.online("dina")
    house.offline("dina")                                              # tapped, subscribed, then the socket dropped
    assert ("left", placed.call_id, "dina", True) in house.push.log
    house.registry.hangup(placed.call_id, house.users["kitchen"])
    one = house.dial("alice", "bob")
    house.online("bob")
    house.offline("bob")                                               # a 1:1: the whole call ends
    assert one.ok and house.push.log[-1] == ("ended", one.call_id)


async def test_a_phone_that_loses_receive_ends_the_call_and_the_ring(house, hass):
    placed = house.dial("kitchen", "dina")
    (entry,) = [e for e in phone_entries(hass) if e.data["ha_user_id"] == house.users["dina"]]
    async_set_house_receive(hass, entry, False)
    assert house.push.log[1:] == [("ended", placed.call_id)]


async def test_a_call_with_no_pushed_callee_never_asks_the_sender_anything(house):
    placed = house.dial("kitchen", "alice")                            # online: the socket
    house.registry.answer(placed.call_id, house.users["alice"])
    house.registry.hangup(placed.call_id, house.users["kitchen"])
    assert house.push.log == []


# ---- the sender's failures never touch a call ----------------------------------------------------------------------

@pytest.mark.parametrize("hook", ["ring", "answered", "left", "ended"])
async def test_a_sender_bug_never_changes_a_call(house, hook):
    house.push.raise_on.add(hook)
    placed = house.everyone()
    assert placed.ok                                                   # even when the ring itself raised
    house.online("dina")
    house.registry.decline(placed.call_id, house.users["dina"])
    house.registry.answer(placed.call_id, house.users["alice"])
    house.registry.hangup(placed.call_id, house.users["kitchen"])
    assert placed.call_id not in house.registry._calls
    assert house.conns["kitchen"].house()[-1][0] == "ended"


async def test_a_ring_that_reached_nobody_leaves_the_call_a_socket_call(house):
    class Capped(FakePush):
        def house_ring(self, call_id, from_ref, callees, ring_s):
            super().house_ring(call_id, from_ref, callees, ring_s)
            return set()                                               # every callee capped or without a registration

    capped = Capped(house.users["dina"])
    house.hass.data[DOMAIN][const.DATA_PUSH] = capped
    placed = house.dial("kitchen", "dina")
    house.registry.hangup(placed.call_id, house.users["kitchen"])
    assert capped.kinds() == ["ring"]                                  # nothing was pushed, so nothing is ended


# ---- the roster follows the push state -----------------------------------------------------------------------------

async def test_the_roster_is_told_when_a_phone_becomes_or_stops_being_push_only(house, hass):
    house.push.users.discard(house.users["dina"])
    house.registry._roster_changed()
    house.conns["alice"].clear()
    rev0 = house.registry.roster_rev()
    house.push.users.add(house.users["dina"])
    async_dispatcher_send(hass, const.SIGNAL_PHONES_CHANGED)
    assert house.conns["alice"].types() == ["house_roster_changed"]
    assert house.registry.roster_rev() != rev0
    async_dispatcher_send(hass, const.SIGNAL_PHONES_CHANGED)           # a send result, nothing moved: silence
    assert house.conns["alice"].types() == ["house_roster_changed"]
    house.push.users.discard(house.users["dina"])
    async_dispatcher_send(hass, const.SIGNAL_PHONES_CHANGED)
    assert house.conns["alice"].types() == ["house_roster_changed"] * 2
    house.registry.stop()
    house.push.users.add(house.users["dina"])
    async_dispatcher_send(hass, const.SIGNAL_PHONES_CHANGED)           # stopped: no listener
    assert house.conns["alice"].types() == ["house_roster_changed"] * 2


async def test_a_door_or_a_busy_phone_adds_no_roster_event_through_the_push_signal(house, hass):
    house.dial("alice", "bob")
    house.conns["alice"].clear()
    async_dispatcher_send(hass, const.SIGNAL_PHONES_CHANGED)
    assert house.conns["alice"].events == []


async def test_house_calls_off_means_the_sender_is_never_asked(hass, hass_storage):
    h = await build_house(hass, hass_storage, phones=(("alice", True), ("dina", True)))
    push = FakePush(h.users["dina"])
    hass.data[DOMAIN][const.DATA_PUSH] = push
    async_stop_house(hass)
    assert get_house(hass) is None
    store = get_content_store(hass)
    state = store.call_state({"label": "Dina", "target": {"type": "phone", "id": "dina"}})
    assert state["available"] is False and state["reason"] != REASON_PUSH_ONLY
    h.door.ring("gate-1", "g1")
    assert push.log == []


# ---- the real manager and sender, over the local fake Google --------------------------------------------------------

async def _real(hass, hass_storage, google, *, users=("dina",), **options):
    h = await build_house(hass, hass_storage, phones=(("alice", True), ("dina", True)))
    entry = _push_entry(hass, hass_storage, google, **options)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    manager = _manager(hass)
    rows = {}
    for who in users:
        row = await manager.registry.async_register(
            ha_user_id=h.users[who], installation_id=f"inst-{who}", kind="fid", registration=f"REG-{who.upper()}-0123456789",
            package=const.PORTICO_PACKAGE, fcm_project_id="portico-test", app_version="0.6.0", name=f"{who}'s phone")
        await manager.registry.async_set_enabled(row["reg_id"], True)
        rows[who] = row
    h.online("alice")
    return h, manager, rows


async def test_the_real_sender_pushes_a_ct_house_ring_for_an_away_phone_and_dismisses_it(
        hass, hass_storage, google):
    h, manager, rows = await _real(hass, hass_storage, google)
    assert h.registry.presence("dina")["state"] == "push_only"
    placed = h.dial("alice", "dina")
    assert placed.ok
    await manager.notifier.async_drain()
    (ring,) = google.sent_messages()
    assert ring["fid"] == rows["dina"]["registration"]
    assert ring["data"] == payload.build_house_ring(house_from_ref("alice"), placed.call_id, int(ring["data"]["t"]), 45)
    assert "door_ref" not in ring["data"] and ring["data"]["ct"] == "house"
    h.registry.hangup(placed.call_id, h.users["alice"])                    # the caller gives up
    await manager.notifier.async_drain()
    kinds = [(m["data"]["kind"], m["data"]["ct"]) for m in google.sent_messages()]
    assert kinds == [("ring", "house"), ("ended", "house")]
    assert manager.counters["ok"] == 2


async def test_the_real_sender_sends_nothing_for_a_phone_on_the_socket_or_one_that_answers(hass, hass_storage, google):
    h, manager, rows = await _real(hass, hass_storage, google)
    h.online("dina")
    placed = h.dial("alice", "dina")
    assert placed.ok
    h.registry.answer(placed.call_id, h.users["dina"])                     # answered on the socket
    h.registry.hangup(placed.call_id, h.users["alice"])
    await manager.notifier.async_drain()
    assert google.sent_messages() == []


async def test_the_real_sender_follows_the_away_rings_own_switches(hass, hass_storage, google):
    h, manager, rows = await _real(hass, hass_storage, google, enabled=False)
    assert h.registry.presence("dina")["state"] == "offline"
    assert h.dial("alice", "dina").code == CODE_TARGET_OFFLINE
    assert google.sent_messages() == []


async def test_the_real_sender_is_not_reachable_with_known_bad_credentials_or_a_dead_registration(
        hass, hass_storage, google):
    h, manager, rows = await _real(hass, hass_storage, google)
    manager._credentials_invalid = True
    assert h.registry.presence("dina")["state"] == "offline"
    manager._credentials_invalid = False
    assert h.registry.presence("dina")["state"] == "push_only"
    manager.registry.mark_dead(rows["dina"]["reg_id"], "unregistered")      # a revoked token
    assert h.registry.presence("dina")["state"] == "offline"
    assert h.dial("alice", "dina").code == CODE_TARGET_OFFLINE
    await manager.registry.async_set_enabled(rows["dina"]["reg_id"], False)
    assert google.sent_messages() == []


async def test_a_house_call_to_the_gate_is_refused_and_never_pushes_and_push_only_phones_stay_in_the_rev(house):
    """3f x 3c: the gate is a Talk target placed by the content store, never by the house registry; asking the registry for a
    `door` target is refused and the sender is never asked. The roster rev still moves with the push_only set."""
    placed = house.place("alice", "door", "gate-1")
    assert not placed.ok and house.push.log == []
    assert house.registry.target_state("door", "gate-1") is None
    away_rev = house.registry.roster_rev()
    house.online("dina")                  # callable either way (unreachable() is None): only the push_only flag differs
    online_rev = house.registry.roster_rev()
    house.offline("dina")
    assert online_rev != away_rev and house.registry.roster_rev() == away_rev


async def test_a_door_ring_through_the_real_sender_is_a_door_message_and_never_ct_house(hass, hass_storage, google):
    h, manager, rows = await _real(hass, hass_storage, google)
    h.door.ring("gate-1", "g1")
    await manager.notifier.async_drain()
    for message in google.sent_messages():
        assert "ct" not in message["data"] and "from_ref" not in message["data"] and "door_ref" in message["data"]
