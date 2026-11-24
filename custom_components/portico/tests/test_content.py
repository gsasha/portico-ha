"""Content profiles (content design phase A): the allow-list, the store, and
`portico/content/get` / `portico/content/run`, over a real websocket.

The security model under test: the profile IS the grant. A phone runs an action
by id from its OWN profile only, the plugin makes the call itself, and nothing a
client sends picks a service or an entity.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from pytest_homeassistant_custom_component.common import async_capture_events, async_mock_service

from ..content import (
    MAX_HOME_NAME, ProfileError, RunRefused, get_content_store, normalize_home_name, normalize_profile,
    profile_rev,
)
from .blocks_view import resolved as _resolved, served as _served
from .driver_part import TransportError
from ..provider import DATA_PROVIDERS, DOMAIN, ProviderError, ProviderRegistry
from .fake_provider import FakeProvider
from .test_signaling import _add_device, _setup_signaling


def _provide(hass, door_id, name="Door"):
    """A FakeProvider with one door, registered with the provider registry content resolves through."""
    registry = hass.data.setdefault(DOMAIN, {}).get(DATA_PROVIDERS)
    if registry is None:
        registry = hass.data[DOMAIN][DATA_PROVIDERS] = ProviderRegistry(hass)
    fake = FakeProvider()
    fake.add_door(door_id, name=name)
    registry.register(fake)
    return fake

pytestmark = pytest.mark.asyncio


def _profile(**extra):
    return {
        "cameras": [{"id": "gate", "label": "Gate", "entity_id": "camera.gate"}],
        "actions": [
            {"id": "open-gate", "label": "Open gate", "kind": "lock_open", "entity_id": "lock.gate"},
            {"id": "porch", "label": "Porch light scene", "kind": "scene_activate", "entity_id": "scene.porch"},
        ],
        **extra,
    }


# ---- the allow-list -----------------------------------------------------

async def test_normalize_defaults_confirm_by_kind():
    out = normalize_profile(_profile())
    actions = {a["id"]: a for a in out["actions"]}
    assert actions["open-gate"]["confirm"] is True
    assert actions["porch"]["confirm"] is False


@pytest.mark.parametrize(
    "bad",
    [
        {"actions": [{"id": "x", "label": "X", "kind": "switch_toggle", "entity_id": "switch.a"}]},  # kind not allowed
        {"actions": [{"id": "x", "label": "X", "kind": "lock_open", "entity_id": "switch.a"}]},  # wrong domain
        {"actions": [{"id": "x", "label": "X", "kind": "lock_open", "entity_id": "lock.a; rm"}]},  # not an entity id
        {"actions": [{"id": "x", "label": "X", "kind": "portico_open"}]},  # no device_id
        {"actions": [{"id": "X!", "label": "X", "kind": "lock_open", "entity_id": "lock.a"}]},  # bad id
        {"actions": [{"id": "x", "label": "", "kind": "lock_open", "entity_id": "lock.a"}]},  # no label
        {"actions": [{"id": "x", "label": "L" * 41, "kind": "lock_open", "entity_id": "lock.a"}]},  # long label
        {"cameras": [{"id": "c", "label": "C", "entity_id": "lock.a"}]},  # not a camera
        {"cameras": [{"id": "c", "label": "C", "entity_id": "camera.a"}],
         "actions": [{"id": "c", "label": "C", "kind": "lock_open", "entity_id": "lock.a"}]},  # id used twice
        {"actions": [{"id": f"a{n}", "label": "A", "kind": "lock_open", "entity_id": "lock.a"} for n in range(25)]},
        "not an object",
    ],
)
async def test_normalize_refuses(bad):
    with pytest.raises(ProfileError):
        normalize_profile(bad)


async def test_normalize_drops_unknown_keys_and_rev_is_stable():
    raw = _profile()
    raw["actions"][0]["service"] = "lock.unlock"  # a client-chosen service is never kept
    out = normalize_profile(raw)
    assert "service" not in out["actions"][0]
    assert profile_rev(out) == profile_rev(normalize_profile(_profile()))


# ---- content/get --------------------------------------------------------

async def _enroll(hass, user, name="Alice phone", **kwargs):
    entry, runtime = _add_device(hass, enrolled_phones=[{"ha_user_id": user.id, "name": name}], **kwargs)
    await _setup_signaling(hass)
    return entry, runtime


async def test_get_is_empty_until_a_profile_is_set(hass, hass_ws_client, hass_read_only_user, hass_read_only_access_token):
    await _enroll(hass, hass_read_only_user)
    client = await hass_ws_client(hass, hass_read_only_access_token)
    await client.send_json({"id": 1, "type": "portico/content/get"})
    resp = await client.receive_json()
    assert resp["success"], resp
    page = _served(resp["result"]["pages"])
    assert page["cameras"] == [] and page["actions"] == []
    assert resp["result"]["kind"] == "phone" and "phone" not in resp["result"]   # contract 3: no flat `phone`


async def test_get_resolves_availability(hass, hass_ws_client, hass_read_only_user, hass_read_only_access_token):
    await _enroll(hass, hass_read_only_user)
    hass.states.async_set("camera.gate", "idle")
    hass.states.async_set("lock.gate", "unavailable")
    await get_content_store(hass).async_set(hass_read_only_user.id, _profile())

    client = await hass_ws_client(hass, hass_read_only_access_token)
    await client.send_json({"id": 1, "type": "portico/content/get"})
    result = (await client.receive_json())["result"]
    page = _served(result["pages"])
    assert page["cameras"][0]["available"] is True
    by_id = {a["id"]: a for a in page["actions"]}
    assert by_id["open-gate"]["available"] is False  # unavailable
    assert by_id["porch"]["available"] is False  # entity does not exist
    store = get_content_store(hass)
    assert result["rev"] == store.resolve_document(store._endpoint_id(hass_read_only_user.id))["rev"]


async def test_get_refuses_a_caller_that_is_not_an_enrolled_phone(hass, hass_ws_client, hass_read_only_access_token):
    _add_device(hass, enrolled_phones=[])
    await _setup_signaling(hass)
    client = await hass_ws_client(hass, hass_read_only_access_token)
    await client.send_json({"id": 1, "type": "portico/content/get"})
    resp = await client.receive_json()
    assert not resp["success"] and resp["error"]["code"] == "unauthorized"


async def test_admin_can_read_a_named_phone_but_a_phone_cannot(hass, hass_ws_client, hass_admin_user,
                                                                  hass_read_only_user, hass_read_only_access_token):
    await _enroll(hass, hass_read_only_user)
    await get_content_store(hass).async_set(hass_read_only_user.id, _profile())

    endpoint = get_content_store(hass)._endpoint_id(hass_read_only_user.id)
    admin = await hass_ws_client(hass)  # the default token is the admin's
    await admin.send_json({"id": 1, "type": "portico/content/get", "endpoint": endpoint})
    resp = await admin.receive_json()
    assert resp["success"] and resp["result"]["endpoint"] == endpoint and len(_served(resp["result"]["pages"])["actions"]) == 2

    await admin.send_json({"id": 2, "type": "portico/content/get", "endpoint": "nobody"})
    assert (await admin.receive_json())["error"]["code"] == "unknown_endpoint"

    await admin.send_json({"id": 3, "type": "portico/content/get"})  # an admin that names nobody
    assert (await admin.receive_json())["error"]["code"] == "unauthorized"

    phone = await hass_ws_client(hass, hass_read_only_access_token)
    await phone.send_json({"id": 1, "type": "portico/content/get", "endpoint": "someone-else"})
    assert (await phone.receive_json())["error"]["code"] == "unauthorized"
    # contract 3 has no `profile` (a phone's name): the schema refuses it for everyone
    await phone.send_json({"id": 2, "type": "portico/content/get", "profile": "alice phone"})
    assert (await phone.receive_json())["error"]["code"] == "invalid_format"


# ---- content/run --------------------------------------------------------

async def _run(client, action_id, msg_id=1):
    await client.send_json({"id": msg_id, "type": "portico/content/run", "action_id": action_id})
    return await client.receive_json()


async def test_run_makes_the_call_as_the_phone_user_and_logs_it(hass, hass_ws_client, hass_read_only_user,
                                                               hass_read_only_access_token):
    await _enroll(hass, hass_read_only_user)
    hass.states.async_set("lock.gate", "locked")
    hass.states.async_set("scene.porch", "scening")
    opens = async_mock_service(hass, "lock", "open")
    scenes = async_mock_service(hass, "scene", "turn_on")
    log = async_capture_events(hass, "logbook_entry")
    await get_content_store(hass).async_set(hass_read_only_user.id, _profile())
    client = await hass_ws_client(hass, hass_read_only_access_token)

    resp = await _run(client, "open-gate")
    assert resp["success"], resp
    await hass.async_block_till_done()
    assert len(opens) == 1 and opens[0].data == {"entity_id": "lock.gate"}
    assert opens[0].context.user_id == hass_read_only_user.id
    assert not scenes
    assert log[-1].data["name"] == "Portico: Alice phone" and log[-1].data["message"] == 'ran "Open gate"'


async def test_admin_running_a_phones_button_is_attributed_to_the_admin(hass, hass_ws_client, hass_admin_user,
                                                                       hass_read_only_user):
    await _enroll(hass, hass_read_only_user)
    hass.states.async_set("lock.gate", "locked")
    opens = async_mock_service(hass, "lock", "open")
    log = async_capture_events(hass, "logbook_entry")
    await get_content_store(hass).async_set(hass_read_only_user.id, _profile())
    admin = await hass_ws_client(hass)

    await admin.send_json({"id": 1, "type": "portico/content/run", "action_id": "open-gate",
                           "endpoint": get_content_store(hass)._endpoint_id(hass_read_only_user.id)})
    resp = await admin.receive_json()
    assert resp["success"], resp
    await hass.async_block_till_done()
    assert len(opens) == 1 and opens[0].context.user_id == hass_admin_user.id
    assert log[-1].data["name"] == "Portico: Alice phone"
    assert log[-1].data["message"] == f'ran "Open gate" (run by {hass_admin_user.name})'


async def test_run_refuses_an_action_that_is_not_in_the_profile(hass, hass_ws_client, hass_read_only_user,
                                                                hass_read_only_access_token):
    await _enroll(hass, hass_read_only_user)
    hass.states.async_set("lock.gate", "locked")
    opens = async_mock_service(hass, "lock", "open")
    unlocks = async_mock_service(hass, "lock", "unlock")
    log = async_capture_events(hass, "logbook_entry")
    await get_content_store(hass).async_set(hass_read_only_user.id, _profile())
    client = await hass_ws_client(hass, hass_read_only_access_token)

    for n, bad in enumerate(("lock.unlock", "lock.gate", "front-door"), start=1):
        resp = await _run(client, bad, n)
        assert not resp["success"] and resp["error"]["code"] == "unknown_action"
    await hass.async_block_till_done()
    assert not opens and not unlocks
    assert "refused" in log[-1].data["message"]


async def test_one_phone_cannot_run_anothers_action(hass, hass_ws_client, hass_admin_user, hass_read_only_user,
                                                    hass_read_only_access_token):
    _add_device(hass, enrolled_phones=[
        {"ha_user_id": hass_admin_user.id, "name": "Admin phone"},
        {"ha_user_id": hass_read_only_user.id, "name": "Other phone"},
    ])
    await _setup_signaling(hass)
    hass.states.async_set("lock.gate", "locked")
    opens = async_mock_service(hass, "lock", "open")
    await get_content_store(hass).async_set(hass_admin_user.id, _profile())  # only the admin's phone has it
    client = await hass_ws_client(hass, hass_read_only_access_token)

    resp = await _run(client, "open-gate")
    assert not resp["success"] and resp["error"]["code"] == "unknown_action"
    await hass.async_block_till_done()
    assert not opens


async def test_run_is_rate_limited_and_refuses_an_unavailable_entity(hass, hass_ws_client, hass_read_only_user,
                                                                      hass_read_only_access_token):
    await _enroll(hass, hass_read_only_user)
    hass.states.async_set("lock.gate", "locked")
    opens = async_mock_service(hass, "lock", "open")
    await get_content_store(hass).async_set(hass_read_only_user.id, _profile())
    client = await hass_ws_client(hass, hass_read_only_access_token)

    assert (await _run(client, "open-gate", 1))["success"]
    second = await _run(client, "open-gate", 2)
    assert not second["success"] and second["error"]["code"] == "rate_limited"
    await hass.async_block_till_done()
    assert len(opens) == 1

    third = await _run(client, "porch", 3)  # scene.porch has no state
    assert not third["success"] and third["error"]["code"] == "unavailable"


async def test_portico_open_calls_the_device_relay(hass, hass_ws_client, hass_read_only_user, hass_read_only_access_token):
    _entry, runtime = await _enroll(hass, hass_read_only_user, device_id="10.0.0.9", attach=False)
    fake = _provide(hass, runtime.device_id)
    profile = {"actions": [{"id": "gate-relay", "label": "Open gate", "kind": "portico_open", "device_id": runtime.device_id}]}
    await get_content_store(hass).async_set(hass_read_only_user.id, profile)
    client = await hass_ws_client(hass, hass_read_only_access_token)

    resp = await _run(client, "gate-relay")
    assert resp["success"], resp
    assert fake.calls_to("open_relay") == [((runtime.device_id,), {})]


async def test_portico_open_goes_through_the_provider(hass, hass_ws_client, hass_read_only_user,
                                                      hass_read_only_access_token):
    """WS-06: the door is resolved by the registry and the relay is the provider's open_relay, nothing else:
    the device entry's own client is never touched, and a namespaced id reaches the same door."""
    client_mock = AsyncMock()
    _entry, runtime = await _enroll(hass, hass_read_only_user, device_id="10.0.0.9", client=client_mock, attach=False)
    fake = _provide(hass, "gate-1", name="Gate")
    profile = {"actions": [{"id": "namespaced", "label": "Open", "kind": "portico_open", "device_id": "fake:gate-1"},
                           {"id": "bare", "label": "Open 2", "kind": "portico_open", "device_id": "gate-1"}]}
    await get_content_store(hass).async_set(hass_read_only_user.id, profile)
    client = await hass_ws_client(hass, hass_read_only_access_token)

    assert (await _run(client, "namespaced", 1))["success"]
    store = get_content_store(hass)
    store._last_run.clear()
    assert (await _run(client, "bare", 2))["success"]
    assert fake.calls_to("open_relay") == [(("gate-1",), {}), (("gate-1",), {})]
    client_mock.open_relay.assert_not_awaited()
    assert _resolved(store, hass_read_only_user.id)["actions"][0]["name"] == "Gate"


