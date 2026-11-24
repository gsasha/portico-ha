"""The Unix-socket JSON-RPC client to the transport binary (Rust, transport-rs/).

Design doc §9.4. A Unix domain socket under HA's own runtime storage path
(the hub's one socket under `.storage/portico_intercom/`), newline-delimited
JSON-RPC 2.0. Requests get a response; events are notifications (no `id`).

Python -> Go (requests): configure, deconfigure, dial, open_relay, answer, keepalive,
    hangup, webrtc_offer/answer/ice.
Go -> Python (notifications): ringing, call_state_changed,
    driver_availability {device_id, available, reason}, driver_error,
    capabilities_changed {device_id, capabilities}
    (the whole set again, e.g. CanDial following the station's registration; an older
    Python part that does not know it logs it as unhandled).

The Go binary is the source of truth for call state it's a party to; for a
`direct`-mode call, the app itself is the source of truth and reports back
over the signaling API (signaling.py, §9.5), not this channel.

Wire shapes here match transport/cmd/portico-transport/main.go exactly —
this is a client for a specific, already-built server, not a general
JSON-RPC library:
  - configure(driver, config) -> Capabilities dict
  - dial/answer(device_id, host, port) -> {"ok": true}
  - open_relay(device_id) -> {"ok": true}
  - keepalive -> always "not_implemented" (§9.5, no per-call
    keepalive-timeout tracking exists in the Go binary yet) — calling it is
    not a bug, getting TransportError back is expected.
  - hangup -> "not_implemented" too, except for a call a driver answered
    itself (gate_sip_server, the talk driver), which that driver ends; a call the driver already released
    (the app closes its audio leg before it sends the hangup) is answered the same way, and the provider
    treats that as success when the tracker no longer has the call live.
  - answer may fail with code "already_answered" (somebody else has the call).
  - answer may fail with code "claims_disabled" (a ring-only door: nothing to answer).
  - stream_url(device_id) -> {"url": <rtsps URL with the console's per-camera
    token>}; a driver without a minted URL answers "not_supported"
  - webrtc_offer(call_id, sdp) -> {"sdp": <answer>}
  - webrtc_answer(call_id, sdp) -> {"ok": true}
  - webrtc_ice(call_id, candidate) -> {"ok": true}
  - media_open(call_id, codec) -> {codec, frame_bytes, frame_ms}; media_frame(call_id, d);
    media_close(call_id, reason): away call audio (away_media.py). Go -> Python notifications
    media_frame {call_id, d} and media_closed {call_id, reason}.
  - house_open(call_id, legs) -> {session, legs}; house_close(call_id) -> {ok, closed}: the house-call bridge
    (M7 3b-S; wheel 0.6.0 started with -house-calls only: the hub, not this class, decides whether to send them).
    Go -> Python notification house_leg_state {call_id, leg, state, reason?, sdp?}; a frame-channel leg's audio
    rides media_frame / media_close / media_closed under its own `house-...` leg id.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Uplink back-pressure (HA-L7): media_frame_nowait never awaits drain(), so a stalled Go reader would
# let the StreamWriter buffer grow for as long as the call lives. Past this many buffered bytes new
# frames are dropped (a late frame is worthless audio). A frame line is a few hundred bytes at
# 25 frames/s, so 4 KiB is on the order of 0.3-0.5 s of audio.
MEDIA_WRITE_BUFFER_CAP = 4096

NotificationHandler = Callable[[str, dict[str, Any]], Awaitable[None] | None]


class TransportError(Exception):
    """An error response from the transport binary (ipc.Error, §9.4)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class TransportClient:
    """Owns the Unix-socket connection to the transport subprocess.

    One connection at a time, matching the Go server's own contract
    (internal/ipc.Server accepts a single connection, §9.4) — this
    class is not meant to be shared across multiple concurrent logical
    clients, only across the one integration instance that owns it.
    """

    def __init__(self, socket_path: str) -> None:
        self._socket_path = socket_path
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._next_id = 1
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._read_task: asyncio.Task[None] | None = None
        self._media_dropping = False
        self._notification_handlers: list[NotificationHandler] = []
        # Set when the connection is over (EOF, read error, or close()): a supervisor of a connection it did not
        # spawn (the remote link, remote.py) waits on it instead of on a process.
        self.closed = asyncio.Event()

    def on_notification(self, handler: NotificationHandler) -> None:
        """Register a callback for every unsolicited Go -> Python message
        (ringing, call_state_changed, driver_availability, driver_error,
        §9.4). Handlers run
        in arrival order; an exception in one doesn't stop the others
        (logged, not raised) — a bad handler must not break every other
        consumer of the same notification stream."""
        self._notification_handlers.append(handler)

    async def connect(self) -> None:
        """Open the connection and start the background reader.

        Raises whatever asyncio.open_unix_connection raises (FileNotFoundError
        if the transport binary hasn't created the socket yet, ConnectionRefusedError
        if it crashed after creating it but before accepting) — the caller
        (__init__.py's subprocess lifecycle) decides how to retry, this
        class doesn't invent its own retry loop."""
        self._reader, self._writer = await asyncio.open_unix_connection(self._socket_path)
        self._read_task = asyncio.ensure_future(self._read_loop())

    async def close(self) -> None:
        if self._read_task is not None:
            self._read_task.cancel()
            self._read_task = None
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:  # noqa: BLE001 - best-effort close, never fail teardown on it
                pass
            self._writer = None
        self._reader = None
        self.closed.set()
        # Any request still waiting for a response when the connection
        # closes will never get one — fail them explicitly rather than
        # leaving their awaiters hung forever.
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("portico transport: connection closed"))
        self._pending.clear()

    async def _read_loop(self) -> None:
        assert self._reader is not None
        try:
            while True:
                line = await self._reader.readline()
                if not line:
                    break  # EOF - the subprocess closed the connection or died
                self._handle_line(line)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a malformed line must not kill the whole reader
            _LOGGER.exception("portico transport: error in IPC read loop")
        finally:
            self.closed.set()
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("portico transport: connection closed"))
            self._pending.clear()

    def _handle_line(self, line: bytes) -> None:
        try:
            msg = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            _LOGGER.warning("portico transport: dropping malformed IPC line: %r", line)
            return

        # Discriminator matches the Go side exactly: a Response always
        # carries "id" (internal/ipc.Response); a Notification never
        # does (internal/ipc.Notification only has "method"/"params").
        if "id" in msg:
            fut = self._pending.pop(msg["id"], None)
            if fut is None or fut.done():
                return  # stale/duplicate response - nothing waiting on it
            if msg.get("error") is not None:
                fut.set_exception(TransportError(msg["error"]["code"], msg["error"]["message"]))
            else:
                fut.set_result(msg.get("result") or {})
            return

        method = msg.get("method")
        if not method:
            _LOGGER.warning("portico transport: IPC line has neither id nor method: %r", line)
            return
        params = msg.get("params") or {}
        for handler in self._notification_handlers:
            try:
                result = handler(method, params)
                if result is not None:
                    asyncio.ensure_future(result)
            except Exception:  # noqa: BLE001 - one bad handler must not break the others
                _LOGGER.exception("portico transport: notification handler failed for %s", method)

    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._writer is None:
            raise ConnectionError("portico transport: not connected")
        request_id = self._next_id
        self._next_id += 1
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_event_loop().create_future()
        self._pending[request_id] = fut
        raw = json.dumps({"id": request_id, "method": method, "params": params})
        self._writer.write(raw.encode() + b"\n")
        await self._writer.drain()
        return await fut

    async def configure(self, driver: str, config: Mapping[str, Any]) -> dict[str, Any]:
        """Push a driver's config-entry data; returns its Capabilities (§4.2)."""
        # ConfigEntry.data is a read-only MappingProxyType, which json.dumps rejects.
        return await self._request("configure", {"driver": driver, "config": dict(config)})

    async def deconfigure(self, device_id: str) -> dict[str, Any]:
        """Stop and forget one driver without ending the process (the one hub's unload of a single door)."""
        return await self._request("deconfigure", {"device_id": device_id})

    async def dial(self, device_id: str, host: str, port: int) -> dict[str, Any]:
        return await self._request("dial", {"device_id": device_id, "host": host, "port": port})

    async def answer(self, device_id: str, host: str, port: int) -> dict[str, Any]:
        return await self._request("answer", {"device_id": device_id, "host": host, "port": port})

    async def open_relay(self, device_id: str) -> dict[str, Any]:
        return await self._request("open_relay", {"device_id": device_id})

    async def stream_url(self, device_id: str) -> dict[str, Any]:
        return await self._request("stream_url", {"device_id": device_id})

    async def hangup(self, call_id: str, reason: str) -> dict[str, Any]:
        return await self._request("hangup", {"call_id": call_id, "reason": reason})

    async def external_ring(self, device_id: str, call_id: str, hold: float) -> dict[str, Any]:
        """A ring that Python owns (ha_event): tell the Go `unifi_protect_talk` driver "call X is ringing" so it can be
        answered by id. Announces nothing to the phones."""
        return await self._request("external_ring", {"device_id": device_id, "call_id": call_id, "hold": hold})

    async def talk_health(self, device_id: str) -> dict[str, Any]:
        """Read-only: the talk door's diagnostics block (session_up, bytes_to_camera, frames_from_camera, drops_up,
        drops_down, last_ffmpeg, last_end)."""
        return await self._request("talk_health", {"device_id": device_id})

    async def keepalive(self, call_id: str) -> dict[str, Any]:
        return await self._request("keepalive", {"call_id": call_id})

    async def media_open(self, call_id: str, codec: str, *, stats: bool = False) -> dict[str, Any]:
        """Away call audio (away_media.py): open the phone's frame leg; -> {codec, frame_bytes, frame_ms}. `stats` asks the
        wheel for the leg's counters in `media_closed` (DEV-811 P3); the key is sent only when true, so every other call is
        the request of old."""
        params: dict[str, Any] = {"call_id": call_id, "codec": codec}
        if stats:
            params["stats"] = True
        return await self._request("media_open", params)

    async def media_close(self, call_id: str, reason: str) -> dict[str, Any]:
        return await self._request("media_close", {"call_id": call_id, "reason": reason})

    def media_frame_nowait(self, call_id: str, data: str) -> None:
        """One uplink frame (base64). Written at once, in call order, and never awaited: Go's reply
        carries an id nothing waits on and is dropped as stale by _handle_line. Frames are dropped while
        the socket's write buffer is over MEDIA_WRITE_BUFFER_CAP; one log line per episode."""
        if self._writer is None:
            raise ConnectionError("portico transport: not connected")
        if self._writer.transport.get_write_buffer_size() > MEDIA_WRITE_BUFFER_CAP:
            if not self._media_dropping:
                self._media_dropping = True
                _LOGGER.warning("portico transport: IPC socket not draining, dropping uplink media frames")
            return
        self._media_dropping = False
        request_id = self._next_id
        self._next_id += 1
        raw = json.dumps({"id": request_id, "method": "media_frame", "params": {"call_id": call_id, "d": data}})
        self._writer.write(raw.encode() + b"\n")

    async def house_open(self, call_id: str, legs: list[dict[str, Any]]) -> dict[str, Any]:
        """Open a house call's two-leg bridge (wheel 0.6.0 with -house-calls; hub.py guards it)."""
        return await self._request("house_open", {"call_id": call_id, "legs": legs})

    async def house_close(self, call_id: str) -> dict[str, Any]:
        """Close a house call's bridge; idempotent on the Go side."""
        return await self._request("house_close", {"call_id": call_id})

    async def webrtc_offer(self, call_id: str, sdp: str) -> dict[str, Any]:
        return await self._request("webrtc_offer", {"call_id": call_id, "sdp": sdp})

    async def webrtc_answer(self, call_id: str, sdp: str) -> dict[str, Any]:
        return await self._request("webrtc_answer", {"call_id": call_id, "sdp": sdp})

    async def webrtc_ice(self, call_id: str, candidate: str) -> dict[str, Any]:
        return await self._request("webrtc_ice", {"call_id": call_id, "candidate": candidate})


