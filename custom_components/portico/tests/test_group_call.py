"""Group call elements, P1 (design portico_group_call_elements_design.md, G4 to G6): the three groups `everyone`, `phones`
and `panels` are one filter on `_expected_everyone`; the reason words `no_phones` / `no_panels` beside `no_receivers`; only a
screen may place one (HC-9); the caller, its HA user, receive-off, DND, offline-without-push and busy phones are never rung."""
from __future__ import annotations

import pytest

from ..call_endpoints import REASON_BUSY, REASON_NO_PANELS, REASON_NO_PHONES, REASON_NO_RECEIVERS
from ..content import get_content_store
from ..endpoints import get_endpoint_registry
from ..house import CODE_FORBIDDEN, CODE_NOT_CALLABLE, GROUP_IDS
from ..phone_entry import CONF_PANEL, async_set_house_receive
from .house_env import phone_entry
from .test_panel_house import PANEL_USER, SLUG, _world
from .test_endpoints import _provide  # noqa: F401
from .test_enrollment_flow import _qr_capture, qr_payloads  # noqa: F401 - fixtures by name
from .test_push_registry import _custom_integrations, _phone_user  # noqa: F401 - the first is an autouse fixture

pytestmark = pytest.mark.asyncio

CONF_PANEL_SCREEN = "panel_screen"        # a legacy option an old entry may still carry: it is ignored

SLUG2 = "panel2-app"


@pytest.fixture
async def g(hass, hass_storage):
    """The panel-mode world plus a SECOND panel (`panel2`, screen `panel2-app`). Phones: alice, dina. Panels: panel, panel2."""
    h = await _world(hass, hass_storage, extra_dashboards={
        SLUG2: {"name": "Hall panel", "ha_user_id": "u-panel2", "cameras": []}})
    entry = phone_entry(hass, "u-panel2", "panel2", "Panel2")
    hass.config_entries.async_update_entry(entry, options={**entry.options, CONF_PANEL: True, CONF_PANEL_SCREEN: SLUG2})
    await get_content_store(hass).async_ensure_loaded()
    h.users.update({"panel2": "u-panel2", SLUG2: "u-panel2"})
    for who in ("alice", "dina", "panel", "panel2", "kitchen"):
        h.online(who)
    for conn in h.conns.values():
        conn.clear()
    return h


def _rang(h) -> set[str]:
    return {who for who, c in h.conns.items() if any(e["type"] == "house_call" and e["state"] == "ringing" and e["to"] == who
                                                     for e in c.events)}


def _screen(h, slug=SLUG):
    return get_endpoint_registry(h.hass).get(slug)


async def test_the_group_ids_are_the_three_words():
    assert GROUP_IDS == ("everyone", "phones", "panels")


async def test_everyone_rings_phones_and_panels_but_not_the_caller(g):
    result = g.place(SLUG, "group", "everyone")
    assert result.ok
    assert _rang(g) == {"alice", "dina", "panel2"}                       # the caller's own phone half (panel) is never rung


async def test_phones_rings_the_phones_and_not_the_panels(g):
    assert g.place(SLUG, "group", "phones").ok
    assert _rang(g) == {"alice", "dina"}


async def test_panels_rings_the_other_panels_only(g):
    assert g.place(SLUG, "group", "panels").ok
    assert _rang(g) == {"panel2"}                                        # a panel pressing it rings the OTHER panels
    mine = [e for e in g.conns["panel"].events if e["type"] == "house_call"]
    assert [e["state"] for e in mine] == ["ringing"] and mine[0]["to"] == "panels"


async def test_the_second_panel_calling_panels_rings_the_first(g):
    assert g.place(SLUG2, "group", "panels").ok
    assert _rang(g) == {"panel"}


async def test_a_phone_entry_is_a_panel_only_with_panel_true(g, hass):
    """Until the entry carries `panel: true` a tablet is a phone for this purpose (design 2 and 9 Q2): `phones` rings it and
    `panels` does not."""
    entry = next(e for e in hass.config_entries.async_entries("portico") if e.unique_id == "phone:panel2")
    hass.config_entries.async_update_entry(entry, options={k: v for k, v in entry.options.items()
                                                           if k not in (CONF_PANEL, CONF_PANEL_SCREEN)})
    alice = g.registry._endpoint("alice")
    assert {e.id for e in g.registry._expected_everyone(alice, "panels")[0]} == {"panel"}        # panel2 is a phone now
    assert {e.id for e in g.registry._expected_everyone(alice, "phones")[0]} == {"dina", "panel2"}
    assert g.place(SLUG, "group", "phones").ok
    assert _rang(g) == {"alice", "dina", "panel2"}


async def test_the_callers_ha_user_is_excluded_from_every_group(g, hass):
    """R4: a phone entry of the caller's own user is skipped, in all three groups."""
    reg = g.registry
    caller = reg._endpoint("panel")
    for group in GROUP_IDS:
        assert "panel" not in {e.id for e in reg._expected_everyone(caller, group)[0]}


async def test_receive_off_dnd_offline_and_busy_are_excluded(g, hass):
    entries = {e.unique_id: e for e in hass.config_entries.async_entries("portico")}
    async_set_house_receive(hass, entries["phone:dina"], False)          # house receive off
    g.registry.set_dnd(g.conns["panel2"], True)                          # DND
    g.offline("alice")                                                   # offline, no push registration
    for group in GROUP_IDS:
        assert g.registry._expected_everyone(g.registry._endpoint("panel"), group)[0] == [], group
    g.online("alice")
    g.registry.set_dnd(g.conns["panel2"], False)
    for conn in g.conns.values():
        conn.clear()
    assert g.place("alice", "phone", "dina").ok is False                 # dina receives nothing now
    assert g.place("alice", "phone", "panel2").ok                        # alice and panel2 are now in a house call
    assert {e.id for e in g.registry._expected_everyone(g.registry._endpoint("panel"), "everyone")[0]} == set()


