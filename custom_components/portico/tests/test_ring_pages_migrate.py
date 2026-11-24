"""The ring-pages migration (R-B, design `portico_ring_pages_per_talk_design.md` 5): the pure analysis and rewrite, then the admin
command over a websocket against a real store (dry run, refusal, one write, the copy, idempotence)."""
from __future__ import annotations

import copy

import pytest

from ..content import ContentStore
from ..elements import get_elements_store
from ..content_resolve import endpoint_table
from ..pages_migrate import MigrationRefused  # noqa: F401 - the refusal type the command maps
from ..ring_pages_migrate import (
    CASE_CONFLICT, CASE_CONSISTENT, CASE_MIXED, CASE_ORPHAN, analyse, differences, migrate_document,
)
from .test_content_rules import _action
from .test_editor_commands import _send
from .test_pages_migrate import (  # noqa: F401 - pworld is a fixture, by name
    DOOR_ID, PHONE_ID, _content, _device, _load, _ring, _six, pworld, store_door_talk,
)

pytestmark = pytest.mark.asyncio

T1, T2, T3 = "el-00000001", "el-00000002", "el-00000003"
PARTIES = {T1: ("door", "gate-1"), T2: ("door", "side-1")}
OPEN = _action("open", "lock_open")


def _rb(page_id, talk, *buttons):
    return _ring(page_id, talk, blocks=[{"id": "b", "title": "B", "cameras": [], "actions": list(buttons)}])


# ---- the pure part ----------------------------------------------------------------------------------------------------------

async def test_the_five_cases_of_the_design():
    doc = _six([_rb("r-all", T1), _rb("r-some", T2), _rb("r-x", T3), _rb("r-c1", "el-00000004"), _rb("r-c2", "el-00000004")],
               {"a": _device(ring={T1: "r-all", T2: "r-some", "el-00000004": "r-c1"}),
                "b": _device(ring={T1: "r-all"})})
    cases = {c["talk"]: c for c in analyse(doc, {**PARTIES, "el-00000004": ("door", "x")})}
    assert cases[T1]["case"] == CASE_CONSISTENT and cases[T1]["pages"][0]["devices"] == ["a", "b"]
    assert cases[T2]["case"] == CASE_MIXED and cases[T2]["unassigned_devices"] == ["b"]
    assert cases[T3]["case"] == CASE_ORPHAN                                     # its Talk element stands for nobody
    assert cases["el-00000004"]["case"] == CASE_CONFLICT
    assert [p["devices"] for p in cases["el-00000004"]["pages"]] == [["a"], []]
    assert "el-00000005" not in cases                                           # a Talk element with no page needs nothing


async def test_the_rewrite_empties_every_ring_map_and_drops_only_the_losers_whole():
    doc = _six([_rb("r1", T1, OPEN), _rb("r2", T1), _rb("r3", T2)], {"a": _device(ring={T1: "r1", T2: "r3"}), "b": _device(ring={T1: "r2"})})
    new, dropped = migrate_document(doc, {T1: "r2"})
    assert [p["id"] for p in new["pages"]] == ["r2", "r3"] and [p["id"] for p in dropped] == ["r1"]
    assert dropped[0]["blocks"][0]["actions"][0]["id"] == "open"                # the content is kept in the report
    assert all(d["ring"] == {} for d in new["devices"].values()) and doc["devices"]["a"]["ring"]   # the input is untouched
    with pytest.raises(Exception, match="choose one"):
        migrate_document(doc, {})
    assert [p["id"] for p in migrate_document(doc, {}, provisional=True)[0]["pages"]] == ["r1", "r3"]


async def test_differences_list_the_mixed_case_and_nothing_for_the_consistent_one():
    from .test_ring_per_talk import _door
    gate = _door()
    doc = _six([_rb("r", T1, OPEN)], {"a": _device(ring={T1: "r"}), "b": _device()})
    after, _ = migrate_document(doc, {})
    diff = differences(doc, after, [gate], PARTIES)
    assert [(d["callee"], d["caller"]) for d in diff] == [("b", gate.id)]
    assert diff[0]["before"]["ring_buttons"] == [] and diff[0]["after"]["ring_buttons"] == ["open"]
    consistent = _six([_rb("r", T1, OPEN)], {"a": _device(ring={T1: "r"}), "b": _device(ring={T1: "r"})})
    assert differences(consistent, migrate_document(consistent, {})[0], [gate], PARTIES) == []


# ---- a document shaped like the live one: element references with no `kind` (the dry run raised KeyError: 'kind') ---------------

LIVE_ELEMENTS = {
    "el-door": {"id": "el-door", "kind": "talk", "name": "Door", "options": {"door": "gate-1"}},
    "el-phone": {"id": "el-phone", "kind": "talk", "name": "Phone", "options": {"phone": "ep-phone"}},
    "el-group": {"id": "el-group", "kind": "talk", "name": "Phones", "options": {"group": "phones"}},
    "el-open": {"id": "el-open", "kind": "button", "name": "Open", "entity_id": "lock.gate", "options": {"action": "lock_open"}},
}


