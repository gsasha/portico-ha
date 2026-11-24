"""UI split P2: the Phones tab's websocket commands (design 4.2). All three are `admin` in auth.COMMANDS and call
`auth.require` first; the panel is open to every household login, so the class is the only access control.

  portico/phones/list                                  the header and the rows (a read: nothing is written, nothing adopted)
  portico/phones/enroll {name, allow_http, ha_dial, confirmed_entry_id?}
                                                       draw a one-time QR (the enrolment core); the reply carries the
                                                       QR's token and size, never the QR (enroll_view.py serves it)
  portico/phones/cancel {entry_id}                     kill a QR this server drew; remove the phone only if that
                                                       enrolment created it and it is still waiting for its first scan
  portico/phones/revoke {entry_id, confirm}            UI split P3: remove a phone (tokens, its HA user when Portico made
                                                       it, push registration, content override, pending code). `confirm`
                                                       must be true: the server refuses without it, whatever the panel did
  portico/phones/set {entry_id, patch}                 UI split P3: partial patch of {doors?, away_ring?, house_receive?}

Rows are keyed by the phone's config entry id (a legacy `enrolled_phones` row by a hash of its own key), NEVER by the
`enroll_id`: that is a credential for offline registration. Timestamps are ISO-8601 UTC or null.

Errors: `unauthorized` (the table), `needs_tailscale_setup`, `needs_approval`, `no_ha_url`, `no_https_url`,
`invalid_ha_dial` (the frame's error carries `field: "ha_dial"`), `tailscale_mint_failed`, `no_push_entry`, `bad_name`
(`field: "phone_name"`), `pending_cap`, `unknown_phone`, and `unknown` for any unexpected exception (never a bare 500).
P3 adds `confirmation_required` (revoke), `bad_patch`, `unknown_door` (`field: "doors"`), `legacy_phone` (a legacy row has
no options to hold doors or house calls) and `no_registration` (the push ring notification needs the app to have registered).
`needs_confirm` is not an error: it is a result that carries the existing phone.

Nothing here logs a request or a reply body.
"""
from __future__ import annotations

import hashlib
import logging
import math
from datetime import datetime
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util

from . import auth, enroll_redeem, enrollment_core, enrollment_flow, phone_entry
from .provider import DATA_PROVIDERS
from .enroll_qr import get_qr_store
from .enrollment_core import (
    NAME_MAX,
    PENDING_CAP,
    Created,
    Error,
    NeedsApproval,
    NeedsConfirmReenroll,
    NeedsTailscaleSetup,
)
from .phone_entry import CONF_DOORS, CONF_HOUSE_RECEIVE, STATE_ENROLLED, STATE_PENDING, PhoneRecord
from .push import const as push_const
from .push import get_manager

_LOGGER = logging.getLogger(__name__)

# The pixel size of the panel's PNG: one pixel per module, with the 4-module quiet zone (the panel scales it).
PANEL_QR_BOX = 1

_ENTRY_ID = vol.All(str, vol.Length(min=1, max=128))

# The codes a client may see (design 4.2). Anything else the core says is `unknown`.
_CODES = frozenset({
    "needs_tailscale_setup", "needs_approval", "no_ha_url", "no_https_url", "invalid_ha_dial", "tailscale_mint_failed",
    "no_push_entry", "bad_name", "pending_cap", "unknown_phone", "unknown",
    "confirmation_required", "bad_patch", "unknown_door", "legacy_phone", "no_registration",
    "already_panel", "bad_slug", "not_portico_user", "slug_taken", "declare_refused", "not_a_panel", "no_phone",
})

