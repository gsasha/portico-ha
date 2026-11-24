"""The ring source of an `ha_event` door: a Home Assistant event entity that fires when the doorbell is pressed.

Why this exists (portico_front_door_design.md, option B). The Protect console's public events stream has never been
shown to emit `ring` for this estate's doorbell, while HA's own UniFi Protect integration does: its event entity
`event.*_doorbell` gets event type `ring` on a press, and the estate's old front-door ring already rides it. This
module listens to that entity and starts a ring on the door's call-state tracker, from where the ordinary pipeline
(call-state sensor, push notifier, phones' websocket, the endpoint's camera) takes over. No Go code, no transport
process, no console connection (with the `talkback` option on, the door also attaches to the hub, and a press then also sends
`external_ring` to the Go driver so the ring can be answered; the ring itself is still made here).

Ring-only (unless `talkback` is on and the Go driver attached): the door states no audio and no relay
(drivers/ha_event.py), and `RingOnlyClient` below refuses every transport request a phone could make for it (answer, relay, webrtc, media). It never calls a lock, a gate or any other
service.

What counts as a press (each rule is a way the entity changes without anyone pressing):
* the entity's state is the event's timestamp and its `event_type` attribute the type; only the configured type rings;
* the entity's current event is remembered at start, so a state that already exists never rings;
* an event is identified by the attribute `event_id` (else by its timestamp state); one id rings once;
* a restore (None / unknown / unavailable -> value after a restart or a reload) carries an OLD timestamp: a press must
  be fresh (FRESH_SECONDS) whatever the transition, which is also what separates the first real press after a restart
  from the restore;
* Home Assistant must be running (the startup state replay rings nothing);
* two presses closer than MIN_GAP_SECONDS are one ring (a bell that bounces).
"""
from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from homeassistant.core import CoreState, Event, HomeAssistant, State, callback
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.util import dt as dt_util

from .transport import TransportError

if TYPE_CHECKING:
    from .door import DoorRuntime

_LOGGER = logging.getLogger(__name__)

FRESH_SECONDS = 30.0        # an event older than this by its own timestamp is a restore or a replay, not a press
FUTURE_SECONDS = 300.0      # a timestamp further ahead than this is a broken clock, not a press
MIN_GAP_SECONDS = 3.0       # presses closer than this are one ring
_SEEN_MEMORY = 64
SOURCE = "ha_event"
BELL_PRESSED = "bell_pressed"   # the hangup reason when a bell press closes my own call out


class RingOnlyClient:
    """Stands in for the transport client of a door that has none: it answers every request a phone can make with
    the refusal the provider already maps (`claims_disabled` -> verdict `refused`), `not_supported` for the rest.
    `hangup` and `keepalive` succeed, so a phone can dismiss the ring. Frame sending is fire-and-forget upstream."""

    async def hangup(self, call_id: str, reason: str) -> dict[str, Any]:
        return {"ok": True}

    async def keepalive(self, call_id: str) -> dict[str, Any]:
        return {"ok": True}

    async def answer(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise TransportError("claims_disabled", "this door is ring-only: nothing to answer")

    def media_frame_nowait(self, *args: Any, **kwargs: Any) -> None:
        return None

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)

        async def _refuse(*args: Any, **kwargs: Any):
            raise TransportError("not_supported", f"this door is ring-only: {name} is not available")

        return _refuse


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "x"


