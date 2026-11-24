"""Pages unified, P-A (design `portico_pages_unify_design.md`): the schema 6 model, its validators, the resolver, the grants, and
the migration that gets there.

Two halves. The first is PURE (no Home Assistant): golden documents run through `migrate_to_v6`, and
`resolve_endpoint(migrated) == resolve_endpoint(original)` (`rev` included) is asserted for every device and every caller, for a
door rule, a phone rule, a screen rule, `kind:` rules, an override page that replaces a template page, a dangling button, an
orphan rule, an element reference and a ring/call order the single-list form cannot say. The second drives the real store and the
admin command over a websocket: dormant until applied, the `.v4`/`.v5` copy, the refusals, the grants.
"""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest
from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.common import async_mock_service

from .. import content, pages_migrate
from ..content import ContentStore, ProfileError, RunRefused, SaveConflict, get_content_store
from ..content_resolve import endpoint_table, resolve_endpoint
from ..elements import get_elements_store
from ..pages_migrate import export_to_v5, migrate_to_v6, prove_equality
from ..pages_model import normalize_document_v6
from ..pages_resolve import is_pages_model, parties_of
from .test_content_rules import (
    GATE, GATE_DEVICE, GATE_ID, ALICE, SIDE, SIDE_DEVICE, SIDE_ID, STATES, DINA, FakeRegistry, _action, _doc, _page, _rule,
)
from .test_editor_commands import _client, _send
from .test_endpoints import _phone_entry, _provide
from .test_push_registry import _phone_user
from .test_signaling import _setup_signaling

pytestmark = pytest.mark.asyncio

KITCHEN = {"name": "Kitchen tablet", "ha_user_id": "u-k", "cameras": ["camera.kitchen"]}

E_GATE_TALK, E_SIDE_TALK = "el-00000001", "el-00000002"
ELEMENTS = {
    E_GATE_TALK: {"id": E_GATE_TALK, "kind": "talk", "name": "Gate", "options": {"door": GATE_DEVICE}},
    E_SIDE_TALK: {"id": E_SIDE_TALK, "kind": "talk", "name": "Side door", "options": {"door": SIDE_DEVICE}},
    "talk-phone-alice": {"id": "talk-phone-alice", "kind": "talk", "name": "Alice's phone", "managed": True,
                         "options": {"phone": "alice"}},
    "talk-phone-dina": {"id": "talk-phone-dina", "kind": "talk", "name": "Dina's phone", "managed": True,
                        "options": {"phone": "dina"}},
    "talk-screen-kitchen": {"id": "talk-screen-kitchen", "kind": "talk", "name": "Kitchen tablet", "managed": True,
                            "options": {"screen": "kitchen"}},
}
DOORS_ONLY = {k: v for k, v in ELEMENTS.items() if k.startswith("el-")}

_RETIRED = pytest.mark.skip(reason="R-D: this migration still emits per-device `ring` maps, which the resolver no longer reads (design "
                                   "portico_ring_pages_per_talk_design.md); the v5->v6 migrator is to be retired as a whole")



class TalkRegistry(FakeRegistry):
    """The fake registry of test_content_rules plus what the store adds for schema 6: which Talk element stands for whom."""

    def __init__(self, elements=ELEMENTS):
        super().__init__([ALICE, DINA, GATE, SIDE], doors=[GATE, SIDE])
        self._elements = elements

    def talk_parties(self):
        return parties_of(self._elements)


def _golden_base(**kw):
    doc = _doc(dashboards={"kitchen": copy.deepcopy(KITCHEN)}, **kw)
    doc["overrides"]["dina"] = {"pages": [], "rules": [], "roster_hide": []}
    doc["overrides"]["kitchen"] = {"pages": [], "rules": [], "roster_hide": []}
    return doc


def _golden_door():
    """A door rule at both scopes, and an override page that REPLACES the template's `home` on one phone."""
    doc = _golden_base(rules=[_rule("side", SIDE_ID, ["camera.side"], ring=["side_relay"], call=["side_relay"])],
                       override_rules=[_rule("gate", GATE_ID, ["camera.driveway", "camera.gate"], ring=["open_gate"],
                                             call=["relay", "open_gate"])])
    doc["overrides"]["alice"]["pages"] = [_page("home", "My home", [], [_action("open_gate", "lock_open"),
                                                                       _action("relay", "portico_open", device_id=GATE_DEVICE),
                                                                       _action("side_relay", "portico_open", device_id=SIDE_DEVICE)]),
                                          _page("garage", "Garage", [], [])]
    return doc


def _golden_phone():
    return _golden_base(override_rules=[_rule("dina", "dina", ["camera.kitchen"], ring=[])])


def _golden_screen():
    return _golden_base(rules=[_rule("tablet", "kitchen", None, ring=["open_gate"], call=[])])


def _golden_kinds():
    """`kind:` rules at the template, an own-id rule that must win over its kind rule, one kind with a single member."""
    return _golden_base(rules=[_rule("doors", "kind:door", ["camera.driveway"], ring=["open_gate"], call=["relay"]),
                               _rule("phones", "kind:phone", ["camera.kitchen"], ring=[]),
                               _rule("screens", "kind:dashboard", None, ring=["relay"], call=["relay"])],
                        override_rules=[_rule("gate", GATE_ID, None, ring=["relay"], call=[])])


def _golden_dangling():
    """A rule naming a button that is gone, one that two pages share with different content, and a talk button."""
    doc = _golden_base(rules=[_rule("doors", "kind:door", None, ring=["open_gate", "ghost", "twin", "tk"], call=["ghost"])])
    doc["template"]["pages"].append(_page("other", "Other", [], [_action("twin", "lock_open", entity_id="lock.other")]))
    doc["template"]["pages"][0]["blocks"][0]["actions"].append(_action("twin", "lock_open"))
    doc["template"]["pages"][0]["blocks"][0]["actions"].append({"id": "tk", "label": "Talk", "kind": "talk", "confirm": False,
                                                                 "door": GATE_DEVICE})
    return doc


def _golden_orphan():
    """Rules for callers nobody answers to: a removed phone (template and override) and a rule for the device itself."""
    doc = _golden_base(rules=[_rule("old", "gone-phone", ["camera.gate"], ring=["open_gate"]),
                              _rule("alice", "alice", None, ring=[])],
                       override_rules=[_rule("old2", "fake:removed-door", None, ring=[])])
    doc["overrides"]["dina"]["rules"] = [_rule("me", "dina", None, ring=[])]         # dina's rule for herself never applies
    return doc


def _golden_order():
    """Ring and call lists in different orders: the two-block form with the same ids in both blocks."""
    return _golden_base(rules=[_rule("doors", "kind:door", None, ring=["open_gate", "relay"], call=["relay", "open_gate"])])


