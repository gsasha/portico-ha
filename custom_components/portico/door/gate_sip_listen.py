"""The gate_sip_server entry's settings the Python side owns: the ring-answered helper, the `configure` payload, the
Reconfigure form and the `house_calls` option rules (DEV-518).

The module's name is historical: it held the gate_sip_observer's listen leg, claims and peer-token Store until the observer
door was retired (DEV-518 C5, D-20261122-ad10). What is left is the server entry's.

Where each setting lives.
  - ``entry.data["gate_sip_listen"] = {"ring_answered_entity": "input_boolean.x"}``: Python's own bookkeeping key, never sent
    to the transport (``transport_config`` strips it). Entry DATA, not options: an options flow's final
    ``async_create_entry`` replaces ``entry.options`` wholesale, data written through ``async_update_entry`` survives.
  - ``ring_answered_entity`` is the helper turned ON when Portico's user has answered, which is what closes every phone's
    ring screen as "taken elsewhere" (design section 12 D1). It is the only Home Assistant write made at answer time, once
    per granted answer, by ``async_after_answer`` and by nothing else.

What reaches the transport. ``transport_config`` builds the ``configure`` payload: ``{**entry.data, "device_id": ...}``
minus Python's own ``gate_sip_listen`` key and the entry kind ``entry_type``.

House calls (M7 3g): ``house_calls`` (default off) is the door entry's OPTION that hub.py reads, at the start of the
Portico hub process only, to pass ``-house-calls`` to a wheel that has the bridge (0.6.0 or newer). It lives in the
entry's options, never in its data, so the ``configure`` payload does not carry it, and it is stored ONLY when on. Saving
it ON with an older wheel is refused (``house_needs_wheel``), the mirror of the hub's own rule.

The form is a *Reconfigure* flow, not a Configure menu entry, so the existing Configure menu (and its text) is untouched.
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import selector

from .const import ENTRY_TYPE

_LOGGER = logging.getLogger(__name__)

KEY = "gate_sip_listen"
SERVER_DRIVER = "gate_sip_server"   # W5: Portico's own SIP server; it is the answerer, so it has no listen leg or claims


def settings(data) -> dict[str, Any]:
    """{"ring_answered_entity"} from an entry's data: the helper's entity id, or None for anything missing or malformed."""
    raw = data.get(KEY) if isinstance(data.get(KEY), dict) else {}
    entity = raw.get("ring_answered_entity")
    if not (isinstance(entity, str) and entity.startswith("input_boolean.") and len(entity) > len("input_boolean.")):
        entity = None
    return {"ring_answered_entity": entity}


async def transport_config(hass: HomeAssistant, entry: ConfigEntry, device_id: str) -> dict[str, Any]:
    """The ``configure`` payload for this entry (see the module docstring)."""
    # Python's own keys never reach the transport: the bookkeeping key and the entry kind (`entry_type`).
    config = {k: v for k, v in entry.data.items() if k not in (KEY, ENTRY_TYPE)}
    config["device_id"] = device_id
    if entry.data.get("driver") == SERVER_DRIVER:
        # DEV-811 P3: ask the wheel for the call counters. A wheel that does not know the key ignores it (the driver parses the
        # keys it knows); one that does lists `call_stats` in the capabilities and the stats then flow. Taking this line out
        # is the integration-side off switch (design section 7).
        config["call_stats"] = True
    return config


async def async_after_answer(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Portico's user answered the ring: turn the ring-answered helper ON so every phone's ring screen closes as "taken
    elsewhere" (design section 12 D1). Does nothing unless this is a gate_sip_server entry with a helper chosen (W5). A
    failure is logged and swallowed: the call is already answered, and a missing helper must not turn that into an error for
    the person talking."""
    if entry.data.get("driver") != SERVER_DRIVER:
        return
    entity = settings(entry.data)["ring_answered_entity"]
    if not entity:
        return
    try:
        await hass.services.async_call("input_boolean", "turn_on", {"entity_id": entity}, blocking=False)
    except Exception as err:  # noqa: BLE001 - see the docstring
        _LOGGER.warning("portico: answered the ring but could not turn on %s (%s: %s)", entity, type(err).__name__, err)
        return
    _LOGGER.info("portico: answered the ring; turned on %s", entity)


