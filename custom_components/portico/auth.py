"""Who may send which `portico/*` websocket command: the one authorization table (WS-10).

Every handler calls `require(connection, msg, command)` first and nothing else checks the caller's
identity. A command's row is a set of caller classes; the caller is allowed when it belongs to at
least one of them:

  admin      the connection's HA user is an administrator
  phone      the connection's HA user is an enrolled Portico phone (push.registration.enrolled_phone_name)
  dashboard  the connection's HA user is declared for a dashboard endpoint (content store v3 `dashboards`)
  any        any authenticated HA user

DECISION (owner, 2026-10-21; first taken 2026-10-20 as review finding HA-M2, #108, and re-confirmed on 2026-10-21 after
M6 E3 had narrowed these rows): the call commands (`answer`, `hangup`, `open_relay`, `keepalive`, `webrtc_offer`,
`webrtc_answer`, `webrtc_ice`, `calls/subscribe` and `doors`, the door listing) are open to ANY authenticated HA user,
and this is on purpose. The risk, accepted: any HA login, administrator or not, and any leaked HA token, can open the
gate relay, answer or hang up a call and read call state, and entity permissions are not consulted. The reasoning (as
recorded in HA-M2): every HA account on this install belongs to a household member and is trusted with the front door
and the gate already, and the kitchen tablet's user is neither an administrator nor an enrolled phone. Revisit this ONLY
if an untrusted account can exist: a guest or non-household HA user, a login shared outside the household, or an
account created for a third party. Then narrow these nine rows to `admin, phone, dashboard`; `is_dashboard_user` and
the `dashboard` class below are the ready mechanism, so declaring the tablet's user a dashboard endpoint is the step
that makes that possible. History: M6 E3 (contract 3, O8) had narrowed these rows to `admin, phone, dashboard`; the
owner reverted that on 2026-10-21.

Everything else is as contract 3 set it: `content/get`, `run`, `snapshot`, `live`, `live_ice`, `live_close` and `subscribe` are `admin, phone, dashboard`;
`content/document`, `preview`, `save`, `cameras/info`, `camera/webrtc_offer`, the five `phones/*` (list, enroll, cancel, revoke, set) and the Settings tab's six (`door_status`, `door_settings/get|set`, `ui_settings/get|set`) and the Elements page's three (`elements/list|set|delete`) are `admin`; `dial` is `admin, phone` (no client sends it);
`media_*`, `push/*` and `connlog/report` are `phone` (`connlog/get` and `connlog/clear` are `admin`); the five `house/*` commands (M7 3a, 3b-S) are `admin, phone, dashboard`. Only `version`, `kiosk/hello` and the nine call commands are open to a login with no
Portico role. A command that is registered but missing from `COMMANDS` is refused (fail closed);
tests/test_auth_table.py fails when that happens, so a new command cannot be added unlisted.

A refusal is `unauthorized` unless the command keeps the wire code it always had (`REFUSALS`).
"""
from __future__ import annotations

import logging

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant

from .push.registration import enrolled_phone_name

_LOGGER = logging.getLogger(__name__)

ADMIN = "admin"
PHONE = "phone"
DASHBOARD = "dashboard"
ANY = "any"
CLASSES = (ADMIN, PHONE, DASHBOARD, ANY)

_ANY = frozenset({ANY})
_ADMIN_PHONE = frozenset({ADMIN, PHONE})
_ADMIN_PHONE_DASHBOARD = frozenset({ADMIN, PHONE, DASHBOARD})
_PHONE = frozenset({PHONE})
_ADMIN = frozenset({ADMIN})