_MESSAGES = {
    "needs_tailscale_setup": "Tailscale is not set up for enrolment: finish it in the Home Assistant dialog",
    "needs_approval": "This install enrols in approve mode, which is done in the Home Assistant dialog",
    "no_ha_url": "Home Assistant has no external or internal URL set",
    "no_https_url": "Home Assistant has only a plain http URL and plain http was not allowed",
    "invalid_ha_dial": "ha_dial must be a private IP address and a port",
    "tailscale_mint_failed": "Tailscale did not give a key for this phone",
    "no_push_entry": "Portico's settings entry is missing",
    "bad_name": f"The phone's name must be 1 to {NAME_MAX} characters",
    "pending_cap": f"{PENDING_CAP} phones are already waiting to be scanned",
    "unknown_phone": "No phone is known under that id",
    "unknown": "Something went wrong; see the Home Assistant log",
    "confirmation_required": "Revoking a phone must be confirmed",
    "bad_patch": "Nothing to change, or a value of the wrong kind",
    "unknown_door": "One of those doors is not a door Portico has now",
    "legacy_phone": "This phone was enrolled before phone entries: only its push ring notification can be changed here",
    "no_registration": "The phone's app has not registered for the push ring notification yet",
    "already_panel": "This phone is already a panel",
    "bad_slug": "The slug must be 1-32 characters of a-z, 0-9, _ or -",
    "not_portico_user": "Only a phone whose Home Assistant user Portico made can be a panel",
    "slug_taken": "That slug is already a screen, a phone or a door",
    "declare_refused": "The screen could not be declared; the phone was left as it was",
}


def _send_error(connection: websocket_api.ActiveConnection, msg_id: int, code: str, field: str | None = None,
                message: str | None = None) -> None:
    frame = websocket_api.error_message(msg_id, code, message or _MESSAGES.get(code, code))
    if field:
        frame["error"]["field"] = field
    connection.send_message(frame)


def push_entry(hass: HomeAssistant):
    """The install-wide settings (push) entry, or None."""
    for entry in hass.config_entries.async_entries(phone_entry.DOMAIN):
        if entry.data.get(push_const.ENTRY_TYPE) == push_const.ENTRY_TYPE_PUSH:
            return entry
    return None


def _iso(value: datetime | str | None) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return dt_util.as_utc(value).isoformat()
    return str(value)


def _row_id(entry, phone: dict[str, Any]) -> str:
    """A phone entry's id, or for a legacy `enrolled_phones` row a hash of its own key (never the raw `enroll_id`)."""
    if phone_entry.is_phone_entry(entry):
        return entry.entry_id
    key = str(phone.get("enroll_id") or phone.get("ha_user_id") or phone.get("name") or "")
    return "legacy-" + hashlib.sha256(key.encode("utf-8", "replace")).hexdigest()[:16]


def _row(hass: HomeAssistant, entry, phone: dict[str, Any]) -> dict[str, Any]:
    try:
        record: PhoneRecord | None = PhoneRecord.from_data(phone)
    except ValueError:
        record = None                      # a legacy row with no HA user: still listed, as it is read everywhere
    enroll_id = phone.get("enroll_id") if isinstance(phone.get("enroll_id"), str) else None
    state = record.state if record else STATE_ENROLLED
    manager = get_manager(hass)
    regs = None
    if manager is not None and record is not None:
        regs = manager.registry.rows_for_phone(enroll_id, record.ha_user_id)
    seen = [r.get("last_seen_at") for r in (regs or []) if r.get("last_seen_at")]
    left = enroll_redeem.get_registry(hass).seconds_left(enroll_id) if enroll_id else None
    pending = state == STATE_PENDING
    return {
        "entry_id": _row_id(entry, phone),
        "legacy": not phone_entry.is_phone_entry(entry),
        "name": str(phone.get("name") or "phone"),
        "state": state,
        "expired": bool(pending and left is None),
        "code_expires_in": math.ceil(left) if (pending and left is not None) else None,
        "enrolled_at": record.enrolled_at or None if record else (phone.get("enrolled_at") or None),
        "last_connection": max(seen) if seen else None,
        "push_registered": None if regs is None else bool(regs),
        "doors": phone.get(phone_entry.CONF_DOORS),
        "away_ring": None if not regs else any(bool(r.get("push_enabled")) for r in regs),
        "house_receive": bool(phone_entry.is_phone_entry(entry) and entry.options.get(CONF_HOUSE_RECEIVE) is True),
        **_panel_fields(entry),
    }


def _panel_fields(entry) -> dict[str, Any]:
    """`panel: true` for a panel's row only (DEV-807); nothing for any other row, so the rows of a house with no panel are as they
    were. A legacy `panel_screen` option on the entry is ignored (not sent)."""
    if phone_entry.is_phone_entry(entry) and entry.options.get(phone_entry.CONF_PANEL) is True:
        return {"panel": True}
    return {}


def _live_pending_count(hass: HomeAssistant) -> int:
    return enrollment_core._live_pending(hass, exclude=None)