# How long a TCP connect may take, and the window the Go side gives the auth line (ipc/tcp.go authWindow).
TCP_CONNECT_TIMEOUT = 5.0
TCP_AUTH_TIMEOUT = 5.0


class TcpTransportClient(TransportClient):
    """The same IPC over TCP to a `portico-transport` container (`-listen`, DEV-518 W8/W8b). The first line sent is
    `{"id":N,"method":"auth","params":{"token":...}}`; the server answers it before serving anything else and drops the
    connection on a wrong token or after 5 s of silence. Nothing else differs from the Unix-socket client.

    The token is never logged, never in an exception message, never in `repr`."""

    def __init__(self, host: str, port: int, token: str) -> None:
        super().__init__(f"tcp://{host}:{port}")
        self._host, self._port, self._token = host, port, token

    def __repr__(self) -> str:
        return f"<TcpTransportClient {self._host}:{self._port}>"

    async def connect(self) -> None:
        """Open the connection, start the reader and authenticate. Raises OSError / TimeoutError for a connection that
        cannot be made, TransportError('unauthorized') for a refused token, ConnectionError when the server hangs up
        instead of answering; on any failure the connection is closed again."""
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self._host, self._port), TCP_CONNECT_TIMEOUT)
        self._read_task = asyncio.ensure_future(self._read_loop())
        try:
            await asyncio.wait_for(self._request("auth", {"token": self._token}), TCP_AUTH_TIMEOUT)
        except BaseException:
            await self.close()
            raise
