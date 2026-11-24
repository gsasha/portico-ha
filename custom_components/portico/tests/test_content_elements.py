"""DEV-760 placements P1, the pure part: element references in the content document (content_elements.py + the validators of
content.py). No Home Assistant. The acceptance test of the design (4.2) lives here: the resolved output of an inline document
equals, byte for byte and including `rev`, that of the version of it written with references.
"""
from __future__ import annotations

import copy
import json
import logging

import pytest

from .. import content
from ..content import ProfileError, normalize_document, normalize_page, normalize_rule
from ..content_elements import (
    ElementRefError, check_refs, expand_document, has_refs, iter_refs, referenced_ids, usage_index,
)
from ..content_resolve import resolve_endpoint
from .test_content_rules import GATE_DEVICE, REGISTRY, STATES, _action, _doc, _page, _rule

pytestmark = pytest.mark.asyncio

E_GATE = "el-00000001"
E_YARD = "el-00000002"
E_STREAM = "el-00000003"
E_BUTTON = "el-00000004"
E_SIDE = "el-00000005"

ELEMENTS = {
    E_GATE: {"id": E_GATE, "kind": "camera", "name": "Gate", "entity_id": "camera.gate", "options": {}},
    E_YARD: {"id": E_YARD, "kind": "camera", "name": "Driveway", "entity_id": "camera.driveway",
             "options": {"size": "big"}},
    E_STREAM: {"id": E_STREAM, "kind": "camera", "name": "Stream only", "options": {"stream": "rtsp://10.6.0.6:8554/a"}},
    E_BUTTON: {"id": E_BUTTON, "kind": "button", "name": "Open", "entity_id": "lock.gate",
               "options": {"action": "lock_open"}},
    E_SIDE: {"id": E_SIDE, "kind": "camera", "name": "Side", "entity_id": "camera.side", "options": {}},
}


def _ref(item_id, element, **extra):
    return {"id": item_id, "element": element, **extra}


def _inline_and_refs():
    """The same document twice: D (inline) and D' (references plus the elements that hold the same data)."""
    inline = _doc(rules=[_rule("r1", "kind:door", cameras=["camera.gate", "camera.driveway"], ring=["open_gate"])],
                  override_rules=[_rule("r2", "dina", cameras=["camera.side"])],
                  dashboards={"kitchen": {"name": "Kitchen", "ha_user_id": "u-k", "cameras": ["camera.gate"]}},
                  endpoints={"alice": {"cameras": ["camera.side", "camera.gate"]}})
    inline["template"]["pages"][0]["blocks"][0]["cameras"] = [
        {"id": "gate", "label": "Gate", "entity_id": "camera.gate"},
        {"id": "yard", "label": "Driveway", "entity_id": "camera.driveway", "size": "big"},
        {"id": "yard2", "label": "Back yard", "entity_id": "camera.driveway", "size": "small", "live": True},
        {"id": "side", "label": "Side", "entity_id": "camera.side", "live": True},
    ]
    inline["overrides"]["alice"]["pages"] = [_page("garden", "Garden", [
        {"id": "g1", "label": "Gate", "entity_id": "camera.gate"}], [])]
    refs = copy.deepcopy(inline)
    refs["template"]["pages"][0]["blocks"][0]["cameras"] = [
        _ref("gate", E_GATE),                                          # label and size come from the element
        _ref("yard", E_YARD),                                          # the element's size: big
        _ref("yard2", E_YARD, label="Back yard", size="small", live=True),    # every override at once
        _ref("side", E_SIDE, live=True),
    ]
    refs["overrides"]["alice"]["pages"][0]["blocks"][0]["cameras"] = [_ref("g1", E_GATE)]
    refs["template"]["rules"][0]["cameras"] = [E_GATE, E_YARD]
    refs["overrides"]["alice"]["rules"][0]["cameras"] = [E_SIDE]
    refs["dashboards"]["kitchen"]["cameras"] = [E_GATE]
    refs["endpoints"]["alice"]["cameras"] = [E_SIDE, "camera.gate"]     # an entity id and an element id may be mixed
    refs["schema_version"] = 5
    return inline, refs


