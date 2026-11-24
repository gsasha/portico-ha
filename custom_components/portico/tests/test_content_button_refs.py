"""DEV-760 placements P3: BUTTON references in the content document.

Safety is the point: a button can open a door or the gate. What these tests pin: the wire is identical to an inline button
(incl. `rev` and the grants); a reference can ask first MORE but never less than the action's default; the element's CURRENT
action/entity is read at every run, with the caller's HA user; the grant (the button is on a page the caller may use) is
re-checked at every run; rule buttons stay page-button ids. Services are mocked (`async_mock_service`): nothing real is called.
"""
from __future__ import annotations

import copy
import json

import pytest
from pytest_homeassistant_custom_component.common import async_mock_service

from ..content import KINDS, ProfileError, RunRefused, get_content_store, normalize_page
from ..content_elements import (ElementRefError, check_refs, expand_action, expand_document, has_refs, stored_ref_keys,
                                usage_index)
from ..content_resolve import resolve_endpoint
from ..elements import ElementError, get_elements_store
from .test_content_rules import GATE_DEVICE, REGISTRY, STATES, _doc, _rule
from .test_push_registry import _custom_integrations  # noqa: F401 - autouse fixture by name

pytestmark = pytest.mark.asyncio

B_OPEN, B_UNLOCK, B_COVER, B_DOOR, B_BLOCKDOOR, B_CALL, B_TALK, B_SCRIPT, B_LOCK = (f"el-1000000{n}" for n in range(1, 10))
DOOR = "portico_intercom:http://10.6.0.7:8091"


def _btn(element_id, name, action, entity=None, **options):
    out = {"id": element_id, "kind": "button", "name": name, "options": {"action": action, **options}}
    if entity:
        out["entity_id"] = entity
    return out


ELEMENTS = {e["id"]: e for e in (
    {**_btn(B_OPEN, "Open gate", "lock_open", "lock.gate", color="red"), "icon": "lock-open"},
    _btn(B_UNLOCK, "Unlock", "lock_unlock", "lock.front"),
    _btn(B_COVER, "Gate cover", "cover_open", "cover.gate"),
    _btn(B_DOOR, "Door", "portico_open", target=GATE_DEVICE),
    _btn(B_BLOCKDOOR, "Block door", "portico_open"),
    _btn(B_CALL, "Call kitchen", "call", target="dashboard:kitchen"),
    _btn(B_TALK, "Talk", "talk"),
    {**_btn(B_SCRIPT, "Lamp", "script_run", "script.lamp"), "icon": "lightbulb"},
    _btn(B_LOCK, "Lock", "lock_lock", "lock.front"),
)}


def _ref(item_id, element, **extra):
    return {"id": item_id, "element": element, **extra}


def _blocks_inline():
    return [{"id": "main", "title": "Main", "door": DOOR, "cameras": [], "actions": [
        {"id": "open", "label": "Open gate", "kind": "lock_open", "entity_id": "lock.gate", "icon": "lock-open",
         "color": "red", "confirm": True},
        {"id": "open2", "label": "Gate again", "kind": "lock_open", "entity_id": "lock.gate", "icon": "lock-open",
         "color": "red", "confirm": True, "show_label": False},
        {"id": "unlock", "label": "Unlock", "kind": "lock_unlock", "entity_id": "lock.front", "confirm": True},
        {"id": "cover", "label": "Gate cover", "kind": "cover_open", "entity_id": "cover.gate", "confirm": True},
        {"id": "door", "label": "Door", "kind": "portico_open", "device_id": GATE_DEVICE, "confirm": True},
        {"id": "bdoor", "label": "Block door", "kind": "portico_open", "confirm": True},
        {"id": "call", "label": "Call kitchen", "kind": "call", "target": {"type": "dashboard", "id": "kitchen"},
         "confirm": False},
        {"id": "talk", "label": "Talk", "kind": "talk", "confirm": False},
        {"id": "lamp", "label": "Lamp", "kind": "script_run", "entity_id": "script.lamp", "icon": "lightbulb",
         "confirm": True},                                    # tightened: ask-first on a harmless action is allowed
        {"id": "lock", "label": "Lock", "kind": "lock_lock", "entity_id": "lock.front", "confirm": False},
    ]}]


