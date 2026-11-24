"""The endpoint registry: who a call can name (requirements ENDPOINT-01..03, plan 2.6).

An *endpoint* is anything a call button, a page record or (later) the auth table can name by a stable id:

- a **phone**: its `enroll_id` (ENDPOINT-02). A row enrolled before enroll ids existed has none; its HA
  user id stands in, the same rule the provider protocol uses for `phone_id`.
- a **door**: `<provider domain>:<device_id>` (`provider.Door.endpoint_id`), read from the provider
  registry, so it appears and goes with its provider and survives a door entry re-add: the id is a
  function of the stable `device_id`, never of an HA entry id (PROV-03). Split an id on its FIRST colon
  only: a `device_id` may be a URL (the gate's is `gate-sip-server:<host>:<port>`).
- a **dashboard**: declared in the editor (M6, content store v3 `dashboards`): id = its slug, `ha_user_id` the HA
  user the screen is logged in as, `cameras` what a callee sees when it calls. Listed once the content store has
  loaded; a phone's own camera declaration (`endpoints.<id>.cameras` in the store) fills its `cameras`.

This registry is READ-ONLY and derived: it holds no state of its own, so a door that goes and comes back
resolves again with the same id (ENDPOINT-02's acceptance). It does not place calls. Placement stays on
the seam `call_endpoints.CallEndpointRegistry` that DEV-610 and gate-sip G2 register into (ENDPOINT-03);
until one does, the default `Null` registry answers `not_available` for every kind.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any, Callable

from homeassistant.core import HomeAssistant

from .call_endpoints import REASON_OFFLINE
from .provider import DATA_PROVIDERS, DOMAIN, CallTransition, Unsubscribe

_LOGGER = logging.getLogger(__name__)

DATA_ENDPOINTS = "endpoints"      # hass.data[DOMAIN][DATA_ENDPOINTS] -> EndpointRegistry

KIND_PHONE = "phone"
KIND_DASHBOARD = "dashboard"
KIND_DOOR = "door"
KINDS = (KIND_PHONE, KIND_DASHBOARD, KIND_DOOR)   # the same words as call_endpoints.TARGET_TYPES


@dataclass(frozen=True, slots=True)
class Endpoint:
    id: str                       # phone: enroll_id; dashboard: slug; door: Door.endpoint_id
    kind: str                     # "phone" | "dashboard" | "door"
    provider: str | None          # door: the provider domain, kept apart from the string form
    device_id: str | None         # door: the bare device_id
    name: str
    ha_user_id: str | None        # phone, dashboard
    cameras: tuple[str, ...]      # UI-03: what a callee sees when this endpoint calls; a door's default is its camera
    room: str | None              # dashboard
    override: str | None          # the content override it uses (2.7)
    available: bool
    reason: str | None            # call_endpoints.REASON_*; None when available
    can_dial: bool = False        # door: the driver reports `CanDial` (exactly True); false for everything else (3f)

    def names_door(self, ref: Any) -> bool:
        """Whether a door reference (a block's `door`, a `portico_open` `device_id`) names THIS door endpoint. It is
        the rule of `ProviderRegistry.door`: a reference is namespaced iff the text before its FIRST colon is the
        provider's domain (then the rest is the device_id), otherwise it is a bare device_id, whole (so a URL such
        as `http://10.6.0.7:8091` is bare). `a:b` therefore never names the door `b` of another provider. A device_id
        that itself starts with its own provider's domain and a colon still matches whole (the old, exact compare),
        although the registry could not resolve such a reference."""
        if self.kind != KIND_DOOR or not self.provider or not self.device_id or not isinstance(ref, str):
            return False
        prefix, sep, rest = ref.partition(":")
        return ref == self.device_id or (bool(sep) and prefix == self.provider and rest == self.device_id)


def _phone_endpoint(phone: dict) -> Endpoint | None:
    ha_user_id = phone.get("ha_user_id")
    endpoint_id = phone.get("enroll_id") or ha_user_id
    if not isinstance(endpoint_id, str) or not endpoint_id:
        return None
    return Endpoint(
        id=endpoint_id, kind=KIND_PHONE, provider=None, device_id=None,
        name=str(phone.get("name") or "phone"),
        ha_user_id=ha_user_id if isinstance(ha_user_id, str) else None,
        cameras=(), room=None, override=None, available=True, reason=None,
    )


class EndpointRegistry:
    """hass.data["portico"]["endpoints"]: phones from the enrolled-phone rows (phone entries arrive with
    PH-13b), doors from the provider registry, dashboards from the content store v2 (M6)."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._listeners: list[Callable[[], None]] = []
        self._unsub_providers: Unsubscribe | None = None
        self._unsub_arrivals: Unsubscribe | None = None

    # ---- reads -----------------------------------------------------------------------------------

    def _phones(self) -> list[Endpoint]:
        from .push.registration import _phones      # every enrolled row on every device entry

        out: dict[str, Endpoint] = {}
        for _entry, phone in _phones(self.hass):
            endpoint = _phone_endpoint(phone)
            if endpoint is not None:
                out.setdefault(endpoint.id, endpoint)   # one phone on two entries is one endpoint
        store = self._content()
        if store is not None:
            for endpoint_id, declaration in store.endpoint_declarations.items():
                if endpoint_id in out:
                    out[endpoint_id] = replace(out[endpoint_id], cameras=tuple(declaration.get("cameras", ())))
        return list(out.values())

    def _content(self):
        """The content store, when it has loaded: the source of dashboards and phone camera declarations."""
        store = self.hass.data.get(DOMAIN, {}).get("content")
        return store if store is not None and store.loaded else None

    def _dashboards(self) -> list[Endpoint]:
        store = self._content()
        if store is None:
            return []
        out = []
        for slug, d in store.dashboards.items():
            out.append(Endpoint(
                id=slug, kind=KIND_DASHBOARD, provider=None, device_id=None, name=d.get("name") or slug,
                ha_user_id=d.get("ha_user_id"), cameras=tuple(d.get("cameras", ())), room=d.get("room"),
                override=d.get("override"), available=True, reason=None))
        return out

    def _doors(self) -> list[Endpoint]:
        providers = self.hass.data.get(DOMAIN, {}).get(DATA_PROVIDERS)
        if providers is None:
            return []
        out = []
        for door in providers.doors():
            out.append(Endpoint(
                id=door.endpoint_id, kind=KIND_DOOR, provider=door.provider, device_id=door.id,
                name=door.name, ha_user_id=None,
                cameras=(door.camera_entity_id,) if door.camera_entity_id else (),
                room=None, override=None, available=door.available,
                reason=None if door.available else REASON_OFFLINE,
                can_dial=door.can_dial,
            ))
        return out

    def endpoints(self, *, kind: str | None = None) -> list[Endpoint]:
        """Every endpoint, phones then dashboards then doors; `kind` narrows it."""
        out: list[Endpoint] = []
        if kind in (None, KIND_PHONE):
            out.extend(self._phones())
        if kind in (None, KIND_DASHBOARD):
            out.extend(self._dashboards())
        if kind in (None, KIND_DOOR):
            out.extend(self._doors())
        return out

    def get(self, endpoint_id: str, *, kind: str | None = None) -> Endpoint | None:
        """The endpoint with this id, or None. A phone id (an opaque token) and a door id
        (`<provider>:<device_id>`) cannot collide in practice; `kind` makes the lookup exact when a caller
        knows it (a call button names both)."""
        for endpoint in self.endpoints(kind=kind):
            if endpoint.id == endpoint_id:
                return endpoint
        return None

    def for_user(self, ha_user_id: str) -> Endpoint | None:
        """The endpoint an HA user is: the phone enrolled for it, else the first dashboard declared for it (the
        identity resolver of the auth table)."""
        if not ha_user_id:
            return None
        for endpoint in self.endpoints():
            if endpoint.ha_user_id == ha_user_id:
                return endpoint
        return None

    def dashboards_for_user(self, ha_user_id: str | None) -> list[Endpoint]:
        """Every dashboard declared for this HA user (the kitchen tablet's user may serve more than one)."""
        if not ha_user_id:
            return []
        return [e for e in self._dashboards() if e.ha_user_id == ha_user_id]

    # ---- change notification ---------------------------------------------------------------------

    def add_listener(self, cb: Callable[[], None]) -> Unsubscribe:
        """`cb()` runs on the event loop when the endpoint list may have changed; re-read `endpoints()`.
        Fed by the provider registry's `unavailable` snapshots (a door or a whole provider leaving), by its
        arrivals (a door added, or a provider registering later) and by `notify()`. A raising listener is logged and skipped."""
        self._listeners.append(cb)
        self._subscribe_providers()

        def _unsub() -> None:
            if cb in self._listeners:
                self._listeners.remove(cb)
            if not self._listeners:
                self._release_providers()

        return _unsub

    def notify(self) -> None:
        for cb in list(self._listeners):
            try:
                cb()
            except Exception:
                _LOGGER.exception("a Portico endpoint listener raised; skipped")

    def _release_providers(self) -> None:
        for attr in ("_unsub_providers", "_unsub_arrivals"):
            unsub = getattr(self, attr)
            if unsub is not None:
                unsub()
                setattr(self, attr, None)

    def _subscribe_providers(self) -> None:
        if self._unsub_providers is not None:
            return
        providers = self.hass.data.get(DOMAIN, {}).get(DATA_PROVIDERS)
        if providers is not None:
            self._unsub_providers = providers.subscribe(self._on_transition)
            self._unsub_arrivals = providers.subscribe_arrivals(lambda _door: self.notify())

    def _on_transition(self, transition: CallTransition) -> None:
        if transition.state == "unavailable":      # the registry's snapshot for a door that left (PROV-05)
            self.notify()