# ---- the acceptance test: same wire ----------------------------------------------------------------------------------

async def test_the_resolved_document_of_the_reference_version_equals_the_inline_one_for_every_endpoint_including_rev():
    inline, refs = _inline_and_refs()
    assert has_refs(refs) and not has_refs(inline)
    expanded = expand_document(refs, ELEMENTS)
    assert not has_refs(expanded)
    # the stored document is not changed by expanding it
    assert refs["template"]["pages"][0]["blocks"][0]["cameras"][0] == _ref("gate", E_GATE)
    for endpoint in ("alice", "dina", "kitchen"):
        a = resolve_endpoint(inline, endpoint, states=STATES, registry=REGISTRY)
        b = resolve_endpoint(expanded, endpoint, states=STATES, registry=REGISTRY)
        assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True), endpoint
        assert a["rev"] == b["rev"]
    alice = resolve_endpoint(expanded, "alice", states=STATES, registry=REGISTRY)
    assert [c["id"] for c in alice["pages"][0]["blocks"][0]["cameras"]] == ["gate", "yard", "yard2", "side"]
    assert alice["pages"][0]["blocks"][0]["cameras"][1]["size"] == "big"
    assert "entity_id" in alice["pages"][0]["blocks"][0]["cameras"][0]        # exactly where an inline camera has it today


async def test_a_document_with_no_reference_is_returned_untouched_as_the_same_object():
    inline, _refs = _inline_and_refs()
    assert expand_document(inline, ELEMENTS) is inline


async def test_an_element_edit_changes_the_resolved_rev_and_a_rename_follows_the_unoverridden_label():
    _inline, refs = _inline_and_refs()
    before = resolve_endpoint(expand_document(refs, ELEMENTS), "alice", states=STATES, registry=REGISTRY)
    renamed = {**ELEMENTS, E_GATE: {**ELEMENTS[E_GATE], "name": "Front gate"}}
    after = resolve_endpoint(expand_document(refs, renamed), "alice", states=STATES, registry=REGISTRY)
    cams = after["pages"][0]["blocks"][0]["cameras"]
    assert cams[0]["label"] == "Front gate" and cams[2]["label"] == "Back yard"      # an override keeps its own
    assert before["rev"] != after["rev"]


# ---- expansion, per rule ---------------------------------------------------------------------------------------------

async def test_overrides_win_defaults_fill_and_only_live_true_is_kept():
    doc = {"template": {"pages": [_page("home", "Home", [
        _ref("a", E_YARD), _ref("b", E_YARD, size="small", label="", live=False), _ref("c", E_YARD, live=True)], [])]}}
    cams = expand_document(doc, ELEMENTS)["template"]["pages"][0]["blocks"][0]["cameras"]
    assert cams[0] == {"id": "a", "label": "Driveway", "entity_id": "camera.driveway", "size": "big"}
    assert cams[1] == {"id": "b", "label": "", "entity_id": "camera.driveway", "size": "small"}      # explicit empty label kept
    assert cams[2]["live"] is True


async def test_a_missing_wrong_kind_or_stream_only_element_is_dropped_with_one_callback_each_and_the_page_survives():
    doc = {"template": {"pages": [_page("home", "Home", [
        _ref("a", "el-deadbeef"), _ref("b", E_BUTTON), _ref("c", E_STREAM), _ref("d", E_GATE),
        {"id": "inline", "label": "I", "entity_id": "camera.side"}], [])],
        "rules": [_rule("r", "kind:door", cameras=[E_STREAM, "camera.gate", "el-deadbeef", E_YARD])]}}
    dropped = []
    out = expand_document(doc, ELEMENTS, on_drop=lambda eid, kind: dropped.append(eid))
    assert [c["id"] for c in out["template"]["pages"][0]["blocks"][0]["cameras"]] == ["d", "inline"]
    assert out["template"]["rules"][0]["cameras"] == ["camera.gate", "camera.driveway"]
    assert sorted(dropped) == sorted(["el-deadbeef", E_BUTTON, E_STREAM, E_STREAM, "el-deadbeef"])