def _blocks_refs():
    return [{"id": "main", "title": "Main", "door": DOOR, "cameras": [], "actions": [
        _ref("open", B_OPEN),
        _ref("open2", B_OPEN, label="Gate again", show_label=False),
        _ref("unlock", B_UNLOCK), _ref("cover", B_COVER), _ref("door", B_DOOR), _ref("bdoor", B_BLOCKDOOR),
        _ref("call", B_CALL), _ref("talk", B_TALK), _ref("lamp", B_SCRIPT, confirm=True), _ref("lock", B_LOCK)]}]


def _pair():
    inline = _doc()
    inline["template"]["pages"][0]["blocks"] = _blocks_inline()
    inline["template"]["rules"] = [_rule("r1", "kind:door", ring=["open", "door"], call=["unlock"])]
    refs = copy.deepcopy(inline)
    refs["template"]["pages"][0]["blocks"] = _blocks_refs()
    refs["schema_version"] = 5
    return inline, refs


def _dump(x):
    return json.dumps(x, sort_keys=True)


# ---- the same wire ---------------------------------------------------------------------------------------------------

async def test_button_references_resolve_to_the_very_same_wire_incl_rev_for_lock_gate_cover_door_call_talk():
    inline, refs = _pair()
    assert has_refs(refs) and not has_refs(inline)
    expanded = expand_document(refs, ELEMENTS)
    assert not has_refs(expanded)
    for endpoint in ("alice", "dina"):
        a = resolve_endpoint(inline, endpoint, states=STATES, registry=REGISTRY)
        b = resolve_endpoint(expanded, endpoint, states=STATES, registry=REGISTRY)
        assert _dump(a) == _dump(b), endpoint
        assert a["rev"] == b["rev"]
    actions = {x["id"]: x for x in resolve_endpoint(expanded, "alice", states=STATES,
                                                    registry=REGISTRY)["pages"][0]["blocks"][0]["actions"]}
    assert "element" not in json.dumps(actions)                          # the reference key never reaches the wire
    assert actions["bdoor"]["device_id"] == DOOR                         # the block's door, materialised as today


async def test_a_reference_has_no_entity_in_the_stored_document_and_the_rule_buttons_see_the_same_ids():
    inline, refs = _pair()
    stored = refs["template"]["pages"][0]["blocks"][0]["actions"][0]
    assert stored == {"id": "open", "element": B_OPEN}
    assert expand_document(refs, ELEMENTS)["template"]["rules"] == inline["template"]["rules"]


# ---- the two-tap rule: a reference never asks LESS than the action's default ---------------------------------------

GUARDED = [("lock_open", "lock.a"), ("lock_unlock", "lock.a"), ("cover_open", "cover.a"), ("portico_open", None)]


@pytest.mark.parametrize("action,entity", GUARDED)
async def test_a_door_lock_or_gate_reference_asks_first_whatever_the_reference_says(action, entity):
    element = _btn("el-20000001", "X", action, entity, **({"target": "dev"} if action == "portico_open" else {}))
    for extra in ({}, {"confirm": False}, {"confirm": True}):
        out = expand_action(_ref("x", element["id"], **extra), element, "door")
        assert out["confirm"] is True, (action, extra)


@pytest.mark.parametrize("action,entity", [(a, e) for a, e in (
    ("lock_lock", "lock.a"), ("cover_close", "cover.a"), ("button_press", "button.a"), ("script_run", "script.a"),
    ("scene_activate", "scene.a"))])
async def test_a_harmless_action_can_be_tightened_but_never_loosened_below_its_default(action, entity):
    element = _btn("el-20000002", "X", action, entity)
    assert KINDS[action][2] is False
    assert expand_action(_ref("x", element["id"]), element)["confirm"] is False
    assert expand_action(_ref("x", element["id"], confirm=True), element)["confirm"] is True
    assert expand_action(_ref("x", element["id"], confirm=False), element)["confirm"] is False


async def test_call_and_talk_are_never_asked_whatever_the_reference_says():
    for eid in (B_CALL, B_TALK):
        assert expand_action(_ref("x", eid, confirm=True), ELEMENTS[eid], "door")["confirm"] is False


