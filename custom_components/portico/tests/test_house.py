"""The house-call registry (M7 3a; house calls design 4.1, 5, 7, 15.11 to 15.14): the state machine, the roster, "Everyone",
presence and Do Not Disturb, doors outranking house calls, the caps. All with fakes: no media, no network, no clock but
HA's own (`async_fire_time_changed`). INERT: nothing here rings a real phone."""
from __future__ import annotations

import pytest

from ..call_endpoints import (
    REASON_BUSY,
    REASON_DND,
    REASON_NO_RECEIVERS,
    REASON_OFFLINE,
    REASON_RECEIVE_OFF,
    get_call_registry,
)
from ..endpoints import get_endpoint_registry
from ..house import (
    CODE_ALREADY_ANSWERED,
    CODE_BUSY,
    CODE_FORBIDDEN,
    CODE_NOT_CALLABLE,
    CODE_PLACE_FORBIDDEN,
    CODE_TARGET_DND,
    CODE_TARGET_OFFLINE,
    CODE_UNKNOWN_CALL,
    HOUSE_PREFIX,
    async_setup_house,
    async_stop_house,
    get_house,
    house_from_ref,
)
from ..house_ledger import HOUSE_CAP
from ..phone_entry import async_set_house_receive, phone_entries
from .house_env import FakeConn, build_house, phone_entry
from .test_push_registry import _custom_integrations  # noqa: F401 - autouse fixture by name

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def house(hass, hass_storage):
    h = await build_house(hass, hass_storage)
    for endpoint in ("alice", "dina", "kitchen"):
        h.online(endpoint)
    for conn in h.conns.values():
        conn.clear()
    return h


def _ids(h, who):
    return h.users[who]


# ---- placing a call to one endpoint --------------------------------------------------------------------------------

async def test_a_call_rings_the_caller_and_the_callee_with_a_house_id_and_no_media(house):
    result = house.dial("alice", "dina")
    assert result.ok and result.call_id.startswith(HOUSE_PREFIX) and result.media is None
    assert house.conns["alice"].events == [
        {"type": "house_call", "call_id": result.call_id, "state": "ringing", "from": "alice", "to": "dina"}]
    assert house.conns["dina"].events == [
        {"type": "house_call", "call_id": result.call_id, "state": "ringing", "from": "alice", "to": "dina"}]
    assert house.conns["kitchen"].events == []                  # nobody else hears of it


async def test_the_default_id_is_distinct_from_a_door_id(hass, hass_storage):
    h = await build_house(hass, hass_storage)
    real = h.registry._default_id()
    assert real.startswith("hso-") and not real.startswith("gso-") and real != h.registry._default_id()


async def test_answer_goes_to_answered_then_the_audio_deadline_ends_it_in_error(house):
    call_id = house.dial("alice", "dina").call_id
    result, code = house.registry.answer(call_id, _ids(house, "dina"))
    assert code is None and result == {"call_id": call_id, "media": None}
    assert house.conns["alice"].house() == [("ringing", "dina", None), ("answered", "dina", None)]
    assert house.conns["dina"].house() == [("ringing", "dina", None), ("answered", "dina", None)]
    await house.advance(9)
    assert house.conns["alice"].house()[-1] == ("answered", "dina", None)      # still inside the deadline
    await house.advance(2)                                                      # 10 s with no callee frame
    assert house.conns["alice"].house()[-1] == ("ended", "dina", "error")
    assert house.conns["dina"].house()[-1] == ("ended", "dina", "error")
    assert house.registry.ledger.house_calls() == 0


async def test_the_first_callee_frame_makes_it_active_and_the_deadline_is_off(house):
    call_id = house.dial("alice", "dina").call_id
    house.registry.answer(call_id, _ids(house, "dina"))
    await house.advance(5)
    assert house.registry.mark_active(call_id) is True
    assert house.conns["alice"].house()[-1] == ("active", "dina", None)
    await house.advance(60)
    assert house.conns["alice"].house()[-1] == ("active", "dina", None)        # no `error` at 10 s
    assert house.registry.mark_active(call_id) is False                         # only from `answered`
    ok, code = house.registry.hangup(call_id, _ids(house, "alice"))
    assert ok and code is None
    assert house.conns["alice"].house()[-1] == ("ended", "dina", "hangup")
    assert house.conns["dina"].house()[-1] == ("ended", "dina", "hangup")


async def test_nobody_answers_in_45_seconds(house):
    house.dial("alice", "dina")
    await house.advance(44)
    assert house.conns["dina"].house() == [("ringing", "dina", None)]
    await house.advance(2)
    assert house.conns["alice"].house()[-1] == ("ended", "dina", "no_answer")
    assert house.conns["dina"].house()[-1] == ("ended", "dina", "no_answer")
    assert house.registry.ledger.house_calls() == 0


