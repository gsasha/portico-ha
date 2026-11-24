"""The editor's websocket commands (M6 E2, design 3.2 / 4.4 / 4.5 / 5.3).

  portico/content/document  the working set: the stored document, its `doc_rev`, every phone's and screen's
                            resolved document, the endpoint rows and the HA users a screen can be declared for
  portico/content/preview   a DRAFT document resolved for one endpoint exactly as `content/get` would after a
                            save (the same pure resolver); writes nothing
  portico/content/save      the one write: the whole document, refused when stale (`conflict`) or invalid
  portico/kiosk/hello       a kiosk page tells HA it is open; bounded, memory only
  portico/config/status     the file configuration's state for the editor banner (config_service.py); read-only, administrators only
  portico/cameras/info      the picture size (and frame rate, when HA knows it) of up to 20 camera entities, for the
                            picker's spec chips (camera_info.py); administrators only, never sent by the phone app

The editor commands are `admin` in auth.COMMANDS and call `auth.require` first. They add commands only:
`content/get`, `content/run`, `content/snapshot`, `content_changed` and the `{phones}` reply of `content/list`
(the panel on disk reads it until E5a) are untouched.
"""
from __future__ import annotations

import re
import time
from collections import OrderedDict
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from . import auth, config_sync
from .camera_info import MAX_IDS, get_camera_info
from .content import (
    MAX_ENDPOINT_ID,
    ProfileError,
    SaveConflict,
    SaveRefused,
    get_content_store,
    phone_facts,
    phone_label,
)
from .content_resolve import endpoint_table, resolve_endpoint
from .endpoints import KIND_DASHBOARD, KIND_PHONE, get_endpoint_registry, is_app_screen, is_panel_user
from .live_rtsp import push_entry_options
from .provider import DOMAIN

# Options of the Portico settings (push) entry that dress the kiosk page (DEV-836); both unset by default. Sent to the page in the
# `kiosk` object of the hello reply, which any logged-in user may read, so neither may hold a secret.
CONF_KIOSK_BACKGROUND_URL = "kiosk_background_url"      # an http(s) or same-origin picture URL for the page background
CONF_KIOSK_BACK_PATH = "kiosk_back_path"                # an in-app route the card's back rail goes to
KIOSK_SEEN_MAX = 32        # kiosk/hello remembers at most this many users, least recently seen evicted
DATA_KIOSK_SEEN = "kiosk_seen"

_ENDPOINT_ID = vol.All(str, vol.Length(min=1, max=MAX_ENDPOINT_ID))
_REV = vol.All(str, vol.Length(min=1, max=64))
_CAMERA_ID = re.compile(r"camera\.[a-z0-9_]+")           # an entity id of the camera domain, as HA writes them


class KioskSeen:
    """Who has said hello from a kiosk page: `user id -> (name, monotonic last seen)`, in memory only (gone at a
    restart, re-sent by the page on every connect). One entry per user id, non-administrator and non-system users
    only, at most KIOSK_SEEN_MAX, the least recently seen evicted: an unbounded write by any logged-in user is
    exactly what the bound is for (R6-09)."""

    def __init__(self, limit: int = KIOSK_SEEN_MAX) -> None:
        self._limit = limit
        self._seen: OrderedDict[str, tuple[str, float]] = OrderedDict()

    def record(self, user) -> bool:
        """Remember `user`; False (nothing stored) for an administrator or a system user."""
        if user is None or user.is_admin or user.system_generated:
            return False
        self._seen[user.id] = (user.name or "", time.monotonic())
        self._seen.move_to_end(user.id)
        while len(self._seen) > self._limit:
            self._seen.popitem(last=False)
        return True

    def is_open(self, user_id: str) -> bool:
        return user_id in self._seen

    def __len__(self) -> int:
        return len(self._seen)


def get_kiosk_seen(hass: HomeAssistant) -> KioskSeen:
    data = hass.data.setdefault(DOMAIN, {})
    seen = data.get(DATA_KIOSK_SEEN)
    if seen is None:
        seen = data[DATA_KIOSK_SEEN] = KioskSeen()
    return seen


