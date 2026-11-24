"""UI split P4 and P5: the Settings tab's door commands. All three are `admin` in auth.COMMANDS and call `auth.require` first;
the panel is open to every household login, so the class is the only access control.

  portico/door_status {}                       a read-only card per door: what the door and the hub report (P4)
  portico/door_settings/get {}                 per door: a `revision` and the choices (P5)
  portico/door_settings/set {entry_id, revision, patch, confirm?, validate_only?}
                                               kept for the wire contract; changes nothing (see below)

Since the unification (U1a) the door side is part of this integration, so these are ordinary `portico/*` commands: no
second integration registers a websocket command, no ban was lifted, and the door entries are read through the same
entries and runtimes the entities use.

WHAT THE PANEL MAY CHANGE: nothing now. The settings this tab changed (the gate observer's listen leg, "Answer from Portico",
"Talk to the gate", the ring-answered helper and `house_calls`) belonged to the gate_sip_observer door, which was retired
(DEV-518 C5). A gate_sip_server door keeps its own settings in Home Assistant's Reconfigure dialog
(`door/gate_sip_listen.py`). So a patch is `bad_patch` when it is empty, has an unknown key or a value of the wrong kind;
`unchanged` when it holds only the deprecated `away_media` (accepted and ignored, as the wire contract has it); and
`not_for_this_driver` for any former observer key. Every `door_settings/get` and `door_status` field the panel read is still
published, with the observer's parts null, so an older panel keeps working until the panel drops them.

NEVER over a websocket: any password, key or URL. Nothing here logs a request or a reply body.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util

from . import auth
from .door import gate_sip_listen
from .door.const import DOMAIN, is_door_entry
from .door.hub import CONF_HOUSE_CALLS, get_hub

_LOGGER = logging.getLogger(__name__)

_ENTRY_ID = vol.All(str, vol.Length(max=128), vol.Length(min=1))

# The patch keys the panel used to send for the gate observer. They are still recognised so a stale panel is told
# `not_for_this_driver` and not `bad_patch`; none of them writes anything any more.
FORMER_OBSERVER_KEYS = ("listen_enabled", "audio_port", "claims_enabled", "ring_answered_entity", "dial_enabled", CONF_HOUSE_CALLS)
# `door_settings/get`'s reply field, recorded in contract/ws_v3.json: every key is a reload, `away_media` is live.
EFFECTS = {**{key: "reload" for key in FORMER_OBSERVER_KEYS if key != CONF_HOUSE_CALLS}, "away_media": "live"}

_HELPER = re.compile(r"^input_boolean\.[a-z0-9_]{1,200}$")
_REASON_MAX = 160
_USERINFO = re.compile(r"(?<=//)[^/\s@]*@")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

_MESSAGES = {
    "unknown_door": "That is not a door Portico has",
    "bad_patch": "Nothing to change, or a setting that cannot be changed from here, or a value of the wrong kind",
    "not_for_this_driver": "This door has no settings that can be changed from here",
    "stale_revision": "The door's settings changed since this page read them; reload the page",
    "unknown": "Something went wrong; see the Home Assistant log",
}


def _send_error(connection: websocket_api.ActiveConnection, msg_id: int, code: str, field: str | None = None) -> None:
    frame = websocket_api.error_message(msg_id, code, _MESSAGES.get(code, code))
    if field:
        frame["error"]["field"] = field
    connection.send_message(frame)


# ---- reading the doors ---------------------------------------------------------------------------------------------

def door_entries(hass: HomeAssistant) -> list[ConfigEntry]:
    return [e for e in hass.config_entries.async_entries(DOMAIN) if is_door_entry(e)]


def _entry(hass: HomeAssistant, entry_id: str) -> ConfigEntry | None:
    return next((e for e in door_entries(hass) if e.entry_id == entry_id), None)


def _runtime(entry: ConfigEntry):
    """The door's runtime while it is loaded, else None (a door whose setup failed is still listed)."""
    if entry.state is not ConfigEntryState.LOADED:
        return None
    return getattr(entry, "runtime_data", None)


def _clean(text: Any) -> str:
    """A driver's free text, made safe to show: a string, no control characters, no `user:pass@` in a URL, cut short."""
    if not isinstance(text, str):
        return ""
    return _USERINFO.sub("", _CONTROL.sub(" ", text)).strip()[:_REASON_MAX]


