"""`portico/content/snapshot`: one still from a camera of the caller's OWN profile,
as base64 over the app's existing socket (so over the tailnet too). Same grant
rule as `content/run`: the phone names a camera id, never an entity."""
from __future__ import annotations

import base64
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.camera import Image
from pytest_homeassistant_custom_component.common import async_capture_events

from ..content import MAX_SNAPSHOT_BYTES, get_content_store
from .test_signaling import _add_device, _setup_signaling

pytestmark = pytest.mark.asyncio

JPEG = b"\xff\xd8FAKE-JPEG-BYTES\xff\xd9"
PROFILE = {"cameras": [
    {"id": "gate", "label": "Gate", "entity_id": "camera.gate"},
    {"id": "yard", "label": "Yard", "entity_id": "camera.yard"},
]}


async def _setup(hass, user, name="Alice phone"):
    _add_device(hass, enrolled_phones=[{"ha_user_id": user.id, "name": name}])
    await _setup_signaling(hass)
    hass.states.async_set("camera.gate", "idle")
    hass.states.async_set("camera.yard", "unavailable")
    await get_content_store(hass).async_set(user.id, PROFILE)


async def _snap(client, camera_id, msg_id=1, **extra):
    await client.send_json({"id": msg_id, "type": "portico/content/snapshot", "camera_id": camera_id, **extra})
    return await client.receive_json()


def _image(content=JPEG):
    return AsyncMock(return_value=Image(content_type="image/jpeg", content=content))


async def test_snapshot_returns_the_picture_as_base64_and_is_not_logged(
        hass, hass_ws_client, hass_read_only_user, hass_read_only_access_token):
    await _setup(hass, hass_read_only_user)
    log = async_capture_events(hass, "logbook_entry")
    client = await hass_ws_client(hass, hass_read_only_access_token)
    with patch("homeassistant.components.camera.async_get_image", new=_image()) as get_image:
        resp = await _snap(client, "gate", width=480)
    assert resp["success"], resp
    assert resp["result"]["content_type"] == "image/jpeg"
    assert base64.b64decode(resp["result"]["content"]) == JPEG
    assert get_image.await_args.args[1] == "camera.gate" and get_image.await_args.kwargs["width"] == 480
    await hass.async_block_till_done()
    assert not log  # polled every few seconds: a success is not a logbook line


async def test_snapshot_refuses_a_camera_that_is_not_in_the_profile(
        hass, hass_ws_client, hass_read_only_user, hass_read_only_access_token):
    await _setup(hass, hass_read_only_user)
    log = async_capture_events(hass, "logbook_entry")
    client = await hass_ws_client(hass, hass_read_only_access_token)
    with patch("homeassistant.components.camera.async_get_image", new=_image()) as get_image:
        for n, bad in enumerate(("camera.gate", "front", "../x"), start=1):
            resp = await _snap(client, bad, n)
            assert not resp["success"] and resp["error"]["code"] == "unknown_camera"
    assert not get_image.await_count
    await hass.async_block_till_done()
    assert "refused a snapshot" in log[-1].data["message"]


async def test_snapshot_cannot_use_another_phones_camera(
        hass, hass_ws_client, hass_admin_user, hass_read_only_user, hass_read_only_access_token):
    _add_device(hass, enrolled_phones=[{"ha_user_id": hass_admin_user.id, "name": "Admin phone"},
                                       {"ha_user_id": hass_read_only_user.id, "name": "Other phone"}])
    await _setup_signaling(hass)
    hass.states.async_set("camera.gate", "idle")
    await get_content_store(hass).async_set(hass_admin_user.id, PROFILE)  # only the admin's phone has it
    client = await hass_ws_client(hass, hass_read_only_access_token)
    with patch("homeassistant.components.camera.async_get_image", new=_image()) as get_image:
        resp = await _snap(client, "gate")
    assert not resp["success"] and resp["error"]["code"] == "unknown_camera"
    assert not get_image.await_count


async def test_snapshot_unavailable_camera_and_rate_limit(
        hass, hass_ws_client, hass_read_only_user, hass_read_only_access_token):
    await _setup(hass, hass_read_only_user)
    client = await hass_ws_client(hass, hass_read_only_access_token)
    with patch("homeassistant.components.camera.async_get_image", new=_image()):
        assert (await _snap(client, "yard", 1))["error"]["code"] == "unavailable"
        assert (await _snap(client, "gate", 2))["success"]
        again = await _snap(client, "gate", 3)
    assert not again["success"] and again["error"]["code"] == "rate_limited"


async def test_snapshot_camera_failure_and_oversize_are_refusals(
        hass, hass_ws_client, hass_read_only_user, hass_read_only_access_token):
    await _setup(hass, hass_read_only_user)
    client = await hass_ws_client(hass, hass_read_only_access_token)
    with patch("homeassistant.components.camera.async_get_image", new=AsyncMock(side_effect=TimeoutError("boom"))):
        resp = await _snap(client, "gate", 1)
    assert not resp["success"] and resp["error"]["code"] == "failed" and "boom" not in str(resp)

    get_content_store(hass)._last_run.clear()
    with patch("homeassistant.components.camera.async_get_image", new=_image(b"x" * (MAX_SNAPSHOT_BYTES + 1))):
        resp = await _snap(client, "gate", 2)
    assert not resp["success"] and resp["error"]["code"] == "too_large"


@pytest.mark.parametrize("width", [10, 5000, "wide"])
async def test_snapshot_width_is_checked_by_the_schema(
        hass, hass_ws_client, hass_read_only_user, hass_read_only_access_token, width):
    await _setup(hass, hass_read_only_user)
    client = await hass_ws_client(hass, hass_read_only_access_token)
    resp = await _snap(client, "gate", 1, width=width)
    assert not resp["success"] and resp["error"]["code"] == "invalid_format"


async def test_snapshot_for_a_caller_that_is_not_an_enrolled_phone(hass, hass_ws_client, hass_read_only_access_token):
    _add_device(hass, enrolled_phones=[])
    await _setup_signaling(hass)
    client = await hass_ws_client(hass, hass_read_only_access_token)
    resp = await _snap(client, "gate")
    assert not resp["success"] and resp["error"]["code"] == "unauthorized"
