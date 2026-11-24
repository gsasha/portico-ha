"""Google OAuth for the FCM sender: a PyJWT RS256 assertion exchanged at the
token URI (sender design §2.4), the same flow HA's own google_assistant
integration runs. PyJWT and cryptography ship with HA core, so no new
requirement (§2.1; verified against the installed HA 2026.9.4: PyJWT 2.13.0,
cryptography 48.0.1 are hard requirements of the homeassistant package).

- The token is cached until `expires_in - 300 s` and one asyncio.Lock keeps a
  single refresh in flight.
- A timer refreshes it in the background before that, so a ring never waits
  for an OAuth round trip.
- `invalidate()` drops it (FcmClient calls it on a 401 and retries once).
- Errors carry a short code (`credentials_invalid`, `transient`,
  `bad_response`) and Google's own short error code, never a response body,
  the assertion or a token.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable

import aiohttp
import jwt
from homeassistant.core import HassJob, HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later

from . import const
from .credentials import ServiceAccount

_LOGGER = logging.getLogger(__name__)

GRANT_TYPE = "urn:ietf:params:oauth:grant-type:jwt-bearer"
REFRESH_MARGIN = 300
_REFRESH_LEAD = 30  # the timer fires this long before the cache goes stale
_RETRY_AFTER_FAILURE = 60
_CREDENTIAL_ERRORS = {"invalid_grant", "unauthorized_client", "invalid_client", "access_denied", "invalid_scope"}
_CODE_RE = re.compile(r"^[A-Za-z_]{1,40}$")


class OAuthError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code} ({detail})" if detail else code)
        self.code = code
        self.detail = detail


def _short_code(value: object) -> str:
    return value if isinstance(value, str) and _CODE_RE.fullmatch(value) else ""


class AccessTokenSource:
    def __init__(
        self,
        hass: HomeAssistant,
        account: ServiceAccount,
        *,
        token_uri: str | None = None,
        clock: Callable[[], float] = time.monotonic,
        timeout: float = 10.0,
        on_error: Callable[[OAuthError], None] | None = None,
        on_success: Callable[[], None] | None = None,
    ) -> None:
        self._hass = hass
        self._account = account
        self._token_uri = token_uri or const.ENDPOINTS.token_uri
        self._clock = clock
        self._timeout = timeout
        self._on_error = on_error
        self._on_success = on_success
        self._lock = asyncio.Lock()
        self._token: str | None = None
        self._valid_until = 0.0
        self._cancel_timer: Callable[[], None] | None = None
        self._refresh_task: asyncio.Task | None = None
        self._stopped = False

    def _valid(self) -> bool:
        return self._token is not None and self._clock() < self._valid_until

    async def async_get_token(self) -> str:
        if self._valid():
            return self._token  # type: ignore[return-value]
        async with self._lock:
            if not self._valid():
                await self._async_fetch()
            return self._token  # type: ignore[return-value]

    def invalidate(self, token: str) -> None:
        if token == self._token:
            self._token = None

    async def async_start(self) -> None:
        """Prefetch so the first ring finds a token. A failure is reported
        through on_error and retried by the timer; it never raises."""
        try:
            async with self._lock:
                await self._async_fetch()
        except OAuthError:
            pass

    @callback
    def async_stop(self) -> None:
        self._stopped = True
        if self._cancel_timer is not None:
            self._cancel_timer()
            self._cancel_timer = None
        if self._refresh_task is not None and not self._refresh_task.done():
            self._refresh_task.cancel()

    def _assertion(self) -> str:
        now = int(time.time())
        claims = {
            "iss": self._account.client_email,
            "scope": const.FCM_SCOPE,
            "aud": self._token_uri,
            "iat": now,
            "exp": now + 3600,
        }
        headers = {"kid": self._account.private_key_id} if self._account.private_key_id else None
        return jwt.encode(claims, self._account.signing_key, algorithm="RS256", headers=headers)

    async def _async_fetch(self) -> None:
        try:
            await self._async_fetch_inner()
        except OAuthError as err:
            if self._on_error is not None:
                self._on_error(err)
            if err.code != "credentials_invalid":
                self._schedule(_RETRY_AFTER_FAILURE)
            raise

    async def _async_fetch_inner(self) -> None:
        session = async_get_clientsession(self._hass)
        form = {"grant_type": GRANT_TYPE, "assertion": self._assertion()}
        try:
            async with session.post(
                self._token_uri, data=form, timeout=aiohttp.ClientTimeout(total=self._timeout)
            ) as resp:
                status = resp.status
                try:
                    body = await resp.json(content_type=None)
                except ValueError:
                    body = None
        except TimeoutError:
            raise OAuthError("transient", "timeout") from None
        except aiohttp.ClientError as err:
            raise OAuthError("transient", type(err).__name__) from None

        if status == 200:
            token = body.get("access_token") if isinstance(body, dict) else None
            if not isinstance(token, str) or not token:
                raise OAuthError("bad_response", "no access_token")
            try:
                expires_in = int(body.get("expires_in", 3600))
            except (TypeError, ValueError):
                expires_in = 3600
            expires_in = max(60, min(expires_in, 3600))
            margin = min(REFRESH_MARGIN, expires_in // 2)
            self._token = token
            self._valid_until = self._clock() + expires_in - margin
            self._schedule(max(5, expires_in - margin - _REFRESH_LEAD))
            if self._on_success is not None:
                self._on_success()
            return

        code = _short_code(body.get("error")) if isinstance(body, dict) else ""
        if status in (408, 429) or status >= 500:
            raise OAuthError("transient", code or f"http_{status}")
        if 400 <= status < 500:
            # invalid_grant (key deleted/revoked), unauthorized_client, ...:
            # a person has to fix the credential (§2.4, §7).
            raise OAuthError("credentials_invalid", code or f"http_{status}")
        raise OAuthError("bad_response", f"http_{status}")

    def _schedule(self, delay: float) -> None:
        if self._stopped:
            return
        if self._cancel_timer is not None:
            self._cancel_timer()
        self._cancel_timer = async_call_later(
            self._hass, delay, HassJob(self._timer_fired, "portico push token refresh", cancel_on_shutdown=True)
        )

    @callback
    def _timer_fired(self, _now) -> None:
        self._cancel_timer = None
        if self._stopped:
            return
        self._refresh_task = self._hass.async_create_task(
            self._async_background_refresh(), "portico push token refresh", eager_start=True
        )

    async def _async_background_refresh(self) -> None:
        try:
            async with self._lock:
                await self._async_fetch()
        except OAuthError as err:
            _LOGGER.debug("portico push: background token refresh failed: %s", err.code)
