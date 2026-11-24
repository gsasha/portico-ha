"""The resolvers of the content store, pure over a document (M6 E1b, design 2.4-2.6, R6-07).

A *document* is the stored v4 file's data: `{template: {pages, rules, roster}, overrides: {<endpoint id>: {pages,
rules, roster_hide}}, dashboards, endpoints, home_name?}` (what `ContentStore.document()` returns, and what
`portico/content/preview` will pass in). Nothing here reads the store, Home Assistant, the clock or the network:

- `states`    anything with `.get(entity_id)` returning an object with `.state` and `.attributes` (hass.states);
- `registry`  an object with `endpoints()` (the endpoint registry's list, phones/doors/dashboards), `door(device_id)`
              (the Door a Portico `device_id` names, or None) and `call_state(action)` (`{label, available, reason}`
              for a call button) - `ContentStore` is the production one.

so the same functions serve `content/get` (the store is one caller) and `content/preview` (an unsaved document is the
other). The rules are the requirements' 6.2 order, written as `resolve_rule`; what a rule does not say is
filled in by `default_buttons` / the caller's declaration.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import replace
from typing import Any

from .endpoints import KIND_DASHBOARD, KIND_DOOR, KIND_PHONE, Endpoint
from .pages_resolve import (
    WHEN_CALL, WHEN_RING, assigned_ring_page, device_pages, is_pages_model, roster_hide_of, roster_of,
)

_LOGGER = logging.getLogger(__name__)

HOME_PAGE = "home"
ROSTER_ALL = "*"
TALK_OFF = "talk_off"         # the reason a Talk button is off today: no door reports `CanDial` (blocks design 7.3)
UNKNOWN_CALLER = "*"          # the `callers` key for a caller the registry does not know
MAX_RESOLVED_BYTES = 64 * 1024   # S6: a resolved document over this logs a warning

# live state: not part of `rev`. `door_available` (a block's door) is live like `available`: without it every flip of the
# gate's availability would change every endpoint's `rev` and make every phone and the kiosk refetch (blocks design 4.1).
# `can_dial` (the gate's roster entry, M7 3f) is live like `available`: Talk's availability is never part of `rev` either, so a
# reload of the gate that turns the dial on does not make every phone and the kiosk refetch (test_talk_gate); the house
# roster `rev` (`house_roster_changed`) is what moves. `gate_call` (a call button's own marker, DEV-695) follows the same
# `CanDial`, so it is live for the same reason.
_VOLATILE = frozenset({"available", "name", "reason", "target_state", "door_available", "count", "can_dial",
                      "gate_call"})


# ---- hashes -------------------------------------------------------------------

def _digest(obj: Any) -> str:
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def doc_rev(document: dict[str, Any]) -> str:
    """The hash of the raw stored document: what `content/document` returns and `content/save` takes back as
    `base_doc_rev` (R6-06). Not the same thing as an endpoint's `rev`."""
    return _digest(document)


def _stable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _stable(v) for k, v in obj.items() if k not in _VOLATILE and k != "rev"}
    if isinstance(obj, list):
        return [_stable(v) for v in obj]
    return obj


def endpoint_rev(resolved: dict[str, Any]) -> str:
    """The hash of one endpoint's resolved document minus `available`, `name` and the other live-state fields, so
    it changes when the content changes and not when a camera blinks (as `profile_rev` does for the flat profile)."""
    return _digest(_stable(resolved))


def short_id(prefix: str, value: str, length: int = 12) -> str:
    """`c-<12 hex>` for a caller's camera, `r-<12 hex>` for a roster entry: ids a client can hold without ever
    holding the entity id or endpoint id behind them (CB-1)."""
    return f"{prefix}-{hashlib.sha256(value.encode('utf-8')).hexdigest()[:length]}"


# ---- pages ------------------------------------------------------------------------

def page_cameras(page: dict[str, Any]) -> list[dict[str, Any]]:
    """Every camera of a page, of every block in block order."""
    return [c for block in page.get("blocks", []) for c in block.get("cameras", [])]


