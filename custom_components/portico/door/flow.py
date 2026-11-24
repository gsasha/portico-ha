"""The door branch of the Portico config flow: adding a door, and a door's options.

Design doc §4.7, §9.2. A standard "Add Integration" wizard:
pick a driver (dahua_sip, unifi_protect, ha_event) from a dropdown, fill in that
driver's own IP/credential/driver-specific fields (each driver's
schema lives in drivers/, §4.2), then OPTIONALLY pick an HA action to
run when this device's relay lock is opened. That last step exists
because a device's own onboard relay does not necessarily drive the
thing you actually want opened: this estate's own gate is a real
example — the Dahua VTO's relay was deliberately never wired to the
gate motor (a Shelly dry-contact relay is, wired straight into the
Beninca gate controller — see gate_intercom_two_way_audio_design.md's
2026-09-05 note and devices/smart_devices.md's
`example-gate-relay-ex-fence` row); the VTO's relay is reserved for an
unwired side door instead. the owner's call (2026-09-27): let the
installer point "open" at whatever HA action actually does the job,
rather than Portico assuming a device's own relay is the one that
matters.

This is a MIXIN of the one `PorticoConfigFlow` (`../config_flow.py`), whose first step is a menu: "Add a door" lands
on `async_step_door` below, "Push ring notification" on the push branch. A created door entry carries `entry_type: "door"` and keeps
its `driver` field (the driver id is a door's own datum). Phone enrollment, the Tailscale question and the push ring notification
push entry are the other branches. The one option a door keeps is "Away call audio" (the door entry's `away_media`
option, read by provider_impl.py).
"""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers import selector

from .const import ENTRY_TYPE, ENTRY_TYPE_DOOR
from .drivers import DRIVERS, ha_event, is_local_driver, load_drivers
from .drivers.gate_sip_server import KEY as SIP_SERVER_KEY, REMOTE_SCHEMA, entry_conflicts, normalize_remote, remote_conflicts


# The field each driver's own CONFIG_SCHEMA (§4.2) uses to identify the
# physical device it configures — used only to build a per-device
# unique_id so the same device can't be added twice. Not part of
# DriverMeta itself: DriverMeta carries the config-flow schema ONLY
# (drivers/__init__.py), and this mapping is a config-flow-local concern.
_UNIQUE_ID_FIELD = {
    "dahua_sip": "host",
    "unifi_protect": "nvr_host",
    # `gate-sip-server:<host>:<port>`: the listen address, never the station's (DEV-518 W2).
    "gate_sip_server": "device_id",
    # The normalised event entity id, set by the driver's normalize: one door per doorbell event entity.
    "ha_event": "event_entity",
}


