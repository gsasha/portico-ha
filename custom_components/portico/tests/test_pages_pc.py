"""Pages unified P-C, server half (Devices D2): who owns a Talk element (one on several screens is allowed and wakes each of them,
D-20261122-42b1), `wake` in `content/document`, `content/get` and `content_changed`, and the `in_use` refusal for deleting a library page a device
shows. The pure ownership functions first, then the real store over a websocket."""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from .. import content, pages_ownership as own
from ..content import ContentStore
from ..elements import get_elements_store
from ..signaling import _content_changed
from .test_editor_commands import _send
from .test_endpoints import _phone_entry, _provide
from .test_push_registry import _phone_user
from .test_signaling import _setup_signaling

pytestmark = pytest.mark.asyncio

T_GATE, T_SIDE = "el-00000001", "el-00000002"
PARTIES = {
    T_GATE: ("door", "gate-1"), T_SIDE: ("door", "side-1"),
    "talk-screen-kitchen": ("screen", "kitchen"), "talk-phone-alice": ("phone", "alice"),
}


def _page(page_id, *talks, kind="content"):
    return {"id": page_id, "label": page_id.title(), "kind": kind,
            "blocks": [{"id": "b", "title": "B", "cameras": [], "actions": [{"id": f"a-{t}", "element": t} for t in talks]}]}


def _doc(pages, devices, screens=("kitchen", "hall")):
    return {"schema_version": 6, "pages": pages, "dashboards": {s: {"name": s.title(), "ha_user_id": f"u-{s}", "cameras": []}
                                                                for s in screens},
            "devices": {k: {"pages": [{"page": p} for p in v], "ring": {}} for k, v in devices.items()}, "endpoints": {}}


# ---- the pure part ------------------------------------------------------------------------------------------------------

async def test_a_screen_contains_the_door_talk_elements_of_its_own_pages_and_nothing_else():
    doc = _doc([_page("p1", T_GATE), _page("p2", T_SIDE, "talk-screen-kitchen", "talk-phone-alice")],
               {"kitchen": ["p1"], "hall": ["p2"], "alice": ["p1", "p2"]})
    assert own.door_owners(doc, PARTIES) == {T_GATE: ["kitchen"], T_SIDE: ["hall"]}     # alice is no screen; targets are not placed
    wake = own.wake_map(doc, PARTIES)
    assert wake["kitchen"]["doors"] == ["gate-1"] and wake["kitchen"]["elements"] == [T_GATE]
    assert wake["kitchen"]["self_elements"] == ["talk-screen-kitchen"] and wake["kitchen"]["screen_call"] is True
    assert wake["hall"]["doors"] == ["side-1"] and wake["hall"]["self_elements"] == []


async def test_a_page_has_no_idea_where_it_lands_the_same_page_on_two_screens_wakes_both():
    doc = _doc([_page("shared", T_GATE), _page("own", T_SIDE)], {"kitchen": ["shared", "own"], "hall": ["shared"]})
    assert own.door_owners(doc, PARTIES) == {T_GATE: ["hall", "kitchen"], T_SIDE: ["kitchen"]}
    wake = own.wake_map(doc, PARTIES)
    assert wake["kitchen"]["doors"] == ["gate-1", "side-1"] and wake["hall"]["doors"] == ["gate-1"]
    assert own.wake_for_element(doc, PARTIES, T_GATE) == ["hall", "kitchen"]         # every screen that holds it
    assert own.wake_for_element(doc, PARTIES, T_SIDE) == ["kitchen"]
    assert own.screens_for_door(doc, PARTIES, "gate-1") == ["hall", "kitchen"] and own.screens_for_door(doc, PARTIES, "side-1") == ["kitchen"]


async def test_wake_for_a_screen_or_phone_element_and_for_a_door_nobody_holds():
    doc = _doc([_page("p1", T_GATE)], {"kitchen": ["p1"]})
    assert own.wake_for_element(doc, PARTIES, "talk-screen-kitchen") == ["kitchen"]
    assert own.wake_for_element(doc, PARTIES, "talk-phone-alice") == []
    assert own.wake_for_element(doc, PARTIES, "el-ffffffff") == []
    assert own.screens_for_door(doc, PARTIES, "side-1") == []                        # phones only
    assert own.wake_for_element({"schema_version": 4}, PARTIES, T_GATE) == [] and own.wake_map({"schema_version": 4}, PARTIES) == {}


