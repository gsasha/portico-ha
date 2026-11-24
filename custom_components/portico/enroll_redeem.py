"""DEV-544: the one-time enrollment code, and its redemption.

The enrollment QR no longer carries a Home Assistant token. It carries a CODE: 192 random bits, URL-safe, good for
ten minutes and for one redemption. The phone presents the code to `POST /api/portico/enroll/redeem` and gets back a
Home Assistant REFRESH token for its own user, plus a first 24-hour access token. A photographed, screenshotted or
pasted QR is therefore a credential for ten minutes at most, and only until the real phone has used it.

Transport (decided, the owner 2026-10-21 + the build): an unauthenticated HTTP view, not a websocket command.

- The phone holds no HA token yet, so there is nothing to authenticate a websocket with. HA's websocket accepts only
  an access token in its `auth` phase; a command "available before auth" does not exist, and building one would mean
  a second, hand-written pre-auth protocol on a socket that HA closes after a few seconds of silence.
- HA's own sign-in works the same way: `/auth/token` is an unauthenticated POST view (`requires_auth = False`) that
  trades a code for tokens. This view is the same shape, so the app can reach it through the same OkHttp path it
  already builds for `/auth/token` (the tailnet forwarder or the direct URL).
- `contract/app_commands_v3.txt` and `ws_v3.json` do not change: it is not a websocket command.

The rules, in the order the view applies them:

0. Only `Content-Type: application/json` is read (`415` otherwise, counted nowhere), so a cross-site "simple" POST
   from a web page never reaches the handler.
1. A throttle answers next, `429` with `Retry-After: 2`, and then NOTHING else is evaluated: no code is checked
   for a throttled request, so the throttle cannot be used to confirm a guess. There is no wall: a scope with too many
   failures in a minute admits one request every 2 seconds. Well-formed redemptions with a wrong code count toward a
   global window and the caller's address window; a malformed body counts toward its address window only (it can
   not be a guess, so it can not be used to throttle everyone). The address is the connected peer, and only when the
   request carries no X-Forwarded-For / Forwarded / X-Real-IP header; those requests, and addresses that do not fit
   in the bounded table, share one `proxied` bucket. A header is never believed (the Tailscale subnet router is a
   trusted proxy on this estate, so HA would believe a forged one), and the peer, not a header's claim, is what is
   kept as the token's last-used address. When a scope turns hot an HA repair issue says so; it clears at the next
   redemption and after an hour of quiet. Successes do not reset anything.
2. The body is read with a hard cap (640 bytes) BEFORE any hashing, must be one JSON object with exactly four string
   keys, each of a fixed shape. Anything else is the uniform failure.
3. The code is checked in constant time against HMAC-SHA256 digests under a per-process random key. The record is
   found by comparing the digest of the `enroll_id` against EVERY pending record (no early exit, no dict lookup keyed
   by the secret), and the code digest is compared once whether or not a record was found (against a random dummy),
   so an unknown id and a wrong code cost the same.
4. Unknown id, wrong code, expired, already used, burned and malformed bodies all produce the same answer: HTTP 400
   `{"error": "invalid_or_expired"}`. Nothing tells a guesser which one it was.
5. Five wrong codes against one record burn it (the installer draws a new QR). Wrong codes against an unknown id
   burn nothing; they count toward the windows.
6. Single use: the record turns `redeemed` in the same synchronous step that accepts the code, before the first
   `await`, and the whole decision runs under one lock. Two simultaneous calls cannot both win.
7. A lost response is the realistic failure (cellular, a tailnet that is still waking). For two minutes after a
   redemption, the SAME phone (same `installation_id` AND `nonce`) presenting the same code gets the same refresh
   token again, with a fresh access token. A DIFFERENT holder presenting the code in that window means someone else
   has the QR: the issued refresh token is revoked, the phone is marked `compromised`, a repair issue is raised, and
   the answer is the uniform failure. (If the thief is first, the real phone's attempt does exactly this and the
   thief's token dies.)

Nothing secret is stored: codes and ids exist in memory only as keyed digests, the registry is memory only (an HA
restart drops pending codes; the installer draws a new QR), and no log line, diagnostics file, repair issue or
persistent notice carries a code, a token, a nonce or an `enroll_id`. The only record of an outcome is a class word
and the first 4 hex characters of the SHA-256 of the `enroll_id`.

The refresh token is HA's own NORMAL token: `client_id` is `PORTICO_CLIENT_ID` (HA 2026.10's `/auth/token` accepts a
refresh grant with no client id only for a token that has none, and a NORMAL token must have one; the id must pass
`indieauth.verify_client_id`, an http(s) URL with a path), `access_token_expiration` is 24 hours. HA expires a NORMAL
refresh token 90 days after its last use (`REFRESH_TOKEN_EXPIRATION`, rolled forward at every access-token creation);
a phone unused for 90 days must be enrolled again. The app obtains access tokens with
`POST /auth/token` (`grant_type=refresh_token`, `refresh_token`, `client_id`), form-encoded.

Existing phones are untouched: their ten-year token keeps working until THAT phone is re-enrolled (same name, a new
QR); the redemption then removes the user's Portico-made long-lived tokens (`_revoke_legacy_tokens`).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import math
import re
import secrets
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from http import HTTPStatus
from typing import Any

from aiohttp import web
from homeassistant.auth.models import TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN, TOKEN_TYPE_NORMAL
from homeassistant.components import http
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from .endpoints import announce_panels
from .phone_entry import (
    REFRESH_STATE_ACTIVE,
    STATE_COMPROMISED,
    STATE_ENROLLED,
    STATE_PENDING,
    TOKEN_KIND_REFRESH,
    PhoneRecord,
    async_update_phone,
    dismiss_enroll_notification,
    phone_entries,
    phone_entry_for_user,
)

_LOGGER = logging.getLogger(__name__)

DOMAIN = "portico"
REDEEM_PATH = "/api/portico/enroll/redeem"

# The client id of every refresh token enrollment issues. Not a real site: HA never fetches it for a refresh grant, it
# only has to pass `indieauth.verify_client_id` (an http(s) URL with a path) and be sent back unchanged.
PORTICO_CLIENT_ID = "https://portico.invalid/phone"

CODE_BYTES = 24                                    # 192 bits, 32 URL-safe characters
CODE_LIFETIME = timedelta(minutes=10)
ACCESS_TOKEN_LIFETIME = timedelta(hours=24)
REPLAY_WINDOW = timedelta(minutes=2)
MAX_WRONG_CODES = 5                                # against one record
PENDING_MAX_AGE = timedelta(hours=24)              # an unredeemed phone is removed after this
SWEEP_INTERVAL = timedelta(hours=1)

# Rate limiting (design section 6), measured on the registry's clock. A THROTTLE, never a wall: once a scope has
# seen this many failures inside the window, it admits one request every THROTTLE_SECONDS and answers the rest 429
# with `Retry-After: 2`. A flood can slow enrolment to about 30 attempts a minute for everyone, never stop it, and a
# brute force is capped at the same rate on top of the 192-bit code and the 5-wrong burn.
WINDOW_SECONDS = 60.0
GLOBAL_FAILURES = 20                               # well-formed redemptions with a wrong code, from anyone
IP_FAILURES = 10                                   # failures (a malformed body included) from one address
THROTTLE_SECONDS = 2.0
MAX_TRACKED_ADDRESSES = 1024
PROXIED_KEY = "proxied"                            # the one shared bucket for requests that carry a forwarding header
                                                   # and for addresses that do not fit in the table
THROTTLE_ISSUE_AGE = 3600.0                        # the "being hammered" repair issue clears after this much quiet

# The longest body the field shapes allow is 506 bytes compact, 513 with the spaces a pretty-printing JSON library adds.
MAX_BODY_BYTES = 640
_KEEP_EXPIRED_SECONDS = 3600.0                     # memory hygiene only; the expiry decision is made at redemption

# The request fields and the shape each must have (contract/enroll_v1.json says the same).
_CODE_RE = re.compile(r"[A-Za-z0-9_-]{22,128}")
_ENROLL_ID_RE = re.compile(r"[A-Za-z0-9_-]{16,128}")          # portico-app's own rule for an enroll_id
_INSTALLATION_RE = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_NONCE_RE = re.compile(r"[A-Za-z0-9_-]{16,64}")
_FIELDS = {
    "code": _CODE_RE,
    "enroll_id": _ENROLL_ID_RE,
    "installation_id": _INSTALLATION_RE,
    "nonce": _NONCE_RE,
}

ERROR_INVALID = "invalid_or_expired"
ERROR_RATE_LIMITED = "rate_limited"
ERROR_UNSUPPORTED = "unsupported_media_type"

_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}

# Repair issues (strings.json `issues`). Each names the phone, never a secret.
ISSUE_QR_REUSED = "enroll_qr_reused"
ISSUE_CODE_BURNED = "enroll_code_burned"
ISSUE_PENDING_EXPIRED = "enroll_pending_expired"
_ALL_ISSUES = (ISSUE_QR_REUSED, ISSUE_CODE_BURNED, ISSUE_PENDING_EXPIRED)
ISSUE_THROTTLED = "enroll_endpoint_throttled"        # one issue for the endpoint, not per phone

_DATA_REGISTRY = "portico_enroll_registry"
_DATA_VIEW = "portico_enroll_redeem_view"

# Record states (in memory).
_PENDING = "pending"
_REDEEMED = "redeemed"
_BURNED = "burned"
_COMPROMISED = "compromised"


def new_code() -> str:
    """A fresh one-time code: `CODE_BYTES` random bytes, URL-safe, no padding."""
    return secrets.token_urlsafe(CODE_BYTES)


def _ref(enroll_id: str) -> str:
    """What a log line may say about an enroll id: four hex characters of its SHA-256."""
    return hashlib.sha256(enroll_id.encode("utf-8", "surrogatepass")).hexdigest()[:4]


@dataclass
class _Record:
    id_digest: bytes = field(repr=False)
    code_digest: bytes = field(repr=False)
    user_id: str
    name: str
    ref: str
    expires: float                                  # registry clock
    state: str = _PENDING
    wrong: int = 0
    holder_digest: bytes | None = field(default=None, repr=False)
    refresh_token_id: str | None = field(default=None, repr=False)
    replay_until: float = 0.0


@dataclass
class Outcome:
    """What a redemption attempt came to. `ok` carries the response body; otherwise only `kind` (for the log)."""

    ok: bool
    kind: str
    payload: dict[str, Any] | None = field(default=None, repr=False)


class FailureLimiter:
    """Failure windows and a throttle, on an injectable clock.

    Two scopes: every caller (`global`, fed only by well-formed redemptions with a wrong code, because a malformed
    body cannot be a guess at a code and costs an attacker nothing) and one address (fed by every failure). A scope
    is HOT while it holds its limit of failures inside one window. A hot scope admits one request per
    THROTTLE_SECONDS and answers the others 429; it never refuses outright, so no flood of anonymous junk can shut a
    real phone out for longer than a couple of seconds at a time. `record_failure` reports the moment a scope turns
    hot, so the caller can tell the installer.

    The address key is the connected peer, and only for a request with no forwarding header (the view decides); all
    other requests, and addresses that do not fit in the bounded table, share one `proxied` bucket."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._global: deque[float] = deque(maxlen=GLOBAL_FAILURES)
        self._global_admitted = float("-inf")
        self._ips: dict[str, deque[float]] = {}
        self._ip_admitted: dict[str, float] = {}

    @staticmethod
    def _hot(window: deque[float], limit: int, now: float) -> bool:
        return len(window) == limit and now - window[0] < WINDOW_SECONDS

    def retry_after(self, key: str) -> int | None:
        """None when the request is admitted; else the whole seconds to wait (the throttle). Admitting a request on a
        hot scope uses up that scope's slot."""
        now = self._clock()
        window = self._ips.get(key)
        scopes = []
        if self._hot(self._global, GLOBAL_FAILURES, now):
            scopes.append(("g", self._global_admitted))
        if window is not None and self._hot(window, IP_FAILURES, now):
            scopes.append(("i", self._ip_admitted.get(key, float("-inf"))))
        if not scopes:
            return None
        if any(now - admitted < THROTTLE_SECONDS for _scope, admitted in scopes):
            return math.ceil(THROTTLE_SECONDS)
        for scope, _admitted in scopes:
            if scope == "g":
                self._global_admitted = now
            else:
                self._ip_admitted[key] = now
        return None

    def record_failure(self, key: str, *, count_global: bool) -> str | None:
        """Count one failure. `count_global`: it was a well-formed redemption with a wrong code. Returns "global" or
        "address" when that scope has just turned hot, else None."""
        now = self._clock()
        tripped: str | None = None
        if count_global:
            was = self._hot(self._global, GLOBAL_FAILURES, now)
            self._global.append(now)
            if not was and self._hot(self._global, GLOBAL_FAILURES, now):
                self._global_admitted = now             # the failure that tripped it used the slot
                tripped = "global"
                _LOGGER.warning("Portico enrollment redemption: %d wrong codes within %d s from anyone, attempts are "
                                "throttled to one every %d s", GLOBAL_FAILURES, int(WINDOW_SECONDS), int(THROTTLE_SECONDS))
        window = self._ips.get(key)
        if window is None:
            if len(self._ips) >= MAX_TRACKED_ADDRESSES:
                self._prune(now)
            if len(self._ips) >= MAX_TRACKED_ADDRESSES:
                key = PROXIED_KEY                        # no room: counted in the shared bucket, never dropped
            window = self._ips.get(key)
            if window is None:
                window = self._ips[key] = deque(maxlen=IP_FAILURES)
        was = self._hot(window, IP_FAILURES, now)
        window.append(now)
        if not was and self._hot(window, IP_FAILURES, now):
            self._ip_admitted[key] = now
            tripped = tripped or "address"
            _LOGGER.warning("Portico enrollment redemption: one source failed %d times within %d s, its attempts are "
                            "throttled to one every %d s", IP_FAILURES, int(WINDOW_SECONDS), int(THROTTLE_SECONDS))
        return tripped

    def _prune(self, now: float) -> None:
        for key in [k for k, w in self._ips.items() if k != PROXIED_KEY and (not w or now - w[-1] >= WINDOW_SECONDS)]:
            self._ips.pop(key, None)
            self._ip_admitted.pop(key, None)


