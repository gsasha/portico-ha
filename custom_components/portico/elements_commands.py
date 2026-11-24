"""The Elements page's websocket commands (DEV-760, PR E2; design `portico_elements_design.md` section 4).

  portico/elements/list    the stored elements, their `rev`, `used_by` per element, and the registry the Add form is
                           built from (kinds, icons, palette, limits, and `talk_doors`: the doors a Talk element can name,
                           each with its driver and whether it can dial now)
  portico/elements/set     create (no `id`) or replace (with `id`) one element; `base_rev` is the optimistic-concurrency
                           token; `validate_only` checks everything and writes nothing
  portico/elements/delete  remove one element, named by `element_id` (a websocket message's own `id` is the frame id, so
                           the design's `{id, ...}` cannot be the key); an element a screen uses is REFUSED with `in_use` and the
                           screens (`used_by`), whatever the message says: there is no confirm override (the owner, 2026-11-09)

All three are `admin` in `auth.COMMANDS` and call `auth.require` first. Errors are websocket errors with a stable code;
no reply carries an exception's text (an unexpected failure answers `unknown`). The only thing written is
`.storage/portico.elements`: the content store, the endpoint registry, services and config entries are not touched
(ELEM-07). Nothing here runs an action or writes to Home Assistant (D8).
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from . import auth, config_sync
from .content import ICON_NAMES, KINDS, MAX_LABEL, PALETTE_NAMES
from .elements import (
    MAX_ELEMENTS,
    MAX_SHORT,
    MAX_STATE_LEN,
    MAX_STATES,
    MAX_STREAM_LEN,
    ElementError,
    get_elements_store,
    icon_policy,
    kinds_description,
    talk_door_choices,
)

_LOGGER = logging.getLogger(__name__)

_REV = vol.All(str, vol.Length(min=1, max=64))
_ID = vol.All(str, vol.Length(min=1, max=160))      # a managed Talk id is `talk-phone-<enrol id>`

_MESSAGES = {
    "unknown": "Something went wrong; see the Home Assistant log",
    "conflict": "The elements changed since they were read; reload and try again",
    "unknown_element": "No such element",
    "name_taken": "That name is already used",
    "kind_locked": "An element's kind cannot change; remove it and add a new one",
    "too_many": "There are too many elements",
    "newer_schema": "The stored elements are from a newer version and cannot be changed by this one",
    "in_use": "Screens use this element; remove those first",
    "in_use_action": "Screens use this button; its action cannot change. Remove it from those screens first",
    "in_use_stream": "Screens use this camera; it needs a camera entity",
    "in_use_retarget": "Screens use this element; changing what it points at changes all of them. Send confirm to apply",
    "managed_target_exists": "This Talk element belongs to a phone or screen that still exists; remove that first",
    "managed_locked": "A managed Talk element can only be renamed",
    "migration_missing": "The stored elements could not be migrated",
    "invalid": "That element is not valid",
}


def _send_error(connection: websocket_api.ActiveConnection, msg_id: int, err: ElementError | str) -> None:
    code = err.code if isinstance(err, ElementError) else err
    text = str(err) if isinstance(err, ElementError) and code == "invalid" else _MESSAGES.get(code, code)
    frame = websocket_api.error_message(msg_id, code, text)
    if isinstance(err, ElementError):
        if err.field:
            frame["error"]["field"] = err.field
        if err.used_by is not None:
            frame["error"]["used_by"] = err.used_by
    connection.send_message(frame)


def _registry() -> dict[str, Any]:
    kinds = kinds_description()
    if "button" in kinds:
        kinds["button"]["actions"] = [{"action": name, "entity_domain": spec[0]} for name, spec in KINDS.items()]
    return kinds


def _limits() -> dict[str, int]:
    return {"max_elements": MAX_ELEMENTS, "name": MAX_LABEL, "states": MAX_STATES, "state": MAX_STATE_LEN,
            "short": MAX_SHORT, "stream": MAX_STREAM_LEN}


@websocket_api.websocket_command({vol.Required("type"): "portico/elements/list"})
@websocket_api.async_response
async def handle_elements_list(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    if not auth.require(connection, msg, "portico/elements/list"):
        return
    try:
        store = get_elements_store(hass)
        await store.async_ensure_content()
        reply = await store.async_list()
        used = store.used_by_map()
        reply["used_by"] = {e["id"]: used.get(e["id"], []) for e in reply["elements"] if isinstance(e.get("id"), str)}
        reply.update(kinds=_registry(), icons=list(ICON_NAMES), icon_policy=icon_policy(), palette=list(PALETTE_NAMES), limits=_limits(),
                     talk_doors=talk_door_choices(hass))
    except Exception as err:  # noqa: BLE001 - never a bare 500, and never the exception's text
        _LOGGER.warning("Portico: elements/list failed (%s)", type(err).__name__)
        _send_error(connection, msg["id"], "unknown")
        return
    connection.send_result(msg["id"], reply)


@websocket_api.websocket_command({
    vol.Required("type"): "portico/elements/set",
    vol.Required("element"): dict,
    vol.Required("base_rev"): _REV,
    vol.Optional("validate_only", default=False): bool,
    # Needed to change the entity or target of an element a screen uses (`in_use_retarget` otherwise)
    vol.Optional("confirm", default=False): bool,
})
@websocket_api.async_response
async def handle_elements_set(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    if not auth.require(connection, msg, "portico/elements/set"):
        return
    try:
        result = await get_elements_store(hass).async_set(msg["element"], msg["base_rev"],
                                                          validate_only=msg["validate_only"], confirm=msg["confirm"])
    except ElementError as err:
        _send_error(connection, msg["id"], err)
        return
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Portico: elements/set failed (%s)", type(err).__name__)
        _send_error(connection, msg["id"], "unknown")
        return
    if msg["validate_only"]:
        result = {**result, "valid": True}
    else:
        config_sync.async_schedule(hass)       # DEV-824 P3: the files follow the save; never fails or delays it
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command({
    vol.Required("type"): "portico/elements/delete",
    vol.Required("element_id"): _ID,
    vol.Required("base_rev"): _REV,
    # Accepted and IGNORED: a tab from before this rule may still send it, and an element in use is refused either way.
    vol.Optional("confirm", default=False): bool,
})
@websocket_api.async_response
async def handle_elements_delete(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    if not auth.require(connection, msg, "portico/elements/delete"):
        return
    try:
        result = await get_elements_store(hass).async_delete(msg["element_id"], msg["base_rev"])
    except ElementError as err:
        _send_error(connection, msg["id"], err)
        return
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("Portico: elements/delete failed (%s)", type(err).__name__)
        _send_error(connection, msg["id"], "unknown")
        return
    config_sync.async_schedule(hass)
    connection.send_result(msg["id"], result)


@callback
def async_setup_elements_commands(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, handle_elements_list)
    websocket_api.async_register_command(hass, handle_elements_set)
    websocket_api.async_register_command(hass, handle_elements_delete)