async def test_portico_open_with_no_such_door_is_unavailable(hass, hass_ws_client, hass_read_only_user,
                                                             hass_read_only_access_token):
    await _enroll(hass, hass_read_only_user)
    fake = _provide(hass, "gate-1")
    profile = {"actions": [{"id": "gone", "label": "Open", "kind": "portico_open", "device_id": "no-such-door"}]}
    await get_content_store(hass).async_set(hass_read_only_user.id, profile)
    client = await hass_ws_client(hass, hass_read_only_access_token)

    resp = await _run(client, "gone")
    assert not resp["success"] and resp["error"]["code"] == "unavailable"
    assert fake.calls_to("open_relay") == []


async def test_portico_open_reports_a_provider_error(hass, hass_ws_client, hass_read_only_user, hass_read_only_access_token):
    _entry, runtime = await _enroll(hass, hass_read_only_user, device_id="10.0.0.9", attach=False)
    fake = _provide(hass, runtime.device_id)
    fake.raise_on["open_relay"] = ProviderError("not_supported", "this device has no relay")
    profile = {"actions": [{"id": "gate-relay", "label": "Open gate", "kind": "portico_open", "device_id": runtime.device_id}]}
    await get_content_store(hass).async_set(hass_read_only_user.id, profile)
    client = await hass_ws_client(hass, hass_read_only_access_token)

    resp = await _run(client, "gate-relay")
    assert not resp["success"] and resp["error"]["code"] == "not_supported"


async def test_portico_open_reports_a_transport_error(hass, hass_ws_client, hass_read_only_user, hass_read_only_access_token):
    """A provider whose exception is not ours (a TransportError) works the same: `code` and `message`."""
    _entry, runtime = await _enroll(hass, hass_read_only_user, device_id="10.0.0.9", attach=False)
    fake = _provide(hass, runtime.device_id)
    fake.raise_on["open_relay"] = TransportError("not_supported", "this device has no relay")
    profile = {"actions": [{"id": "gate-relay", "label": "Open gate", "kind": "portico_open", "device_id": runtime.device_id}]}
    await get_content_store(hass).async_set(hass_read_only_user.id, profile)
    client = await hass_ws_client(hass, hass_read_only_access_token)

    resp = await _run(client, "gate-relay")
    assert not resp["success"] and resp["error"]["code"] == "not_supported"


async def test_a_failing_service_is_a_refusal_not_a_crash(hass, hass_ws_client, hass_read_only_user,
                                                          hass_read_only_access_token):
    await _enroll(hass, hass_read_only_user)
    hass.states.async_set("lock.gate", "locked")
    hass.services.async_register("lock", "open", AsyncMock(side_effect=RuntimeError("boom")))
    await get_content_store(hass).async_set(hass_read_only_user.id, _profile())
    client = await hass_ws_client(hass, hass_read_only_access_token)

    resp = await _run(client, "open-gate")
    assert not resp["success"] and resp["error"]["code"] == "failed"
    assert "boom" not in str(resp)


# ---- subscribe ----------------------------------------------------------