async def _header(hass: HomeAssistant) -> dict[str, Any]:
    entry = push_entry(hass)
    mode = None
    complete = False
    ha_dial = None
    if entry is not None:
        data = entry.data
        mode = data.get("tailscale_mode")
        if mode is None and "tailscale_api_key" in data:
            mode = "auto"            # an entry from before the option: the enrolment core backfills this on its first use
        if mode is not None:
            complete = await enrollment_flow._async_tailscale_answer_complete(hass, entry, mode)
        typed = data.get("ha_dial")
        ha_dial = typed if isinstance(typed, str) and typed else None
    pending = _live_pending_count(hass)
    return {
        "push_entry": entry is not None,
        "tailscale_mode": mode,
        "tailscale_complete": complete,
        # What the Enrol page can do by itself: `manual`, or `auto` whose credentials exist. `approve` needs a browser
        # approval per phone and anything else needs the dialog (design F6).
        "enrol_here": entry is not None and complete and mode in ("manual", "auto"),
        "ha_dial": ha_dial,
        "pending": pending,
        "pending_cap": PENDING_CAP,
        "cap_reached": pending >= PENDING_CAP,
    }


@websocket_api.websocket_command({vol.Required("type"): "portico/phones/list"})
@websocket_api.async_response
async def handle_phones_list(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """`{}` -> `{header, phones: [row]}`. A read: legacy rows are listed where they are, not adopted."""
    if not auth.require(connection, msg, "portico/phones/list"):
        return
    try:
        header = await _header(hass)
        rows = [_row(hass, entry, phone) for entry, phone in phone_entry.phone_rows(hass)]
    except Exception as err:  # noqa: BLE001 - never a bare 500
        _LOGGER.warning("Portico: phones/list failed (%s)", type(err).__name__)
        _send_error(connection, msg["id"], "unknown")
        return
    connection.send_result(msg["id"], {"header": header, "phones": rows})


@websocket_api.websocket_command({
    vol.Required("type"): "portico/phones/enroll",
    vol.Required("name"): vol.All(str, vol.Length(max=200)),
    vol.Required("allow_http"): bool,
    vol.Required("ha_dial"): vol.All(str, vol.Length(max=200)),
    vol.Optional("confirmed_entry_id"): _ENTRY_ID,
})
@websocket_api.async_response
async def handle_phones_enroll(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Draw a one-time QR for the phone `name` through the enrolment core. -> `{result: "created", entry_id, qr_token,
    modules, expires_in, expires_at, notes, dial_target, created}`, `{result: "needs_confirm", entry_id, name}` (an
    enrolled phone of that name exists: send again with `confirmed_entry_id`), or a typed error."""
    if not auth.require(connection, msg, "portico/phones/enroll"):
        return
    try:
        result = await enrollment_core.create_enrollment(
            hass, push_entry(hass),
            name=msg["name"], allow_http=msg["allow_http"], ha_dial_raw=msg["ha_dial"],
            confirmed_entry_id=msg.get("confirmed_entry_id"), qr_box_size=PANEL_QR_BOX,
        )
        reply = _reply(hass, result)
    except Exception as err:  # noqa: BLE001 - never a bare 500, and never the exception's text (it may hold a key)
        _LOGGER.warning("Portico: phones/enroll failed (%s)", type(err).__name__)
        _send_error(connection, msg["id"], "unknown")
        return
    if isinstance(reply, tuple):
        _send_error(connection, msg["id"], *reply)
        return
    connection.send_result(msg["id"], reply)


def _reply(hass: HomeAssistant, result) -> dict[str, Any] | tuple[str, str | None]:
    """The reply for a core result, or `(code, field)` for an error."""
    if isinstance(result, Created):
        record = hass.config_entries.async_get_entry(result.entry_id)
        enroll_id = record.data.get("enroll_id") if record is not None else None
        if not enroll_id:
            return ("unknown", None)
        token = get_qr_store(hass).put(
            hass, entry_id=result.entry_id, enroll_id=enroll_id, png=result.qr_png,
            text=result.qr_text, created=result.created)
        return {
            "result": "created",
            "entry_id": result.entry_id,
            "qr_token": token,
            "modules": result.modules,
            "expires_in": result.expires_in,
            "expires_at": _iso(result.expires_at),
            "notes": list(result.notes),
            "dial_target": result.dial_target,
            "created": result.created,
        }
    if isinstance(result, NeedsConfirmReenroll):
        return {"result": "needs_confirm", "entry_id": result.entry_id, "name": result.name}
    if isinstance(result, NeedsTailscaleSetup):
        return ("needs_tailscale_setup", None)
    if isinstance(result, NeedsApproval):
        return ("needs_approval", None)
    if isinstance(result, Error):
        if result.code == "unknown" and result.exception is not None:
            _LOGGER.warning("Portico: enrolling failed (%s)", type(result.exception).__name__)
        return (result.code if result.code in _CODES else "unknown", result.field)
    return ("unknown", None)


@websocket_api.websocket_command({
    vol.Required("type"): "portico/phones/cancel",
    vol.Required("entry_id"): _ENTRY_ID,
})
@websocket_api.async_response
async def handle_phones_cancel(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Kill the QR this server drew for `entry_id`. The phone's entry is removed too ONLY when this server remembers
    that this enrolment created it and it is still `pending` with a live code; a phone that already enrolled, or one
    this server did not just create, is never removed here. -> `{result: "cancelled", removed}`, or `unknown_phone` when
    this server holds no live QR for that entry (nothing is changed then)."""
    if not auth.require(connection, msg, "portico/phones/cancel"):
        return
    entry_id = msg["entry_id"]
    try:
        # Under the enrolment lock: a cancel must not interleave with the draw it is cancelling.
        async with enrollment_core._lock(hass):
            outcome = await _cancel_locked(hass, entry_id)
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Portico: phones/cancel failed (%s)", type(err).__name__)
        _send_error(connection, msg["id"], "unknown")
        return
    if outcome is None:
        _send_error(connection, msg["id"], "unknown_phone", message="No QR for that phone is waiting to be scanned")
        return
    connection.send_result(msg["id"], {"result": "cancelled", "removed": outcome})


async def _cancel_locked(hass: HomeAssistant, entry_id: str) -> bool | None:
    """True: the code is dead and the entry removed. False: the code is dead, the entry stays. None: nothing to do."""
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or not phone_entry.is_phone_entry(entry):
        return None
    store = get_qr_store(hass)
    remembered = store.record_for_entry(entry_id)
    registry = enroll_redeem.get_registry(hass)
    record = PhoneRecord.from_data(entry.data)
    if remembered is None or not record.enroll_id or registry.seconds_left(record.enroll_id) is None:
        return None
    # Kill the code first, in this synchronous stretch: from here the phone cannot redeem it, so the entry below is
    # still a phone that never enrolled when it goes.
    registry.discard(record.enroll_id)
    store.forget(entry_id)
    phone_entry.dismiss_enroll_notification(hass, entry_id)
    if remembered.created and record.state == STATE_PENDING:
        await hass.config_entries.async_remove(entry_id)
        return True
    return False


# ---- UI split P3: revoke and set ------------------------------------------------------------------------------------

_DOOR_ID = vol.All(str, vol.Length(min=1, max=256))


def _find_row(hass: HomeAssistant, row_id: str):
    """(entry, phone) for the row whose public id is `row_id`: a phone entry by its entry id, a legacy row by the hash of
    its own key. None when there is none. Looks only at what `phone_rows` yields, so nothing else can be named."""
    for entry, phone in phone_entry.phone_rows(hass):
        if _row_id(entry, phone) == row_id:
            return entry, phone
    return None


def _current_door_ids(hass: HomeAssistant) -> set[str]:
    """Every door Portico has now, of any class (what `portico/doors` lists)."""
    registry = hass.data.get(phone_entry.DOMAIN, {}).get(DATA_PROVIDERS)
    return {door.id for door in (registry.doors() if registry is not None else [])}


@websocket_api.websocket_command({
    vol.Required("type"): "portico/phones/revoke",
    vol.Required("entry_id"): _ENTRY_ID,
    vol.Required("confirm"): bool,
})
@websocket_api.async_response
async def handle_phones_revoke(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Revoke one phone for good: its HA tokens, its HA user (only a user Portico made, never an administrator or the
    owner: `phone_entry._made_by_portico`), its push registration, its content override and any pending code. The
    confirmation is checked HERE: `confirm` must be exactly true, so a script or a stale tab cannot revoke by accident.
    -> `{result: "revoked", legacy}` or `confirmation_required` / `unknown_phone`."""
    if not auth.require(connection, msg, "portico/phones/revoke"):
        return
    if msg["confirm"] is not True:
        _send_error(connection, msg["id"], "confirmation_required")
        return
    try:
        async with enrollment_core._lock(hass):          # not between an enrolment's checks and its writes
            outcome = await _revoke_locked(hass, msg["entry_id"])
    except Exception as err:  # noqa: BLE001 - never a bare 500, and never the exception's text
        _LOGGER.warning("Portico: phones/revoke failed (%s)", type(err).__name__)
        _send_error(connection, msg["id"], "unknown")
        return
    if outcome is None:
        _send_error(connection, msg["id"], "unknown_phone")
        return
    connection.send_result(msg["id"], {"result": "revoked", "legacy": outcome})


async def _revoke_locked(hass: HomeAssistant, row_id: str) -> bool | None:
    """True: a legacy row was revoked. False: a phone entry was. None: no such phone (nothing changed)."""
    found = _find_row(hass, row_id)
    if found is None:
        return None
    entry, phone = found
    if phone_entry.is_phone_entry(entry):
        get_qr_store(hass).forget(entry.entry_id)
        # The entry's removal hook runs `async_revoke_phone` for exactly the HA user named in THIS entry's data.
        await hass.config_entries.async_remove(entry.entry_id)
        return False
    # A legacy row lives on the settings (or an old door) entry: take it off first, so it is never listed again, then
    # revoke what it held through the same steps as an entry's removal.
    phone_entry.drop_legacy_row(hass, entry, phone)
    try:
        record = PhoneRecord.from_data(phone)
    except ValueError:
        # No HA user in the row: nothing to delete; drop only what its enroll id (if any) registered.
        enroll_id = phone.get("enroll_id")
        manager = get_manager(hass)
        if isinstance(enroll_id, str) and enroll_id and manager is not None:
            await manager.registry.async_remove_phone(enroll_id, "")
        return True
    await phone_entry.async_revoke_record(hass, record, None)
    return True


_PATCH = vol.Schema({
    vol.Optional(CONF_DOORS): vol.Any(None, vol.All([_DOOR_ID], vol.Length(max=64))),
    vol.Optional("away_ring"): bool,
    vol.Optional("house_receive"): bool,
}, extra=vol.PREVENT_EXTRA)


@websocket_api.websocket_command({
    vol.Required("type"): "portico/phones/set",
    vol.Required("entry_id"): _ENTRY_ID,
    vol.Required("patch"): dict,
})
@websocket_api.async_response
async def handle_phones_set(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Change a phone's settings: `doors` (null = every door, or a list, each id one Portico has now), `away_ring` (on
    or off for every registration the phone has) and `house_receive`. Everything is checked before anything is written.
    -> `{result: "updated", phone: <the row as `list` sends it>}`."""
    if not auth.require(connection, msg, "portico/phones/set"):
        return
    try:
        patch = _PATCH(msg["patch"])
    except vol.Invalid:
        patch = None
    if not patch:
        _send_error(connection, msg["id"], "bad_patch")
        return
    try:
        async with enrollment_core._lock(hass):
            reply = await _set_locked(hass, msg["entry_id"], patch)
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Portico: phones/set failed (%s)", type(err).__name__)
        _send_error(connection, msg["id"], "unknown")
        return
    if isinstance(reply, tuple):
        _send_error(connection, msg["id"], *reply)
        return
    connection.send_result(msg["id"], reply)


async def _set_locked(hass: HomeAssistant, row_id: str, patch: dict[str, Any]):
    """The reply, or `(code, field)`. Validates the whole patch first; then writes the options in one update, then the
    push ring notification."""
    found = _find_row(hass, row_id)
    if found is None:
        return ("unknown_phone", None)
    entry, phone = found
    is_entry = phone_entry.is_phone_entry(entry)
    if not is_entry and (CONF_DOORS in patch or "house_receive" in patch):
        return ("legacy_phone", None)
    doors = None
    if patch.get(CONF_DOORS) is not None:
        doors = list(dict.fromkeys(patch[CONF_DOORS]))           # in the order given, once each (as phone_flow writes it)
        known = _current_door_ids(hass)
        if any(door not in known for door in doors):
            return ("unknown_door", CONF_DOORS)
    regs: list[dict[str, Any]] = []
    manager = None
    if "away_ring" in patch:
        try:
            record = PhoneRecord.from_data(phone)
        except ValueError:
            return ("no_registration", None)
        manager = get_manager(hass)
        enroll_id = phone.get("enroll_id") if isinstance(phone.get("enroll_id"), str) else None
        regs = manager.registry.rows_for_phone(enroll_id, record.ha_user_id) if manager is not None else []
        if not regs:
            return ("no_registration", None)
    if is_entry and (CONF_DOORS in patch or "house_receive" in patch):
        options = dict(entry.options)
        if CONF_DOORS in patch:
            options[CONF_DOORS] = doors
        if "house_receive" in patch:
            options[CONF_HOUSE_RECEIVE] = bool(patch["house_receive"])
        hass.config_entries.async_update_entry(entry, options=options)
        if "house_receive" in patch:
            phone_entry._house_reconcile(hass)
    for reg in regs:
        await manager.registry.async_set_enabled(reg["reg_id"], bool(patch["away_ring"]))
    fresh = _find_row(hass, row_id)
    if fresh is None:
        return ("unknown", None)
    return {"result": "updated", "phone": _row(hass, *fresh)}


async def _make_panel_reply(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict, command: str) -> None:
    """The body of `phones/make_panel` and of its deprecated alias: make the phone a panel (make_panel.py), reply with its row."""
    if not auth.require(connection, msg, command):
        return
    from .make_panel import PanelRefused, async_make_panel

    try:
        async with enrollment_core._lock(hass):
            found = _find_row(hass, msg["entry_id"])
            if found is None:
                _send_error(connection, msg["id"], "unknown_phone")
                return
            await async_make_panel(hass, found[0])
            fresh = _find_row(hass, msg["entry_id"])
            reply = {"result": "updated", "phone": _row(hass, *fresh)} if fresh is not None else None
    except PanelRefused as err:
        _send_error(connection, msg["id"], err.code if err.code in _CODES else "unknown", err.field, str(err))
        return
    except Exception as err:  # noqa: BLE001 - never a bare 500, and never the exception's text
        _LOGGER.warning("Portico: %s failed (%s)", command, type(err).__name__)
        _send_error(connection, msg["id"], "unknown")
        return
    if reply is None:
        _send_error(connection, msg["id"], "unknown_phone")
        return
    connection.send_result(msg["id"], reply)


@websocket_api.websocket_command({
    vol.Required("type"): "portico/phones/make_panel",
    vol.Required("entry_id"): _ENTRY_ID,
})
@websocket_api.async_response
async def handle_phones_make_panel(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """DEV-807 (own pages): make an enrolled phone a panel that owns its pages (the Devices tab's "Make a panel"): `panel: true`, no
    door list, the readiness sensor; no screen is declared and there is no slug.
    -> `{result: "updated", phone: <the row as `list` sends it>}`, or `unknown_phone`, `legacy_phone`, `already_panel`,
    `not_portico_user`."""
    await _make_panel_reply(hass, connection, msg, "portico/phones/make_panel")


@websocket_api.websocket_command({
    vol.Required("type"): "portico/phones/make_panel_screen",
    vol.Required("entry_id"): _ENTRY_ID,
    vol.Optional("slug"): vol.All(str, vol.Length(min=1, max=64)),     # ignored: kept so an older editor's command still works
})
@websocket_api.async_response
async def handle_phones_make_panel_screen(hass: HomeAssistant, connection: websocket_api.ActiveConnection,
                                          msg: dict) -> None:
    """DEPRECATED alias (kept one release) of `portico/phones/make_panel`: the old "make a panel screen" command. `slug` is
    ignored and no screen is declared."""
    await _make_panel_reply(hass, connection, msg, "portico/phones/make_panel_screen")


@callback
def async_setup_phones_commands(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, handle_phones_list)
    websocket_api.async_register_command(hass, handle_phones_enroll)
    websocket_api.async_register_command(hass, handle_phones_cancel)
    websocket_api.async_register_command(hass, handle_phones_revoke)
    websocket_api.async_register_command(hass, handle_phones_set)
    websocket_api.async_register_command(hass, handle_phones_make_panel)
    websocket_api.async_register_command(hass, handle_phones_make_panel_screen)