async def test_the_users_of_a_page_are_the_devices_that_show_it_and_a_ring_page_has_none():
    doc = _doc([_page("a"), _page("r", kind="ring")], {"kitchen": ["a"], "alice": []})
    doc["devices"]["alice"]["ring"] = {T_GATE: "r"}          # a stale per-device map of a restored document is not an owner
    assert own.device_users_of_page(doc, "a") == ["kitchen"] and own.device_users_of_page(doc, "r") == []


# ---- the store over the websocket ---------------------------------------------------------------------------------------

def _file(data):
    return {"version": 1, "minor_version": 1, "key": "portico.content", "data": data}


@pytest.fixture
async def pworld(hass, hass_storage, hass_admin_user):
    phone, token = await _phone_user(hass, name="Alice phone user")
    _phone_entry(hass, [{"ha_user_id": phone.id, "name": "Alice's phone", "enroll_id": "alice"}])
    await _setup_signaling(hass)
    _provide(hass, "fake", "gate-1", "side-1", name="A door")
    users = {}
    for slug in ("kitchen", "hall", "yard"):
        users[slug] = await hass.auth.async_create_user(f"{slug} tablet", group_ids=["system-users"])
    return SimpleNamespace(hass=hass, storage=hass_storage, admin=hass_admin_user, users=users, phone=phone)


async def _talks(world):
    elements = get_elements_store(world.hass)
    await elements.async_ensure_loaded()
    ids = []
    for door, name in (("gate-1", "Gate"), ("side-1", "Side door")):
        made = await elements.async_set({"kind": "talk", "name": name, "options": {"door": door}}, elements.rev)
        ids.append(made["element"]["id"])
    return ids


def _world_doc(world, gate, side, devices, *, shared=True):
    pages = [_page("p-gate", gate), _page("p-side", side), _page("p-plain")]
    doc = _doc(pages, devices, ("kitchen", "hall", "yard"))
    for slug, user in world.users.items():
        doc["dashboards"][slug]["ha_user_id"] = user.id
    return doc


async def _load(world, data):
    world.storage["portico.content"] = _file(copy.deepcopy(data))
    store = ContentStore(world.hass)
    world.hass.data["portico"][content.DATA_CONTENT] = store
    await store.async_ensure_loaded()
    return store


async def _client(world, hass_ws_client):
    return await hass_ws_client(world.hass)


async def test_a_save_that_puts_a_door_talk_element_on_a_second_screen_succeeds_and_wakes_both(pworld, hass_ws_client):
    gate, side = await _talks(pworld)
    store = await _load(pworld, _world_doc(pworld, gate, side, {"kitchen": ["p-gate"], "hall": ["p-plain"]}))
    admin = await _client(pworld, hass_ws_client)
    got = (await _send(admin, "portico/content/document"))["result"]
    assert "talk_conflicts" not in got and got["wake"]["kitchen"]["doors"] == ["gate-1"] and got["wake"]["hall"]["doors"] == []
    edited = copy.deepcopy(got["document"])
    edited["devices"]["hall"]["pages"] = [{"page": "p-gate"}]
    resp = await _send(admin, "portico/content/save", 2, document=edited, base_doc_rev=got["doc_rev"])
    assert resp["success"], resp                                                   # no refusal (D-20261122-42b1)
    assert store.wake_for_talk_element(gate) == ["hall", "kitchen"] and store.screens_for_door("gate-1") == ["hall", "kitchen"]
    assert store.wake_for_endpoint("hall")["doors"] == ["gate-1"] and store.wake_for_endpoint("kitchen")["doors"] == ["gate-1"]
    again = (await _send(admin, "portico/content/document", 4))["result"]
    assert again["wake"]["hall"]["doors"] == ["gate-1"] and again["wake"]["kitchen"]["doors"] == ["gate-1"]
    ok = copy.deepcopy(got["document"])                                            # a second element for the same door is fine
    ok["devices"]["hall"]["pages"] = [{"page": "p-side"}]
    resp = await _send(admin, "portico/content/save", 5, document=ok, base_doc_rev=again["doc_rev"])
    assert resp["success"], resp