class HaEventRing:
    """Watches one event entity for one event type and rings the runtime's door."""

    def __init__(self, hass: HomeAssistant, runtime: "DoorRuntime", *, event_entity: str, event_type: str,
                 hold: float, clock: Callable[[], float] | None = None) -> None:
        self._hass = hass
        self._runtime = runtime
        self._entity = event_entity
        self._type = event_type
        self._hold = hold
        self._clock = clock or time.monotonic
        self._seen: list[str] = []
        self._last_ring: float | None = None
        self._unsub: Callable[[], None] | None = None
        self._cancel_hold: Callable[[], None] | None = None

    # ---- lifecycle -----------------------------------------------------------------------------------

    def start(self) -> None:
        state = self._hass.states.get(self._entity)
        if state is not None:
            self._remember(self._key(state))      # whatever event the entity holds now is history
        self._set_available(state is not None and state.state != "unavailable")
        self._unsub = async_track_state_change_event(self._hass, [self._entity], self._on_change)

    def stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        self._clear_hold()

    # ---- the entity changed --------------------------------------------------------------------------

    @callback
    def _on_change(self, event: Event) -> None:
        new: State | None = event.data.get("new_state")
        available = new is not None and new.state != "unavailable"
        self._set_available(available)
        if not available or new.state == "unknown":
            return
        if self._accepts(new):
            self._ring(self._key(new))

    def _set_available(self, available: bool) -> None:
        self._runtime.handle_notification(
            "driver_availability", {"device_id": self._runtime.device_id, "available": available,
                                    "reason": "" if available else f"{self._entity} is unavailable or missing"})

    @staticmethod
    def _key(state: State) -> str:
        event_id = state.attributes.get("event_id")
        return str(event_id) if event_id else state.state

    def _remember(self, key: str) -> None:
        if key not in self._seen:
            self._seen.append(key)
            del self._seen[:-_SEEN_MEMORY]

    def _accepts(self, new: State) -> bool:
        if new.attributes.get("event_type") != self._type:
            self._remember(self._key(new))
            return False
        key = self._key(new)
        if key in self._seen:
            _LOGGER.debug("portico: %s: event %s already seen, no ring", self._entity, key)
            return False
        self._remember(key)
        if self._hass.state is not CoreState.running:
            return False
        stamp = dt_util.parse_datetime(new.state)
        if stamp is None:
            _LOGGER.debug("portico: %s: state %r is not a timestamp, no ring", self._entity, new.state)
            return False
        age = (dt_util.utcnow() - stamp).total_seconds()
        if age > FRESH_SECONDS or age < -FUTURE_SECONDS:
            _LOGGER.debug("portico: %s: event %s is %.0f s old, a restore not a press", self._entity, key, age)
            return False
        now = self._clock()
        if self._last_ring is not None and now - self._last_ring < MIN_GAP_SECONDS:
            _LOGGER.debug("portico: %s: event %s within %.0f s of the last ring, no ring", self._entity, key, MIN_GAP_SECONDS)
            return False
        self._last_ring = now
        return True

    # ---- the ring ------------------------------------------------------------------------------------

    def _ring(self, key: str) -> None:
        call_id = f"ha-event-{_slug(key)}"
        out_id = self._runtime.call_state.outbound_call_id
        if self._runtime.talk_attached and out_id is not None:
            # A bell press while MY call out is live closes that call and nothing else (the owner, 2026-11-11): no ring is
            # registered with Go and the phones are not told. The press is consumed; a ring held or an inbound call
            # answered is not touched by this branch (outbound slot only).
            _LOGGER.info("portico: %s rang (%s) on door %s: bell during own call: hung up, no ring (%s)",
                         self._entity, self._type, self._runtime.device_id, out_id)
            self._hass.async_create_task(self._runtime.talk_hangup(out_id, BELL_PRESSED))
            return
        _LOGGER.info("portico: %s rang (%s), ringing door %s", self._entity, self._type, self._runtime.device_id)
        if self._runtime.talk_attached:
            # Front-door talk: the Go driver learns "this call id is ringing, answerable for `hold` s". Queued BEFORE the
            # phones are told so it reaches Go before any phone can answer; it announces nothing and a failure only
            # makes this one ring unanswerable (logged by the runtime).
            self._hass.async_create_task(self._runtime.talk_external_ring(call_id, float(self._hold)))
        self._runtime.handle_notification(
            "ringing", {"call_id": call_id, "device_id": self._runtime.device_id, "source": SOURCE})
        self._clear_hold()

        @callback
        def _expire_at(_now) -> None:       # a plain lambda would run in the executor, off the event loop
            self._expire(call_id)

        self._cancel_hold = async_call_later(self._hass, self._hold, _expire_at)

    def _expire(self, call_id: str) -> None:
        self._cancel_hold = None
        tracker = self._runtime.call_state
        if tracker.call_id == call_id and tracker.state == "ringing":
            tracker.apply_event({"call_id": call_id, "state": "ended"})
            if self._runtime.talk_attached:
                self._hass.async_create_task(self._runtime.talk_hangup(call_id))

    def _clear_hold(self) -> None:
        if self._cancel_hold is not None:
            self._cancel_hold()
            self._cancel_hold = None
