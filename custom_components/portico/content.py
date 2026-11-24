"""Content profiles: what the Portico app (and the dashboard card) shows
besides a call (content design, phase A).

A profile belongs to one enrolled phone (keyed by its HA user id) and is an
ordered list of cameras and actions. It is stored in `.storage/portico.content`,
never in a config entry, so editing content never reloads the integration.

Besides the profiles the same file holds one install-wide setting, `home_name` (the name of the home the
app shows at the top; absent when unset, never defaulted in code) and `show_fps` (show the frame rate on live pictures: a
boolean stored only when true, so a file without it is unchanged; strict on a save, lenient on a load).

Schema v6 (Pages unified, P-A). `{schema_version: 6, pages: [...], devices: {...}, roster?, dashboards, endpoints, ...}`: one
library of pages of kind `content` or `ring`, and per device its own page list and ring assignments; no template, no overrides,
no rules (`pages_resolve.py`, `pages_model.py`; design `portico_pages_unify_design.md`). It is written ONLY by the explicit
admin command `portico/content/pages_migrate` (`pages_migrate.py`), which first leaves the file as it was read as
`.storage/portico.content.v4` or `.v5` (and refuses to write without that copy); a build that stops at schema 5 reads it as
"newer" and refuses every write. Until that command is applied nothing in this build writes a 6, and a schema 4/5 document
resolves exactly as before.

Schema v4 (blocks, DEV-658 B1). A page is `{id, label, blocks}` and a block `{id, title, door?, cameras, actions}`;
the cameras and buttons moved into blocks and nothing else about them changed (ids, validators, kinds). The file on
disk holds `blocks` and nothing else on a page (B8 dropped the derived flat `cameras`/`actions` mirror; a file written
by an older build still carries one, which is ignored and gone at the next save). The one-time copy of the v3 file
(`.storage/portico.content.v3`) is the way back to a schema-3 build. A v3 file is read through `migrate_v3_to_v4`
(pure): every page becomes ONE block (id = the page id, title = the page label) and nothing is dropped. The v3
description follows.

Schema v3 (M6 E1; v2 was PH-9). The file is `{schema_version: 3, home_name?, template, overrides, dashboards,
endpoints}`: a household `template` (pages, rules, roster), one `overrides` record per endpoint id (a phone's
`enroll_id`, or its HA user id when it has none), the declared `dashboards`, and `endpoints` (a phone's camera
declaration). A page is `{id, label, cameras, actions}`; `content/get` still serves the "home" page flat, so the
v1 wire is unchanged, and a page's tiles keep the v1 record shapes. v3 adds `label`, validated `rules` and
`roster` (`[]` now means nobody; `["*"]` every endpoint) and `endpoints`; it removes nothing.

Older files are read through `migrate_v1_to_v2` / `migrate_v2_to_v3` / `migrate_v3_to_v4` (pure) and NOTHING is
written back until the first normal save, which writes v4 and first leaves a one-time copy of the older file as
`.storage/portico.content.v<N>` (N = the version read). NO migration drops a stored override or profile: an
override whose key no endpoint has (a re-enrolled phone's old id, a v1 profile whose user is no phone) is kept
and listed as "Not attached" (`orphan_keys`), to be moved or deleted by an administrator.
A file with a NEWER schema_version than this build knows is read as far as it can be and every write is
refused (ProfileError), so a downgraded build never rewrites it.

The profile IS the grant: `portico/content/run` takes an action id, finds it in
the caller's own profile, and the plugin makes the call itself. The action kinds
are allow-listed by domain, checked when a profile is saved and again when an
action runs. Nothing here takes a service name or an entity id from a client.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
import time
import unicodedata
from collections.abc import Callable, Collection
from typing import Any

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Context, HomeAssistant, callback
from homeassistant.helpers.storage import Store

from .call_endpoints import (
    MAX_TARGET_ID,
    REASON_BUSY,
    REASON_NOT_AVAILABLE,
    REASON_OFFLINE,
    REASON_PHONE_ONLY,
    REASON_PUSH_ONLY,
    REASON_UNKNOWN_TARGET,
    ROSTER_TARGET_TYPES,
    TARGET_TYPES,
    NullCallEndpointRegistry,
    get_call_registry,
)
from .content_resolve import (
    HOME_PAGE, TALK_OFF, ROSTER_ALL, caller_camera_entities, doc_rev, merged_pages, page_actions, page_cameras,
    resolve_endpoint, resolve_page, resolve_roster, roster_action_id, short_id, endpoint_table,
)
from .content_elements import (
    REF_KEY, ElementRefError, check_refs, expand_document, has_cameras, has_refs, is_element_id, is_ref, small_entity_of, stored_ref_keys,
    referenced_ids,
)
from .endpoints import (
    KIND_DASHBOARD, KIND_DOOR, KIND_PHONE, get_endpoint_registry, is_app_screen, is_panel_user, panel_users, phone_of)
from .phone_entry import phone_house_receive
from .pages_resolve import SCHEMA_PAGES, all_pages, assigned_ring_pages, is_pages_model, parties_of
from . import actions, pages_ownership
from . import live_rtsp as live_rtsp_module
from .camera_info import get_camera_info
from .provider import DATA_PROVIDERS, DOMAIN, ProviderError, error_code
from .talk_relay import get_talk_relay

GATE_DRIVERS = frozenset({"gate_sip_server"})      # the drivers whose door can be called from Talk

_LOGGER = logging.getLogger(__name__)

STORE_KEY = "portico.content"
DATA_CONTENT = "content"
SCHEMA_VERSION = 6     # this build can READ and WRITE the unified Pages model (schema 6); a build that stops at 5 refuses it
SCHEMA_PLAIN = 4       # a document with no reference is written exactly as before: stamped 4 (design 3.2)
SCHEMA_REFS = 5        # stamped only when at least one reference exists
ACCEPTED_SCHEMAS = (SCHEMA_PLAIN, SCHEMA_REFS)     # of the LEGACY (template + overrides) model; a document with `pages` or
# `devices` is the unified model, schema 6 (`pages_model.normalize_document_v6`), written only by the explicit
# `portico/content/pages_migrate` apply (until then nothing in this build writes a 6: dormant).
# `content/save` and `content/preview` take a document of this build's schema only (blocks design 4.2); a document
# that says schema 3 is a tab left open across the restart and is refused (reload the page). A PAGE of a schema-4
# document with no `blocks` is still wrapped into one block (`normalize_page`, a v3 page read from a file); a page a
# client SENDS with flat `cameras`/`actions` is refused (`refuse_flat_pages`: "update the Portico panel").
# HOME_PAGE ("home") is the one page a v1 client (app 0.4.3, the card on disk) reads; the others are v3 pages.
# ROSTER_ALL ("*"): a roster of exactly ["*"] = every endpoint, in registry order; [] = nobody. Both are
# defined in content_resolve (which cannot import this module) and imported above.

MAX_CAMERAS = 24       # per page, across its blocks
MAX_ACTIONS = 24
MAX_BLOCKS = 8         # per page
MAX_BLOCK_CAMERAS = 12
MAX_BLOCK_ACTIONS = 12
MAX_LABEL = 40
MAX_TITLE = 40         # a block's title
MAX_HOME_NAME = 40
MAX_PAGES = 12
MAX_RULES = 12
MAX_RULE_BUTTONS = 8
MAX_ROSTER = 64
MAX_ENDPOINT_ID = 160   # a door's id is `<provider>:<device_id>` and a device_id may be a URL
TALK_CAP_S = 300.0      # gate-sip G2's cap on a dialled call (blocks design 7.1); a remembered talk call expires after it
RUN_INTERVAL = 2.0  # seconds between two runs of the same action by the same phone
SNAPSHOT_INTERVAL = 1.0  # seconds between two snapshots of the same camera for the same phone
SNAPSHOT_WIDTH_DEFAULT = 640
SNAPSHOT_WIDTH_MIN = 64
SNAPSHOT_WIDTH_MAX = 1920       # was 1280; the phone's zoomed stills ask for up to 1920 (blocks design 4.2, question 7)
MAX_SNAPSHOT_BYTES = 1_500_000  # of the picture, before base64; HA's websocket limit is far above this

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
ENTITY_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")

# kind -> (entity domain the profile may name, service to call or None, confirm default)
KINDS: dict[str, tuple[str | None, tuple[str, str] | None, bool]] = {
    "portico_open": (None, None, True),  # names a Portico device_id instead of an entity
    "lock_open": ("lock", ("lock", "open"), True),
    "button_press": ("button", ("button", "press"), False),
    "script_run": ("script", ("script", "turn_on"), False),
    "scene_activate": ("scene", ("scene", "turn_on"), False),
    # A call button (house calls design section 14): names a target, not an entity; it is placed through the
    # call-endpoint registry. There is NO confirm step to call anyone (the owner, 2026-10-14, CB-9): the flag is
    # neither offered by the builder nor honoured here, whatever a stored or incoming profile says.
    "call": (None, None, False),
    # Blocks (DEV-658 B1). Service calls run with the HA user of the caller, exactly like `lock_open`.
    "lock_lock": ("lock", ("lock", "lock"), False),
    "lock_unlock": ("lock", ("lock", "unlock"), True),
    "cover_open": ("cover", ("cover", "open_cover"), True),     # a gate, a garage door
    "cover_close": ("cover", ("cover", "close_cover"), False),
    # Talk to the door: acts on the block's `door`, no target of its own; never offered a confirm, never honoured
    # (CB-9). Its run path is `_run_talk` (the gate: `_talk_view`); the viewer-less `_action_available` says False.
    "talk": (None, None, False),
}
KINDS_WITHOUT_CONFIRM = frozenset({"call", "talk"})
# `content/run` refusals of a Talk press by reason (code, message); `talk_off` is the one every press gets today.
_BLOCK_DOOR = object()      # `_run_talk`'s default: the door is the one of the block that holds the Talk button

_TALK_REFUSALS = {
    "talk_off": ("not_available", "Talk to the door is switched off"),
    "no_door": ("no_door", "Door removed"),
    "not_connected": ("not_connected", "The gate is not connected"),
    "busy": ("busy", "The gate is busy"),
    # An explicitly targeted Talk element only (the per-block Talk keeps `talk_off`). The CODE stays `not_available`, the one
    # an app already maps; the text says which case it is.
    "dial_off": ("not_available", "Dialing is switched off at the door"),
    "no_dial": ("not_available", "This door cannot be called"),
}

STREAM_SIZE_RETRY = 600.0      # seconds between two background size refreshes of one camera (camera_info caches 10 minutes too)
STREAM_SIZE_RETRY_UNKNOWN = 60.0   # ... while no size is known yet (a camera that was down, or says nothing): a minute
STREAM_SIZE_SELF_RETRIES = 10      # a live camera whose size is still unknown after a refresh is asked again by the store itself, this often
CAMERA_SIZES = ("auto", "big", "small")
# The button palette (contract/palette_v1.json is the same list with the colours; a test keeps the names equal and
# recomputes every contrast ratio). No colour means the theme's default button.
PALETTE_NAMES: tuple[str, ...] = ("blue", "green", "amber", "red", "purple", "teal", "pink", "grey")


# The icons the phone app can draw next to a button (Material Design Icons names, without "mdi:"). The ONE
# list in the plugin: the panel's icon picker offers exactly these, and contract/icons_v1.json is the same list
# for the app, kept equal by tests/test_content.py here and HomeIconsTest in portico-app. validate() below
# still accepts any short string a profile already holds (an older "mdi:something" is never an error); the
# app simply draws nothing for a name it does not bundle and falls back to an icon for the button's kind.
ICON_NONE = "none"  # the picker's word for "no icon of my own"; never stored
ICON_NAMES: tuple[str, ...] = (
    "door", "door-open", "door-closed", "door-closed-lock", "gate", "gate-open", "garage", "garage-open",
    "fence", "lock", "lock-open", "lock-open-variant", "lock-outline", "lock-open-outline",
    "lock-open-variant-outline", "key", "key-variant", "shield-home", "shield-lock", "home", "home-outline",
    "home-lock", "home-export-outline", "bell", "bell-ring", "alarm-light", "doorbell", "lightbulb",
    "lightbulb-on", "lightbulb-outline", "lightbulb-off", "ceiling-light", "lamp", "floor-lamp",
    "wall-sconce", "led-strip-variant", "light-switch", "toggle-switch", "toggle-switch-off", "power",
    "power-plug", "flash", "thermometer", "thermostat", "fan", "air-conditioner", "radiator", "fire",
    "snowflake", "water-boiler", "weather-sunny", "weather-night", "blinds", "blinds-open", "window-shutter",
    "window-shutter-open", "window-open", "window-closed", "curtains", "curtains-closed", "roller-shade",
    "cctv", "camera", "video", "motion-sensor", "phone", "phone-ring", "phone-hangup", "phone-in-talk",
    "phone-voip", "volume-high", "volume-off", "microphone", "script-text", "play", "play-circle", "palette",
    "movie-open", "sofa", "bed", "sleep", "car", "car-electric", "walk", "exit-run", "water", "water-pump",
    "sprinkler", "sprinkler-variant", "flower", "washing-machine", "television", "speaker", "robot-vacuum",
    "coffee", "gesture-tap-button", "gesture-tap", "checkbox-marked-circle", "cog",
    # added for blocks (DEV-658 B1); all present in @mdi/svg 7.4.47, which the app bundles
    "door-sliding", "door-sliding-lock", "door-sliding-open", "gate-arrow-left", "gate-arrow-right", "boom-gate",
    "boom-gate-up", "garage-variant", "garage-open-variant", "garage-lock", "lock-smart", "lock-check",
    "lock-clock", "lock-alert", "lock-open-check", "account-voice", "microphone-off", "phone-cancel",
    "doorbell-video",
)


class ProfileError(ValueError):
    """A profile that cannot be saved; the message says which item and why."""


class SaveRefused(ProfileError):
    """A save refused with its own wire code (`in_use`) and fields the panel reads (D2). It is a
    ProfileError, so a caller that only knows `invalid` still refuses; `content/save` sends `code` and `fields`."""

    def __init__(self, code: str, message: str, **fields: Any) -> None:
        super().__init__(message)
        self.code = code
        self.fields = fields


class RunRefused(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code)
        self.code = code
        self.message = message


def _label(raw: Any, where: str, *, allow_empty: bool = False) -> str:
    """A trimmed label. `allow_empty` is for a camera, whose picture names itself: an empty or blank label is stored as
    "" (the key stays, the surfaces draw no name); it must still be a string."""
    if not isinstance(raw, str) or (not raw.strip() and not allow_empty):
        raise ProfileError(f"{where}: a label is required")
    label = raw.strip()
    if len(label) > MAX_LABEL:
        raise ProfileError(f"{where}: the label is longer than {MAX_LABEL} characters")
    return label


def _item_id(raw: Any, where: str, seen: set[str]) -> str:
    if not isinstance(raw, str) or not ID_RE.match(raw):
        raise ProfileError(f"{where}: the id must be 1-32 characters of a-z, 0-9, _ or -")
    if raw in seen:
        raise ProfileError(f"{where}: the id {raw!r} is used twice")
    seen.add(raw)
    return raw


def _entity(raw: Any, domain: str, where: str) -> str:
    if not isinstance(raw, str) or not ENTITY_RE.match(raw) or raw.split(".", 1)[0] != domain:
        raise ProfileError(f"{where}: a {domain} entity is required")
    return raw


def _call_target(raw: Any, where: str) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise ProfileError(f"{where}: a call button needs a target")
    target_type, target_id = raw.get("type"), raw.get("id")
    if target_type not in TARGET_TYPES:
        raise ProfileError(f"{where}: the call target type must be one of {', '.join(TARGET_TYPES)}")
    if not isinstance(target_id, str) or not target_id or len(target_id) > MAX_TARGET_ID:
        raise ProfileError(f"{where}: the call target needs an id of 1 to {MAX_TARGET_ID} characters")
    return {"type": target_type, "id": target_id}


def _has_usable_icon(icon: Any) -> bool:
    """An icon a button with hidden text can stand on: one of ICON_NAMES, written with or without "mdi:"."""
    return isinstance(icon, str) and icon.removeprefix("mdi:") in ICON_NAMES


def _enum(raw: Any, allowed: tuple[str, ...], what: str, where: str, *, strict: bool, notes: list[str]) -> str | None:
    """`raw` when it is one of `allowed`; None when absent. An unknown value is refused on a save and, on a load
    (`strict=False`), read as absent and noted (a value a later build wrote must never drop the page)."""
    if raw is None:
        return None
    if isinstance(raw, str) and raw in allowed:
        return raw
    if strict:
        raise ProfileError(f"{where}: the {what} must be one of {', '.join(allowed)}")
    notes.append(f"{where}: unknown {what} {raw!r}")
    return None


def _camera_ref(item: dict[str, Any], where: str, seen: set[str], *, strict: bool, notes: list[str]) -> dict[str, Any]:
    """A camera that is a reference to an element: `{id, element, label?, size?, live?}` and nothing else (the element
    holds the entity; only the per-screen overrides are kept here, and only when present). `content_elements` expands it."""
    element_id = item.get(REF_KEY)
    if not is_element_id(element_id):
        raise ProfileError(f"{where}: the element must be an element id (el- and 8 hex digits)")
    out: dict[str, Any] = {"id": _item_id(item.get("id"), where, seen), REF_KEY: element_id}
    if "label" in item:
        out["label"] = _label(item["label"], where, allow_empty=True)
    size = _enum(item.get("size"), CAMERA_SIZES, "size", where, strict=strict, notes=notes)
    if size is not None:
        out["size"] = size
    live = item.get("live")
    if live is not None:
        if isinstance(live, bool):
            if live:
                out["live"] = True
        elif strict:
            raise ProfileError(f"{where}: live must be true or false")
        else:
            notes.append(f"{where}: unknown live value {live!r}")
    return out


def _action_ref(item: dict[str, Any], where: str, seen: set[str], *, strict: bool) -> dict[str, Any]:
    """A button that is a reference to an element: `{id, element, label?, show_label?, confirm?}` and nothing else. The
    element holds the action, the entity or target, the icon and the colour (read at every expansion, so at every run).
    `confirm` is kept as sent so that the save can REFUSE a `false` on a door, lock or gate action; expansion never
    honours a `false` (it can only tighten: `content_elements.expand_action`)."""
    element_id = item.get(REF_KEY)
    if not is_element_id(element_id):
        raise ProfileError(f"{where}: the element must be an element id (el- and 8 hex digits)")
    out: dict[str, Any] = {"id": _item_id(item.get("id"), where, seen), REF_KEY: element_id}
    if "label" in item:
        out["label"] = _label(item["label"], where)
    for key, what in (("show_label", "show_label"), ("confirm", "confirm")):
        if key in item:
            if not isinstance(item[key], bool):
                raise ProfileError(f"{where}: {what} must be true or false")
            out[key] = item[key]
    return out


def _normalize_items(cameras_in: list[Any], actions_in: list[Any], *, seen: set[str], door: str | None,
                     known_target: Callable[[str, str], bool] | None, strict: bool, notes: list[str],
                     prefix: str = "") -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The cameras and actions of one block (or of a flat v1/v3 profile, `door` None): ids unique in `seen`, which a
    page shares across its blocks. `strict=False` is the load: it only relaxes what a later build may have written
    (unknown `size`/`color`, `show_label` without an icon, `talk` without a door), never an id, a kind or a target."""
    cameras: list[dict[str, Any]] = []
    for n, item in enumerate(cameras_in):
        where = f"{prefix}camera {n + 1}"
        if not isinstance(item, dict):
            raise ProfileError(f"{where}: must be an object")
        if is_ref(item):
            try:
                cameras.append(_camera_ref(item, where, seen, strict=strict, notes=notes))
            except ProfileError as err:
                if strict:
                    raise
                notes.append(f"{where}: dropped a malformed element reference ({err})")    # on a load only this tile goes
            continue
        out: dict[str, Any] = {
            "id": _item_id(item.get("id"), where, seen),
            "label": _label(item.get("label"), where, allow_empty=True),
            "entity_id": _entity(item.get("entity_id"), "camera", where),
        }
        size = _enum(item.get("size"), CAMERA_SIZES, "size", where, strict=strict, notes=notes)
        if size is not None:
            out["size"] = size
        live = item.get("live")
        if live is not None:
            if isinstance(live, bool):
                if live:
                    out["live"] = True      # stored only when true: absent is false, and a document without it is unchanged
            elif strict:
                raise ProfileError(f"{where}: live must be true or false")
            else:
                notes.append(f"{where}: unknown live value {live!r}")    # a later build's value must never drop the page
        cameras.append(out)

    actions: list[dict[str, Any]] = []
    for n, item in enumerate(actions_in):
        where = f"{prefix}action {n + 1}"
        if not isinstance(item, dict):
            raise ProfileError(f"{where}: must be an object")
        if is_ref(item):
            try:
                actions.append(_action_ref(item, where, seen, strict=strict))
            except ProfileError as err:
                if strict:
                    raise
                notes.append(f"{where}: dropped a malformed element reference ({err})")    # on a load only this button goes
            continue
        kind = item.get("kind")
        if kind not in KINDS:
            raise ProfileError(f"{where}: unknown kind {kind!r}")
        domain, _service, confirm_default = KINDS[kind]
        out = {
            "id": _item_id(item.get("id"), where, seen),
            "label": _label(item.get("label"), where),
            "kind": kind,
        }
        if kind == "call":
            target = _call_target(item.get("target"), where)
            if known_target is not None and not known_target(target["type"], target["id"]):
                raise ProfileError(f"{where}: there is nothing called {target['id']!r} to call ({target['type']})")
            out["target"] = target
        elif kind == "talk":
            own_door = item.get("door")       # a Talk ELEMENT's explicit target (DEV-518): wins over the block's door
            if own_door is not None:
                if not isinstance(own_door, str) or not own_door or len(own_door) > 128:
                    raise ProfileError(f"{where}: the door must be a Portico device_id of 1 to 128 characters")
                out["door"] = own_door
            elif door is None and strict:
                raise ProfileError(f"{where}: a talk button needs the block to have a door")
        elif domain is None:
            device_id = item.get("device_id")
            if device_id is None and door is not None:
                pass            # acts on the block's door; `page_actions` writes it in where blocks are read
            elif not isinstance(device_id, str) or not device_id or len(device_id) > 128:
                raise ProfileError(f"{where}: a Portico device_id is required")
            else:
                out["device_id"] = device_id
        else:
            out["entity_id"] = _entity(item.get("entity_id"), domain, where)
        icon = item.get("icon")
        if icon is not None:
            if not isinstance(icon, str) or len(icon) > 64:
                raise ProfileError(f"{where}: the icon must be a short string")
            out["icon"] = icon
        color = _enum(item.get("color"), PALETTE_NAMES, "colour", where, strict=strict, notes=notes)
        if color is not None:
            out["color"] = color
        show_label = item.get("show_label")
        if show_label is not None:
            if not isinstance(show_label, bool):
                raise ProfileError(f"{where}: show_label must be true or false")
            if show_label is False and not _has_usable_icon(out.get("icon")):
                if strict:
                    raise ProfileError(f"{where}: text can be hidden only on a button with an icon from the list")
                notes.append(f"{where}: hidden text without a usable icon")
            else:
                out["show_label"] = show_label
        if kind in KINDS_WITHOUT_CONFIRM:
            out["confirm"] = False  # never asked, never honoured (CB-9); a `confirm: true` sent is ignored
        else:
            confirm = item.get("confirm", confirm_default)
            if not isinstance(confirm, bool):
                raise ProfileError(f"{where}: confirm must be true or false")
            out["confirm"] = confirm
        actions.append(out)
    return cameras, actions


