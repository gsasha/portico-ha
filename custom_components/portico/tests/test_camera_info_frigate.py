"""`portico/cameras/info` for the cameras of the Frigate integration (DEV-663): a measured frame rate from Frigate's
own read-only API, nothing else.

Pinned here: only an entity of a config entry whose domain is `frigate` is looked up; the camera is mapped by its
`camera_name` attribute; `fps` is Frigate's measured `camera_fps` rounded to the nearest 0.5 with
`fps_source: "frigate-stats"`, and `detect` is the configured detect stream; one GET each of `/api/stats` and
`/api/config` per refresh for all cameras, kept 60 s (a failure too); a timeout, a 401, bad JSON, a missing camera or
no Frigate at all leave `fps` null and never raise; the URL and credentials never reach a reply or a log; no request
carries a credential; every request is a GET.
"""
from __future__ import annotations

import json
import logging
import types

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from homeassistant.helpers import entity_registry as er

from .. import camera_info
from ..camera_info import CameraInfo, frigate_cameras
from .test_camera_info import _info, _rows, cam, clock  # noqa: F401 - fixtures by name
from .test_editor_commands import world  # noqa: F401 - the fixture, by name

URL = "http://frigate-secret.invalid:5000"

# Frigate 0.14 / 0.15 shapes (trimmed to what the panel reads)
STATS = {
    "cameras": {
        "gate": {"camera_fps": 14.87, "process_fps": 14.9, "skipped_fps": 0.0, "detection_fps": 0.0},
        "backyard": {"camera_fps": 9.74, "process_fps": 9.7, "skipped_fps": 0.0, "detection_fps": 0.0},
        "sidepath": {"camera_fps": 0.0, "process_fps": 0.0, "skipped_fps": 0.0, "detection_fps": 0.0},
    },
    "detectors": {"cpu1": {"inference_speed": 9.1}},
    "service": {"version": "0.15.0-abcdef"},
}
CONFIG = {
    "cameras": {
        "gate": {"detect": {"enabled": True, "fps": 5, "width": 1280, "height": 720}, "record": {"enabled": True}},
        "backyard": {"detect": {"fps": 10, "width": 640, "height": 480}},
        "sidepath": {"detect": {"fps": 5, "width": 1280, "height": 720}},
        "facade_upper": {"detect": {"fps": 5, "width": 1920, "height": 1080}},
    },
}


class FakeFrigate:
    """Stands in for `CameraInfo._frigate_get`: serves the two documents, or raises what it is told to."""

    def __init__(self):
        self.stats = STATS
        self.config = CONFIG
        self.fail: dict[str, Exception] = {}
        self.calls: list[str] = []

    async def __call__(self, info, hass, entry, path):
        self.calls.append(path)
        if path in self.fail:
            raise self.fail[path]
        return {"/api/stats": self.stats, "/api/config": self.config}[path]


@pytest.fixture
def frigate(monkeypatch):
    fake = FakeFrigate()
    monkeypatch.setattr(CameraInfo, "_frigate_get", lambda self, hass, entry, path: fake(self, hass, entry, path))
    return fake


def _entity(world, domain, entity_id, camera_name=None, *, entry=None, state="idle", extra=None):
    """A camera entity of `entry`'s integration, in the entity registry and in the state machine."""
    hass = world.hass
    object_id = entity_id.split(".", 1)[1]
    hass.states.async_remove(entity_id)            # the `world` fixture already has a state called camera.gate
    if entry is None:
        entry = MockConfigEntry(domain=domain, data={"url": URL})
        entry.add_to_hass(hass)
    er.async_get(hass).async_get_or_create("camera", domain, f"uid-{entity_id}", config_entry=entry,
                                           suggested_object_id=object_id)
    attrs = {"width": 1280, "height": 720, **(extra or {})}
    if camera_name is not None:
        attrs["camera_name"] = camera_name
    hass.states.async_set(entity_id, state, attrs)
    return entry


def _frigate_entry(world):
    entry = MockConfigEntry(domain="frigate", data={"url": URL})
    entry.add_to_hass(world.hass)
    return entry


# --- the mapping ---------------------------------------------------------------------------------------------------


