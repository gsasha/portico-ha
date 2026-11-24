"""The provider registry: the seam between the websocket/content/push side and the doors (requirements PROV-01..07).

Since the fold of the former `portico_intercom` integration into `portico` there is ONE provider, the door side's
`door.provider_impl.TransportProvider`, registered directly by `async_setup` (`registry.register(...)`). The registry,
the `Provider` protocol and the conformance suites stay as an internal seam (the earlier discovery through HA's
integration-platform mechanism, the API-version check and its repair issue are gone: they existed for third-party
drivers in a separate integration).

The seam is a wire, not a shared module (plan 2.1, review R1-03): a provider returns plain dicts and
raises exceptions carrying `code` and `message`; this module turns the dicts into its own frozen
dataclasses (`Door.from_dict`, `CallTransition.from_dict`, `AnswerResult.from_dict`). The wire shapes are
documented in contract/provider_v1.md.
"""
from __future__ import annotations

import hashlib
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable

from homeassistant.core import Context, HomeAssistant

_LOGGER = logging.getLogger(__name__)

DOMAIN = "portico"
API_VERSION = 1
# `house_leg_state` states a provider may report (provider_v1.md section 4b).
HOUSE_LEG_STATES = ("attached", "first_frame", "lost", "sdp")
EVENT_INTERCOM_CALL = "portico_intercom_call"   # PROV-04; fired by the door side from its own tracker fan-out;
                                                # the registry's consumers only listen

DATA_PROVIDERS = "providers"                    # hass.data[DOMAIN][DATA_PROVIDERS] -> ProviderRegistry

CallState = str   # "idle" | "ringing" | "answered" | "active" | "ended" | "unavailable" (registry snapshot)

ANSWER_VERDICTS = frozenset({"answered", "taken_elsewhere", "refused", "not_available"})   # STATE-02

_MAX_REMEMBERED_CALLS = 256
OUTBOUND_STATES = frozenset({"dialing", "in_call", "ended"})   # the states of a call Portico placed (section 4a)


class ProviderError(Exception):
    """A provider's failure: the websocket error code and text. A provider's own exception type works
    equally well on the wire as long as it carries `code` and `message` attributes."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def error_code(err: BaseException) -> str | None:
    """The `code` of a provider's exception, or None for one that carries none."""
    code = getattr(err, "code", None)
    return code if isinstance(code, str) else None


def _bad(what: str, raw: Any, detail: str) -> ProviderError:
    return ProviderError("bad_provider_data", f"{what}: {detail} (got {type(raw).__name__})")


def _need(raw: Any, what: str, key: str, types: type | tuple[type, ...], *, optional: bool = False) -> Any:
    if not isinstance(raw, dict):
        raise _bad(what, raw, "expected a dict")
    if key not in raw:
        raise _bad(what, raw, f"missing key {key!r}")
    value = raw[key]
    if value is None and optional:
        return None
    wanted = types if isinstance(types, tuple) else (types,)
    # bool is an int subclass: a bool where a number is wanted is a mistake
    if not isinstance(value, wanted) or (isinstance(value, bool) and bool not in wanted):
        raise _bad(what, raw, f"key {key!r} has the wrong type {type(value).__name__}")
    return value


