"""The push ring notification of a HOUSE call: the notifier's second, separate path (M7 3c, design 15.5).

HA-harness session, the same rig as test_push_notifier.py (a real PushRegistry, FcmClient and AccessTokenSource over the local
fake Google, an injected clock and sleep). Nothing leaves 127.0.0.1 and no real push is sent. The registry side (who is
"away", when the hooks fire) is test_house_push.py; the wire shapes are test_push_wire.py."""
from __future__ import annotations

import asyncio
import json

import pytest

from custom_components.portico.house import house_from_ref
from custom_components.portico.push import const, payload
from custom_components.portico.push.fcm import Outcome

from .fake_google import fcm_error
from .test_push_notifier import ENTRY, _kinds, _phone, _settle, google, rig  # noqa: F401 - fixtures by name

FROM = house_from_ref("ep-kitchen")
CALL = "hso-1759700000000000000-1-a1b2c3"


def _ring(rig, callees=None, call=CALL, ring_s=45):
    return rig.notifier.house_ring(call, FROM, callees if callees is not None else {"ep-1": "user-1"}, ring_s)


def _by_reg(rig, row):
    return [m["data"] for m in rig.google.sent_messages() if (m.get("fid") or m.get("token")) == row["registration"]]


# ---- the message -------------------------------------------------------------------------------------------------

async def test_a_house_ring_is_a_ct_house_push_with_an_opaque_from_ref_and_no_door_ref(rig):
    row = await _phone(rig.registry, 1)
    t_ms = int(rig.clock.wall * 1000)
    assert _ring(rig) == {"ep-1"}
    await _settle(rig)
    (message,) = rig.google.sent_messages()
    assert message["data"] == payload.build_house_ring(FROM, CALL, t_ms, 45)
    assert message["data"]["ct"] == "house" and message["data"]["from_ref"] == FROM and "door_ref" not in message["data"]
    assert message["fid"] == row["registration"]
    assert message["android"] == {"priority": "HIGH", "ttl": "45s", "restricted_package_name": const.PORTICO_PACKAGE,
                                  "direct_boot_ok": False}
    # privacy: no endpoint id, user id, phone name or caller name anywhere in what Google gets
    wire = json.dumps(message)
    for secret in ("ep-1", "ep-kitchen", "user-1", "phone 1", "Kitchen", "kitchen"):
        assert secret not in wire, secret


async def test_the_ring_window_of_the_message_is_the_house_calls_not_the_door_option(rig):
    await _phone(rig.registry, 1)
    rig.state["window"] = 90                      # the door ring window option
    _ring(rig, ring_s=30)                         # the house call rings 30 s
    await _settle(rig)
    (message,) = rig.google.sent_messages()
    assert int(message["data"]["exp"]) - int(message["data"]["t"]) == 30_000 and message["android"]["ttl"] == "30s"


async def test_only_the_named_callees_registrations_are_rung(rig):
    mine = await _phone(rig.registry, 1)
    other = await _phone(rig.registry, 2)
    assert _ring(rig, {"ep-1": "user-1"}) == {"ep-1"}
    await _settle(rig)
    assert [m["kind"] for m in _by_reg(rig, mine)] == ["ring"] and _by_reg(rig, other) == []


async def test_two_callees_each_get_their_own_ring_and_a_user_with_two_installs_gets_both(rig):
    a1 = await _phone(rig.registry, 1, user="user-a")
    a2 = await _phone(rig.registry, 11, user="user-a")
    b = await _phone(rig.registry, 2, user="user-b")
    assert _ring(rig, {"ep-a": "user-a", "ep-b": "user-b"}) == {"ep-a", "ep-b"}
    await _settle(rig)
    for row in (a1, a2, b):
        assert [m["kind"] for m in _by_reg(rig, row)] == ["ring"]
    assert len(rig.google.fcm_requests) == 3