def block_actions(block: dict[str, Any]) -> list[dict[str, Any]]:
    """Every action of one block, of every kind, with the block's `door` written into a `portico_open` action that
    names none (`default_buttons` and the run grant read raw merged pages, not resolved ones, so the `device_id` has
    to be materialised where blocks are read; blocks design 3.3/3.6)."""
    door = block.get("door")
    out: list[dict[str, Any]] = []
    for action in block.get("actions", []):
        if door and action.get("kind") == "portico_open" and not action.get("device_id"):
            action = {**action, "device_id": door}
        out.append(action)
    return out


def page_actions(page: dict[str, Any]) -> list[dict[str, Any]]:
    """Every action of a page, of every kind, of every block in block order, `device_id` materialised
    (`block_actions`)."""
    return [a for block in page.get("blocks", []) for a in block_actions(block)]


def merged_pages(document: dict[str, Any], endpoint_id: str) -> list[dict[str, Any]]:
    """The template's pages with the endpoint's override laid over them: an override page replaces the template
    page of the same id, any other joins at the end. For a schema 6 document there is no merging: the device's own list
    (`pages_resolve.device_pages`)."""
    if is_pages_model(document):
        return device_pages(document, endpoint_id)
    template = document.get("template") or {}
    merged = {p["id"]: p for p in template.get("pages", [])}
    for page in ((document.get("overrides") or {}).get(endpoint_id) or {}).get("pages", []):
        merged[page["id"]] = page
    return list(merged.values())


def _entity_name(states: Any, entity_id: str) -> str:
    state = states.get(entity_id)
    friendly = state.attributes.get("friendly_name") if state else None
    return str(friendly) if friendly else entity_id


def _entity_available(states: Any, entity_id: str) -> bool:
    state = states.get(entity_id)
    return state is not None and state.state not in ("unavailable", "unknown")


def _call_state(registry: Any, action: dict[str, Any], viewer: Endpoint | None) -> dict[str, Any]:
    """A call button's or a roster entry's state. A registry that knows who is looking (`call_state_for`: the store, for
    the gate target of M7 3f, which only a phone can place) is asked with the viewer; every other registry, and every
    call with no viewer, is asked as before."""
    for_viewer = getattr(registry, "call_state_for", None)
    if viewer is not None and callable(for_viewer):
        return for_viewer(action, viewer)
    return registry.call_state(action)


def _is_gate_call(registry: Any, action: dict[str, Any]) -> bool:
    """True when a call button's target is the gate and a press of it goes through Talk's path (the store's `gate_call`:
    a door that reports `CanDial` now, and that the placement registry does not own). Never raises, and fails to False:
    a registry with no `gate_call` (every other registry, a test double) marks nothing, so the key is simply absent."""
    ask = getattr(registry, "gate_call", None)
    if not callable(ask):
        return False
    try:
        return ask(action["target"]) is True
    except Exception as err:
        _LOGGER.warning("portico content: the gate-call marker failed: %s", type(err).__name__)
        return False


def resolve_action(action: dict[str, Any], *, states: Any, registry: Any, door: str | None = None,
                   endpoint: Endpoint | None = None) -> dict[str, Any]:
    """An action with `name` and `available` (a call button also `reason` and `target_state`; a `talk` button also
    `reason`). `door` is the block's door and `endpoint` the endpoint the document is resolved for: only a `talk`
    button reads them (blocks design 4.1: `talk` asks `registry.talk_state`)."""
    if action["kind"] == "call":
        state = _call_state(registry, action, endpoint)
        out = {**action, "name": state["label"], "available": state["available"], "reason": state["reason"],
               "target_state": state}
        if _is_gate_call(registry, action):
            out["gate_call"] = True      # DEV-695: absent unless this button is the gate call; independent of the roster
        return out
    if action["kind"] == "talk":
        own = action.get("door")          # a Talk element's explicit door (DEV-518); else the block's
        state = _talk_state(registry, own or door, endpoint, explicit=bool(own))
        return {**action, "available": state["available"], "name": action["label"], "reason": state["reason"]}
    if action["kind"] == "portico_open":
        door = registry.door(action["device_id"])
        return {**action, "available": door is not None, "name": door.name if door is not None else action["device_id"]}
    return {**action, "available": _entity_available(states, action["entity_id"]),
            "name": _entity_name(states, action["entity_id"])}


