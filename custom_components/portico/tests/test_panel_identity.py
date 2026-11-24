"""DEV-807 panel mode, server slice 1: the identity foundation (design section 5). Nothing here changes a house with no
panel; every test builds one.

One option on the phone entry (`panel: true`; a legacy `panel_screen` option is ignored) is the
single source of truth: `endpoints.is_panel_user`, `is_app_screen` (a FORMER panel screen: a declared screen whose login is a
panel's), `phone_of_user` and `phone_of`. Pinned: the predicate's edge cases, the options surviving every
options writer, the (file, function) classification and the static test over call sites, an unfiltered `endpoint_table`
with filtering only where the doc says, `screens` / `phone_endpoint` in the content documents, "make a panel screen" (options
first, then the screen, rolled back on failure), the editor refusals, the revoke removal and the load-time report.

Slice 2 (test_panel_house.py) adds the house-call side: house.py filtering, the caller normalisation R1/R3, Talk keying, the
heartbeat and the readiness sensor. The house.py rows below are `now` since then.
"""
from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .. import endpoints as ep
from ..config_flow import DOMAIN
from ..content import ProfileError, SaveRefused, get_content_store
from ..content_resolve import endpoint_table, resolve_endpoint, resolve_roster
from ..endpoints import get_endpoint_registry, is_app_screen, panel_users, phone_of, phone_of_user
from ..managed_talk import wanted_managed
from ..make_panel import PanelRefused, async_make_panel
from ..phone_entry import (
    CONF_DOORS, CONF_PANEL, PhoneRecord, async_revoke_phone, async_set_house_receive)
from .test_endpoints import _phone_entry, _provide
from .test_push_registry import _phone_user

pytestmark = pytest.mark.asyncio

CONF_PANEL_SCREEN = "panel_screen"        # a legacy option an old entry may still carry: it is ignored

PKG = Path(__file__).resolve().parent.parent
SLUG = "kitchen-panel-app"


def _v3(**data):
    base = {"schema_version": 3, "template": {"pages": [], "rules": [], "roster": ["*"]}, "overrides": {},
            "dashboards": {}, "endpoints": {}}
    base.update(data)
    return {"version": 1, "minor_version": 1, "key": "portico.content", "data": base}


def _screen(user_id, name="Kitchen panel"):
    return {"name": name, "ha_user_id": user_id, "cameras": ["camera.kitchen"]}


def _panel_entry(hass, user_id, enroll_id="ep-panel", name="Kitchen panel", **options):
    options = {CONF_PANEL: True, CONF_PANEL_SCREEN: SLUG, **options}
    record = PhoneRecord(ha_user_id=user_id, name=name, enroll_id=enroll_id, enrolled_at="2026-11-19T10:00:00+00:00")
    entry = MockConfigEntry(domain=DOMAIN, title=name, unique_id=f"phone:{record.key}", data=record.to_data(), options=options)
    entry.add_to_hass(hass)
    return entry


async def _loaded(hass, hass_storage, dashboards):
    hass_storage["portico.content"] = _v3(dashboards=dashboards)
    store = get_content_store(hass)
    await store.async_ensure_loaded()
    return store


# ---- the predicate ------------------------------------------------------------------------------------------------------

async def test_the_app_screen_is_a_declared_screen_whose_login_is_a_panels(hass, hass_storage):
    _panel_entry(hass, "u-panel")
    assert is_app_screen(hass, SLUG) is False                          # the store has not loaded
    await _loaded(hass, hass_storage, {SLUG: _screen("u-panel")})
    assert is_app_screen(hass, SLUG)
    phone = phone_of(hass, SLUG)
    assert phone is not None and phone.kind == "phone" and phone.id == "ep-panel" and phone.ha_user_id == "u-panel"
    assert phone_of_user(hass, "u-panel").id == "ep-panel" and phone_of_user(hass, "u-nobody") is None and phone_of_user(hass, None) is None
    assert panel_users(hass) == {"u-panel": "ep-panel"}
    assert get_content_store(hass).is_app_screen(SLUG) and get_content_store(hass).phone_of(SLUG) == phone


