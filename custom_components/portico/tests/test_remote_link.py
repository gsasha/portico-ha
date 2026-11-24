"""The REMOTE SIP-server door's TCP link (DEV-518 W8b): `TcpTransportClient` and `RemoteLink` against a fake IPC server shaped
like transport-rs/src/ipc/tcp.rs + transport-rs/src/process/standalone.rs, over REAL sockets on loopback only.

Run in the no-HA session with the other real-socket file (scripts/test_offline.sh SESSION2): pytest-socket in the HA harness
blocks real connections. No Home Assistant object is built: the door is a small stand-in with the surface the link uses.
"""
from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace

import pytest

from custom_components.portico.door import transport as transport_module
from custom_components.portico.door.remote import RemoteConfigError, RemoteLink
from custom_components.portico.door.transport import TcpTransportClient, TransportError

pytestmark = pytest.mark.asyncio

TOKEN = "t0ken-0123456789-abcdefghij-XYZ"       # 30 characters
SIP_PASSWORD = "sip-secret-12"
DEVICE = "gate-sip-server:10.6.0.7:5060"
CAPS = {"CanDial": True, "ReportsAvailability": False, "HasRelay": False}
FAST = (0.02, 0.02, 0.05)


class FakeContainer:
    """The container's `-listen` side: auth first, then a tiny verb set. `configure` for the preloaded device is adopted."""

    def __init__(self, token=TOKEN, device=DEVICE, silent=False):
        self.token, self.device, self.silent = token, device, silent
        self.server: asyncio.AbstractServer | None = None
        self.lines: list[dict] = []            # every request line received, in order, across connections
        self.conns: list[asyncio.StreamWriter] = []
        self.auth_ok = 0
        self.auth_bad = 0
        self.port = 0

    async def start(self, port=0):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", port)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def _handle(self, reader, writer):
        self.conns.append(writer)
        try:
            first = await reader.readline()
            if not first or self.silent:
                if self.silent:
                    await reader.read()           # say nothing until the client gives up
                return
            req = json.loads(first)
            self.lines.append(req)
            if req.get("method") != "auth" or req["params"].get("token") != self.token:
                self.auth_bad += 1
                writer.write(json.dumps({"id": req.get("id"), "error": {"code": "unauthorized", "message": "bad token"}}).encode() + b"\n")
                await writer.drain()
                return
            self.auth_ok += 1
            writer.write(json.dumps({"id": req["id"], "result": {"ok": True}}).encode() + b"\n")
            await writer.drain()
            while True:
                line = await reader.readline()
                if not line:
                    return
                req = json.loads(line)
                self.lines.append(req)
                m, p = req["method"], req["params"]
                if m == "configure" and p["config"].get("device_id") == self.device:
                    out = {"id": req["id"], "result": CAPS}
                elif m == "configure":
                    out = {"id": req["id"], "error": {"code": "invalid_config", "message": "no"}}
                elif m == "deconfigure":
                    out = {"id": req["id"], "result": {"ok": True}}
                else:
                    out = {"id": req["id"], "error": {"code": "not_implemented", "message": m}}
                writer.write(json.dumps(out).encode() + b"\n")
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    def push(self, method, params):
        for w in self.conns:
            if not w.is_closing():
                w.write(json.dumps({"method": method, "params": params}).encode() + b"\n")

    async def drop_all(self):
        for w in self.conns:
            w.close()
        self.conns.clear()

    async def stop(self):
        await self.drop_all()
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None

    def methods(self):
        return [r["method"] for r in self.lines]


class FakeDoor:
    """The surface RemoteLink touches on a DoorRuntime."""

    def __init__(self, device_id=DEVICE):
        self.device_id = device_id
        self.transport_available = False
        self.capabilities = None
        self.notifications: list[tuple[str, dict]] = []
        self.gone = 0
        self.link: RemoteLink | None = None

    async def async_configure(self):
        client = self.link.client
        assert client is not None
        self.capabilities = await client.configure("gate_sip_server", {"device_id": self.device_id})
        self.transport_available = True

    def transport_gone(self):
        self.gone += 1
        self.transport_available = False

    def handle_notification(self, method, params):
        self.notifications.append((method, params))


