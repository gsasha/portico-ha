"""TransportProvider: the door side's doors, seen through the provider protocol (contract/provider_v1.md).

PH-2 built the first half (doors, call-state subscription, door-change subscription, open_relay,
secrets_for, call_state); PH-3a added the call half the websocket commands use (dial, hangup, keepalive,
webrtc_*); PH-3b makes `answer` the one arbitration (the verdict, the transport call and the ring-answered
helper write all live here), serves the away call audio (`media_*`), and fires the bus event
`portico_intercom_call` from the same transition fan-out the registry listens to (PROV-04). It speaks the wire: plain dicts out, exceptions with `code` and `message`
(TransportError carries both).
The provider is created at domain level by the integration's `async_setup` (`install_provider`) and handed to the
provider registry directly (one built-in provider since the fold of `portico_intercom` into `portico`).

The provider holds the entries that are *attached* (set up and running), not every config entry: on
unload it detaches first and then notifies the door listeners, so `doors()` already lacks the door when
a listener re-reads (the `subscribe_doors` ordering rule).
"""
from __future__ import annotations

import asyncio
import functools
import logging
import time
from collections import OrderedDict
from typing import Any, Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import entity_registry as er

from ..call_events import add_wire_stats
from .call_state import OutboundTransition, Transition
from .const import DOMAIN, DRIVER_ID
from .gate_sip_listen import SERVER_DRIVER, async_after_answer
from .relay_action import has_relay_action, run_relay_action
from .transport import TransportError

_LOGGER = logging.getLogger(__name__)

API_VERSION = 1
EVENT_INTERCOM_CALL = "portico_intercom_call"   # contract/provider_v1.md section 6; the door side fires it (a historical name)
_MAX_REMEMBERED_CALLS = 256   # call_id -> door_id, like the registry's own bound

DATA_TRANSPORT_PROVIDER = "provider"   # hass.data[DRIVER_ID]["provider"] -> TransportProvider


def get_provider(hass: HomeAssistant) -> "TransportProvider | None":
    data = getattr(hass, "data", None)
    if not isinstance(data, dict):
        return None
    return data.get(DRIVER_ID, {}).get(DATA_TRANSPORT_PROVIDER)




def _verdict(verdict: str, mode: str | None = None, detail: dict[str, Any] | None = None) -> dict:
    return {"verdict": verdict, "mode": mode, "detail": dict(detail or {})}


def _logged_answer(fn):
    """One diagnostic line for the verdict of `answer`: the tracker as it stood at the command's arrival, the time since
    the ring, and the verdict (DEV-764). Never logs the host or the claimant."""
    @functools.wraps(fn)
    async def wrapper(self, door_id, call_id, *, client_kind, claimant, host="", port=0):
        entry = self._entry_for_door(door_id)
        tracker = entry.runtime_data.call_state if entry is not None else None
        seen = f"{tracker.call_id}/{tracker.state}" if tracker is not None else "no-entry"
        ring_ms = (int((time.monotonic() - tracker.ring_mono) * 1000)
                   if tracker is not None and tracker.ring_mono is not None else None)
        try:
            verdict = await fn(self, door_id, call_id, client_kind=client_kind, claimant=claimant, host=host, port=port)
        except Exception as err:
            _LOGGER.info("portico answer: door=%s call=%s kind=%s tracker=%s ring_age_ms=%s raised %s",
                         door_id, call_id, client_kind, seen, ring_ms, getattr(err, "code", None) or type(err).__name__)
            raise
        _LOGGER.info("portico answer: door=%s call=%s kind=%s tracker=%s ring_age_ms=%s verdict=%s mode=%s",
                     door_id, call_id, client_kind, seen, ring_ms, verdict.get("verdict"), verdict.get("mode"))
        return verdict
    return wrapper