# ---- who is NOT rung --------------------------------------------------------------------------------------------

async def test_a_callee_with_no_live_registration_is_not_rung(rig):
    assert _ring(rig, {"ep-x": "user-nobody"}) == set()
    await _phone(rig.registry, 1, enabled=False)                 # the push ring notification is switched off for that phone
    assert _ring(rig, {"ep-1": "user-1"}, call="hso-2") == set()
    await _settle(rig)
    assert rig.google.fcm_requests == []


async def test_the_global_switch_off_rings_nothing(rig):
    await _phone(rig.registry, 1)
    rig.state["enabled"] = False
    assert _ring(rig) == set()
    rig.notifier.house_answered(CALL, "ep-1")
    rig.notifier.house_ended(CALL)
    await _settle(rig)
    assert rig.google.fcm_requests == []


async def test_a_revoked_token_is_pruned_and_never_retried_and_never_rung_again(rig):
    row = await _phone(rig.registry, 1)
    rig.google.fcm_script.append(fcm_error(404, "NOT_FOUND", error_code="UNREGISTERED"))
    assert _ring(rig) == {"ep-1"}                                # at queue time the row looked fine
    await _settle(rig)
    assert len(rig.google.fcm_requests) == 1
    kept = rig.registry.get(row["reg_id"])
    assert kept["state"] == "unregistered" and kept["registration"] is None
    rig.notifier.house_ended(CALL)                               # the ring was never delivered: no dismiss
    await _settle(rig)
    assert len(rig.google.fcm_requests) == 1
    rig.clock.advance(10)
    assert _ring(rig, call="hso-2") == set()                     # and the phone is no longer a target


async def test_the_staged_test_allow_list_applies_to_a_house_ring_too(rig):
    allowed = await _phone(rig.registry, 1)
    other = await _phone(rig.registry, 2)
    rig.notifier._test_phones = lambda: [allowed["reg_id"]]
    assert _ring(rig, {"ep-1": "user-1", "ep-2": "user-2"}) == {"ep-1"}
    await _settle(rig)
    assert _by_reg(rig, other) == [] and len(_by_reg(rig, allowed)) == 1


# ---- the chain: ring, answered, ended ---------------------------------------------------------------------------

async def test_the_winner_hears_nothing_more_and_the_loser_is_told_answered_elsewhere(rig):
    winner = await _phone(rig.registry, 1, user="user-a")
    loser = await _phone(rig.registry, 2, user="user-b")
    _ring(rig, {"ep-a": "user-a", "ep-b": "user-b"})
    await _settle(rig)
    rig.notifier.house_answered(CALL, "ep-a")
    await _settle(rig)
    rig.notifier.house_ended(CALL)
    await _settle(rig)
    assert [m["kind"] for m in _by_reg(rig, winner)] == ["ring"]                 # it answered on its own app
    assert [m["kind"] for m in _by_reg(rig, loser)] == ["ring", "answered"]      # and no ended after an accepted answered
    answered = _by_reg(rig, loser)[1]
    assert answered == payload.build_house_answered(FROM, CALL, int(answered["t"]))
    assert "door_ref" not in answered and answered["by"] == "other" and answered["ct"] == "house"


async def test_a_call_that_ends_unanswered_dismisses_every_rung_phone_once(rig):
    a = await _phone(rig.registry, 1, user="user-a")
    b = await _phone(rig.registry, 2, user="user-b")
    _ring(rig, {"ep-a": "user-a", "ep-b": "user-b"})
    await _settle(rig)
    rig.notifier.house_ended(CALL)
    rig.notifier.house_ended(CALL)                                               # a repeat does nothing
    await _settle(rig)
    for row in (a, b):
        kinds = [m["kind"] for m in _by_reg(rig, row)]
        assert kinds == ["ring", "ended"]
        assert _by_reg(rig, row)[1]["ct"] == "house" and "door_ref" not in _by_reg(rig, row)[1]


