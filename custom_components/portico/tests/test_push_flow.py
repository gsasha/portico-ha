# ruff: noqa: F811 - test parameters named after the imported fixtures
"""The "Push ring notification (Firebase)" config-flow branch and the push options flow
(sender design §2.3, §3.3, §5.2; decisions 1, 2, 4).

HA-harness session; the live token check goes to the local fake Google only.
"""
from __future__ import annotations

import json

import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.portico import push_flow
from custom_components.portico.config_flow import DOMAIN
from custom_components.portico.push import const

from .driver_part import phones_host
from .fake_google import CLIENT_EMAIL, KEY_ID, service_account_info
from .test_push_registry import (  # noqa: F401 - fixtures by name
    REG,
    _custom_integrations,
    _manager,
    _observer_entry,
    _phone_user,
    _register_msg,
    _setup_all,
    fake_transport,
    google,
)
from .test_push_wire import GOOGLE_SERVICES


async def _start(hass):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    # "Add integration > Portico" is a menu (a door, or the push ring notification); the push ring notification is the credentials form
    assert result["type"] is FlowResultType.MENU and result["menu_options"] == ["door", "push_credentials", "sip_server_door", "sip_server_remote"]
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "push_credentials"})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "push_credentials"
    return result


def _sa(google, **changes) -> str:
    return json.dumps({**service_account_info(google.token_uri), **changes})


def _key_body(google) -> str:
    return service_account_info(google.token_uri)["private_key"].splitlines()[1]


async def test_valid_service_account_creates_the_push_entry(hass, hass_storage, google):
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {push_flow.F_SA_JSON: _sa(google), push_flow.F_GS_JSON: json.dumps(GOOGLE_SERVICES)})
    assert result["type"] is FlowResultType.CREATE_ENTRY, result.get("errors")
    entry = result["result"]
    assert entry.unique_id == const.PUSH_UNIQUE_ID
    assert entry.data == {
        const.ENTRY_TYPE: const.ENTRY_TYPE_PUSH, "project_id": "portico-test", "client_email": CLIENT_EMAIL,
        "private_key_id": KEY_ID, "package": const.PORTICO_PACKAGE,
        "firebase": {"project_id": "portico-test", "sender_id": "123456789012",
                     "app_id": "1:123456789012:android:bbbb", "api_key": "AIza-portico-key"},
    }
    # A fresh install: new phones get the push ring notification (decision Q3), and the
    # test allow-list starts empty.
    assert entry.options == {const.CONF_ENABLED: True, const.CONF_RING_WINDOW: 45,
                             const.CONF_NEW_PHONES_ENABLED: True, const.CONF_TEST_PHONES: []}
    # A live token fetch (the flow's, then the new entry's own prefetch), and
    # no message (§2.3).
    assert 1 <= len(google.token_requests) <= 2 and google.fcm_requests == []
    # The key is in the private Store and nowhere in the entry.
    assert hass_storage[const.CREDENTIALS_STORE_KEY]["data"]["private_key"].splitlines()[1] == _key_body(google)
    assert _key_body(google) not in json.dumps(entry.as_dict(), default=str)
    assert "private_key\"" not in json.dumps(entry.as_dict()["data"], default=str)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.portico_push_ring_notification").state == "ok"


async def test_google_services_is_optional(hass, google):
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {push_flow.F_SA_JSON: _sa(google)})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert "firebase" not in result["result"].data


async def test_an_uploaded_file_is_read(hass, google, monkeypatch):
    sa_id, gs_id = "0" * 31 + "1", "0" * 31 + "2"  # the file selector wants UUIDs
    files = {sa_id: _sa(google), gs_id: json.dumps(GOOGLE_SERVICES)}

    async def _fake_read(_hass, file_id):
        return files[file_id]

    monkeypatch.setattr(push_flow, "_read_upload", _fake_read)
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {push_flow.F_SA_FILE: sa_id, push_flow.F_GS_FILE: gs_id})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].data["firebase"]["app_id"] == "1:123456789012:android:bbbb"


