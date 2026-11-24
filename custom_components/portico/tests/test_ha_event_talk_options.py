# ruff: noqa: F811 - test parameters named after the imported fixtures
"""The talk-back fields in an ha_event door's Configure (options) form: shown for ha_event only, validated by the driver's
own rules, a blank key keeps the stored one, the key never reaches diagnostics, and the saved options reach the running door
(talkback_enabled, the configure payload) and reload it. Offline; the transport is faked, nothing contacts a console.
"""
from __future__ import annotations

import pytest
from homeassistant.data_entry_flow import FlowResultType

from custom_components.portico.diagnostics import async_get_config_entry_diagnostics
from custom_components.portico.door.drivers import ha_event
from custom_components.portico.door import hub as portico_hub

from .test_ha_event_door import TALK_ESTATE, _front_entry, _setup
from .test_ha_event_talk import KEY, TalkFake, talk_transport  # noqa: F401 - fixtures by name
from .test_observer import _custom_integrations, _dahua_entry, fake_transport  # noqa: F401

NEW = "NEW-PROTECT-KEY-5678"


def _names(form):
    return {str(k) for k in form["data_schema"].schema}


async def _form(hass, entry):
    return await hass.config_entries.options.async_init(entry.entry_id)


async def _save(hass, entry, **fields):
    if fields.get("talkback") is True and not ha_event.merged_talk_data(entry.data, entry.options).get("protect_host"):
        fields = {**TALK_ESTATE, **fields}          # no estate default: a first talkback save states the three options
    form = await _form(hass, entry)
    return await hass.config_entries.options.async_configure(form["flow_id"], dict(fields))


# --- the form ------------------------------------------------------------------------------------------------------------------


async def test_an_ha_event_door_shows_the_talk_fields_and_the_key_is_a_password(hass, fake_transport):
    entry = _front_entry(hass, talkback=True, protect_api_key=KEY)
    form = await _form(hass, entry)
    assert form["step_id"] == "door_talk_init"
    assert _names(form) >= {"talkback", "protect_api_key", "clear_protect_api_key", "protect_host", "camera_id",
                            "downlink_url", "guard_url", "verify_ssl", "gain"}
    (key,) = [k for k in form["data_schema"].schema if str(k) == "protect_api_key"]
    assert form["data_schema"].schema[key].config["type"] == "password"
    assert KEY not in repr(form["data_schema"].schema) and KEY not in str(form["description_placeholders"])


async def test_the_form_never_defaults_the_key_field(hass, fake_transport):
    entry = _front_entry(hass, talkback=True, protect_api_key=KEY)
    form = await _form(hass, entry)
    (key,) = [k for k in form["data_schema"].schema if str(k) == "protect_api_key"]
    import voluptuous as vol
    assert key.default is vol.UNDEFINED


async def test_another_driver_has_a_form_without_fields(hass, fake_transport):
    entry = _dahua_entry(hass)
    form = await _form(hass, entry)
    assert form["step_id"] == "door_init" and _names(form) == set()


# --- validation and storage ----------------------------------------------------------------------------------------------------


async def test_talkback_on_without_a_key_is_refused_with_the_driver_error(hass, fake_transport):
    entry = _front_entry(hass)
    r = await _save(hass, entry, talkback=True)
    assert r["type"] is FlowResultType.FORM and r["errors"] == {"protect_api_key": "protect_api_key_required"}
    assert "talkback" not in entry.options
    r = await _save(hass, entry, talkback=True, protect_api_key="   ")
    assert r["errors"] == {"protect_api_key": "protect_api_key_required"}


