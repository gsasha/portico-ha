"""The editor's websocket commands (M6 E2, design 3.2, 4.4, 4.5, 5.3, review log R6-06/08/09/16/17):
`portico/content/document`, `content/preview`, `content/save` and `kiosk/hello`, over a real websocket as an
administrator, an enrolled phone, a declared kiosk user and an ordinary user.

What is pinned: the three editor commands are admin-only (a phone and a plain user are refused, nothing is
written); a save is refused when stale and when invalid and then changes nothing; a save never drops a
"Not attached" override by omission; the write goes through the store's one write path (the pre-migration copy,
the refusal over a newer schema); a preview is byte-for-byte what the app would receive after the save; `kiosk/hello`
is bounded. The v1 commands are left alone (their own test files pin them).
"""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest
from homeassistant.auth.const import GROUP_ID_ADMIN
from pytest_homeassistant_custom_component.common import MockConfigEntry

from ..config_flow import DOMAIN
from ..content import ProfileError, get_content_store
from ..diagnostics import async_get_config_entry_diagnostics, pseudonymise_document
from ..editor_commands import KIOSK_SEEN_MAX, KioskSeen, get_kiosk_seen
from ..endpoints import get_endpoint_registry
from .test_endpoints import _phone_entry, _provide
from .test_push_registry import _phone_user
from .test_signaling import _setup_signaling

pytestmark = pytest.mark.asyncio

PHONE_ID = "ep-alice"
DOOR_ID = "fake:gate-1"


def _v3(**data):
    base = {"schema_version": 3, "template": {"pages": [], "rules": [], "roster": ["*"]}, "overrides": {},
            "dashboards": {}, "endpoints": {}}
    base.update(data)
    return {"version": 1, "minor_version": 1, "key": "portico.content", "data": base}


def _v3_page(page_id="home", label="Home", camera="camera.gate", action_id="open_gate", **extra):
    """A schema-3 page (flat lists): what a v3 FILE holds. The wire takes blocks only since B8 (`_page`)."""
    return {"id": page_id, "label": label,
            "cameras": [{"id": "gate", "label": "Gate", "entity_id": camera}] if camera else [],
            "actions": [{"id": action_id, "label": "Open the gate", "kind": "lock_open", "entity_id": "lock.gate",
                         "confirm": True}] if action_id else [], **extra}


def _page(page_id="home", label="Home", camera="camera.gate", action_id="open_gate", **extra):
    """A schema-4 page of one block holding what `_v3_page` holds."""
    flat = _v3_page(page_id, label, camera, action_id)
    return {"id": page_id, "label": label, "blocks": [{"id": page_id, "title": label, "cameras": flat["cameras"],
                                                       "actions": flat["actions"]}], **extra}


def _blocked(actions, page_id="home", cameras=()):
    """A schema-4 page of one block holding `actions` (and `cameras`)."""
    return {"id": page_id, "label": page_id.title(), "blocks": [{"id": page_id, "title": page_id.title(),
                                                                 "cameras": list(cameras), "actions": actions}]}


def _doc(**sections):
    document = {"template": {"pages": [_page()], "rules": [], "roster": ["*"]}, "overrides": {}, "dashboards": {},
                "endpoints": {}}
    document.update(sections)
    return document


@pytest.fixture
async def world(hass, hass_storage, hass_admin_user):
    """An administrator (the default ws client), an enrolled phone `ep-alice`, an ordinary user, one door."""
    phone, phone_token = await _phone_user(hass, name="Alice phone user")
    plain, plain_token = await _phone_user(hass, name="Plain user")
    tablet, tablet_token = await _phone_user(hass, name="Kitchen tablet user")
    _phone_entry(hass, [{"ha_user_id": phone.id, "name": "Alice's phone", "enroll_id": PHONE_ID}])
    await _setup_signaling(hass)
    _provide(hass, "fake", "gate-1", name="Gate", camera_entity_id="camera.gate")
    hass.states.async_set("camera.gate", "idle", {"friendly_name": "Gate camera"})
    hass.states.async_set("lock.gate", "locked", {"friendly_name": "Gate lock"})
    store = get_content_store(hass)
    await store.async_ensure_loaded()
    return SimpleNamespace(hass=hass, store=store, storage=hass_storage, admin_user=hass_admin_user, phone=phone,
                           plain=plain, tablet=tablet,
                           tokens={"phone": phone_token, "plain": plain_token, "tablet": tablet_token})


async def _client(world, hass_ws_client, who="admin"):
    token = None if who == "admin" else world.tokens[who]
    return await hass_ws_client(world.hass, access_token=token) if token else await hass_ws_client(world.hass)


async def _send(client, type_, msg_id=1, **body):
    await client.send_json({"id": msg_id, "type": type_, **body})
    return await client.receive_json()


async def _document(world, hass_ws_client):
    admin = await _client(world, hass_ws_client)
    resp = await _send(admin, "portico/content/document")
    assert resp["success"], resp
    return admin, resp["result"]


async def _reload(world, data):
    """Plant `data` as the content file and make the store read it afresh (a restart, in effect)."""
    from homeassistant.helpers.storage import Store

    world.storage["portico.content"] = {"version": 1, "minor_version": 1, "key": "portico.content", "data": data}
    store = world.store
    store._store = Store(world.hass, 1, "portico.content", atomic_writes=True)
    store._template = {"pages": [], "rules": [], "roster": ["*"]}
    store._overrides, store._dashboards, store._endpoint_decls = {}, {}, {}
    store._home_name, store._premigration, store._newer_schema, store._loaded = "", None, None, False
    await store.async_ensure_loaded()