async def test_the_element_changing_its_action_after_the_fact_cannot_leave_an_unconfirmed_door_opener():
    """Belt and braces for the guard in elements.py (a used button's action cannot change): even if the stored reference
    carried a stale `confirm: false`, expanding it against a lock_open element asks first."""
    ref = _ref("x", "el-30000001", confirm=False)
    as_script = _btn("el-30000001", "X", "script_run", "script.a")
    as_lock = _btn("el-30000001", "X", "lock_open", "lock.a")
    assert expand_action(ref, as_script)["confirm"] is False
    assert expand_action(ref, as_lock)["confirm"] is True


async def test_the_save_refuses_a_reference_that_turns_ask_first_off_on_a_door_lock_or_gate_action():
    for eid in (B_OPEN, B_UNLOCK, B_COVER, B_DOOR):
        doc = {"template": {"pages": [{"id": "home", "label": "Home", "blocks": [
            {"id": "b", "title": "B", "door": DOOR, "cameras": [], "actions": [_ref("x", eid, confirm=False)]}]}]}}
        with pytest.raises(ElementRefError) as err:
            check_refs(doc, ELEMENTS)
        assert err.value.code == "confirm_weaker" and "x" in str(err.value)
    ok = {"template": {"pages": [{"id": "home", "label": "Home", "blocks": [
        {"id": "b", "title": "B", "door": DOOR, "cameras": [], "actions": [
            _ref("x", B_LOCK, confirm=True), _ref("y", B_SCRIPT, confirm=False), _ref("z", B_OPEN, confirm=True)]}]}]}}
    check_refs(ok, ELEMENTS)


# ---- placement rules --------------------------------------------------------------------------------------------------

async def _check(actions, door=DOOR, stored=frozenset(), known=None):
    doc = {"template": {"pages": [{"id": "home", "label": "Home", "blocks": [
        {"id": "b", "title": "B", **({"door": door} if door else {}), "cameras": [], "actions": actions}]}]}}
    check_refs(doc, ELEMENTS, stored_ids=stored, known_target=known)


async def test_which_elements_can_be_placed_where():
    await _check([_ref("a", B_OPEN), _ref("b", B_CALL), _ref("c", B_TALK), _ref("d", B_BLOCKDOOR)], known=lambda t, i: True)
    with pytest.raises(ElementRefError) as err:
        await _check([_ref("a", "el-ffffffff")])
    assert err.value.code == "element_missing"
    with pytest.raises(ElementRefError) as err:
        await _check([_ref("a", "el-00000001")]) if False else check_refs(
            {"template": {"pages": [{"id": "h", "label": "H", "blocks": [{"id": "b", "title": "B", "cameras": [], "actions": [
                _ref("a", "el-40000001")]}]}]}}, {"el-40000001": {"id": "el-40000001", "kind": "camera", "name": "C",
                                                                   "entity_id": "camera.a", "options": {}}})
    assert err.value.code == "element_kind"
    with pytest.raises(ElementRefError) as err:
        await _check([_ref("a", B_BLOCKDOOR)], door=None)           # no target and no door: nothing to open
    assert err.value.code == "not_placeable"
    with pytest.raises(ElementRefError) as err:
        await _check([_ref("a", B_TALK)], door=None)
    assert err.value.code == "not_placeable"
    with pytest.raises(ElementRefError) as err:
        await _check([_ref("a", B_CALL)], known=lambda t, i: False)
    assert err.value.code == "not_placeable"
    with pytest.raises(ElementRefError) as err:
        await _check([_ref("a", B_UNLOCK, show_label=False)])        # hidden text needs an icon the element does not have
    assert err.value.code == "show_label_icon"
    await _check([_ref("a", B_OPEN, show_label=False)])             # this one has an icon


async def test_a_rule_cannot_name_a_reference_to_a_talk_button():
    doc = {"template": {"pages": [{"id": "home", "label": "Home", "blocks": [
        {"id": "b", "title": "B", "door": DOOR, "cameras": [], "actions": [_ref("t", B_TALK)]}]}],
        "rules": [_rule("r", "kind:door", ring=["t"])]}}
    with pytest.raises(ElementRefError, match="talk"):
        check_refs(doc, ELEMENTS)