async def test_subscribe_gets_content_changed_for_the_phones_own_profile(hass, hass_ws_client, hass_read_only_user,
                                                                         hass_read_only_access_token):
    await _enroll(hass, hass_read_only_user)
    client = await hass_ws_client(hass, hass_read_only_access_token)
    await client.send_json({"id": 1, "type": "portico/content/subscribe"})
    assert (await client.receive_json())["success"]

    store = get_content_store(hass)
    await store.async_set(hass_read_only_user.id, _profile())
    event = (await client.receive_json())["event"]
    endpoint = store._endpoint_id(hass_read_only_user.id)
    assert event == {"type": "content_changed", "endpoint": endpoint, "rev": store.resolve_document(endpoint)["rev"]}


# ---- the panel's registration ---------------------------------------------------

async def test_content_list_is_gone_in_contract_3(hass, hass_ws_client, hass_admin_user):
    """`portico/content/list` is removed with no alias (M6 E3): the panel reads `content/document` since #101."""
    await _setup_signaling(hass)
    admin = await hass_ws_client(hass)
    await admin.send_json({"id": 1, "type": "portico/content/list"})
    resp = await admin.receive_json()
    assert not resp["success"] and resp["error"]["code"] == "unknown_command"


async def test_the_panel_is_registered_for_every_user_with_a_guard(hass):
    from unittest.mock import MagicMock, patch

    import portico

    hass.http = MagicMock()
    with patch("homeassistant.components.panel_custom.async_register_panel", new=AsyncMock()) as register:
        await portico._async_register_panel(hass)
    kwargs = register.await_args.kwargs
    assert kwargs["require_admin"] is False     # every HA user may open it; every write is `admin` in the auth table
    assert kwargs["webcomponent_name"] == "portico-panel" and kwargs["frontend_url_path"] == "portico"
    import os

    panel_js = os.path.join(os.path.dirname(portico.__file__), "www", "portico-panel.js")
    assert kwargs["module_url"] == portico._versioned(portico._PANEL_URL_PATH, portico._content_token(panel_js))
    # DEV-704: each file is a route of its own with `Cache-Control: no-cache` (portico._async_serve_script), in order:
    # panel, Phones, Settings, Elements, Elements renderer (what each route serves is proved over real HTTP in test_card_setup.py)
    calls = hass.http.app.router.add_route.call_args_list
    assert [c.args[:2] for c in calls] == [
        ("GET", portico._PANEL_URL_PATH), ("GET", portico._PHONES_URL_PATH), ("GET", portico._SETTINGS_URL_PATH),
        ("GET", portico._ELEMENTS_URL_PATH), ("GET", portico._ELEMENTS_RENDER_URL_PATH)]
    # UI split P2: the Phones tab's module is served beside it, content-hashed like the panel, and its hashed URL
    # travels in the panel's config (the panel imports it lazily; the bare path is the fallback)
    phones_js = os.path.join(os.path.dirname(portico.__file__), "www", "portico-phones.js")
    # UI split P4: the Settings tab's module is served the same way, last
    settings_js = os.path.join(os.path.dirname(portico.__file__), "www", "portico-settings.js")
    render_js = os.path.join(os.path.dirname(portico.__file__), "www", "portico-elements-render.js")
    elements_js = os.path.join(os.path.dirname(portico.__file__), "www", "portico-elements.js")
    assert kwargs["config"] == {
        "phones_module": portico._versioned(portico._PHONES_URL_PATH, portico._content_token(phones_js)),
        "settings_module": portico._versioned(portico._SETTINGS_URL_PATH, portico._content_token(settings_js)),
        "elements_module": portico._versioned(portico._ELEMENTS_URL_PATH, portico._content_token(elements_js)),
        "elements_render_module": portico._versioned(portico._ELEMENTS_RENDER_URL_PATH, portico._content_token(render_js)),
    }

    # A registration problem is logged, never raised: the calls do not depend on the panel.
    with patch("homeassistant.components.panel_custom.async_register_panel",
               new=AsyncMock(side_effect=KeyError("frontend"))):
        await portico._async_register_panel(hass)


async def test_the_phones_module_is_served_over_real_http_by_the_integration(hass, hass_client, enable_custom_integrations):
    """UI split P2: the real setup serves the Phones tab's module at its static path, as a JavaScript module the panel can
    `import()` (it exports the controller), next to the panel and under its own content hash in the panel's config."""
    import os

    import portico
    from homeassistant.setup import async_setup_component

    from custom_components.portico.config_flow import DOMAIN

    from unittest.mock import AsyncMock, patch

    from homeassistant.components.frontend import DATA_EXTRA_MODULE_URL

    hass.data[DATA_EXTRA_MODULE_URL] = set()
    assert await async_setup_component(hass, "http", {})
    with patch("homeassistant.components.panel_custom.async_register_panel", new=AsyncMock()):
        assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    resp = await (await hass_client()).get(portico._PHONES_URL_PATH)
    assert resp.status == 200
    body = await resp.text()
    assert "export class PhonesController" in body and "export function qrScale" in body
    with open(os.path.join(os.path.dirname(portico.__file__), "www", "portico-phones.js"), encoding="utf-8") as handle:
        assert body == handle.read()


async def test_the_panel_file_is_served_over_real_http(hass, hass_client):
    import os

    import portico
    from homeassistant.setup import async_setup_component

    assert await async_setup_component(hass, "http", {})
    await hass.async_block_till_done()
    path = os.path.join(os.path.dirname(portico.__file__), "www", "portico-panel.js")
    portico._async_serve_script(hass, portico._PANEL_URL_PATH, path)
    resp = await (await hass_client()).get(portico._PANEL_URL_PATH)
    assert resp.status == 200
    assert 'customElements.define("portico-panel"' in await resp.text()


async def test_store_survives_a_reload_and_drops_an_invalid_stored_profile(hass, hass_storage, hass_read_only_user):
    _add_device(hass, enrolled_phones=[{"ha_user_id": hass_read_only_user.id, "name": "P"},
                                       {"ha_user_id": "broken", "name": "Q"}])
    hass_storage["portico.content"] = {
        "version": 1, "minor_version": 1, "key": "portico.content",
        "data": {"profiles": {
            hass_read_only_user.id: _profile(),
            "broken": {"actions": [{"id": "x", "label": "X", "kind": "switch_toggle", "entity_id": "switch.a"}]},
        }},
    }
    store = get_content_store(hass)
    await store.async_ensure_loaded()
    assert len(store.profile_for(hass_read_only_user.id)["actions"]) == 2
    assert store.profile_for("broken") == {"cameras": [], "actions": []}


async def test_run_refused_carries_a_code():
    err = RunRefused("unavailable", "x")
    assert err.code == "unavailable" and err.message == "x"


async def test_icon_names_are_exactly_the_shared_contract_list():
    """contract/icons_v1.json is the list the phone app bundles; portico-app has a copy and its own test."""
    import json
    from pathlib import Path

    from ..content import ICON_NAMES, ICON_NONE

    contract = json.loads((Path(__file__).resolve().parents[3] / "contract" / "icons_v1.json").read_text())
    assert contract["version"] == 1 and contract["none"] == ICON_NONE
    assert list(ICON_NAMES) == contract["icons"]
    assert len(set(ICON_NAMES)) == len(ICON_NAMES) and ICON_NONE not in ICON_NAMES
    assert all(n == n.lower() and " " not in n and ":" not in n for n in ICON_NAMES)


@pytest.mark.parametrize("icon", ["mdi:gate", "mdi:some-old-icon-not-in-the-list", "hass:lock", "anything", "none"])
async def test_a_stored_icon_string_is_never_refused(icon):
    profile = normalize_profile({
        "cameras": [],
        "actions": [{"id": "a", "label": "A", "kind": "lock_open", "entity_id": "lock.a", "icon": icon}],
    })
    assert profile["actions"][0]["icon"] == icon


# ---- the install-wide home name ---------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("My Home", "My Home"), ("  My Home  ", "My Home"), ("", ""), ("   ", ""), (None, ""),
    ("x" * MAX_HOME_NAME, "x" * MAX_HOME_NAME), ("  " + "x" * MAX_HOME_NAME + "  ", "x" * MAX_HOME_NAME),
    ("Dom \u05d1\u05d9\u05ea", "Dom \u05d1\u05d9\u05ea"),
])
async def test_home_name_is_trimmed_and_empty_means_not_set(raw, expected):
    assert normalize_home_name(raw) == expected


@pytest.mark.parametrize("raw", [
    "x" * (MAX_HOME_NAME + 1), "a\nb", "a\tb", "a\x00b", "\x07", "a\x7fb", "a\u2028b", 22, ["x"], {"a": 1}, True,
])
async def test_home_name_refuses_too_long_control_characters_and_non_strings(raw):
    with pytest.raises(ProfileError):
        normalize_home_name(raw)


