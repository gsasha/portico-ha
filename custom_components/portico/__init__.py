"""Portico: one Home Assistant integration (websocket contract, enrollment, content, the push ring notification, the card
and panel, and the doors: the transport, the call state and the door entities).

Design doc: automation/portico_design.md in the estate docs repo, §9.2; the fold of the former `portico_intercom`
integration into this one: automation/portico_unify_design.md.

Three kinds of config entry, told apart by `data["entry_type"]`: `push` (the push ring notification, the install-wide settings
host, push/__init__.py), `phone` (one per enrolled phone, phone_entry.py) and `door` (one per door, `door/`). A door
keeps its own `data["driver"]` field (the driver id), which says nothing about the entry's kind. An entry with
`data.driver` and NO `entry_type` is an old-format door entry from before the first split: it is refused here with one
line and removed by the legacy sweep in `async_remove_entry`.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os

from aiohttp import web
from homeassistant.auth import EVENT_USER_REMOVED
from homeassistant.components import frontend
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CoreState, Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

try:  # HA 2026.11 moved the key from components.http to helpers.http
    from homeassistant.helpers.http import KEY_ALLOW_CONFIGURED_CORS
except ImportError:  # older cores
    from homeassistant.components.http import KEY_ALLOW_CONFIGURED_CORS

from . import enroll_view, whoami
from .call_events import async_setup_call_events
from .config_flow import DOMAIN
from .config_service import async_setup_config_service
from .config_sync import async_setup_config_sync
from .content import announce_gate_change, get_content_store
from .door import (
    async_setup_door_entry,
    async_setup_door_side,
    async_unload_door_entry,
)
from .door.const import is_door_entry
from .enroll_redeem import SWEEP_INTERVAL, async_register_view, async_sweep_pending
from .house import async_setup_house
from .managed_talk import async_request_reconcile as async_request_managed_talk
from .managed_talk import async_setup_managed_talk
from .gate_target import async_setup_gate_watch
from .phone_entry import async_revoke_phone, is_phone_entry, phone_device_identifiers, phone_entries
from .provider import DATA_PROVIDERS, ProviderRegistry
from .push import (
    async_remove_push_entry,
    async_setup_push_entry,
    async_unload_push_entry,
)
from .push.const import ENTRY_TYPE, ENTRY_TYPE_PUSH
from .signaling import async_setup_signaling
from .tailscale_approve import OAuthAppStore

# The Lovelace card's own URL — a static path this module registers in
# async_setup, plus frontend.add_extra_js_url pointing at it (§9.7): NOT
# `frontend: panels`, which registers a sidebar page rather than a card
# usable inside an arbitrary dashboard.
_CARD_URL_PATH = "/portico/portico-card.js"

# The admin sidebar page that shows what each enrolled phone shows (content
# design §4): a custom panel, registered here beside the card.
_PANEL_URL_PATH = "/portico/portico-panel.js"
_PANEL_NAME = "portico-panel"
_PANEL_URL = "portico"

# The Phones tab's module (UI split P2), imported lazily by the panel.
_PHONES_FILE = "portico-phones.js"
_PHONES_URL_PATH = "/portico/portico-phones.js"

# The Settings tab's module (UI split P4 to P6), served and imported the same way.
_SETTINGS_FILE = "portico-settings.js"
_SETTINGS_URL_PATH = "/portico/portico-settings.js"

# The Elements renderers (DEV-760 E3): a shared ES module with no tab of its own yet; the Elements tab (E4) imports it.
_ELEMENTS_FILE = "portico-elements.js"
_ELEMENTS_URL_PATH = "/portico/portico-elements.js"
_ELEMENTS_RENDER_FILE = "portico-elements-render.js"
_ELEMENTS_RENDER_URL_PATH = "/portico/portico-elements-render.js"


def _manifest_version() -> str:
    """The integration's manifest `version` ('' when it cannot be read). Read once at import, which HA does in an
    executor, so nothing blocks the loop. Only the FALLBACK token now (see `_content_token`)."""
    try:
        with open(os.path.join(os.path.dirname(__file__), "manifest.json"), encoding="utf-8") as handle:
            return str(json.load(handle).get("version") or "")
    except (OSError, ValueError):
        return ""


_VERSION = _manifest_version()

_TOKEN_LENGTH = 12


def _content_token(path: str) -> str:
    """The `?v=` token of a served file: the first 12 hex chars of the sha256 of its bytes. A changed file changes its
    URL by itself (the manifest version did not move when the card JS changed, and a kiosk WebView plus HA's service
    worker kept serving the old card), and an unchanged file keeps its URL. Blocking file read: call it in an executor.
    When the file cannot be read, the manifest version stands in (one warning), so a URL is still registered."""
    try:
        with open(path, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()[:_TOKEN_LENGTH]
    except OSError as err:
        _LOGGER.warning("Portico: %s could not be read for its URL token (%s); using the manifest version",
                        os.path.basename(path), err)
        return _VERSION


def _versioned(url_path: str, token: str) -> str:
    """`<url_path>?v=<token>` (M6 R7-07): the URL the card and the panel are REGISTERED under, so a WebView that
    cached the bare URL (Fully Kiosk's) fetches the file again whenever the file's content, hence the token, changed.
    The registered URL is fixed at HA startup. The static path itself stays the bare one (aiohttp ignores the query).
    An empty token leaves the bare URL."""
    return f"{url_path}?v={token}" if token else url_path


_LOGGER = logging.getLogger(__name__)

# DEV-704: every Portico script is sent with `Cache-Control: no-cache` ("store it, but ask the server before each reuse").
# HA's own static helper offers only a month-long `max-age` (`cache_headers=True`) or no header at all (`False`); with no
# header a browser may reuse a file for days from its Last-Modified age without asking, under ANY URL it has cached,
# hashed or not. The file still goes out with its ETag and Last-Modified, so an unchanged file costs one 304.
_REVALIDATE_HEADERS = {"Cache-Control": "no-cache"}


def _async_serve_script(hass: HomeAssistant, url_path: str, file_path: str) -> None:
    """Serve one file of `www/` at `url_path` (GET only, as HA's own static paths are) with `Cache-Control: no-cache`, CORS-allowed like HA's own
    static paths. Replaces `StaticPathConfig(..., cache_headers=False)`, which cannot set that header."""

    async def _handler(request: web.Request) -> web.StreamResponse:
        # A stale `?v=`: the panel's config registers each module URL once, at HA startup, so after the files change without
        # a restart (a dev-sync pull) the panel still imports `?v=<old>`. Whatever cached that URL (a WebView, a proxy, a
        # browser's module cache) would keep the old code. Answer it with a never-cached redirect to the current token, so
        # the file is always fetched under a URL that names its present bytes. The bare URL and a current token are served.
        asked = request.query.get("v")
        if asked is not None:
            current = await hass.async_add_executor_job(_content_token, file_path)
            if asked != current:
                return web.HTTPFound(_versioned(url_path, current), headers={"Cache-Control": "no-store"})
        return web.FileResponse(file_path, headers=_REVALIDATE_HEADERS)

    route = hass.http.app.router.add_route("GET", url_path, _handler)
    hass.http.app[KEY_ALLOW_CONFIGURED_CORS](route)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Domain-wide setup, called once regardless of how many devices get
    configured — the websocket_api commands (signaling.py, §9.5) and the
    Lovelace card (§9.7) are each one shared surface for every configured
    device, not per-entry."""
    async_setup_signaling(hass)

    # DEV-544: the unauthenticated endpoint a phone redeems its one-time enrollment code at, and the sweep that
    # removes a phone whose QR nobody redeemed within 24 hours (user and entry; an abandoned enrollment leaves no
    # user behind).
    async_register_view(hass)

    # UI split P2: the admin-only view that serves the Phones tab's QR (a PNG, fetched once with the user's bearer).
    enroll_view.async_register_view(hass)

    # DEV-798 (SEC-02): the unauthenticated endpoint Home Assistant proves itself at before a phone sends it a token.
    whoami.async_register_view(hass)

    async def _sweep(_now=None) -> None:
        try:
            await async_sweep_pending(hass)
        except Exception:  # noqa: BLE001 - the next sweep tries again
            _LOGGER.warning("Portico: the sweep of unredeemed phones failed", exc_info=True)

    stop_sweep = async_track_time_interval(hass, _sweep, SWEEP_INTERVAL, cancel_on_shutdown=True)
    @callback
    def _stop_sweep(_event: Event) -> None:
        stop_sweep()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _stop_sweep)

    # The provider registry (PH-1): reachable after async_setup with no entry. Since the fold of `portico_intercom` into
    # this integration it holds ONE built-in provider (the doors'), registered here directly; content and the push
    # sender consume the registry. Registering is idempotent for the same domain, so a second async_setup is harmless.
    registry = hass.data.setdefault(DOMAIN, {}).get(DATA_PROVIDERS)
    if registry is None:
        registry = hass.data[DOMAIN][DATA_PROVIDERS] = ProviderRegistry(hass)
    door_provider = async_setup_door_side(hass)
    if registry.provider(door_provider.domain) is not door_provider:
        registry.register(door_provider)

    # M7 3a: the house-call registry, always built (D-20261122-aafb removed the global House calls switch; a stored `house_calls`
    # option on the settings entry is ignored).
    async_setup_house(hass)

    # DEV-824 P1: `portico.validate_config` reads /config/portico/ and reports; applies nothing.
    async_setup_config_service(hass)
    async_setup_config_sync(hass)      # DEV-824 P3: portico.export_config

    # DEV-811 P4: every ring and call as a `portico_call` event, and the live set behind `sensor.portico_calls`. Listens to the
    # door and outbound transitions the registry fans out and to the house registry's call-level moments; changes none of them.
    call_events = async_setup_call_events(hass)
    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, lambda _event: call_events.stop())

    # DEVICES-10: every screen and enrolled phone has a managed Talk element; created at start and on every change.
    async_setup_managed_talk(hass)

    # M7 3f: the gate as a roster endpoint and call target. Says nothing until some door reports `CanDial` (none does today).
    async_setup_gate_watch(hass, lambda: announce_gate_change(hass))

    card_path = os.path.join(os.path.dirname(__file__), "www", "portico-card.js")
    _async_serve_script(hass, _CARD_URL_PATH, card_path)
    card_token = await hass.async_add_executor_job(_content_token, card_path)
    frontend.add_extra_js_url(hass, _versioned(_CARD_URL_PATH, card_token))

    await _async_register_panel(hass)

    _schedule_content_preload(hass)

    # P-4: a phone's HA user deleted in Settings > People leaves its entry with nothing to authenticate: remove the
    # entry too (its revocation finds the user gone and does the rest).
    async def _user_removed(event: Event) -> None:
        user_id = event.data.get("user_id")
        for entry in phone_entries(hass):
            if user_id and entry.data.get("ha_user_id") == user_id:
                _LOGGER.info("Portico: the Home Assistant user of the phone %r was deleted; removing its entry",
                             entry.title)
                hass.async_create_task(hass.config_entries.async_remove(entry.entry_id), eager_start=False)

    hass.bus.async_listen(EVENT_USER_REMOVED, _user_removed)

    return True


