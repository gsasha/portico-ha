"""Logbook wording for Portico's events (DEV-811 P4). `portico_call` itself is not described: its `ended` phase already writes
one `logbook_entry` line per call (call_events.py), and describing every phase would put four rows per call in the logbook."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.core import Event, HomeAssistant, callback

from .actions import ACTION_OPEN, EVENT_ACTION
from .call_events import EVENT_CALL_STATS
from .provider import DOMAIN

_SKIP = {"call_id", "door_id"}


@callback
def async_describe_events(hass: HomeAssistant, async_describe_event: Callable[[str, str, Callable[[Event], dict[str, Any]]], None]) -> None:
    @callback
    def _describe_stats(event: Event) -> dict[str, Any]:
        numbers = ", ".join(f"{key} {value}" for key, value in event.data.items()
                            if key not in _SKIP and isinstance(value, (int, float, str)))
        return {"name": "Portico call", "message": f"audio numbers: {numbers or 'none'}"}

    async_describe_event(DOMAIN, EVENT_CALL_STATS, _describe_stats)

    @callback
    def _describe_action(event: Event) -> dict[str, Any]:
        data = event.data
        who = data.get("actor") or "someone"
        door = data.get("door_name") or data.get("door_id") or "a door"
        verb = "opened" if data.get("action") == ACTION_OPEN else str(data.get("action") or "acted on")
        return {"name": "Portico", "message": f"{who} {verb} {door}"}

    async_describe_event(DOMAIN, EVENT_ACTION, _describe_action)
