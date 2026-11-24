"""The talkback single-session guard: a truth table and a few shapes; no network, nothing is wired to a door."""
from __future__ import annotations

import asyncio
import inspect
import json

import pytest

from custom_components.portico.door import talkback_guard as g


def _body(**kw) -> str:
    return json.dumps({"ok": True, "appAttached": False, "sessionUp": False, **kw})


@pytest.mark.parametrize("body, allowed, reason", [
    (_body(), True, g.REASON_OK),
    (_body(sessionUp=True), False, g.REASON_BUSY),
    (_body(ok=False), False, g.REASON_NOT_OK),
    (json.dumps({"ok": True}), False, g.REASON_MALFORMED),                 # no sessionUp
    (_body(sessionUp=0), False, g.REASON_MALFORMED),                      # not a bool
    (_body(sessionUp="false"), False, g.REASON_MALFORMED),
    (_body(sessionUp=None), False, g.REASON_MALFORMED),
    ("not json", False, g.REASON_MALFORMED),
    ("[]", False, g.REASON_MALFORMED),
    ("", False, g.REASON_MALFORMED),
    (None, False, g.REASON_MALFORMED),
])
def test_decide_truth_table(body, allowed, reason):
    v = g.decide(body)
    assert (v.allowed, v.reason) == (allowed, reason)


URL = "http://example.invalid/health"


async def test_an_empty_url_is_no_external_guard_and_fetches_nothing():
    """DEV-835: bell-talk is retired. No URL: allowed, nothing asked; Portico's own session is still held once."""
    asked = []

    async def fetch(url):
        asked.append(url)
        raise AssertionError("an empty guard url must not be fetched")

    assert g.DEFAULT_GUARD_URL == ""
    v = await g.check(fetch, "")
    assert v.allowed and v.reason == g.REASON_OK and asked == []
    guard = g.TalkbackGuard(fetch)
    assert (await guard.try_open()).allowed and guard.held
    second = await guard.try_open()
    assert not second.allowed and second.reason == g.REASON_BUSY and asked == []
    guard.release()
    assert (await guard.try_open()).allowed


async def test_an_unreachable_or_slow_bell_talk_refuses():
    async def boom(url):
        raise OSError("no route")

    async def slow(url):
        await asyncio.sleep(5)
        return _body()

    assert (await g.check(boom, URL)).reason == g.REASON_UNREACHABLE
    v = await g.check(slow, URL, timeout=0.05)
    assert not v.allowed and v.reason == g.REASON_UNREACHABLE


async def test_the_guard_asks_the_url_it_was_given_and_holds_one_session():
    asked = []

    async def fetch(url):
        asked.append(url)
        return _body()

    guard = g.TalkbackGuard(fetch, "http://example.invalid/health")
    first = await guard.try_open()
    assert first.allowed and guard.held and asked == ["http://example.invalid/health"]
    second = await guard.try_open()                 # Portico's own second session: refused without asking bell-talk
    assert not second.allowed and second.reason == g.REASON_BUSY and len(asked) == 1
    guard.release()
    assert not guard.held and (await guard.try_open()).allowed


async def test_a_refusal_holds_nothing():
    async def busy(url):
        return _body(sessionUp=True)

    guard = g.TalkbackGuard(busy, URL)
    assert not (await guard.try_open()).allowed and not guard.held


async def test_concurrent_opens_grant_exactly_one():
    async def fetch(url):
        await asyncio.sleep(0.01)
        return _body()

    guard = g.TalkbackGuard(fetch, URL)
    verdicts = await asyncio.gather(*(guard.try_open() for _ in range(5)))
    assert sum(v.allowed for v in verdicts) == 1


def test_the_module_has_no_network_and_no_door_services():
    src = inspect.getsource(g)
    for needle in ("import socket", "aiohttp", "urllib", "httpx", "requests", "async_call", "lock.", "open_relay", "unlock"):
        assert needle not in src, needle