def _schedule_content_preload(hass: HomeAssistant) -> None:
    """M6 E1: dashboards and phone camera declarations live in the content store and are listed by the endpoint
    registry (and so by the auth table) only once it has loaded. Load it when HA has started, so a kiosk user is
    recognised without waiting for the first content command. Reading only: the file is not written. A setup
    while HA is already running (tests, a late reload) keeps the lazy first-use load."""
    if hass.state is CoreState.running:
        return

    async def _preload(_event: Event) -> None:
        try:
            await get_content_store(hass).async_ensure_loaded()
        except Exception:  # noqa: BLE001 - the lazy load on first use still works
            _LOGGER.warning("Portico: the content store could not be loaded at start", exc_info=True)

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _preload)


async def _async_register_panel(hass: HomeAssistant) -> None:
    """The Portico sidebar panel, registered for every logged-in HA user (M6 O1): the panel routes by role and every
    write is `admin` in the auth table (auth.COMMANDS), so a non-admin who opens it can change nothing. A failure
    here is logged and never stops the integration: the panel is a view, the calls do not need it."""
    from homeassistant.components import panel_custom

    panel_path = os.path.join(os.path.dirname(__file__), "www", "portico-panel.js")
    try:
        panel_token = await hass.async_add_executor_job(_content_token, panel_path)
        _async_serve_script(hass, _PANEL_URL_PATH, panel_path)
        # UI split P2 and P4: the Phones tab and the Settings tab are each their own module, content-hashed like the panel,
        # and the panel imports them lazily (a failed import leaves the content tabs working). Their hashed URLs travel in
        # the panel's config.
        config: dict[str, str] = {}
        for config_key, file_name, url_path, label in (
            ("phones_module", _PHONES_FILE, _PHONES_URL_PATH, "Phones"),
            ("settings_module", _SETTINGS_FILE, _SETTINGS_URL_PATH, "Settings"),
            ("elements_module", _ELEMENTS_FILE, _ELEMENTS_URL_PATH, "Elements"),
            ("elements_render_module", _ELEMENTS_RENDER_FILE, _ELEMENTS_RENDER_URL_PATH, "Elements renderer"),
        ):
            module_path = os.path.join(os.path.dirname(__file__), "www", file_name)
            try:
                module_token = await hass.async_add_executor_job(_content_token, module_path)
                _async_serve_script(hass, url_path, module_path)
                config[config_key] = _versioned(url_path, module_token)
            except Exception as err:  # noqa: BLE001 - the panel alone is worth registering
                _LOGGER.warning("The Portico %s tab module could not be served (%s: %s)", label, type(err).__name__, err)
        await panel_custom.async_register_panel(
            hass,
            webcomponent_name=_PANEL_NAME,
            frontend_url_path=_PANEL_URL,
            module_url=_versioned(_PANEL_URL_PATH, panel_token),
            sidebar_title="Portico",
            sidebar_icon="mdi:doorbell-video",
            require_admin=False,
            config=config,
        )
    except Exception as err:  # noqa: BLE001 - see the docstring
        _LOGGER.warning("The Portico panel could not be registered (%s: %s)", type(err).__name__, err)