# ---- who may send what -----------------------------------------------------------------------------------

EDITOR = {
    "portico/content/document": {},
    "portico/content/preview": {"document": _doc(), "endpoint": PHONE_ID},
    "portico/content/save": {"document": _doc(), "base_doc_rev": "whatever"},
}


@pytest.mark.parametrize("command", sorted(EDITOR))
async def test_a_phone_and_a_plain_user_are_refused_and_nothing_is_written(world, hass_ws_client, command):
    before = world.store.doc_rev
    for who in ("phone", "plain", "tablet"):
        client = await _client(world, hass_ws_client, who)
        resp = await _send(client, command, **EDITOR[command])
        assert resp["success"] is False and resp["error"]["code"] == "unauthorized", (who, resp)
    assert world.store.doc_rev == before
    assert "portico.content" not in world.storage


@pytest.mark.parametrize("command", sorted(EDITOR))
async def test_an_administrator_reaches_every_editor_command(world, hass_ws_client, command):
    body = dict(EDITOR[command])
    if command == "portico/content/save":
        body["base_doc_rev"] = world.store.doc_rev
    resp = await _send(await _client(world, hass_ws_client), command, **body)
    assert resp["success"], resp


async def test_the_editor_rows_are_admin_only_in_the_table():
    from .. import auth
    for command in EDITOR:
        assert auth.COMMANDS[command] == frozenset({auth.ADMIN}), command
    assert auth.COMMANDS["portico/kiosk/hello"] == frozenset({auth.ANY})


async def test_a_second_admin_class_user_is_enough_and_a_demoted_one_is_not(world, hass_ws_client):
    """The class is read from the connection's user at each command, not cached from connect."""
    user, token = await _phone_user(world.hass, name="Promoted")
    client = await hass_ws_client(world.hass, access_token=token)
    assert (await _send(client, "portico/content/document"))["success"] is False
    await world.hass.auth.async_update_user(user, group_ids=[GROUP_ID_ADMIN])
    resp = await _send(client, "portico/content/document", 2)
    assert resp["success"], resp


# ---- content/document ------------------------------------------------------------------------------------

async def test_document_returns_the_working_set(world, hass_ws_client):
    world.hass.states.async_set("camera.kitchen", "idle")
    await world.store.async_save_document(
        _doc(dashboards={"kitchen": {"name": "Kitchen tablet", "ha_user_id": world.tablet.id, "room": "Kitchen",
                                      "cameras": ["camera.kitchen"]}},
             overrides={"old-phone": {"pages": [_page("extra", "Extra", action_id=None)], "rules": [],
                                      "roster_hide": []}}),
        world.store.doc_rev)
    _admin, result = await _document(world, hass_ws_client)
    assert set(result) == {"document", "doc_rev", "resolved", "endpoints", "orphans", "ha_users"}
    assert result["document"] == world.store.document() and result["doc_rev"] == world.store.doc_rev
    page = result["document"]["template"]["pages"][0]
    assert result["document"]["schema_version"] == 4 and page["blocks"] and set(page) == {"id", "label", "blocks"}
    assert result["orphans"] == ["old-phone"]
    # one resolved document per callee (phones and screens), each exactly what the store resolves
    assert set(result["resolved"]) == {PHONE_ID, "kitchen"}
    assert result["resolved"][PHONE_ID] == world.store.resolve_document(PHONE_ID)
    assert result["resolved"]["kitchen"]["kind"] == "dashboard"
    assert result["resolved"][PHONE_ID]["pages"][0]["blocks"][0]["actions"][0]["name"] == "Gate lock"
    rows = {r["id"]: r for r in result["endpoints"]}
    assert set(rows) == {PHONE_ID, "kitchen", DOOR_ID}
    assert rows[PHONE_ID]["kind"] == "phone" and rows[PHONE_ID]["label"].startswith("Alice's phone")
    assert rows["kitchen"]["ha_user_id"] == world.tablet.id and rows["kitchen"]["cameras"] == ["camera.kitchen"]
    assert rows[DOOR_ID]["cameras"] == ["camera.gate"]
    assert "secrets" not in json.dumps(result)         # no door secret, no registration, no token in the working set


async def test_document_lists_ha_users_without_system_users_and_marks_the_open_kiosk(world, hass_ws_client):
    await world.hass.auth.async_create_system_user("Supervisor")
    _admin, result = await _document(world, hass_ws_client)
    users = {u["id"]: u for u in result["ha_users"]}
    assert world.tablet.id in users and world.admin_user.id in users
    assert all(set(u) == {"id", "name", "is_admin", "kiosk_open"} for u in users.values())
    assert "Supervisor" not in {u["name"] for u in users.values()}
    assert users[world.tablet.id]["kiosk_open"] is False
    tablet = await _client(world, hass_ws_client, "tablet")
    assert (await _send(tablet, "portico/kiosk/hello"))["success"]
    _admin, result = await _document(world, hass_ws_client)
    assert {u["id"]: u["kiosk_open"] for u in result["ha_users"]}[world.tablet.id] is True


async def test_document_of_an_empty_store_is_the_empty_v4_document(world, hass_ws_client):
    _admin, result = await _document(world, hass_ws_client)
    assert result["document"] == {"schema_version": 4, "template": {"pages": [], "rules": [], "roster": ["*"]},
                                  "overrides": {}, "dashboards": {}, "endpoints": {}}
    assert result["orphans"] == [] and result["resolved"][PHONE_ID]["pages"] == []