def get_endpoint_registry(hass: HomeAssistant) -> EndpointRegistry:
    data = hass.data.setdefault(DOMAIN, {})
    registry = data.get(DATA_ENDPOINTS)
    if registry is None:
        registry = data[DATA_ENDPOINTS] = EndpointRegistry(hass)
    return registry


# ---- the panel (DEV-807; own pages: `portico_panel_own_pages_design.md`) -------------------------------------------
#
# A panel enrols as a phone and owns its pages, Ring pages and roster hiding in `devices[<phone endpoint id>]`, exactly like a
# phone. The flag is ONE option on the phone entry: `panel: true` (`phone_entry.CONF_PANEL`). A legacy `panel_screen` option (the screen a
# panel used before it owned its pages) is ignored: nothing reads or writes it. Everything below reads the flag, so there is one predicate and no flag on
# `Endpoint`, in the content document or in `EndpointRegistry.endpoints()`/`get()`.


def is_panel_user(hass: HomeAssistant, user_id: Any) -> bool:
    """Whether the phone entry of this HA user has `panel: true`."""
    from .phone_entry import CONF_PANEL, phone_entry_for_user

    entry = phone_entry_for_user(hass, user_id) if isinstance(user_id, str) and user_id else None
    return entry is not None and entry.options.get(CONF_PANEL) is True


