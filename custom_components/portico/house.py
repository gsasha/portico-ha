"""House calls, the server model (house calls design 4.1, 5, 7, 15; M7 PR 3a). INERT: nothing here is reachable by a
real user until later PRs.

A house call is a call between two endpoints of the house (a phone, or a declared screen), or from a screen to every
phone that receives ("Everyone"). This module is the registry of those calls and the roster they are placed from. It
has no media (3b) and no client (3b-A, 3d): `place()` returns `media: None`, and a call that is answered ends `error` at
the 10 s audio deadline because nothing opens a leg. M7 3c adds the push for AWAY phones (15.5): a callee with `receive` on and
no open subscription is rung by a `ct: house` push (`push_only` presence) when the push ring notification is set up for it; the ring of a
phone with an open subscription is the socket's, as before.

**It is always on** (D-20261122-aafb: who is called is decided per action by the Talk and group elements, so the global House
calls switch is gone). `async_setup_house` builds the registry at domain setup; the settings entry's old option `house_calls`
(OPTION_HOUSE_CALLS) is still read as a name but IGNORED, so an entry that stored it loads as before. `get_house(hass)` is None only
before setup or in a test that passes `enabled=False`, and then every hook in signaling.py and content.py sees None, the four
`portico/house/*` commands answer `not_available`, the roster gains nothing and the placement seam stays the Null registry. A second system beside the old one
(`house-talk`, `input_text.intercom_*`, the `home-house` card): this module never imports, reads or writes any of
them (tests assert it).

**Stay-out rules (design 15.4, binding on 3a).** A house call id (`hso-...`) never enters `CallStateTracker`,
`ProviderRegistry.note_call`, `_require_call` or the push notifier: the registry listens to the door transitions the
notifier listens to and never feeds them anything. It has its own events (`house_call` on `portico/calls/subscribe`)
and its own ids.

Who rings: the expected set of "Everyone" and the callability of a phone follow HC-2 (a phone receives only when its
`house_receive` option is on, default off), HC-3 (online = an open `portico/calls/subscribe`), HC-4 (a phone that
reported Do Not Disturb is never rung), HC-5 (one house call per endpoint; a door outranks it; four at once) and HC-9
(only a screen may call everyone, enforced here by endpoint kind).
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import CALLBACK_TYPE, HassJob, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_call_later

from .call_endpoints import (
    REASON_BUSY,
    REASON_DND,
    REASON_NO_PANELS,
    REASON_NO_PHONES,
    REASON_NO_RECEIVERS,
    REASON_NOT_AVAILABLE,
    REASON_UNKNOWN_TARGET,
    REASON_OFFLINE,
    REASON_PUSH_ONLY,
    REASON_RECEIVE_OFF,
    Endpoint as TargetEndpoint,
    PlaceResult,
    TargetState,
    set_call_registry,
)
from .endpoints import (
    KIND_DASHBOARD, KIND_PHONE, Endpoint, door_in_refs, get_endpoint_registry, is_app_screen, is_panel_user, panel_wake_doors, phone_of)
from .gate_target import dialable_doors
from .house_bridge import HouseBridge
from .house_ledger import (
    BUSY_STATES,  # noqa: F401 - re-exported for the tests
    CODE_TOO_MANY_CALLS,  # noqa: F401
    DOOR_ACTIVE,
    DOOR_RINGING,
    HOUSE_ACTIVE,
    HOUSE_RINGING,
    IDLE,
    ClaimLedger,
    Reservation,
)
from .phone_entry import may_open, phone_entry_for_user, phone_house_receive
from .provider import DATA_PROVIDERS, DOMAIN, CallTransition

_LOGGER = logging.getLogger(__name__)

DATA_HOUSE = "house"
OPTION_HOUSE_CALLS = "house_calls"        # the wire key of a panel's `house_calls` (its own `house_receive`); the old settings-entry option of this name is ignored

HOUSE_PREFIX = "hso-"                     # a house call id; a door's are `gso-...` (design 4)
GROUP_EVERYONE = "everyone"               # the group target of a screen's "Everyone" key
GROUP_PHONES = "phones"                   # every receiving phone entry that is not a panel (group-call design G4)
GROUP_PANELS = "panels"                   # every receiving phone entry that is a panel (`panel: true`, an enrolled app screen)
GROUP_IDS = (GROUP_EVERYONE, GROUP_PHONES, GROUP_PANELS)
GROUP_TYPE = "group"
GROUP_REF = f"{GROUP_TYPE}:{GROUP_EVERYONE}"   # what the roster action id is derived from (never collides with a slug)

RING_WINDOW_S = 45                        # the push contract's ring_window_s (design 4.1)
MEDIA_DEADLINE_S = 10                     # answered -> active needs the first callee frame within this (4.1)

# `house_call` event states and the reasons an `ended` carries (design 4).
STATE_RINGING, STATE_ANSWERED, STATE_ACTIVE, STATE_ENDED = "ringing", "answered", "active", "ended"
REASON_CANCELLED, REASON_DECLINED, REASON_NO_ANSWER = "cancelled", "declined", "no_answer"
REASON_ERROR, REASON_HANGUP, REASON_ANSWERED_ELSEWHERE = "error", "hangup", "answered_elsewhere"
END_REASONS = ("cancelled", "declined", "busy", "no_answer", "dnd", "error", "hangup", "answered_elsewhere")

# Error codes (design 4, 14.8, 15.13). One code, `not_callable`, for every reason a target cannot be named, so the roster
# cannot be probed.
CODE_NOT_AVAILABLE = "not_available"      # house calls are not switched on
CODE_NOT_CALLABLE = "not_callable"
CODE_FORBIDDEN = "forbidden"              # a phone naming "everyone" (HC-9)
CODE_PLACE_FORBIDDEN = "place_forbidden"  # an administrator placing a call as another endpoint
CODE_TARGET_OFFLINE = "target_offline"
CODE_TARGET_DND = "target_dnd"
CODE_BUSY = "busy"
CODE_UNKNOWN_CALL = "unknown_call"
CODE_ALREADY_ANSWERED = "already_answered"
CODE_NOT_SUBSCRIBED = "not_subscribed"
ERROR_CODES = (CODE_NOT_AVAILABLE, CODE_NOT_CALLABLE, CODE_FORBIDDEN, CODE_PLACE_FORBIDDEN, CODE_TARGET_OFFLINE,
               CODE_TARGET_DND, CODE_BUSY, CODE_UNKNOWN_CALL, CODE_ALREADY_ANSWERED, CODE_NOT_SUBSCRIBED,
               CODE_TOO_MANY_CALLS)

_ENDED_MEMORY = 64


EMPTY_ROSTER_REV = hashlib.sha256(json.dumps([]).encode()).hexdigest()[:12]   # `roster_rev` of a house with no rows: what a
                                                                               # client is told when house calls go off


def house_from_ref(endpoint_id: str) -> str:
    """The opaque `from_ref` of a house ring's push (push/v2, `ct: house`): never a name, never the endpoint id itself.
    `e-` and 32 hex, a function of the stable endpoint id, in the id grammar of push/payload.py."""
    return "e-" + hashlib.sha256(endpoint_id.encode()).hexdigest()[:32]


@dataclass(eq=False)
class _Conn:
    """One open `portico/calls/subscribe` connection: presence (HC-3) and the sink of this user's house events."""

    connection: Any
    msg_id: int
    user_id: str
    dnd: bool = False
    dnd_seq: int = 0          # when the client last reported; the latest report among a user's connections wins
    last_rev: str | None = None
    told_rev: str | None = None   # the revision of the last `house_roster_changed` this connection was sent (not the implicit first one)