class DoorConfigFlowMixin:
    """Mixed into PorticoConfigFlow: the steps that add a door (§4.7). The attributes are read with class defaults
    (and assigned per instance), so the flow's constructor does not need to know about a door."""

    _driver_key: str | None = None
    _driver_config: dict[str, Any] = {}
    _camera_config: dict[str, Any] = {}

    async def async_step_door(self, user_input: dict[str, Any] | None = None):
        """Step 1: driver selection (reached from the first menu's "Add a door")."""
        load_drivers()

        if user_input is not None:
            self._driver_key = user_input["driver"]
            return await self.async_step_driver_config()

        # A hidden driver (gate_sip_server) is not in the picker: it has its own menu item.
        choices = {key: key for key in sorted(DRIVERS) if not DRIVERS[key].hidden}
        schema = vol.Schema({vol.Required("driver"): vol.In(choices)})
        return self.async_show_form(step_id="door", data_schema=schema)

    async def async_step_sip_server_door(self, user_input: dict[str, Any] | None = None):
        """The first menu's "SIP-server door (advanced)": Portico's own SIP server for the gate station (DEV-518 W2).
        It is its own menu item, not a picker choice, and the form refuses to submit without a confirmation tick. It
        goes on to the ordinary driver_config form, so everything after is the common door flow."""
        load_drivers()
        self._driver_key = SIP_SERVER_KEY
        return await self.async_step_driver_config()

    async def async_step_sip_server_remote(self, user_input: dict[str, Any] | None = None):
        """The first menu's "SIP-server door, remote container (advanced)" (DEV-518 W8b): the same door, but its SIP server
        runs in a `portico-transport` container and this entry only connects to it (door/remote.py). Local checks only;
        the connection is made after setup, in the background. Goes on to the ordinary camera step."""
        load_drivers()
        self._driver_key = SIP_SERVER_KEY
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                data = normalize_remote(user_input)
            except vol.Invalid as err:
                errors[str(err.path[0]) if err.path else "base"] = err.error_message
            else:
                problem = remote_conflicts(data, [dict(e.data) for e in self.hass.config_entries.async_entries("portico")])
                if problem is not None:
                    errors[problem[0]] = problem[1]
            if not errors:
                await self.async_set_unique_id(f"{SIP_SERVER_KEY}:{data['device_id']}")
                self._abort_if_unique_id_configured()
                self._driver_config = data
                return await self.async_step_camera_config()
        return self.async_show_form(step_id="sip_server_remote", data_schema=REMOTE_SCHEMA, errors=errors)

    async def async_step_driver_config(self, user_input: dict[str, Any] | None = None):
        """Step 2: the selected driver's own schema (drivers/*.py, §4.2)."""
        assert self._driver_key is not None  # only reachable via async_step_door
        driver = DRIVERS[self._driver_key]

        errors: dict[str, str] = {}
        if user_input is not None:
            if driver.normalize is not None:
                # Local checks only — no driver's flow contacts its device
                # (§7).
                try:
                    user_input = driver.normalize(user_input)
                except vol.Invalid as err:
                    field = str(err.path[0]) if err.path else "base"
                    errors[field] = err.error_message
            if not errors and self._driver_key == SIP_SERVER_KEY:
                # Needs the other entries (other server entries' ports), so it is not
                # part of the pure normalize. Reads entries only; touches no network.
                problem = entry_conflicts(user_input, [dict(e.data) for e in self.hass.config_entries.async_entries("portico")])
                if problem is not None:
                    errors[problem[0]] = problem[1]
            if not errors:
                id_field = _UNIQUE_ID_FIELD[self._driver_key]
                await self.async_set_unique_id(f"{self._driver_key}:{user_input[id_field]}")
                self._abort_if_unique_id_configured()
                self._driver_config = user_input
                return await self.async_step_camera_config()

        return self.async_show_form(
            step_id="driver_config",
            data_schema=driver.config_schema,
            description_placeholders={"driver": driver.key},
            errors=errors,
        )

    async def async_step_camera_config(self, user_input: dict[str, Any] | None = None):
        """Step 3: optionally point this device's camera entity at an
        already-configured camera entity instead of creating a new one
        (§9.2, entities/camera.py) — e.g. an existing UniFi Protect or
        Frigate camera entity for the same physical device, avoiding a
        duplicate. Skippable: leaving it empty lets Portico create its own
        camera entity, backed by HA's bundled go2rtc instance (§4.4).

        Required, not skippable, for a driver with no video of its own
        (DriverMeta.has_own_video False — gate_sip_server, ha_event): its
        only camera is an HA camera entity."""
        assert self._driver_key is not None
        driver = DRIVERS[self._driver_key]

        if user_input is not None:
            self._camera_config = user_input
            if is_local_driver(self._driver_key):
                # A ring-only door has no relay to point an action at: the step is not offered, so no door-open
                # action can be stored on it.
                return self._create_door_entry({})
            return await self.async_step_relay_action()

        marker = vol.Optional if driver.has_own_video else vol.Required
        schema = vol.Schema(
            {
                marker("existing_camera_entity"): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="camera")
                )
            }
        )
        return self.async_show_form(
            step_id="camera_config",
            data_schema=schema,
            description_placeholders={"driver": driver.key},
        )

    async def async_step_relay_action(self, user_input: dict[str, Any] | None = None):
        """Step 4: optionally pick the HA action this device's relay lock's
        `open` should run (§ this docstring block's header note above) —
        skippable (an empty action falls back to the driver's own
        OpenRelay via the transport binary, once that exists)."""
        assert self._driver_key is not None
        driver = DRIVERS[self._driver_key]

        if user_input is not None:
            return self._create_door_entry(user_input)

        schema = vol.Schema({vol.Optional("open_relay_action"): selector.ActionSelector()})
        return self.async_show_form(step_id="relay_action", data_schema=schema)

    def _create_door_entry(self, user_input: dict[str, Any]):
        assert self._driver_key is not None
        driver = DRIVERS[self._driver_key]
        # A driver whose form asks for a `name` (ha_event: "front") titles the entry with it.
        title = self._driver_config.get("name") or f"Portico ({driver.key})"
        return self.async_create_entry(
            title=str(title),
            data={
                ENTRY_TYPE: ENTRY_TYPE_DOOR,
                "driver": self._driver_key,
                **self._driver_config,
                "existing_camera_entity": self._camera_config.get("existing_camera_entity"),
                "open_relay_action": user_input.get("open_relay_action") or [],
            },
        )