def test_the_two_documents_become_one_map_of_cameras():
    out = frigate_cameras(STATS, CONFIG)
    assert out["gate"] == {"fps": 15, "detect": {"fps": 5, "width": 1280, "height": 720}}
    assert out["backyard"]["fps"] == 9.5
    assert out["sidepath"]["fps"] is None, "a camera Frigate measures at 0 fps has no rate"
    assert out["facade_upper"] == {"fps": None, "detect": {"fps": 5, "width": 1920, "height": 1080}}


@pytest.mark.parametrize("value,expected", [(14.87, 15), (14.7, 14.5), (14.74, 14.5), (14.76, 15), (0.2, None), (0.3, 0.5),
                                            (25, 25), ("25", None), (True, None), (float("nan"), None), (-3, None), (5000, None)])
def test_the_measured_rate_is_rounded_to_half_a_frame(value, expected):
    assert frigate_cameras({"cameras": {"c": {"camera_fps": value}}}, None)["c"]["fps"] == expected


@pytest.mark.parametrize("stats,config", [(None, None), ([], "x"), ({"cameras": []}, {"cameras": 3}), ({"cameras": {"a": 5}}, {"cameras": {"a": None}}),
                                         ({"cameras": {"a": {"camera_fps": "x"}}}, {"cameras": {"a": {"detect": "x"}}})])
def test_a_document_of_another_shape_gives_no_rate_and_no_error(stats, config):
    assert all(v == {"fps": None, "detect": None} for v in frigate_cameras(stats, config).values())


# --- the rows ------------------------------------------------------------------------------------------------------


async def test_a_frigate_camera_gets_the_measured_rate_its_source_and_the_detect_stream(world, hass_ws_client, cam, frigate):
    _entity(world, "frigate", "camera.gate", "gate", entry=_frigate_entry(world))
    row = _rows(await _info(world, hass_ws_client, ["camera.gate"]))["camera.gate"]
    assert row == {"entity_id": "camera.gate", "width": 1280, "height": 720, "fps": 15, "source": "attributes",
                   "fps_source": "frigate-stats", "detect": {"fps": 5, "width": 1280, "height": 720}}


async def test_one_get_each_of_stats_and_config_serves_all_cameras_then_the_cache_serves_a_minute(world, hass_ws_client, cam, frigate, clock):
    entry = _frigate_entry(world)
    for name in ("gate", "backyard", "sidepath"):
        _entity(world, "frigate", f"camera.{name}", name, entry=entry)
    rows = _rows(await _info(world, hass_ws_client, ["camera.gate", "camera.backyard", "camera.sidepath"]))
    assert sorted(frigate.calls) == ["/api/config", "/api/stats"], "two GETs for three cameras"
    assert [rows[f"camera.{n}"]["fps"] for n in ("gate", "backyard", "sidepath")] == [15, 9.5, None]
    clock[0] += 59
    await _info(world, hass_ws_client, ["camera.gate"])
    assert len(frigate.calls) == 2, "within a minute nothing is asked again"
    clock[0] += 2
    await _info(world, hass_ws_client, ["camera.gate"])
    assert len(frigate.calls) == 4, "after a minute both are asked again"


async def test_a_new_rate_shows_after_the_cache_expires(world, hass_ws_client, cam, frigate, clock):
    _entity(world, "frigate", "camera.gate", "gate", entry=_frigate_entry(world))
    assert _rows(await _info(world, hass_ws_client, ["camera.gate"]))["camera.gate"]["fps"] == 15
    frigate.stats = {"cameras": {"gate": {"camera_fps": 20.0}}}
    clock[0] += 61
    assert _rows(await _info(world, hass_ws_client, ["camera.gate"]))["camera.gate"]["fps"] == 20


@pytest.mark.parametrize("failure", [TimeoutError(), ValueError("status 401"), json.JSONDecodeError("bad", "x", 0), OSError("down")])
async def test_a_failing_frigate_leaves_the_rate_null_and_the_size_alone(world, hass_ws_client, cam, frigate, failure):
    _entity(world, "frigate", "camera.gate", "gate", entry=_frigate_entry(world))
    frigate.fail = {"/api/stats": failure, "/api/config": failure}
    resp = await _info(world, hass_ws_client, ["camera.gate"])
    assert _rows(resp)["camera.gate"] == {"entity_id": "camera.gate", "width": 1280, "height": 720, "fps": None, "source": "attributes"}


