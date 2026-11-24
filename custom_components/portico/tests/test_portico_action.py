"""DEV-811 P5 (short version): `portico_action` for door openings, the shared user-less context, best effort."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.core import Context, Event, EventBus
from pytest_homeassistant_custom_component.common import async_capture_events, async_mock_service

from .. import actions
from ..logbook import async_describe_events
from .test_content import _enroll, _provide, _run   # noqa: F401  (the same fixtures the open tests use)
from .test_content import get_content_store


async def _gate_run(hass, hass_ws_client, user, token, action_id):
    client = await hass_ws_client(hass, token)
    return await _run(client, action_id)


async def test_the_open_event_and_the_gate_action_share_one_user_less_context(
        hass, hass_ws_client, hass_read_only_user, hass_read_only_access_token):
    entry, runtime = await _enroll(hass, hass_read_only_user, device_id="10.0.0.9",
                                   capabilities={"DirectCapable": False, "HasRelay": False})
    button = "button.gate_open"
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "open_relay_action": [{"action": "button.press", "target": {"entity_id": button}}]})
    presses = async_mock_service(hass, "button", "press")
    seen = async_capture_events(hass, actions.EVENT_ACTION)
    await get_content_store(hass).async_set(hass_read_only_user.id, {"actions": [
        {"id": "g", "label": "Open gate", "kind": "portico_open", "device_id": runtime.device_id}]})
    resp = await _gate_run(hass, hass_ws_client, hass_read_only_user, hass_read_only_access_token, "g")
    assert resp["success"], resp
    await hass.async_block_till_done()
    assert len(presses) == 1 and len(seen) == 1
    event = seen[0]
    assert presses[0].context.id == event.context.id          # the script's service call
    assert event.context.user_id is None and presses[0].context.user_id is None   # no permission change
    assert event.data["action"] == "open" and event.data["door_id"] == runtime.device_id
    assert event.data["actor_user"] == hass_read_only_user.id and event.data["source"] == "app"
    assert event.data["actor"] and "result" not in event.data
    for secret in ("token", "host", "address", "sdp"):
        assert secret not in event.data


async def test_the_state_change_of_the_gate_entity_carries_the_same_context(
        hass, hass_ws_client, hass_read_only_user, hass_read_only_access_token):
    entry, runtime = await _enroll(hass, hass_read_only_user, device_id="10.0.0.9",
                                   capabilities={"DirectCapable": False, "HasRelay": False})
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "open_relay_action": [{"action": "test.open_gate"}]})
    seen = async_capture_events(hass, actions.EVENT_ACTION)
    changes = async_capture_events(hass, "state_changed")

    async def _open(call):
        hass.states.async_set("switch.gate", "on", context=call.context)
    hass.services.async_register("test", "open_gate", _open)
    await get_content_store(hass).async_set(hass_read_only_user.id, {"actions": [
        {"id": "g", "label": "Open gate", "kind": "portico_open", "device_id": runtime.device_id}]})
    assert (await _gate_run(hass, hass_ws_client, hass_read_only_user, hass_read_only_access_token, "g"))["success"]
    await hass.async_block_till_done()
    gate = [e for e in changes if e.data["entity_id"] == "switch.gate"]
    assert gate and gate[0].context.id == seen[0].context.id


async def test_open_relay_hands_the_provider_the_event_context(
        hass, hass_ws_client, hass_read_only_user, hass_read_only_access_token):
    _entry, runtime = await _enroll(hass, hass_read_only_user, device_id="10.0.0.9", attach=False)
    fake = _provide(hass, runtime.device_id)
    seen = async_capture_events(hass, actions.EVENT_ACTION)
    await get_content_store(hass).async_set(hass_read_only_user.id, {"actions": [
        {"id": "g", "label": "Open gate", "kind": "portico_open", "device_id": runtime.device_id}]})
    assert (await _gate_run(hass, hass_ws_client, hass_read_only_user, hass_read_only_access_token, "g"))["success"]
    assert len(fake.calls_to("open_relay")) == 1               # once, never twice
    assert fake.open_contexts[0].id == seen[0].context.id


async def test_a_failure_in_the_event_does_not_stop_the_opening(
        hass, hass_ws_client, hass_read_only_user, hass_read_only_access_token):
    _entry, runtime = await _enroll(hass, hass_read_only_user, device_id="10.0.0.9", attach=False)
    fake = _provide(hass, runtime.device_id)
    await get_content_store(hass).async_set(hass_read_only_user.id, {"actions": [
        {"id": "g", "label": "Open gate", "kind": "portico_open", "device_id": runtime.device_id}]})
    real = EventBus.async_fire

    def _fail_for_action(self, name, *a, **k):
        if name == actions.EVENT_ACTION:
            raise RuntimeError("boom")
        return real(self, name, *a, **k)
    with patch.object(EventBus, "async_fire", _fail_for_action):
        assert isinstance(actions.begin_open(hass, actor="a", actor_user="u", source="app", door_id="d", door_name="D"), Context)
        resp = await _gate_run(hass, hass_ws_client, hass_read_only_user, hass_read_only_access_token, "g")
    assert resp["success"], resp
    assert len(fake.calls_to("open_relay")) == 1
    assert fake.open_contexts[0].user_id is None


async def test_a_refused_open_fires_no_event(hass, hass_ws_client, hass_read_only_user, hass_read_only_access_token):
    _entry, runtime = await _enroll(hass, hass_read_only_user, device_id="10.0.0.9", attach=False)
    fake = _provide(hass, runtime.device_id)
    seen = async_capture_events(hass, actions.EVENT_ACTION)
    await get_content_store(hass).async_set(hass_read_only_user.id, {"actions": [
        {"id": "g", "label": "Open gate", "kind": "portico_open", "device_id": runtime.device_id}]})
    client = await hass_ws_client(hass, hass_read_only_access_token)
    resp = await _run(client, "no-such-action")                 # not in the profile: refused as today
    assert not resp["success"]
    assert seen == [] and fake.calls_to("open_relay") == []


def test_the_logbook_describes_the_event_as_one_readable_row():
    described = {}
    async_describe_events(None, lambda domain, name, fn: described.__setitem__(name, fn))
    row = described[actions.EVENT_ACTION](Event(actions.EVENT_ACTION, {
        "action": "open", "actor": "AlicePixel10", "door_name": "Side Door", "door_id": "d1"}))
    assert row == {"name": "Portico", "message": "AlicePixel10 opened Side Door"}
