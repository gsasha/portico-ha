"""Tests for signaling.py's portico/* websocket_api commands (§9.5),
against a real HA websocket connection (pytest-homeassistant-custom-
component's hass_ws_client) — not a mocked connection object, since the
real risk here is the same class of bug this project keeps finding:
a wrong assumption about a real HA API's shape.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from .driver_part import TransportError

from ..config_flow import DOMAIN
from ..provider import DATA_PROVIDERS
from .driver_part import _add_device, install_provider  # noqa: F401 - the driver part's real provider, wired in
from .fake_provider import FakeProvider

pytestmark = pytest.mark.asyncio


async def _setup_signaling(hass):
    from ..signaling import async_setup_signaling

    install_provider(hass)      # what the two domain setups do: the registry, and the driver part's own provider
    async_setup_signaling(hass)


def _registry(hass):
    return hass.data[DOMAIN][DATA_PROVIDERS]


async def test_doors_carry_facts_and_never_secrets(hass, hass_ws_client):
    _add_device(hass, enrolled_phones=[])
    await _setup_signaling(hass)

    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "portico/doors"})
    resp = await client.receive_json()

    assert resp["success"], resp
    doors = resp["result"]["doors"]
    assert "devices" not in resp["result"]
    assert len(doors) == 1
    assert doors[0]["capabilities"]["DirectCapable"] is True
    assert "secrets" not in doors[0] and "entry_id" not in doors[0] and "device_id" not in doors[0]


async def test_doors_never_reveal_secrets_even_to_the_enrolled_phone(hass, hass_ws_client, hass_admin_user):
    """WS-03: the secrets half of v1's `devices` is gone and no command serves it (`doors/secrets` is cut)."""
    _add_device(hass, enrolled_phones=[{"ha_user_id": hass_admin_user.id, "name": "test phone"}])
    await _setup_signaling(hass)

    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "portico/doors"})
    resp = await client.receive_json()

    assert resp["success"], resp
    assert "secrets" not in resp["result"]["doors"][0]
    assert "sip_user" not in str(resp)
    await client.send_json({"id": 2, "type": "portico/doors/secrets"})
    assert (await client.receive_json())["error"]["code"] == "unknown_command"


async def test_subscribe_receives_a_real_call_state_change(hass, hass_ws_client):
    entry, runtime = _add_device(hass)
    await _setup_signaling(hass)

    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "portico/calls/subscribe"})
    ack = await client.receive_json()
    assert ack["success"], ack
    snapshot = await asyncio.wait_for(client.receive_json(), timeout=5)  # G1: one snapshot per door first
    assert snapshot["event"]["snapshot"] is True

    # Simulate a real notification arriving from the transport binary —
    # exactly what __init__.py's _handle_notification does.
    runtime.call_state.apply_event({"call_id": "c1", "state": "ringing"})

    event = await client.receive_json()
    assert event["type"] == "event"
    assert event["event"]["type"] == "call_state_changed"
    assert event["event"]["call_id"] == "c1"
    assert event["event"]["state"] == "ringing"
    assert event["event"]["prev_state"] == "idle" and event["event"]["door_id"] == "127.0.0.1"


# --------------------------------------------------------------------------
# G1 (portico_app_p2_plan.md §4): `ended` with the call id, and a snapshot
# --------------------------------------------------------------------------


async def _recv(client):
    # A bounded wait: a missing event fails the test instead of hanging it.
    return await asyncio.wait_for(client.receive_json(), timeout=5)


async def _subscribe(client, msg_id=1):
    await client.send_json({"id": msg_id, "type": "portico/calls/subscribe"})
    ack = await _recv(client)
    assert ack["type"] == "result" and ack["success"], ack
    return ack


async def _events(client, n):
    out = []
    for _ in range(n):
        msg = await _recv(client)
        assert msg["type"] == "event", msg
        out.append(msg["event"])
    return out


async def test_calls_subscribe_sends_the_result_then_one_idle_snapshot_per_door(hass, hass_ws_client):
    _add_device(hass)
    await _setup_signaling(hass)
    client = await hass_ws_client(hass)

    await _subscribe(client)  # the result is the first message
    (snap,) = await _events(client, 1)
    assert snap == {
        "type": "call_state_changed", "snapshot": True,
        "door_id": "127.0.0.1",
        "call_id": None, "state": "idle",
    }


async def test_a_provider_snapshot_that_is_malformed_or_not_a_snapshot_state_is_dropped(hass, hass_ws_client, caplog):
    """HA-L2: `provider.call_state()` is validated like a transition (CallSnapshot.from_dict) and its state must be
    idle|ringing|answered|active. A bad dict (no call_id key, a non-dict, `ended`, an unknown string) loses that
    door's snapshot, logged; it neither fails the subscribe nor reaches the wire."""
    await _setup_signaling(hass)
    fake = FakeProvider("fake")
    bad = {
        "no-call-id": {"door_id": "no-call-id", "state": "idle"},
        "ended": {"door_id": "ended", "call_id": None, "state": "ended"},
        "bogus": {"door_id": "bogus", "call_id": None, "state": "bogus"},
        "unavailable": {"door_id": "unavailable", "call_id": None, "state": "unavailable"},
        "not-a-dict": "idle",
    }
    for door_id in ("good", *bad):
        fake.add_door(door_id)
    fake.call_state = lambda door_id: (
        {"door_id": door_id, "call_id": "c9", "state": "answered"} if door_id == "good" else bad[door_id])
    _registry(hass).register(fake)
    client = await hass_ws_client(hass)

    await _subscribe(client)
    (snap,) = await _events(client, 1)
    assert (snap["door_id"], snap["call_id"], snap["state"], snap["snapshot"]) == ("good", "c9", "answered", True)

    # A door that arrives later gets the same treatment: the bad one sends nothing, the good one its snapshot.
    fake.call_state = lambda door_id: (
        {"door_id": door_id, "call_id": None, "state": "idle"} if door_id == "late-good" else {"door_id": door_id})
    fake.add_door("late-bad")
    fake.add_door("late-good")
    (late,) = await _events(client, 1)
    assert (late["door_id"], late["state"], late["snapshot"]) == ("late-good", "idle", True)
    for door_id in ("no-call-id", "ended", "bogus", "unavailable", "not-a-dict", "late-bad"):
        assert door_id in caplog.text


async def test_snapshot_during_a_live_ring_reports_ringing_with_the_call_id(hass, hass_ws_client):
    _entry, runtime = _add_device(hass)
    runtime.call_state.apply_event({"call_id": "c7", "state": "ringing"})
    await _setup_signaling(hass)
    client = await hass_ws_client(hass)

    await _subscribe(client)
    (snap,) = await _events(client, 1)
    assert snap["snapshot"] is True
    assert (snap["call_id"], snap["state"], snap["door_id"]) == ("c7", "ringing", "127.0.0.1")


async def test_ring_then_end_sends_ended_with_the_ended_call_id_and_no_idle_echo(hass, hass_ws_client):
    _entry, runtime = _add_device(hass)
    await _setup_signaling(hass)
    client = await hass_ws_client(hass)
    await _subscribe(client)
    await _events(client, 1)  # snapshot

    runtime.call_state.apply_event({"call_id": "c1", "state": "ringing"})
    runtime.call_state.apply_event({"call_id": "c1", "state": "answered"})
    runtime.call_state.apply_event({"call_id": "c1", "state": "ended"})

    base = {"type": "call_state_changed", "door_id": "127.0.0.1"}
    assert await _events(client, 3) == [
        {**base, "call_id": "c1", "state": "ringing", "prev_state": "idle"},
        {**base, "call_id": "c1", "state": "answered", "prev_state": "ringing"},
        {**base, "call_id": "c1", "state": "ended", "prev_state": "answered"},   # the call that ended, not null
    ]
    # and nothing follows: the v1 `idle` echo after `ended` is gone (WS-04)
    runtime.call_state.apply_event({"call_id": "m", "state": "ringing"})
    (marker,) = await _events(client, 1)
    assert (marker["call_id"], marker["state"]) == ("m", "ringing")


async def test_ended_without_a_call_id_still_names_the_tracked_call(hass, hass_ws_client):
    """_end_tracked_call / a Go `ended` may carry only the tracker's id or
    none; the event names the call the tracker was following."""
    _entry, runtime = _add_device(hass)
    await _setup_signaling(hass)
    client = await hass_ws_client(hass)
    await _subscribe(client)
    await _events(client, 1)

    runtime.call_state.apply_event({"call_id": "c2", "state": "ringing"})
    runtime.call_state.apply_event({"state": "ended"})
    events = await _events(client, 2)
    assert [(e["call_id"], e["state"]) for e in events] == [("c2", "ringing"), ("c2", "ended")]