async def test_expansion_never_changes_other_fields_of_the_block_page_or_override():
    inline, refs = _inline_and_refs()
    refs["template"]["pages"][0]["blocks"][0]["door"] = "x:door"
    refs["overrides"]["alice"]["roster_hide"] = ["dina"]
    out = expand_document(refs, ELEMENTS)
    assert out["template"]["pages"][0]["blocks"][0]["door"] == "x:door"
    assert out["overrides"]["alice"]["roster_hide"] == ["dina"] and out["home_name"] == refs["home_name"]
    assert out["template"]["pages"][0]["blocks"][0]["actions"] == refs["template"]["pages"][0]["blocks"][0]["actions"]


# ---- validation of a reference ---------------------------------------------------------------------------------------

async def test_a_camera_reference_keeps_only_id_element_and_the_present_overrides():
    page = normalize_page(_page("home", "Home", [
        {"id": "a", "element": E_GATE, "entity_id": "camera.other", "junk": 1},
        _ref("b", E_YARD, label="  Yard ", size="big", live=True), _ref("c", E_YARD, live=False)], []))
    cams = page["blocks"][0]["cameras"]
    assert cams[0] == {"id": "a", "element": E_GATE}
    assert cams[1] == {"id": "b", "element": E_YARD, "label": "Yard", "size": "big", "live": True}
    assert cams[2] == {"id": "c", "element": E_YARD}


@pytest.mark.parametrize("bad", [dict(element="gate"), dict(element="el-1"), dict(element=5), dict(element=E_GATE, size="huge"),
                                 dict(element=E_GATE, live="yes"), dict(element=E_GATE, label=7), dict(element=E_GATE, id="A!"),
                                 dict(element=E_GATE, label="x" * 41)])
async def test_a_malformed_camera_reference_is_refused(bad):
    with pytest.raises(ProfileError):
        normalize_page(_page("home", "Home", [{"id": "a", **bad}], []))


async def test_two_items_may_not_share_an_id_across_inline_and_reference():
    with pytest.raises(ProfileError, match="used twice"):
        normalize_page(_page("home", "Home", [_ref("a", E_GATE), {"id": "a", "label": "x", "entity_id": "camera.x"}], []))


async def test_a_button_reference_is_accepted_since_p3_and_a_bad_element_id_is_not():
    page = normalize_page(_page("home", "Home", [], [{"id": "b", "element": E_BUTTON}]))
    assert page["blocks"][0]["actions"] == [{"id": "b", "element": E_BUTTON}]
    with pytest.raises(ProfileError, match="element must be an element id"):
        normalize_page(_page("home", "Home", [], [{"id": "b", "element": "nope"}]))


async def test_an_unknown_size_in_a_reference_is_read_as_absent_on_a_load_not_a_dropped_page():
    page = normalize_page(_page("home", "Home", [_ref("a", E_GATE, size="gigantic")], []), strict=False)
    assert page["blocks"][0]["cameras"] == [{"id": "a", "element": E_GATE}]


async def test_rule_cameras_take_entity_ids_or_element_ids():
    rule = normalize_rule(_rule("r", "kind:door", cameras=["camera.gate", E_YARD]))
    assert rule["cameras"] == ["camera.gate", E_YARD]
    for bad in (["light.x"], ["el-ZZZ"], [7], ["el-0000000G"]):
        with pytest.raises(ProfileError):
            normalize_rule(_rule("r", "kind:door", cameras=bad))
    assert normalize_rule(_rule("r", "kind:door"))["cameras"] is None


async def test_rule_buttons_stay_page_button_ids_an_element_id_is_not_a_grant():
    inline, _refs = _inline_and_refs()
    inline["template"]["rules"][0]["ring_buttons"] = [E_BUTTON]
    with pytest.raises(ProfileError, match="not a button on any of its pages"):
        normalize_document(inline)


# ---- the schema stamp ------------------------------------------------------------------------------------------------

