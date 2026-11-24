"""House-wide call entities on the settings entry (DEV-811 P4; design: automation/portico_call_audio_design.md section P4).

`sensor.portico_calls` is `idle | ringing | talking` over every kind of call (door, house, outbound); `binary_sensor.portico_call_active`
is on while it is not `idle`. Both read the live set of `call_events.CallEvents`; neither polls. One per house, owned by the settings
entry: with no such entry there are no entities (the `portico_call` events are fired regardless).
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback

from ..call_events import IDLE, STATES, get_call_events


class _CallEntity:
    _attr_should_poll = False
    _attr_has_entity_name = False

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry

    @property
    def _events(self):
        return get_call_events(self.hass)

    @property
    def available(self) -> bool:
        return self._events is not None

    async def async_added_to_hass(self) -> None:
        events = self._events
        if events is not None:
            self.async_on_remove(events.add_listener(self._handle_change))

    @callback
    def _handle_change(self) -> None:
        self.async_write_ha_state()


class PorticoCallsSensor(_CallEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = STATES
    _attr_name = "Portico calls"
    _attr_icon = "mdi:phone-classic"
    _unrecorded_attributes = frozenset({"calls", "last_call"})

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry)
        self._attr_unique_id = f"{entry.entry_id}_calls"

    @property
    def native_value(self) -> str:
        events = self._events
        return events.state if events is not None else IDLE

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        events = self._events
        if events is None:
            return {}
        attrs: dict[str, Any] = {"calls": events.live_calls()}
        if events.last_call is not None:
            attrs["last_call"] = dict(events.last_call)
        return attrs


class PorticoCallActiveSensor(_CallEntity, BinarySensorEntity):
    """On while any call is ringing or talking (`sensor.portico_calls` is not `idle`)."""

    _attr_name = "Portico call active"
    _attr_icon = "mdi:phone-in-talk"

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry)
        self._attr_unique_id = f"{entry.entry_id}_call_active"

    @property
    def is_on(self) -> bool:
        events = self._events
        return events is not None and events.state != IDLE