async def test_a_decline_is_terminal_on_a_one_to_one_call(house):
    call_id = house.dial("alice", "dina").call_id
    ok, _ = house.registry.decline(call_id, _ids(house, "dina"))
    assert ok
    assert house.conns["alice"].house()[-1] == ("ended", "dina", "declined")
    assert house.conns["dina"].house()[-1] == ("ended", "dina", "declined")


async def test_the_caller_cancels_while_ringing_and_a_callee_hanging_up_is_a_decline(house):
    first = house.dial("alice", "dina").call_id
    assert house.registry.hangup(first, _ids(house, "alice")) == (True, None)
    assert house.conns["dina"].house()[-1] == ("ended", "dina", "cancelled")
    second = house.dial("alice", "dina").call_id
    assert house.registry.hangup(second, _ids(house, "dina")) == (True, None)
    assert house.conns["alice"].house()[-1] == ("ended", "dina", "declined")


async def test_events_for_an_ended_call_are_dropped_and_ended_is_terminal(house):
    call_id = house.dial("alice", "dina").call_id
    house.registry.hangup(call_id, _ids(house, "alice"))
    sent = [len(c.events) for c in house.conns.values()]
    assert house.registry.answer(call_id, _ids(house, "dina")) == (None, CODE_UNKNOWN_CALL)
    assert house.registry.decline(call_id, _ids(house, "dina")) == (False, CODE_UNKNOWN_CALL)
    assert house.registry.hangup(call_id, _ids(house, "alice")) == (False, CODE_UNKNOWN_CALL)
    assert house.registry.mark_active(call_id) is False
    await house.advance(120)                                                    # the timers were cancelled too
    assert [len(c.events) for c in house.conns.values()] == sent


async def test_a_stranger_learns_nothing_about_a_call(house):
    call_id = house.dial("alice", "dina").call_id
    house.registry.attach(FakeConn("u-stranger"), 1)
    for method in (house.registry.answer, house.registry.decline, house.registry.hangup):
        assert method(call_id, "u-stranger")[1] == CODE_UNKNOWN_CALL
        assert method(call_id, None)[1] == CODE_UNKNOWN_CALL
        assert method("hso-nonsense", _ids(house, "dina"))[1] == CODE_UNKNOWN_CALL
    assert house.registry.answer(call_id, _ids(house, "alice")) == (None, CODE_UNKNOWN_CALL)   # the caller cannot answer


async def test_a_loser_of_the_race_is_told_already_answered_not_unknown(house):
    call_id = house.everyone().call_id
    assert house.registry.answer(call_id, _ids(house, "dina"))[1] is None
    assert house.registry.answer(call_id, _ids(house, "alice")) == (None, CODE_ALREADY_ANSWERED)
    assert house.registry.hangup(call_id, _ids(house, "alice")) == (False, CODE_UNKNOWN_CALL)   # nothing to hang up


# ---- who may be called, and who is told what -----------------------------------------------------------------------

async def test_one_code_for_every_reason_a_target_cannot_be_named(house, hass):
    phone_entry(hass, "u-off", "off", "Off", receive=False)
    house.users["off"] = "u-off"
    codes = {
        "self": house.dial("alice", "alice"),
        "unknown": house.place("alice", "phone", "nobody"),
        "receive off": house.dial("alice", "off"),
        "wrong kind for the id": house.place("alice", "dashboard", "dina"),
        "an unknown group": house.place("kitchen", "group", "nobody"),
    }
    assert {name: (r.ok, r.code) for name, r in codes.items()} == {name: (False, CODE_NOT_CALLABLE) for name in codes}
    assert len({r.message for r in codes.values()}) == 1      # the words never say which cause
    assert house.registry.ledger.house_calls() == 0


async def test_an_offline_callee_and_one_in_dnd_are_told_before_any_ring(house):
    house.offline("dina")
    result = house.dial("alice", "dina")
    assert (result.ok, result.code) == (False, CODE_TARGET_OFFLINE)
    house.online("dina", dnd=True)
    result = house.dial("alice", "dina")
    assert (result.ok, result.code) == (False, CODE_TARGET_DND)
    assert house.conns["dina"].house() == [] and house.registry.ledger.house_calls() == 0


async def test_one_call_per_endpoint(house):
    first = house.dial("alice", "dina")
    assert first.ok
    assert house.dial("kitchen", "dina").code == CODE_BUSY           # the callee is in a call
    assert house.dial("kitchen", "alice").code == CODE_BUSY          # so is the caller of the first
    assert house.dial("alice", "kitchen").code == CODE_BUSY          # the caller may not place a second
    assert house.registry.ledger.house_calls() == 1
    house.registry.hangup(first.call_id, _ids(house, "alice"))
    assert house.dial("kitchen", "dina").ok                           # free again


async def test_at_most_four_house_calls_at_once(hass, hass_storage):
    phones = tuple((f"p{n}", True) for n in range(10))
    h = await build_house(hass, hass_storage, phones=phones, kitchen=False)
    for endpoint in h.users:
        h.online(endpoint)
    calls = [h.dial(f"p{2 * n}", f"p{2 * n + 1}") for n in range(HOUSE_CAP)]
    assert all(c.ok for c in calls)
    fifth = h.dial("p8", "p9")
    assert (fifth.ok, fifth.code) == (False, CODE_BUSY)
    h.registry.hangup(calls[0].call_id, h.users["p0"])
    assert h.dial("p8", "p9").ok