async def test_stats_failing_but_config_answering_still_gives_the_detect_stream(world, hass_ws_client, cam, frigate):
    _entity(world, "frigate", "camera.gate", "gate", entry=_frigate_entry(world))
    frigate.fail = {"/api/stats": TimeoutError()}
    row = _rows(await _info(world, hass_ws_client, ["camera.gate"]))["camera.gate"]
    assert row["fps"] is None and "fps_source" not in row
    assert row["detect"] == {"fps": 5, "width": 1280, "height": 720}


async def test_a_failure_is_remembered_for_a_minute_too(world, hass_ws_client, cam, frigate, clock):
    _entity(world, "frigate", "camera.gate", "gate", entry=_frigate_entry(world))
    frigate.fail = {"/api/stats": TimeoutError(), "/api/config": TimeoutError()}
    await _info(world, hass_ws_client, ["camera.gate"])
    await _info(world, hass_ws_client, ["camera.gate"])
    assert len(frigate.calls) == 2, "a down Frigate is not asked again at once"


async def test_a_camera_frigate_does_not_know_stays_unknown(world, hass_ws_client, cam, frigate):
    _entity(world, "frigate", "camera.ghost", "ghost", entry=_frigate_entry(world))
    row = _rows(await _info(world, hass_ws_client, ["camera.ghost"]))["camera.ghost"]
    assert row["fps"] is None and "fps_source" not in row and "detect" not in row


async def test_a_frigate_camera_with_no_camera_name_is_left_alone(world, hass_ws_client, cam, frigate):
    _entity(world, "frigate", "camera.gate", None, entry=_frigate_entry(world))
    row = _rows(await _info(world, hass_ws_client, ["camera.gate"]))["camera.gate"]
    assert row["fps"] is None and frigate.calls == []


async def test_the_picture_size_still_comes_from_the_picture_and_the_rate_from_frigate(world, hass_ws_client, cam, frigate):
    world.hass.states.async_remove("camera.gate")
    entry = _frigate_entry(world)
    er.async_get(world.hass).async_get_or_create("camera", "frigate", "uid-x", config_entry=entry, suggested_object_id="gate")
    world.hass.states.async_set("camera.gate", "idle", {"camera_name": "gate"})
    row = _rows(await _info(world, hass_ws_client, ["camera.gate"]))["camera.gate"]
    assert (row["width"], row["height"], row["source"]) == (640, 360, "still")
    assert row["fps"] == 15 and row["fps_source"] == "frigate-stats"


# --- who may use the path ------------------------------------------------------------------------------------------


async def test_another_integrations_camera_never_touches_frigate_even_with_a_camera_name_and_a_url(world, hass_ws_client, cam, frigate):
    _entity(world, "unifiprotect", "camera.gate", "gate", extra={"fps": 25})
    row = _rows(await _info(world, hass_ws_client, ["camera.gate"]))["camera.gate"]
    assert row["fps"] == 25 and "fps_source" not in row
    assert frigate.calls == []


async def test_an_entity_with_no_registry_entry_never_touches_frigate(world, hass_ws_client, cam, frigate):
    _frigate_entry(world)
    world.hass.states.async_set("camera.gate", "idle", {"camera_name": "gate", "width": 10, "height": 10})
    _rows(await _info(world, hass_ws_client, ["camera.gate"]))
    assert frigate.calls == []


async def test_without_the_frigate_integration_nothing_changes_for_any_camera(world, hass_ws_client, cam, frigate):
    world.hass.states.async_set("camera.uni", "idle", {"width": 1600, "height": 1200, "fps": 20})
    resp = await _info(world, hass_ws_client, ["camera.uni", "camera.gate"])
    assert _rows(resp)["camera.uni"] == {"entity_id": "camera.uni", "width": 1600, "height": 1200, "fps": 20, "source": "attributes"}
    assert frigate.calls == []


async def test_an_unavailable_frigate_camera_is_not_looked_up(world, hass_ws_client, cam, frigate):
    _entity(world, "frigate", "camera.gate", "gate", entry=_frigate_entry(world), state="unavailable")
    row = _rows(await _info(world, hass_ws_client, ["camera.gate"]))["camera.gate"]
    assert row["source"] == "unavailable" and row["fps"] is None and frigate.calls == []


# --- no leakage ------------------------------------------------------------------------------------------------------


