"""The non-secret options of the door and phone config entries as YAML (DEV-824 P6, design `portico_config_yaml_design.md`
section 4 `doors/` and `phones/`, section 8, section 12 P6).

WHAT IS COVERED (and nothing else):

* a `gate_sip_server` door: `dial_enabled` (entry data, a transport key: changing it RELOADS the entry), `ring_answered_entity`
  (entry data, an `input_boolean` helper), `house_bridge` (the door entry's option the hub reads once, at its next start; no reload).
  `driver` is read-only and must match the live entry. Any other driver's file carries `driver` and `name` only.
* a phone entry: `doors` (which doors it may open, `null` = every door) and `house_receive` (options). `panel` is shown and
  READ-ONLY: making a phone a panel does more than set the flag (`make_panel.py`), so it stays the Devices tab's action.

WHAT IS NEVER HERE: `open_relay_action` (what opens a door), every password, key and token, a door's host or ports, a phone's
identity and token state, entry creation or deletion. A file for a door or phone that does not exist is refused; a door or phone
with no file is left alone. A key a file leaves out leaves that value alone (`null` is a value, section 4 of `config_yaml.py`).

DEFERRAL. These options change who can open a door and who is rung, and a reload drops a door's SIP registration, so none of them is
applied while a call is live (`binary_sensor.portico_call_active`'s own source, `call_events`). The whole reload is then DEFERRED, not
split: nothing is applied, the reply says `deferred: true`, and the tree stays a pending edit until `portico.reload_config` is called
again when the house is idle. (Nothing retries by itself: an unattended later apply is not what a merge approved.)

NO HA RESTART: options are written with `async_update_entry`; only a `dial_enabled` change reloads that one entry, after the
content and elements are applied and outside the elements lock. A failed reload is a warning (the saved value is what the entry holds).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from . import config_yaml
from .call_events import IDLE, get_call_events
from .config_yaml import SERVER_DRIVER, ConfigError
from .door import gate_sip_listen
from .door.const import DOMAIN, is_door_entry
from .door.drivers import device_id_for
from .door.hub import CONF_HOUSE_CALLS, read_installed_wheel_version, wheel_has_house_calls
from .phone_entry import CONF_DOORS, CONF_HOUSE_RECEIVE, CONF_PANEL, PhoneRecord, is_phone_entry
from .provider import DATA_PROVIDERS

_LOGGER = logging.getLogger(__name__)

# YAML keys (the option of the hub's flag is named for what it is, not by its stored key)
K_DIAL, K_RING_ENTITY, K_BRIDGE = "dial_enabled", "ring_answered_entity", "house_bridge"


# ---- reading the live entries (the export) ----------------------------------------------------------------------------------

def door_key(entry: ConfigEntry) -> str:
    """The key a door's file is named by: the device id the door is known by on both sides of IPC (`device_id_for`)."""
    return str(device_id_for(entry.data, entry.entry_id))


def phone_key(entry: ConfigEntry) -> str | None:
    try:
        return PhoneRecord.from_data(dict(entry.data)).key
    except ValueError:
        return None


def door_entries(hass: HomeAssistant) -> dict[str, ConfigEntry]:
    return {door_key(e): e for e in hass.config_entries.async_entries(DOMAIN) if is_door_entry(e)}


def phone_entries(hass: HomeAssistant) -> dict[str, ConfigEntry]:
    out: dict[str, ConfigEntry] = {}
    for entry in hass.config_entries.async_entries(DOMAIN):
        if is_phone_entry(entry) and (key := phone_key(entry)) is not None:
            out.setdefault(key, entry)
    return out


def door_record(entry: ConfigEntry) -> dict[str, Any]:
    record: dict[str, Any] = {"name": (entry.title or door_key(entry))[:120], "driver": str(entry.data.get("driver") or "unknown")}
    if entry.data.get("driver") == SERVER_DRIVER:
        record[K_DIAL] = entry.data.get("dial_enabled") is True
        record[K_RING_ENTITY] = gate_sip_listen.settings(entry.data)["ring_answered_entity"]
        record[K_BRIDGE] = entry.options.get(CONF_HOUSE_CALLS) is True
    return record


def phone_record(entry: ConfigEntry) -> dict[str, Any]:
    doors = entry.options.get(CONF_DOORS)
    panel = entry.options.get(CONF_PANEL) is True
    return {"name": str(entry.data.get("name") or "phone")[:120],
            CONF_DOORS: list(doors) if isinstance(doors, list) and not panel else None,      # a panel answers every door
            CONF_HOUSE_RECEIVE: entry.options.get(CONF_HOUSE_RECEIVE) is True, CONF_PANEL: panel}


