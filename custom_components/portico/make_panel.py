"""DEV-807 panel mode: "make a panel" (own-pages design section 4/5; it replaces "make a panel screen").

A panel enrols as a phone and OWNS its pages, Ring pages and roster hiding in `devices[<phone endpoint id>]`, exactly like a
phone. Making a phone a panel therefore writes ONE option on the phone entry, `panel: true`, clears the phone's door list (every
door answers; `wake` is computed from the door Talk elements on its pages) and creates the readiness sensor. No screen is
declared, and `panel_screen` is never written (a legacy option of that name on an old entry is ignored).

The Devices tab's "Make a panel" action (portico-phones.js) is the one caller. A house with no panel behaves exactly as before:
nothing here runs until an administrator presses it.
"""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from . import phone_entry
from .phone_entry import CONF_DOORS, CONF_PANEL

_LOGGER = logging.getLogger(__name__)


class PanelRefused(Exception):
    """The action was refused; nothing was changed. `code` is the wire code, `field` the form field it names."""

    def __init__(self, code: str, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.field = field


async def async_make_panel(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Make the phone entry `entry` a panel. Raises PanelRefused (nothing changed). The caller holds the enrolment lock."""
    user_id = entry.data.get("ha_user_id")
    if not phone_entry.is_phone_entry(entry):
        raise PanelRefused("legacy_phone", "A phone enrolled before phone entries cannot be a panel")
    if entry.options.get(CONF_PANEL) is True:
        raise PanelRefused("already_panel", "This phone is already a panel")
    user = await hass.auth.async_get_user(user_id) if isinstance(user_id, str) else None
    if user is None or not phone_entry._made_by_portico(user):
        # a screen-only login loses `portico/push/*` and `media_*`, so only a user Portico made (a phone of its own) may be one
        raise PanelRefused("not_portico_user", "Only a phone whose Home Assistant user Portico made can be a panel")
    after = {k: v for k, v in entry.options.items() if k != CONF_DOORS}        # every door answers: `wake` is the one door list
    after[CONF_PANEL] = True
    hass.config_entries.async_update_entry(entry, options=after)
    from .entities.sensor import async_ensure_panel_sensor

    async_ensure_panel_sensor(hass, entry)             # the readiness sensor exists from the moment the phone is a panel
