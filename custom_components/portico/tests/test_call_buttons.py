"""Call buttons (house calls design section 14, steps 14a): the `call` action kind, its validation, the
resolved target in `content/get`, `content/run` through the call-endpoint registry.

Everything runs against FAKES. The default registry knows no endpoint and places nothing, which is what a
real install has until the house-call and gate-sip G2 work registers a real one.
"""
from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import async_capture_events

from ..call_endpoints import (
    CallEndpointRegistry,
    NullCallEndpointRegistry,
    PlaceResult,
    get_call_registry,
    set_call_registry,
)
from ..content import ProfileError, get_content_store, normalize_profile
from .blocks_view import served as _served
from .fake_call_registry import FakeCallEndpointRegistry
from .test_content import _enroll, _run
from .test_signaling import _add_device
from .test_push_registry import _custom_integrations  # noqa: F401 - autouse fixture by name

pytestmark = pytest.mark.asyncio


def _call(**extra):
    return {"id": "talk", "label": "Kitchen", "kind": "call", "target": {"type": "dashboard", "id": "ep-kitchen"}, **extra}


def _fake(hass) -> FakeCallEndpointRegistry:
    fake = FakeCallEndpointRegistry()
    fake.add("dashboard", "ep-kitchen", "Kitchen tablet")
    fake.add("phone", "ep-dina", "Dina's phone", available=False, reason="offline")
    fake.add("door", "gate-1", "Gate", listed=False)
    set_call_registry(hass, fake)
    return fake


# ---- validation ---------------------------------------------------------

async def test_the_default_registry_is_a_registry_that_knows_nothing(hass):
    registry = get_call_registry(hass)
    assert isinstance(registry, NullCallEndpointRegistry) and isinstance(registry, CallEndpointRegistry)
    assert registry.endpoints(caller_user_id="u") == [] and registry.target_state("phone", "x") is None
    result = await registry.place(caller_user_id="u", caller_phone="p", target_type="phone", target_id="x")
    assert not result.ok and result.code == "not_available"


async def test_a_call_action_normalizes_with_its_target_and_no_confirm():
    out = normalize_profile({"actions": [_call(icon="mdi:phone-in-talk")]})["actions"][0]
    assert out == {"id": "talk", "label": "Kitchen", "kind": "call", "icon": "mdi:phone-in-talk",
                   "target": {"type": "dashboard", "id": "ep-kitchen"}, "confirm": False}


@pytest.mark.parametrize("confirm", [True, False, "yes", None])
async def test_confirm_is_never_honoured_for_a_call(confirm):
    out = normalize_profile({"actions": [_call(confirm=confirm)]})["actions"][0]
    assert out["confirm"] is False  # CB-9: no confirm step to call anyone, whatever was sent


async def test_other_kinds_keep_their_confirm_default():
    out = normalize_profile({"actions": [{"id": "o", "label": "O", "kind": "lock_open", "entity_id": "lock.a"}]})
    assert out["actions"][0]["confirm"] is True


@pytest.mark.parametrize(
    "target",
    [None, "ep", {}, {"type": "dashboard"}, {"id": "x"}, {"type": "tv", "id": "x"}, {"type": "phone", "id": ""},
     {"type": "phone", "id": "i" * 65}, {"type": "phone", "id": 7}],
)
async def test_a_bad_call_target_is_refused(target):
    with pytest.raises(ProfileError):
        normalize_profile({"actions": [{"id": "t", "label": "T", "kind": "call", "target": target}]})


async def test_the_client_cannot_smuggle_an_entity_or_service_into_a_call():
    out = normalize_profile({"actions": [_call(entity_id="lock.front", service="lock.open", device_id="d")]})["actions"][0]
    assert "entity_id" not in out and "service" not in out and "device_id" not in out


async def test_saving_refuses_a_target_the_registry_does_not_know(hass):
    store = get_content_store(hass)
    with pytest.raises(ProfileError):  # the default registry knows nothing
        await store.async_set("u1", {"actions": [_call()]})
    _fake(hass)
    saved = await store.async_set("u1", {"actions": [_call()]})
    assert saved["actions"][0]["target"]["id"] == "ep-kitchen"
    with pytest.raises(ProfileError):
        await store.async_set("u1", {"actions": [_call(target={"type": "phone", "id": "nobody"})]})
    # a known id of the wrong type is unknown too
    with pytest.raises(ProfileError):
        await store.async_set("u1", {"actions": [_call(target={"type": "phone", "id": "ep-kitchen"})]})


async def test_a_button_whose_target_vanished_stays_and_other_edits_still_save(hass):
    store = get_content_store(hass)
    fake = _fake(hass)
    await store.async_set("u1", {"actions": [_call()]})
    del fake.states[("dashboard", "ep-kitchen")]  # the endpoint is removed
    profile = store.profile_for("u1")
    await store.async_set("u1", {"cameras": [{"id": "c", "label": "C", "entity_id": "camera.c"}],
                                 "actions": profile["actions"]})  # saving something else is fine
    assert store.profile_for("u1")["actions"][0]["target"]["id"] == "ep-kitchen"
    del fake.states[("phone", "ep-dina")]
    with pytest.raises(ProfileError):  # but a NEW button to another vanished target is not
        await store.async_set("u1", {"actions": [*profile["actions"], _call(id="t2", target={"type": "phone", "id": "ep-dina"})]})