@pytest.mark.parametrize(("value", "error"), [
    ("{not json", "sa_invalid_json"),
    (json.dumps({"type": "authorized_user"}), "sa_not_service_account"),
    ("KEY", "sa_invalid_key"),
    ("GOOGLE_URI", "sa_token_uri_not_allowed"),
    ("NO_EMAIL", "sa_missing_fields"),
])
async def test_an_invalid_service_account_is_a_field_error(hass, hass_storage, google, value, error):
    if value == "KEY":
        value = _sa(google, private_key="-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n")
    elif value == "GOOGLE_URI":
        value = _sa(google, token_uri="https://evil.example/token")
    elif value == "NO_EMAIL":
        value = _sa(google, client_email="")
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {push_flow.F_SA_JSON: value})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {push_flow.F_SA_JSON: error}
    assert google.token_requests == []
    assert const.CREDENTIALS_STORE_KEY not in hass_storage
    assert _key_body(google) not in json.dumps(result, default=str)
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_google_refusing_the_key_is_a_field_error(hass, hass_storage, google):
    google.token_script.append((400, {"error": "invalid_grant"}, {}))
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {push_flow.F_SA_JSON: _sa(google)})
    assert result["errors"] == {push_flow.F_SA_JSON: "sa_rejected"}
    assert const.CREDENTIALS_STORE_KEY not in hass_storage


async def test_google_unreachable_is_cannot_connect(hass, google):
    google.token_script.append((503, {}, {}))
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {push_flow.F_SA_JSON: _sa(google)})
    assert result["errors"] == {"base": "cannot_connect"}


async def test_nothing_given_is_refused(hass, google):
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {push_flow.F_SA_JSON: "  "})
    assert result["errors"] == {"base": "service_account_required"}


@pytest.mark.parametrize(("services", "error"), [
    ("nope", "gs_invalid_json"),
    (json.dumps({**GOOGLE_SERVICES, "client": GOOGLE_SERVICES["client"][:1]}), "gs_package_not_found"),
    (json.dumps({**GOOGLE_SERVICES, "project_info": {**GOOGLE_SERVICES["project_info"], "project_id": "other-x"}}),
     "gs_project_mismatch"),
])
async def test_google_services_errors(hass, google, services, error):
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {push_flow.F_SA_JSON: _sa(google), push_flow.F_GS_JSON: services})
    assert result["errors"] == {push_flow.F_GS_JSON: error}


async def test_only_one_push_entry(hass, google):
    for expected in (FlowResultType.CREATE_ENTRY, FlowResultType.ABORT):
        result = await _start(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {push_flow.F_SA_JSON: _sa(google)})
        assert result["type"] is expected
    assert result["reason"] == "already_configured"


async def test_removing_the_entry_deletes_the_key_and_registry(hass, hass_storage, google):
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {push_flow.F_SA_JSON: _sa(google)})
    await hass.async_block_till_done()
    assert const.CREDENTIALS_STORE_KEY in hass_storage
    assert await hass.config_entries.async_remove(result["result"].entry_id)
    await hass.async_block_till_done()
    assert const.CREDENTIALS_STORE_KEY not in hass_storage
    assert const.REGISTRY_STORE_KEY not in hass_storage


# --------------------------------------------------------------------------
# Options
# --------------------------------------------------------------------------


async def _options(hass, entry, step):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    return await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": step})


async def test_settings(hass, hass_storage, google, fake_transport):
    _d, push = await _setup_all(hass, hass_storage, google)
    result = await _options(hass, push, "push_settings")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {const.CONF_ENABLED: False, const.CONF_RING_WINDOW: 30})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    # The entry predates the option (none stored): the form offered OFF, and
    # saving the form makes that explicit.
    assert push.options == {const.CONF_ENABLED: False, const.CONF_RING_WINDOW: 30,
                            const.CONF_NEW_PHONES_ENABLED: False}
    await hass.async_block_till_done()
    assert _manager(hass).ring_window == 30
    assert hass.states.get("sensor.portico_push_ring_notification").state == "disabled"


async def test_settings_text_options_saved_cleared_and_validated(hass, hass_storage, google, fake_transport):
    """DEV-836: wheel_base_url, kiosk_background_url and kiosk_back_path are settable here; blank clears;
    an invalid value re-shows the form with an error and saves nothing; other options are kept."""
    _d, push = await _setup_all(hass, hass_storage, google)
    hass.config_entries.async_update_entry(push, options={**push.options, "other_option": "keep"})
    base = {const.CONF_ENABLED: True, const.CONF_RING_WINDOW: 45, const.CONF_NEW_PHONES_ENABLED: True}
    values = {"wheel_base_url": "https://git.example.com/owner/portico-ha",
              "kiosk_background_url": "/local/kiosk.jpg", "kiosk_back_path": "/lovelace/home"}

    result = await _options(hass, push, "push_settings")
    result = await hass.config_entries.options.async_configure(result["flow_id"], {**base, **values})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert push.options["other_option"] == "keep"
    for key, value in values.items():
        assert push.options[key] == value

    # An invalid value: the form comes back with the error, the entry is unchanged.
    result = await _options(hass, push, "push_settings")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**base, "wheel_base_url": "ftp://x", "kiosk_background_url": "//evil/x.jpg",
                            "kiosk_back_path": "lovelace"})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"wheel_base_url": "invalid_wheel_base_url",
                                "kiosk_background_url": "invalid_kiosk_background_url",
                                "kiosk_back_path": "invalid_kiosk_back_path"}
    assert push.options["wheel_base_url"] == values["wheel_base_url"]

    # Blank (or omitted) clears one option and leaves the rest alone.
    result = await _options(hass, push, "push_settings")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**base, "wheel_base_url": "", "kiosk_background_url": values["kiosk_background_url"]})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert "wheel_base_url" not in push.options and "kiosk_back_path" not in push.options
    assert push.options["kiosk_background_url"] == "/local/kiosk.jpg"
    assert push.options["other_option"] == "keep"


