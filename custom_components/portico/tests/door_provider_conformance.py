"""The provider conformance suite (TEST-06): what every provider, the driver part's and any third party's,
must satisfy (contract/provider_v1.md). It is a module of checks, not a test file: a test module runs it

    from .provider_conformance import ProviderHarness, check_provider, CHECKS

    async def test_my_provider_passes_the_conformance_suite():
        await check_provider(make_harness)            # every check, a fresh harness each

    @pytest.mark.parametrize("check", CHECKS, ids=lambda c: c.__name__)
    async def test_conformance(check):                # or one test per check
        await check(make_harness())

A provider cannot add a door to itself, so the factory returns a ProviderHarness: the provider plus the
four levers the suite pulls (`add_door`, `remove_door`, `unload`, `ring`). `make_harness` may be sync or
async. The suite imports nothing from Portico except the dataclasses it validates a provider's dicts with
(from .. import provider); a driver part that copies this file and `fake_provider.py` replaces that one
import with its own door/transition shape checks.

THIS COPY is the driver part's (S-2): it imports nothing from the UI part, so the dataclasses are replaced by the
shape checks below (the same keys and types as the UI part's Door / CallTransition / AnswerResult). One case
differs from the UI part's copy: a driver knows no phones (they are the UI part's, STATE-01), so
`check_secrets_for_unknown_phone` covers the unknown door and the empty phone id only.
"""
from __future__ import annotations

import asyncio
import inspect
import threading
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from typing import Protocol, runtime_checkable

API_VERSION = 1
ANSWER_VERDICTS = frozenset({"answered", "taken_elsewhere", "refused", "not_available"})


@runtime_checkable
class Provider(Protocol):
    """The method names of provider_v1.md; all the registry checks."""

    API_VERSION: int
    domain: str

    def doors(self) -> list[dict]: ...
    def call_state(self, door_id: str) -> dict | None: ...
    def subscribe(self, listener: Callable[[dict], None]) -> Callable[[], None]: ...
    def subscribe_doors(self, listener: Callable[[], None]) -> Callable[[], None]: ...
    def secrets_for(self, door_id: str, phone_id: str) -> dict[str, Any] | None: ...
    async def answer(self, door_id: str, call_id: str, *, client_kind: str, claimant: str,
                     host: str = "", port: int = 0) -> dict: ...
    async def dial(self, door_id: str, host: str, port: int) -> dict[str, Any]: ...
    async def hangup(self, call_id: str, reason: str) -> dict[str, Any]: ...
    async def open_relay(self, door_id: str, context: Any = None) -> dict[str, Any]: ...
    async def keepalive(self, call_id: str) -> dict[str, Any]: ...
    async def webrtc_offer(self, call_id: str, sdp: str) -> dict[str, Any]: ...
    async def webrtc_answer(self, call_id: str, sdp: str) -> dict[str, Any]: ...
    async def webrtc_ice(self, call_id: str, candidate: str) -> dict[str, Any]: ...
    async def media_open(self, call_id: str, codec: str, on_frame: Callable[[str], None],
                         on_closed: Callable[[str], None]) -> dict[str, Any]: ...
    def media_frame(self, call_id: str, data: str) -> None: ...
    async def media_close(self, call_id: str, reason: str) -> dict[str, Any]: ...


def _typed(raw: dict, key: str, types: type | tuple, *, optional: bool = False) -> Any:
    assert key in raw, f"missing key {key!r}"
    value = raw[key]
    if value is None and optional:
        return None
    wanted = types if isinstance(types, tuple) else (types,)
    assert isinstance(value, wanted) and (bool in wanted or not isinstance(value, bool)), \
        f"key {key!r} has the wrong type {type(value).__name__}"
    return value


@dataclass
class Door:
    """The UI part's Door.from_dict, as shape checks."""

    id: str

    @classmethod
    def from_dict(cls, provider: str, raw: dict) -> "Door":
        _typed(raw, "name", str)
        _typed(raw, "driver", str)
        _typed(raw, "capabilities", dict)
        _typed(raw, "camera_entity_id", str, optional=True)
        _typed(raw, "available", bool)
        _typed(raw, "away_media", bool)
        # (the v1 `ref` key is gone: the UI part derives Door.door_ref, M6 E3; an unknown key is ignored)
        return cls(id=_typed(raw, "id", str))


