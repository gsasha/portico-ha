"""Live video for a camera tile (live video design, part 1: the server). Three websocket commands, siblings of
`portico/content/snapshot`, all `admin, phone, dashboard` in the auth table:

  portico/content/live {camera_id, sdp, size?, endpoint?}   a SUBSCRIPTION. The result is `{session_id, stream}`; the
                                                            events that follow are `{type: "session", session_id, stream}` first, then `{type: "answer", sdp}`,
                                                            `{type: "candidate", candidate}` and `{type: "error", code, message}`
  portico/content/live_ice {session_id, candidate}          one client ICE candidate -> `{}`
  portico/content/live_close {session_id}                   -> `{closed}`; an unsubscribe or a dropped websocket closes too

WebRTC, VIDEO ONLY: the card sends a recvonly video offer, there is no audio line. The offer is handed to Home Assistant's
OWN camera WebRTC support, in this process, the way HA's `camera/webrtc/offer` websocket handler does it (homeassistant/
components/camera/webrtc.py: `get_camera_from_entity_id`, the `StreamType.WEB_RTC` capability test, then
`camera.async_handle_async_webrtc_offer(offer, session_id, send_message)`; candidates through
`camera.async_on_webrtc_candidate`; a session closes with `camera.close_webrtc_session`). There is no second route to the
camera: no stream URL, no token, no entity id reaches the page, and go2rtc's own HTTP API is never touched.

THE GRANT is `content/snapshot`'s, one function (`ContentStore._granted_camera`): the caller class and the endpoint come
from `signaling._content_target`, the camera from the endpoint's resolved document by the id the card was given
(`unknown_camera` otherwise, logged). Then the same availability test (`unavailable`).

NO PRODUCT CAP (the owner's decision of 2026-10-25: "no cap but show a warning"). A very high TECHNICAL guard stops a
runaway client: MAX_LIVE_PER_ENDPOINT per endpoint and MAX_LIVE_PER_INSTALL in all, `too_many_live`.

A SESSION HAS NO EXPIRY while its websocket is open (the owner wants live tiles to keep running; a timer would only add a
renewal to get wrong). It dies with the subscription: `live_close`, `unsubscribe_events`, or the connection closing (HA
calls every subscription's remover on close), so a vanished card cannot leak one.

SIZE: `big` takes the camera's main stream, `small` its sub stream. A Home Assistant camera entity is one stream, so the
pair is an explicit lookup, never a guess: the settings entry's option `live_sub_cameras` maps a camera entity to the
entity that carries its sub stream (default: empty, so both sizes use the main one). A mapped sub entity that is not
available, or has no WebRTC, falls back to the main stream.

A camera Home Assistant cannot stream over WebRTC, or whose handler fails, is `live_unavailable`: the card stays on stills,
which are never removed.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError

from .content import RunRefused

_LOGGER = logging.getLogger(__name__)

MAX_LIVE_PER_ENDPOINT = 16          # a runaway guard, not a product limit
MAX_LIVE_PER_INSTALL = 32
MAX_SDP = 16384                     # bytes of the browser's offer
MAX_CANDIDATE = 2048
LIVE_SIZES = ("big", "small")
CONF_LIVE_SUB_CAMERAS = "live_sub_cameras"      # settings entry option: {main camera entity: sub-stream camera entity}

_DATA_KEY = "portico_live"

_UNAVAILABLE_TEXT = "live video is not available for this camera"


def new_session_id() -> str:
    """A session id the card holds. Random; a test (and the contract recording) replaces this function."""
    return secrets.token_hex(8)


@dataclass
class LiveSession:
    id: str
    endpoint_id: str
    connection: Any
    msg_id: int
    stream: str                             # "main" | "sub": which of the camera's streams this session carries
    camera: Any = None                      # HA's camera entity
    ha_session_id: str = ""
    closed: bool = False
    registry: Any = field(default=None, repr=False)
    camera_id: str = ""                     # the card's camera id (the diagnostic log line names it)
    opened: float = field(default_factory=time.monotonic)

    @callback
    def close(self, reason: str, *, by_hass: bool = False) -> None:
        """Idempotent. Forget the session, drop its websocket subscription and tell HA's camera to let go.
        `by_hass`: HA itself is removing the subscription (`unsubscribe_events`, or the connection closing and walking
        `connection.subscriptions`): it must not be popped from under that walk."""
        if self.closed:
            return
        self.closed = True
        if self.registry is not None and self.registry.sessions.get(self.id) is self:
            del self.registry.sessions[self.id]
        if not by_hass:
            self.connection.subscriptions.pop(self.msg_id, None)
        self._release()
        reg = self.registry
        _LOGGER.info("portico live: closed session %s of %s (camera %s, %s stream) after %ds: %s; now %d for it, %d in all",
                     self.id, self.endpoint_id, self.camera_id, self.stream, self.age(), reason,
                     reg.count(self.endpoint_id) if reg is not None else 0, reg.count() if reg is not None else 0)
        if reg is not None:
            reg.changed()

    def age(self) -> int:
        return int(time.monotonic() - self.opened)

    def _release(self) -> None:
        if self.camera is None or not self.ha_session_id:
            return
        try:
            self.camera.close_webrtc_session(self.ha_session_id)
        except Exception:  # noqa: BLE001 - the camera or its provider may be gone; nothing is left to close then
            _LOGGER.debug("portico live: closing HA's session failed", exc_info=True)


@dataclass
class _Registry:
    sessions: dict[str, LiveSession] = field(default_factory=dict)
    listeners: list[Callable[[], None]] = field(default_factory=list)

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """The live-count sensor: told whenever a session opens or closes. Returns the remover."""
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener) if listener in self.listeners else None

    def changed(self) -> None:
        for listener in list(self.listeners):
            try:
                listener()
            except Exception:  # noqa: BLE001 - a sensor must never break a session
                _LOGGER.debug("portico live: a count listener failed", exc_info=True)

    def held(self, endpoint_id: str) -> str:
        """`id (camera, stream, age)` for each session an endpoint holds, oldest first: who to look at on a refusal."""
        rows = sorted((s for s in self.sessions.values() if s.endpoint_id == endpoint_id), key=lambda s: s.opened)
        return "; ".join(f"{s.id} ({s.camera_id}, {s.stream}, {s.age()}s)" for s in rows) or "none"

    def count(self, endpoint_id: str | None = None) -> int:
        return sum(1 for s in self.sessions.values() if endpoint_id is None or s.endpoint_id == endpoint_id)


def registry(hass: HomeAssistant) -> _Registry:
    return hass.data.setdefault(_DATA_KEY, _Registry())


def _sub_entity(hass: HomeAssistant, main: str) -> str | None:
    """The explicit sub-stream entity for `main` from the settings entry's `live_sub_cameras` option, or None. Anything
    that is not a `camera.` entity id pair is ignored."""
    from .phones_commands import push_entry

    entry = push_entry(hass)
    mapping = entry.options.get(CONF_LIVE_SUB_CAMERAS) if entry is not None else None
    if not isinstance(mapping, dict):
        return None
    sub = mapping.get(main)
    if isinstance(sub, str) and sub.startswith("camera.") and sub != main:
        return sub
    return None


def _webrtc_camera(hass: HomeAssistant, entity_id: str):
    """HA's camera entity for `entity_id` when it can stream over WebRTC, else None. The test HA's own handler applies
    (camera/webrtc.py `require_webrtc_support`): the camera exists and is on, and WEB_RTC is one of its frontend stream
    types."""
    from homeassistant.components.camera.const import StreamType
    from homeassistant.components.camera.helper import get_camera_from_entity_id

    try:
        camera = get_camera_from_entity_id(hass, entity_id)
    except HomeAssistantError:
        return None
    if StreamType.WEB_RTC not in camera.camera_capabilities.frontend_stream_types:
        return None
    return camera


def _pick_stream(hass: HomeAssistant, main: str, size: str, small: str | None = None):
    """(camera, "main" | "sub") for a granted camera entity and the wanted variant (`size`: `small` = the small stream, any
    other value = the main one), or (None, "main") when HA cannot stream it. `small` is the element's own small stream
    (DEV-768); without one the install option `live_sub_cameras` still applies. A small stream that cannot stream falls
    back to main."""
    if size == "small":
        sub = small or _sub_entity(hass, main)
        if sub is not None:
            state = hass.states.get(sub)
            if state is not None and state.state not in ("unavailable", "unknown"):
                camera = _webrtc_camera(hass, sub)
                if camera is not None:
                    return camera, "sub"
    return _webrtc_camera(hass, main), "main"


def _event(session: LiveSession, payload: dict[str, Any]) -> None:
    try:
        session.connection.send_event(session.msg_id, payload)
    except Exception:  # noqa: BLE001 - the connection may already be gone
        _LOGGER.debug("portico live: could not send an event", exc_info=True)


def _relay(session: LiveSession):
    """HA's `send_message` callback for this session: HA's messages as Portico's events. A provider's error closes the
    session (the card falls back to stills); HA's own text is not forwarded (it can name internal hosts)."""
    from homeassistant.components.camera.webrtc import WebRTCAnswer, WebRTCCandidate, WebRTCError

    @callback
    def send_message(message) -> None:
        if session.closed:
            return
        if isinstance(message, WebRTCAnswer):
            _event(session, {"type": "answer", "sdp": message.answer})
        elif isinstance(message, WebRTCCandidate):
            _event(session, {"type": "candidate", "candidate": message.as_dict()["candidate"]})
        elif isinstance(message, WebRTCError):
            _LOGGER.debug("portico live: the camera reported %s", message.code)
            _event(session, {"type": "error", "code": "live_unavailable", "message": _UNAVAILABLE_TEXT})
            session.close("relay error")

    return send_message