async def test_loading_a_stored_profile_keeps_a_call_to_a_vanished_target(hass, hass_storage):
    # a v1 profile whose user is no enrolled phone is an orphan and is dropped (PH-9), so u1 is enrolled
    _add_device(hass, enrolled_phones=[{"ha_user_id": "u1", "name": "P"}])
    hass_storage["portico.content"] = {
        "version": 1, "minor_version": 1, "key": "portico.content",
        "data": {"profiles": {"u1": {"cameras": [], "actions": [_call()]}}},
    }
    store = get_content_store(hass)
    await store.async_ensure_loaded()  # default registry: nothing is known
    assert store.profile_for("u1")["actions"][0]["kind"] == "call"


# ---- content/get --------------------------------------------------------

async def _get(client):
    await client.send_json({"id": 1, "type": "portico/content/get"})
    resp = await client.receive_json()
    assert resp["success"], resp
    return {a["id"]: a for a in _served(resp["result"]["pages"])["actions"]}


async def test_get_resolves_each_call_target(hass, hass_ws_client, hass_read_only_user, hass_read_only_access_token):
    await _enroll(hass, hass_read_only_user)
    _fake(hass)
    await get_content_store(hass).async_set(hass_read_only_user.id, {"actions": [
        _call(), _call(id="dina", label="Dina", target={"type": "phone", "id": "ep-dina"}),
        _call(id="gate", label="Talk", target={"type": "door", "id": "gate-1"})]})
    client = await hass_ws_client(hass, hass_read_only_access_token)
    actions = await _get(client)
    assert actions["talk"]["target_state"] == {"label": "Kitchen tablet", "available": True, "reason": None}
    assert actions["dina"]["target_state"] == {"label": "Dina's phone", "available": False, "reason": "offline"}
    # name/available mirror it, so code that reads those on every action keeps working
    assert actions["talk"]["name"] == "Kitchen tablet" and actions["talk"]["available"] is True
    assert actions["dina"]["available"] is False and actions["dina"]["reason"] == "offline"
    assert actions["talk"]["target"] == {"type": "dashboard", "id": "ep-kitchen"} and actions["talk"]["confirm"] is False


async def test_a_renamed_endpoint_renames_the_button_state(hass, hass_ws_client, hass_read_only_user,
                                                           hass_read_only_access_token):
    await _enroll(hass, hass_read_only_user)
    fake = _fake(hass)
    await get_content_store(hass).async_set(hass_read_only_user.id, {"actions": [_call()]})
    fake.states[("dashboard", "ep-kitchen")] = type(fake.states[("dashboard", "ep-kitchen")])("Pantry tablet", True)
    client = await hass_ws_client(hass, hass_read_only_access_token)
    assert (await _get(client))["talk"]["target_state"]["label"] == "Pantry tablet"


async def test_get_with_the_default_registry_shows_every_call_button_off(
        hass, hass_ws_client, hass_read_only_user, hass_read_only_access_token, hass_storage):
    await _enroll(hass, hass_read_only_user)
    hass_storage["portico.content"] = {
        "version": 1, "minor_version": 1, "key": "portico.content",
        "data": {"profiles": {hass_read_only_user.id: {"cameras": [], "actions": [_call()]}}},
    }
    client = await hass_ws_client(hass, hass_read_only_access_token)
    action = (await _get(client))["talk"]
    assert action["target_state"] == {"label": "Kitchen", "available": False, "reason": "not_available"}


async def test_get_with_a_registry_that_dropped_the_target_says_unknown_target(
        hass, hass_ws_client, hass_read_only_user, hass_read_only_access_token):
    await _enroll(hass, hass_read_only_user)
    fake = _fake(hass)
    await get_content_store(hass).async_set(hass_read_only_user.id, {"actions": [_call()]})
    del fake.states[("dashboard", "ep-kitchen")]
    client = await hass_ws_client(hass, hass_read_only_access_token)
    assert (await _get(client))["talk"]["target_state"] == {"label": "Kitchen", "available": False, "reason": "unknown_target"}


async def test_a_registry_that_raises_in_get_means_off_not_a_crash(
        hass, hass_ws_client, hass_read_only_user, hass_read_only_access_token):
    await _enroll(hass, hass_read_only_user)
    fake = _fake(hass)
    await get_content_store(hass).async_set(hass_read_only_user.id, {"actions": [_call()]})

    def boom(*_a):
        raise RuntimeError("backend bug")

    fake.target_state = boom
    client = await hass_ws_client(hass, hass_read_only_access_token)
    assert (await _get(client))["talk"]["available"] is False


# ---- content/run --------------------------------------------------------

