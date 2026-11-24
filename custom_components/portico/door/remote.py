"""One remote SIP-server door's link to its `portico-transport` container (DEV-518 W8b).

The hub (hub.py) owns the in-process transport that every ordinary door shares. A gate_sip_server entry in REMOTE mode
(remote_cfg.py) does not use it: this link keeps ONE TCP IPC connection to the container's `-listen` port for THAT door,
and nothing else. Per connection:

  1. open TCP, send the `auth` line first (the server drops a connection that does not within 5 s);
  2. `configure {driver, config: {device_id}}`: the container ADOPTS the SIP server it started from its own environment
     and replies with its capabilities (`CanDial` ...). The entry's SIP password never travels, and the container ignores
     any config sent here;
  3. the door is available for exactly as long as that connection holds. On a drop (container restart, network blip)
     the door's calls end, its entities go unavailable, and the link reconnects with a growing delay (1 s to 30 s) and
     configures again.

A ring or a call state change while the link is down is NOT replayed (the container has nobody to write to; the house still
rings from the doorbell-button automation in HA, which does not depend on this link). A wrong token is not retried quickly:
it retries at the longest delay and says so once per outage; changing the token in the entry reloads it.

Nothing here logs the token or puts it in an exception message.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from .remote_cfg import CONF_HOST, CONF_PORT, CONF_TOKEN, SERVER_DRIVER, check_remote, normalize_host
from .transport import TcpTransportClient, TransportClient, TransportError

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .door import DoorRuntime

_LOGGER = logging.getLogger(__name__)

BACKOFF = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)
_DECONFIGURE_TIMEOUT = 5.0


class RemoteConfigError(ValueError):
    """The entry's remote fields are not acceptable (hand-edited data; the form refuses these). Names the field only."""


class RemoteLink:
    """The connection of one remote door. `client` is the live authenticated client, or None while it is down."""

    def __init__(self, hass: "HomeAssistant", door: "DoorRuntime", data: Any, *,
                 client_factory: Callable[[str, int, str], TransportClient] | None = None,
                 backoff: tuple[float, ...] = BACKOFF) -> None:
        problem = check_remote(data.get(CONF_HOST), data.get(CONF_PORT), data.get(CONF_TOKEN))
        if problem is not None:
            raise RemoteConfigError(f"{problem[0]}: {problem[1]}")
        self.hass = hass
        self.door = door
        self.host = normalize_host(data[CONF_HOST])
        self.port = int(data[CONF_PORT])
        self._token = data[CONF_TOKEN]
        self._factory = client_factory
        self._backoff = backoff
        self.client: TransportClient | None = None
        self.connected = False
        self.connects = 0                  # successful connections (diagnostics)
        self.last_error: str | None = None  # an error CLASS or TransportError code, never a message with data in it
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    # ---- lifecycle -------------------------------------------------------------------------------------

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.ensure_future(self._run())

    async def stop(self, *, detach: bool = True) -> None:
        """End the link. `detach` sends `deconfigure` first (the container only detaches: the SIP registration stays)."""
        self._stopping = True
        task, self._task = self._task, None
        client = self.client
        if detach and client is not None and self.connected:
            try:
                await asyncio.wait_for(client.deconfigure(self.door.device_id), _DECONFIGURE_TIMEOUT)
            except Exception as err:  # noqa: BLE001 - the link is going away either way
                _LOGGER.debug("portico remote: deconfigure of %s failed: %s", self.door.device_id, type(err).__name__)
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self.client, self.connected = None, False
        if client is not None:
            await client.close()

    # ---- the connection loop ---------------------------------------------------------------------------

    async def _run(self) -> None:
        attempt = 0
        reported = False
        while not self._stopping:
            client = (self._factory or TcpTransportClient)(self.host, self.port, self._token)
            client.on_notification(lambda method, params, c=client: self._on_notification(c, method, params))
            was_up = False
            try:
                await client.connect()
                self.client = client
                await self.door.async_configure()      # uses door.client -> this link's client; sets capabilities, available
                self.connected, was_up = True, True
                self.connects += 1
                self.last_error = None
                attempt, reported = 0, False
                _LOGGER.info("portico: remote SIP server %s connected (%s:%s)", self.door.device_id, self.host, self.port)
                await client.closed.wait()
                _LOGGER.warning("portico: remote SIP server %s: the connection was lost; reconnecting", self.door.device_id)
            except asyncio.CancelledError:
                raise
            except TransportError as err:
                self.last_error = err.code
                if err.code == "unauthorized":
                    attempt = len(self._backoff)       # a wrong token does not heal by retrying fast
                    level, text = _LOGGER.error, "the container refused the token"
                else:
                    level, text = _LOGGER.warning, f"configure failed ({err.code})"
                (level if not reported else _LOGGER.debug)(
                    "portico: remote SIP server %s (%s:%s): %s", self.door.device_id, self.host, self.port, text)
                reported = True
            except (OSError, asyncio.TimeoutError, ConnectionError) as err:
                self.last_error = type(err).__name__
                (_LOGGER.warning if not reported else _LOGGER.debug)(
                    "portico: remote SIP server %s (%s:%s) is not reachable (%s); retrying",
                    self.door.device_id, self.host, self.port, type(err).__name__)
                reported = True
            finally:
                self.connected = False
                self.client = None
                if self.door.transport_available or was_up:
                    self.door.transport_gone()        # calls end, entities unavailable: availability IS the connection
                try:
                    await asyncio.shield(client.close())
                except Exception:  # noqa: BLE001
                    pass
            if self._stopping:
                return
            delay = self._backoff[min(attempt, len(self._backoff) - 1)]
            attempt += 1
            await asyncio.sleep(delay)

    # ---- notifications ---------------------------------------------------------------------------------

    def _on_notification(self, client: TransportClient, method: str, params: dict[str, Any]) -> None:
        """Route one notification from the container to the link's one door. A stopped client's are dropped."""
        if client is not self.client:
            return
        if method in ("media_frame", "media_closed"):
            from .provider_impl import get_provider

            provider = get_provider(self.hass)
            if provider is not None:
                provider.on_media_notification(method, params)
            return
        if method == "call_audio_stats":
            from .provider_impl import get_provider

            provider = get_provider(self.hass)
            if provider is not None:
                provider.on_call_audio_stats(params)
            return
        if method == "house_leg_state":
            return          # a remote container runs no house bridge
        device_id = params.get("device_id")
        if device_id not in (None, "", self.door.device_id):
            _LOGGER.warning("portico remote: dropping %s for device %r on door %r", method, device_id, self.door.device_id)
            return
        self.door.handle_notification(method, params)

    # ---- diagnostics -----------------------------------------------------------------------------------

    def diagnostics(self) -> dict[str, Any]:
        """No host, no token: connection facts only."""
        return {"mode": "remote", "connected": self.connected, "connects": self.connects, "last_error": self.last_error}