# ---- content/save ----------------------------------------------------------------------------------------

async def test_save_stores_the_document_and_the_phone_sees_it(world, hass_ws_client):
    heard = []
    world.store.add_home_listener(lambda: heard.append("home"))
    admin, got = await _document(world, hass_ws_client)
    resp = await _send(admin, "portico/content/save", 2, document=_doc(), base_doc_rev=got["doc_rev"])
    assert resp["success"], resp
    assert resp["result"] == {"doc_rev": world.store.doc_rev} and resp["result"]["doc_rev"] != got["doc_rev"]
    assert world.storage["portico.content"]["data"]["template"]["pages"][0]["id"] == "home"
    assert heard == ["home"]                                  # subscribers refetch (content_changed)
    # and the contract-3 reply the app reads shows the page's cameras and actions
    phone = await _client(world, hass_ws_client, "phone")
    page = (await _send(phone, "portico/content/get"))["result"]["pages"][0]
    (block,) = page["blocks"]
    assert [c["id"] for c in block["cameras"]] == ["gate"] and [a["id"] for a in block["actions"]] == ["open_gate"]
    assert "cameras" not in page and "actions" not in page
    # the next document is the saved one at the new revision
    _admin, again = await _document(world, hass_ws_client)
    assert again["doc_rev"] == resp["result"]["doc_rev"] and again["document"]["template"]["pages"][0]["id"] == "home"


async def test_save_of_the_unchanged_document_writes_and_announces_nothing(world, hass_ws_client):
    admin, got = await _document(world, hass_ws_client)
    await _send(admin, "portico/content/save", 2, document=_doc(), base_doc_rev=got["doc_rev"])
    admin, got = await _document(world, hass_ws_client)
    heard = []
    world.store.add_home_listener(lambda: heard.append(1))
    resp = await _send(admin, "portico/content/save", 3, document=got["document"], base_doc_rev=got["doc_rev"])
    assert resp["success"] and resp["result"] == {"doc_rev": got["doc_rev"]} and heard == []


async def test_save_refuses_a_stale_base_doc_rev_and_changes_nothing(world, hass_ws_client):
    """Two sessions load the same document; the second to save is told so and the first one's edit stands."""
    a, got_a = await _document(world, hass_ws_client)
    b, got_b = await _document(world, hass_ws_client)
    assert got_a["doc_rev"] == got_b["doc_rev"]
    first = _doc(template={"pages": [_page(label="From A")], "rules": [], "roster": ["*"]})
    assert (await _send(a, "portico/content/save", 2, document=first, base_doc_rev=got_a["doc_rev"]))["success"]
    after_a = world.store.doc_rev
    heard = []
    world.store.add_home_listener(lambda: heard.append(1))
    second = _doc(template={"pages": [_page(label="From B")], "rules": [], "roster": ["*"]})
    resp = await _send(b, "portico/content/save", 2, document=second, base_doc_rev=got_b["doc_rev"])
    assert resp["success"] is False and resp["error"]["code"] == "conflict", resp
    assert world.store.doc_rev == after_a and heard == []
    assert world.store.document()["template"]["pages"][0]["label"] == "From A"


async def test_save_checks_the_base_even_when_the_document_is_otherwise_the_same(world, hass_ws_client):
    admin, got = await _document(world, hass_ws_client)
    resp = await _send(admin, "portico/content/save", 2, document=got["document"], base_doc_rev="0" * 12)
    assert resp["success"] is False and resp["error"]["code"] == "conflict"


async def test_a_conflict_that_appears_while_a_save_awaits_is_still_a_conflict(world, hass_ws_client, monkeypatch):
    """The user lookups of a new screen await; another save landing in that gap must not be overwritten."""
    admin, got = await _document(world, hass_ws_client)
    real = world.hass.auth.async_get_user

    async def slow(user_id):
        await world.store.async_save_document(_doc(home_name="Landed meanwhile"), world.store.doc_rev)
        return await real(user_id)

    monkeypatch.setattr(world.hass.auth, "async_get_user", slow)
    document = _doc(dashboards={"kitchen": {"name": "K", "ha_user_id": world.tablet.id, "cameras": []}})
    resp = await _send(admin, "portico/content/save", 2, document=document, base_doc_rev=got["doc_rev"])
    assert resp["success"] is False and resp["error"]["code"] == "conflict", resp
    assert world.store.home_name == "Landed meanwhile" and world.store.dashboards == {}