def normalize_profile(raw: Any, *, known_target: Callable[[str, str], bool] | None = None,
                      strict: bool = True) -> dict[str, Any]:
    """Validate and normalise a flat profile `{cameras, actions}` (what a v1/v3 page held, and what a block holds
    besides its id and title). Unknown keys are dropped, an unknown action kind is refused. Raises ProfileError.

    `known_target(type, id)`, when given, must say whether a call button's target exists: an unknown one is
    refused. Loading a stored profile does not pass it (a button whose target vanished stays, shown off).
    A flat profile has no block, so no `door`: a `talk` button or a `portico_open` without a device_id is refused."""
    if not isinstance(raw, dict):
        raise ProfileError("the profile must be an object")
    cameras_in = raw.get("cameras", [])
    actions_in = raw.get("actions", [])
    if not isinstance(cameras_in, list) or not isinstance(actions_in, list):
        raise ProfileError("cameras and actions must be lists")
    if len(cameras_in) > MAX_CAMERAS or len(actions_in) > MAX_ACTIONS:
        raise ProfileError(f"at most {MAX_CAMERAS} cameras and {MAX_ACTIONS} actions")
    cameras, actions = _normalize_items(cameras_in, actions_in, seen=set(), door=None, known_target=known_target,
                                        strict=strict, notes=[])
    return {"cameras": cameras, "actions": actions}


def _block_title(raw: Any, where: str) -> str:
    """A block's title: 1 to MAX_TITLE characters after trimming and, being new, no control, line or paragraph
    separator character (the home-name rule). Raises ProfileError."""
    if not isinstance(raw, str) or not raw.strip():
        raise ProfileError(f"{where}: a title is required")
    title = raw.strip()
    if len(title) > MAX_TITLE:
        raise ProfileError(f"{where}: the title is longer than {MAX_TITLE} characters")
    if any(unicodedata.category(c) in ("Cc", "Zl", "Zp") for c in title):
        raise ProfileError(f"{where}: the title must not contain control characters or line breaks")
    return title


def title_from_label(label: Any, page_id: str) -> str:
    """The title of the block a page's label becomes: the label with every control, line or paragraph separator
    character turned into a space (a label never had the rule a title has), trimmed and cut to MAX_TITLE; the page's
    default label when nothing is left. A label without such characters becomes the title unchanged."""
    text = label if isinstance(label, str) else ""
    text = "".join(" " if unicodedata.category(c) in ("Cc", "Zl", "Zp") else c for c in text).strip()[:MAX_TITLE].strip()
    return text or default_page_label(page_id)[:MAX_TITLE]


def normalize_block(raw: Any, seen_blocks: set[str], seen_items: set[str], *, page_id: str = "",
                    known_target: Callable[[str, str], bool] | None = None, strict: bool = True,
                    notes: list[str] | None = None, position: int = 1) -> dict[str, Any]:
    """One validated block `{id, title, door?, cameras, actions}`. `seen_blocks` (this page's block ids) and
    `seen_items` (this page's camera and action ids, shared by all its blocks) make a repeat an error: block ids
    and item ids are separate namespaces. `strict=False` is the load (see `_normalize_items`). Raises ProfileError."""
    notes = [] if notes is None else notes
    if not isinstance(raw, dict):
        raise ProfileError("a block must be an object")
    block_id = raw.get("id")
    if not isinstance(block_id, str) or not ID_RE.match(block_id) or block_id in seen_blocks:
        raise ProfileError(f"block {position}: a block needs a unique id of 1-32 characters of a-z, 0-9, _ or -")
    where = f"block {block_id!r}"
    title = raw.get("title")
    if strict:
        title = _block_title(title, where)
    elif not isinstance(title, str) or not title.strip():
        raise ProfileError(f"{where}: a title is required")
    else:
        title = title.strip()
    out: dict[str, Any] = {"id": block_id, "title": title}
    door = raw.get("door")
    if door is not None:
        if not isinstance(door, str) or not door or len(door) > 128:
            raise ProfileError(f"{where}: the door must be a Portico device_id of 1 to 128 characters")
        out["door"] = door
    cameras_in, actions_in = raw.get("cameras", []), raw.get("actions", [])
    if not isinstance(cameras_in, list) or not isinstance(actions_in, list):
        raise ProfileError(f"{where}: cameras and actions must be lists")
    if strict and (len(cameras_in) > MAX_BLOCK_CAMERAS or len(actions_in) > MAX_BLOCK_ACTIONS):
        raise ProfileError(f"{where}: at most {MAX_BLOCK_CAMERAS} cameras and {MAX_BLOCK_ACTIONS} buttons in a block")
    out["cameras"], out["actions"] = _normalize_items(
        cameras_in, actions_in, seen=seen_items, door=out.get("door"), known_target=known_target, strict=strict,
        notes=notes, prefix=f"{where}: ")
    seen_blocks.add(block_id)
    return out


def normalize_home_name(raw: Any) -> str:
    """The install's name for the home ("My Home"): a short plain string, trimmed, 0 to MAX_HOME_NAME
    characters; "" (or None) means "not set". Control characters (newline, tab, NUL, DEL, the Unicode line
    and paragraph separators) are refused, not stripped. Raises ProfileError."""
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise ProfileError("the home name must be text")
    name = raw.strip()
    if len(name) > MAX_HOME_NAME:
        raise ProfileError(f"the home name is longer than {MAX_HOME_NAME} characters")
    if any(unicodedata.category(c) in ("Cc", "Zl", "Zp") for c in name):
        raise ProfileError("the home name must not contain control characters or line breaks")
    return name