async def test_a_dropped_late_event_sends_nothing(hass, hass_ws_client):
    _entry, runtime = _add_device(hass)
    await _setup_signaling(hass)
    client = await hass_ws_client(hass)
    await _subscribe(client)
    await _events(client, 1)

    runtime.call_state.apply_event({"call_id": "c1", "state": "ringing"})
    runtime.call_state.apply_event({"call_id": "c1", "state": "ended"})
    runtime.call_state.apply_event({"call_id": "c1", "state": "answered"})  # late: dropped
    runtime.call_state.apply_event({"call_id": "c3", "state": "ringing"})   # a marker after it
    events = await _events(client, 3)
    assert [(e["call_id"], e["state"]) for e in events] == [
        ("c1", "ringing"), ("c1", "ended"), ("c3", "ringing"),
    ]


async def test_two_devices_get_a_snapshot_each_and_their_own_events(hass, hass_ws_client):
    _entry_a, runtime_a = _add_device(hass, device_id="10.3.0.20")
    _entry_b, runtime_b = _add_device(hass, device_id="10.3.0.21")
    runtime_b.call_state.apply_event({"call_id": "b1", "state": "ringing"})
    await _setup_signaling(hass)
    client = await hass_ws_client(hass)
    await _subscribe(client)

    snaps = await _events(client, 2)
    assert all(s["snapshot"] is True for s in snaps)
    assert {(s["door_id"], s["call_id"], s["state"]) for s in snaps} == {
        ("10.3.0.20", None, "idle"),
        ("10.3.0.21", "b1", "ringing"),
    }

    runtime_b.call_state.apply_event({"call_id": "b1", "state": "ended"})
    runtime_a.call_state.apply_event({"call_id": "a1", "state": "ringing"})
    events = await _events(client, 2)
    assert [(e["door_id"], e["call_id"], e["state"]) for e in events] == [
        ("10.3.0.21", "b1", "ended"),
        ("10.3.0.20", "a1", "ringing"),
    ]
    assert not any("snapshot" in e for e in events)


@pytest.mark.parametrize("yields", [0, 1, 2, 3, 4, 6, 8, 12])
async def test_no_gap_between_the_snapshot_and_live_events(hass, hass_ws_client, yields):
    """The handler is one synchronous @callback: registering the listeners,
    reading the snapshot and sending result + snapshots happen with no await
    in between, so no transition can land between them. A change applied
    around the moment the subscribe is processed (`yields` loop turns after
    sending it, so it lands before, between or after) is either inside the
    snapshot or arrives after it as a live event: never lost, never
    duplicated, never ahead of the result or the snapshot."""
    from homeassistant.core import is_callback

    from ..signaling import handle_calls_subscribe

    assert is_callback(handle_calls_subscribe)

    _entry, runtime = _add_device(hass)
    await _setup_signaling(hass)
    client = await hass_ws_client(hass)

    await client.send_json({"id": 1, "type": "portico/calls/subscribe"})
    for _ in range(yields):
        await asyncio.sleep(0)
    runtime.call_state.apply_event({"call_id": "c1", "state": "ringing"})
    for _ in range(yields):
        await asyncio.sleep(0)
    runtime.call_state.apply_event({"call_id": "c1", "state": "ended"})
    await hass.async_block_till_done()
    runtime.call_state.apply_event({"call_id": "m", "state": "ringing"})  # end marker

    first = await _recv(client)
    assert first["type"] == "result", first
    rest = []
    while not rest or rest[-1]["call_id"] != "m":
        msg = await _recv(client)
        assert msg["type"] == "event", msg
        rest.append(msg["event"])
    assert rest[0]["snapshot"] is True
    assert not any("snapshot" in e for e in rest[1:])
    seen = [(e["call_id"], e["state"]) for e in rest]
    tail = [("c1", "ringing"), ("c1", "ended"), ("m", "ringing")]
    # The snapshot is some prefix-state of the sequence; the live events are
    # exactly the rest of it.
    assert seen in (
        [(None, "idle")] + tail,
        tail,                       # snapshot taken while c1 rang
        [(None, "idle")] + tail[2:],  # snapshot after the end
        [("m", "ringing")],         # snapshot after everything
    ), seen


async def test_unsubscribe_removes_the_transition_listener(hass, hass_ws_client):
    """The subscription is one listener on the provider registry; the tracker keeps only the provider's own."""
    _entry, runtime = _add_device(hass)
    await _setup_signaling(hass)
    client = await hass_ws_client(hass)
    before_t = list(runtime.call_state._transition_listeners)
    before_l = list(runtime.call_state._listeners)
    before_r = list(_registry(hass)._listeners)

    await _subscribe(client)
    await _events(client, 1)
    assert len(_registry(hass)._listeners) == len(before_r) + 1
    assert runtime.call_state._transition_listeners == before_t  # the signaling layer never touches the tracker
    assert runtime.call_state._listeners == before_l  # no plain listener any more

    await client.send_json({"id": 2, "type": "unsubscribe_events", "subscription": 1})
    resp = await _recv(client)
    assert resp["success"], resp
    assert _registry(hass)._listeners == before_r
    assert runtime.call_state._transition_listeners == before_t
    assert runtime.call_state._listeners == before_l


async def test_closing_the_connection_removes_the_transition_listener(hass, hass_ws_client):
    _entry, runtime = _add_device(hass)
    await _setup_signaling(hass)
    client = await hass_ws_client(hass)
    await _subscribe(client)
    await _events(client, 1)
    assert len(_registry(hass)._listeners) == 1

    await client.close()
    await hass.async_block_till_done()
    assert _registry(hass)._listeners == []


async def test_a_raising_transition_listener_does_not_break_the_subscription(hass, hass_ws_client):
    _entry, runtime = _add_device(hass)

    def boom(_transition):
        raise RuntimeError("boom")

    runtime.call_state.add_transition_listener(boom)  # registered before the subscription
    await _setup_signaling(hass)
    client = await hass_ws_client(hass)
    await _subscribe(client)
    await _events(client, 1)

    runtime.call_state.apply_event({"call_id": "c1", "state": "ringing"})
    runtime.call_state.apply_event({"call_id": "c1", "state": "ended"})
    events = await _events(client, 2)
    assert [(e["call_id"], e["state"]) for e in events] == [("c1", "ringing"), ("c1", "ended")]
    assert runtime.call_state.state == "idle"


async def test_two_subscriptions_each_get_snapshot_and_events(hass, hass_ws_client):
    _entry, runtime = _add_device(hass)
    await _setup_signaling(hass)
    c1 = await hass_ws_client(hass)
    c2 = await hass_ws_client(hass)
    await _subscribe(c1)
    await _events(c1, 1)
    await _subscribe(c2)
    await _events(c2, 1)

    runtime.call_state.apply_event({"call_id": "c1", "state": "ringing"})
    for client in (c1, c2):
        (e,) = await _events(client, 1)
        assert (e["call_id"], e["state"]) == ("c1", "ringing")


async def test_answer_already_answered_race(hass, hass_ws_client):
    entry, runtime = _add_device(hass)
    runtime.call_state.apply_event({"call_id": "c1", "state": "ringing"})
    await _setup_signaling(hass)

    client = await hass_ws_client(hass)

    await client.send_json({
        "id": 1, "type": "portico/answer", "door_id": "127.0.0.1",
        "call_id": "c1", "client_kind": "app",
    })
    resp1 = await client.receive_json()
    assert resp1["success"], resp1
    assert resp1["result"]["mode"] == "direct"

    # Someone else (or a retry) tries to answer the SAME call again.
    await client.send_json({
        "id": 2, "type": "portico/answer", "door_id": "127.0.0.1",
        "call_id": "c1", "client_kind": "app",
    })
    resp2 = await client.receive_json()
    assert resp2["success"] is False
    assert resp2["error"]["code"] == "already_answered"


async def test_answer_mode_direct_for_app_against_direct_capable_driver(hass, hass_ws_client):
    entry, runtime = _add_device(hass, capabilities={"DirectCapable": True})
    runtime.call_state.apply_event({"call_id": "c1", "state": "ringing"})
    await _setup_signaling(hass)

    client = await hass_ws_client(hass)
    await client.send_json({
        "id": 1, "type": "portico/answer", "door_id": "127.0.0.1",
        "call_id": "c1", "client_kind": "app",
    })
    resp = await client.receive_json()
    assert resp["success"], resp
    assert resp["result"]["mode"] == "direct"
    # direct mode must NOT touch the transport binary at all (§9.6).
    runtime.client.answer.assert_not_called()
    assert runtime.call_state.state == "answered"


