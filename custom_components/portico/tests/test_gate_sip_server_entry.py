"""gate_sip_server, Python half (DEV-518 W2): the SECOND config entry of the SIP-server cut-over, and every way it is
made impossible to create or bind by accident. Offline: nothing here touches the network, the station, gate-sip, RPC2 or
a real Home Assistant; a flow that reaches the end creates an entry only in the test harness's own throwaway hass."""
from __future__ import annotations

import pytest
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.portico.door.const import DOMAIN, ENTRY_TYPE, ENTRY_TYPE_DOOR
from custom_components.portico.door.drivers import DRIVERS, load_drivers
from custom_components.portico.door.drivers import gate_sip_server as srv
from custom_components.portico.door.drivers.gate_sip_server import entry_conflicts, normalize

OK = {"sip_host": "127.0.0.1", "sip_port": 15060, "rtp_port": 45000, "password": "12345678", "confirm": True}
LAN = {"allow_non_loopback": True, "allow_from": "10.8.0.39"}


def _err(data) -> tuple[str, str]:
    with pytest.raises(vol.Invalid) as e:
        normalize(data)
    return str(e.value.path[0]), e.value.error_message


# --- the pure guards -------------------------------------------------------------------------------------------------

def test_defaults_are_loopback_and_high_ports():
    out = normalize({"password": "12345678", "confirm": True})
    assert out["sip_listen"] == "127.0.0.1:15060" and out["rtp_port"] == 45000
    assert out["allow_non_loopback"] is False and out["allow_from"] == []
    assert out["device_id"] == "gate-sip-server:127.0.0.1:15060"
    assert out["user"] == "8001" and out["realm"] == "VDP"
    assert "confirm" not in out
    schema_defaults = srv.CONFIG_SCHEMA({"password": "12345678"})
    assert schema_defaults["sip_host"] == "127.0.0.1" and schema_defaults["sip_port"] == 15060
    assert schema_defaults["rtp_port"] == 45000 and schema_defaults["confirm"] is False


def test_it_is_hidden_from_the_picker_and_has_no_video_of_its_own():
    load_drivers()
    assert DRIVERS["gate_sip_server"].hidden is True and DRIVERS["gate_sip_server"].has_own_video is False
    assert all(not DRIVERS[k].hidden for k in ("dahua_sip", "unifi_protect"))


def test_confirmation_is_required():
    assert _err({**OK, "confirm": False}) == ("confirm", "confirm_required")
    assert _err({k: v for k, v in OK.items() if k != "confirm"}) == ("confirm", "confirm_required")
    assert _err({**OK, "confirm": "yes"}) == ("confirm", "confirm_required")


@pytest.mark.parametrize("pw", ["", "1234567", "1234567890123456", None, 12345678])
def test_password_must_be_8_to_15_characters(pw):
    assert _err({**OK, "password": pw}) == ("password", "password_length")


def test_password_bounds_are_accepted():
    assert normalize({**OK, "password": "12345678"})["password"] == "12345678"
    assert normalize({**OK, "password": "123456789012345"})["password"] == "123456789012345"


@pytest.mark.parametrize("host", ["station.example", "", "10.8.0.999", None, "localhost"])
def test_host_must_be_an_ip_literal(host):
    assert _err({**OK, "sip_host": host}) == ("sip_host", "invalid_sip_host")


@pytest.mark.parametrize("host", ["10.6.0.5", "0.0.0.0", "192.168.1.1", "::"])
def test_a_non_loopback_bind_needs_both_flags(host):
    assert _err({**OK, "sip_host": host})[1] == "lan_bind_needs_flags"
    assert _err({**OK, "sip_host": host, "allow_non_loopback": True}) == ("allow_from", "lan_bind_needs_flags")
    assert _err({**OK, "sip_host": host, "allow_from": "10.8.0.39"}) == ("allow_non_loopback", "lan_bind_needs_flags")
    assert normalize({**OK, "sip_host": host, **LAN})["allow_from"] == ["10.8.0.39"]


@pytest.mark.parametrize("bad", ["0.0.0.0/0", "::/0", "not-an-ip", "10.8.0.39,zzz", "10.8.0.0/33"])
def test_allow_from_is_validated_and_never_allows_everyone(bad):
    got = _err({**OK, "sip_host": "10.6.0.5", "allow_non_loopback": True, "allow_from": bad})
    assert got[0] == "allow_from" and got[1] in ("invalid_allow_from", "allow_from_any")


def test_allow_from_forms_are_normalised():
    out = normalize({**OK, "sip_host": "10.6.0.5", "allow_non_loopback": True, "allow_from": " 10.8.0.39 , 10.8.0.0/16 ,, "})
    assert out["allow_from"] == ["10.8.0.39", "10.8.0.0/16"]


@pytest.mark.parametrize("port", [5060, 80, 1023, 8091, 8092, 8095, 40000])
def test_gate_sip_ports_and_low_ports_are_locked_on_loopback(port):
    assert _err({**OK, "sip_port": port}) == ("sip_port", "standard_port_locked")
    assert _err({**OK, "rtp_port": port}) == ("rtp_port", "standard_port_locked")


def test_the_lan_unlock_lifts_the_locked_ports():
    out = normalize({**OK, "sip_host": "10.6.0.5", "sip_port": 5060, "rtp_port": 40000, **LAN})
    assert out["sip_listen"] == "10.6.0.5:5060" and out["rtp_port"] == 40000


