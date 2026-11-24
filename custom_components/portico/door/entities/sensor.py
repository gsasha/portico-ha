"""One sensor entity per device: the call-state enum (§4.5, §9.2).

Values: idle|ringing|answered|active|ended. `idle` is HA-side only (no
call_id tracked); the wire states from the transport binary are exactly
ringing|answered|active|ended (§4.5). `Ended -> idle` is immediate, no
dwell timer, so `ended` is a declared option the state never rests on.

Push-driven, never polled (§9.2): the transport's `ringing` and
`call_state_changed` notifications go through __init__.py's
_handle_notification into CallStateTracker.apply_event, whose listener
writes this entity's state. Attribute `call_id`: the tracked call, or None.

Availability: DoorRuntime.entities_available: the transport
subprocess is up and configured AND, for a driver that reports its device's
availability (Capabilities.ReportsAvailability; dahua_sip, unifi_protect with ring
events), its last `driver_availability` said available. A driver that never
reports follows the transport alone.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

CALL_STATE_OPTIONS = ["idle", "ringing", "answered", "active", "ended"]


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([PorticoCallStateSensor(entry)])


class PorticoCallStateSensor(SensorEntity):
    """Mirrors CallStateTracker.state (call_state.py) into HA."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = CALL_STATE_OPTIONS
    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_name = "Call state"

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._runtime = entry.runtime_data
        self._attr_unique_id = f"{entry.entry_id}_call_state"
        if self._runtime.ring_only:
            # The doors have no HA device, so "Call state" alone would collide with the gate's `sensor.call_state`
            # (and become `sensor.call_state_2`). A ring-only door names its own: `sensor.<name>_call_state`.
            self._attr_has_entity_name = False
            self._attr_name = f"{entry.title} call state"
        elif entry.data.get("driver") == "gate_sip_server":
            # W5: the server entry's sensor has a name of its own (`sensor.sip_server_call_state`), so HA never has to
            # number it `sensor.call_state_2`.
            self._attr_name = "SIP server call state"

    @property
    def available(self) -> bool:
        return self._runtime.entities_available

    @property
    def native_value(self) -> str:
        return self._runtime.call_state.state

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"call_id": self._runtime.call_state.call_id}

    async def async_added_to_hass(self) -> None:
        tracker = self._runtime.call_state
        tracker.add_listener(self._handle_change)
        self.async_on_remove(lambda: tracker.remove_listener(self._handle_change))
        self._runtime.add_availability_listener(self._handle_change)
        self.async_on_remove(lambda: self._runtime.remove_availability_listener(self._handle_change))

    @callback
    def _handle_change(self) -> None:
        self.async_write_ha_state()
