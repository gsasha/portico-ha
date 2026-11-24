"""DEV-760 placements P1, the stores: the content store reads references through ONE expanded view, the element store knows its
users (`used_by`), refuses to remove or re-purpose a used element, and tells the content store when an element changes.
HA-harness session, plain `hass` (no websocket); the websocket side is test_content_elements_ws.py.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging

import pytest

from ..content import ContentStore, ProfileError, SaveConflict, get_content_store
from ..elements import ElementError, ElementStore, get_elements_store
from .test_content_elements import E_GATE, E_SIDE, E_YARD, _inline_and_refs, _ref
from .test_content_rules import _page
from .test_push_registry import _custom_integrations  # noqa: F401 - autouse fixture by name

pytestmark = pytest.mark.asyncio

PHONE = "ep-alice"


def _file(data):
    return {"version": 1, "minor_version": 1, "key": "portico.content", "data": data}


async def _make_elements(hass):
    """The three camera elements of the pure fixtures, with server-minted ids; returns {fixture id: real id}."""
    elements = get_elements_store(hass)
    ids = {}
    for fixture_id, raw in ((E_GATE, {"kind": "camera", "name": "Gate", "entity_id": "camera.gate"}),
                            (E_YARD, {"kind": "camera", "name": "Driveway", "entity_id": "camera.driveway",
                                      "options": {"size": "big"}}),
                            (E_SIDE, {"kind": "camera", "name": "Side", "entity_id": "camera.side"})):
        ids[fixture_id] = (await elements.async_set(raw, elements.rev))["element"]["id"]
    return elements, ids


def _swap(document, ids):
    text = json.dumps(document)
    for fixture_id, real in ids.items():
        text = text.replace(fixture_id, real)
    return json.loads(text)


def _pair(admin_id, ids):
    inline, refs = _inline_and_refs()
    for doc in (inline, refs):
        doc["dashboards"]["kitchen"]["ha_user_id"] = admin_id
        doc["overrides"][PHONE] = doc["overrides"].pop("alice")
        doc["endpoints"][PHONE] = doc["endpoints"].pop("alice")
    return inline, _swap(refs, ids)


async def _seed_v4(hass, hass_storage, data=None):
    base = {"schema_version": 4, "template": {"pages": [], "rules": [], "roster": ["*"]}, "overrides": {},
            "dashboards": {}, "endpoints": {}}
    base.update(data or {})
    hass_storage["portico.content"] = _file(copy.deepcopy(base))
    return base


# ---- the same wire, through the store -------------------------------------------------------------------------------

async def test_the_store_resolves_grants_and_rosters_the_same_for_the_reference_version(hass, hass_storage, hass_admin_user):
    elements, ids = await _make_elements(hass)
    inline, refs = _pair(hass_admin_user.id, ids)
    for entity in ("camera.gate", "camera.driveway", "camera.side"):
        hass.states.async_set(entity, "idle", {"friendly_name": entity.upper()})
    store = get_content_store(hass)
    await store.async_ensure_loaded()
    await store.async_save_document(inline, store.doc_rev)

    def observe():
        return {
            "resolved": {ep: store.resolve_document(ep) for ep in (PHONE, "kitchen")},
            "cameras": store._all_cameras(PHONE), "actions": store._all_actions(PHONE),
            "roster": store.roster_for(PHONE), "pages": store.pages_for_endpoint(PHONE),
            "profile": store.profile_for("nobody"),
        }

    before = observe()
    await store.async_save_document(refs, store.doc_rev)
    assert store.document()["schema_version"] == 5 and store.referenced_element_ids()
    after = observe()
    assert json.dumps(before, sort_keys=True) == json.dumps(after, sort_keys=True)
    assert before["resolved"][PHONE]["rev"] == after["resolved"][PHONE]["rev"]
    snapshot_ids = {c["id"] for c in after["cameras"]}
    assert {"gate", "yard", "yard2", "side", "g1"} <= snapshot_ids          # the page cameras, granted by their item ids
    # and the stored document holds the references, not copies
    assert store.document()["template"]["pages"][0]["blocks"][0]["cameras"][0] == _ref("gate", ids[E_GATE])


# ---- the lazy stamp and the one-time v4 copy -------------------------------------------------------------------------

async def test_a_v4_file_with_no_reference_is_written_as_4_and_leaves_no_v4_copy(hass, hass_storage):
    await _seed_v4(hass, hass_storage)
    store = get_content_store(hass)
    await store.async_ensure_loaded()
    await store.async_set_home_name("My Home")
    assert hass_storage["portico.content"]["data"]["schema_version"] == 4
    assert "portico.content.v4" not in hass_storage
    assert store.document()["schema_version"] == 4


async def test_the_first_save_that_stamps_5_leaves_the_v4_file_once_and_detaching_everything_returns_to_4(hass, hass_storage,
                                                                                                          hass_admin_user):
    original = await _seed_v4(hass, hass_storage, {"home_name": "Original"})      # before anything loads the content store
    elements, ids = await _make_elements(hass)
    store = get_content_store(hass)
    await store.async_ensure_loaded()
    with_ref = copy.deepcopy(store.document())
    with_ref["template"]["pages"] = [_page("home", "Home", [_ref("gate", ids[E_GATE])], [])]
    await store.async_save_document(with_ref, store.doc_rev)
    assert hass_storage["portico.content"]["data"]["schema_version"] == 5
    assert hass_storage["portico.content.v4"]["data"] == original                  # the file as it was
    # detach all: no reference left, the file is a 4 again, the copy stays as it was
    plain = copy.deepcopy(store.document())
    plain["template"]["pages"] = [_page("home", "Home", [{"id": "gate", "label": "Gate", "entity_id": "camera.gate"}], [])]
    plain["home_name"] = "Changed"
    await store.async_save_document(plain, store.doc_rev)
    assert hass_storage["portico.content"]["data"]["schema_version"] == 4
    # references again: the existing copy is never overwritten
    again = copy.deepcopy(store.document())
    again["template"]["pages"][0]["blocks"][0]["cameras"] = [_ref("gate", ids[E_GATE])]
    await store.async_save_document(again, store.doc_rev)
    assert hass_storage["portico.content"]["data"]["schema_version"] == 5
    assert hass_storage["portico.content.v4"]["data"] == original


async def test_a_file_stamped_5_is_read_back_whole_and_a_newer_one_is_still_read_only(hass, hass_storage, hass_admin_user):
    elements, ids = await _make_elements(hass)
    await _seed_v4(hass, hass_storage)
    store = get_content_store(hass)
    await store.async_ensure_loaded()
    doc = copy.deepcopy(store.document())
    doc["template"]["pages"] = [_page("home", "Home", [_ref("gate", ids[E_GATE], size="big", live=True)], [])]
    await store.async_save_document(doc, store.doc_rev)
    saved = copy.deepcopy(hass_storage["portico.content"]["data"])
    again = ContentStore(hass)
    await again.async_ensure_loaded()
    assert again.document() == store.document() and again.doc_rev == store.doc_rev
    assert again._newer_schema is None
    # a file from the future stays untouched
    newer = {**saved, "schema_version": 7}
    hass_storage["portico.content"] = _file(newer)
    future = ContentStore(hass)
    await future.async_ensure_loaded()
    with pytest.raises(ProfileError, match="schema 7"):
        await future.async_set_home_name("X")
    assert hass_storage["portico.content"]["data"] == newer


async def test_the_v4_copy_failing_to_write_never_blocks_the_save(hass, hass_storage, monkeypatch, caplog):
    await _seed_v4(hass, hass_storage)
    elements, ids = await _make_elements(hass)
    store = get_content_store(hass)
    await store.async_ensure_loaded()

    from homeassistant.helpers.storage import Store
    real = Store.async_save

    async def selective(self, data):
        if self.key == "portico.content.v4":
            raise OSError("disk full")
        return await real(self, data)

    monkeypatch.setattr(Store, "async_save", selective)
    doc = copy.deepcopy(store.document())
    doc["template"]["pages"] = [_page("home", "Home", [_ref("gate", ids[E_GATE])], [])]
    with caplog.at_level(logging.WARNING):
        await store.async_save_document(doc, store.doc_rev)
    assert hass_storage["portico.content"]["data"]["schema_version"] == 5
    assert any("could not keep a copy of the v4 file" in r.getMessage() for r in caplog.records)


# ---- the save check against the live elements ------------------------------------------------------------------------

async def _store_with(hass, hass_storage, cameras):
    await _seed_v4(hass, hass_storage)
    store = get_content_store(hass)
    await store.async_ensure_loaded()
    doc = copy.deepcopy(store.document())
    doc["template"]["pages"] = [_page("home", "Home", cameras, [])]
    return store, doc


async def test_a_new_reference_to_a_missing_wrong_kind_or_stream_only_element_is_refused_naming_the_item(hass, hass_storage):
    elements, ids = await _make_elements(hass)
    button = (await elements.async_set({"kind": "button", "name": "Lock", "entity_id": "lock.front",
                                        "options": {"action": "lock_lock"}}, elements.rev))["element"]["id"]
    stream = (await elements.async_set({"kind": "camera", "name": "Stream", "options": {"stream": "rtsp://10.6.0.6:8554/a"}},
                                       elements.rev))["element"]["id"]
    store, doc = await _store_with(hass, hass_storage, [])
    for element_id, code in (("el-deadbeef", "element_missing"), (button, "element_kind"), (stream, "not_placeable")):
        bad = copy.deepcopy(doc)
        bad["template"]["pages"][0]["blocks"][0]["cameras"] = [_ref("cam-1", element_id)]
        with pytest.raises(ProfileError, match=rf"{code}.*cam-1"):
            await store.async_save_document(bad, store.doc_rev)
        with pytest.raises(ProfileError, match=code):
            store.prepare_document(bad)                       # content/preview takes the same path
    assert store.document()["template"]["pages"] == []


async def test_a_reference_already_stored_whose_element_vanished_is_kept_so_other_edits_still_save(hass, hass_storage, caplog):
    elements, ids = await _make_elements(hass)
    store, doc = await _store_with(hass, hass_storage, [_ref("gate", ids[E_GATE])])
    await store.async_save_document(doc, store.doc_rev)
    # the element is gone behind the content store's back (an older build, a restore)
    elements._elements = [e for e in elements._elements if e["id"] != ids[E_GATE]]
    edited = copy.deepcopy(store.document())
    edited["home_name"] = "Renamed home"
    await store.async_save_document(edited, store.doc_rev)
    assert store.document()["template"]["pages"][0]["blocks"][0]["cameras"] == [_ref("gate", ids[E_GATE])]
    # a NEW reference to the same vanished id from a second tile is still allowed (it is already in the stored document)...
    # ...but a different vanished id is not
    worse = copy.deepcopy(store.document())
    worse["template"]["pages"][0]["blocks"][0]["cameras"].append(_ref("other", "el-0badf00d"))
    with pytest.raises(ProfileError, match="element_missing"):
        await store.async_save_document(worse, store.doc_rev)
    # reading it: the tile is left out, the page and the rest survive, and the warning is spoken once per element
    with caplog.at_level(logging.WARNING):
        for _ in range(3):
            page = store.resolve_document(PHONE)["pages"][0]
    assert page["blocks"][0]["cameras"] == []
    assert sum("is left out" in r.getMessage() and ids[E_GATE] in r.getMessage() for r in caplog.records) == 1


async def test_a_reference_to_an_element_being_removed_is_refused_by_the_save(hass, hass_storage):
    elements, ids = await _make_elements(hass)
    store, doc = await _store_with(hass, hass_storage, [_ref("gate", ids[E_GATE])])
    release = asyncio.Event()
    started = asyncio.Event()
    real = elements._store.async_save

    async def slow(data):
        started.set()
        await release.wait()
        return await real(data)

    elements._store.async_save = slow
    unused = ids[E_YARD]
    delete = asyncio.ensure_future(elements.async_delete(unused, elements.rev))
    await started.wait()
    assert unused in elements.pending_delete
    doc["template"]["pages"][0]["blocks"][0]["cameras"] = [_ref("gate", ids[E_GATE]), _ref("yard", unused)]
    with pytest.raises(ProfileError, match="element_pending"):
        await store.async_save_document(doc, store.doc_rev)
    release.set()
    await delete
    assert unused not in elements.pending_delete
    with pytest.raises(ProfileError, match="element_missing"):             # and once it is gone, the ordinary check
        await store.async_save_document(doc, store.doc_rev)


async def test_a_stale_base_still_conflicts_with_references_in_the_document(hass, hass_storage):
    elements, ids = await _make_elements(hass)
    store, doc = await _store_with(hass, hass_storage, [_ref("gate", ids[E_GATE])])
    with pytest.raises(SaveConflict):
        await store.async_save_document(doc, "0" * 12)


# ---- element edits reach the screens -------------------------------------------------------------------------------------

async def test_an_element_edit_announces_to_every_subscriber_only_while_a_screen_uses_an_element(hass, hass_storage):
    elements, ids = await _make_elements(hass)
    store, doc = await _store_with(hass, hass_storage, [])
    calls = []
    store.add_home_listener(lambda: calls.append("home"))
    other = (await elements.async_set({"kind": "status", "name": "Hall", "entity_id": "sensor.hall"}, elements.rev))["element"]
    assert calls == []                                              # no screen uses an element: nobody is woken
    doc["template"]["pages"][0]["blocks"][0]["cameras"] = [_ref("gate", ids[E_GATE])]
    await store.async_save_document(doc, store.doc_rev)
    calls.clear()
    rev_before, doc_rev_before = store.resolve_document(PHONE)["rev"], store.doc_rev
    gate = next(e for e in elements._elements if e["id"] == ids[E_GATE])
    await elements.async_set({**gate, "name": "Front gate"}, elements.rev)
    assert calls == ["home"]
    assert store.doc_rev == doc_rev_before                         # an element edit is no conflict for an open editor
    assert store.resolve_document(PHONE)["rev"] != rev_before      # but the screens see the new look
    assert store.resolve_document(PHONE)["pages"][0]["blocks"][0]["cameras"][0]["label"] == "Front gate"
    calls.clear()
    await elements.async_set({**other, "name": "Hall 2"}, elements.rev)
    assert calls == ["home"]                                       # any element edit while references exist: cheap and safe
    elements.remove_listener(store._on_elements_changed)


async def test_an_inline_camera_follows_its_elements_small_stream_and_the_screens_are_told(hass, hass_storage):
    """DEV-768: a screen built before small streams holds inline cameras (an entity, no element reference). The element for
    that entity still gives it `small: true`, an element edit announces to the screens, and the live grant carries the entity."""
    elements, ids = await _make_elements(hass)
    store, doc = await _store_with(hass, hass_storage, [{"id": "cam-1", "label": "Gate", "entity_id": "camera.gate", "live": True}])
    await store.async_save_document(doc, store.doc_rev)
    calls = []
    store.add_home_listener(lambda: calls.append("home"))
    before = store.resolve_document(PHONE)
    assert "small" not in before["pages"][0]["blocks"][0]["cameras"][0]
    gate = next(e for e in elements._elements if e["id"] == ids[E_GATE])
    await elements.async_set({**gate, "options": {"small_entity_id": "camera.gate_sub"}}, elements.rev)
    assert calls == ["home"]                                       # no reference in the document, still announced
    after = store.resolve_document(PHONE)
    assert after["pages"][0]["blocks"][0]["cameras"][0]["small"] is True
    assert after["rev"] != before["rev"] and "gate_sub" not in json.dumps(after)
    assert store.fallback_small_entity("camera.gate") == "camera.gate_sub" and store.fallback_small_entity("camera.side") is None
    hass.states.async_set("camera.gate", "idle")
    assert (await store.async_live_cameras("u", "p", "cam-1", endpoint_id=PHONE))[1] == "camera.gate_sub"
    elements.remove_listener(store._on_elements_changed)


async def test_a_failing_listener_never_fails_the_element_write(hass, caplog):
    elements = get_elements_store(hass)
    elements.add_listener(lambda: 1 / 0)
    with caplog.at_level(logging.WARNING):
        result = await elements.async_set({"kind": "status", "name": "Hall", "entity_id": "sensor.hall"}, elements.rev)
    assert result["element"]["name"] == "Hall" and "a change listener failed" in caplog.text


async def test_content_loads_even_when_the_element_store_cannot(hass, hass_storage, monkeypatch, caplog):
    await _seed_v4(hass, hass_storage)

    async def boom(self):
        raise OSError("broken")

    monkeypatch.setattr(ElementStore, "async_ensure_loaded", boom)
    store = get_content_store(hass)
    with caplog.at_level(logging.WARNING):
        await store.async_ensure_loaded()
    assert store.loaded and "elements could not be loaded" in caplog.text


# ---- used_by is real ----------------------------------------------------------------------------------------------------

async def test_used_by_lists_every_screen_and_delete_of_a_used_element_is_refused_with_no_override(hass, hass_storage,
                                                                                                  hass_admin_user):
    elements, ids = await _make_elements(hass)
    inline, refs = _pair(hass_admin_user.id, ids)
    store = get_content_store(hass)
    await store.async_ensure_loaded()
    assert elements.used_by(ids[E_GATE]) == [] and elements.used_by_map() == {i: [] for i in ids.values()}
    await store.async_save_document(refs, store.doc_rev)
    users = elements.used_by(ids[E_GATE])
    assert {"endpoint": "template", "label": "Home > Home", "page": "home", "block": "home"} in users
    assert {"endpoint": "template", "label": "Rule r1"} in users
    assert {"endpoint": "kitchen", "label": "Screen cameras"} in users
    assert elements.used_by_map()[ids[E_GATE]] == users
    with pytest.raises(ElementError) as err:
        await elements.async_delete(ids[E_GATE], elements.rev)
    assert err.value.code == "in_use" and err.value.used_by == users
    assert any(e["id"] == ids[E_GATE] for e in elements._elements)
    # remove the references, then it goes
    await store.async_save_document(inline, store.doc_rev)
    await elements.async_delete(ids[E_GATE], elements.rev)
    assert all(e["id"] != ids[E_GATE] for e in elements._elements)


async def test_used_by_is_empty_before_the_content_store_has_loaded_and_loading_it_fills_it(hass, hass_storage):
    elements, ids = await _make_elements(hass)
    store, doc = await _store_with(hass, hass_storage, [_ref("gate", ids[E_GATE])])
    await store.async_save_document(doc, store.doc_rev)
    probe = ElementStore(hass)
    assert probe.used_by(ids[E_GATE]) != []                      # the singleton content store is loaded and holds the reference
    hass.data["portico"].pop("content")                          # a restart: a new content store, not yet loaded
    assert probe.used_by(ids[E_GATE]) == []
    await probe.async_ensure_content()
    assert probe.used_by(ids[E_GATE]) != []


# ---- the guards on a used element (P3 concern, in place now) ---------------------------------------------------------------

async def _button(hass):
    elements = get_elements_store(hass)
    raw = {"kind": "button", "name": "Gate", "entity_id": "cover.gate", "options": {"action": "cover_open"}}
    return elements, (await elements.async_set(raw, elements.rev))["element"]


async def test_the_action_of_a_used_button_cannot_change_its_entity_can_and_warns_with_the_screens(hass, monkeypatch):
    elements, button = await _button(hass)
    users = [{"endpoint": "template", "label": "Home > Gate", "page": "home", "block": "gate"}]
    # unused: anything goes
    free = await elements.async_set({**button, "options": {"action": "cover_close"}}, elements.rev)
    button = free["element"]
    monkeypatch.setattr(elements, "used_by", lambda element_id: users)
    with pytest.raises(ElementError) as err:
        await elements.async_set({**button, "options": {"action": "cover_open"}}, elements.rev)
    assert err.value.code == "in_use_action" and err.value.field == "options.action" and err.value.used_by == users
    # the same action with another entity: refused until confirmed, naming the screens; validate_only reports it too
    changed = {**button, "entity_id": "cover.garage"}
    for kwargs in ({"validate_only": True}, {}):
        with pytest.raises(ElementError) as err:
            await elements.async_set(changed, elements.rev, **kwargs)
        assert err.value.code == "in_use_retarget" and err.value.used_by == users
    assert next(e for e in elements._elements if e["id"] == button["id"])["entity_id"] == "cover.gate"      # nothing written
    await elements.async_set(changed, elements.rev, validate_only=True, confirm=True)        # a confirmed check still writes nothing
    assert next(e for e in elements._elements if e["id"] == button["id"])["entity_id"] == "cover.gate"
    done = await elements.async_set(changed, elements.rev, confirm=True)
    assert next(e for e in elements._elements if e["id"] == button["id"])["entity_id"] == "cover.garage"
    # a rename or a colour change needs no confirm
    await elements.async_set({**done["element"], "name": "Garage"}, elements.rev)


async def test_a_used_camera_cannot_become_stream_only_and_a_new_element_has_no_guard(hass, monkeypatch):
    elements = get_elements_store(hass)
    cam = (await elements.async_set({"kind": "camera", "name": "Gate", "entity_id": "camera.gate"}, elements.rev))["element"]
    users = [{"endpoint": "template", "label": "Home > Gate"}]
    monkeypatch.setattr(elements, "used_by", lambda element_id: users)
    stream_only = {"id": cam["id"], "kind": "camera", "name": "Gate", "options": {"stream": "rtsp://10.6.0.6:8554/gate"}}
    with pytest.raises(ElementError) as err:
        await elements.async_set(stream_only, elements.rev)
    assert err.value.code == "in_use_stream" and err.value.used_by == users
    # another entity: needs the confirm
    with pytest.raises(ElementError) as err:
        await elements.async_set({**cam, "entity_id": "camera.driveway"}, elements.rev)
    assert err.value.code == "in_use_retarget"
    await elements.async_set({**cam, "entity_id": "camera.driveway"}, elements.rev, confirm=True)
    # a brand-new element (no id) is never guarded
    fresh = await elements.async_set({"kind": "camera", "name": "Stream", "options": {"stream": "rtsp://10.6.0.6:8554/x"}},
                                     elements.rev)
    assert fresh["element"]["id"]

