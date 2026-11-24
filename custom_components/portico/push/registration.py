"""Registering a phone's push target: the ONE implementation behind both
`portico/push/register` (the phone itself, over the WebSocket, signaling.py)
and the admin's pasted registration string (offline registration,
push_flow.py, `reg_string_spec.md`).

Both paths check the same field shapes (`FIELDS`), refuse the same values in
the same order (`async_register_phone`) and upsert the same registry row on
(ha_user_id, installation_id), so a phone registered from a pasted string and
later over the WebSocket keeps one row.
"""
from __future__ import annotations

import hmac
from typing import Any

import voluptuous as vol

from . import const

KINDS = ("fid", "token")

# The field shapes `portico/push/register` accepts (its voluptuous schema).
FIELDS: dict[str, Any] = {
    "installation_id": vol.All(str, vol.Length(min=1, max=128)),
    "kind": vol.In(list(KINDS)),
    "registration": vol.All(str, vol.Length(min=1, max=4096)),
    "package": vol.All(str, vol.Length(min=1, max=255)),
    "fcm_project_id": vol.All(str, vol.Length(min=1, max=128)),
    "app_version": vol.All(str, vol.Length(max=64)),
}

# Values a broken client sends instead of a real registration (the estate's
# fcm-ring once forwarded a never-set input_text's "unknown" to FCM).
PLACEHOLDERS = {"unknown", "unavailable", "none", "null", "undefined"}


class RegistrationRefused(Exception):
    """`code` is the WebSocket error code; `message` never quotes the input."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code)
        self.code = code
        self.message = message


def _phones(hass):
    """(entry, phone) for every enrolled phone, live or not: the phone entries (P-4), then the legacy
    `enrolled_phones` rows still on a Portico entry that has not adopted them (the settings entry before its first
    setup after P-4; an old door entry). A phone in two places (same HA user) is yielded once, the phone entry
    first. Every consumer reads phones through here (phone_entry.phone_rows)."""
    from ..phone_entry import phone_rows

    yield from phone_rows(hass)


def enrolled_phone(hass, user_id: str | None) -> dict[str, Any] | None:
    """The enrolled phone `user_id` is (its entry's data plus `doors`, or a legacy row), or None."""
    if user_id is None:
        return None
    for _entry, phone in _phones(hass):
        if phone.get("ha_user_id") == user_id:
            return phone
    return None


def enrolled_phone_name(hass, user_id: str | None) -> str | None:
    """The enrolled name of `user_id` on any device entry, live or not; None
    when it is not an enrolled phone user (§3.3: that identity is the key)."""
    if user_id is None:
        return None
    for _entry, phone in _phones(hass):
        if phone.get("ha_user_id") == user_id:
            return str(phone.get("name") or "phone")
    return None


async def async_find_phone_by_enroll_id(hass, enroll_id: str) -> tuple[str, str] | None:
    """(ha_user_id, name) of the enrolled phone whose QR carried `enroll_id`,
    or None. Every stored id is compared (no early exit, constant-time per
    id), and a phone whose HA user is gone or deactivated is not found: the
    enrolled_phones record outlives a user deleted in Settings > People."""
    wanted = enroll_id.encode("utf-8")
    found: str | None = None
    for _entry, phone in _phones(hass):
        stored = phone.get("enroll_id")
        if isinstance(stored, str) and stored and hmac.compare_digest(stored.encode("utf-8"), wanted):
            found = found or phone.get("ha_user_id")
    if not found:
        return None
    user = await hass.auth.async_get_user(found)
    if user is None or not user.is_active:
        return None
    name = enrolled_phone_name(hass, found)
    return (found, name) if name is not None else None


async def async_register_phone(
    manager, *, user_id: str, name: str, installation_id: str, kind: str, registration: str, package: str,
    fcm_project_id: str, app_version: str, enroll_id: str | None = None,
) -> dict[str, Any]:
    """`portico/push/register`'s checks and upsert, for an already identified
    enrolled phone. Raises RegistrationRefused; returns the row."""
    registration = registration.strip()
    if not registration or registration.lower() in PLACEHOLDERS:
        raise RegistrationRefused("invalid_registration", "blank or placeholder registration")
    if fcm_project_id != manager.project_id:
        raise RegistrationRefused(
            "project_mismatch",
            "this phone uses a different Firebase project than Home Assistant; re-scan the enrollment QR",
        )
    if package != manager.package:
        raise RegistrationRefused("package_mismatch", "registrations are accepted for the Portico app only")
    return await manager.registry.async_register(
        ha_user_id=user_id, installation_id=installation_id, kind=kind, registration=registration,
        package=package, fcm_project_id=fcm_project_id, app_version=app_version, name=name, enroll_id=enroll_id,
    )
