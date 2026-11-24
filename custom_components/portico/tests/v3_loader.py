"""The schema-3 loader, copied VERBATIM from `content.py` at portico-ha `origin/main` e71aa5b (the build that is
live before blocks): the validators a build that reverts to schema 3 runs over the pages of a schema-4 file. Tests
feed it the pages the schema-4 build writes to disk to prove "every page survives" (blocks design 3.5, R3).

Only the functions the page loader needs, unchanged. Do not "fix" or modernise anything here: it is a fixture of
what an older build does. `ProfileError` is its own class on purpose (this module must not import `content`).
"""
from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

from ..call_endpoints import MAX_TARGET_ID, TARGET_TYPES

_LOGGER = logging.getLogger("v3_loader")

MAX_CAMERAS = 24

MAX_ACTIONS = 24

MAX_LABEL = 40

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

ENTITY_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")

KINDS: dict[str, tuple[str | None, tuple[str, str] | None, bool]] = {
    "portico_open": (None, None, True),  # names a Portico device_id instead of an entity
    "lock_open": ("lock", ("lock", "open"), True),
    "button_press": ("button", ("button", "press"), False),
    "script_run": ("script", ("script", "turn_on"), False),
    "scene_activate": ("scene", ("scene", "turn_on"), False),
    # A call button (house calls design section 14): names a target, not an entity; it is placed through the
    # call-endpoint registry. There is NO confirm step to call anyone (Alice, 2026-10-14, CB-9): the flag is
    # neither offered by the builder nor honoured here, whatever a stored or incoming profile says.
    "call": (None, None, False),
}

class ProfileError(ValueError):
    """A profile that cannot be saved; the message says which item and why."""

def _label(raw: Any, where: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
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

def normalize_profile(raw: Any, *, known_target: Callable[[str, str], bool] | None = None) -> dict[str, Any]:
    """Validate and normalise a profile. Unknown keys are dropped, an unknown
    action kind is refused. Raises ProfileError.

    `known_target(type, id)`, when given, must say whether a call button's target exists: an unknown one is
    refused. Loading a stored profile does not pass it (a button whose target vanished stays, shown off)."""
    if not isinstance(raw, dict):
        raise ProfileError("the profile must be an object")
    cameras_in = raw.get("cameras", [])
    actions_in = raw.get("actions", [])
    if not isinstance(cameras_in, list) or not isinstance(actions_in, list):
        raise ProfileError("cameras and actions must be lists")
    if len(cameras_in) > MAX_CAMERAS or len(actions_in) > MAX_ACTIONS:
        raise ProfileError(f"at most {MAX_CAMERAS} cameras and {MAX_ACTIONS} actions")

    seen: set[str] = set()
    cameras: list[dict[str, Any]] = []
    for n, item in enumerate(cameras_in):
        where = f"camera {n + 1}"
        if not isinstance(item, dict):
            raise ProfileError(f"{where}: must be an object")
        cameras.append({
            "id": _item_id(item.get("id"), where, seen),
            "label": _label(item.get("label"), where),
            "entity_id": _entity(item.get("entity_id"), "camera", where),
        })

    actions: list[dict[str, Any]] = []
    for n, item in enumerate(actions_in):
        where = f"action {n + 1}"
        if not isinstance(item, dict):
            raise ProfileError(f"{where}: must be an object")
        kind = item.get("kind")
        if kind not in KINDS:
            raise ProfileError(f"{where}: unknown kind {kind!r}")
        domain, _service, confirm_default = KINDS[kind]
        out: dict[str, Any] = {
            "id": _item_id(item.get("id"), where, seen),
            "label": _label(item.get("label"), where),
            "kind": kind,
        }
        if kind == "call":
            target = _call_target(item.get("target"), where)
            if known_target is not None and not known_target(target["type"], target["id"]):
                raise ProfileError(f"{where}: there is nothing called {target['id']!r} to call ({target['type']})")
            out["target"] = target
        elif domain is None:
            device_id = item.get("device_id")
            if not isinstance(device_id, str) or not device_id or len(device_id) > 128:
                raise ProfileError(f"{where}: a Portico device_id is required")
            out["device_id"] = device_id
        else:
            out["entity_id"] = _entity(item.get("entity_id"), domain, where)
        icon = item.get("icon")
        if icon is not None:
            if not isinstance(icon, str) or len(icon) > 64:
                raise ProfileError(f"{where}: the icon must be a short string")
            out["icon"] = icon
        if kind == "call":
            out["confirm"] = False  # never asked, never honoured (CB-9); a `confirm: true` sent is ignored
        else:
            confirm = item.get("confirm", confirm_default)
            if not isinstance(confirm, bool):
                raise ProfileError(f"{where}: confirm must be true or false")
            out["confirm"] = confirm
        actions.append(out)
    return {"cameras": cameras, "actions": actions}

def default_page_label(page_id: str) -> str:
    """A page's label when none is stored: its id, title-cased ("garden-lights" -> "Garden Lights")."""
    return page_id.replace("-", " ").replace("_", " ").title()

def normalize_page(raw: Any, seen: set[str] | None = None, *,
                   known_target: Callable[[str, str], bool] | None = None) -> dict[str, Any]:
    """One validated page `{id, label, cameras, actions}`. `label` defaults to the id, title-cased.
    `seen` (page ids so far) makes a repeated id an error; `known_target` is `normalize_profile`'s. Raises ProfileError."""
    if not isinstance(raw, dict):
        raise ProfileError("a page must be an object")
    page_id = raw.get("id")
    if not isinstance(page_id, str) or not ID_RE.match(page_id) or (seen is not None and page_id in seen):
        raise ProfileError("a page needs a unique id of 1-32 characters of a-z, 0-9, _ or -")
    label = raw.get("label")
    label = default_page_label(page_id) if label is None else _label(label, f"page {page_id}")
    if seen is not None:
        seen.add(page_id)
    return {"id": page_id, "label": label, **normalize_profile(raw, known_target=known_target)}

def _normalize_pages(raw: Any, where: str) -> list[dict[str, Any]]:
    """The lenient form (a load): validated pages `[{id, label, cameras, actions}]`; an invalid page is
    dropped with a warning."""
    pages: list[dict[str, Any]] = []
    seen: set[str] = set()
    for page in raw if isinstance(raw, list) else ():
        try:
            pages.append(normalize_page(page, seen))
        except ProfileError as err:
            # A hand-edited or older file must not take the others down.
            _LOGGER.warning("portico content: dropped an invalid stored profile (%s: %s)", where, err)
    return pages
