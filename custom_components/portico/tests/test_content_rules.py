"""Rules, `callers`, the roster and the endpoint's document, pure over a document (M6 E1b, design 2.4-2.6, R6-03, R6-06,
R6-07).

Nothing here starts Home Assistant: the resolvers of content_resolve take a document, a `states` and a `registry`, so
the eight worked cases of the design (2.4) run against plain objects.
"""
from __future__ import annotations

import copy
import types

import pytest

from ..content_resolve import (
    UNKNOWN_CALLER, default_buttons, doc_rev, endpoint_rev, merged_pages, resolve_caller, resolve_endpoint,
    resolve_roster, resolve_rule, roster_action_id, short_id,
)
from ..endpoints import KIND_DASHBOARD, KIND_DOOR, KIND_PHONE, Endpoint

GATE_ID = "portico_intercom:http://10.6.0.7:8091"
GATE_DEVICE = "http://10.6.0.7:8091"
SIDE_ID = "portico_intercom:http://10.6.0.8:8091"
SIDE_DEVICE = "http://10.6.0.8:8091"


def _ep(id, kind, name, **kw):
    base = dict(id=id, kind=kind, provider=None, device_id=None, name=name, ha_user_id=None, cameras=(), room=None,
                override=None, available=True, reason=None)
    base.update(kw)
    return Endpoint(**base)


GATE = _ep(GATE_ID, KIND_DOOR, "Gate", provider="portico_intercom", device_id=GATE_DEVICE, cameras=("camera.gate",))
SIDE = _ep(SIDE_ID, KIND_DOOR, "Side door", provider="portico_intercom", device_id=SIDE_DEVICE, cameras=("camera.side",))
ALICE = _ep("alice", KIND_PHONE, "Alice's phone", ha_user_id="u-alice")
DINA = _ep("dina", KIND_PHONE, "Dina's phone", ha_user_id="u-dina")


class FakeStates:
    def __init__(self, **entities):
        self._s = {k: types.SimpleNamespace(state=v[0], attributes={"friendly_name": v[1]} if v[1] else {})
                   for k, v in entities.items()}

    def get(self, entity_id):
        return self._s.get(entity_id)


STATES = FakeStates(**{"camera.gate": ("idle", "Gate cam"), "camera.driveway": ("idle", "Driveway"),
                       "camera.kitchen": ("idle", "Kitchen cam"), "camera.side": ("idle", None),
                       "lock.gate": ("locked", "Gate lock")})


class FakeRegistry:
    def __init__(self, endpoints, doors=()):
        self._endpoints = endpoints
        self._doors = {d.device_id: d for d in doors}

    def endpoints(self):
        return list(self._endpoints)

    def door(self, device_id):
        return self._doors.get(device_id)

    def call_state(self, action):
        return {"label": action["label"], "available": False, "reason": "not_available"}


REGISTRY = FakeRegistry([ALICE, DINA, GATE, SIDE], doors=[GATE, SIDE])


def _action(id, kind, **kw):
    base = {"label": id, "confirm": True}
    if kind == "lock_open":
        base["entity_id"] = "lock.gate"
    if kind == "portico_open":
        base["device_id"] = kw.pop("device_id")
    return {"id": id, "kind": kind, **base, **kw}


def _page(page_id, label, cameras, actions):
    """A schema-4 page of one block (the resolvers read blocks only since B8)."""
    return {"id": page_id, "label": label,
            "blocks": [{"id": page_id, "title": label, "cameras": cameras, "actions": actions}]}


def _doc(*, rules=(), override_rules=(), dashboards=None, endpoints=None, roster=("*",), hide=()):
    home = _page("home", "Home", [{"id": "gate", "label": "Gate", "entity_id": "camera.gate"}],
                 [_action("open_gate", "lock_open"),
                  _action("relay", "portico_open", device_id=GATE_DEVICE),
                  _action("side_relay", "portico_open", device_id=SIDE_DEVICE)])
    return {
        "schema_version": 4, "home_name": "My Home",
        "template": {"pages": [home], "rules": [dict(r) for r in rules], "roster": list(roster)},
        "overrides": {"alice": {"pages": [], "rules": [dict(r) for r in override_rules], "roster_hide": list(hide)}},
        "dashboards": dashboards or {}, "endpoints": endpoints or {},
    }


