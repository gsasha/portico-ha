# ruff: noqa: F811 - test parameters named after the imported fixtures
"""Golden proof that M7 3f changes nothing while no door reports `CanDial` (every install today).

`fixtures/gate_off_golden.json` was recorded from `main` BEFORE 3f existed (the PR body says how): what the phone and the
kitchen screen are served (`portico/content/get`, the whole resolved document, so the roster, a stored `call` button that
targets the gate and a Talk button included), what the editor's `portico/content/document` lists as endpoints, the
house roster revision, and what a press of the stored button and of the gate's roster key answer. With the dial off,
the recording made now equals it, byte for byte. The two door-flow goldens are pinned by hash here as well.

Regenerate ONLY on a tree that does not contain 3f: PORTICO_REGENERATE_GATE_OFF_GOLDEN=1.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

import pytest

from ..house import async_setup_house
from .test_content_wire_blocks import _entrance, _fake
from .test_editor_commands import PHONE_ID, _client, _reload, _send, _v3, world  # noqa: F401 - fixtures by name
from .test_talk_gate import DOOR

pytestmark = pytest.mark.asyncio

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN_FILE = FIXTURES / "gate_off_golden.json"
REGENERATE_ENV = "PORTICO_REGENERATE_GATE_OFF_GOLDEN"


def _blocks_document(world):
    block = _entrance()
    block["actions"].append({"id": "call-gate", "label": "Gate", "kind": "call", "target": {"type": "door", "id": DOOR},
                             "confirm": False})
    return {"schema_version": 4,
            "template": {"pages": [{"id": "home", "label": "Home", "blocks": [block]}], "rules": [], "roster": ["*"]},
            "overrides": {},
            "dashboards": {"kitchen": {"name": "Kitchen tablet", "ha_user_id": world.tablet.id, "room": "Kitchen",
                                       "cameras": []}},
            "endpoints": {}}


async def _record(world, hass_ws_client) -> dict:
    world.hass.states.async_set("camera.yard", "idle")
    for entity in ("camera.side", "camera.lawn", "lock.gate", "scene.evening", "cover.gate"):
        world.hass.states.async_set(entity, "idle")
    await _reload(world, _blocks_document(world))
    out: dict = {}
    phone = await _client(world, hass_ws_client, "phone")
    tablet = await _client(world, hass_ws_client, "tablet")
    admin = await _client(world, hass_ws_client)
    out["get_phone"] = await _send(phone, "portico/content/get", 1)
    out["get_screen"] = await _send(tablet, "portico/content/get", 1)
    out["document_endpoints"] = (await _send(admin, "portico/content/document", 1))["result"]["endpoints"]
    house = async_setup_house(world.hass, enabled=True)
    out["house_roster_rev"] = house.roster_rev()
    out["house_picker"] = sorted((e.type, e.id) for e in house.endpoints(caller_user_id=world.phone.id))
    roster_id = next(e["action_id"] for e in out["get_phone"]["result"]["roster"] if e["id"] == DOOR)
    out["run_button"] = await _send(phone, "portico/content/run", 2, action_id="call-gate")
    world.store._last_run.clear()
    out["run_roster_key"] = await _send(phone, "portico/content/run", 3, action_id=roster_id)
    out["dials"] = len(_fake(world).calls_to("dial"))
    text = json.dumps(out)
    for user in await world.hass.auth.async_get_users():
        text = text.replace(user.id, "user-" + re.sub(r"[^a-z0-9]+", "-", user.name.lower()).strip("-"))
    return json.loads(text)


async def test_with_the_dial_off_everything_served_is_what_main_served_before_3f(world, hass_ws_client):
    recorded = await _record(world, hass_ws_client)
    if os.environ.get(REGENERATE_ENV) == "1":
        GOLDEN_FILE.write_text(json.dumps(recorded, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    golden = json.loads(GOLDEN_FILE.read_text(encoding="utf-8"))
    assert recorded == golden
    assert recorded["dials"] == 0
    assert "can_dial" not in json.dumps(recorded)


async def test_the_golden_has_the_gate_in_it_so_the_comparison_means_something():
    golden = json.loads(GOLDEN_FILE.read_text(encoding="utf-8"))
    roster = golden["get_phone"]["result"]["roster"]
    (gate,) = [e for e in roster if e["id"] == DOOR]
    assert (gate["kind"], gate["available"], gate["reason"]) == ("door", False, "not_available")
    assert golden["run_button"]["error"]["code"] == "not_available"
    assert golden["run_roster_key"]["error"]["code"] == "not_available"


async def test_the_two_door_flow_goldens_are_the_ones_main_has():
    for name, digest in (("door_flow_golden.json", "b23df32ee62000b4ffb423c89b2f5c9e0078c30f9b9f015833e8d939c03204dc"),
                         ("talk_flow_golden.json", "1c7441fcf4a7a8d1b7961f619a8a8a2159e93834f18c29f2779852c673ed475e")):
        assert hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() == digest, name