def _talk_state(registry: Any, door: str | None, endpoint: Endpoint | None, *, explicit: bool = False) -> dict[str, Any]:
    """`{available, reason}` of a Talk button, from the registry's `talk_state` (blocks design 7.3/7.4). Never raises,
    and fails closed: a registry that has no `talk_state`, or that raises, means `talk_off`. Nothing here touches the
    provider; the registry's answer is a read."""
    ask = getattr(registry, "talk_state", None)
    if ask is None:
        return {"available": False, "reason": TALK_OFF}
    try:
        state = ask(door, endpoint, explicit=True) if explicit else ask(door, endpoint)
        available, reason = state["available"] is True, state["reason"]
        return {"available": available, "reason": reason if available or isinstance(reason, str) else TALK_OFF}
    except Exception as err:
        _LOGGER.warning("portico content: the talk state failed: %s", type(err).__name__)
        return {"available": False, "reason": TALK_OFF}


def effective_size(camera: dict[str, Any], first: bool) -> str:
    """A camera's size as the surfaces draw it: `big`/`small` as stored; `auto` (or absent) is `big` for the first camera
    of its block and `small` for the others (blocks design D5, 4.1)."""
    size = camera.get("size")
    if size in ("big", "small"):
        return size
    return "big" if first else "small"


def _fallback_small(registry: Any, entity_id: str) -> str | None:
    """The small stream the install-wide `live_sub_cameras` option maps `entity_id` to (the store offers it; a fake registry
    does not), used only when the element has no small stream of its own. Never raises."""
    ask = getattr(registry, "fallback_small_entity", None)
    try:
        return ask(entity_id) if callable(ask) else None
    except Exception:  # noqa: BLE001 - a read that fails is "no small stream"
        return None


def _streams(registry: Any, entity_id: str, small: str | None) -> dict[str, dict[str, int]]:
    """`{"big": {width, height}, "small": {width, height}}` for the sizes the registry knows (the store offers
    `stream_size(entity_id) -> (w, h) | None`; a fake registry does not), each key only when known. The small stream's
    entity id is used here and never leaves the server. Never raises."""
    ask = getattr(registry, "stream_size", None)
    out: dict[str, dict[str, int]] = {}
    if not callable(ask):
        return out
    for key, entity in (("big", entity_id), ("small", small)):
        if not entity:
            continue
        try:
            size = ask(entity)
        except Exception:  # noqa: BLE001 - a read that fails is "size unknown"
            continue
        if size:
            out[key] = {"width": int(size[0]), "height": int(size[1])}
    return out


def resolve_block(block: dict[str, Any], *, states: Any, registry: Any, endpoint: Endpoint | None = None) -> dict[str, Any]:
    """One block resolved: `{id, title, door?, door_available?, cameras, actions}`. A camera carries `available`, `name`
    and its effective `size`; an action every kind (the 3.3 kinds and `talk` included), `device_id` materialised from
    the block's door, `color` / `show_label` as stored, `available`/`name` (and `reason` for `call` and `talk`).
    `door_available` (only when the block has a `door`) is true when `registry.door(door)` knows the door AND the
    provider reports it up; a door that was removed and one that is down both read false (the `portico_open` button's
    own `available` still says only whether the door is known). It is a live field and not part of `rev`.
    A `live: true` camera whose registry offers `live_rtsp(entity_id, state)` (the store does) may also carry `live_rtsp`, the
    away route's stream hint (live_rtsp.py): computed here per request, never stored, and part of `rev`."""
    out: dict[str, Any] = {"id": block["id"], "title": block["title"]}
    door = block.get("door")
    if door:
        known = registry.door(door)
        out["door"] = door
        out["door_available"] = known is not None and bool(getattr(known, "available", True))
    live_rtsp = getattr(registry, "live_rtsp", None)       # the store only: a registry without it (a fake) adds nothing
    cameras = []
    for n, c in enumerate(block.get("cameras", [])):
        item = {**c, "size": effective_size(c, n == 0), "available": _entity_available(states, c["entity_id"]),
                "name": _entity_name(states, c["entity_id"])}
        # DEV-768: the small stream's entity id never leaves the server; the client learns only that one exists
        small = item.pop("small_entity_id", None) or _fallback_small(registry, c["entity_id"])
        if small:
            item["small"] = True
        item.pop("streams", None)           # only ever computed, never what a stored document says
        streams = _streams(registry, c["entity_id"], small)
        if streams:
            item["streams"] = streams       # additive, computed per request, never stored; part of `rev`
        if c.get("live") is True and callable(live_rtsp):
            # additive, computed per request and never stored: the stream hint for the app's away route (live_rtsp.py)
            hint = live_rtsp(c["entity_id"], states.get(c["entity_id"]), has_small=True) if small \
                else live_rtsp(c["entity_id"], states.get(c["entity_id"]))
            if hint:
                item["live_rtsp"] = hint
        cameras.append(item)
    out["cameras"] = cameras
    out["actions"] = [resolve_action(a, states=states, registry=registry, door=door, endpoint=endpoint)
                      for a in block_actions(block)]
    return out