async def test_answer_mode_relay_for_browser_even_against_direct_capable_driver(hass, hass_ws_client):
    entry, runtime = _add_device(hass, capabilities={"DirectCapable": True})
    runtime.call_state.apply_event({"call_id": "c1", "state": "ringing"})
    runtime.client.answer = AsyncMock(return_value={"ok": True})
    await _setup_signaling(hass)

    client = await hass_ws_client(hass)
    await client.send_json({
        "id": 1, "type": "portico/answer", "door_id": "127.0.0.1",
        "call_id": "c1", "client_kind": "browser",
        "host": "10.0.0.5", "port": 4000,
    })
    resp = await client.receive_json()
    assert resp["success"], resp
    assert resp["result"]["mode"] == "relay"
    runtime.client.answer.assert_awaited_once_with("127.0.0.1", "10.0.0.5", 4000)


async def test_answer_mode_relay_for_app_against_a_non_direct_capable_driver(hass, hass_ws_client):
    entry, runtime = _add_device(hass, capabilities={"DirectCapable": False})
    runtime.call_state.apply_event({"call_id": "c1", "state": "ringing"})
    runtime.client.answer = AsyncMock(return_value={"ok": True})
    await _setup_signaling(hass)

    client = await hass_ws_client(hass)
    await client.send_json({
        "id": 1, "type": "portico/answer", "door_id": "127.0.0.1",
        "call_id": "c1", "client_kind": "app",
    })
    resp = await client.receive_json()
    assert resp["success"], resp
    assert resp["result"]["mode"] == "relay"


async def test_dial_unknown_door_returns_a_clean_error(hass, hass_ws_client):
    await _setup_signaling(hass)
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "portico/dial", "door_id": "no-such-device"})
    resp = await client.receive_json()
    assert resp["success"] is False
    assert resp["error"]["code"] == "unknown_door"


async def test_dial_propagates_a_real_transport_error(hass, hass_ws_client):
    entry, runtime = _add_device(hass)
    runtime.client.dial = AsyncMock(side_effect=TransportError("dial_failed", "device unreachable"))
    await _setup_signaling(hass)

    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "portico/dial", "door_id": "127.0.0.1"})
    resp = await client.receive_json()
    assert resp["success"] is False
    assert resp["error"]["code"] == "dial_failed"
    assert resp["error"]["message"] == "device unreachable"


async def test_webrtc_offer_relays_to_transport_and_returns_the_answer(hass, hass_ws_client):
    entry, runtime = _add_device(hass)
    runtime.call_state.apply_event({"call_id": "c1", "state": "ringing"})   # the call the offer is about
    runtime.client.webrtc_offer = AsyncMock(return_value={"sdp": "v=0..."})
    await _setup_signaling(hass)

    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "portico/webrtc_offer", "call_id": "c1", "door_id": "127.0.0.1",
                            "sdp": "v=0 offer"})
    resp = await client.receive_json()
    assert resp["success"], resp
    assert resp["result"]["sdp"] == "v=0..."
    runtime.client.webrtc_offer.assert_awaited_once_with("c1", "v=0 offer")


async def test_doors_carry_the_derived_push_ref_not_the_entry_id(hass, hass_ws_client):
    """Push v2 (M6 E3): a push names its door by `door_ref`, `d-` and 32 hex derived from the door's endpoint id
    (never the config entry id, never the device id, which for the observer is an internal URL); the app maps it to
    a door through `portico/doors`."""
    entry, _runtime = _add_device(hass)
    await _setup_signaling(hass)
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "portico/doors"})
    resp = await client.receive_json()
    (door,) = resp["result"]["doors"]
    (real,) = _registry(hass).doors()
    assert door["door_ref"] == real.door_ref and door["door_ref"] != entry.entry_id
    assert entry.entry_id not in str(resp["result"])


# --------------------------------------------------------------------------
# The door's display name (the app's ring screen, PR-F)
# --------------------------------------------------------------------------


async def test_doors_name_each_door_by_its_entry_title(hass, hass_ws_client, hass_admin_user):
    """The app shows `name` on the ring screen (DevicesCache.nameFor); without
    it every ring says "Door". The name is the config entry's title, the one
    the installer sees and can rename in Settings > Devices & Services."""
    _add_device(hass, device_id="10.3.0.20", title="Front door")
    _add_device(hass, device_id="10.3.0.21", title="Vehicle gate",
                          enrolled_phones=[{"ha_user_id": hass_admin_user.id, "name": "p"}])
    await _setup_signaling(hass)
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "portico/doors"})
    resp = await client.receive_json()

    assert resp["success"], resp
    by_door = {d["door_id"]: d for d in resp["result"]["doors"]}
    assert by_door["10.3.0.20"]["name"] == "Front door"   # a browser-scoped device
    assert by_door["10.3.0.21"]["name"] == "Vehicle gate"  # an enrolled phone's device


async def test_door_name_follows_a_renamed_entry(hass, hass_ws_client):
    entry, _ = _add_device(hass, title="Gate")
    await _setup_signaling(hass)
    hass.config_entries.async_update_entry(entry, title="Back gate")
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "portico/doors"})
    resp = await client.receive_json()
    assert resp["result"]["doors"][0]["name"] == "Back gate"


async def test_a_door_has_exactly_the_contract_3_keys(hass, hass_ws_client):
    """`portico/doors` (3.4): `door_id`, `door_ref`, `endpoint_id`, `name`, `driver`, `capabilities`,
    `camera_entity_id`, `available`, `away_media`, `answerable`; `entry_id` and the secrets half are gone."""
    _add_device(hass, driver="dahua_sip", capabilities={"DirectCapable": True, "HasRelay": True},
                title="Front door")
    await _setup_signaling(hass)
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "portico/doors"})
    resp = await client.receive_json()
    (door,) = resp["result"]["doors"]
    assert set(door) == {"door_id", "door_ref", "endpoint_id", "name", "driver", "capabilities", "camera_entity_id",
                         "available", "away_media", "answerable"}
    assert door["door_id"] == "127.0.0.1" and door["name"] == "Front door" and door["driver"] == "dahua_sip"
    assert door["capabilities"] == {"DirectCapable": True, "HasRelay": True}
    assert isinstance(door["available"], bool) and door["away_media"] is True
    assert door["endpoint_id"] == f"portico_intercom:{door['door_id']}"


async def test_doors_carry_endpoint_id_and_door_ref(hass, hass_ws_client):
    """The card keys `content/get`'s `callers` by the door's endpoint id, which it cannot derive from the bare
    `door_id` (a URL for the live gate); `door_ref` is the derived push ref. Both come from the registry's Door, for
    every provider's doors."""
    await _setup_signaling(hass)
    fake = FakeProvider("fake")
    fake.add_door("http://10.6.0.7:8091", name="Gate", driver="fakedrv", capabilities={},
                  camera_entity_id="camera.gate")
    _registry(hass).register(fake)
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "portico/doors"})
    (door,) = (await client.receive_json())["result"]["doors"]
    (real,) = _registry(hass).doors()
    assert door["endpoint_id"] == "fake:http://10.6.0.7:8091" == real.endpoint_id
    assert door["door_ref"] == real.door_ref and door["door_ref"].startswith("d-") and len(door["door_ref"]) == 34
    assert door["door_id"] == "http://10.6.0.7:8091" and door["camera_entity_id"] == "camera.gate"
    assert "entry_id" not in door and "device_id" not in door


# --------------------------------------------------------------------------
# PH-3a: the door and call commands go through the provider registry
# --------------------------------------------------------------------------


async def _ws(hass, hass_ws_client, body, msg_id=1):
    client = await hass_ws_client(hass)
    await client.send_json({"id": msg_id, **body})
    return await client.receive_json()


async def test_no_provider_doors_is_empty_and_answer_is_not_available(hass, hass_ws_client):
    """STRUCT-07: with no provider registered nothing crashes; the door list is empty and every door or
    call feature answers `not_available` (the v1 wire has no other word for it)."""
    from ..signaling import async_setup_signaling

    async_setup_signaling(hass)           # no registry, no provider: the UI part alone
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "portico/doors"})
    assert (await client.receive_json())["result"] == {"doors": []}
    for i, body in enumerate([
        {"type": "portico/answer", "door_id": "d", "call_id": "c", "client_kind": "app"},
        {"type": "portico/dial", "door_id": "d"},
        {"type": "portico/open_relay", "door_id": "d"},
    ], start=2):
        await client.send_json({"id": i, **body})
        resp = await client.receive_json()
        assert resp["success"] is False and resp["error"]["code"] == "not_available", resp
    await client.send_json({"id": 9, "type": "portico/calls/subscribe"})
    assert (await client.receive_json())["success"]            # a subscription to nothing is fine
    await client.send_json({"id": 10, "type": "portico/webrtc_offer", "call_id": "c", "door_id": "d", "sdp": "x"})
    resp = await client.receive_json()
    assert resp["error"]["code"] == "not_available"             # the offer names a door now: no provider, no door
    await client.send_json({"id": 11, "type": "portico/webrtc_answer", "call_id": "c", "sdp": "x"})
    assert (await client.receive_json())["error"]["code"] == "no_transport"   # answer/ice stay keyed by the call


