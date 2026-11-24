"""The "approve in Tailscale" enrollment mode (DEV-556): Tailscale's OAuth
device provisioning (https://tailscale.com/docs/features/oauth-apps/device-provisioning,
alpha).

The installer registers, once, a Tailscale OAuth app with the
`auth_keys:create:once` scope and Portico's callback URL as its redirect URI.
For each phone the enrollment flow sends the HA admin to Tailscale's consent
screen; Tailscale redirects back to `CALLBACK_PATH` with a `code` and the
`state` Portico sent; the flow exchanges the code (with the app's client
secret) for an access token, which is itself a single-use, one-hour auth key
that goes into the phone's QR.

Why a view of our own: HA's stock OAuth callback (`/auth/external/callback`,
helpers/config_entry_oauth2_flow.py `OAuth2AuthorizeCallbackView`) resumes
only CONFIG flows (`hass.config_entries.flow.async_configure`), and
my.home-assistant.io/redirect/oauth sends the browser to that same fixed
path. Enrollment is an OPTIONS flow, so neither can return to it.

The view needs no HA login: it is a top-level browser navigation from
Tailscale, and HA's auth is a bearer token, not a cookie. Its only power is
to hand a code to the one flow that issued the `state`:

- `state` is 32 random bytes (`secrets.token_urlsafe`), bound to exactly one
  flow id, single-use, valid for `STATE_TTL_SECONDS`, and looked up by
  comparing SHA-256 digests with `hmac.compare_digest` against every live
  record (no early exit, no dict lookup keyed by the secret).
- An unknown, expired, replayed or malformed state returns a fixed plain-text
  400 page and touches nothing. Nothing from the query is ever echoed or
  logged; responses are text/plain, `no-store`, `no-referrer`.
- The view never redirects and never fetches a URL; the token endpoint and
  the redirect URI sent to it are constants/stored values, never taken from
  the request.
- The view only records the result on the flow's record; the token exchange
  runs in the admin's next (authenticated) flow request, and the flow ignores
  any user_input on the approval step, so the flow API cannot inject a code.

The OAuth app's client secret lives only in a 0600 private Store per entry
(`.storage/portico.tailscale_oauth_app.<entry_id>`), like the push service
account (push/credentials.py); entry data holds the client id and the
registered redirect URI only.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any
from urllib.parse import urlencode

import aiohttp
from aiohttp import web
from homeassistant.components import http
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import UnknownFlow
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers.storage import Store

_LOGGER = logging.getLogger(__name__)

AUTHORIZE_URL = "https://login.tailscale.com/a/oauth_authorize"
TOKEN_URL = "https://api.tailscale.com/api/v2/oauth/token"
OAUTH_APPS_URL = "https://api.tailscale.com/api/v2/tailnet/-/oauth-apps"
SCOPE = "auth_keys:create:once"
CALLBACK_PATH = "/api/portico/tailscale/callback"
OAUTH_APP_NAME = "Portico (Home Assistant)"

STORE_KEY_PREFIX = "portico.tailscale_oauth_app."
STATE_TTL_SECONDS = 600
HTTP_TIMEOUT_SECONDS = 30

_DATA_REGISTRY = "portico_tailscale_approvals"
_DATA_VIEW = "portico_tailscale_callback_view"

_SECRET_PREFIX = "tskey-app-"
_CLIENT_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
_SECRET_RE = re.compile(r"tskey-app-[A-Za-z0-9_-]{3,1024}")
_STATE_RE = re.compile(r"[A-Za-z0-9_-]{16,256}")
_CODE_RE = re.compile(r"[\x21-\x7e]{1,2048}")
_TOKEN_RE = re.compile(r"[\x21-\x7e]{1,1024}")

# What a failed exchange or app creation can raise besides a 4xx rejection.
HTTP_ERRORS = (aiohttp.ClientError, TimeoutError, ValueError, KeyError, TypeError)


def _monotonic() -> float:
    return time.monotonic()


def parse_client_secret(secret: str, client_id: str | None = None) -> tuple[str, str]:
    """(client id, secret) from an OAuth app's `tskey-app-<clientID>-<secret>`.

    With no client id given it is the part up to the first dash after the
    prefix; a given client id must be what the secret embeds (it may itself
    contain a dash). ValueError("client_secret"/"client_id") otherwise; the
    message never carries the value."""
    secret = (secret or "").strip()
    if not _SECRET_RE.fullmatch(secret):
        raise ValueError("client_secret")
    rest = secret[len(_SECRET_PREFIX):]
    client_id = (client_id or "").strip()
    if client_id:
        if not _CLIENT_ID_RE.fullmatch(client_id):
            raise ValueError("client_id")
        if not rest.startswith(client_id + "-") or len(rest) == len(client_id) + 1:
            raise ValueError("client_id")
        return client_id, secret
    found, dash, tail = rest.partition("-")
    if not dash or not found or not tail:
        raise ValueError("client_secret")
    return found, secret


def authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
    return AUTHORIZE_URL + "?" + urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": SCOPE,
            "state": state,
            "response_type": "code",
        }
    )


class OAuthAppStore:
    """The OAuth app's client secret, in a 0600 private Store per entry."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store[dict] = Store(
            hass, 1, f"{STORE_KEY_PREFIX}{entry_id}", private=True, atomic_writes=True
        )

    async def async_load_secret(self) -> str | None:
        data = await self._store.async_load()
        secret = data.get("client_secret") if isinstance(data, dict) else None
        return secret if isinstance(secret, str) and secret.strip() else None

    async def async_save_secret(self, secret: str) -> None:
        await self._store.async_save({"client_secret": secret})

    async def async_remove(self) -> None:
        await self._store.async_remove()