async def test_every_undefined_case_reduces_to_the_one_predicate(hass, hass_storage):
    entry = _panel_entry(hass, "u-panel")
    store = await _loaded(hass, hass_storage, {SLUG: _screen("u-panel"), "second": _screen("u-panel", "Second"),
                                               "look-alike": _screen("u-other", "Look-alike")})
    assert is_app_screen(hass, SLUG) is True
    assert is_app_screen(hass, "second") is True            # no slug match: any declared screen of the panel's login is a former one
    assert is_app_screen(hass, "look-alike") is False and is_app_screen(hass, "nope") is False     # a browser screen of another login
    assert is_app_screen(hass, None) is False and is_app_screen(hass, "") is False
    # re-pointed to another login: an ordinary screen at once
    store._dashboards = {**store.dashboards, SLUG: _screen("u-other")}
    assert is_app_screen(hass, SLUG) is False and phone_of(hass, SLUG) is None
    # deleted
    store._dashboards = {k: v for k, v in store.dashboards.items() if k != SLUG}
    assert is_app_screen(hass, SLUG) is False and phone_of_user(hass, "u-panel").id == "ep-panel"     # the panel does not care
    # restored, then the flag cleared (or not exactly true): an ordinary screen
    store._dashboards = {**store.dashboards, SLUG: _screen("u-panel")}
    assert is_app_screen(hass, SLUG) is True
    for options in ({CONF_PANEL: False, CONF_PANEL_SCREEN: SLUG}, {CONF_PANEL: "yes", CONF_PANEL_SCREEN: SLUG}, {}):
        hass.config_entries.async_update_entry(entry, options=options)
        assert is_app_screen(hass, SLUG) is False, options
    hass.config_entries.async_update_entry(entry, options={CONF_PANEL: True})         # the flag alone is a panel
    assert is_app_screen(hass, SLUG) is True


async def test_a_legacy_row_cannot_carry_options_so_it_is_never_a_panel(hass, hass_storage):
    _phone_entry(hass, [{"ha_user_id": "u-old", "name": "Old", "enroll_id": "ep-old", CONF_PANEL: True,
                         CONF_PANEL_SCREEN: SLUG}])
    await _loaded(hass, hass_storage, {SLUG: _screen("u-old")})
    assert is_app_screen(hass, SLUG) is False and panel_users(hass) == {}


async def test_every_options_writer_keeps_the_panel_options(hass, hass_storage):
    from .. import phones_commands

    entry = _panel_entry(hass, "u-panel", **{CONF_DOORS: ["gate-1"]})
    await _loaded(hass, hass_storage, {SLUG: _screen("u-panel")})
    async_set_house_receive(hass, entry, True)
    assert entry.options[CONF_PANEL] is True and entry.options[CONF_PANEL_SCREEN] == SLUG and entry.options["house_receive"]
    row_id = phones_commands._row_id(entry, dict(entry.data))
    reply = await phones_commands._set_locked(hass, row_id, {"house_receive": False})
    assert reply["result"] == "updated" and reply["phone"]["panel"] is True and "panel_screen" not in reply["phone"]     # the legacy option is ignored, never sent
    reply = await phones_commands._set_locked(hass, row_id, {CONF_DOORS: None})
    assert entry.options[CONF_PANEL] is True and entry.options[CONF_PANEL_SCREEN] == SLUG and entry.options[CONF_DOORS] is None
    assert is_app_screen(hass, SLUG)


# ---- the classification and the static test ------------------------------------------------------------------------------