def _golden_roster():
    return _golden_base(rules=[], roster=("alice", "dina"), hide=("dina",))


GOLDEN = {"door": _golden_door, "phone": _golden_phone, "screen": _golden_screen, "kinds": _golden_kinds,
          "dangling": _golden_dangling, "orphan": _golden_orphan, "order": _golden_order, "roster": _golden_roster}


def _migrate(doc, elements=ELEMENTS, registry=None):
    registry = registry or TalkRegistry(elements)
    plan = migrate_to_v6(doc, endpoints=endpoint_table(doc, registry), elements=elements)
    rows = prove_equality(doc, plan.document, plan.callees, elements=elements, states=STATES, registry=registry,
                          extra_parties=plan.talks.planned_parties())
    return plan, rows, registry


# ---- the equality proof: golden documents ------------------------------------------------------------------------------

@_RETIRED
@pytest.mark.parametrize("name", sorted(GOLDEN))
async def test_every_golden_document_resolves_identically_after_the_migration(name):
    doc = GOLDEN[name]()
    plan, rows, _registry = _migrate(doc)
    assert plan.problems == [] and is_pages_model(plan.document)
    assert {r["endpoint"] for r in rows} >= {"alice", "dina", "kitchen"}
    for row in rows:
        assert row["equal"], row
        assert row["rev_before"] == row["rev_after"] and row["callers"] >= 3
    assert "template" not in plan.document and "overrides" not in plan.document          # no template, no rules
    assert "rules" not in json.dumps(plan.document)


@_RETIRED
@pytest.mark.parametrize("name", sorted(GOLDEN))
async def test_the_migrated_document_is_canonical_and_the_migration_is_deterministic(name):
    doc = GOLDEN[name]()
    plan, _rows, registry = _migrate(doc)
    assert normalize_document_v6(plan.document) == plan.document          # what a save would store is what it is
    again, _, _ = _migrate(copy.deepcopy(doc))
    assert again.document == plan.document
    with pytest.raises(ProfileError, match="already schema 6"):
        migrate_to_v6(plan.document, endpoints=endpoint_table(plan.document, registry), elements=ELEMENTS)


@pytest.mark.parametrize("name", sorted(GOLDEN))
async def test_export_to_v5_gets_back_to_a_document_that_resolves_the_same(name):
    doc = GOLDEN[name]()
    plan, _rows, registry = _migrate(doc)
    endpoints = endpoint_table(plan.document, registry)
    back, notes = export_to_v5(plan.document, endpoints=endpoints, parties=parties_of(ELEMENTS))
    assert back["schema_version"] == 4 and back["template"]["pages"] == [] and back["template"]["rules"] == []
    rows = prove_equality(doc, back, plan.callees, elements=ELEMENTS, states=STATES, registry=registry)
    unequal = [r for r in rows if not r["equal"]]
    # the only rules the way back cannot say are the ones a v5 rule cannot name (a dropped dangling button is the same)
    assert not unequal or name in ("orphan",), (name, unequal, notes)


async def test_the_door_golden_in_detail_pages_are_shared_and_an_overridden_home_keeps_its_wire_id():
    plan, _rows, _ = _migrate(_golden_door())
    doc = plan.document
    by_id = {p["id"]: p for p in doc["pages"]}
    alice, dina = doc["devices"]["alice"], doc["devices"]["dina"]
    # dina and the kitchen show the template's pages: ONE library page each, referenced by both
    assert dina["pages"] == [{"page": "home"}] and doc["devices"]["kitchen"]["pages"] == [{"page": "home"}]
    # alice's own home replaces the template's: a fresh library id, the wire id stays "home"
    assert alice["pages"] == [{"page": "home-2", "as": "home"}, {"page": "garage"}]
    assert by_id["home-2"]["label"] == "My home" and by_id["home"]["label"] == "Home"
    # alice's rule for the gate wins over nothing; the side door's template rule reaches every device
    gate_page = by_id[alice["ring"][E_GATE_TALK]]
    assert gate_page["kind"] == "ring" and gate_page["talk"] == E_GATE_TALK
    assert gate_page["cameras"] == ["camera.driveway", "camera.gate"]          # a migrated page may keep more than one camera
    assert [b.get("when") for b in gate_page["blocks"]] == ["ring", "call"]
    assert E_GATE_TALK not in dina["ring"] and E_SIDE_TALK in dina["ring"]
    # the side door's rule names a button on `home`; alice's own home differs only in its label, so the copies are equal and shared
    assert alice["ring"][E_SIDE_TALK] == dina["ring"][E_SIDE_TALK]


async def test_identical_rules_in_several_scopes_share_one_ring_page():
    plan, _rows, _ = _migrate(_golden_kinds())
    pages = [p for p in plan.document["pages"] if p["kind"] == "ring"]
    devices = plan.document["devices"]
    # kind:door, kind:phone and kind:dashboard are converted per caller: one page per (caller, content); the doors' template rule is
    # one page per door shared by alice, dina and the kitchen
    assert devices["dina"]["ring"][E_SIDE_TALK] == devices["kitchen"]["ring"][E_SIDE_TALK] == devices["alice"]["ring"][E_SIDE_TALK]
    assert devices["alice"]["ring"][E_GATE_TALK] != devices["dina"]["ring"][E_GATE_TALK]       # alice's own id-rule won there
    assert len(pages) == len({p["id"] for p in pages})
    assert plan.rules_by_caller_kind == {"door": 1, "kind": 3}
    assert plan.unused_rules == []


async def test_a_phone_a_screen_and_a_door_caller_each_get_a_ring_page_of_their_own_talk_element():
    plan, _rows, _ = _migrate(_golden_phone())
    by_id = {p["id"]: p for p in plan.document["pages"]}
    page = by_id[plan.document["devices"]["alice"]["ring"]["talk-phone-dina"]]
    assert page["talk"] == "talk-phone-dina" and page["cameras"] == ["camera.kitchen"] and page["blocks"] == []
    assert plan.rules_by_caller_kind == {"phone": 1}
    plan, _rows, _ = _migrate(_golden_screen())
    by_id = {p["id"]: p for p in plan.document["pages"]}
    for device in ("alice", "dina"):
        assert by_id[plan.document["devices"][device]["ring"]["talk-screen-kitchen"]]["cameras"] is None
    assert plan.rules_by_caller_kind == {"screen": 1}


@_RETIRED
async def test_precedence_an_own_caller_rule_beats_the_kind_rule_for_that_caller_only():
    plan, rows, registry = _migrate(_golden_kinds())
    assert all(r["equal"] for r in rows)
    out = resolve_endpoint(plan.document, "alice", states=STATES, registry=registry)
    assert [b["id"] for b in out["callers"][GATE_ID]["ring_buttons"]] == ["relay"]           # the override rule for the gate id
    assert [b["id"] for b in out["callers"][SIDE_ID]["ring_buttons"]] == ["open_gate"]       # kind:door for the other door