class EnrollRegistry:
    """The live one-time codes: in memory only, keyed digests only. `clock` is monotonic seconds and may be
    replaced (tests)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self._key = secrets.token_bytes(32)
        self._dummy = secrets.token_bytes(32)
        self._records: list[_Record] = []
        self._lock = asyncio.Lock()
        self.limiter = FailureLimiter(lambda: self.clock())
        self.last_throttled: float | None = None         # registry clock, when a scope last turned hot

    # ---- bookkeeping -----------------------------------------------------------------------------------------

    def _digest(self, tag: bytes, *values: str) -> bytes:
        message = tag + b"\0" + b"\0".join(v.encode("utf-8", "surrogatepass") for v in values)
        return hmac.new(self._key, message, hashlib.sha256).digest()

    def __len__(self) -> int:
        return len(self._records)

    def register(self, enroll_id: str, code: str, user_id: str, name: str) -> datetime:
        """Make `code` the one live code of `enroll_id` (a code it had before is dead at once). Returns when it
        expires, as a wall-clock time for the QR page."""
        id_digest = self._digest(b"id", enroll_id)
        self._records = [r for r in self._records if not hmac.compare_digest(r.id_digest, id_digest)]
        self._records.append(_Record(
            id_digest=id_digest, code_digest=self._digest(b"code", code), user_id=user_id, name=name,
            ref=_ref(enroll_id), expires=self.clock() + CODE_LIFETIME.total_seconds()))
        return dt_util.utcnow() + CODE_LIFETIME

    def seconds_left(self, enroll_id: str) -> float | None:
        """Seconds until `enroll_id`'s live code stops working, or None when it has none: no record, one already
        redeemed, burned or flagged, or one past its expiry. (The expiry itself is private and monotonic.)"""
        id_digest = self._digest(b"id", enroll_id)
        now = self.clock()
        left: float | None = None
        for record in self._records:                       # every record, no early exit (as `_redeem`)
            if hmac.compare_digest(record.id_digest, id_digest) and record.state == _PENDING and now < record.expires:
                left = record.expires - now
        return left

    def discard(self, enroll_id: str) -> None:
        """Forget `enroll_id`'s record, whatever its state (a removed phone)."""
        id_digest = self._digest(b"id", enroll_id)
        self._records = [r for r in self._records if not hmac.compare_digest(r.id_digest, id_digest)]

    def _purge(self, now: float) -> None:
        # Hygiene only. A PENDING record past its expiry is refused by the check in `_redeem`, not dropped here.
        self._records = [r for r in self._records
                         if now < (r.replay_until if r.state == _REDEEMED else r.expires + _KEEP_EXPIRED_SECONDS)]

    # ---- the redemption --------------------------------------------------------------------------------------

    async def async_redeem(self, hass: HomeAssistant, *, enroll_id: str, code: str, installation_id: str, nonce: str,
                           remote_ip: str | None = None) -> Outcome:
        async with self._lock:
            return await self._redeem(hass, enroll_id, code, installation_id, nonce, remote_ip)

    async def _redeem(self, hass: HomeAssistant, enroll_id: str, code: str, installation_id: str, nonce: str,
                      remote_ip: str | None) -> Outcome:
        now = self.clock()
        self._purge(now)
        presented_id = self._digest(b"id", enroll_id)
        found: _Record | None = None
        for record in self._records:
            if hmac.compare_digest(record.id_digest, presented_id):      # every record, no early exit
                found = record
        # One code compare whatever happened above: unknown id and wrong code cost the same.
        code_ok = hmac.compare_digest(found.code_digest if found is not None else self._dummy,
                                      self._digest(b"code", code))
        if found is None:
            return Outcome(False, "unknown")
        if not code_ok:
            if found.state == _PENDING and now < found.expires:
                found.wrong += 1
                if found.wrong >= MAX_WRONG_CODES:
                    found.state = _BURNED
                    _raise_issue(hass, ISSUE_CODE_BURNED, found)
                    return Outcome(False, "burned")
            return Outcome(False, "wrong_code")

        holder = self._digest(b"holder", installation_id, nonce)
        if found.state == _REDEEMED:
            if found.holder_digest is not None and hmac.compare_digest(found.holder_digest, holder):
                return self._replay(hass, found, remote_ip)
            return self._compromise(hass, found)
        if found.state != _PENDING:
            return Outcome(False, "used")
        if now >= found.expires:
            return Outcome(False, "expired")

        # Accepted. The state flips here, before the first await: this is the single-use gate.
        found.state = _REDEEMED
        found.holder_digest = holder
        found.replay_until = now + REPLAY_WINDOW.total_seconds()
        try:
            payload = await self._issue_tokens(hass, found, remote_ip)
        except Exception as err:  # noqa: BLE001 - the class only: a message could quote HA internals
            found.state = _BURNED
            _LOGGER.error("Portico enrollment redemption: issuing the refresh token failed (%s); the code is spent, "
                          "draw a new QR (ref %s)", type(err).__name__, found.ref)
            return Outcome(False, "error")
        return Outcome(True, "redeemed", payload)

    async def _issue_tokens(self, hass: HomeAssistant, record: _Record, remote_ip: str | None) -> dict[str, Any]:
        user = await hass.auth.async_get_user(record.user_id)
        entry = phone_entry_for_user(hass, record.user_id)
        if user is None or not user.is_active or entry is None:
            raise LookupError("the phone's user or entry is gone")
        refresh = await hass.auth.async_create_refresh_token(
            user,
            client_id=PORTICO_CLIENT_ID,
            client_name=f"portico-{entry.entry_id}-{user.id}",
            token_type=TOKEN_TYPE_NORMAL,
            access_token_expiration=ACCESS_TOKEN_LIFETIME,
        )
        try:
            access = hass.auth.async_create_access_token(refresh, remote_ip)
            record.refresh_token_id = refresh.id
            async_update_phone(hass, entry, state=STATE_ENROLLED, token_kind=TOKEN_KIND_REFRESH,
                               refresh_state=REFRESH_STATE_ACTIVE, token_expires_at=None, pending_since=None)
            announce_panels(hass, record.user_id)       # DEV-807: a re-enrolled panel's `phone_endpoint` is announced
            # Last, and only once everything that can fail has not: the phone now holds a token of its own, so
            # whatever long-lived token enrollment made for this user before DEV-544 (a phone being enrolled
            # again by name) has done its job.
            _revoke_legacy_tokens(hass, user)
        except BaseException:
            hass.auth.async_remove_refresh_token(refresh)
            raise
        _clear_issues(hass, record.user_id)
        dismiss_enroll_notification(hass, entry.entry_id)               # the "a QR is waiting" notice (enrollment_core)
        ir.async_delete_issue(hass, DOMAIN, ISSUE_THROTTLED)           # a redemption got through: not being blocked
        _LOGGER.info("Portico enrollment redemption: redeemed (ref %s)", record.ref)
        return _response(refresh.token, access)

    def _replay(self, hass: HomeAssistant, record: _Record, remote_ip: str | None) -> Outcome:
        """The same phone asking again after a lost response: the same refresh token, a fresh access token."""
        token = hass.auth.async_get_refresh_token(record.refresh_token_id) if record.refresh_token_id else None
        if token is None:
            return Outcome(False, "used")
        try:
            access = hass.auth.async_create_access_token(token, remote_ip)
        except Exception:  # noqa: BLE001
            return Outcome(False, "used")
        _LOGGER.info("Portico enrollment redemption: replayed to the same phone (ref %s)", record.ref)
        return Outcome(True, "replayed", _response(token.token, access))

    def _compromise(self, hass: HomeAssistant, record: _Record) -> Outcome:
        """The code was presented by a second holder: revoke what the first one got, flag the phone, say nothing."""
        token = hass.auth.async_get_refresh_token(record.refresh_token_id) if record.refresh_token_id else None
        if token is not None:
            hass.auth.async_remove_refresh_token(token)
        record.state = _COMPROMISED
        entry = phone_entry_for_user(hass, record.user_id)
        if entry is not None:
            async_update_phone(hass, entry, state=STATE_COMPROMISED, refresh_state="none")
        _raise_issue(hass, ISSUE_QR_REUSED, record)
        _LOGGER.warning("Portico enrollment redemption: a code was used by two different holders; the token it "
                        "issued was revoked (ref %s)", record.ref)
        return Outcome(False, "compromised")


