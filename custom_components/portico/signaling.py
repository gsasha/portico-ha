"""Portico's signaling API — HA <-> clients (browser card + native app).

Design doc §9.5. Registered via HA Core's own websocket_api (no separate
auth system, no separate port). Who may send which command is auth.py's one
table; every handler calls auth.require first. Commands:

  portico/version      - the contract version (3), the provider API version, the providers
  portico/doors        - the doors' facts (id, derived push ref, endpoint id, name, driver, Capabilities, camera,
                          availability, away-media flag); no secrets (WS-03)
  portico/calls/subscribe  - call-state snapshots and events, for every door
  portico/content/subscribe - `content_changed` for the caller's own endpoint (an admin: a named one, or all)
  portico/dial / hangup / open_relay
  portico/answer       - computes `mode: direct|relay` from Capabilities +
                          client_kind (§9.6) and relays to transport.py
  portico/keepalive    - direct-mode lock liveness (§9.5)
  portico/webrtc_offer / webrtc_answer / webrtc_ice  - browser client only
  portico/content/get | run | snapshot  - the caller's endpoint document (pages, callers,
                          roster), running one action of it by id, and one still from
                          one of its cameras as base64 (content.py; the document IS the grant)
  portico/content/live | live_ice | live_close  - live video of a camera tile: WebRTC, video only, through Home Assistant's own
                          camera support (live.py); the grant is snapshot's
  portico/content/document | preview | save, portico/kiosk/hello  - the editor's commands (editor_commands.py)
  portico/phones/list | enroll | cancel | revoke | set  - the Phones tab (UI split P2, P3; admin only; phones_commands.py)
  portico/door_status, door_settings/get | set  - the Settings tab's doors (UI split P4, P5; admin only; door_commands.py)
  portico/ui_settings/get | set  - the Settings tab's push ring notification preferences (UI split P6; admin only; ui_settings.py)
  portico/push/register | unregister | status  - the push ring notification registry
                          (FCM sender design §3.3): an enrolled phone's own
                          rows only, never a registration value back

Contract 3 (M6 E3) has no aliases: the contract-2 names (`portico/devices`, `portico/subscribe`, `content/list`,
`device_id`, `entry_id`, `profile`) are gone, not served beside the new ones. Who may send what is auth.py's table.

Known gap: portico/answer's "already answered" arbitration (§4.5, §6) is
made by the door's provider (provider_impl.TransportProvider.answer; the
one arbitration, STATE-02) against call_state.py's own tracked state —
not against the transport's call manager (transport-rs/src/call/), which
isn't wired into the IPC surface yet (the transport's
"answer" handler has no concept of arbitration, only "ask the driver to
answer"). This is a real, single-instance-of-HA-only arbitration point,
same as the design's own single-HA-instance assumption elsewhere — not
a shortcut invented for this pass, but worth closing the gap once
call.Manager's real semantics (already_answered vs device_busy, in
particular) are worth surfacing precisely rather than approximating.
This module only turns the provider's verdict into the wire.
"""
from __future__ import annotations

import base64
import logging
import re
import time
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from . import actions, auth, away_media, live
from .config_flow import DOMAIN
from .content import (
    SNAPSHOT_WIDTH_DEFAULT,
    SNAPSHOT_WIDTH_MAX,
    SNAPSHOT_WIDTH_MIN,
    RunRefused,
    get_content_store,
)
from .door_commands import async_setup_door_commands
from .editor_commands import async_setup_editor_commands
from .elements_commands import async_setup_elements_commands
from .endpoints import KIND_DASHBOARD, KIND_PHONE, Endpoint, get_endpoint_registry
from .connlog import async_setup_connlog_commands
from .panel_call import async_setup_panel_call, get_panel_calls
from .panel_wall import async_setup_panel_wall_commands, get_panel_wall
from . import house_attach
from .house import get_house
from .house_commands import async_setup_house_commands
from .phone_entry import may_open
from .phones_commands import async_setup_phones_commands
from .ui_settings import async_setup_ui_settings_commands
from .provider import API_VERSION, DATA_PROVIDERS, AnswerResult, CallSnapshot, CallTransition, ProviderError, ProviderRegistry, error_code
from .push import get_manager
from .push.registration import FIELDS as REGISTER_FIELDS
from .push.registration import (
    RegistrationRefused,
    _phones,
    async_register_phone,
    enrolled_phone,
    enrolled_phone_name,
)
from .push.registry import PushRegistry
from .talk_relay import get_talk_relay

_LOGGER = logging.getLogger(__name__)

_SNAPSHOT_STATES = frozenset({"idle", "ringing", "answered", "active"})   # what a subscribe snapshot may carry


def _registry(hass: HomeAssistant) -> ProviderRegistry | None:
    """The provider registry (provider.py); None before the integration's domain setup has run."""
    return hass.data.get(DOMAIN, {}).get(DATA_PROVIDERS)


def _snapshot_event(door, provider) -> dict | None:
    """The subscribe snapshot of one door, from the provider's `call_state()`; None when there is none to send.
    The dict is validated like a transition (CallSnapshot.from_dict) and its state must be one a snapshot may
    carry (provider_v1.md section 4: never `ended`, the registry alone says `unavailable`); a provider that
    sends anything else loses that door's snapshot, logged, rather than failing the whole subscribe."""
    try:
        raw = provider.call_state(door.id) if provider is not None else None
        if raw is None:
            return None
        snap = CallSnapshot.from_dict(raw)
    except ProviderError as err:
        _LOGGER.warning("%s: no call snapshot for %s: %s", door.provider, door.id, err)
        return None
    if snap.state not in _SNAPSHOT_STATES:
        _LOGGER.warning("%s: no call snapshot for %s: state %r is not one of %s",
                        door.provider, door.id, snap.state, "|".join(sorted(_SNAPSHOT_STATES)))
        return None
    return {**_call_state_event(door.id, snap.call_id, snap.state), "snapshot": True}


