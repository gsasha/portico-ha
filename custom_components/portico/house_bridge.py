"""House calls, the media glue between the call model and the hub's bridge (design 15.4, 15.12, 15.16; M7 PR 3b-S).

INERT until a provider can carry it: the house registry always exists (house calls are always on, D-20261122-aafb) and owns a
`HouseBridge`, and nothing here does a thing until a registered provider reports `house_bridge_supported()`
(the driver part's hub runs wheel >= 0.6.0 started with `-house-calls`, which needs its own option). With a 0.5.x wheel
the second never holds, so `portico/house/media_open` answers `not_supported`, a call is the 3a call (it ends `error` at
the 10 s audio deadline) and no IPC verb is ever sent.

How a house call gets its audio. The call model (house.py) is unchanged: place, ring, answer. Once a call is `answered`,
each of its two ends sends `portico/house/media_open {call_id, codec | sdp}` and that is how it DECLARES its leg:
no `sdp` is a frame channel over the signaling socket (the away path; downlink frames arrive as events on that command
id, uplink is `portico/media_frame {call_id, d}`, a goodbye is `portico/media_close {call_id}`), an `sdp` is a WebRTC
offer through the hub's SFU (the answer arrives as an event `{t: "sdp", sdp}`). The hub's `house_open` needs both legs
at once (exactly two, sides `from` = the caller and `to` = the callee), so the bridge opens when the second end has
declared; the first waits for it. The other ends' legs are never opened before an answer, so a microphone is never open
for the whole ring (design 8) and a callee that lost the race never has a leg.

What ends the bridge, always through the one door `HouseCallRegistry._end` -> `call_ended`: a hang-up from either end,
the 45 s and 10 s timers, a door call answered by one of the two (door outranks house), a phone whose last subscription
dropped, a leg the hub reports `lost`, a phone's frame channel closing, and the hub's process going away. `call_ended`
closes the bridge (`house_close`, idempotent) and tells each end's media command `{t: "end", reason}`.

Stay-out rules (15.4): a house call id never enters a door's tracker, `note_call` or `_require_call`; the hub's legs are in
its own house-owned table; the legs of a call are `house-<hex>-<side>` ids that no door call has.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import callback

from .provider import DATA_PROVIDERS, DOMAIN, error_code

_LOGGER = logging.getLogger(__name__)

SIDE_FROM, SIDE_TO = "from", "to"         # the caller's leg and the callee's (the hub's wire)
KIND_FRAMES, KIND_WEBRTC = "frames", "webrtc"
CODECS = ("pcmu", "l16")
OWNER_MAX = 64                            # the hub's bound on a leg owner's name
ATTACH_WAIT_S = 15                        # the first end waits at most this for the second (the 10 s audio deadline ends the call first)

CODE_NOT_SUPPORTED = "not_supported"      # no provider runs the bridge: the 0.5.x wheel, or the option off
CODE_UNKNOWN_CALL = "unknown_call"
CODE_ALREADY_OPEN = "already_open"
CODE_NO_TRANSPORT = "no_transport"
CODE_TOO_MANY = "too_many_calls"

# Kept as local strings so this module imports nothing from house.py (which imports it); a test pins them to house.py's.
_ANSWERED, _ACTIVE = "answered", "active"
_END_ERROR, _END_HANGUP, _END_BUSY = "error", "hangup", "busy"


@dataclass(eq=False)
class _Leg:
    """One end's declared leg and, once the bridge is open, its reply."""

    side: str
    endpoint: str
    connection: Any
    msg_id: int
    kind: str
    codec: str | None
    sdp: str | None
    done: asyncio.Future
    leg_id: str | None = None
    sdp_answer: str | None = None
    live: bool = False          # the bridge opened: callbacks registered, the connection's subscription set
    replied: bool = False       # the command's result has been (is being) sent: events may follow
    closed: bool = False


@dataclass(eq=False)
class _BCall:
    call_id: str
    provider: Any
    legs: dict[str, _Leg] = field(default_factory=dict)
    opening: bool = False
    opened: bool = False
    ended: bool = False


