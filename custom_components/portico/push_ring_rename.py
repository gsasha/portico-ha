"""One-time rename of what the push ring notification (formerly "away ring") left in Home Assistant under its old name.

The owner, 2026-11-12: "away ring" is unclear, the name is "push ring notification" (decisions D-20261112-a03d, D-20261112-f53d).
New installs get the new names from the entity's default name (`PorticoAwayRingSensor._attr_name`) and the config flow
title. An install that already has the old ones keeps them, so at the push entry's setup (before its platforms load):

  * the diagnostic sensor `sensor.portico_away_ring` becomes `sensor.portico_push_ring_notification`, through the entity
    registry, and ONLY when its current entity_id is still exactly the old default (a user-renamed id is never touched)
    and the new id is free. The unique id (`<entry_id>_away_ring`) is deliberately kept, so history and statistics follow;
  * the entry title "Portico away ring" (the old default) becomes "Portico push ring notification"; a title the user
    set is never touched.

Idempotent: after the first run nothing matches, so a second run changes nothing. Never raises.
"""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

DOMAIN = "portico"

_LOGGER = logging.getLogger(__name__)

OLD_SENSOR_ENTITY_ID = "sensor.portico_away_ring"
NEW_SENSOR_ENTITY_ID = "sensor.portico_push_ring_notification"
SENSOR_UNIQUE_ID_SUFFIX = "_away_ring"             # kept: the registry key history and statistics hang on
OLD_ENTRY_TITLE = "Portico away ring"
NEW_ENTRY_TITLE = "Portico push ring notification"


def async_migrate_push_ring_names(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Rename the push entry's sensor id and title if they are still the old defaults. Never raises."""
    try:
        registry = er.async_get(hass)
        found = registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}{SENSOR_UNIQUE_ID_SUFFIX}")
        if found == OLD_SENSOR_ENTITY_ID:
            if registry.async_get(NEW_SENSOR_ENTITY_ID) is not None or hass.states.get(NEW_SENSOR_ENTITY_ID) is not None:
                _LOGGER.warning("Portico: %s is taken, so %s keeps its old id", NEW_SENSOR_ENTITY_ID, OLD_SENSOR_ENTITY_ID)
            else:
                registry.async_update_entity(OLD_SENSOR_ENTITY_ID, new_entity_id=NEW_SENSOR_ENTITY_ID)
                _LOGGER.info("Portico: renamed %s to %s (the away ring is now the push ring notification)",
                             OLD_SENSOR_ENTITY_ID, NEW_SENSOR_ENTITY_ID)
        if entry.title == OLD_ENTRY_TITLE:
            hass.config_entries.async_update_entry(entry, title=NEW_ENTRY_TITLE)
            _LOGGER.info('Portico: renamed the entry "%s" to "%s"', OLD_ENTRY_TITLE, NEW_ENTRY_TITLE)
    except Exception:  # noqa: BLE001 - a rename must never fail the setup
        _LOGGER.exception("Portico: the push ring notification rename failed; the old names stay")