async def test_a_dangling_stored_reference_is_dropped_from_the_wire_and_the_grant_not_kept_as_a_button():
    inline, refs = _pair()
    expanded = expand_document(refs, {k: v for k, v in ELEMENTS.items() if k != B_OPEN})
    ids = [a["id"] for a in expanded["template"]["pages"][0]["blocks"][0]["actions"]]
    assert "open" not in ids and "open2" not in ids and "door" in ids


async def test_a_button_whose_element_becomes_unbuildable_is_dropped_never_run_half_built():
    bad = {**ELEMENTS[B_OPEN], "entity_id": "switch.not_a_lock"}              # wrong domain for lock_open
    assert expand_action(_ref("x", B_OPEN), bad) is None
    assert expand_action(_ref("x", B_OPEN), {**bad, "kind": "camera"}) is None
    assert expand_action(_ref("x", B_BLOCKDOOR), ELEMENTS[B_BLOCKDOOR], None) is None
    assert expand_action(_ref("x", B_CALL), _btn("el-5", "c", "call", target="nonsense")) is None


async def test_usage_index_lists_button_refs():
    _inline, refs = _pair()
    users = usage_index(refs)
    assert [u["block"] for u in users[B_OPEN]] == ["main", "main"] and users[B_OPEN][0]["page"] == "home"


async def test_normalize_keeps_only_id_element_and_the_three_overrides_and_checks_their_types():
    page = normalize_page({"id": "home", "label": "Home", "blocks": [{"id": "b", "title": "B", "cameras": [], "actions": [
        {"id": "a", "element": B_OPEN, "entity_id": "lock.evil", "kind": "script_run", "icon": "x", "color": "red",
         "label": " Hi ", "show_label": False, "confirm": True}]}]})
    assert page["blocks"][0]["actions"][0] == {"id": "a", "element": B_OPEN, "label": "Hi", "show_label": False,
                                                "confirm": True}
    for bad in ({"element": "nonsense"}, {"element": B_OPEN, "confirm": "yes"}, {"element": B_OPEN, "show_label": 1},
                {"element": B_OPEN, "label": ""}):
        with pytest.raises(ProfileError):
            normalize_page({"id": "home", "label": "Home", "blocks": [{"id": "b", "title": "B", "cameras": [], "actions": [
                {"id": "a", **bad}]}]})


# ---- the stores and the run path --------------------------------------------------------------------------------------

async def _world(hass, hass_storage, hass_admin_user):
    elements = get_elements_store(hass)
    await elements.async_ensure_loaded()
    ids = {}
    for key, raw in (("open", {"kind": "button", "name": "Open gate", "entity_id": "lock.gate", "icon": "lock-open",
                               "options": {"action": "lock_open"}}),
                     ("lamp", {"kind": "button", "name": "Lamp", "entity_id": "script.lamp",
                               "options": {"action": "script_run"}})):
        ids[key] = (await elements.async_set(raw, elements.rev))["element"]["id"]
    store = get_content_store(hass)
    await store.async_ensure_loaded()
    doc = copy.deepcopy(store.document())
    doc["overrides"]["u1"] = {"pages": [{"id": "home", "label": "Home", "blocks": [{"id": "b", "title": "B", "cameras": [],
                                                                                   "actions": [
        {"id": "gate", "element": ids["open"]}, {"id": "lamp", "element": ids["lamp"]}]}]}],
        "rules": [], "roster_hide": []}
    await store.async_save_document(doc, store.doc_rev)
    for entity in ("lock.gate", "lock.other", "script.lamp"):
        hass.states.async_set(entity, "on")
    return store, elements, ids


async def test_a_referenced_lock_open_still_needs_the_second_tap_and_runs_as_the_caller(hass, hass_storage, hass_admin_user):
    store, _elements, _ids = await _world(hass, hass_storage, hass_admin_user)
    opens = async_mock_service(hass, "lock", "open")
    resolved = store.resolve_document("u1")["pages"][0]["blocks"][0]["actions"]
    gate = next(a for a in resolved if a["id"] == "gate")
    assert gate["kind"] == "lock_open" and gate["confirm"] is True            # the wire says: ask before running
    assert next(a for a in resolved if a["id"] == "lamp")["confirm"] is False
    assert "entity_id" in gate and "element" not in gate
    await store.async_run("u1", "Phone", "gate", caller_user_id="the-caller")
    assert len(opens) == 1 and opens[0].data == {"entity_id": "lock.gate"} and opens[0].context.user_id == "the-caller"


