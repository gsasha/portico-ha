"""Portico config flow: ONE flow for the three entry kinds, and the options flows.

"Add integration > Portico" shows a menu: **Add a door** (the driver picker and the door's steps, `door/flow.py`) or
**Push ring notification (Firebase)** (the one push entry, `push_flow.py`). A phone entry is created in code only (`phone` source).
Each kind has its own options flow, chosen by `entry_type`.

Phone/app enrollment (enrollment_flow.py): generates the single QR code
carrying both the per-phone HA user's long-lived access token and the
Tailscale pre-authorized key (§4.7, §4.8). It runs from the push entry's Configure menu (the install-wide
settings host, S-1). An old door entry (`data.driver`, from before the split) has an options flow that only
points at Portico Intercom (`door_entry_moved`).
"""
from __future__ import annotations

from typing import Any

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback

from .door.const import ENTRY_TYPE_DOOR
from .door.flow import DoorConfigFlowMixin, DoorOptionsFlow
from .door.gate_sip_listen import GateSipListenFlowMixin
from .phone_entry import ENTRY_TYPE_PHONE, UNIQUE_ID_PREFIX, PhoneRecord
from .phone_flow import PhoneOptionsFlow
from .push.const import ENTRY_TYPE, ENTRY_TYPE_PUSH
from .push_flow import PushConfigFlowMixin, PushOptionsFlow

DOMAIN = "portico"


class PorticoConfigFlow(DoorConfigFlowMixin, GateSipListenFlowMixin, PushConfigFlowMixin,
                        config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a Portico config flow: a door (door/flow.py), the one "Push ring notification (Firebase)" push entry
    (push_flow.py), or a phone (the `phone` source). A gate_sip_server door's Reconfigure is its settings form
    (door/gate_sip_listen.py); every other entry's Reconfigure aborts `reconfigure_not_supported`."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        """The first menu: add a door, set up the push ring notification, or (advanced) add a SIP-server door (in-process, or a remote container)."""
        return self.async_show_menu(step_id="user", menu_options=["door", "push_credentials", "sip_server_door", "sip_server_remote"])

    async def async_step_phone(self, data: dict[str, Any] | None = None):
        """The `phone` source (P-4, SPK-6): one entry per enrolled phone. Started in code only, by the enrollment
        options flow and by the adoption of a pre-P-4 row (`phone_entry.async_create_phone_entry`), never from the
        UI: the UI starts a flow with source `user`. `data` is a `PhoneRecord`'s data; the unique id is the phone's
        key, so the same phone is never created twice."""
        try:
            record = PhoneRecord.from_data(data or {})
        except ValueError:
            return self.async_abort(reason="invalid_phone")
        await self.async_set_unique_id(f"{UNIQUE_ID_PREFIX}{record.key}")
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=record.name, data=record.to_data())

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> config_entries.OptionsFlow:
        """The Configure button, chosen by `entry_type`: the push entry's (push_flow.py; it hosts enrollment since
        S-1; what a phone shows is edited in the Portico panel), a phone's, a door's (Away call audio). An old-format
        door entry (`data.driver` with no `entry_type`, from before the split) only points at "delete it".
        This must stay a @staticmethod taking `config_entry` (OptionsFlowManager.async_create_flow calls
        `handler.async_get_options_flow(entry)` on the flow *class*, not an instance)."""
        kind = config_entry.data.get(ENTRY_TYPE)
        if kind == ENTRY_TYPE_PUSH:
            return PushOptionsFlow()
        if kind == ENTRY_TYPE_PHONE:
            return PhoneOptionsFlow()
        if kind == ENTRY_TYPE_DOOR:
            return DoorOptionsFlow()
        return DoorEntryMovedOptionsFlow()


class DoorEntryMovedOptionsFlow(config_entries.OptionsFlow):
    """An old-format door entry (`data.driver` with no `entry_type`, from before the split) is refused at setup and
    waits to be deleted: its Configure aborts with `door_entry_moved`, and writes nothing."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        return self.async_abort(reason="door_entry_moved")