@dataclass(eq=False)
class _Call:
    call_id: str
    caller: str                         # endpoint id
    target: str                         # an endpoint id, or GROUP_EVERYONE
    group: bool
    expected: set[str]                  # who was rung
    ringing: set[str]                   # who is still ringing
    state: str = STATE_RINGING
    reason: str | None = None
    answered_by: str | None = None
    declined: int = 0
    displaced: int = 0
    lost: int = 0
    ring_timer: CALLBACK_TYPE | None = None
    media_timer: CALLBACK_TYPE | None = None
    pushed: set[str] = field(default_factory=set)   # callees rung by a `ct: house` push (away at the moment of the ring)
    users: dict[str, str | None] = field(default_factory=dict)   # endpoint id -> its HA user, as of the placement (R5)


class HouseHandle:
    """What `attach` returns for one connection: `snapshots()` to send after the subscribe result, `detach()` on close."""

    def __init__(self, registry: "HouseCallRegistry", conn: _Conn) -> None:
        self._registry = registry
        self._conn = conn

    @property
    def registry(self) -> "HouseCallRegistry":
        """The registry this handle was attached to: a handle of a registry that was stopped is not the live one's."""
        return self._registry

    @property
    def hears_roster(self) -> bool:
        """Is this connection told `house_roster_changed`? Only a login that is an endpoint's user is (it has a revision)."""
        return self._conn.last_rev is not None

    def snapshots(self) -> list[dict]:
        return self._registry.snapshots_for(self._conn)

    def roster_event(self) -> dict | None:
        """`house_roster_changed {rev}` for this connection alone, or None for a login that is no endpoint's user. Sent to a
        subscription that was attached after it opened (the switch was turned on), so its client reads the roster at once."""
        conn = self._conn
        if conn.last_rev is None:
            return None
        rev = self._registry.roster_rev()
        if conn.told_rev == rev:
            return None                                            # it was told this very revision a moment ago
        conn.told_rev = conn.last_rev = rev
        return {"type": "house_roster_changed", "rev": rev}

    def detach(self) -> None:
        self._registry.detach(self._conn)


