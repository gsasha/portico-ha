"""Who opened a door: the `portico_action` event (DEV-811 P5, the short version).

For a door opened from the app (`portico/open_relay`, a `portico_open` element) the app builds ONE user-less `Context`,
fires `portico_action` with it BEFORE the action, and runs the action with the SAME context, so HA's logbook groups the
action's own rows (its service call, the gate entity's state change) with the event. The actor's HA user is a field of
the event, never the context's user: no permission check changes. The event carries no result.

Best effort: a failure in here must never stop the gate, so `begin_open` swallows every exception and returns a plain
`Context()`, exactly what the action got before P5.

Privacy: names and door ids only; no tokens, audio or addresses.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import Context, HomeAssistant

_LOGGER = logging.getLogger(__name__)

EVENT_ACTION = "portico_action"
ACTION_OPEN = "open"
SOURCE_APP, SOURCE_PANEL = "app", "panel"


def source_for(hass: HomeAssistant, user_id: str | None) -> str:
    """`panel` for a wall-panel login (a dashboard endpoint's HA user), else `app`. Best effort: `app` on any failure."""
    try:
        from .endpoints import KIND_DASHBOARD, get_endpoint_registry
        if user_id and any(e.ha_user_id == user_id and e.kind == KIND_DASHBOARD
                           for e in get_endpoint_registry(hass).endpoints()):
            return SOURCE_PANEL
    except Exception:   # noqa: BLE001
        pass
    return SOURCE_APP


def begin_open(hass: HomeAssistant, *, actor: str | None, actor_user: str | None, source: str,
               door_id: str, door_name: str | None) -> Context:
    """Fire `portico_action` (action `open`) and return the context to run the door's action with."""
    try:
        ctx = Context()
        data: dict[str, Any] = {"action": ACTION_OPEN, "source": source, "door_id": door_id}
        if actor:
            data["actor"] = actor
        if actor_user:
            data["actor_user"] = actor_user
        if door_name:
            data["door_name"] = door_name
        hass.bus.async_fire(EVENT_ACTION, data, context=ctx)
        return ctx
    except Exception as err:   # noqa: BLE001 - best effort: the action then runs exactly as before P5
        _LOGGER.debug("portico: could not record the action: %s", type(err).__name__)
        return Context()