async def test_a_callee_that_declined_is_sent_nothing_more_the_other_is_dismissed(rig):
    decliner = await _phone(rig.registry, 1, user="user-a")
    other = await _phone(rig.registry, 2, user="user-b")
    _ring(rig, {"ep-a": "user-a", "ep-b": "user-b"})
    await _settle(rig)
    rig.notifier.house_left(CALL, "ep-a", notify=False)
    rig.notifier.house_ended(CALL)
    await _settle(rig)
    assert [m["kind"] for m in _by_reg(rig, decliner)] == ["ring"]               # it knows; no "missed call"
    assert [m["kind"] for m in _by_reg(rig, other)] == ["ring", "ended"]


async def test_a_callee_a_door_took_is_dismissed_at_once_and_never_told_answered(rig):
    taken = await _phone(rig.registry, 1, user="user-a")
    other = await _phone(rig.registry, 2, user="user-b")
    _ring(rig, {"ep-a": "user-a", "ep-b": "user-b"})
    await _settle(rig)
    rig.notifier.house_left(CALL, "ep-a", notify=True)
    await _settle(rig)
    rig.notifier.house_answered(CALL, "ep-b")
    rig.notifier.house_ended(CALL)
    await _settle(rig)
    assert [m["kind"] for m in _by_reg(rig, taken)] == ["ring", "ended"]         # ended once, no answered after it
    assert [m["kind"] for m in _by_reg(rig, other)] == ["ring"]


async def test_an_unanswered_dismiss_is_not_sent_to_a_phone_whose_ring_never_arrived(rig):
    got = await _phone(rig.registry, 1, user="user-a")
    missed = await _phone(rig.registry, 2, user="user-b")
    rig.google.fcm_default = lambda record: (
        fcm_error(400, "INVALID_ARGUMENT", field="message.data")
        if record["json"]["message"]["fid"] == missed["registration"] else (200, {"name": "x"}, {}))
    _ring(rig, {"ep-a": "user-a", "ep-b": "user-b"})
    await _settle(rig)
    rig.notifier.house_ended(CALL)
    await _settle(rig)
    assert [m["kind"] for m in _by_reg(rig, got)] == ["ring", "ended"]
    assert [m["kind"] for m in _by_reg(rig, missed)] == ["ring"]                 # only the failed ring; no dismiss


# ---- cancelled before the push lands; failures ------------------------------------------------------------------

async def test_a_call_cancelled_while_the_ring_is_in_flight_still_dismisses_in_order(rig):
    row = await _phone(rig.registry, 1)
    rig.google.fcm_delay = 0.05
    _ring(rig)
    rig.notifier.house_ended(CALL)                                               # the caller hung up at once
    await _settle(rig)
    assert [m["kind"] for m in _by_reg(rig, row)] == ["ring", "ended"]           # never an ended before its ring


async def test_a_call_cancelled_before_the_ring_was_ever_sent_sends_nothing(rig):
    await _phone(rig.registry, 1)
    loop = asyncio.get_running_loop()
    rig.hass.async_create_background_task = lambda coro, name, **kw: loop.create_task(coro, name=name)   # not eager
    _ring(rig)
    rig.notifier.house_ended(CALL)
    await _settle(rig)
    assert rig.google.fcm_requests == []


async def test_a_ring_that_is_being_retried_stops_when_the_call_ends(rig):
    await _phone(rig.registry, 1)
    rig.google.fcm_default = lambda record: fcm_error(503, "UNAVAILABLE", error_code="UNAVAILABLE")
    real_sleep = rig.notifier._sleep

    async def sleep_and_end(seconds):
        rig.notifier.house_ended(CALL)
        await real_sleep(seconds)

    rig.notifier._sleep = sleep_and_end
    _ring(rig)
    await _settle(rig)
    assert len(rig.google.fcm_requests) == 1                                     # the retry is not made