@dataclass
class CallTransition:
    @classmethod
    def from_dict(cls, raw: dict) -> "CallTransition":
        _typed(raw, "door_id", str)
        _typed(raw, "call_id", str, optional=True)
        _typed(raw, "prev_state", str)
        _typed(raw, "state", str)
        _typed(raw, "prev_call_id", str, optional=True)
        _typed(raw, "at_wall", (int, float))
        return cls()


@dataclass
class AnswerResult:
    verdict: str
    mode: str | None = None
    detail: dict | None = None

    @classmethod
    def from_dict(cls, raw: dict) -> "AnswerResult":
        verdict = _typed(raw, "verdict", str)
        assert verdict in ANSWER_VERDICTS, f"unknown verdict {verdict!r}"
        mode = raw.get("mode")
        assert mode is None or isinstance(mode, str), "key 'mode' has the wrong type"
        detail = raw.get("detail") or {}
        assert isinstance(detail, dict), "key 'detail' has the wrong type"
        return cls(verdict=verdict, mode=mode, detail=dict(detail))


@dataclass
class ProviderHarness:
    provider: Any
    add_door: Callable[[str], Any]            # attach a door with this id, then notify subscribe_doors listeners
    remove_door: Callable[[str], Any]         # detach it, then notify
    unload: Callable[[], Any]                 # the provider's integration unloads: every door detaches, then notify
    ring: Callable[[str, str], Any]           # (door_id, call_id): the door starts ringing; a transition is emitted


Factory = Callable[[], "ProviderHarness | Awaitable[ProviderHarness]"]


async def _call(fn: Callable, *args: Any) -> Any:
    out = fn(*args)
    return await out if inspect.isawaitable(out) else out


async def _make(factory: Factory) -> ProviderHarness:
    return await _call(factory)


# ---- the checks -------------------------------------------------------------------------------------------

async def check_version_and_shape(h: ProviderHarness) -> None:
    p = h.provider
    assert isinstance(p, Provider), "a provider must have every protocol method"
    assert isinstance(p.API_VERSION, int) and not isinstance(p.API_VERSION, bool)
    assert p.API_VERSION == API_VERSION, f"API_VERSION {p.API_VERSION} != {API_VERSION}"
    assert isinstance(p.domain, str) and p.domain and ":" not in p.domain, "domain is an HA domain, no colon"


async def check_doors_shape(h: ProviderHarness) -> None:
    assert h.provider.doors() == [], "a fresh provider has no doors"
    await _call(h.add_door, "d1")
    raw = h.provider.doors()
    assert isinstance(raw, list) and len(raw) == 1
    door = Door.from_dict(h.provider.domain, raw[0])           # validates every key and type
    assert door.id == "d1"
    assert raw[0].get("provider", h.provider.domain) == h.provider.domain
    assert set(raw[0]) >= {"id", "provider", "name", "driver", "capabilities", "camera_entity_id",
                           "available", "away_media"}
    snap = h.provider.call_state("d1")
    assert snap is not None and snap["state"] == "idle"
    assert h.provider.call_state("no-such-door") is None


async def check_transition_subscribe_symmetry(h: ProviderHarness) -> None:
    await _call(h.add_door, "d1")
    seen: list[dict] = []
    unsub = h.provider.subscribe(seen.append)
    assert callable(unsub)
    await _call(h.ring, "d1", "c1")
    assert seen, "a ring reaches a subscribed listener"
    CallTransition.from_dict(seen[-1])
    assert seen[-1]["door_id"] == "d1" and seen[-1]["call_id"] == "c1" and seen[-1]["state"] == "ringing"
    count = len(seen)
    unsub()
    unsub()                                    # a second unsubscribe is harmless
    await _call(h.ring, "d1", "c2")
    assert len(seen) == count, "no delivery after unsubscribe"


async def check_doors_subscribe_symmetry_and_order(h: ProviderHarness) -> None:
    p = h.provider
    loop_thread = threading.get_ident()
    events: list[tuple[str, bool, int]] = []   # ("added"/"removed"-time observation of d1, thread)
    other: list[int] = []

    def listener() -> None:
        events.append(("n", any(d["id"] == "d1" for d in p.doors()), threading.get_ident()))

    def raising() -> None:
        raise RuntimeError("a listener that raises")

    unsub_raising = p.subscribe_doors(raising)
    unsub = p.subscribe_doors(listener)
    unsub_other = p.subscribe_doors(lambda: other.append(1))
    await _call(h.add_door, "d1")
    assert events and events[-1][1] is True, "on setup the provider ATTACHES first, then notifies"
    assert other, "a raising listener must not stop the others (the fake or provider isolates it)"
    await _call(h.remove_door, "d1")
    assert events[-1][1] is False, "on unload the provider DETACHES first, then notifies"
    assert all(t == loop_thread for _, _, t in events), "listeners run on the event loop"
    n = len(events)
    unsub()
    unsub()
    await _call(h.add_door, "d1")
    assert len(events) == n, "no delivery after unsubscribe"
    unsub_raising()
    unsub_other()


