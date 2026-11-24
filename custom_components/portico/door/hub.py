"""The one transport hub: ONE `portico-transport` process per HA instance (PROC-01, P-3 / PH-11).

Before P-3 every door entry spawned its own Go process and socket. Now `hass.data[DRIVER_ID]["hub"]` (`portico_intercom`, the door side's namespace)
owns the single process, its IPC socket (`.storage/portico_intercom/hub.sock`) and its IPC client, and every
door (`door.DoorRuntime`) attaches to it:

* the first `async_attach` checks the installed wheel (REL-04), spawns the process and connects;
* each attach sends `configure` for that door; each detach sends `deconfigure` for it and the process stops
  after the last detach;
* notifications are routed by `device_id` (the door) or, for a transition with none, by `call_id` (the door
  the call was learned from: a ring, or a `dial` result, R2-08); `media_frame`/`media_closed` go by `call_id`
  to the provider's away-media callbacks (PROC-02). Nothing ever picks "the first door";
* a crashed process is restarted once and every attached door is re-`configure`d (design §4.1, §9.4). The
  restart-once count is per process lifetime and starts over when a door attaches to a process that is not
  running (a clean start).

This module is the only place in the part with process code (`create_subprocess_exec`, the socket path).
"""
from __future__ import annotations

import asyncio
import importlib.metadata
import logging
import os
from collections import OrderedDict
from functools import partial
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN, DRIVER_ID
from .provider_impl import get_provider
from .wheel import async_ensure_wheel
from .remote import RemoteConfigError, RemoteLink
from .transport import TransportClient, TransportError

if TYPE_CHECKING:
    from .door import DoorRuntime

_LOGGER = logging.getLogger(__name__)

DATA_HUB = "hub"   # hass.data[DRIVER_ID]["hub"] -> TransportHub

# The directory under /config/.storage that holds the hub's IPC socket. The UI part keeps the `portico.*` store
# namespace; this part's socket is its own (S-2). One socket per HA instance since P-3 (it was one per entry).
SOCKET_DIR = ".storage/portico_intercom"
SOCKET_NAME = "hub.sock"

# The oldest wheel whose IPC verbs this Python needs (`deconfigure`, `device_id` on `call_state_changed` and
# `driver_error`: transport-v0.5.0). Bumped by every change that adds an IPC verb or field this part relies on
# (REL-04); a wheel release this Python does not rely on moves only the pin in wheel.py (the 0.5.1 wheel did: its
# additions are optional and a 0.5.0 wheel still serves every verb used here). The pin is installed by
# `wheel.async_ensure_wheel` just before this check when the installed wheel is older than the pin, so this check
# fires only when that install did not leave a wheel at least this new (a failed install, `skip_pip`, a hand install
# of an old wheel); it is cheap, and the version is in the diagnostics either way.
MIN_WHEEL_VERSION = "0.5.0"
WHEEL_DIST = "portico-transport"        # the wheel's distribution name (packaging/rust/build_wheels.py); the wheel ships no __version__
ISSUE_WHEEL_TOO_OLD = "wheel_too_old"

# House calls (M7 3b-S; design 15.4, 15.16). The bridge lives in wheel 0.6.0 and is OFF there unless the process is
# started with `-house-calls`. This part passes the flag ONLY when the installed wheel is 0.6.0 or newer AND a door
# entry's option `house_calls` is true (absent = off; written only by a door's Reconfigure form, M7 3g, and
# that form shows it only with a wheel that has the bridge): a 0.5.x binary exits on an argv
# flag it does not know, so the flag must never reach one. MIN_WHEEL_VERSION is NOT raised for this (a 0.5.1 wheel
# still serves every verb a door needs); the release PRs that moved the pin to 0.6.0, 0.6.1, 0.6.2, 0.6.3, 0.6.4, 0.6.5, 0.6.6, 0.6.7, 0.6.8, 0.6.9 and 0.6.10 left the floor at 0.5.0 on purpose (every house path above is feature-detected).
CONF_HOUSE_CALLS = "house_calls"
HOUSE_CALLS_MIN_WHEEL = "0.6.0"
HOUSE_CALLS_FLAG = "-house-calls"
HOUSE_CALL_SUMMARY_PREFIX = "portico-transport: house call "   # the transport's house-call log line (transport-rs/src/house/)

