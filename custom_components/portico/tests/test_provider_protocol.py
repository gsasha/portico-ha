"""PH-1: the provider protocol, the registry, the fake provider and the conformance suite.

HA-harness session. Nothing consumes the registry yet; these tests drive it directly after
`async_setup_component(hass, "portico", {})` with no config entry (async_setup creates it).
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from unittest.mock import patch

import pytest
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component

from custom_components.portico.provider import (
    API_VERSION,
    DATA_PROVIDERS,
    DOMAIN,
    AnswerResult,
    CallTransition,
    Door,
    ProviderError,
    ProviderRegistry,
)

from .fake_provider import FakeProvider
from .provider_conformance import CHECKS, ProviderHarness, check_provider

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "custom_components"


@pytest.fixture(autouse=True)
async def _portico(hass, enable_custom_integrations, tmp_path, monkeypatch):
    from homeassistant.components.frontend import DATA_EXTRA_MODULE_URL

    monkeypatch.setattr(hass.config, "path", lambda *parts: str(tmp_path.joinpath(*parts)))
    assert await async_setup_component(hass, "http", {})
    hass.data[DATA_EXTRA_MODULE_URL] = set()
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    yield


@pytest.fixture
def registry(hass) -> ProviderRegistry:
    return hass.data[DOMAIN][DATA_PROVIDERS]


def _harness(fake: FakeProvider | None = None) -> ProviderHarness:
    fake = fake or FakeProvider()
    return ProviderHarness(provider=fake, add_door=fake.add_door, remove_door=fake.remove_door,
                           unload=fake.unload, ring=fake.ring)


def _record(registry: ProviderRegistry) -> list[CallTransition]:
    seen: list[CallTransition] = []
    registry.subscribe(seen.append)
    return seen


# ---- the conformance suite ---------------------------------------------------------------------------------

async def test_fake_provider_passes_the_conformance_suite():
    await check_provider(_harness)


@pytest.mark.parametrize("check", CHECKS, ids=lambda c: c.__name__)
async def test_each_conformance_check_on_its_own(check):
    await check(_harness())


async def test_the_conformance_suite_catches_a_broken_provider():
    """A suite that cannot fail proves nothing: a provider that lets a doors listener see the door
    still attached after an unload breaks the ordering rule."""
    class AttachLast(FakeProvider):
        def remove_door(self, door_id):             # notifies BEFORE detaching, and only then
            self._notify_doors()
            self._doors.pop(door_id, None)
            self._states.pop(door_id, None)

    with pytest.raises(AssertionError, match="DETACHES first"):
        await check_provider(lambda: _harness(AttachLast()))

    class TwoAnswers(FakeProvider):
        async def answer(self, door_id, call_id, **kw):
            return {"verdict": "answered", "mode": "direct", "detail": {}}

    with pytest.raises(AssertionError, match="at most one"):
        await check_provider(lambda: _harness(TwoAnswers()))


# ---- the registry after async_setup, with no entry ---------------------------------------------------------

async def test_the_registry_exists_after_setup_with_no_entry(hass, registry):
    assert isinstance(registry, ProviderRegistry)
    assert registry.doors() == []
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_transition_reaches_the_listener(registry):
    """PROV-04's listener half; the bus-event half is the driver part's (PH-3b)."""
    fake = FakeProvider()
    fake.add_door("gate-1")
    registry.register(fake)
    seen = _record(registry)
    fake.ring("gate-1", "call-1")
    fake.state("call-1", "answered")
    assert [(t.door_id, t.call_id, t.prev_state, t.state) for t in seen] == [
        ("gate-1", "call-1", "idle", "ringing"),
        ("gate-1", "call-1", "ringing", "answered"),
    ]
    assert all(isinstance(t, CallTransition) and t.at_wall > 0 for t in seen)
    assert seen[1].prev_call_id == "call-1"


async def test_a_bad_transition_is_dropped_and_a_raising_listener_skipped(registry, caplog):
    fake = FakeProvider()
    fake.add_door("gate-1")
    registry.register(fake)
    good: list[CallTransition] = []

    def boom(_t):
        raise RuntimeError("listener bug")

    registry.subscribe(boom)
    registry.subscribe(good.append)
    fake._emit({"door_id": "gate-1", "state": "ringing"})      # keys missing: dropped
    assert good == []
    fake.ring("gate-1", "c1")
    assert len(good) == 1
    assert "listener bug" in caplog.text or "listener raised" in caplog.text


# ---- door ids (PROV-03, PROV-06, R2-09) --------------------------------------------------------------------

async def test_bare_id_shared_by_two_providers_is_ambiguous(registry):
    a, b = FakeProvider("fake_a"), FakeProvider("fake_b")
    a.add_door("gate")
    b.add_door("gate")
    b.add_door("only-b")
    registry.register(a)
    registry.register(b)
    with pytest.raises(ProviderError) as err:
        registry.door("gate")
    assert err.value.code == "ambiguous_door"
    assert registry.door("fake_a:gate")[0] is a
    assert registry.door("fake_b:gate")[0] is b
    assert registry.door("only-b")[0] is b
    assert registry.door("nobody") is None


async def test_a_bare_url_id_is_not_mistaken_for_a_namespaced_one(registry):
    """R2-09: `http://10.6.0.7:8091` has a colon, but `http` is no registered provider domain."""
    fake = FakeProvider("fake")
    fake.add_door("http://10.6.0.7:8091", name="Gate")
    registry.register(fake)
    provider, door = registry.door("http://10.6.0.7:8091")
    assert provider is fake and door.id == "http://10.6.0.7:8091"
    # namespaced with the URL id: the split is on the FIRST colon only
    provider, door = registry.door("fake:http://10.6.0.7:8091")
    assert provider is fake and door.id == "http://10.6.0.7:8091"
    assert door.endpoint_id == "fake:http://10.6.0.7:8091"


async def test_door_ref_is_a_valid_push_id_for_a_url_door():
    """WS-08, R1-01: the push id grammar refuses a URL; the derived ref satisfies it and is stable."""
    door = Door.from_dict("fake", FakeProvider._door_dict("http://10.6.0.7:8091"))
    assert re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", door.door_ref)
    assert door.door_ref.startswith("d-") and len(door.door_ref) == 34
    again = Door.from_dict("fake", FakeProvider._door_dict("http://10.6.0.7:8091"))
    assert again.door_ref == door.door_ref
    other = Door.from_dict("fake_b", FakeProvider._door_dict("http://10.6.0.7:8091"))
    assert other.door_ref != door.door_ref


async def test_from_dict_rejects_a_missing_or_mistyped_key():
    good = FakeProvider._door_dict("g")
    Door.from_dict("fake", good)
    for key in good:
        if key in ("provider",):
            continue
        broken = {k: v for k, v in good.items() if k != key}
        with pytest.raises(ProviderError) as err:
            Door.from_dict("fake", broken)
        assert err.value.code == "bad_provider_data"
    with pytest.raises(ProviderError):
        Door.from_dict("fake", {**good, "available": "yes"})
    with pytest.raises(ProviderError):
        Door.from_dict("fake", {**good, "id": 7})
    with pytest.raises(ProviderError):
        Door.from_dict("fake", "not a dict")                   # type: ignore[arg-type]
    with pytest.raises(ProviderError):
        CallTransition.from_dict({"door_id": "g", "call_id": None, "prev_state": "idle", "state": "ringing",
                                  "prev_call_id": None, "at_wall": True})
    assert AnswerResult.from_dict({"verdict": "answered", "mode": "relay"}).mode == "relay"
    with pytest.raises(ProviderError):
        AnswerResult.from_dict({"verdict": "maybe"})


# ---- registering ---------------------------------------------------------------------------------------------

async def test_a_second_register_of_the_same_domain_replaces_the_first(registry):
    first, second = FakeProvider("fake"), FakeProvider("fake")
    first.add_door("old")
    second.add_door("new")
    stale = registry.register(first)
    seen = _record(registry)
    registry.register(second)
    assert [d.id for d in registry.doors()] == ["new"]
    assert registry.provider("fake") is second
    assert first._listeners == [] and first._door_listeners == [], "the replaced provider is unsubscribed"
    assert [(t.door_id, t.state) for t in seen] == [("old", "unavailable")]
    first.ring("old", "c1")                                  # late traffic from the replaced one
    assert len(seen) == 1
    stale()                                                  # the old handle must not remove the replacement
    assert registry.provider("fake") is second


async def test_a_provider_whose_doors_raise_counts_as_no_doors(registry, caplog):
    bad, good = FakeProvider("fake_bad"), FakeProvider("fake_good")
    good.add_door("g")
    bad.doors_raise = RuntimeError("driver bug")
    registry.register(bad)
    registry.register(good)
    assert [d.id for d in registry.doors()] == ["g"]
    assert registry.door("fake_bad:anything") is None
    assert "doors() raised" in caplog.text
    # a bad dict among good ones is only that door
    good.add_door("h")
    good._doors["broken"] = {"id": "broken"}
    assert sorted(d.id for d in registry.doors()) == ["g", "h"]


# ---- calls, unregistering, door changes (registry behaviour PH-3a and PH-5 build on) ----------------------------

async def test_for_call_learns_from_a_ring_and_from_a_dial_result(registry):
    a, b = FakeProvider("fake_a"), FakeProvider("fake_b")
    a.add_door("ga")
    b.add_door("gb")
    registry.register(a)
    registry.register(b)
    assert registry.for_call("c1") is None
    b.ring("gb", "c1")
    assert registry.for_call("c1") is b
    result = await a.dial("ga", "10.0.0.1", 5060)                # a dialled call has no ring (R2-08)
    assert registry.for_call(result["call_id"]) is None
    registry.note_call("fake_a", result)
    assert registry.for_call(result["call_id"]) is a
    registry.note_call("fake_a", {"ok": True})                   # no call_id: ignored
    registry.note_call("fake_a", None)


async def test_unregister_sends_one_unavailable_snapshot_per_door(registry):
    fake = FakeProvider("fake")
    fake.add_door("d1")
    fake.add_door("d2")
    unregister = registry.register(fake)
    seen = _record(registry)
    unregister()
    assert sorted((t.door_id, t.state, t.call_id) for t in seen) == [("d1", "unavailable", None),
                                                                     ("d2", "unavailable", None)]
    assert registry.doors() == []
    unregister()
    assert len(seen) == 2, "a second unregister is a no-op"
    fake.ring("d1", "c1")
    fake.add_door("d3")
    assert len(seen) == 2, "a late notification after unregister is ignored"


async def test_a_door_leaving_a_registered_provider_sends_unavailable_for_it_only(registry):
    fake = FakeProvider("fake")
    fake.add_door("d1")
    fake.add_door("d2")
    registry.register(fake)
    seen = _record(registry)
    fake.remove_door("d1")
    assert [(t.door_id, t.state) for t in seen] == [("d1", "unavailable")]
    assert [d.id for d in registry.doors()] == ["d2"]
    fake.add_door("d3")
    assert [t.door_id for t in seen] == ["d1"], "an arrival is not an unavailable"
    assert sorted(d.id for d in registry.doors()) == ["d2", "d3"]


# ---- PH-5: doors leaving and arriving, provider unload, two providers (on the v1 subscribe wire) ----------------

async def _recv(client):
    import asyncio

    return await asyncio.wait_for(client.receive_json(), timeout=5)


async def _subscribe(client, msg_id=1):
    await client.send_json({"id": msg_id, "type": "portico/calls/subscribe"})
    ack = await _recv(client)
    assert ack["type"] == "result" and ack["success"], ack


async def _events(client, n):
    out = []
    for _ in range(n):
        msg = await _recv(client)
        assert msg["type"] == "event", msg
        out.append(msg["event"])
    return out


def _shape(events):
    return [(e["door_id"], e["state"], e["call_id"], e.get("snapshot", False)) for e in events]


async def test_one_door_unloading_mid_subscription_sends_unavailable_and_keeps_the_others(
        hass, hass_ws_client, registry):
    fake = FakeProvider("fake")
    fake.add_door("d1")
    fake.add_door("d2")
    registry.register(fake)
    client = await hass_ws_client(hass)
    await _subscribe(client)
    assert sorted(_shape(await _events(client, 2))) == [("d1", "idle", None, True), ("d2", "idle", None, True)]
    fake.ring("d1", "c1")
    assert registry.for_call("c1") is fake

    fake.remove_door("d1")                                    # an entry unload: detach first, then notify
    fake.ring("d2", "c2")                                     # the other door carries on
    assert _shape(await _events(client, 3)) == [
        ("d1", "ringing", "c1", False), ("d1", "unavailable", None, True), ("d2", "ringing", "c2", False)]
    assert [d.id for d in registry.doors()] == ["d2"]
    assert registry.for_call("c1") is None, "a call of the door that left is no longer routable"
    assert registry.for_call("c2") is fake

    await client.send_json({"id": 2, "type": "portico/hangup", "call_id": "c1", "reason": "x"})
    assert (await _recv(client))["error"]["code"] == "unknown_call"
    fake.remove_door("d1")                                    # removing again says nothing more
    fake.set_available("d2", False)                           # an availability change is not a departure
    fake.ring("d2", "c3")
    assert _shape(await _events(client, 1)) == [("d2", "ringing", "c3", False)]


async def test_a_dialled_call_of_a_door_that_left_is_no_longer_routable(hass, hass_ws_client, registry):
    fake = FakeProvider("fake")
    fake.add_door("d1")
    registry.register(fake)
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "portico/dial", "door_id": "d1"})
    call_id = (await _recv(client))["result"]["call_id"]
    assert registry.for_call(call_id) is fake
    fake.remove_door("d1")
    assert registry.for_call(call_id) is None


async def test_provider_unload_mid_subscription(hass, hass_ws_client, registry):
    gone, stays = FakeProvider("fake_a"), FakeProvider("fake_b")
    gone.add_door("a1")
    gone.add_door("a2")
    stays.add_door("b1")
    unregister = registry.register(gone)
    registry.register(stays)
    client = await hass_ws_client(hass)
    await _subscribe(client)
    assert len(await _events(client, 3)) == 3                  # the snapshots
    gone.ring("a1", "ca")
    stays.ring("b1", "cb")
    await _events(client, 2)

    unregister()                                               # the provider's integration unloads (PROV-05)
    assert sorted(_shape(await _events(client, 2))) == [
        ("a1", "unavailable", None, True), ("a2", "unavailable", None, True)]
    assert [d.id for d in registry.doors()] == ["b1"], "the UI part keeps running with the other provider"
    assert registry.for_call("ca") is None and registry.for_call("cb") is stays
    assert registry.provider("fake_a") is None

    await client.send_json({"id": 2, "type": "portico/open_relay", "door_id": "a1"})
    assert (await _recv(client))["error"]["code"] == "unknown_door"
    gone.ring("a2", "late")                                    # a late event from the unloaded provider: ignored
    stays.state("cb", "answered")
    assert _shape(await _events(client, 1)) == [("b1", "answered", "cb", False)]


async def test_a_provider_unloading_leaves_the_ui_part_running(hass, hass_ws_client, registry):
    fake = FakeProvider("fake")
    fake.add_door("d1")
    unregister = registry.register(fake)
    client = await hass_ws_client(hass)
    await _subscribe(client)
    await _events(client, 1)
    unregister()
    assert _shape(await _events(client, 1)) == [("d1", "unavailable", None, True)]
    await client.send_json({"id": 2, "type": "portico/doors"})
    assert (await _recv(client))["result"] == {"doors": []}
    await client.send_json({"id": 3, "type": "portico/open_relay", "door_id": "d1"})
    # (the built-in door provider stays registered since the fold, so the answer is `unknown_door`, not the
    # `not_available` of "no provider at all", STRUCT-07, which only a registry with none can give)
    assert (await _recv(client))["error"]["code"] == "unknown_door"
    fake2 = FakeProvider("fake")                                         # and it can come back, doors and all
    fake2.add_door("d1")
    registry.register(fake2)
    assert _shape(await _events(client, 1)) == [("d1", "idle", None, True)]


async def test_two_providers_do_not_collide(hass, hass_ws_client, registry):
    a, b = FakeProvider("fake_a"), FakeProvider("fake_b")
    a.add_door("door-a")
    b.add_door("door-b")
    registry.register(a)
    registry.register(b)
    a.ring("door-a", "ca")
    b.ring("door-b", "cb")
    client = await hass_ws_client(hass)
    for i, (door, call) in enumerate([("door-b", "cb"), ("door-a", "ca")], start=1):
        await client.send_json({"id": i, "type": "portico/answer", "door_id": door, "call_id": call,
                                "client_kind": "app"})
        assert (await _recv(client))["success"]
    assert [c[0] for c in b.calls_to("answer")] == [("door-b", "cb")]
    assert [c[0] for c in a.calls_to("answer")] == [("door-a", "ca")]

    # the same bare id in both: refused bare, reaches the named one namespaced, and calls follow their owner
    a.add_door("same")
    b.add_door("same")
    await client.send_json({"id": 5, "type": "portico/open_relay", "door_id": "same"})
    assert (await _recv(client))["error"]["code"] == "ambiguous_door"
    await client.send_json({"id": 6, "type": "portico/open_relay", "door_id": "fake_a:same"})
    assert (await _recv(client))["success"]
    assert a.calls_to("open_relay") == [(("same",), {})] and b.calls_to("open_relay") == []
    await client.send_json({"id": 7, "type": "portico/hangup", "call_id": "cb", "reason": "x"})
    assert (await _recv(client))["success"]
    assert b.calls_to("hangup") and not a.calls_to("hangup")
    assert sorted(d.endpoint_id for d in registry.doors()) == [
        "fake_a:door-a", "fake_a:same", "fake_b:door-b", "fake_b:same"]


async def test_a_door_added_after_a_subscription_appears(hass, hass_ws_client, registry):
    fake = FakeProvider("fake")
    fake.add_door("d1")
    registry.register(fake)
    client = await hass_ws_client(hass)
    await _subscribe(client)
    assert _shape(await _events(client, 1)) == [("d1", "idle", None, True)]

    fake.add_door("d2")                                        # an entry set up after the subscription
    assert _shape(await _events(client, 1)) == [("d2", "idle", None, True)]
    fake.ring("d2", "c2")
    assert _shape(await _events(client, 1)) == [("d2", "ringing", "c2", False)]
    fake.set_available("d1", False)                            # not an arrival
    other = FakeProvider("other")                              # a provider that registers later brings its doors
    other.add_door("o1")
    registry.register(other)
    assert _shape(await _events(client, 1)) == [("o1", "idle", None, True)]


async def test_a_departed_door_that_comes_back_gets_a_fresh_snapshot(hass, hass_ws_client, registry):
    """The runbook's T8: an entry reload is an unload then a setup of one door while the provider stays."""
    fake = FakeProvider("fake")
    fake.add_door("d1")
    registry.register(fake)
    client = await hass_ws_client(hass)
    await _subscribe(client)
    await _events(client, 1)
    fake.remove_door("d1")
    fake.add_door("d1")
    assert _shape(await _events(client, 2)) == [("d1", "unavailable", None, True), ("d1", "idle", None, True)]