async def check_answer_verdicts(h: ProviderHarness) -> None:
    p = h.provider
    await _call(h.add_door, "d1")
    await _call(h.ring, "d1", "c1")
    first = await _verdict(p, "d1", "c1", "u1:c1")
    second = await _verdict(p, "d1", "c1", "u2:c2")
    assert first.verdict in ANSWER_VERDICTS and second.verdict in ANSWER_VERDICTS
    answered = [v for v in (first, second) if v.verdict == "answered"]
    assert len(answered) <= 1, "one arbitration: at most one claimant is answered"
    if first.verdict == "answered":
        assert second.verdict == "taken_elsewhere"
        assert first.mode in ("direct", "relay", None)
    unknown = await _verdict(p, "d1", "no-such-call", "u3:c3")
    assert unknown.verdict in ("not_available", "refused"), "an unknown call is never answered"


async def _verdict(p: Any, door_id: str, call_id: str, claimant: str) -> AnswerResult:
    """A verdict from the result dict; a provider may instead refuse with an exception carrying a code."""
    try:
        raw = await p.answer(door_id, call_id, client_kind="phone", claimant=claimant)
    except Exception as err:
        code = getattr(err, "code", None)
        assert isinstance(code, str) and isinstance(getattr(err, "message", None), str), \
            "a provider's exception carries `code` and `message`"
        return AnswerResult(verdict="refused", detail={"error": code})
    return AnswerResult.from_dict(raw)


async def check_secrets_for_unknown_phone(h: ProviderHarness) -> None:
    await _call(h.add_door, "d1")
    # a driver knows no phones: the UI part asks only for phones it has enrolled (PROV-07 is its gate)
    assert h.provider.secrets_for("d1", "") is None
    assert h.provider.secrets_for("no-such-door", "no-such-phone") is None


async def check_unload_empties_doors(h: ProviderHarness) -> None:
    p = h.provider
    await _call(h.add_door, "d1")
    await _call(h.add_door, "d2")
    notified: list[int] = []
    unsub = p.subscribe_doors(lambda: notified.append(len(p.doors())))
    await _call(h.unload)
    assert p.doors() == [], "unload empties doors()"
    assert notified and notified[-1] == 0, "listeners are told after the doors have gone"
    unsub()


async def check_one_door_leaves_and_comes_back(h: ProviderHarness) -> None:
    """PH-5 (R1-04): a door leaving while the provider stays: the others stay, one notification per change,
    the departed door is unknown to call_state(), and the same door can arrive again (an entry reload)."""
    p = h.provider
    await _call(h.add_door, "d1")
    await _call(h.add_door, "d2")
    seen: list[list[str]] = []
    unsub = p.subscribe_doors(lambda: seen.append(sorted(d["id"] for d in p.doors())))
    await _call(h.remove_door, "d1")
    assert seen == [["d2"]], "one notification, taken after the door had gone, the other door untouched"
    assert p.call_state("d1") is None and p.call_state("d2") is not None
    await _call(h.add_door, "d1")
    assert seen[-1] == ["d1", "d2"], "the same door id can arrive again"
    assert p.call_state("d1") is not None
    unsub()


async def check_calls_after_unload_do_not_crash_the_loop(h: ProviderHarness) -> None:
    await _call(h.add_door, "d1")
    await _call(h.unload)
    assert h.provider.call_state("d1") is None
    await asyncio.sleep(0)                      # nothing scheduled by the unload raises


CHECKS = [
    check_version_and_shape,
    check_doors_shape,
    check_transition_subscribe_symmetry,
    check_doors_subscribe_symmetry_and_order,
    check_answer_verdicts,
    check_secrets_for_unknown_phone,
    check_unload_empties_doors,
    check_one_door_leaves_and_comes_back,
    check_calls_after_unload_do_not_crash_the_loop,
]


async def check_provider(factory: Factory) -> None:
    """Run every check against a fresh harness from `factory`."""
    for check in CHECKS:
        try:
            await check(await _make(factory))
        except AssertionError as err:
            raise AssertionError(f"{check.__name__}: {err}") from err