async def test_an_element_edit_of_the_entity_takes_effect_on_the_very_next_run(hass, hass_storage, hass_admin_user):
    store, elements, ids = await _world(hass, hass_storage, hass_admin_user)
    opens = async_mock_service(hass, "lock", "open")
    await store.async_run("u1", "Phone", "gate")
    store._last_run.clear()
    element = copy.deepcopy(elements.elements_by_id()[ids["open"]])
    element["entity_id"] = "lock.other"
    with pytest.raises(ElementError) as err:
        await elements.async_set(element, elements.rev)
    assert err.value.code == "in_use_retarget" and err.value.used_by
    await elements.async_set(element, elements.rev, confirm=True)
    await store.async_run("u1", "Phone", "gate")
    assert [c.data["entity_id"] for c in opens] == ["lock.gate", "lock.other"]


async def test_changing_the_action_of_a_used_button_and_deleting_it_are_refused(hass, hass_storage, hass_admin_user):
    store, elements, ids = await _world(hass, hass_storage, hass_admin_user)
    element = copy.deepcopy(elements.elements_by_id()[ids["lamp"]])
    element.update(entity_id="lock.gate", options={"action": "lock_open"})
    with pytest.raises(ElementError) as err:
        await elements.async_set(element, elements.rev)
    assert err.value.code == "in_use_action"
    with pytest.raises(ElementError) as err:
        await elements.async_delete(ids["lamp"], elements.rev)
    assert err.value.code == "in_use" and err.value.used_by


async def test_the_grant_is_rechecked_at_every_run_a_button_taken_off_the_page_cannot_run(hass, hass_storage,
                                                                                         hass_admin_user):
    store, _elements, _ids = await _world(hass, hass_storage, hass_admin_user)
    opens = async_mock_service(hass, "lock", "open")
    doc = copy.deepcopy(store.document())
    doc["overrides"]["u1"]["pages"][0]["blocks"][0]["actions"] = [a for a in doc["overrides"]["u1"]["pages"][0][
        "blocks"][0]["actions"] if a["id"] != "gate"]
    await store.async_save_document(doc, store.doc_rev)
    with pytest.raises(RunRefused) as err:
        await store.async_run("u1", "Phone", "gate")
    assert err.value.code == "unknown_action" and opens == []
    with pytest.raises(RunRefused) as err:                      # nor does the element id work as an action id
        await store.async_run("u1", "Phone", _ids["open"])
    assert err.value.code == "unknown_action" and opens == []


async def test_an_element_that_vanished_or_broke_cannot_run_and_calls_nothing(hass, hass_storage, hass_admin_user):
    store, elements, ids = await _world(hass, hass_storage, hass_admin_user)
    opens = async_mock_service(hass, "lock", "open")
    # an element the file still names but the store lost (a hand edit): the button is simply not in the grant
    elements._elements = [e for e in elements._elements if e["id"] != ids["open"]]
    with pytest.raises(RunRefused) as err:
        await store.async_run("u1", "Phone", "gate")
    assert err.value.code == "unknown_action" and opens == []


async def test_the_save_checks_buttons_against_the_live_elements_and_stamps_5(hass, hass_storage, hass_admin_user):
    store, _elements, ids = await _world(hass, hass_storage, hass_admin_user)
    base = copy.deepcopy(store.document())
    page = base["overrides"]["u1"]["pages"][0]["blocks"][0]["actions"]
    weaker = copy.deepcopy(base)
    weaker["overrides"]["u1"]["pages"][0]["blocks"][0]["actions"].append({"id": "gate2", "element": ids["open"], "confirm": False})
    with pytest.raises(ProfileError, match=r"confirm_weaker.*gate2"):
        await store.async_save_document(weaker, store.doc_rev)
    with pytest.raises(ProfileError, match="element_missing"):
        store.prepare_document({**base, "overrides": {"u1": {**base["overrides"]["u1"], "pages": [
            {**base["overrides"]["u1"]["pages"][0], "blocks": [{"id": "b", "title": "B", "cameras": [], "actions": [
                {"id": "z", "element": "el-deadbeef"}]}]}]}}})
    tightened = copy.deepcopy(base)
    tightened["overrides"]["u1"]["pages"][0]["blocks"][0]["actions"][1]["confirm"] = True
    await store.async_save_document(tightened, store.doc_rev)
    lamp = next(a for a in store.resolve_document("u1")["pages"][0]["blocks"][0]["actions"] if a["id"] == "lamp")
    assert lamp["confirm"] is True and store.document()["schema_version"] == 5 and page


