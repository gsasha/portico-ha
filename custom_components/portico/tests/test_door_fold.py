# ruff: noqa: F811 - test parameters named after the imported fixtures
"""What the fold of `portico_intercom` into `portico` (DEV-686) must keep true, as tests of their own.

The door tests moved here keep their assertions; these are the ones the fold adds: the two meanings of the old `DOMAIN`
(design 1.4), the kind dispatch of the options flow and diagnostics, and the wheel that is no longer in the manifest
(`test_wheel.py`). Entry-kind dispatch of setup, the sensor platform and the scans of the domain's entries is
`test_unified_load.py`.
"""
from __future__ import annotations

import json

import pytest
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.portico.diagnostics import async_get_config_entry_diagnostics
from custom_components.portico.door.const import DOMAIN, DRIVER_ID, ENTRY_TYPE, ENTRY_TYPE_DOOR, is_door_entry
from custom_components.portico.provider import Door

from .test_observer import (  # noqa: F401 - fixtures by name
    DAHUA_HOST,
    GATE_SIP_URL,
    _custom_integrations,
    _dahua_entry,
    _observer_entry,
    fake_transport,
)


def test_the_two_names_the_old_domain_stood_for_are_different_constants():
    """The registry platform and every repair issue's domain are `portico`; the provider id and the door side's
    hass.data namespace stay `portico_intercom` (persisted in endpoint ids, the content store and the push door_ref)."""
    assert DOMAIN == "portico" and DRIVER_ID == "portico_intercom"


async def test_the_provider_id_is_the_driver_id_and_the_entity_platform_is_the_domain(hass, fake_transport):
    """A Portico-owned camera is looked up in the entity registry by PLATFORM `portico`, while the Door it belongs to
    carries provider `portico_intercom` (so its endpoint id, content rules and push door_ref are unchanged)."""
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=f"dahua_sip:{DAHUA_HOST}",
        data={ENTRY_TYPE: ENTRY_TYPE_DOOR, "driver": "dahua_sip", "host": DAHUA_HOST, "sip_user": "9901#0",
              "sip_password": "x", "rtsp_username": "u", "rtsp_password": "p", "open_relay_action": []})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    camera = er.async_get(hass).async_get_entity_id("camera", DOMAIN, f"{entry.entry_id}_camera")
    assert camera is not None
    assert er.async_get(hass).async_get_entity_id("camera", DRIVER_ID, f"{entry.entry_id}_camera") is None
    (door_dict,) = hass.data[DRIVER_ID]["provider"].doors()
    assert door_dict["provider"] == DRIVER_ID and door_dict["camera_entity_id"] == camera
    door = Door.from_dict(DRIVER_ID, door_dict)
    assert door.endpoint_id == f"portico_intercom:{DAHUA_HOST}"                       # the persisted id grammar
    assert door.door_ref == "d-" + __import__("hashlib").sha256(door.endpoint_id.encode()).hexdigest()[:32]


async def test_the_door_entities_keep_their_unique_ids_and_ids(hass, fake_transport):
    """The migration keeps the entry id; the entities must therefore keep `<entry_id>_call_state` and the
    id `sensor.call_state`, now under platform `portico`."""
    entry = _observer_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    rows = {e.entity_id: e for e in er.async_entries_for_config_entry(registry, entry.entry_id)}
    assert sorted(rows) == ["sensor.call_state"]
    assert rows["sensor.call_state"].unique_id == f"{entry.entry_id}_call_state"
    assert {row.platform for row in rows.values()} == {DOMAIN}


async def test_a_driver_error_repair_issue_lives_under_the_integration_domain(hass, fake_transport):
    """Issues are shown with the translations of their domain: `portico` has the door texts, `portico_intercom` has none."""
    entry = _observer_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    fake_transport.instances[-1].driver_error("boom")
    await hass.async_block_till_done()
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"driver_error_{entry.entry_id}")
    assert issue is not None and issue.translation_key == "driver_error"
    assert ir.async_get(hass).async_get_issue(DRIVER_ID, f"driver_error_{entry.entry_id}") is None


# ---- the options flow is chosen by the entry kind ---------------------------------------------------------------


async def test_a_doors_configure_is_away_call_audio_and_has_its_own_step_id(hass, fake_transport):
    entry = _observer_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert (result["type"], result["step_id"]) == (FlowResultType.FORM, "door_init")
    assert list(result["data_schema"].schema) == []                       # Away call audio is always on: no option
    saved = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert saved["type"] is FlowResultType.CREATE_ENTRY and saved["data"] == {}


async def test_each_kind_gets_its_own_options_flow(hass, fake_transport):
    """push -> the menu `init`; door -> `door_init`; phone -> its flow; an entry of no kind -> `door_entry_moved`."""
    from .house_env import phone_entry

    push = MockConfigEntry(domain=DOMAIN, unique_id="push", data={ENTRY_TYPE: "push"})
    push.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(push.entry_id)
    assert result["type"] is FlowResultType.MENU and result["step_id"] == "init"
    hass.config_entries.options.async_abort(result["flow_id"])

    door = _observer_entry(hass)
    result = await hass.config_entries.options.async_init(door.entry_id)
    assert result["step_id"] == "door_init" and result["type"] is FlowResultType.FORM
    hass.config_entries.options.async_abort(result["flow_id"])

    phone = phone_entry(hass, "user-1", "enr-1", "A phone")
    result = await hass.config_entries.options.async_init(phone.entry_id)
    assert result["step_id"] == "phone_doors"
    hass.config_entries.options.async_abort(result["flow_id"])

    legacy = MockConfigEntry(domain=DOMAIN, unique_id="old", data={"driver": "gate_sip_observer"})
    legacy.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(legacy.entry_id)
    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "door_entry_moved")


def test_is_door_entry_reads_the_kind_not_the_driver():
    assert is_door_entry(MockConfigEntry(domain=DOMAIN, data={ENTRY_TYPE: "door"}))
    assert not is_door_entry(MockConfigEntry(domain=DOMAIN, data={"driver": "gate_sip_observer"}))
    assert not is_door_entry(MockConfigEntry(domain=DOMAIN, data={ENTRY_TYPE: "push", "driver": "x"}))


# ---- diagnostics: one dispatcher, one redaction set ------------------------------------------------------------


async def test_a_doors_diagnostics_have_the_runtime_and_hub_and_redact_every_door_credential(hass, fake_transport):
    entry = _dahua_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert set(diag) == {"entry", "runtime", "hub"}
    assert diag["runtime"]["call_state"] == {"state": "idle", "call_id": None}
    assert diag["hub"]["doors_attached"] == 1
    assert diag["entry"]["data"]["sip_password"] == "**REDACTED**"
    assert diag["entry"]["data"]["rtsp_password"] == "**REDACTED**"
    assert "x" != diag["entry"]["data"]["sip_password"] and "p" != diag["entry"]["data"]["rtsp_password"]
    assert diag["entry"]["data"][ENTRY_TYPE] == ENTRY_TYPE_DOOR


def test_the_merged_redaction_set_is_a_superset_of_the_former_door_set():
    """The door's own TO_REDACT (deleted with its diagnostics module) is contained, name for name."""
    from custom_components.portico.diagnostics import TO_REDACT

    former_door_set = {
        "sip_password", "rtsp_password", "nvr_api_key", "tailscale_api_key",
        "private_key", "registration", "api_key", "ha_token", "access_token", "token", "fid",
        "client_secret", "tailscale_client_secret", "api_access_token", "peer_token",
    }
    assert former_door_set <= TO_REDACT