async def test_a_phone_may_not_call_everyone_and_the_server_says_so(house):
    result = house.place("alice", "group", "everyone")
    assert (result.ok, result.code) == (False, CODE_FORBIDDEN)
    assert house.conns["dina"].events == []


async def test_an_administrator_cannot_place_a_call_as_another_endpoint(house):
    result = house.dial("alice", "dina")
    house.registry.hangup(result.call_id, _ids(house, "alice"))
    refused = house.place("alice", "phone", "dina", acting_user_id="u-admin")
    assert (refused.ok, refused.code) == (False, CODE_PLACE_FORBIDDEN)
    mismatch = house.registry.place_call(caller_user_id="u-dina", target_type="phone", target_id="alice",
                                         caller_endpoint="alice")
    assert (mismatch.ok, mismatch.code) == (False, CODE_NOT_CALLABLE)       # the endpoint is not that user's


async def test_a_door_target_is_not_a_house_call_yet(house):
    result = house.place("alice", "door", "fake:gate-1")
    assert (result.ok, result.code) == (False, "not_available")


# ---- Everyone ---------------------------------------------------------------------------------------------------

async def test_everyone_rings_every_receiving_online_phone_but_not_the_caller(house):
    placed = house.everyone()
    assert placed.ok
    assert house.conns["kitchen"].house() == [("ringing", "everyone", None)]
    assert house.conns["alice"].house() == [("ringing", "alice", None)]
    assert house.conns["dina"].house() == [("ringing", "dina", None)]


async def test_first_answer_wins_and_the_others_stop_ringing_silently(house):
    call_id = house.everyone().call_id
    result, code = house.registry.answer(call_id, _ids(house, "dina"))
    assert code is None and result["call_id"] == call_id
    assert house.conns["kitchen"].house() == [("ringing", "everyone", None), ("answered", "dina", None)]
    assert house.conns["dina"].house() == [("ringing", "dina", None), ("answered", "dina", None)]
    assert house.conns["alice"].house() == [("ringing", "alice", None), ("ended", "alice", "answered_elsewhere")]
    assert house.registry.answer(call_id, _ids(house, "alice")) == (None, CODE_ALREADY_ANSWERED)
    house.registry.hangup(call_id, _ids(house, "dina"))
    assert house.conns["alice"].house()[-1] == ("ended", "alice", "answered_elsewhere")    # not told twice


async def test_a_decline_on_everyone_is_local_and_all_declined_ends_it(house):
    call_id = house.everyone().call_id
    house.registry.decline(call_id, _ids(house, "alice"))
    assert house.conns["alice"].house()[-1] == ("ended", "alice", "declined")
    assert house.conns["dina"].house() == [("ringing", "dina", None)]            # dina still rings
    assert house.conns["kitchen"].house() == [("ringing", "everyone", None)]     # the caller is not told yet
    house.registry.decline(call_id, _ids(house, "dina"))
    assert house.conns["kitchen"].house()[-1] == ("ended", "everyone", "declined")
    assert house.registry.ledger.house_calls() == 0


async def test_everyone_stops_at_45_seconds_for_everybody(house):
    house.everyone()
    await house.advance(46)
    for who in ("kitchen", "alice", "dina"):
        assert house.conns[who].house()[-1][2] == "no_answer"


async def test_the_everyone_set_follows_the_table_of_15_14(hass, hass_storage):
    """Rings: online, receive on, not DND, idle. Skipped: receive off, offline, DND, a door call (live or held), already
    in a house call. Only "ringer" is left."""
    h = await build_house(hass, hass_storage, phones=(
        ("ringer", True), ("off", False), ("offline", True), ("dnd", True), ("indoor", True), ("busy", True),
        ("partner", True)))
    for endpoint in ("kitchen", "ringer", "off", "dnd", "indoor", "busy", "partner"):
        h.online(endpoint, dnd=True if endpoint == "dnd" else None)
    reservation = h.registry.reserve_door_answer(h.users["indoor"], "g1")
    h.registry.settle_door_answer(reservation, True, "gate-1")             # indoor holds a door call
    assert h.place("busy", "phone", "partner").ok                          # busy and partner are in a house call
    for conn in h.conns.values():
        conn.clear()
    assert h.everyone().ok
    assert h.conns["ringer"].house() == [("ringing", "ringer", None)]
    for silent in ("off", "dnd", "indoor", "busy", "partner"):
        assert h.conns[silent].house() == [], silent
    assert "offline" not in h.conns
    (entry,) = h.registry.group_entries(get_endpoint_registry(hass).get("kitchen"))
    assert entry["count"] == 0 and entry["reason"] == REASON_BUSY          # the screen is on a call now