async def test_an_old_inline_button_still_saves_and_runs_untouched(hass, hass_storage, hass_admin_user):
    store, _elements, _ids = await _world(hass, hass_storage, hass_admin_user)
    doc = copy.deepcopy(store.document())
    doc["overrides"]["u1"]["pages"][0]["blocks"][0]["actions"].append(
        {"id": "inl", "label": "Inline", "kind": "lock_open", "entity_id": "lock.gate", "confirm": True})
    await store.async_save_document(doc, store.doc_rev)
    opens = async_mock_service(hass, "lock", "open")
    await store.async_run("u1", "Phone", "inl")
    assert len(opens) == 1


# ---- review round: ids shared across pages, stuck documents, per-reference bypass, lenient load ------------------------------

def _page_with(page_id, actions):
    return {"id": page_id, "label": page_id.title(), "blocks": [{"id": "b", "title": "B", "cameras": [], "actions": actions}]}


async def test_two_different_buttons_sharing_an_id_across_pages_run_nothing_not_the_first(hass, hass_storage, hass_admin_user):
    """The scenario of the review: page 'home' has a lock_open `open` (asks first); another page has a reference to a harmless
    script with the SAME id. The phone may show the script; a press names only the id. Before the fix the first (the lock)
    ran; now neither does."""
    store, _elements, ids = await _world(hass, hass_storage, hass_admin_user)
    opens = async_mock_service(hass, "lock", "open")
    scripts = async_mock_service(hass, "script", "turn_on")
    doc = copy.deepcopy(store.document())
    pages = doc["overrides"]["u1"]["pages"]
    pages[0]["blocks"][0]["actions"] = [{"id": "open", "element": ids["open"]}]
    pages.append(_page_with("second", [{"id": "open", "element": ids["lamp"]}]))
    with pytest.raises(ProfileError, match="used on two pages"):
        await store.async_save_document(doc, store.doc_rev)          # a NEW duplicate cannot be saved
    # a document that already holds one (an old file) still loads: put it in memory the way a load does
    store._overrides["u1"]["pages"] = [normalize_page(p, strict=False) for p in pages]
    with pytest.raises(RunRefused) as err:
        await store.async_run("u1", "Phone", "open")
    assert err.value.code == "unknown_action" and opens == [] and scripts == []
    # the resolver does hand out both, and the FIRST match is the lock: that is what the old grant would have run
    assert [a["kind"] for a in store._all_actions("u1") if a["id"] == "open"] == ["lock_open", "script_run"]


async def test_the_same_button_listed_twice_with_one_id_still_runs(hass, hass_storage, hass_admin_user):
    store, _elements, ids = await _world(hass, hass_storage, hass_admin_user)
    opens = async_mock_service(hass, "lock", "open")
    twin = {"id": "open", "element": ids["open"]}
    store._overrides["u1"]["pages"] = [normalize_page(_page_with("home", [twin]), strict=False),
                                       normalize_page(_page_with("second", [dict(twin)]), strict=False)]
    await store.async_run("u1", "Phone", "open")
    assert len(opens) == 1