async def test_an_unsubscribed_connection_leaves_no_listener_behind(hass, hass_ws_client, registry):
    fake = FakeProvider("fake")
    fake.add_door("d1")
    registry.register(fake)
    client = await hass_ws_client(hass)
    # the endpoint registry's always-on subscription (managed Talk, DEVICES-10) is already there: compare against it
    arrivals_before, listeners_before = list(registry._arrival_listeners), list(registry._listeners)
    await _subscribe(client, 1)
    await _events(client, 1)
    assert len(registry._arrival_listeners) > len(arrivals_before) and len(registry._listeners) > len(listeners_before)
    await client.send_json({"id": 2, "type": "unsubscribe_events", "subscription": 1})
    assert (await _recv(client))["success"]
    assert registry._arrival_listeners == arrivals_before and registry._listeners == listeners_before
    fake.add_door("d2")
    fake.remove_door("d1")


# ---- CanDial (P2): the Python-side concept of the dial capability; absent everywhere today --------------------

def _door_dict(capabilities):
    return {"id": "g", "name": "G", "driver": "x", "capabilities": capabilities, "camera_entity_id": None,
            "available": True, "away_media": False}


@pytest.mark.parametrize("capabilities,expected", [
    ({}, False), ({"HasRelay": True}, False), ({"CanDial": False}, False), ({"CanDial": "true"}, False),
    ({"CanDial": 1}, False), ({"can_dial": True}, False), ({"CanDial": True}, True),
])
def test_a_door_can_dial_only_when_the_driver_says_exactly_true(capabilities, expected):
    from custom_components.portico.provider import Door
    assert Door.from_dict("fake", _door_dict(capabilities)).can_dial is expected