# (file, function) -> (class, status). Classes follow the design (section 5 item 3, R1): `filtered` = offered as a callee or
# listed to someone else; `exempt` = serves the screen's own content or Talk, or is bookkeeping; `visible` = must keep listing the
# panel screen. status `now` = implemented (slices 1 and 2).
FILTERED_NOW, EXEMPT, VISIBLE = ("filtered", "now"), ("exempt", "-"), ("visible", "-")
CLASSIFICATION = {
    ("actions.py", "source_for"): EXEMPT,                            # labels the actor of an action `panel` or `app` by login; never a callee list
    ("auth.py", "is_dashboard_user"): EXEMPT,
    ("panel_call.py", "_panel_for"): EXEMPT,                         # lists the panels' own phone endpoints for the service; never a callee list
    ("panel_call.py", "_preconditions"): EXEMPT,                     # the panel's own phone and screen, for its busy state and Everyone key
    ("content.py", "_all_cameras"): FILTERED_NOW,                    # the caller loop
    ("content.py", "_app_screen_ids"): EXEMPT,
    ("content.py", "_call_state"): FILTERED_NOW,                     # through `_endpoint`
    ("content.py", "_check_library_delete"): EXEMPT,                 # library-delete names
    ("content.py", "_check_element_refs"): FILTERED_NOW,             # the `known` target check, through `_known_target`
    ("content.py", "_check_panel_screens"): EXEMPT,                  # refuses a NEW screen for a panel's login
    ("content.py", "_endpoint"): FILTERED_NOW,                       # a call target; also the gate and dial lookup
    ("content.py", "_endpoint_by_id"): EXEMPT,                       # `_run_talk` who-check and `_resolve_page` need the screen
    ("content.py", "_endpoint_id"): EXEMPT,                          # the override key of a user: `for_user` returns the phone first
    ("content.py", "_gate_door_id"): FILTERED_NOW,                   # the dial lookup, through `_endpoint`
    ("content.py", "_known_target"): FILTERED_NOW,                   # `known`, and the normalisers' `target_known` through it
    ("content.py", "_load_content"): EXEMPT,
    ("content.py", "_panel_fields"): EXEMPT,
    ("content.py", "_resolve_page"): EXEMPT,
    ("content.py", "_run_talk"): EXEMPT,                             # the who-check needs the screen; the KEYING is the phone's (`_talk_keyed`)
    ("content.py", "_save"): EXEMPT,
    ("content.py", "async_move_orphan_to_endpoint"): FILTERED_NOW,   # the orphan move
    ("content.py", "async_set"): FILTERED_NOW,                       # `known`
    ("content.py", "endpoints"): EXEMPT,                             # the unfiltered list the pure resolvers are handed
    ("content.py", "orphan_keys"): EXEMPT,
    ("content.py", "prepare_document"): EXEMPT,                      # the slug-clash check: a second screen must not take the slug
    ("content.py", "roster_for"): FILTERED_NOW,                      # through `resolve_roster`
    ("config_apply.py", "_door_rings"): EXEMPT,                      # doors only; the panels are handed to `screens_for_door`
    ("config_apply.py", "_locked"): EXEMPT,                          # the YAML apply diffs the STORED screens (`content.dashboards`), unfiltered
    ("config_apply.py", "effect_report"): EXEMPT,                    # before/after per endpoint over the unfiltered `endpoint_table`
    ("content_resolve.py", "endpoint_table"): EXEMPT,                # UNFILTERED on purpose: the panel's own `content/get` needs it
    ("content_resolve.py", "resolve_endpoint"): FILTERED_NOW,        # the `callers` loop (and `resolve_roster` ids)
    ("diagnostics.py", "_content_document"): EXEMPT,
    ("editor_commands.py", "_endpoint_rows"): VISIBLE,
    ("editor_commands.py", "handle_content_document"): VISIBLE,      # the `resolved` map
    ("editor_commands.py", "handle_content_preview"): VISIBLE,       # `known` uses the unfiltered table
    ("editor_commands.py", "handle_kiosk_hello"): EXEMPT,
    ("elements.py", "endpoint_exists"): FILTERED_NOW,                # `_check_talk_endpoint`, `talk_endpoint_removed`, `managed_target_exists`
    ("endpoints.py", "_dashboards"): EXEMPT,
    ("endpoints.py", "for_user"): EXEMPT,
    ("endpoints.py", "get"): EXEMPT,
    ("endpoints.py", "is_app_screen"): EXEMPT,
    ("endpoints.py", "phone_of_user"): EXEMPT,                      # the panel's phone, by login
    ("endpoints.py", "phone_of"): EXEMPT,
    ("gate_target.py", "dialable_doors"): EXEMPT,                    # doors only
    ("house.py", "_conns_of"): FILTERED_NOW,
    ("house.py", "_door_rang"): FILTERED_NOW,
    ("house.py", "_endpoint"): FILTERED_NOW,
    ("house.py", "_endpoints"): FILTERED_NOW,
    ("house.py", "_open"): FILTERED_NOW,
    ("house.py", "_raw_endpoint"): EXEMPT,                           # the caller of `place_call` (R3) and `detach` (R5) must see the screen
    ("house.py", "detach"): EXEMPT,                                  # R5: every endpoint of the closing user, the screen included
    ("house.py", "_participant"): FILTERED_NOW,
    ("house.py", "answer"): FILTERED_NOW,
    ("house.py", "group_entries"): FILTERED_NOW,
    ("house.py", "group_state"): FILTERED_NOW,                       # group-call design: the Everyone key's body, moved here; the three groups share it
    ("house.py", "place_call"): FILTERED_NOW,
    ("house.py", "presence"): FILTERED_NOW,
    ("house.py", "reconcile"): FILTERED_NOW,
    ("house.py", "snapshots_for"): FILTERED_NOW,
    ("house.py", "target_state"): FILTERED_NOW,
    ("managed_talk.py", "async_setup_managed_talk"): EXEMPT,         # the change listener
    ("managed_talk.py", "wanted_managed"): FILTERED_NOW,
    ("pages_migrate.py", "plan_for_store"): EXEMPT,
    ("ring_pages_migrate.py", "run_ring_migration"): EXEMPT,         # an admin command: the callers of the proof, not a panel view
    ("make_panel.py", "async_make_panel"): EXEMPT,
    ("panel_wall.py", "server_phone_endpoint"): EXEMPT,              # the phone of a panel's user: the id `house_ok` compares
    ("signaling.py", "_content_target"): EXEMPT,                     # the phone with no `endpoint`, the screen when named
    ("signaling.py", "handle_content_subscribe"): EXEMPT,
}
_CALLS = {"get_endpoint_registry", "endpoints", "_endpoint_by_id", "_endpoint", "endpoint_table", "dashboards_for_user", "for_user"}
_ATTRS = {"dashboards", "_endpoint_by_id", "_endpoint"}


