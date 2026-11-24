"""The driver part's per-door runtime: one configured door's call-state tracker, availability
and its share of the one hub's IPC client (an entry with `data.driver`).

Since P-3 a door owns no process: `hub.py` spawns, supervises and restarts the single transport binary of the HA
instance and routes each notification here by `device_id` (or `call_id`). A door attaches to the hub in
`async_start` (the hub `configure`s it) and detaches in `async_stop` (the hub `deconfigure`s it). No
DataUpdateCoordinator: state arrives as pushed events over the IPC channel (transport.py, §9.4).
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from . import gate_sip_listen
from .call_state import CallStateTracker, is_outbound_event
from .const import DOMAIN
from .drivers import DRIVERS, device_id_for, is_local_driver, load_drivers
from .drivers.ha_event import TALK_DRIVER, merged_talk_data, talk_transport_config, talkback_enabled
from .ha_event_ring import HaEventRing, RingOnlyClient
from .gate_sip_listen import transport_config as listen_transport_config
from .remote_cfg import is_remote
from .transport import TransportClient

if TYPE_CHECKING:
    from .hub import TransportHub

_LOGGER = logging.getLogger(__name__)


class DoorRuntime:
    """Everything async_setup_entry needs to keep alive for one configured device: that device's call-state
    tracker, availability. Stored on entry.runtime_data. It has no process code: the hub
    (hub.py) owns the one subprocess and the one IPC client this door's requests go through.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, hub: "TransportHub") -> None:
        self.hass = hass
        self.entry = entry
        self._hub = hub
        self.capabilities: dict | None = None
        # The one id this device is known by on both sides of the IPC
        # channel: sent to Go as `device_id` in `configure` (so Go registers
        # the device under it and stamps it on `ringing`), and what
        # signaling.py looks entries up by (drivers/__init__.py device_id_for).
        device_id = device_id_for(entry.data, entry.entry_id)
        self.device_id = device_id
        self.call_state = CallStateTracker(device_id, entry.entry_id)
        self._device_id = device_id
        # Whether the transport subprocess is up and configured (§9.4: every
        # entity goes unavailable for a transport restart gap).
        self.transport_available = False
        # The driver's own view of its device, from `driver_availability`
        # (dahua_sip, unifi_protect with ring events). None until
        # the running transport's driver first reports, and again whenever
        # the transport stops or dies. Only consulted for a driver whose
        # Capabilities.ReportsAvailability is true: see entities_available.
        self.driver_available: bool | None = None
        self._availability_listeners: list = []
        self._capabilities_listeners: list = []
        # A ring-only door (driver ha_event) has no Go driver: no hub attach, no process, a stand-in client.
        self.ring_only = is_local_driver(entry.data.get("driver"))
        # A gate_sip_server entry in REMOTE mode (remote_cfg.py): its SIP server runs in a container, this door talks to it
        # over its own TCP link (remote.py), not through the hub's process.
        self.remote = is_remote(entry.data)
        self._ring_source: HaEventRing | None = None
        self._ring_only_client = RingOnlyClient() if self.ring_only else None
        # Front-door talk (portico_front_door_talk_plan.md, T6): an ha_event door whose `talkback` option is on ALSO
        # attaches to the hub, as the Go driver `unifi_protect_talk`. `talk_wanted` is the entry's setting;
        # `talk_attached` is True only while the Go driver is configured and holds the answer handle. If the attach
        # fails (an older wheel, no process) the door stays what it always was: ring-only, not answerable.
        self.talk_wanted = bool(self.ring_only and talkback_enabled(self._talk_data()))
        self.talk_attached = False
        self._talk_snapshot = self._talk_effective()
        # The last reason the talk path gave for a failed answer or an ended call (Go's `call_reason`), for logs,
        # diagnostics and the `talk_health` read: {call_id, kind, reason}. Never a credential or a URL.
        self.last_call_reason: dict[str, str] | None = None

    def _talk_data(self) -> dict:
        """The entry's data with its talk options laid over it (ha_event.merged_talk_data)."""
        return merged_talk_data(self.entry.data, self.entry.options)

    def _talk_effective(self) -> tuple:
        """What the talk path would do with the entry now: on or off and the exact `configure` payload. Defaults the form
        writes into the options therefore do not count as a change."""
        data = self._talk_data()
        return (talkback_enabled(data), talk_transport_config(data, self.device_id))

    def talk_settings_changed(self) -> bool:
        """Whether the entry's talk settings now differ, in effect, from the ones this runtime started with."""
        return self._talk_effective() != self._talk_snapshot

    @property
    def can_dial(self) -> bool:
        """Whether the driver can place a call and report it: `CanDial` in the capabilities its `configure`
        returned, exactly True. The gate_sip_server driver sets it while its station is registered and `dial_enabled`
        is on; False for every other driver and configuration. The UI part reads it through the door's capabilities
        (Talk's gate)."""
        return (self.capabilities or {}).get("CanDial") is True

    @property
    def dial_configured(self) -> bool:
        """Whether this entry is set up to place calls (a gate_sip_server entry with `dial_enabled` on), whatever
        `CanDial` says right now. The provider publishes the outbound call state in its snapshot for such a door always, so
        Talk believes the tracker (idle once the call ended) and not its own memory when `CanDial` is momentarily off."""
        if self.entry.data.get("driver") == gate_sip_listen.SERVER_DRIVER:
            return self.entry.data.get("dial_enabled") is True      # W5: the server has no claims leg; the switch is all
        return False

    @property
    def client(self) -> TransportClient | None:
        """The hub's IPC client (shared by every door), or None while the hub's process is down. A ring-only door's
        is a stand-in that refuses everything but a dismiss."""
        if self._ring_only_client is not None and not self.talk_attached:
            return self._ring_only_client      # type: ignore[return-value]
        if self.talk_attached:
            return self._hub.client
        if self.remote:
            return self._hub.remote_client(self.device_id)
        return self._hub.client

    async def async_start(self) -> None:
        """Attach to the hub: it starts the process if this is the first door, then `configure`s this door
        (REL-04's wheel check and the spawn are the hub's). Raises what the hub raises."""
        if self.ring_only:
            self._start_ring_only()
            if self.talk_wanted:
                await self._attach_talk()
            return
        await self._hub.async_attach(self)

    async def _attach_talk(self) -> None:
        """Attach the talk driver to the hub. Any failure leaves the door ring-only (the ring source is already
        running and does not need Go): logged without the configure payload, which holds the Protect key."""
        try:
            await self._hub.async_attach(self)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 - the ring must survive whatever the talk path does
            self.talk_attached = False
            _LOGGER.warning("portico: %s: the talkback backend could not start (%s); the door stays ring-only",
                            self._device_id, getattr(err, "code", None) or type(err).__name__)
            self.capabilities = self._local_capabilities()
            self._notify_capabilities()

    def _local_capabilities(self) -> dict:
        load_drivers()
        meta = DRIVERS[self.entry.data["driver"]]
        data = self._talk_data()
        if self.talk_wanted and not self.talk_attached:
            data["talkback"] = False        # the backend is not there: say so honestly
        return (meta.local_capabilities_for(data) if meta.local_capabilities_for else dict(meta.local_capabilities or {}))

    def _start_ring_only(self) -> None:
        """A door with no Go driver: capabilities are the driver's own, the ring source is the HA event entity."""
        data = self.entry.data
        self.capabilities = self._local_capabilities()
        self.set_transport_available(True)       # nothing to wait for: availability follows the event entity
        self._ring_source = HaEventRing(
            self.hass, self, event_entity=data["event_entity"], event_type=data.get("event_type") or "ring",
            hold=float(data.get("ring_hold") or 30))
        self._ring_source.start()

    def note_call(self, call_id: str) -> None:
        """A call id learned from a `dial` result: the hub routes that call's notifications here by it, for the
        outbound call whose events carry no device id (R2-08)."""
        self._hub.note_call(call_id, self.device_id)

    async def async_configure(self) -> None:
        """Push this entry's config to the hub's transport (§9.4) and
        record the Capabilities it returns. The config carries `device_id`
        explicitly, so the Go side never derives a different id of its own
        (a driver would otherwise default to its own host field)."""
        client = self._hub.client if self.ring_only else self.client      # a talk door is not "attached" yet: not the stand-in
        assert client is not None
        driver = self.entry.data["driver"]
        if self.ring_only:
            # A talkback ha_event door: the Go driver behind it has its own name and takes the talkback options.
            driver = TALK_DRIVER
        # Start actually touches the network (real SIP REGISTER for
        # dahua_sip, §9.3) — the
        # design doc's activation-gate rule (§7) applies from here, not
        # from spawning the subprocess alone.
        if self.ring_only:
            config = talk_transport_config(self._talk_data(), self.device_id)    # holds the key: never logged
        elif self.remote:
            # The container adopts the server it started from its own environment and ignores this config: the entry's
            # credentials (the IPC token) are used to connect and never travel again, and there is no SIP password here.
            config = {"device_id": self.device_id}
        else:
            config = await listen_transport_config(self.hass, self.entry, self.device_id)
        self.capabilities = await client.configure(driver, config)
        if self.ring_only:
            self.talk_attached = True
        # A freshly configured driver supersedes whatever it reported
        # before: clear a standing driver_error repair issue.
        ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)
        self.set_transport_available(True)

    @property
    def _issue_id(self) -> str:
        return f"driver_error_{self.entry.entry_id}"

    def add_availability_listener(self, listener) -> None:
        self._availability_listeners.append(listener)

    def remove_availability_listener(self, listener) -> None:
        if listener in self._availability_listeners:
            self._availability_listeners.remove(listener)

    @property
    def reports_availability(self) -> bool:
        """Whether this entry's driver sends `driver_availability` (Go's
        Capabilities.ReportsAvailability from `configure`; absent = no)."""
        return (self.capabilities or {}).get("ReportsAvailability") is True

    @property
    def entities_available(self) -> bool:
        """What the entities of this entry show as `available`: the
        transport is up, and, for a driver that reports its device's
        availability, its last report said available (None, nothing
        reported yet, counts as unavailable so a fresh start never shows a
        false `idle`). A driver that never reports follows the transport
        alone, as before driver_availability existed."""
        if not self.transport_available:
            return False
        if not self.reports_availability:
            return True
        return self.driver_available is True

    def add_capabilities_listener(self, listener) -> None:
        self._capabilities_listeners.append(listener)

    def remove_capabilities_listener(self, listener) -> None:
        if listener in self._capabilities_listeners:
            self._capabilities_listeners.remove(listener)

    def _notify_capabilities(self) -> None:
        for listener in list(self._capabilities_listeners):
            try:
                listener()
            except Exception:
                _LOGGER.exception("portico: a capabilities listener raised")

    def _notify_availability(self) -> None:
        for listener in list(self._availability_listeners):
            listener()

    def _end_tracked_call(self) -> None:
        """End the call this entry's tracker follows, if any, through the
        ordinary apply_event path. Used when whatever was following the call
        (the driver's device, or the transport itself) is gone: a stale
        `ringing`/`answered` must not outlive it. A late event from Go for
        that call is then dropped by the tracker (`ended` is terminal)."""
        call_id = self.call_state.call_id
        if call_id is not None or self.call_state.state != "idle":
            self.call_state.apply_event({"call_id": call_id, "state": "ended"})
        self.call_state.end_outbound_call()

    def transport_gone(self) -> None:
        """The transport process stopped or died (or this door left it): its driver's calls and its
        view of the device die with it (Go's Stop deliberately raises no
        `ended`). The hub runs this before a respawn, so
        the new process's first availability report is not wiped."""
        self.driver_available = None
        if self.talk_attached:
            # The Go driver went with the process; it re-attaches (configure) on the next process. Until then the
            # door is not answerable and the ring source keeps ringing.
            self.talk_attached = False
        self._end_tracked_call()
        if self.transport_available:
            self.set_transport_available(False)  # notifies the listeners
        else:
            self._notify_availability()

    def set_transport_available(self, available: bool) -> None:
        if available == self.transport_available:
            return
        self.transport_available = available
        self._notify_availability()

    def _apply_driver_availability(self, params: dict) -> None:
        """`driver_availability {device_id, available, reason}`: the driver's
        own view of its device, sent on transitions only. Unavailable ends
        the tracked call (the driver can no longer see it end) and makes the
        entities unavailable; available makes them available again, idle.

        Deliberately NOT a repair issue: an outage is ordinary (a
        station re-registering for minutes) and clears itself; a repair
        issue is for what needs a human (driver_error)."""
        device = params.get("device_id")
        if device not in (None, self.device_id):
            _LOGGER.warning(
                "portico transport: dropping driver_availability for device %r on the %r transport",
                device, self.device_id,
            )
            return
        available = params.get("available")
        if not isinstance(available, bool):
            _LOGGER.warning("portico transport: malformed driver_availability: %s", params)
            return
        if available == self.driver_available:
            return
        self.driver_available = available
        if available:
            _LOGGER.info("portico: %s is available again", self._device_id)
        else:
            _LOGGER.warning(
                "portico: %s is unavailable: %s", self._device_id, params.get("reason") or "no reason given"
            )
            self._end_tracked_call()
        self._notify_availability()

    def _apply_capabilities_changed(self, params: dict) -> None:
        """`capabilities_changed {device_id, capabilities}`: the driver's whole capability set again, after `configure`
        returned it. The gate_sip_server driver sends it when `CanDial` or `StationRegistered` changes (the station registering), so
        a change after this entry loaded takes effect without a reload.
        Every reader (`can_dial`, the provider's door list, Talk's gate) reads `self.capabilities` live, so storing
        it is the fail-closed part; the listeners then announce the change (provider_impl -> door hooks -> content)."""
        device = params.get("device_id")
        if device not in (None, self.device_id):
            _LOGGER.warning(
                "portico transport: dropping capabilities_changed for device %r on the %r transport",
                device, self.device_id,
            )
            return
        capabilities = params.get("capabilities")
        if not isinstance(capabilities, dict):
            _LOGGER.warning("portico transport: malformed capabilities_changed: %s", params)
            return
        if capabilities == (self.capabilities or {}) and self.capabilities is not None:
            return
        was = self.can_dial
        self.capabilities = dict(capabilities)
        _LOGGER.info("portico: %s capabilities changed (can_dial %s -> %s)", self._device_id, was, self.can_dial)
        self._notify_capabilities()

    def handle_notification(self, method: str, params: dict) -> None:
        """One notification the hub routed to this door (by `device_id`, or by `call_id` for the transitions of a
        call whose id the hub learned). The hub carries other doors' rings too, so the device-id check stays:
        a ring or transition for any other device id means Python and Go disagree about which device this is;
        drop it loudly rather than ring the wrong one."""
        if method in ("ringing", "call_state_changed"):
            event_device = params.get("device_id")
            if event_device not in (None, "", self.device_id):
                _LOGGER.warning(
                    "portico transport: dropping a %s for device %r on door %r", method, event_device, self.device_id
                )
                return
        if method in ("ringing", "call_state_changed") and is_outbound_event(params):
            # A call Portico placed (the hub's `out-` id): its own state, never a visitor ring.
            self.call_state.apply_outbound_event({"state": "ringing", **params} if method == "ringing" else params)
        elif method == "ringing":
            self.call_state.apply_event({"state": "ringing", **params})
        elif method == "call_state_changed":
            self.call_state.apply_event(params)
        elif method == "driver_availability":
            self._apply_driver_availability(params)
        elif method == "capabilities_changed":
            self._apply_capabilities_changed(params)
        elif method == "call_reason":
            self._apply_call_reason(params)
        elif method == "driver_error":
            self._raise_driver_error(params)
        else:
            _LOGGER.debug("portico transport: unhandled notification %s: %s", method, params)

    def _apply_call_reason(self, params: dict) -> None:
        """`call_reason {device_id, call_id, kind, reason}` (talk door): why an answer did not open (`answer_failed`: the ring
        stays answerable) or why a call ended (`ended`). Logged at WARNING when it is a failure, kept for diagnostics.
        A phone sees the call just end or stay silent; this is where the reason is readable."""
        kind, reason, call_id = params.get("kind"), params.get("reason"), params.get("call_id")
        if not all(isinstance(v, str) for v in (kind, reason, call_id)):
            _LOGGER.warning("portico transport: malformed call_reason: %s", params)
            return
        self.last_call_reason = {"call_id": call_id, "kind": kind, "reason": reason}
        level = _LOGGER.warning if kind == "answer_failed" or not (reason.startswith("hung up") or reason == "the ring expired unanswered") \
            else _LOGGER.info
        level("portico: %s call %s %s: %s", self._device_id, call_id, kind, reason)

    # ---- the talk door's two Python-to-Go calls (read-only / registration) ---------------------------

    async def talk_external_ring(self, call_id: str, hold: float) -> None:
        """Tell the Go driver the ring `call_id` is held for `hold` seconds (HaEventRing, after it rang the phones).
        A failure is logged: the phones still ring, the call just cannot be answered."""
        client = self.client
        if not self.talk_attached or client is None:
            return
        try:
            await client.external_ring(self.device_id, call_id, hold)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("portico: %s: external_ring for %s failed (%s); this ring cannot be answered",
                            self._device_id, call_id, getattr(err, "code", None) or type(err).__name__)

    async def talk_hangup(self, call_id: str, reason: str = "ring_expired") -> None:
        """End the Go call `call_id`: the Python-side ring expired (its own hold would, a moment later), or a bell press
        closed the call out (reason `bell_pressed`)."""
        client = self.client
        if not self.talk_attached or client is None:
            return
        try:
            await client.hangup(call_id, reason)
        except Exception as err:  # noqa: BLE001 - not_implemented / no such call: the Go hold ends it anyway
            _LOGGER.debug("portico: %s: hangup of %s: %r", self._device_id, call_id, err)

    async def talk_health(self) -> dict | None:
        """The Go driver's read-only diagnostics block, or None when there is no talk backend (or it cannot be read)."""
        client = self.client
        if not self.talk_attached or client is None:
            return None
        try:
            return dict(await client.talk_health(self.device_id))
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("portico: %s: talk_health: %r", self._device_id, err)
            return None

    def _raise_driver_error(self, params: dict) -> None:
        """§9.4: `driver_error` is surfaced as a standard HA repair issue on
        the config entry. §9.4 names the notification but not its params;
        this reads an optional `message` (and `code`) and tolerates anything
        else. The issue is cleared by the next successful configure (a
        transport restart or an entry reload) and on unload.

        The Go side emits it through the optional errorReporter of its event sink (PH-10), stamped with the
        `device_id` the hub routes by. It is for configuration/start failures; ordinary unavailability arrives
        as driver_availability and raises no issue."""
        message = str(params.get("message") or params.get("code") or "unknown driver error")
        _LOGGER.error("portico transport driver_error for %s: %s", self._device_id, params)
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._issue_id,
            is_fixable=False,
            is_persistent=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key="driver_error",
            translation_placeholders={
                "device": self.entry.title or self._device_id,
                "driver": str(self.entry.data.get("driver")),
                "message": message,
            },
        )

    async def async_stop(self) -> None:
        """Leave the hub: the door's calls and device view end now, then the hub `deconfigure`s it (and stops the
        process when this was the last door)."""
        ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)
        if self._ring_source is not None:
            self._ring_source.stop()
            self._ring_source = None
        self.transport_gone()
        if self.ring_only and not self.talk_wanted:
            return
        await self._hub.async_detach(self)
