"""Dashboards and phone camera declarations from the content store v3, in the endpoint registry (M6 E1b, design 2.7,
ENDPOINT-01), and the store-side wiring of the resolvers (a roster call through `content/run`, the start-up load).
"""
from __future__ import annotations

import pytest

from ..call_endpoints import set_call_registry
from ..content import get_content_store
from ..endpoints import get_endpoint_registry
from .fake_call_registry import FakeCallEndpointRegistry
from .test_content import _enroll, _run
from .test_endpoints import _phone_entry, _provide

pytestmark = pytest.mark.asyncio


def _v3(**data):
    base = {"schema_version": 3, "template": {"pages": [], "rules": [], "roster": ["*"]}, "overrides": {},
            "dashboards": {}, "endpoints": {}}
    base.update(data)
    return {"version": 1, "minor_version": 1, "key": "portico.content", "data": base}


KITCHEN = {"name": "Kitchen tablet", "ha_user_id": "u-kitchen", "room": "Kitchen", "cameras": ["camera.kitchen"]}


async def test_dashboards_are_listed_once_the_store_has_loaded(hass, hass_storage):
    hass_storage["portico.content"] = _v3(dashboards={"kitchen": KITCHEN})
    reg = get_endpoint_registry(hass)
    assert reg.endpoints(kind="dashboard") == []                    # the store has not loaded: nothing to list
    await get_content_store(hass).async_ensure_loaded()
    (kitchen,) = reg.endpoints(kind="dashboard")
    assert (kitchen.id, kitchen.kind, kitchen.name, kitchen.ha_user_id, kitchen.room) == (
        "kitchen", "dashboard", "Kitchen tablet", "u-kitchen", "Kitchen")
    assert kitchen.cameras == ("camera.kitchen",) and kitchen.available and kitchen.reason is None
    assert reg.get("kitchen", kind="dashboard") == kitchen and reg.for_user("u-kitchen") == kitchen
    assert reg.dashboards_for_user("u-kitchen") == [kitchen] and reg.dashboards_for_user("u-other") == []
    assert reg.dashboards_for_user(None) == [] and reg.for_user(None) is None


async def test_endpoints_are_phones_then_dashboards_then_doors_and_a_phone_wins_for_its_user(hass, hass_storage):
    _phone_entry(hass, [{"ha_user_id": "u-kitchen", "name": "Dina's phone", "enroll_id": "ep-dina"}])
    _provide(hass, "fake", "gate-1")
    hass_storage["portico.content"] = _v3(dashboards={"kitchen": KITCHEN})
    await get_content_store(hass).async_ensure_loaded()
    reg = get_endpoint_registry(hass)
    assert [e.kind for e in reg.endpoints()] == ["phone", "dashboard", "door"]
    assert reg.for_user("u-kitchen").id == "ep-dina"                # the phone first, then a dashboard
    assert [e.id for e in reg.dashboards_for_user("u-kitchen")] == ["kitchen"]


async def test_a_phones_camera_declaration_fills_its_cameras(hass, hass_storage):
    _phone_entry(hass, [{"ha_user_id": "u-dina", "name": "Dina's phone", "enroll_id": "ep-dina"}])
    hass_storage["portico.content"] = _v3(endpoints={"ep-dina": {"cameras": ["camera.dina"]},
                                                     "ep-gone": {"cameras": ["camera.x"]}})
    reg = get_endpoint_registry(hass)
    assert reg.get("ep-dina").cameras == ()
    await get_content_store(hass).async_ensure_loaded()
    assert reg.get("ep-dina").cameras == ("camera.dina",)
    assert reg.get("ep-gone") is None                               # a declaration for no phone lists nothing


async def test_a_save_tells_the_registry_listeners(hass, hass_storage):
    heard = []
    get_endpoint_registry(hass).add_listener(lambda: heard.append(1))
    await get_content_store(hass).async_set_home_name("X")
    assert heard == [1]


async def test_a_dashboard_user_is_a_dashboard_caller_class(hass, hass_storage):
    from .. import auth
    user = await hass.auth.async_create_user("tablet", group_ids=["system-users"])
    assert auth.is_dashboard_user(hass, user.id) is False
    hass_storage["portico.content"] = _v3(dashboards={"kitchen": {**KITCHEN, "ha_user_id": user.id}})
    await get_content_store(hass).async_ensure_loaded()
    assert auth.is_dashboard_user(hass, user.id) is True and auth.is_dashboard_user(hass, "other") is False
    assert auth.DASHBOARD in auth.caller_classes(hass, user)
    # M6 E3: the content commands name `dashboard`, nothing that writes (the call commands are `any` again, by the
    # owner's decision of 2026-10-21).
    assert {c for c, row in auth.COMMANDS.items() if auth.DASHBOARD in row} == {
        "portico/content/get", "portico/content/run", "portico/content/snapshot", "portico/content/subscribe",
        "portico/content/live", "portico/content/live_ice", "portico/content/live_close",
        "portico/house/answer", "portico/house/decline", "portico/house/hangup", "portico/house/presence",
        "portico/house/media_open"}


async def test_the_store_preloads_when_home_assistant_has_started(hass, hass_storage):
    from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
    from homeassistant.core import CoreState
    from .. import _schedule_content_preload
    hass_storage["portico.content"] = _v3(dashboards={"kitchen": KITCHEN})
    hass.set_state(CoreState.starting)
    _schedule_content_preload(hass)
    store = get_content_store(hass)
    assert not store.loaded
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    assert store.loaded and list(store.dashboards) == ["kitchen"]
    hass.set_state(CoreState.running)


