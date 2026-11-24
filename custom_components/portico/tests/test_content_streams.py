"""Stream sizes in the resolved document (the `streams` object of a camera item).

Pinned: a camera item carries `streams: {big?: {width, height}, small?: {width, height}}` for the sizes the server knows,
and no key at all when it knows none (a document and a registry without sizes are what they were); the small entity id
never leaves the server; the build itself never probes (it reads CameraInfo's memory and starts at most one background
refresh per camera per STREAM_SIZE_RETRY); a size that arrives announces a refetch and moves `rev`; a failed refresh keeps
the last size.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from .. import camera_info
from ..camera_info import get_camera_info
from ..content_resolve import resolve_block
from .test_content_elements_store import PHONE, _make_elements, _store_with

pytestmark = pytest.mark.asyncio


def _registry(sizes):
    return SimpleNamespace(door=lambda d: None, fallback_small_entity=lambda e: "camera.gate_sub" if e == "camera.gate" else None,
                           stream_size=lambda e: sizes.get(e))


def _block():
    return {"id": "b", "title": "B", "cameras": [{"id": "g", "label": "G", "entity_id": "camera.gate"},
                                                  {"id": "y", "label": "Y", "entity_id": "camera.yard"}], "actions": []}


def _states():
    return SimpleNamespace(get=lambda e: None)


async def test_the_resolver_carries_both_variants_and_only_the_known_ones():
    reg = _registry({"camera.gate": (2560, 1440), "camera.gate_sub": (640, 480)})
    gate, yard = resolve_block(_block(), states=_states(), registry=reg)["cameras"]
    assert gate["streams"] == {"big": {"width": 2560, "height": 1440}, "small": {"width": 640, "height": 480}}
    assert gate["small"] is True and "gate_sub" not in json.dumps(gate)
    assert "streams" not in yard                                   # nothing known, no key
    only_small = _registry({"camera.gate_sub": (640, 360)})
    assert resolve_block(_block(), states=_states(), registry=only_small)["cameras"][0]["streams"] == {"small": {"width": 640, "height": 360}}


async def test_a_registry_without_sizes_or_one_that_raises_adds_nothing():
    plain = SimpleNamespace(door=lambda d: None, fallback_small_entity=lambda e: None)
    assert all("streams" not in c for c in resolve_block(_block(), states=_states(), registry=plain)["cameras"])

    def boom(e):
        raise RuntimeError("x")
    broken = SimpleNamespace(door=lambda d: None, fallback_small_entity=lambda e: None, stream_size=boom)
    assert all("streams" not in c for c in resolve_block(_block(), states=_states(), registry=broken)["cameras"])


async def test_the_store_learns_sizes_in_the_background_announces_and_moves_rev(hass, hass_storage):
    elements, ids = await _make_elements(hass)
    store, doc = await _store_with(hass, hass_storage, [{"id": "cam-1", "label": "Gate", "entity_id": "camera.gate", "live": True}])
    await store.async_save_document(doc, store.doc_rev)
    await hass.async_block_till_done()
    hass.states.async_set("camera.gate", "idle", {"width": 2560, "height": 1440})
    store._size_asked.clear()                                      # the camera was down at the first ask; ask again
    calls = []
    store.add_home_listener(lambda: calls.append("home"))
    first = store.resolve_document(PHONE)
    assert "streams" not in first["pages"][0]["blocks"][0]["cameras"][0], "the first build never waits for a probe"
    await hass.async_block_till_done()
    assert calls, "a size that arrived announces a refetch"
    second = store.resolve_document(PHONE)
    assert second["rev"] != first["rev"]
    assert second["pages"][0]["blocks"][0]["cameras"][0]["streams"]["big"] == {"width": 2560, "height": 1440}
    calls.clear()
    for _ in range(5):                                             # repeated builds: no new task, no new announce
        store.resolve_document(PHONE)
    await hass.async_block_till_done()
    assert calls == []
    elements.remove_listener(store._on_elements_changed)


async def test_a_failed_refresh_keeps_the_last_size_and_a_dead_camera_is_not_probed(hass):
    info = get_camera_info(hass)
    hass.states.async_set("camera.a", "idle", {"width": 1920, "height": 1080})
    assert await info.async_refresh_sizes(hass, ["camera.a"]) is True
    assert await info.async_refresh_sizes(hass, ["camera.a"]) is False          # unchanged
    hass.states.async_set("camera.a", "unavailable")
    assert await info.async_refresh_sizes(hass, ["camera.a"]) is False
    assert info.known_size("camera.a") == (1920, 1080)
    assert info.known_size("camera.none") is None and info.stream_reads == 0


async def test_an_rtsp_size_comes_from_the_editors_cached_stream_read(hass, monkeypatch):
    info = get_camera_info(hass)
    reads = []

    async def fake(self, hass_, entity_id):
        reads.append(entity_id)
        return (2560, 1440)

    monkeypatch.setattr(camera_info.CameraInfo, "_stream_size", fake)
    hass.states.async_set("camera.rtsp", "idle", {})
    assert await info.async_refresh_sizes(hass, ["camera.rtsp"]) is True
    assert info.known_size("camera.rtsp") == (2560, 1440) and reads == ["camera.rtsp"]


async def test_a_live_camera_with_an_unknown_size_is_asked_again_without_a_document_build(hass, hass_storage, monkeypatch):
    """The small stream's first description failed (go2rtc not up): the store itself asks again a minute later and
    announces the size, instead of waiting for the app to reconnect and build the document."""
    from datetime import timedelta

    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    from .. import content as content_module

    elements, ids = await _make_elements(hass)
    store, doc = await _store_with(hass, hass_storage, [{"id": "cam-1", "label": "Gate", "entity_id": "camera.gate", "live": True}])
    await store.async_save_document(doc, store.doc_rev)
    await hass.async_block_till_done()
    answers = [None, (352, 288)]

    async def fake(self, hass_, entity_id):
        return answers.pop(0)

    monkeypatch.setattr(camera_info.CameraInfo, "_stream_size", fake)
    hass.states.async_set("camera.gate", "idle", {})
    store._size_asked.clear()
    calls = []
    store.add_home_listener(lambda: calls.append("home"))
    assert store.stream_size("camera.gate") is None
    await hass.async_block_till_done()
    assert calls == [] and len(store._size_timers) == 1, "nothing learned, one self-retry armed"
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=content_module.STREAM_SIZE_RETRY_UNKNOWN + 1))
    await hass.async_block_till_done()
    assert calls == ["home"] and store.stream_size("camera.gate") == (352, 288)
    assert store._size_timers == {}
    store.cancel_size_retries()
    elements.remove_listener(store._on_elements_changed)