COMMANDS: dict[str, frozenset[str]] = {
    # contract 3 (WS-02): the handshake is open to any logged-in user, so a client can say "wrong server version"
    "portico/version": _ANY,
    # doors and calls: any authenticated HA user, by the owner's decision of 2026-10-21 (module docstring)
    "portico/doors": _ANY,
    "portico/calls/subscribe": _ANY,
    # the content feed is not a call command: an enrolled phone, a declared screen, or an administrator (E3, unchanged)
    "portico/content/subscribe": _ADMIN_PHONE_DASHBOARD,
    "portico/dial": _ADMIN_PHONE,
    "portico/hangup": _ANY,
    "portico/open_relay": _ANY,
    "portico/answer": _ANY,
    "portico/keepalive": _ANY,
    "portico/webrtc_offer": _ANY,
    "portico/webrtc_answer": _ANY,
    "portico/webrtc_ice": _ANY,
    # away media: the connection must be an enrolled phone (the door-level check stays in away_media)
    "portico/media_open": _PHONE,
    "portico/media_frame": _PHONE,
    "portico/media_close": _PHONE,
    # the connection log (DEV-705): an enrolled phone sends its reports; only an administrator reads or clears them
    "portico/connlog/report": _PHONE,
    "portico/connlog/get": _ADMIN,
    "portico/connlog/clear": _ADMIN,
    # a panel's heartbeat (DEV-807): an enrolled phone's, and only a panel's is kept (`not_a_panel` otherwise)
    "portico/wall/heartbeat": _PHONE,
    # a panel app's answer to the server's request to place the Everyone call (DEV-807 follow-up): an enrolled phone's, only a panel's is kept
    "portico/panel/call_result": _PHONE,
    # the push ring notification registry: an enrolled phone's own rows
    "portico/push/register": _PHONE,
    "portico/push/unregister": _PHONE,
    "portico/push/status": _PHONE,
    # content: an enrolled phone, a declared screen, or an administrator naming an endpoint; which endpoint the
    # caller may use is decided in signaling._content_target
    "portico/content/get": _ADMIN_PHONE_DASHBOARD,
    "portico/content/run": _ADMIN_PHONE_DASHBOARD,
    "portico/content/snapshot": _ADMIN_PHONE_DASHBOARD,
    # live video of a camera tile (live.py): snapshot's row and snapshot's grant. `live_ice` and `live_close` only touch a
    # session THIS connection opened, so they need no endpoint
    "portico/content/live": _ADMIN_PHONE_DASHBOARD,
    "portico/content/live_ice": _ADMIN_PHONE_DASHBOARD,
    "portico/content/live_close": _ADMIN_PHONE_DASHBOARD,
    # the editor (M6 E2): the working set, a draft resolved as the app would see it, and the one write are the
    # administrator's alone; `kiosk/hello` is open to any logged-in user because a kiosk page's user is by
    # definition not an administrator, and it is bounded in editor_commands.py (5.3).
    "portico/content/document": _ADMIN,
    "portico/content/preview": _ADMIN,
    "portico/content/save": _ADMIN,
    "portico/content/pages_migrate": _ADMIN,
    "portico/content/ring_pages_migrate": _ADMIN,
    "portico/kiosk/hello": _ANY,
    # the picker's spec chips: the picture size of camera entities, probed on request; the app never sends it
    "portico/cameras/info": _ADMIN,
    # DEV-824 P5: the editor banner's read of the file configuration (pending hand edits, last write, last reload); read-only
    "portico/config/status": _ADMIN,
    # the panel's live window asks Frigate's own go2rtc for the WebRTC answer (frigate_webrtc.py); the app never sends it
    "portico/camera/webrtc_offer": _ADMIN,
    # the Phones tab (UI split P2): list, draw a QR, cancel one. The panel is open to every household login, so this
    # class is the only access control; the QR itself is an admin-only HTTP view (enroll_view.py).
    "portico/phones/list": _ADMIN,
    "portico/phones/enroll": _ADMIN,
    "portico/phones/cancel": _ADMIN,
    # UI split P3: revoke a phone (needs `confirm: true` server-side) and change its doors / push ring notification / house calls
    "portico/phones/revoke": _ADMIN,
    "portico/phones/set": _ADMIN,
    # DEV-807 panel mode: DEPRECATED alias of phones/make_panel (kept one release; the slug is ignored)
    "portico/phones/make_panel": _ADMIN,
    "portico/phones/make_panel_screen": _ADMIN,
    # UI split P4 and P5: the Settings tab's doors. The status is a read; the settings change what the door does and may
    # reload it, so `set` also needs `confirm: true` server-side (door_commands.py). No peer token or other secret is ever in
    # a request or a reply.
    "portico/door_status": _ADMIN,
    "portico/door_settings/get": _ADMIN,
    "portico/door_settings/set": _ADMIN,
    # UI split P6: the Settings tab's push ring notification preferences (ring on/off and window, new phones, test phones)
    "portico/ui_settings/get": _ADMIN,
    "portico/ui_settings/set": _ADMIN,
    # the Elements page (DEV-760 E2): the store of named UI elements; administrators only, writes only .storage/portico.elements
    "portico/elements/list": _ADMIN,
    "portico/elements/set": _ADMIN,
    "portico/elements/delete": _ADMIN,
    # house calls (M7 3a): an enrolled phone, a declared screen, or an administrator may send them; they answer
    # `not_available` while house calls are off, and which call a caller may touch is decided in house.py (a user that is
    # not in a call learns nothing about it)
    "portico/house/answer": _ADMIN_PHONE_DASHBOARD,
    "portico/house/decline": _ADMIN_PHONE_DASHBOARD,
    "portico/house/hangup": _ADMIN_PHONE_DASHBOARD,
    "portico/house/presence": _ADMIN_PHONE_DASHBOARD,
    "portico/house/media_open": _ADMIN_PHONE_DASHBOARD,    # 3b-S: declares an end's audio leg; which call is house.py's
}