async def test_settings_form_offers_stored_text_options(hass, hass_storage, google, fake_transport):
    _d, push = await _setup_all(hass, hass_storage, google)
    hass.config_entries.async_update_entry(push, options={**push.options, "kiosk_back_path": "/lovelace/home"})
    result = await _options(hass, push, "push_settings")
    schema = result["data_schema"].schema
    keys = {str(k): k for k in schema}
    assert {"wheel_base_url", "kiosk_background_url", "kiosk_back_path"} <= keys.keys()
    assert keys["kiosk_back_path"].description == {"suggested_value": "/lovelace/home"}


async def test_phones_toggle_and_remove(hass, hass_storage, hass_ws_client, google, fake_transport):
    alice, alice_token = await _phone_user(hass, "a")
    bob, bob_token = await _phone_user(hass, "b")
    _d, push = await _setup_all(hass, hass_storage, google, enrolled=[(alice.id, "Alice"), (bob.id, "Bob")])
    ids = {}
    for name, token in (("Alice", alice_token), ("Bob", bob_token)):
        client = await hass_ws_client(hass, access_token=token)
        await client.send_json(_register_msg())
        ids[name] = (await client.receive_json())["result"]["reg_id"]

    result = await _options(hass, push, "push_phones")
    assert result["step_id"] == "push_phones"
    assert set(result["data_schema"].schema["enabled_phones"].options) == set(ids.values())
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"enabled_phones": [ids["Alice"]], "remove_phones": [ids["Bob"]]})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    registry = _manager(hass).registry
    assert registry.get(ids["Alice"])["push_enabled"] is True
    assert registry.get(ids["Bob"]) is None
    assert push.options == {}  # untouched


async def test_key_rotation_same_project_only(hass, hass_storage, google, fake_transport):
    _d, push = await _setup_all(hass, hass_storage, google)
    result = await _options(hass, push, "push_key")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {push_flow.F_SA_JSON: _sa(google, project_id="another-project")})
    assert result["errors"] == {push_flow.F_SA_JSON: "sa_project_changed"}

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {push_flow.F_SA_JSON: _sa(google, private_key_id=KEY_ID, client_email=CLIENT_EMAIL)})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert push.data["private_key_id"] == KEY_ID
    assert _key_body(google) not in json.dumps(push.as_dict(), default=str)


async def test_an_old_door_entry_has_no_enrollment_options_flow_any_more(hass):
    """S-2: a `portico` entry with `data.driver` (a door from before the split) is refused at setup; its Configure
    only points at Portico Intercom."""
    entry = MockConfigEntry(domain=DOMAIN, data={"driver": "gate_sip_observer", "host": "gate.example"},
                            unique_id="gate_sip_observer:gate.example")
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "door_entry_moved")


# --------------------------------------------------------------------------
# G2 / decision Q3: "New phones get the push ring notification", an option on the push entry
# --------------------------------------------------------------------------


def _default(schema, key):
    for marker in schema.schema:
        if marker == key:
            return marker.default()
    raise AssertionError(f"{key} not in the form")


async def _flow_push_entry(hass, google, **fields):
    """The push entry made through the real config flow, beside an observer
    entry with one enrolled phone. Returns (push entry, phone token)."""
    user, token = await _phone_user(hass)
    device = _observer_entry(hass)
    phones_host(hass, [{"ha_user_id": user.id, "name": "Pixel"}])
    assert await hass.config_entries.async_setup(device.entry_id)
    await hass.async_block_till_done()
    result = await _start(hass)
    assert _default(result["data_schema"], const.CONF_NEW_PHONES_ENABLED) is True
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {push_flow.F_SA_JSON: _sa(google), **fields})
    assert result["type"] is FlowResultType.CREATE_ENTRY, result.get("errors")
    await hass.async_block_till_done()
    return result["result"], token


