"""DEV-807 follow-up (D-20261121-c98a): `portico.call_everyone`, the kitchen panel's "Call phones" button.

The dashboard button runs a script that calls this service; the service asks the PANEL'S APP to place a house call to Everyone. The
server never places the call itself: the call is the app's (its microphone, its call screen, its ringback tone), placed through the
normal `portico/content/run` path as the panel's phone identity, exactly as if someone pressed the Everyone row on the app's own
screen. What this module adds is one request and one answer on the panel app's existing authenticated socket:

  server -> app   an event on the app's `portico/calls/subscribe` stream: `{type: "panel_call", request_id, target: "everyone"}`
  app -> server   `portico/panel/call_result {request_id, ok, code?, text?, call_id?}` (phone class, a panel's login only)

The service waits for the answer (at most ACK_TIMEOUT_S) and returns it, so a script sees a clear error and never a silent failure:
`ok: true, state: "ringing"` once the app is in front and the phones are ringing, or an error saying why not (panel not connected,
app too old, Panel mode or house calls off, a call already live, the app could not come to the front, the server refused the call).

Who may call it: any authenticated HA context, deliberately NOT admin only. The dashboard's user is neither an administrator nor an
enrolled phone (auth.py, the owner's 2026-10-21 decision on call commands), and a script keeps the pressing user's context, so an
admin-only service would refuse the one button it exists for. What bounds it instead: the target is fixed (Everyone, from the panel's
own phone identity, so the phones that ring are the ones set to receive house calls), the only free parameter names WHICH panel, there
is at most one request in flight per panel and one per COOLDOWN_S, a panel that is on a call or ringing is refused here and again by the
app, and the microphone opens only through the app's normal call path: after a person answers, with the "Microphone is open" banner.
Nothing here carries audio, a credential or free text to the app.

In memory only: no request is stored, nothing is logged beyond one INFO line per request (who, which panel, the outcome).
"""
from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from . import auth
from .endpoints import KIND_PHONE, get_endpoint_registry, is_panel_user
from .house import get_house
from .panel_wall import HEARTBEAT_STALE_S, get_panel_wall
from .phone_entry import phone_entries, CONF_PANEL
from .provider import DOMAIN

_LOGGER = logging.getLogger(__name__)

SERVICE_CALL_EVERYONE = "call_everyone"
DATA_PANEL_CALL = "panel_call"
EVENT_TYPE = "panel_call"
TARGET_EVERYONE = "everyone"

ACK_TIMEOUT_S = 15.0     # the app's own deadline is shorter (it places a call: its step timer is 10 s), so a late answer is rare
COOLDOWN_S = 10.0        # one request per panel per this long, whatever its outcome
REQUEST_ID_RE = re.compile(r"^[0-9a-f]{16}$")
CODE_RE = re.compile(r"^[a-z0-9_]{1,40}$")
TEXT_MAX = 120

# The app's refusal codes the server knows by name (anything else is shown as the app's own text); documented in the design addendum.
APP_CODES = ("not_panel_mode", "house_calls_off", "busy", "paused", "no_everyone", "no_front", "timeout", "refused", "stale")

CALL_EVERYONE_SCHEMA = vol.Schema({vol.Optional("panel"): vol.All(str, vol.Length(min=1, max=64))})


@dataclass(eq=False)
class _Stream:
    connection: Any
    msg_id: int


@dataclass(eq=False)
class _Pending:
    request_id: str
    user_id: str
    future: "asyncio.Future[dict[str, Any]]"


@dataclass(eq=False)
class _PanelState:
    streams: list[_Stream] = field(default_factory=list)
    pending: _Pending | None = None
    last_request: float | None = None         # monotonic