async def test_an_empty_set_is_refused_at_placement_and_never_reported_as_everyone_declined(house, hass):
    for endpoint in ("alice", "dina"):
        async_set_house_receive(hass, phone_entries(hass)[0 if endpoint == "alice" else 1], False)
    house.conns["kitchen"].clear()
    result = house.everyone()
    assert (result.ok, result.code) == (False, CODE_NOT_CALLABLE)
    assert house.conns["kitchen"].events == [] and house.registry.ledger.house_calls() == 0


async def test_a_set_emptied_by_a_door_answer_ends_busy_not_declined(hass, hass_storage):
    h = await build_house(hass, hass_storage, phones=(("alice", True),))
    h.online("kitchen")
    h.online("alice")
    call_id = h.everyone().call_id
    reservation = h.registry.reserve_door_answer(h.users["alice"], "g1")
    h.registry.settle_door_answer(reservation, True, "gate-1")
    assert h.conns["kitchen"].house()[-1] == ("ended", "everyone", "busy")
    assert call_id not in h.registry._calls


async def test_everyone_while_the_screen_is_itself_busy_is_refused(house):
    house.dial("alice", "kitchen")
    assert house.everyone().code == CODE_BUSY


# ---- the roster (CB-10), receive (HC-2) and the Everyone key ---------------------------------------------------

async def test_target_state_says_why_in_a_fixed_order(house, hass):
    reg = house.registry
    ready = reg.target_state("phone", "dina")
    assert (ready.label, ready.available, ready.reason) == ("Dina", True, None)
    assert reg.target_state("phone", "nobody") is None and reg.target_state("dashboard", "dina") is None
    assert reg.target_state("door", "fake:gate-1") is None
    house.offline("dina")
    assert reg.target_state("phone", "dina").reason == REASON_OFFLINE
    house.online("dina", dnd=True)
    assert reg.target_state("phone", "dina").reason == REASON_DND
    house.registry.set_dnd(house.conns["dina"], False)
    house.dial("alice", "kitchen")
    house.dial("kitchen", "alice")                                          # refused: busy caller; kitchen is in a call
    assert reg.target_state("dashboard", "kitchen").reason == REASON_BUSY
    async_set_house_receive(hass, phone_entries(hass)[1], False)
    assert reg.target_state("phone", "dina").reason == REASON_RECEIVE_OFF   # receive off outranks everything else


async def test_receive_is_off_by_default_and_a_phone_without_it_is_not_listed(hass, hass_storage):
    h = await build_house(hass, hass_storage, phones=(("alice", True), ("dina", None)))     # dina: no option at all
    assert h.registry.receives(get_endpoint_registry(hass).get("dina")) is False
    assert h.registry.receives(get_endpoint_registry(hass).get("alice")) is True
    listed = {e.id for e in h.registry.endpoints(caller_user_id=h.users["alice"])}
    assert listed == {"kitchen"}                                            # not the caller, not a phone with receive off
    assert {e.id for e in h.registry.endpoints(caller_user_id=h.users["kitchen"])} == {"alice"}


async def test_the_everyone_key_exists_for_a_screen_only_and_counts_the_phones(house):
    alice = get_endpoint_registry(house.hass).get("alice")
    kitchen = get_endpoint_registry(house.hass).get("kitchen")
    assert house.registry.group_entries(alice) == []                            # HC-9: a phone has no such key
    assert house.registry.group_entries(None) == []
    (entry,) = house.registry.group_entries(kitchen)
    assert (entry["id"], entry["kind"], entry["name"], entry["available"], entry["reason"], entry["count"]) == (
        "everyone", "group", "Everyone", True, None, 2)
    assert entry["action_id"].startswith("r-")


async def test_the_everyone_key_is_off_with_its_own_words_when_no_phone_would_ring(house, hass):
    kitchen = get_endpoint_registry(hass).get("kitchen")
    house.offline("alice")
    house.offline("dina")
    (entry,) = house.registry.group_entries(kitchen)
    assert (entry["available"], entry["reason"], entry["count"]) == (False, REASON_NO_RECEIVERS, 0)
    house.online("alice")
    house.dial("alice", "kitchen")
    (entry,) = house.registry.group_entries(kitchen)
    assert entry["reason"] == REASON_BUSY                                       # the screen itself is on a call


async def test_removing_a_phone_or_switching_receive_off_ends_the_call_it_is_in(house, hass):
    call_id = house.dial("alice", "dina").call_id
    async_set_house_receive(hass, phone_entries(hass)[1], False)
    assert house.conns["alice"].house()[-1] == ("ended", "dina", "error")
    assert call_id not in house.registry._calls
    # a phone entry that is removed: the same
    again = house.dial("alice", "kitchen").call_id
    await hass.config_entries.async_remove(phone_entries(hass)[0].entry_id)
    await hass.async_block_till_done()
    assert again not in house.registry._calls


