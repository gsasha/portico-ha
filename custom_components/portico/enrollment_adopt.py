"""S-1 of the Portico split: the push entry becomes a second home for enrollment.

Enrollment state (the Tailscale answer, `ha_dial`, the OAuth app's client secret
in its private Store) lives today on a DEVICE entry. The coming split deletes
the device entries on migration day, so the push entry ("Push ring notification (Firebase)",
the settings host) must hold it first. `async_adopt_enrollment` COPIES it there,
once, at the push entry's setup:

  - only what the push entry does not already have (it never overwrites);
  - never touching the device entry (nothing is removed, so the old host keeps
    working and a surprise restart is harmless);
  - idempotent: a second run finds the answer on the push entry and does nothing;
  - the secret first, then the entry data in one write, so a failure halfway
    leaves the push entry without an answer (the next setup retries) and never
    with an `approve` answer whose secret is missing;
  - no secret, key or address in any log line.

Phone rows are NOT copied here: `push.registration._phones` reads them from every
entry, so a phone enrolled on a device entry keeps working where it is. Since P-4
each phone is its own entry; the rows on the push entry become entries in
`phone_entry.async_adopt_phones` (a device entry's rows are read, not adopted).

`oauth_copy_is_readable` is the migration runbook's check that the secret the
owner does not hold anywhere else really arrived.
"""
from __future__ import annotations

import hmac
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .push import const
from .tailscale_approve import OAuthAppStore

_LOGGER = logging.getLogger(__name__)

DOMAIN = "portico"

# The entry-data keys that make up the Tailscale answer (enrollment_flow.py).
TAILSCALE_KEYS = (
    "tailscale_mode",
    "tailscale_api_key",
    "tailscale_tailnet",
    "tailscale_tag",
    "tailscale_client_id",
    "tailscale_redirect_uri",
)


def _door_entries(hass: HomeAssistant) -> list[ConfigEntry]:
    """Every OLD-FORMAT device entry: a Portico entry whose data names a driver and has no entry kind (from before the
    first split, when it held the enrollment state). A door entry of today carries `entry_type: door` and a `driver`
    too, but it never held enrollment state and is not a donor."""
    return [e for e in hass.config_entries.async_entries(DOMAIN)
            if e.data.get("driver") and not e.data.get(const.ENTRY_TYPE)]


def _push_entry(hass: HomeAssistant) -> ConfigEntry | None:
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.data.get(const.ENTRY_TYPE) == const.ENTRY_TYPE_PUSH:
            return entry
    return None


def _has_answer(data: Any) -> bool:
    """The push entry already holds a Tailscale answer (complete or not: it is
    never overwritten). A pre-mode entry has only `tailscale_api_key`."""
    return "tailscale_mode" in data or "tailscale_api_key" in data


async def _source_with_answer(hass: HomeAssistant, doors: list[ConfigEntry]) -> ConfigEntry | None:
    """The first device entry with a COMPLETE answer, by the check enrollment
    itself uses (an `approve` answer counts only with its secret in the Store)."""
    from .enrollment_flow import _async_tailscale_answer_complete, _tailscale_answer_complete

    for door in doors:
        if "tailscale_mode" not in door.data and "tailscale_api_key" in door.data:
            # An entry from before the mode existed: enrollment treats it as `auto`.
            if _tailscale_answer_complete({**door.data, "tailscale_mode": "auto"}):
                return door
        elif await _async_tailscale_answer_complete(hass, door):
            return door
    return None


async def _first_door_secret(hass: HomeAssistant, doors: list[ConfigEntry],
                             first: ConfigEntry | None = None) -> str | None:
    ordered = ([first] if first is not None else []) + [d for d in doors if d is not first]
    for door in ordered:
        secret = await OAuthAppStore(hass, door.entry_id).async_load_secret()
        if secret is not None:
            return secret
    return None


async def async_adopt_enrollment(hass: HomeAssistant, push: ConfigEntry) -> list[str]:
    """Copy the enrollment state from the device entries onto `push`. Returns
    what was adopted (names only), [] when there was nothing to do."""
    doors = _door_entries(hass)
    if not doors:
        return []
    adopted: list[str] = []
    store = OAuthAppStore(hass, push.entry_id)
    have_secret = await store.async_load_secret() is not None
    new_data: dict[str, Any] = {}

    if not _has_answer(push.data):
        source = await _source_with_answer(hass, doors)
        if source is not None:
            # The secret before the entry data (a failed Store write leaves the push entry untouched).
            if not have_secret:
                secret = await _first_door_secret(hass, doors, source)
                if secret is not None:
                    await store.async_save_secret(secret)
                    have_secret = True
                    adopted.append("oauth_app_secret")
            for key in TAILSCALE_KEYS:
                if key in source.data and key not in push.data:
                    new_data[key] = source.data[key]
            if source.data.get("ha_dial") and "ha_dial" not in push.data:
                new_data["ha_dial"] = source.data["ha_dial"]
            if new_data:
                adopted.append("tailscale_answer")
                if "ha_dial" in new_data:
                    adopted.append("ha_dial")
    elif push.data.get("tailscale_mode") == "approve" and not have_secret:
        # An `approve` answer without its secret (an earlier copy was cut short):
        # the same repair, from whichever device entry still holds it.
        secret = await _first_door_secret(hass, doors)
        if secret is not None:
            await store.async_save_secret(secret)
            adopted.append("oauth_app_secret")

    if new_data:
        hass.config_entries.async_update_entry(push, data={**push.data, **new_data})
    if adopted:
        _LOGGER.info(
            "portico: the push ring notification entry adopted enrollment settings from a device entry (%s); "
            "the device entry is unchanged", ", ".join(adopted))
    return adopted


async def oauth_copy_is_readable(hass: HomeAssistant) -> bool:
    """The migration runbook's check: True only when the push entry's OAuth app
    secret is readable through `OAuthAppStore.async_load_secret` (the load the
    enrollment gate and the approve step use) AND, while a device entry still
    holds one, equals it. False when the push entry has no secret, or holds one
    that differs from every device entry's. Compares, never returns or logs a
    secret. Call it with `await oauth_copy_is_readable(hass)` before S-2's
    migration deletes the device entries."""
    push = _push_entry(hass)
    if push is None:
        return False
    copied = await OAuthAppStore(hass, push.entry_id).async_load_secret()
    if copied is None:
        return False
    originals = []
    for door in _door_entries(hass):
        secret = await OAuthAppStore(hass, door.entry_id).async_load_secret()
        if secret is not None:
            originals.append(secret)
    if not originals:
        return True
    return any(hmac.compare_digest(copied.encode(), o.encode()) for o in originals)
