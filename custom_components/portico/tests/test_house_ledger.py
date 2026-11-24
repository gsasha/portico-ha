"""The ClaimLedger (M7 3a; house calls design 7, 15.3, 15.13): caps, outranking, reservations. Pure: no Home Assistant."""
from __future__ import annotations

import asyncio

import pytest

from ..house_ledger import (
    DOOR_ACTIVE,
    DOOR_CAP_PER_USER,
    DOOR_RINGING,
    HOUSE_ACTIVE,
    HOUSE_CAP,
    HOUSE_RINGING,
    IDLE,
    ClaimLedger,
    Reservation,
)


def _claim(ledger: ClaimLedger, user: str, call: str, door: str | None = "d1") -> Reservation:
    reservation = ledger.reserve_door(user, call)
    assert isinstance(reservation, Reservation), reservation
    ledger.confirm_door(reservation, door)
    return reservation


def test_the_constants_are_the_decided_caps():
    assert (DOOR_CAP_PER_USER, HOUSE_CAP) == (2, 4)     # 15.13: two doors a phone; HC-5: four house calls at once


def test_a_user_holds_two_door_calls_and_the_third_is_refused():
    ledger = ClaimLedger()
    _claim(ledger, "u1", "g1")
    _claim(ledger, "u1", "g2")
    assert ledger.door_sessions("u1") == 2
    assert ledger.reserve_door("u1", "g3") == "too_many_calls"
    assert ledger.door_sessions("u1") == 2          # a refusal records nothing


def test_the_cap_is_per_user_not_global():
    ledger = ClaimLedger()
    _claim(ledger, "u1", "g1")
    _claim(ledger, "u1", "g2")
    assert isinstance(ledger.reserve_door("u2", "g3"), Reservation)


def test_a_reservation_counts_before_the_provider_has_answered():
    """The race: two answers by one user, the provider still deciding on both. They are decided one after the other."""
    ledger = ClaimLedger()
    first = ledger.reserve_door("u1", "g1")
    second = ledger.reserve_door("u1", "g2")
    third = ledger.reserve_door("u1", "g3")
    assert isinstance(first, Reservation) and isinstance(second, Reservation) and third == "too_many_calls"
    ledger.release_door(second)                     # the provider refused the second
    assert isinstance(ledger.reserve_door("u1", "g3"), Reservation)


def test_the_same_call_asked_twice_is_not_counted_twice():
    ledger = ClaimLedger()
    _claim(ledger, "u1", "g1")
    again = ledger.reserve_door("u1", "g1")
    assert isinstance(again, Reservation) and ledger.door_sessions("u1") == 1
    _claim(ledger, "u1", "g2")
    assert isinstance(ledger.reserve_door("u1", "g2"), Reservation)      # still the same two calls
    assert ledger.reserve_door("u1", "g3") == "too_many_calls"


def test_releasing_one_reservation_keeps_the_other_askers_slot():
    ledger = ClaimLedger()
    a = ledger.reserve_door("u1", "g1")
    b = ledger.reserve_door("u1", "g1")
    ledger.release_door(a)
    assert ledger.door_sessions("u1") == 1          # b still asks for g1
    ledger.release_door(b)
    assert ledger.door_sessions("u1") == 0


def test_a_slot_frees_when_the_door_call_ends_and_the_claim_is_found_by_call():
    ledger = ClaimLedger()
    _claim(ledger, "u1", "g1")
    _claim(ledger, "u1", "g2")
    assert ledger.claimant("g1") == "u1"
    assert ledger.door_ended("g1") == ["u1"]
    assert ledger.door_sessions("u1") == 1 and ledger.claimant("g1") is None
    assert isinstance(ledger.reserve_door("u1", "g3"), Reservation)


def test_a_claim_confirmed_after_the_call_ended_is_not_kept():
    """The provider's answer arrives after the call's `ended`: the slot must not leak."""
    ledger = ClaimLedger()
    reservation = ledger.reserve_door("u1", "g1")
    ledger.door_ended("g1")
    ledger.confirm_door(reservation, "d1")
    assert ledger.door_sessions("u1") == 0 and ledger.claimant("g1") is None


def test_a_released_claim_frees_the_slot_but_the_call_goes_on_ringing():
    ledger = ClaimLedger()
    ledger.door_ringing("g1", "d1")
    _claim(ledger, "u1", "g1")
    ledger.release_claims("g1")
    assert ledger.door_sessions("u1") == 0 and ledger.door_rings() == ["g1"]