async def test_an_already_running_hass_keeps_the_lazy_load(hass):
    from .. import _schedule_content_preload
    _schedule_content_preload(hass)                                 # hass is running in tests
    await hass.async_block_till_done()
    assert not get_content_store(hass).loaded


async def test_an_orphan_can_be_moved_onto_a_declared_dashboard(hass, hass_storage):
    page = {"id": "home", "label": "Home", "cameras": [], "actions": []}
    hass_storage["portico.content"] = _v3(dashboards={"kitchen": KITCHEN},
                                          overrides={"old": {"pages": [page], "rules": [], "roster_hide": []}})
    store = get_content_store(hass)
    await store.async_ensure_loaded()
    assert store.orphan_keys() == ["old"]
    await store.async_move_orphan_to_endpoint("old", "kitchen")
    assert [p["id"] for p in store.pages_for_endpoint("kitchen")] == ["home"] and store.orphan_keys() == []
    assert "kitchen" in store.overrides and "kitchen" not in store.orphan_keys()  # a dashboard's own is no orphan


# ---- the store as the resolvers' registry ----------------------------------------------------------

async def test_the_store_resolves_a_document_over_the_real_registries(hass, hass_storage, hass_read_only_user):
    _phone_entry(hass, [{"ha_user_id": hass_read_only_user.id, "name": "Alice's phone", "enroll_id": "ep-alice"},
                        {"ha_user_id": "u-dina", "name": "Dina's phone", "enroll_id": "ep-dina"}])
    _provide(hass, "fake", "gate-1", name="Gate", camera_entity_id="camera.gate")
    hass.states.async_set("camera.gate", "idle", {"friendly_name": "Gate cam"})
    hass.states.async_set("lock.gate", "locked")
    page = {"id": "home", "label": "Home", "cameras": [],
            "actions": [{"id": "open_gate", "label": "Open", "kind": "lock_open", "entity_id": "lock.gate", "confirm": True},
                        {"id": "relay", "label": "Relay", "kind": "portico_open", "device_id": "gate-1", "confirm": True}]}
    hass_storage["portico.content"] = _v3(template={"pages": [page], "rules": [], "roster": ["*"]},
                                          dashboards={"kitchen": KITCHEN})
    store = get_content_store(hass)
    await store.async_ensure_loaded()
    out = store.resolve_document("ep-alice")
    assert out["kind"] == "phone" and [p["id"] for p in out["pages"]] == ["home"]
    gate = out["callers"]["fake:gate-1"]
    assert [b["id"] for b in gate["ring_buttons"]] == ["open_gate", "relay"]     # the relay's device_id is this door
    assert gate["ring_buttons"][1]["available"] is True and gate["cameras"][0]["name"] == "Gate cam"
    assert set(out["callers"]) == {"ep-dina", "kitchen", "fake:gate-1", "*"}
    assert [r["id"] for r in out["roster"]] == ["ep-dina", "kitchen", "fake:gate-1"]
    assert out["rev"] and store.doc_rev == store.doc_rev and len(store.doc_rev) == 12
    before = store.doc_rev
    assert store.document() == store._data() and store.document() is not store._data()
    await store.async_set_home_name("My Home")
    assert store.doc_rev != before


async def test_a_roster_entry_runs_as_a_call_through_the_same_grant(
        hass, hass_ws_client, hass_read_only_user, hass_read_only_access_token, hass_storage):
    from ..content_resolve import roster_action_id
    await _enroll(hass, hass_read_only_user)
    _phone_entry(hass, [{"ha_user_id": "u-dina", "name": "Dina's phone", "enroll_id": "ep-dina"}])
    fake = FakeCallEndpointRegistry()
    fake.add("phone", "ep-dina", "Dina's phone")
    set_call_registry(hass, fake)
    client = await hass_ws_client(hass, hass_read_only_access_token)
    resp = await _run(client, roster_action_id("ep-dina"))
    assert resp["result"]["ran"] is True and fake.placed[0]["target_type"] == "phone"
    assert fake.placed[0]["target_id"] == "ep-dina"
    # a made-up r- id, and an endpoint hidden from this phone's roster, are refused like any unknown action
    assert (await _run(client, "r-000000000000", 2))["error"]["code"] == "unknown_action"
    store = get_content_store(hass)
    store.overrides.setdefault(store._endpoint_id(hass_read_only_user.id),
                               {"pages": [], "rules": [], "roster_hide": []})["roster_hide"].append("ep-dina")
    assert (await _run(client, roster_action_id("ep-dina"), 3))["error"]["code"] == "unknown_action"


async def test_the_roster_follows_the_template_and_an_empty_one_is_nobody(hass, hass_storage, hass_read_only_user):
    _phone_entry(hass, [{"ha_user_id": hass_read_only_user.id, "name": "Alice's phone", "enroll_id": "ep-alice"},
                        {"ha_user_id": "u-dina", "name": "Dina's phone", "enroll_id": "ep-dina"}])
    hass_storage["portico.content"] = _v3(template={"pages": [], "rules": [], "roster": []})
    store = get_content_store(hass)
    await store.async_ensure_loaded()
    assert store.roster_for("ep-alice") == []
    store.template["roster"][:] = ["ep-dina"]
    assert [e["id"] for e in store.roster_for("ep-alice")] == ["ep-dina"]
