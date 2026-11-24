# ruff: noqa: F811 - test parameters named after the imported fixtures
"""The Talk element (DEV-518, D-20261109-20a6): an Elements-page kind that names ONE intercom door, placed on a screen like a
button, and pressed through the one existing dial path (`ContentStore._run_talk`) with that door and `explicit=True`.

Pinned here: the element's validation and the door check (exists when saved; a stored reference to a door that later
disappears is kept and reads "door removed"); the picker data of `elements/list`; the resolved `talk` action (its own `door`,
never ask-first); the press with an explicit door while the one-gate rule is AMBIGUOUS (the explicit target wins, exactly one
`dial`); the refusals by case (removed / not connected / busy / dial off / no dial capability); and the old per-block Talk
unchanged (the one-gate rule still applies to it, `talk_off` still its word).
"""
from __future__ import annotations

import copy

import pytest

from ..content import RunRefused
from ..content_elements import ElementRefError, check_refs
from ..elements import ElementError, get_elements_store, validate_element
from .test_content_wire_blocks import _doc4, _entrance, _fake, _save
from .test_editor_commands import PHONE_ID, _client, _send, world  # noqa: F401 - fixtures by name
from .test_push_registry import _custom_integrations  # noqa: F401 - autouse fixture by name

pytestmark = pytest.mark.asyncio

SRV = "gate_sip_server"
OBS = SRV                 # a second gate-driver door (the observer was retired, DEV-518 C5)


async def _talk(world, name="Gate talk", door="gate-1", **extra):
    elements = get_elements_store(world.hass)
    await elements.async_ensure_loaded()
    out = await elements.async_set({"kind": "talk", "name": name, "options": {"door": door}, **extra}, elements.rev)
    return out["element"]


def _ref_doc(element_id, **ref):
    block = {"id": "calls", "title": "Calls", "cameras": [], "actions": [{"id": "talk-el", "element": element_id, **ref}]}
    return _doc4(block)


def _two_gates(world, *, obs=(True, True), srv=(True, True)):
    fake = _fake(world)
    for door_id, driver, (can, up) in (("gate-obs", OBS, obs), ("gate-srv", SRV, srv)):
        fake.add_door(door_id, name=door_id, driver=driver, camera_entity_id="camera.gate",
                      capabilities={"CanDial": True} if can else {}, available=up)


def _action(world):
    page = world.store.resolve_document(PHONE_ID)["pages"][0]
    (action,) = [a for b in page["blocks"] for a in b["actions"] if a["id"] == "talk-el"]
    return action


async def _press(world, action_id="talk-el"):
    phone = world.store._endpoint_by_id(PHONE_ID)
    return await world.store.async_run(phone.ha_user_id, "S", action_id, caller_user_id=phone.ha_user_id, endpoint_id=PHONE_ID)


async def _refusal(world, action_id="talk-el"):
    world.store._last_run.clear()
    with pytest.raises(RunRefused) as err:
        await _press(world, action_id)
    return err.value.code, err.value.message


# ---- validation ----------------------------------------------------------------------------------------------------

async def test_a_talk_element_stores_label_icon_and_door_with_phone_as_the_default_icon(world):
    el = await _talk(world)
    assert el["kind"] == "talk" and el["name"] == "Gate talk" and el["icon"] == "phone"
    assert el["options"] == {"door": "gate-1"} and "entity_id" not in el
    other = await _talk(world, name="Other", icon="gate")
    assert other["icon"] == "gate"


async def test_the_door_form_is_normalised_to_the_doors_own_id(world):
    el = await _talk(world, door="fake:gate-1")        # `<provider>:<id>` resolves to the same door
    assert el["options"]["door"] == "gate-1"


@pytest.mark.parametrize("raw, field", [
    ({"kind": "talk", "name": "T", "options": {}}, "options.door"),
    ({"kind": "talk", "name": "T", "options": {"door": ""}}, "options.door"),
    ({"kind": "talk", "name": "T", "options": {"door": 5}}, "options.door"),
    ({"kind": "talk", "name": "T", "options": {"door": "x" * 129}}, "options.door"),
    ({"kind": "talk", "name": "T", "options": {"door": "d", "target": "x"}}, "options"),
    ({"kind": "talk", "name": "T", "entity_id": "lock.gate", "options": {"door": "d"}}, "entity_id"),
    ({"kind": "talk", "name": "T", "icon": "mdi:not-in-the-fixed-set", "options": {"door": "d"}}, "icon"),
])
async def test_shape_errors_are_refused(raw, field):
    with pytest.raises(ElementError) as err:
        validate_element(raw)
    assert err.value.code == "invalid" and err.value.field == field