async def test_a_stored_duplicate_id_loads_and_other_edits_still_save_but_no_new_duplicate_can_be_added(hass, hass_storage,
                                                                                                         hass_admin_user):
    store, _elements, ids = await _world(hass, hass_storage, hass_admin_user)
    pages = [normalize_page(_page_with("home", [{"id": "x", "element": ids["open"]}]), strict=False),
             normalize_page(_page_with("second", [{"id": "x", "element": ids["lamp"]}]), strict=False)]
    store._overrides["u1"]["pages"] = pages
    doc = copy.deepcopy(store.document())
    doc["home_name"] = "Elsewhere"
    await store.async_save_document(doc, store.doc_rev)             # the old duplicate is tolerated
    assert store.document()["home_name"] == "Elsewhere"
    worse = copy.deepcopy(store.document())
    worse["overrides"]["u1"]["pages"].append(_page_with("third", [{"id": "y", "element": ids["lamp"]}]))
    worse["overrides"]["u1"]["pages"][0]["blocks"][0]["actions"].append({"id": "y", "element": ids["lamp"]})
    with pytest.raises(ProfileError, match="'y'"):
        await store.async_save_document(worse, store.doc_rev)


async def test_a_stored_confirm_false_on_a_reference_that_became_a_door_does_not_stick_every_later_save(hass, hass_storage,
                                                                                                         hass_admin_user):
    store, _elements, ids = await _world(hass, hass_storage, hass_admin_user)
    store._overrides["u1"]["pages"][0]["blocks"][0]["actions"][0]["confirm"] = False     # stored weak; the element is lock_open
    doc = copy.deepcopy(store.document())
    doc["home_name"] = "Other edit"
    await store.async_save_document(doc, store.doc_rev)             # does not raise
    stored = store.document()["overrides"]["u1"]["pages"][0]["blocks"][0]["actions"][0]
    assert stored["confirm"] is True
    gate = next(a for a in store.resolve_document("u1")["pages"][0]["blocks"][0]["actions"] if a["id"] == "gate")
    assert gate["confirm"] is True
    # a NEW reference with confirm false is still refused
    fresh = copy.deepcopy(store.document())
    fresh["overrides"]["u1"]["pages"][0]["blocks"][0]["actions"].append({"id": "gate2", "element": ids["open"], "confirm": False})
    with pytest.raises(ProfileError, match="confirm_weaker"):
        await store.async_save_document(fresh, store.doc_rev)


async def test_the_stored_allowance_is_per_reference_a_new_placement_of_the_same_element_is_checked_in_full():
    doc_old = {"template": {"pages": [_page_with("home", [_ref("t", B_TALK)])]}}
    doc_old["template"]["pages"][0]["blocks"][0].pop("door", None)
    stored = stored_ref_keys(doc_old)
    assert stored
    check_refs(doc_old, ELEMENTS, stored_refs=stored)                       # the old one (no door) is kept
    new = copy.deepcopy(doc_old)
    new["template"]["pages"].append(_page_with("p2", [_ref("t2", B_TALK)]))  # a NEW placement, still no door
    with pytest.raises(ElementRefError) as err:
        check_refs(new, ELEMENTS, stored_refs=stored)
    assert err.value.code == "not_placeable"
    new2 = copy.deepcopy(doc_old)
    new2["template"]["pages"][0]["blocks"][0]["actions"].append(_ref("u", B_UNLOCK, show_label=False))
    with pytest.raises(ElementRefError) as err:
        check_refs(new2, ELEMENTS, stored_refs=stored)
    assert err.value.code == "show_label_icon"


async def test_a_malformed_stored_reference_drops_only_that_button_on_a_load():
    page = normalize_page(_page_with("home", [
        {"id": "ok", "label": "Ok", "kind": "script_run", "entity_id": "script.x", "confirm": False},
        {"id": "bad", "element": "nope"},
        {"id": "bad2", "element": B_OPEN, "confirm": "yes"},
        {"id": "good", "element": B_OPEN}]), strict=False)
    assert [a["id"] for a in page["blocks"][0]["actions"]] == ["ok", "good"]
    with pytest.raises(ProfileError):
        normalize_page(_page_with("home", [{"id": "bad", "element": "nope"}]))      # a save is still strict


async def test_a_call_element_may_name_a_screen_declared_in_the_same_document(hass, hass_storage, hass_admin_user):
    elements = get_elements_store(hass)
    await elements.async_ensure_loaded()
    call = (await elements.async_set({"kind": "button", "name": "Call tab", "options": {"action": "call",
                                                                                         "target": "dashboard:newscreen"}},
                                     elements.rev))["element"]["id"]
    store = get_content_store(hass)
    await store.async_ensure_loaded()
    doc = copy.deepcopy(store.document())
    doc["template"]["pages"] = [_page_with("home", [{"id": "c", "element": call}])]
    with pytest.raises(ProfileError, match="nothing to call"):
        store.prepare_document(doc)
    doc["dashboards"]["newscreen"] = {"name": "New", "ha_user_id": hass_admin_user.id, "room": "", "cameras": []}
    store.prepare_document(doc)