async def test_dangling_shared_and_talk_buttons_are_dropped_as_they_are_today_and_listed():
    plan, rows, _ = _migrate(_golden_dangling())
    assert all(r["equal"] for r in rows)
    reasons = {(d["button"], d["list"]): d["reason"] for d in plan.dropped if d["scope"] == "alice"}
    assert "no longer has it" in reasons[("ghost", "ring_buttons")] and "no longer has it" in reasons[("ghost", "call_buttons")]
    assert "share this id" in reasons[("twin", "ring_buttons")] and "talk" in reasons[("tk", "ring_buttons")]
    page = next(p for p in plan.document["pages"] if p["kind"] == "ring")
    assert [a["id"] for b in page["blocks"] for a in b["actions"]] == ["open_gate"]


async def test_a_rule_for_a_caller_nobody_answers_to_is_kept_as_a_ring_page_of_a_talk_element_that_is_not_there():
    plan, rows, _ = _migrate(_golden_orphan())
    assert all(r["equal"] for r in rows)
    doc = plan.document
    orphan_talk = pages_migrate.orphan_talk_id("gone-phone")
    ring = doc["devices"]["alice"]["ring"]
    assert orphan_talk in ring and doc["devices"]["dina"]["ring"][orphan_talk] == ring[orphan_talk]      # a template rule: every device
    assert pages_migrate.orphan_talk_id("fake:removed-door") in ring and pages_migrate.orphan_talk_id("fake:removed-door") not in doc["devices"]["dina"]["ring"]
    assert plan.rules_by_caller_kind == {"orphan": 2, "phone": 1}           # the template's rule for alice applies to the others
    assert [(u["scope"], u["rule"]) for u in plan.unused_rules] == [("dina", "me")]     # a rule for the device itself never applied
    assert orphan_talk not in parties_of(ELEMENTS)                          # "Talk element removed": it never rings


async def test_ring_and_call_in_different_orders_survive_in_two_blocks_that_repeat_the_ids():
    plan, rows, _ = _migrate(_golden_order())
    assert all(r["equal"] for r in rows)
    page = next(p for p in plan.document["pages"] if p["kind"] == "ring")
    assert [b["when"] for b in page["blocks"]] == ["ring", "call"]
    assert [a["id"] for a in page["blocks"][0]["actions"]] == ["open_gate", "relay"]
    assert [a["id"] for a in page["blocks"][1]["actions"]] == ["relay", "open_gate"]


async def test_the_roster_and_each_phones_hidden_entries_are_carried_over():
    plan, rows, _ = _migrate(_golden_roster())
    assert all(r["equal"] for r in rows)
    assert plan.document["roster"] == ["alice", "dina"] and plan.document["devices"]["alice"]["roster_hide"] == ["dina"]
    assert plan.document["home_name"] == "My Home"


@_RETIRED
async def test_a_talk_element_that_does_not_exist_yet_is_planned_with_the_id_the_managed_element_will_have():
    plan, rows, _ = _migrate(_golden_phone(), elements=DOORS_ONLY)
    assert all(r["equal"] for r in rows)                                    # proven through the overlay of planned elements
    (need,) = plan.talks.missing()
    assert (need.kind, need.party, need.element_id, need.exists) == ("phone", "dina", "talk-phone-dina", False)
    assert need.as_row()["managed"] is True
    assert "talk-phone-dina" in plan.document["devices"]["alice"]["ring"]
    # without the overlay the phone's Ring page finds no Talk element and the built-in default rings: the proof sees it
    registry = TalkRegistry(DOORS_ONLY)
    bare = prove_equality(_golden_phone(), plan.document, plan.callees, elements=DOORS_ONLY, states=STATES, registry=registry)
    assert not all(r["equal"] for r in bare if r["endpoint"] == "alice")


async def test_an_element_reference_is_copied_as_a_reference_unless_it_needs_the_blocks_door():
    elements = {**ELEMENTS,
                "el-0000000a": {"id": "el-0000000a", "kind": "button", "name": "Gate lock", "entity_id": "lock.gate",
                                "options": {"action": "lock_open"}},
                "el-0000000b": {"id": "el-0000000b", "kind": "button", "name": "Door relay",
                                "options": {"action": "portico_open"}}}      # no target: acts on the block's door
    doc = _golden_base(rules=[_rule("doors", "kind:door", None, ring=["lock", "relay"], call=["lock"])])
    block = doc["template"]["pages"][0]["blocks"][0]
    block["door"] = GATE_DEVICE
    block["actions"] = [{"id": "lock", "element": "el-0000000a"}, {"id": "relay", "element": "el-0000000b"}]
    doc["schema_version"] = 5
    plan, rows, _ = _migrate(doc, elements=elements)
    assert plan.problems == [] and all(r["equal"] for r in rows), (plan.problems, rows)
    page = next(p for p in plan.document["pages"] if p["kind"] == "ring" and p["talk"] == E_GATE_TALK)
    actions = {a["id"]: a for b in page["blocks"] for a in b["actions"]}
    assert actions["lock"] == {"id": "lock", "element": "el-0000000a"}                  # still a reference
    assert actions["relay"]["kind"] == "portico_open" and actions["relay"]["device_id"] == GATE_DEVICE   # inlined: no door here


# ---- validators ---------------------------------------------------------------------------------------------------------

def _ring(page_id="ring-a", talk=E_GATE_TALK, cameras=None, blocks=None, **extra):
    return {"id": page_id, "label": "Ring", "kind": "ring", "talk": talk, "cameras": cameras,
            "blocks": blocks if blocks is not None else [], **extra}


def _content(page_id="home", label="Home", actions=(), **extra):
    return {"id": page_id, "label": label, "kind": "content",
            "blocks": [{"id": "b", "title": label, "cameras": [], "actions": list(actions)}], **extra}


def _six(pages=(), devices=None, **extra):
    return {"schema_version": 6, "pages": list(pages), "devices": devices or {}, "dashboards": {}, "endpoints": {}, **extra}


def _device(pages=(), ring=None):
    return {"pages": [{"page": p} if isinstance(p, str) else p for p in pages], "ring": ring or {}}


def _open():
    return _action("open", "lock_open")


async def test_a_ring_page_needs_a_talk_element():
    for bad in ({}, {"talk": None}, {"talk": ""}, {"talk": "not-a-talk-id"}, {"talk": "el-XYZ"}):
        page = _ring()
        page.pop("talk")
        page.update(bad)
        with pytest.raises(ProfileError, match="Talk element"):
            normalize_document_v6(_six([page]))
    normalize_document_v6(_six([_ring(talk="talk-phone-dina"), _ring("ring-b", talk="talk-screen-kitchen")]))


