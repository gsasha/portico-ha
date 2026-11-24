"""Group call elements, P2 (design portico_group_call_elements_design.md): a Talk element with `options.group`
(everyone / phones / panels) stores, expands to the existing `call` action with the group target, can be placed only on a
screen's page and never on a phone's page, a Ring page or in a rule, and a hand-written call button still cannot name a group."""
from __future__ import annotations

import copy

import pytest

from ..call_endpoints import TARGET_TYPES
from ..content import ProfileError, _call_target
from ..content_elements import ElementRefError, check_refs, expand_action, expand_document, is_group_element
from ..elements import ElementError, KIND_SPECS, ElementStore, get_elements_store, validate_element
from .test_content_wire_blocks import _doc4, _save
from .test_editor_commands import PHONE_ID, world  # noqa: F401 - fixtures by name
from .test_push_registry import _custom_integrations  # noqa: F401 - autouse fixture by name

pytestmark = pytest.mark.asyncio

GROUPS = ("everyone", "phones", "panels")


def _el(group="panels", eid="el-aaaaaaaa", name="Call all panels", **extra):
    return {"id": eid, "kind": "talk", "name": name, "icon": "phone", "options": {"group": group}, **extra}


# ---- the validator ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("group", GROUPS)
async def test_a_group_talk_element_validates_with_the_default_icon_and_no_other_option(group):
    clean = validate_element({"kind": "talk", "name": "Call " + group, "options": {"group": group}})
    assert clean["options"] == {"group": group} and clean["icon"] == "phone" and "entity_id" not in clean
    assert is_group_element(clean)


@pytest.mark.parametrize("options,field", [
    ({"group": "everybody"}, "options.group"), ({"group": ""}, "options.group"), ({"group": None}, "options.group"),
    ({"group": ["everyone"]}, "options.group"), ({"group": "everyone", "door": "gate-1"}, "options"),
    ({"group": "phones", "phone": "p"}, "options"), ({"group": "panels", "screen": "s"}, "options"),
    ({"group": "everyone", "extra": "x"}, "options")])
async def test_a_bad_group_is_invalid_with_its_field(options, field):
    with pytest.raises(ElementError) as err:
        validate_element({"kind": "talk", "name": "T", "options": options})
    assert err.value.code == "invalid" and err.value.field == field


async def test_the_id_regex_and_schema_are_unchanged_and_a_group_is_never_managed():
    assert validate_element(_el())["id"] == "el-aaaaaaaa"
    with pytest.raises(ElementError):
        validate_element(_el(eid="talk-phone-x"))
    with pytest.raises(ElementError):
        validate_element({**_el(eid="talk-phone-x"), "managed": True}, managed_ok=True)


async def test_the_kinds_description_offers_the_group_field_for_the_form():
    fields = [n for n, _t, _r in KIND_SPECS["talk"].fields]
    assert fields == ["door", "screen", "phone", "group"]


# ---- expansion ----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("group", GROUPS)
async def test_a_group_element_expands_to_the_existing_call_action_with_the_group_target(group):
    out = expand_action({"id": "a1", "element": "el-aaaaaaaa"}, _el(group))
    assert out == {"id": "a1", "label": "Call all panels", "kind": "call", "target": {"type": "group", "id": group},
                   "icon": "phone", "confirm": False}


async def test_a_reference_cannot_ask_for_a_confirm_and_may_rename_and_hide_the_label():
    out = expand_action({"id": "a1", "element": "x", "label": "Panels", "show_label": False, "confirm": True}, _el())
    assert out["confirm"] is False and out["label"] == "Panels" and out["show_label"] is False


async def test_a_stored_bad_group_word_does_not_expand():
    assert expand_action({"id": "a1", "element": "x"}, {"kind": "talk", "name": "T", "options": {"group": "nobody"}}) is None


