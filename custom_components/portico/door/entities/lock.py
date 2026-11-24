"""One lock entity per device with a relay — the door/gate strike (§9.2).

`lock`, not `button` — matches how the estate's own live system already
models the gate strike (`lock.open`), so a Portico-managed relay reads the
same way in automations and dashboards as the gate lock does today.

Semantics: is_locked is always True (momentary strike, no real lock/unlock
state); async_unlock() == async_open(). LockEntityFeature.OPEN advertised,
no LOCK/UNLOCK feature.

What actually happens on open (§ config_flow.py's relay_action step docs
the "why"): if the installer configured an `open_relay_action` at setup,
that HA action runs directly — this covers the real case this estate's
own gate is: the SIP device's own onboard relay was deliberately never
wired to the gate motor (a separate Shelly relay is), so "open this
device's relay" has to mean "run whatever HA action actually opens the
gate", not "ask the SIP device to do it". If no action was configured,
this falls back to the driver's own OpenRelay via the transport binary's
open_relay request (§9.4, §9.5) — for a device whose own relay genuinely
does the job. A direct-path call's relay open still goes through this
entity either way, even though its audio doesn't — auditable the same
way any other automation-driven open is.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.lock import LockEntity, LockEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from ..relay_action import run_relay_action

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Add this device's relay lock — only if the driver has a relay.

    Gated on Capabilities.HasRelay from the transport's `configure` response
    (§9.4; Go is the single source of truth, §4.2), never on the driver's
    name. gate_sip_server reports HasRelay false (§4.2a) and gets no lock;
    dahua_sip and unifi_protect report true and keep theirs, with
    open_relay_action semantics unchanged. Missing capabilities count as no
    relay: a lock whose `open` can run an arbitrary HA action must not
    appear by default.
    """
    capabilities = entry.runtime_data.capabilities or {}
    if capabilities.get("HasRelay") is not True:
        _LOGGER.debug(
            "Portico device %s: driver reports no relay (HasRelay=%r); no lock entity",
            entry.entry_id, capabilities.get("HasRelay"),
        )
        return
    async_add_entities([PorticoRelayLock(hass, entry)])


class PorticoRelayLock(LockEntity):
    """A momentary door/gate strike, modeled as a lock (§9.2)."""

    _attr_supported_features = LockEntityFeature.OPEN
    _attr_is_locked = True

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_relay"
        self._attr_name = "Relay"
        self._attr_has_entity_name = True

    async def async_open(self, **kwargs: Any) -> None:
        action = self._entry.data.get("open_relay_action")
        if action:
            await run_relay_action(self.hass, self._entry)
            return
        raise HomeAssistantError(
            "This device has no configured relay action (Settings > Devices > "
            "this device > Reconfigure to add one), and the transport binary's "
            "own OpenRelay isn't wired up yet (Phase 1 follow-up)."
        )

    async def async_unlock(self, **kwargs: Any) -> None:
        await self.async_open(**kwargs)