async def test_home_name_is_unset_by_default_and_nothing_is_stored(hass, hass_storage):
    store = get_content_store(hass)
    await store.async_ensure_loaded()
    assert store.home_name == ""
    await store.async_set_home_name("")  # clearing what was never set writes nothing and tells nobody
    assert "portico.content" not in hass_storage


async def test_home_name_survives_a_restart_and_clearing_removes_it(hass, hass_storage, hass_read_only_user):
    store = get_content_store(hass)
    assert await store.async_set_home_name("  My Home ") == "My Home"
    await store.async_set(hass_read_only_user.id, _profile())
    saved = hass_storage["portico.content"]["data"]
    assert saved["home_name"] == "My Home" and list(saved["overrides"]) == [hass_read_only_user.id]

    # A restart: a fresh store object over the same file.
    from ..content import ContentStore
    again = ContentStore(hass)
    await again.async_ensure_loaded()
    assert again.home_name == "My Home"
    assert len(again.profile_for(hass_read_only_user.id)["actions"]) == 2

    await again.async_set_home_name("")
    assert again.home_name == "" and "home_name" not in hass_storage["portico.content"]["data"]
    assert list(hass_storage["portico.content"]["data"]["overrides"]) == [hass_read_only_user.id]


async def test_an_invalid_stored_home_name_is_dropped_not_fatal(hass, hass_storage, hass_read_only_user):
    _add_device(hass, enrolled_phones=[{"ha_user_id": hass_read_only_user.id, "name": "P"}])
    hass_storage["portico.content"] = {
        "version": 1, "minor_version": 1, "key": "portico.content",
        "data": {"home_name": "bad\nname", "profiles": {hass_read_only_user.id: _profile()}},
    }
    store = get_content_store(hass)
    await store.async_ensure_loaded()
    assert store.home_name == "" and len(store.profile_for(hass_read_only_user.id)["actions"]) == 2


async def test_a_refused_home_name_changes_nothing(hass, hass_storage):
    store = get_content_store(hass)
    await store.async_set_home_name("My Home")
    with pytest.raises(ProfileError):
        await store.async_set_home_name("a\nb")
    assert store.home_name == "My Home" and hass_storage["portico.content"]["data"]["home_name"] == "My Home"


async def test_get_has_home_name_only_when_set(hass, hass_ws_client, hass_read_only_user, hass_read_only_access_token):
    await _enroll(hass, hass_read_only_user)
    store = get_content_store(hass)
    await store.async_set(hass_read_only_user.id, _profile())
    client = await hass_ws_client(hass, hass_read_only_access_token)

    await client.send_json({"id": 1, "type": "portico/content/get"})
    before = (await client.receive_json())["result"]
    assert "home_name" not in before
    assert before["rev"] == store.resolve_document(store._endpoint_id(hass_read_only_user.id))["rev"]

    await store.async_set_home_name("My Home")
    await client.send_json({"id": 2, "type": "portico/content/get"})
    after = (await client.receive_json())["result"]
    assert after["home_name"] == "My Home"
    assert {k: v for k, v in after.items() if k not in ("home_name", "rev")} == \
        {k: v for k, v in before.items() if k != "rev"}
    assert after["rev"] != before["rev"]  # a client that compares revs refetches

    await store.async_set_home_name("")
    await client.send_json({"id": 3, "type": "portico/content/get"})
    assert (await client.receive_json())["result"] == before  # byte-identical to the never-set reply


async def test_get_has_home_name_for_a_phone_with_no_profile(hass, hass_ws_client, hass_read_only_user,
                                                              hass_read_only_access_token):
    await _enroll(hass, hass_read_only_user)
    await get_content_store(hass).async_set_home_name("My Home")
    client = await hass_ws_client(hass, hass_read_only_access_token)
    await client.send_json({"id": 1, "type": "portico/content/get"})
    result = (await client.receive_json())["result"]
    page = _served(result["pages"])
    assert result["home_name"] == "My Home" and page["cameras"] == [] and page["actions"] == []


async def test_a_home_name_change_reaches_a_subscribed_phone(hass, hass_ws_client, hass_read_only_user,
                                                              hass_read_only_access_token):
    await _enroll(hass, hass_read_only_user)
    store = get_content_store(hass)
    client = await hass_ws_client(hass, hass_read_only_access_token)
    await client.send_json({"id": 1, "type": "portico/content/subscribe"})
    assert (await client.receive_json())["success"]

    before_home_name_rev = store.resolve_document(store._endpoint_id(hass_read_only_user.id))["rev"]
    await store.async_set_home_name("My Home")
    event = (await client.receive_json())["event"]
    endpoint = store._endpoint_id(hass_read_only_user.id)
    assert event == {"type": "content_changed", "endpoint": endpoint, "rev": store.resolve_document(endpoint)["rev"]}
    await client.send_json({"id": 2, "type": "portico/content/get"})
    assert (await client.receive_json())["result"]["rev"] == event["rev"]

    await store.async_set_home_name("My Home")  # the same name again is no change: no event
    await store.async_set_home_name("")
    event = (await client.receive_json())["event"]
    assert event["type"] == "content_changed" and event["rev"] == store.resolve_document(endpoint)["rev"]
    assert event["rev"] == before_home_name_rev     # clearing the name is the document it was before


# ---- content store schema v2 (PH-9, plan 2.7) ----------------------------------

# A copy of the profile shape the v1 contract recorder used (not imported: that file moves with PH-6/PH-8).
CONTENT_PROFILE = {
    "cameras": [
        {"id": "gate", "label": "Gate", "entity_id": "camera.gate"},
        {"id": "garage", "label": "Garage", "entity_id": "camera.garage"},
    ],
    "actions": [
        {"id": "open-front", "label": "Front door", "kind": "lock_open", "entity_id": "lock.front", "confirm": True},
        {"id": "evening", "label": "Evening", "kind": "scene_activate", "entity_id": "scene.evening", "confirm": False},
        {"id": "gate-relay", "label": "Gate", "kind": "portico_open", "device_id": "http://10.8.0.5:8080", "confirm": True},
    ],
}


def _v1_file(profiles, **extra):
    return {"version": 1, "minor_version": 1, "key": "portico.content", "data": {"profiles": profiles, **extra}}


def _stored(data):
    return {"version": 1, "minor_version": 1, "key": "portico.content", "data": data}


def _v2_data(**over):
    page = {"id": "home", **normalize_profile(_profile())}
    data = {
        "schema_version": 2, "home_name": "My Home",
        "template": {"pages": [], "rules": [], "roster": []},
        "overrides": {"enr-1": {"pages": [page], "rules": [], "roster_hide": []}},
        "dashboards": {},
    }
    data.update(over)
    return data


async def test_content_store_v1_file_loads_as_v4(hass, hass_storage, hass_read_only_user):
    """UI-10: a v1 file (`profiles` keyed by HA user id, the shape of CONTENT_PROFILE above) loads
    as v4 (one block per page): each profile is the "home" page of the override of the phone endpoint with that user; the phone
    keeps its profile and rev; the home name is carried over; and loading writes nothing."""
    _add_device(hass, enrolled_phones=[{"ha_user_id": hass_read_only_user.id, "name": "P", "enroll_id": "enr-1"}])
    hass_storage["portico.content"] = _v1_file({hass_read_only_user.id: CONTENT_PROFILE}, home_name="My Home")
    before = json.loads(json.dumps(hass_storage["portico.content"]))

    store = get_content_store(hass)
    await store.async_ensure_loaded()
    expected = normalize_profile(CONTENT_PROFILE)
    assert store.profile_for(hass_read_only_user.id) == expected
    assert store.home_name == "My Home" and store.dashboards == {}
    # Keyed by the phone endpoint's id (its enroll_id), as a one-page set.
    (page,) = store.pages_for_endpoint("enr-1")
    assert page["id"] == "home" and page["label"] == "Home"
    assert page == {"id": "home", "label": "Home", "blocks": [{"id": "home", "title": "Home", **expected}]}
    assert store.pages_for_endpoint("nobody") == []
    assert hass_storage["portico.content"] == before  # nothing is written until the first normal save

    await store.async_set_home_name("Home 23")  # the first normal save writes v4
    data = hass_storage["portico.content"]["data"]
    assert data["schema_version"] == 4 and "profiles" not in data and data["home_name"] == "Home 23"
    assert list(data["overrides"]) == ["enr-1"]
    assert data["template"] == {"pages": [], "rules": [], "roster": ["*"]}
    assert data["overrides"]["enr-1"]["pages"][0]["id"] == "home" and data["dashboards"] == {}
    assert data["endpoints"] == {}