def _call_sites() -> dict[tuple[str, str], set[str]]:
    """Every (file, outermost function) of the integration (tests and the web UI left out) that calls the registry, aliased
    receivers included (`registry.endpoints()`, `ContentStore.endpoints()`, `self._endpoint(...)`), or reads `.dashboards`."""
    found: dict[tuple[str, str], set[str]] = {}
    for path in sorted(PKG.rglob("*.py")):
        rel = path.relative_to(PKG).as_posix()
        if rel.startswith(("tests/", "www/")):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))

        class Visitor(ast.NodeVisitor):
            def __init__(self) -> None:
                self.stack: list[str] = []

            def visit_FunctionDef(self, node):
                self.stack.append(node.name)
                self.generic_visit(node)
                self.stack.pop()

            visit_AsyncFunctionDef = visit_FunctionDef

            def _hit(self, name):
                found.setdefault((rel, self.stack[0] if self.stack else "<module>"), set()).add(name)

            def visit_Call(self, node):
                f = node.func
                name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
                if name in _CALLS:
                    self._hit(name)
                self.generic_visit(node)

            def visit_Attribute(self, node):
                if node.attr in _ATTRS:
                    self._hit(node.attr)
                self.generic_visit(node)

        Visitor().visit(tree)
    return found


def test_every_registry_call_site_is_classified():
    sites = _call_sites()
    missing = sorted(set(sites) - set(CLASSIFICATION))
    assert not missing, (f"a new call site of the endpoint registry is not classified for panel mode (DEV-807, design section 5 "
                         f"item 3 R1): add it to CLASSIFICATION as filtered, exempt or visible, with its behavioural test: {missing}")


def test_every_classified_function_still_exists():
    for (rel, function), (kind, status) in CLASSIFICATION.items():
        tree = ast.parse((PKG / rel).read_text(encoding="utf-8"))
        names = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        assert function in names, f"{rel}:{function} is classified but no longer exists"
        assert kind in ("filtered", "exempt", "visible") and status in ("now", "-")


def test_the_house_side_reads_the_one_predicate():
    """Slice 2: house.py filters through `is_app_screen` and normalises the caller through `phone_of` (R1, R3)."""
    source = (PKG / "house.py").read_text(encoding="utf-8")
    assert "is_app_screen" in source and "phone_of" in source and "is_panel_user" in source and "panel_screen_of" not in source


# ---- unfiltered where it must be, filtered where the doc says -------------------------------------------------------------

class _World:
    pass


async def _panel_world(hass, hass_storage):
    """A panel (phone `ep-panel`, screen SLUG), another phone `ep-alice`, a plain screen `hall`, a door."""
    w = _World()
    w.hass = hass
    w.panel_user, _ = await _phone_user(hass, name="Portico — Kitchen panel")
    w.alice, _ = await _phone_user(hass, name="Portico — Alice")
    _provide(hass, "fake", "gate-1", name="Gate", camera_entity_id="camera.gate")
    w.entry = _panel_entry(hass, w.panel_user.id)
    MockConfigEntry(domain=DOMAIN, title="Alice", unique_id="phone:ep-alice", options={},
                    data=PhoneRecord(ha_user_id=w.alice.id, name="Alice", enroll_id="ep-alice",
                                     enrolled_at="2026-11-19T10:00:00+00:00").to_data()).add_to_hass(hass)
    w.store = await _loaded(hass, hass_storage, {SLUG: _screen(w.panel_user.id), "hall": _screen("u-hall", "Hall")})
    return w