async def test_receive_survives_an_options_edit_of_the_doors(hass, hass_storage):
    h = await build_house(hass, hass_storage)
    entry = phone_entries(hass)[0]
    from ..phone_flow import PhoneOptionsFlow

    assert PhoneOptionsFlow                                                 # the real Configure flow, below
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"all_doors": False, "doors": ["gate-1"]})
    assert result["type"].value == "create_entry"
    assert phone_entries(hass)[0].options["house_receive"] is True and phone_entries(hass)[0].options["doors"] == ["gate-1"]
    assert h.registry.receives(get_endpoint_registry(hass).get("alice"))


# ---- presence (HC-3) and Do Not Disturb (HC-4) ------------------------------------------------------------------

async def test_online_means_an_open_subscription_and_nothing_else(hass, hass_storage):
    h = await build_house(hass, hass_storage)
    assert h.registry.presence("alice") == {"state": "offline", "dnd": False}
    h.online("alice")
    assert h.registry.presence("alice") == {"state": "online", "dnd": False}
    h.offline("alice")
    assert h.registry.presence("alice") == {"state": "offline", "dnd": False}
    assert h.registry.presence("nobody") is None


async def test_presence_lives_in_memory_and_never_in_ha_state(hass, hass_storage):
    from homeassistant.const import EVENT_STATE_CHANGED
    from pytest_homeassistant_custom_component.common import async_capture_events

    h = await build_house(hass, hass_storage)
    before = {s.entity_id for s in hass.states.async_all()}
    changes = async_capture_events(hass, EVENT_STATE_CHANGED)
    h.online("alice", dnd=True)
    h.online("dina")
    h.offline("alice")
    await hass.async_block_till_done()
    assert {s.entity_id for s in hass.states.async_all()} == before and changes == []


async def test_dnd_is_the_latest_report_of_the_users_open_connections_and_clears_on_drop(hass, hass_storage):
    h = await build_house(hass, hass_storage)
    first = h.online("alice", dnd=True)
    assert h.registry.presence("alice")["dnd"] is True
    second = FakeConn(h.users["alice"], msg_id=2)
    handle = h.registry.attach(second, 2)
    assert h.registry.presence("alice")["dnd"] is True                   # the second has not reported: the first stands
    h.registry.set_dnd(second, False)
    assert h.registry.presence("alice")["dnd"] is False                   # the latest report wins
    h.registry.set_dnd(first, True)
    assert h.registry.presence("alice")["dnd"] is True
    handle.detach()
    assert h.registry.presence("alice")["dnd"] is True                    # the first's report stands
    h.offline("alice")
    assert h.registry.presence("alice") == {"state": "offline", "dnd": False}   # cleared with the connection
    assert h.registry.set_dnd(first, True) is False                       # a connection that left cannot report


async def test_the_roster_is_told_to_refetch_when_presence_changes(house):
    rev0 = house.registry.roster_rev()
    house.registry.set_dnd(house.conns["dina"], True)
    assert house.conns["alice"].types() == ["house_roster_changed"]
    event = house.conns["alice"].events[0]
    assert event["rev"] == house.registry.roster_rev() != rev0
    house.registry.set_dnd(house.conns["dina"], True)                      # no change in what anyone shows: nothing sent
    assert house.conns["alice"].types() == ["house_roster_changed"]
    newcomer = FakeConn("u-late")
    house.registry.attach(newcomer, 1)
    assert newcomer.events == []                                          # a new subscription reads the roster itself


async def test_a_caller_that_drops_while_ringing_cancels_and_a_callee_that_drops_ends_it(house):
    first = house.dial("alice", "dina").call_id
    house.offline("alice")
    assert house.conns["dina"].house()[-1] == ("ended", "dina", "cancelled") and first not in house.registry._calls
    second = house.dial("dina", "kitchen").call_id
    house.offline("kitchen")
    assert house.conns["dina"].house()[-1] == ("ended", "kitchen", "error") and second not in house.registry._calls


async def test_a_dropped_everyone_callee_leaves_the_others_ringing(house):
    call_id = house.everyone().call_id
    house.offline("alice")
    assert call_id in house.registry._calls
    assert house.conns["dina"].house() == [("ringing", "dina", None)]
    house.offline("dina")
    assert house.conns["kitchen"].house()[-1] == ("ended", "everyone", "error")     # emptied by drops, not declines


async def test_a_new_subscription_gets_the_calls_it_is_in_as_snapshots(house):
    call_id = house.dial("alice", "dina").call_id
    house.offline("dina")                                  # the ring ended for the dropped callee
    assert call_id not in house.registry._calls
    call_id = house.dial("alice", "kitchen").call_id
    handle_conn = FakeConn(house.users["kitchen"], msg_id=7)
    handle = house.registry.attach(handle_conn, 7)
    assert handle.snapshots() == [{"type": "house_call", "call_id": call_id, "state": "ringing", "from": "alice",
                                   "to": "kitchen", "snapshot": True}]
    caller = house.handles["alice"].snapshots()
    assert caller == [{"type": "house_call", "call_id": call_id, "state": "ringing", "from": "alice", "to": "kitchen",
                       "snapshot": True}]
    house.registry.answer(call_id, house.users["kitchen"])
    assert house.handles["alice"].snapshots()[0]["to"] == "kitchen" and house.handles["alice"].snapshots()[0]["state"] == "answered"
    assert house.registry.attach(FakeConn("u-stranger"), 1).snapshots() == []