async def test_first_v4_save_keeps_a_v1_copy(hass, hass_storage, hass_read_only_user):
    """R3-08: the first v4 save over a v1 file leaves `portico.content.v1` holding the v1 data, once."""
    _add_device(hass, enrolled_phones=[{"ha_user_id": hass_read_only_user.id, "name": "P", "enroll_id": "enr-1"}])
    v1 = _v1_file({hass_read_only_user.id: CONTENT_PROFILE}, home_name="My Home")
    hass_storage["portico.content"] = json.loads(json.dumps(v1))
    store = get_content_store(hass)
    await store.async_ensure_loaded()
    assert "portico.content.v1" not in hass_storage  # loading writes nothing
    await store.async_set_home_name("Home 23")
    assert hass_storage["portico.content.v1"]["data"] == v1["data"]
    assert "portico.content.v2" not in hass_storage and "portico.content.v3" not in hass_storage
    assert hass_storage["portico.content"]["data"]["schema_version"] == 4
    hass_storage["portico.content.v1"]["data"]["home_name"] = "marker"
    await store.async_set_home_name("Home 24")  # a later save does not touch the copy
    assert hass_storage["portico.content.v1"]["data"]["home_name"] == "marker"


async def test_first_v4_save_keeps_a_v2_copy(hass, hass_storage):
    """The pre-migration copy is generalised (R6-04): a v2 file is copied once as `portico.content.v2`."""
    v2 = _v2_data()
    hass_storage["portico.content"] = _stored(json.loads(json.dumps(v2)))
    store = get_content_store(hass)
    await store.async_ensure_loaded()
    assert "portico.content.v2" not in hass_storage  # loading writes nothing
    await store.async_set_home_name("Home 23")
    assert hass_storage["portico.content.v2"]["data"] == v2
    assert hass_storage["portico.content"]["data"]["schema_version"] == 4
    hass_storage["portico.content.v2"]["data"]["home_name"] = "marker"
    await store.async_set_home_name("Home 24")
    assert hass_storage["portico.content.v2"]["data"]["home_name"] == "marker"
    assert "portico.content.v1" not in hass_storage


async def test_a_v4_file_makes_no_copy(hass, hass_storage):
    hass_storage["portico.content"] = _stored({
        "schema_version": 4, "template": {"pages": [], "rules": [], "roster": ["*"]}, "overrides": {},
        "dashboards": {}, "endpoints": {}})
    store = get_content_store(hass)
    await store.async_set_home_name("Home 23")
    assert sorted(k for k in hass_storage if k.startswith("portico.content")) == ["portico.content"]


async def test_existing_v1_copy_is_not_overwritten(hass, hass_storage, hass_read_only_user):
    _add_device(hass, enrolled_phones=[{"ha_user_id": hass_read_only_user.id, "name": "P"}])
    hass_storage["portico.content"] = _v1_file({hass_read_only_user.id: CONTENT_PROFILE})
    old = {"version": 1, "minor_version": 1, "key": "portico.content.v1", "data": {"profiles": {}, "kept": True}}
    hass_storage["portico.content.v1"] = json.loads(json.dumps(old))
    store = get_content_store(hass)
    await store.async_set_home_name("X")
    assert hass_storage["portico.content.v1"] == old
    assert hass_storage["portico.content"]["data"]["schema_version"] == 4


async def test_a_failed_copy_warns_and_does_not_block_the_save(hass, hass_storage, hass_read_only_user, caplog):
    from unittest.mock import patch
    from homeassistant.helpers.storage import Store
    _add_device(hass, enrolled_phones=[{"ha_user_id": hass_read_only_user.id, "name": "P"}])
    hass_storage["portico.content"] = _v1_file({hass_read_only_user.id: CONTENT_PROFILE})
    store = get_content_store(hass)
    await store.async_ensure_loaded()
    real = Store.async_save

    async def save(self, data):
        if self.key.endswith(".v1"):
            raise OSError("disk full")
        return await real(self, data)

    with patch.object(Store, "async_save", save), caplog.at_level("WARNING"):
        await store.async_set_home_name("X")
    assert "could not keep a copy of the v1 file" in caplog.text
    assert hass_storage["portico.content"]["data"]["schema_version"] == 4


async def test_v1_orphan_profile_becomes_not_attached(hass, hass_storage, hass_read_only_user, caplog):
    """R6-02: a v1 profile whose user is no enrolled phone is KEPT, as an override keyed by its old user id,
    and survives the first save (v2's own migration dropped it, which emptied the phone's Home)."""
    _add_device(hass, enrolled_phones=[{"ha_user_id": hass_read_only_user.id, "name": "P"}])
    hass_storage["portico.content"] = _v1_file({hass_read_only_user.id: _profile(), "gone-user": _profile()})
    store = get_content_store(hass)
    with caplog.at_level("WARNING"):
        await store.async_ensure_loaded()
    assert "gone-user" in caplog.text and "Not attached" in caplog.text
    assert len(store.profile_for(hass_read_only_user.id)["actions"]) == 2
    assert store.orphan_keys() == ["gone-user"]
    # Keyed by the user id it was stored under, so a user with that id (and no phone) still reads it.
    assert len(store.profile_for("gone-user")["actions"]) == 2
    await store.async_set_home_name("X")
    saved = hass_storage["portico.content"]["data"]["overrides"]
    assert sorted(saved) == sorted([hass_read_only_user.id, "gone-user"])
    assert saved["gone-user"]["pages"][0]["blocks"][0]["cameras"] == normalize_profile(_profile())["cameras"]
    assert "cameras" not in saved["gone-user"]["pages"][0]               # no flat mirror on disk (B8)


def _everything_file(version):
    """A file of `version` (1 or 2) holding a profile for a live phone and two orphans, a rule, a roster hide,
    a dashboard: the test below asserts none of it is lost."""
    rich = normalize_profile(_profile())
    if version == 1:
        return _v1_file({"u-live": _profile(), "u-old-1": _profile(), "u-old-2": CONTENT_PROFILE}, home_name="H")
    return _stored({
        "schema_version": 2, "home_name": "H",
        "template": {"pages": [{"id": "garden", **rich}], "rules": [], "roster": []},
        "overrides": {
            "enr-live": {"pages": [{"id": "home", **rich}], "rules": [], "roster_hide": ["door-x"]},
            "enr-old-1": {"pages": [{"id": "home", **rich}, {"id": "extra", "cameras": [], "actions": []}],
                          "rules": [{"id": "r1", "caller": "kind:door", "cameras": None,
                                     "ring_buttons": ["porch"], "call_buttons": []}], "roster_hide": []},
            "enr-old-2": {"pages": [{"id": "home", **rich}], "rules": [], "roster_hide": []},
        },
        "dashboards": {"kitchen": {"name": "K", "ha_user_id": "u-k", "room": "kitchen", "cameras": ["camera.gate"]}}})


@pytest.mark.parametrize("version", [1, 2])
async def test_a_v1_or_v2_file_with_orphans_loses_nothing(hass, hass_storage, version):
    """R6-02/R6-04: whichever older file is on the box, load + first save keeps every profile and override
    (live and orphaned), and the older file is copied once as `.v<N>`."""
    _add_device(hass, enrolled_phones=[{"ha_user_id": "u-live", "name": "P", "enroll_id": "enr-live"}])
    original = _everything_file(version)
    hass_storage["portico.content"] = json.loads(json.dumps(original))
    store = get_content_store(hass)
    await store.async_ensure_loaded()
    expected = {"u-live": "enr-live", "u-old-1": "u-old-1", "u-old-2": "u-old-2"} if version == 1 else \
        {"enr-live": "enr-live", "enr-old-1": "enr-old-1", "enr-old-2": "enr-old-2"}
    assert sorted(store.overrides) == sorted(expected.values())     # nothing dropped in memory
    await store.async_set_home_name("H2")
    saved = hass_storage["portico.content"]["data"]
    assert saved["schema_version"] == 4 and sorted(saved["overrides"]) == sorted(expected.values())
    n = version
    assert hass_storage[f"portico.content.v{n}"]["data"] == original["data"]      # the older file, verbatim
    if version == 1:
        for key in ("u-old-1", "u-old-2"):
            assert key in store.orphan_keys()
        assert len(saved["overrides"]["u-old-2"]["pages"][0]["blocks"][0]["actions"]) == 3
    else:
        assert store.orphan_keys() == ["enr-old-1", "enr-old-2"]
        assert [p["id"] for p in saved["overrides"]["enr-old-1"]["pages"]] == ["home", "extra"]
        assert saved["overrides"]["enr-old-1"]["rules"][0]["ring_buttons"] == ["porch"]
        assert saved["overrides"]["enr-live"]["roster_hide"] == ["door-x"]
        assert saved["dashboards"]["kitchen"]["ha_user_id"] == "u-k"
        assert saved["template"]["pages"][0]["id"] == "garden" and saved["template"]["pages"][0]["label"] == "Garden"