async def test_a_ring_page_holds_no_talk_button_and_no_camera_and_a_content_page_no_when():
    talk = {"id": "tk", "label": "Talk", "kind": "talk", "confirm": False, "door": GATE_DEVICE}
    block = {"id": "b", "title": "B", "cameras": [], "actions": [talk]}
    with pytest.raises(ProfileError, match="Talk button"):
        normalize_document_v6(_six([_ring(blocks=[block])]))
    camera = {"id": "b", "title": "B", "cameras": [{"id": "c", "label": "C", "entity_id": "camera.gate"}], "actions": []}
    with pytest.raises(ProfileError, match="buttons only"):
        normalize_document_v6(_six([_ring(blocks=[camera])]))
    with pytest.raises(ProfileError, match="when"):
        normalize_document_v6(_six([_ring(blocks=[{"id": "b", "title": "B", "cameras": [], "actions": [], "when": "never"}])]))
    page = _content(actions=[_open()])
    page["blocks"][0]["when"] = "ring"
    with pytest.raises(ProfileError, match="only a Ring page"):
        normalize_document_v6(_six([page]))


async def test_an_id_may_repeat_across_the_ring_and_the_call_blocks_but_not_inside_one_audience():
    def block(block_id, when=None, title=None):
        out = {"id": block_id, "title": title or block_id.title(), "cameras": [], "actions": [_open()]}
        if when:
            out["when"] = when
        return out

    normalize_document_v6(_six([_ring(blocks=[block("r", "ring"), block("c", "call")])]))
    with pytest.raises(ProfileError, match="used twice"):
        normalize_document_v6(_six([_ring(blocks=[block("a"), block("b", "ring")])]))
    with pytest.raises(ProfileError, match="used twice"):
        normalize_document_v6(_six([_ring(blocks=[block("a", "ring"), block("b", "ring")])]))


@_RETIRED
async def test_a_ring_assignment_names_a_ring_page_of_that_talk_element():
    pages = [_content(), _ring("ring-gate", E_GATE_TALK), _ring("ring-side", E_SIDE_TALK)]
    good = _six(pages, {"alice": _device(["home"], {E_GATE_TALK: "ring-gate"})})
    assert normalize_document_v6(good)["devices"]["alice"]["ring"] == {E_GATE_TALK: "ring-gate"}
    for ring, match in (({E_GATE_TALK: "ring-side"}, "not of"), ({E_GATE_TALK: "home"}, "must name a Ring page"),
                        ({E_GATE_TALK: "nope"}, "must name a Ring page"), ({"junk": "ring-gate"}, "not a Talk element id")):
        with pytest.raises(ProfileError, match=match):
            normalize_document_v6(_six(pages, {"alice": _device(["home"], ring)}))


async def test_a_device_lists_content_pages_once_with_unique_wire_ids_and_at_most_twelve():
    pages = [_content("home"), _content("a", "A"), _ring("ring-gate")]
    for entries, match in ((["ring-gate"], "is a Ring page"), (["home", "home"], "listed twice"), (["nope"], "no page"),
                           ([{"page": "home", "as": "x"}, {"page": "a", "as": "x"}], "go by the id"),
                           ([{"page": "home", "as": "A!"}], "wire id")):
        with pytest.raises(ProfileError, match=match):
            normalize_document_v6(_six(pages, {"alice": _device(entries)}))
    many = [_content(f"p{n}", f"P{n}") for n in range(13)]
    with pytest.raises(ProfileError, match="at most 12"):
        normalize_document_v6(_six(many, {"alice": _device([p["id"] for p in many])}))
    assert len(normalize_document_v6(_six(many[:12], {"alice": _device([p["id"] for p in many[:12]])}))["pages"]) == 12
    # `as` equal to the page's own id is not stored
    assert normalize_document_v6(_six(pages, {"alice": _device([{"page": "home", "as": "home"}])}))["devices"]["alice"]["pages"] == [{"page": "home"}]


async def test_a_document_is_one_model_or_the_other_and_schema_6_stamps_only_on_use():
    with pytest.raises(ProfileError, match="unknown field"):
        content.normalize_document({**_six(), "template": {"pages": [], "rules": [], "roster": ["*"]}})
    with pytest.raises(ProfileError, match="schema_version"):
        content.normalize_document({"schema_version": 5, "pages": []})
    legacy = content.normalize_document({"template": {"pages": [], "rules": [], "roster": ["*"]}})
    assert legacy["schema_version"] == 4 and "pages" not in legacy                  # nothing stamps a 6 by itself
    assert content.normalize_document({"pages": []})["schema_version"] == 6          # a library asks for the new model
    flat = {"id": "home", "label": "Home", "kind": "content", "actions": [], "cameras": [], "blocks": []}
    with pytest.raises(ProfileError, match="flat"):
        content.refuse_flat_pages(_six([flat]))


async def test_the_existing_button_validators_run_unchanged_on_ring_page_blocks():
    bad = {"id": "x", "label": "X", "kind": "lock_open", "entity_id": "switch.not_a_lock"}
    with pytest.raises(ProfileError, match="lock entity"):
        normalize_document_v6(_six([_ring(blocks=[{"id": "b", "title": "B", "cameras": [], "actions": [bad]}])]))
    weak = {"id": "x", "label": "X", "kind": "lock_open", "entity_id": "lock.a", "confirm": False}
    out = normalize_document_v6(_six([_ring(blocks=[{"id": "b", "title": "B", "cameras": [], "actions": [weak]}])]))
    assert out["pages"][0]["blocks"][0]["actions"][0]["confirm"] is False             # as for any inline button (the kind's default is kept elsewhere)


# ---- the resolver -------------------------------------------------------------------------------------------------------

def _resolved(doc, device="alice", registry=None):
    return resolve_endpoint(doc, device, states=STATES, registry=registry or TalkRegistry())


async def test_a_device_with_no_pages_shows_none_and_rings_with_the_built_in_default():
    doc = _six([], {"alice": _device()})
    out = _resolved(doc)
    assert out["pages"] == []
    gate = out["callers"][GATE_ID]
    assert [c["name"] for c in gate["cameras"]] == ["Gate cam"] and gate["ring_buttons"] == []        # no pages: no open buttons
    nobody = resolve_endpoint(doc, "stranger", states=STATES, registry=TalkRegistry())
    assert nobody["pages"] == [] and nobody["kind"] is None


async def test_the_default_is_the_callers_cameras_and_the_doors_open_buttons_from_the_devices_own_pages():
    doc = _six([_content("home", actions=[_open(), _action("relay", "portico_open", device_id=GATE_DEVICE)])],
               {"alice": _device(["home"])})
    gate = _resolved(doc)["callers"][GATE_ID]
    assert [b["id"] for b in gate["ring_buttons"]] == ["open", "relay"] == [b["id"] for b in gate["call_buttons"]]


