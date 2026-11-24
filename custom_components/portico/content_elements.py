"""Element references inside the content document (DEV-760, placements phase P1; design `portico_elements_placements_design.md`
sections 3 and 8, the owner's decisions of 2026-11-09).

A screen can name an *element* (a reusable camera made on the Elements page) instead of defining the camera inline:

    camera      {id, element: "el-3fa91c07", label?, size?, live?}       # in a block's `cameras`
    rule camera / phone or screen camera declaration: an entry that is an element id (`el-xxxxxxxx`) instead of an entity id

This module is PURE (no Home Assistant, no store, no clock): it walks a document, expands references into the inline items the
rest of the code already understands, checks them against a map of elements, and counts where each element is used. The
resolver, the grants and every validator then run UNCHANGED on the expanded document, so the wire is identical to the one an
inline document produces (the acceptance test of the design, section 4.2).

Cameras: only elements with an `entity_id` are placeable (a stream-only camera has no entity for a snapshot, the live path or
the `c-` grant id; the owner, 2026-11-09).

Buttons (phase P3): an action may be `{id, element, label?, show_label?, confirm?}`. Safety first, because a button can open a
door or the gate: the reference carries NO entity, action or target (the element's, read at expansion, i.e. at every run); the
only overrides are label, show-text and ask-first; and ask-first can only be TIGHTENED: for an action whose default asks
(`portico_open`, `lock_open`, `lock_unlock`, `cover_open`: doors, locks, gates) a stored `confirm: false` is refused at save and
ignored at expansion, so the resolved `confirm` is `default OR ref.confirm is True`, never less than the action's default.

Talk (DEV-518): a Talk element (`kind: talk`, `options.door`) is placed in `actions` like a button and expands to a `talk` action that
carries its own `door`: the press dials THAT door, the block's door and the one-gate pick are not involved, and it is never ask-first.
A Talk element with `options.phone` / `options.screen` (DEVICES-10; the managed ones `talk-phone-<id>` / `talk-screen-<id>`
included) expands instead to the existing `call` action aimed at that phone or screen (a house call).

`elements` everywhere is `{element id: stored element}` (the shape `ElementStore.elements_by_id()` returns).
"""
from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterator
from typing import Any

from .pages_resolve import KIND_RING, is_pages_model, is_talk_id, library, page_kind

_LOGGER = logging.getLogger(__name__)

ELEMENT_ID_RE = re.compile(r"^(?:el-[a-z0-9-]{3,40}|talk-(?:screen|phone)-[A-Za-z0-9_-]{1,128})$")   # + the managed Talk ids (DEVICES-10)
REF_KEY = "element"
TALK_GROUPS = ("everyone", "phones", "panels")      # the words of a Talk element's `options.group` (group-call design G1); `elements.py` validates against it
# What a camera reference may carry besides its id and the element: the per-screen overrides (label, size, live).
CAMERA_REF_KEYS = frozenset({"id", REF_KEY, "label", "size", "live"})
ACTION_REF_KEYS = frozenset({"id", REF_KEY, "label", "show_label", "confirm"})


def _kinds() -> dict[str, Any]:
    """`content.KINDS` and friends, imported late (content imports this module)."""
    from . import content
    return content.KINDS


def parse_call_target(value: Any) -> dict[str, str] | None:
    """A `call` button element's target string `"<type>:<id>"` (type one of the call target types) as the inline
    `{type, id}`; None when it is not that. (Design gap: the elements design leaves a call element's target an opaque string.)"""
    from .call_endpoints import TARGET_TYPES
    if not isinstance(value, str) or ":" not in value:
        return None
    target_type, target_id = value.split(":", 1)
    if target_type not in TARGET_TYPES or not target_id or len(target_id) > 160:
        return None
    return {"type": target_type, "id": target_id}


