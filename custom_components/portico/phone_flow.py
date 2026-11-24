"""A phone entry's Configure (P-4): which doors this phone is enrolled for (R4-04).

Everything else about a phone is read-only here (its identity is the entry's data) or lives on the settings entry
(its content: the Portico panel in the sidebar). Removing the phone is deleting the entry.
"""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers import config_validation as cv

from .phone_entry import CONF_DOORS
from .provider import DATA_PROVIDERS

CONF_ALL_DOORS = "all_doors"


class PhoneOptionsFlow(config_entries.OptionsFlow):
    """One step: every door (the default, and what a phone enrolled before P-4 has), or a chosen list. A door
    that is not in the list gets this phone none of its secrets, and no call audio."""

    def _doors(self) -> dict[str, str]:
        registry = self.hass.data.get("portico", {}).get(DATA_PROVIDERS)
        return {door.id: door.name or door.id for door in (registry.doors() if registry is not None else [])}

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        return await self.async_step_phone_doors()

    async def async_step_phone_doors(self, user_input: dict[str, Any] | None = None):
        options = self.config_entry.options
        stored = options.get(CONF_DOORS)
        if user_input is not None:
            chosen = None if user_input.get(CONF_ALL_DOORS, True) else list(dict.fromkeys(user_input.get(CONF_DOORS, [])))
            return self.async_create_entry(title="", data={**options, CONF_DOORS: chosen})
        # A stored door that is not listed now (offline, removed) stays selectable, so saving does not drop it.
        labels = {**{d: d for d in (stored or [])}, **self._doors()}
        schema = vol.Schema({
            vol.Required(CONF_ALL_DOORS, default=stored is None): bool,
            vol.Optional(CONF_DOORS, default=list(stored or [])): cv.multi_select(labels),
        })
        return self.async_show_form(step_id="phone_doors", data_schema=schema)
