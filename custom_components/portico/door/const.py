"""The door side's constants: the two names the pre-fold code spelled `DOMAIN`.

Before the fold the door code was its own integration, domain `portico_intercom`, and `DOMAIN` meant three different
things. They are separate now, on purpose:

* `DOMAIN` is Home Assistant's domain, `portico`: the config entries, the entity registry's `platform`, repair issues
  (and so their translations), the Script domain of the relay action, the flow handler.
* `DRIVER_ID` is the door provider's id and the door side's `hass.data` namespace, `portico_intercom`. It is a persisted
  identifier, NOT a domain any more, and must not change: the door's endpoint id is `<DRIVER_ID>:<device_id>`, which
  keys the content store's rules and callers, the phone app's `ContentProfile.callers` and the push `door_ref` hash
  (`provider.Door.door_ref`). The bus event `portico_intercom_call` and the hub socket directory
  `.storage/portico_intercom/` keep their historical names for the same reason.
"""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry

DOMAIN = "portico"
DRIVER_ID = "portico_intercom"

# `entry_type` is the key the push entry already uses (push/const.py ENTRY_TYPE); a door entry carries it too.
ENTRY_TYPE = "entry_type"
ENTRY_TYPE_DOOR = "door"


def is_door_entry(entry: ConfigEntry) -> bool:
    """A door entry: `data.entry_type == "door"`. The driver (`data.driver`) is a door's own field and says nothing
    about the entry's kind."""
    return entry.data.get(ENTRY_TYPE) == ENTRY_TYPE_DOOR

# Away call audio is ALWAYS on (the owner, 2026-11-12: "I don't see why home assistant's option could ever be off"). The door
# entry's old `away_media` option is no longer read, written or shown; an entry that still stores it (True or False) is
# ignored. The `away_media` flag a door publishes (provider_v1) is kept for old apps and panels: it now means "this door can
# carry call audio as frames" (see provider_impl), no longer an option.