def guarded_kind(kind: Any) -> bool:
    """True for an action whose default asks first (doors, locks, gates): its ask-first can never be turned off."""
    spec = _kinds().get(kind)
    return bool(spec and spec[2])


class ElementRefError(ValueError):
    """A reference a save must refuse. `code` is stable (`element_missing`, `element_kind`, `not_placeable`, `element_pending`);
    the message names the item and carries no stored value."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def is_element_id(value: Any) -> bool:
    return isinstance(value, str) and ELEMENT_ID_RE.match(value) is not None


def is_ref(item: Any) -> bool:
    return isinstance(item, dict) and REF_KEY in item


def placeable_camera(element: Any) -> bool:
    """A camera element a screen may use: kind camera with an entity (not stream-only)."""
    return (isinstance(element, dict) and element.get("kind") == "camera"
            and isinstance(element.get("entity_id"), str) and bool(element["entity_id"]))


# ---- walking a document ------------------------------------------------------------------------------------------------

def _scopes(document: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    """(scope name, {pages, rules}) for the template and every override; for a schema 6 document the one library, as scope
    `pages`."""
    if is_pages_model(document):
        yield "pages", {"pages": library(document)}
        return
    template = document.get("template")
    if isinstance(template, dict):
        yield "template", template
    overrides = document.get("overrides")
    for key, override in (overrides.items() if isinstance(overrides, dict) else ()):
        if isinstance(override, dict):
            yield key, override


def _blocks(scope: dict[str, Any]) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
    for page in scope.get("pages") or ():
        if isinstance(page, dict):
            for block in page.get("blocks") or ():
                if isinstance(block, dict):
                    yield page, block


def iter_refs(document: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Every reference in the document: `{element, endpoint, label, kind, where, page?, block?, item?}`.

    `kind` is `camera`/`action` (a block item), `rule` (a rule's camera list), `declaration` (a phone's camera
    declaration) or `screen` (a declared screen's cameras). `label` is what a person reads ("Home > Garden")."""
    for scope_name, scope in _scopes(document):
        for page, block in _blocks(scope):
            label = f"{page.get('label') or page.get('id')} > {block.get('title') or block.get('id')}"
            for list_name, kind in (("cameras", "camera"), ("actions", "action")):
                for item in block.get(list_name) or ():
                    if is_ref(item) and isinstance(item[REF_KEY], str):
                        yield {"element": item[REF_KEY], "endpoint": scope_name, "label": label, "kind": kind,
                               "page": page.get("id"), "block": block.get("id"), "item": item.get("id"),
                               "ref": item, "door": block.get("door"), "ring": page_kind(page) == KIND_RING,
                               "where": f"{scope_name} page {page.get('id')!r} {kind} {item.get('id')!r}"}
        if scope_name == "pages":
            # A Ring page names its Talk element and may name a camera element: both are uses (the delete guard lists them).
            for page in scope.get("pages") or ():
                if not isinstance(page, dict) or page_kind(page) != KIND_RING:
                    continue
                label = f"Ring page {page.get('label') or page.get('id')}"
                if isinstance(page.get("talk"), str):
                    yield {"element": page["talk"], "endpoint": scope_name, "label": label, "kind": "ringtalk",
                           "page": page.get("id"), "where": f"ring page {page.get('id')!r} talk"}
                for entry in page.get("cameras") or ():
                    if is_element_id(entry):
                        yield {"element": entry, "endpoint": scope_name, "label": label, "kind": "ringcamera",
                               "page": page.get("id"), "where": f"ring page {page.get('id')!r} cameras"}
            continue
        for rule in scope.get("rules") or ():
            for entry in (rule.get("cameras") or ()) if isinstance(rule, dict) else ():
                if is_element_id(entry):
                    yield {"element": entry, "endpoint": scope_name, "label": f"Rule {rule.get('id')}", "kind": "rule",
                           "where": f"{scope_name} rule {rule.get('id')!r} cameras"}
    declarations = document.get("endpoints")
    for key, declaration in (declarations.items() if isinstance(declarations, dict) else ()):
        for entry in (declaration.get("cameras") or ()) if isinstance(declaration, dict) else ():
            if is_element_id(entry):
                yield {"element": entry, "endpoint": key, "label": "Camera declaration", "kind": "declaration",
                       "where": f"endpoint {key!r} cameras"}
    dashboards = document.get("dashboards")
    for slug, dashboard in (dashboards.items() if isinstance(dashboards, dict) else ()):
        for entry in (dashboard.get("cameras") or ()) if isinstance(dashboard, dict) else ():
            if is_element_id(entry):
                yield {"element": entry, "endpoint": slug, "label": "Screen cameras", "kind": "screen",
                       "where": f"screen {slug!r} cameras"}