def _phone_ids(hass: HomeAssistant, user_id: str | None, door_id: str | None = None) -> list[str]:
    """The provider-protocol `phone_id` of every enrolled phone this HA user is: its `enroll_id`, or for a row
    enrolled before enroll ids existed its HA user id (the same rule as provider_impl._phone_id). With `door_id`,
    only the phones enrolled for that door (R4-04: a phone entry's `doors` option; a phone with no list may open
    every door), so a phone is never handed the secrets of a door it is not enrolled for."""
    if user_id is None:
        return []
    ids: list[str] = []
    for _entry, phone in _phones(hass):
        if phone.get("ha_user_id") == user_id and (door_id is None or may_open(phone, door_id)):
            phone_id = phone.get("enroll_id") or phone.get("ha_user_id")
            if phone_id not in ids:
                ids.append(phone_id)
    return ids


def _send_provider_error(connection: websocket_api.ActiveConnection, msg_id: int, err: BaseException) -> bool:
    """Send a provider's failure (an exception carrying `code` and `message`, provider_v1.md section 2) as the
    command's error. False when the exception carries no code: the caller re-raises, as before."""
    code = error_code(err)
    if code is None:
        return False
    connection.send_error(msg_id, code, str(getattr(err, "message", err)))
    return True


def _who(connection) -> str:
    """The phone, for a diagnostic line: the login's display name (never a token or an id of a secret)."""
    user = getattr(connection, "user", None)
    return getattr(user, "name", None) or getattr(user, "id", None) or "-"


def _conn(connection) -> str:
    """A short per-connection tag, so one websocket's lines can be told from another's."""
    return f"{id(connection) & 0xFFFFFF:06x}"


def _snapshot_brief(provider, door_id: str) -> str:
    """`call/state[/outbound]` of a door as the provider sees it now, for a diagnostic line; never raises."""
    try:
        snap = provider.call_state(door_id)
    except Exception as err:   # noqa: BLE001
        return f"error:{type(err).__name__}"
    if not isinstance(snap, dict):
        return "none"
    return f"{snap.get('call_id')}/{snap.get('state')}/out={snap.get('outbound_state')}"


def _talk_elements(node: Any, out: list[str] | None = None) -> list[str]:
    """`id:available/reason` of every Talk element in a resolved document (diagnostics only)."""
    out = [] if out is None else out
    if isinstance(node, dict):
        if node.get("kind") == "talk" or node.get("type") == "talk":
            out.append(f"{node.get('id', '?')}:{node.get('available')}/{node.get('reason')}")
        for value in node.values():
            _talk_elements(value, out)
    elif isinstance(node, list):
        for value in node:
            _talk_elements(value, out)
    return out


# ids of connections that already got a document (ActiveConnection has __slots__: no weakref); bounded, diagnostics only
_FIRST_DOC_SEEN: set[int] = set()


def _door_for(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict, door_id: str):
    """(registry, provider, Door) for a door id the client named, or None after sending the error.
    No provider at all answers `not_available` (STRUCT-07); an unknown door is `unknown_door` (contract 3; `unknown_device` in v1)."""
    registry = _registry(hass)
    if registry is None or not registry.providers():
        _LOGGER.info("portico: %s refused for door=%s conn=%s: not_available (no provider)", msg.get("type"), door_id,
                     _conn(connection))
        connection.send_error(msg["id"], "not_available", "no Portico door provider is available")
        return None
    try:
        found = registry.door(door_id)
    except ProviderError as err:
        _LOGGER.info("portico: %s refused for door=%s conn=%s: %s", msg.get("type"), door_id, _conn(connection), err.code)
        connection.send_error(msg["id"], err.code, err.message)
        return None
    if found is None:
        _LOGGER.info("portico: %s refused for door=%s conn=%s: unknown_door", msg.get("type"), door_id, _conn(connection))
        connection.send_error(msg["id"], "unknown_door", door_id)
        return None
    provider, door = found
    return registry, provider, door


def _provider_for_call(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict,
                       *, empty_code: str = "unknown_call", empty_text: str = ""):
    """The provider that owns `msg["call_id"]` (PROC-02: learned from transitions and dial results, never
    "the first live entry"), or None after sending the error: `unknown_call` when nothing owns it, and
    `empty_code` when there is no door at all (v1 answered `no_transport` for the webrtc commands)."""
    registry = _registry(hass)
    provider = registry.for_call(msg["call_id"]) if registry is not None else None
    if provider is not None:
        return provider
    _LOGGER.info("portico: %s refused for call=%s conn=%s: %s", msg.get("type"), msg.get("call_id"), _conn(connection),
                 empty_code if empty_code != "unknown_call" and (registry is None or not registry.doors()) else "unknown_call")
    if empty_code != "unknown_call" and (registry is None or not registry.doors()):
        connection.send_error(msg["id"], empty_code, empty_text)
    else:
        connection.send_error(msg["id"], "unknown_call", msg["call_id"])
    return None


# The websocket contract this integration serves (contract/ws_v3.json, `_meta.version`). The app sends
# `portico/version` first and refuses to continue on any other value (WS-02). Contract 3 has no aliases (M6 O2).
CONTRACT_VERSION = 3