async def test_run_places_the_call_through_the_registry_and_returns_its_result(
        hass, hass_ws_client, hass_read_only_user, hass_read_only_access_token):
    await _enroll(hass, hass_read_only_user)
    fake = _fake(hass)
    log = async_capture_events(hass, "logbook_entry")
    await get_content_store(hass).async_set(hass_read_only_user.id, {"actions": [_call()]})
    client = await hass_ws_client(hass, hass_read_only_access_token)
    resp = await _run(client, "talk")
    assert resp["success"], resp
    assert resp["result"] == {"ran": True, "call_id": "hso-fake-1", "media": {"kind": "fake"}}
    assert fake.placed == [{"caller_user_id": hass_read_only_user.id, "caller_phone": "Alice phone",
                            "target_type": "dashboard", "target_id": "ep-kitchen"}]
    await hass.async_block_till_done()
    assert log[-1].data["message"] == 'ran "Kitchen" (rang)'


async def test_run_takes_the_target_from_the_profile_never_from_the_client(
        hass, hass_ws_client, hass_read_only_user, hass_read_only_access_token):
    await _enroll(hass, hass_read_only_user)
    fake = _fake(hass)
    await get_content_store(hass).async_set(hass_read_only_user.id, {"actions": [_call()]})
    client = await hass_ws_client(hass, hass_read_only_access_token)
    await client.send_json({"id": 1, "type": "portico/content/run", "action_id": "talk",
                            "target": {"type": "phone", "id": "ep-dina"}, "endpoint": "ep-dina"})
    resp = await client.receive_json()
    assert not resp["success"] and resp["error"]["code"] == "invalid_format"  # HA's schema refuses extra keys
    assert fake.placed == []
    assert (await _run(client, "talk", 2))["success"]
    assert [p["target_id"] for p in fake.placed] == ["ep-kitchen"]


async def test_run_with_the_default_registry_is_a_clear_not_available_and_calls_nothing(
        hass, hass_ws_client, hass_read_only_user, hass_read_only_access_token, hass_storage):
    await _enroll(hass, hass_read_only_user)
    hass_storage["portico.content"] = {
        "version": 1, "minor_version": 1, "key": "portico.content",
        "data": {"profiles": {hass_read_only_user.id: {"cameras": [], "actions": [_call()]}}},
    }
    log = async_capture_events(hass, "logbook_entry")
    client = await hass_ws_client(hass, hass_read_only_access_token)
    resp = await _run(client, "talk")
    assert not resp["success"] and resp["error"]["code"] == "not_available"
    assert "cannot place calls" in resp["error"]["message"]
    await hass.async_block_till_done()
    assert log[-1].data["message"] == 'refused "Kitchen" (not_available)'


async def test_run_passes_a_registry_refusal_on_with_its_code(
        hass, hass_ws_client, hass_read_only_user, hass_read_only_access_token):
    await _enroll(hass, hass_read_only_user)
    fake = _fake(hass)
    fake.result = PlaceResult(ok=False, code="target_dnd", message="Dina has Do Not Disturb on")
    await get_content_store(hass).async_set(hass_read_only_user.id, {"actions": [_call()]})
    client = await hass_ws_client(hass, hass_read_only_access_token)
    resp = await _run(client, "talk")
    assert not resp["success"] and resp["error"] == {"code": "target_dnd", "message": "Dina has Do Not Disturb on"}


async def test_a_registry_that_raises_in_run_is_a_refusal_not_a_crash(
        hass, hass_ws_client, hass_read_only_user, hass_read_only_access_token):
    await _enroll(hass, hass_read_only_user)
    fake = _fake(hass)
    fake.raise_on_place = RuntimeError("backend bug")
    await get_content_store(hass).async_set(hass_read_only_user.id, {"actions": [_call()]})
    client = await hass_ws_client(hass, hass_read_only_access_token)
    resp = await _run(client, "talk")
    assert not resp["success"] and resp["error"]["code"] == "failed"


async def test_run_is_rate_limited_per_action(hass, hass_ws_client, hass_read_only_user, hass_read_only_access_token):
    await _enroll(hass, hass_read_only_user)
    fake = _fake(hass)
    await get_content_store(hass).async_set(hass_read_only_user.id, {"actions": [_call()]})
    client = await hass_ws_client(hass, hass_read_only_access_token)
    assert (await _run(client, "talk", 1))["success"]
    second = await _run(client, "talk", 2)
    assert not second["success"] and second["error"]["code"] == "rate_limited"
    assert len(fake.placed) == 1


async def test_a_non_call_run_result_is_unchanged(hass, hass_ws_client, hass_read_only_user, hass_read_only_access_token):
    from pytest_homeassistant_custom_component.common import async_mock_service

    await _enroll(hass, hass_read_only_user)
    hass.states.async_set("scene.porch", "x")
    async_mock_service(hass, "scene", "turn_on")
    await get_content_store(hass).async_set(hass_read_only_user.id, {"actions": [
        {"id": "porch", "label": "Porch", "kind": "scene_activate", "entity_id": "scene.porch"}]})
    client = await hass_ws_client(hass, hass_read_only_access_token)
    assert (await _run(client, "porch"))["result"] == {"ran": True}
