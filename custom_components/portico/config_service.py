"""`portico.validate_config`: read the YAML tree under `/config/portico/` and say whether it is valid (DEV-824 P1, design
section 5.3 with `dry_run`). Read-only: nothing is applied, nothing is written, no live store is touched. The checks are the
editor's PURE validators (structure, field rules, limits, secrets); the checks that need Home Assistant (that an entity, a
phone, a door or a Talk target exists) are `portico.reload_config` (P2, config_apply.py), so a green answer here says `checked: "structure"`.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse

from . import auth, config_apply, config_sync, config_yaml
from .provider import DOMAIN

_LOGGER = logging.getLogger(__name__)

SERVICE_VALIDATE_CONFIG = "validate_config"
SERVICE_RELOAD_CONFIG = "reload_config"
CONFIG_DIR = "portico"


def read_tree(root: str) -> tuple[dict[str, str], list[config_yaml.ConfigError]]:
    """Every regular `.yaml` file under `root` (and `README.md` is skipped) as `{relative path: text}`. A symlink, an unreadable
    or non-UTF-8 file, or too many files is an error, not a skip. Blocking: run it in the executor."""
    files: dict[str, str] = {}
    errors: list[config_yaml.ConfigError] = []
    total = 0
    for base, dirs, names in os.walk(root, followlinks=False):
        dirs.sort()
        for name in sorted(names):
            full = os.path.join(base, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if not name.endswith(".yaml"):
                continue
            if os.path.islink(full):
                errors.append(config_yaml.ConfigError(rel, 1, 1, "a symbolic link is not allowed", "symlink"))
                continue
            if len(files) >= config_yaml.MAX_FILES:
                errors.append(config_yaml.ConfigError("", 1, 1, f"more than {config_yaml.MAX_FILES} files", "too_many_files"))
                return files, errors
            try:
                size = os.path.getsize(full)
                if size > config_yaml.MAX_FILE_BYTES or total + size > config_yaml.MAX_TREE_BYTES:
                    errors.append(config_yaml.ConfigError(rel, 1, 1, "the file is too large", "too_big"))
                    continue
                with open(full, encoding="utf-8") as handle:
                    files[rel] = handle.read()
                total += size
            except (OSError, UnicodeDecodeError) as err:
                errors.append(config_yaml.ConfigError(rel, 1, 1, f"cannot be read: {type(err).__name__}", "unreadable"))
    return files, errors


def validate_directory(root: str) -> dict[str, Any]:
    """The service's reply for the tree at `root` (blocking)."""
    if not os.path.isdir(root):
        error = config_yaml.ConfigError("", 1, 1, "there is no portico directory in the Home Assistant config", "no_directory")
        return {"ok": False, "checked": "structure", "errors": [error.as_dict()], "counts": {}}
    files, read_errors = read_tree(root)
    result = config_yaml.validate_files(files)
    errors = [e.as_dict() for e in read_errors] + [e.as_dict() for e in result.errors]
    return {"ok": not errors, "checked": "structure", "errors": errors, "counts": result.counts}


@websocket_api.websocket_command({vol.Required("type"): "portico/config/status"})
@websocket_api.async_response
async def handle_config_status(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """DEV-824 P5: `{}` -> `{enabled, dir_exists, pending, pending_total, last_write_error, last_reload}` for the editor's banner.
    Administrators only, read-only (it reads the files' bytes and two in-memory records; nothing is written or fired)."""
    if not auth.require(connection, msg, "portico/config/status"):
        return
    reply = await config_sync.status(hass)
    reply["last_reload"] = config_apply.last_reload(hass)
    connection.send_result(msg["id"], reply)


def async_setup_config_service(hass: HomeAssistant) -> None:
    async def _handle(call: ServiceCall) -> ServiceResponse:
        root = hass.config.path(CONFIG_DIR)
        reply = await hass.async_add_executor_job(validate_directory, root)
        if not reply["ok"]:
            _LOGGER.warning("Portico: the YAML configuration has %d error(s); the first is %s", len(reply["errors"]),
                            reply["errors"][0])
        return reply

    hass.services.async_register(DOMAIN, SERVICE_VALIDATE_CONFIG, _handle, supports_response=SupportsResponse.ONLY)

    async def _reload(call: ServiceCall) -> ServiceResponse:
        """DEV-824 P2: validate, check against the live house, report and (unless `dry_run`) apply, all or nothing. A refusal is a
        reply with `ok: false` and `errors` as file:line:col, never an exception, so a pipeline can show it."""
        root = hass.config.path(CONFIG_DIR)
        if not await hass.async_add_executor_job(os.path.isdir, root):
            error = config_yaml.ConfigError("", 1, 1, "there is no portico directory in the Home Assistant config", "no_directory")
            refusal = {"ok": False, "applied": False, "dry_run": call.data["dry_run"], "changed": False, "checked": "full",
                       "errors": [{**error.as_dict(), "at": ":1:1"}], "warnings": [], "report": None}
            config_apply.record_reload(hass, refusal)
            return refusal
        files, read_errors = await hass.async_add_executor_job(read_tree, root)
        reply = await config_apply.reload_config(hass, files, read_errors, dry_run=call.data["dry_run"])
        config_apply.record_reload(hass, reply)
        if not reply["ok"]:
            _LOGGER.warning("Portico: reload_config refused: %d error(s); the first is %s", len(reply["errors"]),
                            reply["errors"][0] if reply["errors"] else None)
        return reply

    hass.services.async_register(DOMAIN, SERVICE_RELOAD_CONFIG, _reload, schema=config_apply.SERVICE_SCHEMA,
                                 supports_response=SupportsResponse.ONLY)
