"""HA imports platforms as portico.binary_sensor. A door entry's entities live in door/entities/; the settings (push ring
notification) entry hosts the house-wide `binary_sensor.portico_call_active` (DEV-811 P4)."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .door.const import is_door_entry
from .door.entities.binary_sensor import async_setup_entry as async_setup_door_binary_sensors
from .entities.call_sensors import PorticoCallActiveSensor
from .push.const import ENTRY_TYPE, ENTRY_TYPE_PUSH


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    if is_door_entry(entry):
        await async_setup_door_binary_sensors(hass, entry, async_add_entities)
    elif entry.data.get(ENTRY_TYPE) == ENTRY_TYPE_PUSH:
        async_add_entities([PorticoCallActiveSensor(entry)])


__all__ = ["async_setup_entry"]
