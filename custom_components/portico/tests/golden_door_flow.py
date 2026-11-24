"""A scripted door flow through the REAL driver part, recorded: what a phone's `portico/calls/subscribe` sees, what
`sensor.*_call_state` and the `portico_intercom_call` bus event say (the kitchen takeover automation's triggers), and the
push ring notification messages. Imports nothing of house calls: `test_house_golden.py` records it with house calls off (the
shipped default), with them on and idle, and with a house call ringing, and requires all three to equal GOLDEN_DOOR_FLOW,
which was recorded from `main` before the house-call code existed (the PR body says how)."""
from __future__ import annotations

import asyncio

from homeassistant.const import EVENT_STATE_CHANGED
from pytest_homeassistant_custom_component.common import async_capture_events

from .driver_part import GATE_SIP_URL
from .test_push_registry import _manager, _phone_user, _register_msg, _setup_all


async def _recv(client):
    return await asyncio.wait_for(client.receive_json(), timeout=5)


async def _drain(client, quiet: float = 0.4) -> list[dict]:
    """Every frame that arrives until the socket has been quiet for `quiet` seconds."""
    out = []
    while True:
        try:
            out.append(await asyncio.wait_for(client.receive_json(), timeout=quiet))
        except TimeoutError:
            return out


async def record_door_flow(hass, hass_storage, google, fake_transport, hass_ws_client, prepare=None,
                           after_subscribe=None, mid_flow=None) -> dict:
    user, token = await _phone_user(hass)
    await _setup_all(hass, hass_storage, google, enrolled=[(user.id, "p")])
    client = await hass_ws_client(hass, access_token=token)
    await client.send_json(_register_msg())
    reg_id = (await client.receive_json())["result"]["reg_id"]
    await _manager(hass).registry.async_set_enabled(reg_id, True)
    if prepare is not None:
        await prepare(hass, user)

    bus = async_capture_events(hass, "portico_intercom_call")
    states = async_capture_events(hass, EVENT_STATE_CHANGED)
    await client.send_json({"id": 10, "type": "portico/calls/subscribe"})
    assert (await _recv(client))["success"]
    snapshot = (await _recv(client))["event"]
    if after_subscribe is not None:
        await after_subscribe(hass, user)

    transport = fake_transport.instances[-1]
    notifier = _manager(hass).notifier
    # a visitor rings, is answered by someone else, hangs up; a second ring is missed
    transport.ring("gso-1")
    await hass.async_block_till_done()
    await notifier.async_drain()
    transport.state("gso-1", "answered")
    await hass.async_block_till_done()
    await notifier.async_drain()
    if mid_flow is not None:        # M7 P3-B: something else happens on the same door while the visitor is answered
        await mid_flow(hass, user)
    transport.state("gso-1", "active")
    await hass.async_block_till_done()
    transport.state("gso-1", "ended")
    await hass.async_block_till_done()
    await notifier.async_drain()
    transport.ring("gso-2")
    await hass.async_block_till_done()
    await notifier.async_drain()
    transport.state("gso-2", "ended")
    await hass.async_block_till_done()
    await notifier.async_drain()

    frames = [f["event"] for f in await _drain(client)]
    return {
        "snapshot": snapshot,
        "door_frames": [(f["door_id"], f["call_id"], f["state"], f.get("prev_state")) for f in frames
                        if f["type"] == "call_state_changed"],
        "bus": [(e.data["device_id"], e.data["state"], e.data.get("call_id")) for e in bus],
        "sensor": [(s.data["new_state"].state, dict(s.data["new_state"].attributes)) for s in states
                   if s.data["entity_id"] == "sensor.call_state" and s.data["new_state"] is not None],
        "push": [{k: v for k, v in r["json"]["message"]["data"].items() if k not in ("t", "exp")}
                 for r in google.fcm_requests],
        "other_frames": [f["type"] for f in frames if f["type"] != "call_state_changed"],
        "gate": GATE_SIP_URL,
    }