def resolve_page(page: dict[str, Any], *, states: Any, registry: Any, endpoint: Endpoint | None = None) -> dict[str, Any]:
    """A page resolved as the wire serves it: `{blocks}` and nothing else (the derived flat `cameras`/`actions` went
    in B8; blocks design 3.6). The blocks are the resolved ones (`resolve_block`, every kind)."""
    return {"blocks": [resolve_block(b, states=states, registry=registry, endpoint=endpoint)
                       for b in page.get("blocks", [])]}


# ---- who is who -------------------------------------------------------------------

def _app_screen(registry: Any, endpoint_id: Any) -> bool:
    """Whether the registry (the content store, in production) says `endpoint_id` is the screen half of a panel. A registry
    without the method (a test double, an older caller) has no panels, so nothing is hidden (DEV-807)."""
    check = getattr(registry, "is_app_screen", None)
    try:
        return bool(check(endpoint_id)) if callable(check) else False
    except Exception:  # noqa: BLE001 - a registry that errors hides nothing
        return False


def _phone_half_of(registry: Any, endpoint_id: Any) -> str | None:
    """The id of the phone half when `endpoint_id` is a panel's app screen, else None."""
    phone_of = getattr(registry, "phone_of", None)
    try:
        phone = phone_of(endpoint_id) if callable(phone_of) and _app_screen(registry, endpoint_id) else None
    except Exception:  # noqa: BLE001
        return None
    return getattr(phone, "id", None)


def endpoint_table(document: dict[str, Any], registry: Any) -> list[Endpoint]:
    """The endpoints as this document sees them: phones and doors from the registry, a phone's cameras from the
    document's `endpoints`, and the dashboards from the DOCUMENT (so a preview shows a screen not yet saved)."""
    declared = document.get("endpoints") or {}
    out: list[Endpoint] = []
    for ep in registry.endpoints():
        if ep.kind == KIND_DASHBOARD:
            continue                                  # replaced by the document's own below
        if ep.kind == KIND_PHONE:
            ep = replace(ep, cameras=tuple((declared.get(ep.id) or {}).get("cameras", ())))
        out.append(ep)
    dashboards = []
    for slug, d in (document.get("dashboards") or {}).items():
        dashboards.append(Endpoint(
            id=slug, kind=KIND_DASHBOARD, provider=None, device_id=None, name=d.get("name") or slug,
            ha_user_id=d.get("ha_user_id"), cameras=tuple(d.get("cameras", ())), room=d.get("room"),
            override=d.get("override"), available=True, reason=None))
    # registry order: phones, then dashboards, then doors
    phones = [e for e in out if e.kind == KIND_PHONE]
    doors = [e for e in out if e.kind == KIND_DOOR]
    return phones + dashboards + doors


# ---- rules ------------------------------------------------------------------------

