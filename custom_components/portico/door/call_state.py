"""Mirrors call state into Home Assistant entities.

Design doc §4.5, §9.2. Wire states from the transport binary (or a
client's own self-reported direct-path answer) are exactly
ringing|answered|active|ended — `idle` is HA-side only, the sensor's
default when no call_id is tracked at all, and the `Ended -> idle`
transition is immediate (no dwell timer, §4.5).

Also owns the `[CLI]`-free "already answered" mirroring for the
sensor/lock/camera entities — this module is a thin translation layer
between the Go binary's event stream (or the app's own WebSocket
self-report) and HA's entity model, not a poller (§9.2).
"""
from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

_LOGGER = logging.getLogger(__name__)

_VALID_STATES = {"ringing", "answered", "active", "ended"}

# The hub mints the id of a call that Portico itself placed (a `dial`) as `out-<16 hex>` (transport P1,
# `driver.CallDialer`). A call we placed is not a visitor ringing: it must never reach the door's ring
# entity, the push, the phones' call stream or the takeover automation. See is_outbound_event.
OUTBOUND_CALL_PREFIX = "out-"

# How many recently ended call ids to remember, so a late event for one of
# them is recognised and dropped (see apply_event). One device has one call
# at a time; this is many calls of slack.
_ENDED_MEMORY = 64


@dataclass(frozen=True, slots=True)
class Transition:
    """One applied state change, as the FCM sender needs it (sender design
    §2.2). `call_id` is the call the change is about: on `ended` it is the
    call that ended, which the tracker has already forgotten by the time its
    argument-less listeners run. `prev_state` may equal `state` (a repeated
    `ringing`, or a new call's `ringing` replacing the tracked one)."""

    device_id: str
    entry_id: str | None
    call_id: str | None
    prev_state: str
    state: str
    prev_call_id: str | None
    at_monotonic: float
    at_wall: float


def is_outbound_event(event: dict) -> bool:
    """Whether a `ringing`/`call_state_changed` event is about a call Portico placed rather than a visitor's.

    True when its `call_id` starts with the hub's `out-` prefix, or when it carries a direction marker
    (`direction == "outbound"`, or `outbound` is true). No driver or hub sends a marker today; the id prefix is
    the one signal the Go hub (P1) puts on every outbound call. An event with neither is a visitor event,
    exactly as before."""
    call_id = event.get("call_id")
    if isinstance(call_id, str) and call_id.startswith(OUTBOUND_CALL_PREFIX):
        return True
    return event.get("direction") == "outbound" or event.get("outbound") is True


# The states of an outbound call, as the Talk feature sees them. Wire `ringing` is the far end being rung (we
# are dialling), `answered`/`active` is the conversation, `ended` is terminal. Deliberately NOT the visitor
# vocabulary: an outbound `ringing` must never read as a visitor at the door.
_OUTBOUND_STATE = {"ringing": "dialing", "answered": "in_call", "active": "in_call", "ended": "ended"}


@dataclass(frozen=True, slots=True)
class OutboundTransition:
    """One applied change of an outbound call, for the listeners of add_outbound_listener. `state` is
    `dialing`, `in_call` or `ended`; `prev_state` is one of those or `idle`."""

    device_id: str
    entry_id: str | None
    call_id: str
    prev_state: str
    state: str
    at_wall: float