def _link(port, token=TOKEN, door=None, backoff=FAST):
    door = door or FakeDoor()
    data = {"remote_host": "127.0.0.1", "remote_port": port, "remote_token": token}
    link = RemoteLink(SimpleNamespace(), door, data, backoff=backoff)
    door.link = link
    return link, door


async def _until(predicate, timeout=3.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not predicate():
        assert loop.time() < end, "condition not reached"
        await asyncio.sleep(0.01)


@pytest.fixture
async def container():
    c = await FakeContainer().start()
    yield c
    await c.stop()


# ---- the client: auth line first --------------------------------------------------------------------------------------

async def test_remote_client_sends_auth_first_and_then_serves(container):
    client = TcpTransportClient("127.0.0.1", container.port, TOKEN)
    await client.connect()
    assert await client.configure("gate_sip_server", {"device_id": DEVICE}) == CAPS
    assert container.methods() == ["auth", "configure"]
    assert container.lines[0]["params"] == {"token": TOKEN}
    await client.close()
    assert client.closed.is_set()


async def test_remote_client_wrong_token_is_unauthorized_and_closes(container):
    client = TcpTransportClient("127.0.0.1", container.port, "x" * 30)
    with pytest.raises(TransportError) as e:
        await client.connect()
    assert e.value.code == "unauthorized" and "x" * 30 not in str(e.value)
    assert container.auth_bad == 1 and client.closed.is_set()


async def test_remote_client_gives_up_when_the_server_never_answers_auth(monkeypatch):
    monkeypatch.setattr(transport_module, "TCP_AUTH_TIMEOUT", 0.2)
    silent = await FakeContainer(silent=True).start()
    try:
        client = TcpTransportClient("127.0.0.1", silent.port, TOKEN)
        with pytest.raises(asyncio.TimeoutError):
            await client.connect()
        assert client.closed.is_set()
    finally:
        await silent.stop()


async def test_remote_client_refused_connection_raises_oserror():
    c = await FakeContainer().start()
    port = c.port
    await c.stop()
    with pytest.raises(OSError):
        await TcpTransportClient("127.0.0.1", port, TOKEN).connect()


async def test_remote_client_repr_and_errors_hold_no_token(container):
    client = TcpTransportClient("127.0.0.1", container.port, TOKEN)
    assert TOKEN not in repr(client) and TOKEN not in str(vars(client).get("_socket_path"))


# ---- the link ---------------------------------------------------------------------------------------------------------

async def test_remote_link_configures_the_adopted_device_without_any_secret(container):
    link, door = _link(container.port)
    link.start()
    try:
        await _until(lambda: door.transport_available)
        assert door.capabilities == CAPS and link.connected and link.connects == 1
        sent = json.dumps(container.lines[1])
        assert container.methods() == ["auth", "configure"]
        assert container.lines[1]["params"] == {"driver": "gate_sip_server", "config": {"device_id": DEVICE}}
        assert TOKEN not in sent and SIP_PASSWORD not in sent
    finally:
        await link.stop()


async def test_remote_link_availability_follows_the_connection_and_it_reconnects(container):
    link, door = _link(container.port)
    link.start()
    try:
        await _until(lambda: door.transport_available)
        await container.drop_all()
        await _until(lambda: not door.transport_available and door.gone >= 1)       # unavailable the moment it drops
        await _until(lambda: door.transport_available and link.connects == 2)         # then back, configured again
        assert container.methods().count("configure") == 2 and container.methods().count("auth") == 2
    finally:
        await link.stop()


async def test_remote_link_waits_for_a_container_that_is_not_up_yet_and_never_raises():
    c = await FakeContainer().start()
    port = c.port
    await c.stop()
    link, door = _link(port)
    link.start()
    try:
        await asyncio.sleep(0.15)
        assert not door.transport_available and not link.connected and link.last_error in ("ConnectionRefusedError", "OSError")
        c2 = await FakeContainer().start(port)
        try:
            await _until(lambda: door.transport_available)
        finally:
            await c2.stop()
    finally:
        await link.stop()


async def test_remote_link_wrong_token_never_goes_available_and_retries_slowly(container):
    link, door = _link(container.port, token="w" * 30, backoff=(0.02, 0.4))
    link.start()
    try:
        await _until(lambda: container.auth_bad >= 1)
        await asyncio.sleep(0.2)
        assert container.auth_bad == 1                      # the wrong token jumped to the LONGEST delay, no hammering
        assert not door.transport_available and link.last_error == "unauthorized"
    finally:
        await link.stop()


async def test_remote_link_a_failed_configure_is_a_failed_connect(container):
    link, door = _link(container.port, door=FakeDoor("some-other-id"))
    link.start()
    try:
        await _until(lambda: link.last_error == "invalid_config")
        assert not door.transport_available
    finally:
        await link.stop()


async def test_remote_link_routes_notifications_and_drops_a_foreign_device(container):
    link, door = _link(container.port)
    link.start()
    try:
        await _until(lambda: door.transport_available)
        container.push("ringing", {"call_id": "c1", "device_id": DEVICE})
        container.push("ringing", {"call_id": "c2", "device_id": "someone-else"})
        container.push("house_leg_state", {"call_id": "h"})
        container.push("call_state_changed", {"call_id": "c1", "state": "answered"})        # no device id: this link's door
        await _until(lambda: len(door.notifications) == 2)
        assert [m for m, _ in door.notifications] == ["ringing", "call_state_changed"]
    finally:
        await link.stop()


async def test_remote_link_stop_detaches_only(container):
    link, door = _link(container.port)
    link.start()
    await _until(lambda: door.transport_available)
    await link.stop()
    await _until(lambda: "deconfigure" in container.methods())
    assert container.lines[-1]["params"] == {"device_id": DEVICE}
    assert link.client is None and not link.connected


async def test_remote_link_stop_without_detach_sends_nothing(container):
    link, door = _link(container.port)
    link.start()
    await _until(lambda: door.transport_available)
    await link.stop(detach=False)
    assert "deconfigure" not in container.methods()


async def test_remote_link_requests_go_through_the_live_client_only(container):
    link, door = _link(container.port)
    assert link.client is None
    link.start()
    try:
        await _until(lambda: door.transport_available)
        with pytest.raises(TransportError) as e:
            await link.client.dial(DEVICE, "", 0)
        assert e.value.code == "not_implemented"
        await container.drop_all()
        await _until(lambda: link.client is None or not link.connected)
    finally:
        await link.stop()


async def test_remote_link_logs_hold_no_token_or_password(container, caplog):
    caplog.set_level(logging.DEBUG)
    bad, door = _link(container.port, token="w" * 30, backoff=(0.01,))
    bad.start()
    await _until(lambda: container.auth_bad >= 1)
    await bad.stop()
    c = await FakeContainer().start()
    port = c.port
    await c.stop()
    gone, door2 = _link(port, backoff=(0.01,))
    gone.start()
    await asyncio.sleep(0.1)
    await gone.stop()
    ok, door3 = _link(container.port)
    ok.start()
    await _until(lambda: door3.transport_available)
    await container.drop_all()
    await _until(lambda: ok.connects == 2)
    await ok.stop()
    assert caplog.text and TOKEN not in caplog.text and "w" * 30 not in caplog.text and SIP_PASSWORD not in caplog.text
    assert json.dumps(ok.diagnostics()).find(TOKEN) < 0 and "127.0.0.1" not in json.dumps(ok.diagnostics())


async def test_remote_link_refuses_a_bad_entry_at_construction():
    door = FakeDoor()
    for data in ({"remote_host": "8.8.8.8", "remote_port": 9100, "remote_token": TOKEN},
                 {"remote_host": "10.6.0.7", "remote_port": 8091, "remote_token": TOKEN},
                 {"remote_host": "10.6.0.7", "remote_port": 9100, "remote_token": "short"}):
        with pytest.raises(RemoteConfigError) as e:
            RemoteLink(SimpleNamespace(), door, data)
        assert TOKEN not in str(e.value)
