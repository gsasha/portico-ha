"""Managed Talk elements: one per screen and one per enrolled phone (DEVICES-10, owner D-20261109-4c5c; design home
`portico_devices_design.md` 4.4).

House calls work only through Talk elements. Every screen and every enrolled phone has a Talk element flagged
`managed: true`, id `talk-screen-<endpoint id>` / `talk-phone-<endpoint id>`, `options: {screen: <id>}` / `{phone: <id>}`.
A press expands to the existing `call` action (content_elements.py), so the server's own rules (V1: the target's
`house_receive`, presence, busy, Do Not Disturb) decide whether it is on.

`async_reconcile` is the one idempotent step that keeps them: it CREATES the missing element of each current screen and
phone and nothing else. An element whose phone or screen is gone is an orphan: kept as it is, listed with
`target_removed: true` ("phone removed" / "screen removed") and never deleted automatically (the user may delete it:
`ElementStore.async_delete` refuses only while the target exists). It runs at startup, after a phone is enrolled or
revoked, and after a screen is created or deleted (the content store's save notifies the endpoint registry). Runs are
coalesced: a request while one runs asks for exactly one more run afterwards. Nothing here touches the content store's
document or schema; a failure is logged and never reaches the caller.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import CoreState, Event, HomeAssistant, callback
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED

from .content import get_content_store
from .elements import ID_RE, MANAGED_ID_RE, get_elements_store, managed_talk_id

_LOGGER = logging.getLogger(__name__)

DATA_STATE = "managed_talk"


class _State:
    def __init__(self) -> None:
        self.running = False
        self.again = False
        self.unsub: Any = None


def _state(hass: HomeAssistant) -> _State:
    domain_data = hass.data.setdefault("portico", {})
    state = domain_data.get(DATA_STATE)
    if state is None:
        state = domain_data[DATA_STATE] = _State()
    return state


def wanted_managed(hass: HomeAssistant, *, screens: bool = True) -> list[dict[str, Any]]:
    """The managed Talk elements that should exist now: `[{id, name, options}]`, phones then screens. A screen is listed only
    when `screens` (the content store has loaded: before that the registry knows no screen). An endpoint id that cannot make
    a valid element id is skipped with one warning."""
    from .endpoints import KIND_DASHBOARD, KIND_PHONE, get_endpoint_registry, is_app_screen

    registry = get_endpoint_registry(hass)
    out: list[dict[str, Any]] = []
    for kind, option, label in ((KIND_PHONE, "phone", "phone"), (KIND_DASHBOARD, "screen", "screen")):
        if kind == KIND_DASHBOARD and not screens:
            continue
        for endpoint in registry.endpoints(kind=kind):
            if kind == KIND_DASHBOARD and is_app_screen(hass, endpoint.id):
                continue            # a panel's app screen gets no managed Talk element: its phone half has one (DEV-807)
            element_id = managed_talk_id(option, endpoint.id)
            match = MANAGED_ID_RE.match(element_id)
            if not ID_RE.match(element_id) or match is None:
                _LOGGER.warning("portico managed talk: a %s endpoint id cannot be an element id; skipped", label)
                continue
            out.append({"id": element_id, "name": endpoint.name or label, "options": {option: endpoint.id}})
    return out


async def async_reconcile(hass: HomeAssistant) -> list[str]:
    """Create the missing managed Talk elements; returns the ids created (empty when nothing was missing). Never raises."""
    try:
        content = get_content_store(hass)
        try:
            await content.async_ensure_loaded()
        except Exception:  # noqa: BLE001 - phones are still reconciled; screens wait for the next run
            _LOGGER.warning("portico managed talk: the content store could not be loaded; screens wait", exc_info=True)
        wanted = wanted_managed(hass, screens=content.loaded)
        if not wanted:
            return []
        return await get_elements_store(hass).async_add_managed(wanted)
    except Exception as err:  # noqa: BLE001 - includes ElementError (a newer schema): logged, never raised
        _LOGGER.warning("portico managed talk: reconcile did not run (%s)", getattr(err, "code", type(err).__name__))
        return []


async def _run(hass: HomeAssistant) -> None:
    state = _state(hass)
    if state.running:
        state.again = True
        return
    state.running = True
    try:
        while True:
            state.again = False
            await async_reconcile(hass)
            if not state.again:
                return
    finally:
        state.running = False


@callback
def async_request_reconcile(hass: HomeAssistant) -> None:
    """Ask for a reconcile (after an enrol, a revoke, a screen change, at start). Returns at once; coalesced."""
    hass.async_create_task(_run(hass), eager_start=False)


@callback
def async_setup_managed_talk(hass: HomeAssistant) -> None:
    """Startup (once): reconcile when HA has started (now, if it already has), and after every change to the screens, which
    the content store announces through the endpoint registry. Phones ask through `async_request_reconcile` themselves."""
    from .endpoints import get_endpoint_registry

    state = _state(hass)
    if state.unsub is not None:
        return
    state.unsub = get_endpoint_registry(hass).add_listener(lambda: async_request_reconcile(hass))
    if hass.state is CoreState.running:
        async_request_reconcile(hass)
    else:
        async def _started(_event: Event) -> None:
            async_request_reconcile(hass)

        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _started)