async def test_v2_file_loads_as_v4_and_keeps_every_override(hass, hass_storage):
    v2 = _v2_data(dashboards={"kitchen": {"name": "Kitchen", "ha_user_id": "u-k", "room": "kitchen",
                                          "cameras": ["camera.gate"], "override": "kitchen"}})
    hass_storage["portico.content"] = _stored(json.loads(json.dumps(v2)))
    before = json.loads(json.dumps(hass_storage["portico.content"]))
    store = get_content_store(hass)
    await store.async_ensure_loaded()  # no phone is enrolled: v2 is keyed by endpoint id and needs none
    assert hass_storage["portico.content"] == before            # loading writes nothing
    (page,) = store.pages_for_endpoint("enr-1")
    assert page == {"id": "home", "label": "Home",
                    "blocks": [{"id": "home", "title": "Home", **normalize_profile(_profile())}]}
    assert store.home_name == "My Home"
    assert store.dashboards == v2["dashboards"]
    assert store.template["roster"] == ["*"]                    # v2's [] ("not set") is everyone (R6-05)
    await store.async_set_home_name("My Home B")
    saved = hass_storage["portico.content"]["data"]
    # the file holds the blocks and nothing else on a page (B8 dropped the flat mirror)
    assert saved["overrides"]["enr-1"]["pages"] == [page]
    assert saved["dashboards"] == v2["dashboards"]
    assert saved["endpoints"] == {} and saved["template"]["roster"] == ["*"]
    # and the next load of the v4 file is the same document
    from ..content import ContentStore
    again = ContentStore(hass)
    await again.async_ensure_loaded()
    assert again._data() == saved
    assert again._disk_data() == saved


async def test_migrate_v2_to_v3_is_pure_and_drops_only_invalid_rules(caplog):
    from ..content import migrate_v2_to_v3
    good = {"id": "doors", "caller": "kind:door", "cameras": None, "ring_buttons": ["a"], "call_buttons": []}
    bad = {"id": "Bad Id", "caller": "kind:door"}
    orphan = {"pages": [{"id": "home", "cameras": [], "actions": []}], "rules": [good, bad], "roster_hide": ["x"]}
    v2 = {"schema_version": 2, "template": {"pages": [], "rules": [good, bad], "roster": []},
          "overrides": {"gone": orphan}, "dashboards": {"k": {"name": "K"}}, "home_name": "H"}
    snapshot = json.loads(json.dumps(v2))
    with caplog.at_level("WARNING"):
        out = migrate_v2_to_v3(v2)
    assert v2 == snapshot                                           # the input is not touched
    assert out["schema_version"] == 3 and out["endpoints"] == {} and out["home_name"] == "H"
    assert out["template"]["rules"] == [good] and out["template"]["roster"] == ["*"]
    assert out["overrides"]["gone"]["rules"] == [good] and out["overrides"]["gone"]["roster_hide"] == ["x"]
    assert out["overrides"]["gone"]["pages"][0]["label"] == "Home"
    assert out["dashboards"] == {"k": {"name": "K"}}                # a dashboard is carried over untouched
    assert caplog.text.count("dropped 1 stored rule") == 2
    assert migrate_v2_to_v3({"template": {"roster": ["a", "b"]}})["template"]["roster"] == ["a", "b"]
    assert migrate_v2_to_v3({"template": {"roster": "garbage"}})["template"]["roster"] == ["*"]


async def test_a_newer_schema_is_read_but_never_rewritten(hass, hass_storage, caplog):
    """HA-L9: a file from a newer build is read, but every write is refused so it is not downgraded."""
    page = {"id": "home", "label": "Home", "blocks": [{"id": "home", "title": "Home", **normalize_profile(_profile())}]}
    newer = _stored({
        "schema_version": 7, "home_name": "My Home", "future_field": {"x": 1},
        "template": {"pages": [], "rules": [], "roster": ["*"]},
        "overrides": {"enr-1": {"pages": [page], "rules": [], "roster_hide": []}}, "dashboards": {}})
    hass_storage["portico.content"] = json.loads(json.dumps(newer))
    store = get_content_store(hass)
    with caplog.at_level("WARNING"):
        await store.async_ensure_loaded()
    assert "newer than this build knows" in caplog.text
    assert store.home_name == "My Home" and store.pages_for_endpoint("enr-1") == [page]  # still read
    with pytest.raises(ProfileError, match="schema 7"):
        await store.async_set_home_name("Other")
    with pytest.raises(ProfileError, match="schema 7"):
        await store.async_set("enr-1", _profile())
    with pytest.raises(ProfileError, match="schema 7"):
        await store.async_remove("u", "enr-1")
    with pytest.raises(ProfileError, match="schema 7"):
        await store.async_delete_orphan("enr-1")
    assert store.home_name == "My Home" and store.pages_for_endpoint("enr-1") == [page]  # no in-memory drift
    assert hass_storage["portico.content"] == newer


async def test_older_build_reads_v4_and_refuses_writes(hass, hass_storage, monkeypatch):
    """The rollback story of E1 (HA-L9): the v3 build, started on a v4 file, reads it, refuses every write and
    never rewrites it as v3. Simulated with this code's SCHEMA_VERSION set back to 3; the real v3 loader reading the
    mirror is `test_content_blocks.py`."""
    from .. import content
    from ..content import ContentStore
    store = get_content_store(hass)
    await store.async_set("enr-1", _profile())          # writes a v4 file
    saved = json.loads(json.dumps(hass_storage["portico.content"]))
    assert saved["data"]["schema_version"] == 4
    monkeypatch.setattr(content, "SCHEMA_VERSION", 3)
    older = ContentStore(hass)
    await older.async_ensure_loaded()
    assert older.profile_for("enr-1")["actions"]                                # still read
    with pytest.raises(ProfileError, match="schema 4"):
        await older.async_set("enr-1", _profile())
    with pytest.raises(ProfileError, match="schema 4"):
        await older.async_set_home_name("X")
    assert hass_storage["portico.content"] == saved                             # never rewritten


def _wrapped(v3_page):
    """A v3 page `{id, label, cameras, actions}` as the v4 page the loader makes of it: one block, id = the page id, title = the label."""
    return {"id": v3_page["id"], "label": v3_page["label"], "blocks": [
        {"id": v3_page["id"], "title": v3_page["label"], "cameras": v3_page["cameras"], "actions": v3_page["actions"]}]}


async def test_template_pages_are_laid_under_an_override(hass, hass_storage):
    tpl = {"id": "home", "label": "Home", **normalize_profile(_profile())}
    extra = {"id": "garage", "label": "Garage", "cameras": [], "actions": []}
    mine = {"id": "home", "label": "Mine", "cameras": [], "actions": []}
    hass_storage["portico.content"] = _stored({
        "schema_version": 3, "template": {"pages": [tpl, extra], "rules": [], "roster": ["*"]},
        "overrides": {"enr-1": {"pages": [mine], "rules": [], "roster_hide": []}}, "dashboards": {}, "endpoints": {}})
    store = get_content_store(hass)
    await store.async_ensure_loaded()
    assert store.pages_for_endpoint("enr-1") == [_wrapped(mine), _wrapped(extra)]   # the override's home replaces the template's
    assert store.pages_for_endpoint("enr-2") == [_wrapped(tpl), _wrapped(extra)]    # no override: the template
    assert [p["id"] for p in store.resolve_endpoint("enr-2")["pages"]] == ["home", "garage"]
    assert [p["label"] for p in store.resolve_endpoint("enr-2")["pages"]] == ["Home", "Garage"]
    assert len(store.profile_for("enr-2")["actions"]) == 2          # a user with no override sees the template's home


GET_KEYS = {"endpoint", "kind", "rev", "pages", "callers", "roster"}      # contract 3: the endpoint's document


async def test_get_is_the_endpoint_document_and_nothing_flat(hass, hass_ws_client, hass_read_only_user,
                                                             hass_read_only_access_token):
    """Contract 3 (M6 E3): `content/get` is the endpoint's document (`endpoint`, `kind`, `rev`, `pages`, `callers`,
    `roster`, and `home_name` when set). v1's flat `phone`/`cameras`/`actions` are gone, and so are the request keys
    `profile` and `pages` (the schema refuses them)."""
    await _enroll(hass, hass_read_only_user)
    await get_content_store(hass).async_set(hass_read_only_user.id, _profile())
    client = await hass_ws_client(hass, hass_read_only_access_token)
    await client.send_json({"id": 1, "type": "portico/content/get"})
    plain = (await client.receive_json())["result"]
    assert set(plain) == GET_KEYS and plain["kind"] == "phone"
    assert [p["id"] for p in plain["pages"]] == ["home"]
    for key in ("phone", "cameras", "actions"):
        assert key not in plain
    await client.send_json({"id": 2, "type": "portico/content/get", "pages": True})
    assert (await client.receive_json())["error"]["code"] == "invalid_format"


def _two_page_template(store, hass_storage):
    garden = {"id": "garden", "label": "Garden", "cameras": [{"id": "lawn", "label": "Lawn", "entity_id": "camera.lawn"}],
              "actions": [{"id": "sprinkle", "label": "Sprinkle", "kind": "script_run", "entity_id": "script.sprinkle",
                           "confirm": False}]}
    hass_storage["portico.content"] = _stored({
        "schema_version": 3, "template": {"pages": [garden], "rules": [], "roster": ["*"]},
        "overrides": {}, "dashboards": {}, "endpoints": {}})
    return garden


