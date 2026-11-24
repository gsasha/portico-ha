# ruff: noqa: F811 - test parameters named after the imported fixtures
"""The one-time rename "away ring" -> "push ring notification" (2026-11-12) of an existing install.

The push entry of `fixtures/core.config_entries.json` is titled "Portico away ring", as the box's real entry is. The
sensor's unique id (`<push entry id>_away_ring`) is the same before and after, so history and statistics follow.
"""
from __future__ import annotations

import logging

from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from custom_components.portico.config_flow import DOMAIN

from .test_push_registry import _custom_integrations, google  # noqa: F401 - fixtures by name
from .test_unified_load import PUSH_ID, _credentials, _fixture_entries

OLD = "sensor.portico_away_ring"
NEW = "sensor.portico_push_ring_notification"
UNIQUE = f"{PUSH_ID}_away_ring"


def _seed(hass, entries, entity_id=OLD):
    """The registry row an old install has: the old default id, the unchanged unique id."""
    object_id = entity_id.split(".")[1]
    return er.async_get(hass).async_get_or_create(
        "sensor", DOMAIN, UNIQUE, suggested_object_id=object_id, config_entry=entries[PUSH_ID])


async def _boot(hass, hass_storage, google):
    _credentials(hass_storage, google)
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()


async def test_old_default_id_and_title_are_renamed_keeping_the_unique_id(hass, hass_storage, google, caplog):
    entries = _fixture_entries(hass)
    assert _seed(hass, entries).entity_id == OLD
    caplog.set_level(logging.INFO)
    await _boot(hass, hass_storage, google)
    registry = er.async_get(hass)
    assert registry.async_get(OLD) is None
    assert registry.async_get_entity_id("sensor", DOMAIN, UNIQUE) == NEW
    assert hass.states.get(NEW).state == "ok" and hass.states.get(OLD) is None
    assert entries[PUSH_ID].title == "Portico push ring notification"
    assert f"renamed {OLD} to {NEW}" in caplog.text


async def test_migration_is_idempotent(hass, hass_storage, google):
    from custom_components.portico.push_ring_rename import async_migrate_push_ring_names
    entries = _fixture_entries(hass)
    _seed(hass, entries)
    await _boot(hass, hass_storage, google)
    async_migrate_push_ring_names(hass, entries[PUSH_ID])         # a second run changes nothing
    assert er.async_get(hass).async_get_entity_id("sensor", DOMAIN, UNIQUE) == NEW
    assert entries[PUSH_ID].title == "Portico push ring notification"


async def test_a_user_renamed_id_and_title_are_left_alone(hass, hass_storage, google):
    entries = _fixture_entries(hass)
    hass.config_entries.async_update_entry(entries[PUSH_ID], title="Front door phones")
    _seed(hass, entries, "sensor.my_own_ring_sensor")
    await _boot(hass, hass_storage, google)
    assert er.async_get(hass).async_get_entity_id("sensor", DOMAIN, UNIQUE) == "sensor.my_own_ring_sensor"
    assert entries[PUSH_ID].title == "Front door phones"


async def test_a_taken_new_id_keeps_the_old_one(hass, hass_storage, google, caplog):
    entries = _fixture_entries(hass)
    _seed(hass, entries)
    er.async_get(hass).async_get_or_create(
        "sensor", "other", "someone-elses", suggested_object_id="portico_push_ring_notification")
    await _boot(hass, hass_storage, google)
    assert er.async_get(hass).async_get_entity_id("sensor", DOMAIN, UNIQUE) == OLD
    assert "is taken" in caplog.text


async def test_a_fresh_install_gets_the_new_id(hass, hass_storage, google):
    _fixture_entries(hass)                                   # no registry row yet
    await _boot(hass, hass_storage, google)
    assert er.async_get(hass).async_get_entity_id("sensor", DOMAIN, UNIQUE) == NEW