# Commands whose refusal keeps the code and text the v1 wire always sent.
REFUSALS: dict[str, tuple[str, str]] = {
    "portico/media_open": ("not_enrolled", "only an enrolled phone may open call audio"),
    "portico/push/register": ("unauthorized", "only an enrolled Portico phone may do this"),
    "portico/push/unregister": ("unauthorized", "only an enrolled Portico phone may do this"),
    "portico/push/status": ("unauthorized", "only an enrolled Portico phone may do this"),
    "portico/connlog/report": ("unauthorized", "only an enrolled Portico phone may do this"),
    "portico/wall/heartbeat": ("unauthorized", "only an enrolled Portico phone may do this"),
    "portico/content/get": ("unauthorized", "only an enrolled Portico phone, or an admin naming one, may do this"),
    "portico/content/run": ("unauthorized", "only an enrolled Portico phone, or an admin naming one, may do this"),
    "portico/content/snapshot": ("unauthorized", "only an enrolled Portico phone, or an admin naming one, may do this"),
    "portico/content/live": ("unauthorized", "only an enrolled Portico phone, or an admin naming one, may do this"),
    "portico/content/live_ice": ("unauthorized", "only an enrolled Portico phone, or an admin naming one, may do this"),
    "portico/content/live_close": ("unauthorized", "only an enrolled Portico phone, or an admin naming one, may do this"),
}


def is_dashboard_user(hass: HomeAssistant, user_id: str | None) -> bool:
    """Is this HA user declared for a dashboard endpoint (content store v3 `dashboards`)? False until the editor
    declares one, and while the store has not loaded (it is read from the file once, at startup or first use)."""
    from .endpoints import get_endpoint_registry

    return bool(get_endpoint_registry(hass).dashboards_for_user(user_id))


def caller_classes(hass: HomeAssistant, user) -> frozenset[str]:
    """The classes a connection's HA user belongs to (`any` for every authenticated user)."""
    if user is None:
        return frozenset()
    classes = {ANY}
    if user.is_admin:
        classes.add(ADMIN)
    if enrolled_phone_name(hass, user.id) is not None:
        classes.add(PHONE)
    if is_dashboard_user(hass, user.id):
        classes.add(DASHBOARD)
    return frozenset(classes)


def allowed(hass: HomeAssistant, user, command: str) -> bool:
    """Does the table let this user send `command`? An unlisted command is never allowed."""
    row = COMMANDS.get(command)
    if row is None:
        return False
    return bool(row & caller_classes(hass, user))


def require(connection: websocket_api.ActiveConnection, msg: dict, command: str) -> bool:
    """True when the connection may send `command`; otherwise the error is sent and False returned."""
    if allowed(connection.hass, connection.user, command):
        return True
    if command not in COMMANDS:
        _LOGGER.error("portico: %s is not in the authorization table (auth.COMMANDS); refusing it", command)
    code, text = REFUSALS.get(command, ("unauthorized", "Unauthorized"))
    connection.send_error(msg["id"], code, text)
    return False