def _endpoint_rows(hass: HomeAssistant) -> list[dict]:
    """The endpoint registry as rows for the editor's sidebar and pickers: phones, screens, doors. A phone row
    carries the editor's `label` (its name with the enrolment day and whether its app registered, so two phones
    of one name are told apart)."""
    from .push.registration import _phones

    phones: dict[str, tuple[str, dict]] = {}
    for _entry, phone in _phones(hass):
        endpoint_id = phone.get("enroll_id") or phone.get("ha_user_id")
        if endpoint_id and phone.get("ha_user_id"):
            phones.setdefault(endpoint_id, (phone["ha_user_id"], phone))
    rows = []
    for ep in get_endpoint_registry(hass).endpoints():
        label = ep.name
        if ep.kind == KIND_PHONE and ep.id in phones:
            user_id, phone = phones[ep.id]
            label = phone_label(ep.name, phone_facts(hass, user_id, phone))
        row = {"id": ep.id, "kind": ep.kind, "name": ep.name, "label": label, "ha_user_id": ep.ha_user_id,
               "cameras": list(ep.cameras), "room": ep.room, "available": ep.available, "reason": ep.reason}
        if ep.can_dial:
            row["can_dial"] = True        # the gate (3f): the call-target picker offers a door only with this; absent otherwise
        if ep.kind == KIND_DASHBOARD and is_app_screen(hass, ep.id):
            row["app_screen"] = True      # DEV-807: a FORMER panel screen (its login is a panel's); the Devices tab lists it like any screen
        if ep.kind == KIND_PHONE and is_panel_user(hass, ep.ha_user_id):
            row["panel"] = True           # own pages: a panel owns door Talk elements on its own pages (the editor's ownership mirror)
        rows.append(row)
    return rows


def _ownership_fields(store) -> dict[str, Any]:
    """Schema 6 only (the wire for 4/5 is as it was): `wake` `{<screen>: {doors,
    elements, self_elements, screen_call}}` (D2). A door Talk element on several screens wakes each of them (D-20261122-42b1)."""
    if not store.uses_pages_model:
        return {}
    return {"wake": store.wake_map()}


