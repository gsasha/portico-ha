"""The registration string a phone shows when it cannot reach Home Assistant
(offline registration, contract `reg_string_spec.md` v1, the owner 2026-10-07).

One opaque base64url token. Decoded: `[0x01][CRC-32 big-endian of the JSON]
[compact UTF-8 JSON]`, the JSON exactly the keys `v,e,i,k,r,p,f,a` (format
version, enrollment id, installation id, kind, registration value, package,
Firebase project id, app version). No HA token, Tailscale key or URL.

The CRC catches a truncated or mistyped paste; it is not a security measure.
What makes a string usable is `e`: a random id only the QR of a phone
enrolled on THIS Home Assistant carried (push/registration.py). It is the only
credential behind this path, so diagnostics redact it.

The string is deliberately NOT signed (owner decision 2026-10-20, HA-L10: "redaction
is enough"). Accepted residual risk: a support bundle from before the redaction, or
`core.config_entries` (0644), lets someone forge a string; the admin pasting it is the
gate, and a forged string only re-points that phone's data-only rings (door ref, call
id, timing). Reopen only if a string carries more than that, the paste stops being an
admin action, or `e` is exposed elsewhere. docs/guides/push-notifications.md, "If the phone cannot reach HA".

`parse` never logs and never puts the input into an error: RegStringError
carries only a failure class (`code`), safe to log.
"""
from __future__ import annotations

import base64
import binascii
import json
import re
import zlib
from dataclasses import dataclass, field

import voluptuous as vol

from .registration import FIELDS

FORMAT_VERSION = 1
MAX_CHARS = 2048
# Leading bytes that look like a later format of this string (update HA's
# Portico) rather than random text.
_FUTURE_VERSIONS = range(2, 10)
_HEADER = 5  # version byte + CRC-32
_ALPHABET = re.compile(r"[A-Za-z0-9_-]+")
_KEYS = ("v", "e", "i", "k", "r", "p", "f", "a")
# JSON key -> the `portico/push/register` field it is (same shape check).
_REGISTER_KEYS = {"i": "installation_id", "k": "kind", "r": "registration", "p": "package",
                  "f": "fcm_project_id", "a": "app_version"}
_ENROLL_ID = vol.All(str, vol.Length(min=1, max=128))


class RegStringError(ValueError):
    """`code`: not_a_string | too_long | damaged | version | invalid."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class RegString:
    # repr=False everywhere: a parsed string shows nothing in a log or a traceback.
    enroll_id: str = field(repr=False)
    installation_id: str = field(repr=False)
    kind: str = field(repr=False)
    registration: str = field(repr=False)
    package: str = field(repr=False)
    fcm_project_id: str = field(repr=False)
    app_version: str = field(repr=False)


def _no_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate key")
        out[key] = value
    return out


def parse(text: str) -> RegString:
    """The pasted text as a RegString, or RegStringError(code)."""
    if not isinstance(text, str):
        raise RegStringError("not_a_string")
    s = "".join(text.split())  # every whitespace a messenger or text box inserted
    if len(s) > MAX_CHARS:
        raise RegStringError("too_long")
    s = s.rstrip("=").replace("+", "-").replace("/", "_")
    if not s or not _ALPHABET.fullmatch(s):
        raise RegStringError("not_a_string")
    try:
        raw = base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    except (binascii.Error, ValueError):
        raise RegStringError("not_a_string") from None
    if len(raw) <= _HEADER:
        raise RegStringError("not_a_string")
    version = raw[0]
    if version in _FUTURE_VERSIONS:
        raise RegStringError("version")
    if version != FORMAT_VERSION:
        raise RegStringError("not_a_string")
    body = raw[_HEADER:]
    if zlib.crc32(body) != int.from_bytes(raw[1:_HEADER], "big"):
        raise RegStringError("damaged")
    try:
        obj = json.loads(body.decode("utf-8"), object_pairs_hook=_no_duplicates)
    except (UnicodeDecodeError, ValueError):
        raise RegStringError("invalid") from None
    if not isinstance(obj, dict) or set(obj) != set(_KEYS):
        raise RegStringError("invalid")
    if type(obj["v"]) is not int or obj["v"] != version:
        raise RegStringError("invalid")
    try:
        enroll_id = _ENROLL_ID(obj["e"])
        values = {name: FIELDS[name](obj[key]) for key, name in _REGISTER_KEYS.items()}
    except vol.Invalid:
        raise RegStringError("invalid") from None
    return RegString(enroll_id=enroll_id, **values)