async def test_a_hand_written_call_button_still_cannot_name_a_group():
    assert "group" not in TARGET_TYPES
    with pytest.raises(ProfileError):
        _call_target({"type": "group", "id": "everyone"}, "w")
    button = {"kind": "button", "name": "B", "options": {"action": "call", "target": "group:everyone"}}
    assert expand_action({"id": "a1", "element": "x"}, button) is None


# ---- placement: screens only --------------------------------------------------------------------------------------------

def _doc6(*, devices, dashboards, blocks=None):
    blocks = blocks or [{"id": "b", "title": "Calls", "cameras": [], "actions": [{"id": "g1", "element": "el-aaaaaaaa"}]}]
    return {"schema_version": 6, "pages": [{"id": "home", "label": "Home", "blocks": blocks}], "devices": devices,
            "dashboards": dashboards, "endpoints": {}}


ELEMENTS = {"el-aaaaaaaa": _el()}


async def test_a_screen_page_may_hold_a_group_and_it_expands_there():
    doc = _doc6(devices={"kitchen": {"pages": [{"page": "home"}]}}, dashboards={"kitchen": {"name": "Kitchen"}})
    check_refs(doc, ELEMENTS)
    out = expand_document(doc, ELEMENTS)
    (action,) = out["pages"][0]["blocks"][0]["actions"]
    assert action["kind"] == "call" and action["target"] == {"type": "group", "id": "panels"}


async def test_a_phones_page_may_not_hold_a_group():
    doc = _doc6(devices={"ep-alice": {"pages": [{"page": "home"}]}}, dashboards={"kitchen": {"name": "Kitchen"}})
    with pytest.raises(ElementRefError) as err:
        check_refs(doc, ELEMENTS)
    assert err.value.code == "not_placeable" and "screen" in str(err.value)


async def test_a_page_shared_by_a_screen_and_a_phone_is_refused_for_the_phone():
    doc = _doc6(devices={"kitchen": {"pages": [{"page": "home"}]}, "ep-alice": {"pages": [{"page": "home"}]}},
                dashboards={"kitchen": {"name": "Kitchen"}})
    with pytest.raises(ElementRefError) as err:
        check_refs(doc, ELEMENTS)
    assert err.value.code == "not_placeable" and "ep-alice" in str(err.value)


async def test_an_unassigned_library_page_is_not_refused_until_a_phone_gets_it():
    check_refs(_doc6(devices={}, dashboards={}), ELEMENTS)


async def test_a_legacy_template_reaches_phones_so_it_may_not_hold_a_group_but_a_screens_override_may():
    block = {"id": "b", "title": "Calls", "cameras": [], "actions": [{"id": "g1", "element": "el-aaaaaaaa"}]}
    page = {"id": "home", "label": "Home", "blocks": [block]}
    legacy = {"schema_version": 4, "template": {"pages": [page], "rules": []}, "overrides": {}, "dashboards": {}, "endpoints": {}}
    with pytest.raises(ElementRefError) as err:
        check_refs(legacy, ELEMENTS)
    assert err.value.code == "not_placeable"
    ok = copy.deepcopy(legacy)
    ok["template"]["pages"] = []
    ok["overrides"] = {"kitchen": {"pages": [page], "rules": []}}
    ok["dashboards"] = {"kitchen": {"name": "Kitchen"}}
    check_refs(ok, ELEMENTS)
    phone = copy.deepcopy(ok)
    phone["overrides"] = {"ep-alice": {"pages": [page], "rules": []}}
    with pytest.raises(ElementRefError):
        check_refs(phone, ELEMENTS)


async def test_a_ring_page_cannot_hold_a_group_as_its_talk_or_as_a_button():
    ring = {"id": "ring1", "label": "Ring", "kind": "ring", "talk": "el-aaaaaaaa", "cameras": []}
    doc = {"schema_version": 6, "pages": [ring], "devices": {}, "dashboards": {}, "endpoints": {}}
    with pytest.raises(ElementRefError) as err:
        check_refs(doc, ELEMENTS)
    assert err.value.code == "element_kind"


