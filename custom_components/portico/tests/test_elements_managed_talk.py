"""DEVICES-10 / D1: managed Talk elements (one per screen and per enrolled phone), their reconcile, and their expansion to the
existing `call` action (design home `portico_devices_design.md` 4.4; owner D-20261109-4c5c).

Pinned: validation (exactly one of door / screen / phone; `managed` is never believed from a client); the reconcile (creates
the missing element of each phone and screen, idempotent, keeps a name the user chose, keeps an orphan and lists it
`target_removed`, never deletes); the deletion refusal while the phone or screen exists; the managed element's target is locked;
the expansion to a `call` action aimed at the phone or screen; V1 (the server's own rules decide `available` and `reason`);
and additivity (nothing placed: every resolved document and the content store file are unchanged by the reconcile).
"""
from __future__ import annotations

import copy

import pytest

from .. import managed_talk
from ..content import get_content_store
from ..content_elements import expand_action, is_element_id
from ..elements import ElementError, get_elements_store, validate_element
from .house_env import build_house
from .test_push_registry import _custom_integrations  # noqa: F401 - autouse fixture by name

pytestmark = pytest.mark.asyncio


# ---- validation (pure) -----------------------------------------------------------------------------------------

def _talk(**options):
    return {"kind": "talk", "name": "T", "options": options}


@pytest.mark.parametrize("options", [{"door": "gate-1"}, {"screen": "kitchen"}, {"phone": "ep-alice"}, {"group": "everyone"},
                                     {"group": "phones"}, {"group": "panels"}])
async def test_a_talk_element_names_exactly_one_target(options):
    assert validate_element(_talk(**options))["options"] == options


@pytest.mark.parametrize("options", [{}, {"door": "a", "phone": "b"}, {"screen": "a", "phone": "b"},
                                     {"door": "a", "screen": "b", "phone": "c"}, {"phone": ""}, {"screen": 3},
                                     {"phone": "x" * 161}, {"group": "everyone", "door": "a"}, {"group": "everyone", "phone": "b"},
                                     {"group": "nobody"}, {"group": ""}, {"group": 3}, {"group": "Everyone"}])
async def test_a_talk_element_with_none_or_two_targets_is_refused(options):
    with pytest.raises(ElementError) as err:
        validate_element(_talk(**options))
    assert err.value.code == "invalid"


async def test_managed_is_never_believed_from_a_client_and_must_agree_with_the_id():
    managed = {"id": "talk-phone-ep-alice", "kind": "talk", "name": "Alice", "options": {"phone": "ep-alice"}, "managed": True}
    with pytest.raises(ElementError):
        validate_element(managed)                                              # the flag, from a client
    assert validate_element(managed, managed_ok=True)["managed"] is True
    for bad in ({**managed, "options": {"phone": "other"}},                    # id and target disagree
                {**managed, "options": {"screen": "ep-alice"}},
                {**managed, "managed": False},
                {**managed, "id": "el-0123abcd"},
                {**managed, "kind": "status", "options": {}}):
        with pytest.raises(ElementError):
            validate_element(bad, managed_ok=True)
    plain = {k: v for k, v in managed.items() if k != "managed"}
    with pytest.raises(ElementError):
        validate_element(plain)                                                # the managed id shape is for managed elements
    assert is_element_id("talk-phone-ep-alice") and is_element_id("talk-screen-kitchen") and not is_element_id("talk-x-1")


# ---- the reconcile ---------------------------------------------------------------------------------------------

async def _house(hass, hass_storage):
    house = await build_house(hass, hass_storage)
    elements = get_elements_store(hass)
    await elements.async_ensure_loaded()
    return house, elements


def _by_id(listing):
    return {e["id"]: e for e in listing["elements"]}


async def test_reconcile_creates_one_managed_element_per_phone_and_per_screen_and_is_idempotent(hass, hass_storage):
    _house_, elements = await _house(hass, hass_storage)
    await hass.async_block_till_done()                                      # the startup request, if it ran
    created = await managed_talk.async_reconcile(hass)
    listing = _by_id(await elements.async_list())
    assert set(listing) >= {"talk-phone-alice", "talk-phone-dina", "talk-screen-kitchen"}
    assert created == [] or set(created) <= set(listing)
    for element_id, options, name in (("talk-phone-alice", {"phone": "alice"}, "Alice"),
                                      ("talk-phone-dina", {"phone": "dina"}, "Dina"),
                                      ("talk-screen-kitchen", {"screen": "kitchen"}, "Kitchen")):
        assert listing[element_id]["managed"] is True and listing[element_id]["kind"] == "talk"
        assert listing[element_id]["options"] == options and listing[element_id]["name"] == name
        assert "target_removed" not in listing[element_id]
    rev = elements.rev
    assert await managed_talk.async_reconcile(hass) == [] and elements.rev == rev        # nothing missing: nothing written