def test_a_door_that_leaves_takes_its_rings_and_claims():
    ledger = ClaimLedger()
    ledger.door_ringing("g1", "d1")
    ledger.door_ringing("g2", "d2")
    _claim(ledger, "u1", "g3", "d1")
    ledger.door_gone("d1")
    assert ledger.door_rings() == ["g2"] and ledger.door_sessions("u1") == 0


def test_a_door_outranks_a_house_call_on_every_endpoint():
    ledger = ClaimLedger()
    assert ledger.open_house("hso-1", {"a", "b"}) is None
    assert ledger.endpoint_state("a") == HOUSE_RINGING
    ledger.set_house_state("hso-1", "active")
    assert ledger.endpoint_state("a") == HOUSE_ACTIVE and ledger.endpoint_state("c") == IDLE
    ledger.door_ringing("g1", "d1")
    assert ledger.endpoint_state("a") == DOOR_RINGING and ledger.endpoint_state("c") == DOOR_RINGING
    _claim(ledger, "ua", "g1")
    assert ledger.endpoint_state("a", ("ua",)) == DOOR_ACTIVE      # the claimant's door call outranks its house call
    assert ledger.endpoint_state("b", ("ub",)) == DOOR_RINGING     # the other end of the house call: the ring over it


def test_a_door_ring_only_reaches_the_endpoints_it_is_rung_for():
    ledger = ClaimLedger()
    ledger.door_ringing("g1", "d1")
    reaches = {"a": lambda door: door == "d1", "b": lambda door: door == "other"}
    assert ledger.endpoint_state("a", rung_by=reaches["a"]) == DOOR_RINGING
    assert ledger.endpoint_state("b", rung_by=reaches["b"]) == IDLE


def test_resolving_the_door_call_frees_the_ring_but_not_the_claim():
    ledger = ClaimLedger()
    ledger.door_ringing("g1", "d1")
    _claim(ledger, "u1", "g1")
    ledger.door_resolved("g1")
    assert ledger.endpoint_state("e", ()) == IDLE
    assert ledger.endpoint_state("e", ("u1",)) == DOOR_ACTIVE


def test_one_house_call_per_endpoint():
    ledger = ClaimLedger()
    assert ledger.open_house("hso-1", {"a", "b"}) is None
    assert ledger.open_house("hso-2", {"b", "c"}) == "b"      # b is taken; nothing of hso-2 is registered
    assert ledger.house_call_of("c") is None and ledger.house_calls() == 1
    ledger.leave_house("hso-1", "b")
    assert ledger.open_house("hso-2", {"b", "c"}) is None


def test_the_cap_of_four_house_calls():
    ledger = ClaimLedger()
    for n in range(HOUSE_CAP):
        assert ledger.open_house(f"hso-{n}", {f"a{n}", f"b{n}"}) is None
    assert not ledger.house_slots_free()
    assert ledger.open_house("hso-x", {"p", "q"}) == "*"
    ledger.close_house("hso-0")
    assert ledger.house_slots_free() and ledger.open_house("hso-x", {"p", "q"}) is None
    assert ledger.house_call_of("a0") is None


def test_closing_a_house_call_frees_every_member():
    ledger = ClaimLedger()
    ledger.open_house("hso-1", {"a", "b", "c"})
    ledger.leave_house("hso-1", "c")
    ledger.close_house("hso-1")
    assert [ledger.endpoint_state(e) for e in "abc"] == [IDLE, IDLE, IDLE] and ledger.house_calls() == 0


@pytest.mark.asyncio
async def test_concurrent_reservations_by_one_user_never_exceed_the_cap():
    """Twenty answers racing through awaits: at most the cap are reserved, the rest are told `too_many_calls`."""
    ledger = ClaimLedger()
    results: list = []

    async def answer(n: int) -> None:
        results.append(ledger.reserve_door("u1", f"g{n}"))
        await asyncio.sleep(0)                      # the provider is asked here
        if isinstance(results[-1], Reservation):
            ledger.confirm_door(results[-1], "d1")

    await asyncio.gather(*(answer(n) for n in range(20)))
    granted = [r for r in results if isinstance(r, Reservation)]
    assert len(granted) == DOOR_CAP_PER_USER and results.count("too_many_calls") == 18
    assert ledger.door_sessions("u1") == DOOR_CAP_PER_USER