class PanelCalls:
    """The requests this server has made of panel apps, by HA user id of the panel's phone login. In memory only."""

    def __init__(self, hass: HomeAssistant, *, clock=time.monotonic, ack_timeout: float = ACK_TIMEOUT_S,
                 cooldown: float = COOLDOWN_S) -> None:
        self.hass = hass
        self._clock = clock
        self._ack_timeout = ack_timeout
        self._cooldown = cooldown
        self._panels: dict[str, _PanelState] = {}

    def _state(self, user_id: str) -> _PanelState:
        return self._panels.setdefault(user_id, _PanelState())

    # ---- the app's stream ------------------------------------------------------------------------------------------

    def attach(self, connection: Any, msg_id: int) -> "callable":
        """A `portico/calls/subscribe` stream opened: the one a request is sent on. Returns the detach."""
        user = getattr(connection, "user", None)
        user_id = user.id if user is not None else None
        if not user_id:
            return lambda: None
        stream = _Stream(connection, msg_id)
        self._state(user_id).streams.append(stream)

        def detach() -> None:
            state = self._panels.get(user_id)
            if state is not None and stream in state.streams:
                state.streams.remove(stream)

        return detach

    # ---- the request -----------------------------------------------------------------------------------------------

    def _panel_for(self, name: str | None) -> tuple[str, str]:
        """(HA user id, display name) of the panel the service means: `name` (the phone's name or endpoint id, any case), or the only one."""
        registry = get_endpoint_registry(self.hass)
        panels = []
        for entry in phone_entries(self.hass):
            if entry.options.get(CONF_PANEL) is not True:
                continue
            user_id = entry.data.get("ha_user_id")
            phone = next((e for e in registry.endpoints(kind=KIND_PHONE) if e.ha_user_id == user_id), None)
            if user_id and phone is not None:
                panels.append((user_id, phone.name, phone.id))
        if not panels:
            raise ServiceValidationError("no_panel: no phone is set up as a panel")
        if name is None:
            if len(panels) > 1:
                raise ServiceValidationError("ambiguous_panel: more than one panel exists; name one with `panel`")
            return panels[0][0], panels[0][1]
        wanted = name.strip().casefold()
        hit = [p for p in panels if wanted in (p[1].casefold(), p[2].casefold())]
        if len(hit) != 1:
            raise ServiceValidationError("no_panel: no panel with that name")
        return hit[0][0], hit[0][1]

    def _preconditions(self, user_id: str) -> None:
        """Every refusal the server can decide alone, before the app is asked. Each is a clear error, never a silent no-op."""
        house = get_house(self.hass)
        if house is None:           # only before domain setup finished: never a user switch (D-20261122-aafb)
            raise HomeAssistantError("not_connected: house calls are not started yet on this server")
        wall = get_panel_wall(self.hass)
        beat, age, sockets = wall.status(user_id)
        if sockets <= 0:
            raise HomeAssistantError("not_connected: the panel's app is not connected to Home Assistant")
        if beat is None or age is None or age > HEARTBEAT_STALE_S:
            raise HomeAssistantError("not_connected: the panel's app has not reported for over 3 minutes")
        if beat.get("panel_call") is not True:          # an app that does not know this request never says so
            raise ServiceValidationError("app_too_old: the panel's app does not support this call yet; update it")
        if beat.get("panel_mode") is not True:
            raise ServiceValidationError("not_panel_mode: Panel mode is off in the app")
        if beat.get("overlay_ok") is not True:
            raise ServiceValidationError("no_front: the app lacks Display over other apps, so it cannot come to the front")
        if beat.get("microphone_ok") is not True:
            raise ServiceValidationError("no_microphone: the app has no microphone permission")
        if beat.get("front_reason") in ("ring", "call"):
            raise ServiceValidationError("busy: the panel is on a call or a ring")
        phone_id = wall.server_phone_endpoint(user_id)
        phone = house._endpoint(phone_id) if phone_id else None  # noqa: SLF001 - the registry's own resolver
        if phone is None or house.busy_state(phone) != "idle":
            raise ServiceValidationError("busy: the panel is on a call or a ring")
        group = house.group_entries(phone)
        if not group:
            raise ServiceValidationError("no_everyone: the panel has no Everyone key (its pages are not set up)")
        if group[0].get("available") is not True:
            reason = group[0].get("reason")
            raise ServiceValidationError(f"no_everyone: Everyone is not available ({reason}); is any phone set to receive house calls?")

    async def async_call_everyone(self, name: str | None, *, caller: str | None) -> dict[str, Any]:
        user_id, panel_name = self._panel_for(name)
        state = self._state(user_id)
        now = self._clock()
        if state.pending is not None:
            raise ServiceValidationError("in_progress: a call request is already waiting for this panel")
        if state.last_request is not None and now - state.last_request < self._cooldown:
            raise ServiceValidationError("rate_limited: this panel was asked a moment ago; wait a few seconds")
        self._preconditions(user_id)
        if not state.streams:
            raise HomeAssistantError("not_connected: the panel's app is not connected to Home Assistant")
        request_id = secrets.token_hex(8)
        future: "asyncio.Future[dict[str, Any]]" = self.hass.loop.create_future()
        state.pending = _Pending(request_id, user_id, future)
        state.last_request = now
        event = {"type": EVENT_TYPE, "request_id": request_id, "target": TARGET_EVERYONE}
        sent = 0
        for stream in list(state.streams):
            try:
                stream.connection.send_event(stream.msg_id, event)
                sent += 1
            except Exception:  # noqa: BLE001 - a closing socket must not break the request
                _LOGGER.debug("portico panel call: an event could not be sent", exc_info=True)
        outcome = "?"
        try:
            if sent == 0:
                outcome = "not_connected"
                raise HomeAssistantError("not_connected: the panel's app is not connected to Home Assistant")
            try:
                answer = await asyncio.wait_for(future, self._ack_timeout)
            except asyncio.TimeoutError:
                outcome = "no_answer"
                raise HomeAssistantError(
                    f"no_answer: the panel's app did not answer within {int(self._ack_timeout)} s; it may be asleep or hung") from None
            if answer.get("ok") is True:
                outcome = "ringing"
                return {"ok": True, "state": "ringing", "panel": panel_name}
            outcome = f"refused:{answer.get('code') or '?'}"
            raise ServiceValidationError(f"{answer.get('code') or 'refused'}: {answer.get('text') or 'the panel refused the call'}")
        finally:
            state.pending = None
            _LOGGER.info("portico panel call: panel=%s by=%s sent=%d outcome=%s", panel_name, caller or "context-less", sent, outcome)

    # ---- the answer ------------------------------------------------------------------------------------------------

    def result(self, user_id: str, request_id: str, ok: bool, code: str | None, text: str | None) -> bool:
        """The app's answer. False when it is not the answer to a request now waiting for THIS login (late, repeated, someone else's)."""
        state = self._panels.get(user_id)
        pending = state.pending if state is not None else None
        if pending is None or pending.request_id != request_id or pending.user_id != user_id or pending.future.done():
            return False
        pending.future.set_result({"ok": ok, "code": code, "text": text})
        return True


