# ruff: noqa: F811 - test parameters named after the imported fixtures
"""The driver part's doors as the UI part sees them (S-2): the provider registry, the websocket, the push sender and
content, over a REAL door entry (`entry_type: "door"`; the transport faked, driver_part.py).

These are the tests that used to live in the driver tests' `test_transport_provider.py` and `test_observer.py`
while there was one integration: each reads the door THROUGH this part. The provider's own behaviour (dial,
answer, media, the bus event) is the driver part's `test_transport_provider.py`.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from homeassistant.setup import async_setup_component

from custom_components.portico.config_flow import DOMAIN
from custom_components.portico.provider import DATA_PROVIDERS, CallTransition
from custom_components.portico.door.transport import TransportError

from .driver_part import DRIVER_DOMAIN, GATE_SIP_URL, _observer_entry, fake_transport, get_provider  # noqa: F401
from .fake_provider import FakeProvider
from .test_push_registry import (  # noqa: F401 - fixtures by name
    _custom_integrations,
    _manager,
    _phone_user,
    _register_msg,
    _setup_all,
    google,
)


async def _setup_door(hass):
    """The door (the driver part's entry) and then the UI part, the way a restart orders them (the UI part lists the
    driver part in `after_dependencies`): the registry finds the provider through the `portico.py` platform."""
    entry = _observer_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    return entry


def _registry(hass):
    return hass.data[DOMAIN][DATA_PROVIDERS]


def _provider(hass):
    return get_provider(hass)


async def test_the_provider_is_discovered_and_lists_the_door_with_its_derived_ref(hass, fake_transport):
    entry = await _setup_door(hass)
    assert _registry(hass).provider(DRIVER_DOMAIN) is _provider(hass)
    (door,) = _registry(hass).doors()
    assert (door.id, door.provider, door.name, door.driver) == (
        GATE_SIP_URL, DRIVER_DOMAIN, entry.title, "gate_sip_observer")
    assert door.camera_entity_id == "camera.gate" and door.available is True and door.away_media is True
    assert door.capabilities == entry.runtime_data.capabilities
    # the push `door_ref` is derived from the endpoint id, not the driver entry's id (push/v2); the dict carries no `ref`
    assert door.door_ref == _registry(hass).door(GATE_SIP_URL)[1].door_ref and door.door_ref != entry.entry_id
    assert door.door_ref.startswith("d-") and "ref" not in _provider(hass).doors()[0]
    # a bare URL id is not mistaken for a namespaced one, and the namespaced form finds the same door
    assert _registry(hass).door(GATE_SIP_URL)[1].id == GATE_SIP_URL
    assert _registry(hass).door(f"{DRIVER_DOMAIN}:{GATE_SIP_URL}")[1].id == GATE_SIP_URL


async def test_a_legacy_entry_that_stored_away_media_false_still_carries_call_audio(hass, hass_ws_client, fake_transport):
    """Away call audio is always on (2026-11-12): the old `away_media` option is neither read nor shown. A door entry
    that still stores False publishes the door's `away_media` flag as true, in the provider and on `portico/doors`
    (old apps read the flag), so `portico/media_open` is never answered `away_media_disabled` for it."""
    entry = _observer_entry(hass)
    hass.config_entries.async_update_entry(entry, options={"away_media": False})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    (door,) = _registry(hass).doors()
    assert door.away_media is True
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "portico/doors"})
    (row,) = (await client.receive_json())["result"]["doors"]
    assert row["away_media"] is True
    await client.send_json({"id": 2, "type": "portico/media_open", "door_id": GATE_SIP_URL, "call_id": "c"})
    reply = await client.receive_json()
    assert reply["error"]["code"] != "away_media_disabled"


async def test_the_unavailable_snapshot_of_a_door_that_left_names_the_door(hass, hass_ws_client, fake_transport):
    """`portico/doors` carries the derived `door_ref`; the `unavailable` snapshot of a door that has just left names
    it by `door_id` (contract 3 has no `entry_id` on the wire)."""
    entry = await _setup_door(hass)
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "portico/doors"})
    (door,) = (await client.receive_json())["result"]["doors"]
    assert door["door_id"] == GATE_SIP_URL and door["door_ref"] == _registry(hass).doors()[0].door_ref
    assert entry.entry_id not in str(door)

    await client.send_json({"id": 2, "type": "portico/calls/subscribe"})
    assert (await client.receive_json())["success"]
    snapshot = (await client.receive_json())["event"]
    assert snapshot["door_id"] == GATE_SIP_URL and "entry_id" not in snapshot
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    left = [(await asyncio.wait_for(client.receive_json(), timeout=5))["event"] for _ in range(1)]
    assert left[0]["state"] == "unavailable" and left[0]["door_id"] == GATE_SIP_URL


async def test_a_transition_reaches_a_registry_subscriber_with_the_door_id(hass, fake_transport):
    await _setup_door(hass)
    seen: list[CallTransition] = []
    _registry(hass).subscribe(seen.append)
    go = fake_transport.instances[-1]
    go.ring("gso-1")
    go.state("gso-1", "ended")
    assert [(t.door_id, t.call_id, t.prev_state, t.state) for t in seen] == [
        (GATE_SIP_URL, "gso-1", "idle", "ringing"), (GATE_SIP_URL, "gso-1", "ringing", "ended")]
    assert _registry(hass).for_call("gso-1") is _provider(hass)


async def test_unloading_a_door_detaches_first_then_sends_unavailable(hass, fake_transport):
    entry = await _setup_door(hass)
    seen: list[CallTransition] = []
    _registry(hass).subscribe(seen.append)
    tracker = entry.runtime_data.call_state
    assert len(tracker._transition_listeners) == 1
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert _registry(hass).doors() == []
    assert [(t.door_id, t.state) for t in seen] == [(GATE_SIP_URL, "unavailable")]
    assert tracker._transition_listeners == []
    # setting it up again brings the door back
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert [d.id for d in _registry(hass).doors()] == [GATE_SIP_URL]


async def test_a_door_leaving_mid_ring_ends_the_call_for_subscribers(hass, hass_ws_client, fake_transport):
    entry = await _setup_door(hass)
    client = await hass_ws_client(hass)

    async def recv():
        return await asyncio.wait_for(client.receive_json(), timeout=5)

    async def events(n):
        out = []
        for _ in range(n):
            msg = await recv()
            assert msg["type"] == "event", msg
            out.append(msg["event"])
        return [(e["door_id"], e["state"], e["call_id"], e.get("snapshot", False)) for e in out]

    await client.send_json({"id": 1, "type": "portico/calls/subscribe"})
    assert (await recv())["success"]
    assert await events(1) == [(GATE_SIP_URL, "idle", None, True)]
    fake_transport.instances[-1].ring("gso-1")
    assert await events(1) == [(GATE_SIP_URL, "ringing", "gso-1", False)]

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    # an ended call (no `idle` echo in contract 3), then the snapshot of the door leaving
    assert await events(2) == [
        (GATE_SIP_URL, "ended", "gso-1", False),
        (GATE_SIP_URL, "unavailable", None, True),
    ]
    assert _registry(hass).for_call("gso-1") is None


async def test_the_event_fires_beside_the_registry_fan_out_not_instead_of_it(hass, fake_transport):
    from pytest_homeassistant_custom_component.common import async_capture_events

    await _setup_door(hass)
    events = async_capture_events(hass, "portico_intercom_call")
    seen: list[CallTransition] = []
    _registry(hass).subscribe(seen.append)
    fake_transport.instances[-1].ring("gso-1")
    await hass.async_block_till_done()
    assert [(e.data["device_id"], e.data["state"]) for e in events] == [(GATE_SIP_URL, "ringing")]
    assert [(t.door_id, t.state) for t in seen] == [(GATE_SIP_URL, "ringing")]


def _not_the_endpoint_registry(listeners) -> list:
    """The provider registry's subscribers other than the endpoint registry's own (managed Talk keeps that one for the
    whole HA run, managed_talk.async_setup_managed_talk; it is not a per-entry subscription and never leaves)."""
    from custom_components.portico.endpoints import EndpointRegistry
    from custom_components.portico.call_events import CallEvents
    from custom_components.portico.house import HouseCallRegistry

    # the house registry is always on now (D-20261122-aafb) and keeps its own door listener for the whole run
    return [cb for cb in listeners if not isinstance(getattr(cb, "__self__", None), (EndpointRegistry, HouseCallRegistry, CallEvents))]


async def test_push_follows_one_registry_subscription_not_each_tracker(hass, hass_storage, google, fake_transport):
    device, push = await _setup_all(hass, hass_storage, google)
    registry = _registry(hass)
    # PUSH-02: one subscription for the push sender. The endpoint registry's own, always-on one (managed Talk,
    # DEVICES-10, hears doors leave for the whole HA run) is not the sender's: count the others.
    assert len(_not_the_endpoint_registry(registry._listeners)) == 1
    assert len(device.runtime_data.call_state._transition_listeners) == 1  # the provider's, not the sender's
    assert await hass.config_entries.async_unload(push.entry_id)
    await hass.async_block_till_done()
    assert _not_the_endpoint_registry(registry._listeners) == []


async def _ring_ready(hass, hass_storage, google, hass_ws_client):
    user, token = await _phone_user(hass)
    await _setup_all(hass, hass_storage, google, enrolled=[(user.id, "p")])
    client = await hass_ws_client(hass, access_token=token)
    await client.send_json(_register_msg())
    reg_id = (await client.receive_json())["result"]["reg_id"]
    await _manager(hass).registry.async_set_enabled(reg_id, True)


async def test_the_ring_limiter_is_keyed_by_door_id(hass, hass_storage, google, fake_transport, hass_ws_client):
    await _ring_ready(hass, hass_storage, google, hass_ws_client)
    fake_transport.instances[-1].ring("gso-1")
    await hass.async_block_till_done()
    assert list(_manager(hass).notifier._device_rings) == [GATE_SIP_URL]


async def test_a_second_providers_door_rings_a_phone_by_its_derived_ref(hass, hass_storage, google, fake_transport,
                                                                      hass_ws_client):
    """Push v2: every door has a `door_ref` derived from its endpoint id, so a door of another provider rings too,
    under the ref `portico/doors` gives for it; the real door rings under its own."""
    await _ring_ready(hass, hass_storage, google, hass_ws_client)
    fake = FakeProvider("fake_other")
    fake.add_door("http://10.6.0.9:8091")                  # a URL id: the wire never carries it, only the derived ref
    _registry(hass).register(fake)
    fake.ring("http://10.6.0.9:8091", "fx-1")
    await hass.async_block_till_done()
    await _manager(hass).notifier.async_drain()
    (ring,) = [r["json"]["message"]["data"] for r in google.fcm_requests]
    other = _registry(hass).door("fake_other:http://10.6.0.9:8091")[1]
    assert ring["call_id"] == "fx-1" and ring["door_ref"] == other.door_ref and ring["pv"] == "2"
    assert "10.6.0.9" not in str(ring) and "ct" not in ring and "from_ref" not in ring
    fake_transport.instances[-1].ring("gso-9")
    await hass.async_block_till_done()
    await _manager(hass).notifier.async_drain()
    real = _registry(hass).door(GATE_SIP_URL)[1]
    assert [(r["json"]["message"]["data"]["call_id"], r["json"]["message"]["data"]["door_ref"])
            for r in google.fcm_requests] == [("fx-1", other.door_ref), ("gso-9", real.door_ref)]


async def test_a_door_that_is_not_in_the_registry_never_rings_a_phone(hass, hass_storage, google, fake_transport,
                                                                     hass_ws_client):
    """A transition for a door the registry does not know has no ref to put on the wire: ignored, no crash."""
    await _ring_ready(hass, hass_storage, google, hass_ws_client)
    notifier = _manager(hass).notifier
    assert notifier._wire_id("ghost-door") is None
    notifier.handle_transition(CallTransition(door_id="ghost-door", call_id="g1", prev_state="idle",
                                              state="ringing", prev_call_id=None, at_wall=1.0))
    await notifier.async_drain()
    assert google.fcm_requests == []


async def test_secrets_for_stays_in_the_provider_and_no_command_serves_it(hass, hass_ws_client, hass_admin_user):
    """PROV-07 kept, WS-03 done: the provider still answers `secrets_for` for an enrolled phone's enroll_id (the day a
    client needs it), and `portico/doors` hands out none of it to anyone: no `secrets` key, no credential in the reply."""
    from .test_signaling import _add_device, _setup_signaling

    _add_device(hass, device_id="10.3.0.20",
                enrolled_phones=[{"ha_user_id": hass_admin_user.id, "name": "p", "enroll_id": "enroll-A"}])
    await _setup_signaling(hass)
    expected = {"host": "10.3.0.20", "sip_user": "9999#0", "sip_password": "x", "rtsp_username": "u",
                "rtsp_password": "p"}
    assert _provider(hass).secrets_for("10.3.0.20", "enroll-A") == expected
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "portico/doors"})
    reply = await client.receive_json()
    doors = {d["door_id"]: d for d in reply["result"]["doors"]}
    assert "secrets" not in doors["10.3.0.20"]
    for secret in ("9999#0", "sip_password", "rtsp_password"):
        assert secret not in str(reply)


def test_content_and_push_no_longer_reach_into_device_entries():
    """WS-06 / PUSH-02: no lazy signaling import in content, no tracker or runtime_data in content or push."""
    base = Path(__file__).resolve().parent.parent
    content = (base / "content.py").read_text()
    assert "_find_entry" not in content and "signaling" not in content and "runtime_data" not in content
    for path in (base / "push").glob("*.py"):
        text = path.read_text()
        assert "runtime_data.call_state" not in text and "add_transition_listener" not in text, path.name


# --------------------------------------------------------------------------
# 5. not_supported reaches the websocket caller as a typed error
# --------------------------------------------------------------------------

def _not_supported(method: str):
    async def _raise(*args, **kwargs):
        raise TransportError("not_supported", f"gate_sip_observer: {method}: not_supported (observer mode)")
    return _raise


@pytest.mark.parametrize(
    ("ws_type", "method", "extra"),
    [
        ("portico/dial", "dial", {}),
        ("portico/open_relay", "open_relay", {}),
    ],
)
async def test_not_supported_is_a_typed_websocket_error(hass, hass_ws_client, fake_transport, ws_type, method, extra):
    entry = await _setup_door(hass)
    ws = await hass_ws_client(hass)
    setattr(entry.runtime_data.client, method, _not_supported(method))
    await ws.send_json({"id": 1, "type": ws_type, "door_id": GATE_SIP_URL, **extra})
    resp = await asyncio.wait_for(ws.receive_json(), timeout=5)
    assert resp["success"] is False
    assert resp["error"]["code"] == "not_supported"
    assert "observer" in resp["error"]["message"]


async def test_answer_not_supported_is_typed_and_leaves_the_call_ringing(hass, hass_ws_client, fake_transport):
    entry = await _setup_door(hass)
    ws = await hass_ws_client(hass)
    entry.runtime_data.client.answer = _not_supported("Answer")
    fake_transport.instances[-1].ring("gso-1")

    await ws.send_json({"id": 1, "type": "portico/answer", "door_id": GATE_SIP_URL,
                        "call_id": "gso-1", "client_kind": "browser"})
    resp = await asyncio.wait_for(ws.receive_json(), timeout=5)
    assert resp["success"] is False
    assert resp["error"]["code"] == "not_supported"
    # The existing app still owns answering; Portico's mirror is untouched.
    assert entry.runtime_data.call_state.state == "ringing"


# ---- the outbound call in the provider's snapshot never reaches the phones (DEV-658) ---------------------------------------

async def _calls_client(hass, hass_ws_client):
    client = await hass_ws_client(hass)

    async def recv():
        return await asyncio.wait_for(client.receive_json(), timeout=5)

    await client.send_json({"id": 1, "type": "portico/calls/subscribe"})
    assert (await recv())["success"]
    return client, recv


async def test_the_phones_snapshot_is_unchanged_with_no_outbound_call_and_with_one(hass, hass_ws_client, fake_transport):
    entry = await _setup_door(hass)
    tracker = entry.runtime_data.call_state
    provider = _provider(hass)
    assert set(provider.call_state(GATE_SIP_URL)) == {"door_id", "call_id", "state"}      # no outbound call: no new keys
    _, recv = await _calls_client(hass, hass_ws_client)
    first = (await recv())["event"]
    assert first == {"type": "call_state_changed", "door_id": GATE_SIP_URL, "call_id": None, "state": "idle", "snapshot": True}

    entry.runtime_data.capabilities = {**entry.runtime_data.capabilities, "CanDial": True}   # the provider now publishes
    tracker.apply_event({"call_id": "out-9", "state": "ringing", "outbound": True})
    assert provider.call_state(GATE_SIP_URL)["outbound_state"] == "dialing"
    _, recv2 = await _calls_client(hass, hass_ws_client)                                     # a phone subscribing mid-dial
    assert (await recv2())["event"] == first                                                 # sees the same visitor snapshot


async def test_an_outbound_call_never_makes_a_visitor_ring_appear(hass, hass_ws_client, fake_transport):
    entry = await _setup_door(hass)
    tracker = entry.runtime_data.call_state
    entry.runtime_data.capabilities = {**entry.runtime_data.capabilities, "CanDial": True}   # the keys are published
    assert _provider(hass).call_state(GATE_SIP_URL)["outbound_state"] == "idle"
    _, recv = await _calls_client(hass, hass_ws_client)
    assert (await recv())["event"]["state"] == "idle"
    for state in ("ringing", "answered", "ended"):
        tracker.apply_event({"call_id": "out-9", "state": state, "outbound": True})
    await hass.async_block_till_done()
    # nothing was sent to the phone for the outbound call: the next thing it hears is a real visitor ring
    fake_transport.instances[-1].ring("gso-1")
    event = (await recv())["event"]
    assert (event["call_id"], event["state"]) == ("gso-1", "ringing")


async def test_a_door_set_up_to_dial_publishes_the_outbound_state_even_while_candial_is_off(hass, fake_transport):
    """Talk believes a provider that publishes the outbound keys. A gate observer set up to dial publishes them whatever
    `CanDial` says now, so a call that ended while the capability was off (gate-sip's dial switch, a stale republish)
    reads idle and Talk's own 300 s memory of it is dropped at once instead of standing."""
    from unittest.mock import PropertyMock, patch

    entry = await _setup_door(hass)
    provider = _provider(hass)
    assert entry.runtime_data.can_dial is False and "outbound_state" not in provider.call_state(GATE_SIP_URL)
    with patch.object(type(entry.runtime_data), "dial_configured", new_callable=PropertyMock, return_value=True):
        snapshot = provider.call_state(GATE_SIP_URL)
    assert (snapshot["outbound_state"], snapshot["outbound_call_id"]) == ("idle", None)
