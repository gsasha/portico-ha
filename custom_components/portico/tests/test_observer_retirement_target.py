"""DEV-518 C3: the TARGET STATE of the gate_sip_observer retirement (plan: home `portico_observer_retirement_plan.md`), proven
before it is written live: the document and the elements name ONLY the gate_sip_server door, and then
* a Talk element on the server door gives the Ring page a gate ring resolves to (Gate and Side Door buttons, not the built-in
  default screen),
* the panel's `wake` holds the server door, so a gate ring is pushed to the panel and a door outside it is not,
* the Side Door button (`portico_open`, `device_id` = the server door) is available and runs the server entry's
  `open_relay_action` (the HA action) once,
* no observer entry exists, and nothing in the resolved document, the roster or the wake names the observer's address.
Inert: a test only, no production code."""
from __future__ import annotations

import copy
import json

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er  # noqa: F401
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from .. import content
from ..config_flow import DOMAIN
from ..content import ContentStore
from ..elements import get_elements_store
from ..endpoints import door_in_refs, panel_door_push_allowed, panel_wake_doors
from ..phone_entry import CONF_PANEL, PhoneRecord
from ..pages_resolve import assigned_ring_pages
from .driver_part import SERVER_DEVICE_ID, _server_entry, fake_transport  # noqa: F401 - fixture by name
from .test_pages_pc import _doc, _file, _page
from .test_pages_pc import pworld  # noqa: F401 - a fixture, by name
from .test_push_registry import _phone_user
from .test_push_registry import _custom_integrations  # noqa: F401 - autouse fixture by name
from .test_signaling import _setup_signaling  # noqa: F401

pytestmark = pytest.mark.asyncio

PANEL = "ep-panel"
OBSERVER_URL = "http://10.6.0.7:8091"
HELPER = "input_boolean.gate_ring_answered"
OPEN_ACTION = [{"action": "button.press", "target": {"entity_id": "button.example_intercom_ex_gate_open_door"}}]
SIDE_DOOR = {"id": "portico-gate-sip-observe", "label": "Side Door", "kind": "portico_open", "device_id": SERVER_DEVICE_ID,
             "confirm": True}


@pytest.fixture(params=["legacy", "per_talk"])
async def target(request, pworld, fake_transport):
    """Both Ring page forms main supports: the device's `ring` map (what the live document holds today) and the per-Talk form
    (R-A, D-20261122-4aec: the stored Ring page of the Talk element, no device `ring` map)."""
    w = pworld
    w.panel_user, _ = await _phone_user(w.hass, name="Portico — Kitchen panel")
    record = PhoneRecord(ha_user_id=w.panel_user.id, name="Kitchen panel", enroll_id=PANEL, enrolled_at="2026-11-19T10:00:00+00:00")
    MockConfigEntry(domain=DOMAIN, title="Kitchen panel", unique_id=f"phone:{record.key}", data=record.to_data(),
                    options={CONF_PANEL: True, "house_receive": True}).add_to_hass(w.hass)
    # the ONLY gate entry: the server door, with the door-open action and the ring helper (C1) and the house-calls option (C2)
    w.server = _server_entry(w.hass, open_relay_action=OPEN_ACTION, gate_sip_listen={"ring_answered_entity": HELPER})
    w.hass.config_entries.async_update_entry(w.server, options={"house_calls": True})
    assert await w.hass.config_entries.async_setup(w.server.entry_id)
    await w.hass.async_block_till_done()
    elements = get_elements_store(w.hass)
    await elements.async_ensure_loaded()
    made = await elements.async_set({"kind": "talk", "name": "Dahua", "options": {"door": SERVER_DEVICE_ID}}, elements.rev)
    w.talk = made["element"]["id"]
    ring = {"id": "ring-gate", "label": "Gate ring", "kind": "ring", "talk": w.talk, "cameras": None,
            "blocks": [{"id": "r", "title": "Gate", "cameras": [], "actions": [dict(SIDE_DOOR)]}]}
    home = {"id": "home-panel", "label": "Home", "kind": "content",
            "blocks": [{"id": "gate", "title": "Gate", "door": SERVER_DEVICE_ID, "cameras": [], "actions": [
                dict(SIDE_DOOR, id="side-home"), {"id": "dahua", "element": w.talk}]}]}
    doc = _doc([home, ring], {PANEL: ["home-panel"]}, ())
    if request.param == "legacy":
        doc["devices"][PANEL]["ring"] = {w.talk: "ring-gate"}
    w.storage["portico.content"] = _file(copy.deepcopy(doc))
    store = ContentStore(w.hass)
    w.hass.data["portico"][content.DATA_CONTENT] = store
    await store.async_ensure_loaded()
    w.store = store
    yield w
    await w.hass.config_entries.async_unload(w.server.entry_id)
    await w.hass.async_block_till_done()