async def test_a_ring_for_a_callee_who_declined_while_it_was_retried_stops(rig):
    await _phone(rig.registry, 1)
    rig.google.fcm_default = lambda record: fcm_error(503, "UNAVAILABLE", error_code="UNAVAILABLE")
    real_sleep = rig.notifier._sleep

    async def sleep_and_decline(seconds):
        rig.notifier.house_left(CALL, "ep-1", notify=False)
        await real_sleep(seconds)

    rig.notifier._sleep = sleep_and_decline
    _ring(rig)
    await _settle(rig)
    assert len(rig.google.fcm_requests) == 1


async def test_a_failing_push_is_retried_inside_the_house_deadline_only_and_never_raises(rig):
    await _phone(rig.registry, 1)
    rig.google.fcm_default = lambda record: fcm_error(503, "UNAVAILABLE", error_code="UNAVAILABLE")
    rig.state["window"] = 120                                                    # the door window must not stretch it
    assert _ring(rig, ring_s=20) == {"ep-1"}
    await _settle(rig)
    assert sum(rig.clock.slept) <= 15 and rig.clock.slept[:3] == [0.5, 1, 2]     # 20 s - the 5 s margin
    assert rig.events.results[-1][2] is Outcome.TRANSIENT


async def test_google_down_never_breaks_the_house_ring(rig):
    await _phone(rig.registry, 1)
    await rig.google.close()
    assert _ring(rig) == {"ep-1"}
    rig.notifier.house_ended(CALL)
    await _settle(rig)
    assert rig.events.results and all(r[2] is Outcome.TRANSIENT for r in rig.events.results)


# ---- rate limits ------------------------------------------------------------------------------------------------

async def test_a_callee_is_rung_at_most_once_per_5_seconds_and_20_an_hour(rig):
    await _phone(rig.registry, 1)
    assert _ring(rig, call="hso-a") == {"ep-1"}
    rig.clock.advance(2)
    assert _ring(rig, call="hso-b") == set()                                     # 2 s later: capped, no push
    assert rig.events.skips == ["device_rate"]
    rig.clock.advance(4)
    assert _ring(rig, call="hso-c") == {"ep-1"}
    await _settle(rig)
    for n in range(30):
        rig.clock.advance(6)
        _ring(rig, call=f"hso-loop-{n}")
    await _settle(rig)
    assert [k for k, _ in _kinds(rig.google)].count("ring") == 20


async def test_the_cap_is_per_callee_endpoint_and_a_door_has_its_own(rig):
    await _phone(rig.registry, 1, user="user-a")
    await _phone(rig.registry, 2, user="user-b")
    assert _ring(rig, {"ep-a": "user-a"}, call="hso-a") == {"ep-a"}
    assert _ring(rig, {"ep-b": "user-b"}, call="hso-b") == {"ep-b"}              # another callee, same instant
    rig.tracker().apply_event({"call_id": "c1", "state": "ringing"})             # and the door, same instant
    await _settle(rig)
    assert sorted(k for k, _ in _kinds(rig.google)) == ["ring"] * 4


async def test_house_rings_share_the_global_circuit_with_door_rings(rig):
    for n in range(70):
        await _phone(rig.registry, n, user=f"user-{n}")
    callees = {f"ep-{n}": f"user-{n}" for n in range(70)}
    _ring(rig, callees)
    await _settle(rig)
    assert len(rig.google.fcm_requests) == 60 and rig.events.circuit == [True]
    assert rig.events.skips.count("rate_limited") == 10
    rig.tracker().apply_event({"call_id": "c1", "state": "ringing"})             # the circuit is open for the door too
    await _settle(rig)
    assert len(rig.google.fcm_requests) == 60


# ---- the door path is untouched; idempotence; bugs ------------------------------------------------------------------