@dataclass(frozen=True, slots=True)
class Door:
    id: str                       # the driver's device_id (drivers.device_id_for), never an entry_id (PROV-03)
    provider: str                 # the provider's HA domain; endpoint id is f"{provider}:{id}" (ENDPOINT-02)
    name: str                     # the entry title
    driver: str                   # "dahua_sip" | "unifi_protect" | "gate_sip_server" | a third party's key
    capabilities: dict[str, Any]  # Go's Capabilities as returned by configure, verbatim
    camera_entity_id: str | None  # existing_camera_entity or the driver's own camera entity
    available: bool
    away_media: bool              # the door entry's `away_media` option [V away_media.py:43-44], published by the
                                  # provider so the UI part never reads a driver entry (rule 4.3-3; review R1-05)

    @property
    def can_dial(self) -> bool:   # Go's `CanDial` capability (wire name = the Go field): exactly True, else False.
        return self.capabilities.get("CanDial") is True   # absent today everywhere; nothing reads it yet

    @property
    def endpoint_id(self) -> str:      # split on the FIRST colon only: a device_id may be a URL (the live one
        return f"{self.provider}:{self.id}"   # is), an HA domain has no colon

    @property
    def door_ref(self) -> str:    # the push `door_ref` (WS-08): the id grammar on both sides is
                                  # ^[A-Za-z0-9._:-]{1,128}$ [V push/payload.py:36; app Contract.kt:43] and refuses a
                                  # URL, so the ref is derived: "d-" + sha256(endpoint_id)[:32]; a function of the
                                  # stable id, so it survives the re-add; `portico/doors` carries it; the app matches on it
        return "d-" + hashlib.sha256(self.endpoint_id.encode()).hexdigest()[:32]

    @classmethod
    def from_dict(cls, provider: str, raw: dict) -> "Door":
        """The registry's validation of a provider's door dict: a missing or mistyped key raises
        ProviderError("bad_provider_data", ...) which the registry logs and counts as "no door" for that
        entry; a provider that keeps sending bad dicts is not unregistered (its other doors may be fine).
        The dict's own "provider" key is not trusted: the registry knows whom it asked."""
        what = "door"
        return cls(
            id=_need(raw, what, "id", str),
            provider=provider,
            name=_need(raw, what, "name", str),
            driver=_need(raw, what, "driver", str),
            capabilities=dict(_need(raw, what, "capabilities", dict)),
            camera_entity_id=_need(raw, what, "camera_entity_id", str, optional=True),
            available=_need(raw, what, "available", bool),
            away_media=_need(raw, what, "away_media", bool),
        )


@dataclass(frozen=True, slots=True)
class CallSnapshot:
    door_id: str
    call_id: str | None
    state: CallState

    @classmethod
    def from_dict(cls, raw: dict) -> "CallSnapshot":
        what = "call snapshot"
        return cls(
            door_id=_need(raw, what, "door_id", str),
            call_id=_need(raw, what, "call_id", str, optional=True),
            state=_need(raw, what, "state", str),
        )


@dataclass(frozen=True, slots=True)
class CallTransition:               # mirrors call_state.Transition [V call_state.py:33-47] without entry_id
    door_id: str
    call_id: str | None
    prev_state: CallState
    state: CallState
    prev_call_id: str | None
    at_wall: float

    @classmethod
    def from_dict(cls, raw: dict) -> "CallTransition":
        """A bad transition is dropped and logged by the registry."""
        what = "transition"
        return cls(
            door_id=_need(raw, what, "door_id", str),
            call_id=_need(raw, what, "call_id", str, optional=True),
            prev_state=_need(raw, what, "prev_state", str),
            state=_need(raw, what, "state", str),
            prev_call_id=_need(raw, what, "prev_call_id", str, optional=True),
            at_wall=float(_need(raw, what, "at_wall", (int, float))),
        )


@dataclass(frozen=True, slots=True)
class OutboundTransition:
    """One change of a call Portico itself placed (provider_v1.md section 4a): `dialing`, `in_call` or `ended`. Never a
    visitor transition, never shown to a phone's `call_state_changed`; the talk relay is its only reader."""
    provider: str                 # the domain the registry heard it from
    door_id: str
    call_id: str
    prev_state: str               # dialing | in_call | idle
    state: str                    # dialing | in_call | ended
    at_wall: float

    @classmethod
    def from_dict(cls, provider: str, raw: dict) -> "OutboundTransition":
        what = "outbound transition"
        state = _need(raw, what, "state", str)
        if state not in OUTBOUND_STATES:
            raise _bad(what, raw, f"unknown state {state!r}")
        call_id = _need(raw, what, "call_id", str)
        if not call_id:
            raise _bad(what, raw, "empty call_id")
        return cls(
            provider=provider,
            door_id=_need(raw, what, "door_id", str),
            call_id=call_id,
            prev_state=_need(raw, what, "prev_state", str),
            state=state,
            at_wall=float(_need(raw, what, "at_wall", (int, float))),
        )


