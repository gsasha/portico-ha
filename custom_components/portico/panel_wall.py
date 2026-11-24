"""DEV-807 panel mode, server slice 2: the panel heartbeat and its readiness sensor (design section 9).

`portico/wall/heartbeat` (phone class) is what a panel's app sends: at once on socket open, on every change of a ready input and
every 60 s. The server keeps the last one per HA user IN MEMORY (nothing is stored, nothing is written to a log) and answers
`{house_ok, phone_endpoint}`, so the app can compare and refetch when they differ from what it has cached (section 5 item 2).

`sensor.portico_panel_<name>` (entities/sensor.py, one per phone entry whose `panel` option is on) has four states:

  ready             every input holds
  ready_no_doors    every input holds and the panel knows 0 doors (`wake_doors` 0, no `wake_all`)
  not_ready         something does not hold, or the heartbeat is older than 3 minutes (`heartbeat_stale`)
  unknown           from HA start until the first heartbeat or 150 s, whichever is first

`ready` needs: the panel's `calls/subscribe` is open, a heartbeat younger than 3 minutes, an app at or above the server's minimum,
`panel_mode`, `endpoint_ok`, overlay, FSI, microphone, notifications, `ring_audible`, battery exemption, stay-connected,
`ring_level_ok`, doors known, and, whenever the phone entry's `house_receive` is on, `house_ok` (receive on, the heartbeat's
`phone_endpoint` equal to the server's own; the old `house_switch` / `house_gate` are still accepted and shown but no longer read; a heartbeat with no `phone_endpoint` is an app
that does not support panel house calls, so false). With receive off, doors-only readiness is unchanged.

`ring_sent_not_shown`: the server sent a ring for a door in the panel's `wake` and, 10 s later, no heartbeat received since has
carried that call id with `last_ring_path` `direct` or `fsi`. The call id is the match, not clocks. `phone_endpoint` is never in
the attributes.
"""
from __future__ import annotations

import logging
import re
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import CALLBACK_TYPE, HassJob, HomeAssistant, callback
from homeassistant.helpers.event import async_call_later

from . import auth
from .endpoints import door_in_refs, is_panel_user, panel_wake_doors, phone_of_user
from .phone_entry import CONF_HOUSE_RECEIVE, phone_entry_for_user
from .provider import DOMAIN

_LOGGER = logging.getLogger(__name__)

DATA_WALL = "panel_wall"

HEARTBEAT_STALE_S = 180          # a heartbeat older than this reads `not_ready` and sets `heartbeat_stale`
UNKNOWN_S = 150                  # `unknown` lasts from HA start until the first heartbeat or this long
RING_SHOWN_WITHIN_S = 10         # a ring the app did not report shown within this is `ring_sent_not_shown`
_RINGS_KEPT = 8

STATE_READY, STATE_READY_NO_DOORS, STATE_NOT_READY, STATE_UNKNOWN = "ready", "ready_no_doors", "not_ready", "unknown"
STATES = [STATE_READY, STATE_READY_NO_DOORS, STATE_NOT_READY, STATE_UNKNOWN]

FRONT_REASONS = ("ring", "call", "hand", "none")
RING_PATHS = ("direct", "fsi", "banner", "failed", "filtered")
RING_PATHS_SHOWN = ("direct", "fsi")
CALL_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")        # the push payload's id rule (push/payload.py)

# every input `ready` reads, besides the version, the doors and the house gate; absent = False
READY_BOOLS = ("panel_mode", "endpoint_ok", "overlay_ok", "fsi_ok", "microphone_ok", "notifications_ok", "ring_audible",
               "battery_exempt", "stay_connected", "ring_level_ok")
OTHER_BOOLS = ("foreground", "overdue", "house_switch", "house_gate", "wake_all", "auto_revoke_exempt", "panel_call")
# what the sensor shows besides the state: bounded, no secrets, no free text, never `phone_endpoint`
ATTRIBUTE_FIELDS = ("app_version", *READY_BOOLS, "foreground", "overdue", "front_reason", "wake_doors", "wake_all",
                    "last_ring_call", "last_ring_path", "last_ring_door", "house_switch", "house_gate", "auto_revoke_exempt")


@dataclass(eq=False)
class _Ring:
    seq: int
    call_id: str
    shown: bool = False
    timer: CALLBACK_TYPE | None = None


@dataclass(eq=False)
class _User:
    beat: dict[str, Any] | None = None
    beat_at: float | None = None             # monotonic
    sockets: int = 0                          # open `portico/calls/subscribe` of this user
    rings: "OrderedDict[str, _Ring]" = field(default_factory=OrderedDict)
    not_shown_seq: int | None = None          # the seq of the ring flagged `ring_sent_not_shown`, or None