async def async_start(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict, store, endpoint,
                      user_id: str, name: str) -> None:
    """`content/live`, after auth and `_content_target` (signaling.py). Every refusal before the result is an error
    frame; once the result is sent, a failure is an `error` event and the session closes."""
    try:
        entity_id, small = await store.async_live_cameras(user_id, name, msg["camera_id"], endpoint_id=endpoint.id)
    except RunRefused as err:
        connection.send_error(msg["id"], err.code, err.message)
        return

    reg = registry(hass)
    if reg.count(endpoint.id) >= MAX_LIVE_PER_ENDPOINT or reg.count() >= MAX_LIVE_PER_INSTALL:
        _LOGGER.warning("portico live: refused a session for %s (endpoint %s): %d of %d for it, %d of %d in all; "
                        "this endpoint holds: %s", name, endpoint.id, reg.count(endpoint.id), MAX_LIVE_PER_ENDPOINT,
                        reg.count(), MAX_LIVE_PER_INSTALL, reg.held(endpoint.id))
        connection.send_error(msg["id"], "too_many_live", "too many live streams are open")
        return

    camera, stream = _pick_stream(hass, entity_id, msg["size"], small)
    if camera is None:
        connection.send_error(msg["id"], "live_unavailable", _UNAVAILABLE_TEXT)
        return

    session = LiveSession(id=new_session_id(), endpoint_id=endpoint.id, connection=connection, msg_id=msg["id"],
                          stream=stream, camera=camera, registry=reg, camera_id=msg["camera_id"])
    from homeassistant.util.ulid import ulid

    session.ha_session_id = ulid()
    reg.sessions[session.id] = session                                  # counted before the first await below
    _LOGGER.info("portico live: opened session %s of %s (camera %s, %s stream); now %d for it, %d in all", session.id,
                 endpoint.id, session.camera_id, stream, reg.count(endpoint.id), reg.count())
    reg.changed()
    connection.subscriptions[msg["id"]] = lambda: session.close("socket closed or unsubscribed", by_hass=True)
    connection.send_result(msg["id"], {"session_id": session.id, "stream": stream})
    # HA's client library resolves a subscription with the unsubscribe function only and drops the result frame, so the
    # card learns its session id from the FIRST event, as with HA's own `camera/webrtc/offer`
    _event(session, {"type": "session", "session_id": session.id, "stream": stream})

    try:
        await camera.async_handle_async_webrtc_offer(msg["sdp"], session.ha_session_id, _relay(session))
    except asyncio.CancelledError:
        session.close("cancelled")
        raise
    except Exception as err:  # noqa: BLE001 - HA raises HomeAssistantError; anything else is the provider's bug
        _LOGGER.debug("portico live: the offer failed: %s", type(err).__name__)
        if not session.closed:
            _event(session, {"type": "error", "code": "live_unavailable", "message": _UNAVAILABLE_TEXT})
        session.close("offer failed (%s)" % type(err).__name__)
        return
    if session.closed:
        session._release()          # closed while the offer was in flight: the provider may have registered it since


