"""Test helpers for the phone entries (P-4)."""
from __future__ import annotations

from custom_components.portico.push.registration import _phones


def enrolled(hass) -> list[dict]:
    """Every enrolled phone as the consumers read it (phone entries and legacy rows)."""
    return [phone for _entry, phone in _phones(hass)]


def phone_entries(hass):
    from custom_components.portico.phone_entry import phone_entries as _entries

    return _entries(hass)
