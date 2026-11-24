"""The gate as a roster endpoint and a call target (M7 3f, house calls design 14e and 15.15). INERT.

The gate is the door whose driver reports `CanDial` (a gate_sip_server entry with `dial_enabled` on and its station registered). It is already an endpoint of kind `door`, id
`<provider>:<device_id>`, name from the door; what 3f adds is that it is CALLABLE only while that holds:

- the roster entry of a dialable door carries `can_dial: true` (live state: not part of the document `rev`, as Talk's
  availability never is); with the dial off the entry is exactly what it was (unavailable, `not_available`) and carries
  no new key;
- the editor's endpoint row carries `can_dial: true` and the house picker lists the door, only then: the call-target
  picker offers the gate only while it can dial. A save is not tightened (a known door endpoint can be named, as before,
  and a stored reference is kept whatever `CanDial` says: it shows "not available", it is never deleted);
- a `call` button (or a roster key) that targets the gate is placed through Talk's path in `content.py` (`_run_talk`:
  the same gate, the same single `provider.dial`, the same `talk_state` relay to the placing phone only), never through a
  second implementation;
- `GateWatch` notices a change of the set of doors that can dial (a door arriving, leaving or being re-configured) and
  tells the clients through the callable it is given (`content.announce_gate_change`: `house_roster_changed {rev}` when
  house calls are on, whose `rev` includes the dialable doors). With nothing dialable, before and after, it says nothing.

This module imports no house module (the door path never does, `test_house_boundary.py`): the announcement is passed in.
"""
from __future__ import annotations

import logging
from collections.abc import Callable

from homeassistant.core import HomeAssistant

from .endpoints import KIND_DOOR, Endpoint, get_endpoint_registry

_LOGGER = logging.getLogger(__name__)

DATA_GATE_WATCH = "gate_watch"


def dialable_doors(hass: HomeAssistant) -> list[Endpoint]:
    """The door endpoints whose driver reports `CanDial` now (exactly True): empty on every install today. Never raises."""
    try:
        return [e for e in get_endpoint_registry(hass).endpoints(kind=KIND_DOOR) if e.can_dial is True]
    except Exception as err:     # noqa: BLE001 - a registry bug must not take a roster down; it means "none"
        _LOGGER.warning("portico gate: the dialable doors could not be read: %s", type(err).__name__)
        return []


class GateWatch:
    """Remembers which doors could dial and announces a change of that set (and nothing else)."""

    def __init__(self, hass: HomeAssistant, announce: Callable[[], None]) -> None:
        self._hass = hass
        self._announce = announce
        self._known: frozenset[str] = frozenset()
        self._unsub: Callable[[], None] | None = None

    def start(self) -> None:
        from .provider import DATA_PROVIDERS, DOMAIN

        registry = self._hass.data.get(DOMAIN, {}).get(DATA_PROVIDERS)
        if registry is None or self._unsub is not None:
            return
        self._known = frozenset(e.id for e in dialable_doors(self._hass))
        self._unsub = registry.add_doors_hook(self.check)

    def stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    def check(self) -> bool:
        """Re-read the dialable set; announce and return True when it changed."""
        now = frozenset(e.id for e in dialable_doors(self._hass))
        if now == self._known:
            return False
        self._known = now
        try:
            self._announce()
        except Exception:     # noqa: BLE001 - the announcement is best effort; the next change tries again
            _LOGGER.warning("portico gate: the change of the gate's CanDial could not be announced", exc_info=True)
        return True


def async_setup_gate_watch(hass: HomeAssistant, announce: Callable[[], None]) -> GateWatch:
    """Domain setup, after the provider registry exists. Idempotent."""
    from .provider import DOMAIN

    data = hass.data.setdefault(DOMAIN, {})
    watch = data.get(DATA_GATE_WATCH)
    if watch is None:
        watch = data[DATA_GATE_WATCH] = GateWatch(hass, announce)
        watch.start()
    return watch
