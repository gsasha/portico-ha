"""Every ring and call as a Home Assistant event (DEV-811 P4; design: automation/portico_call_audio_design.md section P4).

One model for every kind of call. Per call: `ringing` -> `answered` -> `active` -> `ended`, one `portico_call` bus event per
phase, so a ring is an event even when nobody answers. Three kinds, each with its own source of phases:

* `door`      a visitor call, from the door tracker's transitions (the provider registry's `subscribe`);
* `outbound`  a Talk call placed from a phone, from the outbound listener (`subscribe_outbound`): `dialing` is phase
              `ringing`, `in_call` is phase `active`, there is no separate `answered`;
* `house`     a call between two endpoints of the house, from the CALL-LEVEL moments of `house.py` only (place, answer,
              mark_active, `_end`), never the per-endpoint `_emit`, which also ends single endpoints while the call goes on.

The legacy `portico_intercom_call` event and `sensor.call_state` are untouched: nothing here changes what they carry.

This module also owns the live set behind `sensor.portico_calls` (`idle | ringing | talking`) and
`binary_sensor.portico_call_active` (on while the sensor is not `idle`). The set is built from the events' OWN call ids (the
door tracker notifies its listeners before it stores the new call, so its `call_id` is stale inside a listener). A 60-second
pruner asks each call's source whether it still holds the call; an id is dropped only when the source no longer holds it AND
the id's last event is more than 60 seconds old, and the drop is an `ended` with `outcome: lost`. There is no time-based drop:
a call its source still holds is a call, however long.

`portico_call_stats` (one per call, 5 seconds after `ended`) carries the counters that P3 delivers through `add_stats_part`
(`add_wire_stats` for what a transport notification carried); with no part, no stats event is fired. `ended` never waits for it.

Privacy: no audio, frames, SDP, addresses, tokens or phone ids; endpoint names only. A field without a source is omitted, not
null.
"""
from __future__ import annotations

import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.core import CALLBACK_TYPE, HassJob, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later, async_track_time_interval

from .provider import DATA_PROVIDERS, DOMAIN, CallTransition, OutboundTransition

_LOGGER = logging.getLogger(__name__)

EVENT_CALL = "portico_call"
EVENT_CALL_STATS = "portico_call_stats"
DATA_CALL_EVENTS = "call_events"
DATA_STATS_PARTS = "call_stats_parts"

KIND_DOOR, KIND_OUTBOUND, KIND_HOUSE = "door", "outbound", "house"
PHASE_RINGING, PHASE_ANSWERED, PHASE_ACTIVE, PHASE_ENDED = "ringing", "answered", "active", "ended"

# What the tracker may say about a call; anything else (`unavailable`, `idle`) is not a call phase.
_DOOR_PHASES = (PHASE_RINGING, PHASE_ANSWERED, PHASE_ACTIVE)
_OUTBOUND_PHASE = {"dialing": PHASE_RINGING, "in_call": PHASE_ACTIVE}

# `sensor.portico_calls` states.
IDLE, RINGING, TALKING = "idle", "ringing", "talking"
STATES = [IDLE, RINGING, TALKING]

OUTCOME_ANSWERED, OUTCOME_MISSED, OUTCOME_SUPERSEDED = "answered", "missed", "superseded"
OUTCOME_LOST, OUTCOME_RANG, OUTCOME_UNKNOWN = "lost", "rang", "unknown"

PRUNE_INTERVAL_S = 60                 # the pruner's period, and the age an id's last event must exceed before it may be dropped
STATS_DELAY_S = 5                     # `portico_call_stats` fires this long after `ended`
_DONE_MEMORY = 256                    # ended ids remembered, so a late event for one is dropped
_STATS_MEMORY = 64                    # calls whose stats parts are kept
_STATS_PARTS_PER_CALL = 8


