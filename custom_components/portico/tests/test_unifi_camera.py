"""unifi_protect's browser video path: the camera entity asks the transport to
mint the console's RTSPS URL (IPC `stream_url`) instead of returning None, and
the IPC client carries that request. No HA harness and no real console: a stub
client, and a fake Unix-socket server shaped like the Go binary.

Run in the second pytest session (plain pytest, `-p no:homeassistant -p no:socket`).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from types import SimpleNamespace

import pytest

from custom_components.portico.door.entities.camera import PorticoCamera
from custom_components.portico.door.transport import TransportClient, TransportError

pytestmark = pytest.mark.asyncio

TOKEN_URL = "rtsps://nvr.example:7441/SECRETTOKEN?enableSrtp"


class _Client:
    def __init__(self, result=None, exc=None):
        self.result, self.exc, self.asked = result, exc, []

    async def stream_url(self, device_id):
        self.asked.append(device_id)
        if self.exc:
            raise self.exc
        return self.result


def _camera(client, driver="unifi_protect", available=True):
    runtime = SimpleNamespace(client=client, device_id="nvr.example", transport_available=available)
    entry = SimpleNamespace(
        entry_id="e1", runtime_data=runtime,
        data={"driver": driver, "nvr_host": "nvr.example", "nvr_api_key": "KEY", "camera_id": "c"},
    )
    return PorticoCamera(SimpleNamespace(), entry)


async def test_unifi_protect_stream_source_is_the_minted_url():
    client = _Client({"url": TOKEN_URL})
    assert await _camera(client).stream_source() == TOKEN_URL
    assert client.asked == ["nvr.example"]


@pytest.mark.parametrize("result", [{}, {"url": ""}, {"url": "http://evil/x"}, {"url": 5}])
async def test_a_non_rtsps_answer_is_no_stream(result):
    assert await _camera(_Client(result)).stream_source() is None


@pytest.mark.parametrize("exc", [
    TransportError("stream_url_failed", "POST x: HTTP 401"),
    TransportError("not_supported", "this driver has no minted stream URL"),
    ConnectionError("closed"),
])
async def test_a_failed_mint_is_none_and_logs_no_secret(exc, caplog):
    with caplog.at_level(logging.DEBUG):
        assert await _camera(_Client(exc=exc)).stream_source() is None
    assert "KEY" not in caplog.text and "SECRETTOKEN" not in caplog.text


async def test_transport_down_is_none_without_asking():
    client = _Client({"url": TOKEN_URL})
    assert await _camera(client, available=False).stream_source() is None
    assert client.asked == []


async def test_dahua_stream_source_is_unchanged():
    entry = SimpleNamespace(
        entry_id="e", data={"driver": "dahua_sip", "host": "h", "rtsp_username": "u", "rtsp_password": "p"},
    )
    cam = PorticoCamera(SimpleNamespace(), entry)
    assert await cam.stream_source() == "rtsp://u:p@h/cam/realmonitor?channel=1&subtype=1"


async def test_client_sends_stream_url_request():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "f.sock")
        seen = []

        async def handle(reader, writer):
            msg = json.loads(await reader.readline())
            seen.append(msg)
            writer.write((json.dumps({"id": msg["id"], "result": {"url": TOKEN_URL}}) + "\n").encode())
            await writer.drain()

        server = await asyncio.start_unix_server(handle, path=path)
        async with server:
            client = TransportClient(path)
            await client.connect()
            try:
                assert await client.stream_url("nvr.example") == {"url": TOKEN_URL}
            finally:
                await client.close()
        assert seen[0]["method"] == "stream_url"
        assert seen[0]["params"] == {"device_id": "nvr.example"}