async def test_reconcile_keeps_a_users_name_and_makes_a_name_that_is_taken_unique(hass, hass_storage):
    _house_, elements = await _house(hass, hass_storage)
    await elements.async_set({"kind": "status", "name": "Dina", "entity_id": "sensor.x", "options": {}}, elements.rev)
    await managed_talk.async_reconcile(hass)
    listing = _by_id(await elements.async_list())
    assert listing["talk-phone-dina"]["name"] == "Dina (phone)"
    renamed = {**listing["talk-phone-alice"], "name": "Alice's Pixel"}
    renamed.pop("managed")
    await elements.async_set(renamed, elements.rev)                          # a rename keeps the flag (server-side)
    await managed_talk.async_reconcile(hass)
    again = _by_id(await elements.async_list())["talk-phone-alice"]
    assert again["name"] == "Alice's Pixel" and again["managed"] is True


async def test_a_phone_enrolled_later_gets_its_element(hass, hass_storage):
    from .house_env import phone_entry

    _house_, elements = await _house(hass, hass_storage)
    await managed_talk.async_reconcile(hass)
    phone_entry(hass, "u-newphone", "newphone", "New phone")
    managed_talk.async_request_reconcile(hass)
    await hass.async_block_till_done()
    assert "talk-phone-newphone" in _by_id(await elements.async_list())


async def test_a_screen_created_later_gets_its_element_through_the_content_save(hass, hass_storage):
    _house_, elements = await _house(hass, hass_storage)
    managed_talk.async_setup_managed_talk(hass)                              # what async_setup does at start
    await hass.async_block_till_done()
    store = get_content_store(hass)
    user = await hass.auth.async_create_user("Hall tablet", group_ids=["system-users"])
    document = store.document()
    document["dashboards"]["hall"] = {"name": "Hall screen", "ha_user_id": user.id, "room": "Hall", "cameras": []}
    await store.async_save_document(document, store.doc_rev)
    await hass.async_block_till_done()
    assert "talk-screen-hall" in _by_id(await elements.async_list())


async def test_an_orphan_is_kept_and_listed_as_removed_and_can_then_be_deleted(hass, hass_storage):
    house, elements = await _house(hass, hass_storage)
    await managed_talk.async_reconcile(hass)
    entry = next(e for e in hass.config_entries.async_entries("portico") if e.data.get("enroll_id") == "dina")
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    listing = await elements.async_list()
    assert _by_id(listing)["talk-phone-dina"]["target_removed"] is True
    assert "target_removed" not in _by_id(listing)["talk-phone-alice"]
    assert await managed_talk.async_reconcile(hass) == []                    # never deleted, never re-created
    assert "talk-phone-dina" in _by_id(await elements.async_list())
    await elements.async_delete("talk-phone-dina", elements.rev)             # the user may remove an orphan
    assert "talk-phone-dina" not in _by_id(await elements.async_list())
    assert await managed_talk.async_reconcile(hass) == []


async def test_deletion_is_refused_while_the_phone_or_screen_exists(hass, hass_storage):
    _house_, elements = await _house(hass, hass_storage)
    await managed_talk.async_reconcile(hass)
    for element_id in ("talk-phone-alice", "talk-screen-kitchen"):
        with pytest.raises(ElementError) as err:
            await elements.async_delete(element_id, elements.rev)
        assert err.value.code == "managed_target_exists"
        assert element_id in _by_id(await elements.async_list())


async def test_a_managed_element_can_only_be_renamed(hass, hass_storage):
    _house_, elements = await _house(hass, hass_storage)
    await managed_talk.async_reconcile(hass)
    mine = _by_id(await elements.async_list())["talk-phone-alice"]
    for patch in ({"options": {"phone": "dina"}}, {"options": {"door": "gate-1"}}):
        with pytest.raises(ElementError) as err:
            await elements.async_set({**{k: v for k, v in mine.items() if k != "managed"}, **patch}, elements.rev)
        assert err.value.code in ("managed_locked", "invalid")
    with pytest.raises(ElementError):                                          # a client cannot mint one either
        await elements.async_set({"id": "talk-phone-zed", "kind": "talk", "name": "Z", "options": {"phone": "zed"},
                                  "managed": True}, elements.rev)


async def test_a_user_element_must_name_a_phone_or_screen_that_exists(hass, hass_storage):
    _house_, elements = await _house(hass, hass_storage)
    for options in ({"phone": "nobody"}, {"screen": "nowhere"}):
        with pytest.raises(ElementError) as err:
            await elements.async_set({"kind": "talk", "name": "X", "options": options}, elements.rev)
        assert err.value.field in ("options.phone", "options.screen")
    made = await elements.async_set({"kind": "talk", "name": "Mine", "options": {"phone": "dina"}}, elements.rev)
    assert "managed" not in made["element"] and made["element"]["id"].startswith("el-")


