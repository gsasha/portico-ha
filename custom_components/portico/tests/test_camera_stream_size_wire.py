"""`camera_stream_size.describe_size` against a real loopback server (DEV-766).

Pinned here: one DESCRIBE with no credential is sent; a reply in pieces is read to the end; a 401, a 404, silence, a
broken or short reply, a flood of headers, a refused connection and a server that never answers are all `None` and never
raise; an address with a login, another scheme or a query is not connected to at all.

Real sockets: this file runs in the second pytest session (`-p no:homeassistant -p no:socket`, see scripts/test_offline.sh),
because the Home Assistant plugin refuses every socket.
"""
from __future__ import annotations

import asyncio

import pytest

from ..camera_stream_size import describe_size
from .test_camera_stream_size import SDP_FACADE

# --- the request, against a real local server -----------------------------------------------------------------------


class _Server:
    """A tiny RTSP-ish server on loopback that records what it was sent and answers as told."""

    def __init__(self, reply):
        self.reply = reply
        self.requests: list[bytes] = []
        self.connections = 0
        self._server = None

    async def _handle(self, reader, writer):
        self.connections += 1
        data = await reader.readuntil(b"\r\n\r\n")
        self.requests.append(data)
        if callable(self.reply):
            await self.reply(reader, writer)
        else:
            writer.write(self.reply)
            await writer.drain()
        writer.close()

    async def __aenter__(self):
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        self.url = f"rtsp://127.0.0.1:{self.port}/facade_lower"
        return self

    async def __aexit__(self, *exc):
        self._server.close()
        await self._server.wait_closed()


def _ok(sdp):
    body = sdp.encode()
    return b"RTSP/1.0 200 OK\r\nCSeq: 1\r\nContent-Type: application/sdp\r\nContent-Length: %d\r\n\r\n" % len(body) + body


async def test_a_describe_of_a_real_connection_gives_the_size_and_sends_nothing_secret():
    async with _Server(_ok(SDP_FACADE)) as server:
        assert await describe_size(server.url) == (2560, 1440)
    assert len(server.requests) == 1
    request = server.requests[0].decode()
    assert request.startswith(f"DESCRIBE rtsp://127.0.0.1:{server.port}/facade_lower RTSP/1.0\r\n")
    assert "Authorization" not in request and request.count("\r\n\r\n") == 1


async def test_a_reply_in_two_pieces_is_read_to_the_end():
    async def slow(reader, writer):
        data = _ok(SDP_FACADE)
        writer.write(data[:40])
        await writer.drain()
        await asyncio.sleep(0.05)
        writer.write(data[40:])
        await writer.drain()
    async with _Server(slow) as server:
        assert await describe_size(server.url) == (2560, 1440)


@pytest.mark.parametrize("reply", [
    b"RTSP/1.0 401 Unauthorized\r\nCSeq: 1\r\n\r\n", b"RTSP/1.0 404 Not Found\r\n\r\n", b"", b"garbage with no end",
    b"RTSP/1.0 200 OK\r\nContent-Length: notanumber\r\n\r\n", b"\xff\xfe\x00\x00\r\n\r\n",
    b"RTSP/1.0 200 OK\r\nContent-Length: 500\r\n\r\nv=0\r\nm=video 0 RTP/AVP 96\r\n",
])
async def test_a_refusal_a_broken_or_short_reply_is_none(reply):
    async with _Server(reply) as server:
        assert await describe_size(server.url) is None


async def test_a_server_that_never_answers_is_given_up_on_at_the_timeout():
    async def silent(reader, writer):
        await asyncio.sleep(5)
    async with _Server(silent) as server:
        loop = asyncio.get_running_loop()
        start = loop.time()
        assert await describe_size(server.url, timeout=0.2) is None
        assert loop.time() - start < 2


async def test_a_reply_of_endless_headers_is_cut_off_not_read_forever():
    async def flood(reader, writer):
        for _ in range(200):
            writer.write(b"X-Pad: " + b"a" * 1000 + b"\r\n")
            await writer.drain()
    async with _Server(flood) as server:
        assert await describe_size(server.url, timeout=2) is None


async def test_a_refused_connection_is_none():
    async with _Server(b"") as server:
        url = server.url
    assert await describe_size(url, timeout=1) is None      # the port is closed now


async def test_an_address_with_a_login_is_not_connected_to_at_all():
    async with _Server(_ok(SDP_FACADE)) as server:
        for url in (f"rtsp://user:secret@127.0.0.1:{server.port}/x", f"rtsps://127.0.0.1:{server.port}/x",
                    f"rtsp://127.0.0.1:{server.port}/x?token=1", None, 5):
            assert await describe_size(url) is None
    assert server.connections == 0