async def test_a_provider_with_no_such_door_is_unknown_door(hass, hass_ws_client):
    await _setup_signaling(hass)
    for body in ({"type": "portico/open_relay", "door_id": "nope"},
                 {"type": "portico/answer", "door_id": "nope", "call_id": "c", "client_kind": "app"},
                 {"type": "portico/webrtc_offer", "door_id": "nope", "call_id": "c", "sdp": "x"}):
        resp = await _ws(hass, hass_ws_client, body)
        assert resp["error"]["code"] == "unknown_door", body


async def test_webrtc_offer_on_the_second_door_reaches_that_door(hass, hass_ws_client):
    """PROC-02: a call is routed to the door that owns it, never to "the first live entry"."""
    _first, first = _add_device(hass, device_id="10.3.0.20")
    _second, second = _add_device(hass, device_id="10.3.0.21")
    first.client.webrtc_offer = AsyncMock(return_value={"sdp": "from-first"})
    second.client.webrtc_offer = AsyncMock(return_value={"sdp": "from-second"})
    second.client.webrtc_answer = AsyncMock(return_value={"ok": True})
    second.client.webrtc_ice = AsyncMock(return_value={"ok": True})
    await _setup_signaling(hass)
    second.call_state.apply_event({"call_id": "c2", "state": "ringing"})

    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "portico/webrtc_offer", "call_id": "c2", "door_id": "10.3.0.21",
                            "sdp": "offer"})
    resp = await client.receive_json()
    assert resp["success"] and resp["result"] == {"sdp": "from-second"}
    second.client.webrtc_offer.assert_awaited_once_with("c2", "offer")
    first.client.webrtc_offer.assert_not_awaited()
    await client.send_json({"id": 2, "type": "portico/webrtc_answer", "call_id": "c2", "sdp": "ans"})
    assert (await client.receive_json())["success"]
    second.client.webrtc_answer.assert_awaited_once_with("c2", "ans")
    await client.send_json({"id": 3, "type": "portico/webrtc_ice", "call_id": "c2", "candidate": "cand"})
    assert (await client.receive_json())["success"]
    second.client.webrtc_ice.assert_awaited_once_with("c2", "cand")


async def test_webrtc_on_the_second_of_two_providers_reaches_that_provider(hass, hass_ws_client):
    await _setup_signaling(hass)
    fakes = {}
    for domain in ("fake_a", "fake_b"):
        fakes[domain] = FakeProvider(domain)
        fakes[domain].add_door(f"{domain}-door")
        _registry(hass).register(fakes[domain])
    fakes["fake_b"].ring("fake_b-door", "cb")
    resp = await _ws(hass, hass_ws_client, {"type": "portico/webrtc_offer", "call_id": "cb",
                                            "door_id": "fake_b-door", "sdp": "o"})
    assert resp["result"] == {"sdp": "fake-answer-sdp"}
    assert fakes["fake_b"].calls_to("webrtc_offer") == [(("cb", "o"), {})]
    assert fakes["fake_a"].calls_to("webrtc_offer") == []


async def test_a_call_nobody_owns_is_unknown_call(hass, hass_ws_client):
    _add_device(hass)
    await _setup_signaling(hass)
    for command, extra in (("hangup", {}), ("keepalive", {}), ("webrtc_offer", {"sdp": "x", "door_id": "127.0.0.1"}),
                           ("webrtc_ice", {"candidate": "x"})):
        resp = await _ws(hass, hass_ws_client, {"type": f"portico/{command}", "call_id": "ghost", **extra})
        assert resp["success"] is False and resp["error"] == {"code": "unknown_call", "message": "ghost"}, command


async def test_hangup_and_keepalive_reach_the_door_that_rang(hass, hass_ws_client):
    _entry, runtime = _add_device(hass)
    runtime.client.hangup = AsyncMock(return_value={"ok": True})
    runtime.client.keepalive = AsyncMock(return_value={"ok": True, "ttl": 5})
    await _setup_signaling(hass)
    runtime.call_state.apply_event({"call_id": "c1", "state": "ringing"})

    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "portico/keepalive", "call_id": "c1"})
    assert (await client.receive_json())["result"] == {"ok": True, "ttl": 5}
    await client.send_json({"id": 2, "type": "portico/hangup", "call_id": "c1", "reason": "done"})
    resp = await client.receive_json()
    assert resp["success"] and resp["result"] == {"ok": True}
    runtime.client.hangup.assert_awaited_once_with("c1", "done")
    assert runtime.call_state.state == "idle"       # the provider ends the tracked call after a good hangup


async def test_hangup_transport_error_is_passed_through_with_its_code(hass, hass_ws_client):
    _entry, runtime = _add_device(hass)
    runtime.client.hangup = AsyncMock(side_effect=TransportError("no_such_call", "gone"))
    await _setup_signaling(hass)
    runtime.call_state.apply_event({"call_id": "c1", "state": "ringing"})
    resp = await _ws(hass, hass_ws_client, {"type": "portico/hangup", "call_id": "c1"})
    assert resp["error"] == {"code": "no_such_call", "message": "gone"}
    assert runtime.call_state.state == "ringing"


async def test_hangup_after_dial_is_routed_to_the_dialling_provider(hass, hass_ws_client):
    """R2-08: a dialled call has no ring and no transition, so the registry learns its id from dial()'s result."""
    await _setup_signaling(hass)
    fake = FakeProvider("fake")
    fake.add_door("gate-1")
    _registry(hass).register(fake)
    client = await hass_ws_client(hass)

    await client.send_json({"id": 1, "type": "portico/dial", "door_id": "gate-1", "host": "10.9.0.1", "port": 7})
    dialled = await client.receive_json()
    assert dialled["success"], dialled
    call_id = dialled["result"]["call_id"]
    assert fake.calls_to("dial") == [(("gate-1", "10.9.0.1", 7), {})]

    await client.send_json({"id": 2, "type": "portico/hangup", "call_id": call_id, "reason": "bye"})
    assert (await client.receive_json())["success"]
    assert fake.calls_to("hangup") == [((call_id, "bye"), {})]
    await client.send_json({"id": 3, "type": "portico/keepalive", "call_id": call_id})
    assert (await client.receive_json())["success"]
    assert fake.calls_to("keepalive") == [((call_id,), {})]


async def test_commands_name_a_door_bare_or_namespaced_and_reach_its_provider(hass, hass_ws_client):
    await _setup_signaling(hass)
    fake = FakeProvider("fake")
    fake.add_door("http://10.6.0.7:8091")           # a URL id, like the live gate's
    _registry(hass).register(fake)
    client = await hass_ws_client(hass)
    for i, door_id in enumerate(["http://10.6.0.7:8091", "fake:http://10.6.0.7:8091"], start=1):
        await client.send_json({"id": i, "type": "portico/open_relay", "door_id": door_id})
        assert (await client.receive_json())["success"]
    assert fake.calls_to("open_relay") == [(("http://10.6.0.7:8091",), {})] * 2


async def test_a_bare_door_id_two_providers_supply_is_ambiguous(hass, hass_ws_client):
    await _setup_signaling(hass)
    for domain in ("fake_a", "fake_b"):
        fake = FakeProvider(domain)
        fake.add_door("same")
        _registry(hass).register(fake)
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "portico/open_relay", "door_id": "same"})
    assert (await client.receive_json())["error"]["code"] == "ambiguous_door"
    await client.send_json({"id": 2, "type": "portico/open_relay", "door_id": "fake_b:same"})
    assert (await client.receive_json())["success"]


async def test_a_providers_error_with_a_code_becomes_that_error(hass, hass_ws_client):
    from ..provider import ProviderError

    await _setup_signaling(hass)
    fake = FakeProvider("fake")
    fake.add_door("gate-1")
    fake.raise_on["open_relay"] = ProviderError("door_busy", "try later")
    _registry(hass).register(fake)
    resp = await _ws(hass, hass_ws_client, {"type": "portico/open_relay", "door_id": "gate-1"})
    assert resp["error"] == {"code": "door_busy", "message": "try later"}


async def test_doors_and_calls_subscribe_list_another_providers_doors_too(hass, hass_ws_client):
    """A second provider's door shows in the list and its transitions reach the subscriber; a door that leaves gets the registry's `unavailable` snapshot
    and one that arrives its snapshot (PH-5)."""
    await _setup_signaling(hass)
    fake = FakeProvider("fake")
    fake.add_door("gate-1", name="Side gate", driver="fakedrv", capabilities={"DirectCapable": False})
    _registry(hass).register(fake)
    client = await hass_ws_client(hass)

    await client.send_json({"id": 1, "type": "portico/doors"})
    (door,) = (await client.receive_json())["result"]["doors"]
    assert door == {"door_id": "gate-1", "driver": "fakedrv",
                    "capabilities": {"DirectCapable": False}, "name": "Side gate",
                    "endpoint_id": "fake:gate-1", "door_ref": _registry(hass).doors()[0].door_ref,
                    "camera_entity_id": None, "available": True, "away_media": False,
                    "answerable": True}

    await _subscribe(client, 2)
    (snap,) = await _events(client, 1)
    assert snap == {"type": "call_state_changed", "snapshot": True, "door_id": "gate-1",
                    "call_id": None, "state": "idle"}
    fake.ring("gate-1", "f1")
    fake.remove_door("gate-1")
    fake.add_door("gate-2")
    fake.ring("gate-2", "f2")
    events = await _events(client, 4)
    assert [(e["door_id"], e["call_id"], e["state"], e.get("snapshot", False)) for e in events] == [
        ("gate-1", "f1", "ringing", False), ("gate-1", None, "unavailable", True),
        ("gate-2", None, "idle", True), ("gate-2", "f2", "ringing", False)]