async def test_the_document_is_stamped_5_only_when_a_reference_exists():
    inline, refs = _inline_and_refs()
    assert normalize_document(copy.deepcopy(inline))["schema_version"] == 4
    assert normalize_document(copy.deepcopy(refs))["schema_version"] == 5
    # what the client SAYS does not decide it: the content does
    assert normalize_document({**copy.deepcopy(inline), "schema_version": 5})["schema_version"] == 4
    assert normalize_document({**copy.deepcopy(refs), "schema_version": 4})["schema_version"] == 5
    only_rule = copy.deepcopy(inline)
    only_rule["template"]["rules"][0]["cameras"] = [E_GATE]
    assert normalize_document(only_rule)["schema_version"] == 5          # any reference stamps it, not only a tile
    for version in (3, 0, "4", None):
        with pytest.raises(ProfileError, match="schema_version"):
            normalize_document({**copy.deepcopy(inline), "schema_version": version})
    with pytest.raises(ProfileError, match="unknown field"):         # a 6 is the unified model: template/overrides are not its fields
        normalize_document({**copy.deepcopy(inline), "schema_version": 6})


async def test_a_normalised_reference_document_normalises_to_itself():
    _inline, refs = _inline_and_refs()
    once = normalize_document(copy.deepcopy(refs))
    assert normalize_document(copy.deepcopy(once)) == once


# ---- usage and the save check ----------------------------------------------------------------------------------------

async def test_usage_index_lists_every_place_with_a_readable_label_in_one_pass():
    _inline, refs = _inline_and_refs()
    use = usage_index(refs)
    assert use[E_GATE] == [
        {"endpoint": "template", "label": "Home > Home", "page": "home", "block": "home"},
        {"endpoint": "template", "label": "Rule r1"},
        {"endpoint": "alice", "label": "Garden > Garden", "page": "garden", "block": "garden"},
        {"endpoint": "kitchen", "label": "Screen cameras"},
    ]
    assert {u["label"] for u in use[E_SIDE]} == {"Home > Home", "Rule r2", "Camera declaration"}
    assert referenced_ids(refs) == {E_GATE, E_YARD, E_SIDE}
    assert usage_index(_inline_and_refs()[0]) == {}
    assert [r["kind"] for r in iter_refs(refs) if r["element"] == E_YARD] == ["camera", "camera", "rule"]


async def test_check_refs_names_the_item_and_the_reason_never_a_value():
    def check(doc, **kw):
        with pytest.raises(ElementRefError) as err:
            check_refs(doc, ELEMENTS, **kw)
        return err.value

    def doc_with(*refs, rule=None):
        d = {"template": {"pages": [_page("home", "Home", list(refs), [])], "rules": []}}
        if rule:
            d["template"]["rules"] = [rule]
        return d

    assert check(doc_with(_ref("a", "el-deadbeef"))).code == "element_missing"
    assert check(doc_with(_ref("a", E_BUTTON))).code == "element_kind"
    assert check(doc_with(_ref("a", E_STREAM))).code == "not_placeable"
    assert check(doc_with(_ref("a", E_GATE)), pending={E_GATE}).code == "element_pending"
    err = check(doc_with(_ref("gate-1", "el-deadbeef")))
    assert "gate-1" in str(err) and "camera" in str(err)
    assert check(doc_with(rule=_rule("r", "kind:door", cameras=[E_STREAM]))).code == "not_placeable"
    # fine: a placeable camera, and a stored reference whose element has since vanished
    check_refs(doc_with(_ref("a", E_GATE)), ELEMENTS)
    check_refs(doc_with(_ref("a", "el-deadbeef")), ELEMENTS, stored_ids={"el-deadbeef"})
    check_refs({"template": {"pages": [_page("home", "Home", [], [])]}}, {})        # no reference, nothing to check


async def test_a_dangling_reference_logs_once_per_element_through_the_callback_only(caplog):
    doc = {"template": {"pages": [_page("home", "Home", [_ref("a", "el-deadbeef")], [])]}}
    with caplog.at_level(logging.WARNING):
        expand_document(doc, {})
    assert not caplog.records                 # the pure function is silent: the store decides how often to speak