# --- the state registry ---------------------------------------------------


@dataclass
class ApprovalRecord:
    """One issued `state`, bound to one flow. `result` is set once, by the
    callback view: ("code", code) or ("error", "access_denied" | "error")."""

    flow_id: str
    digest: bytes = field(repr=False)
    expires: float
    result: tuple[str, str] | None = field(default=None, repr=False)


def _digest(state: str) -> bytes:
    return hashlib.sha256(state.encode("utf-8", "surrogatepass")).digest()


class ApprovalRegistry:
    """Live approval states (in memory: a flow does not survive a restart
    either). No timer: expired records are purged on every access."""

    def __init__(self) -> None:
        self._records: list[ApprovalRecord] = []

    def __len__(self) -> int:
        self._purge()
        return len(self._records)

    def _purge(self) -> None:
        now = _monotonic()
        self._records = [r for r in self._records if r.expires > now]

    def issue(self, flow_id: str) -> tuple[str, ApprovalRecord]:
        """A fresh state for `flow_id`; any earlier one of that flow is revoked."""
        self.revoke_flow(flow_id)
        state = secrets.token_urlsafe(32)
        record = ApprovalRecord(flow_id, _digest(state), _monotonic() + STATE_TTL_SECONDS)
        self._records.append(record)
        return state, record

    def consume(self, state: str) -> ApprovalRecord | None:
        """The live record for `state`, removed (single use); None otherwise."""
        self._purge()
        digest = _digest(state)
        found: ApprovalRecord | None = None
        for record in self._records:
            # Every record is compared, a match does not stop the loop.
            if hmac.compare_digest(record.digest, digest):
                found = record
        if found is not None:
            self._records.remove(found)
        return found

    def revoke_flow(self, flow_id: str) -> None:
        self._records = [r for r in self._records if r.flow_id != flow_id]


@callback
def get_registry(hass: HomeAssistant) -> ApprovalRegistry:
    registry = hass.data.get(_DATA_REGISTRY)
    if registry is None:
        registry = hass.data[_DATA_REGISTRY] = ApprovalRegistry()
    return registry


@callback
def async_register_view(hass: HomeAssistant) -> None:
    """Register the callback view once (HA's router accepts late routes)."""
    if hass.data.get(_DATA_VIEW):
        return
    hass.http.register_view(TailscaleCallbackView())
    hass.data[_DATA_VIEW] = True


# --- the callback view ----------------------------------------------------

_PAGE_UNKNOWN = (
    "This Tailscale approval link is unknown, expired or already used, so it was ignored. "
    "Close this tab, close the Portico dialog in Home Assistant and enroll the phone again."
)
_PAGE_DENIED_NO_STATE = (
    "Tailscale reports the approval was declined. Close this tab and the Portico dialog in "
    "Home Assistant, then enroll the phone again or choose another Tailscale mode."
)
_PAGE_FLOW_GONE = (
    "The Portico dialog this approval belonged to is closed. Close this tab and enroll the "
    "phone again."
)
_PAGE_DONE = "Done. Close this tab and return to Home Assistant to finish enrolling the phone."
_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