async def test_the_endpoint_table_is_unfiltered_and_the_panels_own_document_is_a_dashboard_with_its_phone_fields(hass, hass_storage):
    w = await _panel_world(hass, hass_storage)
    document = w.store.document()
    assert [e.id for e in endpoint_table(document, w.store) if e.kind == "dashboard"] == [SLUG, "hall"]    # unfiltered
    own = w.store.resolve_for_endpoint(SLUG)
    assert own["kind"] == "dashboard" and own["endpoint"] == SLUG
    assert own["screens"] == ["ep-panel"] and own["phone_endpoint"] == "ep-panel"      # the former screen's own document has the phone fields
    assert w.store.resolve_for_endpoint("ep-panel")["screens"] == ["ep-panel"]          # the shim: the one entry is the phone id
    assert w.store.resolve_for_endpoint("ep-panel")["phone_endpoint"] == "ep-panel"
    assert "screens" not in w.store.resolve_for_endpoint("ep-alice") and "phone_endpoint" not in w.store.resolve_for_endpoint("hall")
    # the same lookups through the editor's preview path: the table the panel's slug is resolved in still holds it
    assert resolve_endpoint(w.store.expand(document), SLUG, states=hass.states, registry=w.store)["kind"] == "dashboard"


async def test_another_devices_callers_and_roster_lack_the_app_screen_and_the_panels_own_roster_lacks_its_phone(hass, hass_storage):
    w = await _panel_world(hass, hass_storage)
    alice = w.store.resolve_for_endpoint("ep-alice")
    assert SLUG not in alice["callers"] and "ep-panel" in alice["callers"] and "hall" in alice["callers"]
    assert SLUG not in {r["id"] for r in alice["roster"]} and "ep-panel" in {r["id"] for r in alice["roster"]}
    hall = {r["id"] for r in w.store.resolve_for_endpoint("hall")["roster"]}
    assert SLUG not in hall
    own = w.store.resolve_for_endpoint(SLUG)
    assert "ep-panel" not in {r["id"] for r in own["roster"]} and "ep-alice" in {r["id"] for r in own["roster"]}
    # `resolve_roster` itself, with an explicit roster
    doc = {**w.store.document(), "template": {**w.store.document()["template"], "roster": [SLUG, "ep-alice"]}}
    out = resolve_roster(doc, "ep-alice", endpoints=endpoint_table(doc, w.store), registry=w.store)
    assert [r["id"] for r in out] == []                     # alice's own id is dropped, the app screen is filtered
    # the camera grant: another device's snapshot grant carries no caller camera of the app screen
    w.store._dashboards = {**w.store.dashboards, SLUG: {**w.store.dashboards[SLUG], "cameras": ["camera.panel_only"]}}
    assert all(c["entity_id"] != "camera.panel_only" for c in w.store._all_cameras("ep-alice"))


async def test_a_call_to_the_app_screen_is_unknown_and_a_call_to_its_phone_is_not(hass, hass_storage):
    w = await _panel_world(hass, hass_storage)
    assert w.store._endpoint({"type": "dashboard", "id": SLUG}) is None
    assert w.store._endpoint({"type": "dashboard", "id": "hall"}).id == "hall"
    assert w.store._endpoint({"type": "phone", "id": "ep-panel"}).id == "ep-panel"
    assert w.store._endpoint_by_id(SLUG).id == SLUG               # exempt: the screen's own Talk and pages need it
    state = w.store._call_state({"id": "c", "label": "Panel", "kind": "call", "target": {"type": "dashboard", "id": SLUG}})
    assert state["available"] is False and state["label"] == "Panel"        # the button's own label: no endpoint was found
    hall = w.store._call_state({"id": "c", "label": "Panel", "kind": "call", "target": {"type": "dashboard", "id": "hall"}})
    assert hall["label"] == "Hall"                                          # an ordinary screen is found by name