def install_provider(hass: HomeAssistant) -> "TransportProvider":
    """The door side's one provider, created once under hass.data[DRIVER_ID] (domain setup calls this
    before any entry, so the registry always holds the object the entries attach to). Tests call it too."""
    data = hass.data.setdefault(DRIVER_ID, {})
    provider = data.get(DATA_TRANSPORT_PROVIDER)
    if provider is None:
        provider = data[DATA_TRANSPORT_PROVIDER] = TransportProvider(hass)
    return provider


class _Attached:
    __slots__ = ("entry", "listener", "outbound_listener", "capabilities_listener")

    def __init__(self, entry: ConfigEntry, listener: Callable[[Transition], None],
                 outbound_listener: Callable[[OutboundTransition], None],
                 capabilities_listener: Callable[[], None]) -> None:
        self.entry = entry
        self.listener = listener
        self.outbound_listener = outbound_listener
        self.capabilities_listener = capabilities_listener


class TransportProvider:
    API_VERSION = API_VERSION
    domain = DRIVER_ID     # the door provider id (persisted in endpoint ids), NOT the HA domain: const.py

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._attached: dict[str, _Attached] = {}          # entry_id -> attachment
        self._listeners: list[Callable[[dict], None]] = []
        self._door_listeners: list[Callable[[], None]] = []
        self._outbound_listeners: list[Callable[[dict], None]] = []
        self._call_doors: OrderedDict[str, str] = OrderedDict()   # call_id -> door_id, learned from transitions
        # call_id -> (on_frame, on_closed) of the open away-media legs
        self._media: dict[str, tuple[Callable[[str], None], Callable[[str], None]]] = {}
        self._answer_locks: dict[str, asyncio.Lock] = {}   # door_id -> the lock the one arbitration holds
        # house calls (optional seam methods, provider_v1.md section 4b): a house call is never in the door tables above
        self._house_legs: OrderedDict[str, list[str]] = OrderedDict()   # house call id -> its frame-channel leg ids
        self._house_listeners: list[Callable[[dict], None]] = []
        self._house_hub_unsub: Callable[[], None] | None = None

    # ---- entry lifecycle (called by __init__.async_setup_entry / async_unload_entry) --------------------

    def attach(self, entry: ConfigEntry) -> None:
        """Attach first, then notify. A second attach of the same entry is a no-op."""
        if entry.entry_id in self._attached:
            return
        tracker = entry.runtime_data.call_state
        attached = _Attached(entry, self._on_transition, self._on_outbound, self._notify_doors)
        tracker.add_transition_listener(attached.listener)
        tracker.add_outbound_listener(attached.outbound_listener)
        # A driver that republishes its capabilities (the gate's CanDial) changes the door's description: the
        # registry re-reads the doors and the UI part's hooks run, as for a door that arrived.
        entry.runtime_data.add_capabilities_listener(attached.capabilities_listener)
        self._attached[entry.entry_id] = attached
        # A call already in flight when the door attaches (a ring that arrived between the hub start and this
        # attach) produced no transition we heard: seed the call -> door map from the tracker so a hangup,
        # keepalive or webrtc_* for it still routes instead of answering `unknown_call` (R3-02).
        if tracker.call_id is not None and tracker.state != "idle":
            self._remember_call(tracker.call_id, entry.runtime_data.device_id)
        self._notify_doors()

    def detach(self, entry: ConfigEntry) -> None:
        """Detach first, then notify."""
        attached = self._attached.pop(entry.entry_id, None)
        if attached is None:
            return
        runtime = getattr(entry, "runtime_data", None)
        tracker = getattr(runtime, "call_state", None)
        if tracker is not None:
            if tracker.call_id is not None and tracker.state != "idle":
                # A door that leaves mid-call ends the call first, while the listener is still attached: the
                # subscribers see ended + idle (the card and the app stop ringing), then the `unavailable`.
                tracker.apply_event({"call_id": tracker.call_id, "state": "ended"})
            tracker.remove_transition_listener(attached.listener)
            tracker.remove_outbound_listener(attached.outbound_listener)
        if runtime is not None and hasattr(runtime, "remove_capabilities_listener"):
            runtime.remove_capabilities_listener(attached.capabilities_listener)
        self._notify_doors()

    # ---- the protocol: doors and call state --------------------------------------------------------------

    def doors(self) -> list[dict]:
        out = []
        for attached in list(self._attached.values()):
            entry = attached.entry
            runtime = entry.runtime_data
            out.append({
                "id": runtime.device_id,
                "provider": self.domain,
                "name": entry.title,
                "driver": entry.data.get("driver") or "",
                "capabilities": dict(runtime.capabilities or {}),
                "camera_entity_id": self._camera_entity_id(entry),
                "available": bool(runtime.entities_available),
                # A ring-only door has no call audio to carry, unless its talk-back is attached (QAC away-talk, 2026-11-11):
                # then a phone that placed a Talk call may carry that call's audio as frames (away_media.py, out- ids).
                # `away_media` is no longer an option (always on); it stays published for old apps/panels and now says only whether
                # this door can carry call audio.
                "away_media": (not runtime.ring_only or bool(getattr(runtime, "talk_attached", False))),
            })
        return out

    def _camera_entity_id(self, entry: ConfigEntry) -> str | None:
        existing = entry.data.get("existing_camera_entity")
        if existing:
            return str(existing)
        # The entity registry's platform is the HA domain (`portico`), not the provider id.
        return er.async_get(self.hass).async_get_entity_id("camera", DOMAIN, f"{entry.entry_id}_camera")

    def call_state(self, door_id: str) -> dict | None:
        entry = self._entry_for_door(door_id)
        if entry is None:
            return None
        tracker = entry.runtime_data.call_state
        snapshot = {"door_id": door_id, "call_id": tracker.call_id, "state": tracker.state}
        # The call Portico placed (P2's tracker side-state): Talk's `busy` reads it, so a dial placed by another app is
        # seen. A door that cannot dial (every door today) carries the two keys only while a call is going, so with
        # none the snapshot is exactly the visitor's. A door that can dial states them always, idle included: Talk
        # believes a provider that publishes them, and only an explicit idle ends its own 300 s memory of a dial the
        # far end hung up. The phones' snapshot (signaling._snapshot_event) keeps door_id, call_id and state only.
        active = tracker.outbound_call_id is not None and tracker.outbound_state in ("dialing", "in_call")
        if (active or getattr(entry.runtime_data, "can_dial", False) is True
                or getattr(entry.runtime_data, "dial_configured", False) is True):
            snapshot["outbound_state"] = tracker.outbound_state
            snapshot["outbound_call_id"] = tracker.outbound_call_id
        return snapshot

    def subscribe(self, listener: Callable[[dict], None]) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._discard(self._listeners, listener)

    def subscribe_doors(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._door_listeners.append(listener)
        return lambda: self._discard(self._door_listeners, listener)

    def subscribe_outbound(self, listener: Callable[[dict], None]) -> Callable[[], None]:
        """OPTIONAL (contract/provider_v1.md section 4a): `listener(dict)` per change of a call Portico placed
        (`dialing` / `in_call` / `ended`; the tracker's side slot, never a visitor transition). Nothing in a
        visitor's path hangs off this: the listeners here hear an `out-` id and nothing else."""
        self._outbound_listeners.append(listener)
        return lambda: self._discard(self._outbound_listeners, listener)

    @staticmethod
    def _discard(items: list, item: Any) -> None:
        if item in items:
            items.remove(item)

    def _on_transition(self, t: Transition) -> None:
        for call_id in (t.call_id, t.prev_call_id):
            if call_id:
                self._remember_call(call_id, t.device_id)
        raw = {"door_id": t.device_id, "call_id": t.call_id, "prev_state": t.prev_state, "state": t.state,
               "prev_call_id": t.prev_call_id, "at_wall": t.at_wall}
        self._after_connect_first_answer(t)
        if t.state != t.prev_state:
            self._announce_talk_change()
        # PROV-04 / STRUCT-08: the bus event is the driver's, fired whether or not anything listens here.
        self.hass.bus.async_fire(EVENT_INTERCOM_CALL, {
            "device_id": t.device_id, "state": t.state, "call_id": t.call_id, "prev_state": t.prev_state,
            "prev_call_id": t.prev_call_id, "at_wall": t.at_wall})
        for listener in list(self._listeners):
            try:
                listener(dict(raw))
            except Exception:   # a raising listener is logged and skipped (protocol rule)
                _LOGGER.exception("portico: a provider transition listener raised")

    def _announce_talk_change(self) -> None:
        """A visitor call changed state (ringing, answered, ended): Talk's availability (`busy`) is live state that is not
        part of the document `rev`, so a phone only learns of it by refetching. a rev-less `content_changed` makes every
        subscriber refetch (the rev did not move, so a rev would be skipped). Once per transition, never per tick; best effort."""
        try:
            from ..content import get_content_store

            get_content_store(self.hass).announce_live()
        except Exception:   # noqa: BLE001 - never let the announcement stop the live path
            _LOGGER.debug("portico: Talk change could not be announced", exc_info=True)

    def _after_connect_first_answer(self, t: Transition) -> None:
        """W5: a gate_sip_server call is answered by the Go hub when the browser's audio leg has connected, not by
        `answer()` (see there), so the ring-answered helper (the "taken elsewhere" close) is written when the call REALLY
        becomes answered: the transition ringing -> answered of that entry's own call. Fire and forget; a no-op without a
        helper, and `async_after_answer` swallows its own failures."""
        if t.state != "answered" or t.prev_state != "ringing" or not t.call_id or t.call_id != t.prev_call_id:
            return
        entry = self._entry_for_door(t.device_id)
        if entry is None or entry.data.get("driver") != SERVER_DRIVER:
            return
        self.hass.async_create_task(async_after_answer(self.hass, entry))

    def _on_outbound(self, t: OutboundTransition) -> None:
        raw = {"door_id": t.device_id, "call_id": t.call_id, "prev_state": t.prev_state, "state": t.state,
               "at_wall": t.at_wall}
        for listener in list(self._outbound_listeners):
            try:
                listener(dict(raw))
            except Exception:   # a raising listener is logged and skipped (protocol rule)
                _LOGGER.exception("portico: a provider outbound listener raised")

    def _notify_doors(self) -> None:
        for listener in list(self._door_listeners):
            try:
                listener()
            except Exception:
                _LOGGER.exception("portico: a provider door listener raised")

    # ---- the protocol: secrets and the relay -------------------------------------------------------------

    def secrets_for(self, door_id: str, phone_id: str) -> dict[str, Any] | None:
        """The credentials a phone needs for a door: the shared device credentials today. This part knows no
        phones (they are the UI part's, STATE-01 / S-2): the UI part asks only for the `phone_id` of a phone it
        has enrolled (signaling `_phone_ids`), and that is the gate (PROV-07). None for an unknown door or an
        empty phone id."""
        entry = self._entry_for_door(door_id)
        if entry is None or not phone_id:
            return None
        return {
            "host": door_id,
            "sip_user": entry.data.get("sip_user"),
            "sip_password": entry.data.get("sip_password"),
            "rtsp_username": entry.data.get("rtsp_username"),
            "rtsp_password": entry.data.get("rtsp_password"),
        }

    async def open_relay(self, door_id: str, context: Context | None = None) -> dict[str, Any]:
        entry = self._entry_for_door(door_id)
        if entry is None:
            raise TransportError("not_available", "no such door")
        # A driver with no relay of its own (gate_sip_server, HasRelay false) answers OpenRelay with
        # not_supported: "the door action is the HA action the installer chose". Run that action here, the
        # same code the lock entity runs. A driver that has a relay keeps calling it; with neither, the
        # driver's own error stands. Missing capabilities count as no relay (as the lock platform does).
        if entry.runtime_data.ring_only:
            # A ring-only door opens nothing, whatever its entry holds (design: no door-open capability).
            raise TransportError("not_supported", "this door is ring-only")
        capabilities = entry.runtime_data.capabilities or {}
        if capabilities.get("HasRelay") is not True and has_relay_action(entry):
            try:
                await run_relay_action(self.hass, entry, context)
            except Exception as err:   # noqa: BLE001 - any failure of the installer's action is a refusal
                _LOGGER.warning("portico: the open_relay_action for %s failed: %s", door_id, type(err).__name__)
                raise TransportError("internal", f"the configured open action failed ({type(err).__name__})") from err
            return {"ok": True}
        return await entry.runtime_data.client.open_relay(door_id)

    # ---- the protocol: calls -----------------------------------------------------------------------------

    async def dial(self, door_id: str, host: str, port: int) -> dict[str, Any]:
        entry = self._require_door(door_id)
        result = await entry.runtime_data.client.dial(door_id, host, port)
        self._note_call_result(door_id, result)
        return result

    async def hangup(self, call_id: str, reason: str) -> dict[str, Any]:
        entry = self._require_call(call_id)
        tracker = entry.runtime_data.call_state
        try:
            result = await entry.runtime_data.client.hangup(call_id, reason)
        except TransportError as err:
            # The app closes its audio leg first and sends the hangup ~70 ms later; a driver that releases the call
            # on the leg close leaves the hub not knowing it, and it answers `not_implemented` (its text is stale). A call that is no longer live here is already over: success.
            # A call still live in the tracker keeps the error; a call no door owns is `unknown_call` above.
            if err.code == "not_implemented" and tracker.call_id == call_id and tracker.state == "ringing" \
                    and entry.data.get("driver") == SERVER_DRIVER:
                # gate_sip_server: a claimed call is still `ringing` (the hub answers when the audio attaches), and the hub
                # only ends a call it answered. Nothing to end: the station's own CANCEL closes it. Not an error.
                _LOGGER.info("portico: hangup of %s: claimed but never answered (gate_sip_server), nothing to end", call_id)
                return {"ok": True}
            if err.code != "not_implemented" or tracker.call_id == call_id:
                raise
            _LOGGER.info("portico: hangup of %s: already released by the driver, treated as ended", call_id)
            return {"ok": True}
        tracker.apply_event({"call_id": call_id, "state": "ended"})
        return result

    async def keepalive(self, call_id: str) -> dict[str, Any]:
        return await self._require_call(call_id).runtime_data.client.keepalive(call_id)

    async def webrtc_offer(self, call_id: str, sdp: str) -> dict[str, Any]:
        return await self._require_call(call_id).runtime_data.client.webrtc_offer(call_id, sdp)

    async def webrtc_answer(self, call_id: str, sdp: str) -> dict[str, Any]:
        return await self._require_call(call_id).runtime_data.client.webrtc_answer(call_id, sdp)

    async def webrtc_ice(self, call_id: str, candidate: str) -> dict[str, Any]:
        return await self._require_call(call_id).runtime_data.client.webrtc_ice(call_id, candidate)

    @_logged_answer
    async def answer(self, door_id: str, call_id: str, *, client_kind: str, claimant: str,
                     host: str = "", port: int = 0) -> dict:
        """The one arbitration (STATE-02): the verdict dict of contract/provider_v1.md section 5. The call must
        be this door's tracked call and still ringing; otherwise it was taken (`taken_elsewhere`) or is gone
        (`not_available`). An app against a direct-capable driver dials the device itself, so only the tracker
        moves (`direct`); anything else asks the transport to answer (`relay`), whose own `already_answered`
        and `claims_disabled` refusals become the verdicts `taken_elsewhere` and `refused`. After a granted
        relay answer nothing else is written here: the ring-answered helper of a gate_sip_server call is written on its
        ringing -> answered transition (`_after_connect_first_answer`).
        `claimant` is for the UI part's own claim table and is not used here."""
        entry = self._entry_for_door(door_id)
        if entry is None:
            return _verdict("not_available")
        runtime = entry.runtime_data
        tracker = runtime.call_state
        relay = not (client_kind == "app" and bool((runtime.capabilities or {}).get("DirectCapable")))
        result: dict[str, Any] = {}
        # One lock per door, held across the transport call with the tracker re-checked inside it: two claimants
        # that both saw `ringing` would otherwise both be granted (the transport's `already_answered` is not a
        # guarantee for every driver); the second now reads `answered` and gets `taken_elsewhere`.
        async with self._answer_locks.setdefault(door_id, asyncio.Lock()):
            if tracker.call_id != call_id or tracker.state != "ringing":
                taken = tracker.call_id == call_id and tracker.state in ("answered", "active")
                return _verdict("taken_elsewhere" if taken else "not_available")
            if relay and (entry.data.get("driver") == SERVER_DRIVER or getattr(runtime, "talk_attached", False)):
                # W5, the W3 note: the legacy IPC `answer` answers AT ONCE, and gate_sip_server answers nothing before the
                # browser's audio leg has connected (the Go hub does it from `webrtc_offer`, ConnectAnswerer). So for this
                # driver the verdict is only the permission to go on to `webrtc_offer`: no IPC `answer`, no tracker move
                # (Go's call_state_changed `answered` moves it, when the audio is really up), no helper write (that follows
                # the transition). A second claimant is told by the hub: `already_answered`.
                # The same set holds the front door while its talkback driver is attached (`unifi_protect_talk`, front-door
                # talk plan T6): the Go driver answers from `webrtc_offer` once the leg has connected, and opens the
                # console session only then.
                return _verdict("answered", "relay", {})
            if relay:
                try:
                    result = await runtime.client.answer(door_id, host, port)
                except TransportError as err:
                    if err.code == "already_answered":
                        return _verdict("taken_elsewhere")
                    if err.code == "claims_disabled":
                        return _verdict("refused")
                    raise
            tracker.apply_event({"call_id": call_id, "state": "answered"})
        return _verdict("answered", "relay" if relay else "direct", result)

    # ---- the protocol: away call audio (portico_away_media_design.md) ------------------------------------

    async def media_open(self, call_id: str, codec: str, on_frame: Callable[[str], None],
                         on_closed: Callable[[str], None]) -> dict[str, Any]:
        """Open the phone's frame leg on the transport. Downlink frames reach `on_frame(data)`; the leg closing
        on its own (no uplink, device hang-up) reaches `on_closed(reason)` once. The callbacks are registered
        before the request, so a frame that races the reply is not lost."""
        entry = self._require_call(call_id)
        self._media[call_id] = (on_frame, on_closed)
        try:
            if self._wants_stats(entry):
                return await entry.runtime_data.client.media_open(call_id, codec, stats=True)
            return await entry.runtime_data.client.media_open(call_id, codec)
        except BaseException:
            self._media.pop(call_id, None)
            raise

    @staticmethod
    def _wants_stats(entry: ConfigEntry) -> bool:
        """DEV-811 P3: ask for the leg's counters only of a door whose driver lists the `call_stats` feature (the capabilities
        of its configure reply): an older wheel, a revert wheel and any other driver get the request of old."""
        if entry.data.get("driver") != SERVER_DRIVER:
            return False
        features = (getattr(entry.runtime_data, "capabilities", None) or {}).get("Features")
        return isinstance(features, list) and "call_stats" in features

    def media_frame(self, call_id: str, data: str) -> None:
        """One uplink frame, fire and forget; a dead IPC socket is the call state's business."""
        try:
            self._require_call(call_id).runtime_data.client.media_frame_nowait(call_id, data)
        except Exception:   # noqa: BLE001
            _LOGGER.debug("portico away media: frame not sent", exc_info=True)

    async def media_close(self, call_id: str, reason: str) -> dict[str, Any]:
        self._media.pop(call_id, None)
        return await self._require_call(call_id).runtime_data.client.media_close(call_id, reason)

    def on_media_notification(self, method: str, params: dict) -> None:
        """Go -> Python: a downlink frame for the phone, or the leg closing on its own. Called by the door's
        runtime for the `media_frame` and `media_closed` notifications; a call with no open leg is ignored."""
        call_id = params.get("call_id")
        if method == "media_closed":
            # BEFORE the leg lookup: a phone that hung up removed its leg (media_close pops it) and the notification that
            # follows must still deliver the counters. Whatever happens to the leg, this part belongs to the call.
            add_wire_stats(self.hass, call_id, params.get("stats"))
        leg = self._media.get(call_id) if isinstance(call_id, str) else None
        if leg is None:
            return
        on_frame, on_closed = leg
        try:
            if method == "media_frame":
                data = params.get("d")
                if isinstance(data, str):
                    on_frame(data)
            elif method == "media_closed":
                self._media.pop(call_id, None)
                on_closed(str(params.get("reason") or "closed"))
        except Exception:   # noqa: BLE001 - a raising callback is logged and skipped, like a listener
            _LOGGER.exception("portico: an away-media callback raised")

    def on_call_audio_stats(self, params: dict) -> None:
        """Go -> Python: the gate-side counters of an answered call, once, at the end of its audio (DEV-811 P3). The hub
        routes the `call_audio_stats` notification here; the counters wait for the call-end helper (call_events.py), which
        fires the stats event when its rule says."""
        add_wire_stats(self.hass, params.get("call_id"), params)

    # ---- the protocol, optional: house calls (contract/provider_v1.md section 4b; M7 3b-S) ----------------------
    #
    # A house call is NOT a door call: its `hso-...` id never enters `_call_doors`, a tracker or `_require_call`, and these
    # methods never touch an entry. They go to the hub, and only when the hub runs the bridge (wheel >= 0.6.0 started
    # with `-house-calls`): everywhere else they answer `not_supported` and nothing is sent.

    def _hub(self):
        from .hub import get_hub        # imported here: hub.py imports this module

        return get_hub(self.hass)

    def house_bridge_supported(self) -> bool:
        """Feature detection: does this provider's hub run the house-call bridge right now? False for a 0.5.x wheel,
        for the option off and while the process is not running."""
        try:
            return bool(self._hub().house_calls_active)
        except Exception:   # noqa: BLE001 - "cannot tell" is "no"
            return False

    async def house_open(self, call_id: str, legs: list[dict]) -> dict[str, Any]:
        """Open the bridge for a house call: `legs` are `{side: from|to, kind: frames|webrtc, codec | sdp, owner?}`. The
        result is the hub's `{session, legs}`; the frame-channel leg ids are remembered so `house_close` can forget their
        callbacks."""
        result = await self._hub().house_open(call_id, legs)
        ids = [leg["leg_id"] for leg in (result.get("legs") or []) if isinstance(leg, dict) and leg.get("leg_id")]
        self._house_legs[call_id] = ids
        self._house_legs.move_to_end(call_id)
        while len(self._house_legs) > _MAX_REMEMBERED_CALLS:
            self._house_legs.popitem(last=False)
        return result

    async def house_close(self, call_id: str) -> dict[str, Any]:
        """Close a house call's bridge and forget its legs' callbacks. Idempotent."""
        for leg_id in self._house_legs.pop(call_id, []):
            self._media.pop(leg_id, None)
        return await self._hub().house_close(call_id)

    def subscribe_house(self, listener: Callable[[dict], None]) -> Callable[[], None]:
        """`listener({call_id, leg, state, reason?, sdp?})` per `house_leg_state` from the hub (state `attached`,
        `first_frame`, `lost` or `sdp`), plus a `lost` per open bridge when the transport process goes away."""
        self._house_listeners.append(listener)
        if self._house_hub_unsub is None:
            self._house_hub_unsub = self._hub().add_house_listener(self._on_house_leg_state)

        def _unsub() -> None:
            self._discard(self._house_listeners, listener)
            if not self._house_listeners and self._house_hub_unsub is not None:
                self._house_hub_unsub()
                self._house_hub_unsub = None

        return _unsub

    def _on_house_leg_state(self, params: dict) -> None:
        for listener in list(self._house_listeners):
            try:
                listener(dict(params))
            except Exception:   # a raising listener is logged and skipped (protocol rule)
                _LOGGER.exception("portico: a provider house listener raised")

    def house_media_open(self, leg_id: str, on_frame: Callable[[str], None], on_closed: Callable[[str], None]) -> None:
        """Register the callbacks of a frame-channel leg the hub already created (`house_open` made it): downlink frames
        reach `on_frame(data)`, the leg ending on its own reaches `on_closed(reason)` once. No request is sent."""
        self._media[leg_id] = (on_frame, on_closed)

    def house_media_frame(self, leg_id: str, data: str) -> None:
        """One uplink frame for a frame-channel leg, fire and forget (the hub's IPC routes `house-...` ids to the bridge)."""
        try:
            client = self._hub().client
            if client is not None:
                client.media_frame_nowait(leg_id, data)
        except Exception:   # noqa: BLE001
            _LOGGER.debug("portico house media: frame not sent", exc_info=True)

    async def house_media_close(self, leg_id: str, reason: str) -> dict[str, Any]:
        """The phone's goodbye on its leg: the hub ends the call."""
        self._media.pop(leg_id, None)
        client = self._hub().client
        if client is None:
            return {}
        return await client.media_close(leg_id, reason)

    # ---- helpers ---------------------------------------------------------------------------------------

    def _require_door(self, door_id: str) -> ConfigEntry:
        entry = self._entry_for_door(door_id)
        if entry is None:
            raise TransportError("not_available", "no such door")
        return entry

    def _require_call(self, call_id: str) -> ConfigEntry:
        """The attached entry that owns this call, learned from transitions and dial results; a call that no
        attached door owns is `unknown_call` (never "the first live entry", PROC-02)."""
        door_id = self._call_doors.get(call_id)
        entry = self._entry_for_door(door_id) if door_id is not None else None
        if entry is None:
            raise TransportError("unknown_call", call_id)
        return entry

    def _remember_call(self, call_id: str, door_id: str) -> None:
        self._call_doors[call_id] = door_id
        self._call_doors.move_to_end(call_id)
        while len(self._call_doors) > _MAX_REMEMBERED_CALLS:
            self._call_doors.popitem(last=False)

    def _note_call_result(self, door_id: str, result: Any) -> None:
        call_id = result.get("call_id") if isinstance(result, dict) else None
        if isinstance(call_id, str) and call_id:
            self._remember_call(call_id, door_id)
            # the hub routes the outbound call's own transitions (which carry no device id) by this (R2-08)
            entry = self._entry_for_door(door_id)
            runtime = getattr(entry, "runtime_data", None)
            if runtime is not None:
                runtime.note_call(call_id)

    def door_is_server_driver(self, door_id: str) -> bool:
        """True when the door's entry runs the gate_sip_server driver (asked by away_media, which never reads the entry)."""
        entry = self._entry_for_door(door_id)
        return entry is not None and entry.data.get("driver") == SERVER_DRIVER

    def door_wants_stats(self, door_id: str) -> bool:
        """True when the door's driver lists the `call_stats` feature (see `_wants_stats`); False for an unknown door."""
        entry = self._entry_for_door(door_id)
        return entry is not None and self._wants_stats(entry)

    def _entry_for_door(self, door_id: str) -> ConfigEntry | None:
        for attached in self._attached.values():
            if attached.entry.runtime_data.device_id == door_id:
                return attached.entry
        return None
