"""A panel owns its pages (design `portico_panel_own_pages_design.md`, PR 1: the server core).

A panel's pages, Ring pages and roster hiding live in `devices[<phone endpoint id>]` like a phone's. Pinned here: ownership and
`wake` on the panel phone id (a former panel screen is skipped as an owner), that there is NO fallback to the former screen
while the panel's own list is empty, the `screens: [<phone id>]` shim that app 0.6.98 adopts as its slug, the panel push filter and the
door ring keyed by the phone id, group call elements on a panel's pages, the editor refusing a new screen for a panel's login,
and that a browser screen and a plain phone behave exactly as before."""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .. import content, pages_ownership as own
from ..config_flow import DOMAIN
from ..content import ContentStore, SaveRefused
from ..content_elements import ElementRefError, check_group_placement
from ..endpoints import door_in_refs, panel_door_push_allowed, panel_wake_doors
from ..phone_entry import CONF_PANEL, PhoneRecord
from ..pages_resolve import assigned_ring_pages
from .test_pages_pc import _doc, _file, _page, _send, _talks  # noqa: F401
from .test_pages_pc import pworld  # noqa: F401 - a fixture, by name
from .test_signaling import _setup_signaling  # noqa: F401
from .test_push_registry import _phone_user

pytestmark = pytest.mark.asyncio

PANEL = "ep-panel"                  # the panel's phone endpoint id
FORMER = "kitchen-panel-app"        # its former screen
CONF_PANEL_SCREEN = "panel_screen"  # the legacy option an old entry may still carry: ignored
PARTIES = {"el-00000001": ("door", "gate-1"), "el-00000002": ("door", "side-1")}
GATE, SIDE = "el-00000001", "el-00000002"


# ---- the pure ownership rules -----------------------------------------------------------------------------------------------

def _pure(devices, screens):
    doc = _doc([_page("pg", GATE), _page("ps", SIDE), _page("pp")], devices, ("hall",))
    doc["dashboards"].update(screens)
    return doc


async def test_a_panel_phone_id_owns_its_door_elements_like_a_screen_slug():
    doc = _pure({PANEL: ["pg"], "hall": ["ps"]}, {})
    panels = {"u-panel": PANEL}
    assert own.door_owners(doc, PARTIES) == {SIDE: ["hall"]}                       # without the panel list it owns nothing
    assert own.door_owners(doc, PARTIES, panels) == {GATE: [PANEL], SIDE: ["hall"]}
    wake = own.wake_map(doc, PARTIES, panels)
    assert wake[PANEL] == {"doors": ["gate-1"], "elements": [GATE], "self_elements": [], "screen_call": True}
    assert wake["hall"]["doors"] == ["side-1"]
    assert own.wake_for_element(doc, PARTIES, GATE, panels) == [PANEL] and own.screens_for_door(doc, PARTIES, "gate-1", panels) == [PANEL]


async def test_a_former_panel_screen_is_skipped_as_an_owner_so_it_wakes_only_the_panel():
    doc = _pure({PANEL: ["pg"], FORMER: ["pg"]}, {FORMER: {"name": "Old", "ha_user_id": "u-panel", "cameras": []}})
    panels = {"u-panel": PANEL}
    assert own.owner_ids(doc, panels) == ["hall", PANEL]                           # the former screen is not an owner
    assert own.door_owners(doc, PARTIES, panels) == {GATE: [PANEL]}
    assert own.wake_map(doc, PARTIES, panels)[FORMER]["doors"] == []               # still a row, but it wakes for nothing
    assert own.wake_for_element(doc, PARTIES, GATE, panels) == [PANEL]
    # with no panel flag the same screen is an ordinary owner again (the old behaviour for a browser screen)
    assert own.door_owners(doc, PARTIES) == {GATE: [FORMER]}


async def test_a_door_element_on_two_panels_or_a_panel_and_a_browser_screen_wakes_all_of_them():
    doc = _pure({PANEL: ["pg"], "ep-two": ["pg"], "hall": ["pg"]}, {})
    panels = {"u-panel": PANEL, "u-two": "ep-two"}
    assert own.door_owners(doc, PARTIES, panels) == {GATE: [PANEL, "ep-two", "hall"]}
    assert own.wake_for_element(doc, PARTIES, GATE, panels) == [PANEL, "ep-two", "hall"]   # every owner wakes (D-20261122-42b1)
    assert own.screens_for_door(doc, PARTIES, "gate-1", panels) == sorted([PANEL, "ep-two", "hall"])


async def test_a_plain_phone_still_owns_nothing_and_a_document_without_panels_is_as_before():
    doc = _pure({"alice": ["pg"], "hall": ["ps"]}, {})
    assert own.door_owners(doc, PARTIES, {}) == own.door_owners(doc, PARTIES) == {SIDE: ["hall"]}
    assert "alice" not in own.wake_map(doc, PARTIES, {})