# How long to keep retrying the initial IPC connect after spawning the subprocess: it needs a moment to create
# its Unix socket. 5s / 0.1s poll is generous relative to how fast a Go binary starts, and ConfigEntryNotReady
# beyond that tells HA to retry entry setup with its own backoff, rather than inventing a second one here.
_CONNECT_RETRY_INTERVAL = 0.1
_CONNECT_RETRY_ATTEMPTS = 50

_DECONFIGURE_TIMEOUT = 15      # Go's Stop blocks for the clean SIP de-register
_MAX_REMEMBERED_CALLS = 256    # call_id -> device_id, like the provider's own bound
_MAX_HOUSE_CALLS = 16          # open house bridges the hub remembers (Go caps them at 4; this is only a bound)


class HubError(Exception):
    """The hub refused to attach a door (the entry's setup returns False with this message logged)."""


class WheelTooOld(HubError):
    """REL-04: the installed wheel is older than MIN_WHEEL_VERSION. A repair issue is raised; no door attaches."""


def get_hub(hass: HomeAssistant) -> "TransportHub":
    """The instance's one hub, created on first use (domain setup does not need to have run first)."""
    data = hass.data.setdefault(DRIVER_ID, {})
    hub = data.get(DATA_HUB)
    if hub is None:
        hub = data[DATA_HUB] = TransportHub(hass)
    return hub


def _parse_version(text: str):
    from packaging.version import InvalidVersion, Version
    try:
        return Version(text)
    except InvalidVersion:
        return None


def read_installed_wheel_version() -> str | None:
    """The installed `portico-transport` wheel's version, or None when its metadata cannot be read. Blocking (it lists a
    directory): off the event loop. The hub and a door's Reconfigure form (M7 3g) both read it here."""
    try:
        return importlib.metadata.version(WHEEL_DIST)
    except importlib.metadata.PackageNotFoundError:
        return None


def wheel_has_house_calls(version: str | None) -> bool:
    """Does a wheel of this version have the house-call bridge (>= 0.6.0, a final release; PEP 440 puts 0.6.0rc1 and
    0.6.0.dev1 below it)? An unreadable or missing version is False: the flag is never sent on a guess."""
    have = _parse_version(version) if version else None
    return have is not None and have >= _parse_version(HOUSE_CALLS_MIN_WHEEL)