def _live_doc():
    """Reference actions (`{id, element}`, no `kind`), a shared Ring page used by three devices through the legacy
    `devices[*].ring` map, and devices with nothing assigned or with a leftover entry."""
    home = {"id": "home", "label": "Home", "kind": "content", "blocks": [{"id": "h", "title": "Home", "cameras": [], "actions": [
        {"id": "door-talk", "element": "el-door"}, {"id": "phone-talk", "element": "el-phone"},
        {"id": "group-talk", "element": "el-group"}, {"id": "open", "element": "el-open"}]}]}
    shared = _ring("ring-door", "el-door", blocks=[{"id": "r", "title": "R", "cameras": [], "actions": [{"id": "open", "element": "el-open"}]}])
    return _six([home, shared], {
        "d1": _device(["home"], ring={"el-door": "ring-door"}), "d2": _device(["home"], ring={"el-door": "ring-door"}),
        "d3": _device(["home"], ring={"el-door": "ring-door"}),
        "d4": _device(["home"], ring={"el-phone": "gone-page"}),            # a leftover entry: its page does not exist
        "d5": _device(), "d6": _device(["home"])})


async def test_the_comparison_resolves_element_references_like_the_real_resolver():
    from ..content_elements import expand_document
    from .test_ring_per_talk import _door
    doc = _live_doc()
    parties = {"el-door": ("door", "gate-1"), "el-phone": ("phone", "ep-phone"), "el-group": ("group", "phones")}
    expand = lambda d: expand_document(d, LIVE_ELEMENTS)  # noqa: E731
    gate = _door()
    after, dropped = migrate_document(doc, {})
    assert dropped == []
    with pytest.raises(KeyError):                                              # the raw document is the bug: no `kind` on a reference
        differences(doc, after, [gate], parties)
    diff = differences(doc, after, [gate], parties, expand)
    # d1-d3 used the page; d4 and d6 had none but their own pages hold the same lock button, so the same buttons ring
    assert [d["callee"] for d in diff] == ["d5"]                              # d5 has no pages at all: now the shared page rings
    assert diff[0]["before"]["ring_buttons"] == [] and diff[0]["after"]["ring_buttons"] == ["open"]
    assert [c["case"] for c in analyse(doc, parties)] == [CASE_MIXED]


# ---- the command over a real store ---------------------------------------------------------------------------------------

async def _world(pworld, pages_for, devices_for):
    talk = await store_door_talk(pworld)
    doc = _six(pages_for(talk), devices_for(talk))
    store = await _load(pworld, doc, elements=False)
    return store, talk


def _ws(admin, msg_id, **body):
    return _send(admin, "portico/content/ring_pages_migrate", msg_id, **body)


async def test_dry_run_writes_nothing_a_conflict_blocks_the_apply_and_a_choice_unblocks_it(pworld, hass_ws_client):
    store, talk = await _world(pworld, lambda t: [_content("home"), _rb("r1", t, OPEN), _rb("r2", t)],
                               lambda t: {PHONE_ID: _device(["home"], ring={t: "r2"}), "ep-other": _device()})
    before = copy.deepcopy(pworld.storage["portico.content"])
    admin = await hass_ws_client(pworld.hass)
    dry = await _ws(admin, 1)
    assert dry["success"], dry
    report = dry["result"]
    assert pworld.storage["portico.content"] == before and "portico.content.v6-before-ring-per-talk" not in pworld.storage
    assert report["counts"]["case_conflict"] == 1 and report["unresolved_conflicts"] == [talk] and report["can_apply"] is False
    assert [p["id"] for p in report["conflicts"][0]["pages"]] == ["r1", "r2"] and report["conflicts"][0]["pages"][0]["content"]
    # the apply refuses until a page is chosen
    refused = await _ws(admin, 2, dry_run=False, base_doc_rev=report["doc_rev"])
    assert refused["error"]["code"] == "conflict_unresolved", refused
    assert pworld.storage["portico.content"] == before
    wrong = await _ws(admin, 3, dry_run=False, base_doc_rev=report["doc_rev"], choices={talk: "nope"})
    assert wrong["error"]["code"] == "conflict_unresolved"
    chosen = await _ws(admin, 4, choices={talk: "r2"})                         # the dry run with the choice shows what the apply does
    assert chosen["result"]["can_apply"] is True and [p["id"] for p in chosen["result"]["dropped_pages"]] == ["r1"]
    assert "portico.content.v6-before-ring-per-talk" not in pworld.storage


