"""`portico/house/media_open` over a real HA websocket (M7 3b-S): the wire a phone or the kitchen page will speak, against a
FAKE hub. OFF and unsupported first (the shipped state), then a whole call: place, answer, both ends declare frame legs,
audio both ways, active on the callee's first frame, hang-up. No real audio is involved (offline tests cannot judge it)."""
from __future__ import annotations

import asyncio

import pytest

from .fake_house_provider import FakeHouseProvider
from .test_endpoints import _providers
from .test_house_commands import _advance, _events, _result, _send, _subscribe, _world
from .test_push_registry import _custom_integrations  # noqa: F401 - autouse fixture by name

pytestmark = pytest.mark.asyncio

MEDIA = {"type": "portico/house/media_open", "call_id": "hso-1"}


async def _recv_until(client, predicate, limit=20):
    for _ in range(limit):
        msg = _stash_pop(client) or await asyncio.wait_for(client.receive_json(), timeout=5)
        if predicate(msg):
            return msg
    raise AssertionError("no matching frame")


def _stash_pop(client):
    stash = client.__dict__.setdefault("stash", [])
    return stash.pop(0) if stash else None


async def _kitchen_calls_dina(hass, w):
    roster = (await _send(w.kitchen_ws, 40, {"type": "portico/content/get"}))["result"]["roster"]
    (dina,) = [e for e in roster if e["id"] == "dina"]
    run = await _send(w.kitchen_ws, 41, {"type": "portico/content/run", "action_id": dina["action_id"]})
    assert run["success"], run
    call_id = run["result"]["call_id"]
    await _events(w.kitchen_ws, 1)
    await _events(w.dina_ws, 1)
    return call_id


async def test_off_media_open_is_not_available_for_every_caller_in_the_row_and_unauthorized_outside_it(
        hass, hass_storage, hass_ws_client):
    w = await _world(hass, hass_storage, hass_ws_client, house_on=False)
    for who in ("alice_ws", "kitchen_ws", "admin_ws"):
        resp = await _send(getattr(w, who), 1, MEDIA)
        assert resp["success"] is False and resp["error"]["code"] == "not_available", (who, resp)
    assert (await _send(w.plain_ws, 1, MEDIA))["error"]["code"] == "unauthorized"


async def test_on_but_no_bridge_provider_media_open_is_not_supported(hass, hass_storage, hass_ws_client):
    w = await _world(hass, hass_storage, hass_ws_client, house_on=True)
    for client in (w.dina_ws, w.kitchen_ws):
        await _subscribe(client)
    call_id = await _kitchen_calls_dina(hass, w)
    assert (await _send(w.dina_ws, 50, {"type": "portico/house/answer", "call_id": call_id, "client_kind": "app"}))["success"]
    resp = await _send(w.dina_ws, 51, {**MEDIA, "call_id": call_id})
    assert resp["success"] is False and resp["error"]["code"] == "not_supported"
    resp = await _send(w.alice_ws, 52, {**MEDIA, "call_id": call_id})          # not in the call: learns nothing
    assert resp["error"]["code"] == "unknown_call"


async def test_a_whole_call_over_the_wire_with_a_fake_hub(hass, hass_storage, hass_ws_client):
    w = await _world(hass, hass_storage, hass_ws_client, house_on=True)
    hub = FakeHouseProvider()
    _providers(hass).register(hub)
    for client in (w.dina_ws, w.kitchen_ws):
        await _subscribe(client)
    call_id = await _kitchen_calls_dina(hass, w)
    answered = await _send(w.dina_ws, 50, {"type": "portico/house/answer", "call_id": call_id, "client_kind": "app"})
    assert answered["result"] == {"call_id": call_id, "media": None}
    (kitchen_answered,) = await _events(w.kitchen_ws, 1)
    assert kitchen_answered["state"] == "answered"
    await _events(w.dina_ws, 1)

    # both ends declare; the kitchen asks first and its reply waits for the callee's
    await w.kitchen_ws.send_json({"id": 60, **MEDIA, "call_id": call_id, "codec": "l16"})
    await asyncio.sleep(0.05)
    assert hub.opened == []
    await w.dina_ws.send_json({"id": 61, **MEDIA, "call_id": call_id, "codec": "l16"})
    kitchen_reply = await _result(w.kitchen_ws, 60)
    dina_reply = await _result(w.dina_ws, 61)
    expected = {"kind": "frames", "codec": "l16", "frame_bytes": 1280, "frame_ms": 40}
    assert kitchen_reply["success"] and kitchen_reply["result"] == expected and dina_reply["result"] == expected
    ((opened_id, legs),) = hub.opened
    assert opened_id == call_id
    assert [(leg["side"], leg["owner"], leg["kind"]) for leg in legs] == [("from", "kitchen", "frames"), ("to", "dina", "frames")]

    # downlink: an event on the command id; uplink: the shared media_frame, from the owner's connection only
    kitchen_leg, dina_leg = sorted(hub.legs)
    hub.downlink(dina_leg, "DOWN-DINA")
    event = await _recv_until(w.dina_ws, lambda m: m["type"] == "event" and m["id"] == 61)
    assert event["event"] == {"t": "f", "d": "DOWN-DINA"}
    await w.dina_ws.send_json({"id": 62, "type": "portico/media_frame", "call_id": call_id, "d": "UP-DINA"})
    await w.alice_ws.send_json({"id": 63, "type": "portico/media_frame", "call_id": call_id, "d": "NOT-MINE"})
    await w.kitchen_ws.send_json({"id": 64, "type": "portico/media_frame", "call_id": call_id, "d": "UP-KITCHEN"})
    await asyncio.sleep(0.05)
    assert hub.uplink == [(dina_leg, "UP-DINA"), (kitchen_leg, "UP-KITCHEN")]

    # the callee's first frame makes the call active for both ends
    hub.leg_state(call_id, "to", "first_frame")
    active = await _recv_until(w.kitchen_ws, lambda m: m["type"] == "event" and m["event"].get("type") == "house_call")
    assert active["event"]["state"] == "active"
    await _advance(hass, w, 30)                                   # no 10 s deadline any more
    assert hub.closed == []

    # the kitchen hangs up: both ends hear `end` on their media command, the hub's bridge is closed once
    assert (await _send(w.kitchen_ws, 70, {"type": "portico/house/hangup", "call_id": call_id}))["success"]
    end = await _recv_until(w.dina_ws, lambda m: m["type"] == "event" and m["id"] == 61 and m["event"].get("t") == "end")
    assert end["event"] == {"t": "end", "reason": "hangup"}
    await hass.async_block_till_done()
    assert hub.closed == [call_id]