@_RETIRED
async def test_a_ring_page_gives_the_cameras_and_the_ring_and_call_buttons_by_when():
    ring = _ring("ring-gate", E_GATE_TALK, ["camera.driveway"], blocks=[
        {"id": "both", "title": "Both", "cameras": [], "actions": [_action("a", "lock_open")]},
        {"id": "r", "title": "R", "when": "ring", "cameras": [], "actions": [_action("b", "lock_open")]},
        {"id": "c", "title": "C", "when": "call", "cameras": [], "actions": [_action("c", "lock_open")]}])
    doc = _six([_content(), ring], {"alice": _device(["home"], {E_GATE_TALK: "ring-gate"})})
    gate = _resolved(doc)["callers"][GATE_ID]
    assert [c["name"] for c in gate["cameras"]] == ["Driveway"]
    assert [b["id"] for b in gate["ring_buttons"]] == ["a", "b"] and [b["id"] for b in gate["call_buttons"]] == ["a", "c"]
    # another caller is not affected, and a ring page assigned by another device is not this device's
    assert _resolved(doc)["callers"][SIDE_ID]["ring_buttons"] == []
    assert _resolved(doc, "dina")["callers"][GATE_ID]["cameras"][0]["name"] == "Gate cam"


async def test_cameras_null_means_the_callers_own_and_an_empty_list_means_none():
    own = _six([_content(), _ring("ring-gate", cameras=None)], {"alice": _device(["home"], {E_GATE_TALK: "ring-gate"})})
    none = _six([_content(), _ring("ring-gate", cameras=[])], {"alice": _device(["home"], {E_GATE_TALK: "ring-gate"})})
    assert [c["name"] for c in _resolved(own)["callers"][GATE_ID]["cameras"]] == ["Gate cam"]
    assert _resolved(none)["callers"][GATE_ID]["cameras"] == []


async def test_a_stale_or_unmatched_assignment_never_rings_it_falls_to_the_default():
    pages = [_content("home", actions=[_open()]), _ring("ring-gate", E_GATE_TALK, ["camera.driveway"])]
    stale = _six(pages, {"alice": _device(["home"], {E_GATE_TALK: "ring-gate"})})
    stale["pages"][1]["talk"] = E_SIDE_TALK                                  # the page moved to another Talk element
    assert [c["name"] for c in _resolved(stale)["callers"][GATE_ID]["cameras"]] == ["Gate cam"]
    removed = _six(pages, {"alice": _device(["home"], {E_GATE_TALK: "ring-gate"})})
    registry = TalkRegistry({k: v for k, v in ELEMENTS.items() if k != E_GATE_TALK})        # "Talk element removed"
    assert [c["name"] for c in _resolved(removed, registry=registry)["callers"][GATE_ID]["cameras"]] == ["Gate cam"]
    assert _resolved(removed, registry=SimpleNamespace(endpoints=lambda: [ALICE, GATE], door=lambda d: GATE,
                                                       call_state=lambda a: {"label": "x", "available": False,
                                                                             "reason": "not_available"})
                     )["callers"][GATE_ID]["cameras"][0]["name"] == "Gate cam"       # a registry with no talk_parties at all


async def test_a_device_emits_a_wire_id_as_and_keeps_its_own_order_and_the_legacy_resolver_is_untouched():
    pages = [_content("home", "Home"), _content("home-2", "Mine"), _content("a", "A")]
    doc = _six(pages, {"alice": _device([{"page": "home-2", "as": "home"}, "a", {"page": "home", "as": "old"}])})
    out = _resolved(doc)
    assert [(p["id"], p["label"]) for p in out["pages"]] == [("home", "Mine"), ("a", "A"), ("old", "Home")]
    legacy = _doc()
    assert [p["id"] for p in resolve_endpoint(legacy, "alice", states=STATES, registry=TalkRegistry())["pages"]] == ["home"]


# ---- the store: dormant, the copy, the refusals, the grants --------------------------------------------------------------

PHONE_ID = "ep-alice"
DOOR_ID = "fake:gate-1"


@pytest.fixture
async def pworld(hass, hass_storage, hass_admin_user):
    phone, token = await _phone_user(hass, name="Alice phone user")
    _phone_entry(hass, [{"ha_user_id": phone.id, "name": "Alice's phone", "enroll_id": PHONE_ID}])
    await _setup_signaling(hass)
    _provide(hass, "fake", "gate-1", name="Gate", camera_entity_id="camera.gate")
    for entity, name in (("camera.gate", "Gate camera"), ("camera.driveway", "Driveway")):
        hass.states.async_set(entity, "idle", {"friendly_name": name})
    hass.states.async_set("lock.gate", "locked", {"friendly_name": "Gate lock"})
    return SimpleNamespace(hass=hass, storage=hass_storage, phone=phone, token=token, admin=hass_admin_user)


def _gate_page(extra_cameras=()):
    return {"id": "home", "label": "Home", "blocks": [{"id": "home", "title": "Home", "cameras": [
        {"id": "gate", "label": "Gate", "entity_id": "camera.gate"}, *extra_cameras],
        "actions": [_action("open_gate", "lock_open"), _action("relay", "portico_open", device_id="gate-1")]}]}


def _legacy(version=4, rules=None, pages=None):
    return {"schema_version": version,
            "template": {"pages": pages if pages is not None else [_gate_page()],
                         "rules": [_rule("gate", DOOR_ID, ["camera.driveway"], ring=["open_gate"], call=["relay"])]
                         if rules is None else rules, "roster": ["*"]},
            "overrides": {PHONE_ID: {"pages": [], "rules": [], "roster_hide": []}}, "dashboards": {}, "endpoints": {}}


def _file(data):
    return {"version": 1, "minor_version": 1, "key": "portico.content", "data": data}


async def _load(world, data, *, elements=True):
    """Plant `data` as the content file and give the integration a freshly loaded store (a restart, in effect)."""
    world.storage["portico.content"] = _file(copy.deepcopy(data))
    store = ContentStore(world.hass)
    world.hass.data["portico"][content.DATA_CONTENT] = store
    await store.async_ensure_loaded()
    if elements:
        await store_door_talk(world)
    return store


async def store_door_talk(world, name="Gate"):
    elements = get_elements_store(world.hass)
    await elements.async_ensure_loaded()
    made = await elements.async_set({"kind": "talk", "name": name, "options": {"door": "gate-1"}}, elements.rev)
    return made["element"]["id"]


async def _migrate_ws(world, hass_ws_client, *, validate_only=True, base=None, msg_id=1):
    admin = await hass_ws_client(world.hass)
    body = {"validate_only": validate_only, **({"base_doc_rev": base} if base else {})}
    return admin, await _send(admin, "portico/content/pages_migrate", msg_id, **body)