class DoorOptionsFlow(config_entries.OptionsFlow):
    """The door's Configure form. Away call audio over the websocket (the UI part's away_media.py; design:
    automation/portico_away_media_design.md) is always on and has no option. An `ha_event`
    door (the front door) also shows its talk-back fields (portico_front_door_talk_plan.md, T7), stored in the options
    too and laid over the entry's data wherever they are read (ha_event.merged_talk_data); saving a change reloads the
    entry (door/__init__.py), which attaches or detaches the Go driver. The other settings of a door are not options:
    the listen leg is the Reconfigure form.

    The Protect API key is a password-style field that is never shown again. Leaving it blank on a later save KEEPS the
    stored key (also while talkback is turned off: switching off never deletes it). The key is removed only by ticking
    "Remove the stored Protect key" with talkback off."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        # The push entry's options entry point is a menu with the step id `init`; the door's form has its own step id
        # (`door_init`, strings.json) so the two never share a text.
        return await self.async_step_door_init(user_input)

    async def async_step_door_init(self, user_input: dict[str, Any] | None = None):
        has_talk = self.config_entry.data.get("driver") == ha_event.KEY
        merged = ha_event.merged_talk_data(self.config_entry.data, self.config_entry.options)
        errors: dict[str, str] = {}
        if user_input is not None:
            options = dict(self.config_entry.options)
            options.pop("away_media", None)       # the retired Away call audio option: dropped when the form is saved
            if has_talk:
                talk, errors = self._talk_from_form(user_input, merged)
                options.update(talk)
            if not errors:
                return self.async_create_entry(data=options)
            merged = {**merged, **{k: v for k, v in user_input.items() if k in ha_event.TALK_DEFAULTS or k == "talkback"}}
        fields: dict[Any, Any] = {}
        if has_talk:
            value = lambda key: merged.get(key) if merged.get(key) not in (None, "") else ha_event.TALK_DEFAULTS[key]  # noqa: E731
            fields.update({
                vol.Required("talkback", default=merged.get("talkback") is True): bool,
                # No default, ever: the stored key is never put back into the form.
                vol.Optional("protect_api_key"): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)),
                vol.Optional("clear_protect_api_key", default=False): bool,
                vol.Optional("protect_host", default=str(value("protect_host"))): str,
                vol.Optional("camera_id", default=str(value("camera_id"))): str,
                vol.Optional("downlink_url", default=str(value("downlink_url"))): str,
                # suggested_value, not default: a default would put a cleared field back (HA fills an omitted key from it)
                vol.Optional("guard_url", description={"suggested_value": str(value("guard_url"))}): str,
                vol.Optional("verify_ssl", default=bool(value("verify_ssl"))): bool,
                vol.Optional("gain", default=int(value("gain"))): selector.NumberSelector(
                    selector.NumberSelectorConfig(min=1, max=16, step=1, mode=selector.NumberSelectorMode.BOX)),
            })
        return self.async_show_form(
            step_id="door_talk_init" if has_talk else "door_init", data_schema=vol.Schema(fields), errors=errors,
            description_placeholders={"key_state": "stored" if merged.get("protect_api_key") else "not set"} if has_talk else None,
        )

    async def async_step_door_talk_init(self, user_input: dict[str, Any] | None = None):
        """The same form under its own step id (an ha_event door's text mentions talk-back; strings.json)."""
        return await self.async_step_door_init(user_input)

    @staticmethod
    def _talk_from_form(user_input: dict[str, Any], merged: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
        """(the talk options to store, errors). The submitted fields are checked by the driver's own `normalize` with the
        key resolved first: typed key, else "" if cleared, else the stored one."""
        talk: dict[str, Any] = {"talkback": bool(user_input.get("talkback"))}
        for key in ("protect_host", "camera_id", "downlink_url", "guard_url"):
            talk[key] = str(user_input.get(key) or "").strip() or ha_event.TALK_DEFAULTS[key]
        talk["verify_ssl"] = bool(user_input.get("verify_ssl", ha_event.TALK_DEFAULTS["verify_ssl"]))
        try:
            talk["gain"] = int(user_input.get("gain", ha_event.TALK_DEFAULTS["gain"]))
        except (TypeError, ValueError):
            return {}, {"gain": "invalid_talkback_option"}
        if not 1 <= talk["gain"] <= 16:
            return {}, {"gain": "invalid_talkback_option"}
        typed = str(user_input.get("protect_api_key") or "").strip()
        if typed:
            talk["protect_api_key"] = typed
        elif user_input.get("clear_protect_api_key"):
            talk["protect_api_key"] = ""
        problem = ha_event.validate_talk_options({**merged, **talk})
        if problem is not None:
            return {}, {problem[0]: problem[1]}
        return talk, {}