def phone_of_user(hass: HomeAssistant, user_id: Any) -> Endpoint | None:
    """The phone `Endpoint` of this HA user (the one identity of a panel in house calls, `wake`, Talk and the document), or None."""
    if not isinstance(user_id, str) or not user_id:
        return None
    phone = get_endpoint_registry(hass).for_user(user_id)
    return phone if phone is not None and phone.kind == KIND_PHONE else None


def panel_users(hass: HomeAssistant) -> dict[str, str]:
    """`{HA user id: phone endpoint id}` of every panel (a phone entry with `panel: true` whose phone endpoint exists)."""
    from .phone_entry import CONF_PANEL, phone_entries

    out: dict[str, str] = {}
    for entry in phone_entries(hass):
        user_id = entry.data.get("ha_user_id")
        if entry.options.get(CONF_PANEL) is True and isinstance(user_id, str) and user_id:
            phone = phone_of_user(hass, user_id)
            if phone is not None:
                out[user_id] = phone.id
    return out


def _loaded_store(hass: HomeAssistant):
    store = hass.data.get(DOMAIN, {}).get("content")
    return store if store is not None and store.loaded else None


def is_app_screen(hass: HomeAssistant, endpoint_id: Any) -> bool:
    """Whether `endpoint_id` is a FORMER panel screen: a declared screen whose `ha_user_id` is a panel's user (no slug match).
    Such a screen stays out of house calls, ownership and the Talk keying (the R1 rule: the panel's identity is its phone). False
    when the content store has not loaded, and for anything that is not a declared screen. A browser or dashboard screen (its
    login is no panel's) is never one. Never raises."""
    try:
        if not isinstance(endpoint_id, str) or not endpoint_id:
            return False
        store = _loaded_store(hass)
        if store is None:
            return False
        declaration = store.dashboards.get(endpoint_id)
        if not isinstance(declaration, dict):
            return False
        return is_panel_user(hass, declaration.get("ha_user_id"))
    except Exception:  # noqa: BLE001 - a predicate that raises would hide a screen from every consumer
        _LOGGER.warning("portico endpoints: is_app_screen could not be read", exc_info=True)
        return False