class HouseCallRegistry:
    """The house-call registry: also the `CallEndpointRegistry` the content code places calls through (call_endpoints.py)."""

    def __init__(self, hass: HomeAssistant, *, ring_s: float = RING_WINDOW_S, media_s: float = MEDIA_DEADLINE_S,
                 id_factory: Callable[[], str] | None = None) -> None:
        self.hass = hass
        self.ledger = ClaimLedger()
        self._ring_s = ring_s
        self._media_s = media_s
        self._id_factory = id_factory or self._default_id
        self._serial = 0
        self._dnd_serial = 0
        self._calls: dict[str, _Call] = {}
        self._ended: OrderedDict[str, None] = OrderedDict()
        self._conns: list[_Conn] = []
        self._unsub_doors: Callable[[], None] | None = None
        self._unsub_phones: Callable[[], None] | None = None
        self._away_ids: frozenset[str] = frozenset()
        self._closed = False
        # The media glue (3b-S): does nothing unless a provider's hub runs the bridge (house_bridge.py).
        self.bridge = HouseBridge(self)

    # ---- lifecycle ----------------------------------------------------------------------------------------------

    def start(self) -> None:
        """Listen to the door transitions the push notifier listens to (read only: nothing is fed back to them)."""
        registry = self.hass.data.get(DOMAIN, {}).get(DATA_PROVIDERS)
        if registry is not None and self._unsub_doors is None:
            self._unsub_doors = registry.subscribe(self.on_door_transition)
        self.bridge.start()
        if self._unsub_phones is None:
            # a push registration appearing, dying or being switched off changes who is `push_only`: re-read the roster
            from .push.const import SIGNAL_PHONES_CHANGED

            self._unsub_phones = async_dispatcher_connect(self.hass, SIGNAL_PHONES_CHANGED, self._phones_changed)

    @callback
    def _phones_changed(self) -> None:
        """The push ring notification's state moved (every push result fires this too): tell clients only when WHO IS `push_only` changed,
        so a busy phone or a sent message adds no roster event of its own."""
        if not self._closed and self._away_set() != self._away_ids:
            self._roster_changed()

    def _away_set(self) -> frozenset[str]:
        return frozenset(e.id for e in self._endpoints() if self.away(e))

    def stop(self) -> None:
        self._closed = True
        if self._unsub_phones is not None:
            self._unsub_phones()
            self._unsub_phones = None
        if self._unsub_doors is not None:
            self._unsub_doors()
            self._unsub_doors = None
        for call in list(self._calls.values()):
            self._end(call, REASON_ERROR)
        self.bridge.stop()

    def _later(self, delay: float, action: Callable[[str], None], call_id: str) -> CALLBACK_TYPE:
        """A timer on the event loop (never an executor thread: the action is a `callback`), dropped at shutdown."""
        @callback
        def _fire(_now) -> None:
            action(call_id)

        return async_call_later(self.hass, delay, HassJob(_fire, f"portico house timer {call_id}", cancel_on_shutdown=True))

    def _default_id(self) -> str:
        self._serial += 1
        return f"{HOUSE_PREFIX}{time.time_ns()}-{self._serial}-{secrets.token_hex(3)}"

    # ---- endpoints, receive, presence ----------------------------------------------------------------------------

    def _raw_endpoint(self, endpoint_id: str | None) -> Endpoint | None:
        """The registry's phone or screen of this id, the app screen of a panel included: only the caller of `place_call` and the
        users of an endpoint (R3, R5) may see it; everything that offers an endpoint as a callee goes through `_endpoint`."""
        if not endpoint_id:
            return None
        ep = get_endpoint_registry(self.hass).get(endpoint_id)
        return ep if ep is not None and ep.kind in (KIND_PHONE, KIND_DASHBOARD) else None

    def _endpoint(self, endpoint_id: str | None) -> Endpoint | None:
        """A phone or screen that can be in a house call. The app screen of a panel is not one: its phone half is (DEV-807 R1)."""
        ep = self._raw_endpoint(endpoint_id)
        return None if ep is not None and is_app_screen(self.hass, ep.id) else ep

    def _endpoints(self) -> list[Endpoint]:
        return [e for e in get_endpoint_registry(self.hass).endpoints()
                if e.kind in (KIND_PHONE, KIND_DASHBOARD) and not is_app_screen(self.hass, e.id)]

    def _endpoint_for_user(self, user_id: str | None) -> Endpoint | None:
        if not user_id:
            return None
        found = [e for e in self._endpoints() if e.ha_user_id == user_id]
        return found[0] if len(found) == 1 else None      # a login serving several screens must name one

    @staticmethod
    def _users(ep: Endpoint) -> tuple[str, ...]:
        return (ep.ha_user_id,) if ep.ha_user_id else ()

    def receives(self, ep: Endpoint) -> bool:
        """HC-2: a phone is callable only with its `house_receive` option on (default off); a declared screen is callable
        because it was declared (CB-4: it rings in place and a person presses Answer)."""
        if ep.kind == KIND_DASHBOARD:
            return not is_app_screen(self.hass, ep.id)         # the app screen of a panel is never a callee (its phone half is)
        return ep.kind == KIND_PHONE and phone_house_receive(self.hass, ep.ha_user_id)

    def online(self, ep: Endpoint) -> bool:
        """HC-3: the endpoint's user holds an open subscription. No heartbeat, nothing written to HA state."""
        users = self._users(ep)
        return any(c.user_id in users for c in self._conns)

    def dnd(self, ep: Endpoint) -> bool:
        """HC-4: Do Not Disturb as the latest report from any open connection of the endpoint's user says; a connection
        that closes takes its report with it, so DND is cleared when the last one drops."""
        users = self._users(ep)
        reports = [c for c in self._conns if c.user_id in users and c.dnd_seq]
        return bool(reports) and max(reports, key=lambda c: c.dnd_seq).dnd

    # ---- the push side (3c): `push_only` presence and the push ring notification --------------------------------------------------

    def _push(self):
        """The push ring notification's manager, or None when there is none (the push entry is not set up). Never raises."""
        try:
            from .push import get_manager

            return get_manager(self.hass)
        except Exception:  # noqa: BLE001
            return None

    def push_reachable(self, ep: Endpoint) -> bool:
        """The push ring notification can reach this phone by notification: the push path is on and the phone has a live registration. A
        screen never does."""
        if ep.kind != KIND_PHONE or not ep.ha_user_id:
            return False
        manager = self._push()
        try:
            return manager is not None and manager.house_has_push(ep.ha_user_id)
        except Exception:  # noqa: BLE001 - a push bug must read as "not reachable", never break the roster
            _LOGGER.debug("portico house: the push state could not be read", exc_info=True)
            return False

    def away(self, ep: Endpoint) -> bool:
        """HC-3 + 15.5: no open subscription, but a notification can reach it (presence `push_only`)."""
        return not self.online(ep) and self.push_reachable(ep)

    def presence(self, endpoint_id: str) -> dict[str, Any] | None:
        ep = self._endpoint(endpoint_id)
        if ep is None:
            return None
        state = "online" if self.online(ep) else "push_only" if self.push_reachable(ep) else "offline"
        return {"state": state, "dnd": self.dnd(ep)}

    def _push_guard(self, what: str, action: Callable[[Any], Any]) -> None:
        """Run one push hook; a push bug must never end or change a call."""
        manager = self._push()
        if manager is None:
            return
        try:
            action(manager)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("portico house: the push ring notification failed (%s); the call is unaffected", what)

    def _push_ring(self, call: _Call, callees: list[Endpoint]) -> None:
        """Queue the `ct: house` push for the callees that are away at this moment."""
        away = {e.id: e.ha_user_id for e in callees if e.ha_user_id and self.away(e)}
        if not away:
            return
        from_ref = house_from_ref(call.caller)

        def ring(manager) -> None:
            call.pushed = set(manager.house_ring(call.call_id, from_ref, away, int(self._ring_s)))

        self._push_guard("ring", ring)

    def _push_answered(self, call: _Call, winner: str) -> None:
        if call.pushed:
            self._push_guard("answered", lambda m: m.house_answered(call.call_id, winner))

    def _push_left(self, call: _Call, endpoint_id: str, *, notify: bool) -> None:
        if endpoint_id in call.pushed:
            self._push_guard("left", lambda m: m.house_left(call.call_id, endpoint_id, notify=notify))

    def _push_ended(self, call: _Call) -> None:
        if call.pushed:
            self._push_guard("ended", lambda m: m.house_ended(call.call_id))

    def _rung_by(self, ep: Endpoint) -> Callable[[str | None], bool]:
        """Does a door ring reach this endpoint? A phone only for a door it is enrolled for (`doors` option, R4-04); a
        screen for every door. The phone half of a panel is rung by the doors its screen wakes for (`wake`, R6): a door the app
        drops must not end or refuse a house call; a panel with no usable screen is rung by every door, as the app rings then."""
        if ep.kind != KIND_PHONE:
            return lambda _door: True
        if ep.ha_user_id and is_panel_user(self.hass, ep.ha_user_id):
            wake = panel_wake_doors(self.hass, ep.id)
            if wake is None:
                return lambda _door: True
            return lambda door: door is None or door_in_refs(wake, door)
        entry = phone_entry_for_user(self.hass, ep.ha_user_id) if ep.ha_user_id else None
        phone = {"doors": entry.options.get("doors")} if entry is not None else {}
        return lambda door: door is None or may_open(phone, door)

    def busy_state(self, ep: Endpoint) -> str:
        return self.ledger.endpoint_state(ep.id, self._users(ep), self._rung_by(ep))

    # ---- the CallEndpointRegistry seam (call_endpoints.py) ------------------------------------------------------

    def endpoints(self, *, caller_user_id: str) -> list[TargetEndpoint]:
        """What the builder may offer (CB-10): callable endpoints only (a phone with receive off is not listed), never the
        caller itself."""
        me = self._endpoint_for_user(caller_user_id)
        out = [TargetEndpoint(e.kind, e.id, e.name) for e in self._endpoints()
               if self.receives(e) and (me is None or e.id != me.id)]
        if me is None or me.kind == KIND_PHONE:                  # the gate (3f): only a phone can talk to it, only with `CanDial`
            out.extend(TargetEndpoint(d.kind, d.id, d.name) for d in dialable_doors(self.hass))
        return out

    def target_state(self, target_type: str, target_id: str) -> TargetState | None:
        if target_type not in (KIND_PHONE, KIND_DASHBOARD):
            return None                  # a door target is the gate's: the content store places it through Talk's path (3f)
        ep = self._endpoint(target_id)
        if ep is None or ep.kind != target_type:
            return None
        reason = self._unreachable(ep)
        if reason is None and not self.online(ep):
            return TargetState(ep.name, True, REASON_PUSH_ONLY)      # callable, and it will ring by notification (15.5)
        return TargetState(ep.name, reason is None, reason)

    def _unreachable(self, ep: Endpoint) -> str | None:
        """Why a target cannot be rung now, in the order a caller should be told, or None when it can."""
        if not self.receives(ep):
            return REASON_RECEIVE_OFF
        if not self.online(ep) and not self.push_reachable(ep):
            return REASON_OFFLINE
        if self.dnd(ep):
            return REASON_DND
        if self.busy_state(ep) != IDLE:
            return REASON_BUSY
        return None

    async def place(self, *, caller_user_id: str, caller_phone: str, target_type: str, target_id: str,
                    caller_endpoint: str | None = None, acting_user_id: str | None = None) -> PlaceResult:
        return self.place_call(caller_user_id=caller_user_id, target_type=target_type, target_id=target_id,
                               caller_endpoint=caller_endpoint, acting_user_id=acting_user_id)

    # ---- placing -----------------------------------------------------------------------------------------------

    @staticmethod
    def _refuse(code: str, message: str) -> PlaceResult:
        return PlaceResult(ok=False, code=code, message=message)

    def place_call(self, *, caller_user_id: str, target_type: str, target_id: str,
                   caller_endpoint: str | None = None, acting_user_id: str | None = None) -> PlaceResult:
        """Place a call. Synchronous end to end (no await between the checks and the claim), so two placements racing for
        one endpoint are decided one after the other. Never raises for a refusal."""
        # R3, in this order: the caller is resolved through the RAW registry (the app names the screen's slug for `content/run`, and
        # `_endpoint` would refuse it), its kind is kept for HC-9, and an app screen is then replaced by its phone for every
        # later step (busy, the ledger, `_expected_everyone`, the events).
        caller = self._raw_endpoint(caller_endpoint) if caller_endpoint else self._endpoint_for_user(caller_user_id)
        original_kind = caller.kind if caller is not None else None
        if caller is not None and is_app_screen(self.hass, caller.id):
            caller = phone_of(self.hass, caller.id)
        if caller is None or (caller.ha_user_id and caller.ha_user_id != caller_user_id):
            return self._refuse(CODE_NOT_CALLABLE, "that endpoint cannot be called")
        if acting_user_id is not None and acting_user_id != caller_user_id:
            # an administrator running a phone's button: a house call rings a person, so it is placed as oneself only
            return self._refuse(CODE_PLACE_FORBIDDEN, "a house call is placed by the endpoint itself")
        if target_type == GROUP_TYPE:
            if target_id not in GROUP_IDS:
                return self._refuse(CODE_NOT_CALLABLE, "that endpoint cannot be called")
            if original_kind != KIND_DASHBOARD and not (caller.ha_user_id and is_panel_user(self.hass, caller.ha_user_id)):
                return self._refuse(CODE_FORBIDDEN, "only a screen or a panel may call a group")      # HC-9: a panel is a screen here
            return self._place_everyone(caller, target_id)
        if target_type not in (KIND_PHONE, KIND_DASHBOARD):
            return self._refuse(CODE_NOT_AVAILABLE, "this Home Assistant cannot place that call yet")
        target = self._endpoint(target_id)
        if target is None or target.kind != target_type or target.id == caller.id or not self.receives(target):
            return self._refuse(CODE_NOT_CALLABLE, "that endpoint cannot be called")      # one code for every cause
        busy = self._caller_refusal(caller)
        if busy is not None:
            return busy
        if not self.online(target) and not self.push_reachable(target):
            return self._refuse(CODE_TARGET_OFFLINE, "that endpoint is not reachable")
        if self.dnd(target):
            return self._refuse(CODE_TARGET_DND, "that endpoint has Do Not Disturb on")
        if self.busy_state(target) != IDLE:
            return self._refuse(CODE_BUSY, "that endpoint is on a call")
        call = self._open(caller, target.id, group=False, expected={target.id})
        return call if isinstance(call, PlaceResult) else PlaceResult(ok=True, call_id=call.call_id, media=None)

    def _caller_refusal(self, caller: Endpoint) -> PlaceResult | None:
        """The caller may place only when it is itself idle (rule 0: a door outranks a house call, tap included)."""
        if self.busy_state(caller) != IDLE:
            return self._refuse(CODE_BUSY, "the caller is on a call or a door is ringing")
        if not self.ledger.house_slots_free():
            return self._refuse(CODE_BUSY, "too many house calls at once")
        return None

    def _expected_everyone(self, caller: Endpoint | None, only: str = GROUP_EVERYONE) -> tuple[list[Endpoint], int]:
        """15.14: every phone with receive on that is not the caller, online, not in DND, and idle (not in a door call, live
        or held, not ringing for one, not in a house call). Returns (the set, how many receiving phones were skipped).
        A `push_only` phone (receive on, no open subscription, a live push registration) is rung by notification (3c).
        `only` narrows the one set (group-call design G4): `phones` keeps the entries that are not panels
        (`is_panel_user` is false), `panels` the entries that are. `caller` None excludes nobody (a state read
        with no viewer)."""
        rung, skipped = [], 0
        own_users = {u for u in self._users(caller)} if caller is not None else set()   # R4: every endpoint of the caller's user
        for ep in self._endpoints():
            if ep.kind != KIND_PHONE or (caller is not None and ep.id == caller.id) or not self.receives(ep):
                continue
            if ep.ha_user_id and ep.ha_user_id in own_users:
                continue
            if only != GROUP_EVERYONE:
                is_panel = bool(ep.ha_user_id) and is_panel_user(self.hass, ep.ha_user_id)
                if is_panel != (only == GROUP_PANELS):
                    continue
            if (self.online(ep) or self.push_reachable(ep)) and not self.dnd(ep) and self.busy_state(ep) == IDLE:
                rung.append(ep)
            else:
                skipped += 1
        return rung, skipped

    @staticmethod
    def _no_one(group: str) -> tuple[str, str]:
        """(reason, words) for a group nobody in can receive now. Never reported as "declined" (DEV-424)."""
        if group == GROUP_PANELS:
            return REASON_NO_PANELS, "no other panel can receive a call right now"
        return (REASON_NO_PHONES if group == GROUP_PHONES else REASON_NO_RECEIVERS), \
            "no phone is set to receive house calls right now"

    def _place_everyone(self, caller: Endpoint, group: str = GROUP_EVERYONE) -> PlaceResult:
        busy = self._caller_refusal(caller)
        if busy is not None:
            return busy
        rung, _skipped = self._expected_everyone(caller, group)
        if not rung:
            # An empty set is refused here and NEVER reported as "everyone declined" (the old design's DEV-424 bug).
            return self._refuse(CODE_NOT_CALLABLE, self._no_one(group)[1])
        call = self._open(caller, group, group=True, expected={e.id for e in rung})
        return call if isinstance(call, PlaceResult) else PlaceResult(ok=True, call_id=call.call_id, media=None)

    def _open(self, caller: Endpoint, target: str, *, group: bool, expected: set[str]) -> _Call | PlaceResult:
        call_id = self._id_factory()
        clash = self.ledger.open_house(call_id, {caller.id, *expected})
        if clash is not None:
            return self._refuse(CODE_BUSY, "an endpoint is already on a call")
        call = _Call(call_id=call_id, caller=caller.id, target=target, group=group, expected=set(expected),
                     ringing=set(expected))
        for endpoint_id in (caller.id, *expected):
            member = self._raw_endpoint(endpoint_id)
            call.users[endpoint_id] = member.ha_user_id if member is not None else None
        self._calls[call_id] = call
        call.ring_timer = self._later(self._ring_s, self._ring_timeout, call_id)
        self._emit(call, caller.id, STATE_RINGING, to=target)
        for endpoint_id in sorted(expected):
            self._emit(call, endpoint_id, STATE_RINGING, to=endpoint_id)
        self._call_moment(call, STATE_RINGING)
        self._push_ring(call, [e for e in (self._endpoint(i) for i in sorted(expected)) if e is not None])
        return call

    # ---- answering, declining, hanging up ----------------------------------------------------------------------

    def _participant(self, user_id: str | None, call: _Call) -> str | None:
        """The endpoint of this call that `user_id` is, or None. A user that is not in the call learns nothing."""
        for endpoint_id in (call.caller, *sorted(call.expected)):
            ep = self._endpoint(endpoint_id)
            if ep is not None and ep.ha_user_id and ep.ha_user_id == user_id:
                return endpoint_id
        return None

    def answer(self, call_id: str, user_id: str | None) -> tuple[dict[str, Any] | None, str | None]:
        """First answer wins. Returns (the result, None) or (None, the error code). Atomic: no await."""
        call = self._calls.get(call_id)
        if call is None or call.state == STATE_ENDED:
            return None, CODE_UNKNOWN_CALL
        endpoint_id = self._participant(user_id, call)
        if endpoint_id is None or endpoint_id == call.caller:
            return None, CODE_UNKNOWN_CALL                       # the caller cannot answer its own call
        if call.state != STATE_RINGING:
            return None, CODE_ALREADY_ANSWERED if endpoint_id in call.expected else CODE_UNKNOWN_CALL
        if endpoint_id not in call.ringing:
            return None, CODE_UNKNOWN_CALL                       # declined, or displaced by a door
        ep = self._endpoint(endpoint_id)
        if ep is None or self.busy_state(ep) not in (HOUSE_RINGING,):
            return None, CODE_BUSY
        call.state = STATE_ANSWERED
        call.answered_by = endpoint_id
        self.ledger.set_house_state(call_id, STATE_ANSWERED)
        if call.ring_timer is not None:
            call.ring_timer()
            call.ring_timer = None
        losers = sorted(call.ringing - {endpoint_id})
        call.ringing = {endpoint_id}
        self._push_answered(call, endpoint_id)
        for other in losers:                                      # only endpoints that were ringing hear about it
            self.ledger.leave_house(call_id, other)
            self._emit(call, other, STATE_ENDED, reason=REASON_ANSWERED_ELSEWHERE, to=other)
        self._emit(call, call.caller, STATE_ANSWERED, to=endpoint_id)
        self._emit(call, endpoint_id, STATE_ANSWERED, to=endpoint_id)
        self._call_moment(call, STATE_ANSWERED)
        call.media_timer = self._later(self._media_s, self._media_timeout, call_id)
        # `media` stays None: each end declares its leg with `portico/house/media_open` (house_bridge.py), which answers
        # `not_supported` unless a provider runs the bridge, so without one this is the 3a call (error at the deadline).
        return {"call_id": call_id, "media": None}, None

    def mark_active(self, call_id: str) -> bool:
        """The first media frame from the callee reached the bridge (3b's `house_leg_state first_frame`)."""
        call = self._calls.get(call_id)
        if call is None or call.state != STATE_ANSWERED:
            return False
        call.state = STATE_ACTIVE
        self.ledger.set_house_state(call_id, STATE_ACTIVE)
        if call.media_timer is not None:
            call.media_timer()
            call.media_timer = None
        self._emit(call, call.caller, STATE_ACTIVE, to=call.answered_by)
        if call.answered_by:
            self._emit(call, call.answered_by, STATE_ACTIVE, to=call.answered_by)
        self._call_moment(call, STATE_ACTIVE)
        return True

    def decline(self, call_id: str, user_id: str | None) -> tuple[bool, str | None]:
        call = self._calls.get(call_id)
        if call is None or call.state == STATE_ENDED:
            return False, CODE_UNKNOWN_CALL
        endpoint_id = self._participant(user_id, call)
        if endpoint_id is None or endpoint_id == call.caller or call.state != STATE_RINGING \
                or endpoint_id not in call.ringing:
            return False, CODE_UNKNOWN_CALL
        self._decline(call, endpoint_id)
        return True, None

    def _decline(self, call: _Call, endpoint_id: str) -> None:
        call.declined += 1
        self._push_left(call, endpoint_id, notify=False)          # it declined: it needs no dismiss
        if not call.group:
            self._end(call, REASON_DECLINED)                      # terminal on a 1:1
            return
        call.ringing.discard(endpoint_id)                         # local on a fan-out
        self.ledger.leave_house(call.call_id, endpoint_id)
        self._emit(call, endpoint_id, STATE_ENDED, reason=REASON_DECLINED, to=endpoint_id)
        self._maybe_exhausted(call)

    def _maybe_exhausted(self, call: _Call) -> None:
        """Nobody is left ringing on a fan-out: the call ends. `declined` only when somebody really declined; a set emptied
        by connections that dropped is `error`, one emptied by doors alone is `busy`. A set that was never non-empty never
        gets here (placement refused it), so "everyone declined" cannot come from an empty set."""
        if call.state == STATE_RINGING and not call.ringing:
            self._end(call, REASON_DECLINED if call.declined else REASON_ERROR if call.lost else REASON_BUSY)

    def hangup(self, call_id: str, user_id: str | None) -> tuple[bool, str | None]:
        """Either side. The caller cancels while ringing and ends while talking; a ringing callee's hang-up is a decline."""
        call = self._calls.get(call_id)
        if call is None or call.state == STATE_ENDED:
            return False, CODE_UNKNOWN_CALL
        endpoint_id = self._participant(user_id, call)
        if endpoint_id is None:
            return False, CODE_UNKNOWN_CALL
        if call.state == STATE_RINGING:
            if endpoint_id == call.caller:
                self._end(call, REASON_CANCELLED)
            elif endpoint_id in call.ringing:
                self._decline(call, endpoint_id)
            else:
                return False, CODE_UNKNOWN_CALL
            return True, None
        if endpoint_id != call.caller and endpoint_id != call.answered_by:
            return False, CODE_UNKNOWN_CALL                       # a loser of the race has nothing to hang up
        self._end(call, REASON_HANGUP)
        return True, None

    def _ring_timeout(self, call_id: str) -> None:
        call = self._calls.get(call_id)
        if call is not None and call.state == STATE_RINGING:
            call.ring_timer = None
            self._end(call, REASON_NO_ANSWER)

    def _media_timeout(self, call_id: str) -> None:
        """No callee frame within the deadline: a person is not released back to ringing (design 4.1); the call ends."""
        call = self._calls.get(call_id)
        if call is not None and call.state == STATE_ANSWERED:
            call.media_timer = None
            self._end(call, REASON_ERROR)

    def _end(self, call: _Call, reason: str) -> None:
        """Terminal: late events for an ended call are dropped (`_calls` forgets it; the id is remembered as ended)."""
        if call.state == STATE_ENDED:
            return
        call.state = STATE_ENDED
        call.reason = reason
        for timer in (call.ring_timer, call.media_timer):
            if timer is not None:
                timer()
        call.ring_timer = call.media_timer = None
        members = {call.caller, *call.ringing}
        if call.answered_by:
            members.add(call.answered_by)
        self.ledger.close_house(call.call_id)
        self._push_ended(call)
        self._calls.pop(call.call_id, None)
        self._ended[call.call_id] = None
        while len(self._ended) > _ENDED_MEMORY:
            self._ended.popitem(last=False)
        for endpoint_id in sorted(members):
            self._emit(call, endpoint_id, STATE_ENDED, reason=reason,
                       to=call.target if endpoint_id == call.caller else endpoint_id)
        self._call_moment(call, STATE_ENDED, reason=reason)
        try:
            self.bridge.call_ended(call.call_id, reason)     # close the hub's bridge, tell the media commands
        except Exception:  # noqa: BLE001 - the call model is done either way
            _LOGGER.exception("portico house: the bridge could not be closed for %s", call.call_id)

    # ---- doors outrank house calls (design 7, rule 0) ------------------------------------------------------------

    def on_door_transition(self, transition: CallTransition) -> None:
        """Read only: the door side of the ledger, and the house calls a ringing door displaces. Never raises into the
        provider registry's fan-out."""
        try:
            self._on_door_transition(transition)
        except Exception:  # noqa: BLE001 - a bug here must not touch the door path
            _LOGGER.exception("portico house: a door transition could not be applied; the door path is unaffected")

    def _on_door_transition(self, t: CallTransition) -> None:
        if t.state == "unavailable":
            self.ledger.door_gone(t.door_id)
            return
        call_id = t.call_id
        if not call_id or call_id.startswith(HOUSE_PREFIX):
            return
        if t.state == "ringing":
            if t.prev_state in ("answered", "active"):
                self.ledger.release_claims(call_id)               # a released claim: the door rings again
            self.ledger.door_ringing(call_id, t.door_id)
            self._door_rang(t.door_id)
        elif t.state in ("answered", "active"):
            self.ledger.door_resolved(call_id)
        elif t.state == "ended":
            self.ledger.door_ended(call_id)

    def _door_rang(self, door_id: str | None) -> None:
        """A door rings: every ringing house call that touches an endpoint the door reaches gives way. A callee the door
        reaches leaves the ring (a 1:1 caller is told `busy`; a fan-out says nothing unless nobody is left); a caller the
        door reaches cancels its outgoing ring. A call that is already talking is left alone: the door shows over it and
        answering the door ends it (`door_claim_granted`)."""
        for call in list(self._calls.values()):
            if call.state != STATE_RINGING:
                continue
            for endpoint_id in sorted(call.ringing):
                ep = self._endpoint(endpoint_id)
                if ep is None or not self._rung_by(ep)(door_id):
                    continue
                call.displaced += 1
                if not call.group:
                    self._end(call, REASON_BUSY)
                    break
                call.ringing.discard(endpoint_id)
                self.ledger.leave_house(call.call_id, endpoint_id)
                self._push_left(call, endpoint_id, notify=True)
                self._emit(call, endpoint_id, STATE_ENDED, reason=REASON_BUSY, to=endpoint_id)
            if call.state != STATE_RINGING:
                continue
            caller = self._endpoint(call.caller)
            if caller is not None and self._rung_by(caller)(door_id):
                self._end(call, REASON_CANCELLED)
            else:
                self._maybe_exhausted(call)

    def door_claim_granted(self, user_id: str, call_id: str, door_id: str | None = None) -> None:
        """The provider granted this user's answer of a door call: any house call the user is in ends for both ends
        (answering the door ends the house call, design 7)."""
        for call in list(self._calls.values()):
            endpoint_id = self._participant(user_id, call)
            if endpoint_id is None:
                continue
            if call.state in (STATE_ANSWERED, STATE_ACTIVE):
                self._end(call, REASON_HANGUP)
            elif endpoint_id == call.caller:
                self._end(call, REASON_CANCELLED)
            elif endpoint_id in call.ringing:
                call.displaced += 1
                if not call.group:
                    self._end(call, REASON_BUSY)
                else:
                    call.ringing.discard(endpoint_id)
                    self.ledger.leave_house(call.call_id, endpoint_id)
                    self._push_left(call, endpoint_id, notify=True)
                    self._emit(call, endpoint_id, STATE_ENDED, reason=REASON_BUSY, to=endpoint_id)
                    self._maybe_exhausted(call)

    def reserve_door_answer(self, user_id: str | None, call_id: str) -> Reservation | str | None:
        """Before `portico/answer` asks the provider: None when there is no user to count, a `Reservation`, or
        `too_many_calls`."""
        if not user_id or call_id.startswith(HOUSE_PREFIX):
            return None
        return self.ledger.reserve_door(user_id, call_id)

    def settle_door_answer(self, reservation: Reservation, granted: bool, door_id: str | None = None) -> None:
        if granted:
            self.ledger.confirm_door(reservation, door_id)
            self.door_claim_granted(reservation.user_id, reservation.call_id, door_id)
        else:
            self.ledger.release_door(reservation)

    # ---- connections, presence, events --------------------------------------------------------------------------

    def attach(self, connection: Any, msg_id: int) -> HouseHandle | None:
        """An open `portico/calls/subscribe`: the user is online (HC-3) and receives its endpoints' `house_call` events.
        None for a connection with no user. Presence changes tell every attached client to re-read its roster."""
        user = getattr(connection, "user", None)
        if user is None:
            return None
        conn = _Conn(connection=connection, msg_id=msg_id, user_id=user.id)
        self._conns.append(conn)
        self._roster_changed()
        return HouseHandle(self, conn)

    def detach(self, conn: _Conn) -> None:
        if conn not in self._conns:
            return
        self._conns.remove(conn)
        if not any(c.user_id == conn.user_id for c in self._conns):
            # R5: every endpoint of this user, not the one `_endpoint_for_user` finds (it finds none for a user with two), and
            # every endpoint a call recorded for it: a screen deleted or re-pointed while the call rang is still in that call
            lost = {e.id for e in get_endpoint_registry(self.hass).endpoints()
                    if e.kind in (KIND_PHONE, KIND_DASHBOARD) and e.ha_user_id == conn.user_id}
            for call in list(self._calls.values()):
                lost.update(i for i, user in call.users.items() if user == conn.user_id)
            for endpoint_id in sorted(lost):
                self._endpoint_lost(endpoint_id)
        self._roster_changed()

    def _endpoint_lost(self, endpoint_id: str) -> None:
        """The endpoint's last connection dropped: a call that is still ringing for it ends (design 4.1: no reconnect
        grace). A call that is talking is the media layer's: with a bridge available it ends here too (the bridge has
        no other way to know an end's socket is gone when its leg is a WebRTC one); without one it is left to the
        10 s audio deadline, as in 3a."""
        for call in list(self._calls.values()):
            if call.state in (STATE_ANSWERED, STATE_ACTIVE):
                if endpoint_id in (call.caller, call.answered_by) and self.bridge.supported():
                    self._end(call, REASON_ERROR)
                continue
            if call.state != STATE_RINGING:
                continue
            if endpoint_id == call.caller:
                self._end(call, REASON_CANCELLED)
            elif endpoint_id in call.ringing:
                if not call.group:
                    self._end(call, REASON_ERROR)
                else:
                    call.lost += 1
                    call.ringing.discard(endpoint_id)
                    self.ledger.leave_house(call.call_id, endpoint_id)
                    self._push_left(call, endpoint_id, notify=True)
                    self._emit(call, endpoint_id, STATE_ENDED, reason=REASON_ERROR, to=endpoint_id)
                    self._maybe_exhausted(call)

    def set_dnd(self, connection: Any, dnd: bool) -> bool:
        """`portico/house/presence {dnd}`: this connection's own report. False when the connection is not subscribed."""
        for conn in self._conns:
            if conn.connection is connection:
                self._dnd_serial += 1
                conn.dnd, conn.dnd_seq = bool(dnd), self._dnd_serial
                self._roster_changed()
                return True
        _LOGGER.info("portico house: presence report refused: connection %x is not subscribed to calls", id(connection) & 0xFFFFFF)
        return False

    def active_count(self) -> int:
        """How many house calls are in progress (ringing or talking) right now: what the House calls switch shows."""
        return len(self._calls)

    def reconcile(self) -> None:
        """A phone was removed or its `receive` switched off: any ringing call it is in ends, and the roster is re-read.
        (Removing a phone ends any call it is in, design 6.)"""
        for call in list(self._calls.values()):
            members = {call.caller, *call.ringing, *([call.answered_by] if call.answered_by else [])}
            for endpoint_id in members:
                ep = self._endpoint(endpoint_id)
                if ep is not None and self.receives(ep):
                    continue
                if endpoint_id == call.caller and ep is not None:
                    continue                                       # a caller needs no receive switch
                self._end(call, REASON_ERROR)
                break
        self._roster_changed()

    def _conns_of(self, endpoint_id: str) -> list[_Conn]:
        ep = self._endpoint(endpoint_id)
        if ep is None:
            return []
        users = self._users(ep)
        return [c for c in self._conns if c.user_id in users]

    def _event(self, call: _Call, state: str, *, reason: str | None, to: str | None, snapshot: bool = False) -> dict:
        event: dict[str, Any] = {"type": "house_call", "call_id": call.call_id, "state": state,
                                 "from": call.caller, "to": to}
        if reason is not None:
            event["reason"] = reason
        if snapshot:
            event["snapshot"] = True
        return event

    def _emit(self, call: _Call, endpoint_id: str, state: str, *, reason: str | None = None, to: str | None = None) -> None:
        event = self._event(call, state, reason=reason, to=to)
        for conn in self._conns_of(endpoint_id):
            try:
                conn.connection.send_event(conn.msg_id, event)
            except Exception:  # noqa: BLE001 - one dead connection must not stop the rest
                _LOGGER.debug("portico house: an event could not be sent", exc_info=True)

    def _call_moment(self, call: _Call, phase: str, *, reason: str | None = None) -> None:
        """DEV-811 P4: one CALL-LEVEL moment (place, answer, active, end) for the `portico_call` event. Never the per-endpoint
        `_emit`, which also ends single endpoints while the call goes on. Names only, never an endpoint id. Never raises."""
        try:
            from .call_events import get_call_events

            events = get_call_events(self.hass)
            if events is None:
                return

            def name(endpoint_id: str | None) -> str | None:
                ep = self._raw_endpoint(endpoint_id)
                return ep.name if ep is not None and ep.name else None

            events.house_moment(
                call.call_id, phase, from_name=name(call.caller),
                to_name=call.target if call.target in GROUP_IDS else name(call.target),
                answered_by=name(call.answered_by), reason=reason)
        except Exception:  # noqa: BLE001 - the call model is done either way
            _LOGGER.debug("portico house: the call event could not be fired", exc_info=True)

    def snapshots_for(self, conn: _Conn) -> list[dict]:
        """Every call that involves this connection's user, as it stands (`snapshot: true`, sent after the door snapshots).
        A callee that has left a call (declined, displaced by a door, lost) is not in it any more."""
        out: list[dict] = []
        for call in self._calls.values():
            for endpoint_id in (call.caller, *sorted(call.expected)):
                ep = self._endpoint(endpoint_id)
                if ep is None or ep.ha_user_id != conn.user_id:
                    continue
                if endpoint_id == call.caller:
                    to = call.answered_by or call.target
                elif endpoint_id in call.ringing or endpoint_id == call.answered_by:
                    to = endpoint_id
                else:
                    continue
                out.append(self._event(call, call.state, reason=None, to=to, snapshot=True))
        return out

    def roster_rev(self) -> str:
        """A hash of every endpoint's reachability: what `house_roster_changed {rev}` carries, so a client re-reads its
        roster only when something it shows changed."""
        rows = sorted((e.id, self._unreachable(e) or (REASON_PUSH_ONLY if self.away(e) else ""))
                      for e in self._endpoints())
        rows.extend(sorted((d.id, "gate") for d in dialable_doors(self.hass)))      # 3f: no row at all while no door reports `CanDial`
        return hashlib.sha256(json.dumps(rows).encode()).hexdigest()[:12]

    def roster_changed(self) -> None:
        """Public: tell every attached client whose roster rev moved (the gate's `CanDial` flipped, 3f)."""
        self._roster_changed()

    def _roster_changed(self) -> None:
        self._away_ids = self._away_set()
        rev = self.roster_rev()
        endpoint_users = {e.ha_user_id for e in self._endpoints() if e.ha_user_id}
        for conn in list(self._conns):
            if conn.user_id not in endpoint_users:
                continue                                           # a login that is no endpoint is told nothing
            if conn.last_rev == rev:
                continue
            first = conn.last_rev is None
            conn.last_rev = rev
            if first:
                continue                                           # a new subscription reads the roster itself
            try:
                conn.told_rev = rev
                conn.connection.send_event(conn.msg_id, {"type": "house_roster_changed", "rev": rev})
            except Exception:  # noqa: BLE001
                _LOGGER.debug("portico house: a roster event could not be sent", exc_info=True)

    # ---- the roster's Everyone key (15.14) ------------------------------------------------------------------------

    def group_state(self, group: str, callee: Endpoint | None) -> tuple[str | None, int] | None:
        """(reason or None, how many it would ring now) of a group as `callee` sees it, or None when `callee` is not a screen
        (HC-9, by kind) or the group is not one of GROUP_IDS. The one place the three groups' state is decided: the roster key,
        a Talk group element's tile and the press (`_place_everyone`) all read the same set. `busy` when the screen itself is on
        a call, else the group's own empty-set reason."""
        if callee is None or group not in GROUP_IDS:
            return None
        original_kind = callee.kind                       # HC-9 is read on the kind the caller was NAMED with (R3)
        if is_app_screen(self.hass, callee.id):
            callee = phone_of(self.hass, callee.id)       # busy and the expected set are the phone's (R3, R4)
            if callee is None:
                return None
        if original_kind != KIND_DASHBOARD and not (callee.ha_user_id and is_panel_user(self.hass, callee.ha_user_id)):
            return None                                   # a panel is a screen here: it owns its pages as a phone, calls as a screen
        live = self._endpoint(callee.id)
        rung, _skipped = self._expected_everyone(callee, group)
        reason: str | None = None
        if live is not None and self.busy_state(live) != IDLE:
            reason = REASON_BUSY
        elif not rung:
            reason = self._no_one(group)[0]
        return reason, len(rung)

    def group_target_state(self, group: str, viewer: Endpoint | None) -> TargetState:
        """The state of a Talk group element's `call` tile (`content._call_state`). Off with `not_available` for a viewer that
        is not a screen (a phone's page may not hold one, G5) and with `unknown_target` for a word that is not a group."""
        # the label is empty on purpose: the tile keeps the ELEMENT's own name ("Call all panels"; `content._call_state` falls
        # back to the action label), so a group has no fixed wording of its own (G6)
        if group not in GROUP_IDS:
            return TargetState("", False, REASON_UNKNOWN_TARGET)
        state = self.group_state(group, viewer)
        if state is None:
            return TargetState("", False, REASON_NOT_AVAILABLE)
        return TargetState("", state[0] is None, state[0])

    def group_entries(self, callee: Endpoint | None) -> list[dict[str, Any]]:
        """The roster entries a screen gets before its phones: "Everyone", on a dashboard endpoint or a panel's phone only (HC-9).
        `count` is how many phones it would ring now; the key is off with `no_receivers` when none would ("No phone is
        set to receive house calls") and with `busy` when the screen itself is on a call."""
        if callee is None:
            return []
        state = self.group_state(GROUP_EVERYONE, callee)
        if state is None:
            return []
        from .content_resolve import roster_action_id

        reason, count = state
        return [{"action_id": roster_action_id(GROUP_REF), "id": GROUP_EVERYONE, "kind": GROUP_TYPE,
                 "name": "Everyone", "available": reason is None, "reason": reason, "count": count}]