def _page(text: str, status: HTTPStatus = HTTPStatus.OK) -> web.Response:
    return web.Response(text=text, status=status, content_type="text/plain", headers=_HEADERS)


class TailscaleCallbackView(http.HomeAssistantView):
    """Tailscale's redirect target. See the module docstring for its rules."""

    requires_auth = False
    url = CALLBACK_PATH
    name = "api:portico:tailscale:callback"

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app[http.KEY_HASS]
        query = request.query
        state = query.get("state", "")
        if not _STATE_RE.fullmatch(state):
            if "error" in query and not state:
                return _page(_PAGE_DENIED_NO_STATE, HTTPStatus.BAD_REQUEST)
            return _page(_PAGE_UNKNOWN, HTTPStatus.BAD_REQUEST)
        registry: ApprovalRegistry | None = hass.data.get(_DATA_REGISTRY)
        record = registry.consume(state) if registry is not None else None
        if record is None:
            return _page(_PAGE_UNKNOWN, HTTPStatus.BAD_REQUEST)

        # The state is spent from here on, whatever the rest says.
        code = query.get("code", "")
        if "error" in query:
            record.result = ("error", "access_denied" if query["error"] == "access_denied" else "error")
        elif _CODE_RE.fullmatch(code):
            record.result = ("code", code)
        else:
            record.result = ("error", "error")
        try:
            await hass.config_entries.options.async_configure(record.flow_id, {})
        except UnknownFlow:
            return _page(_PAGE_FLOW_GONE, HTTPStatus.GONE)
        _LOGGER.debug("Portico: Tailscale approval callback resumed an enrollment flow")
        return _page(_PAGE_DONE)


# --- calls to Tailscale -----------------------------------------------------


class TailscaleRejected(Exception):
    """Tailscale answered 4xx: the OAuth app (or admin token) is wrong."""

    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status


async def async_exchange_code(
    hass: HomeAssistant, *, client_id: str, client_secret: str, code: str, redirect_uri: str
) -> str:
    """POST /api/v2/oauth/token: the code for an access token, which is a
    single-use auth key. TailscaleRejected on a 4xx; HTTP_ERRORS otherwise.
    Nothing here logs; callers log the exception class and status only."""
    session = aiohttp_client.async_get_clientsession(hass)
    async with session.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
        },
        timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS),
    ) as resp:
        if 400 <= resp.status < 500:
            raise TailscaleRejected(resp.status)
        resp.raise_for_status()
        body: Any = await resp.json(content_type=None)
    token = body["access_token"]
    if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
        raise ValueError("Tailscale returned no usable access token")
    return token


async def async_create_oauth_app(
    hass: HomeAssistant, *, api_access_token: str, redirect_uri: str
) -> tuple[str, str | None]:
    """POST /api/v2/tailnet/-/oauth-apps with an admin API access token
    (HTTP Basic, the token as user name, as the doc's `curl -u "<token>:"`).
    Returns the app's client secret (`tskey-app-...`) and its `id` when the
    secret embeds it (else None: the id is then read from the secret). The
    token is used for this one request and kept nowhere."""
    session = aiohttp_client.async_get_clientsession(hass)
    auth = base64.b64encode(f"{api_access_token}:".encode()).decode("ascii")
    async with session.post(
        OAUTH_APPS_URL,
        json={"name": OAUTH_APP_NAME, "redirectUris": [redirect_uri], "scopes": [SCOPE]},
        headers={"Authorization": f"Basic {auth}"},
        timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS),
    ) as resp:
        if resp.status in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
            raise TailscaleRejected(resp.status)
        resp.raise_for_status()
        body: Any = await resp.json(content_type=None)
    secret = body["clientSecret"]
    if not isinstance(secret, str):
        raise TypeError("no client secret")
    app_id = body.get("id")
    if isinstance(app_id, str) and secret.strip().startswith(f"{_SECRET_PREFIX}{app_id}-"):
        return secret, app_id
    return secret, None


async def async_remove_entry_store(hass: HomeAssistant, entry_id: str) -> None:
    await OAuthAppStore(hass, entry_id).async_remove()
