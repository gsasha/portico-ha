"""Tests for transport.py's IPC client against a fake Unix-socket server
shaped exactly like the real Go transport binary's protocol
(internal/ipc, §9.4) — response/notification discriminated by the
presence of "id", concurrent requests correlated by id, and a closed
connection failing every request still pending on it.

Run SEPARATELY from test_signaling.py, not in the same pytest session:
    pytest custom_components/portico/tests/test_transport.py custom_components/portico/tests/test_call_state.py
transport.py has no homeassistant dependency at all, and these tests use
real Unix sockets against a fake server — pytest-homeassistant-custom-
component's bundled pytest-socket plugin blocks real socket connections
by design (correctly, for HA's own test suite), and neither
`--allow-unix-socket` nor the `socket_enabled` fixture was able to
un-block it when this file ran in the SAME session as test_signaling.py
(tried both, confirmed with real failing runs, not assumed). Since this
file needs no HA dependency to begin with, running it in a separate
plain pytest-asyncio session is the real fix, not a workaround.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from unittest.mock import MagicMock

import pytest

from custom_components.portico.door.transport import MEDIA_WRITE_BUFFER_CAP, TransportClient, TransportError

pytestmark = pytest.mark.asyncio


@pytest.fixture
def sock_path():
    with tempfile.TemporaryDirectory() as d:
        yield os.path.join(d, "fake.sock")


async def _fake_server(sock_path, handle_conn):
    if os.path.exists(sock_path):
        os.remove(sock_path)
    return await asyncio.start_unix_server(handle_conn, path=sock_path)


async def test_request_response_and_notification(sock_path):
    received = []

    async def handle_conn(reader, writer):
        while True:
            line = await reader.readline()
            if not line:
                return
            msg = json.loads(line)
            received.append(msg)
            if msg["method"] == "configure":
                resp = {"id": msg["id"], "result": {"DirectCapable": True}}
                writer.write((json.dumps(resp) + "\n").encode())
                # An unrelated notification right after the response, to
                # prove interleaving on the same stream is handled
                # correctly, not just isolated request/response pairs.
                notif = {"method": "ringing", "params": {"call_id": "c1", "device_id": "d1"}}
                writer.write((json.dumps(notif) + "\n").encode())
                await writer.drain()
            elif msg["method"] == "dial":
                resp = {"id": msg["id"], "error": {"code": "dial_failed", "message": "nope"}}
                writer.write((json.dumps(resp) + "\n").encode())
                await writer.drain()

    server = await _fake_server(sock_path, handle_conn)
    async with server:
        notifications = []
        client = TransportClient(sock_path)
        client.on_notification(lambda method, params: notifications.append((method, params)))
        await client.connect()

        caps = await client.configure("dahua_sip", {"host": "1.2.3.4"})
        assert caps == {"DirectCapable": True}

        await asyncio.sleep(0.05)  # let the notification arrive and dispatch
        assert notifications == [("ringing", {"call_id": "c1", "device_id": "d1"})]

        with pytest.raises(TransportError) as exc_info:
            await client.dial("d1", "5.6.7.8", 1234)
        assert exc_info.value.code == "dial_failed"

        await client.close()


async def test_configure_accepts_a_read_only_mapping(sock_path):
    """HA's ConfigEntry.data is a MappingProxyType, not a dict — the real
    __init__.py passes it straight to configure(). json.dumps rejects it
    ("Object of type mappingproxy is not JSON serializable"), which kept the
    live entry from setting up on 2026-10-03 while every test here, using
    plain dicts, stayed green."""
    from types import MappingProxyType

    received = []

    async def handle_conn(reader, writer):
        line = await reader.readline()
        msg = json.loads(line)
        received.append(msg)
        writer.write((json.dumps({"id": msg["id"], "result": {}}) + "\n").encode())
        await writer.drain()

    server = await _fake_server(sock_path, handle_conn)
    async with server:
        client = TransportClient(sock_path)
        await client.connect()
        entry_data = MappingProxyType({"driver": "dahua_sip", "host": "1.2.3.4"})
        await client.configure("dahua_sip", entry_data)
        await client.close()

    assert received[0]["params"]["config"] == {"driver": "dahua_sip", "host": "1.2.3.4"}


async def test_concurrent_requests_correlate_by_id_not_by_order(sock_path):
    async def handle_conn(reader, writer):
        while True:
            line = await reader.readline()
            if not line:
                return
            msg = json.loads(line)
            # Reply out of order (id=1 slower than id=2) to prove
            # correlation is by id, not by send/receive order.
            await asyncio.sleep(0.05 if msg["id"] == 1 else 0.01)
            resp = {"id": msg["id"], "result": {"echo": msg["id"]}}
            writer.write((json.dumps(resp) + "\n").encode())
            await writer.drain()

    server = await _fake_server(sock_path, handle_conn)
    async with server:
        client = TransportClient(sock_path)
        await client.connect()

        r1, r2 = await asyncio.gather(
            client.configure("d1", {}), client.configure("d2", {})
        )
        assert r1 == {"echo": 1}
        assert r2 == {"echo": 2}

        await client.close()


async def test_connection_closed_fails_pending_request(sock_path):
    async def handle_conn(reader, writer):
        line = await reader.readline()
        assert line
        writer.close()  # hang up without ever responding

    server = await _fake_server(sock_path, handle_conn)
    async with server:
        client = TransportClient(sock_path)
        await client.connect()
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(client.configure("d1", {}), timeout=2)
        await client.close()


async def test_deconfigure_sends_the_device_id_and_surfaces_a_refusal(sock_path):
    """The one-hub unload of a single door (P-3): `deconfigure {device_id}`; Go answers unknown_device for a door it
    does not hold."""
    received = []

    async def handle_conn(reader, writer):
        while True:
            line = await reader.readline()
            if not line:
                return
            msg = json.loads(line)
            received.append(msg)
            resp = ({"id": msg["id"], "result": {"ok": True}} if msg["params"]["device_id"] == "known"
                    else {"id": msg["id"], "error": {"code": "unknown_device", "message": "nope"}})
            writer.write((json.dumps(resp) + "\n").encode())
            await writer.drain()

    server = await _fake_server(sock_path, handle_conn)
    async with server:
        client = TransportClient(sock_path)
        await client.connect()
        assert await client.deconfigure("known") == {"ok": True}
        with pytest.raises(TransportError) as exc_info:
            await client.deconfigure("other")
        assert exc_info.value.code == "unknown_device"
        assert [(m["method"], m["params"]) for m in received] == [
            ("deconfigure", {"device_id": "known"}), ("deconfigure", {"device_id": "other"})]
        await client.close()


async def test_uplink_frames_are_dropped_while_the_ipc_buffer_is_over_the_cap(caplog):
    client = TransportClient("/nonexistent")
    client._writer = MagicMock()
    size = client._writer.transport.get_write_buffer_size
    size.return_value = MEDIA_WRITE_BUFFER_CAP + 1
    for _ in range(3):
        client.media_frame_nowait("c1", "QUJD")
    client._writer.write.assert_not_called()
    assert client._next_id == 1
    assert len([r for r in caplog.records if "dropping uplink" in r.getMessage()]) == 1
    size.return_value = MEDIA_WRITE_BUFFER_CAP     # drained to the cap: frames flow again
    client.media_frame_nowait("c1", "QUJD")
    assert client._writer.write.call_count == 1
    size.return_value = MEDIA_WRITE_BUFFER_CAP + 1  # a second stall is a new episode, logged again
    client.media_frame_nowait("c1", "QUJD")
    assert client._writer.write.call_count == 1
    assert len([r for r in caplog.records if "dropping uplink" in r.getMessage()]) == 2
