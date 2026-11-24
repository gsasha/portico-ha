"""RingNotifier: call transitions -> push messages (sender design §2.2,
§2.4, §2.5).

- `→ ringing` with a new call id: a ring to every registration that is
  switched on (a new phone starts per the entry's "New phones get the away
  ring" option) and live.
- `ringing → answered|active`: `answered`, only to phones whose ring Google
  accepted.
- `→ ended`: `ended`, only to phones whose ring was accepted and that did not
  already get an accepted `answered` (the app ignores an `ended` after an
  `answered`, and a HIGH message with nothing visible counts against the
  phone, §4.2).
- `answered → ringing` (a released claim): nothing.
- A `ringing` for a new call that replaces a tracked one without an `ended`:
  `ended` for the replaced call, then the new ring.

Per registration the messages of one call are chained (ring, then answered,
then ended), so a dismiss never overtakes its ring. A phone whose WebSocket
is live still gets the push; the app de-duplicates (decision 7).

Caps: at most one of each kind per call per registration; new ring pushes
per device entry at most 1 per 5 s and 20 per hour; at most 60 messages a
minute in total, past which a 5-minute circuit opens. Capped rings still ring
over the WebSocket; only the push is skipped.

Retries are bounded by the message's deadline, never a count: a ring's is
`ring_window - 5 s` from the transition, a dismiss's 60 s; backoff 0.5, 1, 2,
4, 4... s; `Retry-After` is honoured only when it fits; a 429 without one is
dropped (Google asks for at least a minute).

Every send runs in its own background task; nothing here can raise into the
tracker or delay the WebSocket ring.

House calls (M7 3c, design 15.5). A SECOND, separate path (`house_ring`, `house_answered`, `house_left`, `house_ended`),
called only by the house-call registry and never by `handle_transition`: a house call id (`hso-...`) never enters
`self._calls`, `_finished` or the door rate-limit keys, and a door ring never sends `ct: house`. It builds
`ct: house` messages with an opaque `from_ref` and no `door_ref` for the phones the registry names as AWAY (receive on,
no open subscription), through the same fan-out, chain, retry, per-callee cap (key `house:<endpoint>`) and global
circuit as a door ring. A callee that answered, declined or went away is "quiet": it is sent nothing more.

The staged test (spec §8.3, G6): while the test allow-list is set, a ring
goes only to switched-on phones on it. `async_send_test` sends one `test`
message per allow-listed phone the caller passes, with no retry, at most one
call per 10 s and 10 an hour, each message taking a global slot.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any, Protocol

from homeassistant.core import HomeAssistant, callback

from ..provider import CallTransition
from . import payload
from .fcm import FcmClient, Outcome, SendResult
from .registry import STATE_PROJECT_MISMATCH, PushRegistry

_LOGGER = logging.getLogger(__name__)

BACKOFF = (0.5, 1.0, 2.0, 4.0)
RING_DEADLINE_MARGIN = 5.0
DISMISS_DEADLINE = 60.0
DEVICE_MIN_SPACING = 5.0
DEVICE_PER_HOUR = 20
GLOBAL_PER_MINUTE = 60
CIRCUIT_SECONDS = 300.0
CALL_MEMORY_SECONDS = 300.0
TEST_MIN_SPACING = 10.0
TEST_PER_HOUR = 10
_FINISHED_MEMORY = 256


class NotifierEvents(Protocol):
    def on_result(self, row: dict, kind: str, call_id: str | None, result: SendResult) -> None: ...
    def on_skip(self, reason: str, door_ref: str | None, call_id: str | None) -> None: ...
    def on_circuit(self, is_open: bool) -> None: ...
    def on_summary(self, kind: str, call_id: str, counts: dict[str, int]) -> None: ...


class _NoEvents:
    def on_result(self, *args: Any) -> None:
        pass

    def on_skip(self, *args: Any) -> None:
        pass

    def on_circuit(self, *args: Any) -> None:
        pass

    def on_summary(self, *args: Any) -> None:
        pass


@dataclass
class _Call:
    door_ref: str
    call_id: str
    created: float
    ring_deadline: float
    targets: list[str] = field(default_factory=list)
    delivered: set[str] = field(default_factory=set)
    answered_ok: set[str] = field(default_factory=set)
    sent: dict[str, set[str]] = field(default_factory=dict)
    chain: dict[str, asyncio.Task] = field(default_factory=dict)
    stop_ring_retries: bool = False
    answered_queued: bool = False
    # House calls only (3c); empty and False on a door call, so the door path reads exactly what it did.
    house: bool = False
    from_ref: str | None = None
    endpoints: dict[str, list[str]] = field(default_factory=dict)   # callee endpoint id -> its registration ids
    quiet: set[str] = field(default_factory=set)                     # registrations that are sent nothing more


class RingNotifier:
    def __init__(
        self,
        hass: HomeAssistant,
        registry: PushRegistry,
        client: FcmClient,
        *,
        ring_window_s: Callable[[], int],
        enabled: Callable[[], bool],
        events: NotifierEvents | None = None,
        test_phones: Callable[[], list[str]] | None = None,
        wire_id: Callable[[str], str | None] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        sleep: Callable[[float], Coroutine[Any, Any, None]] = asyncio.sleep,
    ) -> None:
        self._hass = hass
        self._registry = registry
        self._client = client
        self._ring_window = ring_window_s
        self._enabled = enabled
        self._events = events or _NoEvents()
        self._test_phones = test_phones or list
        # door id -> the id the push wire carries as `door_ref` (`Door.door_ref`, push/v2); identity by default
        self._wire_id = wire_id or (lambda door_id: door_id)
        self._test_sends: deque[float] = deque()
        self._mono = monotonic
        self._wall = wall
        self._sleep = sleep
        self._calls: dict[str, _Call] = {}
        self._finished: deque[str] = deque(maxlen=_FINISHED_MEMORY)
        self._house_calls: dict[str, _Call] = {}
        self._house_finished: deque[str] = deque(maxlen=_FINISHED_MEMORY)
        self._device_rings: dict[str, deque[float]] = {}
        self._global: deque[float] = deque()
        self._circuit_until: float | None = None
        self._tasks: set[asyncio.Task] = set()
        self._stopped = False

    # ---- transitions -------------------------------------------------------

    @callback
    def handle_transition(self, t: CallTransition) -> None:
        try:
            self._handle(t)
        except Exception:  # noqa: BLE001 - never into the tracker
            _LOGGER.exception("portico push: failed to handle a call transition")

    def _handle(self, t: CallTransition) -> None:
        if self._stopped or not t.call_id:
            return
        self._expire_calls()
        if not self._enabled():
            return
        wall_ms = int(t.at_wall * 1000)
        if t.state == "ringing":
            if t.prev_call_id and t.prev_call_id != t.call_id and t.prev_call_id in self._calls:
                self._finish(t.prev_call_id, wall_ms)
            if t.call_id in self._calls or t.call_id in self._finished:
                return  # a repeat, or a released claim (answered -> ringing)
            self._start_ring(t.door_id, t.call_id, wall_ms)
        elif t.state in ("answered", "active"):
            call = self._calls.get(t.call_id)
            if call is None or call.answered_queued:
                return
            call.answered_queued = True
            call.stop_ring_retries = True
            _LOGGER.info("portico push: call %s %s %d ms after the ring (first answer)", call.call_id, t.state,
                         int((self._mono() - call.created) * 1000))
            data = payload.build_answered(call.door_ref, call.call_id, wall_ms)
            self._fan_out(call, payload.KIND_ANSWERED, data)
        elif t.state == "ended":
            self._finish(t.call_id, wall_ms)

    def _finish(self, call_id: str, wall_ms: int) -> None:
        self._finished.append(call_id)
        call = self._calls.pop(call_id, None)
        if call is None:
            return
        call.stop_ring_retries = True
        _LOGGER.info("portico push: call %s ended %d ms after the ring (answered=%s)", call_id,
                     int((self._mono() - call.created) * 1000), call.answered_queued)
        self._fan_out(call, payload.KIND_ENDED, payload.build_ended(call.door_ref, call.call_id, wall_ms))

    def _expire_calls(self) -> None:
        now = self._mono()
        for call_id, call in list(self._calls.items()):
            if now - call.created > CALL_MEMORY_SECONDS:
                del self._calls[call_id]
                self._finished.append(call_id)
        for call_id, call in list(self._house_calls.items()):
            if now - call.created > CALL_MEMORY_SECONDS:
                del self._house_calls[call_id]
                self._house_finished.append(call_id)

    def _device_allows(self, door_id: str) -> bool:
        now = self._mono()
        times = self._device_rings.setdefault(door_id, deque())
        while times and now - times[0] > 3600:
            times.popleft()
        if times and now - times[-1] < DEVICE_MIN_SPACING:
            return False
        if len(times) >= DEVICE_PER_HOUR:
            return False
        times.append(now)
        return True

    def _start_ring(self, door_id: str, call_id: str, wall_ms: int) -> None:
        door_ref = self._wire_id(door_id)
        if not door_ref:
            return   # a door the wire has no ref for (not in the registry): ignored
        targets = self._registry.targets()
        allow = set(self._test_phones())
        if allow:  # a staged test is on: never ring a phone that is not on the list
            targets = [row for row in targets if row["reg_id"] in allow]
        targets = self._panel_filtered(door_id, targets)
        if not targets:
            return
        if not self._device_allows(door_id):
            self._events.on_skip("device_rate", door_ref, call_id)
            _LOGGER.info("portico push: ring for call %s skipped by the per-device cap", call_id)
            return
        window = int(self._ring_window())
        data = payload.build_ring(door_ref, call_id, wall_ms, window)
        call = _Call(door_ref, call_id, self._mono(), self._mono() + window - RING_DEADLINE_MARGIN)
        call.targets = [row["reg_id"] for row in targets]
        self._calls[call_id] = call
        self._note_panel_rings(door_id, call_id, targets)
        _LOGGER.info("portico push: ring for call %s to %d registration(s)", call_id, len(targets))
        self._fan_out(call, payload.KIND_RING, data)

    def _panel_filtered(self, door_id: str, targets: list[dict]) -> list[dict]:
        """DEV-807 item 8: a panel's registration is skipped for a door outside its screen's `wake` (door rings only; a house
        push never comes through here). A panel with no screen or no `wake` keeps every door. Fails open."""
        try:
            from ..endpoints import panel_door_push_allowed

            return [row for row in targets if panel_door_push_allowed(self._hass, row.get("ha_user_id"), door_id)]
        except Exception:  # noqa: BLE001
            _LOGGER.debug("portico push: the panel push filter failed open", exc_info=True)
            return targets

    def _note_panel_rings(self, door_id: str, call_id: str, targets: list[dict]) -> None:
        """DEV-807: tell the panel wall which users this door ring was sent to, for `ring_sent_not_shown`. Never raises."""
        try:
            from ..panel_wall import get_panel_wall

            wall = get_panel_wall(self._hass)
            for user_id in {row.get("ha_user_id") for row in targets}:
                wall.note_ring(user_id, call_id, door_id)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("portico push: the panel wall could not be told of a ring", exc_info=True)

    # ---- fan-out -----------------------------------------------------------

    def _fan_out(self, call: _Call, kind: str, data: dict[str, str], only: list[str] | None = None) -> None:
        counts = {"ok": 0, "dead": 0, "failed": 0, "skipped": 0}
        pending: list[asyncio.Task] = []
        for reg_id in (call.targets if only is None else only):
            if kind != payload.KIND_RING and reg_id in call.quiet:
                continue    # a house callee that answered, declined or left is sent nothing more (never on a door call)
            if kind in call.sent.setdefault(reg_id, set()):
                continue
            call.sent[reg_id].add(kind)
            prev = call.chain.get(reg_id)
            task = self._hass.async_create_background_task(
                self._run_one(call, reg_id, kind, data, prev, counts),
                f"portico push {kind} {call.call_id}",
            )
            call.chain[reg_id] = task
            self._track(task)
            pending.append(task)
        if pending:
            self._track(self._hass.async_create_background_task(
                self._summarise(kind, call.call_id, pending, counts), f"portico push summary {call.call_id}"))

    def _track(self, task: asyncio.Task) -> None:
        if task.done():  # an eager task can finish before it is returned
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _summarise(self, kind: str, call_id: str, tasks: list[asyncio.Task], counts: dict) -> None:
        await asyncio.gather(*tasks, return_exceptions=True)
        if any(counts.values()):
            _LOGGER.info(
                "portico push: %s for call %s: ok=%d dead=%d failed=%d skipped=%d",
                kind, call_id, counts["ok"], counts["dead"], counts["failed"], counts["skipped"],
            )
            self._events.on_summary(kind, call_id, dict(counts))

    async def _run_one(self, call: _Call, reg_id: str, kind: str, data: dict[str, str],
                       prev: asyncio.Task | None, counts: dict[str, int]) -> None:
        try:
            if prev is not None:
                await asyncio.gather(prev, return_exceptions=True)
            if kind == payload.KIND_RING and call.house and (call.stop_ring_retries or reg_id in call.quiet):
                counts["skipped"] += 1
                return  # a house ring whose call ended or whose callee was settled before it was ever sent
            if kind != payload.KIND_RING:
                if reg_id not in call.delivered:
                    return  # dismisses go only to phones that got the ring
                if kind == payload.KIND_ENDED and reg_id in call.answered_ok:
                    return
            if kind == payload.KIND_RING:
                deadline = call.ring_deadline
            else:
                deadline = self._mono() + DISMISS_DEADLINE
            result = await self._send_with_retries(call, reg_id, kind, data, deadline)
            if result is None:
                counts["skipped"] += 1
                return
            if result.outcome is Outcome.OK:
                counts["ok"] += 1
                if kind == payload.KIND_RING:
                    call.delivered.add(reg_id)
                elif kind == payload.KIND_ANSWERED:
                    call.answered_ok.add(reg_id)
            elif result.outcome is Outcome.DEAD:
                counts["dead"] += 1
            else:
                counts["failed"] += 1
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a send bug must not escape the task
            counts["failed"] += 1
            _LOGGER.exception("portico push: %s for call %s failed unexpectedly", kind, call.call_id)

    def _take_global_slot(self) -> bool:
        now = self._mono()
        if self._circuit_until is not None:
            if now < self._circuit_until:
                return False
            self._circuit_until = None
            self._global.clear()
            self._events.on_circuit(False)
        while self._global and now - self._global[0] >= 60:
            self._global.popleft()
        if len(self._global) >= GLOBAL_PER_MINUTE:
            self._circuit_until = now + CIRCUIT_SECONDS
            _LOGGER.warning(
                "portico push: more than %d messages in a minute; pausing all pushes for %d s",
                GLOBAL_PER_MINUTE, int(CIRCUIT_SECONDS),
            )
            self._events.on_circuit(True)
            return False
        self._global.append(now)
        return True

    async def _send_with_retries(self, call: _Call, reg_id: str, kind: str, data: dict[str, str],
                                 deadline: float) -> SendResult | None:
        attempt = 0
        result: SendResult | None = None
        while True:
            row = self._registry.get(reg_id)
            if row is None or not row.get("registration"):
                return result
            if not self._take_global_slot():
                self._events.on_skip("rate_limited", call.door_ref or None, call.call_id)
                return result
            try:
                message = payload.build_message(
                    kind=row["kind"], registration=row["registration"], package=row["package"], data=data,
                    now_ms=int(self._wall() * 1000), ring_window_s=int(self._ring_window()),
                )
            except payload.PayloadError as err:
                _LOGGER.error("portico push: refusing to build a %s message: %s", kind, err)
                return SendResult(Outcome.PAYLOAD_REJECTED, None, "LOCAL")
            result = await self._client.async_send(message)
            self._apply_result(row, kind, call.call_id, result)
            if result.outcome not in (Outcome.TRANSIENT, Outcome.QUOTA):
                return result
            if kind == payload.KIND_RING and (call.stop_ring_retries or (call.house and reg_id in call.quiet)):
                return result
            wait = BACKOFF[min(attempt, len(BACKOFF) - 1)]
            remaining = deadline - self._mono()
            if result.retry_after is not None:
                if result.retry_after > remaining:
                    return result
                wait = max(wait, result.retry_after)
            elif result.outcome is Outcome.QUOTA:
                _LOGGER.info("portico push: %s for call %s dropped: FCM quota exceeded", kind, call.call_id)
                return result
            if wait >= remaining:
                return result
            await self._sleep(wait)
            attempt += 1
            if kind == payload.KIND_RING and (call.stop_ring_retries or (call.house and reg_id in call.quiet)):
                return result

    # ---- house calls (M7 3c, design 15.5): the push ring notification of a house call ------------------------------------------

    def house_targets(self, user_id: str | None) -> list[dict]:
        """The registrations a house ring may reach for one HA user: the same filter as a door ring (switched on, live,
        not dead, and on the staged-test list while one is set). Empty when the push ring notification is off."""
        if not user_id or self._stopped or not self._enabled():
            return []
        rows = [row for row in self._registry.targets() if row["ha_user_id"] == user_id]
        allow = set(self._test_phones())
        if allow:  # a staged test is on: never ring a phone that is not on the list
            rows = [row for row in rows if row["reg_id"] in allow]
        return rows

    @callback
    def house_ring(self, call_id: str, from_ref: str, callees: dict[str, str], ring_s: int) -> set[str]:
        """Ring the away callees of a house call: `callees` maps an endpoint id to its HA user id. Returns the endpoint ids
        a push was queued for (a capped or registration-less callee is left out; it still rings if it comes online)."""
        try:
            return self._house_ring(call_id, from_ref, callees, int(ring_s))
        except Exception:  # noqa: BLE001 - never into the house-call registry
            _LOGGER.exception("portico push: failed to start a house ring")
            return set()

    def _house_ring(self, call_id: str, from_ref: str, callees: dict[str, str], ring_s: int) -> set[str]:
        if self._stopped or not self._enabled() or call_id in self._house_calls or call_id in self._house_finished:
            return set()
        self._expire_calls()
        call = _Call("", call_id, self._mono(), self._mono() + ring_s - RING_DEADLINE_MARGIN, house=True,
                     from_ref=from_ref)
        for endpoint_id, user_id in sorted(callees.items()):
            rows = self.house_targets(user_id)
            if not rows:
                continue
            if not self._device_allows(f"house:{endpoint_id}"):
                self._events.on_skip("device_rate", None, call_id)
                _LOGGER.info("portico push: house ring for call %s skipped by the per-callee cap", call_id)
                continue
            call.endpoints[endpoint_id] = [row["reg_id"] for row in rows]
            call.targets.extend(call.endpoints[endpoint_id])
        if not call.endpoints:
            return set()
        data = payload.build_house_ring(from_ref, call_id, int(self._wall() * 1000), ring_s)
        self._house_calls[call_id] = call
        self._fan_out(call, payload.KIND_RING, data)
        return set(call.endpoints)

    @callback
    def house_answered(self, call_id: str, winner: str) -> None:
        """First answer won: the winner is settled (its app answered over the socket), everyone else rung by push is
        told "answered elsewhere" (only those whose ring was accepted, the door's chain rule)."""
        call = self._house_calls.get(call_id)
        if call is None or call.answered_queued:
            return
        try:
            call.quiet.update(call.endpoints.get(winner, ()))
            call.answered_queued = True
            call.stop_ring_retries = True
            self._fan_out(call, payload.KIND_ANSWERED,
                          payload.build_house_answered(call.from_ref, call_id, int(self._wall() * 1000)))
        except Exception:  # noqa: BLE001
            _LOGGER.exception("portico push: failed to handle a house call being answered")

    @callback
    def house_left(self, call_id: str, endpoint_id: str, *, notify: bool) -> None:
        """One callee of a still-ringing house call is out of it: it declined (`notify` False: it knows) or a door took
        it (`notify` True: its ring is dismissed). Either way nothing more is sent to it."""
        call = self._house_calls.get(call_id)
        if call is None:
            return
        try:
            regs = list(call.endpoints.get(endpoint_id, ()))
            if notify:
                self._fan_out(call, payload.KIND_ENDED,
                              payload.build_house_ended(call.from_ref, call_id, int(self._wall() * 1000)), only=regs)
            call.quiet.update(regs)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("portico push: failed to handle a house callee leaving")

    @callback
    def house_ended(self, call_id: str) -> None:
        """The call is over (cancelled, unanswered, hung up, an error): the ring stops, and every pushed phone whose ring
        was accepted and that has not been settled is dismissed."""
        call = self._house_calls.pop(call_id, None)
        if call is None:
            return
        self._house_finished.append(call_id)
        try:
            call.stop_ring_retries = True
            self._fan_out(call, payload.KIND_ENDED,
                          payload.build_house_ended(call.from_ref, call_id, int(self._wall() * 1000)))
        except Exception:  # noqa: BLE001
            _LOGGER.exception("portico push: failed to end a house ring")

    # ---- the staged test (spec §8.3) ------------------------------------------

    def take_test_slot(self) -> bool:
        """One `portico.push_test` call per TEST_MIN_SPACING, TEST_PER_HOUR an
        hour. False: refused, nothing sent."""
        now = self._mono()
        while self._test_sends and now - self._test_sends[0] > 3600:
            self._test_sends.popleft()
        if self._test_sends and now - self._test_sends[-1] < TEST_MIN_SPACING:
            return False
        if len(self._test_sends) >= TEST_PER_HOUR:
            return False
        self._test_sends.append(now)
        return True

    async def async_send_test(self, rows: list[dict]) -> list[tuple[dict, SendResult | None]]:
        """One `test` message to each row, one attempt each (a test that fails
        is reported, not retried). The caller has already checked the rows
        against the allow-list. None: skipped by the global circuit."""
        now_ms = int(self._wall() * 1000)
        data = payload.build_test(now_ms)
        out: list[tuple[dict, SendResult | None]] = []
        for row in rows:
            if self._stopped or not self._take_global_slot():
                self._events.on_skip("rate_limited", None, None)
                out.append((row, None))
                continue
            try:
                message = payload.build_message(
                    kind=row["kind"], registration=row["registration"], package=row["package"], data=data,
                    now_ms=now_ms, ring_window_s=int(self._ring_window()),
                )
            except payload.PayloadError as err:
                _LOGGER.error("portico push: refusing to build a test message: %s", err)
                out.append((row, SendResult(Outcome.PAYLOAD_REJECTED, None, "LOCAL")))
                continue
            result = await self._client.async_send(message)
            self._apply_result(row, payload.KIND_TEST, None, result)
            out.append((row, result))
        return out

    def _apply_result(self, row: dict, kind: str, call_id: str | None, result: SendResult) -> None:
        reg_id = row["reg_id"]
        if result.outcome is Outcome.OK:
            self._registry.record_result(reg_id, "ok", ok=True)
        elif result.outcome is Outcome.DEAD:
            self._registry.mark_dead(reg_id, result.error.lower() or "unregistered")
        elif result.outcome is Outcome.PROJECT_MISMATCH:
            self._registry.record_result(reg_id, "project_mismatch", ok=False, state=STATE_PROJECT_MISMATCH)
        else:
            self._registry.record_result(reg_id, result.outcome.value, ok=False)
        self._events.on_result(row, kind, call_id, result)

    # ---- lifecycle ---------------------------------------------------------

    async def async_drain(self) -> None:
        """Wait for every in-flight send (tests; and a clean unload)."""
        while True:
            # Done tasks are dropped here, not only by their done callback:
            # awaiting a gather of finished tasks does not yield, so the
            # callbacks would never get to run.
            pending = [task for task in self._tasks if not task.done()]
            self._tasks.intersection_update(pending)
            if not pending:
                return
            await asyncio.gather(*pending, return_exceptions=True)

    @callback
    def async_stop(self) -> None:
        self._stopped = True
        for task in list(self._tasks):
            task.cancel()
        self._calls.clear()
        self._house_calls.clear()