def door_records(hass: HomeAssistant) -> dict[str, dict[str, Any]]:
    return {key: door_record(entry) for key, entry in sorted(door_entries(hass).items())}


def phone_records(hass: HomeAssistant) -> dict[str, dict[str, Any]]:
    return {key: phone_record(entry) for key, entry in sorted(phone_entries(hass).items())}


# ---- the call-idle rule -----------------------------------------------------------------------------------------------------

def calls_idle(hass: HomeAssistant) -> bool:
    """True when no call is live (the source of `binary_sensor.portico_call_active`). With no call tracker (no settings entry) there is
    nothing that could say a call is live, so the house reads as idle."""
    events = get_call_events(hass)
    return events is None or events.state == IDLE


# ---- the check ---------------------------------------------------------------------------------------------------------------

@dataclass
class EntryChange:
    kind: str                       # "door" | "phone"
    key: str
    entry: ConfigEntry
    changes: dict[str, tuple[Any, Any]]          # option -> (before, after)
    reload: bool = False


@dataclass
class Plan:
    changes: list[EntryChange] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.changes)

    def report(self) -> dict[str, list[dict[str, Any]]]:
        def rows(kind: str) -> list[dict[str, Any]]:
            return [{"id": c.key, "name": c.entry.title or c.key, "changes": {k: {"before": b, "after": a} for k, (b, a) in c.changes.items()},
                     "reloads_entry": c.reload} for c in self.changes if c.kind == kind]

        return {"doors": rows("door"), "phones": rows("phone")}

    def sensitive(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for change in self.changes:
            for option, (before, after) in change.changes.items():
                if change.kind == "door":
                    detail = {K_DIAL: "the door may place outbound calls" if after else "the door may no longer place outbound calls",
                              K_RING_ENTITY: "the helper turned on when Portico answers changes",
                              K_BRIDGE: "the house-call bridge is switched " + ("on" if after else "off") + " for the hub's next start"}[option]
                    out.append({"kind": "door_option", "id": change.key, "option": option, "detail": detail})
                elif option == CONF_DOORS:
                    out.append({"kind": "phone_doors", "id": change.key, "detail": "which doors this phone may open changes",
                                "before": "every door" if before is None else before, "after": "every door" if after is None else after})
                else:
                    out.append({"kind": "phone_receive", "id": change.key,
                                "detail": "this phone " + ("receives" if after else "no longer receives") + " calls between the house's devices"})
        return out


def _entity_exists(hass: HomeAssistant, entity_id: str) -> bool:
    return hass.states.get(entity_id) is not None or er.async_get(hass).async_get(entity_id) is not None


async def check(hass: HomeAssistant, result: config_yaml.Validation, errors: list[ConfigError]) -> Plan:
    """Compare the validated door and phone records with the live entries. Appends `file:line:col` errors; returns what would change.
    Writes nothing."""
    plan = Plan()
    doors_by_key, phones_by_key = door_entries(hass), phone_entries(hass)
    lines = result.loaded.lines
    wheel_ok: bool | None = None

    def refuse(path: str, field_name: str | None, message: str, code: str) -> None:
        line, col = lines.get(path, {}).get(field_name, (1, 1)) if field_name else (1, 1)
        errors.append(ConfigError(path, line, col, message, code))

    for key, record in sorted((result.doors or {}).items()):
        path = config_yaml.file_name("doors", key)
        entry = doors_by_key.get(key)
        if entry is None:
            refuse(path, "id", f"there is no door {key!r}; the files cannot add or remove a door", "unknown_door")
            continue
        live = door_record(entry)
        if record["driver"] != live["driver"]:
            refuse(path, "driver", f"the driver is {live['driver']!r} and cannot be changed here", "driver_readonly")
            continue
        changes: dict[str, tuple[Any, Any]] = {}
        bad = False
        for option in config_yaml.DOOR_EDITABLE:
            if option in record and record[option] != live.get(option):
                changes[option] = (live.get(option), record[option])
        if K_RING_ENTITY in changes and changes[K_RING_ENTITY][1] and not _entity_exists(hass, changes[K_RING_ENTITY][1]):
            refuse(path, K_RING_ENTITY, f"{changes[K_RING_ENTITY][1]}: there is no such entity in Home Assistant", "unknown_entity")
            bad = True
        if K_BRIDGE in changes and changes[K_BRIDGE][1]:
            if wheel_ok is None:
                wheel_ok = wheel_has_house_calls(await hass.async_add_executor_job(read_installed_wheel_version))
            if not wheel_ok:
                refuse(path, K_BRIDGE, "the installed transport is too old for the house-call bridge", "house_needs_wheel")
                bad = True
        if changes and not bad:
            plan.changes.append(EntryChange("door", key, entry, changes, reload=K_DIAL in changes))

    registry = hass.data.get(DOMAIN, {}).get(DATA_PROVIDERS)
    live_doors = {d.id for d in registry.doors()} if registry is not None else set()
    for key, record in sorted((result.phones or {}).items()):
        path = config_yaml.file_name("phones", key)
        entry = phones_by_key.get(key)
        if entry is None:
            refuse(path, "id", f"there is no phone {key!r}; the files cannot enrol or revoke a phone", "unknown_phone")
            continue
        live = phone_record(entry)
        if CONF_PANEL in record and record[CONF_PANEL] != live[CONF_PANEL]:
            refuse(path, CONF_PANEL, "whether a phone is a panel cannot be changed here; use the Devices tab", "panel_readonly")
            continue
        if live[CONF_PANEL] and record.get(CONF_DOORS) is not None:
            refuse(path, CONF_DOORS, "a panel answers every door: doors must be null", "invalid")
            continue
        changes = {}
        for option in (CONF_DOORS, CONF_HOUSE_RECEIVE):
            if option in record and record[option] != live[option]:
                changes[option] = (live[option], record[option])
        bad = False
        if CONF_DOORS in changes and changes[CONF_DOORS][1] is not None:
            tolerated = set(live[CONF_DOORS] or [])          # a stored door that is offline now stays, as the phone's own form keeps it
            for door in changes[CONF_DOORS][1]:
                if door not in live_doors and door not in tolerated:
                    refuse(path, CONF_DOORS, f"{door!r} is not a door Portico has", "unknown_door")
                    bad = True
        if changes and not bad:
            plan.changes.append(EntryChange("phone", key, entry, changes))
    return plan


# ---- the apply ---------------------------------------------------------------------------------------------------------------

def apply(hass: HomeAssistant, plan: Plan) -> tuple[list[Callable[[], Awaitable[None]]], Callable[[], None]]:
    """Write every change (synchronous, all before anything awaits). Returns the entry reloads to run afterwards and an `undo` that puts
    every entry back as it was (for a failed write further on in the same apply). Mirrors what each entry's own form writes: a door's
    Reconfigure (`gate_sip_listen`) and a phone's `async_set_house_receive` / options flow."""
    from .content import get_content_store
    from .phone_entry import _house_reconcile

    reloads: list[Callable[[], Awaitable[None]]] = []
    phones_changed = False
    before = [(c.entry, dict(c.entry.data), dict(c.entry.options)) for c in plan.changes]

    def undo() -> None:
        for entry, data, options in before:
            hass.config_entries.async_update_entry(entry, data=data, options=options)
        _house_reconcile(hass)

    for change in plan.changes:
        entry, new = change.entry, {k: after for k, (_before, after) in change.changes.items()}
        if change.kind == "door":
            data = dict(entry.data)
            if K_DIAL in new:
                data.pop("dial_enabled", None)
                if new[K_DIAL]:
                    data["dial_enabled"] = True
            if K_RING_ENTITY in new:
                data.pop(gate_sip_listen.KEY, None)
                if new[K_RING_ENTITY]:
                    data[gate_sip_listen.KEY] = {"ring_answered_entity": new[K_RING_ENTITY]}
            options = gate_sip_listen.house_options(entry.options, new[K_BRIDGE]) if K_BRIDGE in new else dict(entry.options)
            hass.config_entries.async_update_entry(entry, data=data, options=options)
            if change.reload:
                reloads.append(_reloader(hass, entry))
        else:
            options = dict(entry.options)
            if CONF_DOORS in new:
                if new[CONF_DOORS] is None:
                    options.pop(CONF_DOORS, None)
                else:
                    options[CONF_DOORS] = list(new[CONF_DOORS])
                phones_changed = True
            if CONF_HOUSE_RECEIVE in new:
                options[CONF_HOUSE_RECEIVE] = bool(new[CONF_HOUSE_RECEIVE])
            hass.config_entries.async_update_entry(entry, options=options)
            if CONF_HOUSE_RECEIVE in new:
                _house_reconcile(hass)
    if phones_changed:
        get_content_store(hass).announce_all()          # which doors a phone may open is part of what its app fetches
    return reloads, undo


def _reloader(hass: HomeAssistant, entry: ConfigEntry) -> Callable[[], Awaitable[None]]:
    async def run() -> None:
        await hass.config_entries.async_reload(entry.entry_id)

    return run