# ---- expansion to `call`, V1 and additivity --------------------------------------------------------------------

async def test_expansion_is_the_existing_call_action():
    for options, target in (({"phone": "ep-1"}, {"type": "phone", "id": "ep-1"}),
                            ({"screen": "kitchen"}, {"type": "dashboard", "id": "kitchen"})):
        element = {"id": "x", "kind": "talk", "name": "Kitchen", "icon": "phone", "options": options}
        out = expand_action({"id": "a1", "element": "x"}, element)
        assert out == {"id": "a1", "label": "Kitchen", "kind": "call", "target": target, "icon": "phone", "confirm": False}
    door = expand_action({"id": "a1", "element": "x"}, {"kind": "talk", "name": "G", "options": {"door": "gate-1"}})
    assert door["kind"] == "talk" and door["door"] == "gate-1"                  # the door Talk is unchanged


def _doc(house, hass, *element_ids):
    store = get_content_store(hass)
    document = {"schema_version": 4,
                "template": {"pages": [{"id": "home", "label": "Home", "blocks": [
                    {"id": "calls", "title": "Calls", "cameras": [],
                     "actions": [{"id": f"c{i}", "element": eid} for i, eid in enumerate(element_ids)]}]}],
                             "rules": [], "roster": ["*"]},
                "overrides": {}, "dashboards": copy.deepcopy(store.dashboards), "endpoints": {}}
    return store, document


async def test_v1_the_servers_rules_decide_off_and_the_reason(hass, hass_storage):
    house, elements = await _house(hass, hass_storage)
    await managed_talk.async_reconcile(hass)
    for who in ("alice", "dina", "kitchen"):
        house.online(who)
    store, document = _doc(house, hass, "talk-phone-dina", "talk-screen-kitchen")
    await store.async_save_document(document, store.doc_rev)
    page = store.resolve_document("alice")["pages"][0]
    actions = {a["id"]: a for b in page["blocks"] for a in b["actions"]}
    dina, kitchen = actions["c0"], actions["c1"]
    assert dina["kind"] == "call" and dina["target"] == {"type": "phone", "id": "dina"}
    assert kitchen["kind"] == "call" and kitchen["target"] == {"type": "dashboard", "id": "kitchen"}
    assert kitchen["available"] is True                                         # a declared screen is callable
    assert dina["available"] is True and dina["reason"] is None
    entry = next(e for e in hass.config_entries.async_entries("portico") if e.data.get("enroll_id") == "dina")
    from ..phone_entry import CONF_HOUSE_RECEIVE

    hass.config_entries.async_update_entry(entry, options={**entry.options, CONF_HOUSE_RECEIVE: False})
    off = next(a for b in store.resolve_document("alice")["pages"][0]["blocks"] for a in b["actions"] if a["id"] == "c0")
    assert off["available"] is False and off["reason"] == "receive_off"          # Dina's own switch decides
    result = house.place("alice", "phone", "dina")
    assert result.ok is False and result.code == "not_callable"                  # and the press is refused the same way


async def test_a_removed_phones_element_still_expands_and_says_unknown_target(hass, hass_storage):
    house, elements = await _house(hass, hass_storage)
    await managed_talk.async_reconcile(hass)
    store, document = _doc(house, hass, "talk-phone-dina")
    await store.async_save_document(document, store.doc_rev)
    entry = next(e for e in hass.config_entries.async_entries("portico") if e.data.get("enroll_id") == "dina")
    await hass.config_entries.async_remove(entry.entry_id)
    action = store.resolve_document("alice")["pages"][0]["blocks"][0]["actions"][0]
    assert action["kind"] == "call" and action["available"] is False and action["reason"] == "unknown_target"


async def test_additive_nothing_placed_means_nothing_changes(hass, hass_storage):
    house, elements = await _house(hass, hass_storage)
    store = get_content_store(hass)
    before = {ep: copy.deepcopy(store.resolve_document(ep)) for ep in ("alice", "dina", "kitchen")}
    doc_rev, disk = store.doc_rev, copy.deepcopy(hass_storage.get("portico.content"))
    await managed_talk.async_reconcile(hass)
    await hass.async_block_till_done()
    assert store.doc_rev == doc_rev and hass_storage.get("portico.content") == disk     # the content store is untouched
    assert {ep: store.resolve_document(ep) for ep in before} == before                 # every resolved document is the same
    roster = house.registry.endpoints(caller_user_id=house.user("alice"))
    assert {e.id for e in roster} == {"dina", "kitchen", "gate-1"} or {e.id for e in roster} >= {"dina", "kitchen"}