def _rule(id, caller, cameras=None, ring=(), call=()):
    return {"id": id, "caller": caller, "cameras": cameras, "ring_buttons": list(ring), "call_buttons": list(call)}


def _caller(doc, callee, caller, registry=REGISTRY):
    return resolve_caller(doc, callee, caller, states=STATES, registry=registry)


def _cams(entry):
    return [c["name"] for c in entry["cameras"]]


def _ids(buttons):
    return [b["id"] for b in buttons]


# ---- the 8 worked cases of design 2.4 ---------------------------------------------------------

def test_case_1_no_rules_gets_the_doors_camera_and_every_open_button_that_fits():
    out = _caller(_doc(), "alice", GATE)
    assert _cams(out) == ["Gate cam"]
    # R6-03: the portico_open naming THIS door and every lock_open; the other door's relay is not offered
    assert _ids(out["ring_buttons"]) == ["open_gate", "relay"] and _ids(out["call_buttons"]) == ["open_gate", "relay"]
    assert out["name"] == "Gate" and out["kind"] == "door"


def test_case_2_a_template_rule_for_doors_sets_cameras_and_buttons():
    doc = _doc(rules=[_rule("doors", "kind:door", ["camera.driveway"], ring=["open_gate"])])
    out = _caller(doc, "alice", GATE)
    assert _cams(out) == ["Driveway"] and _ids(out["ring_buttons"]) == ["open_gate"] and out["call_buttons"] == []


def test_case_3_the_phones_override_rule_wins_and_null_cameras_mean_the_callers_own():
    doc = _doc(rules=[_rule("doors", "kind:door", ["camera.driveway"], ring=["open_gate"])],
               override_rules=[_rule("gate", GATE_ID, None, ring=[])])
    out = _caller(doc, "alice", GATE)
    assert _cams(out) == ["Gate cam"] and out["ring_buttons"] == []        # step 1 wins; null -> step 5 cameras
    assert _ids(_caller(doc, "alice", SIDE)["ring_buttons"]) == ["open_gate"]   # another door: the template rule


def test_case_4_a_phone_calling_with_no_declaration_is_the_name_only():
    out = _caller(_doc(), "alice", DINA)
    assert out == {"name": "Dina's phone", "kind": "phone", "cameras": [], "ring_buttons": [], "call_buttons": []}


def test_case_5_a_dashboard_calling_shows_its_declared_cameras_and_no_buttons():
    doc = _doc(dashboards={"kitchen": {"name": "Kitchen tablet", "ha_user_id": "u-k", "cameras": ["camera.kitchen"]}})
    kitchen = resolve_endpoint(doc, "alice", states=STATES, registry=REGISTRY)["callers"]["kitchen"]
    assert _cams(kitchen) == ["Kitchen cam"] and kitchen["ring_buttons"] == [] and kitchen["kind"] == "dashboard"


def test_case_6_the_kitchen_tablet_as_callee_gets_the_template_rule_on_its_own_page_set():
    doc = _doc(rules=[_rule("doors", "kind:door", ["camera.driveway"], ring=["open_gate"])],
               dashboards={"kitchen": {"name": "Kitchen tablet", "ha_user_id": "u-k", "cameras": []}})
    out = resolve_endpoint(doc, "kitchen", states=STATES, registry=REGISTRY)
    assert out["kind"] == "dashboard" and "kitchen" not in out["callers"]
    gate = out["callers"][GATE_ID]
    assert _cams(gate) == ["Driveway"] and _ids(gate["ring_buttons"]) == ["open_gate"]


def test_case_7_a_dial_from_a_door_is_the_same_lookup_as_a_ring_from_it():
    doc = _doc()
    assert _caller(doc, "alice", GATE) == resolve_endpoint(doc, "alice", states=STATES, registry=REGISTRY)["callers"][GATE_ID]


def test_case_8_a_caller_the_registry_no_longer_knows_gets_the_star_entry():
    out = resolve_endpoint(_doc(), "alice", states=STATES, registry=REGISTRY)["callers"]
    assert out[UNKNOWN_CALLER] == {"name": None, "kind": None, "cameras": [], "ring_buttons": [], "call_buttons": []}
    assert _caller(_doc(), "alice", None) == out[UNKNOWN_CALLER]
    assert set(out) == {GATE_ID, SIDE_ID, "dina", UNKNOWN_CALLER}          # not the callee itself


# ---- the order (requirements 6.2, steps 1 to 4) --------------------------------------------------