@dataclass(frozen=True, slots=True)
class AnswerResult:
    verdict: str                  # "answered" | "taken_elsewhere" | "refused" | "not_available" (STATE-02)
    mode: str | None = None       # "direct" | "relay" when answered
    detail: dict[str, Any] = field(default_factory=dict)   # the transport's result, passed through

    @classmethod
    def from_dict(cls, raw: dict) -> "AnswerResult":
        what = "answer result"
        verdict = _need(raw, what, "verdict", str)
        if verdict not in ANSWER_VERDICTS:
            raise _bad(what, raw, f"unknown verdict {verdict!r}")
        mode = raw.get("mode")
        if mode is not None and not isinstance(mode, str):
            raise _bad(what, raw, "key 'mode' has the wrong type")
        detail = raw.get("detail") or {}
        if not isinstance(detail, dict):
            raise _bad(what, raw, "key 'detail' has the wrong type")
        return cls(verdict=verdict, mode=mode, detail=dict(detail))


# What crosses the seam in each direction is a dict (contract/provider_v1.md): the listener a provider is
# handed receives the transition dict; the registry's own subscribers receive a CallTransition.
ProviderTransitionListener = Callable[[dict], None]
TransitionListener = Callable[[CallTransition], None]
OutboundListener = Callable[[OutboundTransition], None]
Unsubscribe = Callable[[], None]


@runtime_checkable
class Provider(Protocol):
    """Method names are all the registry checks; a provider needs no Portico import to implement this."""

    API_VERSION: int
    domain: str

    def doors(self) -> list[dict]: ...                                   # door dicts (2.1 wire)
    def call_state(self, door_id: str) -> dict | None: ...               # a snapshot dict, None for an unknown door
    def subscribe(self, listener: ProviderTransitionListener) -> Unsubscribe: ...   # call transitions (dicts)
    def subscribe_doors(self, listener: Callable[[], None]) -> Unsubscribe: ...
        # a door added, removed or changed availability while the provider stays registered: an entry
        # reload or unload in the driver part (the runbook's T8). The registry re-reads doors() and sends
        # an `unavailable` snapshot for each door that left (PROV-05; review R1-04).
        # Ordering (conformance suite has a case for each): on unload the provider DETACHES first, then
        # notifies, so doors() already lacks the door when the listener re-reads; on setup it ATTACHES
        # first, then notifies; listeners run on the event loop; a raising listener is logged and skipped;
        # after unregister() the registry ignores any late notification from that provider.
    def secrets_for(self, door_id: str, phone_id: str) -> dict[str, Any] | None: ...   # PROV-07; shared creds today

    async def answer(self, door_id: str, call_id: str, *, client_kind: str, claimant: str,
                     host: str = "", port: int = 0) -> dict: ...         # one arbitration (STATE-02); an AnswerResult dict
    async def dial(self, door_id: str, host: str, port: int) -> dict[str, Any]: ...
    async def hangup(self, call_id: str, reason: str) -> dict[str, Any]: ...
    async def open_relay(self, door_id: str, context: Context | None = None) -> dict[str, Any]: ...
    async def keepalive(self, call_id: str) -> dict[str, Any]: ...

    async def webrtc_offer(self, call_id: str, sdp: str) -> dict[str, Any]: ...
    async def webrtc_answer(self, call_id: str, sdp: str) -> dict[str, Any]: ...
    async def webrtc_ice(self, call_id: str, candidate: str) -> dict[str, Any]: ...

    async def media_open(self, call_id: str, codec: str, on_frame: Callable[[str], None],
                         on_closed: Callable[[str], None]) -> dict[str, Any]: ...
    def media_frame(self, call_id: str, data: str) -> None: ...
    async def media_close(self, call_id: str, reason: str) -> dict[str, Any]: ...


class _Registration:
    """One registered provider and everything the registry holds for it."""

    __slots__ = ("provider", "alive", "unsubs", "doors")

    def __init__(self, provider: Provider) -> None:
        self.provider = provider
        self.alive = True
        self.unsubs: list[Unsubscribe] = []
        self.doors: dict[str, Door] = {}      # the last good read of doors(), to see what left