# ---- a door outranks a house call (rule 0, design 7) --------------------------------------------------------------

async def test_a_door_ringing_ends_a_one_to_one_ring_and_both_ends_are_told_busy(house):
    call_id = house.dial("alice", "dina").call_id
    house.door.ring("gate-1", "g1")
    assert call_id not in house.registry._calls
    assert house.conns["alice"].house()[-1] == ("ended", "dina", "busy")
    assert house.conns["dina"].house()[-1] == ("ended", "dina", "busy")


async def test_a_door_ringing_displaces_the_everyone_callees_silently_then_cancels_the_screen(house):
    house.everyone()
    house.door.ring("gate-1", "g1")                  # reaches every phone and the screen
    assert house.conns["kitchen"].house()[-1] == ("ended", "everyone", "cancelled")


async def test_a_door_that_does_not_reach_an_endpoint_leaves_its_call_alone(hass, hass_storage):
    h = await build_house(hass, hass_storage, phones=(("alice", True), ("dina", True)), kitchen=False)
    for entry in phone_entries(hass):
        hass.config_entries.async_update_entry(entry, options={**entry.options, "doors": ["some-other-door"]})
    h.online("alice")
    h.online("dina")
    call_id = h.dial("alice", "dina").call_id
    h.door.ring("gate-1", "g1")                       # neither phone is enrolled for this door
    assert call_id in h.registry._calls and h.conns["dina"].house() == [("ringing", "dina", None)]
    assert h.dial("alice", "dina").code == CODE_BUSY  # busy by the house call, not by the door
    assert h.registry.busy_state(get_endpoint_registry(hass).get("alice")) == "house_ringing"


async def test_a_talking_call_is_left_alone_by_a_ringing_door_and_answering_the_door_ends_it(house):
    call_id = house.dial("alice", "dina").call_id
    house.registry.answer(call_id, _ids(house, "dina"))
    house.registry.mark_active(call_id)
    sent = len(house.conns["alice"].events)
    house.door.ring("gate-1", "g1")                                     # the door shows over the call: nothing ends
    assert call_id in house.registry._calls and len(house.conns["alice"].events) == sent
    reservation = house.registry.reserve_door_answer(_ids(house, "dina"), "g1")
    house.registry.settle_door_answer(reservation, True, "gate-1")      # dina answers the door
    assert call_id not in house.registry._calls
    assert house.conns["alice"].house()[-1] == ("ended", "dina", "hangup")      # the far end sees the call end
    assert house.conns["dina"].house()[-1] == ("ended", "dina", "hangup")


async def test_an_outgoing_ring_is_cancelled_when_its_caller_answers_a_door(house):
    call_id = house.dial("alice", "dina").call_id
    reservation = house.registry.reserve_door_answer(_ids(house, "alice"), "g1")
    house.registry.settle_door_answer(reservation, True, "gate-1")
    assert call_id not in house.registry._calls
    assert house.conns["dina"].house()[-1] == ("ended", "dina", "cancelled")


async def test_nothing_can_be_placed_while_a_door_rings_or_is_held_by_the_caller_or_the_callee(house):
    house.door.ring("gate-1", "g1")
    assert house.dial("alice", "dina").code == CODE_BUSY                       # a door ringing: a tap does not win
    reservation = house.registry.reserve_door_answer(_ids(house, "dina"), "g1")
    house.registry.settle_door_answer(reservation, True, "gate-1")
    house.door.state("g1", "answered")
    assert house.dial("alice", "kitchen").ok is True                           # the ring is over; dina alone is busy
    house.registry.hangup(next(iter(house.registry._calls)), _ids(house, "alice"))
    assert house.dial("alice", "dina").code == CODE_BUSY                       # the callee holds a door call
    assert house.dial("dina", "alice").code == CODE_BUSY                       # the caller holds a door call
    house.door.state("g1", "ended")
    assert house.dial("dina", "alice").ok


async def test_a_released_door_claim_rings_again_and_frees_the_slot(house):
    house.door.ring("gate-1", "g1")
    reservation = house.registry.reserve_door_answer(_ids(house, "dina"), "g1")
    house.registry.settle_door_answer(reservation, True, "gate-1")
    house.door.state("g1", "answered")
    assert house.registry.ledger.door_sessions(_ids(house, "dina")) == 1
    house.door.state("g1", "ringing")                                          # answered -> ringing: a released claim
    assert house.registry.ledger.door_sessions(_ids(house, "dina")) == 0
    assert house.registry.ledger.door_rings() == ["g1"]


