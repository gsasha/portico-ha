"""The app commands of the house calls (design 4, 15.6 row 3a, 15.4 rule 3; M7 PRs 3a and 3b-S). Placing a call is not one of
them: it goes through `portico/content/run` (CB-1), so a phone never sends an endpoint id.

  portico/house/answer {call_id, client_kind}  claim a ringing call (first answer wins); returns `{call_id, media}`
  portico/house/decline {call_id}              a callee declines: local on a fan-out, terminal on a 1:1
  portico/house/hangup {call_id}               the caller cancels while ringing; either side ends it while talking
  portico/house/presence {dnd}                 the client's own Do Not Disturb state (HC-4); the server stamps the rest
  portico/house/media_open {call_id, codec | sdp}   (3b-S) an end of an ANSWERED call declares its audio leg: no `sdp` is a frame
                                               channel over this socket (`codec` pcmu|l16, default pcmu), an `sdp` is a WebRTC
                                               offer. Result `{kind: "frames", codec, frame_bytes, frame_ms}` or `{kind:
                                               "webrtc"}`; then events on this command id: `{t: "f", d}` downlink frames,
                                               `{t: "sdp", sdp}` the WebRTC answer, a last `{t: "end", reason}`. The first end
                                               to ask waits for the second (the hub opens one bridge with both legs). Uplink
                                               is the shared `portico/media_frame {call_id, d}` and the goodbye the shared
                                               `portico/media_close {call_id}` (away_media.py hands an `hso-` id here).
                                               `not_supported` unless a provider's hub runs the bridge (wheel 0.6.0 started with
                                               `-house-calls`), `unknown_call` for a call that is not answered or not this
                                               user's, `already_open` for a second ask of one end.

Calls progress as `house_call` events on `portico/calls/subscribe` (house.py). While house calls are off, which is the
default and the only state this ships in, all five answer `not_available` and touch nothing. A caller that is not in the
named call gets `unknown_call` whatever the reason, so a call id tells a stranger nothing. `house/keepalive` is not
built: the socket is the liveness and the 45 s limit is server-side (15.6).
"""
from __future__ import annotations

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from . import auth
from .house import CODE_NOT_AVAILABLE, CODE_NOT_SUBSCRIBED, HouseCallRegistry, get_house
from .house_bridge import CODECS

_CALL_ID = vol.All(str, vol.Length(min=1, max=128))


def _house(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict,
           command: str) -> HouseCallRegistry | None:
    """Auth first, then the registry; None after sending the error (`not_available` while house calls are off)."""
    if not auth.require(connection, msg, command):
        return None
    house = get_house(hass)
    if house is None:
        connection.send_error(msg["id"], CODE_NOT_AVAILABLE, "house calls are not switched on")
        return None
    return house


@websocket_api.websocket_command({
    vol.Required("type"): "portico/house/answer",
    vol.Required("call_id"): _CALL_ID,
    vol.Required("client_kind"): vol.In(["app", "browser"]),
})
@callback
def handle_house_answer(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    house = _house(hass, connection, msg, "portico/house/answer")
    if house is None:
        return
    result, code = house.answer(msg["call_id"], connection.user.id if connection.user else None)
    if code is not None:
        connection.send_error(msg["id"], code, _TEXT.get(code, code))
        return
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command({vol.Required("type"): "portico/house/decline", vol.Required("call_id"): _CALL_ID})
@callback
def handle_house_decline(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    house = _house(hass, connection, msg, "portico/house/decline")
    if house is None:
        return
    ok, code = house.decline(msg["call_id"], connection.user.id if connection.user else None)
    if not ok:
        connection.send_error(msg["id"], code, _TEXT.get(code, code))
        return
    connection.send_result(msg["id"], {})


@websocket_api.websocket_command({vol.Required("type"): "portico/house/hangup", vol.Required("call_id"): _CALL_ID})
@callback
def handle_house_hangup(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    house = _house(hass, connection, msg, "portico/house/hangup")
    if house is None:
        return
    ok, code = house.hangup(msg["call_id"], connection.user.id if connection.user else None)
    if not ok:
        connection.send_error(msg["id"], code, _TEXT.get(code, code))
        return
    connection.send_result(msg["id"], {})


@websocket_api.websocket_command({vol.Required("type"): "portico/house/presence", vol.Required("dnd"): bool})
@callback
def handle_house_presence(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """The client's own DND state. It must be on a connection that has subscribed to calls (that is what makes it present);
    the report is dropped with the connection."""
    house = _house(hass, connection, msg, "portico/house/presence")
    if house is None:
        return
    if not house.set_dnd(connection, msg["dnd"]):
        connection.send_error(msg["id"], CODE_NOT_SUBSCRIBED, "subscribe to calls first")
        return
    connection.send_result(msg["id"], {})


@websocket_api.websocket_command({
    vol.Required("type"): "portico/house/media_open",
    vol.Required("call_id"): _CALL_ID,
    vol.Optional("codec", default="pcmu"): vol.In(CODECS),
    vol.Optional("sdp"): vol.All(str, vol.Length(min=1, max=65536)),
})
@websocket_api.async_response
async def handle_house_media_open(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """An end of an answered house call declares its leg (house_bridge.py). `not_available` while house calls are off,
    `not_supported` while no provider runs the bridge: both before anything is looked up."""
    house = _house(hass, connection, msg, "portico/house/media_open")
    if house is None:
        return
    result, code = await house.bridge.attach(connection, msg["id"], connection.user.id if connection.user else None,
                                             msg["call_id"], codec=msg["codec"], sdp=msg.get("sdp"))
    if code is not None:
        connection.send_error(msg["id"], code, _TEXT.get(code, code))
        return
    connection.send_result(msg["id"], result)


@callback
def route_house_frame(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """`portico/media_frame` for an `hso-` call id (away_media.py hands it here): from the owner of a live frame leg only,
    otherwise dropped with no answer. No authorization per frame (25 a second): the owner passed it at `media_open`."""
    house = get_house(hass)
    if house is not None:
        house.bridge.uplink(connection, msg["call_id"], msg["d"])


@callback
def route_house_close(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """`portico/media_close` for an `hso-` call id: the end's goodbye hangs the call up. Always `{ok: true}`, as for a door."""
    house = get_house(hass)
    if house is not None:
        house.bridge.phone_close(connection, msg["call_id"], connection.user.id if connection.user else None)
    connection.send_result(msg["id"], {"ok": True})


_TEXT = {
    "not_supported": "this Home Assistant has no house-call audio bridge",
    "already_open": "this end already declared its audio",
    "unknown_call": "no such call",
    "already_answered": "this call was already answered",
    "busy": "this endpoint is on another call",
}


@callback
def async_setup_house_commands(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, handle_house_answer)
    websocket_api.async_register_command(hass, handle_house_decline)
    websocket_api.async_register_command(hass, handle_house_hangup)
    websocket_api.async_register_command(hass, handle_house_presence)
    websocket_api.async_register_command(hass, handle_house_media_open)
