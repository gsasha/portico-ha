"""The sensor platform of the three entry kinds: `sensor.portico_push_ring_notification` on the push entry (FCM sender design §7), on
each phone entry (P-4, ENROLL-02) the phone's push registration and last connection, and on each door entry the door's
call state and listen leg (`door/entities/sensor.py`, STATE-01). The push ring notification sensor's unique id
(`<push entry id>_away_ring`) is unchanged.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util
from homeassistant.util import slugify

from .. import live
from ..door.const import is_door_entry
from .call_sensors import PorticoCallsSensor
from ..door.entities.sensor import async_setup_entry as async_setup_door_sensors
from ..panel_wall import STATES as PANEL_STATES, get_panel_wall
from ..phone_entry import CONF_PANEL, is_phone_entry, phone_device_identifiers
from ..provider import DOMAIN
from ..push import get_manager
from ..push.const import SIGNAL_PHONES_CHANGED

AWAY_RING_OPTIONS = ["ok", "degraded", "credentials_invalid", "disabled"]

# A phone's push registration: a registry row's state, or why there is none.
PUSH_STATE_NOT_REGISTERED = "not_registered"      # the push ring notification is on but this phone's app has not registered
PUSH_STATE_AWAY_RING_OFF = "away_ring_off"        # no push ring notification entry is loaded
PHONE_PUSH_OPTIONS = ["ok", "never_sent", "unregistered", "project_mismatch",
                      PUSH_STATE_NOT_REGISTERED, PUSH_STATE_AWAY_RING_OFF]


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    # The door check comes BEFORE the fall-through: a door is not the push ring notification, and any entry that is neither a phone
    # nor a door is the push entry (the push ring notification sensor).
    if is_door_entry(entry):
        await async_setup_door_sensors(hass, entry, async_add_entities)
        return
    if is_phone_entry(entry):
        async_add_entities([PhonePushRegistrationSensor(entry), PhoneLastConnectionSensor(entry)])
        # DEV-807: the readiness sensor of a panel. A phone becomes a panel after its entities were added ("make a panel
        # screen"), so the adder is kept and `async_ensure_panel_sensor` adds it then; a house with no panel gets none.
        hass.data.setdefault(DOMAIN, {}).setdefault(PANEL_ADDERS, {})[entry.entry_id] = async_add_entities

        def _forget_adder() -> None:           # returns None: a callback that returns something is awaited as a job
            hass.data.get(DOMAIN, {}).get(PANEL_ADDERS, {}).pop(entry.entry_id, None)

        entry.async_on_unload(_forget_adder)
        async_ensure_panel_sensor(hass, entry)
        return
    async_add_entities([PorticoAwayRingSensor(entry), PorticoCallsSensor(entry), PorticoLiveSessionsSensor(entry)])


PANEL_ADDERS = "panel_sensor_adders"      # entry id -> the platform's `async_add_entities`
PANEL_SENSORS = "panel_sensors"            # entry ids that already have their readiness sensor
PANEL_TICK = timedelta(seconds=30)         # the server timer: a stale heartbeat turns the sensor `not_ready`


@callback
def async_ensure_panel_sensor(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Add `sensor.portico_panel_<name>` for a phone entry whose `panel` option is on, once. False when it is not a panel, has one
    already, or the sensor platform of this entry is not set up (it is added when it is)."""
    data = hass.data.setdefault(DOMAIN, {})
    adder = data.get(PANEL_ADDERS, {}).get(entry.entry_id)
    done = data.setdefault(PANEL_SENSORS, set())
    if adder is None or entry.entry_id in done or entry.options.get(CONF_PANEL) is not True:
        return False
    done.add(entry.entry_id)

    def _forget() -> None:
        done.discard(entry.entry_id)

    entry.async_on_unload(_forget)
    adder([PanelReadinessSensor(entry)])
    return True


class _PhoneSensor(SensorEntity):
    """Common to the two per-phone sensors (ENROLL-02): the phone's device, a name that makes the entity id
    `sensor.<phone name>_<what>` (never the entry id), and the registry rows of THIS phone (by enroll id, or by HA
    user for a row registered before enroll ids). Never a registration value."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_device_info = DeviceInfo(identifiers=phone_device_identifiers(entry))

    def _rows(self) -> list[dict[str, Any]] | None:
        """This phone's registry rows, or None when the push ring notification is not loaded."""
        manager = get_manager(self.hass)
        if manager is None:
            return None
        data = self._entry.data
        return manager.registry.rows_for_phone(data.get("enroll_id"), data.get("ha_user_id") or "")

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_PHONES_CHANGED, self._handle_change))

    @callback
    def _handle_change(self) -> None:
        self.async_write_ha_state()


