"""The open `portico/calls/subscribe` streams, kept as a list (UI split, house-calls follow-up).

`signaling.handle_calls_subscribe` attaches a stream to the house registry once, when the subscription opens, and lists it here
with the handle it holds, so the handle can be read back and released when the stream closes. House calls are always on
(D-20261122-aafb), so the registry no longer appears or disappears under an open stream: the re-attach function that the removed
House calls switch used (`async_sync_house`) is gone.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.core import HomeAssistant, callback

from .house import HouseHandle
from .provider import DOMAIN

DATA_CALL_SUBS = "calls_subs"


@dataclass(eq=False)
class CallSub:
    """One open `portico/calls/subscribe`: its connection, its command id, and the handle it holds on the house registry."""

    connection: Any
    msg_id: int
    house_handle: HouseHandle | None = None


def _subs(hass: HomeAssistant) -> list[CallSub]:
    return hass.data.setdefault(DOMAIN, {}).setdefault(DATA_CALL_SUBS, [])


@callback
def track(hass: HomeAssistant, connection: Any, msg_id: int, house_handle: HouseHandle | None) -> CallSub:
    sub = CallSub(connection, msg_id, house_handle)
    _subs(hass).append(sub)
    return sub


@callback
def untrack(hass: HomeAssistant, sub: CallSub) -> None:
    subs = _subs(hass)
    if sub in subs:
        subs.remove(sub)