async def test_the_phones_goodbye_on_media_close_hangs_a_house_call_up(hass, hass_storage, hass_ws_client):
    w = await _world(hass, hass_storage, hass_ws_client, house_on=True)
    hub = FakeHouseProvider()
    _providers(hass).register(hub)
    for client in (w.dina_ws, w.kitchen_ws):
        await _subscribe(client)
    call_id = await _kitchen_calls_dina(hass, w)
    await _send(w.dina_ws, 50, {"type": "portico/house/answer", "call_id": call_id, "client_kind": "app"})
    await _events(w.kitchen_ws, 1)
    await _events(w.dina_ws, 1)
    await w.kitchen_ws.send_json({"id": 60, **MEDIA, "call_id": call_id})
    await w.dina_ws.send_json({"id": 61, **MEDIA, "call_id": call_id})
    await _result(w.kitchen_ws, 60)
    await _result(w.dina_ws, 61)
    # a phone that is not in the call cannot hang it up with a media_close
    resp = await _send(w.alice_ws, 80, {"type": "portico/media_close", "call_id": call_id})
    assert resp["success"] and resp["result"] == {"ok": True}
    await hass.async_block_till_done()
    assert hub.closed == []
    resp = await _send(w.dina_ws, 81, {"type": "portico/media_close", "call_id": call_id})
    assert resp["success"] and resp["result"] == {"ok": True}
    await hass.async_block_till_done()
    assert hub.closed == [call_id]
    ended = await _recv_until(w.kitchen_ws, lambda m: m["type"] == "event" and m["event"].get("state") == "ended")
    assert ended["event"]["reason"] == "hangup"


async def test_a_socket_that_closes_while_talking_ends_the_call_at_the_other_end(hass, hass_storage, hass_ws_client):
    w = await _world(hass, hass_storage, hass_ws_client, house_on=True)
    hub = FakeHouseProvider()
    _providers(hass).register(hub)
    for client in (w.dina_ws, w.kitchen_ws):
        await _subscribe(client)
    call_id = await _kitchen_calls_dina(hass, w)
    await _send(w.dina_ws, 50, {"type": "portico/house/answer", "call_id": call_id, "client_kind": "app"})
    await _events(w.kitchen_ws, 1)
    await _events(w.dina_ws, 1)
    await w.kitchen_ws.send_json({"id": 60, **MEDIA, "call_id": call_id})
    await w.dina_ws.send_json({"id": 61, **MEDIA, "call_id": call_id})
    await _result(w.kitchen_ws, 60)
    await _result(w.dina_ws, 61)
    await w.dina_ws.close()
    for _ in range(100):
        await asyncio.sleep(0.02)
        if hub.closed:
            break
    assert hub.closed == [call_id]
    ended = await _recv_until(w.kitchen_ws, lambda m: m["type"] == "event" and m["event"].get("state") == "ended")
    assert ended["event"]["reason"] == "error"


async def test_a_door_frame_still_takes_the_door_path_not_the_house_one(hass, hass_storage, hass_ws_client):
    """`portico/media_frame` for a call id that is not `hso-` is the away-media code, exactly as before: an unknown door call
    answers `unknown_session` (to a phone), and nothing reaches the house bridge."""
    w = await _world(hass, hass_storage, hass_ws_client, house_on=True)
    hub = FakeHouseProvider()
    _providers(hass).register(hub)
    await w.alice_ws.send_json({"id": 90, "type": "portico/media_frame", "call_id": "gso-1", "d": "x"})
    msg = await asyncio.wait_for(w.alice_ws.receive_json(), timeout=5)
    assert msg["id"] == 90 and msg["success"] is False and msg["error"]["code"] == "unknown_session"
    assert hub.uplink == []