def house_calls_error(*, house: bool, house_now: bool, house_wheel: bool) -> str | None:
    """The one rule about the `house_calls` option, called by the dialog (through `validate_listen_settings`) and by the
    Settings tab (`door_settings/set`): ON needs a wheel that has the bridge, unless it is already on (so it can be switched
    off after a wheel rollback). The error key, or None."""
    return "house_needs_wheel" if house and not (house_wheel or house_now) else None


def house_options(options: Any, house: bool) -> dict[str, Any]:
    """What the entry's options hold after a save that names `house_calls`: the option stored ONLY when on, every other
    option kept. The dialog and the Settings tab both write this shape."""
    from .hub import CONF_HOUSE_CALLS

    return {**{k: v for k, v in dict(options).items() if k != CONF_HOUSE_CALLS}, **({CONF_HOUSE_CALLS: True} if house else {})}


class GateSipListenFlowMixin:
    """Mixed into PorticoConfigFlow: Reconfigure for a gate_sip_server entry."""

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None):
        entry = self._get_reconfigure_entry()
        if entry.data.get("driver") == SERVER_DRIVER:
            return await self.async_step_reconfigure_server(user_input)
        return self.async_abort(reason="reconfigure_not_supported")

    async def async_step_reconfigure_server(self, user_input: dict[str, Any] | None = None):
        """W5: what a gate_sip_server entry can change after it exists: `dial_enabled`, the ring-answered helper and
        (DEV-518 C1) the door-open action `open_relay_action`. Nothing else of that entry (ports, password, the LAN
        unlock) is editable here: those are the entry's identity and its guards. Writes entry data only; touches no
        network. Reloads the entry ONLY when a key the transport's `configure` carries changed (`dial_enabled`, the
        remote address/token): a reload drops the SIP registration for 30 s to 5 min. The helper
        (`async_after_answer`) and the action (`provider.open_relay`) are read from `entry.data` at call time, so an
        edit that touches only those is saved without a reload. So is the option `house_calls` (DEV-518 C2): the hub
        reads it once, when its process starts, from any door entry.

        A REMOTE-mode entry (remote_cfg.py, W8b) also shows the container's address, port and token here (the token as a
        row of stars while stored: stars or blank keep it, a typed value replaces it), under the same checks as the add form."""
        from ..enrollment_flow import SECRET_MASK, _PASSWORD, _typed_secret
        from .drivers.gate_sip_server import parse_dial, parse_ring_entity
        from . import remote_cfg
        from .hub import CONF_HOUSE_CALLS, read_installed_wheel_version, wheel_has_house_calls

        entry = self._get_reconfigure_entry()
        if entry.data.get("driver") != SERVER_DRIVER:
            return self.async_abort(reason="reconfigure_not_supported")
        # DEV-518 C2: the hub's `-house-calls` flag is read from ANY door entry's option, once, when its process starts.
        # Read only at the hub's next start, so saving it never needs a reload.
        house_now = entry.options.get(CONF_HOUSE_CALLS) is True
        house_wheel = wheel_has_house_calls(await self.hass.async_add_executor_job(read_installed_wheel_version))
        remote = remote_cfg.is_remote(entry.data)
        errors: dict[str, str] = {}
        if user_input is not None:
            remote_updates: dict[str, Any] = {}
            try:
                dial = parse_dial(bool(user_input.get("dial_enabled")))
                entity = parse_ring_entity(user_input.get("ring_answered_entity") or None)
                action = user_input.get("open_relay_action") or []
                if not isinstance(action, list) or not all(isinstance(a, dict) for a in action):
                    raise vol.Invalid("invalid_action", path=["open_relay_action"])
                options = dict(entry.options)         # untouched unless the form named `house_calls`
                if CONF_HOUSE_CALLS in user_input:
                    house = bool(user_input[CONF_HOUSE_CALLS])
                    code = house_calls_error(house=house, house_now=house_now, house_wheel=house_wheel)
                    if code:
                        raise vol.Invalid(code, path=[CONF_HOUSE_CALLS])
                    options = house_options(entry.options, house)
                if remote:
                    # The container's address and token are editable here (it can move; the token can rotate). The token
                    # field shows stars while one is stored: stars or blank keep it, a typed value replaces it.
                    typed, mask_error = _typed_secret(user_input.get(remote_cfg.CONF_TOKEN))
                    if mask_error:
                        raise vol.Invalid(mask_error, path=[remote_cfg.CONF_TOKEN])
                    host = user_input.get(remote_cfg.CONF_HOST, entry.data[remote_cfg.CONF_HOST])
                    port = user_input.get(remote_cfg.CONF_PORT, entry.data[remote_cfg.CONF_PORT])
                    if isinstance(port, float) and port == int(port):
                        port = int(port)
                    token = typed or entry.data[remote_cfg.CONF_TOKEN]
                    problem = remote_cfg.check_remote(host, port, token)
                    if problem is not None:
                        raise vol.Invalid(problem[1], path=[problem[0]])
                    remote_updates = {remote_cfg.CONF_HOST: remote_cfg.normalize_host(host),
                                      remote_cfg.CONF_PORT: port, remote_cfg.CONF_TOKEN: token}
            except vol.Invalid as err:
                errors[str(err.path[0]) if err.path else "base"] = err.error_message
            else:
                data = {k: v for k, v in entry.data.items() if k not in ("dial_enabled", KEY, "open_relay_action")}
                data.update(remote_updates)
                if dial:
                    data["dial_enabled"] = True
                if entity:
                    data[KEY] = {"ring_answered_entity": entity}
                data["open_relay_action"] = action      # a list, [] when none: the shape the add flow writes
                transport_keys = ("dial_enabled", remote_cfg.CONF_HOST, remote_cfg.CONF_PORT, remote_cfg.CONF_TOKEN)
                if all(entry.data.get(k) == data.get(k) for k in transport_keys):
                    # Nothing the transport was configured with changed: save without a reload (keeps the registration).
                    self.hass.config_entries.async_update_entry(entry, data=data, options=options)
                    return self.async_abort(reason="reconfigure_successful")
                return self.async_update_reload_and_abort(entry, data=data, options=options)
        current = settings(entry.data)
        entity_key = (
            vol.Optional("ring_answered_entity", description={"suggested_value": current["ring_answered_entity"]})
            if current["ring_answered_entity"] else vol.Optional("ring_answered_entity")
        )
        fields: dict[Any, Any] = {}
        if remote:
            fields[vol.Required(remote_cfg.CONF_HOST, default=entry.data[remote_cfg.CONF_HOST])] = str
            fields[vol.Required(remote_cfg.CONF_PORT, default=entry.data[remote_cfg.CONF_PORT])] = selector.NumberSelector(
                selector.NumberSelectorConfig(min=1, max=65535, step=1, mode=selector.NumberSelectorMode.BOX))
            fields[vol.Optional(remote_cfg.CONF_TOKEN, description={"suggested_value": SECRET_MASK})] = _PASSWORD
        fields[vol.Required("dial_enabled", default=entry.data.get("dial_enabled") is True)] = selector.BooleanSelector()
        fields[entity_key] = selector.EntitySelector(selector.EntitySelectorConfig(domain="input_boolean"))
        current_action = entry.data.get("open_relay_action")
        fields[vol.Optional("open_relay_action", description={"suggested_value": current_action})
               if current_action else vol.Optional("open_relay_action")] = selector.ActionSelector()
        if house_wheel or house_now:       # shown when the wheel has the bridge or it is already on
            fields[vol.Required(CONF_HOUSE_CALLS, default=house_now)] = selector.BooleanSelector()
        return self.async_show_form(step_id="reconfigure_server", data_schema=vol.Schema(fields), errors=errors)
