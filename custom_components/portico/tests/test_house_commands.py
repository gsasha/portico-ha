"""The house-call commands over a real websocket (M7 3a): `portico/house/answer | decline | hangup | presence`, `house_call`
events on `portico/calls/subscribe`, placing through `portico/content/run` (CB-1), the Everyone key in a screen's
`content/get`, and the door cap in `portico/answer`. With house calls OFF (the default) every one of them is a no-op.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from ..content import get_content_store
from ..house import async_setup_house
from .house_env import phone_entry
from .test_endpoints import _provide
from .test_push_registry import _custom_integrations, _phone_user  # noqa: F401 - fixtures by name
from .test_signaling import _setup_signaling

pytestmark = pytest.mark.asyncio

COMMANDS = {
    "portico/house/answer": {"call_id": "hso-1", "client_kind": "app"},
    "portico/house/decline": {"call_id": "hso-1"},
    "portico/house/hangup": {"call_id": "hso-1"},
    "portico/house/presence": {"dnd": True},
}


class World:
    pass


async def _recv(client):
    return await asyncio.wait_for(client.receive_json(), timeout=5)


def _stash(client) -> list:
    return client.__dict__.setdefault("stash", [])


async def _result(client, msg_id):
    """The `result` frame for `msg_id`; events that arrive first are kept for `_events`."""
    while True:
        msg = await _recv(client)
        if msg["type"] == "result" and msg["id"] == msg_id:
            return msg
        _stash(client).append(msg)


async def _send(client, msg_id, body):
    await client.send_json({"id": msg_id, **body})
    return await _result(client, msg_id)


async def _world(hass, hass_storage, hass_ws_client, *, house_on: bool, doors: int = 1) -> World:
    w = World()
    w.alice, alice_token = await _phone_user(hass, "Alice")
    w.dina, dina_token = await _phone_user(hass, "Dina")
    w.kitchen, kitchen_token = await _phone_user(hass, "Kitchen tablet")
    w.plain, plain_token = await _phone_user(hass, "Plain")
    w.admin = await hass.auth.async_create_user(name="Admin", group_ids=["system-admin"])
    phone_entry(hass, w.alice.id, "alice", "Alice", receive=True)
    phone_entry(hass, w.dina.id, "dina", "Dina", receive=True)
    hass_storage["portico.content"] = {
        "version": 1, "minor_version": 1, "key": "portico.content",
        "data": {"schema_version": 3, "template": {"pages": [], "rules": [], "roster": ["*"]}, "overrides": {},
                 "dashboards": {"kitchen": {"name": "Kitchen", "ha_user_id": w.kitchen.id, "room": "Kitchen",
                                            "cameras": []}}, "endpoints": {}}}
    w.doors = doors
    w.door = _provide(hass, "fake", *[f"gate-{n}" for n in range(1, doors + 1)])
    await _setup_signaling(hass)
    await get_content_store(hass).async_ensure_loaded()
    w.house = async_setup_house(hass, enabled=True) if house_on else None
    w.alice_ws = await hass_ws_client(hass, access_token=alice_token)
    w.dina_ws = await hass_ws_client(hass, access_token=dina_token)
    w.kitchen_ws = await hass_ws_client(hass, access_token=kitchen_token)
    w.plain_ws = await hass_ws_client(hass, access_token=plain_token)
    w.admin_ws = await hass_ws_client(hass)
    w.clock = dt_util.utcnow()
    w.elapsed = 0.0
    return w


async def _subscribe(client, msg_id=1, doors=1):
    """`portico/calls/subscribe`: the result and one snapshot per door."""
    ack = await _send(client, msg_id, {"type": "portico/calls/subscribe"})
    assert ack["success"], ack
    for snapshot in await _events(client, doors):
        assert snapshot["type"] == "call_state_changed" and snapshot["snapshot"] is True


async def _events(client, n, *, roster=False):
    """The next `n` events. `house_roster_changed` (presence moved: re-read the roster) is skipped unless `roster`."""
    out = []
    while len(out) < n:
        msg = _stash(client).pop(0) if _stash(client) else await _recv(client)
        assert msg["type"] == "event", msg
        if msg["event"]["type"] == "house_roster_changed" and not roster:
            continue
        out.append(msg["event"])
    return out


async def _nothing_more(client):
    assert not [m for m in _stash(client) if m["event"]["type"] != "house_roster_changed"]
    try:
        while True:
            msg = await asyncio.wait_for(client.receive_json(), timeout=0.3)
            assert msg["event"]["type"] == "house_roster_changed", msg
    except TimeoutError:
        return


async def _advance(hass, w, seconds):
    w.elapsed += seconds
    async_fire_time_changed(hass, w.clock + timedelta(seconds=w.elapsed))
    await hass.async_block_till_done()


async def _everyone_action(client, msg_id=50):
    got = await _send(client, msg_id, {"type": "portico/content/get"})
    assert got["success"], got
    (entry,) = [e for e in got["result"]["roster"] if e["kind"] == "group"]
    return entry


# ---- OFF: the default; every command and hook is a no-op ----------------------------------------------------------

@pytest.mark.parametrize("command", sorted(COMMANDS))
async def test_every_house_command_answers_not_available_while_house_calls_are_off(
        hass, hass_storage, hass_ws_client, command):
    w = await _world(hass, hass_storage, hass_ws_client, house_on=False)
    for who in ("alice_ws", "kitchen_ws", "admin_ws"):
        resp = await _send(getattr(w, who), 1, {"type": command, **COMMANDS[command]})
        assert resp["success"] is False and resp["error"]["code"] == "not_available", (who, resp)
    resp = await _send(w.plain_ws, 1, {"type": command, **COMMANDS[command]})
    assert resp["error"]["code"] == "unauthorized"                  # a login with no Portico role: refused first


async def test_off_the_calls_subscription_and_the_kitchens_roster_are_what_they_were(hass, hass_storage, hass_ws_client):
    w = await _world(hass, hass_storage, hass_ws_client, house_on=False)
    await _subscribe(w.alice_ws)
    await _subscribe(w.kitchen_ws)
    await _nothing_more(w.alice_ws)
    got = await _send(w.kitchen_ws, 9, {"type": "portico/content/get"})
    roster = got["result"]["roster"]
    assert [e["kind"] for e in roster] == ["phone", "phone", "door"]   # no Everyone key
    assert all(e["available"] is False and e["reason"] == "not_available" for e in roster)
    assert all("count" not in e for e in roster)
    # a roster run still answers what it answered before
    run = await _send(w.kitchen_ws, 10, {"type": "portico/content/run", "action_id": roster[0]["action_id"]})
    assert run["success"] is False and run["error"]["code"] == "not_available"


async def test_off_a_third_door_answer_by_one_user_still_reaches_the_provider(hass, hass_storage, hass_ws_client):
    """Golden for the cap: with house calls off the answer path has no ledger, as before this PR."""
    w = await _world(hass, hass_storage, hass_ws_client, house_on=False, doors=3)
    for n in range(3):
        w.door.ring(f"gate-{n + 1}", f"g{n}")
        resp = await _send(w.alice_ws, 10 + n, {"type": "portico/answer", "door_id": f"gate-{n + 1}", "call_id": f"g{n}",
                                                 "client_kind": "app"})
        assert resp["success"], resp
    assert len(w.door.calls_to("answer")) == 3


# ---- ON ---------------------------------------------------------------------------------------------------------

async def test_the_kitchens_roster_leads_with_everyone_and_counts_the_phones_that_would_ring(
        hass, hass_storage, hass_ws_client):
    w = await _world(hass, hass_storage, hass_ws_client, house_on=True)
    entry = await _everyone_action(w.kitchen_ws)
    assert (entry["id"], entry["kind"], entry["name"], entry["available"], entry["reason"], entry["count"]) == (
        "everyone", "group", "Everyone", False, "no_receivers", 0)               # nobody is subscribed yet
    await _subscribe(w.alice_ws)
    await _subscribe(w.dina_ws)
    entry = await _everyone_action(w.kitchen_ws, 51)
    assert (entry["available"], entry["reason"], entry["count"]) == (True, None, 2)
    roster = (await _send(w.kitchen_ws, 52, {"type": "portico/content/get"}))["result"]["roster"]
    assert [e["kind"] for e in roster] == ["group", "phone", "phone", "door"]   # Everyone is drawn first
    assert all(e["available"] and e["reason"] is None for e in roster[1:3])
    assert roster[3]["available"] is False                                       # the gate is 3f's, not this PR's
    # a phone's roster has no Everyone key and shows its peer
    phone_roster = (await _send(w.alice_ws, 53, {"type": "portico/content/get"}))["result"]["roster"]
    assert [e["kind"] for e in phone_roster] == ["phone", "dashboard", "door"]


async def test_everyone_from_the_kitchen_rings_the_phones_and_the_first_answer_wins(hass, hass_storage, hass_ws_client):
    w = await _world(hass, hass_storage, hass_ws_client, house_on=True)
    for client in (w.alice_ws, w.dina_ws, w.kitchen_ws):
        await _subscribe(client)
    entry = await _everyone_action(w.kitchen_ws)
    run = await _send(w.kitchen_ws, 60, {"type": "portico/content/run", "action_id": entry["action_id"]})
    assert run["success"], run
    call_id = run["result"]["call_id"]
    assert run["result"]["ran"] is True and run["result"]["media"] is None and call_id.startswith("hso-")
    (kitchen_ring,) = await _events(w.kitchen_ws, 1)
    assert kitchen_ring == {"type": "house_call", "call_id": call_id, "state": "ringing", "from": "kitchen", "to": "everyone"}
    (alice_ring,) = await _events(w.alice_ws, 1)
    (dina_ring,) = await _events(w.dina_ws, 1)
    assert alice_ring["to"] == "alice" and dina_ring["to"] == "dina" and alice_ring["from"] == "kitchen"

    # both answer at once: exactly one wins, the other is told it was taken
    await w.alice_ws.send_json({"id": 61, "type": "portico/house/answer", "call_id": call_id, "client_kind": "app"})
    await w.dina_ws.send_json({"id": 61, "type": "portico/house/answer", "call_id": call_id, "client_kind": "app"})
    replies = {"alice": await _result(w.alice_ws, 61), "dina": await _result(w.dina_ws, 61)}
    winners = [n for n, r in replies.items() if r["success"]]
    losers = [n for n, r in replies.items() if not r["success"]]
    assert len(winners) == 1 and len(losers) == 1, replies
    assert replies[losers[0]]["error"]["code"] == "already_answered"
    assert replies[winners[0]]["result"] == {"call_id": call_id, "media": None}


async def test_a_one_to_one_call_from_a_phones_call_button_declines_and_hangs_up_over_the_wire(
        hass, hass_storage, hass_ws_client):
    w = await _world(hass, hass_storage, hass_ws_client, house_on=True)
    for client in (w.alice_ws, w.dina_ws, w.kitchen_ws):
        await _subscribe(client)
    roster = (await _send(w.alice_ws, 70, {"type": "portico/content/get"}))["result"]["roster"]
    (dina,) = [e for e in roster if e["id"] == "dina"]
    run = await _send(w.alice_ws, 71, {"type": "portico/content/run", "action_id": dina["action_id"]})
    assert run["success"], run
    call_id = run["result"]["call_id"]
    await _events(w.alice_ws, 1)
    await _events(w.dina_ws, 1)
    # a stranger and a non-participant learn nothing
    assert (await _send(w.admin_ws, 1, {"type": "portico/house/hangup", "call_id": call_id}))["error"]["code"] == "unknown_call"
    assert (await _send(w.alice_ws, 72, {"type": "portico/house/decline", "call_id": call_id}))["error"]["code"] == "unknown_call"
    ok = await _send(w.dina_ws, 73, {"type": "portico/house/decline", "call_id": call_id})
    assert ok["success"] and ok["result"] == {}
    (ended,) = await _events(w.alice_ws, 1)
    assert (ended["state"], ended["reason"], ended["to"]) == ("ended", "declined", "dina")
    # the next one (to the kitchen: the same button within 2 s is rate limited): the caller cancels
    (kitchen,) = [e for e in roster if e["id"] == "kitchen"]
    run = await _send(w.alice_ws, 74, {"type": "portico/content/run", "action_id": kitchen["action_id"]})
    assert run["success"], run
    await _events(w.alice_ws, 1)
    await _events(w.kitchen_ws, 1)
    hung = await _send(w.alice_ws, 75, {"type": "portico/house/hangup", "call_id": run["result"]["call_id"]})
    assert hung["success"]
    (ended,) = await _events(w.kitchen_ws, 1)
    assert ended["reason"] == "cancelled"


async def test_a_call_that_is_answered_but_never_gets_audio_ends_in_error_at_10_seconds(hass, hass_storage, hass_ws_client):
    w = await _world(hass, hass_storage, hass_ws_client, house_on=True)
    for client in (w.dina_ws, w.kitchen_ws):
        await _subscribe(client)
    roster = (await _send(w.kitchen_ws, 80, {"type": "portico/content/get"}))["result"]["roster"]
    (dina,) = [e for e in roster if e["id"] == "dina"]
    run = await _send(w.kitchen_ws, 81, {"type": "portico/content/run", "action_id": dina["action_id"]})
    call_id = run["result"]["call_id"]
    await _events(w.kitchen_ws, 1)
    await _events(w.dina_ws, 1)
    answered = await _send(w.dina_ws, 82, {"type": "portico/house/answer", "call_id": call_id, "client_kind": "browser"})
    assert answered["success"]
    assert (await _events(w.kitchen_ws, 1))[0]["state"] == "answered"
    await _events(w.dina_ws, 1)
    await _advance(hass, w, 11)
    assert (await _events(w.kitchen_ws, 1))[0]["reason"] == "error"
    assert (await _events(w.dina_ws, 1))[0]["reason"] == "error"


async def test_presence_commands_need_a_subscription_and_dnd_takes_a_phone_out_of_everyone(
        hass, hass_storage, hass_ws_client):
    w = await _world(hass, hass_storage, hass_ws_client, house_on=True)
    early = await _send(w.alice_ws, 1, {"type": "portico/house/presence", "dnd": True})
    assert early["success"] is False and early["error"]["code"] == "not_subscribed"
    await _subscribe(w.alice_ws, 2)
    await _subscribe(w.kitchen_ws)
    ok = await _send(w.alice_ws, 3, {"type": "portico/house/presence", "dnd": True})
    assert ok["success"] and ok["result"] == {}
    entry = await _everyone_action(w.kitchen_ws)
    assert (entry["available"], entry["reason"], entry["count"]) == (False, "no_receivers", 0)
    # the kitchen's roster says why for the phone itself
    roster = (await _send(w.kitchen_ws, 52, {"type": "portico/content/get"}))["result"]["roster"]
    alice = next(e for e in roster if e["id"] == "alice")
    assert (alice["available"], alice["reason"]) == (False, "dnd")
    run = await _send(w.kitchen_ws, 53, {"type": "portico/content/run", "action_id": entry["action_id"]})
    assert run["success"] is False and run["error"]["code"] == "not_callable"      # an empty set is refused
    await _send(w.alice_ws, 4, {"type": "portico/house/presence", "dnd": False})
    entry = await _everyone_action(w.kitchen_ws, 54)
    assert entry["count"] == 1 and entry["available"] is True


async def test_an_administrator_cannot_ring_the_house_as_the_kitchen(hass, hass_storage, hass_ws_client):
    w = await _world(hass, hass_storage, hass_ws_client, house_on=True)
    await _subscribe(w.alice_ws)
    entry = await _everyone_action(w.kitchen_ws)
    run = await _send(w.admin_ws, 1, {"type": "portico/content/run", "action_id": entry["action_id"],
                                       "endpoint": "kitchen"})
    assert run["success"] is False and run["error"]["code"] == "place_forbidden"
    await _nothing_more(w.alice_ws)                                                   # nobody was rung


async def test_a_phone_cannot_reach_everyone_by_any_id(hass, hass_storage, hass_ws_client):
    w = await _world(hass, hass_storage, hass_ws_client, house_on=True)
    entry = await _everyone_action(w.kitchen_ws)
    run = await _send(w.alice_ws, 1, {"type": "portico/content/run", "action_id": entry["action_id"]})
    assert run["success"] is False and run["error"]["code"] == "unknown_action"      # not in its grant at all
    assert (await _send(w.plain_ws, 2, {"type": "portico/content/run", "action_id": entry["action_id"]}))[
        "error"]["code"] == "unauthorized"


async def test_the_calls_subscription_closing_takes_the_phone_offline(hass, hass_storage, hass_ws_client):
    w = await _world(hass, hass_storage, hass_ws_client, house_on=True)
    await _subscribe(w.alice_ws)
    assert w.house.presence("alice")["state"] == "online"
    await w.alice_ws.close()
    await hass.async_block_till_done()
    assert w.house.presence("alice")["state"] == "offline"


async def test_a_reconnecting_phone_gets_its_live_call_as_a_snapshot_after_the_door_snapshots(
        hass, hass_storage, hass_ws_client):
    w = await _world(hass, hass_storage, hass_ws_client, house_on=True)
    for client in (w.dina_ws, w.kitchen_ws):
        await _subscribe(client)
    roster = (await _send(w.kitchen_ws, 90, {"type": "portico/content/get"}))["result"]["roster"]
    (dina,) = [e for e in roster if e["id"] == "dina"]
    run = await _send(w.kitchen_ws, 91, {"type": "portico/content/run", "action_id": dina["action_id"]})
    call_id = run["result"]["call_id"]
    await _events(w.kitchen_ws, 1)
    again = await _send(w.kitchen_ws, 92, {"type": "portico/calls/subscribe"})
    assert again["success"]
    door_snapshot, house_snapshot = await _events(w.kitchen_ws, 2)
    assert door_snapshot["type"] == "call_state_changed"
    assert house_snapshot == {"type": "house_call", "call_id": call_id, "state": "ringing", "from": "kitchen",
                              "to": "dina", "snapshot": True}


# ---- the per-user door cap in `portico/answer` (15.13) ----------------------------------------------------------

async def test_a_third_door_answer_by_one_user_is_refused_before_the_provider_is_asked(hass, hass_storage, hass_ws_client):
    w = await _world(hass, hass_storage, hass_ws_client, house_on=True, doors=6)
    answers = []
    for n in range(3):
        w.door.ring(f"gate-{n + 1}", f"g{n}")
        answers.append(await _send(w.alice_ws, 10 + n, {"type": "portico/answer", "door_id": f"gate-{n + 1}",
                                                         "call_id": f"g{n}", "client_kind": "app"}))
    assert [a["success"] for a in answers] == [True, True, False]
    assert answers[2]["error"]["code"] == "too_many_calls"
    assert len(w.door.calls_to("answer")) == 2                       # the provider never saw the third
    # another user is not limited by alice's two
    ok = await _send(w.dina_ws, 20, {"type": "portico/answer", "door_id": "gate-3", "call_id": "g2",
                                      "client_kind": "app"})
    assert ok["success"]
    # one ends: a slot is free again
    w.door.state("g0", "ended")
    await hass.async_block_till_done()
    w.door.ring("gate-4", "g3")
    again = await _send(w.alice_ws, 21, {"type": "portico/answer", "door_id": "gate-4", "call_id": "g3",
                                         "client_kind": "app"})
    assert again["success"]


async def test_a_refused_door_answer_gives_the_slot_back(hass, hass_storage, hass_ws_client):
    w = await _world(hass, hass_storage, hass_ws_client, house_on=True, doors=6)
    w.door.refuse_answers = True
    for n in range(5):
        w.door.ring(f"gate-{n + 1}", f"g{n}")
        resp = await _send(w.alice_ws, 10 + n, {"type": "portico/answer", "door_id": f"gate-{n + 1}", "call_id": f"g{n}",
                                                 "client_kind": "app"})
        assert resp["error"]["code"] == "refused", resp                 # never too_many_calls: a refusal holds no slot
    assert w.house.ledger.door_sessions(w.alice.id) == 0


async def test_concurrent_door_answers_by_one_user_never_exceed_two_claims(hass, hass_storage, hass_ws_client):
    w = await _world(hass, hass_storage, hass_ws_client, house_on=True, doors=6)
    for n in range(6):
        w.door.ring(f"gate-{n + 1}", f"g{n}")
    for n in range(6):
        await w.alice_ws.send_json({"id": 100 + n, "type": "portico/answer", "door_id": f"gate-{n + 1}",
                                    "call_id": f"g{n}", "client_kind": "app"})
    results = [await _result(w.alice_ws, 100 + n) for n in range(6)]
    assert sum(r["success"] for r in results) == 2
    assert sorted(r["error"]["code"] for r in results if not r["success"]) == ["too_many_calls"] * 4


async def test_answering_a_door_while_on_a_house_call_ends_the_house_call(hass, hass_storage, hass_ws_client):
    w = await _world(hass, hass_storage, hass_ws_client, house_on=True)
    for client in (w.alice_ws, w.dina_ws):
        await _subscribe(client)
    roster = (await _send(w.alice_ws, 30, {"type": "portico/content/get"}))["result"]["roster"]
    (dina,) = [e for e in roster if e["id"] == "dina"]
    call_id = (await _send(w.alice_ws, 31, {"type": "portico/content/run", "action_id": dina["action_id"]}))[
        "result"]["call_id"]
    await _events(w.alice_ws, 1)
    await _events(w.dina_ws, 1)
    assert (await _send(w.dina_ws, 32, {"type": "portico/house/answer", "call_id": call_id, "client_kind": "app"}))["success"]
    await _events(w.alice_ws, 1)
    await _events(w.dina_ws, 1)
    w.door.ring("gate-1", "g1")
    await _events(w.alice_ws, 1)                                       # the door ring shows over the call (door events)
    await _events(w.dina_ws, 1)
    assert (await _send(w.dina_ws, 33, {"type": "portico/answer", "door_id": "gate-1", "call_id": "g1",
                                         "client_kind": "app"}))["success"]
    ended = (await _events(w.alice_ws, 2))                              # the door's `answered`, then the house call ends
    assert [e for e in ended if e["type"] == "house_call"][0]["reason"] == "hangup"