async def test_a_door_that_leaves_frees_its_endpoints(house):
    house.door.ring("gate-1", "g1")
    assert house.dial("alice", "dina").code == CODE_BUSY
    house.door.remove_door("gate-1")
    assert house.registry.ledger.door_rings() == []
    assert house.dial("alice", "dina").ok


async def test_a_ledger_failure_is_contained_in_the_door_listener(house, monkeypatch, caplog):
    monkeypatch.setattr(house.registry.ledger, "door_ringing", lambda *a, **k: 1 / 0)
    house.door.ring("gate-1", "g1")                                            # must not raise into the provider fan-out
    assert "door path is unaffected" in caplog.text


# ---- stay out of the door path and the old system -----------------------------------------------------------------

async def test_a_house_call_never_reaches_the_provider_registry_or_a_door_provider(house, monkeypatch):
    from ..provider import ProviderRegistry

    def boom(*a, **k):
        raise AssertionError("a house call id must never be noted as a door call")

    monkeypatch.setattr(ProviderRegistry, "note_call", boom)
    seen = []
    from ..provider import DATA_PROVIDERS

    registry = house.hass.data["portico"][DATA_PROVIDERS]
    registry.subscribe(seen.append)                                            # what the push notifier is
    call_id = house.everyone().call_id
    house.registry.answer(call_id, _ids(house, "dina"))
    house.registry.hangup(call_id, _ids(house, "dina"))
    assert seen == []                                                          # no door transition, so no push, no tracker
    assert registry.for_call(call_id) is None and registry.door_of_call(call_id) is None
    assert house.door.calls == []                                              # the door provider was never asked anything


async def test_no_service_is_called_and_no_old_helper_is_touched(house, hass):
    from homeassistant.const import EVENT_CALL_SERVICE, EVENT_STATE_CHANGED
    from pytest_homeassistant_custom_component.common import async_capture_events

    services = async_capture_events(hass, EVENT_CALL_SERVICE)
    states = async_capture_events(hass, EVENT_STATE_CHANGED)
    call_id = house.everyone().call_id
    house.registry.answer(call_id, _ids(house, "dina"))
    house.registry.mark_active(call_id)
    house.registry.hangup(call_id, _ids(house, "kitchen"))
    house.dial("alice", "dina")
    await house.advance(60)
    house.door.ring("gate-1", "g1")
    await hass.async_block_till_done()
    assert services == [] and states == []        # not input_text, input_button, input_boolean, automation or anything
    assert not [s for s in hass.states.async_all() if "intercom" in s.entity_id]


async def test_the_push_shape_for_a_house_ring_has_no_door_and_an_opaque_caller(house):
    from ..push import payload

    ref = house_from_ref("alice")
    assert ref == house_from_ref("alice") and ref != house_from_ref("dina") and ref.startswith("e-") and len(ref) == 34
    assert "alice" not in ref
    ring = payload.build_house_ring(ref, "hso-1", 1_759_700_000_000, 45)
    assert ring == {"pv": "2", "kind": "ring", "call_id": "hso-1", "seq": "1", "t": "1759700000000",
                    "ct": "house", "from_ref": ref, "exp": "1759700045000"}
    assert "door_ref" not in ring
    assert payload.build_house_answered(ref, "hso-1", 1_759_700_005_000) == {
        "pv": "2", "kind": "answered", "call_id": "hso-1", "seq": "2", "t": "1759700005000", "ct": "house",
        "from_ref": ref, "by": "other"}
    assert payload.build_house_ended(ref, "hso-1", 1_759_700_006_000) == {
        "pv": "2", "kind": "ended", "call_id": "hso-1", "seq": "3", "t": "1759700006000", "ct": "house", "from_ref": ref}
    assert payload.message_bytes(payload.build_message(kind="fid", registration="r", package="p", data=ring,
                                                       now_ms=1_759_700_001_000, ring_window_s=45)) < 4096


async def test_a_door_message_without_a_door_ref_is_still_an_error(house):
    from ..push import payload

    for bad in (None, "", "http://x"):
        with pytest.raises(payload.PayloadError):
            payload.build_ring(bad, "gso-1", 1, 45)
    with pytest.raises(payload.PayloadError):
        payload.build_house_ring("has space", "hso-1", 1, 45)


# ---- off by default -----------------------------------------------------------------------------------------------

async def test_house_calls_are_always_on_and_the_old_option_is_ignored(hass):
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from ..config_flow import DOMAIN
    from ..push.const import ENTRY_TYPE, ENTRY_TYPE_PUSH

    assert get_house(hass) is None                               # before domain setup built it
    MockConfigEntry(domain=DOMAIN, data={ENTRY_TYPE: ENTRY_TYPE_PUSH}, options={"house_calls": False},
                    unique_id="push-a").add_to_hass(hass)
    on = async_setup_house(hass)                                  # D-20261122-aafb: option False is ignored
    assert on is not None and get_house(hass) is on and get_call_registry(hass) is on
    assert async_setup_house(hass) is on                         # idempotent
    async_stop_house(hass)
    assert get_house(hass) is None and type(get_call_registry(hass)).__name__ == "NullCallEndpointRegistry"
    assert async_setup_house(hass, enabled=False) is None        # the test override still leaves it off


