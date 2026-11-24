"""`portico/camera/webrtc_offer` (frigate_webrtc.py): the panel's live window asks Frigate's go2rtc for the WebRTC answer.

Pinned: administrators only; only `camera.*` ids; a Frigate-integration camera uses its `camera_name`; a generic camera uses
the name in its `rtsp://<frigate host>:8554/<name>` stream source (an `ffmpeg:` prefix ignored; host equal to the Frigate entry's
host or an address it resolves to) and anything else is `not_frigate_stream`; the POST goes to `<url>/api/go2rtc/webrtc?src=<name>`
with `{"type": "offer", "sdp": ...}` and the integration client's auth headers; a bad status, bad body, timeout or no
answer SDP is `frigate_failed`; no URL, SDP or credential in a reply or a log.
"""
from __future__ import annotations

import asyncio
import json
import logging

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from homeassistant.helpers import entity_registry as er

from .. import frigate_webrtc
from ..frigate_webrtc import add_lan_candidates, stream_name_of
from .test_editor_commands import _client, _send, world  # noqa: F401 - the fixture, by name

URL = "http://frigate-secret.invalid:5000"
OFFER = "v=0\r\no=- 1 1 IN IP4 0.0.0.0\r\ns=-\r\nOFFER-SECRET\r\n"
ANSWER = "v=0\r\no=- 2 2 IN IP4 10.6.0.6\r\ns=-\r\nANSWER\r\n"
TOKEN = "Bearer jwt-secret"


class FakeClient:
    async def get_auth_headers(self):
        return {"Authorization": TOKEN}


@pytest.fixture
def source(monkeypatch):
    """Stands in for the camera's stream source, by entity id."""
    table: dict[str, object] = {}

    async def fake(hass, entity_id, *a, **k):
        value = table.get(entity_id)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr("homeassistant.components.camera.async_get_stream_source", fake)
    return table


def _mine(caplog) -> str:
    """What Portico's own loggers wrote (HA's websocket debug log echoes the request itself, which is not ours)."""
    return "\n".join(r.getMessage() for r in caplog.records if "portico" in r.name)


def _frigate(world, url=URL):
    entry = MockConfigEntry(domain="frigate", data={"url": url})
    entry.add_to_hass(world.hass)
    world.hass.data.setdefault("frigate", {})[entry.entry_id] = {"client": FakeClient()}
    return entry


def _camera(world, entity_id, entry=None, attrs=None):
    hass = world.hass
    if entry is not None:
        er.async_get(hass).async_get_or_create("camera", entry.domain, f"uid-{entity_id}", config_entry=entry,
                                               suggested_object_id=entity_id.split(".", 1)[1])
    hass.states.async_set(entity_id, "idle", attrs or {})


async def _offer(world, hass_ws_client, entity_id="camera.doorbell_sub", who="admin"):
    client = await _client(world, hass_ws_client, who)
    return await _send(client, "portico/camera/webrtc_offer", entity_id=entity_id, sdp=OFFER)


def test_stream_name_of_reads_only_a_plain_restream_address():
    assert stream_name_of("rtsp://10.6.0.6:8554/doorbell_sub") == ("10.6.0.6", "doorbell_sub")
    assert stream_name_of("ffmpeg:rtsp://10.6.0.6:8554/doorbell") == ("10.6.0.6", "doorbell")
    for bad in (None, "", 5, "rtsp://10.6.0.6/doorbell", "rtsp://10.6.0.6:554/doorbell", "rtsp://u:p@10.6.0.6:8554/x",
                "rtsp://10.6.0.6:8554/a/b", "rtsp://10.6.0.6:8554/", "rtsp://10.6.0.6:8554/x?y=1", "rtsp://10.6.0.6:8554/x#f",
                "http://10.6.0.6:8554/x", "rtsp://10.6.0.6:8554/../x"):
        assert stream_name_of(bad) is None, bad


MEDIA_ANSWER = ("v=0\r\no=- 2 2 IN IP4 0.0.0.0\r\ns=-\r\nm=video 9 UDP/TLS/RTP/SAVPF 102\r\nc=IN IP4 0.0.0.0\r\na=mid:0\r\n"
                "a=sendonly\r\na=candidate:1 1 udp 2130706429 176.229.106.125 8555 typ host\r\n")


def test_the_frigate_hosts_lan_candidates_are_added_to_a_media_answer():
    out = add_lan_candidates(MEDIA_ANSWER, ["10.6.0.6"])
    lines = out.split("\r\n")
    assert "a=candidate:9000 1 udp 2130706431 10.6.0.6 8555 typ host" in lines
    assert "a=candidate:9001 1 tcp 1671430143 10.6.0.6 8555 typ host tcptype passive" in lines
    assert "a=candidate:1 1 udp 2130706429 176.229.106.125 8555 typ host" in lines, "Frigate's own candidates are kept"
    assert out.endswith("\r\n") and "\n\n" not in out.replace("\r\n", "")
    assert lines.index("a=sendonly") < lines.index("a=candidate:9000 1 udp 2130706431 10.6.0.6 8555 typ host")