def _bad(case):
    """(name, document, text that must appear in the error)."""
    return {
        "unknown-top-level-field": (_doc(surprise=1), "surprise"),
        "unknown-template-field": (_doc(template={"pages": [], "rules": [], "roster": ["*"], "extra": 1}), "extra"),
        "wrong-schema": (_doc(schema_version=2), "schema_version"),
        "bad-kind": (_doc(template={"pages": [_blocked([
            {"id": "x", "label": "X", "kind": "garage_door", "entity_id": "cover.x"}])], "rules": [],
            "roster": ["*"]}), "unknown kind"),
        "wrong-domain": (_doc(template={"pages": [_page(camera="light.gate")], "rules": [], "roster": ["*"]}),
                         "camera entity"),
        "duplicate-page": (_doc(template={"pages": [_page(), _page()], "rules": [], "roster": ["*"]}), "unique id"),
        "too-many-pages": (_doc(template={"pages": [_page(f"p{n}", action_id=None, camera=None) for n in range(13)],
                                          "rules": [], "roster": ["*"]}), "at most 12 pages"),
        "rule-names-a-missing-button": (_doc(template={"pages": [_page()], "roster": ["*"], "rules": [
            {"id": "doors", "caller": "kind:door", "cameras": None, "ring_buttons": ["nope"], "call_buttons": []}]}),
            "'nope'"),
        "rule-with-a-bad-kind": (_doc(template={"pages": [_page()], "roster": ["*"], "rules": [
            {"id": "r", "caller": "kind:car", "cameras": None, "ring_buttons": [], "call_buttons": []}]}),
            "caller kind"),
        "override-rule-names-a-missing-button": (_doc(overrides={PHONE_ID: {"pages": [], "roster_hide": [], "rules": [
            {"id": "r", "caller": "kind:door", "cameras": None, "ring_buttons": ["ghost"], "call_buttons": []}]}}),
            "'ghost'"),
        "roster-star-with-others": (_doc(template={"pages": [], "rules": [], "roster": ["*", "a"]}), "stands alone"),
        "roster-twice": (_doc(template={"pages": [], "rules": [], "roster": ["a", "a"]}), "twice"),
        "dashboard-without-a-user": (_doc(dashboards={"kitchen": {"name": "K"}}), "ha_user_id"),
        "dashboard-bad-slug": (_doc(dashboards={"Tablet": {"name": "K", "ha_user_id": "u"}}), "slug"),
        "dashboard-slug-is-a-phone": (_doc(dashboards={PHONE_ID: {"name": "K", "ha_user_id": "u"}}), "phone or a door"),
        "dashboard-unknown-user": (_doc(dashboards={"kitchen": {"name": "K", "ha_user_id": "no-such-user"}}),
                                   "no Home Assistant user"),
        "dashboard-camera-not-a-camera": (_doc(dashboards={"kitchen": {"name": "K", "ha_user_id": "u",
                                                                       "cameras": ["light.x"]}}), "camera entity"),
        "endpoint-declaration-bad": (_doc(endpoints={PHONE_ID: {"cameras": ["lock.gate"]}}), "camera entity"),
        "call-button-to-nobody": (_doc(template={"pages": [_blocked([
            {"id": "ring", "label": "Ring", "kind": "call", "target": {"type": "phone", "id": "nobody"}}])],
            "rules": [], "roster": ["*"]}), "nothing called"),
        "home-name-too-long": (_doc(home_name="x" * 41), "home name"),
        "override-not-an-object": (_doc(overrides={PHONE_ID: []}), "must be an object"),
    }[case]


BAD_CASES = ["unknown-top-level-field", "unknown-template-field", "wrong-schema", "bad-kind", "wrong-domain",
             "duplicate-page", "too-many-pages", "rule-names-a-missing-button", "rule-with-a-bad-kind",
             "override-rule-names-a-missing-button", "roster-star-with-others", "roster-twice",
             "dashboard-without-a-user", "dashboard-bad-slug", "dashboard-slug-is-a-phone", "dashboard-unknown-user",
             "dashboard-camera-not-a-camera", "endpoint-declaration-bad", "call-button-to-nobody",
             "home-name-too-long", "override-not-an-object"]


@pytest.mark.parametrize("case", BAD_CASES)
async def test_save_validates_the_whole_document_and_a_refusal_changes_nothing(world, hass_ws_client, case):
    document, text = _bad(case)
    before, stored = world.store.doc_rev, copy.deepcopy(world.store.document())
    heard = []
    world.store.add_home_listener(lambda: heard.append(1))
    admin = await _client(world, hass_ws_client)
    resp = await _send(admin, "portico/content/save", document=document, base_doc_rev=before)
    assert resp["success"] is False and resp["error"]["code"] == "invalid", resp
    assert text in resp["error"]["message"], resp["error"]["message"]
    assert world.store.doc_rev == before and world.store.document() == stored and heard == []
    assert "portico.content" not in world.storage


async def test_a_malformed_request_is_a_schema_error_not_a_crash(world, hass_ws_client):
    admin = await _client(world, hass_ws_client)
    for n, body in enumerate(({"document": "text", "base_doc_rev": "x"}, {"document": {}}, {"base_doc_rev": "x"}), 1):
        resp = await _send(admin, "portico/content/save", n, **body)
        assert resp["success"] is False and resp["error"]["code"] == "invalid_format", resp


async def test_a_call_button_already_stored_survives_an_unrelated_edit(world, hass_ws_client):
    """As the Configure flow: a call button whose target has vanished keeps saving; only a NEW target must exist."""
    stored = _doc(template={"pages": [{"id": "home", "cameras": [], "actions": [
        {"id": "ring", "label": "Ring", "kind": "call", "target": {"type": "phone", "id": "gone"}}]}],
        "rules": [], "roster": ["*"]})                      # a v3 file: flat pages, read through the migration
    await _reload(world, _v3(template=stored["template"])["data"])
    admin, got = await _document(world, hass_ws_client)
    edited = copy.deepcopy(got["document"])
    edited["home_name"] = "My Home"
    resp = await _send(admin, "portico/content/save", 2, document=edited, base_doc_rev=got["doc_rev"])
    assert resp["success"], resp
    edited["template"]["pages"][0]["blocks"][0]["actions"][0]["target"]["id"] = "someone-else"
    resp = await _send(admin, "portico/content/save", 3, document=edited, base_doc_rev=resp["result"]["doc_rev"])
    assert resp["success"] is False and resp["error"]["code"] == "invalid" and "nothing called" in resp["error"]["message"]