def _response(refresh_token: str, access_token: str) -> dict[str, Any]:
    return {
        "refresh_token": refresh_token,
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": int(ACCESS_TOKEN_LIFETIME.total_seconds()),
        "client_id": PORTICO_CLIENT_ID,
    }


def _revoke_legacy_tokens(hass: HomeAssistant, user) -> None:
    """Remove the long-lived tokens enrollment made for `user` (`portico-...` client names). A long-lived token an
    administrator made by hand under another name is not touched."""
    for token in list(user.refresh_tokens.values()):
        if token.token_type == TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN and (token.client_name or "").startswith("portico-"):
            hass.auth.async_remove_refresh_token(token)


# ---- repair issues ---------------------------------------------------------------------------------------------


def _issue_id(kind: str, user_id: str) -> str:
    return f"{kind}_{user_id}"


def _raise_issue(hass: HomeAssistant, kind: str, record: _Record) -> None:
    _create_issue(hass, kind, record.user_id, record.name)


def _create_issue(hass: HomeAssistant, kind: str, user_id: str, name: str) -> None:
    ir.async_create_issue(
        hass, DOMAIN, _issue_id(kind, user_id), is_fixable=False, severity=ir.IssueSeverity.WARNING,
        translation_key=kind, translation_placeholders={"phone": name},
    )