async def test_until_the_migration_is_applied_nothing_writes_a_6_and_the_old_model_resolves_as_before(pworld, hass_ws_client):
    store = await _load(pworld, _legacy())
    before = copy.deepcopy(pworld.storage["portico.content"])
    resolved = store.resolve_document(PHONE_ID)
    assert resolved["callers"][DOOR_ID]["ring_buttons"][0]["id"] == "open_gate"
    # an ordinary save keeps the old model; a schema 6 document sent to a save is refused, nothing changes
    doc = store.document()
    doc["home_name"] = "My Home"
    await store.async_save_document(doc, store.doc_rev)
    assert pworld.storage["portico.content"]["data"]["schema_version"] == 4 and not store.uses_pages_model
    with pytest.raises(ProfileError, match="written only by the Pages migration"):
        await store.async_save_document(_six([_content()], {PHONE_ID: _device(["home"])}), store.doc_rev)
    assert pworld.storage["portico.content"]["data"]["schema_version"] == 4
    after = store.resolve_document(PHONE_ID)
    assert "portico.content.v4" not in pworld.storage
    assert (after["pages"], after["callers"]) == (resolved["pages"], resolved["callers"])        # only the home name moved
    # the dry run writes nothing
    admin, resp = await _migrate_ws(pworld, hass_ws_client)
    assert resp["success"], resp
    assert pworld.storage["portico.content"]["data"]["schema_version"] == 4 and "portico.content.v4" not in pworld.storage
    assert before["data"]["template"] == pworld.storage["portico.content"]["data"]["template"]


async def test_the_dry_run_report_shape(pworld, hass_ws_client):
    store = await _load(pworld, _legacy())
    admin, resp = await _migrate_ws(pworld, hass_ws_client)
    report = resp["result"]
    assert set(report) == {"ok", "can_apply", "doc_rev", "schema", "safety_copy", "counts", "ring_pages",
                           "talk_elements_to_create", "blocked_by", "dropped", "unused_rules", "per_endpoint", "problems"}
    assert report["ok"] and report["can_apply"] and report["doc_rev"] == store.doc_rev
    assert report["schema"] == {"from": 4, "to": 6} and report["safety_copy"] == ".storage/portico.content.v4"
    counts = report["counts"]
    assert counts["endpoints"] == 1 and counts["ring_pages_new"] == 1 and counts["ring_assignments"] == 1
    assert counts["rules_converted_by_caller_kind"] == {"door": 1} and counts["endpoints_equal"] == 1 and counts["endpoints_unequal"] == 0
    assert counts["talk_elements_to_create"] == 0 and counts["dropped_dangling_buttons"] == 0
    (ring,) = report["ring_pages"]
    assert ring["ring_buttons"] == ["open_gate"] and ring["call_buttons"] == ["relay"] and ring["assigned_to"] == [PHONE_ID]
    (device,) = report["per_endpoint"]
    assert device["endpoint"] == PHONE_ID and device["equal"] and device["rev_before"] == device["rev_after"]
    assert (await _send(admin, "portico/content/pages_migrate", 2, validate_only=False))["error"]["code"] == "invalid"


async def test_the_apply_writes_schema_6_leaves_the_v4_copy_and_changes_nothing_a_phone_sees(pworld, hass_ws_client):
    store = await _load(pworld, _legacy())
    old_file = copy.deepcopy(pworld.storage["portico.content"]["data"])
    before = store.resolve_document(PHONE_ID)
    admin, dry = await _migrate_ws(pworld, hass_ws_client)
    admin, resp = await _migrate_ws(pworld, hass_ws_client, validate_only=False, base=dry["result"]["doc_rev"], msg_id=3)
    assert resp["success"] and resp["result"]["applied"] is True, resp
    data = pworld.storage["portico.content"]["data"]
    assert data["schema_version"] == 6 and "template" not in data and "overrides" not in data
    assert pworld.storage["portico.content.v4"]["data"] == old_file              # the way back, exactly as it was
    assert store.uses_pages_model and store.resolve_document(PHONE_ID) == before   # rev included
    # a fresh store reading the file resolves the same too
    again = ContentStore(pworld.hass)
    pworld.hass.data["portico"][content.DATA_CONTENT] = again
    await again.async_ensure_loaded()
    assert again.uses_pages_model and again.resolve_document(PHONE_ID) == before and again.document() == store.document()
    # the second apply is refused; the door's Talk element is the one the page names
    resp = await _send(admin, "portico/content/pages_migrate", 4, validate_only=False, base_doc_rev=store.doc_rev)
    assert resp["error"]["code"] == "already_applied"
    (ring,) = [p for p in store.document()["pages"] if p["kind"] == "ring"]
    assert ring["talk"] in get_elements_store(pworld.hass).elements_by_id()


async def test_a_v5_file_leaves_the_v5_copy_and_an_existing_copy_is_never_overwritten(pworld, hass_ws_client):
    elements = get_elements_store(pworld.hass)
    await elements.async_ensure_loaded()
    camera = (await elements.async_set({"kind": "camera", "name": "Drive", "entity_id": "camera.driveway"}, elements.rev))["element"]["id"]
    page = _gate_page([{"id": "drive", "element": camera}])
    data = _legacy(5, pages=[page])
    store = await _load(pworld, data)
    pworld.storage["portico.content.v4"] = _file({"schema_version": 4, "marker": "an older copy"})
    before = store.resolve_document(PHONE_ID)
    admin, dry = await _migrate_ws(pworld, hass_ws_client)
    assert dry["success"] and dry["result"]["schema"]["from"] == 5, dry
    admin, resp = await _migrate_ws(pworld, hass_ws_client, validate_only=False, base=dry["result"]["doc_rev"], msg_id=3)
    assert resp["success"], resp
    assert pworld.storage["portico.content.v5"]["data"] == data
    assert pworld.storage["portico.content.v4"]["data"]["marker"] == "an older copy"          # never overwritten
    assert store.resolve_document(PHONE_ID) == before
    assert camera in json.dumps(store.document())                                           # the reference survived, not a copy


async def test_the_apply_is_refused_when_the_safety_copy_cannot_be_written_and_nothing_changes(pworld, hass_ws_client, monkeypatch):
    store = await _load(pworld, _legacy())
    stored = copy.deepcopy(pworld.storage["portico.content"])
    real_save = Store.async_save

    async def refuse(self, data):
        if self.key == "portico.content.v4":
            raise OSError("disk full")
        return await real_save(self, data)

    monkeypatch.setattr(Store, "async_save", refuse)
    admin, dry = await _migrate_ws(pworld, hass_ws_client)
    admin, resp = await _migrate_ws(pworld, hass_ws_client, validate_only=False, base=dry["result"]["doc_rev"], msg_id=3)
    assert resp["success"] is False and resp["error"]["code"] == "invalid" and "copy" in resp["error"]["message"]
    assert pworld.storage["portico.content"] == stored and not store.uses_pages_model
    assert store.document()["schema_version"] == 4 and "portico.content.v4" not in pworld.storage