def phone_of(hass: HomeAssistant, slug: Any) -> Endpoint | None:
    """The phone of a former panel screen: the slug, then the screen, then its user, then the phone through
    `registry.for_user` (which returns the phone first). None for a slug that is not a former panel screen."""
    if not is_app_screen(hass, slug):
        return None
    registry = get_endpoint_registry(hass)
    screen = registry.get(slug, kind=KIND_DASHBOARD)
    if screen is None or not screen.ha_user_id:
        return None
    phone = registry.for_user(screen.ha_user_id)
    return phone if phone is not None and phone.kind == KIND_PHONE else None


def panel_wake_doors(hass: HomeAssistant, slug: Any) -> list[str] | None:
    """The door references the panel (its phone endpoint id, `slug`) wakes for (`wake.doors`), or None when it has none to say (a legacy document, a
    store that is not loaded): then every door rings the panel, as the app rings for every door when `wake` is absent."""
    store = _loaded_store(hass)
    if store is None or not isinstance(slug, str):
        return None
    wake = store.wake_for_endpoint(slug)
    return None if wake is None else list(wake["doors"])


def door_in_refs(refs: list[str], door_id: str) -> bool:
    """Whether a door id is one of a screen's `wake` door references (a bare door id, or `<provider>:<id>` of a legacy element)."""
    return any(ref == door_id or (isinstance(ref, str) and ref.partition(":")[2] == door_id) for ref in refs)


def panel_door_push_allowed(hass: HomeAssistant, user_id: Any, door_id: Any) -> bool:
    """DEV-807 design section 5 item 8: whether a DOOR ring may be pushed to this HA user's registration. False only for a
    panel whose phone has a `wake` list that does not hold the door. Everything else passes: a user that is no
    panel, a panel with no `wake` (legacy document), a ring with no door. House pushes never
    ask (they pass). Never raises: any doubt is a push, the app's own filter being the second check."""
    try:
        if not is_panel_user(hass, user_id) or not isinstance(door_id, str) or not door_id:
            return True
        phone = phone_of_user(hass, user_id)
        if phone is None:
            return True
        wake = panel_wake_doors(hass, phone.id)
        return wake is None or door_in_refs(wake, door_id)
    except Exception:  # noqa: BLE001
        _LOGGER.debug("portico endpoints: the panel push filter failed open", exc_info=True)
        return True


def announce_panels(hass: HomeAssistant, user_id: Any = None) -> None:
    """Tell the subscribers of every panel (of this HA user only, when given) to refetch: `house_calls` and
    `phone_endpoint` sit outside `rev`, and `content/subscribe` events come only from the store's `_announce` (DEV-807, design
    section 5 item 2). The panel is addressed by its phone endpoint id (the app's "slug" is that id). Called by every writer
    that changes either one. Never raises."""
    try:
        store = hass.data.get(DOMAIN, {}).get("content")
        if store is None:
            return
        for uid, phone_id in panel_users(hass).items():
            if user_id is None or uid == user_id:
                store._announce(phone_id)
    except Exception:  # noqa: BLE001 - an announcement is a nicety; the write that caused it has happened
        _LOGGER.warning("portico endpoints: a panel announcement failed", exc_info=True)