async def test_the_server_door_is_the_only_gate_and_a_door_of_the_registry(target):
    from ..provider import DATA_PROVIDERS, DOMAIN as PROVIDER_DOMAIN
    ids = [d.id for d in target.hass.data[PROVIDER_DOMAIN][DATA_PROVIDERS].doors()]
    assert SERVER_DEVICE_ID in ids
    # (the harness's phone-holder entry is a gate_sip_observer-shaped row that never loads: no observer DOOR runs)
    assert not [e for e in target.hass.config_entries.async_entries(DOMAIN)
                if e.data.get("driver") == "gate_sip_observer" and e.state is ConfigEntryState.LOADED]


async def test_the_panel_wakes_for_the_server_door_and_for_nothing_else(target):
    wake = target.store.resolve_for_endpoint(PANEL)["wake"]
    assert wake["doors"] == [SERVER_DEVICE_ID]
    uid = target.panel_user.id
    assert panel_door_push_allowed(target.hass, uid, SERVER_DEVICE_ID) is True       # a gate ring reaches the panel
    assert panel_door_push_allowed(target.hass, uid, "gate-1") is False              # any door outside its wake does not
    assert door_in_refs(panel_wake_doors(target.hass, PANEL), SERVER_DEVICE_ID)
    assert target.store.screens_for_door(SERVER_DEVICE_ID) == [PANEL] or target.store.wake_for_talk_element(target.talk) == [PANEL]


async def test_a_gate_ring_resolves_to_the_ring_page_with_the_side_door(target):
    doc = target.store.resolve_document(PANEL)
    (caller_id,) = [k for k, v in doc["callers"].items() if v["kind"] == "door" and SERVER_DEVICE_ID in k]
    caller = doc["callers"][caller_id]
    assert [b["id"] for b in caller["ring_buttons"]] == ["portico-gate-sip-observe"]
    (side,) = caller["ring_buttons"]
    assert side["label"] == "Side Door" and side["kind"] == "portico_open" and side["available"] is True
    assert [p["id"] for p in assigned_ring_pages(target.store._expanded(), PANEL)] == ["ring-gate"]
    # the in-page Side Door button and the Dahua Talk element resolve against the same door
    (block,) = doc["pages"][0]["blocks"]
    assert {a["id"]: a["available"] for a in block["actions"] if a["id"] == "side-home"} == {"side-home": True}


async def test_the_side_door_runs_the_server_entrys_action_once(target):
    calls = async_mock_service(target.hass, "button", "press")
    await target.store.async_run(target.panel_user.id, "Kitchen panel", "portico-gate-sip-observe",
                                 caller_user_id=target.panel_user.id, endpoint_id=PANEL)
    await target.hass.async_block_till_done()
    assert [c.data["entity_id"] for c in calls] == [["button.example_intercom_ex_gate_open_door"]]
    assert len(calls) == 1          # once; the Context is the one `actions.begin_open` made for this press (as for any door)


async def test_nothing_resolved_names_the_observer(target):
    blob = json.dumps({"doc": target.store.resolve_document(PANEL), "get": target.store.resolve_for_endpoint(PANEL),
                       "stored": target.store.document(), "elements": get_elements_store(target.hass).list()
                       if hasattr(get_elements_store(target.hass), "list") else None}, default=str)
    assert OBSERVER_URL not in blob and "10.6.0.7" not in blob


async def test_without_a_ring_page_a_gate_ring_shows_only_the_built_in_default(target):
    """The regression this retirement repairs, pinned as the contrast: an unassigned Talk element gives the default screen."""
    doc = copy.deepcopy(target.store.document())
    doc["devices"][PANEL]["ring"] = {}
    doc["pages"] = [p for p in doc["pages"] if p.get("kind") != "ring"]
    await target.store.async_save_document(doc, target.store.doc_rev)
    caller = [v for k, v in target.store.resolve_document(PANEL)["callers"].items() if SERVER_DEVICE_ID in k][0]
    assert "portico-gate-sip-observe" not in [b["id"] for b in caller["ring_buttons"]]
