"""The Pages editor debugging pass (2026-11-22): a panel's pages, leftover device entries, the names in a refusal.

Derived from live snapshots of a house whose Playroom Panel (a phone with `panel: true`) kept a leftover device entry
(`playroom-panel-app`, its former screen) beside its own."""
from __future__ import annotations

import copy

import pytest

from ..content import ContentStore, SaveRefused
from ..content_elements import ElementRefError, check_group_placement
from ..elements import get_elements_store
from .test_panel_own_pages import FORMER, PANEL, _store, pan  # noqa: F401 - a fixture, by name
from .test_pages_pc import pworld  # noqa: F401 - a fixture, by name

pytestmark = pytest.mark.asyncio

GROUP = "el-0000000a"


def _group_page(page_id="p-group"):
    return {"id": page_id, "label": "Group", "kind": "content",
            "blocks": [{"id": "b", "title": "B", "cameras": [], "actions": [{"id": "a", "element": GROUP}]}]}


async def test_c_a_leftover_device_entry_is_no_phone_for_the_group_call_check():
    doc = {"schema_version": 6, "pages": [_group_page()], "dashboards": {}, "endpoints": {},
           "devices": {PANEL: {"pages": [{"page": "p-group"}], "ring": {}}, FORMER: {"pages": [{"page": "p-group"}], "ring": {}}}}
    elements = {GROUP: {"kind": "talk", "options": {"group": "phones"}}}
    check_group_placement(doc, elements, frozenset({PANEL}), frozenset({PANEL}))      # FORMER is not an endpoint: ignored
    with pytest.raises(ElementRefError):                                               # a live plain phone is still refused
        check_group_placement(doc, elements, frozenset(), frozenset({PANEL, FORMER}))
    with pytest.raises(ElementRefError):                                               # callers that pass no live set: as before
        check_group_placement(doc, elements, frozenset({PANEL}))


async def test_c_a_group_button_on_a_panel_saves_although_a_leftover_entry_holds_the_same_page(pan):
    store = await _store(pan, {PANEL: ["p-plain"]})
    made = await get_elements_store(pan.hass).async_set(
        {"kind": "talk", "name": "Call phones", "options": {"group": "phones"}}, get_elements_store(pan.hass).rev)
    element = made["element"]["id"]
    doc = store.document()
    page = _group_page()
    page["blocks"][0]["actions"][0]["element"] = element
    doc["pages"].append(page)
    doc["devices"][PANEL]["pages"] = [{"page": "p-group"}]
    doc["devices"]["playroom-panel-app"] = {"pages": [{"page": "p-group"}], "ring": {}, "roster_hide": []}     # the leftover
    await store.async_save_document(doc, store.doc_rev, drop_orphans=())
    assert store.document()["devices"][PANEL]["pages"] == [{"page": "p-group"}]


# The former test_b_the_two_screens_refusal_names_a_panel_by_its_name_not_its_id was removed (DEV-825): it pinned the
# `talk_in_two_screens` refusal, which e4de28b deleted on purpose a few hours later (D-20261122-42b1: a door Talk element may now
# be on several screens, each woken by the call). No save raises SaveRefused for that case any more, so nothing is left to name.


async def test_a_a_panels_pages_survive_an_unrelated_save_and_a_reload_from_disk(pan):
    store = await _store(pan, {PANEL: ["p-plain"]})
    doc = store.document()
    doc["home_name"] = "Home"
    await store.async_save_document(doc, store.doc_rev)
    assert store.document()["devices"][PANEL]["pages"] == [{"page": "p-plain"}]
    fresh = ContentStore(pan.hass)
    await fresh.async_ensure_loaded()
    assert fresh.document()["devices"][PANEL]["pages"] == [{"page": "p-plain"}]
    again = copy.deepcopy(fresh.document())
    assert again["devices"][PANEL]["pages"] == [{"page": "p-plain"}]


async def test_d_a_leftover_entry_does_not_block_deleting_a_page_and_loses_only_that_reference(pan):
    store = await _store(pan, {PANEL: ["p-plain"]})
    es = get_elements_store(pan.hass)
    t1 = (await es.async_set({"kind": "talk", "name": "T1", "options": {"phone": PANEL}}, es.rev))["element"]["id"]
    doc = store.document()
    doc["pages"] += [{"id": "r-1", "label": "R1", "kind": "ring", "talk": t1, "cameras": None, "blocks": []}]
    doc["devices"]["old-key"] = {"pages": [{"page": "p-side"}, {"page": "p-plain"}], "ring": {t1: "r-1"},
                                 "roster_hide": ["x"]}
    await store.async_save_document(doc, store.doc_rev, drop_orphans=())
    doc = store.document()
    doc["pages"] = [p for p in doc["pages"] if p["id"] not in ("p-side", "r-1")]
    await store.async_save_document(doc, store.doc_rev)                    # no in_use: a leftover is no user of a page
    assert all(p["id"] != "p-side" for p in store.library)
    kept = store.document()["devices"]["old-key"]                          # the entry stays; only the dead page reference went
    assert kept["pages"] == [{"page": "p-plain"}] and kept["ring"] == {} and kept["roster_hide"] == ["x"]
    assert "old-key" in store.orphan_keys()
    doc = store.document()
    del doc["devices"]["old-key"]
    await store.async_save_document(doc, store.doc_rev, drop_orphans=("old-key",))      # the editor's Delete still works
    assert "old-key" not in store.document()["devices"]


async def test_d_a_real_device_still_blocks_deleting_a_page_while_a_leftover_is_ignored(pan):
    store = await _store(pan, {PANEL: ["p-side"]})
    doc = store.document()
    doc["devices"]["old-key"] = {"pages": [{"page": "p-side"}], "ring": {}, "roster_hide": []}
    await store.async_save_document(doc, store.doc_rev, drop_orphans=())
    doc = store.document()
    doc["pages"] = [p for p in doc["pages"] if p["id"] != "p-side"]
    with pytest.raises(SaveRefused) as err:
        await store.async_save_document(doc, store.doc_rev)
    assert err.value.code == "in_use" and err.value.fields["devices"] == [PANEL] and "old-key" not in str(err.value)