def has_refs(document: dict[str, Any]) -> bool:
    """True when the document holds at least one reference (the lazy schema-5 stamp: design 3.2)."""
    return next(iter_refs(document), None) is not None


def has_cameras(document: dict[str, Any]) -> bool:
    """True when any page of the document holds a camera item, inline or by reference (DEV-768: an inline camera takes its
    small stream from a matching camera element, so an element edit can change what such a screen resolves to)."""
    return any(block.get("cameras") for _scope, scope in _scopes(document) for _page, block in _blocks(scope))


def small_entity_of(elements: dict[str, Any], entity_id: str) -> str | None:
    """DEV-768: the small stream a camera ELEMENT for `entity_id` names, for an inline camera item that holds the entity
    and no element reference (a screen built before small streams existed). The first such element in list order wins;
    one without a usable `small_entity_id` is skipped."""
    for element in elements.values():
        if not isinstance(element, dict) or element.get("kind") != "camera" or element.get("entity_id") != entity_id:
            continue
        small = (element.get("options") or {}).get("small_entity_id")
        if isinstance(small, str) and small.startswith("camera.") and small != entity_id:
            return small
    return None


def usage_index(document: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """element id -> `[{endpoint, label, page?, block?}]`, one pass over the document (design section 8). `endpoint` is the
    override key, `template`, or the key/slug of a declaration or screen."""
    out: dict[str, list[dict[str, Any]]] = {}
    for ref in iter_refs(document):
        row: dict[str, Any] = {"endpoint": ref["endpoint"], "label": ref["label"]}
        if ref.get("page") is not None:
            row["page"] = ref["page"]
        if ref.get("block") is not None:
            row["block"] = ref["block"]
        out.setdefault(ref["element"], []).append(row)
    return out


# ---- expansion ---------------------------------------------------------------------------------------------------------

def expand_camera(ref: dict[str, Any], element: Any) -> dict[str, Any] | None:
    """The inline camera item a reference stands for, or None when the element cannot be one. The same keys, in the same
    order of meaning, as `content._normalize_items` produces for an inline camera, so everything after is identical."""
    if not placeable_camera(element):
        return None
    out: dict[str, Any] = {"id": ref["id"], "label": ref["label"] if "label" in ref else element["name"],
                           "entity_id": element["entity_id"]}
    size = ref.get("size")
    if size is None:
        size = (element.get("options") or {}).get("size")
    if size is not None:
        out["size"] = size
    small = (element.get("options") or {}).get("small_entity_id")
    if isinstance(small, str) and small.startswith("camera.") and small != element["entity_id"]:
        out["small_entity_id"] = small       # DEV-768: server-side only; the resolver turns it into `small: true`
    if ref.get("live") is True:
        out["live"] = True
    return out


def expand_action(ref: dict[str, Any], element: Any, door: str | None = None) -> dict[str, Any] | None:
    """The inline action a button reference stands for, or None when the element cannot be one here. The same keys
    `content._normalize_items` produces for an inline action, so everything after (resolver, grant, run) is identical.

    Everything that matters is the ELEMENT's current value: kind (its `action`), entity, target, icon, colour. The reference
    adds only `label`, `show_label` (a False needs a usable icon, else it is ignored: the text stays) and `confirm`, which
    can only tighten (module docstring)."""
    from . import content

    if isinstance(element, dict) and element.get("kind") == "talk":
        return _expand_talk(ref, element)
    if not isinstance(element, dict) or element.get("kind") != "button":
        return None
    options = element.get("options") or {}
    kind = options.get("action")
    kinds = content.KINDS
    if kind not in kinds:
        return None
    domain, _service, confirm_default = kinds[kind]
    name = element.get("name")
    out: dict[str, Any] = {"id": ref["id"], "label": ref["label"] if "label" in ref else name, "kind": kind}
    if kind == "call":
        target = parse_call_target(options.get("target"))
        if target is None:
            return None
        out["target"] = target
    elif kind == "talk":
        pass                        # acts on the block's door; no target of its own
    elif domain is None:            # portico_open: the element's target is the device id, else the block's door
        device_id = options.get("target")
        if isinstance(device_id, str) and device_id:
            out["device_id"] = device_id
        elif not door:
            return None
    else:
        entity_id = element.get("entity_id")
        if not isinstance(entity_id, str) or entity_id.split(".", 1)[0] != domain or not content.ENTITY_RE.match(entity_id):
            return None
        out["entity_id"] = entity_id
    icon = element.get("icon")
    if isinstance(icon, str) and icon:
        out["icon"] = icon
    color = options.get("color")
    if isinstance(color, str) and color:
        out["color"] = color
    show_label = ref.get("show_label")
    if isinstance(show_label, bool) and (show_label or content._has_usable_icon(out.get("icon"))):
        out["show_label"] = show_label
    if kind in content.KINDS_WITHOUT_CONFIRM:
        out["confirm"] = False
    else:
        out["confirm"] = bool(confirm_default) or ref.get("confirm") is True   # tighten only, never loosen
    return out


def _expand_talk(ref: dict[str, Any], element: dict[str, Any]) -> dict[str, Any] | None:
    """The inline Talk action a Talk element stands for (DEV-518): `kind: talk` with the element's own `door` (the door the
    press dials: no block door and no one-gate pick are involved), never ask-first (CB-9). The door is NOT checked to exist
    here: a door that went away still expands, the press and the state say "door removed"."""
    options = element.get("options") or {}
    if options.get("group") in TALK_GROUPS:            # a group call: the house call with the group target (G3)
        return _expand_house_call(ref, element, {"type": "group", "id": options["group"]})
    for name, target_type in (("phone", "phone"), ("screen", "dashboard")):
        if isinstance(options.get(name), str) and options[name]:
            return _expand_house_call(ref, element, {"type": target_type, "id": options[name]})
    door = options.get("door")
    if not isinstance(door, str) or not door:
        return None
    from . import content

    out: dict[str, Any] = {"id": ref["id"], "label": ref["label"] if "label" in ref else element.get("name"), "kind": "talk",
                           "door": door}
    icon = element.get("icon")
    if isinstance(icon, str) and icon:
        out["icon"] = icon
    show_label = ref.get("show_label")
    if isinstance(show_label, bool) and (show_label or content._has_usable_icon(out.get("icon"))):
        out["show_label"] = show_label
    out["confirm"] = False
    return out


def _expand_house_call(ref: dict[str, Any], element: dict[str, Any], target: dict[str, str]) -> dict[str, Any]:
    """A Talk element on a phone or a screen (DEVICES-10, D1) stands for the EXISTING inline `call` action with that
    `{type, id}` target, so the resolver, the grant, `content/run` and the installed app treat it exactly as a call button
    (V1: the server's own rules, target `house_receive` and presence, decide `available`/`reason` at resolve and press time).
    A phone or screen that is gone still expands: the call state then says `unknown_target`."""
    from . import content

    out: dict[str, Any] = {"id": ref["id"], "label": ref["label"] if "label" in ref else element.get("name"), "kind": "call",
                           "target": target}
    icon = element.get("icon")
    if isinstance(icon, str) and icon:
        out["icon"] = icon
    show_label = ref.get("show_label")
    if isinstance(show_label, bool) and (show_label or content._has_usable_icon(out.get("icon"))):
        out["show_label"] = show_label
    out["confirm"] = False
    return out


def _expand_actions(items: list[Any], elements: dict[str, Any], door: str | None,
                    drop: Callable[[str, str], None]) -> list[Any]:
    out: list[Any] = []
    for item in items:
        if is_ref(item):
            element_id = item.get(REF_KEY)
            expanded = expand_action(item, elements.get(element_id), door) if isinstance(element_id, str) else None
            if expanded is None:
                drop(str(element_id), "button")
                continue
            out.append(expanded)
        else:
            out.append(item)
    return out


def _expand_cameras(items: list[Any], elements: dict[str, Any], drop: Callable[[str, str], None]) -> list[Any]:
    out: list[Any] = []
    for item in items:
        if is_ref(item):
            element_id = item.get(REF_KEY)
            expanded = expand_camera(item, elements.get(element_id)) if isinstance(element_id, str) else None
            if expanded is None:
                drop(str(element_id), "camera")
                continue
            out.append(expanded)
        else:
            out.append(item)
    return out


def _expand_entries(entries: Any, elements: dict[str, Any], drop: Callable[[str, str], None]) -> Any:
    """A rule's, declaration's or screen's camera list: an element id becomes the camera's entity id."""
    if not isinstance(entries, list):
        return entries
    out: list[Any] = []
    for entry in entries:
        if is_element_id(entry):
            element = elements.get(entry)
            if not placeable_camera(element):
                drop(entry, "camera")
                continue
            out.append(element["entity_id"])
        else:
            out.append(entry)
    return out


def _expand_pages(pages: Any, elements: dict[str, Any], drop: Callable[[str, str], None]) -> Any:
    if not isinstance(pages, list):
        return pages
    out = []
    for page in pages:
        if not isinstance(page, dict) or not isinstance(page.get("blocks"), list):
            out.append(page)
            continue
        blocks = []
        for block in page["blocks"]:
            if isinstance(block, dict):
                changed = {}
                if isinstance(block.get("cameras"), list):
                    changed["cameras"] = _expand_cameras(block["cameras"], elements, drop)
                if isinstance(block.get("actions"), list):
                    changed["actions"] = _expand_actions(block["actions"], elements, block.get("door"), drop)
                if changed:
                    block = {**block, **changed}
            blocks.append(block)
        out.append({**page, "blocks": blocks})
    return out


def _expand_rules(rules: Any, elements: dict[str, Any], drop: Callable[[str, str], None]) -> Any:
    if not isinstance(rules, list):
        return rules
    return [{**r, "cameras": _expand_entries(r.get("cameras"), elements, drop)} if isinstance(r, dict) and r.get("cameras")
            else r for r in rules]


def expand_document(document: dict[str, Any], elements: dict[str, Any], *,
                    on_drop: Callable[[str, str], None] | None = None) -> dict[str, Any]:
    """`document` with every reference replaced by the inline item (or entity id) it stands for. Pure: the argument is not
    changed, untouched parts are shared. A document with no reference is returned as the very same object. A reference whose
    element is missing, of the wrong kind or stream-only is dropped (`on_drop(element id, "camera")` is called once per
    drop; a missing element never fails a page: design 3.4.3)."""
    if not has_refs(document):
        return document

    def drop(element_id: str, kind: str) -> None:
        if on_drop is not None:
            on_drop(element_id, kind)

    out = dict(document)
    if is_pages_model(document):
        pages = _expand_pages(document.get("pages"), elements, drop)
        out["pages"] = [({**p, "cameras": _expand_entries(p.get("cameras"), elements, drop)}
                         if isinstance(p, dict) and page_kind(p) == KIND_RING and p.get("cameras") else p) for p in pages]
        return _expand_declared(out, document, elements, drop)
    template = document.get("template")
    if isinstance(template, dict):
        out["template"] = {**template, "pages": _expand_pages(template.get("pages"), elements, drop),
                           "rules": _expand_rules(template.get("rules"), elements, drop)}
    overrides = document.get("overrides")
    if isinstance(overrides, dict):
        out["overrides"] = {
            key: ({**o, "pages": _expand_pages(o.get("pages"), elements, drop),
                   "rules": _expand_rules(o.get("rules"), elements, drop)} if isinstance(o, dict) else o)
            for key, o in overrides.items()}
    return _expand_declared(out, document, elements, drop)


def _expand_declared(out: dict[str, Any], document: dict[str, Any], elements: dict[str, Any],
                     drop: Callable[[str, str], None]) -> dict[str, Any]:
    """The phone declarations' and screens' camera lists, expanded into `out` (the part both models share)."""
    declarations = document.get("endpoints")
    if isinstance(declarations, dict):
        out["endpoints"] = {k: ({**d, "cameras": _expand_entries(d.get("cameras"), elements, drop)}
                                if isinstance(d, dict) else d) for k, d in declarations.items()}
    dashboards = document.get("dashboards")
    if isinstance(dashboards, dict):
        out["dashboards"] = {k: ({**d, "cameras": _expand_entries(d.get("cameras"), elements, drop)}
                                 if isinstance(d, dict) else d) for k, d in dashboards.items()}
    return out


# ---- the save-time check -----------------------------------------------------------------------------------------------

def _check_action_ref(ref: dict[str, Any], element: dict[str, Any], where: str, stored: bool,
                      known_target: Callable[[str, str], bool] | None) -> None:
    from . import content

    item = ref["ref"]
    if element.get("kind") not in ("button", "talk"):
        raise ElementRefError("element_kind", f"{where}: only a button or talk element can go here")
    expanded = expand_action(item, element, ref.get("door"))
    kind = "talk" if element.get("kind") == "talk" else (element.get("options") or {}).get("action")
    # Ask-first is never loosened (the owner, 2026-11-09). A NEW reference that tries is refused. A reference that was already
    # stored (its element may have become a door/lock/gate action after it was made) must not block every later save: it is
    # fixed to true here, and expansion ignores a false anyway.
    if item.get("confirm") is False and guarded_kind(kind):
        if not stored:
            raise ElementRefError("confirm_weaker", f"{where}: a door, lock or gate button always requires confirmation; it cannot be turned off")
        item["confirm"] = True
    if stored:
        return
    if expanded is None:
        raise ElementRefError("not_placeable", f"{where}: that button cannot be placed here (its action, target or the block's door is missing)")
    if kind == "talk" and element.get("kind") == "button" and not ref.get("door"):
        raise ElementRefError("not_placeable", f"{where}: a talk button needs the block to have a door")
    if item.get("show_label") is False and not content._has_usable_icon(expanded.get("icon")):
        raise ElementRefError("show_label_icon", f"{where}: text can be hidden only on a button whose element has an icon")
    if kind == "call" and known_target is not None:
        target = expanded["target"]
        if target["type"] == "group":
            pass                           # expansion yields a group only for one of the three words; nothing live to check
        elif not known_target(target["type"], target["id"]):
            raise ElementRefError("not_placeable", f"{where}: there is nothing to call at that target")


def is_group_element(element: Any) -> bool:
    """True for a Talk element that calls a group (`options.group`, group-call design G1)."""
    return (isinstance(element, dict) and element.get("kind") == "talk"
            and (element.get("options") or {}).get("group") in TALK_GROUPS)


def check_group_placement(document: dict[str, Any], elements: dict[str, Any], group_hosts: frozenset[str] = frozenset(),
                          live_keys: frozenset[str] | None = None) -> None:
    """A group Talk element may sit only on a SCREEN's pages, or a PANEL's (`group_hosts`: the panel phone endpoint ids, which own
    their pages like a phone but are screens for house calls) (G5, HC-9: a phone calling a group is refused at the press, so the
    save says so earlier). A device that is not declared in `dashboards` is a phone. Schema 6: the pages each device lists; an
    older document: the template (it reaches phones) and the overrides of a key that is no screen. A Ring page cannot hold a
    Talk element at all (`check_refs`). `live_keys`, when given, are the endpoint ids that exist now: a device key that is no
    screen and no live endpoint is a leftover ("Not attached", e.g. a former panel screen) and is no phone to refuse. Raises
    ElementRefError (`not_placeable`)."""
    if not any(is_group_element(e) for e in elements.values()):
        return
    screens = document.get("dashboards") if isinstance(document.get("dashboards"), dict) else {}

    def holds_group(page: Any) -> bool:
        for block in (page.get("blocks") or ()) if isinstance(page, dict) else ():
            for item in (block.get("actions") or ()) if isinstance(block, dict) else ():
                if is_ref(item) and is_group_element(elements.get(item.get(REF_KEY))):
                    return True
        return False

    message = "a group call button can be placed only on a screen's page, not on a phone's"
    if is_pages_model(document):
        by_id = {p.get("id"): p for p in library(document) if isinstance(p, dict)}
        for key, device in sorted((document.get("devices") or {}).items()):
            if key in screens or key in group_hosts or not isinstance(device, dict):
                continue
            if live_keys is not None and key not in live_keys:
                continue            # a leftover entry no endpoint answers to: it shows nothing on any phone
            for entry in device.get("pages") or ():
                page = by_id.get(entry.get("page")) if isinstance(entry, dict) else None
                if holds_group(page):
                    raise ElementRefError("not_placeable", f"{message} (page {page.get('id')!r} on {key!r})")
        return
    for scope_name, scope in _scopes(document):
        if scope_name != "template" and scope_name in screens:
            continue
        for page in scope.get("pages") or ():
            if holds_group(page):
                raise ElementRefError("not_placeable", f"{message} (page {page.get('id')!r} in {scope_name!r})")


def _check_rule_talk(document: dict[str, Any], elements: dict[str, Any]) -> None:
    """A rule may not name a button whose element is a talk button (a ring is not the moment to start a talk); the plain
    check cannot see the kind of a reference."""
    template = document.get("template") if isinstance(document.get("template"), dict) else {}
    template_pages = template.get("pages") or []
    scopes = [("template", template_pages, template.get("rules") or [])]
    overrides = document.get("overrides")
    for key, override in (overrides.items() if isinstance(overrides, dict) else ()):
        if isinstance(override, dict):
            scopes.append((key, [*template_pages, *(override.get("pages") or [])], override.get("rules") or []))
    for scope_name, pages, rules in scopes:
        talk_ids = set()
        for page in pages:
            for block in (page.get("blocks") or ()) if isinstance(page, dict) else ():
                for item in (block.get("actions") or ()) if isinstance(block, dict) else ():
                    if is_ref(item):
                        element = elements.get(item.get(REF_KEY))
                        if isinstance(element, dict) and (element.get("kind") == "talk"
                                                          or (element.get("options") or {}).get("action") == "talk"):
                            talk_ids.add(item.get("id"))
        for rule in rules:
            for field in ("ring_buttons", "call_buttons"):
                for button in (rule.get(field) or ()) if isinstance(rule, dict) else ():
                    if button in talk_ids:
                        raise ElementRefError("element_kind", f"{scope_name} rule {rule.get('id')!r} {field}: {button!r} is a talk "
                                              "button; a ring is not the moment to start a talk")


def check_refs(document: dict[str, Any], elements: dict[str, Any], *, stored_ids: set[str] | frozenset[str] = frozenset(),
               stored_refs: frozenset[tuple[Any, ...]] | None = None,
               pending: set[str] | frozenset[str] = frozenset(),
               known_target: Callable[[str, str], bool] | None = None,
               group_hosts: frozenset[str] = frozenset(), live_keys: frozenset[str] | None = None) -> None:
    """Refuse a reference a save may not add (design 3.4.1). A reference to an element that exists must be the right kind and
    placeable (`element_kind` for another kind, `not_placeable` for a stream-only camera or a button that cannot be built). A
    reference to an element that does not exist is `element_missing` unless that element is already referenced by the STORED
    document (`stored_ids`): editing something else must still save, as it does for a call target that vanished. A reference
    to an element being deleted (`pending`) is `element_pending`. A button reference that turns ask-first off on a door, lock
    or gate action is `confirm_weaker` (a stored one is fixed to true instead). `stored_refs` (see `stored_ref_keys`) makes the
    "already stored" allowance per REFERENCE, so a new placement of the same element is checked in full; without it the old
    per-element `stored_ids` rule applies. Raises ElementRefError naming the item, never a value."""
    for ref in iter_refs(document):
        element_id = ref["element"]
        where = ref["where"]
        was_stored = (ref_key(ref) in stored_refs) if stored_refs is not None else (element_id in stored_ids)
        if not (is_talk_id(element_id) if ref["kind"] == "ringtalk" else is_element_id(element_id)):
            raise ElementRefError("element_missing", f"{where}: not an element id")
        if element_id in pending:
            raise ElementRefError("element_pending", f"{where}: that element is being removed")
        element = elements.get(element_id)
        if element is None:
            if was_stored:
                continue
            raise ElementRefError("element_missing", f"{where}: there is no such element")
        if ref["kind"] == "ringtalk":
            if element.get("kind") != "talk":
                raise ElementRefError("element_kind", f"{where}: only a Talk element can be the talk of a Ring page")
            if is_group_element(element) and not was_stored:
                raise ElementRefError("element_kind", f"{where}: a group call cannot be the talk of a Ring page; "
                                      "a ring is not the moment to call a group")
            continue
        if ref["kind"] == "action":
            if ref.get("ring") and not was_stored and (
                    element.get("kind") == "talk" or (element.get("options") or {}).get("action") == "talk"):
                raise ElementRefError("element_kind", f"{where}: a Ring page cannot hold a Talk element; "
                                      "a ring is not the moment to start a talk")
            _check_action_ref(ref, element, where, was_stored, known_target)
            continue
        if element.get("kind") != "camera":
            raise ElementRefError("element_kind", f"{where}: only a camera element can go here")
        if not placeable_camera(element) and not was_stored:
            raise ElementRefError("not_placeable", f"{where}: a camera with only a stream cannot be placed on a screen")
    _check_rule_talk(document, elements)
    check_group_placement(document, elements, group_hosts, live_keys)


def ref_key(ref: dict[str, Any]) -> tuple[Any, ...]:
    """What makes a reference THE SAME reference across two versions of a document: where it sits and which element it names.
    A block item: (scope, page, item id, element); a rule, declaration or screen camera: (scope, kind, element)."""
    if ref["kind"] in ("camera", "action"):
        return (ref["endpoint"], ref.get("page"), ref.get("item"), ref["element"])
    if ref["kind"] in ("ringtalk", "ringcamera"):
        return (ref["endpoint"], ref["kind"], ref.get("page"), ref["element"])
    return (ref["endpoint"], ref["kind"], ref["element"])


def stored_ref_keys(document: dict[str, Any]) -> frozenset[tuple[Any, ...]]:
    return frozenset(ref_key(r) for r in iter_refs(document))


def referenced_ids(document: dict[str, Any]) -> set[str]:
    return {ref["element"] for ref in iter_refs(document)}