async def test_run_and_snapshot_are_granted_over_every_page_of_the_set(
        hass, hass_ws_client, hass_read_only_user, hass_read_only_access_token, hass_storage):
    """The grant is the caller's resolved set: a button or camera on a non-home page runs/serves; one on no page
    does not, and another phone's override page is not in the set."""
    await _enroll(hass, hass_read_only_user)
    _two_page_template(None, hass_storage)
    calls = async_mock_service(hass, "script", "turn_on")
    hass.states.async_set("script.sprinkle", "off")
    store = get_content_store(hass)
    await store.async_ensure_loaded()
    await store.async_set(hass_read_only_user.id, _profile())          # the phone's own home page
    client = await hass_ws_client(hass, hass_read_only_access_token)
    await client.send_json({"id": 1, "type": "portico/content/run", "action_id": "sprinkle"})
    assert (await client.receive_json())["result"] == {"ran": True}
    assert len(calls) == 1
    await client.send_json({"id": 2, "type": "portico/content/run", "action_id": "nope"})
    assert (await client.receive_json())["error"]["code"] == "unknown_action"
    # a camera on the garden page is a grant for the snapshot (it gets as far as "unavailable", not "unknown")
    await client.send_json({"id": 3, "type": "portico/content/snapshot", "camera_id": "lawn"})
    assert (await client.receive_json())["error"]["code"] == "unavailable"
    await client.send_json({"id": 4, "type": "portico/content/snapshot", "camera_id": "elsewhere"})
    assert (await client.receive_json())["error"]["code"] == "unknown_camera"


async def test_the_flat_get_is_the_home_page_whatever_other_pages_exist(
        hass, hass_ws_client, hass_read_only_user, hass_read_only_access_token, hass_storage):
    """v1 wire: a phone with extra pages still reads exactly its home page flat; `rev` is unchanged while the
    set is the one default-labelled home page and changes when another page is added."""
    await _enroll(hass, hass_read_only_user)
    store = get_content_store(hass)
    await store.async_set(hass_read_only_user.id, _profile())
    plain_rev = _resolved(store, hass_read_only_user.id)["rev"]
    store.template["pages"].append({"id": "garden", "label": "Garden", "cameras": [], "actions": []})
    assert _resolved(store, hass_read_only_user.id)["rev"] != plain_rev
    client = await hass_ws_client(hass, hass_read_only_access_token)
    await client.send_json({"id": 1, "type": "portico/content/get"})
    result = (await client.receive_json())["result"]
    assert GET_KEYS <= set(result)
    assert [c["id"] for c in _served(result["pages"])["cameras"]] == ["gate"]


async def test_v1_flat_falls_back_to_the_first_page_when_there_is_no_home(
        hass, hass_ws_client, hass_read_only_user, hass_read_only_access_token, hass_storage):
    """No page with id "home" in the set (a household page set built in the panel): the flat reply serves the
    first page instead of an empty profile, with the same v1 keys, and the grants cover it."""
    await _enroll(hass, hass_read_only_user)
    _two_page_template(None, hass_storage)
    calls = async_mock_service(hass, "script", "turn_on")
    hass.states.async_set("script.sprinkle", "off")
    client = await hass_ws_client(hass, hass_read_only_access_token)
    await client.send_json({"id": 1, "type": "portico/content/get"})
    result = (await client.receive_json())["result"]
    assert GET_KEYS <= set(result)
    assert [c["id"] for c in _served(result["pages"])["cameras"]] == ["lawn"]
    assert [a["id"] for a in _served(result["pages"])["actions"]] == ["sprinkle"]
    await client.send_json({"id": 2, "type": "portico/content/run", "action_id": "sprinkle"})
    assert (await client.receive_json())["result"] == {"ran": True}
    assert len(calls) == 1
    await client.send_json({"id": 3, "type": "portico/content/snapshot", "camera_id": "lawn"})
    assert (await client.receive_json())["error"]["code"] == "unavailable"       # granted, camera just not there


async def test_home_page_still_wins_over_first(
        hass, hass_ws_client, hass_read_only_user, hass_read_only_access_token, hass_storage):
    """With a "home" page present it is served flat even when it is not the first page."""
    await _enroll(hass, hass_read_only_user)
    garden = {"id": "garden", "label": "Garden", "cameras": [{"id": "lawn", "label": "Lawn", "entity_id": "camera.lawn"}],
              "actions": []}
    home = {"id": "home", "label": "Front", "cameras": [{"id": "gate", "label": "Gate", "entity_id": "camera.gate"}],
            "actions": []}
    hass_storage["portico.content"] = _stored({
        "schema_version": 3, "template": {"pages": [garden, home], "rules": [], "roster": ["*"]},
        "overrides": {}, "dashboards": {}, "endpoints": {}})
    client = await hass_ws_client(hass, hass_read_only_access_token)
    await client.send_json({"id": 1, "type": "portico/content/get"})
    result = (await client.receive_json())["result"]
    assert [c["id"] for c in _served(result["pages"])["cameras"]] == ["gate"]


async def test_flat_rev_changes_when_the_served_page_changes(hass, hass_read_only_user, hass_storage):
    """The flat rev follows the served (fallback) page, so changing it fires content_changed for a v1 client."""
    await _enroll(hass, hass_read_only_user)
    _two_page_template(None, hass_storage)
    store = get_content_store(hass)
    await store.async_ensure_loaded()
    uid = hass_read_only_user.id
    first = _resolved(store, uid)["rev"]
    store.template["pages"][0]["blocks"][0]["cameras"].append({"id": "pool", "label": "Pool", "entity_id": "camera.pool"})
    second = _resolved(store, uid)["rev"]
    assert second != first
    assert store.resolve_document(store._endpoint_id(uid))["rev"] == second


async def test_a_page_label_defaults_to_the_id_title_cased_and_is_validated():
    from ..content import normalize_page, normalize_pages
    assert normalize_page({"id": "garden-lights"})["label"] == "Garden Lights"
    assert normalize_page({"id": "x", "label": " Mine "})["label"] == "Mine"
    with pytest.raises(ProfileError):
        normalize_page({"id": "x", "label": ""})
    with pytest.raises(ProfileError):
        normalize_page({"id": "x", "label": "L" * 41})
    with pytest.raises(ProfileError, match="at most 12"):
        normalize_pages([{"id": f"p{n}"} for n in range(13)])
    with pytest.raises(ProfileError):
        normalize_pages([{"id": "a"}, {"id": "a"}])


async def test_configure_flow_save_keeps_the_home_label_and_the_other_pages(hass, hass_storage, hass_read_only_user):
    _add_device(hass, enrolled_phones=[{"ha_user_id": hass_read_only_user.id, "name": "P", "enroll_id": "enr-1"}])
    page = lambda i, label: {"id": i, "label": label, "cameras": [], "actions": []}   # noqa: E731
    hass_storage["portico.content"] = _stored({
        "schema_version": 3, "template": {"pages": [page("home", "Front")], "rules": [], "roster": ["*"]},
        "overrides": {"enr-1": {"pages": [page("extra", "Extra")], "rules": [], "roster_hide": []}},
        "dashboards": {}, "endpoints": {}})
    store = get_content_store(hass)
    await store.async_set(hass_read_only_user.id, _profile())
    pages = hass_storage["portico.content"]["data"]["overrides"]["enr-1"]["pages"]
    assert [(p["id"], p["label"]) for p in pages] == [("home", "Front"), ("extra", "Extra")]


# ---- "Not attached" overrides: move / delete -------------------------------

async def _orphan_setup(hass, hass_storage, hass_read_only_user):
    _add_device(hass, enrolled_phones=[{"ha_user_id": hass_read_only_user.id, "name": "P", "enroll_id": "enr-1"}])
    page = lambda i: {"id": i, "label": i.title(), "cameras": [], "actions": []}      # noqa: E731
    rule = {"id": "r", "caller": "kind:door", "cameras": None, "ring_buttons": [], "call_buttons": []}
    hass_storage["portico.content"] = _stored({
        "schema_version": 3, "template": {"pages": [page("home")], "rules": [rule], "roster": ["*"]},
        "overrides": {"enr-1": {"pages": [page("home")], "rules": [rule], "roster_hide": ["a"]},
                      "enr-old": {"pages": [page("home"), page("extra")], "rules": [rule], "roster_hide": ["b"]}},
        "dashboards": {}, "endpoints": {}})
    store = get_content_store(hass)
    await store.async_ensure_loaded()
    return store


async def test_orphans_are_listed_and_only_orphans_can_be_moved_or_deleted(hass, hass_storage, hass_read_only_user):
    store = await _orphan_setup(hass, hass_storage, hass_read_only_user)
    assert store.orphan_keys() == ["enr-old"]
    for call in (store.async_delete_orphan("enr-1"), store.async_move_orphan_to_template("enr-1"),
                 store.async_move_orphan_to_endpoint("enr-1", "enr-1"), store.async_delete_orphan("nobody")):
        with pytest.raises(ProfileError, match="Not attached"):
            await call
    assert sorted(store.overrides) == ["enr-1", "enr-old"]