async def test_content_get_and_content_changed_carry_wake_for_a_screen_only(pworld, hass_ws_client):
    gate, side = await _talks(pworld)
    store = await _load(pworld, _world_doc(pworld, gate, side, {"kitchen": ["p-gate"], "alice": ["p-gate"]}))
    admin = await _client(pworld, hass_ws_client)
    resp = await _send(admin, "portico/content/get", 1, endpoint="kitchen")
    assert resp["success"], resp
    assert resp["result"]["wake"] == {"doors": ["gate-1"], "screen_call": True}
    assert "wake" not in (await _send(admin, "portico/content/get", 2, endpoint="alice"))["result"]    # a phone: wire unchanged
    assert _content_changed(store, "kitchen")["wake"] == {"doors": ["gate-1"], "screen_call": True}
    assert "wake" not in _content_changed(store, "alice")
    assert store.screens_for_door("gate-1") == ["kitchen"] and store.screens_for_door("side-1") == []


async def test_a_legacy_document_has_no_wake(pworld, hass_ws_client):
    legacy = {"schema_version": 4, "template": {"pages": [], "rules": [], "roster": ["*"]},
              "overrides": {"alice": {"pages": [], "rules": [], "roster_hide": []}}, "dashboards": {}, "endpoints": {}}
    store = await _load(pworld, legacy)
    admin = await _client(pworld, hass_ws_client)
    got = (await _send(admin, "portico/content/document"))["result"]
    assert "wake" not in got and "talk_conflicts" not in got
    assert "wake" not in store.resolve_for_endpoint("alice") and store.wake_for_endpoint("alice") is None


async def test_deleting_a_library_page_a_device_shows_is_refused_with_the_device_list(pworld, hass_ws_client):
    gate, side = await _talks(pworld)
    store = await _load(pworld, _world_doc(pworld, gate, side, {"kitchen": ["p-gate"], "alice": ["p-gate"], "hall": ["p-plain"]}))
    admin = await _client(pworld, hass_ws_client)
    got = (await _send(admin, "portico/content/document"))["result"]
    edited = copy.deepcopy(got["document"])
    edited["pages"] = [p for p in edited["pages"] if p["id"] != "p-gate"]
    resp = await _send(admin, "portico/content/save", 2, document=edited, base_doc_rev=got["doc_rev"])
    assert resp["success"] is False and resp["error"]["code"] == "in_use"
    assert resp["error"]["page"] == "p-gate" and resp["error"]["devices"] == ["alice", "kitchen"]
    assert "Kitchen" in resp["error"]["message"]
    for key in ("kitchen", "alice"):                                               # unassigned in the same save: allowed
        edited["devices"][key]["pages"] = []
    resp = await _send(admin, "portico/content/save", 3, document=edited, base_doc_rev=got["doc_rev"])
    assert resp["success"], resp
    assert all(p["id"] != "p-gate" for p in store.library)


async def test_assign_save_resolve_the_editor_fixture_a_device_shows_what_its_list_says_in_its_order(pworld, hass_ws_client):
    gate, side = await _talks(pworld)
    store = await _load(pworld, _world_doc(pworld, gate, side, {"alice": [], "kitchen": []}))
    admin = await _client(pworld, hass_ws_client)
    got = (await _send(admin, "portico/content/document"))["result"]
    edited = copy.deepcopy(got["document"])
    edited["devices"]["alice"]["pages"] = [{"page": "p-plain"}, {"page": "p-side"}]
    edited["devices"]["kitchen"]["pages"] = [{"page": "p-gate"}]
    resp = await _send(admin, "portico/content/save", 2, document=edited, base_doc_rev=got["doc_rev"])
    assert resp["success"], resp
    assert [p["id"] for p in store.resolve_document("alice")["pages"]] == ["p-plain", "p-side"]      # the first is Home
    assert [p["id"] for p in store.resolve_for_endpoint("kitchen")["pages"]] == ["p-gate"]
    assert store.resolve_for_endpoint("kitchen")["wake"]["doors"] == ["gate-1"]
    again = (await _send(admin, "portico/content/document", 3))["result"]
    assert again["wake"]["kitchen"]["elements"] == [gate] and "talk_conflicts" not in again