async def test_the_door_must_exist_when_saved_and_a_vanished_one_is_kept(world):
    elements = get_elements_store(world.hass)
    await elements.async_ensure_loaded()
    with pytest.raises(ElementError) as err:
        await elements.async_set({"kind": "talk", "name": "T", "options": {"door": "nobody"}}, elements.rev)
    assert err.value.code == "invalid" and err.value.field == "options.door"
    with pytest.raises(ElementError):
        await elements.async_set({"kind": "talk", "name": "T", "options": {"door": "nobody"}}, elements.rev, validate_only=True)
    el = await _talk(world)
    _fake(world).remove_door("gate-1")                 # the door goes away later
    again = await elements.async_set({**el, "name": "Renamed"}, elements.rev)      # still saves: the reference is kept
    assert again["element"]["options"]["door"] == "gate-1" and again["element"]["name"] == "Renamed"
    with pytest.raises(ElementError):                  # but pointing it at a door that is not there is refused
        await elements.async_set({**el, "name": "Renamed", "options": {"door": "other-gone"}}, elements.rev)


async def test_a_missing_or_empty_door_resolves_to_nothing(world):
    elements = get_elements_store(world.hass)
    assert elements.talk_door("") is None and elements.talk_door(None) is None and elements.talk_door("nobody") is None


# ---- picker data ---------------------------------------------------------------------------------------------------

async def test_elements_list_carries_the_doors_with_driver_and_can_dial_now(world, hass_ws_client):
    fake = _fake(world)
    fake.add_door("gate-obs", name="Gate one", driver=OBS, capabilities={"CanDial": True})
    fake.add_door("gate-srv", name="Gate server", driver=SRV, capabilities={}, available=False)
    fake.add_door("bell", name="Doorbell", driver="ha_event", capabilities={})
    admin = await _client(world, hass_ws_client)
    reply = await _send(admin, "portico/elements/list", 1)
    assert reply["success"], reply
    doors = {d["door_id"]: d for d in reply["result"]["talk_doors"]}
    assert doors["gate-obs"] == {"door_id": "gate-obs", "name": "Gate one", "driver": OBS, "can_dial": True, "available": True}
    assert doors["gate-srv"]["can_dial"] is False and doors["gate-srv"]["available"] is False and doors["gate-srv"]["driver"] == SRV
    assert doors["bell"]["driver"] == "ha_event" and doors["bell"]["can_dial"] is False
    assert [d["name"] for d in reply["result"]["talk_doors"]] == sorted(d["name"] for d in reply["result"]["talk_doors"])
    assert reply["result"]["kinds"]["talk"]["fields"] == [
        {"name": "door", "type": "door", "required": False}, {"name": "screen", "type": "screen", "required": False},
        {"name": "phone", "type": "phone", "required": False},
        {"name": "group", "type": "group", "required": False}]       # exactly one of the four (DEVICES-10, group calls)
    assert reply["result"]["icon_policy"]["talk"] == "fixed"


# ---- placement and the resolved action -----------------------------------------------------------------------------

async def test_a_placed_talk_element_resolves_to_a_talk_action_with_its_own_door_and_no_block_door(world):
    el = await _talk(world)
    await _save(world, _ref_doc(el["id"]))
    assert world.storage["portico.content"]["data"]["schema_version"] == 5          # the existing rule: stamped 5 with a reference
    action = _action(world)
    assert action["kind"] == "talk" and action["door"] == "gate-1" and action["label"] == "Gate talk"
    assert action["icon"] == "phone" and action["confirm"] is False
    assert "element" not in action
    assert action["available"] is False and action["reason"] == "no_dial"     # the fake door cannot dial: the case is named


async def test_a_reference_cannot_ask_first_and_show_label_needs_the_icon(world):
    el = await _talk(world)
    await _save(world, _ref_doc(el["id"], confirm=True, show_label=False, label="Call"))
    action = _action(world)
    assert action["confirm"] is False and action["show_label"] is False and action["label"] == "Call"       # CB-9: never confirm-gated


async def test_a_talk_element_cannot_be_named_by_a_rule(world):
    el = await _talk(world)
    doc = _ref_doc(el["id"])
    from ..content_elements import _check_rule_talk
    doc["template"]["rules"] = [{"id": "r", "ring_buttons": ["talk-el"]}]
    with pytest.raises(ElementRefError) as err:
        _check_rule_talk(doc, {el["id"]: el})
    assert err.value.code == "element_kind"
    with pytest.raises(ElementRefError):
        check_refs(doc, {el["id"]: el})


async def test_changing_the_door_of_a_used_talk_element_needs_confirm(world):
    first = await _talk(world)
    _fake(world).add_door("gate-2", name="Gate 2", driver=SRV, capabilities={"CanDial": True})
    await _save(world, _ref_doc(first["id"]))
    elements = get_elements_store(world.hass)
    moved = {**copy.deepcopy(first), "options": {"door": "gate-2"}}
    with pytest.raises(ElementError) as err:
        await elements.async_set(moved, elements.rev)
    assert err.value.code == "in_use_retarget"
    out = await elements.async_set(moved, elements.rev, confirm=True)
    assert out["element"]["options"]["door"] == "gate-2"