def get_panel_calls(hass: HomeAssistant) -> PanelCalls:
    data = hass.data.setdefault(DOMAIN, {})
    calls = data.get(DATA_PANEL_CALL)
    if calls is None:
        calls = data[DATA_PANEL_CALL] = PanelCalls(hass)
    return calls


def _clean(value: str | None) -> str | None:
    """The app's free text, shown to a script: printable, one line, bounded."""
    if not value:
        return None
    return "".join(ch for ch in value if ch.isprintable())[:TEXT_MAX] or None


@websocket_api.websocket_command({
    vol.Required("type"): "portico/panel/call_result",
    vol.Required("request_id"): vol.All(str, vol.Match(REQUEST_ID_RE)),
    vol.Required("ok"): bool,
    vol.Optional("code"): vol.Any(None, vol.All(str, vol.Match(CODE_RE))),
    vol.Optional("text"): vol.Any(None, vol.All(str, vol.Length(max=TEXT_MAX))),
})
@callback
def handle_panel_call_result(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """A panel app's answer to a `panel_call` request. Phone class; only a panel's login, and only for the request now waiting for it
    (`unknown_request` otherwise). -> `{}`."""
    if not auth.require(connection, msg, "portico/panel/call_result"):
        return
    user_id = connection.user.id if connection.user else None
    if not user_id or not is_panel_user(hass, user_id):
        connection.send_error(msg["id"], "not_a_panel", "this phone is not a panel")
        return
    if not get_panel_calls(hass).result(user_id, msg["request_id"], msg["ok"], msg.get("code"), _clean(msg.get("text"))):
        connection.send_error(msg["id"], "unknown_request", "no such request is waiting")
        return
    connection.send_result(msg["id"], {})


@callback
def async_setup_panel_call(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, handle_panel_call_result)

    async def _handle(call: ServiceCall) -> ServiceResponse:
        user = None
        if call.context.user_id:
            user = await hass.auth.async_get_user(call.context.user_id)
        return await get_panel_calls(hass).async_call_everyone(
            call.data.get("panel"), caller=user.name if user is not None else None)

    hass.services.async_register(DOMAIN, SERVICE_CALL_EVERYONE, _handle, schema=CALL_EVERYONE_SCHEMA,
                                 supports_response=SupportsResponse.OPTIONAL)