def revision(entry: ConfigEntry) -> str:
    """A hash over the entry's bookkeeping data and its options."""
    blob = json.dumps({"listen": entry.data.get(gate_sip_listen.KEY), "options": dict(entry.options)},
                      sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _hub_facts(hass: HomeAssistant) -> dict[str, Any]:
    """The hub's process state and wheel version (no socket path)."""
    facts = get_hub(hass).diagnostics()
    return {key: facts.get(key) for key in ("process", "wheel_version", "min_wheel_version", "doors_attached", "restarted_once")}


def door_status(entry: ConfigEntry) -> dict[str, Any]:
    """One door's read-only card. Everything is a name, a state or a boolean; nothing that could be a secret. `settings`,
    `gate_sip` and `listen_state` were the gate observer's: published as null for an older panel."""
    runtime = _runtime(entry)
    out: dict[str, Any] = {
        "entry_id": entry.entry_id,
        "name": entry.title,
        "driver": entry.data.get("driver"),
        "loaded": runtime is not None,
        "entry_state": entry.state.value,
        "away_media": True,                  # deprecated: always on, no option; published for old panels
        "settings": None,
        "gate_sip": None,
        "listen_state": None,
    }
    if runtime is not None:
        out.update({
            "connected": runtime.entities_available,
            "transport_available": runtime.transport_available,
            "driver_available": runtime.driver_available,
            "call_state": runtime.call_state.state,
            "outbound_state": runtime.call_state.outbound_state,
            "can_dial": runtime.can_dial,
        })
    else:
        out.update({"connected": False, "transport_available": False, "driver_available": None, "call_state": None,
                    "outbound_state": None, "can_dial": False})
    return out


def door_settings(entry: ConfigEntry) -> dict[str, Any]:
    return {
        "entry_id": entry.entry_id,
        "name": entry.title,
        "driver": entry.data.get("driver"),
        "loaded": _runtime(entry) is not None,
        "revision": revision(entry),
        "away_media": True,                  # deprecated: always on, no option; published for old panels
        "listen": None,                      # the gate observer's; none is left
    }


def _helpers(hass: HomeAssistant) -> list[dict[str, str]]:
    out = []
    for entity_id in sorted(hass.states.async_entity_ids("input_boolean")):
        state = hass.states.get(entity_id)
        name = (state.attributes.get("friendly_name") if state is not None else None) or entity_id
        out.append({"entity_id": entity_id, "name": str(name)[:80]})
    return out


# ---- the patch -----------------------------------------------------------------------------------------------------

def parse_patch(patch: Any) -> dict[str, Any] | None:
    """The patch as a clean dict, or None when it is empty, has a key the panel may not change (a password or token among
    them), or a value of the wrong kind. `away_media` is accepted and dropped (deprecated)."""
    if not isinstance(patch, dict) or not patch:
        return None
    clean: dict[str, Any] = {}
    for key, value in patch.items():
        if key in ("away_media", "listen_enabled", "claims_enabled", "dial_enabled", CONF_HOUSE_CALLS):
            if value is not True and value is not False:
                return None
            if key == "away_media":
                continue          # deprecated: accepted from old panels (wire contract) and ignored; always on
        elif key == "audio_port":
            if type(value) is not int or not 1 <= value <= 65535:
                return None
        elif key == "ring_answered_entity":
            if value in (None, ""):
                value = None
            elif not isinstance(value, str) or not _HELPER.match(value):
                return None
        else:
            return None
        clean[key] = value
    return clean


# ---- the commands --------------------------------------------------------------------------------------------------

@websocket_api.websocket_command({vol.Required("type"): "portico/door_status"})
@websocket_api.async_response
async def handle_door_status(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """`{}` -> `{hub, doors, as_of}`: a read; nothing is written."""
    if not auth.require(connection, msg, "portico/door_status"):
        return
    try:
        reply = {"hub": _hub_facts(hass), "doors": [door_status(e) for e in door_entries(hass)],
                 "as_of": dt_util.utcnow().isoformat()}
    except Exception as err:  # noqa: BLE001 - never a bare 500, and never the exception's text
        _LOGGER.warning("Portico: door_status failed (%s)", type(err).__name__)
        _send_error(connection, msg["id"], "unknown")
        return
    connection.send_result(msg["id"], reply)


@websocket_api.websocket_command({vol.Required("type"): "portico/door_settings/get"})
@websocket_api.async_response
async def handle_door_settings_get(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """`{}` -> `{doors, helpers, effects}`: a read."""
    if not auth.require(connection, msg, "portico/door_settings/get"):
        return
    try:
        reply = {"doors": [door_settings(e) for e in door_entries(hass)], "helpers": _helpers(hass), "effects": dict(EFFECTS)}
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Portico: door_settings/get failed (%s)", type(err).__name__)
        _send_error(connection, msg["id"], "unknown")
        return
    connection.send_result(msg["id"], reply)


@websocket_api.websocket_command({
    vol.Required("type"): "portico/door_settings/set",
    vol.Required("entry_id"): _ENTRY_ID,
    vol.Required("revision"): vol.All(str, vol.Length(max=64)),
    vol.Required("patch"): dict,
    vol.Optional("confirm", default=False): bool,
    vol.Optional("validate_only", default=False): bool,
})
@websocket_api.async_response
async def handle_door_settings_set(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Replies `{result: "unchanged", reloaded: false, changes: [], reload: false, door}` for a patch of only the deprecated
    `away_media`; every other settable key belonged to the retired gate observer and is `not_for_this_driver`."""
    if not auth.require(connection, msg, "portico/door_settings/set"):
        return
    msg_id = msg["id"]
    entry = _entry(hass, msg["entry_id"])
    if entry is None:
        _send_error(connection, msg_id, "unknown_door")
        return
    patch = parse_patch(msg["patch"])
    if patch is None:
        _send_error(connection, msg_id, "bad_patch")
        return
    if patch:
        _send_error(connection, msg_id, "not_for_this_driver")
        return
    if msg["revision"] != revision(entry):
        _send_error(connection, msg_id, "stale_revision")
        return
    base = {"changes": [], "reload": False}
    if msg["validate_only"]:
        connection.send_result(msg_id, {"result": "valid", "reloaded": False, **base})
        return
    connection.send_result(msg_id, {"result": "unchanged", "reloaded": False, **base, "door": door_settings(entry)})


@callback
def async_setup_door_commands(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, handle_door_status)
    websocket_api.async_register_command(hass, handle_door_settings_get)
    websocket_api.async_register_command(hass, handle_door_settings_set)