# ---- every reader goes through the one expanded view (design risk 13, first row) --------------------------------------

async def test_no_contentstore_method_outside_the_stored_document_accessors_reads_the_raw_containers():
    """A new reader of `_template`/`_overrides`/`_endpoint_decls`/`_dashboards` that skips the expansion would see a bare
    reference (a grant that misses a camera). Only the methods listed below may touch them; add a method to the list only after
    deciding it is meant to see references as references."""
    import inspect
    import re

    stored_view = {
        "__init__", "_load_content", "_save", "_data", "_disk_data", "_keep_v4_copy", "document", "doc_rev", "_known_target",
        "async_set", "async_remove", "_expanded", "_on_elements_changed", "referenced_element_ids", "element_usage",
        # the editor's whole-document write, the orphan moves, and the stored-form accessors the panel and the registry read
        "async_save_document", "_check_element_refs", "prepare_document", "async_move_orphan_to_endpoint", "async_move_orphan_to_template", "async_delete_orphan",
        "orphan_keys", "_orphan", "dashboards", "endpoint_declarations", "overrides", "template",
        # the unified Pages model (P-A): the stored-form accessors, the rollback triple and the migration's plan and adopt
        "library", "devices", "_state", "_restore", "_adopt", "_keep_safety_copy", "_load_legacy", "plan_pages_migration",
        "async_adopt_migrated",
        # Devices D2: ownership of Talk elements is decided on the stored form (a Talk element is a reference on a page)
        "wake_map", "wake_for_talk_element", "screens_for_door", "_check_library_delete",
    }
    raw = re.compile(r"self\._(template|overrides|endpoint_decls|dashboards)\b|self\._data\(\)")
    offenders = [name for name, fn in inspect.getmembers(content.ContentStore)
                 if (inspect.isfunction(fn) or isinstance(fn, property)) and name not in stored_view
                 and raw.search(inspect.getsource(fn.fget if isinstance(fn, property) else fn))]
    assert offenders == [], f"read the raw document without expanding references: {offenders}"


# ---- DEV-768: the small stream is carried server-side and reaches the client only as `small: true` ---------------------------

async def test_a_small_entity_is_carried_by_expansion_and_resolves_to_small_true_without_its_id():
    elements = {**ELEMENTS, E_GATE: {**ELEMENTS[E_GATE], "options": {"small_entity_id": "camera.gate_sub"}}}
    doc = {"template": {"pages": [_page("home", "Home", [_ref("a", E_GATE), _ref("b", E_YARD)], [])], "rules": []}}
    cams = expand_document(doc, elements)["template"]["pages"][0]["blocks"][0]["cameras"]
    assert cams[0]["small_entity_id"] == "camera.gate_sub" and "small_entity_id" not in cams[1]
    out = resolve_endpoint(expand_document(doc, elements), "alice", states=STATES, registry=REGISTRY)
    resolved = out["pages"][0]["blocks"][0]["cameras"]
    assert resolved[0]["small"] is True and "small" not in resolved[1]
    assert "gate_sub" not in json.dumps(out)
    plain = resolve_endpoint(expand_document(doc, ELEMENTS), "alice", states=STATES, registry=REGISTRY)
    assert plain["rev"] != out["rev"]                      # a small stream appearing is a change the phones must refetch


async def test_the_live_sub_cameras_fallback_also_makes_small_true():
    class Fallback:
        def __getattr__(self, name):
            return getattr(REGISTRY, name)

        def fallback_small_entity(self, entity_id):
            return "camera.gate_sub" if entity_id == "camera.gate" else None

    doc = {"template": {"pages": [_page("home", "Home", [_ref("a", E_GATE), _ref("b", E_YARD)], [])], "rules": []}}
    out = resolve_endpoint(expand_document(doc, ELEMENTS), "alice", states=STATES, registry=Fallback())
    cams = out["pages"][0]["blocks"][0]["cameras"]
    assert cams[0]["small"] is True and "small" not in cams[1] and "gate_sub" not in json.dumps(out)
