"""DEV-798 (SEC-02): `POST /api/portico/whoami`, Home Assistant proves itself to an enrolled phone.

Why. A phone enrolled with a private `http://` address tries it first on any network. On a hotel or cafe Wi-Fi a host
that answers at the same private address or `.local` name would be sent the phone's refresh token (`POST /auth/token`) or
access token (the websocket `auth` frame) in clear text. Recognising the network (SSID, gateway) is not proof: anyone can
name an access point "domain". What only the real Home Assistant has is the phone's own refresh token, so it can prove
itself with it, and the phone sends no token anywhere until the proof checks out.

The exchange (contract/whoami_v1.json is the shared fixture, with a test vector):

  phone -> `{"nonce": <16..64 of [A-Za-z0-9_-], fresh for every probe>, "hint": <16 hex>}`
           hint = first 8 bytes of SHA-256(refresh token), as 16 lowercase hex. It only says WHICH token; it is a hash of a
           256-bit secret, so it is no guess at the token and no way to the proof.
  HA    -> `{"proof": <64 lowercase hex>}` = HMAC-SHA256(key = the refresh token, message = "portico-whoami-v1" NUL nonce
           NUL hint). The phone computes the same from its own token and compares in constant time.
  anything else -> `400 {"error": "invalid_or_expired"}`, one answer for a malformed body, an unknown hint and a revoked
           token alike.

What the proof is and is not. It stops IMPERSONATION: a host that does not hold the token cannot produce it, and a fresh
nonce means a recorded proof is useless. It does NOT hide the token that is sent afterwards over `http://` from someone
who can read the real LAN's traffic; only `https://` or the tailnet does that. It needs no new secret, no storage and no
enrollment change: the token is the phone's own, and removing the phone (which removes its refresh token) ends the proof.
A phone enrolled the old format-1 way holds a long-lived access token, which cannot be reproduced from its refresh-token
record, so it has no proof and the app never sends it over a cleartext path it cannot verify.

No login (the phone is asking before it trusts the server with one) and no IP-ban helper, for the reason
`enroll_redeem.EnrollRedeemView` gives. It has no failure limiter of its own, deliberately: nothing here can be guessed
(the hint is a hash, the proof needs the token), and sharing the enrollment limiter would let one phone whose token was
revoked slow every enrollment that comes through the same subnet router.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
from http import HTTPStatus

from aiohttp import web
from homeassistant.auth.models import TOKEN_TYPE_NORMAL
from homeassistant.components import http
from homeassistant.core import HomeAssistant, callback

from .enroll_redeem import PORTICO_CLIENT_ID, _HEADERS, _invalid, _read_capped, _unsupported
from .phone_entry import PhoneRecord, phone_entries

WHOAMI_PATH = "/api/portico/whoami"

_NONCE_RE = re.compile(r"[A-Za-z0-9_-]{16,64}")
_HINT_RE = re.compile(r"[0-9a-f]{16}")
_FIELDS = {"nonce": _NONCE_RE, "hint": _HINT_RE}
_DOMAIN_TAG = b"portico-whoami-v1"

_DATA_VIEW = "portico_whoami_view"


def hint_of(refresh_token: str) -> str:
    """Which token this is, without being the token: 8 bytes of its SHA-256, as 16 lowercase hex characters."""
    return hashlib.sha256(refresh_token.encode("utf-8")).digest()[:8].hex()


def proof_of(refresh_token: str, nonce: str, hint: str) -> str:
    """The proof for `nonce` and `hint`, 64 lowercase hex characters. The phone computes exactly this."""
    message = _DOMAIN_TAG + b"\0" + nonce.encode("ascii") + b"\0" + hint.encode("ascii")
    return hmac.new(refresh_token.encode("utf-8"), message, hashlib.sha256).hexdigest()


def parse_body(raw: bytes | None) -> dict[str, str] | None:
    """`{"nonce", "hint"}` and nothing else, each of its shape, or None. No part of the input is ever echoed."""
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict) or set(data) != set(_FIELDS):
        return None
    for key, pattern in _FIELDS.items():
        value = data[key]
        if not isinstance(value, str) or not pattern.fullmatch(value):
            return None
    return {key: data[key] for key in _FIELDS}


async def _phone_refresh_tokens(hass: HomeAssistant) -> list[str]:
    """The refresh token of every enrolled phone (the ones redemption issued: `PORTICO_CLIENT_ID`, a normal token)."""
    tokens: list[str] = []
    for entry in phone_entries(hass):
        record = PhoneRecord.from_data(entry.data)
        user = await hass.auth.async_get_user(record.ha_user_id)
        if user is None or not user.is_active:
            continue
        tokens.extend(t.token for t in user.refresh_tokens.values()
                      if t.token_type == TOKEN_TYPE_NORMAL and t.client_id == PORTICO_CLIENT_ID)
    return tokens


class WhoamiView(http.HomeAssistantView):
    """`POST /api/portico/whoami`. Only `application/json` is read (415 otherwise), so a cross-site "simple" POST from a
    web page cannot reach the handler."""

    requires_auth = False
    url = WHOAMI_PATH
    name = "api:portico:whoami"

    async def post(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app[http.KEY_HASS]
        if request.content_type != "application/json":
            return _unsupported()
        fields = parse_body(await _read_capped(request))
        if fields is None:
            return _invalid()
        proof: str | None = None
        for token in await _phone_refresh_tokens(hass):           # every token, no early exit
            if hmac.compare_digest(hint_of(token), fields["hint"]):
                proof = proof_of(token, fields["nonce"], fields["hint"])
        if proof is None:
            return _invalid()
        return web.json_response({"proof": proof}, status=HTTPStatus.OK, headers=_HEADERS)


@callback
def async_register_view(hass: HomeAssistant) -> None:
    """Register the view once (HA's router accepts late routes)."""
    if hass.data.get(_DATA_VIEW):
        return
    hass.http.register_view(WhoamiView())
    hass.data[_DATA_VIEW] = True