class PanelWall:
    """What the server knows about each panel's app, by HA user id. In memory only."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._users: dict[str, _User] = {}
        self._listeners: dict[str, list[Callable[[], None]]] = {}
        self._seq = 0
        self.started = time.monotonic()

    # ---- listeners (the sensors) ----------------------------------------------------------------------------------

    def add_listener(self, user_id: str, listener: "Callable[[], None]") -> "Callable[[], None]":
        self._listeners.setdefault(user_id, []).append(listener)

        def _remove() -> None:
            if listener in self._listeners.get(user_id, []):
                self._listeners[user_id].remove(listener)

        return _remove

    def _changed(self, user_id: str) -> None:
        for listener in list(self._listeners.get(user_id, [])):
            try:
                listener()
            except Exception:  # noqa: BLE001 - one sensor must not stop the rest
                _LOGGER.warning("portico panel: a sensor could not be refreshed", exc_info=True)

    def _user(self, user_id: str) -> _User:
        return self._users.setdefault(user_id, _User())

    # ---- the socket ------------------------------------------------------------------------------------------------

    def socket_opened(self, user_id: str | None) -> None:
        if user_id:
            self._user(user_id).sockets += 1
            self._changed(user_id)

    def socket_closed(self, user_id: str | None) -> None:
        if user_id and user_id in self._users:
            self._users[user_id].sockets = max(0, self._users[user_id].sockets - 1)
            self._changed(user_id)

    # ---- the heartbeat ---------------------------------------------------------------------------------------------

    def heartbeat(self, user_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        """Take one heartbeat and return the reply `{house_ok, phone_endpoint}` (`phone_endpoint` is the SERVER's own)."""
        user = self._user(user_id)
        user.beat = dict(fields)
        user.beat_at = time.monotonic()
        call_id, path = fields.get("last_ring_call"), fields.get("last_ring_path")
        ring = user.rings.get(call_id) if isinstance(call_id, str) else None
        if ring is not None and path in RING_PATHS_SHOWN:
            ring.shown = True
            if ring.timer is not None:
                ring.timer()
                ring.timer = None
            if user.not_shown_seq is not None and ring.seq >= user.not_shown_seq:
                user.not_shown_seq = None          # that ring, or a later one, was shown: cleared
        self._changed(user_id)
        return self.reply(user_id, fields)

    def reply(self, user_id: str, fields: dict[str, Any] | None = None) -> dict[str, Any]:
        beat = fields if fields is not None else (self._users[user_id].beat if user_id in self._users else None) or {}
        return {"house_ok": self.house_ok(user_id, beat), "phone_endpoint": self.server_phone_endpoint(user_id)}

    def server_phone_endpoint(self, user_id: str) -> str | None:
        from .endpoints import get_endpoint_registry, KIND_PHONE

        phone = next((e for e in get_endpoint_registry(self.hass).endpoints(kind=KIND_PHONE) if e.ha_user_id == user_id), None)
        return phone.id if phone is not None else None

    def house_receive(self, user_id: str) -> bool:
        entry = phone_entry_for_user(self.hass, user_id)
        return entry is not None and entry.options.get(CONF_HOUSE_RECEIVE) is True

    def house_ok(self, user_id: str, beat: dict[str, Any]) -> bool:
        """Receive on, the heartbeat's `phone_endpoint` equal to the server's own (a heartbeat with none is an app that does not
        support panel house calls). The app's old `house_switch` / `house_gate` are no longer read (D-20261122-aafb)."""
        server = self.server_phone_endpoint(user_id)
        return bool(self.house_receive(user_id) and server is not None and beat.get("phone_endpoint") == server)

    # ---- the rings the server sent ---------------------------------------------------------------------------------

    def note_ring(self, user_id: str | None, call_id: str | None, door_id: str | None) -> None:
        """The server sent a door ring (by socket or by push) to this user. Only a panel's, for a door in its `wake`, is kept; the
        call id is the match. Never raises."""
        try:
            if not user_id or not call_id or not is_panel_user(self.hass, user_id):
                return
            phone = phone_of_user(self.hass, user_id)
            if phone is None:
                return
            wake = panel_wake_doors(self.hass, phone.id)
            if wake is not None and door_id is not None and not door_in_refs(wake, door_id):
                return
            user = self._user(user_id)
            if call_id in user.rings:
                return                               # the socket and the push both announce one ring
            self._seq += 1
            ring = _Ring(self._seq, call_id)
            user.rings[call_id] = ring
            while len(user.rings) > _RINGS_KEPT:
                _, old = user.rings.popitem(last=False)
                if old.timer is not None:
                    old.timer()

            @callback
            def _due(_now) -> None:
                ring.timer = None
                if not ring.shown and call_id in user.rings:
                    user.not_shown_seq = ring.seq if user.not_shown_seq is None else min(user.not_shown_seq, ring.seq)
                    self._changed(user_id)

            ring.timer = async_call_later(self.hass, RING_SHOWN_WITHIN_S,
                                          HassJob(_due, "portico panel ring check", cancel_on_shutdown=True))
        except Exception:  # noqa: BLE001
            _LOGGER.warning("portico panel: a ring could not be noted", exc_info=True)

    def cancel_user(self, user_id: str) -> None:
        """Drop the timers of one user (its sensor is going away)."""
        user = self._users.get(user_id)
        if user is not None:
            for ring in user.rings.values():
                if ring.timer is not None:
                    ring.timer()
                    ring.timer = None

    # ---- the state -------------------------------------------------------------------------------------------------

    def evaluate(self, user_id: str, *, now: float | None = None) -> tuple[str, dict[str, Any]]:
        """`(state, attributes)` of this user's panel now. `last_seen` is in the attributes and is the one that moves without the
        state moving (the sensor does not write for it)."""
        from .signaling import _app_version_ok

        now = time.monotonic() if now is None else now
        user = self._users.get(user_id) or _User()
        beat = user.beat
        receive = self.house_receive(user_id)
        attrs: dict[str, Any] = {"house_receive": receive, "socket_open": user.sockets > 0,
                                 "ring_sent_not_shown": user.not_shown_seq is not None}
        if beat is None or user.beat_at is None:
            state = STATE_UNKNOWN if now - self.started < UNKNOWN_S else STATE_NOT_READY
            attrs["heartbeat_stale"] = state == STATE_NOT_READY
            return state, attrs
        stale = now - user.beat_at > HEARTBEAT_STALE_S
        attrs["heartbeat_stale"] = stale
        attrs.update({k: beat[k] for k in ATTRIBUTE_FIELDS if k in beat})
        house_ok = self.house_ok(user_id, beat)
        attrs["house_ok"] = house_ok
        problems = []
        if not attrs["socket_open"]:
            problems.append("socket")
        if stale:
            problems.append("stale")
        if not _app_version_ok(str(beat.get("app_version") or "")):
            problems.append("version")
        problems.extend(k for k in READY_BOOLS if beat.get(k) is not True)
        if receive and not house_ok:
            problems.append("house")
        attrs["problems"] = problems
        if problems:
            return STATE_NOT_READY, attrs
        no_doors = beat.get("wake_all") is not True and not beat.get("wake_doors")
        return (STATE_READY_NO_DOORS if no_doors else STATE_READY), attrs

    def status(self, user_id: str) -> tuple[dict[str, Any] | None, float | None, int]:
        """`(the last heartbeat or None, its age in seconds or None, the open `calls/subscribe` sockets)` of this login. A copy."""
        user = self._users.get(user_id)
        if user is None:
            return None, None, 0
        age = None if user.beat_at is None else time.monotonic() - user.beat_at
        return (dict(user.beat) if user.beat is not None else None), age, user.sockets

    def last_seen(self, user_id: str) -> float | None:
        user = self._users.get(user_id)
        return user.beat_at if user is not None else None


def get_panel_wall(hass: HomeAssistant) -> PanelWall:
    data = hass.data.setdefault(DOMAIN, {})
    wall = data.get(DATA_WALL)
    if wall is None:
        wall = data[DATA_WALL] = PanelWall(hass)
    return wall


def _fields_schema() -> dict:
    schema: dict = {
        vol.Required("type"): "portico/wall/heartbeat",
        vol.Optional("app_version", default=""): vol.All(str, vol.Length(max=40)),
        vol.Optional("front_reason", default="none"): vol.In(FRONT_REASONS),
        vol.Optional("wake_doors", default=0): vol.All(int, vol.Range(min=0, max=999)),
        vol.Optional("last_ring_call"): vol.Any(None, vol.All(str, vol.Match(CALL_ID_RE))),
        vol.Optional("last_ring_path"): vol.Any(None, vol.In(RING_PATHS)),
        vol.Optional("last_ring_door"): vol.Any(None, vol.All(str, vol.Length(max=128))),
        vol.Optional("phone_endpoint"): vol.Any(None, vol.All(str, vol.Length(max=64))),
    }
    for key in (*READY_BOOLS, *OTHER_BOOLS):
        schema[vol.Optional(key)] = bool
    return schema


@websocket_api.websocket_command(_fields_schema())
@callback
def handle_wall_heartbeat(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """A panel's heartbeat. Phone class; only the login of a phone entry whose `panel` option is on may send one (`not_a_panel`).
    -> `{house_ok, phone_endpoint}`."""
    if not auth.require(connection, msg, "portico/wall/heartbeat"):
        return
    user_id = connection.user.id if connection.user else None
    if not user_id or not is_panel_user(hass, user_id):
        connection.send_error(msg["id"], "not_a_panel", "this phone is not a panel")
        return
    fields = {k: v for k, v in msg.items() if k not in ("id", "type")}
    connection.send_result(msg["id"], get_panel_wall(hass).heartbeat(user_id, fields))


@callback
def async_setup_panel_wall_commands(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, handle_wall_heartbeat)
