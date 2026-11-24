"""`portico/cameras/info` and camera_info.py: the picture size (and frame rate, when HA knows it) the panel's camera
picker shows beside each camera.

Pinned here: the header parsers (JPEG SOF, PNG IHDR, WebP VP8/VP8L/VP8X) read a size and nothing else and refuse
garbage and truncation without raising; the command is administrators only, serves camera entities only, takes the
size from the entity's attributes when they have one and otherwise from the header of the picture it returns now,
never invents a frame rate, probes an unavailable camera not at all, keeps a result 10 minutes, runs at most 3 probes at
once inside a 5 s per-probe and 15 s per-call budget, and puts no picture bytes, URL or token in a reply or a log.
"""
from __future__ import annotations

import asyncio
import logging
import struct
import types

import pytest
from homeassistant.components.camera import Image

from .. import camera_info
from ..camera_info import image_size
from .test_editor_commands import _client, _send, world  # noqa: F401 - the fixture, by name

# --- picture builders (by hand: the parsers are tested against the formats, not against a library) --------------------


def jpeg(width, height, *, sof=0xC0, app=(), before_sof=b"", precision=8) -> bytes:
    """SOI, the given segments, a DHT, the frame header, a scan header and EOI."""
    out = b"\xff\xd8"
    for payload in app:
        out += b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
    out += before_sof
    out += b"\xff\xc4" + struct.pack(">H", 2 + 17) + b"\x00" + bytes(16)        # DHT: its code C4 is not a frame header
    out += bytes([0xFF, sof]) + struct.pack(">HBHHB", 8 + 3, precision, height, width, 1) + b"\x01\x11\x00"
    out += b"\xff\xda" + struct.pack(">H", 8) + b"\x01\x01\x00\x00\x3f\x00" + b"\x12\x34\xff\x00\x56" + b"\xff\xd9"
    return out


def png(width, height) -> bytes:
    return (b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", width, height)
            + b"\x08\x02\x00\x00\x00" + b"\x00\x00\x00\x00" + b"\x00\x00\x00\x00IEND\xaeB`\x82")


def _riff(chunk: bytes) -> bytes:
    return b"RIFF" + struct.pack("<I", 4 + len(chunk)) + b"WEBP" + chunk


def webp_lossy(width, height, scale=(0, 0)) -> bytes:
    data = b"\x30\x01\x00" + b"\x9d\x01\x2a" + struct.pack("<HH", width | (scale[0] << 14), height | (scale[1] << 14)) + bytes(8)
    return _riff(b"VP8 " + struct.pack("<I", len(data)) + data)


def webp_lossless(width, height) -> bytes:
    bits = (width - 1) | ((height - 1) << 14)
    data = b"\x2f" + struct.pack("<I", bits) + bytes(8)
    return _riff(b"VP8L" + struct.pack("<I", len(data)) + data)


def webp_extended(width, height) -> bytes:
    data = b"\x10\x00\x00\x00" + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    return _riff(b"VP8X" + struct.pack("<I", len(data)) + data + b"ALPH" + bytes(8))


# --- the header parsers ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("sof", [0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF])
def test_a_jpeg_frame_header_of_any_kind_gives_the_size(sof):
    """Baseline (C0), extended (C1), progressive (C2), lossless, differential and arithmetic frames all carry it."""
    assert image_size(jpeg(1600, 1200, sof=sof)) == (1600, 1200)


def test_a_jpeg_dht_dac_and_jpg_markers_are_not_frame_headers():
    for code in (0xC4, 0xC8, 0xCC):
        assert image_size(b"\xff\xd8" + bytes([0xFF, code]) + struct.pack(">H", 6) + bytes(4) + b"\xff\xd9") is None


def test_a_jpeg_exif_thumbnail_does_not_fool_the_parser():
    """A phone's APP1 holds a whole thumbnail JPEG with its own frame header; the segment is skipped by length."""
    thumb = jpeg(160, 120)
    exif = b"Exif\x00\x00" + thumb
    assert image_size(jpeg(1920, 1080, app=[exif])) == (1920, 1080)
    assert image_size(jpeg(1920, 1080, app=[exif, b"JFIF\x00" + bytes(20), b"x" * 5000])) == (1920, 1080)