def test_the_resolution_order_is_override_id_then_override_kind_then_template_id_then_template_kind():
    t_kind = _rule("t-kind", "kind:door", ["camera.kitchen"])
    t_id = _rule("t-id", GATE_ID, ["camera.driveway"])
    o_kind = _rule("o-kind", "kind:door", ["camera.side"])
    o_id = _rule("o-id", GATE_ID, ["camera.gate"])
    doc = _doc(rules=[t_kind, t_id], override_rules=[o_kind, o_id])      # list order is the worst case for each step
    seen = []
    for drop in ("o-id", "o-kind", "t-id", "t-kind"):
        seen.append(resolve_rule(doc, "alice", GATE_ID, "door")["id"])
        for rules in (doc["template"]["rules"], doc["overrides"]["alice"]["rules"]):
            rules[:] = [r for r in rules if r["id"] != drop]
    assert seen == ["o-id", "o-kind", "t-id", "t-kind"]
    assert resolve_rule(doc, "alice", GATE_ID, "door") is None


def test_an_override_for_another_endpoint_is_not_consulted():
    doc = _doc()
    doc["overrides"]["dina"] = {"pages": [], "rules": [_rule("y", "kind:door", ["camera.kitchen"])], "roster_hide": []}
    assert resolve_rule(doc, "alice", GATE_ID, "door") is None


def test_within_a_step_the_first_rule_in_list_order_wins():
    doc = _doc(rules=[_rule("a", "kind:door", ["camera.kitchen"]), _rule("b", "kind:door", ["camera.driveway"])])
    assert resolve_rule(doc, "alice", GATE_ID, "door")["id"] == "a"


def test_a_missing_button_and_a_non_camera_are_dropped_with_a_warning(caplog):
    doc = _doc(rules=[_rule("doors", "kind:door", ["lock.gate", "camera.driveway"], ring=["gone", "open_gate"])])
    with caplog.at_level("WARNING"):
        out = _caller(doc, "alice", GATE)
    assert _cams(out) == ["Driveway"] and _ids(out["ring_buttons"]) == ["open_gate"]
    assert "'gone'" in caplog.text and "not a camera entity" in caplog.text


def test_default_buttons_for_a_door_are_page_order_across_pages():
    doc = _doc()
    doc["template"]["pages"].append(_page("garden", "Garden", [], [
        _action("gate_relay_2", "portico_open", device_id=GATE_DEVICE), _action("lamp", "script_run", entity_id="script.x",
                                                                                confirm=False)]))
    assert _ids(default_buttons(merged_pages(doc, "alice"), GATE)) == ["open_gate", "relay", "gate_relay_2"]


# ---- the endpoint's document, rev and doc_rev ------------------------------------------------------

def test_the_resolved_document_carries_pages_with_labels_and_the_home_name():
    out = resolve_endpoint(_doc(), "alice", states=STATES, registry=REGISTRY)
    assert out["endpoint"] == "alice" and out["kind"] == "phone" and out["home_name"] == "My Home"
    (home,) = out["pages"]
    assert home["id"] == "home" and home["label"] == "Home"
    assert set(home) == {"id", "label", "blocks"}
    (block,) = home["blocks"]
    assert block["cameras"][0]["name"] == "Gate cam" and block["actions"][0]["available"] is True
    assert resolve_endpoint(_doc(), "nobody", states=STATES, registry=REGISTRY)["kind"] is None


def test_rev_changes_with_content_and_not_with_availability_or_names():
    doc = _doc()
    first = resolve_endpoint(doc, "alice", states=STATES, registry=REGISTRY)
    dark = FakeStates(**{"camera.gate": ("unavailable", "Renamed"), "lock.gate": ("unavailable", None)})
    second = resolve_endpoint(doc, "alice", states=dark, registry=REGISTRY)
    assert first != second and first["rev"] == second["rev"]
    doc["template"]["pages"][0]["label"] = "Front"
    assert resolve_endpoint(doc, "alice", states=STATES, registry=REGISTRY)["rev"] != first["rev"]
    other = resolve_endpoint(_doc(), "dina", states=STATES, registry=REGISTRY)
    assert other["rev"] != first["rev"]                 # per endpoint: its own callers and roster
    assert endpoint_rev(first) == first["rev"]