async def test_a_screen_declared_in_the_document_can_be_called_in_the_same_document(world, hass_ws_client):
    admin, got = await _document(world, hass_ws_client)
    document = _doc(dashboards={"kitchen": {"name": "Kitchen", "ha_user_id": world.tablet.id, "cameras": []}},
                    template={"pages": [_blocked([
                        {"id": "ring", "label": "Ring kitchen", "kind": "call",
                         "target": {"type": "dashboard", "id": "kitchen"}}])], "rules": [], "roster": ["*"]})
    resp = await _send(admin, "portico/content/save", 2, document=document, base_doc_rev=got["doc_rev"])
    assert resp["success"], resp
    assert get_endpoint_registry(world.hass).get("kitchen", kind="dashboard") is not None


async def test_a_saved_screen_makes_its_user_a_dashboard_caller(world, hass_ws_client):
    from .. import auth
    admin, got = await _document(world, hass_ws_client)
    assert auth.is_dashboard_user(world.hass, world.tablet.id) is False
    document = _doc(dashboards={"kitchen": {"name": "Kitchen", "ha_user_id": world.tablet.id, "cameras": []}})
    assert (await _send(admin, "portico/content/save", 2, document=document, base_doc_rev=got["doc_rev"]))["success"]
    assert auth.is_dashboard_user(world.hass, world.tablet.id) is True
    # and that changes no permission today: it is still refused the editor
    tablet = await _client(world, hass_ws_client, "tablet")
    assert (await _send(tablet, "portico/content/document"))["error"]["code"] == "unauthorized"


# ---- "Not attached": a save never drops an orphan override ---------------------------------------------------

async def _with_orphan(world):
    await _reload(world, _v3(
        template={"pages": [_v3_page()], "rules": [], "roster": ["*"]},
        overrides={"old-phone": {"pages": [_v3_page("old", "Old", action_id=None)], "rules": [], "roster_hide": []}})["data"])
    assert world.store.orphan_keys() == ["old-phone"]


async def test_save_refuses_a_document_that_leaves_out_an_orphan_override(world, hass_ws_client):
    await _with_orphan(world)
    admin, got = await _document(world, hass_ws_client)
    without = copy.deepcopy(got["document"])
    del without["overrides"]["old-phone"]
    resp = await _send(admin, "portico/content/save", 2, document=without, base_doc_rev=got["doc_rev"])
    assert resp["success"] is False and resp["error"]["code"] == "invalid" and "old-phone" in resp["error"]["message"]
    assert "old-phone" in world.store.overrides                           # still there, with its page
    assert world.store.overrides["old-phone"]["pages"][0]["id"] == "old"


async def test_save_keeps_an_orphan_that_is_in_the_document_and_edits_it_when_asked(world, hass_ws_client):
    await _with_orphan(world)
    admin, got = await _document(world, hass_ws_client)
    edited = copy.deepcopy(got["document"])
    edited["overrides"]["old-phone"]["pages"][0]["label"] = "Old, renamed"
    assert (await _send(admin, "portico/content/save", 2, document=edited, base_doc_rev=got["doc_rev"]))["success"]
    assert world.store.overrides["old-phone"]["pages"][0]["label"] == "Old, renamed"


async def test_drop_orphans_deletes_a_named_orphan_and_writes_a_move(world, hass_ws_client):
    await _with_orphan(world)
    admin, got = await _document(world, hass_ws_client)
    moved = copy.deepcopy(got["document"])
    moved["overrides"][PHONE_ID] = moved["overrides"].pop("old-phone")             # a move: new key, old key named
    resp = await _send(admin, "portico/content/save", 2, document=moved, base_doc_rev=got["doc_rev"],
                       drop_orphans=["old-phone"])
    assert resp["success"], resp
    assert "old-phone" not in world.store.overrides and world.store.overrides[PHONE_ID]["pages"][0]["id"] == "old"
    assert world.store.orphan_keys() == []
    # a plain delete
    await _with_orphan(world)
    _admin, got = await _document(world, hass_ws_client)
    gone = copy.deepcopy(got["document"])
    del gone["overrides"]["old-phone"]
    assert (await _send(admin, "portico/content/save", 3, document=gone, base_doc_rev=got["doc_rev"],
                        drop_orphans=["old-phone"]))["success"]
    assert world.store.overrides == {}


async def test_drop_orphans_refuses_what_is_not_an_orphan_or_is_still_in_the_document(world, hass_ws_client):
    await _with_orphan(world)
    admin, got = await _document(world, hass_ws_client)
    attached = copy.deepcopy(got["document"])
    attached["overrides"][PHONE_ID] = {"pages": [], "rules": [], "roster_hide": []}
    resp = await _send(admin, "portico/content/save", 2, document=attached, base_doc_rev=got["doc_rev"],
                       drop_orphans=[PHONE_ID])
    assert resp["success"] is False and "not a 'Not attached'" in resp["error"]["message"]
    resp = await _send(admin, "portico/content/save", 3, document=got["document"], base_doc_rev=got["doc_rev"],
                       drop_orphans=["old-phone"])
    assert resp["success"] is False and "also in the document" in resp["error"]["message"]
    assert "old-phone" in world.store.overrides