@pytest.mark.parametrize("make,change", [
    ({"kind": "button", "name": "Lamp", "entity_id": "script.lamp", "options": {"action": "script_run"}}, {"entity_id": "script.other"}),
    ({"kind": "button", "name": "Scene", "entity_id": "scene.a", "options": {"action": "scene_activate"}}, {"entity_id": "scene.b"}),
    ({"kind": "button", "name": "Push", "entity_id": "button.a", "options": {"action": "button_press"}}, {"entity_id": "button.b"}),
    ({"kind": "button", "name": "Lock", "entity_id": "lock.a", "options": {"action": "lock_lock"}}, {"entity_id": "lock.b"}),
    ({"kind": "button", "name": "Close", "entity_id": "cover.a", "options": {"action": "cover_close"}}, {"entity_id": "cover.b"}),
    ({"kind": "button", "name": "Door", "options": {"action": "portico_open", "target": "dev:1"}}, {"options": {"action": "portico_open", "target": "dev:2"}}),
    ({"kind": "button", "name": "Call", "options": {"action": "call", "target": "dashboard:a"}}, {"options": {"action": "call", "target": "dashboard:b"}}),
    ({"kind": "status", "name": "Gate state", "entity_id": "binary_sensor.a", "options": {}}, {"entity_id": "binary_sensor.b"}),
    ({"kind": "camera", "name": "Cam", "entity_id": "camera.a", "options": {}}, {"entity_id": "camera.b"}),
])
async def test_retargeting_a_used_element_needs_confirm_for_every_kind(hass, monkeypatch, make, change):
    elements = get_elements_store(hass)
    await elements.async_ensure_loaded()
    element = (await elements.async_set(make, elements.rev))["element"]
    users = [{"endpoint": "template", "label": "Home > B"}]
    monkeypatch.setattr(elements, "used_by", lambda element_id: users)
    changed = {**element, **change}
    for kwargs in ({"validate_only": True}, {}):
        with pytest.raises(ElementError) as err:
            await elements.async_set(changed, elements.rev, **kwargs)
        assert err.value.code == "in_use_retarget" and err.value.used_by == users
    done = await elements.async_set(changed, elements.rev, confirm=True)
    assert done["element"]["entity_id" if "entity_id" in change else "options"] == change.get("entity_id", change.get("options"))
    monkeypatch.setattr(elements, "used_by", lambda element_id: [])
    await elements.async_set({**done["element"], "name": "Renamed"}, elements.rev)        # unused or unchanged: no confirm


async def test_the_lamp_script_retargeted_to_open_the_gate_is_refused_until_confirmed_and_then_runs_the_new_entity(
        hass, hass_storage, hass_admin_user):
    store, elements, ids = await _world(hass, hass_storage, hass_admin_user)
    scripts = async_mock_service(hass, "script", "turn_on")
    lamp = copy.deepcopy(elements.elements_by_id()[ids["lamp"]])
    sneaky = {**lamp, "entity_id": "lock.gate", "options": {"action": "script_run"}}
    with pytest.raises(ElementError) as err:
        await elements.async_set(sneaky, elements.rev)
    assert err.value.code in ("in_use_retarget", "invalid")          # a script element cannot hold a lock entity at all
    sneaky = {**lamp, "entity_id": "script.other"}
    with pytest.raises(ElementError) as err:
        await elements.async_set(sneaky, elements.rev)
    assert err.value.code == "in_use_retarget"
    assert elements.elements_by_id()[ids["lamp"]]["entity_id"] == "script.lamp"
    hass.states.async_set("script.other", "off")
    await elements.async_set(sneaky, elements.rev, confirm=True)
    await store.async_run("u1", "Phone", "lamp")
    assert [c.data["entity_id"] for c in scripts] == ["script.other"]