async def test_answer_returns_the_providers_verdict_for_any_provider(hass, hass_ws_client):
    """PH-3b: one arbitration, in the provider. A provider that is not this integration's own answers through
    the same handler; the handler turns its verdict into the wire (`already_answered` stays the code for
    `taken_elsewhere`, M6 review 7 decision 5)."""
    await _setup_signaling(hass)
    fake = FakeProvider("fake")
    fake.add_door("gate-1")
    _registry(hass).register(fake)
    client = await hass_ws_client(hass)
    body = {"type": "portico/answer", "door_id": "gate-1", "call_id": "c", "client_kind": "app"}

    await client.send_json({"id": 1, **body})
    resp = await client.receive_json()
    assert resp["error"]["code"] == "not_available"              # verdict not_available: nothing is ringing

    fake.ring("gate-1", "c")
    await client.send_json({"id": 2, **body, "host": "h", "port": 4})
    resp = await client.receive_json()
    assert resp["success"] and resp["result"] == {"mode": "direct"}
    call = fake.calls_to("answer")[1]          # the first, with nothing ringing, came before
    assert call[0] == ("gate-1", "c") and call[1]["client_kind"] == "app" and call[1]["host"] == "h"
    assert call[1]["claimant"].count(":") == 1          # "<ha user id>:<connection id>"

    await client.send_json({"id": 3, **body})
    assert (await client.receive_json())["error"]["code"] == "already_answered"      # taken_elsewhere


async def test_an_entry_unload_and_reload_reach_a_subscriber_as_unavailable_then_a_fresh_snapshot(
        hass, hass_ws_client):
    """PH-5 on this integration's own provider (the runbook's T8): detach is an unload, attach a setup. The
    `unavailable` snapshot names the door, the other door is untouched."""
    entry1, _r1 = _add_device(hass, device_id="10.0.0.1")
    _entry2, r2 = _add_device(hass, device_id="10.0.0.2")
    await _setup_signaling(hass)
    client = await hass_ws_client(hass)
    await _subscribe(client)
    await _events(client, 2)
    provider = install_provider(hass)

    provider.detach(entry1)
    (gone,) = await _events(client, 1)
    assert gone == {"type": "call_state_changed", "snapshot": True, "door_id": "10.0.0.1",
                    "call_id": None, "state": "unavailable"}
    assert [d.id for d in _registry(hass).doors()] == ["10.0.0.2"]

    provider.attach(entry1)
    (back,) = await _events(client, 1)
    assert back == {"type": "call_state_changed", "snapshot": True, "door_id": "10.0.0.1",
                    "call_id": None, "state": "idle"}
    r2.call_state.apply_event({"call_id": "c2", "state": "ringing"})
    (ring,) = await _events(client, 1)
    assert (ring["door_id"], ring["state"]) == ("10.0.0.2", "ringing")


async def test_a_refused_verdict_is_refused_on_the_wire(hass, hass_ws_client):
    await _setup_signaling(hass)
    fake = FakeProvider("fake")
    fake.add_door("gate-1")
    fake.refuse_answers = True
    _registry(hass).register(fake)
    fake.ring("gate-1", "c")
    resp = await _ws(hass, hass_ws_client,
                     {"type": "portico/answer", "door_id": "gate-1", "call_id": "c", "client_kind": "app"})
    assert resp["error"]["code"] == "refused"


async def test_a_providers_answer_error_with_a_code_becomes_that_error(hass, hass_ws_client):
    await _setup_signaling(hass)
    fake = FakeProvider("fake")
    fake.add_door("gate-1")
    fake.raise_on["answer"] = TransportError("answer_failed", "device said no")
    _registry(hass).register(fake)
    resp = await _ws(hass, hass_ws_client,
                     {"type": "portico/answer", "door_id": "gate-1", "call_id": "c", "client_kind": "app"})
    assert resp["error"] == {"code": "answer_failed", "message": "device said no"}


def test_handle_answer_no_longer_compares_call_state():
    """PH-3b reviewer check: the state comparison is gone from signaling.py (it lives once, in the driver part's
    provider_impl; the driver part's test_transport_provider.py checks that side)."""
    import inspect

    from .. import signaling

    body = inspect.getsource(signaling.handle_answer)
    for needle in ("call_state", '"ringing"', "answer_raw", "after_answer", "TransportError"):
        assert needle not in body, needle


def test_no_entries_zero_anywhere():
    """PROC-02 / STRUCT-07: nothing in the UI part picks "the first live entry" (a source grep)."""
    from pathlib import Path

    base = Path(__file__).resolve().parent.parent
    offenders = [str(p.relative_to(base)) for p in base.rglob("*.py")
                 if "tests" not in p.relative_to(base).parts and "entries[0]" in p.read_text()]
    assert offenders == []


def test_signaling_and_content_never_touch_an_entrys_runtime_data():
    """The reviewer's grep of PH-3a and PH-3b: the door and call commands, the content store and the away
    media no longer reach into a door entry. The push package's only `runtime_data` and `options` are the
    PUSH entry's own (its manager and its settings), which is the UI part's own entry, not a door."""
    import re
    from pathlib import Path

    base = Path(__file__).resolve().parent.parent
    for path in (base / "signaling.py", base / "content.py", base / "away_media.py"):
        text = path.read_text()
        for needle in ("runtime_data", "_device_entries", "_find_entry", "entry_for_door"):
            assert needle not in text, (path.name, needle)
        assert "entry.options" not in text, path.name
    for path in (base / "push").glob("*.py"):
        text = path.read_text()
        assert "runtime_data.call_state" not in text and "_find_entry" not in text, path.name
        for line in text.splitlines():
            if re.search(r"runtime_data|\.options\b", line):
                assert "entry." in line, (path.name, line)      # only ever `entry.runtime_data` / `entry.options`


async def test_version_names_the_contract_and_every_accepted_provider(hass, hass_ws_client):
    """WS-02: the app sends portico/version first and refuses to continue on `contract != 3`. (There is no refusal of
    a provider by its API version any more: the registry's version check went with the fold.)"""
    await _setup_signaling(hass)
    _registry(hass).register(FakeProvider(domain="fake_a"))
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "portico/version"})
    resp = await _recv(client)
    assert resp["success"] and resp["result"] == {
        "contract": 3, "ui_version": 1,
        "providers": [{"domain": "portico_intercom", "api_version": 1}, {"domain": "fake_a", "api_version": 1}],
    }


async def test_version_works_with_no_provider_at_all(hass, hass_ws_client):
    from ..signaling import async_setup_signaling

    async_setup_signaling(hass)           # the UI part alone: no registry
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "portico/version"})
    assert (await _recv(client))["result"] == {"contract": 3, "ui_version": 1, "providers": []}


# --------------------------------------------------------------------------
# M6 K1/E3: screens on the server. `content/get|run|snapshot {endpoint}` for a declared dashboard user, `callers` and
# `roster` in the reply, `content/subscribe` and `content_changed {endpoint, rev}` (contract 3: no flat fields).
# --------------------------------------------------------------------------

_GET_KEYS = {"endpoint", "kind", "rev", "pages", "callers", "roster"}       # (+ `home_name` when set)


def _page(page_id, cameras=(), actions=()):
    return {"id": page_id, "label": page_id.title(),
            "cameras": [{"id": c, "label": c.title(), "entity_id": f"camera.{c}"} for c in cameras],
            "actions": [{"id": a, "label": a.title(), "kind": "script_run", "entity_id": f"script.{a}",
                         "confirm": False} for a in actions]}


def _stored_document(*, pages=(), overrides=None, dashboards=None, endpoints=None):
    data = {"schema_version": 3, "template": {"pages": list(pages), "rules": [], "roster": ["*"]},
            "overrides": overrides or {}, "dashboards": dashboards or {}, "endpoints": endpoints or {}}
    return {"version": 1, "minor_version": 1, "key": "portico.content", "data": data}


