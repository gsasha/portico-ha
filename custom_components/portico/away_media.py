"""Call audio over the signaling WebSocket, for a phone that cannot reach the SFU's UDP port.

Design: automation/portico_away_media_design.md (estate repo), option A. Always on: there is no
option any more (the owner, 2026-11-12; a stored `away_media` option is ignored, door/const.py). The door's provider still
publishes an `away_media` flag, which now says only whether the door CAN carry call audio (a ring-only door without
talk-back cannot); where it is false every command here answers `away_media_disabled`.

How it works. The phone claims the call with `portico/answer` (browser/relay mode, unchanged).
It then sends `portico/media_open {door_id, call_id, codec}` on the SAME websocket
connection. We accept it only from the connection that made the successful claim, for a call
that is answered or active, from an enrolled phone, once. The door's provider gets a
`media_open` with two callbacks, one per downlink frame and one for the leg closing on its own
(this integration's provider asks the transport binary over IPC, internal/wsmedia: a client leg
joined to the device like a browser's WebRTC session). The reply is the command's result;
downlink audio then arrives as events on the same command id: {"t": "f", "d": <base64 frame>}
and a last {"t": "end", "reason": ...}. The claim table is this module's (which connection holds
the call); "already answered" is the provider's one arbitration.
Uplink is `portico/media_frame {call_id, d}`: a command that is never answered on success
(25 per second) and is handled in order on the event loop.

A Talk call the phone placed (`out-` id, away-talk plan) skips the claim: it is accepted from a connection that is
the talk relay's audience for that call (same HA user as the placing endpoint, holding a `portico/calls/subscribe`
stream), while the tracker's outbound slot says `dialing` or `in_call`. Everything after that is the same.

The channel ends when: the phone sends `portico/media_close`; its connection closes or
unsubscribes; the call leaves answered/active; or the transport reports the leg gone (no
uplink for 5 s, device hang-up). Wire details: contract/ws_media_v2.json.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from . import auth
from .call_events import add_stats_part
from .door.remote_cfg import SERVER_DRIVER
from .provider import error_code
from .talk_relay import get_talk_relay

_LOGGER = logging.getLogger(__name__)

CODECS = ("pcmu", "l16")
OUTBOUND_PREFIX = "out-"        # the id of a call Portico placed (the hub mints it)
_DATA_KEY = "portico_away_media"
# How many unclaimed-for-media claims are remembered; a phone opens the channel right after the claim.
_MAX_CLAIMS = 8


LAG_INTERVAL_S = 0.04                        # the loop-lag sampler's period: one frame
LAG_BOUNDS_MS = (5, 10, 20, 40, 80, 160, 320, 640)


class LoopLag:
    """DEV-811 P3: how late a 40 ms sleeping task wakes while a call's channel is open: a loop that is busy delays every frame
    through it, and the phone's own backlog guard cannot see that. Max exact; p95 from a fixed histogram (the upper bound of the
    bin where 95 % of the samples are reached, capped by the max). The clock and the sleep are injected for the tests."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._bins = [0] * (len(LAG_BOUNDS_MS) + 1)
        self.samples = 0
        self.max_ms = 0.0

    def note(self, late_ms: float) -> None:
        late_ms = max(float(late_ms), 0.0)
        self.samples += 1
        self.max_ms = max(self.max_ms, late_ms)
        for i, bound in enumerate(LAG_BOUNDS_MS):
            if late_ms <= bound:
                self._bins[i] += 1
                return
        self._bins[-1] += 1

    def summary(self) -> dict[str, float]:
        if not self.samples:
            return {}
        need = -(-self.samples * 95 // 100)          # ceil(0.95 * n), in integers
        seen = 0
        p95 = self.max_ms
        for i, count in enumerate(self._bins):
            seen += count
            if seen >= need:
                p95 = min(float(LAG_BOUNDS_MS[i]), self.max_ms) if i < len(LAG_BOUNDS_MS) else self.max_ms
                break
        return {"loop_lag_ms_max": round(self.max_ms, 1), "loop_lag_ms_p95": round(p95, 1)}

    async def run(self, sleep: Callable[[float], Any] = asyncio.sleep) -> None:
        """Sample until cancelled."""
        while True:
            due = self._clock() + LAG_INTERVAL_S
            await sleep(LAG_INTERVAL_S)
            self.note((self._clock() - due) * 1000.0)


@dataclass
class _Channel:
    call_id: str
    connection: Any
    msg_id: int
    provider: Any
    unsubscribe_state: Callable[[], None] | None = None
    closed: bool = False
    # DEV-811 P3: the integration's own numbers, kept only when the door's wheel lists `call_stats`
    stats: bool = False
    frames_down: int = 0
    frames_up: int = 0
    lag: LoopLag | None = None
    lag_task: Any = None


@dataclass
class _Registry:
    claims: dict[str, Any] = field(default_factory=dict)  # call_id -> the claiming connection
    channels: dict[str, _Channel] = field(default_factory=dict)  # call_id -> channel


def _registry(hass: HomeAssistant) -> _Registry:
    return hass.data.setdefault(_DATA_KEY, _Registry())


def record_claim(hass: HomeAssistant, enabled: bool, call_id: str, connection) -> None:
    """Called by portico/answer on the provider's verdict `answered` (relay mode): remembers WHICH connection holds the
    call, so only it may open the audio channel. A no-op while the feature is off (`enabled` is the
    door's published `away_media`)."""
    if not enabled:
        return
    claims = _registry(hass).claims
    claims.pop(call_id, None)
    claims[call_id] = connection
    while len(claims) > _MAX_CLAIMS:
        claims.pop(next(iter(claims)))


def _send_end(channel: _Channel, reason: str) -> None:
    try:
        channel.connection.send_event(channel.msg_id, {"t": "end", "reason": reason})
    except Exception:  # noqa: BLE001 - the connection may already be gone
        _LOGGER.debug("portico away media: could not send the end event", exc_info=True)


def _close(hass: HomeAssistant, channel: _Channel, reason: str, *, tell_transport: bool) -> None:
    if channel.closed:
        return
    channel.closed = True
    reg = _registry(hass)
    if reg.channels.get(channel.call_id) is channel:
        del reg.channels[channel.call_id]
    reg.claims.pop(channel.call_id, None)
    if channel.unsubscribe_state is not None:
        channel.unsubscribe_state()
        channel.unsubscribe_state = None
    channel.connection.subscriptions.pop(channel.msg_id, None)
    _finish_stats(hass, channel)
    _send_end(channel, reason)
    if tell_transport:
        hass.async_create_task(_tell_provider_close(channel.provider, channel.call_id, reason))


def _finish_stats(hass: HomeAssistant, channel: _Channel) -> None:
    """The channel is over: stop the sampler and hand the integration's numbers to the call's stats (best effort)."""
    if not channel.stats:
        return
    try:
        if channel.lag_task is not None:
            channel.lag_task.cancel()
            channel.lag_task = None
        part: dict[str, Any] = {"ha_frames_down": channel.frames_down, "ha_frames_up": channel.frames_up}
        if channel.lag is not None:
            part.update(channel.lag.summary())
        add_stats_part(hass, channel.call_id, part)
    except Exception:  # noqa: BLE001 - counters never break the close
        _LOGGER.debug("portico away media: could not record the call's numbers", exc_info=True)


async def _tell_provider_close(provider, call_id: str, reason: str) -> None:
    try:
        await provider.media_close(call_id, reason)
    except Exception:  # noqa: BLE001 - the provider may be gone; the leg dies with it
        _LOGGER.debug("portico away media: media_close failed", exc_info=True)


def _is_server_driver(provider, door_id: str) -> bool:
    """Ask the provider; the door's config entry is the provider's own business, never reached into from here."""
    check = getattr(provider, "door_is_server_driver", None)
    return bool(callable(check) and check(door_id))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "portico/media_open",
        vol.Required("door_id"): str,
        vol.Required("call_id"): str,
        vol.Optional("codec", default="pcmu"): vol.In(CODECS),
    }
)
@websocket_api.async_response
async def handle_media_open(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    if not auth.require(connection, msg, "portico/media_open"):
        return
    from .signaling import _door_for, _phone_ids, _registry as _provider_registry

    found = _door_for(hass, connection, msg, msg["door_id"])
    if found is None:
        return
    _registry_, provider, door = found
    if not door.away_media:
        connection.send_error(msg["id"], "away_media_disabled", "call audio over the websocket is not enabled")
        return
    user = connection.user
    # enrolled for THIS door: the provider hands this phone the door's secrets (PROV-07)
    if not any(provider.secrets_for(door.id, phone_id) is not None for phone_id in _phone_ids(hass, user.id if user else None, door.id)):
        connection.send_error(msg["id"], "not_enrolled", "only an enrolled phone may open call audio")
        return
    call_id = msg["call_id"]
    call_state = provider.call_state(door.id)
    reg = _registry(hass)
    outbound = call_id.startswith(OUTBOUND_PREFIX)
    if outbound:
        # A Talk call the phone placed (away-talk plan, portico_front_door_talk_plan.md section 13): the call lives in the
        # tracker's outbound slot, and its owner is the talk relay's audience for it, not a `portico/answer` claim.
        if (call_state is None or call_state.get("outbound_call_id") != call_id
                or call_state.get("outbound_state") not in ("dialing", "in_call")):
            connection.send_error(msg["id"], "unknown_call", call_id)
            return
        relay = get_talk_relay(hass)
        if relay is None or not relay.owns(connection, door.id, call_id):
            connection.send_error(msg["id"], "not_claimer", "this connection did not place the call")
            return
    else:
        # A gate_sip_server call stays `ringing` after `portico/answer` (the hub answers when the audio leg attaches, and
        # media_open IS that leg), so for that driver only a claimer may open audio on a ringing call.
        ok_states = ("answered", "active")
        if (call_state is not None and call_state.get("state") == "ringing"
                and reg.claims.get(call_id) is connection and _is_server_driver(provider, door.id)):
            ok_states = ("ringing", "answered", "active")
        if call_state is None or call_state["call_id"] != call_id or call_state["state"] not in ok_states:
            connection.send_error(msg["id"], "unknown_call", call_id)
            return
        if reg.claims.get(call_id) is not connection:
            connection.send_error(msg["id"], "not_claimer", "this connection does not hold the call")
            return
    existing = reg.channels.get(call_id)
    if existing is not None and not existing.closed:
        connection.send_error(msg["id"], "already_open", call_id)
        return
    channel = _Channel(call_id=call_id, connection=connection, msg_id=msg["id"], provider=provider)
    reg.channels[call_id] = channel  # before the await: a second open must see it
    wants = getattr(provider, "door_wants_stats", None)
    try:
        channel.stats = bool(callable(wants) and wants(door.id))
    except Exception:  # noqa: BLE001 - "cannot tell" is "no"
        channel.stats = False

    def _on_frame(data: str) -> None:
        if not channel.closed:
            channel.frames_down += 1
            connection.send_event(channel.msg_id, {"t": "f", "d": data})

    def _on_closed(reason: str) -> None:
        _close(hass, channel, reason, tell_transport=False)

    try:
        result = await provider.media_open(call_id, msg["codec"], _on_frame, _on_closed)
    except Exception as err:  # noqa: BLE001 - a provider's failure carries a code; anything else is "no transport"
        reg.channels.pop(call_id, None)
        code = error_code(err)
        if code is not None:
            connection.send_error(msg["id"], code, str(getattr(err, "message", err)))
        else:
            connection.send_error(msg["id"], "no_transport", "the transport is not reachable")
        return

    if channel.stats and not channel.closed:
        channel.lag = LoopLag()
        channel.lag_task = hass.async_create_background_task(channel.lag.run(), "portico call loop lag")

    def _on_state(transition) -> None:
        if transition.state == "ended" and transition.call_id == call_id:
            _close(hass, channel, "call_ended", tell_transport=True)

    # An outbound call's transitions never reach the visitors' listeners (provider.py OutboundTransition).
    registry = _provider_registry(hass)
    channel.unsubscribe_state = (registry.subscribe_outbound(_on_state) if outbound else registry.subscribe(_on_state))

    def _on_unsubscribe() -> None:
        _close(hass, channel, "unsubscribed", tell_transport=True)

    # HA calls every subscription's remover when the connection closes, and on unsubscribe_events.
    connection.subscriptions[msg["id"]] = _on_unsubscribe
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "portico/media_frame",
        vol.Required("call_id"): str,
        vol.Required("d"): str,
    }
)
@callback
def handle_media_frame(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    if msg["call_id"].startswith("hso-"):
        # A house call's leg (M7 3b-S, house_bridge.py): never a door's channel, never the code below.
        from .house_commands import route_house_frame

        route_house_frame(hass, connection, msg)
        return
    channel = _registry(hass).channels.get(msg["call_id"])
    if channel is not None and not channel.closed and channel.connection is connection:
        # The owner of an open channel: this very connection passed auth.require("portico/media_open") when it
        # opened it, and a websocket connection's user is fixed for its lifetime. 25 frames a second per call
        # must not rescan the enrolled phones each time (R3-04). Everyone else still meets the table below.
        pass
    elif not auth.require(connection, msg, "portico/media_frame"):
        return
    elif channel is None or channel.closed:
        connection.send_error(msg["id"], "unknown_session", msg["call_id"])
        return
    else:
        return  # a frame from anyone but the channel's owner is dropped, with no answer
    channel.frames_up += 1
    try:
        channel.provider.media_frame(msg["call_id"], msg["d"])
    except Exception:  # noqa: BLE001 - a dead leg: the call state will end the channel
        _LOGGER.debug("portico away media: frame not sent", exc_info=True)


@websocket_api.websocket_command({vol.Required("type"): "portico/media_close", vol.Required("call_id"): str})
@callback
def handle_media_close(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    if not auth.require(connection, msg, "portico/media_close"):
        return
    if msg["call_id"].startswith("hso-"):
        from .house_commands import route_house_close

        route_house_close(hass, connection, msg)
        return
    channel = _registry(hass).channels.get(msg["call_id"])
    if channel is not None and channel.connection is connection:
        _close(hass, channel, "closed_by_phone", tell_transport=True)
    connection.send_result(msg["id"], {"ok": True})


def async_setup_away_media(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, handle_media_open)
    websocket_api.async_register_command(hass, handle_media_frame)
    websocket_api.async_register_command(hass, handle_media_close)