# ---- module access ------------------------------------------------------------------------------------------------

def get_house(hass: HomeAssistant) -> HouseCallRegistry | None:
    """The house-call registry, or None while house calls are off (the default): every caller does nothing then."""
    return hass.data.get(DOMAIN, {}).get(DATA_HOUSE)


def house_option_on(hass: HomeAssistant) -> bool:
    """House calls are always on (D-20261122-aafb). Kept for its callers; the old `house_calls` option is ignored."""
    return True


@callback
def async_setup_house(hass: HomeAssistant, *, enabled: bool | None = None, **kwargs: Any) -> HouseCallRegistry | None:
    """Domain setup: build and register the registry (house calls are always on; `enabled=False` lets a test leave everything as
    it was). Idempotent."""
    data = hass.data.setdefault(DOMAIN, {})
    if data.get(DATA_HOUSE) is not None:
        return data[DATA_HOUSE]
    if not (house_option_on(hass) if enabled is None else enabled):
        return None
    house = HouseCallRegistry(hass, **kwargs)
    data[DATA_HOUSE] = house
    house.start()
    set_call_registry(hass, house)
    return house


@callback
def async_stop_house(hass: HomeAssistant) -> None:
    """Take the registry down (every house call in progress ends `error`) and put the Null placement registry back. Used by
    the House calls switch (house_switch.py) and by tests."""
    from .call_endpoints import DATA_CALL_REGISTRY, NullCallEndpointRegistry

    data = hass.data.setdefault(DOMAIN, {})
    house = data.pop(DATA_HOUSE, None)
    if house is not None:
        house.stop()
        data[DATA_CALL_REGISTRY] = NullCallEndpointRegistry()
