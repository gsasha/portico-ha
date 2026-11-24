"""The relay that tells the phone that placed a Talk call how it is going (M7 P3-B; house-calls design 15.15 (d)).

Why it exists. A Talk press (`content.ContentStore._run_talk`) dials the door and, once the door answered, replies
with the `out-` call id. The call's later life (`in_call`, `ended`: the station hung up, the 300 s cap, the link)
reaches Python as outbound transitions on the tracker's side slot (call_state.CallStateTracker.apply_outbound_event),
and nothing forwarded them: the phone would never hear `ended`. This module forwards them, to ONE audience.

The event. On the stream a phone already has open (`portico/calls/subscribe`)::

    {"type": "talk_state", "door_id": ..., "call_id": "out-..." | null, "state": "dialing" | "in_call" | "ended",
     "reason": "busy|not_connected|failed|not_available|no_answer|timeout"   # on `ended`, only when there is one}

- `dialing` is synthesised here when the dial is placed: the driver reports no such state (it blocks until the door
  answers), and the call id does not exist yet, so `call_id` is null. `in_call` and `ended` come from the tracker with
  the real id. `ended` also comes from here, with a `reason`, when the dial itself fails (`call_id` null) or the
  placement outlives its watchdog (`timeout`). A tracker `ended` carries no `reason`.
- It goes ONLY to the streams of the phone that placed the call (the placing endpoint's own HA user). Never to another
  phone, never to a dashboard, never into the push path, never into `call_state_changed`, `sensor.call_state`, the door
  entities or the bus event `portico_intercom_call`: the tracker keeps an outbound call off every one of those, and
  this module reads only the tracker's side slot, through the provider seam (`subscribe_outbound`).
- Only a call a Talk press placed is relayed. An outbound call anyone else placed (the old app, `portico/dial`) has
  no placement here and is ignored.
- A client that does not know `talk_state` ignores it (the app's SubscribeTracker and the card do).

When the placing phone's last stream is gone, the call is hung up after TALK_DROP_GRACE_S (a phone back inside the
grace keeps it). The app ends its own side after 8 s of link loss, so the server's grace is the longer one.

Inert: with no door that reports `CanDial` (every door today) `_run_talk` refuses before it reaches this module, no
provider sends an outbound transition, and the only effect of this module's existence is an empty list of streams.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, callback

from .provider import DATA_PROVIDERS, DOMAIN, OutboundTransition, ProviderRegistry, error_code

_LOGGER = logging.getLogger(__name__)

DATA_TALK_RELAY = "talk_relay"

EVENT_TYPE = "talk_state"
STATE_DIALING = "dialing"
STATE_IN_CALL = "in_call"
STATE_ENDED = "ended"

# The reasons portico-app maps to its own sentences (P3-C); anything else would degrade to a generic line there, so
# nothing else is ever sent. A call that simply ends (the door hung up, a hang-up from the phone) carries NO reason.
REASON_TIMEOUT = "timeout"      # the placement outlived its watchdog without an end
REASON_FAILED = "failed"        # the dial raised without a usable code, or was cancelled
WIRE_REASONS = frozenset({"busy", "not_connected", "failed", "not_available", "no_answer", "timeout"})
_REASON_ALIASES = {"gate_busy": "busy", "device_busy": "busy", "yielded": "busy", "dial_disabled": "not_available",
                   "not_supported": "not_available", "claims_disabled": "not_available"}


def wire_reason(code: str | None) -> str:
    """A dial error code as one of WIRE_REASONS: known ones as they are, a few synonyms mapped, the rest `failed`."""
    code = _REASON_ALIASES.get(code or "", code)
    return code if code in WIRE_REASONS else REASON_FAILED

# How long a placing phone may have no stream open before its call is hung up. The app ends its own side after 8 s of
# link loss; this is the longer one, so a call is normally ended by the app first.
TALK_DROP_GRACE_S = 15.0
# The longest a placement is kept without an end: the dial's wait (gate-sip's 120 s ceiling), the 300 s call cap and slack.
PLACEMENT_MAX_S = 450.0


@dataclass(eq=False)
class Placement:
    """One Talk call being placed or going: who placed it, where, and what the phone has been told."""

    endpoint_id: str
    user_id: str
    provider: Any
    domain: str
    door_id: str
    call_id: str | None = None
    sent: str | None = None            # the last state told to the phone
    abandoned: bool = False            # the phone left before the dial returned: hang up when the id is known
    finished: bool = False
    timers: list = field(default_factory=list)
    drop_timer: Any = None             # the pending hang-up of a call whose phone has no stream open


class _Stream:
    __slots__ = ("connection", "msg_id", "user_id")

    def __init__(self, connection: Any, msg_id: int, user_id: str) -> None:
        self.connection = connection
        self.msg_id = msg_id
        self.user_id = user_id


class StreamHandle:
    def __init__(self, relay: "OutboundCallRelay", stream: _Stream) -> None:
        self._relay = relay
        self._stream = stream

    def detach(self) -> None:
        self._relay._detach(self._stream)


class OutboundCallRelay:
    def __init__(self, hass: HomeAssistant, registry: ProviderRegistry | None, *,
                 drop_grace: float = TALK_DROP_GRACE_S, max_seconds: float = PLACEMENT_MAX_S) -> None:
        self.hass = hass
        self._drop_grace = drop_grace
        self._max_seconds = max_seconds
        self._streams: list[_Stream] = []
        self._by_door: dict[tuple[str, str], Placement] = {}
        self._tasks: set = set()
        self._unsub = registry.subscribe_outbound(self._on_outbound) if registry is not None else None

    def stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        for placement in list(self._by_door.values()):
            self._finish(placement)
        for task in list(self._tasks):
            task.cancel()

    # ---- streams (portico/calls/subscribe) ------------------------------------------------------------------------

    @callback
    def attach(self, connection: Any, msg_id: int) -> StreamHandle | None:
        """An open `portico/calls/subscribe`. None for a connection with no user (it can be nobody's phone)."""
        user = getattr(connection, "user", None)
        user_id = getattr(user, "id", None)
        if not user_id:
            return None
        stream = _Stream(connection, msg_id, user_id)
        self._streams.append(stream)
        for placement in self._by_door.values():
            if placement.user_id == user_id:
                self._cancel_drop(placement)     # the phone is back inside the grace
        return StreamHandle(self, stream)

    def _detach(self, stream: _Stream) -> None:
        if stream not in self._streams:
            return
        self._streams.remove(stream)
        for placement in list(self._by_door.values()):
            if placement.user_id == stream.user_id:
                self._check_drop(placement)

    def _has_stream(self, user_id: str) -> bool:
        return any(s.user_id == user_id for s in self._streams)

    def owns(self, connection: Any, door_id: str, call_id: str) -> bool:
        """Is `connection` the audience of the Talk call `call_id` on `door_id`? Yes when a live placement for that call
        belongs to the connection's HA user AND the connection holds one of that user's `portico/calls/subscribe`
        streams (the stream this module sends `talk_state` to). That is the whole claim a Talk call has: it is what
        lets the phone carry the call's audio as frames (away_media.py). Never raises."""
        try:
            user_id = getattr(getattr(connection, "user", None), "id", None)
            if not user_id or not any(s.connection is connection and s.user_id == user_id for s in self._streams):
                return False
            return any(p.door_id == door_id and p.call_id == call_id and p.user_id == user_id and not p.finished
                       for p in self._by_door.values())
        except Exception:  # noqa: BLE001
            return False

    # ---- placing (ContentStore._run_talk) -----------------------------------------------------------------------------

    def placing(self, endpoint_id: str, user_id: str | None, provider: Any, door_id: str) -> Placement | None:
        """A Talk dial is about to be made: remember who placed it and tell that phone `dialing`. None (nothing
        relayed) when there is no user to tell or the door already has a placement. Never raises."""
        try:
            domain = getattr(provider, "domain", None)
            if not user_id or not isinstance(domain, str):
                return None
            key = (domain, door_id)
            if key in self._by_door:
                return None
            placement = Placement(endpoint_id, user_id, provider, domain, door_id)
            self._by_door[key] = placement
            loop = self.hass.loop
            placement.timers.append(loop.call_later(self._max_seconds, self._expire, placement))
            self._send(placement, STATE_DIALING, None)
            self._check_drop(placement)
            return placement
        except Exception:  # noqa: BLE001 - the relay must never stop a dial
            _LOGGER.exception("portico talk relay: placing failed")
            return None

    def placed(self, placement: Placement | None, call_id: str) -> None:
        """The dial returned this call id (the door answered). Never raises."""
        if placement is None or placement.finished:
            return
        try:
            if placement.call_id not in (None, call_id):
                _LOGGER.warning("portico talk relay: the dial returned %s but %s was followed", call_id, placement.call_id)
            placement.call_id = call_id
            if placement.abandoned:
                self._start_task(self._hangup(placement))
        except Exception:  # noqa: BLE001
            _LOGGER.exception("portico talk relay: placed failed")

    def failed(self, placement: Placement | None, err: BaseException | str) -> None:
        """The dial raised (or was cancelled): the phone is told `ended` with a reason and the placement is gone."""
        if placement is None or placement.finished:
            return
        try:
            reason = wire_reason(err if isinstance(err, str) else error_code(err))
            self._send(placement, STATE_ENDED, placement.call_id, reason)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("portico talk relay: failed() failed")
        finally:
            self._finish(placement)

    # ---- the tracker's side slot (through the provider seam) -------------------------------------------------------

    def _on_outbound(self, t: OutboundTransition) -> None:
        placement = self._by_door.get((t.provider, t.door_id))
        if placement is None or placement.finished:
            return                   # a call nobody here placed: not ours to tell anyone
        if placement.call_id is None:
            if t.state == STATE_ENDED:
                return               # an end with no call of ours going yet is some earlier call's, never ours
            placement.call_id = t.call_id
        elif t.call_id != placement.call_id:
            return                   # another call on the same door
        _LOGGER.info("portico talk relay: outbound call %s on %s -> %s", t.call_id, t.door_id, t.state)
        if t.state == STATE_ENDED:
            self._send(placement, STATE_ENDED, t.call_id)
            self._finish(placement)
            return
        if t.state == placement.sent or (t.state == STATE_DIALING and placement.sent == STATE_IN_CALL):
            return                   # told already (the synthesised `dialing`), or a step back
        self._send(placement, t.state, t.call_id)

    # ---- sending ------------------------------------------------------------------------------------------------------------

    def _send(self, placement: Placement, state: str, call_id: str | None, reason: str | None = None) -> None:
        event: dict[str, Any] = {"type": EVENT_TYPE, "door_id": placement.door_id, "call_id": call_id, "state": state}
        if state == STATE_ENDED and reason:
            event["reason"] = reason
        placement.sent = state
        for stream in list(self._streams):
            if stream.user_id != placement.user_id:
                continue
            try:
                stream.connection.send_event(stream.msg_id, dict(event))
            except Exception:  # noqa: BLE001 - a dead socket must not stop the others or the call
                _LOGGER.debug("portico talk relay: send failed", exc_info=True)

    # ---- the end of a placement ---------------------------------------------------------------------------------------

    def _finish(self, placement: Placement) -> None:
        placement.finished = True
        for timer in placement.timers:
            timer.cancel()
        placement.timers.clear()
        self._cancel_drop(placement)
        key = (placement.domain, placement.door_id)
        if self._by_door.get(key) is placement:
            del self._by_door[key]

    def _expire(self, placement: Placement) -> None:
        if placement.finished:
            return
        _LOGGER.warning("portico talk relay: the call on %s outlived %ss; ending it", placement.door_id, self._max_seconds)
        self._send(placement, STATE_ENDED, placement.call_id, REASON_TIMEOUT)
        self._finish(placement)
        if placement.call_id is not None:
            self._start_task(self._hangup(placement))

    def _check_drop(self, placement: Placement) -> None:
        """No stream of the placing phone is open: hang the call up after the grace (unless already scheduled)."""
        if placement.finished or self._has_stream(placement.user_id) or placement.drop_timer is not None:
            return
        placement.drop_timer = self.hass.loop.call_later(self._drop_grace, self._drop, placement)

    def _cancel_drop(self, placement: Placement) -> None:
        if placement.drop_timer is not None:
            placement.drop_timer.cancel()
            placement.drop_timer = None

    def _drop(self, placement: Placement) -> None:
        placement.drop_timer = None
        if placement.finished or self._has_stream(placement.user_id):
            return
        if placement.call_id is None:
            placement.abandoned = True      # the dial is still going: hung up the moment it returns an id
            return
        self._start_task(self._hangup(placement))

    async def _hangup(self, placement: Placement) -> None:
        call_id = placement.call_id
        if call_id is None:
            return
        try:
            await placement.provider.hangup(call_id, "link_lost")
        except Exception as err:  # noqa: BLE001 - the call may be gone already
            _LOGGER.info("portico talk relay: the hang-up of %s failed: %s", call_id, type(err).__name__)

    def _start_task(self, coro) -> None:
        task = self.hass.async_create_background_task(coro, "portico talk relay hangup")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


def get_talk_relay(hass: HomeAssistant) -> OutboundCallRelay | None:
    """The relay, created on first use once the provider registry exists; None before that. It is created by
    the first `portico/calls/subscribe` or the first Talk press, and holds nothing until a Talk call is placed."""
    data = hass.data.setdefault(DOMAIN, {})
    relay = data.get(DATA_TALK_RELAY)
    if relay is None:
        registry = data.get(DATA_PROVIDERS)
        if registry is None:
            return None
        relay = data[DATA_TALK_RELAY] = OutboundCallRelay(hass, registry)

        @callback
        def _stop(_event: Event) -> None:
            relay.stop()          # no timer outlives Home Assistant

        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _stop)
    return relay