# ---- the one write path: the pre-migration copy and HA-L9 ------------------------------------------------------

async def test_the_first_save_over_a_v2_file_leaves_the_v2_copy(world, hass_ws_client):
    v2 = {"schema_version": 2, "template": {"pages": [{"id": "home", "cameras": [], "actions": []}], "rules": [],
                                             "roster": []}, "overrides": {"old": {"pages": [], "rules": [],
                                                                                "roster_hide": []}},
          "dashboards": {}}
    await _reload(world, v2)
    admin, got = await _document(world, hass_ws_client)
    assert got["document"]["template"]["roster"] == ["*"] and "old" in got["document"]["overrides"]
    edited = copy.deepcopy(got["document"])
    edited["home_name"] = "My Home"
    assert (await _send(admin, "portico/content/save", 2, document=edited, base_doc_rev=got["doc_rev"]))["success"]
    assert world.storage["portico.content.v2"]["data"] == v2                       # the file as it was, once
    assert world.storage["portico.content"]["data"]["schema_version"] == 4
    assert "old" in world.storage["portico.content"]["data"]["overrides"]          # nothing dropped


async def test_a_save_over_a_newer_schema_is_refused_and_the_file_is_not_rewritten(world, hass_ws_client):
    newer = {"schema_version": 7, "template": {"pages": [], "rules": [], "roster": ["*"]}, "overrides": {},
             "dashboards": {}, "endpoints": {}, "future_field": {"keep": "me"}}
    await _reload(world, newer)
    admin, got = await _document(world, hass_ws_client)
    resp = await _send(admin, "portico/content/save", 2, document=got["document"], base_doc_rev=got["doc_rev"])
    assert resp["success"] is False and resp["error"]["code"] == "invalid" and "newer" in resp["error"]["message"]
    edited = copy.deepcopy(got["document"])
    edited["home_name"] = "X"
    resp = await _send(admin, "portico/content/save", 3, document=edited, base_doc_rev=got["doc_rev"])
    assert resp["success"] is False and "newer" in resp["error"]["message"]
    assert world.storage["portico.content"]["data"] == newer


async def test_a_save_that_fails_to_write_is_rolled_back_in_memory(world, hass_ws_client, monkeypatch):
    admin, got = await _document(world, hass_ws_client)

    async def boom(_data):
        raise OSError("disk full")

    monkeypatch.setattr(world.store._store, "async_save", boom)
    with pytest.raises(OSError):
        await world.store.async_save_document(_doc(home_name="Lost"), got["doc_rev"])
    assert world.store.doc_rev == got["doc_rev"] and world.store.home_name == ""


# ---- content/preview -------------------------------------------------------------------------------------------

def _ruled(**extra):
    rule = {"id": "doors", "caller": "kind:door", "cameras": ["camera.gate"], "ring_buttons": ["open_gate"],
            "call_buttons": []}
    return _doc(template={"pages": [_page()], "rules": [rule], "roster": ["*"]}, **extra)


async def test_preview_is_exactly_what_the_phone_would_receive_after_the_save(world, hass_ws_client):
    admin, got = await _document(world, hass_ws_client)
    draft = _ruled()
    preview = await _send(admin, "portico/content/preview", 2, document=draft, endpoint=PHONE_ID)
    assert preview["success"], preview
    assert world.store.doc_rev == got["doc_rev"] and "portico.content" not in world.storage      # nothing written
    assert (await _send(admin, "portico/content/save", 3, document=draft, base_doc_rev=got["doc_rev"]))["success"]
    assert preview["result"] == world.store.resolve_document(PHONE_ID)         # the very document, rev included
    assert preview["result"]["callers"][DOOR_ID]["ring_buttons"][0]["id"] == "open_gate"
    assert preview["result"]["callers"][DOOR_ID]["cameras"][0]["id"].startswith("c-")


async def test_preview_narrows_callers_to_the_chosen_caller(world, hass_ws_client):
    admin, _got = await _document(world, hass_ws_client)
    draft = _ruled(dashboards={"kitchen": {"name": "Kitchen", "ha_user_id": world.tablet.id, "cameras": []}})
    everyone = (await _send(admin, "portico/content/preview", 2, document=draft, endpoint=PHONE_ID))["result"]
    assert set(everyone["callers"]) == {"kitchen", DOOR_ID, "*"}
    narrowed = (await _send(admin, "portico/content/preview", 3, document=draft, endpoint=PHONE_ID,
                            caller=DOOR_ID))["result"]
    assert set(narrowed["callers"]) == {DOOR_ID, "*"} and narrowed["callers"][DOOR_ID] == everyone["callers"][DOOR_ID]
    assert narrowed["pages"] == everyone["pages"] and narrowed["roster"] == everyone["roster"]
    unknown = (await _send(admin, "portico/content/preview", 4, document=draft, endpoint=PHONE_ID,
                           caller="a-door-nobody-knows"))["result"]
    assert set(unknown["callers"]) == {"*"}                                            # design case 8


async def test_preview_resolves_a_screen_that_is_not_saved_yet(world, hass_ws_client):
    admin, _got = await _document(world, hass_ws_client)
    draft = _doc(dashboards={"kitchen": {"name": "Kitchen", "ha_user_id": world.tablet.id, "cameras": []}},
                 overrides={"kitchen": {"pages": [_page("only-kitchen", "Only kitchen", action_id=None)],
                                        "rules": [], "roster_hide": []}})
    resp = await _send(admin, "portico/content/preview", 2, document=draft, endpoint="kitchen")
    assert resp["success"], resp
    assert resp["result"]["kind"] == "dashboard" and [p["id"] for p in resp["result"]["pages"]] == ["home", "only-kitchen"]
    assert world.store.dashboards == {}


