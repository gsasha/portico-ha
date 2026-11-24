"""UI split P2: the enrolment QR held for the panel (memory only), and what this server remembers of each enrolment.

The QR decodes to the one-time code and, in `auto` mode, a pre-authorised Tailscale key, so it never travels as a
websocket frame (HA logs every frame at DEBUG). `phones/enroll` puts the PNG here under a 32-byte random token and
replies with the token; the admin-only view (`enroll_view.py`) serves the bytes once the panel fetches them with the
user's own bearer.

A token is NOT a credential by itself (the view also needs an administrator's login), is not consumed by a read (a
retry after a network blip works), and stops working when its code does: at cancel, at expiry, when the phone redeems
the code, when the phone is removed, or when a newer QR is drawn for the same phone. Liveness is asked of the code
registry (`EnrollRegistry.seconds_left`), never kept as a second clock.

The record also remembers whether THIS enrolment created the phone's entry (`created`): that is what lets `cancel`
remove an entry safely. It lives here, in the server's memory, next to the token. A flag from a client is never read.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import HomeAssistant

from . import enroll_redeem

DATA_QR_STORE = "portico_enroll_qr"
TOKEN_BYTES = 32
MAX_RECORDS = 16          # at most this many remembered enrolments; the oldest are dropped (a QR nobody fetched)


@dataclass
class QrRecord:
    digest: bytes                          # sha256 of the token: the token itself is never kept
    entry_id: str                          # the phone's config entry
    enroll_id: str = field(repr=False)     # the code registry's key (never leaves the server)
    png: bytes = field(repr=False)
    created: bool = False                  # this enrolment made the entry (a re-draw of an old one did not)
    text: str = field(default="", repr=False)   # exactly what the QR encodes: the paste field's twin, same lifetime as the PNG


def _digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("utf-8", "replace")).digest()


class QrStore:
    def __init__(self) -> None:
        self._records: list[QrRecord] = []

    def put(self, hass: HomeAssistant, *, entry_id: str, enroll_id: str, png: bytes, created: bool,
            text: str = "") -> str:
        """Remember a fresh QR for `entry_id` and return its token. An earlier QR of the same phone is dead from here
        (the code it carried is). A phone this server created stays "created by us" across New code."""
        self._purge(hass)
        previous = self.record_for_entry(entry_id)
        created = created or bool(previous is not None and previous.created)
        self._records = [r for r in self._records if r.entry_id != entry_id]
        token = secrets.token_urlsafe(TOKEN_BYTES)
        self._records.append(QrRecord(_digest(token), entry_id, enroll_id, png, created, text))
        del self._records[:-MAX_RECORDS]
        return token

    def png_for(self, hass: HomeAssistant, token: str) -> bytes | None:
        """The PNG for `token`, or None (unknown, or its code is no longer live). Every record is compared."""
        self._purge(hass)
        wanted = _digest(token)
        found: bytes | None = None
        for record in self._records:
            if hmac.compare_digest(record.digest, wanted):
                found = record.png
        return found

    def text_for(self, hass: HomeAssistant, token: str) -> str | None:
        """The QR's text for `token`, or None (unknown, expired, or a record kept without text)."""
        self._purge(hass)
        wanted = _digest(token)
        found: str | None = None
        for record in self._records:
            if hmac.compare_digest(record.digest, wanted) and record.text:
                found = record.text
        return found

    def record_for_entry(self, entry_id: str) -> QrRecord | None:
        for record in self._records:
            if record.entry_id == entry_id:
                return record
        return None

    def forget(self, entry_id: str) -> None:
        """The token of `entry_id` stops working (cancel, revoke)."""
        self._records = [r for r in self._records if r.entry_id != entry_id]

    def _purge(self, hass: HomeAssistant) -> None:
        registry = enroll_redeem.get_registry(hass)
        self._records = [r for r in self._records if registry.seconds_left(r.enroll_id) is not None]

    def __len__(self) -> int:
        return len(self._records)


def get_qr_store(hass: HomeAssistant) -> QrStore:
    store: Any = hass.data.get(DATA_QR_STORE)
    if store is None:
        store = hass.data[DATA_QR_STORE] = QrStore()
    return store