def _is_push_entry(entry: ConfigEntry) -> bool:
    """The push ring notification (FCM sender) entry, beside the device entries."""
    return entry.data.get(ENTRY_TYPE) == ENTRY_TYPE_PUSH


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up an entry by its kind: the push ring notification (push) entry (which also hosts enrollment and content), a phone, or
    a door. An old-format door entry (`data.driver`, no `entry_type`) is refused with one line."""
    if _is_push_entry(entry):
        return await async_setup_push_entry(hass, entry)
    if is_phone_entry(entry):
        return await _async_setup_phone_entry(hass, entry)
    if is_door_entry(entry):
        return await async_setup_door_entry(hass, entry)
    if entry.data.get("driver"):
        _LOGGER.error(
            'Portico: entry "%s" (%s) is an old-format door entry (it has a driver but no entry kind). '
            "Delete it and add the door under Settings > Add integration > Portico > Add a door",
            entry.title, entry.entry_id,
        )
    else:
        _LOGGER.error('Portico: entry "%s" (%s) is not a Portico entry kind this version serves', entry.title, entry.entry_id)
    return False


PHONE_PLATFORMS = ["sensor"]


async def _async_setup_phone_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """A phone entry (P-4): its own HA device and its two sensors (ENROLL-01, ENROLL-02). Nothing is started: the
    phone's push state is read from the push ring notification when it is loaded."""
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers=phone_device_identifiers(entry),
        name=entry.title,
        manufacturer="Portico",
        model="Phone",
    )
    await hass.config_entries.async_forward_entry_setups(entry, PHONE_PLATFORMS)
    async_request_managed_talk(hass)        # a phone enrolled (or loaded at start): its managed Talk element (DEVICES-10)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry. A refused entry was never set up: nothing to unload."""
    if _is_push_entry(entry):
        return await async_unload_push_entry(hass, entry)
    if is_phone_entry(entry):
        return await hass.config_entries.async_unload_platforms(entry, PHONE_PLATFORMS)
    if is_door_entry(entry):
        return await async_unload_door_entry(hass, entry)
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Removing the push ring notification entry deletes its private stores (key, registry, OAuth-app secret); removing a door
    deletes its peer-token Store. Removing an old-format door entry (`data.driver` with no `entry_type`, refused since
    S-2) is the legacy sweep: its two private stores go, with one guard (Q4): the OAuth-app secret is the only copy the
    installer cannot recreate (he does not hold the client secret), so it is deleted only once the push entry's own
    copy loads."""
    if _is_push_entry(entry):
        await async_remove_push_entry(hass, entry)
        return
    if is_phone_entry(entry):
        await async_revoke_phone(hass, entry)     # ENROLL-05, PUSH-04
        async_request_managed_talk(hass)        # keeps its Talk element as an orphan ("phone removed"); never deletes it
        return
    if is_door_entry(entry):
        return      # a door entry keeps no private Store since the observer's peer token went (DEV-518 C5)
    if not entry.data.get("driver"):
        return
    # An old-format door entry (no entry kind): the gate-sip peer token's file goes with it.
    await Store(hass, 1, f"portico.gate_sip_peer.{entry.entry_id}", private=True).async_remove()
    if await _push_entry_has_oauth_copy(hass, entry):
        await OAuthAppStore(hass, entry.entry_id).async_remove()
    else:
        _LOGGER.warning(
            "Portico: kept .storage/%s: the push entry holds no copy of the Tailscale OAuth-app secret yet",
            f"portico.tailscale_oauth_app.{entry.entry_id}",
        )


async def _push_entry_has_oauth_copy(hass: HomeAssistant, removed: ConfigEntry) -> bool:
    """Whether a push entry (other than `removed`) has an OAuth-app secret that loads under its own id."""
    for other in hass.config_entries.async_entries(DOMAIN):
        if other.entry_id != removed.entry_id and _is_push_entry(other):
            if await OAuthAppStore(hass, other.entry_id).async_load_secret() is not None:
                return True
    return False