async def test_a_talk_element_cannot_name_the_app_screen_and_no_managed_element_is_wanted_for_it(hass, hass_storage):
    from ..elements import endpoint_exists, talk_endpoint_removed

    w = await _panel_world(hass, hass_storage)
    ids = [m["id"] for m in wanted_managed(hass)]
    assert f"talk-screen-{SLUG}" not in ids and "talk-screen-hall" in ids and "talk-phone-ep-panel" in ids
    assert endpoint_exists(hass, "dashboard", SLUG) is False and endpoint_exists(hass, "dashboard", "hall") is True
    assert talk_endpoint_removed(hass, {"kind": "talk", "options": {"screen": SLUG}}) is True


async def test_the_normalisers_refuse_a_call_button_naming_a_screen_declared_in_the_same_document_when_it_is_the_app_screen(
        hass, hass_storage):
    w = await _panel_world(hass, hass_storage)
    button = {"id": "call-panel", "label": "Panel", "kind": "call", "confirm": False,
              "target": {"type": "dashboard", "id": SLUG}}
    page = {"id": "home", "label": "Home", "blocks": [{"id": "b", "title": "B", "cameras": [], "actions": [button]}]}
    doc = {**w.store.document(), "template": {"pages": [page], "rules": [], "roster": ["*"]}}
    with pytest.raises(ProfileError):
        w.store.prepare_document(doc)
    other = {**button, "id": "call-hall", "target": {"type": "dashboard", "id": "hall"}}
    ok = {**w.store.document(), "template": {"pages": [{**page, "blocks": [{**page["blocks"][0], "actions": [other]}]}],
                                             "rules": [], "roster": ["*"]}}
    w.store.prepare_document(ok)                          # an ordinary screen still validates


async def test_the_orphan_move_never_targets_the_app_screen(hass, hass_storage):
    w = await _panel_world(hass, hass_storage)
    w.store._overrides = {"old": {"pages": [], "rules": [], "roster_hide": []}}
    with pytest.raises(ProfileError):
        await w.store.async_move_orphan_to_endpoint("old", SLUG)


async def test_the_devices_tab_still_lists_the_app_screen_and_flags_it(hass, hass_storage):
    from ..editor_commands import _endpoint_rows

    w = await _panel_world(hass, hass_storage)
    rows = {r["id"]: r for r in _endpoint_rows(hass)}
    assert rows[SLUG]["app_screen"] is True and "app_screen" not in rows["hall"] and "app_screen" not in rows["ep-panel"]


# ---- the editor refusals --------------------------------------------------------------------------------------------------

async def test_the_editor_may_delete_or_repoint_a_former_panel_screen_but_not_declare_a_new_one(hass, hass_storage):
    w = await _panel_world(hass, hass_storage)
    document = w.store.document()
    renamed = {**document, "dashboards": {**document["dashboards"], SLUG: {**document["dashboards"][SLUG], "name": "Renamed"}}}
    await w.store.async_save_document(renamed, w.store.doc_rev)          # an edit that keeps the screen saves
    assert w.store.dashboards[SLUG]["name"] == "Renamed"
    document = w.store.document()
    # a NEW screen for the panel's login is refused, on both paths (the save and the preview)
    fresh = {**document, "dashboards": {**document["dashboards"], "second": _screen(w.panel_user.id, "Second")}}
    with pytest.raises(SaveRefused) as err:
        await w.store.async_save_document(fresh, w.store.doc_rev)
    assert err.value.code == "panel_user_screen" and err.value.fields["screen"] == "second"
    with pytest.raises(SaveRefused):
        w.store.prepare_document(fresh)
    assert "second" not in w.store.dashboards
    # re-pointing the former screen to another login is allowed; so is a new screen for any other login
    repointed = {**document, "dashboards": {**document["dashboards"], SLUG: {**document["dashboards"][SLUG], "ha_user_id": w.alice.id}}}
    await w.store.async_save_document(repointed, w.store.doc_rev)
    assert is_app_screen(hass, SLUG) is False and w.store.dashboards[SLUG]["ha_user_id"] == w.alice.id
    document = w.store.document()
    deleted = {**document, "dashboards": {k: v for k, v in document["dashboards"].items() if k != SLUG}}
    await w.store.async_save_document(deleted, w.store.doc_rev)          # and deleting it is no longer refused
    assert SLUG not in w.store.dashboards