def test_a_jpeg_with_fill_bytes_and_comments_before_the_frame_header():
    comment = b"\xff\xfe" + struct.pack(">H", 2 + 5) + b"hello"
    assert image_size(jpeg(800, 600, before_sof=comment)) == (800, 600)
    # fill bytes (extra FFs) before a marker are legal
    raw = jpeg(800, 600, before_sof=b"")
    assert image_size(raw[:2] + b"\xff\xff\xff" + raw[2:]) == (800, 600)


def test_a_jpeg_with_the_scan_before_any_frame_header_or_a_zero_height_is_refused():
    assert image_size(b"\xff\xd8\xff\xda" + struct.pack(">H", 8) + bytes(6) + b"\xff\xd9") is None
    assert image_size(jpeg(640, 0)) is None            # height defined later by a DNL marker: not read
    assert image_size(jpeg(0, 480)) is None
    assert image_size(b"\xff\xd8\xff\xd9") is None


def test_a_truncated_jpeg_is_none_at_every_length():
    full = jpeg(1600, 1200, app=[b"Exif\x00\x00" + bytes(30)])
    sof_at = full.index(b"\xff\xc0")
    for cut in range(0, sof_at + 8):                    # anywhere before the whole frame header is in
        assert image_size(full[:cut]) is None, cut
    assert image_size(full[:sof_at + 9]) == (1600, 1200)


def test_a_jpeg_segment_that_lies_about_its_length_is_none_not_an_error():
    assert image_size(b"\xff\xd8\xff\xe1\xff\xff" + bytes(10)) is None
    assert image_size(b"\xff\xd8\xff\xe1\x00\x01" + bytes(10)) is None       # a length under 2


def test_a_png_gives_its_size():
    assert image_size(png(1920, 1080)) == (1920, 1080)
    assert image_size(png(1, 1)) == (1, 1)