async def test_the_url_is_in_no_reply_and_no_log(world, hass_ws_client, cam, frigate, caplog):
    caplog.set_level(logging.DEBUG)
    _entity(world, "frigate", "camera.gate", "gate", entry=_frigate_entry(world))
    frigate.fail = {"/api/config": OSError(f"cannot reach {URL}/api/config")}
    resp = await _info(world, hass_ws_client, ["camera.gate"])
    assert "frigate-secret" not in str(resp) and "frigate-secret" not in caplog.text
    assert "5000" not in str(resp)


# --- the real GET, against a fake session ------------------------------------------------------------------------------


class _Response:
    def __init__(self, status, body):
        self.status, self._body = status, body
        self.content = types.SimpleNamespace(read=self._read)

    async def _read(self, n):
        return self._body[:n]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, status=200, body=b"{}"):
        self.requests: list[tuple] = []
        self.status, self.body = status, body

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        return _Response(self.status, self.body)

    def __getattr__(self, name):                 # any other verb would be a bug: fail loudly
        raise AssertionError(f"Frigate must only be read: session.{name}")


def _patch_session(monkeypatch, session):
    seen = []
    monkeypatch.setattr(camera_info, "async_get_clientsession", lambda hass, verify_ssl=True: seen.append(verify_ssl) or session)
    return seen


async def test_the_real_get_reads_the_path_under_the_entry_url_with_no_credential(hass, monkeypatch):
    session = FakeSession(body=json.dumps(STATS).encode())
    verify = _patch_session(monkeypatch, session)
    entry = MockConfigEntry(domain="frigate", data={"url": URL + "/", "validate_ssl": False, "username": "u", "password": "p"})
    assert await CameraInfo()._frigate_get(hass, entry, "/api/stats") == STATS
    assert session.requests == [(URL + "/api/stats", {})], "a GET, no headers, no auth, no params"
    assert verify == [False]


@pytest.mark.parametrize("status", [401, 403, 404, 500])
async def test_the_real_get_refuses_anything_but_200(hass, monkeypatch, status):
    _patch_session(monkeypatch, FakeSession(status=status))
    with pytest.raises(ValueError):
        await CameraInfo()._frigate_get(hass, MockConfigEntry(domain="frigate", data={"url": URL}), "/api/stats")


async def test_the_real_get_refuses_bad_json_an_oversized_body_and_a_missing_url(hass, monkeypatch):
    entry = MockConfigEntry(domain="frigate", data={"url": URL})
    _patch_session(monkeypatch, FakeSession(body=b"<html>"))
    with pytest.raises(ValueError):
        await CameraInfo()._frigate_get(hass, entry, "/api/stats")
    _patch_session(monkeypatch, FakeSession(body=b" " * (camera_info.FRIGATE_MAX_BYTES + 1)))
    with pytest.raises(ValueError):
        await CameraInfo()._frigate_get(hass, entry, "/api/stats")
    with pytest.raises(ValueError):
        await CameraInfo()._frigate_get(hass, MockConfigEntry(domain="frigate", data={}), "/api/stats")


async def test_the_real_get_gives_up_after_its_timeout(hass, monkeypatch):
    import asyncio

    class Slow(FakeSession):
        def get(self, url, **kwargs):
            class Hang:
                async def __aenter__(self_inner):
                    await asyncio.sleep(10)

                async def __aexit__(self_inner, *exc):
                    return False
            return Hang()

    _patch_session(monkeypatch, Slow())
    monkeypatch.setattr(camera_info, "FRIGATE_TIMEOUT", 0.05)
    with pytest.raises(TimeoutError):
        await CameraInfo()._frigate_get(hass, MockConfigEntry(domain="frigate", data={"url": URL}), "/api/stats")


async def test_a_timeout_from_the_real_get_degrades_the_row(world, hass_ws_client, cam, monkeypatch):
    import asyncio

    class Hang:
        async def __aenter__(self):
            await asyncio.sleep(10)

        async def __aexit__(self, *exc):
            return False

    session = FakeSession()
    session.get = lambda url, **kw: Hang()
    _patch_session(monkeypatch, session)
    monkeypatch.setattr(camera_info, "FRIGATE_TIMEOUT", 0.05)
    _entity(world, "frigate", "camera.gate", "gate", entry=_frigate_entry(world))
    row = _rows(await _info(world, hass_ws_client, ["camera.gate"]))["camera.gate"]
    assert row["fps"] is None and row["width"] == 1280