async def test_a_group_call_element_may_sit_on_a_panels_pages_but_not_a_plain_phones():
    group = {"kind": "talk", "options": {"group": "everyone"}}
    page = {"id": "pg", "label": "G", "kind": "content",
            "blocks": [{"id": "b", "title": "B", "cameras": [], "actions": [{"id": "a", "element": "el-0000000a"}]}]}
    doc = {"schema_version": 6, "pages": [page], "dashboards": {}, "endpoints": {},
           "devices": {PANEL: {"pages": [{"page": "pg"}], "ring": {}}}}
    elements = {"el-0000000a": group}
    with pytest.raises(ElementRefError) as err:
        check_group_placement(doc, elements)                                       # as a plain phone: refused (G5)
    assert err.value.code == "not_placeable"
    check_group_placement(doc, elements, frozenset({PANEL}))                       # as a panel: allowed


# ---- the store: fallback, shim, wake, filters ---------------------------------------------------------------------------------

@pytest.fixture
async def pan(pworld):
    """A panel (phone `ep-panel`, flag `panel: true`, `panel_screen` the former screen `kitchen-panel-app` declared for its login),
    a browser screen `hall`, and two door Talk elements."""
    w = pworld
    w.panel_user, w.panel_token = await _phone_user(w.hass, name="Portico — Kitchen panel")
    record = PhoneRecord(ha_user_id=w.panel_user.id, name="Kitchen panel", enroll_id=PANEL, enrolled_at="2026-11-19T10:00:00+00:00")
    w.entry = MockConfigEntry(domain=DOMAIN, title="Kitchen panel", unique_id=f"phone:{record.key}", data=record.to_data(),
                              options={CONF_PANEL: True, CONF_PANEL_SCREEN: FORMER, "house_receive": True})
    w.entry.add_to_hass(w.hass)
    w.gate, w.side = await _talks(w)
    return w


async def _store(w, devices, *, ring=None):
    pages = [_page("p-gate", w.gate), _page("p-side", w.side), _page("p-plain"),
             {"id": "r-gate", "label": "Ring", "kind": "ring", "talk": w.gate, "cameras": [], "blocks": []}]
    doc = _doc(pages, devices, ("hall",))
    doc["dashboards"][FORMER] = {"name": "Kitchen panel", "ha_user_id": w.panel_user.id, "cameras": []}
    doc["dashboards"]["hall"]["ha_user_id"] = w.users["hall"].id
    if ring:
        for key, mapping in ring.items():
            doc["devices"].setdefault(key, {"pages": [], "ring": {}})["ring"] = mapping
    w.storage["portico.content"] = _file(copy.deepcopy(doc))
    store = ContentStore(w.hass)
    w.hass.data["portico"][content.DATA_CONTENT] = store
    await store.async_ensure_loaded()
    return store


def _ids(doc):
    return [p["id"] for p in doc["pages"]]


async def test_a_panel_with_no_page_of_its_own_serves_none_whatever_a_former_screen_holds(pan):
    """No read-time fallback: the former screen's pages, Ring pages and roster hiding are never laid over the panel's row, and the
    legacy `panel_screen` option still on the entry is ignored."""
    store = await _store(pan, {FORMER: ["p-gate", "p-plain"]}, ring={FORMER: {pan.gate: "r-gate"}})
    store._devices[FORMER]["roster_hide"] = ["ep-alice"]
    assert pan.entry.options.get(CONF_PANEL_SCREEN) == FORMER                      # the legacy option is still there
    assert not hasattr(store, "panel_fallbacks") and not hasattr(store, "_with_panel_fallback")
    own_doc = store.resolve_for_endpoint(PANEL)
    assert _ids(own_doc) == [] and own_doc["wake"]["doors"] == []
    assert _ids(store.resolve_for_endpoint(FORMER)) == ["p-gate", "p-plain"]       # the former screen still serves its own
    # a Ring page is the Talk element's, not a device's (R-D): the stored page is granted to every endpoint, it is not laid over the row
    assert [p["id"] for p in assigned_ring_pages(store._expanded(), PANEL)] == ["r-gate"]
    assert "roster_hide" not in store._expanded().get("devices", {}).get(PANEL, {})
    assert PANEL not in store.devices                                              # nothing written


async def test_the_panels_own_pages_or_ring_page_are_served_beside_a_former_screen(pan):
    store = await _store(pan, {FORMER: ["p-gate"], PANEL: ["p-plain"]})
    assert _ids(store.resolve_for_endpoint(PANEL)) == ["p-plain"] and store.resolve_for_endpoint(PANEL)["wake"]["doors"] == []
    ringing = await _store(pan, {FORMER: ["p-gate"], PANEL: []}, ring={PANEL: {pan.gate: "r-gate"}})
    assert [p["id"] for p in assigned_ring_pages(ringing._expanded(), PANEL)] == ["r-gate"]


async def test_a_phone_that_is_no_panel_has_no_wake_even_with_the_legacy_option(pan):
    store = await _store(pan, {FORMER: ["p-gate"]})
    pan.hass.config_entries.async_update_entry(pan.entry, options={CONF_PANEL_SCREEN: FORMER})  # not a panel any more
    assert store.wake_for_endpoint(PANEL) is None