def _clear_issues(hass: HomeAssistant, user_id: str) -> None:
    for kind in _ALL_ISSUES:
        ir.async_delete_issue(hass, DOMAIN, _issue_id(kind, user_id))


# ---- the unauthenticated view ----------------------------------------------------------------------------------


def _invalid() -> web.Response:
    return web.json_response({"error": ERROR_INVALID}, status=HTTPStatus.BAD_REQUEST, headers=_HEADERS)


def _limited(wait: int) -> web.Response:
    return web.json_response({"error": ERROR_RATE_LIMITED}, status=HTTPStatus.TOO_MANY_REQUESTS,
                             headers={**_HEADERS, "Retry-After": str(wait)})


async def _read_capped(request: web.Request) -> bytes | None:
    """The request body, or None when it is longer than `MAX_BODY_BYTES` (read no further than one byte past)."""
    if request.content_length is not None and request.content_length > MAX_BODY_BYTES:
        return None
    body = bytearray()
    async for chunk in request.content.iter_chunked(256):
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            return None
    return bytes(body)


def parse_body(raw: bytes | None) -> dict[str, str] | None:
    """The four request fields, or None for anything that is not exactly one JSON object holding them, each of its
    shape. No part of the input is ever echoed."""
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


def _unsupported() -> web.Response:
    return web.json_response({"error": ERROR_UNSUPPORTED}, status=HTTPStatus.UNSUPPORTED_MEDIA_TYPE, headers=_HEADERS)