@dataclass(eq=False)
class _Live:
    call_id: str
    kind: str
    direction: str | None = None                    # `in` | `out` from the household's side; None when no source says
    door_id: str | None = None
    door_name: str | None = None
    from_name: str | None = None
    to_name: str | None = None
    answered_by: str | None = None
    ring_only: bool = False                         # a door that cannot be answered through Portico: its ring can only be `rang`
    fired: set[tuple[str, str]] = field(default_factory=set)   # (call_id, phase), each fired once
    at: dict[str, float] = field(default_factory=dict)         # phase -> wall time of that phase
    last_mono: float = 0.0


def _iso(wall: float) -> str:
    return datetime.fromtimestamp(wall, tz=timezone.utc).isoformat()


def _round(seconds: float) -> float:
    return round(max(seconds, 0.0), 1)


def get_call_events(hass: HomeAssistant) -> "CallEvents | None":
    return hass.data.get(DOMAIN, {}).get(DATA_CALL_EVENTS)


def call_stats_parts(hass: HomeAssistant, call_id: str) -> list[dict]:
    """The counters delivered for this call so far, one dict per part (P3 owns the writers). Empty today."""
    return list(hass.data.get(DOMAIN, {}).get(DATA_STATS_PARTS, {}).get(call_id, ()))


def add_stats_part(hass: HomeAssistant, call_id: str, part: dict) -> None:
    """Keep one part of a call's counters, bounded. The seam P3's provider calls; also what the tests use."""
    store = hass.data.setdefault(DOMAIN, {}).setdefault(DATA_STATS_PARTS, OrderedDict())
    parts = store.setdefault(call_id, [])
    if len(parts) < _STATS_PARTS_PER_CALL:
        parts.append(dict(part))
    store.move_to_end(call_id)
    while len(store) > _STATS_MEMORY:
        store.popitem(last=False)


_WIRE_SKIP = frozenset({"call_id", "device_id", "reason"})


def add_wire_stats(hass: HomeAssistant, call_id: str, raw: Any) -> None:
    """Keep the counters a transport notification carried (`media_closed.stats` or `call_audio_stats`, DEV-811 P3) as one part
    of the call's stats. Only numbers and short lists of integers pass: nothing that names a person, an address or a token can
    ride a counter, and a bad payload is dropped, never raised."""
    if not isinstance(call_id, str) or not call_id or not isinstance(raw, dict):
        return
    part: dict[str, Any] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or key in _WIRE_SKIP or len(key) > 40:
            continue
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            part[key] = value
        elif isinstance(value, list) and len(value) <= 64 and all(isinstance(v, int) and not isinstance(v, bool) for v in value):
            part[key] = list(value)
    if part:
        add_stats_part(hass, call_id, part)


