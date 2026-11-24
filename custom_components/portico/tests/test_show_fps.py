"""`show_fps` (live video design, part 2): a document-level additive boolean, "show the frame rate on live pictures".

Like `home_name` it is a field of the stored document, saved with it. Unlike it, it is a boolean: stored and sent ONLY when
true (absent is false), refused by a save when it is anything but a boolean, read leniently on a load (a value a later build
wrote is false and noted, the file still loads), and carried on the resolved document so a card hears of it. Every document
without it is byte-for-byte what it was (the golden contract file is the proof for the wire; the cases here are the unit ones).
"""
from __future__ import annotations

import copy
import json

import pytest

from ..content import ContentStore, ProfileError, get_content_store, normalize_document
from ..content_resolve import resolve_endpoint
from .test_content_wire_blocks import _cam, _doc4, _doc_for_rev, _Registry, _States

pytestmark = pytest.mark.asyncio


def _block():
    return {"id": "b", "title": "B", "cameras": [_cam("gate")], "actions": []}


def _doc(**extra):
    return {**_doc4(_block()), **extra}


@pytest.mark.parametrize("value,stored", [(True, True), (False, None)])
async def test_show_fps_is_kept_when_true_and_otherwise_absent(value, stored):
    out = normalize_document(_doc(show_fps=value))
    assert out.get("show_fps") is stored
    assert ("show_fps" in out) == (stored is not None)


async def test_a_document_without_show_fps_is_exactly_what_it_was():
    plain = normalize_document(_doc())
    assert "show_fps" not in plain
    assert normalize_document(_doc(show_fps=False)) == plain                     # false normalizes to nothing at all
    assert list(plain) == ["schema_version", "template", "overrides", "dashboards", "endpoints"]
    on = normalize_document(_doc(show_fps=True))
    assert {k: v for k, v in on.items() if k != "show_fps"} == plain               # and nothing else moves


@pytest.mark.parametrize("bad", ["true", "yes", 1, 0, None, [], {}, "false"])
async def test_a_save_refuses_a_show_fps_that_is_not_a_boolean(bad):
    with pytest.raises(ProfileError, match="show_fps must be true or false"):
        normalize_document(_doc(show_fps=bad))


def _resolved(document):
    return resolve_endpoint(document, "ep", states=_States(), registry=_Registry())


async def test_resolve_carries_show_fps_only_when_true_and_it_changes_the_rev():
    base = _doc_for_rev(_block())
    plain = _resolved(base)
    on = _resolved({**copy.deepcopy(base), "show_fps": True})
    assert "show_fps" not in plain and "show_fps" not in _resolved({**copy.deepcopy(base), "show_fps": False})
    assert on["show_fps"] is True
    assert on["rev"] != plain["rev"]                 # the cards must hear that the setting changed
    assert _resolved(base)["rev"] == plain["rev"]
    assert {k: v for k, v in on.items() if k not in ("show_fps", "rev")} == {k: v for k, v in plain.items() if k != "rev"}


async def test_a_saved_show_fps_survives_the_store_and_a_save_without_it_removes_it(hass, hass_storage):
    store = get_content_store(hass)
    await store.async_ensure_loaded()
    rev0 = store.doc_rev
    await store.async_save_document(_doc(show_fps=True), rev0)
    assert store.document()["show_fps"] is True
    assert store.resolve_document("ep-nobody")["show_fps"] is True
    assert hass_storage["portico.content"]["data"]["show_fps"] is True
    assert store.doc_rev != rev0
    again = ContentStore(hass)                                           # a restart: read back from the file
    await again.async_ensure_loaded()
    assert again.document()["show_fps"] is True
    await again.async_save_document(_doc(), again.doc_rev)
    assert "show_fps" not in again.document() and "show_fps" not in hass_storage["portico.content"]["data"]
    assert "show_fps" not in again.resolve_document("ep-nobody")
    # a refused save changes nothing
    with pytest.raises(ProfileError):
        await again.async_save_document(_doc(show_fps="yes"), again.doc_rev)
    assert "show_fps" not in again.document()
    # other writers of the file keep it: the home name is saved on top of a document that has it
    await again.async_save_document(_doc(show_fps=True), again.doc_rev)
    await again.async_set_home_name("My Home")
    assert again.document()["show_fps"] is True and hass_storage["portico.content"]["data"]["show_fps"] is True


@pytest.mark.parametrize("stored,expected", [(True, True), (False, False), ("yes", False), (1, False), ([], False), (None, False)])
async def test_a_load_is_lenient_about_a_show_fps_it_does_not_know(hass, hass_storage, caplog, stored, expected):
    data = {**_doc4(_block()), "show_fps": stored}
    hass_storage["portico.content"] = {"version": 1, "minor_version": 1, "key": "portico.content", "data": data}
    store = ContentStore(hass)
    with caplog.at_level("WARNING"):
        await store.async_ensure_loaded()                                # never raises: the page still loads
    assert store.document().get("show_fps", False) is expected
    assert [p["id"] for p in store.document()["template"]["pages"]] == ["home"]
    assert ("ignored a stored show_fps" in caplog.text) == (stored is not None and not isinstance(stored, bool))
    json.dumps(store.document())