async def _screens(hass, hass_storage, hass_ws_client, *, kitchen_extra=None, hall=True, tablet_screens=("kitchen",)):
    from ..content import get_content_store
    from .test_push_registry import _phone_user

    tablet, tablet_token = await _phone_user(hass, name="Kitchen tablet user")
    plain, plain_token = await _phone_user(hass, name="Plain user")
    phone_user, phone_token = await _phone_user(hass, name="Phone user")
    hall_user, hall_token = await _phone_user(hass, name="Hall user")
    _add_device(hass, enrolled_phones=[{"ha_user_id": phone_user.id, "name": "Alice's phone", "enroll_id": "ep-alice"}])
    await _setup_signaling(hass)
    fake = FakeProvider()
    fake.add_door("gate-1", name="Front gate", camera_entity_id="camera.gate_door")
    _registry(hass).register(fake)

    dashboards = {slug: {"name": f"{slug.title()} screen", "ha_user_id": tablet.id, "room": slug.title(),
                         "cameras": ["camera.kitchen_cam"] if slug == "kitchen" else []}
                  for slug in tablet_screens}
    if hall:
        dashboards["hall"] = {"name": "Hall screen", "ha_user_id": hall_user.id, "room": "Hall",
                              "cameras": ["camera.hall_cam"]}
    overrides = {
        "kitchen": {"pages": [_page("extra", cameras=["pantry"], actions=["sprinkle"])], "rules": [], "roster_hide": []},
        "hall": {"pages": [_page("secret", cameras=["hallpage"], actions=["hallonly"])], "rules": [], "roster_hide": []},
    }
    hass_storage["portico.content"] = _stored_document(
        pages=[_page("home", cameras=["lobby"], actions=["evening"])], overrides=overrides, dashboards=dashboards)
    store = get_content_store(hass)
    await store.async_ensure_loaded()
    for entity in ("camera.lobby", "camera.pantry", "camera.gate_door", "camera.kitchen_cam", "camera.hall_cam",
                   "camera.hallpage"):
        hass.states.async_set(entity, "idle")
    for entity in ("script.evening", "script.sprinkle", "script.hallonly"):
        hass.states.async_set(entity, "off")
    clients = {
        "tablet": await hass_ws_client(hass, access_token=tablet_token),
        "plain": await hass_ws_client(hass, access_token=plain_token),
        "phone": await hass_ws_client(hass, access_token=phone_token),
        "hall": await hass_ws_client(hass, access_token=hall_token),
        "admin": await hass_ws_client(hass),
    }
    users = {"tablet": tablet, "plain": plain, "phone": phone_user, "hall": hall_user}
    return store, clients, users, fake


_NEXT_ID: dict[int, int] = {}


async def _call(client, body, msg_id=None):
    """One request on a connection and its reply; the ids a connection sends must increase, so it numbers them
    itself (`msg_id` is accepted for readability and ignored)."""
    _NEXT_ID[id(client)] = n = _NEXT_ID.get(id(client), 0) + 1
    await client.send_json({"id": n, **body})
    return await _recv(client)


async def test_content_get_by_endpoint_for_a_declared_dashboard_user(hass, hass_storage, hass_ws_client):
    store, clients, users, fake = await _screens(hass, hass_storage, hass_ws_client)
    door_key = "fake:gate-1"
    for body in ({"type": "portico/content/get", "endpoint": "kitchen"}, {"type": "portico/content/get"}):
        resp = await _call(clients["tablet"], body)
        assert resp["success"], resp
        result = resp["result"]
        assert set(result) == _GET_KEYS and "phone" not in result    # no phone: it is a screen
        assert result["endpoint"] == "kitchen" and result["kind"] == "dashboard"
        assert [p["id"] for p in result["pages"]] == ["home", "extra"]
        assert door_key in result["callers"] and result["callers"][door_key]["kind"] == "door"
        assert "cameras" not in result and "actions" not in result                     # no flat fields in contract 3
        home = result["pages"][0]
        (block,) = home["blocks"]
        assert set(home) == {"id", "label", "blocks"}
        assert [c["id"] for c in block["cameras"]] == ["lobby"] and [a["id"] for a in block["actions"]] == ["evening"]
        assert result["rev"] == store.resolve_document("kitchen")["rev"]
        assert {e["id"] for e in result["roster"]} >= {"ep-alice", "hall", door_key}
        assert "kitchen" not in {e["id"] for e in result["roster"]}                    # not itself


async def test_content_get_refuses_an_undeclared_user(hass, hass_storage, hass_ws_client):
    _store, clients, _users, _fake = await _screens(hass, hass_storage, hass_ws_client)
    for body in ({"type": "portico/content/get"}, {"type": "portico/content/get", "endpoint": "kitchen"},
                 {"type": "portico/content/run", "action_id": "evening", "endpoint": "kitchen"},
                 {"type": "portico/content/snapshot", "camera_id": "lobby", "endpoint": "kitchen"}):
        resp = await _call(clients["plain"], body)
        assert not resp["success"] and resp["error"]["code"] == "unauthorized", body


async def test_a_user_with_two_screens_must_name_one(hass, hass_storage, hass_ws_client):
    _store, clients, _users, _fake = await _screens(hass, hass_storage, hass_ws_client,
                                                    tablet_screens=("kitchen", "den"))
    resp = await _call(clients["tablet"], {"type": "portico/content/get"})
    assert not resp["success"] and resp["error"]["code"] == "unauthorized"
    assert "name the screen" in resp["error"]["message"]
    for slug in ("kitchen", "den"):
        resp = await _call(clients["tablet"], {"type": "portico/content/get", "endpoint": slug})
        assert resp["success"] and resp["result"]["endpoint"] == slug
    resp = await _call(clients["tablet"], {"type": "portico/content/get", "endpoint": "hall"})   # not one of its screens
    assert not resp["success"] and resp["error"]["code"] == "unauthorized"


async def test_a_screen_naming_another_screens_endpoint_is_refused(hass, hass_storage, hass_ws_client):
    _store, clients, _users, _fake = await _screens(hass, hass_storage, hass_ws_client)
    for endpoint in ("hall", "ep-alice", "fake:gate-1", "nope"):
        resp = await _call(clients["tablet"], {"type": "portico/content/get", "endpoint": endpoint})
        assert not resp["success"] and resp["error"]["code"] == "unauthorized", endpoint


async def test_a_phone_naming_another_endpoint_is_refused(hass, hass_storage, hass_ws_client):
    _store, clients, _users, _fake = await _screens(hass, hass_storage, hass_ws_client)
    for endpoint in ("kitchen", "hall", "fake:gate-1"):
        resp = await _call(clients["phone"], {"type": "portico/content/get", "endpoint": endpoint})
        assert not resp["success"] and resp["error"]["code"] == "unauthorized", endpoint
    for body in ({"type": "portico/content/run", "action_id": "evening", "endpoint": "kitchen"},
                 {"type": "portico/content/snapshot", "camera_id": "lobby", "endpoint": "kitchen"}):
        resp = await _call(clients["phone"], body)
        assert not resp["success"] and resp["error"]["code"] == "unauthorized", body
    # its own, by id or by nothing: the document, with no `phone` and no flat fields
    for body in ({"type": "portico/content/get", "endpoint": "ep-alice"}, {"type": "portico/content/get"}):
        resp = await _call(clients["phone"], body)
        assert resp["success"], resp
        assert resp["result"]["endpoint"] == "ep-alice" and resp["result"]["kind"] == "phone"
        assert set(resp["result"]) == _GET_KEYS


async def test_an_admin_names_any_endpoint_and_profile_is_gone(hass, hass_storage, hass_ws_client):
    store, clients, _users, _fake = await _screens(hass, hass_storage, hass_ws_client)
    admin = clients["admin"]
    resp = await _call(admin, {"type": "portico/content/get", "endpoint": "kitchen"})
    assert resp["success"] and resp["result"]["endpoint"] == "kitchen" and "phone" not in resp["result"]
    resp = await _call(admin, {"type": "portico/content/get", "endpoint": "ep-alice"})
    assert resp["success"] and resp["result"]["endpoint"] == "ep-alice" and resp["result"]["kind"] == "phone"
    for old_key in ({"profile": "alice's phone"}, {"pages": True}):                       # contract 2's keys: refused
        resp = await _call(admin, {"type": "portico/content/get", "endpoint": "hall", **old_key})
        assert not resp["success"] and resp["error"]["code"] == "invalid_format", old_key
    for bad in ("nope", "fake:gate-1"):                                                           # unknown; a door has no content
        resp = await _call(admin, {"type": "portico/content/get", "endpoint": bad})
        assert not resp["success"] and resp["error"]["code"] == "unknown_endpoint", bad
    resp = await _call(admin, {"type": "portico/content/get"})                                    # names nobody
    assert resp["error"]["code"] == "unauthorized"


