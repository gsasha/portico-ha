# ruff: noqa: F811 - test parameters named after the imported fixtures
"""E7 (blocks series, DEV-658): the Configure content flow is gone. The Portico panel is the only editor of what a
phone shows, because the flow's `ContentStore.async_set` writes a FLAT profile over the home page and would collapse
a multi-block page. What stays: the push ring notification (push) entry's Configure, with its settings, phones, test phones, key,
registration, enrollment and Tailscale items, and the door entry's one "Away call audio" option."""
from __future__ import annotations

import importlib
import inspect

import pytest
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.portico import enrollment_flow, push_flow
from custom_components.portico.config_flow import DOMAIN
from custom_components.portico.push import const

from .test_enrollment_flow import _push_entry
from .test_push_registry import (  # noqa: F401 - fixtures by name
    _custom_integrations,
    _setup_all,
    fake_transport,
    google,
)

pytestmark = pytest.mark.asyncio

PUSH_MENU = ["push_settings", "push_phones", "push_test_phones", "push_key", "add_phone_registration",
             "enroll_phone", "tailscale_change_mode"]
CONTENT_STEPS = ("content_phone", "content_menu", "content_cameras", "content_action_add", "content_action_target",
                 "content_call_add", "content_call_none", "content_action_remove", "content_done", "home_name")


def test_the_content_flow_module_and_its_steps_are_gone():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("custom_components.portico.content_flow")
    for flow in (enrollment_flow.PorticoOptionsFlow, push_flow.PushOptionsFlow):
        for step in CONTENT_STEPS:
            assert not hasattr(flow, f"async_step_{step}"), (flow.__name__, step)
        assert not any(base.__name__ == "ContentFlowMixin" for base in inspect.getmro(flow))


async def test_the_push_entry_configure_menu_has_no_content_items(hass):
    push = _push_entry(hass, tailscale_mode="manual")
    result = await hass.config_entries.options.async_init(push.entry_id)
    assert (result["type"], result["step_id"]) == (FlowResultType.MENU, "init")
    assert result["menu_options"] == PUSH_MENU
    for step in ("content_phone", "home_name"):
        with pytest.raises(InvalidData):    # a menu refuses a step it does not list
            await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": step})


async def test_an_old_door_entry_of_the_ui_part_has_no_content_flow(hass):
    old_door = MockConfigEntry(domain=DOMAIN, title="Old door", unique_id="gate_sip_observer:old",
                               data={"driver": "gate_sip_observer", "host": "old"})
    old_door.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(old_door.entry_id)
    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "door_entry_moved")


async def test_the_push_entry_configure_still_saves_settings_and_the_test_phones(hass, hass_storage, google, fake_transport):
    _door, push = await _setup_all(hass, hass_storage, google)
    result = await hass.config_entries.options.async_init(push.entry_id)
    assert result["menu_options"] == PUSH_MENU
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "push_settings"})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "push_settings"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {const.CONF_ENABLED: False, const.CONF_RING_WINDOW: 30, const.CONF_NEW_PHONES_ENABLED: True})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert push.options[const.CONF_RING_WINDOW] == 30 and push.options[const.CONF_ENABLED] is False

    result = await hass.config_entries.options.async_init(push.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "push_test_phones"})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "push_test_phones"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {const.CONF_TEST_PHONES: []})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert push.options[const.CONF_TEST_PHONES] == [] and push.options[const.CONF_RING_WINDOW] == 30