def resolve_rule(document: dict[str, Any], callee_id: str, caller_id: str | None,
                 caller_kind: str | None) -> dict[str, Any] | None:
    """The rule for a call from `caller_id` (of `caller_kind`) to `callee_id`, in the order of requirements 6.2:

    1. the callee's override rule for the caller's id;
    2. the callee's override rule for `kind:<caller kind>`;
    3. the template rule for the caller's id;
    4. the template rule for the caller's kind;

    or None (steps 5 and 6 are `resolve_caller`'s: the caller's own declaration, then the name alone). Within a
    step the first rule in list order wins."""
    override_rules = ((document.get("overrides") or {}).get(callee_id) or {}).get("rules", [])
    template_rules = (document.get("template") or {}).get("rules", [])
    keys = []
    if caller_id is not None:
        keys.append(caller_id)
    if caller_kind is not None:
        keys.append(f"kind:{caller_kind}")
    for rules in (override_rules, template_rules):
        for key in keys:
            for rule in rules:
                if rule.get("caller") == key:
                    return rule
    return None


def default_buttons(pages: list[dict[str, Any]], door: Endpoint) -> list[dict[str, Any]]:
    """Step 5 for a door caller (R6-03): every `portico_open` action whose `device_id` is THIS door plus every
    `lock_open` action of the callee's page set, in page order. It is the card's behaviour made precise: the
    card shows every open-kind button whichever door rings (portico-card.js DOOR_KINDS)."""
    out, seen = [], set()
    for page in pages:
        for action in page_actions(page):
            if action["id"] in seen:
                continue
            if action["kind"] == "lock_open" or (action["kind"] == "portico_open" and door.names_door(action.get("device_id"))):
                seen.add(action["id"])
                out.append(action)
    return out


def _find_actions(pages: list[dict[str, Any]], ids: list[str], where: str) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    clash: set[str] = set()
    for page in pages:
        for action in page_actions(page):
            if action["id"] in by_id and by_id[action["id"]] != action:
                clash.add(action["id"])      # two different buttons share this id: a rule cannot name either (fail closed)
            by_id.setdefault(action["id"], action)
    out = []
    for action_id in ids:
        if action_id in clash:
            _LOGGER.warning("portico content: %s names the button %r, which two different pages share; dropped", where, action_id)
        elif action_id in by_id and by_id[action_id].get("kind") == "talk":
            # a ring is not the moment to start a talk (blocks design 3.3): the save refuses it, and a file that
            # holds one anyway is read without it
            _LOGGER.warning("portico content: %s names the talk button %r; dropped", where, action_id)
        elif action_id in by_id:
            out.append(by_id[action_id])
        else:
            _LOGGER.warning("portico content: %s names the button %r, which the page set no longer has; dropped",
                            where, action_id)
    return out


def ring_call_actions(page: dict[str, Any], where: str = "") -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """`(ring_buttons, call_buttons)` of a Ring page: the actions of the blocks whose `when` is absent or `ring`, and absent or
    `call`, in block order (design 2.6). A `talk` action is dropped with a warning (a ring is not the moment to start a talk;
    the save refuses it), and an id repeated inside one list is kept once."""
    ring: list[dict[str, Any]] = []
    call: list[dict[str, Any]] = []
    for block in page.get("blocks", []):
        when = block.get("when")
        for action in block_actions(block):
            if action.get("kind") == "talk":
                _LOGGER.warning("portico content: ring page %r holds the talk button %r; dropped", page.get("id"), action.get("id"))
                continue
            if when in (None, WHEN_RING) and all(a["id"] != action["id"] for a in ring):
                ring.append(action)
            if when in (None, WHEN_CALL) and all(a["id"] != action["id"] for a in call):
                call.append(action)
    return ring, call


def caller_camera_entities(document: dict[str, Any], callee_id: str, caller: Endpoint | None,
                           registry: Any = None) -> list[str]:
    """The camera entity ids `resolve_caller` shows for a call from `caller` to `callee_id`, in order: the rule's
    list (a schema 6 document: the assigned Ring page's), or (rule absent or `cameras: null`) the caller's declaration; only
    `camera.*` entities. It is the grant of `content/snapshot` for a `c-...` id, so it must name exactly the cameras
    `resolve_caller` resolves. `registry` is only read for a schema 6 document (which Talk element stands for the caller)."""
    if caller is None:
        return []
    if is_pages_model(document):
        page = assigned_ring_page(document, callee_id, caller, registry)
        cameras = None if page is None else page.get("cameras")
        entities = list(caller.cameras) if cameras is None else list(cameras)
        return [e for e in entities if isinstance(e, str) and e.startswith("camera.")]
    rule = resolve_rule(document, callee_id, caller.id, caller.kind)
    entities = list(caller.cameras) if rule is None or rule["cameras"] is None else list(rule["cameras"])
    return [e for e in entities if isinstance(e, str) and e.startswith("camera.")]