class PhonePushRegistrationSensor(_PhoneSensor):
    """`sensor.<phone>_push_registration`: the state of the phone's newest registration with the push ring notification."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = PHONE_PUSH_OPTIONS
    _attr_name = "Push registration"
    _attr_icon = "mdi:cellphone-message"

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry)
        self._attr_unique_id = f"{entry.entry_id}_push_registration"

    @staticmethod
    def _newest(rows: list[dict[str, Any]]) -> dict[str, Any]:
        return max(rows, key=lambda r: r.get("last_seen_at") or "")

    @property
    def native_value(self) -> str:
        rows = self._rows()
        if rows is None:
            return PUSH_STATE_AWAY_RING_OFF
        if not rows:
            return PUSH_STATE_NOT_REGISTERED
        state = self._newest(rows).get("state") or "never_sent"
        return state if state in PHONE_PUSH_OPTIONS else "never_sent"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        rows = self._rows() or []
        newest = self._newest(rows) if rows else {}
        return {
            "registrations": len(rows),
            "away_ring_enabled": any(bool(r.get("push_enabled")) for r in rows),
            "last_result": newest.get("last_result"),
            "last_ok_at": newest.get("last_ok_at"),
        }


class PhoneLastConnectionSensor(_PhoneSensor):
    """`sensor.<phone>_last_connection`: when the phone's app last connected. The app registers with the push ring notification
    on every WebSocket connect, so the newest registration's `last_seen_at` is that moment."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_name = "Last connection"
    _attr_icon = "mdi:lan-connect"

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry)
        self._attr_unique_id = f"{entry.entry_id}_last_connection"

    @property
    def native_value(self) -> datetime | None:
        seen = [dt_util.parse_datetime(r.get("last_seen_at") or "") for r in (self._rows() or [])]
        seen = [s for s in seen if s is not None]
        return max(seen) if seen else None


class PanelReadinessSensor(SensorEntity):
    """`sensor.portico_panel_<name>`: `ready` | `ready_no_doors` | `not_ready` | `unknown` (panel_wall.py, design section 9). It is
    written only when the state or an attribute other than `last_seen` changes; a server timer turns a stale heartbeat into
    `not_ready` with `heartbeat_stale`. No `phone_endpoint` in the attributes."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = PANEL_STATES
    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_name = "Readiness"                      # friendly name "<phone> Readiness"; the entity id is set below
    _attr_icon = "mdi:tablet-dashboard"

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._user_id: str = entry.data.get("ha_user_id") or ""
        name = str(entry.data.get("name") or entry.title or "panel")
        self.entity_id = f"sensor.portico_panel_{slugify(name)}"      # `sensor.portico_panel_<name>`, not prefixed by the device
        self._attr_unique_id = f"{entry.entry_id}_panel_readiness"
        self._attr_device_info = DeviceInfo(identifiers=phone_device_identifiers(entry))
        self._signature: tuple | None = None
        self._state = "unknown"
        self._attrs: dict[str, Any] = {}

    @property
    def native_value(self) -> str:
        return self._state

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._attrs

    async def async_added_to_hass(self) -> None:
        wall = get_panel_wall(self.hass)
        self.async_on_remove(wall.add_listener(self._user_id, self._refresh))
        self.async_on_remove(async_track_time_interval(self.hass, self._tick, PANEL_TICK))
        self.async_on_remove(lambda: wall.cancel_user(self._user_id))
        self._refresh(write=False)       # the platform writes the first state itself

    @callback
    def _tick(self, _now) -> None:
        self._refresh()

    @callback
    def _refresh(self, write: bool = True) -> None:
        import time

        wall = get_panel_wall(self.hass)
        state, attrs = wall.evaluate(self._user_id)
        signature = (state, tuple(sorted((k, repr(v)) for k, v in attrs.items())))
        seen = wall.last_seen(self._user_id)
        if seen is not None:
            attrs = {**attrs, "last_seen": dt_util.utcnow() - timedelta(seconds=max(0.0, time.monotonic() - seen))}
        if signature == self._signature:
            self._attrs = {**self._attrs, "last_seen": attrs.get("last_seen")} if "last_seen" in attrs else self._attrs
            return                      # `last_seen` alone moved: the state machine is not written for it
        self._signature = signature
        self._state, self._attrs = state, attrs
        if write and self.hass is not None and self.entity_id:
            self.async_write_ha_state()


class PorticoAwayRingSensor(SensorEntity):
    """`sensor.portico_push_ring_notification` on the push entry (FCM sender design §7):
    ok | degraded | credentials_invalid | disabled, with the last send, 24 h
    counts and one row per registered phone. Never a registration value or
    a key: the attributes come from PushManager.attributes()."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = AWAY_RING_OPTIONS
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Portico push ring notification"
    _attr_icon = "mdi:cellphone-message"
    # The per-phone list changes on every send; keep it out of the recorder.
    _unrecorded_attributes = frozenset({"devices"})

    def __init__(self, entry: ConfigEntry) -> None:
        self._manager = entry.runtime_data
        self._attr_unique_id = f"{entry.entry_id}_away_ring"

    @property
    def native_value(self) -> str:
        return self._manager.state

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._manager.attributes()

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._manager.add_listener(self._handle_change))

    @callback
    def _handle_change(self) -> None:
        self.async_write_ha_state()


class PorticoLiveSessionsSensor(SensorEntity):
    """`sensor.portico_live_sessions` (DEV-805): how many live video sessions are open now, in all; the attributes carry the
    count per endpoint and the two limits. A diagnostic, written only when a session opens or closes."""

    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_name = "Portico live sessions"
    _attr_icon = "mdi:video-wireless"
    _attr_state_class = "measurement"
    _unrecorded_attributes = frozenset({"per_endpoint"})

    def __init__(self, entry: ConfigEntry) -> None:
        self._attr_unique_id = f"{entry.entry_id}_live_sessions"

    @property
    def native_value(self) -> int:
        return live.registry(self.hass).count()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        reg = live.registry(self.hass)
        per: dict[str, int] = {}
        for session in reg.sessions.values():
            per[session.endpoint_id] = per.get(session.endpoint_id, 0) + 1
        return {"per_endpoint": per, "limit_per_endpoint": live.MAX_LIVE_PER_ENDPOINT,
                "limit_per_install": live.MAX_LIVE_PER_INSTALL}

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(live.registry(self.hass).add_listener(self._handle_change))

    @callback
    def _handle_change(self) -> None:
        self.async_write_ha_state()
