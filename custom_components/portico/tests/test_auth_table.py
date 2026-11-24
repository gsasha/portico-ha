"""auth.py: the one authorization table (WS-10), pinned against contract 3 and the owner's decision of 2026-10-21.

The nine call commands are open to any authenticated HA user (HA-M2, #108; M6 E3 had narrowed them to
`admin, phone, dashboard` and the owner reverted that on 2026-10-21). EXPECTED below is written out by hand (not read
from auth.COMMANDS), so a row cannot change without this file changing with it; a command registered but missing from
the table fails here, and so does a stale row.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from homeassistant.components import websocket_api
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .. import auth
from ..endpoints import get_endpoint_registry
from ..config_flow import DOMAIN
from .test_push_registry import _phone_user
from .test_signaling import _setup_signaling

pytestmark = pytest.mark.asyncio

A, P, D, ANY = auth.ADMIN, auth.PHONE, auth.DASHBOARD, auth.ANY

APD = {A, P, D}

# The nine call commands, open to any authenticated HA user by the owner's decision of 2026-10-21 (HA-M2, #108).
CALL_COMMANDS = frozenset({
    "portico/answer", "portico/hangup", "portico/open_relay", "portico/keepalive", "portico/webrtc_offer",
    "portico/webrtc_answer", "portico/webrtc_ice", "portico/calls/subscribe", "portico/doors"})

# Contract 3's authorization, command by command (the design's 3.4 table, with the call commands as decided above).
EXPECTED: dict[str, set[str]] = {
    "portico/version": {ANY},
    "portico/doors": {ANY},
    "portico/calls/subscribe": {ANY},
    "portico/content/subscribe": APD,
    "portico/dial": {A, P},
    "portico/hangup": {ANY},
    "portico/open_relay": {ANY},
    "portico/answer": {ANY},
    "portico/keepalive": {ANY},
    "portico/webrtc_offer": {ANY},
    "portico/webrtc_answer": {ANY},
    "portico/webrtc_ice": {ANY},
    "portico/media_open": {P},
    "portico/media_frame": {P},
    "portico/media_close": {P},
    "portico/push/register": {P},
    "portico/push/unregister": {P},
    "portico/push/status": {P},
    # the connection log (DEV-705): a phone reports, an administrator reads and clears
    "portico/connlog/report": {P},
    "portico/wall/heartbeat": {P},
    "portico/panel/call_result": {P},
    "portico/connlog/get": {A},
    "portico/connlog/clear": {A},
    "portico/content/get": APD,
    "portico/content/run": APD,
    "portico/content/snapshot": APD,
    # live video of a camera tile: snapshot's row (live.py)
    "portico/content/live": APD,
    "portico/content/live_ice": APD,
    "portico/content/live_close": APD,
    # the editor (M6 E2): every write and the working set are admin-only; the kiosk hello is bounded in its handler
    "portico/content/document": {A},
    "portico/content/preview": {A},
    "portico/content/save": {A},
    "portico/content/pages_migrate": {A},
    "portico/content/ring_pages_migrate": {A},
    "portico/kiosk/hello": {ANY},
    # the picker's spec chips (the panel only; the phone app never sends it)
    "portico/cameras/info": {A},
    "portico/config/status": {A},
    "portico/camera/webrtc_offer": {A},
    # the Phones tab (UI split P2): the panel is open to every household login, so these three are admin and nothing else
    "portico/phones/list": {A},
    "portico/phones/enroll": {A},
    "portico/phones/cancel": {A},
    "portico/phones/revoke": {A},
    "portico/phones/set": {A},
    "portico/phones/make_panel_screen": {A},
    "portico/phones/make_panel": {A},
    # the Settings tab (UI split P4 to P6): the doors' status and settings, and the push ring notification preferences; administrators only
    "portico/door_status": {A},
    "portico/door_settings/get": {A},
    "portico/door_settings/set": {A},
    "portico/ui_settings/get": {A},
    "portico/ui_settings/set": {A},
    "portico/elements/list": {A},
    "portico/elements/set": {A},
    "portico/elements/delete": {A},
    # house calls (M7 3a, design 15.6): an enrolled phone, a declared screen or an administrator; not a login with no role
    "portico/house/answer": APD,
    "portico/house/decline": APD,
    "portico/house/hangup": APD,
    "portico/house/presence": APD,
    "portico/house/media_open": APD,
}

# One request per command that passes the schema check, for the end-to-end test.
PAYLOADS: dict[str, dict] = {
    "portico/version": {},
    "portico/doors": {},
    "portico/calls/subscribe": {},
    "portico/content/subscribe": {"endpoint": "<phone>"},
    "portico/dial": {"door_id": "d1"},
    "portico/hangup": {"call_id": "c1"},
    "portico/open_relay": {"door_id": "d1"},
    "portico/answer": {"door_id": "d1", "call_id": "c1", "client_kind": "app"},
    "portico/keepalive": {"call_id": "c1"},
    "portico/webrtc_offer": {"call_id": "c1", "door_id": "d1", "sdp": "s"},
    "portico/webrtc_answer": {"call_id": "c1", "sdp": "s"},
    "portico/webrtc_ice": {"call_id": "c1", "candidate": "c"},
    "portico/media_open": {"door_id": "d1", "call_id": "c1"},
    "portico/media_frame": {"call_id": "c1", "d": "AA=="},
    "portico/media_close": {"call_id": "c1"},
    "portico/push/register": {"installation_id": "i1", "kind": "fid", "registration": "r", "package": "p",
                              "fcm_project_id": "f", "app_version": "0.5.0-alpha"},
    "portico/push/unregister": {"installation_id": "i1"},
    "portico/push/status": {},
    "portico/connlog/report": {"reports": []},
    "portico/wall/heartbeat": {"panel_mode": True},
    "portico/panel/call_result": {"request_id": "0123456789abcdef", "ok": True},
    "portico/connlog/get": {},
    "portico/connlog/clear": {},
    "portico/content/get": {"endpoint": "<phone>"},
    "portico/content/run": {"action_id": "a1", "endpoint": "<phone>"},
    "portico/content/snapshot": {"camera_id": "cam", "endpoint": "<phone>"},
    "portico/content/live": {"camera_id": "cam", "sdp": "v=0", "size": "big", "endpoint": "<phone>"},
    "portico/content/live_ice": {"session_id": "s1", "candidate": {"candidate": "c"}},
    "portico/content/live_close": {"session_id": "s1"},
    "portico/content/document": {},
    "portico/content/preview": {"document": {}, "endpoint": "e1"},
    "portico/content/save": {"document": {}, "base_doc_rev": "r1"},
    "portico/content/pages_migrate": {"validate_only": True},
    "portico/content/ring_pages_migrate": {"dry_run": True},
    "portico/kiosk/hello": {},
    "portico/cameras/info": {"entity_ids": ["camera.gate"]},
    "portico/config/status": {},
    "portico/camera/webrtc_offer": {"entity_id": "camera.gate", "sdp": "v=0"},
    "portico/phones/list": {},
    "portico/phones/enroll": {"name": "Auth test", "allow_http": False, "ha_dial": ""},
    "portico/phones/cancel": {"entry_id": "no-such-entry"},
    "portico/phones/revoke": {"entry_id": "no-such-entry", "confirm": True},
    "portico/phones/set": {"entry_id": "no-such-entry", "patch": {"away_ring": False}},
    "portico/phones/make_panel_screen": {"entry_id": "no-such-entry", "slug": "panel"},
    "portico/phones/make_panel": {"entry_id": "no-such-entry"},
    "portico/door_status": {},
    "portico/door_settings/get": {},
    "portico/door_settings/set": {"entry_id": "no-such-entry", "revision": "r", "patch": {"away_media": True}},
    "portico/ui_settings/get": {},
    "portico/ui_settings/set": {"patch": {"ring_window": 30}},
    "portico/elements/list": {},
    "portico/elements/set": {"element": {"kind": "status", "name": "x"}, "base_rev": "stale"},      # a stale rev: an administrator passes the table and nothing is written
    "portico/elements/delete": {"element_id": "el-00000000", "base_rev": "stale"},
    "portico/house/answer": {"call_id": "hso-1", "client_kind": "app"},
    "portico/house/decline": {"call_id": "hso-1"},
    "portico/house/hangup": {"call_id": "hso-1"},
    "portico/house/presence": {"dnd": False},
    "portico/house/media_open": {"call_id": "hso-1"},
}

# The Elements page (DEV-760 E2): administrators only, nothing else.
ELEMENTS_COMMANDS = {
    "portico/elements/list": {A},
    "portico/elements/set": {A},
    "portico/elements/delete": {A},
}

# The Settings tab's six commands (UI split P4 to P6): door status, door settings get and set, the push ring notification preferences get and set.
SETTINGS_TAB_COMMANDS = {
    "portico/door_status": {A},
    "portico/door_settings/get": {A},
    "portico/door_settings/set": {A},
    "portico/ui_settings/get": {A},
    "portico/ui_settings/set": {A},
}

SOURCE = Path(__file__).resolve().parents[1]


def _registered(hass) -> set[str]:
    return {c for c in hass.data[websocket_api.DOMAIN] if c.startswith("portico/")}


def _source_commands() -> set[str]:
    found: set[str] = set()
    for path in SOURCE.glob("*.py"):
        found |= set(re.findall(r'vol\.Required\("type"\): "(portico/[a-z_/]+)"', path.read_text(encoding="utf-8")))
    return found


@pytest.fixture
async def callers(hass):
    """An administrator, an enrolled phone, and an ordinary user, as HA users."""
    admin = await hass.auth.async_create_user(name="admin", group_ids=["system-admin"])
    phone, phone_token = await _phone_user(hass, name="phone")
    plain, plain_token = await _phone_user(hass, name="plain")
    MockConfigEntry(domain=DOMAIN, data={"driver": "dahua_sip", "host": "h", "enrolled_phones": [
        {"ha_user_id": phone.id, "name": "p"}]}, unique_id="dahua_sip:h").add_to_hass(hass)
    await _setup_signaling(hass)
    return {"admin": admin, "phone": phone, "user": plain, "tokens": {"phone": phone_token, "user": plain_token}}


async def test_every_registered_command_is_in_the_table(hass, callers):
    """A command added without a row fails here (the table is fail-closed at run time as well)."""
    registered = _registered(hass)
    assert registered, "no portico/* command registered"
    assert registered <= set(auth.COMMANDS), f"missing from auth.COMMANDS: {sorted(registered - set(auth.COMMANDS))}"
    assert set(auth.COMMANDS) <= registered, f"stale rows in auth.COMMANDS: {sorted(set(auth.COMMANDS) - registered)}"


def test_every_command_literal_in_the_source_is_in_the_table():
    assert _source_commands() == set(auth.COMMANDS)


def test_the_table_is_contract_3s_authorization():
    assert {c: set(r) for c, r in auth.COMMANDS.items()} == EXPECTED
    assert set(PAYLOADS) == set(EXPECTED)
    for row in auth.COMMANDS.values():
        assert row and row <= set(auth.CLASSES)


async def test_the_call_commands_are_open_to_any_ha_user_and_nothing_else_is(hass, callers):
    """The owner's decision of 2026-10-21 (HA-M2, #108; E3's narrowing reverted): a login with no Portico role (no
    administrator, no enrolled phone, no declared screen) may send the nine call commands, and may NOT send
    `content/*`, `cameras/info`, `dial`, `media_*` or `push/*`."""
    user = callers["user"]
    for command in sorted(CALL_COMMANDS):
        assert auth.COMMANDS[command] == {ANY}, command
        assert auth.allowed(hass, user, command), command
    closed = ("portico/content/get", "portico/content/run", "portico/content/snapshot", "portico/content/subscribe",
              "portico/content/live", "portico/content/live_ice", "portico/content/live_close",
              "portico/content/document", "portico/content/preview", "portico/content/save", "portico/content/pages_migrate", "portico/content/ring_pages_migrate",
              "portico/cameras/info", "portico/config/status", "portico/camera/webrtc_offer", "portico/phones/list", "portico/phones/enroll", "portico/phones/cancel", "portico/phones/revoke", "portico/phones/set", "portico/phones/make_panel_screen", "portico/phones/make_panel", "portico/dial", "portico/media_open", "portico/media_frame", "portico/media_close",
              "portico/push/register", "portico/push/unregister", "portico/push/status",
              "portico/connlog/report", "portico/wall/heartbeat", "portico/panel/call_result", "portico/connlog/get", "portico/connlog/clear",
              *sorted(SETTINGS_TAB_COMMANDS), *sorted(ELEMENTS_COMMANDS),
              "portico/house/answer", "portico/house/decline", "portico/house/hangup", "portico/house/presence",
              "portico/house/media_open")
    for command in closed:
        assert not auth.allowed(hass, user, command), command
    # exactly the nine, plus the two that were always open: nothing else
    assert {c for c in auth.COMMANDS if auth.allowed(hass, user, c)} == CALL_COMMANDS | {
        "portico/version", "portico/kiosk/hello"}
    assert set(closed) == set(auth.COMMANDS) - CALL_COMMANDS - {"portico/version", "portico/kiosk/hello"}


def test_the_set_of_open_commands_is_exactly_this_list():
    """A future change to which commands are `any` is a visible diff of this list, not a silent edit of a row."""
    assert {c for c, row in auth.COMMANDS.items() if ANY in row} == {
        "portico/answer", "portico/hangup", "portico/open_relay", "portico/keepalive", "portico/webrtc_offer",
        "portico/webrtc_answer", "portico/webrtc_ice", "portico/calls/subscribe", "portico/doors",
        "portico/version", "portico/kiosk/hello"}
    # a call command's row is `any` alone: narrowing it again is a decision, and a change of this file with it
    assert all(auth.COMMANDS[c] == frozenset({ANY}) for c in CALL_COMMANDS)


@pytest.mark.parametrize("command", sorted(EXPECTED))
async def test_each_class_gets_what_the_table_says(hass, callers, monkeypatch, command):
    """admin, phone, dashboard-declared user, plain user (and no user at all)."""
    dashboard = await hass.auth.async_create_user(name="dashboard", group_ids=["system-users"])
    monkeypatch.setattr(auth, "is_dashboard_user", lambda _hass, user_id: user_id == dashboard.id)
    row = EXPECTED[command]
    assert auth.allowed(hass, callers["admin"], command) == bool(row & {ANY, A})
    assert auth.allowed(hass, callers["phone"], command) == bool(row & {ANY, P})
    assert auth.allowed(hass, callers["user"], command) == bool(row & {ANY})
    assert auth.allowed(hass, dashboard, command) == bool(row & {ANY, D})
    assert auth.allowed(hass, None, command) is False


async def test_a_declared_screen_may_answer_and_hang_up_but_not_dial(hass, callers, monkeypatch):
    """A declared screen keeps a working card: it answers and hangs up like any other login, and still cannot dial."""
    declared = await hass.auth.async_create_user(name="tablet", group_ids=["system-users"])
    monkeypatch.setattr(auth, "is_dashboard_user", lambda _hass, user_id: user_id == declared.id)
    for command in ("portico/answer", "portico/hangup", "portico/open_relay", "portico/webrtc_offer"):
        assert auth.allowed(hass, declared, command)
        assert auth.allowed(hass, callers["admin"], command) and auth.allowed(hass, callers["phone"], command)
        assert auth.allowed(hass, callers["user"], command)
    assert not auth.allowed(hass, declared, "portico/dial")


async def test_an_unlisted_command_is_refused(hass, callers):
    assert not auth.allowed(hass, callers["admin"], "portico/not_in_the_table")


@pytest.mark.parametrize("command", sorted(EXPECTED))
async def test_the_handler_enforces_the_table(hass, hass_ws_client, callers, command, monkeypatch):
    """Over a real websocket, as admin, phone and plain user: refused exactly when the table says so."""
    real_require = auth.require
    recorded: list[str] = []

    def recording_require(connection, msg, cmd):
        recorded.append(cmd)
        return real_require(connection, msg, cmd)

    # A handler's own `unauthorized` is indistinguishable on the wire from auth.require's, so the preamble
    # itself is what is asserted: every handler calls auth.require with its own command name (R3-12).
    monkeypatch.setattr(auth, "require", recording_require)
    clients = {"admin": await hass_ws_client(hass),
               "phone": await hass_ws_client(hass, access_token=callers["tokens"]["phone"]),
               "user": await hass_ws_client(hass, access_token=callers["tokens"]["user"])}
    classes = {"admin": {ANY, A}, "phone": {ANY, P}, "user": {ANY}}
    # the content commands name the phone's own endpoint, so the handler's own `unauthorized` (an endpoint the caller
    # may not use) is never what a caller inside the row sees
    phone_endpoint = get_endpoint_registry(hass).for_user(callers["phone"].id)
    body = {k: (phone_endpoint.id if v == "<phone>" else v) for k, v in PAYLOADS[command].items()}
    for who, client in clients.items():
        recorded.clear()
        await client.send_json({"id": 1, "type": command, **body})
        resp = await client.receive_json()
        assert recorded and set(recorded) == {command}, (command, who, recorded)
        refused = resp.get("success") is False and resp["error"]["code"] in ("unauthorized", "not_enrolled")
        assert refused == (not (EXPECTED[command] & classes[who])), (command, who, resp)


async def test_a_screen_user_reads_its_endpoint_and_writes_nothing(hass, callers, monkeypatch):
    """The `dashboard` class is on the content reads and on no write: `save`, `preview`, `document`, `dial`, the push
    registry and the away media stay closed to a screen; the call commands and the door list reach it as `any`, and a
    user in no class at all gets only what `any` gives."""
    screen = await hass.auth.async_create_user(name="screen", group_ids=["system-users"])
    monkeypatch.setattr(auth, "is_dashboard_user", lambda _hass, user_id: user_id == screen.id)
    reach = {c for c in auth.COMMANDS if auth.allowed(hass, screen, c)}
    assert reach == {c for c, row in EXPECTED.items() if row & {ANY, D}}
    assert {"portico/content/get", "portico/content/run", "portico/content/snapshot", "portico/kiosk/hello",
            "portico/answer", "portico/doors"} <= reach
    writes = {"portico/content/save", "portico/content/preview", "portico/content/document", "portico/content/pages_migrate", "portico/content/ring_pages_migrate",
              "portico/phones/list", "portico/phones/enroll", "portico/phones/cancel", "portico/phones/revoke", "portico/phones/set", "portico/phones/make_panel_screen", "portico/phones/make_panel", "portico/dial", "portico/media_open", "portico/media_frame", "portico/media_close",
              "portico/push/register", "portico/push/unregister", "portico/push/status",
              "portico/connlog/report", "portico/wall/heartbeat", "portico/panel/call_result", "portico/connlog/get", "portico/connlog/clear",
              *SETTINGS_TAB_COMMANDS, *ELEMENTS_COMMANDS}
    assert reach.isdisjoint(writes)
    # M7 3a, 3b-S: the five house commands are a screen's (it answers, declines, hangs up, reports Do Not Disturb and
    # declares its audio leg)
    assert {"portico/house/answer", "portico/house/decline", "portico/house/hangup", "portico/house/presence",
            "portico/house/media_open"} <= reach
    nobody = {c for c in auth.COMMANDS if auth.allowed(hass, callers["user"], c)}
    assert nobody == {c for c, row in EXPECTED.items() if ANY in row}
    assert nobody.isdisjoint({"portico/content/get", "portico/content/run", "portico/content/snapshot"} | writes)
    assert nobody.isdisjoint({c for c in auth.COMMANDS if c.startswith("portico/house/")})


def test_contract_2_commands_are_not_in_the_table():
    """No aliases (O2): the v1 names are gone from the table, so a handler cannot survive under them."""
    for gone in ("portico/devices", "portico/subscribe", "portico/content/list"):
        assert gone not in auth.COMMANDS


# The UI part's admin commands for the Phones tab (UI split P2). The panel is `require_admin=False`, so each command's
# class is its ONLY access control; this list is the enumeration the design asks for (4.2): add a `portico/phones/*`
# command and this test fails until its class is stated here.
PHONES_COMMANDS = {
    "portico/phones/list": {A},
    "portico/phones/enroll": {A},
    "portico/phones/cancel": {A},
    "portico/phones/revoke": {A},
    "portico/phones/set": {A},
    "portico/phones/make_panel_screen": {A},
    "portico/phones/make_panel": {A},
}


def test_every_phones_command_in_the_source_is_listed_and_admin_only():
    """Every `portico/phones/*` command literal in the UI part's source is in PHONES_COMMANDS with exactly the admin
    class, and in the table with it. A new one cannot arrive unlisted, and none can be opened to a phone or to `any`."""
    in_source = {c for c in _source_commands() if c.startswith("portico/phones/")}
    assert in_source == set(PHONES_COMMANDS)
    for command, row in PHONES_COMMANDS.items():
        assert {c for c in auth.COMMANDS[command]} == row, command
        assert A in auth.COMMANDS[command] and auth.ANY not in auth.COMMANDS[command], command


async def test_a_phone_a_screen_and_an_ordinary_user_cannot_send_any_phones_command(hass, callers, monkeypatch):
    screen = await hass.auth.async_create_user(name="screen2", group_ids=["system-users"])
    monkeypatch.setattr(auth, "is_dashboard_user", lambda _hass, user_id: user_id == screen.id)
    for command in PHONES_COMMANDS:
        assert auth.allowed(hass, callers["admin"], command), command
        for who in (callers["phone"], callers["user"], screen, None):
            assert not auth.allowed(hass, who, command), (command, who)


def test_there_is_no_house_calls_switch_command_any_more():
    """D-20261122-aafb: the global House calls switch is gone; no `portico/house_calls/*` command exists in source or table."""
    assert not {c for c in _source_commands() if c.startswith("portico/house_calls/")}
    assert not [c for c in auth.COMMANDS if c.startswith("portico/house_calls/")]


# The Settings tab (UI split P4 to P6): door status, door settings, the push ring notification preferences. The panel is open to every household
# login, so each command's class is its ONLY access control; a command added to these families must be stated here and stays
# administrator-only.
def _settings_tab_in_source() -> set[str]:
    return {c for c in _source_commands() if c == "portico/door_status" or c.startswith(("portico/door_settings/", "portico/ui_settings/", "portico/claims/"))}


def test_every_settings_tab_command_in_the_source_is_listed_and_admin_only():
    assert _settings_tab_in_source() == set(SETTINGS_TAB_COMMANDS)
    for command, row in SETTINGS_TAB_COMMANDS.items():
        assert set(auth.COMMANDS[command]) == row, command
        assert A in auth.COMMANDS[command] and auth.ANY not in auth.COMMANDS[command], command


async def test_a_phone_a_screen_and_an_ordinary_user_cannot_send_any_settings_tab_command(hass, callers, monkeypatch):
    screen = await hass.auth.async_create_user(name="screen4", group_ids=["system-users"])
    monkeypatch.setattr(auth, "is_dashboard_user", lambda _hass, user_id: user_id == screen.id)
    for command in SETTINGS_TAB_COMMANDS:
        assert auth.allowed(hass, callers["admin"], command), command
        for who in (callers["phone"], callers["user"], screen, None):
            assert not auth.allowed(hass, who, command), (command, who)


def test_every_elements_command_in_the_source_is_listed_and_admin_only():
    assert {c for c in _source_commands() if c.startswith("portico/elements/")} == set(ELEMENTS_COMMANDS)
    for command, row in ELEMENTS_COMMANDS.items():
        assert set(auth.COMMANDS[command]) == row, command
        assert A in auth.COMMANDS[command] and auth.ANY not in auth.COMMANDS[command], command


async def test_a_phone_a_screen_and_an_ordinary_user_cannot_send_an_elements_command(hass, callers, monkeypatch):
    screen = await hass.auth.async_create_user(name="screen4", group_ids=["system-users"])
    monkeypatch.setattr(auth, "is_dashboard_user", lambda _hass, user_id: user_id == screen.id)
    for command in ELEMENTS_COMMANDS:
        assert auth.allowed(hass, callers["admin"], command), command
        for who in (callers["phone"], callers["user"], screen, None):
            assert not auth.allowed(hass, who, command), (command, who)