@websocket_api.websocket_command({vol.Required("type"): "portico/content/document"})
@websocket_api.async_response
async def handle_content_document(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """`{}` -> `{document, doc_rev, resolved: {<endpoint id>: resolved document}, endpoints, orphans, ha_users}`.

    `resolved` has one entry per phone and screen (the callees); `orphans` are the override keys no endpoint has
    ("Not attached"); `ha_users` are the non-system HA users with `kiosk_open` from `kiosk/hello`."""
    if not auth.require(connection, msg, "portico/content/document"):
        return
    store = get_content_store(hass)
    await store.async_ensure_loaded()
    seen = get_kiosk_seen(hass)
    users = [{"id": u.id, "name": u.name, "is_admin": u.is_admin, "kiosk_open": seen.is_open(u.id)}
             for u in await hass.auth.async_get_users() if not u.system_generated]
    connection.send_result(msg["id"], {
        "document": store.document(),               # schema 4, or 5 while it holds element references
        "doc_rev": store.doc_rev,
        "resolved": {ep.id: store.resolve_document(ep.id)
                     for ep in get_endpoint_registry(hass).endpoints() if ep.kind in (KIND_PHONE, KIND_DASHBOARD)},
        "endpoints": _endpoint_rows(hass),
        "orphans": store.orphan_keys(),
        "ha_users": users,
        **_ownership_fields(store),
    })


@websocket_api.websocket_command({
    vol.Required("type"): "portico/content/preview",
    vol.Required("document"): dict,
    vol.Required("endpoint"): _ENDPOINT_ID,
    vol.Optional("caller"): _ENDPOINT_ID,
})
@websocket_api.async_response
async def handle_content_preview(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """A draft `document` resolved for `endpoint` as if saved: the very document `content/get` serves after the
    save, with `callers` narrowed to `caller` (and `"*"`) when given. Nothing is written or announced. A draft
    that does not validate is `invalid`, with the same text a save would give."""
    if not auth.require(connection, msg, "portico/content/preview"):
        return
    store = get_content_store(hass)
    await store.async_ensure_loaded()
    try:
        document = store.prepare_document(msg["document"])
    except ProfileError as err:
        connection.send_error(msg["id"], "invalid", str(err))
        return
    known = {e.id for e in endpoint_table(document, store) if e.kind in (KIND_PHONE, KIND_DASHBOARD)}
    if msg["endpoint"] not in known:
        connection.send_error(msg["id"], "unknown_endpoint", "no phone or screen with that id")
        return
    # References are expanded against the live elements: the preview is what `content/get` sends after the save.
    connection.send_result(msg["id"], resolve_endpoint(
        store.expand(document), msg["endpoint"], states=hass.states, registry=store, caller=msg.get("caller")))


@websocket_api.websocket_command({
    vol.Required("type"): "portico/content/save",
    vol.Required("document"): dict,
    vol.Required("base_doc_rev"): _REV,
    vol.Optional("drop_orphans"): [_ENDPOINT_ID],
})
@websocket_api.async_response
async def handle_content_save(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Store the whole document -> `{doc_rev}`. `conflict` when `base_doc_rev` is no longer the stored document's
    (the editor reloads), `invalid` with the validator's text when it does not validate or the stored file is a
    newer schema. A "Not attached" override is never dropped by leaving it out; `drop_orphans` names the ones to
    delete (also how a move is written: the override appears under its new key and the old key is named here)."""
    if not auth.require(connection, msg, "portico/content/save"):
        return
    store = get_content_store(hass)
    try:
        doc_rev = await store.async_save_document(
            msg["document"], msg["base_doc_rev"], drop_orphans=tuple(msg.get("drop_orphans", ())))
    except SaveConflict as err:
        connection.send_error(msg["id"], "conflict", str(err))
        return
    except SaveRefused as err:
        frame = websocket_api.error_message(msg["id"], err.code, str(err))
        frame["error"].update(err.fields)        # `page`/`devices` (in_use)
        connection.send_message(frame)
        return
    except ProfileError as err:
        connection.send_error(msg["id"], "invalid", str(err))
        return
    config_sync.async_schedule(hass)           # DEV-824 P3: the files follow the save; never fails or delays it
    connection.send_result(msg["id"], {"doc_rev": doc_rev})


@websocket_api.websocket_command({
    vol.Required("type"): "portico/kiosk/hello",
    vol.Optional("endpoint"): _ENDPOINT_ID,
})
@websocket_api.async_response
async def handle_kiosk_hello(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """A kiosk page says it is open: `{endpoint?}` -> `{user: {id, name}, declared: [slugs], endpoint_ok}`.

    `declared` are the screens declared for this connection's user; `endpoint_ok` says the named screen is one of
    them (an administrator may open any declared screen, e.g. to test on a laptop). Open to any logged-in user and
    so bounded: it only records non-administrators, one entry per user, 32 at most, in memory (KioskSeen)."""
    if not auth.require(connection, msg, "portico/kiosk/hello"):
        return
    store = get_content_store(hass)
    await store.async_ensure_loaded()
    user = connection.user
    get_kiosk_seen(hass).record(user)
    registry = get_endpoint_registry(hass)
    declared = [e.id for e in registry.dashboards_for_user(user.id)]
    wanted = msg.get("endpoint")
    if wanted is None:
        ok = bool(declared)
    elif user.is_admin:
        ok = registry.get(wanted, kind=KIND_DASHBOARD) is not None
    else:
        ok = wanted in declared
    reply = {"user": {"id": user.id, "name": user.name}, "declared": declared, "endpoint_ok": ok}
    options = push_entry_options(hass)
    look = {key: options[option] for key, option in (("background_url", CONF_KIOSK_BACKGROUND_URL),
                                                     ("back_path", CONF_KIOSK_BACK_PATH))
            if isinstance(options.get(option), str) and options[option].strip()}
    if look:                                    # omitted while unset: the reply is the old shape
        reply["kiosk"] = look
    connection.send_result(msg["id"], reply)


@websocket_api.websocket_command({
    vol.Required("type"): "portico/cameras/info",
    vol.Required("entity_ids"): vol.All([vol.All(str, vol.Length(min=1, max=255))], vol.Length(min=1, max=MAX_IDS)),
})
@websocket_api.async_response
async def handle_cameras_info(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """`{entity_ids: [..]}` (1 to 20) -> `{cameras: [{entity_id, width, height, fps, source}]}`, one row per distinct id
    in the order asked (camera_info.py says where each number comes from). Only `camera.*` ids are served; any other
    id refuses the whole call as `invalid_entity` before anything is probed, so this is no way to read or fetch an
    arbitrary entity. A camera id that is not in `hass.states` is a row with `source: unavailable`."""
    if not auth.require(connection, msg, "portico/cameras/info"):
        return
    ids = msg["entity_ids"]
    if not all(_CAMERA_ID.fullmatch(entity_id) for entity_id in ids):
        connection.send_error(msg["id"], "invalid_entity", "only camera entities can be asked about")
        return
    connection.send_result(msg["id"], {"cameras": await get_camera_info(hass).async_info(hass, ids)})


@callback
def async_setup_editor_commands(hass: HomeAssistant) -> None:
    from .config_service import handle_config_status

    websocket_api.async_register_command(hass, handle_content_document)
    websocket_api.async_register_command(hass, handle_content_preview)
    websocket_api.async_register_command(hass, handle_content_save)
    websocket_api.async_register_command(hass, handle_kiosk_hello)
    websocket_api.async_register_command(hass, handle_cameras_info)
    websocket_api.async_register_command(hass, handle_config_status)     # DEV-824 P5: the file configuration's state, for the banner
    from .frigate_webrtc import async_setup_frigate_webrtc
    from .pages_migrate import async_setup_pages_migrate
    from .ring_pages_migrate import async_setup_ring_pages_migrate

    async_setup_frigate_webrtc(hass)
    async_setup_pages_migrate(hass)
    async_setup_ring_pages_migrate(hass)
