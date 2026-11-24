"""The `live` flag of a camera tile (live video design, part 1): an additive optional boolean on a block's camera.

Strict on a save (a non-boolean is refused), lenient on a load (a value a later build wrote is read as absent and noted);
stored and carried only when true, so every document without it is byte-for-byte what it was (the golden contract file is
the proof for the wire; the cases here are the unit ones). It is on cameras only: an action that says `live` loses it.
"""
from __future__ import annotations

import pytest

from ..content import ProfileError, normalize_block, normalize_page
from ..content_resolve import resolve_block, resolve_endpoint
from .test_content_blocks import _blk, _cam, _page
from .test_content_wire_blocks import _doc_for_rev, _Registry, _States

pytestmark = pytest.mark.asyncio


def _cameras(block):
    return normalize_block(block, set(), set())["cameras"]


@pytest.mark.parametrize("value,stored", [(True, True), (False, None), (None, None)])
async def test_live_is_kept_when_true_and_otherwise_absent(value, stored):
    (camera,) = _cameras(_blk(cameras=[{**_cam(), "live": value}]))
    assert camera.get("live") is stored
    assert ("live" in camera) == (stored is not None)


async def test_a_camera_without_live_is_exactly_what_it_was():
    assert _cameras(_blk(cameras=[_cam()])) == [_cam()]
    assert _cameras(_blk(cameras=[{**_cam(), "size": "big"}])) == [{**_cam(), "size": "big"}]
    assert _cameras(_blk(cameras=[{**_cam(), "size": "big", "live": True}])) == [{**_cam(), "size": "big", "live": True}]


@pytest.mark.parametrize("bad", ["true", "yes", 1, 0, [], {}, "false"])
async def test_a_save_refuses_a_live_that_is_not_a_boolean(bad):
    with pytest.raises(ProfileError, match="camera 1: live must be true or false"):
        _cameras(_blk(cameras=[{**_cam(), "live": bad}]))


async def test_a_load_reads_a_live_it_does_not_know_as_absent_and_keeps_the_page(caplog):
    page = _page([_blk(cameras=[{**_cam("a"), "live": "maybe"}, {**_cam("b", entity="camera.b"), "live": True}])])
    with caplog.at_level("WARNING"):
        loaded = normalize_page(page, strict=False, where="template")
    a, b = loaded["blocks"][0]["cameras"]
    assert "live" not in a and b["live"] is True
    assert caplog.text.count("was read with 1 value(s) it does not know") == 1
    with pytest.raises(ProfileError):
        normalize_page(page)                                                    # the same page is refused on a save


async def test_live_belongs_to_cameras_only():
    action = {"id": "lk", "label": "Lock", "kind": "lock_lock", "entity_id": "lock.x", "live": True}
    (kept,) = normalize_block(_blk(actions=[action]), set(), set())["actions"]
    assert "live" not in kept


def _resolved(camera):
    registry = _Registry()
    return resolve_block(_blk(cameras=[camera]), states=_States(**{"camera.gate": "idle"}), registry=registry)["cameras"][0]


async def test_resolve_carries_live_onto_the_wire_only_when_true():
    live = _resolved({**_cam(), "live": True})
    plain = _resolved(_cam())
    assert live["live"] is True and live["size"] == "big" and live["entity_id"] == "camera.gate"
    assert "live" not in plain and set(plain) == {"id", "label", "entity_id", "size", "available", "name"}     # unchanged


async def test_toggling_live_changes_the_documents_rev_and_nothing_else_does_with_it_absent():
    def rev(camera):
        document = _doc_for_rev(_blk(cameras=[camera]))
        return resolve_endpoint(document, "ep", states=_States(), registry=_Registry())["rev"]

    assert rev({**_cam(), "live": True}) != rev(_cam())            # the cards must hear that a tile went live
    assert rev(_cam()) == rev(_cam())


async def test_a_saved_live_survives_the_store_and_reaches_content_get(hass, hass_ws_client):
    """Through the real save path and the real `content/get`: the editor's document in, the card's wire out."""
    from .test_editor_commands import PHONE_ID  # noqa: F401 - the fixture module's id; the world is built below
    from .test_endpoints import _phone_entry, _provide
    from .test_push_registry import _phone_user
    from .test_signaling import _setup_signaling
    from ..content import get_content_store

    phone, token = await _phone_user(hass, name="Live phone user")
    _phone_entry(hass, [{"ha_user_id": phone.id, "name": "Live phone", "enroll_id": "ep-live"}])
    await _setup_signaling(hass)
    _provide(hass, "fake", "gate-1", name="Gate", camera_entity_id="camera.gate")
    hass.states.async_set("camera.gate", "idle")
    hass.states.async_set("camera.yard", "idle")
    store = get_content_store(hass)
    await store.async_ensure_loaded()
    document = {"schema_version": 4, "template": {"pages": [_page([_blk(cameras=[
        {**_cam(), "live": True}, _cam("yard", entity="camera.yard"), {**_cam("lawn", entity="camera.lawn"), "live": False}])])],
        "rules": [], "roster": ["*"]}, "overrides": {}, "dashboards": {}, "endpoints": {}}
    await store.async_save_document(document, store.doc_rev)
    stored = store.document()["template"]["pages"][0]["blocks"][0]["cameras"]
    assert [("live" in c, c.get("live")) for c in stored] == [(True, True), (False, None), (False, None)]
    client = await hass_ws_client(hass, access_token=token)
    await client.send_json({"id": 1, "type": "portico/content/get"})
    reply = await client.receive_json()
    cameras = reply["result"]["pages"][0]["blocks"][0]["cameras"]
    assert [c.get("live") for c in cameras] == [True, None, None]
    # a later build's value in the file never drops the page: read as absent
    raw = store.document()
    raw["template"]["pages"][0]["blocks"][0]["cameras"][1]["live"] = "soon"
    with pytest.raises(ProfileError):
        await store.async_save_document(raw, store.doc_rev)