async def test_the_apply_is_one_write_leaves_the_backup_and_is_idempotent(pworld, hass_ws_client, monkeypatch):
    store, talk = await _world(pworld, lambda t: [_content("home"), _rb("r1", t, OPEN), _rb("r2", t)],
                               lambda t: {PHONE_ID: _device(["home"], ring={t: "r1"}), "ep-other": _device(["home"], ring={t: "r2"})})
    original = copy.deepcopy(pworld.storage["portico.content"]["data"])
    writes = []
    real_save = store._save

    async def counting():
        writes.append(1)
        await real_save()

    monkeypatch.setattr(store, "_save", counting)
    admin = await hass_ws_client(pworld.hass)
    base = (await _ws(admin, 1, choices={talk: "r1"}))["result"]["doc_rev"]
    resp = await _ws(admin, 2, dry_run=False, base_doc_rev=base, choices={talk: "r1"})
    assert resp["success"] and resp["result"]["applied"] is True and resp["result"]["backup_written"] is True, resp
    assert len(writes) == 1
    data = pworld.storage["portico.content"]["data"]
    assert [p["id"] for p in data["pages"]] == ["home", "r1"] and all(d["ring"] == {} for d in data["devices"].values())
    assert pworld.storage["portico.content.v6-before-ring-per-talk"]["data"] == original     # the file as it was read
    # the same page now rings for every callee
    again = ContentStore(pworld.hass)
    pworld.hass.data["portico"]["content"] = again
    await again.async_ensure_loaded()
    from ..content_resolve import resolve_caller
    door = next(e for e in endpoint_table(again._expanded(), again) if e.id == DOOR_ID)
    for callee in (PHONE_ID, "ep-other"):
        got = resolve_caller(again._expanded(), callee, door, pages=[], states=pworld.hass.states, registry=again)
        assert [a["id"] for a in got["ring_buttons"]] == ["open"], callee
    # idempotent: nothing to do, no second write, the backup is not touched
    second = await _ws(admin, 3)
    assert second["result"]["nothing_to_do"] is True
    again_apply = await _ws(admin, 4, dry_run=False, base_doc_rev=again.doc_rev)
    assert again_apply["result"]["applied"] is False and len(writes) == 1


async def test_the_apply_needs_a_fresh_base_and_an_existing_backup_is_never_overwritten(pworld, hass_ws_client):
    store, talk = await _world(pworld, lambda t: [_content("home"), _rb("r1", t)], lambda t: {PHONE_ID: _device(["home"], ring={t: "r1"})})
    admin = await hass_ws_client(pworld.hass)
    assert (await _ws(admin, 1, dry_run=False))["error"]["code"] == "invalid"           # no base_doc_rev
    assert (await _ws(admin, 2, dry_run=False, base_doc_rev="stale"))["error"]["code"] == "conflict"
    from homeassistant.helpers.storage import Store
    marker = {"marker": 1}
    await Store(pworld.hass, 1, "portico.content.v6-before-ring-per-talk", atomic_writes=True).async_save(marker)
    resp = await _ws(admin, 3, dry_run=False, base_doc_rev=store.doc_rev)
    assert resp["result"]["applied"] is True and resp["result"]["backup_written"] is False
    assert pworld.storage["portico.content.v6-before-ring-per-talk"]["data"] == marker


async def test_a_schema_4_document_is_refused(pworld, hass_ws_client):
    from .test_pages_migrate import _legacy
    await _load(pworld, _legacy())
    admin = await hass_ws_client(pworld.hass)
    assert (await _ws(admin, 1))["error"]["code"] == "not_pages_model"


async def test_the_dry_run_on_a_document_with_element_references_reports(pworld, hass_ws_client):
    elements = get_elements_store(pworld.hass)
    talk = await store_door_talk(pworld)
    button = (await elements.async_set({"kind": "button", "name": "Open", "entity_id": "lock.gate", "options": {"action": "lock_open"}},
                                        elements.rev))["element"]["id"]
    home = {"id": "home", "label": "Home", "kind": "content", "blocks": [{"id": "h", "title": "Home", "cameras": [], "actions": [
        {"id": "talk", "element": talk}, {"id": "open", "element": button}]}]}
    ring = _ring("r1", talk, blocks=[{"id": "r", "title": "R", "cameras": [], "actions": [{"id": "open", "element": button}]}])
    doc = _six([home, ring], {"a": _device(["home"], ring={talk: "r1"}), "b": _device(["home"], ring={talk: "r1"}),
                              "c": _device(["home"], ring={talk: "r1"}), "d": _device(["home"], ring={"el-deadbeef": "r1"}), "e": _device()})
    await _load(pworld, doc, elements=False)
    before = copy.deepcopy(pworld.storage["portico.content"])
    resp = await _ws(await hass_ws_client(pworld.hass), 1)
    assert resp["success"], resp
    assert resp["result"]["counts"]["talk_elements"] == 1 and pworld.storage["portico.content"] == before