async def test_content_run_for_a_dashboard_uses_its_own_document_as_the_grant(hass, hass_storage, hass_ws_client):
    from homeassistant.core import Context
    from pytest_homeassistant_custom_component.common import async_mock_service

    _store, clients, users, _fake = await _screens(hass, hass_storage, hass_ws_client)
    calls = async_mock_service(hass, "script", "turn_on")
    tablet = clients["tablet"]
    resp = await _call(tablet, {"type": "portico/content/run", "action_id": "sprinkle", "endpoint": "kitchen"})   # its page
    assert resp["success"] and resp["result"] == {"ran": True}
    resp = await _call(tablet, {"type": "portico/content/run", "action_id": "evening"}, 2)    # the template's, unnamed
    assert resp["success"] and resp["result"] == {"ran": True}
    assert [c.data["entity_id"] for c in calls] == ["script.sprinkle", "script.evening"]
    assert all(isinstance(c.context, Context) and c.context.user_id == users["tablet"].id for c in calls)
    resp = await _call(tablet, {"type": "portico/content/run", "action_id": "hallonly", "endpoint": "kitchen"}, 3)
    assert not resp["success"] and resp["error"]["code"] == "unknown_action"       # another screen's page is no grant
    resp = await _call(tablet, {"type": "portico/content/run", "action_id": "hallonly", "endpoint": "hall"}, 4)
    assert not resp["success"] and resp["error"]["code"] == "unauthorized"          # nor may it name that screen
    assert len(calls) == 2
    # the rate limit is per endpoint and action: the same button again at once is refused
    resp = await _call(tablet, {"type": "portico/content/run", "action_id": "sprinkle", "endpoint": "kitchen"}, 5)
    assert not resp["success"] and resp["error"]["code"] == "rate_limited"


async def test_snapshot_of_a_caller_camera_is_granted(hass, hass_storage, hass_ws_client, monkeypatch):
    from homeassistant.components.camera import Image

    from ..content_resolve import short_id

    async def _image(hass_, entity_id, timeout=10, width=None, height=None):
        return Image(content_type="image/jpeg", content=b"FAKE-JPEG")

    monkeypatch.setattr("homeassistant.components.camera.async_get_image", _image)
    _store, clients, _users, _fake = await _screens(hass, hass_storage, hass_ws_client)
    tablet = clients["tablet"]
    got = await _call(tablet, {"type": "portico/content/get"})
    door_camera = got["result"]["callers"]["fake:gate-1"]["cameras"][0]["id"]
    assert door_camera == short_id("c", "camera.gate_door")
    msg_id = 1
    for camera in ("lobby", "pantry", door_camera, short_id("c", "camera.hall_cam")):   # a page, an own page, two callers'
        msg_id += 1
        resp = await _call(tablet, {"type": "portico/content/snapshot", "camera_id": camera, "endpoint": "kitchen"}, msg_id)
        assert resp["success"] and resp["result"]["content_type"] == "image/jpeg", (camera, resp)
    for camera in ("hallpage", short_id("c", "camera.lobby"), short_id("c", "camera.nowhere"), "c-000000000000"):
        msg_id += 1                                          # another screen's page; not a caller's camera; nothing
        resp = await _call(tablet, {"type": "portico/content/snapshot", "camera_id": camera}, msg_id)
        assert not resp["success"] and resp["error"]["code"] == "unknown_camera", camera


async def test_a_declared_screen_has_the_defined_read_only_reach(hass, hass_storage, hass_ws_client):
    """A screen reads and runs its own endpoint, and nothing else of the editor or the phones' lists."""
    _store, clients, _users, _fake = await _screens(hass, hass_storage, hass_ws_client)
    tablet = clients["tablet"]
    for msg_id, body in enumerate(({"type": "portico/content/document"},
                                   {"type": "portico/content/save", "document": {}, "base_doc_rev": "x"},
                                   {"type": "portico/content/preview", "document": {}, "endpoint": "kitchen"},
                                   {"type": "portico/dial", "door_id": "gate-1"}), start=1):
        resp = await _call(tablet, body, msg_id)
        assert not resp["success"] and resp["error"]["code"] == "unauthorized", body
    for msg_id, body in enumerate(({"type": "portico/doors"}, {"type": "portico/content/subscribe"},
                                   {"type": "portico/calls/subscribe"}), start=10):    # the dashboard class reads these
        resp = await _call(tablet, body, msg_id)
        assert resp["success"], body


async def _content_event(client):
    while True:
        message = await _recv(client)
        if message["type"] == "event" and message["event"]["type"] == "content_changed":
            return message["event"]


async def test_content_changed_carries_the_endpoint_and_its_rev(hass, hass_storage, hass_ws_client):
    store, clients, _users, _fake = await _screens(hass, hass_storage, hass_ws_client)
    for who in ("phone", "tablet", "admin", "hall"):
        assert (await _call(clients[who], {"type": "portico/content/subscribe"}))["success"]
    # a change of the phone's own endpoint: the phone hears it with the rev; an admin with `rev: null`; screens not at all
    await store.async_set(_users["phone"].id, {"cameras": [], "actions": []})
    assert await _content_event(clients["phone"]) == {
        "type": "content_changed", "endpoint": "ep-alice", "rev": store.resolve_document("ep-alice")["rev"]}
    assert await _content_event(clients["admin"]) == {"type": "content_changed", "endpoint": "ep-alice", "rev": None}
    # a change that reaches everyone (the home name): each hears its own endpoint and rev, an admin `null`, `null`
    await store.async_set_home_name("My Home")
    assert await _content_event(clients["tablet"]) == {
        "type": "content_changed", "endpoint": "kitchen", "rev": store.resolve_document("kitchen")["rev"]}
    assert await _content_event(clients["hall"]) == {
        "type": "content_changed", "endpoint": "hall", "rev": store.resolve_document("hall")["rev"]}
    assert await _content_event(clients["phone"]) == {
        "type": "content_changed", "endpoint": "ep-alice", "rev": store.resolve_document("ep-alice")["rev"]}
    assert await _content_event(clients["admin"]) == {"type": "content_changed", "endpoint": None, "rev": None}
    # the screen's own endpoint changing reaches that screen (the phone and the other screen are not told)
    store._announce("kitchen")
    assert await _content_event(clients["tablet"]) == {
        "type": "content_changed", "endpoint": "kitchen", "rev": store.resolve_document("kitchen")["rev"]}
    assert await _content_event(clients["admin"]) == {"type": "content_changed", "endpoint": "kitchen", "rev": None}
    await store.async_set_home_name("")        # a marker for the phone and the other screen: nothing from "kitchen" before it
    for who in ("phone", "hall"):
        event = await _content_event(clients[who])
        assert event["endpoint"] in ("ep-alice", "hall") and event["endpoint"] != "kitchen", (who, event)


async def test_rev_is_the_endpoint_rev_for_every_caller(hass, hass_storage, hass_ws_client):
    store, clients, _users, _fake = await _screens(hass, hass_storage, hass_ws_client)
    as_tablet = (await _call(clients["tablet"], {"type": "portico/content/get"}))["result"]["rev"]
    as_admin = (await _call(clients["admin"], {"type": "portico/content/get", "endpoint": "kitchen"}))["result"]["rev"]
    assert as_tablet == as_admin == store.resolve_document("kitchen")["rev"]
    phone = (await _call(clients["phone"], {"type": "portico/content/get"}))["result"]["rev"]
    by_admin = (await _call(clients["admin"], {"type": "portico/content/get", "endpoint": "ep-alice"}))["result"]["rev"]
    assert phone == by_admin == store.resolve_document("ep-alice")["rev"] != as_tablet
    # the rev follows the content, not a camera blinking
    hass.states.async_set("camera.lobby", "unavailable")
    again = (await _call(clients["tablet"], {"type": "portico/content/get"}))["result"]
    assert again["rev"] == as_tablet and again["pages"][0]["blocks"][0]["cameras"][0]["available"] is False
    store.template["pages"][0]["blocks"][0]["cameras"].append({"id": "pool", "label": "Pool", "entity_id": "camera.pool"})
    assert (await _call(clients["tablet"], {"type": "portico/content/get"}))["result"]["rev"] != as_tablet


# --------------------------------------------------------------------------
# M6 E3: contract 3 (the two subscriptions, the narrowing, the door on the webrtc and media commands, app_too_old)
# --------------------------------------------------------------------------