async def test_preview_of_a_phones_cameras_and_a_per_phone_rule_uses_the_draft(world, hass_ws_client):
    admin, _got = await _document(world, hass_ws_client)
    draft = _doc(overrides={PHONE_ID: {"pages": [], "roster_hide": [], "rules": [
        {"id": "quiet", "caller": DOOR_ID, "cameras": None, "ring_buttons": [], "call_buttons": []}]}})
    resp = await _send(admin, "portico/content/preview", 2, document=draft, endpoint=PHONE_ID, caller=DOOR_ID)
    assert resp["result"]["callers"][DOOR_ID]["ring_buttons"] == []       # the override rule wins (design case 3)
    assert resp["result"]["callers"][DOOR_ID]["cameras"][0]["name"] == "Gate camera"


async def test_preview_refuses_an_invalid_draft_with_the_save_text_and_an_unknown_endpoint(world, hass_ws_client):
    admin, got = await _document(world, hass_ws_client)
    document, text = _bad("rule-names-a-missing-button")
    resp = await _send(admin, "portico/content/preview", 2, document=document, endpoint=PHONE_ID)
    assert resp["success"] is False and resp["error"]["code"] == "invalid" and text in resp["error"]["message"]
    saved = await _send(admin, "portico/content/save", 3, document=document, base_doc_rev=got["doc_rev"])
    assert saved["error"]["message"] == resp["error"]["message"]
    resp = await _send(admin, "portico/content/preview", 4, document=_doc(), endpoint=DOOR_ID)
    assert resp["success"] is False and resp["error"]["code"] == "unknown_endpoint"         # a door has no screen
    resp = await _send(admin, "portico/content/preview", 5, document=_doc(), endpoint="nobody")
    assert resp["error"]["code"] == "unknown_endpoint"


# ---- kiosk/hello -----------------------------------------------------------------------------------------------

async def _declare_kitchen(world, hass_ws_client):
    admin, got = await _document(world, hass_ws_client)
    document = _doc(dashboards={"kitchen": {"name": "Kitchen", "ha_user_id": world.tablet.id, "cameras": []},
                                "hall": {"name": "Hall", "ha_user_id": world.tablet.id, "cameras": []}})
    assert (await _send(admin, "portico/content/save", 2, document=document, base_doc_rev=got["doc_rev"]))["success"]


async def test_hello_tells_the_declared_user_which_screens_are_its_own(world, hass_ws_client):
    await _declare_kitchen(world, hass_ws_client)
    tablet = await _client(world, hass_ws_client, "tablet")
    resp = await _send(tablet, "portico/kiosk/hello", endpoint="kitchen")
    assert resp["success"], resp
    assert resp["result"] == {"user": {"id": world.tablet.id, "name": "Kitchen tablet user"},
                              "declared": ["kitchen", "hall"], "endpoint_ok": True}
    assert (await _send(tablet, "portico/kiosk/hello", 2, endpoint="garage"))["result"]["endpoint_ok"] is False
    assert (await _send(tablet, "portico/kiosk/hello", 3))["result"]["endpoint_ok"] is True       # has some screen


async def test_hello_carries_the_kiosk_look_only_when_the_settings_entry_sets_it(world, hass_ws_client):
    """DEV-836: the page background and the back-rail route are options, not code; the reply is the old shape without them."""
    from ..editor_commands import CONF_KIOSK_BACK_PATH, CONF_KIOSK_BACKGROUND_URL
    from ..push import const as push_const

    await _declare_kitchen(world, hass_ws_client)
    tablet = await _client(world, hass_ws_client, "tablet")
    assert "kiosk" not in (await _send(tablet, "portico/kiosk/hello", endpoint="kitchen"))["result"]
    entry = MockConfigEntry(domain=DOMAIN, data={push_const.ENTRY_TYPE: push_const.ENTRY_TYPE_PUSH},
                            options={CONF_KIOSK_BACKGROUND_URL: "https://pics.example.com/bg.jpg", CONF_KIOSK_BACK_PATH: "/lovelace/home"})
    entry.add_to_hass(world.hass)
    resp = await _send(tablet, "portico/kiosk/hello", 2, endpoint="kitchen")
    assert resp["result"]["kiosk"] == {"background_url": "https://pics.example.com/bg.jpg", "back_path": "/lovelace/home"}


async def test_hello_from_an_undeclared_user_says_so_and_is_remembered_for_the_picker(world, hass_ws_client):
    plain = await _client(world, hass_ws_client, "plain")
    resp = await _send(plain, "portico/kiosk/hello", endpoint="kitchen")
    assert resp["result"] == {"user": {"id": world.plain.id, "name": "Plain user"}, "declared": [],
                              "endpoint_ok": False}
    assert get_kiosk_seen(world.hass).is_open(world.plain.id)


async def test_hello_from_an_administrator_is_not_recorded(world, hass_ws_client):
    await _declare_kitchen(world, hass_ws_client)
    admin = await _client(world, hass_ws_client)
    resp = await _send(admin, "portico/kiosk/hello", endpoint="kitchen")        # an admin may test any declared screen
    assert resp["result"]["endpoint_ok"] is True and resp["result"]["declared"] == []
    assert (await _send(admin, "portico/kiosk/hello", 2, endpoint="nope"))["result"]["endpoint_ok"] is False
    assert not get_kiosk_seen(world.hass).is_open(world.admin_user.id) and len(get_kiosk_seen(world.hass)) == 0