async def test_stopping_the_registry_ends_every_call_and_lets_go_of_the_door_listener(house):
    house.dial("alice", "dina")
    from ..provider import DATA_PROVIDERS

    providers = house.hass.data["portico"][DATA_PROVIDERS]
    listeners = len(providers._listeners)
    async_stop_house(house.hass)
    assert len(providers._listeners) == listeners - 1
    assert house.conns["dina"].house()[-1] == ("ended", "dina", "error")


# ---- races, the default, and call buttons ---------------------------------------------------------------------------

async def test_two_placements_racing_for_one_endpoint_are_decided_one_after_the_other(house):
    import asyncio

    async def place(caller):
        return await house.registry.place(caller_user_id=_ids(house, caller), caller_phone=caller,
                                          target_type="phone", target_id="dina", caller_endpoint=caller)

    results = await asyncio.gather(place("alice"), place("kitchen"))
    assert sorted(r.ok for r in results) == [False, True]
    assert [r.code for r in results if not r.ok] == [CODE_BUSY]
    assert house.registry.ledger.house_calls() == 1


async def test_concurrent_answers_on_a_fan_out_have_exactly_one_winner(house):
    import asyncio

    call_id = house.everyone().call_id

    async def answer(who):
        await asyncio.sleep(0)
        return who, house.registry.answer(call_id, _ids(house, who))

    results = dict(await asyncio.gather(answer("alice"), answer("dina")))
    codes = sorted(str(code) for _result, code in results.values())
    assert codes == ["None", CODE_ALREADY_ANSWERED]
    assert sum(result is not None for result, _code in results.values()) == 1
    winner = next(w for w, (result, _c) in results.items() if result is not None)
    assert house.conns["kitchen"].house()[-1] == ("answered", winner, None)


async def test_installing_portico_rings_nobody_because_receive_is_off_by_default(hass, hass_storage):
    h = await build_house(hass, hass_storage, phones=(("alice", None), ("dina", None)))     # no option at all
    for endpoint in h.users:
        h.online(endpoint)
    result = h.everyone()
    assert (result.ok, result.code) == (False, CODE_NOT_CALLABLE)
    assert h.dial("kitchen", "alice").code == CODE_NOT_CALLABLE
    assert h.dial("alice", "dina").code == CODE_NOT_CALLABLE
    assert all(c.house() == [] for c in h.conns.values())
    (entry,) = h.registry.group_entries(get_endpoint_registry(hass).get("kitchen"))
    assert (entry["available"], entry["reason"], entry["count"]) == (False, REASON_NO_RECEIVERS, 0)


async def test_a_call_buttons_state_follows_the_receive_switch(house, hass):
    from ..content import get_content_store

    action = {"label": "Dina", "target": {"type": "phone", "id": "dina"}}
    store = get_content_store(hass)
    assert store.call_state(action) == {"label": "Dina", "available": True, "reason": None}
    async_set_house_receive(hass, phone_entries(hass)[1], False)
    assert store.call_state(action) == {"label": "Dina", "available": False, "reason": REASON_RECEIVE_OFF}
    gone = {"label": "Gone", "target": {"type": "phone", "id": "nobody"}}
    assert store.call_state(gone)["reason"] == "unknown_target"           # a removed target: the button is off


async def test_a_roster_call_is_placed_as_the_endpoint_whose_grant_ran_it(house, hass):
    """`content/run` hands the registry the caller endpoint and the acting user (the seam of this PR)."""
    from ..content import get_content_store

    store = get_content_store(hass)
    roster = store.roster_for("kitchen")
    assert [e["kind"] for e in roster][:3] == ["group", "phone", "phone"]
    run = await store.async_run(house.users["kitchen"], "Kitchen", roster[0]["action_id"],
                                caller_user_id=house.users["kitchen"], endpoint_id="kitchen")
    assert run["call_id"].startswith("hso-") and run["media"] is None
    assert house.conns["alice"].house() == [("ringing", "alice", None)]


async def test_only_endpoint_users_hear_that_the_roster_changed(house):
    stranger = FakeConn("u-stranger")
    house.registry.attach(stranger, 1)
    house.registry.set_dnd(house.conns["dina"], True)
    assert stranger.events == [] and house.conns["alice"].types() == ["house_roster_changed"]


async def test_a_screen_can_hide_the_everyone_key_with_roster_hide(house, hass):
    from ..content import get_content_store

    store = get_content_store(hass)
    assert store.roster_for("kitchen")[0]["kind"] == "group"
    store._overrides["kitchen"] = {"pages": [], "rules": [], "roster_hide": ["everyone"]}
    assert [e["kind"] for e in store.roster_for("kitchen")][0] == "phone"
