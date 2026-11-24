"""Elements: named, reusable pieces of UI (camera, button, status, chip) for the Portico Elements page (DEV-760, PR E1).

Design: home `portico_elements_design.md` sections 1 to 4. This module is the STORE ONLY: `.storage/portico.elements`,
its validators and the `KIND_SPECS` registry. It has no websocket command, no UI and no caller yet (E2 adds the
commands), it never touches the content store (ELEM-07), and nothing here runs an action or writes to Home Assistant.

File: `{schema_version: 1, elements: [ ... ]}`, the list order is the display order. An element is
`{id, kind, name, icon?, entity_id?, options: {...}}`; the id is minted by the server (`el-` + 8 hex) or chosen by the author of the YAML files (`el-<slug>`, 3 to 40 of a-z 0-9 -) and never changes,
the name is the user's (1..40 chars, unique case-insensitively). Unknown keys are refused.

Schema and migration: `MIGRATIONS[n]` takes the data of a schema-n file to schema n+1 in memory (empty today). Nothing
is written until the first normal save, which first leaves a one-time copy `portico.elements.v<n>` (never
overwritten; a copy that cannot be written is logged and never blocks the save). A file NEWER than this build is read
as far as it can be and every write is refused (`newer_schema`).

Loading is lenient: a stored element that fails validation is dropped with one warning (its id only, no values); an
element whose kind this build does not know is kept verbatim, listed with `kind_unknown: true`, deletable, and written
back unchanged.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .content import (
    CAMERA_SIZES,
    ENTITY_RE,
    ICON_NAMES,
    KINDS,
    MAX_ENDPOINT_ID,
    MAX_LABEL,
    PALETTE_NAMES,
    get_content_store,
)
from .content_elements import TALK_GROUPS

_LOGGER = logging.getLogger(__name__)

STORE_KEY = "portico.elements"
SCHEMA_VERSION = 1
DATA_ELEMENTS = "elements_store"
MAX_ELEMENTS = 200
MAX_STATES = 16
MAX_STATE_LEN = 40
MAX_SHORT = 12
MAX_STREAM_LEN = 256
ID_RE = re.compile(r"^(?:el-[a-z0-9-]{3,40}|talk-(?:screen|phone)-[A-Za-z0-9_-]{1,128})$")   # el-xxxxxxxx (minted) or el-<slug> (author-chosen in the YAML files, DEV-824 Q5), and the managed Talk ids
MANAGED_ID_RE = re.compile(r"^talk-(screen|phone)-([A-Za-z0-9_-]{1,128})$")
TALK_TARGETS = ("door", "screen", "phone", "group")   # a Talk element names exactly one of these in `options`
DEFAULT_TALK_ICON = "phone"          # a Talk element with no icon of its own shows a handset (stored explicitly)

# Open question A (stream-only cameras). Design section 2 allows a camera with a credential-free `stream`; the parallel
# design defaulted to an HA camera entity only. Flip this ONE flag to False for entity-only: a `stream` is then refused
# and a camera needs an entity_id.
CAMERA_ALLOWS_STREAM = True

# schema n -> function taking the schema-n data dict to schema n+1. Empty: there is no older schema yet.
MIGRATIONS: dict[int, Callable[[dict], dict]] = {}


class ElementError(Exception):
    """A refused element or write. `code` is stable (invalid, name_taken, conflict, unknown_element, kind_locked,
    too_many, newer_schema, in_use, in_use_action, in_use_stream, migration_missing); `field` names the offending field for `invalid`. The message
    never carries a stored value."""

    def __init__(self, code: str, message: str = "", *, field: str | None = None, used_by: list | None = None) -> None:
        super().__init__(message or code)
        self.code = code
        self.field = field
        self.used_by = used_by


def _bad(field_name: str, message: str) -> ElementError:
    return ElementError("invalid", message, field=field_name)


# ---- field validators (each returns the clean value or raises ElementError) ----

def _clean_name(value: Any) -> str:
    if not isinstance(value, str):
        raise _bad("name", "name must be text")
    name = value.strip()
    if not name or len(name) > MAX_LABEL:
        raise _bad("name", f"name must be 1 to {MAX_LABEL} characters")
    return name


# Kinds that show any Material Design icon (the panel and card draw them with ha-icon); the rest keep the fixed ICON_NAMES set
# a button with hidden text can show (the phone app draws buttons from it).
FREE_ICON_KINDS = frozenset({"status", "chip"})
ICON_SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
MAX_ICON_LEN = 64


def icon_policy() -> dict[str, str]:
    """Per kind: "mdi" (any well-formed mdi name) or "fixed" (one of ICON_NAMES). For the elements/list reply."""
    return {kind: "mdi" if kind in FREE_ICON_KINDS else "fixed" for kind in KIND_SPECS}


def _clean_icon(value: Any, kind: str = "button") -> str:
    if not isinstance(value, str):
        raise _bad("icon", "icon must be text")
    icon = value.removeprefix("mdi:")
    if kind in FREE_ICON_KINDS:
        if not (1 <= len(icon) <= MAX_ICON_LEN) or not ICON_SLUG_RE.match(icon):
            raise _bad("icon", "unknown icon")
        return icon
    if icon not in ICON_NAMES:
        raise _bad("icon", "unknown icon")
    return icon


def _clean_entity(value: Any, domain: str | None = None, field_name: str = "entity_id") -> str:
    if not isinstance(value, str) or not ENTITY_RE.match(value):
        raise _bad(field_name, "not an entity id")
    if domain is not None and value.split(".", 1)[0] != domain:
        raise _bad(field_name, f"must be a {domain} entity")
    return value


def _clean_stream(value: Any) -> str:
    """`rtsp://` or `rtsps://` host[:port]/path with no credentials, query or fragment (as live_rtsp: Portico never stores
    a password in a document)."""
    if not isinstance(value, str) or not value or len(value) > MAX_STREAM_LEN or any(c.isspace() for c in value):
        raise _bad("options.stream", "not a stream address")
    try:
        parts = urlsplit(value)
        host, _port = parts.hostname, parts.port
    except ValueError:
        raise _bad("options.stream", "not a stream address") from None
    if parts.scheme not in ("rtsp", "rtsps") or not host:
        raise _bad("options.stream", "must be rtsp:// or rtsps:// with a host")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise _bad("options.stream", "must not contain credentials")
    if parts.query or parts.fragment or "?" in value or "#" in value:
        raise _bad("options.stream", "must not contain a query or fragment")
    if len(parts.path) < 2:
        raise _bad("options.stream", "needs a path")
    return value


def _clean_choice(value: Any, allowed: tuple[str, ...] | frozenset | list, field_name: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise _bad(field_name, "not an allowed value")
    return value


def _clean_states(value: Any) -> dict[str, dict[str, str]]:
    if not isinstance(value, dict) or len(value) > MAX_STATES:
        raise _bad("options.states", f"states must be a map of at most {MAX_STATES} entries")
    out: dict[str, dict[str, str]] = {}
    for state, rule in value.items():
        if not isinstance(state, str) or not state or len(state) > MAX_STATE_LEN:
            raise _bad("options.states", f"a state must be 1 to {MAX_STATE_LEN} characters")
        if not isinstance(rule, dict) or set(rule) - {"label", "color"}:
            raise _bad("options.states", "a state rule holds only label and color")
        clean: dict[str, str] = {}
        if "label" in rule:
            label = rule["label"]
            if not isinstance(label, str) or not label.strip() or len(label.strip()) > MAX_STATE_LEN:
                raise _bad("options.states", f"a label must be 1 to {MAX_STATE_LEN} characters")
            clean["label"] = label.strip()
        if "color" in rule:
            clean["color"] = _clean_choice(rule["color"], PALETTE_NAMES, "options.states")
        out[state] = clean
    return out


def _clean_attribute(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9_]{1,64}", value):
        raise _bad("options.attribute", "not an attribute name")
    return value


def _clean_short(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > MAX_SHORT:
        raise _bad("options.short", f"short must be 1 to {MAX_SHORT} characters")
    return value.strip()


def _clean_target(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_ENDPOINT_ID:
        raise _bad("options.target", "not a target")
    return value


# ---- per-kind validators: (entity_id or None, options dict) -> (clean entity_id or None, clean options) ----

def _check_option_keys(options: dict, allowed: set[str]) -> None:
    extra = set(options) - allowed
    if extra:
        raise _bad("options", "unknown option")


def _validate_camera(entity_id: Any, options: dict) -> tuple[str | None, dict]:
    _check_option_keys(options, {"stream", "size", "small_entity_id"} if CAMERA_ALLOWS_STREAM else {"size", "small_entity_id"})
    has_stream = "stream" in options
    if (entity_id is None) == (not has_stream) and CAMERA_ALLOWS_STREAM:
        raise _bad("entity_id", "a camera needs exactly one of entity_id and stream")
    if not CAMERA_ALLOWS_STREAM and entity_id is None:
        raise _bad("entity_id", "a camera needs an entity_id")
    clean: dict[str, Any] = {}
    if has_stream:
        clean["stream"] = _clean_stream(options["stream"])
    if "size" in options:
        clean["size"] = _clean_choice(options["size"], CAMERA_SIZES, "options.size")
    clean_entity = None if entity_id is None else _clean_entity(entity_id, "camera")
    if "small_entity_id" in options:
        # DEV-768: the small stream of the same camera; the element's own entity_id is the big one (or the only one)
        if has_stream or clean_entity is None:
            raise _bad("options.small_entity_id", "a small stream needs a camera entity, not a stream address")
        small = _clean_entity(options["small_entity_id"], "camera", "options.small_entity_id")
        if small == clean_entity:
            raise _bad("options.small_entity_id", "the small stream must be a different camera")
        clean["small_entity_id"] = small
    return clean_entity, clean


def _validate_button(entity_id: Any, options: dict) -> tuple[str | None, dict]:
    _check_option_keys(options, {"action", "target", "color"})
    if "action" not in options:
        raise _bad("options.action", "a button needs an action")
    action = _clean_choice(options["action"], tuple(KINDS), "options.action")
    domain = KINDS[action][0]
    if domain is None:
        if entity_id is not None:
            raise _bad("entity_id", "this action takes no entity")
        clean_entity = None
    else:
        if entity_id is None:
            raise _bad("entity_id", f"this action needs a {domain} entity")
        clean_entity = _clean_entity(entity_id, domain)
    clean: dict[str, Any] = {"action": action}
    if "target" in options:
        if action not in ("portico_open", "call", "talk"):
            raise _bad("options.target", "this action takes no target")
        clean["target"] = _clean_target(options["target"])
    if "color" in options:
        clean["color"] = _clean_choice(options["color"], PALETTE_NAMES, "options.color")
    return clean_entity, clean


def _validate_talk(entity_id: Any, options: dict) -> tuple[str | None, dict]:
    """A Talk element (DEV-518, DEVICES-10): what a press dials, EXACTLY ONE of `door` (an intercom door), `screen` (a
    screen/dashboard endpoint id), `phone` (an enrolled phone's endpoint id) or `group` (everyone, phones or panels). Shape only here (pure); that the target EXISTS
    is checked by `ElementStore` when it is new or changed, so a stored element whose door, screen or phone later goes away
    still loads, saves and shows "removed" (never deleted automatically)."""
    _check_option_keys(options, set(TALK_TARGETS))
    if entity_id is not None:
        raise _bad("entity_id", "a talk element takes no entity")
    given = [name for name in TALK_TARGETS if name in options]
    if len(given) != 1:
        if given:
            raise _bad("options", "a talk element needs exactly one of door, screen, phone and group")
        raise _bad("options.door", "a talk element needs a door, a screen, a phone or a group")
    name = given[0]
    value = options[name]
    if name == "group":
        # group-call design 3: one of the three words, nothing else; never checked against live phones (an empty set is a state)
        if not isinstance(value, str) or value.strip() not in TALK_GROUPS:
            raise _bad("options.group", "a group is one of: everyone, phones, panels")
        return None, {"group": value.strip()}
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > (128 if name == "door" else MAX_ENDPOINT_ID):
        raise _bad(f"options.{name}", f"a talk element needs a {name}")
    return None, {name: value.strip()}


def _validate_status(entity_id: Any, options: dict, *, chip: bool = False) -> tuple[str | None, dict]:
    _check_option_keys(options, {"states", "attribute"} | ({"short"} if chip else set()))
    if entity_id is None:
        raise _bad("entity_id", "needs an entity")
    clean: dict[str, Any] = {}
    if "states" in options:
        clean["states"] = _clean_states(options["states"])
    if "attribute" in options:
        clean["attribute"] = _clean_attribute(options["attribute"])
    if chip and "short" in options:
        clean["short"] = _clean_short(options["short"])
    return _clean_entity(entity_id), clean


def _validate_chip(entity_id: Any, options: dict) -> tuple[str | None, dict]:
    return _validate_status(entity_id, options, chip=True)


@dataclass(frozen=True)
class KindSpec:
    """One row of the registry. A new kind adds a row (validator plus renderer name); the envelope and the store
    format do not change (ELEM-09)."""

    label: str
    validate: Callable[[Any, dict], tuple[str | None, dict]]
    renderer: str
    entity_domain: str | None = None        # filter for the Add form; None = any entity (or none)
    fields: tuple[tuple[str, str, bool], ...] = field(default_factory=tuple)    # (name, type, required)


KIND_SPECS: dict[str, KindSpec] = {
    "camera": KindSpec("Camera", _validate_camera, "camera", "camera",
                       (("entity_id", "entity", False), ("small_entity_id", "entity", False), ("stream", "stream", False),
                        ("size", "choice", False))),
    "button": KindSpec("Button", _validate_button, "button", None,
                       (("action", "choice", True), ("entity_id", "entity", False), ("target", "text", False),
                        ("color", "choice", False))),
    "talk": KindSpec("Talk", _validate_talk, "talk", None,
                     (("door", "door", False), ("screen", "screen", False), ("phone", "phone", False),
                      ("group", "group", False))),
    "status": KindSpec("Status", _validate_status, "status", None,
                       (("entity_id", "entity", True), ("states", "states", False), ("attribute", "text", False))),
    "chip": KindSpec("Chip", _validate_chip, "chip", None,
                     (("entity_id", "entity", True), ("states", "states", False), ("attribute", "text", False),
                      ("short", "text", False))),
}

_ENVELOPE_KEYS = {"id", "kind", "name", "icon", "entity_id", "options"}


def managed_talk_id(target_kind: str, endpoint_id: str) -> str:
    """The id of the managed Talk element of a screen (`screen`) or an enrolled phone (`phone`)."""
    return f"talk-{target_kind}-{endpoint_id}"


def validate_element(raw: Any, *, managed_ok: bool = False) -> dict[str, Any]:
    """The clean stored form of `raw` (pure). `id` is not minted here: a present `id` must have the `el-xxxxxxxx` shape (or,
    for a managed Talk element, `talk-screen-<id>` / `talk-phone-<id>`). `managed: true` is the server's flag: accepted only
    with `managed_ok` (the store's own load and reconcile), never from a client, and only on a Talk element whose id and
    `options.screen|phone` agree. Raises ElementError("invalid", field=...)."""
    if not isinstance(raw, dict):
        raise _bad("element", "an element is an object")
    if set(raw) - _ENVELOPE_KEYS - ({"managed"} if managed_ok else set()):
        raise _bad("element", "unknown field")
    kind = raw.get("kind")
    if not isinstance(kind, str) or kind not in KIND_SPECS:
        raise _bad("kind", "unknown kind")
    options = raw.get("options", {})
    if not isinstance(options, dict):
        raise _bad("options", "options must be an object")
    clean: dict[str, Any] = {}
    if "id" in raw:
        if not isinstance(raw["id"], str) or not ID_RE.match(raw["id"]):
            raise _bad("id", "not an element id")
        clean["id"] = raw["id"]
    clean["kind"] = kind
    clean["name"] = _clean_name(raw.get("name"))
    if raw.get("icon") is not None:
        clean["icon"] = _clean_icon(raw["icon"], kind)
    if kind == "talk" and raw.get("icon") is None:
        clean["icon"] = DEFAULT_TALK_ICON
    entity_id, clean_options = KIND_SPECS[kind].validate(raw.get("entity_id"), options)
    if entity_id is not None:
        clean["entity_id"] = entity_id
    clean["options"] = clean_options
    if "managed" in raw:
        match = MANAGED_ID_RE.match(clean.get("id", ""))
        if (raw["managed"] is not True or kind != "talk" or match is None
                or clean_options != {match.group(1): match.group(2)}):
            raise _bad("managed", "not a managed Talk element")
        clean["managed"] = True
    elif MANAGED_ID_RE.match(clean.get("id", "")):
        raise _bad("id", "not an element id")           # the managed id shape belongs to managed elements only
    return clean


def talk_door_choices(hass: HomeAssistant) -> list[dict[str, Any]]:
    """The doors a Talk element can name, for the Elements picker: `[{door_id, name, driver, can_dial, available}]` by name.
    `door_id` is `portico/doors`' own (what the element stores and the app attaches audio to); `can_dial` is the driver's
    `CanDial` now (exactly True), the picker's "can dial now" chip. A door that cannot dial is still listed (it may be switched
    on later, and the chip says why it would refuse). Never raises: no provider registry means no doors."""
    from .provider import DATA_PROVIDERS, DOMAIN

    registry = hass.data.get(DOMAIN, {}).get(DATA_PROVIDERS)
    try:
        doors = list(registry.doors()) if registry is not None else []
    except Exception:  # noqa: BLE001 - a registry bug must not take the whole Elements list down
        _LOGGER.warning("portico elements: the doors could not be listed for the Talk picker", exc_info=True)
        return []
    out = [{"door_id": d.id, "name": d.name, "driver": d.driver, "can_dial": d.can_dial is True,
            "available": d.available is True} for d in doors]
    return sorted(out, key=lambda r: (r["name"].casefold(), r["door_id"]))


def name_key(name: str) -> str:
    """The uniqueness key of a name: trimmed and case-folded."""
    return name.strip().casefold()


def kinds_description() -> dict[str, Any]:
    """The registry as plain data, for the later `elements/list` reply (the Add form is built from it)."""
    return {kind: {"label": s.label, "renderer": s.renderer, "entity_domain": s.entity_domain,
                   "fields": [{"name": n, "type": t, "required": r} for n, t, r in s.fields]}
            for kind, s in KIND_SPECS.items()}


def _rev_of(elements: list[dict]) -> str:
    blob = json.dumps(elements, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


class ElementStore:
    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._store: Store[dict] = Store(hass, 1, STORE_KEY, atomic_writes=True)
        self._elements: list[dict[str, Any]] = []       # validated, or unknown-kind verbatim
        self._loaded = False
        self._load_lock = asyncio.Lock()
        self._lock = asyncio.Lock()                      # around every read-modify-write
        self._newer_schema: int | None = None
        self._premigration: tuple[int, dict] | None = None
        self._migration_missing = False
        self._listeners: list[Callable[[], None]] = []
        self.pending_delete: set[str] = set()            # ids being removed: a save may not add a reference to one

    # ---- change listeners (placements P1: an element edit refreshes the screens that use it) ----

    def add_listener(self, listener: Callable[[], None]) -> None:
        if listener not in self._listeners:
            self._listeners.append(listener)

    def remove_listener(self, listener: Callable[[], None]) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def _notify(self) -> None:
        for listener in list(self._listeners):
            try:
                listener()
            except Exception:  # noqa: BLE001 - a listener must never fail the write that already happened
                _LOGGER.warning("portico elements: a change listener failed", exc_info=True)

    # ---- loading ----

    @property
    def loaded(self) -> bool:
        return self._loaded

    async def async_ensure_loaded(self) -> None:
        if self._loaded:
            return
        async with self._load_lock:
            if self._loaded:
                return
            await self._load()
            self._loaded = True

    async def _load(self) -> None:
        raw = await self._store.async_load()
        if not isinstance(raw, dict):
            self._elements = []
            return
        version = raw.get("schema_version", 1)
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            version = 1
        data = raw
        if version > SCHEMA_VERSION:
            self._newer_schema = version
        elif version < SCHEMA_VERSION:
            snapshot = copy.deepcopy(raw)
            try:
                for n in range(version, SCHEMA_VERSION):
                    data = MIGRATIONS[n](data)
            except KeyError:
                _LOGGER.error("portico elements: no migration from schema %d; the file is left untouched", version)
                self._migration_missing = True
                self._elements = []
                return
            self._premigration = (version, snapshot)
        stored = data.get("elements")
        seen_ids: set[str] = set()
        seen_names: set[str] = set()
        out: list[dict[str, Any]] = []
        for item in stored if isinstance(stored, list) else []:
            el = self._load_one(item, seen_ids, seen_names)
            if el is not None:
                out.append(el)
        self._elements = out

    @staticmethod
    def _load_one(item: Any, seen_ids: set[str], seen_names: set[str]) -> dict[str, Any] | None:
        elid = item.get("id") if isinstance(item, dict) else None
        if not isinstance(elid, str) or not ID_RE.match(elid) or elid in seen_ids:
            _LOGGER.warning("portico elements: dropped a stored element with no usable id")
            return None
        kind = item.get("kind")
        if isinstance(kind, str) and kind not in KIND_SPECS:
            seen_ids.add(elid)
            return copy.deepcopy(item)                   # a newer build's kind: kept verbatim
        try:
            clean = validate_element(item, managed_ok=True)
        except ElementError:
            _LOGGER.warning("portico elements: dropped stored element %s that fails validation", elid)
            return None
        key = name_key(clean["name"])
        if key in seen_names:
            _LOGGER.warning("portico elements: dropped stored element %s with a duplicate name", elid)
            return None
        seen_ids.add(elid)
        seen_names.add(key)
        return clean

    # ---- reads ----

    @property
    def rev(self) -> str:
        return _rev_of(self._elements)

    def _listed(self, el: dict[str, Any]) -> dict[str, Any]:
        out = copy.deepcopy(el)
        if el.get("kind") not in KIND_SPECS:
            out["kind_unknown"] = True
        elif el.get("kind") == "talk" and talk_endpoint_removed(self._hass, el):
            out["target_removed"] = True                 # "phone removed" / "screen removed" (kept, never deleted)
        return out

    async def async_list(self) -> dict[str, Any]:
        await self.async_ensure_loaded()
        return {"schema_version": self._newer_schema or SCHEMA_VERSION, "rev": self.rev,
                "elements": [self._listed(e) for e in self._elements]}

    def elements_by_id(self) -> dict[str, dict[str, Any]]:
        """`{id: stored element}` of what is loaded (the live objects: callers must not change them). Empty before the load."""
        return {e["id"]: e for e in self._elements if isinstance(e.get("id"), str)}

    def used_by_map(self) -> dict[str, list[dict[str, Any]]]:
        """Every element's users from ONE pass over the stored content document: `{element id: [{endpoint, label, page?,
        block?}]}`, an entry (possibly empty) for every element. Needs the content store loaded (see `async_ensure_content`);
        before that every list is empty."""
        usage = get_content_store(self._hass).element_usage() if get_content_store(self._hass).loaded else {}
        return {eid: list(usage.get(eid, [])) for eid in self.elements_by_id()}

    def used_by(self, element_id: str) -> list[dict[str, Any]]:
        """The screens that reference this element: `[{"endpoint": ..., "label": ..., "page"?: ..., "block"?: ...}]`
        (ELEM-02's removal refusal). Sync: call `async_ensure_content` first."""
        content = get_content_store(self._hass)
        return list(content.element_usage().get(element_id, [])) if content.loaded else []

    async def async_ensure_content(self) -> None:
        """Load the content store (which also loads this one), so `used_by` reads real data. Never raises: a content store that
        cannot load leaves `used_by` empty and the write path to its own checks."""
        try:
            await get_content_store(self._hass).async_ensure_loaded()
        except Exception:  # noqa: BLE001
            _LOGGER.warning("portico elements: the content store could not be loaded for the usage scan", exc_info=True)

    # ---- writes ----

    def _check_writable(self) -> None:
        if self._newer_schema is not None:
            raise ElementError("newer_schema", "the stored elements are a newer schema than this build understands")
        if self._migration_missing:
            raise ElementError("migration_missing", "the stored elements could not be migrated")

    # ---- the YAML apply (DEV-824 P2: config_apply.py) ----

    @property
    def lock(self) -> asyncio.Lock:
        """The lock around every read-modify-write; the YAML apply holds it for its whole run so no element write interleaves."""
        return self._lock

    def check_writable(self) -> None:
        self._check_writable()

    def stored(self) -> list[dict[str, Any]]:
        """A copy of the stored list, in display order (managed and unknown-kind elements included)."""
        return copy.deepcopy(self._elements)

    def swap_in_memory(self, elements: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Replace the in-memory list (caller holds `lock`, nothing awaits between this and the write); returns the old list."""
        old, self._elements = self._elements, elements
        return old

    async def write_all(self, elements: list[dict[str, Any]]) -> None:
        """Write `elements` as the whole file and notify listeners (caller holds `lock`)."""
        await self._save(elements)

    def _mint_id(self) -> str:
        taken = {e.get("id") for e in self._elements}
        while True:
            elid = "el-" + secrets.token_hex(4)
            if elid not in taken:
                return elid

    async def async_set(self, element: Any, base_rev: str, *, validate_only: bool = False, confirm: bool = False) -> dict[str, Any]:
        """Create (no `id`) or replace (with `id`) one element. Returns `{element, rev}`; with `validate_only` nothing is
        written (and the rev is the current one)."""
        await self.async_ensure_loaded()
        await self.async_ensure_content()
        async with self._lock:
            self._check_writable()
            clean = validate_element(self._with_managed_flag(element), managed_ok=True)
            if base_rev != self.rev:
                raise ElementError("conflict", "the elements changed since they were read")
            index = None
            if "id" in clean:
                index = next((i for i, e in enumerate(self._elements) if e.get("id") == clean["id"]), None)
                if index is None:
                    raise ElementError("unknown_element", "no such element")
                if self._elements[index].get("kind") not in KIND_SPECS or self._elements[index]["kind"] != clean["kind"]:
                    raise ElementError("kind_locked", "an element's kind cannot change", field="kind")
                self._check_managed_locked(self._elements[index], clean)
                self._guard_used(self._elements[index], clean, confirm=confirm)
            self._check_talk_door(clean, self._elements[index] if index is not None else None)
            self._check_talk_endpoint(clean, self._elements[index] if index is not None else None)
            key = name_key(clean["name"])
            for i, e in enumerate(self._elements):
                if i != index and isinstance(e.get("name"), str) and name_key(e["name"]) == key:
                    raise ElementError("name_taken", "that name is already used", field="name")
            if index is None and len(self._elements) >= MAX_ELEMENTS:
                raise ElementError("too_many", f"at most {MAX_ELEMENTS} elements")
            if index is None:
                clean = {"id": self._mint_id(), **clean}
            if validate_only:
                return {"element": clean, "rev": self.rev}
            updated = list(self._elements)
            if index is None:
                updated.append(clean)
            else:
                updated[index] = clean
            await self._save(updated)
            return {"element": copy.deepcopy(clean), "rev": self.rev}

    def _with_managed_flag(self, element: Any) -> Any:
        """The client's element with the SERVER's `managed` flag: a replace of a managed Talk element is sent without the flag
        (a client can never set it), so it is put back from the stored one. Anything else is returned as it came, and
        `managed` from a client is dropped (a managed-shaped id without the flag is then refused by the validator)."""
        if not isinstance(element, dict):
            return element
        plain = {k: v for k, v in element.items() if k != "managed"}       # a client's own `managed` is never believed
        stored = next((e for e in self._elements if "id" in element and e.get("id") == element.get("id")
                       and e.get("managed") is True), None)
        return plain if stored is None else {**plain, "managed": True}

    @staticmethod
    def _check_managed_locked(old: dict[str, Any], clean: dict[str, Any]) -> None:
        """A managed Talk element is the handle of its phone or screen: only its name and icon change."""
        if old.get("managed") is True and (clean.get("options") != old.get("options") or clean.get("managed") is not True):
            raise ElementError("managed_locked", "a managed Talk element can only be renamed", field="options")

    def _check_talk_endpoint(self, clean: dict[str, Any], old: dict[str, Any] | None) -> None:
        """A NEW or CHANGED Talk `screen` or `phone` must exist now (as a door must, `_check_talk_door`); an unchanged one is
        not checked, so a removed phone or screen is kept ("phone removed") and its element still renames and saves."""
        if clean.get("kind") != "talk" or clean.get("managed") is True:
            return
        for name, kind in (("screen", "dashboard"), ("phone", "phone")):
            wanted = clean["options"].get(name)
            if wanted is None or (old is not None and (old.get("options") or {}).get(name) == wanted):
                continue
            if not endpoint_exists(self._hass, kind, wanted):
                raise _bad(f"options.{name}", f"there is no such {name}")

    def talk_door(self, door_id: Any) -> Any:
        """The provider `Door` a Talk element's `door` names (`portico/doors` `door_id`; a `<provider>:<id>` form also
        resolves), or None when no provider supplies it or two do. Never raises."""
        from .provider import DATA_PROVIDERS, DOMAIN, ProviderError

        registry = self._hass.data.get(DOMAIN, {}).get(DATA_PROVIDERS)
        if registry is None or not isinstance(door_id, str) or not door_id:
            return None
        try:
            found = registry.door(door_id)
        except ProviderError:
            return None
        return None if found is None else found[1]

    def _check_talk_door(self, clean: dict[str, Any], old: dict[str, Any] | None) -> None:
        """A NEW or CHANGED Talk door must exist now, and is stored as the door's own id (so `<provider>:<id>` and a bare id
        end up the same, and an ambiguous bare id is refused). An unchanged door is not checked: a door that disappeared is
        kept and shows "door removed" (same rule as any other stored reference), and renaming the element must still save."""
        if clean.get("kind") != "talk":
            return
        wanted = clean["options"].get("door")
        if wanted is None:
            return                                       # a screen or phone target: `_check_talk_endpoint`
        if old is not None and (old.get("options") or {}).get("door") == wanted:
            return
        door = self.talk_door(wanted)
        if door is None:
            raise _bad("options.door", "there is no such door")
        clean["options"]["door"] = door.id

    def _guard_used(self, old: dict[str, Any], new: dict[str, Any], *, confirm: bool = False) -> None:
        """What a change may do to an element screens use (placements design 9.3, the owner 2026-11-09). A used button's `action`
        cannot change (a stored `confirm: false` is meaningful only for the action it was set for): `in_use_action`. A used
        camera cannot become stream-only (a screen needs an entity): `in_use_stream`. Changing the ENTITY or TARGET of a used
        element (any kind: a script that becomes a gate, a different door, a call target, a status entity) re-points every
        screen that uses it, so it needs an explicit `confirm` (the owner, 2026-11-09, review finding 2): without it the error is
        `in_use_retarget` with the screens (`used_by`), also for `validate_only`, which never writes. Raises ElementError."""
        users = self.used_by(old["id"]) if isinstance(old.get("id"), str) else []
        if not users:
            return
        old_opts, new_opts = old.get("options") or {}, new.get("options") or {}
        if old.get("kind") == "button" and old_opts.get("action") != new_opts.get("action"):
            raise ElementError("in_use_action", "screens use this button; its action cannot change", field="options.action",
                               used_by=users)
        if old.get("kind") == "camera" and not new.get("entity_id"):
            raise ElementError("in_use_stream", "screens use this camera; it needs an entity", field="entity_id",
                               used_by=users)
        if not confirm and (old.get("entity_id") != new.get("entity_id") or old_opts.get("target") != new_opts.get("target")
                            or any(old_opts.get(n) != new_opts.get(n) for n in TALK_TARGETS)):
            raise ElementError("in_use_retarget", "screens use this element; changing what it points at changes all of them",
                               used_by=users)

    async def async_delete(self, element_id: str, base_rev: str) -> dict[str, Any]:
        """Remove one element. An element a screen uses is REFUSED (`in_use`, with the screens): there is no override
        (the owner, 2026-11-09), so a reference can never be left dangling by a delete. The id is marked pending in memory
        before the awaited write, so a content save in that window cannot add a reference to it (design 3.4.2)."""
        await self.async_ensure_loaded()
        await self.async_ensure_content()
        async with self._lock:
            self._check_writable()
            if base_rev != self.rev:
                raise ElementError("conflict", "the elements changed since they were read")
            found = next((e for e in self._elements if e.get("id") == element_id), None)
            if found is None:
                raise ElementError("unknown_element", "no such element")
            if found.get("managed") is True and managed_target_exists(self._hass, found):
                raise ElementError("managed_target_exists",
                                   "this Talk element belongs to a phone or screen that still exists; remove that first")
            users = self.used_by(element_id)
            if users:
                raise ElementError("in_use", "screens use this element; remove those first", used_by=users)
            self.pending_delete.add(element_id)
            try:
                await self._save([e for e in self._elements if e.get("id") != element_id])
            finally:
                self.pending_delete.discard(element_id)
            return {"rev": self.rev}

    async def async_drop_managed(self, element_id: str) -> bool:
        """Remove one MANAGED Talk element by id, with no `base_rev`: the revoke of a panel takes its screen's handle with it
        (DEV-807). Only a managed element whose phone or screen is gone, and which no screen uses; anything else is left and
        False returned. Never raises over a missing element."""
        await self.async_ensure_loaded()
        async with self._lock:
            self._check_writable()
            found = next((e for e in self._elements if e.get("id") == element_id), None)
            if found is None or found.get("managed") is not True or managed_target_exists(self._hass, found):
                return False
            if self.used_by(element_id):
                _LOGGER.warning("portico elements: a revoked panel's Talk element is used by a screen and is kept")
                return False
            self.pending_delete.add(element_id)
            try:
                await self._save([e for e in self._elements if e.get("id") != element_id])
            finally:
                self.pending_delete.discard(element_id)
            return True

    async def async_add_managed(self, wanted: list[dict[str, Any]]) -> list[str]:
        """The reconcile write (managed_talk.py): add each managed Talk element in `wanted` whose id is not stored yet, and
        change nothing else (an existing one keeps its name and icon; an orphan is never touched). Each is validated as a
        managed element; a name another element already holds gets ` (screen)` / ` (phone)` and then a number, so a create
        never fails on a name. Returns the ids added. Nothing is written when there is nothing to add."""
        await self.async_ensure_loaded()
        async with self._lock:
            self._check_writable()
            have = {e.get("id") for e in self._elements}
            taken = {name_key(e["name"]) for e in self._elements if isinstance(e.get("name"), str)}
            added: list[dict[str, Any]] = []
            for item in wanted:
                if item["id"] in have:
                    continue
                base = item["name"].strip()[:MAX_LABEL] or item["id"][:MAX_LABEL]
                name, n = base, 1
                while name_key(name) in taken:
                    suffix = f" ({next(iter(item['options']))})" if n == 1 else f" ({n})"
                    name, n = base[:MAX_LABEL - len(suffix)] + suffix, n + 1
                clean = validate_element({"id": item["id"], "kind": "talk", "name": name, "options": item["options"],
                                          "managed": True}, managed_ok=True)
                added.append(clean)
                taken.add(name_key(name))
                have.add(item["id"])
            if not added:
                return []
            await self._save([*self._elements, *added])
            return [e["id"] for e in added]

    async def _save(self, elements: list[dict[str, Any]]) -> None:
        """Write the file; the first save over an older one first leaves its one-time copy. The in-memory list is only
        replaced once the write has succeeded."""
        self._check_writable()
        older, self._premigration = self._premigration, None
        if older is not None:
            version, raw = older
            try:
                copy_store: Store[dict] = Store(self._hass, 1, f"{STORE_KEY}.v{version}", atomic_writes=True)
                if await copy_store.async_load() is None:
                    await copy_store.async_save(raw)
                    _LOGGER.info("portico elements: kept a copy of the v%d file as %s.v%d", version, STORE_KEY, version)
            except Exception as err:  # noqa: BLE001 - the copy is a courtesy, the save is the job
                _LOGGER.warning("portico elements: could not keep a copy of the v%d file (%s); saving anyway",
                                version, err)
        await self._store.async_save({"schema_version": SCHEMA_VERSION, "elements": elements})
        self._elements = elements
        self._notify()


def endpoint_exists(hass: HomeAssistant, kind: str, endpoint_id: Any) -> bool:
    """Whether an enrolled phone (`kind` `phone`) or a screen (`dashboard`) with this endpoint id exists now. A screen
    needs the content store loaded (a store that has not loaded knows no screen). Never raises."""
    from .endpoints import get_endpoint_registry

    if not isinstance(endpoint_id, str) or not endpoint_id:
        return False
    from .endpoints import is_app_screen

    try:
        if kind == "dashboard" and is_app_screen(hass, endpoint_id):
            return False            # a panel's app screen is not a Talk target; its phone half is (DEV-807)
        return get_endpoint_registry(hass).get(endpoint_id, kind=kind) is not None
    except Exception:  # noqa: BLE001
        _LOGGER.warning("portico elements: the endpoint registry could not be read", exc_info=True)
        return False


def _talk_endpoint(element: dict[str, Any]) -> tuple[str, str] | None:
    options = element.get("options") or {}
    for name, kind in (("screen", "dashboard"), ("phone", "phone")):
        if isinstance(options.get(name), str):
            return kind, options[name]
    return None


def managed_target_exists(hass: HomeAssistant, element: dict[str, Any]) -> bool:
    """True when the phone or screen a Talk element names exists now."""
    target = _talk_endpoint(element)
    return target is not None and endpoint_exists(hass, *target)


def talk_endpoint_removed(hass: HomeAssistant, element: dict[str, Any]) -> bool:
    """True for a Talk element on a screen or phone that is gone ("screen removed" / "phone removed"). A screen is judged
    only once the content store has loaded, so an early read never calls a live screen removed."""
    target = _talk_endpoint(element)
    if target is None:
        return False
    if target[0] == "dashboard":
        content = hass.data.get("portico", {}).get("content")
        if content is None or not content.loaded:
            return False
    return not endpoint_exists(hass, *target)


def get_elements_store(hass: HomeAssistant) -> ElementStore:
    domain_data = hass.data.setdefault("portico", {})
    store = domain_data.get(DATA_ELEMENTS)
    if store is None:
        store = domain_data[DATA_ELEMENTS] = ElementStore(hass)
    return store