async def test_an_older_build_reads_a_6_but_refuses_every_write_and_never_rewrites_it(pworld, monkeypatch):
    store = await _load(pworld, _legacy())
    await _apply(store, pworld)
    written = copy.deepcopy(pworld.storage["portico.content"])
    assert written["data"]["schema_version"] == 6
    monkeypatch.setattr(content, "SCHEMA_VERSION", 5)               # the build before this one: knows 5 at most
    older = ContentStore(pworld.hass)
    await older.async_ensure_loaded()
    assert older._newer_schema == 6
    with pytest.raises(ProfileError, match="schema 6"):
        await older.async_set_home_name("X")
    with pytest.raises(ProfileError, match="schema 6"):
        await older.async_save_document(older.document(), older.doc_rev)
    assert pworld.storage["portico.content"] == written


async def _apply(store, world):
    plan_report = store.plan_pages_migration()[1]
    assert plan_report["ok"], plan_report
    return await pages_migrate.run_migration(store, world.hass, apply=True, base_doc_rev=store.doc_rev)


@_RETIRED
async def test_the_apply_refuses_unless_every_device_resolves_identically(pworld, hass_ws_client, monkeypatch):
    store = await _load(pworld, _legacy())
    real = pages_migrate.migrate_to_v6

    def broken(document, **kw):
        plan = real(document, **kw)
        plan.document["devices"][PHONE_ID]["ring"] = {"el-0000ffff": "gone"}   # a conversion that lost the rule (a stale map keeps the document in the per-device form)
        return plan

    monkeypatch.setattr(pages_migrate, "migrate_to_v6", broken)
    admin, dry = await _migrate_ws(pworld, hass_ws_client)
    assert dry["success"] and dry["result"]["ok"] is False and dry["result"]["counts"]["endpoints_unequal"] == 1
    assert "differs" in dry["result"]["problems"][0] and dry["result"]["per_endpoint"][0]["differences"]
    admin, resp = await _migrate_ws(pworld, hass_ws_client, validate_only=False, base=dry["result"]["doc_rev"], msg_id=3)
    assert resp["error"]["code"] == "refused"
    assert pworld.storage["portico.content"]["data"]["schema_version"] == 4 and not store.uses_pages_model


async def test_a_stale_base_and_a_non_admin_are_refused(pworld, hass_ws_client):
    store = await _load(pworld, _legacy())
    admin, resp = await _migrate_ws(pworld, hass_ws_client, validate_only=False, base="stale", msg_id=1)
    assert resp["error"]["code"] == "conflict" and not store.uses_pages_model
    phone = await hass_ws_client(pworld.hass, access_token=pworld.token)
    resp = await _send(phone, "portico/content/pages_migrate", 1, validate_only=True)
    assert resp["success"] is False and resp["error"]["code"] == "unauthorized"


async def test_a_phone_rule_gets_its_managed_talk_element_from_the_devices_reconcile_at_the_apply(pworld, hass_ws_client):
    rules = [_rule("gate", DOOR_ID, None, ring=["open_gate"]), _rule("dina", "ep-dina", None, ring=[])]
    data = _legacy(rules=rules)
    data["overrides"]["ep-dina"] = {"pages": [], "rules": [], "roster_hide": []}
    _phone_entry(pworld.hass, [{"ha_user_id": "u-dina", "name": "Dina", "enroll_id": "ep-dina"}])
    store = await _load(pworld, data, elements=False)                       # no Talk element exists at all
    elements = get_elements_store(pworld.hass)
    await elements.async_ensure_loaded()
    admin, dry = await _migrate_ws(pworld, hass_ws_client)
    report = dry["result"]
    created = {c["kind"]: c for c in report["talk_elements_to_create"]}
    assert report["ok"] and report["can_apply"] and set(created) == {"door", "phone"}, report
    assert created["phone"]["id"] == "talk-phone-ep-dina" and created["phone"]["managed"] is True
    assert elements.elements_by_id() == {}                                  # the dry run created nothing
    admin, resp = await _migrate_ws(pworld, hass_ws_client, validate_only=False, base=report["doc_rev"], msg_id=3)
    assert resp["success"], resp
    made = elements.elements_by_id()
    assert made["talk-phone-ep-dina"]["managed"] is True and made["talk-phone-ep-dina"]["options"] == {"phone": "ep-dina"}
    assert store.document()["devices"][PHONE_ID]["ring"]["talk-phone-ep-dina"]
    assert resp["result"]["talk_elements_to_create"] == []


async def test_the_apply_refuses_and_changes_nothing_when_a_talk_element_cannot_be_created(pworld, hass_ws_client, monkeypatch):
    rules = [_rule("dina", "ep-dina", None, ring=[])]
    data = _legacy(rules=rules)
    data["overrides"]["ep-dina"] = {"pages": [], "rules": [], "roster_hide": []}
    _phone_entry(pworld.hass, [{"ha_user_id": "u-dina", "name": "Dina", "enroll_id": "ep-dina"}])
    store = await _load(pworld, data, elements=False)
    from .. import managed_talk

    async def nothing(hass):
        return []

    monkeypatch.setattr(managed_talk, "async_reconcile", nothing)
    stored = copy.deepcopy(pworld.storage["portico.content"])
    admin, dry = await _migrate_ws(pworld, hass_ws_client)
    admin, resp = await _migrate_ws(pworld, hass_ws_client, validate_only=False, base=dry["result"]["doc_rev"], msg_id=3)
    assert resp["error"]["code"] == "talk_unavailable"
    assert pworld.storage["portico.content"] == stored and not store.uses_pages_model


async def test_a_door_rule_with_no_talk_element_creates_it_at_the_apply_and_names_the_real_id(pworld, hass_ws_client):
    store = await _load(pworld, _legacy(), elements=False)
    elements = get_elements_store(pworld.hass)
    await elements.async_ensure_loaded()
    assert elements.elements_by_id() == {}
    admin, dry = await _migrate_ws(pworld, hass_ws_client)
    report = dry["result"]
    assert report["ok"] and report["can_apply"] and report["counts"]["talk_elements_to_create"] == 1
    (planned,) = report["talk_elements_to_create"]
    assert planned["kind"] == "door" and planned["provisional_id"] is True and elements.elements_by_id() == {}   # the dry run created none
    admin, resp = await _migrate_ws(pworld, hass_ws_client, validate_only=False, base=report["doc_rev"], msg_id=3)
    assert resp["success"], resp
    (made,) = elements.elements_by_id().values()
    assert made["kind"] == "talk" and made["options"] == {"door": "gate-1"}
    (ring,) = [p for p in store.document()["pages"] if p["kind"] == "ring"]
    assert ring["talk"] == made["id"] and resp["result"]["talk_elements_to_create"] == []
    assert store.resolve_document(PHONE_ID)["callers"][DOOR_ID]["ring_buttons"][0]["id"] == "open_gate"