async def test_a_rule_may_not_name_a_group_button():
    block = {"id": "b", "title": "Calls", "cameras": [], "actions": [{"id": "g1", "element": "el-aaaaaaaa"}]}
    doc = {"schema_version": 4, "template": {"pages": [], "rules": []}, "dashboards": {"kitchen": {"name": "K"}},
           "overrides": {"kitchen": {"pages": [{"id": "home", "label": "H", "blocks": [block]}],
                                     "rules": [{"id": "r1", "ring_buttons": ["g1"]}]}}, "endpoints": {}}
    with pytest.raises(ElementRefError) as err:
        check_refs(doc, ELEMENTS)
    assert err.value.code == "element_kind"


async def test_changing_the_group_of_a_used_element_is_a_retarget_needing_confirm():
    store = ElementStore.__new__(ElementStore)
    store.used_by = lambda _id: [{"endpoint": "kitchen", "label": "Home"}]
    old, new = _el("phones"), _el("panels")
    with pytest.raises(ElementError) as err:
        store._guard_used(old, new)
    assert err.value.code == "in_use_retarget"
    store._guard_used(old, new, confirm=True)


# ---- the store, end to end ------------------------------------------------------------------------------------------------

async def _make(world, group="panels", name="Call all panels"):
    elements = get_elements_store(world.hass)
    await elements.async_ensure_loaded()
    return (await elements.async_set({"kind": "talk", "name": name, "options": {"group": group}}, elements.rev))["element"]


async def test_the_store_creates_the_three_kinds_with_server_ids_default_icon_and_unique_names(world):
    made = [await _make(world, g, f"Call {g}") for g in GROUPS]
    assert [m["options"] for m in made] == [{"group": g} for g in GROUPS]
    assert all(m["id"].startswith("el-") and m["icon"] == "phone" and "managed" not in m for m in made)
    elements = get_elements_store(world.hass)
    with pytest.raises(ElementError) as err:                          # the name is unique, case-insensitively
        await elements.async_set({"kind": "talk", "name": "CALL EVERYONE", "options": {"group": "everyone"}}, elements.rev)
    assert err.value.code in ("duplicate_name", "invalid", "name_taken") or err.value.field == "name"


async def test_a_group_is_not_checked_against_live_phones_and_never_shows_removed(world):
    el = await _make(world, "phones")
    from ..elements import talk_endpoint_removed
    assert talk_endpoint_removed(world.hass, el) is False


async def test_the_save_refuses_a_phones_page_and_a_template_but_keeps_a_screens(world):
    el = await _make(world)
    block = {"id": "calls", "title": "Calls", "cameras": [], "actions": [{"id": "g1", "element": el["id"]}]}
    # an older document's template reaches phones: refused with the same words the editor uses
    with pytest.raises(ProfileError, match="not_placeable.*only on a screen's page, not on a phone's"):
        await _save(world, _doc4(block))
    # an override of the phone: refused
    page = {"id": "home", "label": "Home", "blocks": [block]}
    phone_doc = _doc4(overrides={PHONE_ID: {"pages": [page], "rules": []}})
    phone_doc["template"]["pages"] = []
    with pytest.raises(ProfileError, match="not_placeable"):
        await _save(world, phone_doc)


async def test_a_press_by_a_phone_of_a_group_action_is_refused_forbidden(world):
    """Even if a stale document held one (HC-9 stays): the phone's `content/run` places a `group` target and is refused."""
    from ..content import RunRefused
    from ..house import async_setup_house
    async_setup_house(world.hass, enabled=True)
    store = world.store
    action = {"id": "g1", "label": "Call all", "kind": "call", "target": {"type": "group", "id": "everyone"}, "confirm": False}
    phone = store._endpoint_by_id(PHONE_ID)
    with pytest.raises(RunRefused) as err:
        await store._run_call(phone.ha_user_id, "S", action, 0.0, (PHONE_ID, "g1"))
    assert err.value.code in ("forbidden", "not_callable")