def test_a_png_that_is_not_one_is_none():
    assert image_size(png(640, 480)[:23]) is None
    assert image_size(b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IDAT" + bytes(20)) is None      # IHDR must come first
    assert image_size(b"\x89PNG\r\n\x1a\x00" + png(640, 480)[8:]) is None
    assert image_size(png(0, 480)) is None
    assert image_size(png(640, 0)) is None
    assert image_size(png(2 ** 31 - 1, 480)) is None                 # a claim no camera makes


@pytest.mark.parametrize("build, size", [
    (lambda: webp_lossy(1280, 720), (1280, 720)),
    (lambda: webp_lossy(1280, 720, scale=(2, 1)), (1280, 720)),       # the two scale bits are not part of the size
    (lambda: webp_lossless(1280, 720), (1280, 720)),
    (lambda: webp_lossless(16384, 1), (16384, 1)),
    (lambda: webp_extended(1280, 720), (1280, 720)),
    (lambda: webp_extended(16000, 9000), (16000, 9000)),
])
def test_a_webp_of_each_kind_gives_its_size(build, size):
    assert image_size(build()) == size


def test_a_webp_that_is_broken_is_none():
    good = webp_lossy(640, 480)
    assert image_size(good[:25]) is None
    assert image_size(good[:12]) is None
    bad_start = bytearray(good)
    bad_start[23] = 0x00                                              # the 9D 01 2A start code
    assert image_size(bytes(bad_start)) is None
    assert image_size(webp_lossless(64, 64)[:22]) is None
    assert image_size(webp_extended(64, 64)[:25]) is None
    wrong_sig = bytearray(webp_lossless(64, 64))
    wrong_sig[20] = 0x2E
    assert image_size(bytes(wrong_sig)) is None
    assert image_size(b"RIFF\x00\x00\x00\x00WEBPXXXX" + bytes(30)) is None       # a chunk kind we do not read
    assert image_size(b"RIFF\x00\x00\x00\x00WAVEfmt " + bytes(30)) is None


@pytest.mark.parametrize("data", [
    None, b"", b"\x00", b"\xff", b"\xff\xd8", b"GIF89a\x40\x01\xf0\x00", b"BM" + bytes(60), b"<html>not a picture</html>",
    bytes(range(256)) * 4, b"\x89PNG", b"RIFF", b"\xff\xd8\xff", "a string", 12345,
])
def test_garbage_is_none_and_never_raises(data):
    assert image_size(data) is None


def test_bytearray_and_memoryview_are_read_too():
    assert image_size(bytearray(png(30, 40))) == (30, 40)
    assert image_size(memoryview(jpeg(30, 40))) == (30, 40)


# --- the command -------------------------------------------------------------------------------------------------------

SECRET = b"SECRETPIXELS-" * 8


class FakeCamera:
    """Stands in for `homeassistant.components.camera.async_get_image`: a picture per entity, counted."""

    def __init__(self):
        self.pictures: dict[str, bytes | Exception] = {}
        self.calls: list[str] = []
        self.timeouts: list[int] = []
        self.delay = 0.0
        self.running = 0
        self.max_running = 0

    async def __call__(self, hass, entity_id, timeout=10, width=None, height=None):
        self.calls.append(entity_id)
        self.timeouts.append(timeout)
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            picture = self.pictures.get(entity_id, png(640, 360) + SECRET)
            if isinstance(picture, Exception):
                raise picture
            return Image(content_type="image/png", content=picture)
        finally:
            self.running -= 1


@pytest.fixture
def cam(monkeypatch):
    fake = FakeCamera()
    monkeypatch.setattr("homeassistant.components.camera.async_get_image", fake)
    return fake


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(camera_info, "time", types.SimpleNamespace(monotonic=lambda: now[0]))
    return now


async def _info(world, hass_ws_client, ids, who="admin"):
    return await _send(await _client(world, hass_ws_client, who), "portico/cameras/info", entity_ids=ids)


def _rows(resp):
    assert resp["success"], resp
    return {row["entity_id"]: row for row in resp["result"]["cameras"]}


async def test_attributes_with_a_size_are_used_and_nothing_is_probed(world, hass_ws_client, cam):
    world.hass.states.async_set("camera.uni", "idle", {"width": 1600, "height": 1200, "fps": 20, "bitrate": 5000, "channel_id": 0})
    resp = await _info(world, hass_ws_client, ["camera.uni"])
    assert _rows(resp) == {"camera.uni": {"entity_id": "camera.uni", "width": 1600, "height": 1200, "fps": 20, "source": "attributes"}}
    assert cam.calls == []


async def test_attributes_without_a_frame_rate_leave_it_null(world, hass_ws_client, cam):
    world.hass.states.async_set("camera.uni", "idle", {"width": 1600, "height": 1200})
    world.hass.states.async_set("camera.uni2", "idle", {"width": 1600, "height": 1200, "fps": 0})
    world.hass.states.async_set("camera.uni3", "idle", {"width": 1600, "height": 1200, "fps": "twenty"})
    world.hass.states.async_set("camera.uni4", "idle", {"width": 1600, "height": 1200, "fps": 12.5})
    rows = _rows(await _info(world, hass_ws_client, ["camera.uni", "camera.uni2", "camera.uni3", "camera.uni4"]))
    assert [rows[e]["fps"] for e in ("camera.uni", "camera.uni2", "camera.uni3", "camera.uni4")] == [None, None, None, 12.5]
    assert {r["source"] for r in rows.values()} == {"attributes"}
    assert cam.calls == []


async def test_attributes_are_read_afresh_each_time_not_cached(world, hass_ws_client, cam):
    world.hass.states.async_set("camera.uni", "idle", {"width": 1600, "height": 1200})
    assert _rows(await _info(world, hass_ws_client, ["camera.uni"]))["camera.uni"]["width"] == 1600
    world.hass.states.async_set("camera.uni", "idle", {"width": 800, "height": 600, "fps": 5})
    row = _rows(await _info(world, hass_ws_client, ["camera.uni"]))["camera.uni"]
    assert (row["width"], row["height"], row["fps"]) == (800, 600, 5)


@pytest.mark.parametrize("attrs", [
    {"width": "1600", "height": "1200"}, {"width": True, "height": True}, {"width": 0, "height": 1200},
    {"width": 1600}, {"width": -5, "height": 10}, {"width": float("nan"), "height": 100}, {"height": 1200},
    {"width": 10 ** 9, "height": 10 ** 9}, {},
])
async def test_attributes_that_are_not_a_real_size_fall_back_to_the_picture(world, hass_ws_client, cam, attrs):
    world.hass.states.async_set("camera.odd", "idle", attrs)
    row = _rows(await _info(world, hass_ws_client, ["camera.odd"]))["camera.odd"]
    assert (row["width"], row["height"], row["source"]) == (640, 360, "still")
    assert cam.calls == ["camera.odd"]


async def test_a_camera_with_no_size_is_measured_from_the_picture_header_and_has_no_frame_rate(world, hass_ws_client, cam, caplog):
    caplog.set_level(logging.DEBUG)
    world.hass.states.async_set("camera.frigate", "idle", {"camera_name": "gate", "motion_detection": True, "client_id": "x"})
    cam.pictures["camera.frigate"] = jpeg(1920, 1080) + SECRET
    resp = await _info(world, hass_ws_client, ["camera.frigate"])
    assert _rows(resp) == {"camera.frigate": {"entity_id": "camera.frigate", "width": 1920, "height": 1080, "fps": None, "source": "still"}}
    assert cam.calls == ["camera.frigate"]
    assert cam.timeouts == [5], "one probe waits 5 s"
    assert "SECRETPIXELS" not in caplog.text and "SECRETPIXELS" not in str(resp), "the picture is read for its header and dropped"


async def test_the_reply_has_exactly_the_documented_fields_and_no_url_or_token(world, hass_ws_client, cam):
    world.hass.states.async_set("camera.a", "idle", {"access_token": "TOK123", "entity_picture": "/api/camera_proxy/camera.a?token=TOK123",
                                                     "width": 100, "height": 50})
    world.hass.states.async_set("camera.b", "idle", {"access_token": "TOK456", "entity_picture": "/api/camera_proxy/camera.b?token=TOK456"})
    resp = await _info(world, hass_ws_client, ["camera.a", "camera.b"])
    assert resp["success"]
    assert set(resp["result"]) == {"cameras"}
    for row in resp["result"]["cameras"]:
        assert set(row) == {"entity_id", "width", "height", "fps", "source"}
    text = str(resp)
    assert "TOK" not in text and "token" not in text and "http" not in text and "/api" not in text


async def test_an_unavailable_camera_is_nulls_and_is_not_probed(world, hass_ws_client, cam):
    world.hass.states.async_set("camera.dead", "unavailable", {"width": 1600, "height": 1200, "fps": 20})
    world.hass.states.async_set("camera.huh", "unknown", {})
    rows = _rows(await _info(world, hass_ws_client, ["camera.dead", "camera.huh", "camera.nowhere"]))
    for entity in ("camera.dead", "camera.huh", "camera.nowhere"):
        assert rows[entity] == {"entity_id": entity, "width": None, "height": None, "fps": None, "source": "unavailable"}
    assert cam.calls == []


async def test_a_picture_that_cannot_be_read_is_failed_not_an_error(world, hass_ws_client, cam):
    from homeassistant.exceptions import HomeAssistantError

    for name in ("raises", "garbage", "empty", "timeouterr"):
        world.hass.states.async_set(f"camera.{name}", "idle", {})
    cam.pictures["camera.raises"] = HomeAssistantError("Unable to get image")
    cam.pictures["camera.garbage"] = b"<html>login</html>"
    cam.pictures["camera.empty"] = b""
    cam.pictures["camera.timeouterr"] = TimeoutError()
    rows = _rows(await _info(world, hass_ws_client, ["camera.raises", "camera.garbage", "camera.empty", "camera.timeouterr"]))
    for row in rows.values():
        assert (row["width"], row["height"], row["fps"], row["source"]) == (None, None, None, "failed")


async def test_only_camera_entities_are_served_and_one_bad_id_refuses_the_call_before_any_probe(world, hass_ws_client, cam):
    world.hass.states.async_set("camera.ok", "idle", {})
    world.hass.states.async_set("lock.front", "locked", {})
    for bad in ("lock.front", "sensor.frigate_camera_fps", "camera", "camera.", "light.camera.x", "CAMERA.x", " camera.ok", "../camera.ok", "camera.a b", "camera.A", "camera.ok\n", "x"):
        resp = await _info(world, hass_ws_client, ["camera.ok", bad])
        assert resp["success"] is False and resp["error"]["code"] == "invalid_entity", (bad, resp)
    assert cam.calls == [], "nothing is probed when any id is refused"


async def test_the_size_of_the_list_and_the_shape_of_the_ids_are_checked(world, hass_ws_client, cam):
    admin = await _client(world, hass_ws_client)
    for n, body in enumerate(({"entity_ids": []}, {"entity_ids": [f"camera.c{n}" for n in range(21)]}, {"entity_ids": "camera.ok"},
                 {"entity_ids": [5]}, {"entity_ids": [None]}, {"entity_ids": ["camera." + "x" * 300]}, {}), start=1):
        resp = await _send(admin, "portico/cameras/info", msg_id=n, **body)
        assert resp["success"] is False and resp["error"]["code"] == "invalid_format", (body, resp)
    for n in range(20):
        world.hass.states.async_set(f"camera.c{n}", "unavailable", {})
    assert len(_rows(await _send(admin, "portico/cameras/info", msg_id=99, entity_ids=[f"camera.c{n}" for n in range(20)]))) == 20


async def test_a_duplicate_id_is_one_row_in_the_order_asked(world, hass_ws_client, cam):
    for name in "abc":
        world.hass.states.async_set(f"camera.{name}", "idle", {})
    resp = await _info(world, hass_ws_client, ["camera.c", "camera.a", "camera.c", "camera.b", "camera.a"])
    assert [r["entity_id"] for r in resp["result"]["cameras"]] == ["camera.c", "camera.a", "camera.b"]
    assert sorted(cam.calls) == ["camera.a", "camera.b", "camera.c"], "one picture each"


@pytest.mark.parametrize("who", ["phone", "plain", "tablet"])
async def test_only_an_administrator_may_ask(world, hass_ws_client, cam, who):
    world.hass.states.async_set("camera.gate", "idle", {})
    resp = await _info(world, hass_ws_client, ["camera.gate"], who=who)
    assert resp["success"] is False and resp["error"]["code"] == "unauthorized", resp
    assert cam.calls == [], "a refused caller costs the camera nothing"


async def test_a_refused_caller_is_refused_before_the_ids_are_looked_at(world, hass_ws_client, cam):
    resp = await _info(world, hass_ws_client, ["lock.front"], who="plain")
    assert resp["error"]["code"] == "unauthorized"


async def test_a_second_ask_is_served_from_the_cache_without_a_probe(world, hass_ws_client, cam, clock):
    world.hass.states.async_set("camera.frigate", "idle", {})
    first = _rows(await _info(world, hass_ws_client, ["camera.frigate"]))
    clock[0] += 599
    second = _rows(await _info(world, hass_ws_client, ["camera.frigate"]))
    assert first == second and first["camera.frigate"]["source"] == "still"
    assert cam.calls == ["camera.frigate"], "ten minutes of opening the picker cost one picture"


async def test_a_result_older_than_ten_minutes_is_probed_again(world, hass_ws_client, cam, clock):
    world.hass.states.async_set("camera.frigate", "idle", {})
    await _info(world, hass_ws_client, ["camera.frigate"])
    cam.pictures["camera.frigate"] = png(1280, 720)
    clock[0] += 601
    row = _rows(await _info(world, hass_ws_client, ["camera.frigate"]))["camera.frigate"]
    assert (row["width"], row["height"]) == (1280, 720)
    assert cam.calls == ["camera.frigate"] * 2


async def test_only_the_cameras_not_cached_are_probed(world, hass_ws_client, cam, clock):
    for name in "ab":
        world.hass.states.async_set(f"camera.{name}", "idle", {})
    await _info(world, hass_ws_client, ["camera.a"])
    await _info(world, hass_ws_client, ["camera.a", "camera.b"])
    assert cam.calls == ["camera.a", "camera.b"]


async def test_a_failed_probe_is_remembered_for_a_minute_only(world, hass_ws_client, cam, clock):
    world.hass.states.async_set("camera.flaky", "idle", {})
    cam.pictures["camera.flaky"] = RuntimeError("boom")
    assert _rows(await _info(world, hass_ws_client, ["camera.flaky"]))["camera.flaky"]["source"] == "failed"
    clock[0] += 30
    await _info(world, hass_ws_client, ["camera.flaky"])
    assert cam.calls == ["camera.flaky"], "a dead camera is not hammered by every redraw"
    cam.pictures["camera.flaky"] = png(1024, 768)
    clock[0] += 31
    row = _rows(await _info(world, hass_ws_client, ["camera.flaky"]))["camera.flaky"]
    assert (row["width"], row["source"]) == (1024, "still")


async def test_a_camera_that_goes_unavailable_is_not_served_from_the_cache(world, hass_ws_client, cam, clock):
    world.hass.states.async_set("camera.frigate", "idle", {})
    await _info(world, hass_ws_client, ["camera.frigate"])
    world.hass.states.async_set("camera.frigate", "unavailable", {})
    assert _rows(await _info(world, hass_ws_client, ["camera.frigate"]))["camera.frigate"]["source"] == "unavailable"


async def test_no_more_than_three_probes_run_at_once(world, hass_ws_client, cam):
    ids = [f"camera.c{n}" for n in range(12)]
    for entity in ids:
        world.hass.states.async_set(entity, "idle", {})
    cam.delay = 0.03
    rows = _rows(await _info(world, hass_ws_client, ids))
    assert len(rows) == 12 and {r["source"] for r in rows.values()} == {"still"}
    assert sorted(cam.calls) == sorted(ids)
    assert cam.max_running == camera_info.MAX_IN_FLIGHT == 3


async def test_one_slow_camera_is_given_up_on_after_the_probe_timeout(world, hass_ws_client, cam, monkeypatch):
    monkeypatch.setattr(camera_info, "PROBE_TIMEOUT", 0.05)
    world.hass.states.async_set("camera.slow", "idle", {})
    cam.delay = 5
    row = _rows(await _info(world, hass_ws_client, ["camera.slow"]))["camera.slow"]
    assert (row["source"], row["width"]) == ("failed", None)
    assert cam.running == 0, "the abandoned fetch is cancelled"


async def test_the_whole_call_is_cut_off_at_its_budget_and_nothing_unfinished_is_cached(world, hass_ws_client, cam, monkeypatch):
    monkeypatch.setattr(camera_info, "CALL_BUDGET", 0.15)
    ids = [f"camera.c{n}" for n in range(6)]
    for entity in ids:
        world.hass.states.async_set(entity, "idle", {})
    cam.delay = 0.1                                  # 3 at a time, 0.1 s each: the second three do not finish in 0.15 s
    rows = _rows(await _info(world, hass_ws_client, ids))
    assert [rows[e]["source"] for e in ids] == ["still"] * 3 + ["failed"] * 3
    await asyncio.sleep(0.05)
    assert cam.running == 0, "the probes over budget are cancelled"
    cam.delay = 0
    again = _rows(await _info(world, hass_ws_client, ids))
    assert {r["source"] for r in again.values()} == {"still"}, "the cameras that did not finish were not remembered as failed"
    assert cam.calls.count("camera.c0") == 1, "the ones that did finish were remembered"
    assert cam.calls.count("camera.c5") >= 2


async def test_the_cache_holds_no_picture_bytes(world, hass_ws_client, cam):
    world.hass.states.async_set("camera.frigate", "idle", {})
    await _info(world, hass_ws_client, ["camera.frigate"])
    cache = world.hass.data["portico"]["camera_info"]._cache
    assert set(cache) == {"camera.frigate"}
    assert "SECRETPIXELS" not in repr(cache)
    assert set(cache["camera.frigate"].row) == {"entity_id", "width", "height", "fps", "source"}


async def test_the_command_is_in_the_table_for_administrators_only_and_the_app_does_not_send_it():
    from pathlib import Path

    from .. import auth

    assert auth.COMMANDS["portico/cameras/info"] == frozenset({auth.ADMIN})
    app = (Path(__file__).resolve().parents[3] / "contract" / "app_commands_v3.txt").read_text(encoding="utf-8").splitlines()
    assert "portico/cameras/info" not in app