async def test_content_subscribe_never_carries_a_call_event_and_calls_subscribe_never_a_content_one(
        hass, hass_storage, hass_ws_client):
    """WS-04: the two kinds never share a message shape or a stream."""
    store, clients, users, fake = await _screens(hass, hass_storage, hass_ws_client)
    tablet = clients["tablet"]
    assert (await _call(tablet, {"type": "portico/content/subscribe"}))["success"]
    content_id = _NEXT_ID[id(tablet)]
    assert (await _call(tablet, {"type": "portico/calls/subscribe"}))["success"]
    calls_id = _NEXT_ID[id(tablet)]
    for _ in range(2):                                  # a snapshot per door (two here), on the calls subscription only
        snapshot = await _recv(tablet)
        assert snapshot["id"] == calls_id and snapshot["event"]["snapshot"] is True
    fake.ring("gate-1", "c1")
    await store.async_set_home_name("My Home")
    seen = {content_id: [], calls_id: []}
    for _ in range(2):
        message = await _recv(tablet)
        seen[message["id"]].append(message["event"])
    assert [e["type"] for e in seen[content_id]] == ["content_changed"]
    assert [e["type"] for e in seen[calls_id]] == ["call_state_changed"]
    assert seen[calls_id][0]["door_id"] == "gate-1" and seen[calls_id][0]["prev_state"] == "idle"
    assert set(seen[content_id][0]) == {"type", "endpoint", "rev"}


async def test_content_subscribe_scopes_each_caller(hass, hass_storage, hass_ws_client):
    store, clients, users, _fake = await _screens(hass, hass_storage, hass_ws_client)
    # a plain user (no class) is refused by the table; a phone naming another endpoint, and a screen naming one it
    # is not declared for, by the handler
    assert (await _call(clients["plain"], {"type": "portico/content/subscribe"}))["error"]["code"] == "unauthorized"
    for who, endpoint in (("phone", "kitchen"), ("tablet", "hall"), ("tablet", "ep-alice")):
        resp = await _call(clients[who], {"type": "portico/content/subscribe", "endpoint": endpoint})
        assert not resp["success"] and resp["error"]["code"] == "unauthorized", (who, endpoint)
    # an admin naming an endpoint hears that endpoint only, with its rev; an unknown one is refused
    admin = clients["admin"]
    assert (await _call(admin, {"type": "portico/content/subscribe", "endpoint": "kitchen"}))["success"]
    assert (await _call(admin, {"type": "portico/content/subscribe", "endpoint": "nope"}))["error"]["code"] == \
        "unknown_endpoint"
    store._announce("hall")
    store._announce("kitchen")
    event = await _content_event(admin)
    assert event == {"type": "content_changed", "endpoint": "kitchen", "rev": store.resolve_document("kitchen")["rev"]}


async def test_the_call_commands_are_open_to_a_user_with_no_role(hass, hass_storage, hass_ws_client):
    """The owner's decision of 2026-10-21 end to end: a logged-in user who is no administrator, no enrolled phone and
    no declared screen may send the call commands (the table lets them through; whatever the handler then says is not
    `unauthorized`), and still may not read or run content."""
    _store, clients, _users, fake = await _screens(hass, hass_storage, hass_ws_client)
    fake.ring("gate-1", "c1")
    # the content commands stay closed to the same user (asked first: a call subscription would queue events)
    for body in ({"type": "portico/content/get"}, {"type": "portico/content/run", "action_id": "a1"},
                 {"type": "portico/content/subscribe"}, {"type": "portico/dial", "door_id": "gate-1"}):
        resp = await _call(clients["plain"], body)
        assert not resp["success"] and resp["error"]["code"] == "unauthorized", body
    for body in ({"type": "portico/doors"}, {"type": "portico/calls/subscribe"},
                 {"type": "portico/open_relay", "door_id": "gate-1"}, {"type": "portico/hangup", "call_id": "c1"},
                 {"type": "portico/answer", "door_id": "gate-1", "call_id": "c1", "client_kind": "app"},
                 {"type": "portico/keepalive", "call_id": "c1"},
                 {"type": "portico/webrtc_offer", "door_id": "gate-1", "call_id": "c1", "sdp": "x"},
                 {"type": "portico/webrtc_answer", "call_id": "c1", "sdp": "x"},
                 {"type": "portico/webrtc_ice", "call_id": "c1", "candidate": "x"}):
        resp = await _call(clients["plain"], body)
        assert not (resp.get("success") is False and resp["error"]["code"] == "unauthorized"), body
    assert fake.calls != []                                     # the relay command reached the provider
    # the other classes are no different
    for who in ("phone", "tablet"):
        assert (await _call(clients[who], {"type": "portico/open_relay", "door_id": "gate-1"}))["success"], who


async def test_a_webrtc_offer_is_for_the_named_door_and_a_call_that_door_does_not_own_is_unknown(
        hass, hass_ws_client):
    await _setup_signaling(hass)
    fakes = {}
    for domain in ("fake_a", "fake_b"):
        fakes[domain] = FakeProvider(domain)
        fakes[domain].add_door(f"{domain}-door")
        _registry(hass).register(fakes[domain])
    fakes["fake_b"].ring("fake_b-door", "cb")
    client = await hass_ws_client(hass)
    # the call belongs to door b: naming door a refuses it, and door a's provider is not touched
    await client.send_json({"id": 1, "type": "portico/webrtc_offer", "call_id": "cb", "door_id": "fake_a-door",
                            "sdp": "o"})
    assert (await client.receive_json())["error"] == {"code": "unknown_call", "message": "cb"}
    assert fakes["fake_a"].calls_to("webrtc_offer") == [] and fakes["fake_b"].calls_to("webrtc_offer") == []
    await client.send_json({"id": 2, "type": "portico/webrtc_offer", "call_id": "cb", "door_id": "fake_b-door",
                            "sdp": "o"})
    assert (await client.receive_json())["result"] == {"sdp": "fake-answer-sdp"}
    assert fakes["fake_b"].calls_to("webrtc_offer") == [(("cb", "o"), {})]
    # a v1 offer without a door is a schema error
    await client.send_json({"id": 3, "type": "portico/webrtc_offer", "call_id": "cb", "sdp": "o"})
    assert (await client.receive_json())["error"]["code"] == "invalid_format"


async def test_the_contract_2_commands_and_keys_are_gone(hass, hass_ws_client):
    """No aliases (O2): the v1 names answer `unknown_command`, and the v1 request keys are schema errors."""
    await _setup_signaling(hass)
    fake = FakeProvider("fake")
    fake.add_door("gate-1")
    _registry(hass).register(fake)
    client = await hass_ws_client(hass)
    for n, command in enumerate(("devices", "subscribe", "content/list", "doors/secrets", "house/call"), start=1):
        await client.send_json({"id": n, "type": f"portico/{command}"})
        assert (await client.receive_json())["error"]["code"] == "unknown_command", command
    for n, body in enumerate(({"type": "portico/open_relay", "device_id": "gate-1"},
                              {"type": "portico/dial", "device_id": "gate-1"},
                              {"type": "portico/answer", "device_id": "gate-1", "call_id": "c", "client_kind": "app"},
                              {"type": "portico/media_open", "device_id": "gate-1", "call_id": "c"}), start=20):
        await client.send_json({"id": n, **body})
        assert (await client.receive_json())["error"]["code"] == "invalid_format", body
    assert fake.calls == []


async def test_a_call_that_ended_under_a_subscriber_is_one_event_with_the_call_id(hass, hass_ws_client):
    await _setup_signaling(hass)
    fake = FakeProvider("fake")
    fake.add_door("gate-1")
    _registry(hass).register(fake)
    client = await hass_ws_client(hass)
    await _subscribe(client)
    await _events(client, 1)
    fake.ring("gate-1", "c1")
    fake.state("c1", "ended")
    fake.ring("gate-1", "c2")
    events = await _events(client, 3)
    assert [(e["call_id"], e["state"], e["prev_state"]) for e in events] == [
        ("c1", "ringing", "idle"), ("c1", "ended", "ringing"), ("c2", "ringing", "idle")]


# --- push/register: app_too_old -----------------------------------------------------------------------------------

def test_app_version_ok_parses_the_versions_the_app_sends():
    from ..signaling import MIN_APP_VERSION, _app_version_ok

    assert MIN_APP_VERSION == (0, 5, 0)
    for good in ("0.5.0", "0.5.0-alpha", "0.5.1-alpha", "0.6.0", "1.0.0", "0.10.0-beta", "v0.5.0", " 0.5.0 "):
        assert _app_version_ok(good), good
    for bad in ("", "0.4.3", "0.4.3-alpha", "0.4.99", "0.0.1", "1", "0.5", "garbage", "alpha-0.5.0", "-0.5.0"):
        assert not _app_version_ok(bad), bad


def test_a_ring_only_door_is_not_answerable_and_every_other_is():
    """`portico/doors` -> `answerable`: false only for no audio, no relay, no direct path (the ha_event door)."""
    from custom_components.portico.door.drivers.ha_event import CAPABILITIES
    from custom_components.portico.signaling import _answerable

    assert _answerable(CAPABILITIES) is False
    assert _answerable({}) is True and _answerable(None) is True
    assert _answerable({**CAPABILITIES, "HasRelay": True}) is True
    assert _answerable({**CAPABILITIES, "DirectCapable": True}) is True
    assert _answerable({**CAPABILITIES, "InboundAudio": 1}) is True