class CallStateTracker:
    """One instance per configured device; tracks its current call, if any.

    A call Portico itself placed (is_outbound_event) is tracked on the side, in `outbound_call_id` and
    `outbound_state`, and never touches `call_id`, `state`, the plain listeners or the transition listeners:
    everything that reacts to a visitor (the sensor, the push, the phones' stream, the bus event, the repair
    watch) hangs off those."""

    def __init__(self, device_id: str, entry_id: str | None = None) -> None:
        self.device_id = device_id
        self.entry_id = entry_id
        self.call_id: str | None = None
        self.state: str = "idle"
        self._listeners: list[Callable[[], None]] = []
        self._transition_listeners: list[Callable[[Transition], None]] = []
        self._ended_call_ids: deque[str] = deque(maxlen=_ENDED_MEMORY)
        self.ring_mono: float | None = None     # monotonic time of the current call's ring (diagnostics only)
        # The call Portico placed, if any: its own id, state ("idle" | "dialing" | "in_call") and listeners.
        self.outbound_call_id: str | None = None
        self.outbound_state: str = "idle"
        self._outbound_listeners: list[Callable[[OutboundTransition], None]] = []
        self._ended_outbound_ids: deque[str] = deque(maxlen=_ENDED_MEMORY)

    def add_outbound_listener(self, callback: Callable[[OutboundTransition], None]) -> None:
        """Register a callback that receives each applied change of an outbound call. A raising callback is
        logged and skipped. The provider (provider_impl.TransportProvider) registers one per attached door and hands
        the change to the UI part's talk relay as a dict (contract/provider_v1.md section 4a)."""
        self._outbound_listeners.append(callback)

    def remove_outbound_listener(self, callback: Callable[[OutboundTransition], None]) -> None:
        if callback in self._outbound_listeners:
            self._outbound_listeners.remove(callback)

    def apply_outbound_event(self, event: dict) -> None:
        """Apply an event for a call Portico placed: `dialing` (wire `ringing`), `in_call` (`answered`/`active`),
        `ended` (terminal; a later event for an ended outbound id is dropped). Touches no visitor state."""
        wire_state = event.get("state")
        if wire_state not in _VALID_STATES:
            return
        call_id = event.get("call_id")
        if not isinstance(call_id, str) or not call_id or call_id in self._ended_outbound_ids:
            return
        new_state = _OUTBOUND_STATE[wire_state]
        old_state = self.outbound_state if call_id == self.outbound_call_id else "idle"
        # WARNING (DEV-806): production runs the `portico` logger at WARNING; one line per outbound transition, a handful per call
        _LOGGER.warning("portico call: %s outbound call=%s %s -> %s", self.device_id, call_id, old_state, new_state)
        if new_state == "ended" and self.outbound_call_id not in (None, call_id):
            # the end of some other outbound call than the tracked one: remember it, leave the tracked one
            self._ended_outbound_ids.append(call_id)
            return
        prev_state = self.outbound_state if call_id == self.outbound_call_id else "idle"
        transition = OutboundTransition(
            device_id=self.device_id, entry_id=self.entry_id, call_id=call_id,
            prev_state=prev_state, state=new_state, at_wall=time.time())
        if new_state == "ended":
            self._ended_outbound_ids.append(call_id)
            self.outbound_call_id = None
            self.outbound_state = "idle"
        else:
            self.outbound_call_id = call_id
            self.outbound_state = new_state
        for listener in list(self._outbound_listeners):
            try:
                listener(transition)
            except Exception:  # noqa: BLE001 - never let a listener stop the live path
                _LOGGER.exception("portico: outbound listener failed")

    def end_outbound_call(self) -> None:
        """End the outbound call being tracked, if any (the transport or the driver is gone)."""
        if self.outbound_call_id is not None:
            self.apply_outbound_event({"call_id": self.outbound_call_id, "state": "ended"})

    def add_listener(self, callback: Callable[[], None]) -> None:
        """Register a callback fired after every state change (an entity's
        own async_write_ha_state, typically)."""
        self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[], None]) -> None:
        """Undo add_listener — a websocket subscription's own unsubscribe
        (signaling.py, §9.5) needs this to not leak a callback into a
        closed connection."""
        if callback in self._listeners:
            self._listeners.remove(callback)

    def add_transition_listener(self, callback: Callable[[Transition], None]) -> None:
        """Register a callback that receives each applied change as a
        Transition, BEFORE the tracker mutates (so on `ended` it still holds
        the ending call). Dropped events (unknown state, a late event for an
        ended call) produce none. A raising callback is logged and skipped:
        it can never stop the state change or the plain listeners."""
        self._transition_listeners.append(callback)

    def remove_transition_listener(self, callback: Callable[[Transition], None]) -> None:
        if callback in self._transition_listeners:
            self._transition_listeners.remove(callback)

    def apply_event(self, event: dict) -> None:
        """Apply a `ringing`/`call_state_changed` notification (§9.4) or a
        client's own direct-path self-report (§9.6) — same shape either
        way: {"call_id": ..., "state": ..., ...}. An unrecognized state
        is logged and ignored rather than silently corrupting
        self.state with a value nothing else expects.

        `ended` is terminal (§4.5): any later event for a call already
        ended here is dropped. That is what makes Python ending a call on
        its own (the driver went unavailable, the transport stopped or
        died: DoorRuntime) safe when the Go side, still following
        that call, later sends its answered/ended.

        A call Portico placed (is_outbound_event) is not a visitor: it goes to apply_outbound_event and leaves
        this tracker's state, listeners and transition listeners alone."""
        if is_outbound_event(event):
            self.apply_outbound_event(event)
            return
        new_state = event.get("state")
        if new_state not in _VALID_STATES:
            return
        event_call_id = event.get("call_id")
        if event_call_id is not None and event_call_id in self._ended_call_ids:
            _LOGGER.debug("portico call: %s dropped %s for ended call=%s", self.device_id, new_state, event_call_id)
            return

        if (new_state == "ended" and event_call_id is not None and self.call_id is not None
                and event_call_id != self.call_id):
            # The end of a call that is no longer the current one (the talk driver ends a superseded ring after a
            # new one has been announced): remember it, and leave the live call alone.
            self._ended_call_ids.append(event_call_id)
            _LOGGER.debug("portico call: %s ended superseded call=%s (tracked call=%s %s)", self.device_id,
                          event_call_id, self.call_id, self.state)
            return

        _LOGGER.info("portico call: %s call=%s %s -> %s (tracked call=%s)", self.device_id,
                     event_call_id or self.call_id, self.state, new_state, self.call_id)
        if new_state == "ringing" and event_call_id != self.call_id:
            self.ring_mono = time.monotonic()

        if self._transition_listeners:
            if new_state == "ended":
                about = self.call_id if self.call_id is not None else event_call_id
            else:
                about = event_call_id
            transition = Transition(
                device_id=self.device_id,
                entry_id=self.entry_id,
                call_id=about,
                prev_state=self.state,
                state=new_state,
                prev_call_id=self.call_id,
                at_monotonic=time.monotonic(),
                at_wall=time.time(),
            )
            for transition_listener in list(self._transition_listeners):
                try:
                    transition_listener(transition)
                except Exception:  # noqa: BLE001 - never let a listener stop the live path
                    _LOGGER.exception("portico: transition listener failed")

        if new_state == "ended":
            # Immediate idle, no dwell timer (§4.5) — nothing in the
            # design needed one.
            for ended in {self.call_id, event_call_id} - {None}:
                self._ended_call_ids.append(ended)
            self.call_id = None
            self.state = "idle"
            self.ring_mono = None
        else:
            self.call_id = event.get("call_id")
            self.state = new_state

        for listener in self._listeners:
            listener()