# ---- grants -------------------------------------------------------------------------------------------------------------

def _grant_doc(talk):
    """Schema 6: the ring button `ring_only` exists on the Ring page alone; `home_btn` is on the phone's content page."""
    ring = {"id": "ring-gate", "label": "Ring gate", "kind": "ring", "talk": talk, "cameras": ["camera.driveway"],
            "blocks": [{"id": "r", "title": "R", "cameras": [], "actions": [_action("ring_only", "lock_open")]}]}
    other = {**ring, "id": "ring-other", "blocks": [{"id": "r", "title": "R", "cameras": [], "actions": [
        _action("not_mine", "lock_open", entity_id="lock.other")]}]}
    home = _content("home", actions=[_action("home_btn", "lock_open")])
    return _six([home, ring, other], {PHONE_ID: _device(["home"], {talk: "ring-gate"}),
                                      "ep-other": _device(["home"], {talk: "ring-other"})})


@_RETIRED
async def test_a_ring_page_button_runs_and_one_that_is_not_on_an_assigned_page_is_refused(pworld):
    talk = await store_door_talk(pworld)
    store = await _load(pworld, _grant_doc(talk), elements=False)
    assert store.uses_pages_model
    calls = async_mock_service(pworld.hass, "lock", "open")
    await store.async_run(pworld.phone.id, "Alice", "ring_only", caller_user_id=pworld.phone.id, endpoint_id=PHONE_ID)
    await pworld.hass.async_block_till_done()
    assert len(calls) == 1
    await store.async_run(pworld.phone.id, "Alice", "home_btn", caller_user_id=pworld.phone.id, endpoint_id=PHONE_ID)
    for refused in ("not_mine", "nope"):             # a Ring page another device assigned, and an id nobody has
        with pytest.raises(RunRefused) as err:
            await store.async_run(pworld.phone.id, "Alice", refused, caller_user_id=pworld.phone.id, endpoint_id=PHONE_ID)
        assert err.value.code == "unknown_action"
    assert [a["id"] for a in store._all_actions(PHONE_ID)] == ["home_btn", "ring_only"]
    assert [a["id"] for a in store._all_actions("ep-other")] == ["home_btn", "not_mine"]


async def test_a_ring_pages_camera_is_granted_as_the_callers_c_id_for_the_device_that_assigned_it(pworld):
    talk = await store_door_talk(pworld)
    store = await _load(pworld, _grant_doc(talk), elements=False)
    from ..content_resolve import short_id

    granted = {c["id"] for c in store._all_cameras(PHONE_ID)}
    assert short_id("c", "camera.driveway") in granted                  # the Ring page's camera
    assert short_id("c", "camera.gate") not in granted                  # not the door's own, which the page replaced
    doc = _grant_doc(talk)
    doc["pages"][1]["cameras"] = None                                   # "the caller's own cameras": the door's declaration
    store = await _load(pworld, doc, elements=False)
    assert short_id("c", "camera.gate") in {c["id"] for c in store._all_cameras(PHONE_ID)}


@_RETIRED
async def test_a_schema_6_store_saves_through_the_editor_and_refuses_the_old_shape(pworld, hass_ws_client):
    talk = await store_door_talk(pworld)
    store = await _load(pworld, _grant_doc(talk), elements=False)
    admin = await hass_ws_client(pworld.hass)
    got = (await _send(admin, "portico/content/document"))["result"]
    assert got["document"]["schema_version"] == 6 and PHONE_ID in got["resolved"]
    same = await _send(admin, "portico/content/save", 2, document=got["document"], base_doc_rev=got["doc_rev"])
    assert same["success"] and same["result"]["doc_rev"] == got["doc_rev"]                  # a no-op save is a no-op
    edited = copy.deepcopy(got["document"])
    edited["pages"][0]["label"] = "Renamed"
    resp = await _send(admin, "portico/content/save", 3, document=edited, base_doc_rev=got["doc_rev"])
    assert resp["success"], resp
    assert pworld.storage["portico.content"]["data"]["pages"][0]["label"] == "Renamed"
    assert store.resolve_document(PHONE_ID)["pages"][0]["label"] == "Renamed"
    legacy = {"template": {"pages": [], "rules": [], "roster": ["*"]}, "overrides": {}, "dashboards": {}, "endpoints": {}}
    resp = await _send(admin, "portico/content/save", 4, document=legacy, base_doc_rev=store.doc_rev)
    assert resp["success"] is False and "unified Pages model" in resp["error"]["message"]
    # a Ring page whose Talk element does not exist is refused at the save, as a reference to a missing element is
    bad = copy.deepcopy(edited)
    bad["pages"][1]["talk"] = "el-deadbeef"
    bad["devices"][PHONE_ID]["ring"] = {"el-deadbeef": "ring-gate"}
    resp = await _send(admin, "portico/content/save", 5, document=bad, base_doc_rev=store.doc_rev)
    assert resp["success"] is False and "element_missing" in resp["error"]["message"]


async def test_deleting_a_talk_element_a_ring_page_uses_is_refused_with_the_pages_listed(pworld):
    talk = await store_door_talk(pworld)
    store = await _load(pworld, _grant_doc(talk), elements=False)
    elements = get_elements_store(pworld.hass)
    users = elements.used_by(talk)
    assert {u["page"] for u in users} == {"ring-gate", "ring-other"} and all("Ring page" in u["label"] for u in users)
    from ..elements import ElementError

    with pytest.raises(ElementError) as err:
        await elements.async_delete(talk, elements.rev)
    assert err.value.code == "in_use" and store.uses_pages_model


async def test_the_diagnostics_download_hides_an_enrolment_id_inside_a_managed_talk_id():
    from ..diagnostics import pseudonymise_document

    document = {"schema_version": 6, "devices": {"ep-secret": {"pages": [], "ring": {"talk-phone-ep-other": "ring-a",
                                                                                      "el-00000001": "ring-b"}}},
                "pages": [{"id": "ring-a", "kind": "ring", "talk": "talk-phone-ep-other"},
                          {"id": "ring-c", "kind": "ring", "talk": "talk-screen-kitchen"}]}
    blob = json.dumps(pseudonymise_document(document, {"ep-secret", "ep-other"}))
    assert "ep-secret" not in blob and "ep-other" not in blob
    assert blob.count("talk-phone-endpoint-") == 2 and "talk-screen-kitchen" in blob and "el-00000001" in blob