def test_a_template_change_reaches_every_endpoint_rev_and_an_override_change_only_its_own():
    doc = _doc()
    alice, dina = (resolve_endpoint(doc, e, states=STATES, registry=REGISTRY)["rev"] for e in ("alice", "dina"))
    doc["overrides"]["alice"]["pages"].append(_page("x", "X", [], []))
    assert resolve_endpoint(doc, "alice", states=STATES, registry=REGISTRY)["rev"] != alice
    assert resolve_endpoint(doc, "dina", states=STATES, registry=REGISTRY)["rev"] == dina
    doc["template"]["pages"].append(_page("t", "T", [], []))
    assert resolve_endpoint(doc, "dina", states=STATES, registry=REGISTRY)["rev"] != dina


def test_doc_rev_is_the_hash_of_the_raw_document_and_is_not_an_endpoints_rev():
    doc = _doc()
    rev = doc_rev(doc)
    assert rev == doc_rev(copy.deepcopy(doc)) and len(rev) == 12
    doc["overrides"]["dina"] = {"pages": [], "rules": [], "roster_hide": []}     # a change nobody's rev shows...
    assert doc_rev(doc) != rev                                                    # ...but the document's does
    assert rev != resolve_endpoint(_doc(), "alice", states=STATES, registry=REGISTRY)["rev"]


def test_a_preview_document_shows_a_dashboard_that_is_not_saved_yet():
    doc = _doc(dashboards={"new": {"name": "New tablet", "ha_user_id": "u-n", "cameras": ["camera.kitchen"]}})
    out = resolve_endpoint(doc, "alice", states=STATES, registry=REGISTRY, caller="new")
    assert list(out["callers"]) == ["new", UNKNOWN_CALLER]          # `caller` narrows the map (+ the star entry)
    assert _cams(out["callers"]["new"]) == ["Kitchen cam"]


def test_a_phones_declared_cameras_come_from_the_document():
    doc = _doc(endpoints={"dina": {"cameras": ["camera.kitchen"]}})
    out = resolve_endpoint(doc, "alice", states=STATES, registry=REGISTRY)["callers"]["dina"]
    assert _cams(out) == ["Kitchen cam"]


def test_caller_camera_ids_are_minted_from_the_entity_and_hide_it():
    out = _caller(_doc(), "alice", GATE)["cameras"][0]
    assert out["id"] == short_id("c", "camera.gate") and out["id"].startswith("c-") and len(out["id"]) == 14
    assert "entity_id" not in out and "camera.gate" not in str(out)


# ---- the roster -------------------------------------------------------------------------------

def _roster(doc, callee="alice", registry=REGISTRY):
    return resolve_roster(doc, callee, endpoints=registry.endpoints(), registry=registry)


def test_the_star_roster_is_every_endpoint_but_the_callee_in_registry_order():
    out = _roster(_doc())
    assert [e["id"] for e in out] == ["dina", GATE_ID, SIDE_ID]
    assert out[0] == {"action_id": roster_action_id("dina"), "id": "dina", "kind": "phone", "name": "Dina's phone",
                      "available": False, "reason": "not_available"}
    assert out[0]["action_id"].startswith("r-") and len(out[0]["action_id"]) == 14


def test_an_explicit_roster_keeps_its_order_hides_apply_and_an_empty_roster_is_nobody():
    assert [e["id"] for e in _roster(_doc(roster=[GATE_ID, "dina", "alice"]))] == [GATE_ID, "dina"]
    assert [e["id"] for e in _roster(_doc(hide=["dina"]))] == [GATE_ID, SIDE_ID]
    assert _roster(_doc(roster=[])) == []
    gone = _roster(_doc(roster=["ghost"]))
    assert gone[0]["id"] == "ghost" and gone[0]["kind"] is None and gone[0]["available"] is False


def test_the_roster_follows_the_call_registry_when_it_knows_the_target():
    class Live(FakeRegistry):
        def call_state(self, action):
            if action["target"]["id"] == "dina":
                return {"label": "Dina (home)", "available": True, "reason": None}
            return super().call_state(action)
    out = _roster(_doc(), registry=Live([ALICE, DINA, GATE], doors=[GATE]))
    assert (out[0]["name"], out[0]["available"], out[0]["reason"]) == ("Dina (home)", True, None)
    assert out[1]["available"] is False


def test_the_roster_does_not_touch_the_document():
    doc = _doc()
    snapshot = copy.deepcopy(doc)
    resolve_endpoint(doc, "alice", states=STATES, registry=REGISTRY)
    assert doc == snapshot