def resolve_caller(document: dict[str, Any], callee_id: str, caller: Endpoint | None, *,
                   pages: list[dict[str, Any]] | None = None, states: Any, registry: Any) -> dict[str, Any]:
    """What `callee_id`'s ring and in-call screens show for a call from `caller` (None = a caller the registry does
    not know): `{name, kind, cameras, ring_buttons, call_buttons}`, resolved (design 2.4, steps 1 to 6).

    A rule with `cameras: null` takes its cameras from the caller's declaration (step 5) and its buttons from
    the rule. A camera that is not an HA `camera` entity, and a button the page set no longer has, are dropped
    with a warning. A camera whose registry offers `live_rtsp(entity_id, state)` (the store does) may also carry `live_rtsp`,
    exactly as a page camera does (`resolve_block`) except that no `live: true` is needed."""
    if caller is None:
        return {"name": None, "kind": None, "cameras": [], "ring_buttons": [], "call_buttons": []}
    pages = merged_pages(document, callee_id) if pages is None else pages
    rule = None if is_pages_model(document) else resolve_rule(document, callee_id, caller.id, caller.kind)
    declared = list(caller.cameras)
    if is_pages_model(document):
        # Schema 6: the Ring page of the Talk element standing for the caller, else the built-in default
        page = assigned_ring_page(document, callee_id, caller, registry)
        if page is None:
            camera_entities = declared
            ring = call = default_buttons(pages, caller) if caller.kind == KIND_DOOR else []
        else:
            camera_entities = declared if page.get("cameras") is None else page["cameras"]
            ring, call = ring_call_actions(page)
    elif rule is None:
        camera_entities = declared
        ring = call = default_buttons(pages, caller) if caller.kind == KIND_DOOR else []
    else:
        camera_entities = declared if rule["cameras"] is None else rule["cameras"]
        where = f"rule {rule['id']!r}"
        ring = _find_actions(pages, rule["ring_buttons"], where)
        call = _find_actions(pages, rule["call_buttons"], where)
    live_rtsp = getattr(registry, "live_rtsp", None)       # the store only: a registry without it (a fake) adds nothing
    cameras = []
    for entity_id in camera_entities:
        if not isinstance(entity_id, str) or not entity_id.startswith("camera."):
            _LOGGER.warning("portico content: dropped %r from %s's cameras (not a camera entity)", entity_id, caller.name)
            continue
        # `label` is the caller's name (stable, from the document); `name` is the camera's live friendly name.
        label = caller.name if len(camera_entities) == 1 else f"{caller.name} {len(cameras) + 1}"
        item = {"id": short_id("c", entity_id), "label": label, "name": _entity_name(states, entity_id),
                "available": _entity_available(states, entity_id)}
        if callable(live_rtsp):
            # additive, computed per request and never stored (live_rtsp.py). A caller camera has no per-camera `live` flag:
            # the app decides (the first one shows live), the hint only says how to reach it away from home.
            hint = live_rtsp(entity_id, states.get(entity_id))
            if hint:
                item["live_rtsp"] = hint
        cameras.append(item)
    return {"name": caller.name, "kind": caller.kind, "cameras": cameras,
            "ring_buttons": [resolve_action(a, states=states, registry=registry) for a in ring],
            "call_buttons": [resolve_action(a, states=states, registry=registry) for a in call]}


# ---- the roster -------------------------------------------------------------------

def roster_action_id(endpoint_id: str) -> str:
    return short_id("r", endpoint_id)


