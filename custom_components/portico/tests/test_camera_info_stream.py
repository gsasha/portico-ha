"""`portico/cameras/info` row field `stream` (DEV-766): the size of a camera's LIVE stream.

Pinned here: a camera whose entity offers a plain rtsp:// address gets `stream: {width, height}` read from that stream's own
description while `width`/`height` stay the still (what `content/snapshot` returns); a camera with no address, an address
that fails, or a stream that says nothing has no `stream` key at all (the row is exactly the old five fields); a camera whose
size already came from its attributes, and an unavailable one, are not asked; a result is kept 10 minutes and a failure one
minute; a slow stream is given up on at the budget without holding the command or the other cameras; an address with a login
is never contacted and appears in no reply; a Frigate camera gets its main-stream size beside its 640x480 still.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from homeassistant.helpers import entity_registry as er

from .. import camera_info
from .test_camera_info import _info, _rows, cam, clock, png  # noqa: F401 - fixtures by name
from .test_camera_info_frigate import URL, frigate  # noqa: F401 - the fixture, by name
from .test_editor_commands import world  # noqa: F401 - the fixture, by name

MAIN = "rtsp://10.6.0.6:8554/facade_lower"


class FakeStreams:
    """Stands in for `camera.async_get_stream_source` and `camera_info.describe_size`, both counted."""

    def __init__(self):
        self.addresses: dict[str, object] = {}
        self.sizes: dict[str, tuple[int, int] | None] = {}
        self.delay: dict[str, float] = {}
        self.sources: list[str] = []
        self.described: list[str] = []

    async def source(self, hass, entity_id):
        self.sources.append(entity_id)
        value = self.addresses.get(entity_id)
        if isinstance(value, Exception):
            raise value
        return value

    async def describe(self, url, timeout=4.0):
        self.described.append(url)
        if self.delay.get(url):
            await asyncio.sleep(self.delay[url])
        return self.sizes.get(url)


@pytest.fixture
def streams(monkeypatch):
    fake = FakeStreams()
    monkeypatch.setattr("homeassistant.components.camera.async_get_stream_source", fake.source)
    monkeypatch.setattr(camera_info, "describe_size", fake.describe)
    return fake


def _still_camera(world, entity_id="camera.facade_lower"):
    world.hass.states.async_set(entity_id, "idle", {})


async def test_a_stream_size_is_added_and_the_still_size_is_kept(world, hass_ws_client, cam, streams):
    _still_camera(world)
    streams.addresses["camera.facade_lower"] = MAIN
    streams.sizes[MAIN] = (2560, 1440)
    row = _rows(await _info(world, hass_ws_client, ["camera.facade_lower"]))["camera.facade_lower"]
    assert row == {"entity_id": "camera.facade_lower", "width": 640, "height": 360, "fps": None, "source": "still",
                   "stream": {"width": 2560, "height": 1440}}
    assert streams.described == [MAIN]


@pytest.mark.parametrize("how", ["none", "error", "silent"])
async def test_no_address_a_failing_one_or_a_silent_stream_leaves_the_row_as_it_was(world, hass_ws_client, cam, streams, how):
    _still_camera(world)
    if how == "error":
        streams.addresses["camera.facade_lower"] = RuntimeError("no such entity")
    elif how == "silent":
        streams.addresses["camera.facade_lower"] = MAIN       # describe_size answers None
    row = _rows(await _info(world, hass_ws_client, ["camera.facade_lower"]))["camera.facade_lower"]
    assert row == {"entity_id": "camera.facade_lower", "width": 640, "height": 360, "fps": None, "source": "still"}


async def test_a_camera_whose_size_came_from_its_attributes_is_not_asked(world, hass_ws_client, cam, streams):
    world.hass.states.async_set("camera.uni", "idle", {"width": 1600, "height": 1200})
    streams.addresses["camera.uni"] = MAIN
    streams.sizes[MAIN] = (1, 1)
    row = _rows(await _info(world, hass_ws_client, ["camera.uni"]))["camera.uni"]
    assert "stream" not in row and streams.sources == []


async def test_an_unavailable_camera_is_not_asked(world, hass_ws_client, cam, streams):
    world.hass.states.async_set("camera.gone", "unavailable", {})
    row = _rows(await _info(world, hass_ws_client, ["camera.gone"]))["camera.gone"]
    assert row["source"] == "unavailable" and "stream" not in row and streams.sources == []


async def test_a_second_ask_is_served_from_the_cache_until_ten_minutes_are_up(world, hass_ws_client, cam, streams, clock):
    _still_camera(world)
    streams.addresses["camera.facade_lower"] = MAIN
    streams.sizes[MAIN] = (2560, 1440)
    await _info(world, hass_ws_client, ["camera.facade_lower"])
    clock[0] += 599
    again = _rows(await _info(world, hass_ws_client, ["camera.facade_lower"]))["camera.facade_lower"]
    assert again["stream"] == {"width": 2560, "height": 1440} and streams.sources == ["camera.facade_lower"]
    clock[0] += 2
    await _info(world, hass_ws_client, ["camera.facade_lower"])
    assert streams.sources == ["camera.facade_lower"] * 2


async def test_a_stream_that_said_nothing_is_asked_again_after_a_minute_not_before(world, hass_ws_client, cam, streams, clock):
    _still_camera(world)
    streams.addresses["camera.facade_lower"] = MAIN
    await _info(world, hass_ws_client, ["camera.facade_lower"])
    clock[0] += 59
    await _info(world, hass_ws_client, ["camera.facade_lower"])
    assert len(streams.sources) == 1
    streams.sizes[MAIN] = (1280, 720)
    clock[0] += 2
    row = _rows(await _info(world, hass_ws_client, ["camera.facade_lower"]))["camera.facade_lower"]
    assert len(streams.sources) == 2 and row["stream"] == {"width": 1280, "height": 720}


async def test_a_slow_stream_is_given_up_on_without_holding_the_others(world, hass_ws_client, cam, streams, monkeypatch):
    monkeypatch.setattr(camera_info, "STREAM_BUDGET", 0.1)
    for name in ("fast", "slow"):
        _still_camera(world, f"camera.{name}")
        streams.addresses[f"camera.{name}"] = f"rtsp://10.6.0.6:8554/{name}"
        streams.sizes[f"rtsp://10.6.0.6:8554/{name}"] = (1920, 1080)
    streams.delay["rtsp://10.6.0.6:8554/slow"] = 30
    loop = asyncio.get_running_loop()
    start = loop.time()
    rows = _rows(await _info(world, hass_ws_client, ["camera.fast", "camera.slow"]))
    assert loop.time() - start < 5
    assert rows["camera.fast"]["stream"] == {"width": 1920, "height": 1080}
    assert "stream" not in rows["camera.slow"] and rows["camera.slow"]["source"] == "still"


async def test_an_address_with_a_login_is_never_contacted_and_never_in_a_reply(world, hass_ws_client, cam, monkeypatch):
    secret = "rtsp://admin:hunter2@10.6.0.6:8554/facade_lower"
    _still_camera(world)

    async def source(hass, entity_id):
        return secret
    monkeypatch.setattr("homeassistant.components.camera.async_get_stream_source", source)
    resp = await _info(world, hass_ws_client, ["camera.facade_lower"])        # the REAL describe_size: it must refuse
    row = _rows(resp)["camera.facade_lower"]
    assert "stream" not in row
    assert "hunter2" not in json.dumps(resp) and "10.6.0.6" not in json.dumps(resp)


async def test_a_frigate_camera_shows_its_main_stream_beside_its_detect_sized_still(world, hass_ws_client, cam, streams, frigate):
    entry = MockConfigEntry(domain="frigate", data={"url": URL})
    entry.add_to_hass(world.hass)
    er.async_get(world.hass).async_get_or_create("camera", "frigate", "uid-facade_lower", config_entry=entry,
                                                 suggested_object_id="facade_lower")
    world.hass.states.async_set("camera.facade_lower", "recording", {"camera_name": "facade_lower"})
    cam.pictures["camera.facade_lower"] = png(640, 480)
    streams.addresses["camera.facade_lower"] = MAIN
    streams.sizes[MAIN] = (2560, 1440)
    row = _rows(await _info(world, hass_ws_client, ["camera.facade_lower"]))["camera.facade_lower"]
    assert (row["width"], row["height"], row["source"]) == (640, 480, "still")
    assert row["stream"] == {"width": 2560, "height": 1440}
