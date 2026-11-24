"""Ring pages belong to the Talk element (R-A, design `portico_ring_pages_per_talk_design.md`, D-20261122-4aec / -5b24).

The one stored Ring page of the Talk element standing for the caller rings for every callee. The retired per-device `ring` map
(R-D) is never read by the resolver or the strict validator; a document that still carries one (a restored backup) loads and
resolves without a crash."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from ..content import ProfileError
from ..content_resolve import caller_camera_entities, resolve_caller
from ..endpoints import Endpoint
from ..pages_model import load_pages_model, normalize_document_v6
from ..pages_resolve import assigned_ring_page, assigned_ring_pages

T_GATE, T_GATE2, T_SIDE, T_PHONE = "el-00000001", "el-00000002", "el-00000003", "talk-phone-alice"
REG = SimpleNamespace(talk_parties=lambda: {T_GATE: ("door", "gate-1"), T_GATE2: ("door", "gate-1"), T_SIDE: ("door", "side-1"),
                                            T_PHONE: ("phone", "alice")})


def _door(device_id="gate-1", cameras=("camera.gate",)):
    return Endpoint(id=f"p:{device_id}", kind="door", provider="p", device_id=device_id, name=device_id, ha_user_id=None,
                    cameras=tuple(cameras), room=None, override=None, available=True, reason=None)


def _ring(page_id, talk, cameras=None, blocks=()):
    return {"id": page_id, "label": page_id, "kind": "ring", "talk": talk, "cameras": cameras, "blocks": list(blocks)}


def _doc(pages, devices=None):
    devices = devices or {"kitchen": {}, "alice": {}}
    return {"schema_version": 6, "pages": pages, "dashboards": {}, "endpoints": {},
            "devices": {k: {"pages": [], "ring": v.get("ring", {}), "roster_hide": []} for k, v in devices.items()}}


def test_per_talk_form_is_the_same_page_for_every_callee():
    doc = _doc([_ring("r-gate", T_GATE, ["camera.a"])])
    assert assigned_ring_page(doc, "kitchen", _door(), REG)["id"] == "r-gate"
    assert assigned_ring_page(doc, "alice", _door(), REG)["id"] == "r-gate"
    assert assigned_ring_page(doc, "kitchen", _door("side-1"), REG) is None                  # no stored page: the default
    assert caller_camera_entities(doc, "alice", _door(), REG) == ["camera.a"]


def test_a_stale_ring_map_of_a_restored_document_is_ignored_by_the_resolver():
    doc = _doc([_ring("r-gate", T_GATE, ["camera.a"]), _ring("r-night", T_GATE, ["camera.b"])],
               {"kitchen": {"ring": {T_GATE: "r-night"}}, "alice": {}})
    assert assigned_ring_page(doc, "kitchen", _door(), REG)["id"] == "r-gate"              # not the device's choice
    assert assigned_ring_page(doc, "alice", _door(), REG)["id"] == "r-gate"
    assert [p["id"] for p in assigned_ring_pages(doc, "kitchen")] == ["r-gate", "r-night"]
    assert caller_camera_entities(doc, "kitchen", _door(), REG) == ["camera.a"]
    assert resolve_caller(doc, "kitchen", _door(), states={}, registry=REG)["cameras"]    # no crash


def test_two_talk_elements_for_one_door_the_lowest_id_with_a_stored_page_wins():
    doc = _doc([_ring("r-b", T_GATE2, ["camera.b"]), _ring("r-a", T_GATE, ["camera.a"])])
    assert assigned_ring_page(doc, "kitchen", _door(), REG)["id"] == "r-a"
    only_second = _doc([_ring("r-b", T_GATE2, ["camera.b"])])
    assert assigned_ring_page(only_second, "kitchen", _door(), REG)["id"] == "r-b"


def test_a_phone_caller_matches_its_own_talk_element_and_an_orphan_page_never_rings():
    doc = _doc([_ring("r-phone", T_PHONE), _ring("r-orphan", "el-0000ffff", ["camera.z"])])
    phone = Endpoint(id="alice", kind="phone", provider=None, device_id=None, name="Alice", ha_user_id=None, cameras=(),
                     room=None, override=None, available=True, reason=None)
    assert assigned_ring_page(doc, "kitchen", phone, REG)["id"] == "r-phone"
    assert assigned_ring_page(doc, "kitchen", _door(), REG) is None
    assert [p["id"] for p in assigned_ring_pages(doc, "kitchen", REG)] == ["r-phone"]       # the orphan is not granted
    assert {p["id"] for p in assigned_ring_pages(doc, "kitchen")} == {"r-phone", "r-orphan"}  # no registry: every stored page
    assert assigned_ring_page(doc, "kitchen", None, REG) is None


def test_the_grant_is_every_stored_page_to_every_endpoint_and_nothing_else():
    doc = _doc([_ring("r-gate", T_GATE), _ring("r-side", T_SIDE), {"id": "c", "label": "C", "kind": "content", "blocks": []}])
    for endpoint in ("kitchen", "alice", "unknown"):
        assert [p["id"] for p in assigned_ring_pages(doc, endpoint, REG)] == ["r-gate", "r-side"]


def test_default_is_unchanged_for_an_unedited_talk_element():
    pages = [{"id": "home", "label": "Home", "blocks": [{"id": "b", "title": "B", "cameras": [], "actions": [
        {"id": "open-gate", "label": "Open", "kind": "portico_open", "device_id": "gate-1", "confirm": False}]}]}]
    states = SimpleNamespace(get=lambda entity_id: None)
    registry = SimpleNamespace(talk_parties=REG.talk_parties, door=lambda ref: SimpleNamespace(name="Gate"))
    got = resolve_caller(_doc([]), "kitchen", _door(), pages=pages, states=states, registry=registry)
    assert [a["id"] for a in got["ring_buttons"]] == ["open-gate"] and [c["id"] for c in got["cameras"]] != []
    assert got["call_buttons"] == got["ring_buttons"]


def test_strict_validation_refuses_two_ring_pages_for_one_talk_and_reads_a_ring_map_as_empty():
    pages = [_ring("r-1", T_GATE), _ring("r-2", T_GATE)]
    with pytest.raises(ProfileError, match="r-1.*r-2.*one ring page"):
        normalize_document_v6(_doc(pages))
    with pytest.raises(ProfileError, match="r-1.*r-2.*one ring page"):          # a leftover map no longer excuses it
        normalize_document_v6(_doc(pages, {"kitchen": {"ring": {T_GATE: "r-2"}}}))
    one = normalize_document_v6(_doc([_ring("r-1", T_GATE)], {"kitchen": {"ring": {T_GATE: "r-1", "junk": "gone"}}}))
    assert one["devices"]["kitchen"]["ring"] == {}                                 # tolerated, dropped
    with pytest.raises(ProfileError, match="ring must be an object"):
        normalize_document_v6(_doc([], {"kitchen": {}}) | {"devices": {"kitchen": {"pages": [], "ring": []}}})


def test_lenient_load_keeps_the_first_page_per_talk_and_every_page_while_a_ring_map_survives():
    pages = [_ring("r-1", T_GATE), _ring("r-2", T_GATE), _ring("r-3", T_SIDE)]
    kept, _devices, _roster = load_pages_model(_doc(pages))
    assert [p["id"] for p in kept] == ["r-1", "r-3"]
    kept, _devices, _roster = load_pages_model(_doc(pages, {"kitchen": {"ring": {T_GATE: "r-2"}}}))      # for the migration to analyse
    assert [p["id"] for p in kept] == ["r-1", "r-2", "r-3"]