class TransportHub:
    """One process per HA instance. `hass.data[DRIVER_ID]["hub"]`."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.client: TransportClient | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._supervise_task: asyncio.Task[None] | None = None
        self._drain_task: asyncio.Task[None] | None = None
        self._doors: dict[str, "DoorRuntime"] = {}                 # device_id -> attached door
        self._call_doors: OrderedDict[str, str] = OrderedDict()    # call_id -> device_id
        self._live_client: TransportClient | None = None           # the client whose notifications are current
        self._lock = asyncio.Lock()                                # start/stop/restart and the door table
        self._restarted = False                                    # the one restart of this process lifetime used
        self._shutdown = False                                     # HA is stopping: the hub is done for good
        self._stop_unsub = None
        self._wheel_version: str | None = None
        self._attaching: "DoorRuntime | None" = None               # the door whose attach is starting the process
        self._house_flag = False                                   # the running process was started with -house-calls
        self._house_calls: OrderedDict[str, None] = OrderedDict()  # house call ids whose bridge this hub opened
        self._house_listeners: list[Callable[[dict], None]] = []   # house_leg_state subscribers (the provider's)
        self._remotes: dict[str, RemoteLink] = {}                  # device_id -> a REMOTE SIP-server door's own TCP link (W8b)

    # ---- paths, wheel ----------------------------------------------------------------------------------

    @property
    def socket_path(self) -> str:
        """One socket per HA instance (was `.storage/portico/<entry_id>.sock`, one per entry)."""
        return self.hass.config.path(f"{SOCKET_DIR}/{SOCKET_NAME}")

    def installed_wheel_version(self) -> str | None:
        """The installed `portico-transport` wheel's version, or None when its metadata cannot be read
        (SPK-5: the wheel has no `__version__`; the dist metadata is the read)."""
        return read_installed_wheel_version()

    async def _check_wheel(self) -> None:
        """REL-04, a backstop: raise the repair issue `wheel_too_old` (and WheelTooOld, which makes every entry's
        setup return False) when the installed wheel is older than MIN_WHEEL_VERSION, rather than letting a missing IPC verb
        fail later in the middle of a call. An unreadable version passes: the wheel's own import already proved
        it is installed, and the diagnostics show `null`. The metadata read lists a directory, so it runs in the
        executor, never on the event loop."""
        installed = await self.hass.async_add_executor_job(self.installed_wheel_version)
        self._wheel_version = installed
        have = _parse_version(installed) if installed else None
        if have is None:
            if installed is None:
                _LOGGER.info("portico: the installed portico-transport wheel version cannot be read; not checked")
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_WHEEL_TOO_OLD)
            return
        if have < _parse_version(MIN_WHEEL_VERSION):
            _LOGGER.error(
                "Portico Intercom needs the portico-transport wheel %s or newer but %s is installed; "
                "no door will start until Home Assistant restarts onto the pinned wheel",
                MIN_WHEEL_VERSION, installed,
            )
            ir.async_create_issue(
                self.hass, DOMAIN, ISSUE_WHEEL_TOO_OLD,
                is_fixable=False, is_persistent=False, severity=ir.IssueSeverity.ERROR,
                translation_key=ISSUE_WHEEL_TOO_OLD,
                translation_placeholders={"installed": installed, "required": MIN_WHEEL_VERSION},
            )
            raise WheelTooOld(f"portico-transport {installed} is older than {MIN_WHEEL_VERSION}")
        ir.async_delete_issue(self.hass, DOMAIN, ISSUE_WHEEL_TOO_OLD)

    # ---- house calls (M7 3b-S; inert: see the constants above) -------------------------------------------

    def house_wheel_ok(self) -> bool:
        """Is the installed wheel one that has the bridge (>= 0.6.0, a final release; PEP 440 puts 0.6.0rc1 and
        0.6.0.dev1 below it)? An unreadable version is False: the flag is never sent on a guess."""
        return wheel_has_house_calls(self._wheel_version)

    def house_calls_wanted(self) -> bool:
        """Should the process about to start get `-house-calls`? Only with a wheel that has it AND a door entry whose
        option `house_calls` is true (the attaching door counts: the process starts before it joins `_doors`)."""
        if not self.house_wheel_ok():
            return False
        doors = list(self._doors.values())
        if self._attaching is not None:
            doors.append(self._attaching)
        return any(d.entry.options.get(CONF_HOUSE_CALLS) is True for d in doors)

    @property
    def house_calls_active(self) -> bool:
        """The feature-detection answer every house path reads: a live process that was started with the flag."""
        return self._house_flag and self._running()

    async def house_open(self, call_id: str, legs: list[dict]) -> dict:
        """Open a house call's two-leg bridge. `not_supported` (no IPC at all) unless the hub runs the bridge: an
        old wheel never sees the verb either."""
        client = self.client
        if not self.house_calls_active or client is None:
            raise TransportError("not_supported", "house calls are not enabled on this hub")
        result = await client.house_open(call_id, legs)
        self._house_calls[call_id] = None
        self._house_calls.move_to_end(call_id)
        while len(self._house_calls) > _MAX_HOUSE_CALLS:
            self._house_calls.popitem(last=False)
        return result

    @property
    def open_house_calls(self) -> int:
        """How many house-call bridges this hub has open now (the Settings tab's idle rule: a reload that stops the
        process would end them)."""
        return len(self._house_calls)

    async def house_close(self, call_id: str) -> dict:
        """Close a bridge (idempotent). The hub that cannot run bridges has nothing to close: `not_supported`."""
        client = self.client
        if not self.house_calls_active or client is None:
            raise TransportError("not_supported", "house calls are not enabled on this hub")
        self._house_calls.pop(call_id, None)
        return await client.house_close(call_id)

    def add_house_listener(self, listener: Callable[[dict], None]) -> Callable[[], None]:
        """`listener(params)` per `house_leg_state` notification, plus one `lost` per open bridge when the process
        goes away. Returns the unsubscribe."""
        self._house_listeners.append(listener)

        def _unsub() -> None:
            if listener in self._house_listeners:
                self._house_listeners.remove(listener)

        return _unsub

    def _emit_house(self, params: dict) -> None:
        for listener in list(self._house_listeners):
            try:
                listener(dict(params))
            except Exception:  # noqa: BLE001 - one raising listener must not starve the others
                _LOGGER.exception("portico: a house leg listener raised")

    def _house_transport_gone(self) -> None:
        """The process is gone (crash, last detach, HA stopping): its bridges went with it. Tell the listeners, once
        per bridge this hub opened; a hub that never opened one says nothing."""
        calls, self._house_calls = list(self._house_calls), OrderedDict()
        self._house_flag = False
        for call_id in calls:
            self._emit_house({"call_id": call_id, "leg": "both", "state": "lost", "reason": "transport_gone"})

    # ---- doors -----------------------------------------------------------------------------------------

    @property
    def doors(self) -> list["DoorRuntime"]:
        return list(self._doors.values())

    def _running(self) -> bool:
        return self._process is not None and self._process.returncode is None and self.client is not None

    # ---- remote doors (W8b) ----------------------------------------------------------------------------

    def remote_client(self, device_id: str) -> TransportClient | None:
        """The live client of a remote door's link, or None while it is down (or the door is not remote)."""
        link = self._remotes.get(device_id)
        return link.client if link is not None else None

    def remote_link(self, device_id: str) -> RemoteLink | None:
        return self._remotes.get(device_id)

    async def _attach_remote(self, door: "DoorRuntime") -> dict | None:
        """A remote SIP-server door: no wheel, no process, no shared socket. The link connects in the background, so an
        unreachable container never fails the entry's setup (the door shows unavailable until it answers)."""
        async with self._lock:
            if self._shutdown:
                raise HubError("Home Assistant is stopping")
            if door.device_id in self._remotes or door.device_id in self._doors:
                raise HubError(f"a door with device id {door.device_id!r} is already attached")
            try:
                link = RemoteLink(self.hass, door, door.entry.data)
            except RemoteConfigError as err:
                raise HubError(f"the remote SIP-server settings are not acceptable ({err})") from err
            self._remotes[door.device_id] = link
            link.start()
        return door.capabilities

    async def _detach_remote(self, door: "DoorRuntime") -> None:
        link = self._remotes.pop(door.device_id, None)
        if link is not None:
            await link.stop(detach=not self._shutdown)

    async def async_attach(self, door: "DoorRuntime") -> dict | None:
        """Attach a door: start the process if it is not running (first door, or a hub that gave up), then
        `configure` the door. Returns its Capabilities. Raises WheelTooOld, HubError or what the connect/configure
        raised; on a configure failure the door is left detached again. A remote SIP-server door takes the other path
        (`_attach_remote`)."""
        if door.remote:
            return await self._attach_remote(door)
        if not self._running():
            # Install the pinned wheel first if the installed one is older (wheel.py), OUTSIDE the hub lock: a slow pip
            # (up to three tries of a minute each) must not hold the lock the stop hook and the other doors wait on. It
            # runs before the floor check below, which raises WheelTooOld for a wheel older than the floor, so an old
            # wheel is upgraded BEFORE it, never after. Two doors racing here is harmless: HA's installer serialises
            # on its own pip lock and the second call finds the wheel installed.
            await async_ensure_wheel(self.hass)
        async with self._lock:
            if self._shutdown:
                raise HubError("Home Assistant is stopping")
            held = self._doors.get(door.device_id)
            if held is not None and held is not door:
                raise HubError(f"a door with device id {door.device_id!r} is already attached")
            others = [d for d in self._doors.values() if d is not door]
            if not self._running():
                await self._check_wheel()
                self._restarted = False     # a door attaching to a process that is not running is a clean start
                if self._process is not None:
                    await self._stop_process()      # a process that gave up: drop its dead client first
                self._attaching = door
                try:
                    await self._start_process()
                finally:
                    self._attaching = None
                await self._configure_all(others)
            self._doors[door.device_id] = door
            # Configure under the lock: a restart (`_supervise`) takes it too, so it can neither null `self.client`
            # under this configure nor configure this same door a second time (Go refuses that: already_configured).
            try:
                await door.async_configure()
            except BaseException:
                await self._detach_locked(door)
                raise
        return door.capabilities

    async def async_detach(self, door: "DoorRuntime") -> None:
        """`deconfigure` one door and keep the process for the others; the process stops after the last detach.
        A door that is not attached (or a hub that already stopped) is a no-op."""
        if door.remote:
            async with self._lock:
                await self._detach_remote(door)
            return
        async with self._lock:
            await self._detach_locked(door)

    async def _detach_locked(self, door: "DoorRuntime") -> None:
        """The body of `async_detach`; the caller holds the lock."""
        if self._doors.get(door.device_id) is not door:
            return
        del self._doors[door.device_id]
        for call_id in [c for c, d in self._call_doors.items() if d == door.device_id]:
            del self._call_doors[call_id]
        if self._shutdown:
            return
        client = self.client
        if client is not None and self._running():
            try:
                await asyncio.wait_for(client.deconfigure(door.device_id), _DECONFIGURE_TIMEOUT)
            except TransportError as err:
                if err.code != "unknown_device":
                    _LOGGER.warning("portico: deconfigure of %s failed: %s", door.device_id, err)
            except Exception as err:  # noqa: BLE001 - the door is leaving either way
                _LOGGER.warning("portico: deconfigure of %s failed: %r", door.device_id, err)
        if not self._doors:
            await self._stop_process()

    def note_call(self, call_id: str, device_id: str) -> None:
        """Remember which door a call belongs to (a ring, or a `dial` result), for notifications without a
        `device_id` (an outbound call's transitions)."""
        if not call_id or device_id not in self._doors:
            return
        self._call_doors[call_id] = device_id
        self._call_doors.move_to_end(call_id)
        while len(self._call_doors) > _MAX_REMEMBERED_CALLS:
            self._call_doors.popitem(last=False)

    # ---- process ---------------------------------------------------------------------------------------

    async def _start_process(self) -> None:
        """Spawn the binary and connect the IPC client; no door is configured here. Caller holds the lock."""
        # Imported here, not at module level: this package only exists once the platform-tagged wheel (wheel.py)
        # is installed: importing it at module level would break every test/tool that imports this package without
        # that wheel present, and the wheel may be installed by `async_ensure_wheel` a moment before.
        import portico_transport

        socket_path = self.socket_path
        # The one blocking call of the start-up: off the event loop (it was on the loop until P-3).
        await self.hass.async_add_executor_job(partial(os.makedirs, os.path.dirname(socket_path), exist_ok=True))
        binary = portico_transport.binary_path()
        argv = [binary, "-socket", socket_path]
        house_flag = self.house_calls_wanted()
        if house_flag:
            argv.append(HOUSE_CALLS_FLAG)
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        # Piping without ever reading is a real bug, not a formality: the OS pipe buffer is finite, and once it
        # fills the CHILD PROCESS blocks forever on its own write(): drain it continuously.
        drain = asyncio.ensure_future(self._drain_subprocess_output(process))

        client = TransportClient(socket_path)
        connected = False
        try:
            for _ in range(_CONNECT_RETRY_ATTEMPTS):
                try:
                    await client.connect()
                    connected = True
                    break
                except (FileNotFoundError, ConnectionRefusedError):
                    await asyncio.sleep(_CONNECT_RETRY_INTERVAL)
        except BaseException:
            # A cancellation (HA stopping during a restart, an entry setup cancelled) or any other error inside the
            # retry loop: the process is already spawned and the drain task already running, and nothing else owns
            # them yet. Do not leave them behind.
            drain.cancel()
            await asyncio.shield(self._kill(process))
            raise
        if not connected:
            await self._kill(process)
            drain.cancel()
            raise ConfigEntryNotReady(f"portico transport binary never opened its IPC socket at {socket_path}")

        def _from_this_client(method: str, params: dict) -> None:
            if client is not self._live_client:
                _LOGGER.debug("portico transport: dropping %s from a stopped transport", method)
                return
            self._handle_notification(method, params)

        client.on_notification(_from_this_client)
        self._process, self.client, self._live_client, self._drain_task = process, client, client, drain
        self._house_flag = house_flag
        self._supervise_task = asyncio.ensure_future(self._supervise(process))
        self._register_shutdown_hook()

    async def _configure_all(self, doors: list["DoorRuntime"]) -> None:
        """Re-`configure` already-attached doors on a fresh process; one failing door does not stop the rest."""
        for door in doors:
            try:
                await door.async_configure()
            except TransportError as err:
                if err.code != "already_configured":    # that one is done: the process already holds this door
                    _LOGGER.exception("portico transport: could not configure %s on the restarted process", door.device_id)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("portico transport: could not configure %s on the restarted process", door.device_id)

    def _register_shutdown_hook(self) -> None:
        """HA stopping is not an unload; this one hook stops the hub (SIGTERM, so the drivers de-register)
        instead of the binary being SIGKILLed with the container. Registered once."""
        if self._stop_unsub is not None:
            return

        async def _stop_on_shutdown(_event: Event) -> None:
            self._stop_unsub = None     # a listen_once is spent once it fires
            await self.async_stop()

        self._stop_unsub = self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _stop_on_shutdown)

    async def _drain_subprocess_output(self, process: asyncio.subprocess.Process) -> None:
        """Continuously read the transport subprocess's stdout/stderr so it never blocks on a full pipe buffer,
        logging each line at debug level so real crash output isn't silently lost either."""
        if process.stdout is None:
            return
        try:
            while True:
                line = await process.stdout.readline()
                if not line:
                    return
                text = line.decode(errors="replace").rstrip()
                if text.startswith(HOUSE_CALL_SUMMARY_PREFIX):
                    # The one counters line a house call writes when it ends (M7 3g): the parity run reads these from
                    # the Home Assistant log, so they are not debug noise.
                    _LOGGER.info("portico transport: %s", text)
                else:
                    _LOGGER.debug("portico transport: %s", text)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - this loop must never take the integration down with it
            _LOGGER.exception("portico transport: error draining subprocess output")

    async def _supervise(self, process: asyncio.subprocess.Process) -> None:
        """Restart the subprocess once if it exits unexpectedly (§4.1) and re-`configure` every attached door.
        Only once per process lifetime, not forever: a binary that keeps crashing needs a human to look at logs,
        not an infinite silent retry loop. A deliberate stop cancels this task (or replaces `self._process`)."""
        returncode = await process.wait()
        async with self._lock:
            if process is not self._process or self._shutdown:
                return
            # Before the respawn: the dead process's calls and device views go now, for every door.
            self._live_client = None
            for door in list(self._doors.values()):
                door.transport_gone()
            self._house_transport_gone()
            if self._restarted:
                _LOGGER.error(
                    "portico transport subprocess exited again (code %s) after its one restart; not restarting. "
                    "Reload a door (or restart Home Assistant) once the cause is fixed", returncode)
                return
            self._restarted = True
            _LOGGER.warning("portico transport subprocess exited unexpectedly (code %s), restarting once", returncode)
            old_client, self.client = self.client, None
            if old_client is not None:
                await old_client.close()
            try:
                await self._start_process()
                if self._shutdown:      # HA began stopping while the restart ran: do not leave the new process behind
                    await self._stop_process()
                    return
                await self._configure_all(list(self._doors.values()))
            except Exception:
                _LOGGER.exception("portico transport: failed to restart subprocess")

    @staticmethod
    async def _kill(process: asyncio.subprocess.Process) -> None:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()

    async def _stop_process(self) -> None:
        """Stop the process and drop the client (after the last detach, or at shutdown). Caller holds the lock
        or is the shutdown path."""
        process, client = self._process, self.client
        self._house_transport_gone()
        task, self._supervise_task = self._supervise_task, None
        drain, self._drain_task = self._drain_task, None
        self._process, self.client, self._live_client = None, None, None
        if task is not None:
            task.cancel()
        if client is not None:
            await client.close()
        if process is not None:
            await self._kill(process)
        if drain is not None:
            drain.cancel()

    async def async_stop(self) -> None:
        """HA is stopping: every door's calls end, the process is told to stop (clean de-register), and the hub
        takes no more doors. Idempotent; a door's later `async_detach` is a no-op."""
        if self._shutdown:
            return
        self._shutdown = True
        if self._stop_unsub is not None:
            self._stop_unsub()
            self._stop_unsub = None
        for door in list(self._doors.values()):
            door.transport_gone()
        self._doors.clear()
        self._call_doors.clear()
        remotes, self._remotes = list(self._remotes.values()), {}
        for link in remotes:
            await link.stop(detach=False)       # the container keeps its registration: HA stopping is not a deconfigure
        await self._stop_process()

    # ---- notifications ---------------------------------------------------------------------------------

    def _handle_notification(self, method: str, params: dict) -> None:
        """Route one notification from the (single) process: media by `call_id` to the provider, everything else
        to the door named by `device_id` or, failing that, the door the `call_id` was learned from. A
        notification no attached door owns is dropped and logged, never given to some other door."""
        if method == "house_leg_state":
            # Not a door's: no device id, and no attached door owns a house call. Routed by call id to the house
            # listeners (the provider's), never to a door.
            if isinstance(params.get("call_id"), str):
                self._emit_house(params)
            return
        if method in ("media_frame", "media_closed"):
            provider = get_provider(self.hass)
            if provider is not None:
                provider.on_media_notification(method, params)
            return
        if method == "call_audio_stats":
            # DEV-811 P3: a call's gate-side counters; by call id, never a door's business
            provider = get_provider(self.hass)
            if provider is not None:
                provider.on_call_audio_stats(params)
            return
        device_id = params.get("device_id")
        call_id = params.get("call_id")
        if not isinstance(call_id, str):
            call_id = None
        if device_id:
            door = self._doors.get(device_id)
        elif call_id:
            owner = self._call_doors.get(call_id)
            door = self._doors.get(owner) if owner is not None else None
        else:
            door = None
        if door is not None and device_id and call_id and method in ("ringing", "call_state_changed"):
            self.note_call(call_id, device_id)
        if door is None:
            level = _LOGGER.warning if method in ("ringing", "driver_error") else _LOGGER.debug
            level("portico transport: dropping %s (device %r, call %r): no attached door owns it",
                  method, device_id, call_id)
            return
        door.handle_notification(method, params)

    # ---- diagnostics -----------------------------------------------------------------------------------

    def diagnostics(self) -> dict[str, Any]:
        """The hub's process state and wheel version for a door's diagnostics download. No secret in it."""
        process = self._process
        if process is None:
            state = "stopped"
        elif process.returncode is None:
            state = "running"
        else:
            state = "exited"
        return {
            "process": state,
            "doors_attached": len(self._doors),
            "restarted_once": self._restarted,
            "wheel_version": self._wheel_version,    # read once at start (executor); diagnostics runs on the loop
            "min_wheel_version": MIN_WHEEL_VERSION,
            "socket": f"{SOCKET_DIR}/{SOCKET_NAME}",
        }
