"""The single-session guard of the front door's talkback (portico_front_door_talk_plan.md, section 6).

The doorbell camera has ONE talkback sink, and an armed session is suspected of killing its call button (go2rtc #1760;
uiprotect #447). The old relay, `bell-talk`, publishes whether it holds a session at `GET /health` (`sessionUp`). Before
Portico opens a session it asks, and it opens only on a clear "no session": every other answer (unreachable, slow,
malformed, a field of the wrong type, bell-talk itself reporting not ok) is a refusal. A guess here is how the call
button dies quietly.

This module is pure decision plus a thin, injected fetcher. It is not wired to any door, it opens no socket on import,
and nothing in the repository calls it yet; the Go driver PRs and the wiring PR decide when it runs. The check-then-open
window is not atomic and bell-talk cannot see Portico: the plan records that residual race.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

DEFAULT_GUARD_URL = ""      # empty = no external guard (DEV-835): the old bell-talk relay is retired
GUARD_TIMEOUT = 3.0

# Why a session may not be opened. `busy` is the one that matters; the rest are "cannot tell", which refuses too.
REASON_OK = "ok"
REASON_BUSY = "busy"
REASON_UNREACHABLE = "unreachable"
REASON_MALFORMED = "malformed"
REASON_NOT_OK = "not_ok"

# A fetcher returns the response body of the health URL as text, or raises (any exception means "unreachable").
Fetcher = Callable[[str], Awaitable[str]]


@dataclass(frozen=True)
class GuardVerdict:
    allowed: bool
    reason: str
    detail: str = ""


def decide(body: str) -> GuardVerdict:
    """The truth table, from bell-talk's /health body. Allowed only for a JSON object with `ok` true and `sessionUp`
    exactly false (a bool: 0, "false" and null are not "no session")."""
    try:
        data: Any = json.loads(body)
    except (TypeError, ValueError):
        return GuardVerdict(False, REASON_MALFORMED, "health body is not JSON")
    if not isinstance(data, dict):
        return GuardVerdict(False, REASON_MALFORMED, "health body is not an object")
    if data.get("ok") is not True:
        return GuardVerdict(False, REASON_NOT_OK, "bell-talk does not report ok")
    session_up = data.get("sessionUp")
    if not isinstance(session_up, bool):
        return GuardVerdict(False, REASON_MALFORMED, "sessionUp is missing or not a boolean")
    if session_up:
        return GuardVerdict(False, REASON_BUSY, "bell-talk holds the talkback session")
    return GuardVerdict(True, REASON_OK)


async def check(fetch: Fetcher, url: str = DEFAULT_GUARD_URL, *, timeout: float = GUARD_TIMEOUT) -> GuardVerdict:
    """Ask bell-talk, through the injected fetcher, whether a session may be opened. Never raises. An empty url means
    "no external guard" (bell-talk retired): allowed, nothing is fetched."""
    if not url:
        return GuardVerdict(True, REASON_OK, "no external guard configured")
    try:
        body = await asyncio.wait_for(fetch(url), timeout)
    except Exception as err:   # noqa: BLE001 - any failure to read is "cannot tell", which refuses
        return GuardVerdict(False, REASON_UNREACHABLE, type(err).__name__)
    return decide(body)


class TalkbackGuard:
    """One door's guard: its own open session plus bell-talk's. `try_open` is the only way in; `release` the only way
    out. Portico's own second session is refused without asking anyone."""

    def __init__(self, fetch: Fetcher, url: str = DEFAULT_GUARD_URL, *, timeout: float = GUARD_TIMEOUT) -> None:
        self._fetch = fetch
        self._url = url
        self._timeout = timeout
        self._held = False
        self._lock = asyncio.Lock()

    @property
    def held(self) -> bool:
        return self._held

    async def try_open(self) -> GuardVerdict:
        async with self._lock:
            if self._held:
                return GuardVerdict(False, REASON_BUSY, "Portico already holds the talkback session")
            verdict = await check(self._fetch, self._url, timeout=self._timeout)
            if verdict.allowed:
                self._held = True
            return verdict

    def release(self) -> None:
        self._held = False