async def test_a_typed_key_is_stored_in_the_options_and_the_away_audio_is_kept(hass, fake_transport):
    entry = _front_entry(hass)
    r = await _save(hass, entry, talkback=True, protect_api_key=f"  {NEW} ")
    assert r["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["talkback"] is True and entry.options["protect_api_key"] == NEW
    assert "away_media" not in entry.options and entry.options["gain"] == 1
    assert "protect_api_key" not in entry.data


async def test_a_blank_key_keeps_the_stored_one_in_options_and_in_data(hass, fake_transport):
    entry = _front_entry(hass)
    await _save(hass, entry, talkback=True, protect_api_key=NEW)
    await _save(hass, entry, talkback=True, gain=3)
    assert entry.options["protect_api_key"] == NEW and entry.options["gain"] == 3
    old = _front_entry(hass, talkback=True, protect_api_key=KEY)       # a key from the add-a-door form lives in data
    await _save(hass, old, talkback=True)
    assert ha_event.merged_talk_data(old.data, old.options)["protect_api_key"] == KEY
    assert ha_event.talkback_enabled(ha_event.merged_talk_data(old.data, old.options))


async def test_talkback_off_keeps_the_key_and_only_the_remove_box_clears_it(hass, fake_transport):
    entry = _front_entry(hass, talkback=True, protect_api_key=KEY)
    r = await _save(hass, entry, talkback=False)
    assert r["type"] is FlowResultType.CREATE_ENTRY and entry.options["talkback"] is False
    merged = ha_event.merged_talk_data(entry.data, entry.options)
    assert merged["protect_api_key"] == KEY and not ha_event.talkback_enabled(merged)
    await _save(hass, entry, talkback=False, clear_protect_api_key=True)
    merged = ha_event.merged_talk_data(entry.data, entry.options)
    assert entry.options["protect_api_key"] == "" and merged["protect_api_key"] == ""      # hides the key left in data


async def test_removing_the_key_while_talkback_is_on_is_refused(hass, fake_transport):
    entry = _front_entry(hass, talkback=True, protect_api_key=KEY)
    r = await _save(hass, entry, talkback=True, clear_protect_api_key=True)
    assert r["errors"] == {"protect_api_key": "protect_api_key_required"}


async def test_an_error_never_echoes_the_key(hass, fake_transport):
    entry = _front_entry(hass)
    r = await _save(hass, entry, talkback=True, protect_api_key=" ")
    assert NEW not in repr(r) and KEY not in repr(r)


# --- diagnostics ----------------------------------------------------------------------------------------------------------------


async def test_the_key_is_redacted_in_diagnostics_from_data_and_from_options(hass, fake_transport):
    entry = _front_entry(hass, talkback=True, protect_api_key=KEY)
    await _save(hass, entry, talkback=True, protect_api_key=NEW)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["entry"]["data"]["protect_api_key"] == "**REDACTED**"
    assert diag["entry"]["options"]["protect_api_key"] == "**REDACTED**"
    assert KEY not in repr(diag) and NEW not in repr(diag)


# --- the running door -----------------------------------------------------------------------------------------------------------


async def test_options_reach_the_running_door_and_saving_reloads_it(hass, talk_transport):
    entry = _front_entry(hass)
    await _setup(hass, entry)
    first = entry.runtime_data
    assert first.talk_wanted is False and first.talk_attached is False
    r = await _save(hass, entry, talkback=True, protect_api_key=NEW, gain=2, protect_host="10.9.9.9")
    assert r["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    door = entry.runtime_data
    assert door is not first                                         # the entry reloaded
    assert door.talk_wanted is True and door.talk_attached is True
    go = talk_transport.instances[-1]
    (driver, config), = go.configured
    assert driver == "unifi_protect_talk"
    assert config["protect_api_key"] == NEW and config["gain"] == 2 and config["protect_host"] == "10.9.9.9"


async def test_a_legacy_away_media_option_is_dropped_when_the_form_is_saved(hass, talk_transport):
    entry = _front_entry(hass)
    hass.config_entries.async_update_entry(entry, options={**entry.options, "away_media": False})
    await _setup(hass, entry)
    form = await _form(hass, entry)
    r = await hass.config_entries.options.async_configure(form["flow_id"], {})
    assert r["type"] is FlowResultType.CREATE_ENTRY and "away_media" not in entry.options


async def test_turning_talkback_off_reloads_ring_only(hass, talk_transport):
    entry = _front_entry(hass, talkback=True, protect_api_key=KEY)
    await _setup(hass, entry)
    assert entry.runtime_data.talk_attached is True
    await _save(hass, entry, talkback=False)
    await hass.async_block_till_done()
    assert entry.runtime_data.talk_wanted is False and entry.runtime_data.talk_attached is False


# --- the guard url is optional (DEV-835: bell-talk is retired) ----------------------------------------------------------------


def test_a_new_install_has_no_guard_url_and_no_estate_ip():
    assert ha_event.TALK_DEFAULTS["guard_url"] == ""
    assert "10.6.0.7" not in repr(ha_event.TALK_DEFAULTS)
    new = ha_event.normalize({"event_entity": "event.bell", "talkback": True, "protect_api_key": KEY, **TALK_ESTATE})
    assert ha_event.talk_transport_config(new, "ha_event:x")["guard_url"] == ""


def test_no_estate_value_is_a_default_and_talkback_waits_for_all_of_them():
    """DEV-836: host, camera id and audio URL are empty by default; an entry missing any of them stays ring-only."""
    for key in ("protect_host", "camera_id", "downlink_url", "guard_url"):
        assert ha_event.TALK_DEFAULTS[key] == ""
    full = {"talkback": True, "protect_api_key": KEY, **TALK_ESTATE}
    assert ha_event.talkback_enabled(full) is True
    for key in TALK_ESTATE:
        assert ha_event.talkback_enabled({**full, key: ""}) is False and ha_event.talkback_enabled({k: v for k, v in full.items() if k != key}) is False
    assert ha_event.talk_transport_config({"protect_api_key": KEY}, "ha_event:x")["guard_url"] == ""      # a missing guard_url is no guard
    assert ha_event.talk_transport_config({"protect_api_key": KEY, "guard_url": "http://192.168.2.9/health"}, "ha_event:x")["guard_url"] == "http://192.168.2.9/health"


def test_a_new_talkback_entry_states_no_guard():
    out = ha_event.normalize({"event_entity": "event.bell", "talkback": True, "protect_api_key": KEY, **TALK_ESTATE})
    assert out["guard_url"] == ""
    assert "guard_url" not in ha_event.normalize({"event_entity": "event.bell"})


def test_a_set_guard_url_still_reaches_the_transport():
    cfg = ha_event.talk_transport_config({"protect_api_key": KEY, "guard_url": "http://old:8093/health"}, "ha_event:x")
    assert cfg["guard_url"] == "http://old:8093/health"


async def test_the_guard_url_can_be_cleared_in_the_form(hass, fake_transport):
    entry = _front_entry(hass, talkback=True, protect_api_key=KEY, guard_url="http://192.168.2.7:8093/health")
    form = await _form(hass, entry)
    (key,) = [k for k in form["data_schema"].schema if str(k) == "guard_url"]
    assert key.description == {"suggested_value": "http://192.168.2.7:8093/health"}       # shown, never forced back as a default
    r = await _save(hass, entry, talkback=True)                                           # the field omitted = cleared
    assert r["type"] is FlowResultType.CREATE_ENTRY and entry.options["guard_url"] == ""
    merged = ha_event.merged_talk_data(entry.data, entry.options)
    assert merged["guard_url"] == ""                                                        # hides the URL left in data
    assert ha_event.talk_transport_config(merged, "ha_event:x")["guard_url"] == ""
    r = await _save(hass, entry, talkback=True, guard_url=" http://other:8093/health ")
    assert entry.options["guard_url"] == "http://other:8093/health"