@websocket_api.websocket_command({vol.Required("type"): "portico/version"})
@callback
def handle_version(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """The contract version, the provider API version this part speaks, and the providers it accepted."""
    if not auth.require(connection, msg, "portico/version"):
        return
    registry = _registry(hass)
    providers = [{"domain": provider.domain, "api_version": provider.API_VERSION}
                 for provider in (registry.providers() if registry is not None else [])]
    connection.send_result(msg["id"], {"contract": CONTRACT_VERSION, "ui_version": API_VERSION,
                                       "providers": providers})


def _answerable(capabilities: dict | None) -> bool:
    """False for a ring-only door: no audio either way (NoAudio = 3), no relay and no direct path. Anything else,
    including capabilities that say nothing, is answerable (the answer itself is still arbitrated by the provider)."""
    c = capabilities or {}
    ring_only = (c.get("InboundAudio") == 3 and c.get("OutboundAudio") == 3
                 and not c.get("HasRelay") and not c.get("DirectCapable"))
    return not ring_only


@websocket_api.websocket_command({vol.Required("type"): "portico/doors"})
@callback
def handle_doors(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """The doors' facts, for the app's and the card's door lists (contract 3; `portico/devices` in v1). Driver facts
    only: the per-phone secrets half of v1's `devices` is gone (WS-03, PROV-03), and `provider.secrets_for` serves
    no command. `door_ref` is the derived push ref (`Door.door_ref`), which the app matches a push on."""
    if not auth.require(connection, msg, "portico/doors"):
        return
    registry = _registry(hass)
    doors = [
        {
            "door_id": door.id,
            "door_ref": door.door_ref,
            "endpoint_id": door.endpoint_id,
            # The door's display name: the entry title the installer sees (and can rename) in Settings > Devices &
            # Services. The app's ring screen and notification show it.
            "name": door.name,
            "driver": door.driver,
            "capabilities": door.capabilities,
            "camera_entity_id": door.camera_entity_id,
            "available": door.available,
            "away_media": door.away_media,
            # The server's word on whether a phone can answer this door (ring-only doors say false): the ring screen
            # offers no Answer for it. Additive; an app that does not know it keeps offering Answer.
            "answerable": _answerable(door.capabilities),
        }
        for door in (registry.doors() if registry is not None else [])
    ]
    connection.send_result(msg["id"], {"doors": doors})


def _call_state_event(door_id: str, call_id: str | None, state: str) -> dict:
    return {
        "type": "call_state_changed",
        "door_id": door_id,
        "call_id": call_id,
        "state": state,
    }


@websocket_api.websocket_command({vol.Required("type"): "portico/calls/subscribe"})
@callback
def handle_calls_subscribe(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Call-state events for every configured door, for the lifetime of the websocket subscription (contract 3;
    the call half of v1's `portico/subscribe`: content has `portico/content/subscribe`, and the two never share a
    message shape, WS-04).

    Messages, in order:

    1. the subscribe result;
    2. one snapshot per door, the tracker's state at subscribe time:
       ``{type: "call_state_changed", snapshot: true, door_id, call_id, state}`` with ``state`` one of ``idle``
       (``call_id: null``), ``ringing``, ``answered`` or ``active``;
    3. live events, one per applied transition (a dropped event sends nothing):
       ``{type: "call_state_changed", door_id, call_id, state, prev_state}`` with ``state``
       ``ringing|answered|active``, or ``ended`` with ``call_id`` the call that ended. There is no `idle` echo after
       ``ended``: a client takes the call id from ``ended``.

    No gap: this handler is a synchronous @callback and the tracker is only mutated on the event loop, so
    registering the listener, reading the snapshots and queuing the result and snapshots happen with no
    transition in between; everything the listener sends is queued after them. The door set and the transitions
    come from the provider registry (PH-3a).

    Doors leaving and arriving (PH-5, PROV-05): a door that leaves while the subscription is open (an entry
    unloaded or reloaded, or its whole provider unloading) gets one more snapshot,
    ``{type: "call_state_changed", snapshot: true, door_id, call_id: null, state: "unavailable"}``, and the other
    doors carry on; a door set up after the subscription began gets the snapshot of its state at that moment, like
    the doors present at subscribe time. A client that does not know ``unavailable`` ignores a state it has no
    meaning for. M7 reserves ``{type: "house_call", ...}`` on this stream, and P3-B sends ``{type: "talk_state",
    door_id, call_id, state, reason?}`` (talk_relay.py) to the phone that placed a Talk call and to no one else; a client
    ignores an event type it does not know.
    """
    if not auth.require(connection, msg, "portico/calls/subscribe"):
        return
    registry = _registry(hass)
    snapshots: list[dict] = []

    if registry is not None:
        for door in registry.doors():
            provider = registry.provider(door.provider)
            snapshot = _snapshot_event(door, provider)
            if snapshot is not None:
                snapshots.append(snapshot)

    @callback
    def _listener(transition: CallTransition) -> None:
        _LOGGER.debug("portico calls/subscribe: conn=%s event door=%s call=%s %s -> %s", _conn(connection),
                      transition.door_id, transition.call_id, transition.prev_state, transition.state)
        # Fires BEFORE the tracker mutates: the transition, not the tracker, says which call this is about.
        if transition.state == "unavailable":
            # The registry's own state, never a provider's (provider_v1.md section 4): the door left.
            connection.send_event(
                msg["id"], {**_call_state_event(transition.door_id, None, "unavailable"), "snapshot": True}
            )
            return
        if transition.state == "ringing":
            get_panel_wall(hass).note_ring(connection.user.id if connection.user else None, transition.call_id,
                                           transition.door_id)       # a panel's `ring_sent_not_shown` (DEV-807); a no-op for any other login
        connection.send_event(
            msg["id"],
            {**_call_state_event(transition.door_id, transition.call_id, transition.state),
             "prev_state": transition.prev_state},
        )

    @callback
    def _arrival_listener(door) -> None:
        # A door set up after this subscription began: its snapshot, as for a door present at subscribe time.
        provider = registry.provider(door.provider)
        snapshot = _snapshot_event(door, provider)
        if snapshot is not None:
            connection.send_event(msg["id"], snapshot)

    unsubscribe_calls = registry.subscribe(_listener) if registry is not None else None
    unsubscribe_arrivals = registry.subscribe_arrivals(_arrival_listener) if registry is not None else None
    # M7 3a: `get_house` is None only before domain setup finished (house calls are always on).
    house = get_house(hass)
    house_handle = house.attach(connection, msg["id"]) if house is not None else None
    # The stream is listed, and what it holds on the registry is read from the list when it closes.
    sub = house_attach.track(hass, connection, msg["id"], house_handle)
    panel_user = connection.user.id if connection.user else None
    get_panel_wall(hass).socket_opened(panel_user)        # DEV-807: "the socket is open" is what the panel's readiness reads
    detach_panel_call = get_panel_calls(hass).attach(connection, msg["id"])      # the stream `portico.call_everyone` asks a panel on (panel_call.py)
    # M7 P3-B: this stream may also carry `talk_state`, to the phone that placed a Talk call and to no other client.
    talk_relay = get_talk_relay(hass)
    talk_handle = talk_relay.attach(connection, msg["id"]) if talk_relay is not None else None

    _LOGGER.info("portico calls/subscribe: attach conn=%s phone=%s doors=%d house=%s", _conn(connection),
                 _who(connection), len(snapshots), house_handle is not None)
    for snap in snapshots:
        _LOGGER.debug("portico calls/subscribe: conn=%s snapshot door=%s call=%s state=%s", _conn(connection),
                      snap.get("door_id"), snap.get("call_id"), snap.get("state"))

    @callback
    def unsubscribe() -> None:
        _LOGGER.info("portico calls/subscribe: detach conn=%s phone=%s", _conn(connection), _who(connection))
        if unsubscribe_calls is not None:
            unsubscribe_calls()
        if unsubscribe_arrivals is not None:
            unsubscribe_arrivals()
        house_attach.untrack(hass, sub)
        get_panel_wall(hass).socket_closed(panel_user)
        detach_panel_call()
        if sub.house_handle is not None:
            sub.house_handle.detach()
        if talk_handle is not None:
            talk_handle.detach()

    connection.subscriptions[msg["id"]] = unsubscribe
    connection.send_result(msg["id"])
    for snapshot in snapshots:
        connection.send_event(msg["id"], snapshot)
    if house_handle is not None:
        for snapshot in house_handle.snapshots():       # the house calls this user is in, after the doors'
            connection.send_event(msg["id"], snapshot)


def _content_changed(store, endpoint_id: str) -> dict[str, Any]:
    """`{type, endpoint, rev}` plus `wake` for a screen of a schema 6 document (additive: nobody else gets the key)."""
    event: dict[str, Any] = {"type": "content_changed", "endpoint": endpoint_id,
                             "rev": store.resolve_document(endpoint_id)["rev"]}
    wake = store.wake_for_endpoint(endpoint_id)
    if wake is not None:
        event["wake"] = wake
    return event


@websocket_api.websocket_command({
    vol.Required("type"): "portico/content/subscribe",
    vol.Optional("endpoint"): vol.All(str, vol.Length(min=1, max=64)),
})
@websocket_api.async_response
async def handle_content_subscribe(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """A document changed (content design §3; contract 3; the content half of v1's `portico/subscribe`). The events
    are ``{type: "content_changed", endpoint, rev}`` and never carry a call (WS-04).

    A phone or a declared screen hears about its own endpoint (``endpoint`` naming another is `unauthorized`; a login
    that serves several screens names one), with that endpoint's `rev`. An administrator names one endpoint and gets
    its changes with its `rev`, or names none and gets every change, ``rev: null`` (a kiosk card on an admin's login
    refetches its own). The install-wide home name changing is one more `content_changed`, for the same endpoint. The
    store is loaded first: whether a login is a declared screen depends on it."""
    content_store = get_content_store(hass)
    await content_store.async_ensure_loaded()
    if not auth.require(connection, msg, "portico/content/subscribe"):
        return
    user = connection.user
    user_id = user.id if user else None
    scope: str | None = None     # None: every change (an administrator who names none and has no endpoint of its own)
    owns = bool(user_id) and any(
        e.ha_user_id == user_id for e in get_endpoint_registry(hass).endpoints()
        if e.kind in (KIND_PHONE, KIND_DASHBOARD))
    if msg.get("endpoint") or owns or user is None or not user.is_admin:
        target = _content_target(hass, connection, msg)
        if target is None:
            return
        scope = target[1].id

    @callback
    def _content_listener(endpoint_id: str) -> None:
        if scope is None:
            connection.send_event(msg["id"], {"type": "content_changed", "endpoint": endpoint_id, "rev": None})
        elif endpoint_id == scope:
            connection.send_event(msg["id"], _content_changed(content_store, scope))

    @callback
    def _home_listener() -> None:
        # The install-wide home name or the template changed: every subscriber refetches.
        if scope is None:
            connection.send_event(msg["id"], {"type": "content_changed", "endpoint": None, "rev": None})
        else:
            connection.send_event(msg["id"], _content_changed(content_store, scope))

    @callback
    def _live_listener() -> None:
        # Live state moved (Talk's `busy`) and the rev did not: `rev: null` is "refetch", never "already held".
        connection.send_event(msg["id"], {"type": "content_changed", "endpoint": scope, "rev": None})

    content_store.add_listener(_content_listener)
    content_store.add_live_listener(_live_listener)
    content_store.add_home_listener(_home_listener)

    _LOGGER.info("portico content/subscribe: attach conn=%s phone=%s scope=%s", _conn(connection), _who(connection),
                 scope or "all")

    @callback
    def unsubscribe() -> None:
        _LOGGER.info("portico content/subscribe: detach conn=%s phone=%s", _conn(connection), _who(connection))
        content_store.remove_listener(_content_listener)
        content_store.remove_home_listener(_home_listener)
        content_store.remove_live_listener(_live_listener)

    connection.subscriptions[msg["id"]] = unsubscribe
    connection.send_result(msg["id"])


@websocket_api.websocket_command(
    {
        vol.Required("type"): "portico/dial",
        vol.Required("door_id"): str,
        vol.Optional("host", default=""): str,
        vol.Optional("port", default=0): int,
    }
)
@websocket_api.async_response
async def handle_dial(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    if not auth.require(connection, msg, "portico/dial"):
        return
    found = _door_for(hass, connection, msg, msg["door_id"])
    if found is None:
        return
    registry, provider, door = found
    try:
        result = await provider.dial(door.id, msg["host"], msg["port"])
    except Exception as err:   # noqa: BLE001 - a provider's failure carries `code`/`message`; anything else re-raises
        if not _send_provider_error(connection, msg["id"], err):
            raise
        return
    registry.note_call(provider.domain, result, door.id)   # a dialled call has no ring: learn its id here (R2-08)
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "portico/hangup",
        vol.Required("call_id"): str,
        vol.Optional("reason", default=""): str,
    }
)
@websocket_api.async_response
async def handle_hangup(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    if not auth.require(connection, msg, "portico/hangup"):
        return
    # hangup is keyed by call_id on the wire (§9.4): the registry knows which provider owns the call (PROC-02).
    provider = _provider_for_call(hass, connection, msg)
    if provider is None:
        return
    user = getattr(connection, "user", None)
    _LOGGER.info("portico: hangup requested call_id=%s reason=%s phone=%s conn=%s", msg["call_id"], msg["reason"] or "-",
                 getattr(user, "name", None) or getattr(user, "id", None) or "-", _conn(connection))
    try:
        result = await provider.hangup(msg["call_id"], msg["reason"])
    except Exception as err:   # noqa: BLE001
        _LOGGER.info("portico: hangup call_id=%s failed: %s", msg["call_id"], error_code(err) or type(err).__name__)
        if not _send_provider_error(connection, msg["id"], err):
            raise
        return
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "portico/open_relay",
        vol.Required("door_id"): str,
    }
)
@websocket_api.async_response
async def handle_open_relay(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    if not auth.require(connection, msg, "portico/open_relay"):
        return
    found = _door_for(hass, connection, msg, msg["door_id"])
    if found is None:
        return
    _registry_, provider, door = found
    user = connection.user
    ctx = actions.begin_open(
        hass, actor=(enrolled_phone_name(hass, user.id) if user else None) or (user.name if user else None),
        actor_user=user.id if user else None, source=actions.source_for(hass, user.id if user else None),
        door_id=door.id, door_name=door.name)
    try:
        result = await provider.open_relay(door.id, context=ctx)
    except Exception as err:   # noqa: BLE001
        if not _send_provider_error(connection, msg["id"], err):
            raise
        return
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "portico/answer",
        vol.Required("door_id"): str,
        vol.Required("call_id"): str,
        vol.Required("client_kind"): vol.In(["app", "browser"]),
        vol.Optional("host", default=""): str,
        vol.Optional("port", default=0): int,
    }
)
@websocket_api.async_response
async def handle_answer(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    if not auth.require(connection, msg, "portico/answer"):
        return
    found = _door_for(hass, connection, msg, msg["door_id"])
    if found is None:
        return
    _registry_, provider, door = found
    user = connection.user
    arrived = time.monotonic()
    _LOGGER.info("portico answer: command door=%s call=%s kind=%s phone=%s conn=%s snapshot=%s", door.id, msg["call_id"],
                 msg["client_kind"], _who(connection), _conn(connection), _snapshot_brief(provider, door.id))

    def _outcome(what: str) -> None:
        _LOGGER.info("portico answer: result door=%s call=%s conn=%s %s after_ms=%d", door.id, msg["call_id"],
                     _conn(connection), what, int((time.monotonic() - arrived) * 1000))

    # The provider holds the one arbitration (STATE-02) and, on a granted relay answer, the ring-answered
    # helper write (STATE-03); this handler only carries the claimant in and the verdict out.
    claimant = f"{user.id if user else ''}:{id(connection)}"
    # M7 3a (15.13): a user holds at most two door calls (one live, one held); the ledger refuses a third BEFORE the
    # provider is asked. Off (None) while house calls are off, which is always today: this handler is then what it was.
    house = get_house(hass)
    reservation = house.reserve_door_answer(user.id if user else None, msg["call_id"]) if house is not None else None
    if isinstance(reservation, str):
        _outcome(f"refused={reservation} (house ledger)")
        connection.send_error(msg["id"], reservation, "you are already on two calls")
        return
    try:
        raw = await provider.answer(door.id, msg["call_id"], client_kind=msg["client_kind"], claimant=claimant,
                                    host=msg["host"], port=msg["port"])
        answer = AnswerResult.from_dict(raw)
    except Exception as err:   # noqa: BLE001
        if reservation is not None:
            house.settle_door_answer(reservation, False)
        _outcome(f"error={error_code(err) or type(err).__name__}")
        if not _send_provider_error(connection, msg["id"], err):
            raise
        return
    if reservation is not None:
        house.settle_door_answer(reservation, answer.verdict == "answered", door.id)
    _outcome(f"verdict={answer.verdict} mode={answer.mode}")
    if answer.verdict == "answered":
        if answer.mode == "relay":
            away_media.record_claim(hass, door.away_media, msg["call_id"], connection)
        connection.send_result(msg["id"], {"mode": answer.mode, **answer.detail})
    elif answer.verdict == "refused":
        connection.send_error(msg["id"], "refused", "this door does not let Portico answer")
    elif answer.verdict == "not_available":
        connection.send_error(msg["id"], "not_available", "this call is gone or this door cannot be answered now")
    else:
        # `taken_elsewhere`: the wire keeps `already_answered` (M6 review 7, decision 5: the rename is cut)
        connection.send_error(msg["id"], "already_answered", "this call was already answered")


@websocket_api.websocket_command(
    {
        vol.Required("type"): "portico/keepalive",
        vol.Required("call_id"): str,
    }
)
@websocket_api.async_response
async def handle_keepalive(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    if not auth.require(connection, msg, "portico/keepalive"):
        return
    provider = _provider_for_call(hass, connection, msg)
    if provider is None:
        return
    try:
        result = await provider.keepalive(msg["call_id"])
    except Exception as err:   # noqa: BLE001
        if not _send_provider_error(connection, msg["id"], err):
            raise
        return
    connection.send_result(msg["id"], result)


async def _webrtc(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict, method: str, *args) -> None:
    """webrtc_* is routed by the call id to the provider that owns the call (PROC-02, WS-05): the SDP and the
    candidates pass through untouched."""
    provider = _provider_for_call(hass, connection, msg, empty_code="no_transport",
                                  empty_text="no configured Portico device")
    if provider is None:
        return
    try:
        result = await getattr(provider, method)(msg["call_id"], *args)
    except Exception as err:   # noqa: BLE001
        if not _send_provider_error(connection, msg["id"], err):
            raise
        return
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "portico/webrtc_offer",
        vol.Required("call_id"): str,
        vol.Required("door_id"): str,
        vol.Required("sdp"): str,
    }
)
@websocket_api.async_response
async def handle_webrtc_offer(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """The offer names the door it is for (WS-05): the call must be that door's (or, when the registry never
    learned which door a call belongs to, one of that door's provider's), and the SDP passes through untouched. An unknown door is `unknown_door`; a call that provider does not own is `unknown_call`."""
    if not auth.require(connection, msg, "portico/webrtc_offer"):
        return
    found = _door_for(hass, connection, msg, msg["door_id"])
    if found is None:
        return
    registry, provider, door = found
    owner = registry.door_of_call(msg["call_id"])
    if registry.for_call(msg["call_id"]) is not provider or (owner is not None and owner != door.id):
        connection.send_error(msg["id"], "unknown_call", msg["call_id"])
        return
    await _webrtc(hass, connection, msg, "webrtc_offer", msg["sdp"])
    if str(msg["call_id"]).startswith("out-"):    # an outbound call: the offer outcome, never the SDP
        _LOGGER.info("portico: webrtc_offer for %s on %s handled", msg["call_id"], msg["door_id"])


@websocket_api.websocket_command(
    {
        vol.Required("type"): "portico/webrtc_answer",
        vol.Required("call_id"): str,
        vol.Required("sdp"): str,
    }
)
@websocket_api.async_response
async def handle_webrtc_answer(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    if not auth.require(connection, msg, "portico/webrtc_answer"):
        return
    await _webrtc(hass, connection, msg, "webrtc_answer", msg["sdp"])


@websocket_api.websocket_command(
    {
        vol.Required("type"): "portico/webrtc_ice",
        vol.Required("call_id"): str,
        vol.Required("candidate"): str,
    }
)
@websocket_api.async_response
async def handle_webrtc_ice(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    if not auth.require(connection, msg, "portico/webrtc_ice"):
        return
    await _webrtc(hass, connection, msg, "webrtc_ice", msg["candidate"])


# --------------------------------------------------------------------------
# Push ring notification: the push registry (FCM sender design §3.3)
# --------------------------------------------------------------------------

def _push_caller(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict):
    """(manager, user_id, name), or None after sending the error."""
    manager = get_manager(hass)
    if manager is None:
        connection.send_error(msg["id"], "push_not_configured", "the push ring notification (Firebase) is not set up")
        return None
    user_id = connection.user.id if connection.user else None
    # auth.require has already let only an enrolled phone through (portico/push/*)
    return manager, user_id, enrolled_phone_name(hass, user_id) or "phone"


# The oldest app that speaks contract 3 and push v2 (M6 A3 releases 0.5.0-alpha).
MIN_APP_VERSION = (0, 5, 0)
_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)")


def _app_version_ok(app_version: str) -> bool:
    """Is `app_version` ("0.5.0-alpha", "0.4.3-alpha", ...) at least MIN_APP_VERSION? A pre-release tag counts as its
    release (0.5.0-alpha is the first build of 0.5.0); a blank or unparseable version is too old."""
    found = _VERSION_RE.match(app_version.strip())
    return bool(found) and tuple(int(part) for part in found.groups()) >= MIN_APP_VERSION


@websocket_api.websocket_command(
    {
        vol.Required("type"): "portico/push/register",
        vol.Required("installation_id"): REGISTER_FIELDS["installation_id"],
        vol.Required("kind"): REGISTER_FIELDS["kind"],
        vol.Required("registration"): REGISTER_FIELDS["registration"],
        vol.Required("package"): REGISTER_FIELDS["package"],
        vol.Required("fcm_project_id"): REGISTER_FIELDS["fcm_project_id"],
        vol.Optional("app_version", default=""): REGISTER_FIELDS["app_version"],
    }
)
@websocket_api.async_response
async def handle_push_register(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Upsert the caller's own row on (ha_user_id, installation_id). The app
    calls this on every WebSocket connect (§3.3)."""
    if not auth.require(connection, msg, "portico/push/register"):
        return
    if not _app_version_ok(msg["app_version"]):
        # 0.4.3 speaks contract 2 and drops a `pv: "2"` push: it is refused here so the gap is visible (a zero in
        # sensor.portico_push_ring_notification) and ends when 0.5.0 registers (M6 review 7, decision 2).
        connection.send_error(msg["id"], "app_too_old",
                              f"Portico app {'.'.join(map(str, MIN_APP_VERSION))} or newer is required (contract 3)")
        return
    caller = _push_caller(hass, connection, msg)
    if caller is None:
        return
    manager, user_id, name = caller
    # The checks and the upsert are shared with the admin's pasted
    # registration string (push/registration.py, push_flow.py).
    try:
        row = await async_register_phone(
            manager, user_id=user_id, name=name, installation_id=msg["installation_id"], kind=msg["kind"],
            registration=msg["registration"], package=msg["package"], fcm_project_id=msg["fcm_project_id"],
            app_version=msg["app_version"], enroll_id=(enrolled_phone(hass, user_id) or {}).get("enroll_id"),
        )
    except RegistrationRefused as err:
        connection.send_error(msg["id"], err.code, err.message)
        return
    connection.send_result(msg["id"], {"reg_id": row["reg_id"], "push_enabled": bool(row["push_enabled"])})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "portico/push/unregister",
        vol.Required("installation_id"): vol.All(str, vol.Length(min=1, max=128)),
    }
)
@websocket_api.async_response
async def handle_push_unregister(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    if not auth.require(connection, msg, "portico/push/unregister"):
        return
    caller = _push_caller(hass, connection, msg)
    if caller is None:
        return
    manager, user_id, _name = caller
    if not await manager.registry.async_unregister(user_id, msg["installation_id"]):
        connection.send_error(msg["id"], "not_found", "no such registration for this phone")
        return
    connection.send_result(msg["id"], {"removed": True})


@websocket_api.websocket_command({vol.Required("type"): "portico/push/status"})
@websocket_api.async_response
async def handle_push_status(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """The caller's own rows only, never a registration value (§3.3)."""
    if not auth.require(connection, msg, "portico/push/status"):
        return
    caller = _push_caller(hass, connection, msg)
    if caller is None:
        return
    manager, user_id, _name = caller
    connection.send_result(msg["id"], {
        "state": manager.state,
        "registrations": [PushRegistry.public(row) for row in manager.registry.rows_for_user(user_id)],
    })


# --------------------------------------------------------------------------
# Content (content design §3): what the app and the dashboard card show.
# --------------------------------------------------------------------------

def _unauthorized(connection: websocket_api.ActiveConnection, msg: dict, text: str) -> None:
    connection.send_error(msg["id"], "unauthorized", text)


def _content_target(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict):
    """(store, endpoint, display_name), or None after sending the error. `endpoint` is an `endpoints.Endpoint`, a
    phone or a dashboard; the grant of `content/run` and `content/snapshot` is its resolved document.

    - An enrolled phone gets its own endpoint (`registry.for_user`). `endpoint` naming another is `unauthorized`.
    - A declared screen (a dashboard user) gets the dashboard `endpoint` names, or its only one when it names none; a
      login that serves several screens must name one ("name the screen"); naming another's is `unauthorized`.
    - An administrator names any phone or dashboard with `endpoint` (`unknown_endpoint` for none). Contract 3 has no
      `profile` (a phone's name): an administrator names an endpoint id, as the editor lists them.
    - Anyone else is refused."""
    store = get_content_store(hass)
    registry = get_endpoint_registry(hass)
    user = connection.user
    user_id = user.id if user else None
    wanted_endpoint = msg.get("endpoint")

    if wanted_endpoint and user is not None and user.is_admin:
        endpoint = registry.get(wanted_endpoint)
        if endpoint is None or endpoint.kind not in (KIND_PHONE, KIND_DASHBOARD):
            connection.send_error(msg["id"], "unknown_endpoint", "no phone or screen with that id")
            return None
        return store, endpoint, endpoint.name

    phone = next((e for e in registry.endpoints(kind=KIND_PHONE) if user_id and e.ha_user_id == user_id), None)
    own = enrolled_phone_name(hass, user_id)
    screens = registry.dashboards_for_user(user_id)

    if own is not None and phone is not None:
        if wanted_endpoint:
            if wanted_endpoint == phone.id:
                return store, phone, phone.name
            screen = next((s for s in screens if s.id == wanted_endpoint), None)
            if screen is not None:
                return store, screen, screen.name
            _unauthorized(connection, msg, "a phone can only use its own endpoint")
            return None
        return store, phone, own

    if screens:
        if wanted_endpoint:
            screen = next((s for s in screens if s.id == wanted_endpoint), None)
            if screen is None:
                _unauthorized(connection, msg, "a screen can only use its own endpoint")
                return None
            return store, screen, screen.name
        if len(screens) == 1:
            return store, screens[0], screens[0].name
        _unauthorized(connection, msg, "this login serves several screens; name the screen with `endpoint`")
        return None

    _unauthorized(connection, msg, "only an enrolled Portico phone, or an admin naming one, may do this")
    return None


def _grant_user(endpoint: Endpoint) -> str:
    """The HA user a run is attributed to when no one else is: the endpoint's own (a screen's login, a phone's)."""
    return endpoint.ha_user_id or endpoint.id


@websocket_api.websocket_command({
    vol.Required("type"): "portico/content/get",
    vol.Optional("endpoint"): vol.All(str, vol.Length(min=1, max=64)),
})
@websocket_api.async_response
async def handle_content_get(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """The endpoint's resolved document (`endpoint`, `kind`, `rev`, `home_name`, `pages`, `callers`, `roster`), nothing
    more: contract 3 drops v1's flat `phone`/`cameras`/`actions` and the `profile` and `pages` request keys. The
    store is loaded first: whether a login is a declared screen depends on it."""
    await get_content_store(hass).async_ensure_loaded()
    if not auth.require(connection, msg, "portico/content/get"):
        return
    target = _content_target(hass, connection, msg)
    if target is None:
        return
    store, endpoint, _name = target
    document = store.resolve_for_endpoint(endpoint.id)
    first = id(connection) not in _FIRST_DOC_SEEN
    if first:
        if len(_FIRST_DOC_SEEN) > 256:
            _FIRST_DOC_SEEN.clear()
        _FIRST_DOC_SEEN.add(id(connection))
    _LOGGER.log(logging.INFO if first else logging.DEBUG, "portico content/get: %s document conn=%s phone=%s endpoint=%s rev=%s talk=%s",
                "first" if first else "next", _conn(connection), _who(connection), endpoint.id, document.get("rev"),
                _talk_elements(document) or "-")
    connection.send_result(msg["id"], document)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "portico/content/run",
        vol.Required("action_id"): vol.All(str, vol.Length(min=1, max=64)),
        vol.Optional("endpoint"): vol.All(str, vol.Length(min=1, max=64)),
    }
)
@websocket_api.async_response
async def handle_content_run(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    await get_content_store(hass).async_ensure_loaded()
    if not auth.require(connection, msg, "portico/content/run"):
        return
    target = _content_target(hass, connection, msg)
    if target is None:
        return
    store, endpoint, name = target
    try:
        extra = await store.async_run(
            _grant_user(endpoint), name, msg["action_id"],
            caller_user_id=connection.user.id if connection.user else None,
            caller_name=connection.user.name if connection.user else None,
            endpoint_id=endpoint.id)
    except RunRefused as err:
        connection.send_error(msg["id"], err.code, err.message)
        return
    connection.send_result(msg["id"], {"ran": True, **extra})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "portico/content/snapshot",
        vol.Required("camera_id"): vol.All(str, vol.Length(min=1, max=64)),
        vol.Optional("width", default=SNAPSHOT_WIDTH_DEFAULT): vol.All(
            int, vol.Range(min=SNAPSHOT_WIDTH_MIN, max=SNAPSHOT_WIDTH_MAX)),
        vol.Optional("endpoint"): vol.All(str, vol.Length(min=1, max=64)),
    }
)
@websocket_api.async_response
async def handle_content_snapshot(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """One still from a camera of the caller's own endpoint (a page camera, or a `c-...` camera of one of its
    callers), as base64, over the same socket (and so the same path, tailnet included) the app already has."""
    await get_content_store(hass).async_ensure_loaded()
    if not auth.require(connection, msg, "portico/content/snapshot"):
        return
    target = _content_target(hass, connection, msg)
    if target is None:
        return
    store, endpoint, name = target
    try:
        content_type, data = await store.async_snapshot(
            _grant_user(endpoint), name, msg["camera_id"], msg["width"], endpoint_id=endpoint.id)
    except RunRefused as err:
        connection.send_error(msg["id"], err.code, err.message)
        return
    connection.send_result(msg["id"], {"content_type": content_type, "content": base64.b64encode(data).decode("ascii")})


def _candidate_schema(value: Any) -> dict[str, Any]:
    """A browser's RTCIceCandidateInit: `candidate` (empty is the end-of-candidates mark) and optionally `sdpMid`,
    `sdpMLineIndex` and `usernameFragment`/`userFragment`; nothing else."""
    if not isinstance(value, dict):
        raise vol.Invalid("a candidate is an object")
    return vol.Schema({
        vol.Required("candidate"): vol.All(str, vol.Length(max=live.MAX_CANDIDATE)),
        vol.Optional("sdpMid"): vol.Any(None, vol.All(str, vol.Length(max=64))),
        vol.Optional("sdpMLineIndex"): vol.Any(None, vol.All(int, vol.Range(min=0, max=255))),
        vol.Optional("usernameFragment"): vol.Any(None, vol.All(str, vol.Length(max=256))),
        vol.Optional("userFragment"): vol.Any(None, vol.All(str, vol.Length(max=256))),
    })(value)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "portico/content/live",
        vol.Required("camera_id"): vol.All(str, vol.Length(min=1, max=64)),
        vol.Required("sdp"): vol.All(str, vol.Length(min=1, max=live.MAX_SDP)),
        vol.Optional("size", default="big"): vol.In(live.LIVE_SIZES),
        vol.Optional("endpoint"): vol.All(str, vol.Length(min=1, max=64)),
    }
)
@websocket_api.async_response
async def handle_content_live(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Live video of a camera of the caller's own endpoint, WebRTC, video only (live.py). The grant is
    `content/snapshot`'s: `_content_target` for who and which endpoint, `ContentStore._granted_camera` for which camera."""
    await get_content_store(hass).async_ensure_loaded()
    if not auth.require(connection, msg, "portico/content/live"):
        return
    target = _content_target(hass, connection, msg)
    if target is None:
        return
    store, endpoint, name = target
    await live.async_start(hass, connection, msg, store, endpoint, _grant_user(endpoint), name)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "portico/content/live_ice",
        vol.Required("session_id"): vol.All(str, vol.Length(min=1, max=64)),
        vol.Required("candidate"): _candidate_schema,
    }
)
@websocket_api.async_response
async def handle_content_live_ice(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """One client ICE candidate for a live session THIS connection opened."""
    if not auth.require(connection, msg, "portico/content/live_ice"):
        return
    await live.async_ice(hass, connection, msg)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "portico/content/live_close",
        vol.Required("session_id"): vol.All(str, vol.Length(min=1, max=64)),
    }
)
@callback
def handle_content_live_close(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """End a live session this connection opened (idempotent)."""
    if not auth.require(connection, msg, "portico/content/live_close"):
        return
    live.async_close(hass, connection, msg)


@callback
def async_setup_signaling(hass: HomeAssistant) -> None:
    """Register every portico/* websocket_api command (§9.5)."""
    websocket_api.async_register_command(hass, handle_version)
    websocket_api.async_register_command(hass, handle_doors)
    websocket_api.async_register_command(hass, handle_calls_subscribe)
    websocket_api.async_register_command(hass, handle_content_subscribe)
    websocket_api.async_register_command(hass, handle_dial)
    websocket_api.async_register_command(hass, handle_hangup)
    websocket_api.async_register_command(hass, handle_open_relay)
    websocket_api.async_register_command(hass, handle_answer)
    websocket_api.async_register_command(hass, handle_keepalive)
    websocket_api.async_register_command(hass, handle_webrtc_offer)
    websocket_api.async_register_command(hass, handle_webrtc_answer)
    websocket_api.async_register_command(hass, handle_webrtc_ice)
    away_media.async_setup_away_media(hass)
    websocket_api.async_register_command(hass, handle_push_register)
    websocket_api.async_register_command(hass, handle_push_unregister)
    websocket_api.async_register_command(hass, handle_push_status)
    websocket_api.async_register_command(hass, handle_content_get)
    websocket_api.async_register_command(hass, handle_content_run)
    websocket_api.async_register_command(hass, handle_content_snapshot)
    websocket_api.async_register_command(hass, handle_content_live)
    websocket_api.async_register_command(hass, handle_content_live_ice)
    websocket_api.async_register_command(hass, handle_content_live_close)
    async_setup_editor_commands(hass)       # content/document | preview | save, kiosk/hello (M6 E2)
    async_setup_elements_commands(hass)     # elements/list | set | delete (DEV-760 E2; admin only)
    async_setup_connlog_commands(hass)      # connlog/report (phone) | get | clear (admin) (DEV-705)
    async_setup_panel_wall_commands(hass)   # wall/heartbeat (phone; a panel's; DEV-807)
    async_setup_panel_call(hass)            # panel/call_result (phone; a panel's) and the `portico.call_everyone` service (DEV-807 follow-up)
    async_setup_phones_commands(hass)       # phones/list | enroll | cancel | revoke | set (UI split P2, P3; admin only)
    async_setup_door_commands(hass)         # door_status, door_settings/get | set (UI split P4, P5; admin only)
    async_setup_ui_settings_commands(hass)  # ui_settings/get | set (UI split P6; admin only)
    async_setup_house_commands(hass)        # house/answer | decline | hangup | presence (M7 3a; not_available while off)