async def test_move_an_orphan_to_a_phone_keeps_the_targets_own_and_renames_clashes(hass, hass_storage, hass_read_only_user):
    store = await _orphan_setup(hass, hass_storage, hass_read_only_user)
    heard = []
    store.add_listener(heard.append)
    await store.async_move_orphan_to_endpoint("enr-old", "enr-1")
    assert store.orphan_keys() == [] and "enr-old" not in store.overrides
    target = hass_storage["portico.content"]["data"]["overrides"]["enr-1"]
    assert [(p["id"], p["label"]) for p in target["pages"]] == [("home", "Home"), ("home-2", "Home (moved)"),
                                                                ("extra", "Extra")]
    assert [r["id"] for r in target["rules"]] == ["r", "r-2"] and target["roster_hide"] == ["a", "b"]
    assert heard == ["enr-1"]        # the listeners hear the endpoint's id (M6 K1), not its HA user's
    with pytest.raises(ProfileError, match="not a phone or a screen"):
        await _orphan_to_door(store, hass_storage)


async def _orphan_to_door(store, hass_storage):
    store.overrides["enr-old2"] = {"pages": [], "rules": [], "roster_hide": []}
    await store.async_move_orphan_to_endpoint("enr-old2", "portico_intercom:door")


async def test_move_an_orphan_into_the_template_and_delete_one(hass, hass_storage, hass_read_only_user):
    store = await _orphan_setup(hass, hass_storage, hass_read_only_user)
    heard = []
    store.add_home_listener(lambda: heard.append("all"))
    await store.async_move_orphan_to_template("enr-old")
    data = hass_storage["portico.content"]["data"]
    assert [p["id"] for p in data["template"]["pages"]] == ["home", "home-2", "extra"]
    assert [r["id"] for r in data["template"]["rules"]] == ["r", "r-2"]
    assert list(data["overrides"]) == ["enr-1"] and data["overrides"]["enr-1"]["roster_hide"] == ["a"]
    assert heard == ["all"]
    store.overrides["gone"] = {"pages": [], "rules": [], "roster_hide": []}
    await store.async_delete_orphan("gone")
    assert "gone" not in hass_storage["portico.content"]["data"]["overrides"]


async def test_a_move_past_the_page_limit_is_refused_and_changes_nothing(hass, hass_storage, hass_read_only_user):
    store = await _orphan_setup(hass, hass_storage, hass_read_only_user)
    store.template["pages"].extend({"id": f"p{n}", "label": "P", "cameras": [], "actions": []} for n in range(11))
    with pytest.raises(ProfileError, match="limit"):
        await store.async_move_orphan_to_template("enr-old")
    assert "enr-old" in store.overrides and len(store.template["pages"]) == 12


# ---- rules, roster, declarations (the validators) -----------------------------

async def test_normalize_rule_and_roster_and_declaration():
    from ..content import (
        check_rule_buttons, normalize_endpoint_declaration, normalize_roster, normalize_rule, normalize_rules)
    rule = normalize_rule({"id": "doors", "caller": "kind:door", "cameras": ["camera.a"], "ring_buttons": ["x", "x"]})
    assert rule == {"id": "doors", "caller": "kind:door", "cameras": ["camera.a"], "ring_buttons": ["x"],
                    "call_buttons": []}
    assert normalize_rule({"id": "r", "caller": "portico_intercom:http://10.6.0.7:8091"})["cameras"] is None
    for bad in ({"id": "r"}, {"id": "r", "caller": "kind:cat"}, {"id": "R", "caller": "kind:door"},
                {"id": "r", "caller": "kind:door", "cameras": ["lock.a"]},
                {"id": "r", "caller": "kind:door", "ring_buttons": [f"b{n}" for n in range(9)]},
                {"id": "r", "caller": "kind:door", "call_buttons": ["Bad Id"]}, "x"):
        with pytest.raises(ProfileError):
            normalize_rule(bad)
    with pytest.raises(ProfileError, match="used twice"):
        normalize_rules([rule, rule])
    with pytest.raises(ProfileError, match="at most 12"):
        normalize_rules([{"id": f"r{n}", "caller": "kind:door"} for n in range(13)])
    pages = [{"id": "home", "label": "Home", "blocks": [{"id": "home", "title": "Home", "cameras": [], "actions": [
        {"id": "x", "label": "X", "kind": "lock_open", "entity_id": "lock.a", "confirm": True}]}]}]
    check_rule_buttons([rule], pages, "template")
    with pytest.raises(ProfileError, match="rule 'doors'.*'y'"):
        check_rule_buttons([{**rule, "call_buttons": ["y"]}], pages, "template")
    assert normalize_roster(["*"]) == ["*"] and normalize_roster([]) == [] and normalize_roster(["a", "b"]) == ["a", "b"]
    for bad in (["*", "a"], ["a", "a"], [""], "*", [1]):
        with pytest.raises(ProfileError):
            normalize_roster(bad)
    assert normalize_endpoint_declaration({"cameras": ["camera.a"]}) == {"cameras": ["camera.a"]}
    assert normalize_endpoint_declaration({}) == {"cameras": []}
    with pytest.raises(ProfileError):
        normalize_endpoint_declaration({"cameras": ["lock.a"]})


async def test_a_dashboard_needs_its_user_and_a_v3_file_round_trips_rules_roster_and_declarations(hass, hass_storage):
    from ..content import ContentStore
    rule = {"id": "doors", "caller": "kind:door", "cameras": ["camera.drive"], "ring_buttons": ["open"], "call_buttons": []}
    hass_storage["portico.content"] = _stored({
        "schema_version": 3,
        "template": {"pages": [], "rules": [rule, {"id": "BAD"}], "roster": []},          # [] in v3 = nobody
        "overrides": {}, "endpoints": {"enr-1": {"cameras": ["camera.phone"]}, "enr-2": {"cameras": ["lock.x"]}},
        "dashboards": {"kitchen": {"name": "K", "ha_user_id": "u-k", "cameras": ["camera.k"]},
                       "nouser": {"name": "N"}}})
    store = get_content_store(hass)
    await store.async_ensure_loaded()
    assert store.template["rules"] == [rule] and store.template["roster"] == []
    assert store.endpoint_declarations == {"enr-1": {"cameras": ["camera.phone"]}}
    assert list(store.dashboards) == ["kitchen"]
    await store.async_set_home_name("H")
    saved = hass_storage["portico.content"]["data"]
    assert saved["template"]["roster"] == [] and saved["endpoints"] == {"enr-1": {"cameras": ["camera.phone"]}}
    again = ContentStore(hass)
    await again.async_ensure_loaded()
    assert again._data() == saved


async def test_set_after_enrollment_keys_by_enroll_id_and_survives_a_restart(hass, hass_storage, hass_read_only_user):
    _add_device(hass, enrolled_phones=[{"ha_user_id": hass_read_only_user.id, "name": "P", "enroll_id": "enr-9"}])
    store = get_content_store(hass)
    await store.async_set(hass_read_only_user.id, _profile())
    assert list(hass_storage["portico.content"]["data"]["overrides"]) == ["enr-9"]
    from ..content import ContentStore
    again = ContentStore(hass)
    await again.async_ensure_loaded()
    assert again.profile_for(hass_read_only_user.id) == store.profile_for(hass_read_only_user.id)
    await again.async_remove(hass_read_only_user.id)
    assert hass_storage["portico.content"]["data"]["overrides"] == {}


async def test_the_content_portico_open_run_reaches_the_ha_action(hass, hass_ws_client, hass_read_only_user,
                                                                  hass_read_only_access_token):
    """The live bug (DEV-643): a gate_sip_observer door has no relay, so `portico_open` through content/run must
    run the installer's HA action. SAFETY: the press is a recorded fake; no real action runs."""
    entry, runtime = await _enroll(hass, hass_read_only_user, device_id="10.0.0.9",
                                   capabilities={"DirectCapable": False, "HasRelay": False})
    button = "button.example_intercom_ex_gate_open_door"
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "open_relay_action": [{"action": "button.press", "target": {"entity_id": button}}]})
    presses = async_mock_service(hass, "button", "press")
    profile = {"actions": [{"id": "gate-relay", "label": "Open gate", "kind": "portico_open",
                            "device_id": runtime.device_id}]}
    await get_content_store(hass).async_set(hass_read_only_user.id, profile)
    client = await hass_ws_client(hass, hass_read_only_access_token)

    resp = await _run(client, "gate-relay")
    assert resp["success"], resp
    await hass.async_block_till_done()
    assert len(presses) == 1 and presses[0].data == {"entity_id": [button]}
    runtime.client.open_relay.assert_not_awaited()