async def test_empty_groups_are_refused_with_their_own_reason_and_nothing_placed(g, hass):
    entries = {e.unique_id: e for e in hass.config_entries.async_entries("portico")}
    async_set_house_receive(hass, entries["phone:panel2"], False)
    async_set_house_receive(hass, entries["phone:alice"], False)
    async_set_house_receive(hass, entries["phone:dina"], False)
    states = {grp: g.registry.group_state(grp, _screen(g)) for grp in GROUP_IDS}
    assert states == {"everyone": (REASON_NO_RECEIVERS, 0), "phones": (REASON_NO_PHONES, 0), "panels": (REASON_NO_PANELS, 0)}
    for grp, words in (("everyone", "no phone is set to receive house calls right now"),
                       ("phones", "no phone is set to receive house calls right now"),
                       ("panels", "no other panel can receive a call right now")):
        result = g.place(SLUG, "group", grp)
        assert (result.ok, result.code, result.message) == (False, CODE_NOT_CALLABLE, words)
    assert g.registry.ledger.house_calls() == 0
    assert all(c.house() == [] for c in g.conns.values())      # nothing rang


async def test_a_non_empty_group_is_available_with_its_count(g):
    assert g.registry.group_state("everyone", _screen(g)) == (None, 3)
    assert g.registry.group_state("phones", _screen(g)) == (None, 2)
    assert g.registry.group_state("panels", _screen(g)) == (None, 1)


async def test_the_caller_on_a_call_sees_busy(g):
    assert g.place("alice", "phone", "panel").ok
    for grp in GROUP_IDS:
        assert g.registry.group_state(grp, _screen(g))[0] == REASON_BUSY
        assert g.place(SLUG, "group", grp).code == "busy"


async def test_a_plain_phone_may_not_place_any_group_a_panel_named_by_its_phone_may_and_a_plain_word_is_not_callable(g):
    for grp in GROUP_IDS:
        result = g.place("alice", "group", grp)
        assert (result.ok, result.code) == (False, CODE_FORBIDDEN), grp
        assert g.registry.group_state(grp, g.registry._endpoint("alice")) is None
        # own-pages design: a panel owns its pages as a phone and is still a screen for groups, named by its phone id
        panel = g.registry.group_state(grp, g.registry._endpoint("panel"))
        assert panel is not None, grp
        placed = g.place("panel", "group", grp)
        assert placed.code != CODE_FORBIDDEN, grp
        if placed.ok:
            g.registry.hangup(placed.call_id, "u-panel")
    assert g.place(SLUG, "group", "nobody").code == CODE_NOT_CALLABLE
    assert g.registry.group_state("nobody", _screen(g)) is None


async def test_the_tile_state_has_no_label_of_its_own_and_is_off_for_a_phone(g):
    on = g.registry.group_target_state("panels", _screen(g))
    assert (on.label, on.available, on.reason) == ("", True, None)       # the tile keeps the ELEMENT's name (G6)
    assert g.registry.group_target_state("panels", g.registry._endpoint("alice")).available is False
    assert g.registry.group_target_state("nobody", _screen(g)).reason == "unknown_target"


async def test_the_roster_key_is_unchanged_and_everyone_only(g):
    (entry,) = g.registry.group_entries(_screen(g))
    assert (entry["id"], entry["name"], entry["count"], entry["available"]) == ("everyone", "Everyone", 3, True)


async def test_the_content_stores_tile_state_keeps_the_elements_label_and_reads_the_viewer(g):
    """A Talk group element expands to a `call` action with a group target; its state is the house registry's, as the viewer
    sees it, and the tile keeps the element's own name."""
    store = get_content_store(g.hass)
    action = {"id": "a1", "label": "Call all panels", "kind": "call", "target": {"type": "group", "id": "panels"}, "confirm": False}
    assert store.call_state_for(action, _screen(g)) == {"label": "Call all panels", "available": True, "reason": None}
    # the other panel pressing it sees the first one; a phone viewer, or no viewer-less secret, gets "not available"
    assert store.call_state_for(action, _screen(g, SLUG2))["available"] is True
    assert store.call_state_for(action, g.registry._endpoint("alice"))["reason"] == "not_available"
    # nobody else can receive: the tile is off with the group's own reason
    for who in ("alice", "dina", "panel2"):
        g.registry.set_dnd(g.conns[who], True)
    assert store.call_state_for(action, _screen(g)) == {"label": "Call all panels", "available": False, "reason": REASON_NO_PANELS}
    phones = {**action, "target": {"type": "group", "id": "phones"}}
    assert store.call_state_for(phones, _screen(g))["reason"] == REASON_NO_PHONES
    bad = {**action, "target": {"type": "group", "id": "nobody"}}
    assert store.call_state_for(bad, _screen(g))["reason"] == "unknown_target"


async def test_a_press_of_a_group_tile_places_the_group_call_through_content_run(g):
    """The existing `_run_call` -> `registry.place`: from a screen it rings the set; the grant is the page's action id."""
    store = get_content_store(g.hass)
    action = {"id": "a1", "label": "Call all phones", "kind": "call", "target": {"type": "group", "id": "phones"}, "confirm": False}
    out = await store._run_call(PANEL_USER, "Panel", action, 0.0, (SLUG, "a1"))
    assert out["call_id"] and _rang(g) == {"alice", "dina"}