class HouseBridge:
    """The bridge glue of one `HouseCallRegistry`. Never raises into the call model."""

    def __init__(self, house: Any) -> None:
        self.house = house
        self._calls: dict[str, _BCall] = {}
        self._unsub = None

    # ---- lifecycle and feature detection ---------------------------------------------------------------------------

    def _registry(self):
        return self.house.hass.data.get(DOMAIN, {}).get(DATA_PROVIDERS)

    def start(self) -> None:
        registry = self._registry()
        if registry is not None and self._unsub is None:
            self._unsub = registry.subscribe_house(self.on_leg_state)

    def stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        for call_id in list(self._calls):
            self.call_ended(call_id, _END_ERROR)

    def provider(self) -> Any | None:
        """The provider that can bridge a house call now, or None (the capability check, asked every time: a hub that
        restarted onto another wheel changes the answer)."""
        registry = self._registry()
        return registry.house_provider() if registry is not None else None

    def supported(self) -> bool:
        return self.provider() is not None

    # ---- a client declares its leg -----------------------------------------------------------------------------------

    async def attach(self, connection: Any, msg_id: int, user_id: str | None, call_id: str, *, codec: str,
                     sdp: str | None) -> tuple[dict | None, str | None]:
        """`portico/house/media_open`. Returns (the result, None) or (None, an error code). The first end of a call waits
        here for the second; the second opens the bridge."""
        house = self.house
        call = house._calls.get(call_id)
        if call is None or call.state not in (_ANSWERED, _ACTIVE):
            return None, CODE_UNKNOWN_CALL
        endpoint = house._participant(user_id, call)
        if endpoint is not None and endpoint == call.caller:
            side = SIDE_FROM
        elif endpoint is not None and endpoint == call.answered_by:
            side = SIDE_TO
        else:
            return None, CODE_UNKNOWN_CALL          # a stranger, or a loser of the race: no leg, nothing learned
        provider = self.provider()
        if provider is None:
            return None, CODE_NOT_SUPPORTED
        bc = self._calls.get(call_id)
        if bc is None:
            bc = self._calls[call_id] = _BCall(call_id=call_id, provider=provider)
        if side in bc.legs:
            return None, CODE_ALREADY_OPEN
        leg = _Leg(side=side, endpoint=endpoint, connection=connection, msg_id=msg_id,
                   kind=KIND_WEBRTC if sdp else KIND_FRAMES, codec=None if sdp else codec, sdp=sdp,
                   done=asyncio.get_running_loop().create_future())
        bc.legs[side] = leg
        if len(bc.legs) == 2 and not bc.opening:
            bc.opening = True
            await self._open(call, bc)
        try:
            result, code = await asyncio.wait_for(leg.done, ATTACH_WAIT_S)
        except asyncio.TimeoutError:
            self._leg_closed(call_id, leg, "timeout")
            return None, CODE_UNKNOWN_CALL
        except asyncio.CancelledError:
            self._leg_closed(call_id, leg, "cancelled")      # the phone's socket closed while it waited
            raise
        if code is not None:
            return None, code
        if leg.closed:
            return None, CODE_UNKNOWN_CALL                    # the call ended between the open and this reply
        leg.replied = True
        asyncio.get_running_loop().call_soon(self._flush_sdp, leg)   # after the handler's send_result, never before it
        return result, None

    async def _open(self, call: Any, bc: _BCall) -> None:
        """Both ends have declared: `house_open` on the hub, register the frame legs, resolve both waiting commands."""
        legs = []
        for side in (SIDE_FROM, SIDE_TO):
            leg = bc.legs[side]
            spec: dict[str, Any] = {"side": side, "kind": leg.kind, "owner": leg.endpoint[:OWNER_MAX]}
            spec.update({"sdp": leg.sdp} if leg.kind == KIND_WEBRTC else {"codec": leg.codec})
            legs.append(spec)
        try:
            result = await bc.provider.house_open(call.call_id, legs)
        except asyncio.CancelledError:
            # The declaring end's socket closed while the hub was opening: nobody will own what the hub may have made.
            self._resolve_all(bc, None, CODE_UNKNOWN_CALL)
            self._spawn_close(bc.provider, call.call_id)
            if self.house._calls.get(call.call_id) is call:
                self.house._end(call, _END_ERROR)
            raise
        except Exception as err:  # noqa: BLE001 - a provider's failure carries a code; anything else is "no transport"
            code = error_code(err) or CODE_NO_TRANSPORT
            _LOGGER.warning("portico house: the bridge for %s did not open (%s)", call.call_id, code)
            self._resolve_all(bc, None, code)
            if self.house._calls.get(call.call_id) is call:
                self.house._end(call, _END_BUSY if code == CODE_TOO_MANY else _END_ERROR)
            return
        if bc.ended or self.house._calls.get(call.call_id) is not call or call.state not in (_ANSWERED, _ACTIVE):
            # The call ended while the hub was opening: the hub has a bridge nobody owns now.
            self._spawn_close(bc.provider, call.call_id)
            self._resolve_all(bc, None, CODE_UNKNOWN_CALL)
            return
        described = {d.get("side"): d for d in (result.get("legs") or []) if isinstance(d, dict)}
        if set(described) != {SIDE_FROM, SIDE_TO}:
            _LOGGER.warning("portico house: the hub's reply for %s did not describe both legs", call.call_id)
            self._resolve_all(bc, None, CODE_NO_TRANSPORT)
            self._spawn_close(bc.provider, call.call_id)
            self.house._end(call, _END_ERROR)
            return
        bc.opened = True
        for side, leg in bc.legs.items():
            desc = described[side]
            leg.live = True
            if leg.kind == KIND_FRAMES:
                leg.leg_id = desc.get("leg_id")
                self._wire_frames(bc, leg)
                reply = {"kind": KIND_FRAMES, "codec": desc.get("codec"), "frame_bytes": desc.get("frame_bytes"),
                         "frame_ms": desc.get("frame_ms")}
            else:
                reply = {"kind": KIND_WEBRTC}
                leg.connection.subscriptions[leg.msg_id] = self._unsubscriber(bc.call_id, leg)
            if not leg.done.done():
                leg.done.set_result((reply, None))

    def _wire_frames(self, bc: _BCall, leg: _Leg) -> None:
        """Downlink frames reach the end's command id as `{t: "f", d}`; the leg ending on its own closes the call."""
        def _on_frame(data: str) -> None:
            if leg.replied and not leg.closed:
                leg.connection.send_event(leg.msg_id, {"t": "f", "d": data})

        def _on_closed(reason: str) -> None:
            self._leg_closed(bc.call_id, leg, reason)

        bc.provider.house_media_open(leg.leg_id, _on_frame, _on_closed)
        leg.connection.subscriptions[leg.msg_id] = self._unsubscriber(bc.call_id, leg)

    def _unsubscriber(self, call_id: str, leg: _Leg):
        def _unsubscribe() -> None:           # HA calls it when the connection closes, and on unsubscribe_events
            self._leg_closed(call_id, leg, "unsubscribed")
        return _unsubscribe

    def _resolve_all(self, bc: _BCall, result: dict | None, code: str | None) -> None:
        for leg in bc.legs.values():
            if not leg.done.done():
                leg.done.set_result((result, code))

    def _flush_sdp(self, leg: _Leg) -> None:
        if leg.sdp_answer is not None and leg.replied and not leg.closed:
            sdp, leg.sdp_answer = leg.sdp_answer, None
            self._send(leg, {"t": "sdp", "sdp": sdp})

    @staticmethod
    def _send(leg: _Leg, event: dict) -> None:
        try:
            leg.connection.send_event(leg.msg_id, event)
        except Exception:  # noqa: BLE001 - the connection may already be gone
            _LOGGER.debug("portico house: a media event could not be sent", exc_info=True)

    # ---- inputs: the client's frames and goodbye, the hub's leg states -------------------------------------------------

    def uplink(self, connection: Any, call_id: str, data: str) -> None:
        """`portico/media_frame` for a house call: the frame goes to the hub only from the connection that owns a live
        frame-channel leg of that call; anyone else's is dropped with no answer."""
        bc = self._calls.get(call_id)
        if bc is None:
            return
        for leg in bc.legs.values():
            if leg.connection is connection and leg.live and leg.kind == KIND_FRAMES and not leg.closed:
                try:
                    bc.provider.house_media_frame(leg.leg_id, data)
                except Exception:  # noqa: BLE001 - a dead leg is the hub's `lost` to report
                    _LOGGER.debug("portico house: a frame was not sent", exc_info=True)
                return

    def phone_close(self, connection: Any, call_id: str, user_id: str | None) -> None:
        """`portico/media_close` for a house call: the end's goodbye hangs the call up."""
        bc = self._calls.get(call_id)
        if bc is None or not any(leg.connection is connection for leg in bc.legs.values()):
            return
        self.house.hangup(call_id, user_id)

    @callback
    def on_leg_state(self, raw: dict) -> None:
        """One `house_leg_state` from the hub (via the provider registry). Never raises into the provider's fan-out."""
        try:
            self._on_leg_state(raw)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("portico house: a leg state could not be applied")

    def _on_leg_state(self, raw: dict) -> None:
        call_id, state, side = raw["call_id"], raw["state"], raw["leg"]
        house = self.house
        call = house._calls.get(call_id)
        if call is None:
            return                                           # late: the call is over and the hub's is closing
        if state == "first_frame":
            if side == SIDE_TO:                              # design 4.1: active when the CALLEE's first frame arrives
                house.mark_active(call_id)
        elif state == "lost":
            if call.state in (_ANSWERED, _ACTIVE):
                _LOGGER.info("portico house: %s lost its %s leg (%s)", call_id, side, raw.get("reason"))
                house._end(call, _END_ERROR)
        elif state == "sdp":
            bc = self._calls.get(call_id)
            leg = bc.legs.get(side) if bc is not None else None
            if leg is not None and leg.kind == KIND_WEBRTC and isinstance(raw.get("sdp"), str):
                leg.sdp_answer = raw["sdp"]
                self._flush_sdp(leg)

    def _leg_closed(self, call_id: str, leg: _Leg, reason: str) -> None:
        """A leg is gone from this side (its socket closed, the hub closed the frame channel, nobody came): the call ends."""
        if leg.closed:
            return
        leg.closed = True
        bc = self._calls.get(call_id)
        if bc is not None and bc.legs.get(leg.side) is leg and not leg.live:
            del bc.legs[leg.side]                            # it never joined a bridge: free the side
        if not leg.done.done():
            leg.done.set_result((None, CODE_UNKNOWN_CALL))
        call = self.house._calls.get(call_id)
        if call is not None and call.state in (_ANSWERED, _ACTIVE):
            self.house._end(call, _END_ERROR)

    # ---- the call is over ---------------------------------------------------------------------------------------------

    def call_ended(self, call_id: str, reason: str) -> None:
        """From `HouseCallRegistry._end`, whatever the cause: close the hub's bridge and tell each end's media command."""
        bc = self._calls.pop(call_id, None)
        if bc is None:
            return
        bc.ended = True
        for leg in bc.legs.values():
            leg.closed = True
            if not leg.done.done():
                leg.done.set_result((None, CODE_UNKNOWN_CALL))
            if leg.live:
                leg.connection.subscriptions.pop(leg.msg_id, None)
                if leg.replied:
                    self._send(leg, {"t": "end", "reason": reason})
        if bc.opened:
            self._spawn_close(bc.provider, call_id)

    def _spawn_close(self, provider: Any, call_id: str) -> None:
        async def _close() -> None:
            try:
                await provider.house_close(call_id)
            except Exception:  # noqa: BLE001 - the hub may be gone; its bridges went with it
                _LOGGER.debug("portico house: house_close failed", exc_info=True)

        self.house.hass.async_create_task(_close())