@pytest.mark.parametrize("port", [0, 65536, -1, "5", True, 1.5, None])
def test_ports_must_be_integers_in_range(port):
    assert _err({**OK, "sip_port": port}) == ("sip_port", "invalid_port")


def test_sip_and_rtp_ports_must_differ():
    assert _err({**OK, "sip_port": 20000, "rtp_port": 20000}) == ("rtp_port", "ports_equal")


def test_ipv6_loopback_listen_address_is_bracketed():
    assert normalize({**OK, "sip_host": "::1"})["sip_listen"] == "[::1]:15060"


def test_the_payload_is_what_the_go_driver_accepts():
    """The keys the Go parseConfig reads; the rest are Python bookkeeping it ignores."""
    out = normalize({**OK, "sip_host": "10.6.0.5", "sip_port": 5060, **LAN})
    go_keys = {"sip_listen", "password", "user", "realm", "rtp_port", "allow_non_loopback", "allow_from", "device_id"}
    assert go_keys <= out.keys()
    assert isinstance(out["allow_from"], list) and all(isinstance(x, str) for x in out["allow_from"])


# --- the checks that need the other entries --------------------------------------------------------------------------

def _data(**kw):
    return normalize({**OK, **kw})


def test_two_server_entries_cannot_share_a_port():
    first = {"driver": "gate_sip_server", **_data()}
    assert entry_conflicts(_data(), [first])[1] == "port_in_use_by_entry"
    assert entry_conflicts(_data(sip_port=15061), [first])[1] == "port_in_use_by_entry"    # its rtp 45000 is taken
    assert entry_conflicts(_data(sip_port=15061, rtp_port=45001), [first]) is None
    assert entry_conflicts(_data(sip_host="::1", sip_port=15061, rtp_port=45001), [first]) is None  # other host, no overlap


def test_a_wildcard_bind_overlaps_any_other_server_entry():
    first = {"driver": "gate_sip_server", **_data()}
    wild = _data(sip_host="0.0.0.0", sip_port=15070, rtp_port=45000, **LAN)
    assert entry_conflicts(wild, [first]) == ("rtp_port", "port_in_use_by_entry")


# --- the flow --------------------------------------------------------------------------------------------------------

@pytest.fixture(autouse=True)
async def _integration(hass, enable_custom_integrations, tmp_path, monkeypatch):
    from homeassistant.components.frontend import DATA_EXTRA_MODULE_URL
    from homeassistant.setup import async_setup_component

    monkeypatch.setattr(hass.config, "path", lambda *parts: str(tmp_path.joinpath(*parts)))
    assert await async_setup_component(hass, "http", {})
    hass.data[DATA_EXTRA_MODULE_URL] = set()
    yield


async def _menu(hass):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.MENU
    return result


async def test_the_menu_has_an_explicit_item_and_the_picker_does_not_offer_the_driver(hass):
    result = await _menu(hass)
    assert "sip_server_door" in result["menu_options"]
    picker = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "door"})
    choices = picker["data_schema"].schema["driver"].container
    assert "dahua_sip" in choices and "gate_sip_server" not in choices and "gate_sip_observer" not in choices
    with pytest.raises(Exception):  # a hand-crafted post of the hidden key is rejected by the schema
        await hass.config_entries.flow.async_configure(picker["flow_id"], {"driver": "gate_sip_server"})


async def _to_form(hass):
    result = await _menu(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "sip_server_door"})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "driver_config"
    return result


async def test_the_form_refuses_without_confirmation_and_creates_nothing(hass):
    result = await _to_form(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {**OK, "confirm": False})
    assert result["type"] is FlowResultType.FORM and result["errors"] == {"confirm": "confirm_required"}
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_a_confirmed_loopback_form_goes_on_to_the_camera_step_and_creates_the_entry(hass):
    result = await _to_form(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], dict(OK))
    assert result["step_id"] == "camera_config"
    # no video of its own: a camera entity is required
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"existing_camera_entity": "camera.gate"})
    assert result["step_id"] == "relay_action"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    assert entry.unique_id == "gate_sip_server:gate-sip-server:127.0.0.1:15060"
    assert entry.data[ENTRY_TYPE] == ENTRY_TYPE_DOOR and entry.data["driver"] == "gate_sip_server"
    assert entry.data["sip_listen"] == "127.0.0.1:15060" and entry.data["allow_non_loopback"] is False
    assert entry.data["device_id"] == "gate-sip-server:127.0.0.1:15060"
    assert "confirm" not in entry.data


async def test_a_lan_form_without_flags_is_refused_in_the_flow(hass):
    result = await _to_form(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {**OK, "sip_host": "10.6.0.5"})
    assert result["type"] is FlowResultType.FORM and result["errors"]["allow_non_loopback"] == "lan_bind_needs_flags"


async def test_the_same_listen_address_twice_aborts(hass):
    MockConfigEntry(domain=DOMAIN, unique_id="gate_sip_server:gate-sip-server:127.0.0.1:15060",
                    data={ENTRY_TYPE: ENTRY_TYPE_DOOR, "driver": "gate_sip_server", **_data()}).add_to_hass(hass)
    result = await _to_form(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], dict(OK))
    assert result["type"] is FlowResultType.FORM and result["errors"] == {"sip_port": "port_in_use_by_entry"}


def test_the_password_is_redacted_in_diagnostics():
    from custom_components.portico.diagnostics import _is_secret_key
    assert _is_secret_key("password")
