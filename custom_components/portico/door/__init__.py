"""The door side of Portico: the door transport, the call state, the door entities.

A door is one config entry of the `portico` integration with `data.entry_type == "door"` (`const.py`). The integration's
own `__init__` dispatches on the entry kind and calls the functions here for a door; `async_setup_door_side`
runs once at domain setup.

It owns the transport binary's subprocess lifecycle in `hub.py`: ONE process per HA instance (PROC-01), started by
the first door, restarted once on a crash (design §4.1), stopped after the last. Each door entry is a
`door.DoorRuntime` attached to that hub. Importing this package imports no Home Assistant platform and no wheel.
"""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .door import DoorRuntime
from .hub import HubError, get_hub
from .provider_impl import TransportProvider, get_provider, install_provider

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["binary_sensor", "camera", "lock", "sensor"]


def async_setup_door_side(hass: HomeAssistant) -> TransportProvider:
    """Domain-wide setup, before any entry: create the one door provider and the hub object (no process), and return
    the provider so the caller registers it with the provider registry (the same object every door attaches to)."""
    provider = install_provider(hass)
    get_hub(hass)
    return provider


async def async_setup_door_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a door: attach it to the hub (which spawns the one transport binary if this is the first door and
    configures this entry's driver) and start tracking its call state."""
    runtime = DoorRuntime(hass, entry, get_hub(hass))
    try:
        await runtime.async_start()
        entry.runtime_data = runtime
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except HubError as err:
        # REL-04 (wheel too old, with its repair issue) or a refusal to attach: not retried, not an exception.
        _LOGGER.error('Portico: door "%s" (%s) was not started: %s', entry.title, entry.entry_id, err)
        await runtime.async_stop()
        return False
    except Exception:
        # A failed setup is never followed by async_unload_entry, so without this the door would stay
        # configured on the hub (and keep its SIP registration, §6's session quota) after the failure.
        await runtime.async_stop()
        raise

    # (HA stopping is not an unload either: the hub's own stop hook, hub.py, sends the clean de-register
    # instead of a SIGKILL'd registration, once for every door.)
    # The door joins the provider: its call transitions reach the registry's subscribers (the push
    # sender, PUSH-02) and content resolves it by door id.
    provider = get_provider(hass)
    if provider is not None:
        provider.attach(entry)
    # A saved Configure form that changes the talk settings takes effect by reloading the entry (the Go driver attaches
    # or detaches in async_start). Any other option (away audio) is read live and needs no reload.
    if runtime.ring_only:
        entry.async_on_unload(entry.add_update_listener(_async_talk_options_updated))
    return True


async def _async_talk_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    runtime: DoorRuntime = entry.runtime_data
    if runtime.talk_settings_changed():
        await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_door_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a door entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        provider = get_provider(hass)
        if provider is not None:
            provider.detach(entry)
        runtime: DoorRuntime = entry.runtime_data
        await runtime.async_stop()
    return unload_ok