class CallEvents:
    """The call-event helper: fires `portico_call`, owns the live set and its pruner, and tells entities when it changed."""

    def __init__(self, hass: HomeAssistant, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.hass = hass
        self._clock = clock
        self._live: dict[str, _Live] = {}
        self._done: OrderedDict[str, None] = OrderedDict()
        self._listeners: list[Callable[[], None]] = []
        self._unsubs: list[CALLBACK_TYPE] = []
        self._stats_timers: dict[str, CALLBACK_TYPE] = {}
        self.last_call: dict[str, Any] | None = None

    # ---- lifecycle ------------------------------------------------------------------------------------------------

    def start(self) -> None:
        registry = self.hass.data.get(DOMAIN, {}).get(DATA_PROVIDERS)
        if registry is not None:
            self._unsubs.append(registry.subscribe(self.on_door_transition))
            self._unsubs.append(registry.subscribe_outbound(self.on_outbound_transition))
        self._unsubs.append(async_track_time_interval(
            self.hass, self._prune_tick, timedelta(seconds=PRUNE_INTERVAL_S), cancel_on_shutdown=True))

    def stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        for cancel in list(self._stats_timers.values()):
            cancel()
        self._stats_timers.clear()

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)

        def _remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _remove

    # ---- the sensor's view ----------------------------------------------------------------------------------------

    @property
    def state(self) -> str:
        """`talking` while any call is answered or active; else `ringing` while a visitor or house ring is live; else `idle`.
        An outbound call that is only `dialing` does not make the house `ringing` (the code keeps these apart on purpose)."""
        phases = [(c.kind, self._phase(c)) for c in self._live.values()]
        if any(p in (PHASE_ANSWERED, PHASE_ACTIVE) for _k, p in phases):
            return TALKING
        if any(p == PHASE_RINGING and k != KIND_OUTBOUND for k, p in phases):
            return RINGING
        return IDLE

    @staticmethod
    def _phase(call: _Live) -> str:
        for phase in (PHASE_ACTIVE, PHASE_ANSWERED, PHASE_RINGING):
            if (call.call_id, phase) in call.fired:
                return phase
        return PHASE_RINGING

    def live_calls(self) -> list[dict[str, Any]]:
        out = []
        for call in self._live.values():
            row: dict[str, Any] = {"call_id": call.call_id, "kind": call.kind, "phase": self._phase(call)}
            if call.door_name:
                row["door_name"] = call.door_name
            out.append(row)
        return out

    # ---- door calls -----------------------------------------------------------------------------------------------

    def on_door_transition(self, t: CallTransition) -> None:
        """A visitor call changed state. Never raises into the provider registry's fan-out."""
        try:
            self._on_door_transition(t)
        except Exception:  # noqa: BLE001 - a bug here must not touch the door path
            _LOGGER.exception("portico call events: a door transition could not be applied")

    def _on_door_transition(self, t: CallTransition) -> None:
        if t.state not in (*_DOOR_PHASES, PHASE_ENDED):
            return                                    # `unavailable`: a door leaving is not a call phase
        if not t.call_id:
            _LOGGER.debug("portico call events: %s transition for %s has no call id; ignored", t.state, t.door_id)
            return
        if t.state == PHASE_ENDED:
            call = self._live.get(t.call_id)
            if call is None or t.prev_state == "idle":
                return                                # an end for a call never seen to start fires nothing
            self._finish(call, self._door_outcome(call), t.at_wall)
            return
        if t.call_id in self._done:
            return                                    # a late event for an ended id
        if t.prev_call_id and t.prev_call_id != t.call_id:
            old = self._live.get(t.prev_call_id)
            if old is not None and old.kind == KIND_DOOR:
                self._finish(old, OUTCOME_SUPERSEDED, t.at_wall)
        call = self._live.get(t.call_id)
        if call is None:
            door_name, ring_only = self._door_facts(t.door_id)
            call = _Live(call_id=t.call_id, kind=KIND_DOOR, direction="in", door_id=t.door_id, door_name=door_name,
                         ring_only=ring_only)
            self._live[t.call_id] = call
        self._phase_event(call, t.state, t.at_wall)

    def _door_outcome(self, call: _Live) -> str:
        if (call.call_id, PHASE_ANSWERED) in call.fired or (call.call_id, PHASE_ACTIVE) in call.fired:
            return OUTCOME_ANSWERED
        return OUTCOME_RANG if call.ring_only else OUTCOME_MISSED

    def _door_facts(self, door_id: str) -> tuple[str | None, bool]:
        """The door's name and whether it is ring-only (cannot be answered through Portico), from the provider registry."""
        registry = self.hass.data.get(DOMAIN, {}).get(DATA_PROVIDERS)
        if registry is None:
            return None, False
        try:
            found = registry.door(door_id)
        except Exception:  # noqa: BLE001 - an ambiguous or unknown door only costs the name
            return None, False
        if not found:
            return None, False
        door = found[1]
        from .signaling import _answerable

        return door.name or None, not _answerable(door.capabilities)

    # ---- calls Portico placed -------------------------------------------------------------------------------------

    def on_outbound_transition(self, t: OutboundTransition) -> None:
        try:
            self._on_outbound_transition(t)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("portico call events: an outbound transition could not be applied")

    def _on_outbound_transition(self, t: OutboundTransition) -> None:
        if t.state == "ended":
            call = self._live.get(t.call_id)
            if call is None or t.prev_state == "idle":
                return
            reached = (call.call_id, PHASE_ACTIVE) in call.fired
            self._finish(call, OUTCOME_ANSWERED if reached else OUTCOME_MISSED, t.at_wall)
            return
        phase = _OUTBOUND_PHASE.get(t.state)
        if phase is None or t.call_id in self._done:
            return
        call = self._live.get(t.call_id)
        if call is None:
            door_name, _ring_only = self._door_facts(t.door_id)
            call = _Live(call_id=t.call_id, kind=KIND_OUTBOUND, direction="out", door_id=t.door_id, door_name=door_name)
            self._live[t.call_id] = call
        self._phase_event(call, phase, t.at_wall)

    # ---- house calls ----------------------------------------------------------------------------------------------

    def house_moment(self, call_id: str, phase: str, *, from_name: str | None = None, to_name: str | None = None,
                     answered_by: str | None = None, reason: str | None = None) -> None:
        """One call-level moment of a house call (house.py: place, answer, mark_active, `_end`). Never raises."""
        try:
            now = time.time()
            if phase == PHASE_ENDED:
                call = self._live.get(call_id)
                if call is not None:
                    call.answered_by = answered_by or call.answered_by
                    self._finish(call, reason or OUTCOME_UNKNOWN, now)
                return
            if call_id in self._done:
                return
            call = self._live.get(call_id)
            if call is None:
                call = _Live(call_id=call_id, kind=KIND_HOUSE, from_name=from_name, to_name=to_name)
                self._live[call_id] = call
            if answered_by:
                call.answered_by = answered_by
            self._phase_event(call, phase, now)
        except Exception:  # noqa: BLE001 - the house call model must never be touched by this
            _LOGGER.exception("portico call events: a house call moment could not be applied")

    # ---- firing ---------------------------------------------------------------------------------------------------

    def _payload(self, call: _Live, phase: str, wall: float) -> dict[str, Any]:
        data: dict[str, Any] = {"phase": phase, "kind": call.kind, "call_id": call.call_id, "at": _iso(wall)}
        for key, value in (("direction", call.direction), ("door_id", call.door_id), ("door_name", call.door_name),
                           ("from", call.from_name), ("to", call.to_name), ("answered_by", call.answered_by)):
            if value:
                data[key] = value
        return data

    def _phase_event(self, call: _Live, phase: str, wall: float) -> None:
        key = (call.call_id, phase)
        if key in call.fired:
            return                                    # a repeated `ringing` for one call is a normal transition
        call.fired.add(key)
        call.at[phase] = wall
        call.last_mono = self._clock()
        self._fire(EVENT_CALL, self._payload(call, phase, wall))
        self._changed()

    def _finish(self, call: _Live, outcome: str, wall: float) -> None:
        """The one `ended` of a call: drop it from the live set, remember the id, fire, log, schedule the stats."""
        if self._live.pop(call.call_id, None) is None:
            return
        self._done[call.call_id] = None
        while len(self._done) > _DONE_MEMORY:
            self._done.popitem(last=False)
        call.fired.add((call.call_id, PHASE_ENDED))
        payload = self._payload(call, PHASE_ENDED, wall)
        payload["outcome"] = outcome
        first = call.at.get(PHASE_RINGING)
        answered = call.at.get(PHASE_ANSWERED, call.at.get(PHASE_ACTIVE))
        if first is not None:
            payload["ring_s"] = _round((answered if answered is not None else wall) - first)
        if answered is not None:
            payload["talk_s"] = _round(wall - answered)
        self.last_call = {k: v for k, v in payload.items() if k != "phase"}
        self._fire(EVENT_CALL, payload)
        self._logbook(call, payload)
        self._schedule_stats(call)
        self._changed()

    def _fire(self, event_type: str, data: dict[str, Any]) -> None:
        try:
            self.hass.bus.async_fire(event_type, data)
        except Exception:  # noqa: BLE001 - never let a bus listener's failure stop the call path
            _LOGGER.exception("portico call events: %s could not be fired", event_type)

    def _changed(self) -> None:
        for listener in list(self._listeners):
            try:
                listener()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("portico call events: a listener raised")

    def _logbook(self, call: _Live, payload: dict[str, Any]) -> None:
        """One logbook line per ended call."""
        label = {KIND_DOOR: call.door_name or "Door", KIND_OUTBOUND: f"Talk to {call.door_name or 'door'}",
                 KIND_HOUSE: "House call"}[call.kind]
        text = f"{label}: {payload['outcome']}"
        if call.kind == KIND_HOUSE and call.from_name:
            text += f" ({call.from_name} to {call.to_name or 'unknown'})"
        times = [f"{name} {payload[key]:g} s" for key, name in (("ring_s", "rang"), ("talk_s", "talked")) if key in payload]
        if times:
            text += f" ({', '.join(times)})"
        self._fire("logbook_entry", {"name": "Portico call", "message": text, "domain": DOMAIN})

    # ---- stats ----------------------------------------------------------------------------------------------------

    def _schedule_stats(self, call: _Live) -> None:
        call_id = call.call_id
        door_id = call.door_id

        @callback
        def _fire_stats(_now) -> None:
            self._stats_timers.pop(call_id, None)
            parts = call_stats_parts(self.hass, call_id)
            if not parts:
                return                                # no counters arrived: no event
            merged: dict[str, Any] = {}
            for part in parts:
                merged.update(part)
            data = {"call_id": call_id, **merged}
            if door_id and "door_id" not in data:
                data["door_id"] = door_id
            # The line for whoever has the log level raised; the numbers are read back from the event and its logbook row.
            _LOGGER.info("portico call %s audio: %s", call_id,
                         {key: value for key, value in merged.items() if not isinstance(value, list)})
            self._fire(EVENT_CALL_STATS, data)

        self._stats_timers[call_id] = async_call_later(
            self.hass, STATS_DELAY_S, HassJob(_fire_stats, cancel_on_shutdown=True))

    # ---- the pruner -----------------------------------------------------------------------------------------------

    @callback
    def _prune_tick(self, _now=None) -> None:
        self.prune()

    def prune(self) -> list[str]:
        """Ask each live call's source whether it still holds the call; drop (`ended / lost`) the ones it does not, when their
        last event is more than a prune interval old. Returns the dropped ids."""
        now = self._clock()
        dropped = []
        for call in list(self._live.values()):
            if now - call.last_mono <= PRUNE_INTERVAL_S:
                continue
            if self._source_holds(call) is False:
                self._finish(call, OUTCOME_LOST, time.time())
                dropped.append(call.call_id)
        return dropped

    def _source_holds(self, call: _Live) -> bool | None:
        """True or False when the call's own source can say; None when it cannot be asked (the call is kept)."""
        try:
            if call.kind == KIND_HOUSE:
                house = self.hass.data.get(DOMAIN, {}).get("house")      # house.DATA_HOUSE; read, never imported
                return house is not None and call.call_id in house._calls
            registry = self.hass.data.get(DOMAIN, {}).get(DATA_PROVIDERS)
            if registry is None:
                return None
            found = registry.door(call.door_id) if call.door_id else None
            if not found:
                return False                          # the door is gone, and with it the call
            snapshot = found[0].call_state(found[1].id)
            if snapshot is None:
                return False
            key = "outbound_call_id" if call.kind == KIND_OUTBOUND else "call_id"
            return snapshot.get(key) == call.call_id
        except Exception:  # noqa: BLE001 - a source that cannot answer keeps the call
            _LOGGER.debug("portico call events: could not ask the source of %s", call.call_id, exc_info=True)
            return None


@callback
def async_setup_call_events(hass: HomeAssistant) -> CallEvents:
    """Domain setup: build the helper once and start it. Idempotent."""
    data = hass.data.setdefault(DOMAIN, {})
    helper = data.get(DATA_CALL_EVENTS)
    if helper is None:
        helper = data[DATA_CALL_EVENTS] = CallEvents(hass)
        helper.start()
    return helper
