"""A scriptable provider for tests: no media, no network, records every call.

Like FakeCallEndpointRegistry (fake_call_registry.py) and FakeGoTransport (test_observer.py) it is the
stand-in a test drives instead of the real thing. It speaks the provider wire (contract/provider_v1.md):
plain dicts out, exceptions with `code` and `message`. It imports nothing from Portico, so a driver
part's tests can copy this file unchanged (boundary rule 4.3-1).

    fake = FakeProvider()                      # domain "fake"
    fake.add_door("gate-1")
    fake.ring("gate-1", "call-1")              # a transition to `ringing`
    await fake.answer("gate-1", "call-1", client_kind="phone", claimant="u1:c1")
    fake.calls                                 # [("answer", ("gate-1", "call-1"), {...})]
    fake.raise_on["open_relay"] = Boom()       # the next such call raises (every time until removed)
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable

_LOGGER = logging.getLogger(__name__)

API_VERSION = 1


class FakeProvider:
    API_VERSION = API_VERSION

    def __init__(self, domain: str = "fake", *, api_version: int | None = None) -> None:
        self.domain = domain
        if api_version is not None:
            self.API_VERSION = api_version        # instance attribute: a test registers API_VERSION + 1
        self._doors: dict[str, dict] = {}
        self._states: dict[str, dict] = {}        # door_id -> {"call_id", "state"}
        self._owner: dict[str, str] = {}          # call_id -> door_id
        self._claims: dict[str, str] = {}         # call_id -> claimant that won the answer
        self._listeners: list[Callable[[dict], None]] = []
        self._door_listeners: list[Callable[[], None]] = []
        self._outbound_listeners: list[Callable[[dict], None]] = []
        self.calls: list[tuple[str, tuple, dict]] = []
        self.open_contexts: list[Any] = []
        self.raise_on: dict[str, BaseException] = {}   # method name -> exception to raise
        self.secrets: dict[tuple[str, str], dict[str, Any]] = {}
        self.refuse_answers = False                # answer() returns "refused"
        self.answer_mode = "direct"                # the mode of a granted answer ("relay" for the away-media tests)
        self.doors_raise: BaseException | None = None
        self.media: dict[str, dict] = {}           # call_id -> {"on_frame", "on_closed"} of the open media legs

    # ---- scripting ---------------------------------------------------------------------------------

    def add_door(self, door_id: str, *, name: str | None = None, driver: str = "fake",
                 capabilities: dict[str, Any] | None = None, camera_entity_id: str | None = None,
                 available: bool = True, away_media: bool = False) -> None:
        """Attach first, then notify."""
        self._doors[door_id] = self._door_dict(door_id, self.domain, name=name, driver=driver,
                                               capabilities=capabilities, camera_entity_id=camera_entity_id,
                                               available=available, away_media=away_media)
        self._states.setdefault(door_id, {"call_id": None, "state": "idle"})
        self._notify_doors()

    @staticmethod
    def _door_dict(door_id: str, provider: str = "fake", *, name: str | None = None, driver: str = "fake",
                   capabilities: dict[str, Any] | None = None, camera_entity_id: str | None = None,
                   available: bool = True, away_media: bool = False) -> dict:
        return {
            "id": door_id, "provider": provider, "name": name or door_id, "driver": driver,
            "capabilities": dict(capabilities or {}), "camera_entity_id": camera_entity_id,
            "available": available, "away_media": away_media,
        }

    def remove_door(self, door_id: str) -> None:
        """Detach first, then notify."""
        self._doors.pop(door_id, None)
        self._states.pop(door_id, None)
        self._notify_doors()

    def set_available(self, door_id: str, available: bool) -> None:
        self._doors[door_id]["available"] = available
        self._notify_doors()

    def unload(self) -> None:
        """The whole provider's integration unloading: every door detaches, one notification."""
        self._doors.clear()
        self._states.clear()
        self._notify_doors()

    def ring(self, door_id: str, call_id: str) -> None:
        self._owner[call_id] = door_id
        self.state(call_id, "ringing")

    def state(self, call_id: str, state: str) -> None:
        """Move the door that owns `call_id` to `state` and emit the transition."""
        door_id = self._owner[call_id]
        cur = self._states[door_id]
        prev_state, prev_call_id = cur["state"], cur["call_id"]
        cur["call_id"] = None if state in ("idle", "ended") else call_id
        cur["state"] = state
        self._emit({"door_id": door_id, "call_id": call_id, "prev_state": prev_state, "state": state,
                    "prev_call_id": prev_call_id, "at_wall": time.time()})
        if state == "ended":
            cur["state"] = "idle"    # as the real tracker: a snapshot never carries `ended` (provider_v1.md section 4)

    def outbound(self, door_id: str, call_id: str, state: str, prev_state: str = "idle") -> None:
        """A change of a call Portico placed (provider_v1.md section 4a): `dialing | in_call | ended`."""
        raw = {"door_id": door_id, "call_id": call_id, "prev_state": prev_state, "state": state, "at_wall": time.time()}
        for listener in list(self._outbound_listeners):
            try:
                listener(dict(raw))
            except Exception:
                _LOGGER.exception("fake provider: an outbound listener raised")

    def _emit(self, transition: dict) -> None:
        for listener in list(self._listeners):
            try:
                listener(dict(transition))
            except Exception:               # a raising listener is logged and skipped (protocol rule)
                _LOGGER.exception("fake provider: a transition listener raised")

    def _notify_doors(self) -> None:
        for listener in list(self._door_listeners):
            try:
                listener()
            except Exception:
                _LOGGER.exception("fake provider: a door listener raised")

    def _record(self, method: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((method, args, kwargs))
        if method in self.raise_on:
            raise self.raise_on[method]

    def calls_to(self, method: str) -> list[tuple[tuple, dict]]:
        return [(a, k) for m, a, k in self.calls if m == method]

    # ---- the protocol (contract/provider_v1.md) ----------------------------------------------------------

    def doors(self) -> list[dict]:
        if self.doors_raise is not None:
            raise self.doors_raise
        return [dict(d) for d in self._doors.values()]

    def call_state(self, door_id: str) -> dict | None:
        cur = self._states.get(door_id)
        return None if cur is None else {"door_id": door_id, **cur}

    def subscribe(self, listener: Callable[[dict], None]) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._discard(self._listeners, listener)

    def subscribe_outbound(self, listener: Callable[[dict], None]) -> Callable[[], None]:
        self._outbound_listeners.append(listener)
        return lambda: self._discard(self._outbound_listeners, listener)

    def subscribe_doors(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._door_listeners.append(listener)
        return lambda: self._discard(self._door_listeners, listener)

    @staticmethod
    def _discard(items: list, item: Any) -> None:
        if item in items:
            items.remove(item)

    def secrets_for(self, door_id: str, phone_id: str) -> dict[str, Any] | None:
        return self.secrets.get((door_id, phone_id))

    async def answer(self, door_id: str, call_id: str, *, client_kind: str, claimant: str,
                     host: str = "", port: int = 0) -> dict:
        self._record("answer", door_id, call_id, client_kind=client_kind, claimant=claimant, host=host, port=port)
        if self._owner.get(call_id) != door_id or self._states.get(door_id, {}).get("call_id") != call_id:
            return {"verdict": "not_available", "mode": None, "detail": {}}
        if self.refuse_answers:
            return {"verdict": "refused", "mode": None, "detail": {}}
        if call_id in self._claims:
            return {"verdict": "taken_elsewhere", "mode": None, "detail": {}}
        self._claims[call_id] = claimant
        self.state(call_id, "answered")
        return {"verdict": "answered", "mode": self.answer_mode, "detail": {}}

    async def dial(self, door_id: str, host: str, port: int) -> dict[str, Any]:
        self._record("dial", door_id, host, port)
        call_id = f"{self.domain}-dial-{len([1 for m, *_ in self.calls if m == 'dial'])}"
        self._owner[call_id] = door_id
        return {"ok": True, "call_id": call_id}

    async def hangup(self, call_id: str, reason: str) -> dict[str, Any]:
        self._record("hangup", call_id, reason)
        return {"ok": True}

    async def open_relay(self, door_id: str, context: Any = None) -> dict[str, Any]:
        self.open_contexts.append(context)
        self._record("open_relay", door_id)
        return {"ok": True}

    async def keepalive(self, call_id: str) -> dict[str, Any]:
        self._record("keepalive", call_id)
        return {"ok": True}

    async def webrtc_offer(self, call_id: str, sdp: str) -> dict[str, Any]:
        self._record("webrtc_offer", call_id, sdp)
        return {"sdp": "fake-answer-sdp"}

    async def webrtc_answer(self, call_id: str, sdp: str) -> dict[str, Any]:
        self._record("webrtc_answer", call_id, sdp)
        return {"ok": True}

    async def webrtc_ice(self, call_id: str, candidate: str) -> dict[str, Any]:
        self._record("webrtc_ice", call_id, candidate)
        return {"ok": True}

    async def media_open(self, call_id: str, codec: str, on_frame: Callable[[str], None],
                         on_closed: Callable[[str], None]) -> dict[str, Any]:
        self._record("media_open", call_id, codec)
        self.media[call_id] = {"on_frame": on_frame, "on_closed": on_closed}
        return {"ok": True, "codec": codec}

    def media_frame(self, call_id: str, data: str) -> None:
        self._record("media_frame", call_id, data)

    async def media_close(self, call_id: str, reason: str) -> dict[str, Any]:
        self._record("media_close", call_id, reason)
        return {"ok": True}