def resolve_roster(document: dict[str, Any], callee_id: str, *, endpoints: list[Endpoint],
                   registry: Any) -> list[dict[str, Any]]:
    """The roster of `callee_id`: the template's list (`["*"]` = every endpoint in registry order) minus the
    callee's `roster_hide` minus the callee itself, each entry `{action_id, id, kind, name, available, reason}`.
    `available`/`reason` are the call-endpoint registry's first and the endpoint registry's second, exactly as a
    `call` button resolves; until M7 every entry is `available: false, reason: not_available`."""
    wanted = roster_of(document)
    hidden = set(roster_hide_of(document, callee_id))
    by_id = {e.id: e for e in endpoints}
    ids = [e.id for e in endpoints] if wanted == [ROSTER_ALL] else list(wanted)
    # A panel's app screen is offered to no one as a callee (DEV-807, R1); the panel's own roster also omits its phone half.
    phone_half = _phone_half_of(registry, callee_id)
    ids = [i for i in ids if not _app_screen(registry, i) and i != phone_half]
    groups = getattr(registry, "roster_groups", None)       # a screen's "Everyone" key (M7 3a): none unless house calls are on
    out = [g for g in (groups(callee_id, endpoints) if callable(groups) else []) if g["id"] not in hidden]
    for endpoint_id in ids:
        if endpoint_id == callee_id or endpoint_id in hidden:
            continue
        ep = by_id.get(endpoint_id)
        kind = ep.kind if ep is not None else None
        name = ep.name if ep is not None else endpoint_id
        state = _call_state(registry, {"label": name, "target": {"type": kind or KIND_PHONE, "id": endpoint_id}},
                            by_id.get(callee_id))
        entry = {"action_id": roster_action_id(endpoint_id), "id": endpoint_id, "kind": kind,
                 "name": state["label"] if ep is not None else name,
                 "available": state["available"], "reason": state["reason"]}
        if ep is not None and ep.can_dial:
            entry["can_dial"] = True         # the gate (3f): present only while its door reports `CanDial`; absent otherwise
        out.append(entry)
    return out


# ---- the endpoint's document ------------------------------------------------------

def resolve_endpoint(document: dict[str, Any], endpoint_id: str, *, states: Any, registry: Any,
                     caller: str | None = None) -> dict[str, Any]:
    """The one resolved document of an endpoint (design 2.6): pages, `callers` for every endpoint the registry
    knows (and `"*"` for one it does not), the roster, and its own `rev`. `caller`, when given, narrows `callers`
    to that one endpoint (what `content/preview` asks for)."""
    endpoints = endpoint_table(document, registry)
    me = next((e for e in endpoints if e.id == endpoint_id), None)
    pages = merged_pages(document, endpoint_id)
    out: dict[str, Any] = {"endpoint": endpoint_id, "kind": me.kind if me is not None else None}
    home_name = document.get("home_name")
    if home_name:
        out["home_name"] = home_name
    if document.get("show_fps") is True:
        out["show_fps"] = True      # additive: absent is false, so a document without it resolves byte-identically
    out["pages"] = [{"id": p["id"], "label": p.get("label") or p["id"], **resolve_page(p, states=states, registry=registry, endpoint=me)}
                    for p in pages]
    callers: dict[str, Any] = {}
    for ep in endpoints:
        if ep.id == endpoint_id or (caller is not None and ep.id != caller):
            continue
        if ep.kind == KIND_DASHBOARD and _app_screen(registry, ep.id):
            continue            # a panel's app screen is nobody's caller (DEV-807, R1); its own document still names itself
        callers[ep.id] = resolve_caller(document, endpoint_id, ep, pages=pages, states=states, registry=registry)
    callers[UNKNOWN_CALLER] = resolve_caller(document, endpoint_id, None, states=states, registry=registry)
    out["callers"] = callers
    out["roster"] = resolve_roster(document, endpoint_id, endpoints=endpoints, registry=registry)
    out["rev"] = endpoint_rev(out)
    size = len(json.dumps(out, separators=(",", ":")))
    if size > MAX_RESOLVED_BYTES:
        _LOGGER.warning("portico content: the resolved document of %s is %d bytes (over %d)", endpoint_id, size,
                        MAX_RESOLVED_BYTES)
    return out
