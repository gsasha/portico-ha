"""FCM HTTP v1 `messages:send`, one message per call, classified (sender
design §2.4).

HA's shared aiohttp session, a 5 s timeout per request, at most 8 requests in
flight. A 401 invalidates the cached access token and retries that send once.
The result carries Google's short status/error codes only: never the response
body (it can echo the request), the access token or the registration.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import const
from .oauth import AccessTokenSource, OAuthError

_CODE_RE = re.compile(r"^[A-Z_]{1,40}$")
_REGISTRATION_FIELDS = {"token", "fid", "message.token", "message.fid"}


class Outcome(StrEnum):
    OK = "ok"
    DEAD = "dead"  # UNREGISTERED / NOT_FOUND / INVALID_ARGUMENT naming the registration
    PAYLOAD_REJECTED = "payload_rejected"  # our bug
    PROJECT_MISMATCH = "project_mismatch"  # SENDER_ID_MISMATCH
    CREDENTIALS_INVALID = "credentials_invalid"
    QUOTA = "quota"  # 429
    TRANSIENT = "transient"  # 5xx, network, timeout


@dataclass(frozen=True, slots=True)
class SendResult:
    outcome: Outcome
    status: int | None = None
    error: str = ""
    retry_after: float | None = None


def _code(value: Any) -> str:
    return value if isinstance(value, str) and _CODE_RE.fullmatch(value) else ""


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None  # an HTTP-date: ignored, the deadline decides
    return seconds if seconds >= 0 else None


def classify(status: int, body: Any, retry_after: float | None) -> SendResult:
    """The §2.4 table."""
    if status == 200:
        return SendResult(Outcome.OK, status)
    error = body.get("error") if isinstance(body, dict) else None
    error = error if isinstance(error, dict) else {}
    rpc_status = _code(error.get("status"))
    fcm_code = ""
    fields: set[str] = set()
    for detail in error.get("details") or []:
        if not isinstance(detail, dict):
            continue
        fcm_code = fcm_code or _code(detail.get("errorCode"))
        for violation in detail.get("fieldViolations") or []:
            if isinstance(violation, dict) and isinstance(violation.get("field"), str):
                fields.add(violation["field"])
    code = fcm_code or rpc_status or f"HTTP_{status}"

    if status == 404 or fcm_code == "UNREGISTERED":
        return SendResult(Outcome.DEAD, status, code)
    if status == 400:
        if fields & _REGISTRATION_FIELDS:
            return SendResult(Outcome.DEAD, status, code)
        return SendResult(Outcome.PAYLOAD_REJECTED, status, code)
    if status == 403:
        if fcm_code == "SENDER_ID_MISMATCH":
            return SendResult(Outcome.PROJECT_MISMATCH, status, code)
        return SendResult(Outcome.CREDENTIALS_INVALID, status, code)
    if status == 401:
        return SendResult(Outcome.CREDENTIALS_INVALID, status, code)
    if status == 429:
        return SendResult(Outcome.QUOTA, status, code, retry_after)
    if status >= 500 or status == 408:
        return SendResult(Outcome.TRANSIENT, status, code, retry_after)
    return SendResult(Outcome.PAYLOAD_REJECTED, status, code)


class FcmClient:
    def __init__(
        self,
        hass: HomeAssistant,
        tokens: AccessTokenSource,
        project_id: str,
        *,
        base_url: str | None = None,
        timeout: float = 5.0,
        concurrency: int = 8,
    ) -> None:
        self._hass = hass
        self._tokens = tokens
        self._project_id = project_id
        self._base_url = (base_url or const.ENDPOINTS.fcm_base_url).rstrip("/")
        self._timeout = timeout
        self._semaphore = asyncio.Semaphore(concurrency)

    @property
    def url(self) -> str:
        return f"{self._base_url}/v1/projects/{self._project_id}/messages:send"

    async def async_send(self, message: dict[str, Any]) -> SendResult:
        async with self._semaphore:
            result = await self._attempt(message)
            if result.status == 401:
                # A stale or revoked access token: _attempt dropped it; one
                # retry with a fresh one (§2.4). A second 401 is final.
                result = await self._attempt(message)
            return result

    async def _attempt(self, message: dict[str, Any]) -> SendResult:
        try:
            token = await self._tokens.async_get_token()
        except OAuthError as err:
            outcome = Outcome.CREDENTIALS_INVALID if err.code == "credentials_invalid" else Outcome.TRANSIENT
            return SendResult(outcome, None, f"OAUTH_{err.code.upper()}")
        session = async_get_clientsession(self._hass)
        try:
            async with session.post(
                self.url,
                json=message,
                headers={"Authorization": f"Bearer {token}"},
                timeout=aiohttp.ClientTimeout(total=self._timeout),
            ) as resp:
                status = resp.status
                retry_after = _retry_after(resp.headers.get("Retry-After"))
                try:
                    body = await resp.json(content_type=None)
                except ValueError:
                    body = None
        except TimeoutError:
            return SendResult(Outcome.TRANSIENT, None, "TIMEOUT")
        except aiohttp.ClientError:
            return SendResult(Outcome.TRANSIENT, None, "NETWORK")
        if status == 401:
            self._tokens.invalidate(token)
        return classify(status, body, retry_after)