class ProviderRegistry:
    """hass.data["portico"]["providers"]. Owns registration and the fan-out."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._regs: dict[str, _Registration] = {}
        self._listeners: list[TransitionListener] = []
        self._outbound_listeners: list[OutboundListener] = []
        self._house_listeners: list[Callable[[dict], None]] = []
        self._calls: OrderedDict[str, tuple[str, str | None]] = OrderedDict()   # call_id -> (domain, door_id), bounded
        self._arrival_listeners: list[Callable[[Door], None]] = []
        self._doors_hooks: list[Callable[[], None]] = []      # the UI part's own: told after any change of the door set

    # ---- registration ----------------------------------------------------------------------------

    def register(self, provider: Provider) -> Unsubscribe:
        """Subscribes to the provider's transitions and its door changes; returns the unregister. It does NOT fire the
        bus event: the door side does (PROV-04)."""
        domain = provider.domain
        if domain in self._regs:       # a second register of the same domain replaces the first
            self.unregister(domain)
        reg = _Registration(provider)
        self._regs[domain] = reg
        reg.unsubs.append(provider.subscribe(lambda raw: self._on_transition(reg, raw)))
        reg.unsubs.append(provider.subscribe_doors(lambda: self._on_doors_changed(reg)))
        # OPTIONAL (provider_v1.md section 4a): a provider that places calls may also report them. One without the
        # method is exactly what it was.
        subscribe_outbound = getattr(provider, "subscribe_outbound", None)
        if callable(subscribe_outbound):
            reg.unsubs.append(subscribe_outbound(lambda raw: self._on_outbound(reg, raw)))
        # OPTIONAL (provider_v1.md section 4b): a provider whose hub runs the house-call bridge reports its legs. One
        # without the method is exactly what it was; `API_VERSION` stays 1.
        subscribe_house = getattr(provider, "subscribe_house", None)
        if callable(subscribe_house):
            reg.unsubs.append(subscribe_house(lambda raw: self._on_house_leg(reg, raw)))
        reg.doors = self._read_doors(reg)
        for door in reg.doors.values():     # a provider arriving mid-subscription brings its doors with it
            self._announce_arrival(door)
        self._run_doors_hooks()

        def _unregister() -> None:
            if self._regs.get(domain) is reg:     # a stale handle must not remove its replacement
                self.unregister(domain)

        return _unregister

    def unregister(self, domain: str) -> None:
        """PROV-05: every subscriber of the UI part gets one `unavailable` snapshot per door that disappears."""
        reg = self._regs.pop(domain, None)
        if reg is None:
            return
        reg.alive = False       # a late notification from this provider is ignored from here on
        for unsub in reg.unsubs:
            try:
                unsub()
            except Exception:
                _LOGGER.exception("%s: unsubscribe failed", domain)
        reg.unsubs.clear()
        for call_id in [c for c, (d, _door) in self._calls.items() if d == domain]:
            del self._calls[call_id]
        doors, reg.doors = list(reg.doors.values()), {}
        for door in doors:
            self._emit_unavailable(door)
        self._run_doors_hooks()

    def provider(self, domain: str) -> Provider | None:
        reg = self._regs.get(domain)
        return reg.provider if reg else None

    def providers(self) -> list[Provider]:
        """Every registered provider (empty when none is: STRUCT-07)."""
        return [reg.provider for reg in self._regs.values()]

    # ---- doors -----------------------------------------------------------------------------------

    def _read_doors(self, reg: _Registration) -> dict[str, Door]:
        """A provider whose doors() raises counts as no doors; a bad dict counts as no door."""
        domain = reg.provider.domain
        try:
            raws = list(reg.provider.doors())
        except Exception:
            _LOGGER.exception("%s: doors() raised; counted as no doors", domain)
            return {}
        out: dict[str, Door] = {}
        for raw in raws:
            try:
                door = Door.from_dict(domain, raw)
            except ProviderError as err:
                _LOGGER.warning("%s: ignoring a bad door dict: %s", domain, err.message)
                continue
            out[door.id] = door
        return out

    def doors(self) -> list[Door]:
        out: list[Door] = []
        for reg in list(self._regs.values()):
            out.extend(self._read_doors(reg).values())
        return out

    def door(self, door_id: str) -> tuple[Provider, Door] | None:
        """door_id is namespaced iff the text before its first colon is a REGISTERED provider domain;
        otherwise it is a bare device_id (so `http://10.6.0.7:8091` is bare: `http` is no provider).
        A bare id that two providers both supply is refused as `ambiguous_door` (PROV-06). The rule is in
        contract/provider_v1.md."""
        prefix, sep, rest = door_id.partition(":")
        if sep and prefix in self._regs:
            reg = self._regs[prefix]
            door = self._read_doors(reg).get(rest)
            return (reg.provider, door) if door else None
        found: list[tuple[Provider, Door]] = []
        for reg in list(self._regs.values()):
            door = self._read_doors(reg).get(door_id)
            if door:
                found.append((reg.provider, door))
        if len(found) > 1:
            raise ProviderError("ambiguous_door", f"door id {door_id!r} is supplied by more than one provider; "
                                                  "name it as <provider>:<id>")
        return found[0] if found else None

    # ---- calls -----------------------------------------------------------------------------------

    def for_call(self, call_id: str) -> Provider | None:
        """The provider that owns this call_id, for hangup, keepalive, webrtc_* and media_* (PROC-02).
        Learned from transitions (a ring) AND from the call_id a provider's dial() or answer() result
        carries: a dialled call has no ring and no transition (a driver may emit nothing for an
        outbound call), so dial() must report the id."""
        return self.provider(self._calls.get(call_id, ("", None))[0])

    def door_of_call(self, call_id: str) -> str | None:
        """The id of the door a call belongs to, when the registry learned it (from a transition, or a dial() the UI
        part reported with its door): `portico/webrtc_offer` names the door it is for and is refused for another's call."""
        return self._calls.get(call_id, ("", None))[1]

    def note_call(self, domain: str, result: Any, door_id: str | None = None) -> None:
        """Record the `call_id` of a dial()/answer() result (a dict) for `domain`, and for `door_id` when the
        caller knows it (the call then leaves `for_call` when that door does, PROV-05). The UI part calls this
        with every such result; anything without a string call_id is ignored."""
        call_id = result.get("call_id") if isinstance(result, dict) else None
        if isinstance(call_id, str) and call_id:
            self._remember(call_id, domain, door_id)

    def _remember(self, call_id: str, domain: str, door_id: str | None = None) -> None:
        self._calls[call_id] = (domain, door_id)
        self._calls.move_to_end(call_id)
        while len(self._calls) > _MAX_REMEMBERED_CALLS:
            self._calls.popitem(last=False)

    # ---- fan-out ---------------------------------------------------------------------------------

    def subscribe(self, listener: TransitionListener) -> Unsubscribe:
        self._listeners.append(listener)

        def _unsub() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _unsub

    def _on_transition(self, reg: _Registration, raw: dict) -> None:
        if not reg.alive:
            return
        try:
            transition = CallTransition.from_dict(raw)
        except ProviderError as err:
            _LOGGER.warning("%s: dropping a bad transition: %s", reg.provider.domain, err.message)
            return
        for call_id in (transition.call_id, transition.prev_call_id):
            if call_id:
                self._remember(call_id, reg.provider.domain, transition.door_id)
        self._fan_out(transition)

    def subscribe_outbound(self, listener: OutboundListener) -> Unsubscribe:
        """`listener(OutboundTransition)` per change of a call a provider reports as one Portico placed. A separate
        list from `subscribe`: the visitors' listeners (the phones' stream, the push sender) never hear these."""
        self._outbound_listeners.append(listener)

        def _unsub() -> None:
            if listener in self._outbound_listeners:
                self._outbound_listeners.remove(listener)

        return _unsub

    def _on_outbound(self, reg: _Registration, raw: dict) -> None:
        if not reg.alive:
            return
        try:
            transition = OutboundTransition.from_dict(reg.provider.domain, raw)
        except ProviderError as err:
            _LOGGER.warning("%s: dropping a bad outbound transition: %s", reg.provider.domain, err.message)
            return
        for listener in list(self._outbound_listeners):
            try:
                listener(transition)
            except Exception:
                _LOGGER.exception("a Portico outbound listener raised; skipped")

    # ---- house calls: the optional bridge seam (provider_v1.md section 4b; M7 3b-S) --------------------------

    def house_provider(self) -> Any | None:
        """The registered provider that can bridge a house call right now (`house_bridge_supported()` true), or None.
        A provider without the method, one that answers False and one that raises all mean "no bridge"."""
        for reg in list(self._regs.values()):
            check = getattr(reg.provider, "house_bridge_supported", None)
            if not callable(check):
                continue
            try:
                if check() is True:
                    return reg.provider
            except Exception:   # noqa: BLE001 - "cannot tell" is "no"
                _LOGGER.debug("%s: house_bridge_supported raised", reg.provider.domain, exc_info=True)
        return None

    def subscribe_house(self, listener: Callable[[dict], None]) -> Unsubscribe:
        """`listener(leg dict)` per validated `house_leg_state` of any provider: `{call_id, leg, state, reason?, sdp?}`."""
        self._house_listeners.append(listener)

        def _unsub() -> None:
            if listener in self._house_listeners:
                self._house_listeners.remove(listener)

        return _unsub

    def _on_house_leg(self, reg: _Registration, raw: Any) -> None:
        if not reg.alive:
            return
        if (not isinstance(raw, dict) or not isinstance(raw.get("call_id"), str) or not raw["call_id"]
                or not isinstance(raw.get("leg"), str) or raw.get("state") not in HOUSE_LEG_STATES
                or not isinstance(raw.get("reason", ""), str) or not isinstance(raw.get("sdp", ""), str)):
            _LOGGER.warning("%s: dropping a bad house leg state", reg.provider.domain)
            return
        leg = {k: raw[k] for k in ("call_id", "leg", "state", "reason", "sdp") if k in raw}
        for listener in list(self._house_listeners):
            try:
                listener(dict(leg))
            except Exception:
                _LOGGER.exception("a Portico house-leg listener raised; skipped")

    def _on_doors_changed(self, reg: _Registration) -> None:
        if not reg.alive:
            return
        before = reg.doors
        reg.doors = self._read_doors(reg)
        domain = reg.provider.domain
        for door_id, door in before.items():
            if door_id not in reg.doors:
                self._forget_door_calls(domain, door_id)
                self._emit_unavailable(door)
        for door_id, door in reg.doors.items():
            if door_id not in before:
                self._announce_arrival(door)
        self._run_doors_hooks()

    def add_doors_hook(self, hook: Callable[[], None]) -> Unsubscribe:
        """`hook()` after the door set of any provider changed (arrived, left, or a provider notified `subscribe_doors`):
        the UI part's own seam (M7 3f: has the set of doors that can dial changed?). Not a transition listener and not an
        arrival listener, so the subscriptions the push sender and the websocket keep are what they were."""
        self._doors_hooks.append(hook)

        def _unsub() -> None:
            if hook in self._doors_hooks:
                self._doors_hooks.remove(hook)

        return _unsub

    def _run_doors_hooks(self) -> None:
        for hook in list(self._doors_hooks):
            try:
                hook()
            except Exception:
                _LOGGER.exception("a Portico door-set hook raised; skipped")

    def _forget_door_calls(self, domain: str, door_id: str) -> None:
        """A door that left takes its calls out of `for_call`: a hangup for one is `unknown_call` (PROV-05)."""
        for call_id in [c for c, v in self._calls.items() if v == (domain, door_id)]:
            del self._calls[call_id]

    def subscribe_arrivals(self, listener: Callable[[Door], None]) -> Unsubscribe:
        """`listener(door)` once for each door that appears while its provider stays registered (an entry set
        up after a subscription began, PROV-05's other half), and for each door of a provider that registers."""
        self._arrival_listeners.append(listener)

        def _unsub() -> None:
            if listener in self._arrival_listeners:
                self._arrival_listeners.remove(listener)

        return _unsub

    def _announce_arrival(self, door: Door) -> None:
        for listener in list(self._arrival_listeners):
            try:
                listener(door)
            except Exception:
                _LOGGER.exception("a Portico door-arrival listener raised; skipped")

    def _emit_unavailable(self, door: Door) -> None:
        self._fan_out(CallTransition(door_id=door.id, call_id=None, prev_state="idle",
                                     state="unavailable", prev_call_id=None, at_wall=time.time()))

    def _fan_out(self, transition: CallTransition) -> None:
        for listener in list(self._listeners):
            try:
                listener(transition)
            except Exception:
                _LOGGER.exception("a Portico transition listener raised; skipped")