def _user(n, **kw):
    return SimpleNamespace(id=f"u{n}", name=f"User {n}", is_admin=kw.get("is_admin", False),
                           system_generated=kw.get("system_generated", False))


async def test_hello_memory_is_bounded_least_recently_seen_first_and_one_entry_per_user():
    seen = KioskSeen()
    assert KIOSK_SEEN_MAX == 32
    for n in range(KIOSK_SEEN_MAX):
        assert seen.record(_user(n)) is True
    assert len(seen) == 32
    seen.record(_user(0))                                   # u0 is seen again: it is now the most recent
    seen.record(_user(0))                                   # and a repeat adds no entry
    assert len(seen) == 32
    for n in range(100, 110):                               # ten newcomers evict the ten least recently seen
        seen.record(_user(n))
    assert len(seen) == 32
    assert seen.is_open("u0") and not seen.is_open("u1") and not seen.is_open("u10") and seen.is_open("u11")
    assert all(seen.is_open(f"u{n}") for n in range(100, 110))


async def test_hello_memory_ignores_administrators_system_users_and_no_user():
    seen = KioskSeen()
    assert seen.record(_user(1, is_admin=True)) is False
    assert seen.record(_user(2, system_generated=True)) is False
    assert seen.record(None) is False
    assert len(seen) == 0


async def test_hello_over_the_wire_cannot_grow_the_memory_past_the_bound(world, hass_ws_client):
    seen = get_kiosk_seen(world.hass)
    for n in range(KIOSK_SEEN_MAX + 8):
        seen.record(_user(n))
    plain = await _client(world, hass_ws_client, "plain")
    assert (await _send(plain, "portico/kiosk/hello"))["success"]
    assert len(seen) == KIOSK_SEEN_MAX and seen.is_open(world.plain.id) and not seen.is_open("u0")


async def test_hello_is_refused_without_a_user():
    from .. import auth
    assert not auth.allowed(None, None, "portico/kiosk/hello")


# ---- the diagnostics download -----------------------------------------------------------------------------------

async def test_the_push_entry_diagnostics_carry_the_document_without_any_enroll_id(world):
    await world.store.async_save_document(
        _doc(overrides={PHONE_ID: {"pages": [_page("mine", "Mine", action_id=None)], "roster_hide": [], "rules": [
            {"id": "quiet", "caller": PHONE_ID, "cameras": None, "ring_buttons": [], "call_buttons": []}]},
                        "old-enroll-id": {"pages": [], "rules": [], "roster_hide": [PHONE_ID]}},
             endpoints={PHONE_ID: {"cameras": ["camera.gate"]}},
             dashboards={"kitchen": {"name": "Kitchen", "ha_user_id": world.tablet.id, "cameras": []}},
             home_name="My Home"),
        world.store.doc_rev)
    entry = MockConfigEntry(domain=DOMAIN, title="Portico push ring notification", unique_id="push",
                            data={"entry_type": "push", "enrolled_phones": []})
    entry.add_to_hass(world.hass)
    out = await async_get_config_entry_diagnostics(world.hass, entry)
    text = json.dumps(out)
    assert PHONE_ID not in text and "old-enroll-id" not in text
    content = out["content"]
    assert content["home_name"] == "My Home" and "kitchen" in content["dashboards"]       # a screen's slug stays
    keys = sorted(content["overrides"])
    assert len(keys) == 2 and all(k.startswith("endpoint-") for k in keys)
    mapped = [k for k in keys if content["overrides"][k]["rules"]][0]
    assert content["overrides"][mapped]["rules"][0]["caller"] == mapped           # the same id, the same word
    assert list(content["endpoints"]) == [mapped]
    assert content["overrides"][[k for k in keys if k != mapped][0]]["roster_hide"] == [mapped]


async def test_diagnostics_for_a_door_or_phone_entry_have_no_content_section(world):
    entry = MockConfigEntry(domain=DOMAIN, title="Gate", data={"driver": "dahua_sip", "host": "h", "enrolled_phones": []})
    entry.add_to_hass(world.hass)
    assert "content" not in await async_get_config_entry_diagnostics(world.hass, entry)


async def test_pseudonymise_is_pure_and_total():
    document = {"overrides": {"a": {"roster_hide": ["a", "z"]}}, "x": [{"caller": "a"}], "n": 1, "t": None}
    before = copy.deepcopy(document)
    out = pseudonymise_document(document, {"a"})
    assert document == before and '"a"' not in json.dumps(out)
    word = next(iter(out["overrides"]))
    assert out["overrides"][word]["roster_hide"] == [word, "z"] and out["x"] == [{"caller": word}]
    assert pseudonymise_document(document, set()) == document


# ---- contract 3 ------------------------------------------------------------------------------------------------

async def test_content_list_is_removed_the_panel_reads_the_document(world, hass_ws_client):
    admin = await _client(world, hass_ws_client)
    resp = await _send(admin, "portico/content/list")
    assert not resp["success"] and resp["error"]["code"] == "unknown_command"


async def test_a_profile_error_is_what_the_store_raises_for_a_non_object(world):
    with pytest.raises(ProfileError):
        await world.store.async_save_document([], world.store.doc_rev)