def profile_rev(profile: dict[str, Any]) -> str:
    """A short hash of the profile, so a client can tell whether its cache is current."""
    blob = json.dumps(profile, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


EMPTY_PROFILE: dict[str, Any] = {"cameras": [], "actions": []}


def _empty_template() -> dict[str, Any]:
    return {"pages": [], "rules": [], "roster": [ROSTER_ALL]}


def _empty_override() -> dict[str, Any]:
    return {"pages": [], "rules": [], "roster_hide": []}


def default_page_label(page_id: str) -> str:
    """A page's label when none is stored: its id, title-cased ("garden-lights" -> "Garden Lights")."""
    return page_id.replace("-", " ").replace("_", " ").title()


def migrate_v1_to_v2(data: dict[str, Any], phone_endpoints: dict[str, str]) -> dict[str, Any]:
    """A v1 file's data (`profiles` keyed by HA user id) as v2 data. Pure: writes nothing.

    `phone_endpoints` maps an enrolled phone's HA user id to its endpoint id. Each profile becomes the
    "home" page of the override for the phone whose user id matches. A profile whose user is no enrolled
    phone (an orphan: the phone was removed or re-enrolled) is KEPT as an override keyed by its old user id
    (M6 E1, R6-02; v2's own migration dropped it, which is how a re-enrolled phone lost its Home): it shows
    under "Not attached" until an administrator moves or deletes it. The template is empty and `home_name`
    is carried over. Profiles are copied as they are; the loader validates them."""
    overrides: dict[str, Any] = {}
    profiles = data.get("profiles")
    for user_id, profile in (profiles.items() if isinstance(profiles, dict) else ()):
        endpoint_id = phone_endpoints.get(user_id)
        if endpoint_id is None:
            _LOGGER.warning("portico content: kept the stored profile of user %s as a 'Not attached' override "
                            "(no enrolled phone has that user)", user_id)
            endpoint_id = user_id
        override = _empty_override()
        override["pages"] = [{"id": HOME_PAGE, **profile}] if isinstance(profile, dict) else [{"id": HOME_PAGE}]
        overrides[endpoint_id] = override
    out: dict[str, Any] = {"schema_version": 2, "template": {"pages": [], "rules": [], "roster": []},
                           "overrides": overrides, "dashboards": {}}
    if "home_name" in data:
        out["home_name"] = data["home_name"]
    return out


def _with_labels(pages: Any) -> Any:
    """`pages` with a default `label` on each page that has none; nothing else touched, nothing dropped."""
    if not isinstance(pages, list):
        return pages
    out = []
    for page in pages:
        if isinstance(page, dict) and isinstance(page.get("id"), str) and "label" not in page:
            page = {"id": page["id"], "label": default_page_label(page["id"]),
                    **{k: v for k, v in page.items() if k != "id"}}
        out.append(page)
    return out


def _valid_rules(raw: Any, where: str) -> list[Any]:
    """The rules of `raw` that validate, as they were stored; one warning for the ones that do not."""
    if not isinstance(raw, list):
        return []
    kept, bad = [], 0
    for rule in raw:
        try:
            normalize_rule(rule)
            kept.append(rule)
        except ProfileError:
            bad += 1
    if bad:
        _LOGGER.warning("portico content: dropped %d stored rule(s) that do not validate (%s)", bad, where)
    return kept


def migrate_v2_to_v3(data: dict[str, Any]) -> dict[str, Any]:
    """A v2 file's data as v3 data. Pure: writes nothing. Drops NO override, page or dashboard.

    `label` is defaulted on every page; `endpoints: {}` is added; a stored rule that does not validate is
    dropped with one warning (v2 stored rules and roster but nothing wrote them); an empty roster `[]` (v2's
    "not set") becomes `["*"]`, because after v3 `[]` means nobody (R6-05), and a roster that does not
    validate becomes `["*"]` with a warning. Overrides are copied as they are, orphans included (R6-02).
    A dashboard and `home_name` are carried over untouched."""
    template = data.get("template") if isinstance(data.get("template"), dict) else {}
    try:
        roster = normalize_roster(template.get("roster")) or [ROSTER_ALL]
    except ProfileError as err:
        _LOGGER.warning("portico content: the stored roster does not validate (%s); using everyone", err)
        roster = [ROSTER_ALL]
    overrides: dict[str, Any] = {}
    raw_overrides = data.get("overrides")
    for key, override in (raw_overrides.items() if isinstance(raw_overrides, dict) else ()):
        if isinstance(override, dict):
            override = {**override, "pages": _with_labels(override.get("pages")),
                        "rules": _valid_rules(override.get("rules"), f"override {key}")}
        overrides[key] = override
    out: dict[str, Any] = {
        "schema_version": 3,
        "template": {"pages": _with_labels(template.get("pages")),
                     "rules": _valid_rules(template.get("rules"), "template"), "roster": roster},
        "overrides": overrides,
        "dashboards": copy.deepcopy(data.get("dashboards")) if isinstance(data.get("dashboards"), dict) else {},
        "endpoints": {},
    }
    if "home_name" in data:
        out["home_name"] = data["home_name"]
    return out


def _wrap_page(page: dict[str, Any]) -> dict[str, Any]:
    """A v3 page `{id, label, cameras, actions}` as a v4 page: ONE block, id = the page id, title = the page label
    (`title_from_label`), the page's own cameras and actions lists untouched. Keys other than the two lists stay."""
    page_id = page["id"]
    label = page.get("label")
    block = {"id": page_id, "title": title_from_label(label if label is not None else default_page_label(page_id),
                                                       page_id),
             "cameras": page.get("cameras", []), "actions": page.get("actions", [])}
    return {**{k: v for k, v in page.items() if k not in ("cameras", "actions")}, "blocks": [block]}


def _wrap_pages(pages: Any) -> Any:
    """`pages` with every page that has an id and no `blocks` wrapped into one block; anything else (a page without
    an id, a non-object) is left as it is for the loader to deal with exactly as before."""
    if not isinstance(pages, list):
        return pages
    return [_wrap_page(p) if isinstance(p, dict) and isinstance(p.get("id"), str) and "blocks" not in p else p
            for p in pages]


def migrate_v3_to_v4(data: dict[str, Any]) -> dict[str, Any]:
    """A v3 file's data as v4 data. Pure: writes nothing, mutates nothing. Drops NOTHING.

    Every page of `template.pages` and of every `overrides[*].pages` (a "Not attached" override included) becomes one
    block: `{id: <page id>, title: <page label>, cameras, actions}` with the page's lists untouched, so every item
    keeps its id and every field (ids stay unique per page, decision D2). `size` is unset (= auto), there is no
    door. Rules, roster, `roster_hide`, dashboards, `endpoints` and `home_name` are carried over untouched. An
    override page that replaces a template page by id still does (merging stays page level). A page the loader
    would drop (no id, not an object) is carried as it is and dropped by the loader as before."""
    out = copy.deepcopy(data)
    out["schema_version"] = 4
    template = out.get("template")
    if isinstance(template, dict):
        template["pages"] = _wrap_pages(template.get("pages"))
    overrides = out.get("overrides")
    for override in (overrides.values() if isinstance(overrides, dict) else ()):
        if isinstance(override, dict) and "pages" in override:
            override["pages"] = _wrap_pages(override["pages"])
    return out


def normalize_page(raw: Any, seen: set[str] | None = None, *,
                   known_target: Callable[[str, str], bool] | None = None, strict: bool = True,
                   where: str = "") -> dict[str, Any]:
    """One validated page `{id, label, blocks}`. `label` defaults to the id, title-cased. `seen` (page ids so far)
    makes a repeated id an error; `known_target` is `normalize_profile`'s.

    A page with no `blocks` key is a schema-3 page: its `cameras` and `actions` are wrapped into one block as
    `migrate_v3_to_v4` does, so nothing a v3 writer sent is ever read as an empty page. A page with `blocks` ignores
    any flat `cameras`/`actions` beside it (a file an older build wrote carries them; they are never read). The
    page's cameras and actions together stay inside MAX_CAMERAS / MAX_ACTIONS; the rest of the block limits, the unique block titles and the unknown-value rules are the save's (`strict=True`).
    Raises ProfileError."""
    if not isinstance(raw, dict):
        raise ProfileError("a page must be an object")
    page_id = raw.get("id")
    if not isinstance(page_id, str) or not ID_RE.match(page_id) or (seen is not None and page_id in seen):
        raise ProfileError("a page needs a unique id of 1-32 characters of a-z, 0-9, _ or -")
    label = raw.get("label")
    label = default_page_label(page_id) if label is None else _label(label, f"page {page_id}")
    blocks_in = raw["blocks"] if "blocks" in raw else _wrap_page({**raw, "label": label})["blocks"]
    if not isinstance(blocks_in, list):
        raise ProfileError(f"page {page_id}: blocks must be a list")
    if strict and len(blocks_in) > MAX_BLOCKS:
        raise ProfileError(f"page {page_id}: at most {MAX_BLOCKS} blocks")
    notes: list[str] = []
    seen_blocks: set[str] = set()
    seen_items: set[str] = set()
    blocks = [normalize_block(b, seen_blocks, seen_items, page_id=page_id, known_target=known_target, strict=strict,
                              notes=notes, position=n + 1) for n, b in enumerate(blocks_in)]
    cameras = sum(len(b["cameras"]) for b in blocks)
    actions = sum(len(b["actions"]) for b in blocks)
    if cameras > MAX_CAMERAS or actions > MAX_ACTIONS:
        raise ProfileError(f"page {page_id}: at most {MAX_CAMERAS} cameras and {MAX_ACTIONS} actions on a page")
    if strict:
        titles: set[str] = set()
        for block in blocks:
            folded = block["title"].casefold()
            if folded in titles:
                raise ProfileError(f"page {page_id}: two blocks are called {block['title']!r}")
            titles.add(folded)
    if notes:
        _LOGGER.warning("portico content: page %s%s was read with %d value(s) it does not know (%s)", page_id,
                        f" ({where})" if where else "", len(notes), "; ".join(notes[:3]))
    if seen is not None:
        seen.add(page_id)
    return {"id": page_id, "label": label, "blocks": blocks}


def normalize_pages(raw: Any, where: str = "pages", *,
                    known_target: Callable[[str, str], bool] | None = None) -> list[dict[str, Any]]:
    """The strict form (a save): every page must validate, at most MAX_PAGES. Raises ProfileError."""
    if not isinstance(raw, list):
        raise ProfileError(f"{where}: pages must be a list")
    if len(raw) > MAX_PAGES:
        raise ProfileError(f"{where}: at most {MAX_PAGES} pages")
    seen: set[str] = set()
    return [normalize_page(page, seen, known_target=known_target) for page in raw]


def _normalize_pages(raw: Any, where: str) -> list[dict[str, Any]]:
    """The lenient form (a load): validated pages `[{id, label, blocks}]`; an invalid page is dropped with a
    warning."""
    pages: list[dict[str, Any]] = []
    seen: set[str] = set()
    for page in raw if isinstance(raw, list) else ():
        try:
            pages.append(normalize_page(page, seen, strict=False, where=where))
        except ProfileError as err:
            # A hand-edited or older file must not take the others down.
            _LOGGER.warning("portico content: dropped an invalid stored profile (%s: %s)", where, err)
    return pages


RULE_KINDS = ("kind:door", "kind:phone", "kind:dashboard")


def _camera_entities(raw: Any, where: str) -> list[str]:
    """A list of camera entity ids; an entry may instead be an element id (`el-xxxxxxxx`: a camera element, expanded to its
    entity where the list is read). The two grammars cannot collide: an entity id contains a dot."""
    if not isinstance(raw, list) or len(raw) > MAX_CAMERAS:
        raise ProfileError(f"{where}: cameras must be a list of at most {MAX_CAMERAS} camera entities or elements")
    return [c if is_element_id(c) else _entity(c, "camera", where) for c in raw]


def _button_ids(raw: Any, where: str) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_RULE_BUTTONS:
        raise ProfileError(f"{where}: must be a list of at most {MAX_RULE_BUTTONS} action ids")
    for button in raw:
        if not isinstance(button, str) or not ID_RE.match(button):
            raise ProfileError(f"{where}: {button!r} is not an action id")
    return list(dict.fromkeys(raw))


def normalize_rule(raw: Any) -> dict[str, Any]:
    """One rule `{id, caller, cameras, ring_buttons, call_buttons}` (design 2.4). `caller` is an endpoint id or
    one of RULE_KINDS; `cameras` is a list of camera entity ids or None ("the caller's own declaration").
    That the button ids exist in a page set is checked where the page set is known (`check_rule_buttons`).
    Raises ProfileError naming the rule."""
    if not isinstance(raw, dict):
        raise ProfileError("a rule must be an object")
    rule_id = raw.get("id")
    where = f"rule {rule_id!r}"
    if not isinstance(rule_id, str) or not ID_RE.match(rule_id):
        raise ProfileError("a rule needs an id of 1-32 characters of a-z, 0-9, _ or -")
    caller = raw.get("caller")
    if not isinstance(caller, str) or not caller or len(caller) > MAX_ENDPOINT_ID or caller != caller.strip():
        raise ProfileError(f"{where}: a caller is required (an endpoint id, or one of {', '.join(RULE_KINDS)})")
    if caller.startswith("kind:") and caller not in RULE_KINDS:
        raise ProfileError(f"{where}: the caller kind must be one of {', '.join(RULE_KINDS)}")
    cameras = raw.get("cameras")
    return {"id": rule_id, "caller": caller,
            "cameras": None if cameras is None else _camera_entities(cameras, where),
            "ring_buttons": _button_ids(raw.get("ring_buttons"), f"{where}: ring_buttons"),
            "call_buttons": _button_ids(raw.get("call_buttons"), f"{where}: call_buttons")}


def normalize_rules(raw: Any, where: str = "rules") -> list[dict[str, Any]]:
    """The strict form: every rule validates, ids unique, at most MAX_RULES. Raises ProfileError."""
    if not isinstance(raw, list):
        raise ProfileError(f"{where}: rules must be a list")
    if len(raw) > MAX_RULES:
        raise ProfileError(f"{where}: at most {MAX_RULES} rules")
    rules = [normalize_rule(rule) for rule in raw]
    ids = [r["id"] for r in rules]
    if len(set(ids)) != len(ids):
        raise ProfileError(f"{where}: a rule id is used twice")
    return rules


def duplicate_action_ids(document: dict[str, Any]) -> set[tuple[str, str]]:
    """`{(scope, action id)}` for every action id used twice on the pages one endpoint gets together: the template's pages
    for `template`, the template's with the override's laid over them for an override key. Ids are only required unique PER
    PAGE, but `content/run` names an action by id alone, so two pages sharing one is ambiguous (see `ContentStore.async_run`).
    For a schema 6 document: per device, two DIFFERENT buttons sharing an id (`pages_model.duplicate_action_ids_v6`)."""
    if is_pages_model(document):
        from .pages_model import duplicate_action_ids_v6

        return duplicate_action_ids_v6(document)
    template = document.get("template") or {}

    def clashes(pages: list[Any]) -> set[str]:
        seen: set[str] = set()
        out: set[str] = set()
        for page in pages:
            for block in (page.get("blocks") or ()) if isinstance(page, dict) else ():
                for item in (block.get("actions") or ()) if isinstance(block, dict) else ():
                    if isinstance(item, dict) and isinstance(item.get("id"), str):
                        (out if item["id"] in seen else seen).add(item["id"])
        return out

    found = {("template", i) for i in clashes(template.get("pages") or [])}
    for key, override in (document.get("overrides") or {}).items():
        merged = {p.get("id"): p for p in template.get("pages") or [] if isinstance(p, dict)}
        for page in (override or {}).get("pages") or []:
            if isinstance(page, dict):
                merged[page.get("id")] = page
        found |= {(key, i) for i in clashes(list(merged.values()))}
    return found


def check_rule_buttons(rules: list[dict[str, Any]], pages: list[dict[str, Any]], where: str) -> None:
    """Every `ring_buttons`/`call_buttons` id must be an action of some block of some page in `pages`, and not a
    `talk` button (a ring is not the moment to start a talk). Raises ProfileError naming the rule and the id."""
    kinds = {a["id"]: a.get("kind") for p in pages for a in page_actions(p)}     # a reference has no kind here: checked on save
    for rule in rules:
        for field in ("ring_buttons", "call_buttons"):
            for button in rule[field]:
                if button not in kinds:
                    raise ProfileError(f"{where}: rule {rule['id']!r} {field} names {button!r}, "
                                       "which is not a button on any of its pages")
                if kinds[button] == "talk":
                    raise ProfileError(f"{where}: rule {rule['id']!r} {field} names {button!r}, a talk button; "
                                       "a ring is not the moment to start a talk")


def _lenient_rules(raw: Any, where: str) -> list[dict[str, Any]]:
    rules: list[dict[str, Any]] = []
    for rule in raw if isinstance(raw, list) else ():
        try:
            normalized = normalize_rule(rule)
        except ProfileError as err:
            _LOGGER.warning("portico content: dropped an invalid stored rule (%s: %s)", where, err)
            continue
        if any(r["id"] == normalized["id"] for r in rules):
            _LOGGER.warning("portico content: dropped a stored rule with a repeated id (%s: %s)", where, normalized["id"])
            continue
        rules.append(normalized)
    return rules


def normalize_roster(raw: Any) -> list[str]:
    """The template roster: an ordered list of endpoint ids, or exactly `["*"]` (every endpoint); `[]` is
    nobody. Raises ProfileError."""
    if not isinstance(raw, list):
        raise ProfileError("the roster must be a list")
    if len(raw) > MAX_ROSTER:
        raise ProfileError(f"the roster has more than {MAX_ROSTER} entries")
    for entry in raw:
        if not isinstance(entry, str) or not entry or len(entry) > MAX_ENDPOINT_ID:
            raise ProfileError(f"the roster entry {entry!r} is not an endpoint id")
    if len(set(raw)) != len(raw):
        raise ProfileError("the roster names an endpoint twice")
    if ROSTER_ALL in raw and raw != [ROSTER_ALL]:
        raise ProfileError(f"{ROSTER_ALL!r} stands alone in the roster")
    return list(raw)


def normalize_endpoint_declaration(raw: Any) -> dict[str, Any]:
    """A phone's declaration `{cameras: [camera entity ids]}`: what a callee sees when this phone calls."""
    if not isinstance(raw, dict):
        raise ProfileError("an endpoint declaration must be an object")
    return {"cameras": _camera_entities(raw.get("cameras", []), "endpoint declaration")}


def _normalize_dashboard(raw: Any) -> dict[str, Any]:
    """A dashboard declaration `{name, ha_user_id, room?, cameras, override?}`; `ha_user_id` is required (it
    is how the kiosk's user is recognised). `override` is reserved and unread."""
    if not isinstance(raw, dict):
        raise ProfileError("a dashboard must be an object")
    out: dict[str, Any] = {}
    for key in ("name", "ha_user_id", "room", "override"):
        if isinstance(raw.get(key), str):
            out[key] = raw[key]
    if not out.get("ha_user_id"):
        raise ProfileError("a dashboard needs the HA user it is logged in as (ha_user_id)")
    cameras = raw.get("cameras")
    out["cameras"] = _camera_entities(cameras, "dashboard") if cameras is not None else []
    return out


class SaveConflict(Exception):
    """`content/save` was based on a document that is no longer the stored one (another session saved first)."""


DOCUMENT_KEYS = frozenset({"schema_version", "home_name", "show_fps", "template", "overrides", "dashboards", "endpoints"})
TEMPLATE_KEYS = frozenset({"pages", "rules", "roster"})
OVERRIDE_KEYS = frozenset({"pages", "rules", "roster_hide"})


def _only_keys(raw: dict[str, Any], allowed: frozenset[str], where: str) -> None:
    unknown = sorted(str(k) for k in raw if k not in allowed)
    if unknown:
        raise ProfileError(f"{where}: unknown field {unknown[0]!r}")


def _endpoint_key(key: Any, where: str) -> str:
    if not isinstance(key, str) or not key or len(key) > MAX_ENDPOINT_ID or key != key.strip():
        raise ProfileError(f"{where}: {key!r} is not an endpoint id")
    return key


def normalize_document(raw: Any, *, known_target: Callable[[str, str], bool] | None = None,
                       app_screens: Collection[str] = ()) -> dict[str, Any]:
    """The whole stored document, validated strictly (M6 E2, `content/save` and `content/preview`): the same
    per-record validators a load uses, but nothing is dropped and nothing is guessed. An unknown field anywhere
    in the structure is refused, not lost silently; every rule's buttons must exist in the page set it applies
    to (the template's, plus the override's own for an override rule). Pure. Raises ProfileError naming the item.

    `schema_version`, when present, must be 4 or 5 (5 = may hold element references); the result is stamped 5 only when
    it holds at least one reference, else 4. A document that says 3 is a tab left open across the
    restart of the build that introduced blocks, refused so the editor reloads. A page with no `blocks` is wrapped
    into one block (`normalize_page`). The result is exactly what `ContentStore._data()` would hold, so a document
    that was not edited normalises to itself (that is how a no-op save is a no-op)."""
    if not isinstance(raw, dict):
        raise ProfileError("the document must be an object")
    from .pages_model import declares_pages_model, normalize_document_v6

    if declares_pages_model(raw):
        return normalize_document_v6(raw, known_target=known_target, app_screens=app_screens)
    _only_keys(raw, DOCUMENT_KEYS, "document")
    if raw.get("schema_version", SCHEMA_PLAIN) not in ACCEPTED_SCHEMAS:
        raise ProfileError(f"document: schema_version must be {SCHEMA_PLAIN} or {SCHEMA_REFS}; reload the Portico page")

    dashboards_in = raw.get("dashboards", {})
    if not isinstance(dashboards_in, dict):
        raise ProfileError("dashboards must be an object")
    dashboards: dict[str, dict[str, Any]] = {}
    for slug, dashboard in dashboards_in.items():
        if not isinstance(slug, str) or not ID_RE.match(slug):
            raise ProfileError(f"dashboard {slug!r}: the slug must be 1-32 characters of a-z, 0-9, _ or -")
        try:
            dashboards[slug] = _normalize_dashboard(dashboard)
        except ProfileError as err:
            raise ProfileError(f"dashboard {slug!r}: {err}") from None
        for field in ("name", "room"):
            if len(dashboards[slug].get(field, "")) > MAX_LABEL:
                raise ProfileError(f"dashboard {slug!r}: the {field} is longer than {MAX_LABEL} characters")

    def target_known(target_type: str, target_id: str) -> bool:
        # a screen declared in this same document can be called in it (never the app screen of a panel: DEV-807)
        return (target_type == "dashboard" and target_id in dashboards and target_id not in app_screens) or (
            known_target is None or known_target(target_type, target_id))

    template_in = raw.get("template", {})
    if not isinstance(template_in, dict):
        raise ProfileError("the template must be an object")
    _only_keys(template_in, TEMPLATE_KEYS, "template")
    template_pages = normalize_pages(template_in.get("pages", []), "template pages", known_target=target_known)
    template_rules = normalize_rules(template_in.get("rules", []), "template rules")
    check_rule_buttons(template_rules, template_pages, "template")
    template = {"pages": template_pages, "rules": template_rules,
                "roster": normalize_roster(template_in.get("roster", [ROSTER_ALL]))}

    overrides_in = raw.get("overrides", {})
    if not isinstance(overrides_in, dict):
        raise ProfileError("overrides must be an object")
    overrides: dict[str, dict[str, Any]] = {}
    for key, override in overrides_in.items():
        where = f"override {key!r}"
        _endpoint_key(key, "overrides")
        if not isinstance(override, dict):
            raise ProfileError(f"{where}: must be an object")
        _only_keys(override, OVERRIDE_KEYS, where)
        pages = normalize_pages(override.get("pages", []), f"{where} pages", known_target=target_known)
        rules = normalize_rules(override.get("rules", []), f"{where} rules")
        check_rule_buttons(rules, [*template_pages, *pages], where)
        hide = override.get("roster_hide", [])
        if (not isinstance(hide, list) or len(hide) > MAX_ROSTER
                or any(not isinstance(h, str) or not h or len(h) > MAX_ENDPOINT_ID for h in hide)):
            raise ProfileError(f"{where}: roster_hide must be a list of endpoint ids")
        overrides[key] = {"pages": pages, "rules": rules, "roster_hide": list(dict.fromkeys(hide))}

    declarations_in = raw.get("endpoints", {})
    if not isinstance(declarations_in, dict):
        raise ProfileError("endpoints must be an object")
    declarations: dict[str, dict[str, Any]] = {}
    for key, declaration in declarations_in.items():
        _endpoint_key(key, "endpoints")
        declarations[key] = normalize_endpoint_declaration(declaration)

    out: dict[str, Any] = {"schema_version": SCHEMA_PLAIN, "template": template, "overrides": overrides,
                           "dashboards": dashboards, "endpoints": declarations}
    if has_refs(out):
        out["schema_version"] = SCHEMA_REFS     # stamped 5 only when a reference exists (design 3.2)
    home_name = normalize_home_name(raw.get("home_name"))
    if home_name:
        out["home_name"] = home_name
    if "show_fps" in raw:
        show_fps = raw["show_fps"]
        if not isinstance(show_fps, bool):
            raise ProfileError("show_fps must be true or false")
        if show_fps:
            out["show_fps"] = True      # stored only when true: absent is false, and a document without it is unchanged
    return out


def refuse_flat_pages(raw: Any) -> None:
    """Refuse a document (sent to `content/save` or `content/preview`) in which a page carries flat `cameras` or
    `actions`. Since B8 the server neither serves nor stores the flat form, so a client that sends one is an editor
    from before blocks (the panel from before B3, a tab open across the restart): its edit would be dropped without a
    word, so it is refused with the one thing the person can do. A page with `blocks` and no flat key passes; a
    malformed document is left for `normalize_document` to refuse. Pure. Raises ProfileError."""
    if not isinstance(raw, dict):
        return
    if "pages" in raw or "devices" in raw:
        from .pages_model import refuse_flat_pages_v6

        refuse_flat_pages_v6(raw)
        return
    template = raw.get("template")
    groups: list[tuple[str, Any]] = []
    if isinstance(template, dict):
        groups.append(("template", template.get("pages")))
    overrides = raw.get("overrides")
    if isinstance(overrides, dict):
        groups.extend((f"override {key!r}", o.get("pages")) for key, o in overrides.items() if isinstance(o, dict))
    for where, pages in groups:
        for page in pages if isinstance(pages, list) else ():
            if isinstance(page, dict) and ("cameras" in page or "actions" in page):
                raise ProfileError(f"{where} page {page.get('id')!r} carries flat cameras/actions, which this version of "
                                   "Portico no longer takes; update the Portico panel to edit it")


def _home(pages: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The "home" page as a flat profile, EVERY kind of every block: what the Configure flow edits."""
    page = next((p for p in pages if p["id"] == HOME_PAGE), None)
    return None if page is None else {"cameras": page_cameras(page), "actions": page_actions(page)}


def _fresh_id(wanted: str, taken: set[str]) -> str:
    n = 2
    while True:
        candidate = f"{wanted[:28]}-{n}"
        if candidate not in taken:
            return candidate
        n += 1


def _merge_moved(orphan: dict[str, Any], pages: list[dict[str, Any]],
                 rules: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """`pages`/`rules` with an orphan override's pages and rules added after them. Nothing of the target is
    replaced: an id already taken gets a fresh one (`home-2`), a moved page keeping its tiles and, then, a
    label that says where it came from. The result is checked against the MAX_PAGES / MAX_RULES limits."""
    out_pages, taken = list(pages), {p["id"] for p in pages}
    for page in orphan["pages"]:
        if page["id"] in taken:
            fresh = _fresh_id(page["id"], taken)
            page = {**page, "id": fresh, "label": f"{page['label']} (moved)"[:MAX_LABEL]}
        taken.add(page["id"])
        out_pages.append(page)
    out_rules, taken_rules = list(rules), {r["id"] for r in rules}
    for rule in orphan["rules"]:
        if rule["id"] in taken_rules:
            rule = {**rule, "id": _fresh_id(rule["id"], taken_rules)}
        taken_rules.add(rule["id"])
        out_rules.append(rule)
    if len(out_pages) > MAX_PAGES or len(out_rules) > MAX_RULES:
        raise ProfileError(f"moving it would pass the limit of {MAX_PAGES} pages and {MAX_RULES} rules")
    return out_pages, out_rules


class ContentStore:
    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._store: Store[dict] = Store(hass, 1, STORE_KEY, atomic_writes=True)
        self._template: dict[str, Any] = _empty_template()
        self._overrides: dict[str, dict[str, Any]] = {}     # endpoint id -> {pages, rules, roster_hide}
        self._dashboards: dict[str, dict[str, Any]] = {}    # slug -> declaration (written by the M6 editor)
        self._endpoint_decls: dict[str, dict[str, Any]] = {}   # phone endpoint id -> {cameras} (v3)
        # The unified Pages model (schema 6). `_model6` is true only for a store whose file IS schema 6; until the explicit
        # migration is applied it stays false and the legacy containers above are the document (dormant).
        self._model6 = False
        self._library: list[dict[str, Any]] = []            # pages: content and ring (schema 6)
        self._devices: dict[str, dict[str, Any]] = {}       # endpoint id -> {pages, ring, roster_hide} (schema 6)
        self._roster: list[str] = [ROSTER_ALL]              # schema 6's top-level roster
        self._last_disk: dict[str, Any] | None = None       # the schema 4 or 5 file as last read or written (source of the .v4/.v5 copy)
        # The older file as loaded (version, raw data), until its one-time copy `portico.content.v<N>` is attempted.
        self._premigration: tuple[int, dict[str, Any]] | None = None
        self._loaded = False
        self._elements_ready = False            # the element store loaded and our change listener registered
        self._last_v4: dict[str, Any] | None = None   # the schema-4 file as last read or written (the source of the .v4 copy)
        self._disk_version: int | None = None   # the schema stamp of the file as last read or written (None: no file)
        self._warned_refs: set[str] = set()     # element ids already warned about as unusable (reset on an element edit)
        self._newer_schema: int | None = None   # a stored schema newer than this build: read-only, never rewritten
        self._listeners: list[Callable[[str], None]] = []       # listener(endpoint_id)
        self._home_listeners: list[Callable[[], None]] = []
        self._live_listeners: list[Callable[[], None]] = []     # live state moved, the document rev did not
        self._home_name = ""
        self._show_fps = False
        self._frigate_names: dict[str, str] = {}   # camera entity id -> the Frigate camera name last seen (live_rtsp)
        self._size_asked: dict[str, float] = {}     # camera entity id -> monotonic time its stream size was last asked for
        self._size_pending: set[str] = set()        # entity ids with a size refresh in flight
        self._size_stop_armed = False               # the one HA-stop listener that cancels the self-retries is registered
        self._size_timers: dict[str, Any] = {}      # entity id -> the cancel of its pending self-retry
        self._last_run: dict[tuple[str, str], float] = {}
        # Talk to the door (B7a): the call this store started on a door, (provider domain, door id) -> (endpoint id,
        # call id, monotonic start); and the doors with a dial in flight, so two presses never dial twice.
        self._talk_calls: dict[tuple[str, str], tuple[str, str, float]] = {}
        self._talk_dialing: set[tuple[str, str]] = set()
        self._busy_since: dict[tuple[Any, Any], float] = {}               # (door, endpoint) -> monotonic start of a busy spell (DEV-806)
        self._talk_last: dict[tuple[Any, Any], tuple[Any, Any]] = {}     # (door, endpoint) -> last logged (available, reason)
        self._talk_cause: str | None = None

    async def async_ensure_loaded(self) -> None:
        """Load the file (once) and make sure the element store the references point into is loaded too, with this store
        listening to it (an element edit refreshes the screens). A failure to load elements never blocks content: the
        references then drop out of the resolved view, as a missing element does."""
        await self._load_content()
        if not self._elements_ready:
            from .elements import get_elements_store

            elements = get_elements_store(self._hass)
            try:
                await elements.async_ensure_loaded()
            except Exception:  # noqa: BLE001 - content must load without elements
                _LOGGER.warning("portico content: the elements could not be loaded; references show nothing", exc_info=True)
                return
            elements.add_listener(self._on_elements_changed)
            self._elements_ready = True

    async def _load_content(self) -> None:
        if self._loaded:
            return
        data = await self._store.async_load() or {}
        version = data.get("schema_version", 1)
        self._disk_version = version if data and isinstance(version, int) and not isinstance(version, bool) else None
        self._last_v4 = copy.deepcopy(data) if self._disk_version == SCHEMA_PLAIN else None
        self._last_disk = copy.deepcopy(data) if self._disk_version in (SCHEMA_PLAIN, SCHEMA_REFS) else None
        if not isinstance(version, int) or isinstance(version, bool) or version < 2:
            # The v1 file is converted in memory (v1 -> v2 -> v3 -> v4); nothing is written until the first normal save.
            if data:
                self._premigration = (1, copy.deepcopy(data))
            phones = {ep.ha_user_id: ep.id for ep in reversed(get_endpoint_registry(self._hass).endpoints(kind="phone"))
                      if ep.ha_user_id}
            data = migrate_v3_to_v4(migrate_v2_to_v3(migrate_v1_to_v2(data, phones)))
        elif version == 2:
            self._premigration = (2, copy.deepcopy(data))
            data = migrate_v3_to_v4(migrate_v2_to_v3(data))
        elif version == 3:
            # Converted in memory (every page becomes one block); nothing is written until the first normal save.
            self._premigration = (3, copy.deepcopy(data))
            data = migrate_v3_to_v4(data)
        elif version > SCHEMA_VERSION:
            self._newer_schema = version
            _LOGGER.warning("portico content: the stored file is schema %s, newer than this build knows (%s); "
                            "reading what it can and refusing to write, so the newer file is not downgraded",
                            version, SCHEMA_VERSION)
        elif version == SCHEMA_PAGES:
            self._model6 = True         # this build knows the unified model (a build that stops at 5 reads a 6 as "newer", above)
        if self._model6:
            from .pages_model import load_pages_model

            self._library, self._devices, self._roster = load_pages_model(data)
        else:
            self._load_legacy(data)
        dashboards = data.get("dashboards")
        for slug, dashboard in (dashboards.items() if isinstance(dashboards, dict) else ()):
            try:
                self._dashboards[slug] = _normalize_dashboard(dashboard)
            except ProfileError as err:
                _LOGGER.warning("portico content: dropped an invalid stored dashboard (%s)", err)
        declarations = data.get("endpoints")
        for endpoint_id, declaration in (declarations.items() if isinstance(declarations, dict) else ()):
            try:
                self._endpoint_decls[endpoint_id] = normalize_endpoint_declaration(declaration)
            except ProfileError as err:
                _LOGGER.warning("portico content: dropped an invalid stored endpoint declaration (%s)", err)
        try:
            self._home_name = normalize_home_name(data.get("home_name"))
        except ProfileError as err:
            _LOGGER.warning("portico content: dropped an invalid stored home name (%s)", err)
        show_fps = data.get("show_fps")
        if show_fps is not None and not isinstance(show_fps, bool):
            _LOGGER.warning("portico content: ignored a stored show_fps that is not true or false")     # lenient on load
        self._show_fps = show_fps is True
        self._loaded = True

    def _load_legacy(self, data: dict[str, Any]) -> None:
        """The template and the overrides of a schema 2-5 file (as read, after the in-memory migrations)."""
        template = data.get("template") if isinstance(data.get("template"), dict) else {}
        try:
            roster = normalize_roster(template.get("roster"))
        except ProfileError as err:
            _LOGGER.warning("portico content: the stored roster does not validate (%s); using everyone", err)
            roster = [ROSTER_ALL]
        self._template = {"pages": _normalize_pages(template.get("pages"), "template"),
                          "rules": _lenient_rules(template.get("rules"), "template"), "roster": roster}
        overrides = data.get("overrides")
        for endpoint_id, override in (overrides.items() if isinstance(overrides, dict) else ()):
            if not isinstance(override, dict):
                _LOGGER.warning("portico content: dropped an invalid stored override (not an object)")
                continue
            hide = override.get("roster_hide")
            self._overrides[endpoint_id] = {
                "pages": _normalize_pages(override.get("pages"), "override"),
                "rules": _lenient_rules(override.get("rules"), f"override {endpoint_id}"),
                "roster_hide": [h for h in hide if isinstance(h, str)] if isinstance(hide, list) else [],
            }

    def add_listener(self, listener: Callable[[str], None]) -> None:
        self._listeners.append(listener)

    def remove_listener(self, listener: Callable[[str], None]) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def add_home_listener(self, listener: Callable[[], None]) -> None:
        """Called when something every subscriber must refetch changes: the install-wide home name, the template."""
        self._home_listeners.append(listener)

    def add_live_listener(self, listener: Callable[[], None]) -> None:
        """Called when live state that is not part of the document `rev` changed (Talk's `busy`): a subscriber is told
        to refetch with a rev-less `content_changed`, which the app never skips as "already held"."""
        self._live_listeners.append(listener)

    def remove_live_listener(self, listener: Callable[[], None]) -> None:
        if listener in self._live_listeners:
            self._live_listeners.remove(listener)

    def remove_home_listener(self, listener: Callable[[], None]) -> None:
        if listener in self._home_listeners:
            self._home_listeners.remove(listener)

    @property
    def home_name(self) -> str:
        """The install-wide home name; "" when not set. Call after async_ensure_loaded."""
        return self._home_name

    def _check_writable(self) -> None:
        """Refuse a change while the stored file is a newer schema than this build: saving would rewrite it as v4
        and lose what the newer build added. Raises ProfileError (what every caller already shows or logs)."""
        if self._newer_schema is not None:
            raise ProfileError(f"the stored content is schema {self._newer_schema}, newer than this version of "
                               f"Portico understands ({SCHEMA_VERSION}); update Portico to change it")

    async def _save(self) -> None:
        """Write the v4 file (`_disk_data`: the document, blocks only). The first save over an older file first leaves a
        one-time copy of that file beside it (`.storage/portico.content.v<N>`, N = the version read: 1, 2 or 3) so a
        rollback to the older code has
        something to restore. An existing copy is never overwritten; a copy that cannot be written is logged and
        never blocks the save."""
        self._check_writable()
        older, self._premigration = self._premigration, None
        if older is not None:
            version, raw = older
            try:
                copy_store: Store[dict] = Store(self._hass, 1, f"{STORE_KEY}.v{version}", atomic_writes=True)
                if await copy_store.async_load() is None:
                    await copy_store.async_save(raw)
                    _LOGGER.info("portico content: kept a copy of the v%d file as %s.v%d before the first v%d save",
                                 version, STORE_KEY, version, SCHEMA_PLAIN)
            except Exception as err:  # noqa: BLE001 - the copy is a courtesy, the save is the job
                _LOGGER.warning("portico content: could not keep a copy of the v%d file (%s); saving v%d anyway",
                                version, err, SCHEMA_PLAIN)
        disk = self._disk_data()
        if disk["schema_version"] == SCHEMA_REFS and self._disk_version == SCHEMA_PLAIN:
            await self._keep_v4_copy()
        if disk["schema_version"] == SCHEMA_PAGES and self._disk_version in (SCHEMA_PLAIN, SCHEMA_REFS):
            # Schema 6 is a one-way hard cut: unlike the courtesy copies above, a first 6 is NOT written without the way back.
            await self._keep_safety_copy(self._disk_version)
        await self._store.async_save(disk)
        self._disk_version = disk["schema_version"]
        self._last_v4 = disk if self._disk_version == SCHEMA_PLAIN else None
        self._last_disk = disk if self._disk_version in (SCHEMA_PLAIN, SCHEMA_REFS) else None
        get_endpoint_registry(self._hass).notify()      # a dashboard or a camera declaration may have changed

    async def _keep_v4_copy(self) -> None:
        """The first save that stamps 5 leaves the file as it is now (schema 4) beside it as `portico.content.v4`, the way back to
        a build that does not know references. Never overwritten; a copy that cannot be written is logged and never blocks
        the save."""
        try:
            copy_store: Store[dict] = Store(self._hass, 1, f"{STORE_KEY}.v{SCHEMA_PLAIN}", atomic_writes=True)
            if await copy_store.async_load() is None:
                current = self._last_v4
                if isinstance(current, dict) and current.get("schema_version") == SCHEMA_PLAIN:
                    await copy_store.async_save(copy.deepcopy(current))
                    _LOGGER.info("portico content: kept a copy of the v%d file as %s.v%d before the first v%d save",
                                 SCHEMA_PLAIN, STORE_KEY, SCHEMA_PLAIN, SCHEMA_REFS)
        except Exception as err:  # noqa: BLE001 - the copy is a courtesy, the save is the job
            _LOGGER.warning("portico content: could not keep a copy of the v%d file (%s); saving v%d anyway",
                            SCHEMA_PLAIN, err, SCHEMA_REFS)

    async def _keep_safety_copy(self, version: int) -> None:
        """The first schema-6 write leaves the file as it was read (schema `version`, 4 or 5) beside it as
        `portico.content.v<version>`, the way back to a build that does not know 6. Never overwritten. Unlike `_keep_v4_copy`
        a copy that cannot be written REFUSES the save (ProfileError, nothing changed): there is no switch back without it."""
        try:
            copy_store: Store[dict] = Store(self._hass, 1, f"{STORE_KEY}.v{version}", atomic_writes=True)
            if await copy_store.async_load() is None:
                current = self._last_disk
                if not isinstance(current, dict) or current.get("schema_version") != version:
                    raise ProfileError("the file as read is not available to copy")
                await copy_store.async_save(copy.deepcopy(current))
                _LOGGER.info("portico content: kept a copy of the v%d file as %s.v%d before the first v%d save",
                             version, STORE_KEY, version, SCHEMA_PAGES)
        except Exception as err:  # noqa: BLE001 - re-raised as the refusal it is
            _LOGGER.warning("portico content: could not keep a copy of the v%d file (%s); not writing v%d",
                            version, err, SCHEMA_PAGES)
            raise ProfileError(f"could not keep a copy of the current content file (schema {version}); "
                               "nothing was changed") from err

    def _data(self) -> dict[str, Any]:
        """The STORED document (references as references). Legacy: `schema_version` is 5 only while it holds a reference.
        The unified model (schema 6): `{pages, devices, roster?, dashboards, endpoints}`."""
        if self._model6:
            data6: dict[str, Any] = {"schema_version": SCHEMA_PAGES, "pages": self._library, "devices": self._devices,
                                     "dashboards": self._dashboards, "endpoints": self._endpoint_decls}
            if self._roster != [ROSTER_ALL]:
                data6["roster"] = self._roster
            if self._home_name:
                data6["home_name"] = self._home_name
            if self._show_fps:
                data6["show_fps"] = True
            return data6
        data: dict[str, Any] = {"schema_version": SCHEMA_PLAIN, "template": self._template,
                                "overrides": self._overrides, "dashboards": self._dashboards,
                                "endpoints": self._endpoint_decls}
        if has_refs(data):
            data["schema_version"] = SCHEMA_REFS
        if self._home_name:
            data["home_name"] = self._home_name
        if self._show_fps:
            data["show_fps"] = True
        return data

    def _disk_data(self) -> dict[str, Any]:
        """What the file holds: a copy of `_data()`, blocks only (the derived flat mirror went in B8)."""
        return copy.deepcopy(self._data())

    async def async_set_home_name(self, raw: Any) -> str:
        """Validate, store and announce the home name ("" clears it). A refused name changes nothing; the
        same name again writes nothing and tells nobody. Raises ProfileError."""
        await self.async_ensure_loaded()
        name = normalize_home_name(raw)
        if name == self._home_name:
            return name
        self._check_writable()
        self._home_name = name
        await self._save()
        for listener in list(self._home_listeners):
            listener()
        return name

    def _endpoint_id(self, user_id: str) -> str:
        """The key an HA user's override is stored under: its phone endpoint's id. A user with no enrolled
        phone keys by its own id (the same stand-in `endpoints.py` uses for a row with no enroll id)."""
        endpoint = get_endpoint_registry(self._hass).for_user(user_id)
        return endpoint.id if endpoint is not None and endpoint.kind == "phone" else user_id

    # ---- element references: ONE expanded view (placements design 3.3) -------------------------------------------
    # Every reader that resolves, merges or grants goes through `_expanded()`; only the stored-document accessors
    # (`_data`, `document`, `doc_rev`, `_save`, the editor's write path) see references as references. A test reads this
    # file's source and fails if any other method touches the raw containers.

    def _elements_by_id(self) -> dict[str, Any]:
        from .elements import get_elements_store

        return get_elements_store(self._hass).elements_by_id()

    def _warn_unusable(self, element_id: str, kind: str) -> None:
        if element_id not in self._warned_refs:
            self._warned_refs.add(element_id)
            _LOGGER.warning("portico content: a screen refers to %s %s that is missing or cannot be placed; it is left out",
                            kind, element_id)

    def expand(self, document: dict[str, Any]) -> dict[str, Any]:
        """`document` (stored form) with references expanded against the live elements; the same object when it has none."""
        if not has_refs(document):
            return document
        return expand_document(document, self._elements_by_id(), on_drop=self._warn_unusable)

    def _expanded(self) -> dict[str, Any]:
        """The stored document with every reference expanded: what the resolvers, the grants and the merge read. For a panel
        with no pages of its own serves none: there is no fallback to a former screen."""
        return self.expand(self._data())

    def _on_elements_changed(self) -> None:
        """An element was added, changed or removed: the screens that use one refetch (`doc_rev` does not move: the stored
        document holds only the id). Nothing is announced while no screen uses an element."""
        self._warned_refs.clear()
        if self._loaded and (has_refs(self._data()) or has_cameras(self._data())):   # an inline camera follows its element's small stream
            self._announce(None)

    def referenced_element_ids(self) -> set[str]:
        return referenced_ids(self._data())

    def element_usage(self) -> dict[str, list[dict[str, Any]]]:
        """element id -> the screens that use it (`ElementStore.used_by`)."""
        from .content_elements import usage_index

        return usage_index(self._data())

    def pages_for_endpoint(self, endpoint_id: str) -> list[dict[str, Any]]:
        """The template's pages with the endpoint's override laid over them: an override page replaces the
        template page of the same id, any other joins at the end. References are expanded."""
        return merged_pages(self._expanded(), endpoint_id)

    def profile_for(self, user_id: str) -> dict[str, Any]:
        """The caller's "home" page as a v1 profile: `{cameras, actions}`."""
        return _home(self.pages_for_endpoint(self._endpoint_id(user_id))) or EMPTY_PROFILE

    async def async_set(self, user_id: str, raw: Any) -> dict[str, Any]:
        await self.async_ensure_loaded()
        registry = get_call_registry(self._hass)
        # A call button already in this phone's profile keeps its target even when the target has vanished
        # (editing something else must still save); a new or changed target must be one the registry knows.
        existing = {(a["target"]["type"], a["target"]["id"])
                    for a in self.profile_for(user_id)["actions"] if a["kind"] == "call"}

        endpoints = get_endpoint_registry(self._hass)

        def known(target_type: str, target_id: str) -> bool:
            return ((target_type, target_id) in existing
                    or registry.target_state(target_type, target_id) is not None
                    or (not is_app_screen(self._hass, target_id)
                        and endpoints.get(target_id, kind=target_type) is not None))

        profile = normalize_profile(raw, known_target=known)
        self._check_writable()      # before the in-memory override is touched, so a refusal leaves no drift
        if self._model6:
            raise ProfileError("this install uses the unified Pages model; edit the phone's pages in the Portico panel")
        endpoint_key = self._endpoint_id(user_id)
        # `async_set` (a flat profile; no caller since the Configure flow went, E7) edits the "home" page only: its
        # label (and every other page) is left as it is. With blocks it can only write a flat profile, which would
        # collapse a page of several blocks: refused (blocks design 3.7).
        home = next((p for p in self.pages_for_endpoint(endpoint_key) if p["id"] == HOME_PAGE), None)
        if home is not None and len(home["blocks"]) > 1:
            raise ProfileError("this page has several blocks; edit it in the Portico editor")
        label = home["label"] if home else default_page_label(HOME_PAGE)
        first = home["blocks"][0] if home and home["blocks"] else None
        block = {"id": first["id"] if first else HOME_PAGE,
                 "title": first["title"] if first else title_from_label(label, HOME_PAGE),
                 **({"door": first["door"]} if first and "door" in first else {}), **profile}
        override = self._overrides.setdefault(endpoint_key, _empty_override())
        override["pages"] = [{"id": HOME_PAGE, "label": label, "blocks": [block]},
                             *(p for p in override["pages"] if p["id"] != HOME_PAGE)]
        await self._save()
        for listener in list(self._listeners):
            listener(endpoint_key)
        return profile

    async def async_remove(self, user_id: str, endpoint_id: str | None = None) -> None:
        """Drop a user's override. `endpoint_id` names the key directly: a removed phone entry is no longer in the
        endpoint registry, so `_endpoint_id` would key by the user id and miss it (P-4)."""
        await self.async_ensure_loaded()
        announced = endpoint_id or self._endpoint_id(user_id)
        keys = {announced}
        if endpoint_id:
            keys.add(user_id)           # an override stored under the stand-in key (a row with no enroll id)
        held = self._devices if self._model6 else self._overrides      # a removed phone's own list goes with it
        if any(key in held for key in keys):
            self._check_writable()
        removed = [held.pop(key, None) for key in keys]
        if any(r is not None for r in removed):
            await self._save()
            for listener in list(self._listeners):
                listener(announced)

    # ---- resolved view and running -------------------------------------

    def _resolve_page(self, page: dict[str, Any], endpoint_id: str | None = None) -> dict[str, Any]:
        return resolve_page(page, states=self._hass.states, registry=self, endpoint=self._endpoint_by_id(endpoint_id))

    # The `registry` the pure resolvers (content_resolve) are handed: this store is it.

    def endpoints(self):
        """The endpoint registry's list (phones, dashboards, doors)."""
        return get_endpoint_registry(self._hass).endpoints()

    def is_app_screen(self, endpoint_id: Any) -> bool:
        """Whether `endpoint_id` is the screen half of a panel (`endpoints.is_app_screen`, the one predicate)."""
        return is_app_screen(self._hass, endpoint_id)

    def phone_of(self, slug: Any):
        """The phone half of an app screen, or None (`endpoints.phone_of`)."""
        return phone_of(self._hass, slug)

    def _app_screen_ids(self) -> frozenset[str]:
        return frozenset(slug for slug in self.dashboards if is_app_screen(self._hass, slug))

    def door(self, device_id: str):
        """The Door a Portico `device_id` names, or None."""
        found = self._portico_door(device_id)
        return None if found is None else found[1]

    def fallback_small_entity(self, entity_id: str) -> str | None:
        """The small stream for an inline camera item that holds `entity_id` (DEV-768 fallback): a camera element for that entity
        with a `small_entity_id`, else the `live_sub_cameras` option, else None."""
        from .live import _sub_entity

        return small_entity_of(self._elements_by_id(), entity_id) or _sub_entity(self._hass, entity_id)

    def stream_size(self, entity_id: str) -> tuple[int, int] | None:
        """The live (width, height) of a camera entity for the resolved document (`streams`), from `CameraInfo`'s memory; None
        until learned. Never probes here: an entity not asked about for STREAM_SIZE_RETRY seconds starts ONE background
        refresh (the RTSP description is cached 10 minutes in camera_info), and a size that arrives or changes announces a
        refetch. Never raises."""
        info = get_camera_info(self._hass)
        now = time.monotonic()
        known = info.known_size(entity_id)
        wait = STREAM_SIZE_RETRY if known else STREAM_SIZE_RETRY_UNKNOWN
        if entity_id not in self._size_pending and now - self._size_asked.get(entity_id, -1e9) >= wait:
            self._size_asked[entity_id] = now
            self._size_pending.add(entity_id)
            self._hass.async_create_task(self._refresh_size(entity_id))
        return known

    async def _refresh_size(self, entity_id: str, tries: int = 0) -> None:
        learned = False
        try:
            if await get_camera_info(self._hass).async_refresh_sizes(self._hass, [entity_id]):
                learned = True
                self._announce(None)
        except Exception:   # noqa: BLE001 - a size is a hint; a failure leaves the document as it was
            _LOGGER.debug("portico content: the stream size of %s could not be read", entity_id)
        finally:
            self._size_pending.discard(entity_id)
        self._rearm_size(entity_id, tries, learned)

    def _rearm_size(self, entity_id: str, tries: int, learned: bool) -> None:
        """A live camera whose size is still unknown after a refresh is asked again by the store itself, a minute later, up to
        STREAM_SIZE_SELF_RETRIES times. Before this the retry waited for the next document build, and the app builds one only
        when it connects or is told something changed: a small stream whose first description failed (go2rtc not up yet after a
        restart) stayed unknown, and the gear menu said "Low", until the phone reconnected. A camera that is down is left to the
        next build. A size that arrives announces a refetch like any other."""
        info = get_camera_info(self._hass)
        if learned or info.known_size(entity_id) is not None or tries >= STREAM_SIZE_SELF_RETRIES:
            return
        state = self._hass.states.get(entity_id)
        if state is None or state.state in ("unavailable", "unknown") or entity_id in self._size_timers:
            return
        from homeassistant.helpers.event import async_call_later

        @callback
        def _fire(_now: Any) -> None:
            self._size_timers.pop(entity_id, None)
            if entity_id in self._size_pending:
                return
            self._size_asked[entity_id] = time.monotonic()
            self._size_pending.add(entity_id)
            self._hass.async_create_task(self._refresh_size(entity_id, tries + 1))

        if not self._size_stop_armed:
            self._size_stop_armed = True
            self._hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, callback(lambda _event: self.cancel_size_retries()))
        self._size_timers[entity_id] = async_call_later(self._hass, STREAM_SIZE_RETRY_UNKNOWN, _fire)

    def cancel_size_retries(self) -> None:
        """Cancel the pending self-retries of stream sizes (unload)."""
        for cancel in list(self._size_timers.values()):
            cancel()
        self._size_timers.clear()

    def live_rtsp(self, entity_id: str, state: Any, has_small: bool = False) -> dict[str, str] | None:
        """The `live_rtsp` hint of a live camera tile (live_rtsp.py), computed now from the settings entry's two options and the
        camera's Frigate name; None when the option is off or the entity is not a Frigate camera. Never stored."""
        options = live_rtsp_module.push_entry_options(self._hass)
        explicit = live_rtsp_module.read_away_streams(options).get(entity_id)
        if explicit is not None:                            # DEV-715: an address set for this very camera, any brand, base or not
            return dict(explicit)
        base, subs = live_rtsp_module.read_settings(options)
        if not base:
            return None
        gone = state is None or state.state in ("unavailable", "unknown")
        name = None if state is None else live_rtsp_module.camera_name_of(state.attributes)
        if name is not None:
            self._frigate_names[entity_id] = name
        elif gone:
            name = self._frigate_names.get(entity_id)       # an unavailable camera has lost its attributes: keep `rev` still
        else:
            self._frigate_names.pop(entity_id, None)        # it answers and is not a Frigate camera (any more)
        return live_rtsp_module.hint(base, subs, name, has_small)

    def announce_all(self) -> None:
        """Tell every subscriber to refetch (an option that changes the resolved document, not the stored one)."""
        self._announce(None)

    def announce_live(self) -> None:
        """Tell every subscriber to refetch because live state changed while the `rev` did not (a visitor call began or
        ended: Talk reads `busy`). The event carries `rev: null`, so a phone holding the same rev still refetches."""
        for listener in list(self._live_listeners):
            listener()

    def call_state(self, action: dict[str, Any]) -> dict[str, Any]:
        """{label, available, reason} for a call button or a roster entry; never raises."""
        return self._call_state(action)

    def call_state_for(self, action: dict[str, Any], viewer) -> dict[str, Any]:
        """The same, as seen by `viewer` (an Endpoint): only the gate's state depends on who is looking (only a phone can
        talk to it). `content_resolve` asks this when the registry has it and a viewer is known."""
        return self._call_state(action, viewer)

    def gate_call(self, target: dict[str, Any]) -> bool:
        """True when a call button with this `target` is the gate call (DEV-695): the very test `async_run` makes to send a
        press through Talk's path (`_gate_door_id`), so the marker on the resolved action and the press can never disagree.
        `content_resolve` asks it for every call button. Never raises."""
        return self._gate_door_id(target) is not None

    def roster_groups(self, callee_id: str, endpoints: list) -> list[dict[str, Any]]:
        """The roster entries a SCREEN gets before its phones (M7 3a, 15.14: "Everyone"); none while house calls are off, which
        is always today, and none for a phone (HC-9). Never raises."""
        from .house import get_house

        house = get_house(self._hass)
        if house is None:
            return []
        try:
            return house.group_entries(next((e for e in endpoints if e.id == callee_id), None))
        except Exception as err:  # a registry bug must not take content/get down
            _LOGGER.warning("portico content: the roster groups failed: %s", type(err).__name__)
            return []

    # ---- Talk to the door (blocks design 7.3 and 7.4, B7a): nothing dials unless the door reports `CanDial` ------

    def _endpoint_by_id(self, endpoint_id: str | None):
        """The endpoint registry's entry for an endpoint id, or None (never raises)."""
        if not endpoint_id:
            return None
        try:
            return get_endpoint_registry(self._hass).get(endpoint_id)
        except Exception as err:
            _LOGGER.warning("portico content: endpoint registry failed for %s: %s", endpoint_id, type(err).__name__)
            return None

    def _talk_view(self, door_id: str | None, endpoint, *, anyone: bool = False, explicit: bool = False) -> dict[str, Any]:
        """`_talk_view_raw`, plus one INFO line when the (available, reason) of a door for an endpoint CHANGES, with the
        snapshot that caused it (DEV-764). A steady state logs nothing. The VIEWER of a panel's app screen is its phone half
        (DEV-807 item 4: every Talk state key is the phone's), so the screen's own button reads `available`, not `phone_only`."""
        endpoint = self._talk_keyed(endpoint)
        self._talk_cause = None
        view = self._talk_view_raw(door_id, endpoint, anyone=anyone, explicit=explicit)
        key = (door_id, getattr(endpoint, "id", None))
        now = (view["available"], view["reason"])
        before = self._talk_last.get(key)
        if before != now:
            self._talk_last[key] = now
            self._log_busy_edge(key, before, now)
            _LOGGER.info("portico talk: door=%s phone=%s available=%s reason=%s (was %s)%s", door_id, key[1], now[0], now[1],
                         "-" if before is None else f"{before[0]}/{before[1]}",
                         f" cause: {self._talk_cause}" if self._talk_cause else "")
        return view

    def _log_busy_edge(self, key, before, now) -> None:
        """DEV-806: production runs the `portico` logger at WARNING, so the INFO transition line is never written. Say it at
        WARNING, once per edge, only when a door's Talk reason goes TO or FROM `busy` (never per render), with the cause
        (which of state / outbound / dialing / remembered_dial holds it) and, on clearing, how long it was busy."""
        was_busy = before is not None and before[1] == "busy"
        is_busy = now[1] == "busy"
        if is_busy == was_busy:
            return
        if is_busy:
            self._busy_since[key] = time.monotonic()
            _LOGGER.warning("portico talk: door=%s phone=%s reason=busy began (was %s) cause: %s", key[0], key[1],
                            "-" if before is None else f"{before[0]}/{before[1]}", self._talk_cause or "unknown")
        else:
            since = self._busy_since.pop(key, None)
            lasted = f"{time.monotonic() - since:.1f}s" if since is not None else "unknown"
            _LOGGER.warning("portico talk: door=%s phone=%s reason=busy cleared after %s (now %s/%s)", key[0], key[1], lasted,
                            now[0], now[1])

    def _talk_keyed(self, endpoint):
        """The endpoint a Talk state is KEYED by (owner, rate key, relay placement, `mine`, the viewer of `talk_state`): the phone
        half when `endpoint` is a panel's app screen, else `endpoint` itself. Keying only: the door lookup and the grant stay on
        the slug the app named, and a browser kiosk screen (not an app screen) is unaffected."""
        if endpoint is not None and getattr(endpoint, "kind", None) == KIND_DASHBOARD and is_app_screen(self._hass, endpoint.id):
            return phone_of(self._hass, endpoint.id) or endpoint
        return endpoint

    def _talk_view_raw(self, door_id: str | None, endpoint, *, anyone: bool = False, explicit: bool = False) -> dict[str, Any]:
        """The state of a Talk button, without side effects: `{available, reason, provider, door, mine}`.

        In this order (design 7.4 steps 1 to 4): `phone_only` (the endpoint is not a phone), `no_door`; then a call
        THIS endpoint started on the door that is still going (`connecting` / `in_call`, available: the press stops
        it); `talk_off` (the door does not report `CanDial`: today every door); `not_connected`; `busy` (a visitor
        call, an outbound call that is not this endpoint's, or a dial in flight); otherwise ready (reason None).
        `reason` is the wire's word for the off states and the phase for a call in progress. `provider`, `door` and
        `mine` (the call id of the endpoint's own live call) are for `_run_talk`; the wire gets `available` and
        `reason` only. Never touches the provider beyond a read (`call_state`).

        `explicit` (a Talk ELEMENT names this door, DEV-518): the
        explicit target is the door named, and the door that cannot dial says why: `dial_off` (a gate door with its dial switched off),
        `no_dial` (a driver that cannot place calls at all, e.g. `ha_event`) or `not_connected` (a gate door that is down),
        instead of `talk_off`. Every other check is the same."""
        def off(reason: str, found=None) -> dict[str, Any]:
            return {"available": False, "reason": reason, "provider": found[0] if found else None,
                    "door": found[1] if found else None, "mine": None}

        if not (anyone and endpoint is None) and (endpoint is None or getattr(endpoint, "kind", None) != KIND_PHONE):
            return off("phone_only")          # `anyone` (the gate's state for a caller's button, which has no viewer): no check
        found = self._portico_door(door_id) if door_id else None
        if found is None:
            return off("no_door")
        provider, door = found
        key = (provider.domain, door.id)
        try:
            snapshot = provider.call_state(door.id)
        except Exception as err:
            _LOGGER.warning("portico content: the call state of %s failed: %s", door.id, type(err).__name__)
            snapshot = None
        mine = self._own_talk_call(key, endpoint.id if endpoint is not None else "", snapshot)
        if mine is not None and mine[0] != "ended":
            phase, call_id = mine
            return {"available": True, "reason": phase, "provider": provider, "door": door, "mine": call_id}
        if getattr(door, "can_dial", False) is not True:
            return off(self._no_dial_reason(door) if explicit else TALK_OFF, found)
        if getattr(door, "available", False) is not True:
            return off("not_connected", found)
        if not isinstance(snapshot, dict):
            return off("not_connected", found)      # a door with no call state cannot be shown idle
        if (snapshot.get("state") != "idle" or snapshot.get("outbound_state") in ("dialing", "in_call")
                or key in self._talk_dialing or (key in self._talk_calls and "outbound_state" not in snapshot)):
            self._talk_cause = (f"busy because state={snapshot.get('state')} call={snapshot.get('call_id')} "
                                f"outbound={snapshot.get('outbound_state')} outbound_call={snapshot.get('outbound_call_id')} "
                                f"dialing={key in self._talk_dialing} remembered_dial={key in self._talk_calls}")
            return off("busy", found)
        # ready; right after the end of this endpoint's own call the phase is `ended` (a start is allowed again)
        return {"available": True, "reason": "ended" if mine is not None else None, "provider": provider, "door": door,
                "mine": None}

    @staticmethod
    def _no_dial_reason(door) -> str:
        """Why an explicitly targeted door that does not report `CanDial` cannot be called: see `_talk_view`."""
        if getattr(door, "driver", None) not in GATE_DRIVERS:
            return "no_dial"
        return "dial_off" if getattr(door, "available", False) is True else "not_connected"

    def _own_talk_call(self, key: tuple[str, str], endpoint_id: str, snapshot: Any) -> tuple[str, str] | None:
        """(phase, call id) of a call this endpoint started on the door and that is still going, else None. A provider
        that publishes the outbound call in its snapshot (`outbound_state` `dialing` / `in_call` / `ended`,
        `outbound_call_id`: the tracker of P2) is believed; with none, the store's own memory of the dial stands, for
        at most the 300 s cap. A stale memory is dropped here."""
        remembered = self._talk_calls.get(key)
        if remembered is None:
            return None
        owner, call_id, started = remembered
        if isinstance(snapshot, dict) and "outbound_state" in snapshot:
            state = snapshot["outbound_state"]
            if state in ("dialing", "in_call") and snapshot.get("outbound_call_id") in (call_id, None):
                phase = "connecting" if state == "dialing" else "in_call"
            else:
                self._talk_calls.pop(key, None)     # the call is over: whoever started it
                phase = "ended" if state == "ended" else None
        elif time.monotonic() - started > TALK_CAP_S:
            self._talk_calls.pop(key, None)         # past the cap: whoever started it
            phase = None
        else:
            phase = "in_call"
        if phase is None or owner != endpoint_id:
            return None
        return phase, call_id

    def talk_state(self, door_id: str | None, endpoint=None, *, explicit: bool = False) -> dict[str, Any]:
        """{available, reason} of a Talk button for the block's `door_id` as seen by `endpoint`; never raises
        (the `call_state` pattern). `talk_off` is the only state a door can be in today: none reports `CanDial`."""
        try:
            view = self._talk_view(door_id, endpoint, explicit=explicit)
        except Exception as err:      # a bug in here must not take content/get down; and it never means "on"
            _LOGGER.warning("portico content: the talk state failed: %s", type(err).__name__)
            return {"available": False, "reason": TALK_OFF}
        return {"available": view["available"], "reason": view["reason"]}

    def _talk_door_of(self, endpoint_id: str, action_id: str) -> str | None:
        """The door of the block that holds `action_id`: the same first match `_all_actions` (the grant) finds."""
        for page in self.pages_for_endpoint(endpoint_id):
            for block in page.get("blocks", []):
                if any(a.get("id") == action_id for a in block.get("actions", [])):
                    return block.get("door") or None
        return None

    async def _run_talk(self, user_id: str, phone: str, action: dict[str, Any], endpoint_id: str, by: str | None,
                        caller_user_id: str | None, door_id: str | None | object = _BLOCK_DOOR,
                        explicit: bool = False) -> dict[str, Any]:
        """Press a Talk button (blocks design 7.4), or a call button that targets the gate (M7 3f: `door_id` is then the
        gate's door endpoint id; this is the ONE dial path for both, there is no second one). Each refusal returns before the next step and before the
        provider; `provider.dial` is called in ONE place, at the end, and only when every check holds: the caller is
        the phone endpoint's own login, the block's door exists and reports `CanDial` exactly True, is up, and has no
        call going. A press while this endpoint's own call is going hangs it up by id (never a dial).

        `explicit` is a Talk element's own door (DEV-518): same checks and the same single `dial`, and the refusals name their case (`_talk_view`).

        Today no door reports `CanDial`, so every press is refused `not_available` and nothing is dialled."""
        label = action["label"]
        endpoint = self._talk_keyed(self._endpoint_by_id(endpoint_id))     # a panel's app screen acts as its phone (keying only)
        # 1. who: the endpoint is a phone and the acting login is that phone's own HA user. A dashboard login, and
        # an administrator naming a phone's endpoint, are not (design 7.4 step 1, review 8 item 6).
        if (endpoint is None or endpoint.kind != KIND_PHONE or not caller_user_id or caller_user_id != user_id
                or endpoint.ha_user_id != caller_user_id):
            self._log(phone, f'refused "{label}" (talk is for the phone itself)', None, by)
            raise RunRefused("not_allowed", "only the phone itself can talk to the door")
        if door_id is _BLOCK_DOOR:
            door_id = self._talk_door_of(endpoint_id, action["id"])      # the slug the app named: the pages are the screen's
        view = self._talk_view(door_id, endpoint, explicit=explicit)
        reason = view["reason"]
        _LOGGER.info("portico talk: press by %s on door=%s available=%s reason=%s mine=%s", phone, door_id,
                     view["available"], reason, view["mine"])
        if view["mine"] is not None and reason in ("connecting", "in_call"):
            return await self._talk_hangup(view, phone, label, by)
        if not view["available"]:
            self._log(phone, f'refused "{label}" (talk: {reason})', None, by)
            code, message = _TALK_REFUSALS.get(reason, ("not_available", "Talk to the door is switched off"))
            raise RunRefused(code, message)
        provider, door = view["provider"], view["door"]
        # the whole gate again at the call itself: a bug above must still not reach the provider
        if (provider is None or door is None or getattr(door, "can_dial", False) is not True
                or getattr(door, "available", False) is not True):
            self._log(phone, f'refused "{label}" (talk is switched off)', None, by)
            raise RunRefused("not_available", "Talk to the door is switched off")
        now = time.monotonic()
        owner_id = endpoint.id                      # the state key: the phone's id for a panel (item 4), else `endpoint_id`
        rate_key = (owner_id, action["id"])
        if now - self._last_run.get(rate_key, -RUN_INTERVAL) < RUN_INTERVAL:
            self._log(phone, f'refused "{label}" (too soon after the last run)', None, by)
            raise RunRefused("rate_limited", "this action just ran; wait a moment")
        self._last_run[rate_key] = now
        key = (provider.domain, door.id)
        # M7 P3-B: the phone that pressed is told `dialing` now and how the call goes after (talk_relay.py); only it.
        relay = get_talk_relay(self._hass)
        placement = relay.placing(owner_id, endpoint.ha_user_id, provider, door.id) if relay is not None else None
        self._talk_dialing.add(key)
        try:
            result = await provider.dial(door.id, "", 0)
        except Exception as err:
            code = error_code(err) or "failed"
            if relay is not None:
                relay.failed(placement, err)
            self._log(phone, f'"{label}" failed ({code})', None, by)
            raise RunRefused(code, getattr(err, "message", None) or "the door could not be called") from err
        except BaseException:
            if relay is not None:
                relay.failed(placement, "cancelled")      # the connection that asked went away mid-dial
            raise
        finally:
            self._talk_dialing.discard(key)
        call_id = result.get("call_id") if isinstance(result, dict) else None
        if not isinstance(call_id, str) or not call_id:
            # a dial with no call id cannot be followed, answered or hung up: it is a failure (design 7.2)
            _LOGGER.warning("portico content: the dial to %s returned no call id", door.id)
            if relay is not None:
                relay.failed(placement, "failed")
            self._log(phone, f'"{label}" failed (no call id)', None, by)
            raise RunRefused("failed", "the door did not start a call")
        if relay is not None:
            relay.placed(placement, call_id)
        registry = self._hass.data.get(DOMAIN, {}).get(DATA_PROVIDERS)
        if registry is not None:
            registry.note_call(provider.domain, result, door.id)     # as handle_dial: a dialled call has no ring
        self._talk_calls[key] = (owner_id, call_id, now)
        self._log(phone, f'ran "{label}" (dialled)', None, by)
        return {"call_id": call_id, "media": None}

    async def _talk_hangup(self, view: dict[str, Any], phone: str, label: str, by: str | None) -> dict[str, Any]:
        """The second press: hang up this endpoint's own call by its id."""
        provider, door, call_id = view["provider"], view["door"], view["mine"]
        key = (provider.domain, door.id)
        try:
            await provider.hangup(call_id, "hangup")
        except Exception as err:
            code = error_code(err) or "failed"
            if code in ("unknown_call", "not_available"):
                self._talk_calls.pop(key, None)       # the call is gone already
            self._log(phone, f'"{label}" hangup failed ({code})', None, by)
            raise RunRefused(code, getattr(err, "message", None) or "the call could not be ended") from err
        self._talk_calls.pop(key, None)
        self._log(phone, f'ran "{label}" (hung up)', None, by)
        return {"call_id": call_id, "hung_up": True}

    # ---- the document: what the editor reads and the resolvers take ----

    def document(self) -> dict[str, Any]:
        """A deep copy of the stored v4 document (what `_save` writes)."""
        return copy.deepcopy(self._data())

    @property
    def doc_rev(self) -> str:
        """The hash of the raw stored document: `content/document`'s `doc_rev`, `content/save`'s `base_doc_rev`."""
        return doc_rev(self._data())

    @property
    def loaded(self) -> bool:
        return self._loaded

    def resolve_document(self, endpoint_id: str, *, caller: str | None = None) -> dict[str, Any]:
        """The resolved document (design 2.6) of one endpoint from the stored document."""
        return resolve_endpoint(self._expanded(), endpoint_id, states=self._hass.states, registry=self, caller=caller)

    def roster_for(self, endpoint_id: str) -> list[dict[str, Any]]:
        """The roster of one endpoint, resolved."""
        document = self._expanded()
        return resolve_roster(document, endpoint_id, endpoints=endpoint_table(document, self), registry=self)

    def resolve_for_endpoint(self, endpoint_id: str) -> dict[str, Any]:
        """The reply of `content/get` for one endpoint (contract 3): the resolved document (`endpoint`, `kind`,
        `home_name` when set, `pages` of `{id, label, blocks}`, `callers`, `roster`, `rev`). `rev` is the document's
        `endpoint_rev` for every caller."""
        out = self.resolve_document(endpoint_id)
        wake = self.wake_for_endpoint(endpoint_id)
        if wake is not None:            # a screen of a schema 6 document only: the wire is additive, an old app never sees it
            out = {**out, "wake": wake}
        return {**out, **self._panel_fields(endpoint_id)}

    def _panel_fields(self, endpoint_id: str) -> dict[str, Any]:
        """`screens` (the one-element shim `[<phone endpoint id>]`) and `phone_endpoint` (the login's phone endpoint id), for a
        login whose phone entry is a panel (`panel: true`), in the phone's result and in a former panel screen's own
        document alike: the app names its "slug" in steady state and the slug is the phone id (DEV-807). Both
        sit outside `rev` (the store's `_announce` is what tells a subscriber to refetch). Nothing for any other login, so a
        house with no panel serves byte-identical documents."""
        endpoint = self._endpoint_by_id(endpoint_id)
        user_id = endpoint.ha_user_id if endpoint is not None else None
        if not is_panel_user(self._hass, user_id):
            return {}
        registry = get_endpoint_registry(self._hass)
        phone = next((e for e in registry.endpoints(kind=KIND_PHONE) if e.ha_user_id == user_id), None)
        # The shim for an app that still names a slug (0.6.98): the one entry of `screens` is the phone endpoint id, which the
        # server already lets a phone name as its own endpoint, so the app reads the panel's own document and `wake`.
        out: dict[str, Any] = {"screens": [phone.id] if phone is not None else []}
        if phone is not None:
            out["phone_endpoint"] = phone.id
        # R9: "house calls are on for this login", from the server's own two switches: the phone entry's `house_receive` and the
        # global house switch. The app no longer reads the roster for it. A plain boolean the app persists with `wake`.
        from .house import OPTION_HOUSE_CALLS as HOUSE_CALLS_FIELD     # the wire key is the option's own word (house.py is
        # the one file that spells it: test_house_boundary)

        out[HOUSE_CALLS_FIELD] = bool(phone_house_receive(self._hass, user_id))
        return out

    def resolve_endpoint(self, endpoint_id: str) -> dict[str, Any]:
        """The merged template-plus-override page set of one endpoint, each page resolved (plan 2.7)."""
        return {"pages": [{"id": p["id"], "label": p["label"], **self._resolve_page(p, endpoint_id)}
                          for p in self.pages_for_endpoint(endpoint_id)]}

    def _all_actions(self, endpoint_id: str) -> list[dict[str, Any]]:
        """Every action of every page of the endpoint's set, in page order: the grant for `content/run`."""
        document = self._expanded()
        actions = [a for p in merged_pages(document, endpoint_id) for a in page_actions(p)]
        # Schema 6: the Ring pages are part of the grant: a ring button runs through `content/run` by id like any other. Every
        # stored Ring page that rings for somebody counts (design `portico_ring_pages_per_talk_design.md` 3.3).
        actions.extend(a for p in assigned_ring_pages(document, endpoint_id, self) for a in page_actions(p))
        return actions

    def _roster_action(self, endpoint_id: str, action_id: str) -> dict[str, Any] | None:
        """A roster entry's `r-...` id as a `call` action, when it is in the endpoint's resolved roster: a roster call
        is placed through the same grant (and `_run_call`) as a call button, and the phone never sends an
        endpoint id (CB-1)."""
        if not action_id.startswith("r-"):
            return None
        entry = next((e for e in self.roster_for(endpoint_id) if e["action_id"] == action_id), None)
        if entry is None or entry["kind"] not in ROSTER_TARGET_TYPES:
            return None
        return {"id": action_id, "label": entry["name"], "kind": "call", "confirm": False,
                "target": {"type": entry["kind"], "id": entry["id"]}}

    def _all_cameras(self, endpoint_id: str) -> list[dict[str, Any]]:
        """Every camera of every page of the endpoint's set, then the `c-...` cameras of its `callers`: the grant for
        `content/snapshot`. A caller camera is `{id: c-<12 hex of the entity id>, entity_id}`, the id
        `resolve_caller` hands out."""
        cameras = [c for p in self.pages_for_endpoint(endpoint_id) for c in page_cameras(p)]
        document = self._expanded()
        for caller in endpoint_table(document, self):
            if caller.id == endpoint_id or is_app_screen(self._hass, caller.id):
                continue                # a panel's app screen is never offered to anyone as a caller (DEV-807)
            for entity_id in caller_camera_entities(document, endpoint_id, caller, self):
                cameras.append({"id": short_id("c", entity_id), "entity_id": entity_id})
        return cameras

    @property
    def dashboards(self) -> dict[str, dict[str, Any]]:
        """The declared dashboards (slug -> declaration); empty until the M6 editor writes some."""
        return self._dashboards

    @property
    def endpoint_declarations(self) -> dict[str, dict[str, Any]]:
        """A phone's camera declarations (endpoint id -> {cameras}); v3."""
        return self._endpoint_decls

    @property
    def overrides(self) -> dict[str, dict[str, Any]]:
        return self._overrides

    @property
    def template(self) -> dict[str, Any]:
        return self._template

    @property
    def uses_pages_model(self) -> bool:
        """True when the stored file is the unified model (schema 6): applied by `portico/content/pages_migrate`."""
        return self._model6

    @property
    def library(self) -> list[dict[str, Any]]:
        return self._library

    @property
    def devices(self) -> dict[str, dict[str, Any]]:
        return self._devices

    def talk_parties(self) -> dict[str, tuple[str, str]]:
        """`{talk element id: (party kind, party id)}` of the Talk elements now stored: who each one stands for (a door, a phone,
        a screen). The resolver asks it to find the Ring page a device assigned to the caller's Talk element. Never raises."""
        try:
            return parties_of(self._elements_by_id())
        except Exception as err:  # noqa: BLE001 - a registry bug must not take content/get down
            _LOGGER.warning("portico content: the talk parties failed: %s", type(err).__name__)
            return {}

    # ---- ownership of Talk elements by screens, and waking (Devices D2) ------

    def wake_map(self) -> dict[str, dict[str, Any]]:
        """Per screen slug: `{doors, elements, self_elements, screen_call}` (`pages_ownership.wake_map`); {} for a legacy document."""
        if not self._model6:
            return {}
        return pages_ownership.wake_map(self._data(), self.talk_parties(), panel_users(self._hass))

    def wake_for_endpoint(self, endpoint_id: str) -> dict[str, Any] | None:
        """What `content/get` and `content_changed` say about waking for a SCREEN: `{doors: [door ref...], screen_call: True}`, or
        None for a phone, an unknown endpoint or a legacy document (then no `wake` key: the wire is as it was)."""
        row = self.wake_map().get(endpoint_id)
        return None if row is None else {"doors": list(row["doors"]), "screen_call": row["screen_call"]}

    def wake_for_talk_element(self, element_id: str) -> list[str]:
        """The screen endpoint ids a call for this Talk element wakes (D2); [] when none (phones only)."""
        if not self._model6:
            return []
        return pages_ownership.wake_for_element(self._data(), self.talk_parties(), element_id, panel_users(self._hass))

    def screens_for_door(self, door_id: str) -> list[str]:
        """The screens a call at this door wakes; [] means phones only."""
        if not self._model6:
            return []
        return pages_ownership.screens_for_door(self._data(), self.talk_parties(), door_id, panel_users(self._hass))

    def _element_name(self, element_id: str) -> str:
        element = self._elements_by_id().get(element_id)
        name = element.get("name") if isinstance(element, dict) else None
        return name if isinstance(name, str) and name else element_id

    def _check_library_delete(self, raw: Any) -> None:
        """A library page that a device still shows (or answers with as a Ring page) cannot be deleted: `in_use`, with the device
        list. Read from the raw incoming document BEFORE it is normalised, since the validator would only say that a device
        names a page that is gone. Only a model 6 store; the panel refuses the same delete first."""
        if not self._model6 or not isinstance(raw, dict) or not is_pages_model(raw):
            return
        incoming = {p.get("id") for p in raw.get("pages") or () if isinstance(p, dict)}
        names = {ep.id: ep.name for ep in get_endpoint_registry(self._hass).endpoints() if ep.name}
        names.update({slug: d.get("name") or slug for slug, d in (self._dashboards or {}).items()})
        leftovers = set(self.orphan_keys())      # a "Not attached" entry shows nothing, so it is no user of a page
        for page in self._library:
            if page["id"] in incoming:
                continue
            users = [u for u in pages_ownership.device_users_of_page(raw, page["id"]) if u not in leftovers]
            if users:
                shown = ", ".join(str(names.get(u)) if names.get(u) else f"{u} (not attached)" for u in users)
                raise SaveRefused("in_use", f"The page {page.get('label') or page['id']!r} is shown on {shown}; remove it from "
                                  "those devices before deleting it", page=page["id"], devices=users)

    def _prune_leftover_page_refs(self, raw: Any) -> Any:
        """`raw` with the references to a library page the incoming document deletes removed from the "Not attached" entries
        (their pages list and Ring map). The entry itself and its other data stay (D-20261122-e013); a real device is untouched,
        `_check_library_delete` has already refused when one still uses the page."""
        if not self._model6 or not isinstance(raw, dict) or not is_pages_model(raw) or not isinstance(raw.get("devices"), dict):
            return raw
        incoming = {p.get("id") for p in raw.get("pages") or () if isinstance(p, dict)}
        gone = {p["id"] for p in self._library if p["id"] not in incoming}
        leftovers = set(self.orphan_keys())
        if not gone or not leftovers:
            return raw
        out = dict(raw)
        out["devices"] = dict(raw["devices"])
        for key in leftovers:
            device = out["devices"].get(key)
            if not isinstance(device, dict):
                continue
            device = dict(device)
            device["pages"] = [e for e in device.get("pages") or () if not (isinstance(e, dict) and e.get("page") in gone)]
            out["devices"][key] = device
        return out

    # ---- "Not attached": overrides no endpoint has (R6-02) ------------------

    def orphan_keys(self) -> list[str]:
        """The override keys no phone or dashboard answers to: a re-enrolled phone's old id, a v1 profile's old
        user id. Kept, never dropped; an administrator moves them to a phone or the template, or deletes them."""
        attached = {ep.id for ep in get_endpoint_registry(self._hass).endpoints()
                    if ep.kind in ("phone", "dashboard")} | set(self._dashboards)
        return [key for key in (self._devices if self._model6 else self._overrides) if key not in attached]

    def _orphan(self, key: str) -> dict[str, Any]:
        held = self._devices if self._model6 else self._overrides
        if key not in held or key not in self.orphan_keys():
            raise ProfileError(f"{key!r} is not a 'Not attached' override")
        return held[key]

    def _announce(self, endpoint_id: str | None) -> None:
        """Tell subscribers: one endpoint's listeners, or (None) everyone's, as after a template change."""
        if endpoint_id is None:
            for listener in list(self._home_listeners):
                listener()
        else:
            for listener in list(self._listeners):
                listener(endpoint_id)

    def check_writable(self) -> None:
        """Public `_check_writable` for the migration's orchestration (pages_migrate.run_migration)."""
        self._check_writable()

    def plan_pages_migration(self) -> tuple[Any, dict[str, Any]]:
        """The Pages migration of the stored document, planned and PROVEN but not applied: `(plan, report)` (pages_migrate). Reads
        the stored form (references as references) and the live elements; writes nothing."""
        from .pages_migrate import plan_for_store

        return plan_for_store(self, self._data(), self._elements_by_id(), self._hass.states, self._disk_version)

    async def async_adopt_migrated(self, document: dict[str, Any], base_doc_rev: str | None) -> str:
        """The apply's one content write: `document` (schema 6, from `pages_migrate.migrate_to_v6`) replaces the stored schema 4/5
        one when `base_doc_rev` is still its hash. `_save` leaves the `.v4`/`.v5` copy first and refuses the whole write if that
        copy cannot be made. Raises SaveConflict, ProfileError (a newer schema, no copy), changing nothing. Returns the new
        `doc_rev`."""
        self._check_writable()
        if self._model6:
            raise ProfileError("the document is already the unified Pages model (schema 6)")
        if base_doc_rev != self.doc_rev:
            raise SaveConflict("the document changed since the dry run")
        before = self._state()
        self._adopt(document)           # nothing awaits between the check above and here: two applies cannot both pass
        try:
            await self._save()
        except BaseException:
            self._restore(before)
            raise
        self._announce(None)            # every subscriber refetches (the resolved documents are equal, so their rev is not new)
        return self.doc_rev

    def _refuse_orphan_move(self) -> None:
        if self._model6:
            raise ProfileError("a device's pages are edited on the Devices tab in the unified Pages model; "
                               "delete the 'Not attached' entry or assign its pages to a device there")

    async def async_move_orphan_to_endpoint(self, key: str, endpoint_id: str) -> None:
        """Move the 'Not attached' override `key` onto the phone or dashboard `endpoint_id`. A page or rule whose id
        the target already has arrives under a fresh id (`home-2`) with its label marked; the target's own are
        never replaced; roster hides are added to the target's. Raises ProfileError, changing nothing."""
        await self.async_ensure_loaded()
        self._refuse_orphan_move()
        orphan = self._orphan(key)
        endpoint = get_endpoint_registry(self._hass).get(endpoint_id)
        if endpoint is None or endpoint.kind not in ("phone", "dashboard") or is_app_screen(self._hass, endpoint_id):
            raise ProfileError(f"{endpoint_id!r} is not a phone or a screen")      # a panel's app screen is not a target (DEV-807)
        self._check_writable()
        target = self._overrides.get(endpoint_id) or _empty_override()
        pages, rules = _merge_moved(orphan, target["pages"], target["rules"])
        merged = {"pages": pages, "rules": rules,
                  "roster_hide": list(dict.fromkeys([*target["roster_hide"], *orphan["roster_hide"]]))}
        self._overrides[endpoint_id] = merged
        del self._overrides[key]
        await self._save()
        self._announce(endpoint.id)

    async def async_move_orphan_to_template(self, key: str) -> None:
        """Move the 'Not attached' override `key` into the household template (every endpoint then shows it).
        Same id rule as the move to an endpoint. Its roster hides are dropped: they hid entries for one phone only.
        Raises ProfileError, changing nothing."""
        await self.async_ensure_loaded()
        self._refuse_orphan_move()
        orphan = self._orphan(key)
        self._check_writable()
        pages, rules = _merge_moved(orphan, self._template["pages"], self._template["rules"])
        self._template = {**self._template, "pages": pages, "rules": rules}
        del self._overrides[key]
        await self._save()
        self._announce(None)

    async def async_delete_orphan(self, key: str) -> None:
        """Delete the 'Not attached' override `key`. Raises ProfileError if it is not one."""
        await self.async_ensure_loaded()
        self._orphan(key)
        self._check_writable()
        del (self._devices if self._model6 else self._overrides)[key]
        await self._save()

    # ---- the editor's whole-document write (M6 E2) --------------------------

    def _known_target(self) -> Callable[[str, str], bool]:
        """Is a call button's target one a save may name? As in `async_set`: a call button already stored keeps its
        target even when the target has vanished (editing something else must still save); a new or changed one
        must be known to the placement registry or the endpoint registry."""
        document = self._expanded()         # a reference to a call element counts through its expansion
        existing = {(a["target"]["type"], a["target"]["id"])
                    for page in all_pages(document) for a in page_actions(page) if a.get("kind") == "call"}
        registry = get_call_registry(self._hass)
        endpoints = get_endpoint_registry(self._hass)

        def known(target_type: str, target_id: str) -> bool:
            return ((target_type, target_id) in existing
                    or registry.target_state(target_type, target_id) is not None
                    or (not is_app_screen(self._hass, target_id)
                        and endpoints.get(target_id, kind=target_type) is not None))

        return known

    def _check_element_refs(self, document: dict[str, Any], *, elements_by_id: dict[str, Any] | None = None,
                            stored_refs: frozenset[tuple[Any, ...]] | None = None) -> None:
        """Every reference in `document` must point at a placeable camera element (see `content_elements.check_refs`); one the
        STORED document already holds may have lost its element and is kept. Synchronous, so the last call in a save
        sits in the section with no await. Raises ProfileError.

        `elements_by_id` (the YAML apply, DEV-824 P2) checks against the elements a whole new configuration WOULD hold instead of
        the live ones, and `stored_refs` replaces the "already stored" allowance; nothing is being deleted then (`pending`)."""
        if not has_refs(document):
            return
        from .elements import get_elements_store

        elements = get_elements_store(self._hass)
        try:
            base = self._known_target()
            declared = document.get("dashboards") or {}

            def known(target_type: str, target_id: str) -> bool:
                # a screen declared in this same document can be called in it, as `normalize_document` allows
                return (target_type == "dashboard" and target_id in declared and not is_app_screen(self._hass, target_id)) \
                    or base(target_type, target_id)

            check_refs(document, elements.elements_by_id() if elements_by_id is None else elements_by_id,
                       stored_ids=self.referenced_element_ids(),
                       stored_refs=stored_ref_keys(self._data()) if stored_refs is None else stored_refs,
                       pending=elements.pending_delete, known_target=known,
                       group_hosts=frozenset(panel_users(self._hass).values()),
                       live_keys=frozenset(ep.id for ep in get_endpoint_registry(self._hass).endpoints()
                                           if ep.kind in ("phone", "dashboard")))
        except ElementRefError as err:
            raise ProfileError(f"{err.code}: {err}") from None

    def prepare_document(self, raw: Any, *, elements_by_id: dict[str, Any] | None = None,
                         stored_refs: frozenset[tuple[Any, ...]] | None = None) -> dict[str, Any]:
        """`raw` validated as a whole document (nothing written), plus the check that needs the live endpoints: a
        screen's slug must not be the id of a phone or a door. What `content/preview` resolves and `content/save`
        stores. Raises ProfileError. `elements_by_id` and `stored_refs`: see `_check_element_refs` (the YAML apply)."""
        refuse_flat_pages(raw)
        document = normalize_document(raw, known_target=self._known_target(), app_screens=self._app_screen_ids())
        fresh = duplicate_action_ids(document) - duplicate_action_ids(self._data())
        if fresh:
            scope, action_id = sorted(fresh)[0]
            raise ProfileError(f"{scope}: the button id {action_id!r} is used on two pages; a press names a button by id, "
                               "so every button id must be unique across the pages of one phone or screen")
        self._check_element_refs(document, elements_by_id=elements_by_id, stored_refs=stored_refs)
        taken = {ep.id for ep in get_endpoint_registry(self._hass).endpoints() if ep.kind != "dashboard"}
        for slug in document["dashboards"]:
            if slug in taken:
                raise ProfileError(f"dashboard {slug!r}: that id belongs to a phone or a door; choose another slug")
        self._check_panel_screens(document)
        return document

    def _check_panel_screens(self, document: dict[str, Any]) -> None:
        """A NEW screen cannot be declared for a panel's login (own-pages design section 2): a panel owns its pages on its
        phone, and a second identity for the same login would be a house-call callee the panel is not. A screen already
        declared for that login (a former panel screen) may stay, be edited, re-pointed or deleted. Synchronous. Raises SaveRefused
        (`panel_user_screen`)."""
        panels = panel_users(self._hass)
        if not panels:
            return
        for slug, wanted in (document.get("dashboards") or {}).items():
            user_id = wanted.get("ha_user_id") if isinstance(wanted, dict) else None
            if user_id not in panels:
                continue
            before = self.dashboards.get(slug)
            if isinstance(before, dict) and before.get("ha_user_id") == user_id:
                continue
            name = wanted.get("name") or slug
            raise SaveRefused("panel_user_screen", f'The screen "{name}" cannot use the login of a panel: a panel owns its pages '
                              "itself (Devices, Pages on the panel)", screen=slug)

    async def async_save_document(self, raw: Any, base_doc_rev: str, *, drop_orphans: tuple[str, ...] = ()) -> str:
        """Replace the whole stored document with `raw`, validated whole (`prepare_document`), when `base_doc_rev`
        is still the stored document's hash; returns the new `doc_rev`. Raises SaveConflict (stale base) or
        ProfileError (invalid, or the HA-L9 refusal over a newer schema). Nothing is changed on a refusal.

        It never drops a "Not attached" override by omission: a document that leaves one out is refused, and an
        administrator who means to delete or move one names it in `drop_orphans`. The write is `_save`, the one
        write path, so the one-time copy of an older file and the refusal over a newer one apply as everywhere."""
        await self.async_ensure_loaded()
        self._check_writable()
        if base_doc_rev != self.doc_rev:
            raise SaveConflict("the document changed since it was loaded")
        self._check_library_delete(raw)
        raw = self._prune_leftover_page_refs(raw)
        document = self.prepare_document(raw)
        if is_pages_model(document) != self._model6:
            # The model changes only by the explicit migration (`portico/content/pages_migrate`), never by a save.
            raise ProfileError("this install uses the unified Pages model (schema 6); reload the Portico page"
                               if self._model6 else
                               "schema 6 is written only by the Pages migration; the document is still the template-and-"
                               "overrides model (schema 4 or 5)")
        held = document["devices"] if self._model6 else document["overrides"]
        orphans = set(self.orphan_keys())
        for key in drop_orphans:
            if key not in orphans:
                raise ProfileError(f"{key!r} is not a 'Not attached' override")
            if key in held:
                raise ProfileError(f"{key!r} is to be deleted and is also in the document")
        lost = sorted(k for k in orphans if k not in held and k not in drop_orphans)
        if lost:
            raise ProfileError(f"the document leaves out the 'Not attached' override {lost[0]!r}; keep it, move it "
                               "or name it to be deleted")
        for slug, dashboard in document["dashboards"].items():
            stored = self._dashboards.get(slug)
            if stored is None or stored.get("ha_user_id") != dashboard["ha_user_id"]:
                if await self._hass.auth.async_get_user(dashboard["ha_user_id"]) is None:
                    raise ProfileError(f"dashboard {slug!r}: there is no Home Assistant user {dashboard['ha_user_id']!r}")
        # The user lookups awaited: another save may have landed meanwhile. Nothing awaits between this check and
        # the in-memory change, so two saves cannot both pass it.
        self._check_writable()
        if base_doc_rev != self.doc_rev:
            raise SaveConflict("the document changed since it was loaded")
        self._check_element_refs(document)      # an element may have been removed during the awaits above
        self._check_panel_screens(document)     # a panel may have been made during the awaits above
        if document == self._data():
            return self.doc_rev
        before = self._state()
        self._adopt(document)
        try:
            await self._save()
        except BaseException:
            self._restore(before)
            raise
        self._announce(None)        # every subscriber refetches: a template or home name change reaches all of them
        return self.doc_rev

    # ---- the YAML apply's two-step write (DEV-824 P2: config_apply.py) ---------------

    def stage_applied(self, document: dict[str, Any]) -> tuple[Any, ...]:
        """Make a validated whole document (`prepare_document`) the in-memory one, WITHOUT writing or announcing; returns the state
        to hand back to `restore_applied` / `commit_applied`. Synchronous. Raises ProfileError (a newer schema, or a document of
        the other model: a file apply never changes the model)."""
        self._check_writable()
        if is_pages_model(document) != self._model6:
            raise ProfileError("the files describe the unified Pages model (schema 6) and this install does not use it" if not self._model6
                               else "this install uses the unified Pages model (schema 6)")
        before = self._state()
        self._adopt(document)
        return before

    async def commit_applied(self, before: tuple[Any, ...]) -> None:
        """Write what `stage_applied` put in memory; on any failure the memory goes back to `before` and the error is raised."""
        try:
            await self._save()
        except BaseException:
            self._restore(before)
            raise

    def restore_applied(self, before: tuple[Any, ...]) -> None:
        self._restore(before)

    def announce_applied(self) -> None:
        """Every content subscriber refetches (`content_changed`, no rev). Never fires `portico_config_changed`."""
        self._announce(None)

    def _state(self) -> tuple[Any, ...]:
        """Every in-memory container of the document, for a rollback after a failed write."""
        return (self._template, self._overrides, self._dashboards, self._endpoint_decls, self._home_name, self._show_fps,
                self._model6, self._library, self._devices, self._roster)

    def _restore(self, state: tuple[Any, ...]) -> None:
        (self._template, self._overrides, self._dashboards, self._endpoint_decls, self._home_name, self._show_fps,
         self._model6, self._library, self._devices, self._roster) = state

    def _adopt(self, document: dict[str, Any]) -> None:
        """Make a validated document (`prepare_document`) the in-memory one. The model is the document's own: a save has
        checked it equals the store's; the migration's apply is the one caller that changes it."""
        if is_pages_model(document):
            self._model6 = True
            self._template, self._overrides = _empty_template(), {}      # the legacy containers are gone, not hidden
            self._library, self._devices = document["pages"], document["devices"]
            self._roster = document.get("roster", [ROSTER_ALL])
        else:
            self._model6 = False
            self._template, self._overrides = document["template"], document["overrides"]
        self._dashboards, self._endpoint_decls = document["dashboards"], document["endpoints"]
        self._home_name = document.get("home_name", "")
        self._show_fps = document.get("show_fps", False)

    def _call_state(self, action: dict[str, Any], viewer=None) -> dict[str, Any]:
        """{label, available, reason} for a call button. Never raises: a registry that errors means off.

        Two sources (ENDPOINT-03). The placement registry (`call_endpoints`, where DEV-610 and gate-sip G2
        plug in) is asked first: a backend that places calls owns the state of what it can place. When it
        does not know the target, the endpoint registry supplies the name and the availability of the
        endpoint itself (a door's provider says whether it is up), but no call can be placed to it yet, so
        it is off with `not_available` (or the endpoint's own reason when it is down)."""
        target = action["target"]
        registry = get_call_registry(self._hass)
        if target["type"] == "group":
            # A Talk group element's tile (group-call design): the house registry reads the set as `viewer` sees it (the caller
            # is never rung, so "no other panel" depends on who is looking). Anything else is off, never "unknown".
            ask = getattr(registry, "group_target_state", None)
            try:
                state = ask(target["id"], viewer) if callable(ask) else None
            except Exception as err:  # a backend bug must not take content/get down
                _LOGGER.warning("portico content: group state failed: %s", type(err).__name__)
                state = None
            if state is None:
                return {"label": action["label"], "available": False, "reason": REASON_NOT_AVAILABLE}
            return {"label": state.label or action["label"], "available": bool(state.available),
                    "reason": None if state.available else (state.reason or REASON_NOT_AVAILABLE)}
        try:
            state = registry.target_state(target["type"], target["id"])
        except Exception as err:  # a backend bug must not take content/get down
            _LOGGER.warning("portico content: call registry failed for %s: %s", target["type"], type(err).__name__)
            state = None
        if state is None:
            endpoint = self._endpoint(target)
            gate = self._gate_state(action, endpoint, viewer)
            if gate is not None:
                return gate
            if endpoint is not None:
                reason = REASON_NOT_AVAILABLE if endpoint.available else (endpoint.reason or REASON_NOT_AVAILABLE)
                return {"label": endpoint.name or action["label"], "available": False, "reason": reason}
            # Known to neither: the default registry (calls not set up on this install) or a target that
            # was removed. The label falls back to the button's own.
            reason = REASON_NOT_AVAILABLE if isinstance(registry, NullCallEndpointRegistry) else REASON_UNKNOWN_TARGET
            return {"label": action["label"], "available": False, "reason": reason}
        available = bool(state.available)
        # An available target has no reason, except `push_only` (house calls 3c): callable, and it rings by notification.
        reason = state.reason if available and state.reason == REASON_PUSH_ONLY else (
            None if available else (state.reason or REASON_NOT_AVAILABLE))
        return {"label": state.label or action["label"], "available": available, "reason": reason}

    def _gate_state(self, action: dict[str, Any], endpoint, viewer) -> dict[str, Any] | None:
        """The state of a call button that targets the gate (M7 3f), or None when the target is not a door that reports
        `CanDial` (then the state is what it was before this existed: `not_available`, or the door's own reason). The
        answer is Talk's: the same view, `_talk_view`, so a Talk button and a call to the gate can never disagree. Only a
        phone can place it; `viewer` None (a caller's buttons, which have no viewer) is not held to that."""
        if endpoint is None or endpoint.kind != KIND_DOOR or endpoint.can_dial is not True:
            return None
        label = endpoint.name or action["label"]
        view = self._talk_view(endpoint.id, viewer, anyone=viewer is None)
        if view["available"]:
            return {"label": label, "available": True, "reason": None}
        reason = {"phone_only": REASON_PHONE_ONLY, "no_door": REASON_UNKNOWN_TARGET, "not_connected": REASON_OFFLINE,
                  "busy": REASON_BUSY}.get(view["reason"], REASON_NOT_AVAILABLE)
        return {"label": label, "available": False, "reason": reason}

    def _gate_door_id(self, target: dict[str, Any]) -> str | None:
        """The door endpoint id of a call target that is the gate and may be placed through Talk's path, else None:
        the target is a door, the placement registry does not know it (a registry that places it owns it), and its driver
        reports `CanDial` now. With the dial off this is None for every target, so a call to a door goes where it went."""
        if target.get("type") != KIND_DOOR:
            return None
        try:
            if get_call_registry(self._hass).target_state(target["type"], target["id"]) is not None:
                return None
        except Exception:      # a registry that errors owns nothing; the old path then fails as it did
            return None
        endpoint = self._endpoint(target)
        if endpoint is None or endpoint.kind != KIND_DOOR or endpoint.can_dial is not True:
            return None
        return endpoint.id

    def _endpoint(self, target: dict[str, Any]):
        """The endpoint registry's entry for a call target, or None (never raises)."""
        try:
            if target["type"] == "dashboard" and is_app_screen(self._hass, target["id"]):
                return None         # a panel's app screen is not a callee; its phone half is (DEV-807)
            return get_endpoint_registry(self._hass).get(target["id"], kind=target["type"])
        except Exception as err:
            _LOGGER.warning("portico content: endpoint registry failed for %s: %s", target["type"], type(err).__name__)
            return None

    def _available(self, entity_id: str) -> bool:
        state = self._hass.states.get(entity_id)
        return state is not None and state.state not in ("unavailable", "unknown")

    def _action_available(self, action: dict[str, Any]) -> bool:
        if action["kind"] == "call":
            return self._call_state(action)["available"]
        if action["kind"] == "talk":
            return False        # no viewer here; a Talk button's real state is `_talk_view` / `talk_state` (viewer-aware)
        if action["kind"] == "portico_open":
            return self._portico_door(action["device_id"]) is not None
        return self._available(action["entity_id"])

    def _portico_door(self, device_id: str):
        """(provider, Door) for a door id, through the provider registry (WS-06); None when no provider
        supplies it (or two do: an ambiguous bare id is not a door a profile can name)."""
        registry = self._hass.data.get(DOMAIN, {}).get(DATA_PROVIDERS)
        if registry is None:
            return None
        try:
            return registry.door(device_id)
        except ProviderError:
            return None

    async def async_run(self, user_id: str, phone: str, action_id: str, *, caller_user_id: str | None = None,
                        caller_name: str | None = None, endpoint_id: str | None = None) -> dict[str, Any]:
        """Run one action of the profile `user_id` (the caller's own, or, for an admin, a named phone's); with
        `endpoint_id` the grant is that endpoint's document (a screen's, or a named phone's), `user_id` and `phone`
        then being the endpoint's HA user and display name.
        Raises RunRefused. Returns the extra fields for the result frame (`{}` for every kind but `call`,
        which returns `call_id` and `media`). A service call carries the CALLER's HA user in its Context, so
        HA's audit trail names who acted; when that is not the profile's user (an admin running a phone's
        button) the logbook line names both."""
        acting_user = caller_user_id or user_id
        by = None if acting_user == user_id else f"run by {caller_name or 'an admin'}"
        await self.async_ensure_loaded()
        endpoint_id = endpoint_id or self._endpoint_id(user_id)
        matches = [a for a in self._all_actions(endpoint_id) if a["id"] == action_id]
        if len({json.dumps(a, sort_keys=True) for a in matches}) > 1:
            # Ids are unique per page only. Two DIFFERENT buttons with this id are in the caller's page set, so which one the
            # phone showed cannot be known: run nothing (fail closed), never the first one.
            self._log(phone, f'refused "{action_id}" (two different buttons share this id)', None, by)
            raise RunRefused("unknown_action", "this button's id is used twice on your pages; ask an administrator to fix it")
        action = matches[0] if matches else None
        if action is None:
            action = self._roster_action(endpoint_id, action_id)
        label = action["label"] if action else action_id
        if action is None:
            self._log(phone, f'refused "{label}" (not in this phone\'s profile)', None, by)
            raise RunRefused("unknown_action", "no such action in this phone's profile")
        if action["kind"] == "talk":
            # In the grant (so the id is known); `_run_talk` is the gate. Nothing dials unless it lets it.
            if action.get("door"):
                # a Talk element: its own explicit door, not the block's and not the one-gate pick (DEV-518)
                return await self._run_talk(user_id, phone, action, endpoint_id, by, caller_user_id,
                                            door_id=action["door"], explicit=True)
            return await self._run_talk(user_id, phone, action, endpoint_id, by, caller_user_id)
        if action["kind"] == "call" and (gate_door := self._gate_door_id(action["target"])) is not None:
            # The gate as a call target (M7 3f): only while its door reports `CanDial`; placed by Talk's path, the same
            # gate, `dial` and `talk_state` relay. Any other call (and the gate with the dial off) is placed as before.
            return await self._run_talk(user_id, phone, action, endpoint_id, by, caller_user_id, door_id=gate_door)
        now = time.monotonic()
        key = (endpoint_id, action_id)
        if now - self._last_run.get(key, -RUN_INTERVAL) < RUN_INTERVAL:
            self._log(phone, f'refused "{label}" (too soon after the last run)', action.get("entity_id"), by)
            raise RunRefused("rate_limited", "this action just ran; wait a moment")
        if action["kind"] == "call":
            return await self._run_call(user_id, phone, action, now, key, by, acting_user)
        if not self._action_available(action):
            self._log(phone, f'refused "{label}" (unavailable)', action.get("entity_id"), by)
            raise RunRefused("unavailable", "that device or entity is not available right now")
        self._last_run[key] = now

        if action["kind"] == "portico_open":
            provider, door = self._portico_door(action["device_id"])
            ctx = actions.begin_open(
                self._hass, actor=(phone if acting_user == user_id else caller_name or phone), actor_user=acting_user, source=actions.source_for(self._hass, acting_user),
                door_id=door.id, door_name=door.name)
            try:
                await provider.open_relay(door.id, context=ctx)
            except Exception as err:   # a provider's failure carries `code` and `message` (provider_v1.md)
                code = error_code(err) or "internal"
                self._log(phone, f'"{label}" failed ({code})', None, by)
                raise RunRefused(code, getattr(err, "message", None) or "the door could not be opened") from err
        else:
            _entity_domain, service_call, _confirm = KINDS[action["kind"]]
            service_domain, service = service_call
            try:
                await self._hass.services.async_call(
                    service_domain, service, {"entity_id": action["entity_id"]},
                    blocking=True, context=Context(user_id=acting_user),
                )
            except Exception as err:  # the call is the integration's own; any failure is a refusal to the phone
                _LOGGER.warning("portico content: %s for %s failed: %s", action["kind"], phone, type(err).__name__)
                self._log(phone, f'"{label}" failed ({type(err).__name__})', action["entity_id"], by)
                raise RunRefused("failed", "the action could not be run") from err
        self._log(phone, f'ran "{label}"', action.get("entity_id"), by)
        return {}

    async def _run_call(self, user_id: str, phone: str, action: dict[str, Any], now: float,
                        key: tuple[str, str], by: str | None = None, acting_user: str | None = None) -> dict[str, Any]:
        """Place a call through the registry. Never raises anything but RunRefused; never touches a client
        value (the target comes from the stored profile)."""
        label, target = action["label"], action["target"]
        registry = get_call_registry(self._hass)
        self._last_run[key] = now
        try:
            result = await registry.place(caller_user_id=user_id, caller_phone=phone,
                                          target_type=target["type"], target_id=target["id"],
                                          caller_endpoint=key[0], acting_user_id=acting_user)
        except Exception as err:  # any backend failure is a refusal to the phone, not a crash
            _LOGGER.warning("portico content: placing a call for %s failed: %s", phone, type(err).__name__)
            self._log(phone, f'"{label}" failed ({type(err).__name__})', None, by)
            raise RunRefused("failed", "the call could not be placed") from err
        if not result.ok:
            code = result.code or "failed"
            self._log(phone, f'refused "{label}" ({code})', None, by)
            raise RunRefused(code, result.message or "the call could not be placed")
        self._log(phone, f'ran "{label}" (rang)', None, by)
        return {"call_id": result.call_id, "media": result.media}

    def _granted_camera(self, phone: str, camera_id: str, endpoint_id: str, what: str) -> dict[str, Any]:
        """THE camera grant, shared by `content/snapshot` and `content/live` so the two cannot drift: the camera of
        this id in the endpoint's resolved document (a page camera, or a `c-...` camera of one of its callers). An id
        that is not there is `unknown_camera` and is written to the logbook; no entity id is ever accepted or guessed.
        `what` ("a snapshot", "a live view") is only the logbook's wording. Raises RunRefused."""
        camera = next((c for c in self._all_cameras(endpoint_id) if c["id"] == camera_id), None)
        if camera is None:
            self._log(phone, f'refused {what} of "{camera_id}" (not in this phone\'s profile)', None)
            raise RunRefused("unknown_camera", "no such camera in this phone's profile")
        return camera

    async def async_live_camera(self, user_id: str, phone: str, camera_id: str, *,
                                endpoint_id: str | None = None) -> str:
        """The main entity only; see `async_live_cameras`."""
        return (await self.async_live_cameras(user_id, phone, camera_id, endpoint_id=endpoint_id))[0]

    async def async_live_cameras(self, user_id: str, phone: str, camera_id: str, *,
                                 endpoint_id: str | None = None) -> tuple[str, str | None]:
        """The entity id of a camera the endpoint may watch LIVE (`content/live`): the snapshot's grant
        (`_granted_camera`), then the same availability test. The entity id stays on the server. Raises RunRefused
        (`unknown_camera`, `unavailable`). DEV-768: `(main entity, small entity or None)`; the small one is the element's own
        `small_entity_id`, reached only through this grant, and is not tested here (a missing small stream falls back to main)."""
        await self.async_ensure_loaded()
        endpoint_id = endpoint_id or self._endpoint_id(user_id)
        camera = self._granted_camera(phone, camera_id, endpoint_id, "a live view")
        if not self._available(camera["entity_id"]):
            raise RunRefused("unavailable", "that camera is not available right now")
        return camera["entity_id"], camera.get("small_entity_id") or small_entity_of(self._elements_by_id(), camera["entity_id"])

    async def async_snapshot(self, user_id: str, phone: str, camera_id: str, width: int, *,
                             endpoint_id: str | None = None) -> tuple[str, bytes]:
        """One still from a camera in the caller's own profile (or the `endpoint_id` document): (content type, bytes).

        The app polls this every few seconds while its home screen is open, so a
        success is NOT written to the logbook; a refusal is. Raises RunRefused."""
        await self.async_ensure_loaded()
        endpoint_id = endpoint_id or self._endpoint_id(user_id)
        camera = self._granted_camera(phone, camera_id, endpoint_id, "a snapshot")
        now = time.monotonic()
        key = (endpoint_id, "snapshot:" + camera_id)
        if now - self._last_run.get(key, -SNAPSHOT_INTERVAL) < SNAPSHOT_INTERVAL:
            raise RunRefused("rate_limited", "a snapshot of this camera was just taken; wait a moment")
        if not self._available(camera["entity_id"]):
            raise RunRefused("unavailable", "that camera is not available right now")
        self._last_run[key] = now

        from homeassistant.components import camera as camera_component

        try:
            image = await camera_component.async_get_image(self._hass, camera["entity_id"], width=width)
        except Exception as err:  # a camera that errors or times out is a refusal to the phone, not a crash
            _LOGGER.debug("portico content: snapshot of %s failed: %s", camera["entity_id"], type(err).__name__)
            raise RunRefused("failed", "the camera did not give a picture") from err
        if len(image.content) > MAX_SNAPSHOT_BYTES:
            raise RunRefused("too_large", "the picture is too large to send")
        return image.content_type, bytes(image.content)

    def _log(self, phone: str, message: str, entity_id: str | None, by: str | None = None) -> None:
        if by:
            message = f"{message} ({by})"
        data: dict[str, Any] = {"name": f"Portico: {phone}", "message": message, "domain": "portico"}
        if entity_id:
            data["entity_id"] = entity_id
        self._hass.bus.async_fire("logbook_entry", data)


def phone_facts(hass: HomeAssistant, user_id: str, phone: dict[str, Any]) -> dict[str, Any]:
    """What tells two enrolled phones of the SAME name apart (each enrolment makes a new phone user, so a
    re-enrolled phone shows up twice): the day it was enrolled, and whether the app on it has registered
    with the push ring notification (a push registration row exists for that user). `registered` is None when the away
    ring is not set up, so it cannot say."""
    from .push import get_manager

    manager = get_manager(hass)
    rows = None if manager is None else manager.registry.rows_for_user(user_id)
    registered = None if rows is None else bool(rows)
    # the app version is the LAST SEEN one: the row that registered most recently (each `push/register` rewrites the
    # row's `app_version` and `last_seen_at`), not whatever the phone had when it was enrolled
    version = None
    if rows:
        latest = max(rows, key=lambda r: str(r.get("last_seen_at") or ""))
        version = str(latest.get("app_version") or "").strip()[:32] or None
    return {"enrolled_at": str(phone.get("enrolled_at") or "")[:10] or None, "registered": registered,
            "app_version": version}


def phone_label(name: str, facts: dict[str, Any]) -> str:
    """\"Alice's phone (enrolled 2026-10-08, app 0.5.10)\": the editor's dropdown text. The version is the last one the
    app registered with; "app registered" when it gave none; "app not registered" when no registration exists."""
    bits = []
    if facts.get("enrolled_at"):
        bits.append(f"enrolled {facts['enrolled_at']}")
    if facts.get("registered") is True:
        bits.append(f"app {facts['app_version']}" if facts.get("app_version") else "app registered")
    elif facts.get("registered") is False:
        bits.append("app not registered")
    return f"{name} ({', '.join(bits)})" if bits else name


def announce_gate_change(hass: HomeAssistant) -> None:
    """The set of doors that can dial changed (M7 3f): the house roster `rev` moved, so tell the attached clients
    (`house_roster_changed`) when house calls are on. The document `rev` does not move (the gate's roster flag and
    Talk's availability are live state, `content_resolve._VOLATILE`), but a client only re-reads live state when it
    refetches, so every content subscriber is told to (`content_changed`): an open app shows or removes the Talk button
    without a restart, as gate-sip's dial switch is turned on or off at runtime (CanDial republished by the driver)."""
    from .house import get_house

    try:
        get_content_store(hass).announce_all()
    except Exception:     # noqa: BLE001 - best effort: the house announcement below must still go out
        _LOGGER.warning("portico content: the CanDial change could not be announced to content subscribers", exc_info=True)

    house = get_house(hass)
    if house is not None:
        house.roster_changed()


def get_content_store(hass: HomeAssistant) -> ContentStore:
    domain_data = hass.data.setdefault("portico", {})
    store = domain_data.get(DATA_CONTENT)
    if store is None:
        store = domain_data[DATA_CONTENT] = ContentStore(hass)
    return store