async def _register(hass, hass_ws_client, token, n=1, **overrides):
    client = await hass_ws_client(hass, access_token=token)
    await client.send_json(_register_msg(n, **overrides))
    resp = await client.receive_json()
    assert resp["success"], resp
    return resp["result"]


async def _ring(hass, fake_transport, call_id):
    fake_transport.instances[-1].ring(call_id)
    await hass.async_block_till_done()
    await _manager(hass).notifier.async_drain()


async def test_fresh_install_new_phone_gets_the_away_ring(hass, hass_ws_client, google, fake_transport):
    push, token = await _flow_push_entry(hass, google)
    assert push.options[const.CONF_NEW_PHONES_ENABLED] is True
    result = await _register(hass, hass_ws_client, token)
    assert result["push_enabled"] is True
    await _ring(hass, fake_transport, "gso-1")
    assert [r["json"]["message"]["data"]["kind"] for r in google.fcm_requests] == ["ring"]


async def test_estate_sets_it_off_new_phone_gets_nothing(hass, hass_ws_client, google, fake_transport):
    push, token = await _flow_push_entry(hass, google, **{const.CONF_NEW_PHONES_ENABLED: False})
    assert push.options[const.CONF_NEW_PHONES_ENABLED] is False
    result = await _register(hass, hass_ws_client, token)
    assert result["push_enabled"] is False
    await _ring(hass, fake_transport, "gso-1")
    assert google.fcm_requests == []


async def test_per_phone_switch_still_works_with_the_option_on(hass, hass_ws_client, google, fake_transport):
    push, token = await _flow_push_entry(hass, google)
    reg_id = (await _register(hass, hass_ws_client, token))["reg_id"]
    result = await _options(hass, push, "push_phones")
    assert _default(result["data_schema"], "enabled_phones") == [reg_id]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"enabled_phones": [], "remove_phones": []})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert _manager(hass).registry.get(reg_id)["push_enabled"] is False
    await _ring(hass, fake_transport, "gso-1")
    assert google.fcm_requests == []
    # ... and back on.
    await _manager(hass).registry.async_set_enabled(reg_id, True)
    _manager(hass).notifier._device_rings.clear()
    await _ring(hass, fake_transport, "gso-2")
    assert len(google.fcm_requests) == 1


async def test_changing_the_option_leaves_existing_phones_alone(hass, hass_ws_client, google, fake_transport):
    push, token = await _flow_push_entry(hass, google, **{const.CONF_NEW_PHONES_ENABLED: False})
    first = await _register(hass, hass_ws_client, token, installation_id="inst-old")
    assert first["push_enabled"] is False

    result = await _options(hass, push, "push_settings")
    assert _default(result["data_schema"], const.CONF_NEW_PHONES_ENABLED) is False
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {const.CONF_ENABLED: True, const.CONF_RING_WINDOW: 45,
                            const.CONF_NEW_PHONES_ENABLED: True})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert push.options[const.CONF_NEW_PHONES_ENABLED] is True

    # The existing phone re-registers (every connect, a rotation): still OFF.
    again = await _register(hass, hass_ws_client, token, 2, installation_id="inst-old", registration=REG + "-r")
    assert again == {"reg_id": first["reg_id"], "push_enabled": False}
    # A new installation gets the new default.
    new = await _register(hass, hass_ws_client, token, 3, installation_id="inst-new")
    assert new["push_enabled"] is True and new["reg_id"] != first["reg_id"]


async def test_an_entry_from_before_the_option_keeps_new_phones_off(hass, hass_storage, hass_ws_client, google,
                                                                    fake_transport):
    """Migration: a push entry created before this option has no stored value.
    Missing means OFF (P1's behaviour), not the fresh-install ON."""
    user, token = await _phone_user(hass)
    _d, push = await _setup_all(hass, hass_storage, google, enrolled=[(user.id, "p")],
                                **{const.CONF_ENABLED: True, const.CONF_RING_WINDOW: 45})
    assert const.CONF_NEW_PHONES_ENABLED not in push.options
    assert _manager(hass).new_phones_enabled is False
    result = await _register(hass, hass_ws_client, token)
    assert result["push_enabled"] is False
    result = await _options(hass, push, "push_settings")
    assert _default(result["data_schema"], const.CONF_NEW_PHONES_ENABLED) is False
