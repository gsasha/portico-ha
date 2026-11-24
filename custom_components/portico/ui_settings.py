"""UI split P6: the Settings tab's push ring notification preferences (design 5.2, "UI-side settings"). Two administrator-only websocket commands,
both `admin` in auth.COMMANDS:

  portico/ui_settings/get {}                                        -> the four settings, the phones a test list may name
  portico/ui_settings/set {patch, confirm?, validate_only?}         -> the same, after the change

These are the options of the settings (push) entry that the Home Assistant dialog's "Push ring notification" Configure menu edits
(push_flow.py `push_settings` and `push_test_phones`), written to the same keys the push manager already reads (the entry's
update listener applies them at once: no reload, no restart):

  ring_enabled         `enabled`             the whole push ring notification, on or off
  ring_window          `ring_window`         how long a visitor ring is sent to the phones, 10 to 120 seconds
  new_phones_enabled   `new_phones_enabled`  whether a phone's FIRST registration starts with its push ring notification on
  test_phones          `test_phones`         the staged-test allow-list: while it is not empty a REAL ring goes only to the
                                             listed phones that are also switched on
  live_rtsp_base       `live_rtsp_base`      (DEV-713 S1b-1) live video away from home: the address of the camera server's RTSP port,
                                             `rtsp://host[:port]` and nothing else (no credentials, path or query). Empty = off.
  live_away_streams    `live_away_streams`   (DEV-715) {camera entity id: {main, sub?}}: that camera's own full away RTSP addresses,
                                             any brand or naming, no credentials; wins over the Frigate convention; `{}` clears.
  live_sub_streams     `live_sub_streams`    the Frigate camera names that have a go2rtc `<name>_sub` stream; empty = none
                                             (live_rtsp.py says what the two do). These two are omitted from `get` while unset,
                                             and `""` / `[]` in a patch clears them; changing either announces a refetch.

NO HOUSE CALLS here (design 5.2): there is no global House calls switch any more (D-20261122-aafb). The call path, the doors and the gate are
not touched. Nothing is ever turned on by a default or a migration: a value changes only when an administrator sends it.

`set` needs `confirm: true`, checked HERE whatever the panel did, for every key except `ring_window` (a number between two limits
cannot leave anyone un-rung; the two live-video keys DO need it: the RTSP port is outside Portico's per-camera grant).
`validate_only` runs the checks and writes nothing. The reply never carries a secret and nothing is logged but the key names.
A `test_phones` entry that is not a registered phone is `unknown_phone` and nothing is written (the stored
list is kept as is, even with a phone that has since gone: a staged test fails closed, never open to the household).
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from . import auth, live_rtsp
from .content import get_content_store
from .phones_commands import push_entry
from .push import const as push_const
from .push import get_manager

_LOGGER = logging.getLogger(__name__)

KEYS = ("ring_enabled", "ring_window", "new_phones_enabled", "test_phones", "live_rtsp_base", "live_sub_streams", "live_away_streams")
LIVE_KEYS = frozenset({"live_rtsp_base", "live_sub_streams", "live_away_streams"})
NO_CONFIRM_KEYS = frozenset({"ring_window"})
MAX_TEST_PHONES = 50

_MESSAGES = {
    "confirmation_required": "This changes who the push ring notification reaches and must be confirmed",
    "no_push_entry": "Portico's settings entry is missing, so these settings have nowhere to be kept",
    "bad_patch": "Nothing to change, or a setting that cannot be changed from here, or a value of the wrong kind",
    "push_not_loaded": "The push ring notification is not running, so the phones it knows cannot be listed; fix its credentials first",
    "unknown_phone": "One of those is not a phone registered for the push ring notification",
    "unknown": "Something went wrong; see the Home Assistant log",
}


def _send_error(connection: websocket_api.ActiveConnection, msg_id: int, code: str, field: str | None = None) -> None:
    frame = websocket_api.error_message(msg_id, code, _MESSAGES.get(code, code))
    if field:
        frame["error"]["field"] = field
    connection.send_message(frame)


def facts(hass: HomeAssistant) -> dict[str, Any]:
    entry = push_entry(hass)
    manager = get_manager(hass)
    options = entry.options if entry is not None else {}
    rows = manager.registry.rows() if manager is not None else []
    out = {
        "push_entry": entry is not None,
        "running": manager is not None,
        "ring_enabled": bool(options.get(push_const.CONF_ENABLED, True)),
        "ring_window": int(options.get(push_const.CONF_RING_WINDOW, push_const.DEFAULT_RING_WINDOW)),
        "ring_window_min": push_const.MIN_RING_WINDOW,
        "ring_window_max": push_const.MAX_RING_WINDOW,
        "new_phones_enabled": push_const.new_phones_enabled(options),
        "test_phones": list(manager.test_allow_list) if manager is not None else
                       [str(r) for r in (options.get(push_const.CONF_TEST_PHONES) or [])],
        "phones": [{"reg_id": str(row["reg_id"]), "name": str(row.get("name") or "phone"),
                    "away_ring": bool(row.get("push_enabled"))} for row in rows],
    }
    base, subs = live_rtsp.read_settings(options)
    if base:
        out["live_rtsp_base"] = base            # omitted while unset: a reply from before these options is unchanged
    if subs:
        out["live_sub_streams"] = subs
    away = live_rtsp.read_away_streams(options)
    if away:
        out["live_away_streams"] = away         # DEV-715: per-camera explicit away addresses, omitted while none
    return out


def parse_patch(patch: Any) -> dict[str, Any] | None:
    """The patch as a clean dict, or None when it is empty, names a key this tab does not own, or has the wrong kind of value."""
    if not isinstance(patch, dict) or not patch:
        return None
    clean: dict[str, Any] = {}
    for key, value in patch.items():
        if key in ("ring_enabled", "new_phones_enabled"):
            if value is not True and value is not False:
                return None
        elif key == "ring_window":
            if type(value) is not int or not push_const.MIN_RING_WINDOW <= value <= push_const.MAX_RING_WINDOW:
                return None
        elif key == "test_phones":
            if not isinstance(value, list) or len(value) > MAX_TEST_PHONES or not all(isinstance(v, str) and v for v in value):
                return None
            value = list(dict.fromkeys(value))
        elif key == "live_rtsp_base":
            value = live_rtsp.normalize_base(value)         # "" clears it; None is anything not `rtsp://host[:port]`
            if value is None:
                return None
        elif key == "live_sub_streams":
            value = live_rtsp.normalize_sub_streams(value)
            if value is None:
                return None
        elif key == "live_away_streams":
            value = live_rtsp.normalize_away_streams(value)  # {} clears; None is anything not camera id -> main[, sub] rtsp URLs
            if value is None:
                return None
        else:
            return None
        clean[key] = value
    return clean


_OPTION = {"ring_enabled": push_const.CONF_ENABLED, "ring_window": push_const.CONF_RING_WINDOW,
           "new_phones_enabled": push_const.CONF_NEW_PHONES_ENABLED, "test_phones": push_const.CONF_TEST_PHONES,
           "live_rtsp_base": live_rtsp.CONF_LIVE_RTSP_BASE, "live_sub_streams": live_rtsp.CONF_LIVE_SUB_STREAMS,
           "live_away_streams": live_rtsp.CONF_LIVE_AWAY_STREAMS}
_UNSET = {"live_rtsp_base": "", "live_sub_streams": [], "live_away_streams": {}}      # what an omitted live key reads as


def changed_keys(hass: HomeAssistant, patch: dict[str, Any]) -> list[str]:
    """The keys of a clean `patch` whose value differs from what is set now."""
    before = facts(hass)
    return sorted(key for key, value in patch.items() if before.get(key, _UNSET.get(key)) != value)


def write_settings(hass: HomeAssistant, entry: Any, patch: dict[str, Any], changes: list[str]) -> None:
    """Write `changes` of a clean `patch` to the settings entry's options (synchronous; no reload) and announce a live-video change.
    The one writer of these options, shared by `portico/ui_settings/set` and the YAML apply (DEV-824 P2)."""
    options = dict(entry.options)
    for key in changes:
        if key in _UNSET and patch[key] == _UNSET[key]:
            options.pop(_OPTION[key], None)             # cleared: back to no key at all, as before the option existed
        else:
            options[_OPTION[key]] = patch[key]
    hass.config_entries.async_update_entry(entry, options=options)
    if LIVE_KEYS.intersection(changes):
        get_content_store(hass).announce_all()          # `live_rtsp` is in every resolved document's `rev`


@websocket_api.websocket_command({vol.Required("type"): "portico/ui_settings/get"})
@callback
def handle_ui_settings_get(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """`{}` -> the facts. A read: nothing is written."""
    if not auth.require(connection, msg, "portico/ui_settings/get"):
        return
    try:
        reply = facts(hass)
    except Exception as err:  # noqa: BLE001 - never a bare 500, and never the exception's text
        _LOGGER.warning("Portico: ui_settings/get failed (%s)", type(err).__name__)
        _send_error(connection, msg["id"], "unknown")
        return
    connection.send_result(msg["id"], reply)


@websocket_api.websocket_command({
    vol.Required("type"): "portico/ui_settings/set",
    vol.Required("patch"): dict,
    vol.Optional("confirm", default=False): bool,
    vol.Optional("validate_only", default=False): bool,
})
@callback
def handle_ui_settings_set(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Change the push ring notification settings, now. Replies `{result: "valid" | "unchanged" | "applied", changes, settings}`."""
    if not auth.require(connection, msg, "portico/ui_settings/set"):
        return
    msg_id = msg["id"]
    patch = parse_patch(msg["patch"])
    if patch is None:
        _send_error(connection, msg_id, "bad_patch")
        return
    entry = push_entry(hass)
    if entry is None:
        _send_error(connection, msg_id, "no_push_entry")
        return
    try:
        manager = get_manager(hass)
        if "test_phones" in patch:
            if manager is None:
                _send_error(connection, msg_id, "push_not_loaded", "test_phones")
                return
            if any(manager.registry.get(reg_id) is None for reg_id in patch["test_phones"]):
                _send_error(connection, msg_id, "unknown_phone", "test_phones")
                return
        before = facts(hass)
        changes = sorted(key for key, value in patch.items() if before.get(key, _UNSET.get(key)) != value)
        if msg["validate_only"]:
            connection.send_result(msg_id, {"result": "valid", "changes": changes, "settings": before})
            return
        if not changes:
            connection.send_result(msg_id, {"result": "unchanged", "changes": [], "settings": before})
            return
        if any(key not in NO_CONFIRM_KEYS for key in changes) and msg["confirm"] is not True:
            _send_error(connection, msg_id, "confirmation_required")
            return
        write_settings(hass, entry, patch, changes)
        reply = {"result": "applied", "changes": changes, "settings": facts(hass)}
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Portico: ui_settings/set failed (%s)", type(err).__name__)
        _send_error(connection, msg_id, "unknown")
        return
    _LOGGER.info("Portico: an administrator changed the push ring notification settings: %s", ", ".join(changes))
    from . import config_sync          # noqa: PLC0415 - config_sync reads these settings (import cycle)
    config_sync.async_schedule(hass)   # DEV-824 P3: the files follow the save; never fails or delays it
    connection.send_result(msg_id, reply)


@callback
def async_setup_ui_settings_commands(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, handle_ui_settings_get)
    websocket_api.async_register_command(hass, handle_ui_settings_set)
