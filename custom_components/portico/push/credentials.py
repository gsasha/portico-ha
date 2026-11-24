"""The service-account credential: parse, validate, and keep it in a 0600
private Store (sender design §2.3, decision 1 of §12).

The key lives ONLY in `.storage/portico.push_credentials`, written by HA's
`Store(..., private=True, atomic_writes=True)`: HA's writer skips its 0644
chmod for a private store, so the file keeps the 0600 mode its temp file was
created with. It never goes into a config entry (core.config_entries is 0644
and ends up in support dumps), never into a log, a diagnostics download, a
form default or an exception message. Errors carry a short code only.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from . import const

_REQUIRED = ("project_id", "client_email", "private_key", "token_uri")
# Google project ids: 6-30 chars of [a-z0-9-] (looser bound here); it goes
# into the FCM URL path, so nothing else may.
_PROJECT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{3,62}$")


class CredentialError(ValueError):
    """A service account this integration refuses. `code` doubles as the
    config-flow error key; the message is the code and nothing else."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class GoogleServicesError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ServiceAccount:
    project_id: str
    client_email: str
    private_key_id: str
    token_uri: str
    private_key: str = field(repr=False)
    _signing_key: Any = field(repr=False, compare=False, default=None)

    @property
    def signing_key(self) -> rsa.RSAPrivateKey:
        return self._signing_key

    def to_store(self) -> dict[str, str]:
        return {
            "type": "service_account",
            "project_id": self.project_id,
            "client_email": self.client_email,
            "private_key_id": self.private_key_id,
            "token_uri": self.token_uri,
            "private_key": self.private_key,
        }

    def __str__(self) -> str:  # never the key
        return f"ServiceAccount({self.client_email}, project={self.project_id})"


def _load_json(raw: str | dict, error: type[Exception]) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        # `from None`: a JSONDecodeError holds the whole document in `.doc`.
        raise error("invalid_json") from None
    if not isinstance(data, dict):
        raise error("invalid_json")
    return data


def parse_service_account(raw: str | dict, *, allowed_token_uri: str | None = None) -> ServiceAccount:
    """§2.3 validation: `type`, the four required fields, an RSA key, and the
    pinned token URI (`const.ENDPOINTS.token_uri` unless overridden). The live
    token fetch is the caller's (the config flow's) last step."""
    data = _load_json(raw, CredentialError)
    if data.get("type") != "service_account":
        raise CredentialError("not_service_account")
    if not all(isinstance(data.get(k), str) and data.get(k).strip() for k in _REQUIRED):
        raise CredentialError("missing_fields")
    # fullmatch: `$` alone also matches before a trailing newline, and the
    # project id goes into the FCM URL path.
    if not _PROJECT_ID_RE.fullmatch(data["project_id"]):
        raise CredentialError("invalid_project_id")
    pinned = allowed_token_uri if allowed_token_uri is not None else const.ENDPOINTS.token_uri
    if data["token_uri"] != pinned:
        raise CredentialError("token_uri_not_allowed")
    try:
        key = serialization.load_pem_private_key(data["private_key"].encode(), password=None)
    except (ValueError, TypeError, AttributeError):
        raise CredentialError("invalid_key") from None
    except Exception:  # noqa: BLE001 - cryptography's own unsupported-algorithm errors
        raise CredentialError("invalid_key") from None
    if not isinstance(key, rsa.RSAPrivateKey):
        raise CredentialError("invalid_key")
    return ServiceAccount(
        project_id=data["project_id"],
        client_email=data["client_email"],
        private_key_id=str(data.get("private_key_id") or ""),
        token_uri=data["token_uri"],
        private_key=data["private_key"],
        _signing_key=key,
    )


# portico-app's FirebaseConfig.create (core/enroll/FirebaseConfig.kt) applies
# the same checks before it stores the values; both sides refuse the same
# documents. `mobilesdk_app_id` is "<version>:<project_number>:android:<hex>".
_SENDER_ID_RE = re.compile(r"^[0-9]{1,32}$")
_APP_ID_RE = re.compile(r"^[0-9]+:([0-9]+):android:[0-9A-Za-z]+$")
_API_KEY_RE = re.compile(r"^[\x21-\x7e]{1,256}$")  # printable ASCII, no space
_PLACEHOLDERS = {"unknown", "unavailable", "none", "null", "undefined"}


