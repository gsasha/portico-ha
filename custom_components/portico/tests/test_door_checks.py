# ruff: noqa: F811 - test parameters named after the imported fixtures
"""The checks that lived in the driver part's `test_driver_part_alone.py`, kept for the unified integration.

That file proved the driver part ran with the UI part absent, which is no longer a state that exists; what it also
checked about the door side (the peer-token Store going with the entry, the socket directory, the door option, the
diagnostics redaction, the picker) is here, adapted to the one domain.
"""
from __future__ import annotations

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.portico.diagnostics import async_get_config_entry_diagnostics
from custom_components.portico.door.const import DOMAIN, DRIVER_ID, ENTRY_TYPE, ENTRY_TYPE_DOOR

from .test_observer import (  # noqa: F401 - fixtures by name
    _custom_integrations,
    _observer_entry,
    fake_transport,
)


async def test_the_socket_lives_under_the_historical_storage_directory(hass, fake_transport, tmp_path):
    """`.storage/portico_intercom/hub.sock` keeps its folder name (a stale second hub would remove the path)."""
    from custom_components.portico.door.hub import SOCKET_DIR

    entry = _observer_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert SOCKET_DIR == ".storage/portico_intercom"
    hub_socket = str(tmp_path / ".storage" / "portico_intercom" / "hub.sock")
    assert hass.data[DRIVER_ID]["hub"].socket_path == hub_socket
    assert fake_transport.instances[-1].socket_path == hub_socket
    assert (tmp_path / ".storage" / "portico_intercom").is_dir() and not (tmp_path / ".storage" / "portico").exists()


async def test_a_door_has_no_away_audio_option_and_a_stored_one_is_ignored(hass, fake_transport):
    entry = _observer_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert list(result["data_schema"].schema) == []
    hass.config_entries.async_update_entry(entry, options={"away_media": False})       # a legacy entry
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.data[DRIVER_ID]["provider"].doors()[0]["away_media"] is True


async def test_every_door_secret_is_redacted_in_the_diagnostics(hass, fake_transport):
    """R1-20: the door entry's diagnostics hide every credential it can hold, and the peer token in particular."""
    secrets = {"sip_password": "S1", "rtsp_password": "S2", "nvr_api_key": "S3", "peer_token": "S4",
               "tailscale_api_key": "S5", "api_key": "S6", "token": "S7", "client_secret": "S8"}
    entry = MockConfigEntry(
        domain=DOMAIN, title="Door", unique_id="dahua_sip:station.example",
        data={ENTRY_TYPE: ENTRY_TYPE_DOOR, "driver": "dahua_sip", "host": "station.example", "sip_user": "9901#0",
              **secrets, "existing_camera_entity": "camera.gate", "open_relay_action": []})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    diag = await async_get_config_entry_diagnostics(hass, entry)
    for key in secrets:
        assert diag["entry"]["data"][key] == "**REDACTED**", key
    # DEV-700 A: the station's address is topology, pseudonymised (the same word wherever it recurs), never plain
    assert diag["entry"]["data"]["host"].startswith("redacted-") and "station.example" not in repr(diag)
    assert not any(value in repr(diag) for value in secrets.values())


async def test_the_driver_picker_lists_the_drivers_after_the_menu(hass):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "door"})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "door"
    assert list(result["data_schema"].schema["driver"].container) == ["dahua_sip", "ha_event", "unifi_protect"]


def test_answer_arbitration_lives_once_in_the_provider():
    """PH-3b reviewer check: the state comparison is in `answer`."""
    import inspect

    from custom_components.portico.door.provider_impl import TransportProvider

    impl = inspect.getsource(TransportProvider.answer)
    assert impl.count('"ringing"') == 1
    assert not hasattr(TransportProvider, "entry_id_for")           # the door dict's `ref` replaced it (S-2)
