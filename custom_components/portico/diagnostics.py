"""Diagnostics downloads (sender design §7): redacted.

Redacts every credential a Portico entry can hold (the push entry's Firebase api_key and the adopted
Tailscale credentials; a door's device SIP/RTSP/NVR credentials) and anything NAMED like a secret, with ONE rule for
all entry kinds (DEV-700 option A): a case-insensitive substring PATTERN on the key (`SECRET_KEY_PATTERNS`), an
explicit ALLOW-LIST of known non-secret keys that stay readable (`ALLOWED_KEYS`), hash-pseudonyms for personal and
topology values (`PSEUDONYM_KEY_PATTERNS`), and a value rule that redacts any URL carrying credentials whatever its
key. A new secret-looking config-flow field is therefore covered the day it is added, and
`tests/test_diagnostics_redaction.py` fails when one is neither redacted nor on the allow-list. A door entry adds its
runtime and hub sections (door/diagnostics.py). The service-account private key is never
in an entry to begin with (it lives in the private Store, §2.3), and the push
section is built from the registry's public projection, which has no
registration value.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .content import get_content_store
from .door.const import is_door_entry
from .door.diagnostics import door_diagnostics_sections
from .endpoints import KIND_PHONE, get_endpoint_registry
from .phone_entry import is_phone_entry
from .push import get_manager
from .push.const import ENTRY_TYPE, ENTRY_TYPE_PUSH
from .push.registry import PushRegistry

TO_REDACT = {
    "sip_password", "rtsp_password", "nvr_api_key", "tailscale_api_key",
    "private_key", "registration", "api_key", "ha_token", "access_token", "token", "fid",
    # DEV-544: the one-time enrollment code and what redeeming it returns. None is ever stored in an entry (the code
    # lives in memory as a keyed digest); redacted in case one ever is.
    "refresh_token", "nonce", "enroll_code", "tailscale_authkey",
    # DEV-556: never in an entry (the client secret lives in a private Store,
    # the admin token is used once); redacted in case one ever is.
    "client_secret", "tailscale_client_secret", "api_access_token",
    # DEV-520: gate-sip's listen-mode credential; lives in a private Store, never in an entry.
    "peer_token",
    # Not secret on the wire, but it is the one credential behind offline registration: a pasted registration
    # string is accepted for the phone whose `e` matches (push_flow.py), so a bundle holding it can forge one.
    "enroll_id",
}


# DEV-700 A. The rule, in the order it is applied to each key (lower-cased), at every depth:
#   1. `ALLOWED_KEYS` (exact): readable. A container under one is still walked, its children judged one by one.
#   2. `TO_REDACT` (exact) or any `SECRET_KEY_PATTERNS` substring: the whole value becomes REDACTED.
#   3. any `PSEUDONYM_KEY_PATTERNS` substring: every scalar under it becomes `redacted-<8 hex>`, the same value the same
#      word within one download (salted per download, so a short value such as a host cannot be brute-forced back).
# `open_relay_action` has its own rule (an action list: targets stay, everything else goes), and a string that is a URL
# with credentials, a query secret or a known secret shape is REDACTED whatever its key. When in doubt, redact.
REDACTED = "**REDACTED**"

SECRET_KEY_PATTERNS = (
    "password", "passwd", "passphrase", "secret", "token", "api_key", "apikey", "key", "auth", "credential",
    "private", "registration", "nonce", "fid", "enroll_id", "cookie", "bearer", "session",
    "username", "sip_user", "rtsp",
)

PSEUDONYM_KEY_PATTERNS = (
    "host", "url", "uri", "user", "email", "tailnet", "client_id", "camera_id", "address", "dial", "node", "name",
    "domain", "ssid",
)
# Short words that must match a whole `_`-separated part, not a substring ("ip" would hit "description").
PSEUDONYM_KEY_PARTS = ("ip", "mac", "fqdn")

# Known non-secret keys that the patterns would otherwise catch (or that a reader of a support bundle needs plain).
ALLOWED_KEYS = frozenset({
    "entry_type", "driver", "interval", "enabled", "audio_port", "claims", "dial", "ring_answered_entity",
    "existing_camera_entity", "gate_sip_listen", "verify_ssl", "ring_events", "tailscale_mode", "tailscale_tag",
    "token_kind", "token_expires_at", "refresh_state", "state", "title", "package", "project_id",
    "registrations", "registration_updated_at", "new_phones_enabled", "enrolled_at", "pending_since",
    "ring_window", "away_media", "event_entity", "event_type", "ring_hold", "doors", "house_receive", "capabilities",
})

# What an `open_relay_action` list keeps: which action it runs and on what. Every other key (`data`, headers, a
# template, a variable: where a token or a code would be typed) is REDACTED.
_ACTION_KEEP = frozenset({
    "action", "service", "alias", "enabled", "continue_on_error", "target", "entity_id", "device_id", "area_id",
    "floor_id", "label_id",
})

_URL_USERINFO = re.compile(r"[a-z][a-z0-9+.\-]*://[^/\s?#]*@", re.IGNORECASE)
_SECRET_SHAPE = re.compile(r"tskey-|^\s*bearer\s|-----BEGIN [A-Z ]*PRIVATE KEY", re.IGNORECASE)
_QUERY_SECRET = re.compile(
    r"[?&;](?:[^=&#\s]*(?:" + "|".join(SECRET_KEY_PATTERNS) + r")[^=&#\s]*)=", re.IGNORECASE)


def _is_secret_key(key: str) -> bool:
    return key in TO_REDACT or any(p in key for p in SECRET_KEY_PATTERNS)


def _is_pseudonym_key(key: str) -> bool:
    return any(p in key for p in PSEUDONYM_KEY_PATTERNS) or any(p in key.split("_") for p in PSEUDONYM_KEY_PARTS)


def _is_secret_string(value: str) -> bool:
    return bool(_URL_USERINFO.search(value) or _SECRET_SHAPE.search(value) or _QUERY_SECRET.search(value))


def redact_diagnostics(data: Any) -> Any:
    """A redacted COPY of a diagnostics download (the input is never changed): the rule above, applied to every
    nested dict and list. See the module docstring."""
    salt = os.urandom(8)

    def word(value: Any) -> Any:
        if value is None or isinstance(value, bool) or value == "" or value == REDACTED:
            return value
        digest = hashlib.sha256(salt + str(value).encode("utf-8")).hexdigest()[:8]
        return "redacted-" + digest

    def pseudonymise(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: pseudonymise(v) for k, v in node.items()}
        if isinstance(node, (list, tuple)):
            return [pseudonymise(v) for v in node]
        return word(node)

    def action(node: Any) -> Any:
        if isinstance(node, (list, tuple)):
            return [action(v) for v in node]
        if isinstance(node, dict):
            return {k: (walk(v, True) if str(k).lower() in _ACTION_KEEP else REDACTED) for k, v in node.items()}
        return walk(node, True)

    def walk(node: Any, patterns: bool) -> Any:
        if isinstance(node, dict):
            out: dict[Any, Any] = {}
            for key, value in node.items():
                low = str(key).lower()
                if low == "open_relay_action":
                    out[key] = action(value)
                elif patterns and low in ALLOWED_KEYS:
                    out[key] = walk(value, patterns)
                elif low in TO_REDACT or (patterns and _is_secret_key(low)):
                    out[key] = value if value in ("", None) else REDACTED
                elif patterns and _is_pseudonym_key(low):
                    out[key] = pseudonymise(walk(value, patterns))
                else:
                    out[key] = walk(value, patterns)
            return out
        if isinstance(node, (list, tuple)):
            return [walk(v, patterns) for v in node]
        if isinstance(node, str) and _is_secret_string(node):
            return REDACTED
        return node

    result = {}
    for section, value in dict(data).items():
        # The content document is user-authored and already pseudonymised (`pseudonymise_document`): its key names are
        # slugs and block names, so only the exact-name set and the value rule apply to it.
        result[section] = walk(value, patterns=section != "content")
    return result


_LOGGER = logging.getLogger(__name__)


def pseudonymise_document(document: dict[str, Any], secret_ids: set[str]) -> dict[str, Any]:
    """The content document with every id in `secret_ids` replaced, as a key and as a value, by
    `endpoint-<8 hex of its sha256>`: the same id gets the same word everywhere, so the structure (which override
    belongs to which phone, which rule names whom) can still be read, and no phone's `enroll_id` is in the download
    (it is the credential behind offline registration, see TO_REDACT). Everything else is as stored."""
    def word(value: str) -> str:
        return "endpoint-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]

    def name(value: str) -> str:
        """`word(value)` for an id, and for a managed Talk element id that EMBEDS one (`talk-phone-<enrol id>`,
        `talk-screen-<slug>`; they are the keys of a device's `ring` map and the `talk` of a Ring page): the prefix stays, the
        id inside goes."""
        if value in secret_ids:
            return word(value)
        for prefix in ("talk-phone-", "talk-screen-"):
            if value.startswith(prefix) and value[len(prefix):] in secret_ids:
                return prefix + word(value[len(prefix):])
        return value

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            return {(name(k) if isinstance(k, str) else k): walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return name(node) if isinstance(node, str) else node

    return walk(document)


async def _content_document(hass: HomeAssistant) -> dict[str, Any] | None:
    """The stored content document for the push entry's download (M6 E2), or None when it cannot be read. The
    keys of `overrides` and `endpoints` are phone endpoint ids (enroll ids) or the ids of removed phones: all
    pseudonymised, except a declared screen's slug."""
    try:
        store = get_content_store(hass)
        await store.async_ensure_loaded()
        document = store.document()
        slugs = set(document.get("dashboards", {}))
        secret = {ep.id for ep in get_endpoint_registry(hass).endpoints() if ep.kind == KIND_PHONE}
        secret |= set(document.get("overrides", {})) | set(document.get("devices", {})) | set(document.get("endpoints", {}))
        return pseudonymise_document(document, secret - slugs)
    except Exception:  # noqa: BLE001 - a diagnostics download must not fail over the content file
        _LOGGER.warning("Portico diagnostics: the content document could not be read", exc_info=True)
        return None


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    # A phone entry's title IS the phone's name (a personal value, pseudonymised under `data.name`).
    title = "Portico phone" if is_phone_entry(entry) else entry.title
    out: dict[str, Any] = {
        "entry": {"title": title, "data": dict(entry.data), "options": dict(entry.options)},
    }
    if entry.data.get(ENTRY_TYPE) == ENTRY_TYPE_PUSH:
        manager = get_manager(hass)
        out["push"] = manager.diagnostics() if manager is not None else None
        out["content"] = await _content_document(hass)
    elif is_door_entry(entry):
        out.update(door_diagnostics_sections(entry))
    elif is_phone_entry(entry):
        # A phone entry holds identity facts only (its `enroll_id` is redacted); its push registrations go through the
        # same public projection as the push ring notification's diagnostics (no registration value).
        manager = get_manager(hass)
        rows = (manager.registry.rows_for_phone(entry.data.get("enroll_id"), entry.data.get("ha_user_id") or "")
                if manager is not None else None)
        out["push_registrations"] = None if rows is None else [PushRegistry.public(r) for r in rows]
    return redact_diagnostics(out)