# ---- the press -----------------------------------------------------------------------------------------------------

async def test_an_explicit_door_dials_once_through_the_one_provider_dial(world):
    """Two gate-driver doors can dial and are up. The element names one door, so its press dials that one, through the
    one `provider.dial`."""
    _two_gates(world)
    el = await _talk(world, door="gate-srv")
    await _save(world, _ref_doc(el["id"]))
    action = _action(world)
    assert action["available"] is True and action["reason"] is None and action["door"] == "gate-srv"
    out = await _press(world)
    assert out["call_id"].startswith("fake-dial-")
    assert _fake(world).calls_to("dial") == [(("gate-srv", "", 0), {})]


async def test_a_second_press_during_the_own_call_hangs_up_and_dials_nothing_more(world):
    _two_gates(world)
    el = await _talk(world, door="gate-obs")
    await _save(world, _ref_doc(el["id"]))
    first = await _press(world)
    assert _action(world)["reason"] in ("connecting", "in_call") and _action(world)["available"] is True
    world.store._last_run.clear()
    second = await _press(world)
    assert second == {"call_id": first["call_id"], "hung_up": True}
    assert len(_fake(world).calls_to("dial")) == 1 and len(_fake(world).calls_to("hangup")) == 1


async def test_only_the_phone_itself_may_press_and_a_dashboard_login_is_refused(world):
    _two_gates(world)
    el = await _talk(world, door="gate-srv")
    await _save(world, _ref_doc(el["id"]))
    phone = world.store._endpoint_by_id(PHONE_ID)
    with pytest.raises(RunRefused) as err:
        await world.store.async_run(phone.ha_user_id, "S", "talk-el", caller_user_id=world.plain.id, endpoint_id=PHONE_ID)
    assert err.value.code == "not_allowed"
    assert _fake(world).calls_to("dial") == []


CASES = {
    # name: (door setup, element door, wire reason, run code, run message)
    "removed": (lambda f: f.remove_door("gate-1"), "gate-1", "no_door", "no_door", "Door removed"),
    "not connected": (lambda f: f.add_door("g", driver=SRV, capabilities={"CanDial": True}, available=False), "g",
                      "not_connected", "not_connected", "The gate is not connected"),
    "busy": (lambda f: (f.add_door("g", driver=SRV, capabilities={"CanDial": True}), f.ring("g", "visitor-1")), "g",
             "busy", "busy", "The gate is busy"),
    "dial off at the door": (lambda f: f.add_door("g", driver=OBS, capabilities={}), "g",
                             "dial_off", "not_available", "Dialing is switched off at the door"),
    "a gate that is down and dial off": (lambda f: f.add_door("g", driver=OBS, capabilities={}, available=False), "g",
                                         "not_connected", "not_connected", "The gate is not connected"),
    "no dial capability": (lambda f: f.add_door("g", driver="ha_event", capabilities={}), "g",
                           "no_dial", "not_available", "This door cannot be called"),
    "any other driver without CanDial": (lambda f: f.add_door("g", driver="dahua_sip", capabilities={}), "g",
                                         "no_dial", "not_available", "This door cannot be called"),
}


@pytest.mark.parametrize("name", sorted(CASES))
async def test_each_case_has_its_own_reason_and_text_and_dials_nothing(world, name):
    setup, door, reason, code, message = CASES[name]
    el = await _talk(world, door="gate-1")
    if door != "gate-1":
        setup(_fake(world))
        elements = get_elements_store(world.hass)
        el = (await elements.async_set({**el, "options": {"door": door}}, elements.rev))["element"]
    await _save(world, _ref_doc(el["id"]))
    if door == "gate-1":
        setup(_fake(world))
    action = _action(world)
    assert (action["available"], action["reason"]) == (False, reason)
    assert await _refusal(world) == (code, message)
    assert _fake(world).calls_to("dial") == []


async def test_a_removed_door_keeps_the_element_and_the_tile(world):
    el = await _talk(world)
    await _save(world, _ref_doc(el["id"]))
    _fake(world).remove_door("gate-1")
    action = _action(world)                              # the tile is still there, off, and says why
    assert action["door"] == "gate-1" and action["reason"] == "no_door"
    elements = get_elements_store(world.hass)
    assert [e["id"] for e in (await elements.async_list())["elements"]] == [el["id"]]


# ---- the per-block Talk is unchanged -------------------------------------------------------------------------------

async def test_the_block_talk_with_no_candial_still_says_talk_off_not_the_new_reasons(world):
    _fake(world).add_door("gate-1", driver="ha_event", capabilities={}, camera_entity_id="camera.gate")
    block = _entrance()
    block["door"] = "fake:gate-1"
    await _save(world, _doc4(block))
    page = world.store.resolve_document(PHONE_ID)["pages"][0]
    (talk,) = [a for b in page["blocks"] for a in b["actions"] if a["kind"] == "talk"]
    assert talk["reason"] == "talk_off"