async def test_a_house_call_never_enters_the_door_tables_and_a_door_ring_never_sends_ct_house(rig):
    await _phone(rig.registry, 1)
    _ring(rig)
    rig.tracker().apply_event({"call_id": "c1", "state": "ringing"})
    await _settle(rig)
    assert CALL not in rig.notifier._calls and CALL not in rig.notifier._finished
    messages = rig.google.sent_messages()
    door = [m["data"] for m in messages if m["data"].get("door_ref")]
    house = [m["data"] for m in messages if m["data"].get("ct") == "house"]
    assert len(door) == 1 and len(house) == 1 and door[0]["door_ref"] == ENTRY
    assert "ct" not in door[0] and "from_ref" not in door[0]
    assert "door_ref" not in house[0]
    rig.notifier.house_ended(CALL)
    rig.tracker().apply_event({"call_id": "c1", "state": "ended"})
    await _settle(rig)
    assert sorted((m["data"]["kind"], m["data"].get("ct") or "") for m in rig.google.sent_messages()) == [
        ("ended", ""), ("ended", "house"), ("ring", ""), ("ring", "house")]
    assert CALL not in rig.notifier._finished


async def test_a_repeat_ring_for_the_same_call_sends_one_and_an_ended_call_is_not_rung_again(rig):
    await _phone(rig.registry, 1)
    assert _ring(rig) == {"ep-1"}
    assert _ring(rig) == set()                                                   # the same call id: nothing
    rig.notifier.house_answered(CALL, "ep-zzz")
    rig.notifier.house_answered(CALL, "ep-zzz")
    await _settle(rig)
    rig.notifier.house_ended(CALL)
    await _settle(rig)
    rig.clock.advance(10)
    assert _ring(rig) == set()                                                   # the call is over
    await _settle(rig)
    assert [k for k, _ in _kinds(rig.google)] == ["ring", "answered"]


async def test_a_late_event_for_an_unknown_call_is_ignored(rig):
    await _phone(rig.registry, 1)
    rig.notifier.house_answered("hso-nope", "ep-1")
    rig.notifier.house_left("hso-nope", "ep-1", notify=True)
    rig.notifier.house_ended("hso-nope")
    await _settle(rig)
    assert rig.google.fcm_requests == []


async def test_a_call_that_outlives_the_memory_is_forgotten(rig):
    await _phone(rig.registry, 1)
    _ring(rig)
    await _settle(rig)
    rig.clock.advance(301)
    _ring(rig, call="hso-new")                                                   # any house ring expires old calls first
    rig.notifier.house_ended(CALL)
    await _settle(rig)
    assert CALL not in rig.notifier._house_calls and [k for k, _ in _kinds(rig.google)].count("ended") == 0


async def test_a_bug_in_the_sender_never_reaches_the_house_registry(rig, monkeypatch):
    await _phone(rig.registry, 1)

    def boom(*_a, **_k):
        raise RuntimeError("sender bug")

    original = rig.notifier._registry.targets
    monkeypatch.setattr(rig.notifier._registry, "targets", boom)
    assert _ring(rig) == set()                                                   # no raise
    monkeypatch.setattr(rig.notifier._registry, "targets", original)
    assert _ring(rig, call="hso-ok") == {"ep-1"}
    monkeypatch.setattr(rig.notifier, "_fan_out", boom)
    rig.notifier.house_answered("hso-ok", "ep-x")                                # none of these raise
    rig.notifier.house_left("hso-ok", "ep-1", notify=True)
    rig.notifier.house_ended("hso-ok")


async def test_stopping_the_notifier_forgets_house_calls_and_sends_nothing_more(rig):
    await _phone(rig.registry, 1)
    _ring(rig)
    await _settle(rig)
    rig.notifier.async_stop()
    rig.notifier.house_ended(CALL)
    assert _ring(rig, call="hso-2") == set()
    assert rig.notifier._house_calls == {}
    assert [k for k, _ in _kinds(rig.google)] == ["ring"]
