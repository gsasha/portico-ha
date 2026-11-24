"""`sensor.portico_live_sessions` (DEV-805): the live count, a diagnostic that writes only when a session opens or closes."""
from __future__ import annotations

import pytest
from homeassistant.const import EntityCategory
from homeassistant.helpers import entity_registry as er

from .. import live
from .test_push_registry import (  # noqa: F401 - fixtures by name
    _custom_integrations,
    _setup_all,
    fake_transport,
    google,
)

pytestmark = pytest.mark.asyncio

SENSOR = "sensor.portico_live_sessions"


def _fake(sid, endpoint_id, reg):
    session = live.LiveSession(id=sid, endpoint_id=endpoint_id, connection=None, msg_id=1, stream="main",
                               registry=reg, camera_id="gate")
    reg.sessions[sid] = session
    return session


async def test_the_sensor_counts_sessions_in_all_and_per_endpoint(hass, hass_storage, google, fake_transport):  # noqa: F811
    await _setup_all(hass, hass_storage, google)
    state = hass.states.get(SENSOR)
    assert state is not None and state.state == "0"
    assert state.attributes["limit_per_endpoint"] == live.MAX_LIVE_PER_ENDPOINT
    reg = live.registry(hass)
    a, b = _fake("a", "ep-1", reg), _fake("b", "ep-2", reg)
    _fake("c", "ep-1", reg)
    reg.changed()
    await hass.async_block_till_done()
    state = hass.states.get(SENSOR)
    assert state.state == "3" and state.attributes["per_endpoint"] == {"ep-1": 2, "ep-2": 1}
    a.connection = type("C", (), {"subscriptions": {}})()
    a.close("test")
    b.connection = a.connection
    b.close("test")
    await hass.async_block_till_done()
    state = hass.states.get(SENSOR)
    assert state.state == "1" and state.attributes["per_endpoint"] == {"ep-1": 1}


async def test_the_sensor_is_a_diagnostic(hass, hass_storage, google, fake_transport):  # noqa: F811
    await _setup_all(hass, hass_storage, google)
    entry = er.async_get(hass).async_get(SENSOR)
    assert entry is not None and entry.entity_category is EntityCategory.DIAGNOSTIC