def test_lan_candidates_are_not_repeated_and_a_medialess_sdp_is_untouched():
    once = add_lan_candidates(MEDIA_ANSWER, ["10.6.0.6"])
    assert add_lan_candidates(once, ["10.6.0.6"]) == once
    assert add_lan_candidates(MEDIA_ANSWER, []) == MEDIA_ANSWER
    assert add_lan_candidates(ANSWER, ["10.6.0.6"]) == ANSWER
    two = add_lan_candidates(MEDIA_ANSWER, ["10.6.0.6", "10.6.0.7"])
    assert "10.6.0.6 8555" in two and "10.6.0.7 8555" in two


async def test_the_answer_reaches_the_panel_with_the_frigate_hosts_candidate(world, hass_ws_client, aioclient_mock, source):
    _frigate(world, "http://10.6.0.6:5000")
    _camera(world, "camera.doorbell_sub")
    source["camera.doorbell_sub"] = "rtsp://10.6.0.6:8554/doorbell_sub"
    aioclient_mock.post("http://10.6.0.6:5000/api/go2rtc/webrtc?src=doorbell_sub", json={"type": "answer", "sdp": MEDIA_ANSWER})
    resp = await _offer(world, hass_ws_client)
    assert resp["success"], resp
    assert "10.6.0.6 8555 typ host\r\n" in resp["result"]["answer"]


async def test_a_named_frigate_host_adds_the_addresses_it_resolves_to(world, hass_ws_client, aioclient_mock, source, monkeypatch):
    _frigate(world, "https://frigate.example.invalid:8971")
    _camera(world, "camera.doorbell_sub")
    source["camera.doorbell_sub"] = "rtsp://10.6.0.6:8554/doorbell_sub"
    aioclient_mock.post("https://frigate.example.invalid:8971/api/go2rtc/webrtc?src=doorbell_sub", json={"sdp": MEDIA_ANSWER})

    async def getaddrinfo(self, host, port, **kw):
        return [(2, 1, 6, "", ("10.6.0.6", 0)), (2, 1, 6, "", ("127.0.0.1", 0))]

    monkeypatch.setattr(asyncio.get_running_loop().__class__, "getaddrinfo", getaddrinfo)
    resp = await _offer(world, hass_ws_client)
    answer = resp["result"]["answer"]
    assert "10.6.0.6 8555 typ host" in answer and "127.0.0.1" not in answer


async def test_a_generic_camera_on_the_frigate_host_is_answered_by_frigate(world, hass_ws_client, aioclient_mock, source, caplog):
    _frigate(world, "http://10.6.0.6:5000")
    _camera(world, "camera.doorbell_sub")
    source["camera.doorbell_sub"] = "rtsp://10.6.0.6:8554/doorbell_sub"
    aioclient_mock.post("http://10.6.0.6:5000/api/go2rtc/webrtc?src=doorbell_sub", json={"type": "answer", "sdp": ANSWER})
    with caplog.at_level(logging.DEBUG):
        resp = await _offer(world, hass_ws_client)
    assert resp["success"], resp
    assert resp["result"] == {"answer": ANSWER}
    (method, url, body, headers), = aioclient_mock.mock_calls
    assert method.lower() == "post" and str(url) == "http://10.6.0.6:5000/api/go2rtc/webrtc?src=doorbell_sub"
    assert body == {"type": "offer", "sdp": OFFER}
    assert headers["Authorization"] == TOKEN, "the integration client's own headers"
    mine = _mine(caplog)
    assert "OFFER-SECRET" not in mine and "jwt-secret" not in mine and "ANSWER" not in mine


async def test_a_frigate_integration_camera_uses_its_camera_name(world, hass_ws_client, aioclient_mock, source):
    entry = _frigate(world, "https://frigate.example.invalid:8971")
    _camera(world, "camera.doorbell", entry, {"camera_name": "doorbell", "client_id": "frigate"})
    aioclient_mock.post("https://frigate.example.invalid:8971/api/go2rtc/webrtc?src=doorbell", json={"sdp": ANSWER})
    resp = await _offer(world, hass_ws_client, "camera.doorbell")
    assert resp["success"] and resp["result"] == {"answer": ANSWER}
    assert not source, "the stream source was not even asked"


