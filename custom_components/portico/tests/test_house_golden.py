"""Golden proofs that the house-call model leaves the live door flow alone (M7 3a).

`fixtures/door_flow_golden.json` was recorded by `golden_door_flow.record_door_flow` against `main` BEFORE any house-call
code existed (the same helper, run in a clean clone of origin/main at ac5e59f). These tests record the same scripted flow
(ring, answer, active, end, a missed second ring: the phone's `portico/calls/subscribe` door frames, the
`portico_intercom_call` bus event and `sensor.call_state` the kitchen takeover automation triggers on, the push ring notification)
three ways and require each to equal it: house calls OFF (the shipped default), ON and idle, and ON with a house call
ringing on the phone when the door rings. The sole permitted difference is the house call's own events on the
subscription, and they are asserted too.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ..content import get_content_store
from ..house import async_setup_house, get_house
from ..phone_entry import async_set_house_receive, phone_entries
from .driver_part import fake_transport  # noqa: F401 - fixture by name
from .golden_door_flow import record_door_flow
from ..push import const
from .house_env import FakeConn, phone_entry
from .test_push_registry import _custom_integrations, _manager, _phone_user, google  # noqa: F401 - fixtures by name

pytestmark = pytest.mark.asyncio

GOLDEN_SHA256 = "b23df32ee62000b4ffb423c89b2f5c9e0078c30f9b9f015833e8d939c03204dc"      # recorded from main before any house code
GOLDEN = json.loads((Path(__file__).parent / "fixtures" / "door_flow_golden.json").read_text(encoding="utf-8"))


def _normal(recorded: dict) -> dict:
    return json.loads(json.dumps(recorded))


async def test_the_golden_file_is_the_one_recorded_from_main():
    """3c does not touch the golden: the file is byte for byte what 3a pinned."""
    assert hashlib.sha256((Path(__file__).parent / "fixtures" / "door_flow_golden.json").read_bytes()).hexdigest() == GOLDEN_SHA256


async def test_the_golden_flow_has_the_shape_it_claims():
    """Guard against a golden that proves nothing: it must hold the ring, the answer, the end, the sensor and the push."""
    assert [f[2] for f in GOLDEN["door_frames"]] == ["ringing", "answered", "active", "ended", "ringing", "ended"]
    assert [s[0] for s in GOLDEN["sensor"]][:4] == ["ringing", "answered", "active", "idle"]
    assert [p["kind"] for p in GOLDEN["push"]] == ["ring", "answered"] and GOLDEN["bus"][0][1] == "ringing"
    assert GOLDEN["other_frames"] == []


async def test_house_calls_on_and_idle_the_door_flow_is_still_the_golden_flow(
        hass, hass_storage, google, fake_transport, hass_ws_client):
    async def prepare(hass, user):
        assert async_setup_house(hass, enabled=True) is not None

    recorded = await record_door_flow(hass, hass_storage, google, fake_transport, hass_ws_client, prepare=prepare)
    assert _normal(recorded) == GOLDEN                     # the phone is attached and online; not one door byte differs
    assert get_house(hass).ledger.door_rings() == []       # the ledger saw the calls come and go


async def test_a_house_call_ringing_when_the_door_rings_gives_way_and_the_door_flow_is_unchanged(
        hass, hass_storage, google, fake_transport, hass_ws_client):
    kitchen_user, _token = await _phone_user(hass, "Kitchen tablet")
    hass_storage["portico.content"] = {
        "version": 1, "minor_version": 1, "key": "portico.content",
        "data": {"schema_version": 3, "template": {"pages": [], "rules": [], "roster": ["*"]}, "overrides": {},
                 "dashboards": {"kitchen": {"name": "Kitchen", "ha_user_id": kitchen_user.id, "room": "Kitchen",
                                            "cameras": []}}, "endpoints": {}}}
    calls: dict = {}

    async def prepare(hass, user):
        await get_content_store(hass).async_ensure_loaded()
        async_setup_house(hass, enabled=True)
        (entry,) = [e for e in phone_entries(hass) if e.data["ha_user_id"] == user.id]
        async_set_house_receive(hass, entry, True)

    async def after_subscribe(hass, user):
        house = get_house(hass)
        calls["kitchen"] = FakeConn(kitchen_user.id)
        assert house.attach(calls["kitchen"], 1) is not None
        placed = house.place_call(caller_user_id=kitchen_user.id, target_type="phone",
                                  target_id=user.id, caller_endpoint="kitchen")      # a row with no enroll id: its user id
        assert placed.ok, placed
        calls["id"] = placed.call_id

    recorded = _normal(await record_door_flow(hass, hass_storage, google, fake_transport, hass_ws_client,
                                              prepare=prepare, after_subscribe=after_subscribe))
    # the house call's own events are on the phone's subscription, and nothing else differs from the golden flow
    assert recorded.pop("other_frames") == ["house_roster_changed", "house_call", "house_call"]   # the screen came online;
    #                                                                                  the ring, then its end (the door won)
    assert recorded == {k: v for k, v in GOLDEN.items() if k != "other_frames"}
    assert get_house(hass).ledger.house_calls() == 0
    assert [(state, reason) for state, _to, reason in calls["kitchen"].house()] == [("ringing", None), ("ended", "busy")]


async def test_the_door_push_is_unchanged_beside_a_house_ring_to_an_away_phone(
        hass, hass_storage, google, fake_transport, hass_ws_client):
    """M7 3c: house calls ON, the push ring notification ON, a house call ringing an AWAY phone by notification (`ct: house`) when the door
    rings. The recorded phone's door frames, bus events, sensor and door push are the golden flow's, byte for byte; the
    house messages are a separate stream (no `door_ref`), and no door message carries `ct` or `from_ref`."""
    kitchen_user, _token = await _phone_user(hass, "Kitchen tablet")
    dina_user, _ = await _phone_user(hass, "Dina")
    hass_storage["portico.content"] = {
        "version": 1, "minor_version": 1, "key": "portico.content",
        "data": {"schema_version": 3, "template": {"pages": [], "rules": [], "roster": ["*"]}, "overrides": {},
                 "dashboards": {"kitchen": {"name": "Kitchen", "ha_user_id": kitchen_user.id, "room": "Kitchen",
                                            "cameras": []}}, "endpoints": {}}}
    calls: dict = {}

    async def prepare(hass, user):
        await get_content_store(hass).async_ensure_loaded()
        async_setup_house(hass, enabled=True)
        (entry,) = [e for e in phone_entries(hass) if e.data["ha_user_id"] == user.id]
        async_set_house_receive(hass, entry, True)
        phone_entry(hass, dina_user.id, "dina", "Dina", receive=True)
        row = await _manager(hass).registry.async_register(
            ha_user_id=dina_user.id, installation_id="inst-dina", kind="fid", registration="REG-DINA-0123456789abcdef",
            package=const.PORTICO_PACKAGE, fcm_project_id="portico-test", app_version="0.6.0", name="Dina")
        await _manager(hass).registry.async_set_enabled(row["reg_id"], True)

    async def after_subscribe(hass, user):
        house = get_house(hass)
        calls["kitchen"] = FakeConn(kitchen_user.id)
        assert house.attach(calls["kitchen"], 1) is not None
        assert house.presence("dina")["state"] == "push_only"
        placed = house.place_call(caller_user_id=kitchen_user.id, target_type="phone", target_id="dina",
                                  caller_endpoint="kitchen")
        assert placed.ok, placed
        calls["id"] = placed.call_id

    recorded = _normal(await record_door_flow(hass, hass_storage, google, fake_transport, hass_ws_client,
                                              prepare=prepare, after_subscribe=after_subscribe))
    recorded.pop("other_frames")                         # the house call's own frames are not the door flow's
    recorded.pop("push")                                 # every request, hers too; the recorded phone's own are below
    assert recorded == {k: v for k, v in GOLDEN.items() if k not in ("other_frames", "push")}
    sent = google.sent_messages()
    mine = [m for m in sent if (m.get("fid") or m.get("token")) != "REG-DINA-0123456789abcdef"]
    hers = [m for m in sent if (m.get("fid") or m.get("token")) == "REG-DINA-0123456789abcdef"]
    assert [{k: v for k, v in m["data"].items() if k not in ("t", "exp")} for m in mine] == GOLDEN["push"]
    house_msgs = [m["data"] for m in sent if m["data"].get("ct") == "house"]
    assert [(d["kind"], d["call_id"]) for d in house_msgs] == [("ring", calls["id"]), ("ended", calls["id"])]
    assert all("door_ref" not in d and d["from_ref"].startswith("e-") for d in house_msgs)
    assert all("ct" not in m["data"] and "from_ref" not in m["data"] for m in sent if m["data"].get("door_ref"))
    assert {m["data"]["call_id"] for m in hers if m["data"].get("door_ref")} == {"gso-1"}     # she is rung for the door too