def _text(value: Any, *, integer_ok: bool = False) -> str:
    """`value` as non-blank text, or `missing_fields`. Never `str()` of a
    null, bool, float or container (that gave "None" / "True")."""
    if integer_ok and isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return str(value)
    if not isinstance(value, str) or not value.strip():
        raise GoogleServicesError("missing_fields")
    return value


def parse_google_services(raw: str | dict, package: str) -> dict[str, str]:
    """§5.2: the four Firebase client values (not secret) for `package`'s
    Android app from a `google-services.json`.

    Codes: `invalid_json` (not one JSON object); `missing_fields` (a part
    missing, of the wrong shape, or a value that is not non-blank text);
    `package_not_found` (no Android app for `package`); `invalid_values`
    (present but unusable: a placeholder, a malformed id or key, or an app
    id whose project number is not the file's `project_number`)."""
    data = _load_json(raw, GoogleServicesError)
    info = data.get("project_info")
    if not isinstance(info, dict) or not info.get("project_id") or not info.get("project_number"):
        raise GoogleServicesError("missing_fields")
    project_id = _text(info["project_id"])
    sender_id = _text(info["project_number"], integer_ok=True)

    clients = data.get("client") or []
    if not isinstance(clients, list):
        raise GoogleServicesError("missing_fields")
    for client in clients:
        # Every entry must have the package path (a malformed one aborts the
        # scan, as it always has); only Portico's needs the values.
        if not isinstance(client, dict):
            raise GoogleServicesError("missing_fields")
        client_info = client.get("client_info")
        android = client_info.get("android_client_info") if isinstance(client_info, dict) else None
        if not isinstance(android, dict) or "package_name" not in android:
            raise GoogleServicesError("missing_fields")
        if android["package_name"] != package:
            continue
        keys = client.get("api_key")
        first = keys[0] if isinstance(keys, list) and keys else None
        if not isinstance(first, dict):
            raise GoogleServicesError("missing_fields")
        values = {
            "project_id": project_id,
            "sender_id": sender_id,
            "app_id": _text(client_info.get("mobilesdk_app_id")),
            "api_key": _text(first.get("current_key")),
        }
        _check_values(values)
        return values
    raise GoogleServicesError("package_not_found")


def _check_values(values: dict[str, str]) -> None:
    if any(v.strip().lower() in _PLACEHOLDERS for v in values.values()):
        raise GoogleServicesError("invalid_values")
    # fullmatch: `$` alone also matches before a trailing newline.
    app = _APP_ID_RE.fullmatch(values["app_id"])
    if (
        not _PROJECT_ID_RE.fullmatch(values["project_id"])
        or not _SENDER_ID_RE.fullmatch(values["sender_id"])
        or app is None
        or app.group(1) != values["sender_id"]  # an app of another Firebase project
        or not _API_KEY_RE.fullmatch(values["api_key"])
    ):
        raise GoogleServicesError("invalid_values")


_FIREBASE_KEYS = ("project_id", "sender_id", "app_id", "api_key")


def app_firebase_values(stored: Any) -> dict[str, str] | None:
    """The four Firebase client values a push entry stores (`firebase`), for
    the enrollment QR's `fcm` block, or None when portico-app would refuse
    them. The app's `EnrollmentPayload.parseFcm` refuses the WHOLE enrollment
    over a bad block, so a block it would refuse must not be sent at all.

    Mirrors `FirebaseConfig.create`: each value a non-blank string, not a
    placeholder, then the four patterns (whole-string matches, like Kotlin's
    `Regex.matches`) and the app id's project number equal to the sender id.
    Only the four keys are returned, whatever else `stored` holds."""
    if not isinstance(stored, dict):
        return None
    values = {key: stored.get(key) for key in _FIREBASE_KEYS}
    if not all(isinstance(v, str) and v.strip() for v in values.values()):
        return None
    try:
        _check_values(values)
    except GoogleServicesError:
        return None
    return values


class CredentialStore:
    """The private Store holding the one service account (§2.3 option b)."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store[dict] = Store(
            hass, 1, const.CREDENTIALS_STORE_KEY, private=True, atomic_writes=True
        )

    @property
    def path(self) -> str:
        return self._store.path

    async def async_load(self) -> ServiceAccount | None:
        """None when nothing is stored; CredentialError when what is stored
        no longer parses (the caller raises `push_credentials_invalid`)."""
        data = await self._store.async_load()
        if not data:
            return None
        return parse_service_account(data)

    async def async_save(self, account: ServiceAccount) -> None:
        await self._store.async_save(account.to_store())

    async def async_remove(self) -> None:
        await self._store.async_remove()
