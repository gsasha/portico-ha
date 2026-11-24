"""Golden proofs that the Talk relay (M7 P3-B) leaves the live door flow alone, and a golden for the Talk flow itself.

1. `fixtures/door_flow_golden.json` (recorded from `main` before house calls existed, see test_house_golden.py) is
   untouched by this PR, and the door flow recorded WITH a Talk call going on the same door in the middle of it still
   equals it, field by field: the phone's `call_state_changed` frames, the `portico_intercom_call` bus event and
   `sensor.call_state` (the kitchen takeover's triggers), and the push ring notification. The only addition is the phone's own
   `talk_state` frames, asserted too.
2. `fixtures/talk_flow_golden.json` records a scripted Talk call through the REAL driver part and hub (the Go side
   faked): what the placing phone, a second phone, the bus, the sensor and the push see. It is the shape portico-app's
   P3-C parser is written against. Regenerate with PORTICO_REGENERATE_TALK_GOLDEN=1 and review the diff.

HA-harness session. Everything here is inert on a real install: no door reports `CanDial`.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from homeassistant.const import EVENT_STATE_CHANGED
from pytest_homeassistant_custom_component.common import async_capture_events

from ..content import get_content_store
from ..talk_relay import get_talk_relay
from .driver_part import GATE_SIP_URL, fake_transport, get_provider  # noqa: F401 - fixture by name
from .golden_door_flow import _drain, _recv, record_door_flow
from .test_content_wire_blocks import _doc4
from .test_push_registry import _custom_integrations, _manager, _phone_user, _register_msg, _setup_all, google  # noqa: F401

pytestmark = pytest.mark.asyncio

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN = json.loads((FIXTURES / "door_flow_golden.json").read_text(encoding="utf-8"))
TALK_GOLDEN = FIXTURES / "talk_flow_golden.json"
OUT = "out-0123456789abcdef"


def _normal(recorded):
    return json.loads(json.dumps(recorded))


async def test_the_door_flow_golden_file_is_the_one_main_recorded():
    """P3-B changed no byte of it: a pin on its hash (the value main has had since 3a)."""
    import hashlib

    digest = hashlib.sha256((FIXTURES / "door_flow_golden.json").read_bytes()).hexdigest()
    assert digest == "b23df32ee62000b4ffb423c89b2f5c9e0078c30f9b9f015833e8d939c03204dc"


async def test_the_door_flow_is_the_golden_flow_with_a_talk_call_in_the_middle_of_it(
        hass, hass_storage, google, fake_transport, hass_ws_client):
    """While a visitor is answered, a Talk call for the same phone is placed, answered and ended on the same door. The
    visitor flow's every observable is the golden's; the phone additionally hears its own three `talk_state` frames."""
    async def mid_flow(hass, user):
        (device_entry,) = [e for e in hass.config_entries.async_entries("portico") if e.data.get("entry_type") == "door"]
        door_id = device_entry.runtime_data.device_id
        transport = fake_transport.instances[-1]
        relay = get_talk_relay(hass)
        placement = relay.placing(user.id, user.id, get_provider(hass), door_id)
        assert placement is not None
        transport.notify("call_state_changed", {"call_id": OUT, "device_id": door_id, "state": "answered"})
        relay.placed(placement, OUT)
        transport.notify("call_state_changed", {"call_id": OUT, "device_id": door_id, "state": "ended"})
        await hass.async_block_till_done()

    recorded = _normal(await record_door_flow(hass, hass_storage, google, fake_transport, hass_ws_client,
                                              mid_flow=mid_flow))
    assert recorded.pop("other_frames") == ["talk_state", "talk_state", "talk_state"]
    assert recorded == {k: v for k, v in GOLDEN.items() if k != "other_frames"}


async def test_the_door_flow_is_the_golden_flow_with_the_relay_present_and_no_talk(
        hass, hass_storage, google, fake_transport, hass_ws_client):
    """Today: the relay exists (the subscribe created it), nothing was placed, nothing differs."""
    recorded = _normal(await record_door_flow(hass, hass_storage, google, fake_transport, hass_ws_client))
    assert recorded == GOLDEN
    assert get_talk_relay(hass)._by_door == {}


async def record_talk_flow(hass, hass_storage, google, fake_transport, hass_ws_client) -> dict:
    phone, phone_token = await _phone_user(hass, "Alice phone")
    other, other_token = await _phone_user(hass, "Other phone")
    device, _push = await _setup_all(hass, hass_storage, google, enrolled=[(phone.id, "p"), (other.id, "q")])
    runtime = device.runtime_data
    runtime.capabilities = {**runtime.capabilities, "CanDial": True}       # what P3-A's driver says at stage 2
    store = get_content_store(hass)
    await store.async_ensure_loaded()
    await store.async_save_document(_doc4({"id": "gate", "title": "Gate", "door": GATE_SIP_URL, "cameras": [],
                                           "actions": [{"id": "talk", "label": "Talk", "kind": "talk",
                                                        "confirm": False}]}), store.doc_rev)
    mine = await hass_ws_client(hass, access_token=phone_token)
    theirs = await hass_ws_client(hass, access_token=other_token)
    for client, n in ((mine, 1), (theirs, 1)):
        await client.send_json({"id": n, "type": "portico/calls/subscribe"})
        assert (await _recv(client))["success"]
        await _recv(client)                                              # the door's snapshot
    go = fake_transport.instances[-1]

    async def dial(device_id, host, port):
        go.notify("call_state_changed", {"call_id": OUT, "device_id": device_id, "state": "answered"})
        return {"ok": True, "call_id": OUT}

    go.dial = dial
    bus = async_capture_events(hass, "portico_intercom_call")
    states = async_capture_events(hass, EVENT_STATE_CHANGED)
    await mine.send_json({"id": 2, "type": "portico/content/run", "action_id": "talk"})
    run = []
    while not run or run[-1].get("type") != "result":
        run.append(await _recv(mine))
    go.notify("call_state_changed", {"call_id": OUT, "state": "ended"})       # the gate hung up (unstamped)
    await hass.async_block_till_done()
    after = await _drain(mine)
    return {
        "phone_frames": [f["event"] if f["type"] == "event" else {"result": f["result"]} for f in run + after],
        "other_phone_frames": [f["event"] for f in await _drain(theirs, 0.3)],
        "bus": [(e.data["device_id"], e.data["state"], e.data.get("call_id")) for e in bus],
        "sensor": [(s.data["new_state"].state, dict(s.data["new_state"].attributes)) for s in states
                   if s.data["entity_id"] == "sensor.call_state" and s.data["new_state"] is not None],
        "push": [r["json"]["message"]["data"] for r in google.fcm_requests],
        "gate": GATE_SIP_URL,
    }


async def test_the_talk_flow_through_the_real_driver_is_the_golden_talk_flow(
        hass, hass_storage, google, fake_transport, hass_ws_client):
    recorded = _normal(await record_talk_flow(hass, hass_storage, google, fake_transport, hass_ws_client))
    if os.environ.get("PORTICO_REGENERATE_TALK_GOLDEN") == "1":
        TALK_GOLDEN.write_text(json.dumps(recorded, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    assert recorded == json.loads(TALK_GOLDEN.read_text(encoding="utf-8"))


async def test_the_talk_golden_has_the_shape_it_claims():
    """A golden that proves nothing is worse than none: the placing phone's three frames and the reply, in order, and an
    empty everything-else."""
    golden = json.loads(TALK_GOLDEN.read_text(encoding="utf-8"))
    assert [f.get("state", "reply") for f in golden["phone_frames"]] == ["dialing", "in_call", "reply", "ended"]
    assert [f.get("call_id") for f in golden["phone_frames"][:2]] == [None, OUT]
    assert "reason" not in golden["phone_frames"][3]
    assert golden["other_phone_frames"] == [] and golden["bus"] == [] and golden["sensor"] == [] and golden["push"] == []
