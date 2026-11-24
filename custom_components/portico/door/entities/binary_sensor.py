"""`SIP station registered` (diagnostic): whether the door station is registered with Portico's own SIP server (DEV-518).

Only a `gate_sip_server` entry has it. The server accepts an INVITE only from the station that has REGISTERed (a press
before that gets 403 "not the registered station", e.g. up to ~5 minutes after an HA restart), and nothing else in HA
showed it. The signal is the driver's `StationRegistered` capability (wheel 0.7.2 and newer), which the transport
republishes (`capabilities_changed`) on every good REGISTER, when the registration passes its TTL (330 s, REGISTER about
every 298 s) and on a de-register, with `dial_enabled` on or off. `LastRegisterAt` (unix seconds of the last good
REGISTER) becomes the `last_register` attribute (UTC ISO time), so a stale registration is visible.

Older wheel: no `StationRegistered`, so the entity falls back to `CanDial`, which the transport publishes only while the
entry's `dial_enabled` switch is on (CanDial = dial_enabled AND registered); with the switch off it is `unavailable`
rather than a false `off`. Unavailable too while the transport is down.
"""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from ..gate_sip_listen import SERVER_DRIVER


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    if entry.data.get("driver") == SERVER_DRIVER:
        async_add_entities([PorticoStationRegisteredSensor(entry)])


class PorticoStationRegisteredSensor(BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_name = "SIP station registered"

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._runtime = entry.runtime_data
        self._attr_unique_id = f"{entry.entry_id}_station_registered"

    @property
    def available(self) -> bool:
        # An older wheel publishes no `StationRegistered` and no `CanDial` with dial_enabled off: "off" would be a lie.
        return self._runtime.transport_available and (
            self._reported() is not None or self._entry.data.get("dial_enabled") is True
        )

    def _reported(self) -> bool | None:
        value = (self._runtime.capabilities or {}).get("StationRegistered")
        return value if isinstance(value, bool) else None

    @property
    def is_on(self) -> bool:
        reported = self._reported()
        return self._runtime.can_dial if reported is None else reported

    @property
    def extra_state_attributes(self) -> dict | None:
        at = (self._runtime.capabilities or {}).get("LastRegisterAt")
        if isinstance(at, bool) or not isinstance(at, (int, float)) or at <= 0:
            return None
        return {"last_register": dt_util.utc_from_timestamp(at).isoformat()}

    async def async_added_to_hass(self) -> None:
        runtime = self._runtime
        runtime.add_capabilities_listener(self._handle_change)
        self.async_on_remove(lambda: runtime.remove_capabilities_listener(self._handle_change))
        runtime.add_availability_listener(self._handle_change)
        self.async_on_remove(lambda: runtime.remove_availability_listener(self._handle_change))

    @callback
    def _handle_change(self) -> None:
        self.async_write_ha_state()