async def test_a_browser_screen_is_declared_for_any_login_that_is_no_panels(hass, hass_storage):
    w = await _panel_world(hass, hass_storage)
    document = w.store.document()
    browser, _ = await _phone_user(hass, name="Portico — Browser")
    fresh = {**document, "dashboards": {**document["dashboards"], "wall-browser": _screen(browser.id, "Wall browser")}}
    await w.store.async_save_document(fresh, w.store.doc_rev)
    assert "wall-browser" in w.store.dashboards and is_app_screen(hass, "wall-browser") is False


# ---- make a panel ---------------------------------------------------------------------------------------------------------

async def _plain_world(hass, hass_storage, **options):
    await hass.auth.async_create_user("Owner", group_ids=["system-admin"]) if not await hass.auth.async_get_users() else None
    user, _ = await _phone_user(hass, name="Portico — Kitchen panel")
    record = PhoneRecord(ha_user_id=user.id, name="Kitchen panel", enroll_id="ep-panel", enrolled_at="2026-11-19T10:00:00+00:00")
    entry = MockConfigEntry(domain=DOMAIN, title="Kitchen panel", unique_id="phone:ep-panel", data=record.to_data(),
                            options={"house_receive": True, CONF_DOORS: ["gate-1"], **options})
    entry.add_to_hass(hass)
    store = await _loaded(hass, hass_storage, {})
    return user, entry, store


async def test_make_a_panel_sets_the_flag_alone_and_declares_no_screen(hass, hass_storage):
    user, entry, store = await _plain_world(hass, hass_storage)
    await async_make_panel(hass, entry)
    assert entry.options[CONF_PANEL] is True and CONF_PANEL_SCREEN not in entry.options        # never written again
    assert CONF_DOORS not in entry.options and entry.options["house_receive"] is True      # every door answers; the rest is kept
    assert store.dashboards == {}                                                          # no screen declared
    assert phone_of_user(hass, user.id).id == "ep-panel" and panel_users(hass) == {user.id: "ep-panel"}
    assert store.resolve_for_endpoint("ep-panel")["screens"] == ["ep-panel"]               # the shim works for a brand-new panel
    with pytest.raises(PanelRefused) as err:
        await async_make_panel(hass, entry)
    assert err.value.code == "already_panel"


async def test_make_a_panel_refuses_what_it_must_and_changes_nothing(hass, hass_storage):
    user, entry, store = await _plain_world(hass, hass_storage)
    legacy = _phone_entry(hass, [{"ha_user_id": "u-old", "name": "Old", "enroll_id": "ep-old"}])
    with pytest.raises(PanelRefused) as err:
        await async_make_panel(hass, legacy)
    assert err.value.code == "legacy_phone"
    admin = await hass.auth.async_create_user("Alice admin", group_ids=["system-admin"])
    other = MockConfigEntry(domain=DOMAIN, title="Admin phone", unique_id="phone:ep-admin", options={},
                            data=PhoneRecord(ha_user_id=admin.id, name="Admin phone", enroll_id="ep-admin",
                                             enrolled_at="2026-11-19T10:00:00+00:00").to_data())
    other.add_to_hass(hass)
    with pytest.raises(PanelRefused) as err:
        await async_make_panel(hass, other)
    assert err.value.code == "not_portico_user" and CONF_PANEL not in other.options
    assert CONF_PANEL not in entry.options


async def test_no_managed_talk_element_is_wanted_for_a_panel_that_has_no_screen(hass, hass_storage):
    user, entry, store = await _plain_world(hass, hass_storage)
    await async_make_panel(hass, entry)
    ids = [m["id"] for m in wanted_managed(hass)]
    assert "talk-phone-ep-panel" in ids and not [i for i in ids if i.startswith("talk-screen-")]


# ---- revoke --------------------------------------------------------------------------------------------------------------

async def test_revoking_a_panel_removes_its_device_row_and_leaves_a_screen_alone(hass, hass_storage):
    user, entry, store = await _plain_world(hass, hass_storage)
    await async_make_panel(hass, entry)
    hall, _ = await _phone_user(hass, name="Portico — Hall")
    await store.async_save_document({**store.document(), "dashboards": {"hall": _screen(hall.id, "Hall")}}, store.doc_rev)
    await async_revoke_phone(hass, entry)
    assert list(store.dashboards) == ["hall"]               # revoke no longer calls `async_remove_panel_screen` (gone)
    assert not hasattr(store, "async_remove_panel_screen")