async def test_a_panel_and_its_former_screen_both_holding_a_door_element_wakes_the_panel_only(pan):
    store = await _store(pan, {FORMER: ["p-gate"], PANEL: ["p-gate"]})
    assert store.wake_for_talk_element(pan.gate) == [PANEL] and store.screens_for_door("gate-1") == [PANEL]
    assert store.wake_for_endpoint(PANEL)["doors"] == ["gate-1"] and store.wake_for_endpoint(FORMER)["doors"] == []
    assert panel_wake_doors(pan.hass, PANEL) == ["gate-1"]                         # keyed by the phone id


async def test_the_handshake_of_app_0_6_98_the_one_entry_of_screens_is_the_phone_id_and_content_get_serves_it(pan, hass_ws_client):
    await _store(pan, {FORMER: ["p-gate"], PANEL: ["p-plain"]})
    await _setup_signaling(pan.hass)
    client = await hass_ws_client(pan.hass, access_token=pan.panel_token)
    first = await _send(client, "portico/content/get", 1)                          # no endpoint named: the phone's own document
    assert first["success"], first
    got = first["result"]
    assert got["screens"] == [PANEL] and got["phone_endpoint"] == PANEL and got["endpoint"] == PANEL
    slug = got["screens"][0]                                                       # the app adopts it as its slug and names it
    named = await _send(client, "portico/content/get", 2, endpoint=slug)
    assert named["success"] and named["result"]["endpoint"] == slug == named["result"]["phone_endpoint"]   # endpointOk: profile.endpoint == slug
    assert [p["id"] for p in named["result"]["pages"]] == ["p-plain"] and named["result"]["wake"]["doors"] == []
    # the former screen's slug still works for the old app (the user's own declared screen)
    old = await _send(client, "portico/content/get", 3, endpoint=FORMER)
    assert old["success"] and old["result"]["endpoint"] == FORMER and old["result"]["screens"] == [PANEL]


async def test_a_browser_screen_and_a_plain_phone_are_unchanged(pan, hass_ws_client):
    store = await _store(pan, {FORMER: ["p-gate"], "hall": ["p-side"], "alice": ["p-plain"]})
    assert "screens" not in store.resolve_for_endpoint("hall") and "phone_endpoint" not in store.resolve_for_endpoint("hall")
    assert store.resolve_for_endpoint("hall")["wake"] == {"doors": ["side-1"], "screen_call": True}
    assert "wake" not in store.resolve_for_endpoint("alice") and "screens" not in store.resolve_for_endpoint("alice")
    assert store.wake_for_talk_element(pan.side) == ["hall"] and store.screens_for_door("side-1") == ["hall"]


async def test_the_panel_push_filter_and_the_door_ring_read_the_panel_phones_wake(pan):
    await _store(pan, {PANEL: ["p-gate"]})
    uid = pan.panel_user.id
    assert panel_door_push_allowed(pan.hass, uid, "gate-1") is True
    assert panel_door_push_allowed(pan.hass, uid, "side-1") is False               # a door outside its wake is not pushed
    assert panel_door_push_allowed(pan.hass, uid, None) is True
    assert panel_door_push_allowed(pan.hass, "u-someone-else", "side-1") is True   # a user that is no panel: every door
    assert door_in_refs(panel_wake_doors(pan.hass, PANEL), "gate-1")
    from ..house import async_setup_house

    house = async_setup_house(pan.hass, enabled=True)
    phone = SimpleNamespace(kind="phone", id=PANEL, ha_user_id=uid)
    rung = house._rung_by(phone)
    assert rung("gate-1") is True and rung("side-1") is False and rung(None) is True


async def test_the_editor_refuses_a_new_screen_for_a_panels_login_on_a_live_store(pan):
    store = await _store(pan, {FORMER: ["p-plain"]})
    doc = store.document()
    doc["dashboards"]["second"] = {"name": "Second", "ha_user_id": pan.panel_user.id, "cameras": []}
    with pytest.raises(SaveRefused) as err:
        await store.async_save_document(doc, store.doc_rev)
    assert err.value.code == "panel_user_screen"
    doc = store.document()
    del doc["dashboards"][FORMER]                                                  # deleting a former screen is never refused
    await store.async_save_document(doc, store.doc_rev)
    assert FORMER not in store.dashboards


async def test_a_save_putting_a_panels_door_element_on_a_browser_screen_succeeds_and_wakes_both(pan, hass_ws_client):
    store = await _store(pan, {PANEL: ["p-gate"], "hall": ["p-plain"]})
    doc = store.document()
    doc["devices"]["hall"]["pages"] = [{"page": "p-gate"}]
    await store.async_save_document(doc, store.doc_rev)                            # no refusal (D-20261122-42b1)
    assert sorted(store.wake_for_talk_element(pan.gate)) == sorted([PANEL, "hall"])