def _owned(hass: HomeAssistant, connection: websocket_api.ActiveConnection, session_id: str) -> LiveSession | None:
    """The session of this id when THIS connection opened it; another connection's id is simply unknown."""
    session = registry(hass).sessions.get(session_id)
    if session is None or session.closed or session.connection is not connection:
        return None
    return session


def parse_candidate(raw: dict[str, Any]):
    """A browser's `RTCIceCandidateInit` as HA's, or None when it cannot be one."""
    from webrtc_models import RTCIceCandidateInit

    data = {k: v for k, v in raw.items() if v is not None}
    if "usernameFragment" in data:
        data["userFragment"] = data.pop("usernameFragment")
    try:
        return RTCIceCandidateInit.from_dict(data)
    except Exception:  # noqa: BLE001 - mashumaro's MissingField / ValueError: refuse, say nothing of why
        return None


async def async_ice(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    session = _owned(hass, connection, msg["session_id"])
    if session is None:
        connection.send_error(msg["id"], "unknown_session", "no such live session")
        return
    candidate = parse_candidate(msg["candidate"])
    if candidate is None:
        connection.send_error(msg["id"], "invalid_format", "not an ICE candidate")
        return
    try:
        await session.camera.async_on_webrtc_candidate(session.ha_session_id, candidate)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("portico live: a candidate was refused: %s", type(err).__name__)
        connection.send_error(msg["id"], "live_unavailable", _UNAVAILABLE_TEXT)
        return
    connection.send_result(msg["id"], {})


@callback
def async_close(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Idempotent: `closed` says whether a session was open (an error event may have closed it already)."""
    session = _owned(hass, connection, msg["session_id"])
    if session is not None:
        session.close("live_close")
    connection.send_result(msg["id"], {"closed": session is not None})
