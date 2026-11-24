"""Run the HA action the installer chose to open a door (`open_relay_action`, set in the config flow).

One place, two callers: the relay lock entity (entities/lock.py) and the provider's `open_relay`
(provider_impl.py), so a door whose driver has no relay of its own (gate_sip_server: `HasRelay` false)
opens through the same code as one that has a lock entity.
"""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers.script import Script, async_validate_actions_config

from .const import DOMAIN


def has_relay_action(entry: ConfigEntry) -> bool:
    """True when the installer configured a non-empty `open_relay_action` for this door."""
    return bool(entry.data.get("open_relay_action"))


async def run_relay_action(hass: HomeAssistant, entry: ConfigEntry, context: Context | None = None) -> None:
    """Run the entry's configured `open_relay_action` sequence. Raises whatever the action raises.

    `context` (P5) is the app's user-less context, shared with its `portico_action` event; None runs with a fresh `Context()`.

    The stored value is a raw form submission from the config flow's ActionSelector: Script requires an
    already-validated/normalized sequence (real HA internals reject a raw dict: async_prepare_call_from_config
    expects keys config validation would have filled in, e.g. `service_template`, and KeyErrors without it).
    """
    validated = await async_validate_actions_config(hass, entry.data["open_relay_action"])
    script = Script(hass, validated, f"portico_open_relay_{entry.entry_id}", DOMAIN)
    await script.async_run(context=context if context is not None else Context())