_FORWARDING_HEADERS = ("X-Forwarded-For", "Forwarded", "X-Real-IP")


def _peer(request: web.Request) -> str | None:
    """The address of the connection itself, never one a header claimed (HA's forwarded middleware rewrites
    `request.remote` from X-Forwarded-For when the peer is a trusted proxy, and on this estate the Tailscale subnet
    router is one)."""
    transport = request.transport
    peername = transport.get_extra_info("peername") if transport is not None else None
    if isinstance(peername, (tuple, list)) and peername:
        return str(peername[0])
    return request.remote


def _limiter_key(request: web.Request, peer: str | None) -> str:
    """The per-address limiter key: the connected peer, unless the request carries a forwarding header, when it is
    the shared `proxied` key (a forged chain can then neither dodge the limiter nor lock another address out)."""
    if any(name in request.headers for name in _FORWARDING_HEADERS):
        return PROXIED_KEY
    return peer or "?"


class EnrollRedeemView(http.HomeAssistantView):
    """`POST /api/portico/enroll/redeem`. No HA login (the phone has no token yet) and, deliberately, it never calls
    HA's IP-ban helper: the phone reaches HA through a subnet router, and banning that one address would ban every
    phone. Its own limiter (FailureLimiter) answers instead. Only `application/json` is read (415 otherwise, and
    counted nowhere), so a cross-site "simple" POST from a web page cannot reach the handler: a browser must
    pre-flight a JSON body, and HA's CORS rules refuse that."""

    requires_auth = False
    url = REDEEM_PATH
    name = "api:portico:enroll:redeem"

    async def post(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app[http.KEY_HASS]
        if request.content_type != "application/json":
            return _unsupported()
        registry = get_registry(hass)
        peer = _peer(request)
        key = _limiter_key(request, peer)
        wait = registry.limiter.retry_after(key)
        if wait is not None:
            return _limited(wait)
        fields = parse_body(await _read_capped(request))
        if fields is None:
            # A malformed body is no guess at a code: it counts toward its own address only.
            self._failed(hass, registry, key, count_global=False)
            _LOGGER.info("Portico enrollment redemption: malformed request")
            return _invalid()
        outcome = await registry.async_redeem(hass, remote_ip=peer, **fields)
        if not outcome.ok or outcome.payload is None:
            self._failed(hass, registry, key, count_global=True)
            _LOGGER.info("Portico enrollment redemption: refused (%s, ref %s)", outcome.kind, _ref(fields["enroll_id"]))
            return _invalid()
        return web.json_response(outcome.payload, headers=_HEADERS)

    @staticmethod
    def _failed(hass: HomeAssistant, registry: "EnrollRegistry", key: str, *, count_global: bool) -> None:
        if registry.limiter.record_failure(key, count_global=count_global) is not None:
            registry.last_throttled = registry.clock()
            ir.async_create_issue(
                hass, DOMAIN, ISSUE_THROTTLED, is_fixable=False, severity=ir.IssueSeverity.WARNING,
                translation_key=ISSUE_THROTTLED,
            )


@callback
def get_registry(hass: HomeAssistant) -> EnrollRegistry:
    registry = hass.data.get(_DATA_REGISTRY)
    if registry is None:
        registry = hass.data[_DATA_REGISTRY] = EnrollRegistry()
    return registry


@callback
def async_register_view(hass: HomeAssistant) -> None:
    """Register the view once (HA's router accepts late routes)."""
    if hass.data.get(_DATA_VIEW):
        return
    hass.http.register_view(EnrollRedeemView())
    hass.data[_DATA_VIEW] = True


# ---- the abandoned-enrollment sweep ----------------------------------------------------------------------------


async def async_sweep_pending(hass: HomeAssistant, *, now: datetime | None = None) -> list[str]:
    """Remove every phone that has been `pending` for more than `PENDING_MAX_AGE` (its QR was never redeemed): the
    entry goes through the normal removal, which takes the user and the push rows with it, so an abandoned
    enrollment leaves no user behind. A repair issue names each. Returns the removed phones' names. A pending date
    that cannot be read leaves the phone alone.

    A `pending` phone whose HA user ALREADY holds a Portico refresh token is a phone that redeemed while its entry's
    write did not reach the disk (a crash between the two delayed saves): it works, so it is repaired to `enrolled`,
    never removed. Also clears the "endpoint is being hammered" issue once it has been quiet for an hour."""
    registry = hass.data.get(_DATA_REGISTRY)
    if registry is not None and registry.last_throttled is not None \
            and registry.clock() - registry.last_throttled >= THROTTLE_ISSUE_AGE:
        ir.async_delete_issue(hass, DOMAIN, ISSUE_THROTTLED)
        registry.last_throttled = None
    cutoff = (now or dt_util.utcnow()) - PENDING_MAX_AGE
    removed: list[str] = []
    for entry in phone_entries(hass):
        record = PhoneRecord.from_data(entry.data)
        if record.state != STATE_PENDING:
            continue
        user = await hass.auth.async_get_user(record.ha_user_id)
        if user is not None and any(
                t.token_type == TOKEN_TYPE_NORMAL and (t.client_name or "").startswith("portico-")
                for t in user.refresh_tokens.values()):
            async_update_phone(hass, entry, state=STATE_ENROLLED, token_kind=TOKEN_KIND_REFRESH,
                               refresh_state=REFRESH_STATE_ACTIVE, token_expires_at=None, pending_since=None)
            _LOGGER.warning("Portico: the phone %r redeemed its code but was still recorded as waiting; marked "
                            "enrolled", record.name)
            continue
        since = dt_util.parse_datetime(record.pending_since or record.enrolled_at)
        if since is None or since > cutoff:
            continue
        _create_issue(hass, ISSUE_PENDING_EXPIRED, record.ha_user_id, record.name)
        await hass.config_entries.async_remove(entry.entry_id)
        removed.append(record.name)
        _LOGGER.info("Portico: removed the phone %r, whose enrollment QR was never redeemed", record.name)
    return removed