async def test_the_frigate_host_may_be_a_name_that_resolves_to_the_stream_host(world, hass_ws_client, aioclient_mock, source, monkeypatch):
    _frigate(world, "https://frigate.example.invalid:8971")
    _camera(world, "camera.doorbell_sub")
    source["camera.doorbell_sub"] = "rtsp://10.6.0.6:8554/doorbell_sub"
    aioclient_mock.post("https://frigate.example.invalid:8971/api/go2rtc/webrtc?src=doorbell_sub", json={"sdp": ANSWER})

    async def getaddrinfo(self, host, port, **kw):
        assert host == "frigate.example.invalid"
        return [(2, 1, 6, "", ("10.6.0.6", 0))]

    monkeypatch.setattr(asyncio.get_running_loop().__class__, "getaddrinfo", getaddrinfo)
    resp = await _offer(world, hass_ws_client)
    assert resp["success"], resp


@pytest.mark.parametrize("address", [None, "rtsp://192.168.9.9:8554/doorbell_sub", "rtsp://10.6.0.6:554/doorbell_sub",
                                     "http://10.6.0.6:8554/x", "rtsp://10.6.0.6:8554/a/b", RuntimeError("boom")])
async def test_anything_else_is_not_a_frigate_stream(world, hass_ws_client, aioclient_mock, source, monkeypatch, address):
    _frigate(world, "http://10.6.0.6:5000")
    _camera(world, "camera.other")
    source["camera.other"] = address
    resp = await _offer(world, hass_ws_client, "camera.other")
    assert not resp["success"] and resp["error"]["code"] == "not_frigate_stream"
    assert not aioclient_mock.mock_calls, "nothing is sent to anyone"


async def test_no_frigate_at_all_is_not_a_frigate_stream(world, hass_ws_client, aioclient_mock, source):
    _camera(world, "camera.doorbell_sub")
    source["camera.doorbell_sub"] = "rtsp://10.6.0.6:8554/doorbell_sub"
    resp = await _offer(world, hass_ws_client)
    assert not resp["success"] and resp["error"]["code"] == "not_frigate_stream"


async def test_an_unknown_camera_and_a_non_camera_id(world, hass_ws_client, aioclient_mock, source):
    _frigate(world)
    resp = await _offer(world, hass_ws_client, "camera.nope")
    assert resp["error"]["code"] == "not_found"
    resp = await _offer(world, hass_ws_client, "lock.gate")
    assert resp["error"]["code"] == "invalid_entity"
    assert not aioclient_mock.mock_calls


@pytest.mark.parametrize("kwargs", [{"status": 401}, {"status": 500}, {"text": "not json"}, {"json": {"sdp": 5}},
                                    {"json": {"type": "answer"}}, {"json": {"sdp": "garbage"}}, {"exc": asyncio.TimeoutError()},
                                    {"exc": OSError("refused")}])
async def test_a_frigate_failure_is_frigate_failed_and_leaks_nothing(world, hass_ws_client, aioclient_mock, source, caplog, kwargs):
    _frigate(world, "http://10.6.0.6:5000")
    _camera(world, "camera.doorbell_sub")
    source["camera.doorbell_sub"] = "rtsp://10.6.0.6:8554/doorbell_sub"
    exc = kwargs.pop("exc", None)
    if exc is not None:
        aioclient_mock.post("http://10.6.0.6:5000/api/go2rtc/webrtc?src=doorbell_sub", exc=exc)
    else:
        status = kwargs.pop("status", 200)
        aioclient_mock.post("http://10.6.0.6:5000/api/go2rtc/webrtc?src=doorbell_sub", status=status, **kwargs)
    with caplog.at_level(logging.DEBUG):
        resp = await _offer(world, hass_ws_client)
    assert not resp["success"] and resp["error"]["code"] == "frigate_failed", resp
    blob = json.dumps(resp) + _mine(caplog)
    assert "10.6.0.6" not in json.dumps(resp) and "jwt-secret" not in blob and "OFFER-SECRET" not in blob


async def test_a_missing_client_sends_no_credential(world, hass_ws_client, aioclient_mock, source):
    entry = _frigate(world, "http://10.6.0.6:5000")
    world.hass.data["frigate"].pop(entry.entry_id)
    _camera(world, "camera.doorbell_sub")
    source["camera.doorbell_sub"] = "rtsp://10.6.0.6:8554/doorbell_sub"
    aioclient_mock.post("http://10.6.0.6:5000/api/go2rtc/webrtc?src=doorbell_sub", json={"sdp": ANSWER})
    resp = await _offer(world, hass_ws_client)
    assert resp["success"]
    assert "Authorization" not in aioclient_mock.mock_calls[0][3]


@pytest.mark.parametrize("who", ["phone", "plain", "tablet"])
async def test_only_an_administrator_may_ask(world, hass_ws_client, aioclient_mock, source, who):
    _frigate(world)
    resp = await _offer(world, hass_ws_client, who=who)
    assert not resp["success"] and resp["error"]["code"] in ("unauthorized", "forbidden"), resp
    assert not aioclient_mock.mock_calls
