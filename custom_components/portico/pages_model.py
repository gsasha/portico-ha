"""The schema 6 document: validators and the lenient loader (Pages unified, P-A; design `portico_pages_unify_design.md`
sections 2, 3.0 and 4).

    {schema_version: 6, home_name?, show_fps?, roster?, pages: [...], devices: {...}, dashboards, endpoints}

`normalize_document_v6` is the strict form (a `content/save` or `content/preview`): everything validates, nothing is guessed,
an unknown field is refused. `load_pages_model` is the lenient form (the store reading its own file): an invalid page is
dropped with a warning, an assignment to a page that is gone is KEPT (the resolver skips it), so a hand-edited file never
takes the others down and never loses a device's list silently.

The rules (section 4):
- a Ring page has exactly one `talk` (a Talk element id) and `cameras` (null = the caller's own cameras, or a list of camera
  entity ids / Camera element ids; the editor offers one, a migrated page may keep more, OPTION 4);
- a Ring page's blocks hold buttons only (no Talk element, no camera) and each may say `when: "ring" | "call"` (absent = both);
  an id may repeat across a `ring` block and a `call` block (the two audiences never see both), never inside one audience;
- a device's `pages` names content pages only, no page twice, unique wire ids (`as`), at most MAX_PAGES;
- there is at most one Ring page per `talk` (design `portico_ring_pages_per_talk_design.md`, D-20261122-4aec). A device's `ring`
  map is the retired per-device form: a strict document may still carry one (a restored old backup) and it is read as `{}`,
  ignored; the lenient load keeps it only so `portico/content/ring_pages_migrate` can analyse such a document;
- the existing per-block validators (button kinds, ask-first, ids, limits) run unchanged on every block.
That a Talk element EXISTS is the store's check (`content_elements.check_refs`, kind `ringtalk`), as for every reference.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable, Collection
from typing import Any

from .content import (
    ID_RE, MAX_ACTIONS, MAX_BLOCKS, MAX_PAGES, MAX_ROSTER, MAX_ENDPOINT_ID, ProfileError, _camera_entities,
    _endpoint_key, _label, _normalize_dashboard, _only_keys, default_page_label, normalize_block, normalize_endpoint_declaration,
    normalize_home_name, normalize_page, normalize_roster,
)
from .content_resolve import ROSTER_ALL, block_actions
from .pages_resolve import (
    KIND_CONTENT, KIND_RING, SCHEMA_PAGES, WHENS, assigned_ring_pages, device_pages, is_talk_id, page_kind,
)

_LOGGER = logging.getLogger(__name__)

MAX_LIBRARY_PAGES = 64      # the library holds every device's pages and the Ring pages (design 4.4 says "the existing 12 ... for
                            # the library as a whole"; a library shared by phones and screens cannot be that small, so the
                            # 12 stays per DEVICE (MAX_PAGES) and the library has its own bound)
DOCUMENT_KEYS_V6 = frozenset({"schema_version", "home_name", "show_fps", "roster", "pages", "devices", "dashboards",
                              "endpoints"})
DEVICE_KEYS = frozenset({"pages", "ring", "roster_hide"})
ENTRY_KEYS = frozenset({"page", "as"})
PAGE_KEYS = {KIND_CONTENT: frozenset({"id", "label", "kind", "blocks"}),
             KIND_RING: frozenset({"id", "label", "kind", "blocks", "talk", "cameras"})}


# ---- pages ------------------------------------------------------------------------------------------------------------

def _page_id(raw: dict[str, Any], seen: set[str]) -> str:
    page_id = raw.get("id")
    if not isinstance(page_id, str) or not ID_RE.match(page_id) or page_id in seen:
        raise ProfileError("a page needs a unique id of 1-32 characters of a-z, 0-9, _ or -")
    return page_id


def _normalize_ring_page(raw: dict[str, Any], page_id: str, label: str, *, known_target, strict: bool) -> dict[str, Any]:
    where = f"ring page {page_id}"
    talk = raw.get("talk")
    if not is_talk_id(talk):
        if strict or not isinstance(talk, str) or not talk:
            raise ProfileError(f"{where}: a Ring page needs the Talk element it is the ring page of")
    cameras_in = raw.get("cameras")
    cameras = None if cameras_in is None else _camera_entities(cameras_in, where)
    blocks_in = raw.get("blocks", [])
    if not isinstance(blocks_in, list):
        raise ProfileError(f"{where}: blocks must be a list")
    if strict and len(blocks_in) > MAX_BLOCKS:
        raise ProfileError(f"{where}: at most {MAX_BLOCKS} blocks")
    notes: list[str] = []
    seen_blocks: set[str] = set()
    ring_ids: set[str] = set()      # item ids shown while ringing (blocks with `when` ring, or none)
    call_ids: set[str] = set()      # ... during the call (blocks with `when` call, or none)
    blocks: list[dict[str, Any]] = []
    for n, raw_block in enumerate(blocks_in):
        if not isinstance(raw_block, dict):
            raise ProfileError(f"{where}: a block must be an object")
        when = raw_block.get("when")
        if when is not None and when not in WHENS:
            raise ProfileError(f"{where}: a block's when must be one of {', '.join(WHENS)} (or absent)")
        if strict and (raw_block.get("cameras") or raw_block.get("door")):
            raise ProfileError(f"{where}: a Ring page's blocks hold buttons only; its camera is chosen on the page")
        if when is None:
            audience = ring_ids | call_ids
        else:
            audience = ring_ids if when == "ring" else call_ids
        seen = set(audience)
        block = normalize_block({k: v for k, v in raw_block.items() if k != "when"}, seen_blocks, seen, page_id=page_id,
                                known_target=known_target, strict=strict, notes=notes, position=n + 1)
        fresh = seen - audience
        if when is None:
            ring_ids |= fresh
            call_ids |= fresh
        elif when == "ring":
            ring_ids |= fresh
        else:
            call_ids |= fresh
        if when is not None:
            block["when"] = when
        if any(a.get("kind") == "talk" for a in block["actions"]):
            if strict:
                raise ProfileError(f"{where}: a Ring page cannot hold a Talk button; a ring is not the moment to start a talk")
            block["actions"] = [a for a in block["actions"] if a.get("kind") != "talk"]
            notes.append("a talk button")
        blocks.append(block)
    if strict:
        if len(ring_ids) > MAX_ACTIONS or len(call_ids) > MAX_ACTIONS:
            raise ProfileError(f"{where}: at most {MAX_ACTIONS} buttons")
        titles: set[str] = set()
        for block in blocks:
            folded = block["title"].casefold()
            if folded in titles:
                raise ProfileError(f"{where}: two blocks are called {block['title']!r}")
            titles.add(folded)
    if notes:
        _LOGGER.warning("portico content: %s was read with %d value(s) it does not know (%s)", where, len(notes),
                        "; ".join(notes[:3]))
    return {"id": page_id, "label": label, "kind": KIND_RING, "talk": talk, "cameras": cameras, "blocks": blocks}


def normalize_library_page(raw: Any, seen: set[str], *, known_target: Callable[[str, str], bool] | None = None,
                           strict: bool = True) -> dict[str, Any]:
    """One validated library page, content or ring. `seen` (library page ids so far) makes a repeated id an error and is added
    to. Returns `{id, label, kind, blocks}` (a Ring page also `talk` and `cameras`). Raises ProfileError."""
    if not isinstance(raw, dict):
        raise ProfileError("a page must be an object")
    page_id = _page_id(raw, seen)
    kind = raw.get("kind", KIND_CONTENT)
    if kind not in (KIND_CONTENT, KIND_RING):
        raise ProfileError(f"page {page_id}: the kind must be {KIND_CONTENT} or {KIND_RING}")
    if strict:
        _only_keys(raw, PAGE_KEYS[kind], f"page {page_id}")
    label = raw.get("label")
    label = default_page_label(page_id) if label is None else _label(label, f"page {page_id}")
    if kind == KIND_RING:
        page = _normalize_ring_page(raw, page_id, label, known_target=known_target, strict=strict)
    else:
        if "blocks" not in raw:
            raise ProfileError(f"page {page_id}: blocks are required")
        if strict and any(isinstance(b, dict) and "when" in b for b in raw["blocks"] if isinstance(raw["blocks"], list)):
            raise ProfileError(f"page {page_id}: only a Ring page's blocks say when they are shown")
        blocks_in = raw["blocks"]
        if isinstance(blocks_in, list):
            blocks_in = [{k: v for k, v in b.items() if k != "when"} if isinstance(b, dict) else b for b in blocks_in]
        flat = normalize_page({"id": page_id, "label": label, "blocks": blocks_in}, None, known_target=known_target,
                              strict=strict)
        page = {"id": page_id, "label": flat["label"], "kind": KIND_CONTENT, "blocks": flat["blocks"]}
    seen.add(page_id)
    return page


# ---- devices ----------------------------------------------------------------------------------------------------------

def _entry(raw: Any, where: str) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise ProfileError(f"{where}: a page entry must be an object")
    _only_keys(raw, ENTRY_KEYS, where)
    page = raw.get("page")
    if not isinstance(page, str) or not ID_RE.match(page):
        raise ProfileError(f"{where}: an entry names a library page")
    out = {"page": page}
    wire = raw.get("as")
    if wire is not None:
        if not isinstance(wire, str) or not ID_RE.match(wire):
            raise ProfileError(f"{where}: the wire id (as) must be 1-32 characters of a-z, 0-9, _ or -")
        if wire != page:
            out["as"] = wire
    return out


def normalize_device(key: str, raw: Any, by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """One device `{pages, ring, roster_hide}`, strict, against the library `by_id` (already normalised)."""
    where = f"device {key!r}"
    if not isinstance(raw, dict):
        raise ProfileError(f"{where}: must be an object")
    _only_keys(raw, DEVICE_KEYS, where)
    entries_in = raw.get("pages", [])
    if not isinstance(entries_in, list):
        raise ProfileError(f"{where}: pages must be a list")
    if len(entries_in) > MAX_PAGES:
        raise ProfileError(f"{where}: at most {MAX_PAGES} pages")
    entries: list[dict[str, str]] = []
    used: set[str] = set()
    wires: set[str] = set()
    for n, item in enumerate(entries_in):
        entry = _entry(item, f"{where} page {n + 1}")
        page = by_id.get(entry["page"])
        if page is None:
            raise ProfileError(f"{where}: there is no page {entry['page']!r}")
        if page_kind(page) != KIND_CONTENT:
            raise ProfileError(f"{where}: {entry['page']!r} is a Ring page; a device shows content pages")
        if entry["page"] in used:
            raise ProfileError(f"{where}: the page {entry['page']!r} is listed twice")
        wire = entry.get("as") or entry["page"]
        if wire in wires:
            raise ProfileError(f"{where}: two pages go by the id {wire!r}")
        used.add(entry["page"])
        wires.add(wire)
        entries.append(entry)
    if not isinstance(raw.get("ring", {}), dict):
        raise ProfileError(f"{where}: ring must be an object")       # a retired per-device map: tolerated, never read
    hide = raw.get("roster_hide", [])
    if (not isinstance(hide, list) or len(hide) > MAX_ROSTER
            or any(not isinstance(h, str) or not h or len(h) > MAX_ENDPOINT_ID for h in hide)):
        raise ProfileError(f"{where}: roster_hide must be a list of endpoint ids")
    return {"pages": entries, "ring": {}, "roster_hide": list(dict.fromkeys(hide))}


# ---- the document -----------------------------------------------------------------------------------------------------

def declares_pages_model(raw: Any) -> bool:
    """Whether a raw document asks for the new model: it says schema 6 or carries a library `pages` or a `devices` map."""
    return isinstance(raw, dict) and (raw.get("schema_version") == SCHEMA_PAGES or "pages" in raw or "devices" in raw)


def normalize_document_v6(raw: Any, *, known_target: Callable[[str, str], bool] | None = None,
                          app_screens: Collection[str] = ()) -> dict[str, Any]:
    """The whole schema 6 document, validated strictly (see the module docstring). Pure. Raises ProfileError naming the item.
    The result is exactly what `ContentStore._data()` holds in the new model, so a document that was not edited normalises to
    itself (that is how a no-op save is a no-op)."""
    if not isinstance(raw, dict):
        raise ProfileError("the document must be an object")
    _only_keys(raw, DOCUMENT_KEYS_V6, "document")
    if raw.get("schema_version", SCHEMA_PAGES) != SCHEMA_PAGES:
        raise ProfileError(f"document: schema_version must be {SCHEMA_PAGES} when it has pages or devices; reload the Portico page")
    dashboards = normalize_dashboards(raw.get("dashboards", {}))

    def target_known(target_type: str, target_id: str) -> bool:
        # a screen declared in this same document can be called in it, but never the app screen of a panel (DEV-807)
        return (target_type == "dashboard" and target_id in dashboards and target_id not in app_screens) or (
            known_target is None or known_target(target_type, target_id))

    pages_in = raw.get("pages", [])
    if not isinstance(pages_in, list):
        raise ProfileError("pages must be a list")
    if len(pages_in) > MAX_LIBRARY_PAGES:
        raise ProfileError(f"pages: at most {MAX_LIBRARY_PAGES} pages")
    seen: set[str] = set()
    pages = [normalize_library_page(p, seen, known_target=target_known) for p in pages_in]
    by_id = {p["id"]: p for p in pages}
    devices_in = raw.get("devices", {})
    if not isinstance(devices_in, dict):
        raise ProfileError("devices must be an object")
    devices = {}
    for key, device in devices_in.items():
        _endpoint_key(key, "devices")
        devices[key] = normalize_device(key, device, by_id)
    _one_ring_page_per_talk(pages)
    declarations = normalize_declarations(raw.get("endpoints", {}))
    out: dict[str, Any] = {"schema_version": SCHEMA_PAGES, "pages": pages, "devices": devices, "dashboards": dashboards,
                           "endpoints": declarations}
    roster = normalize_roster(raw.get("roster", [ROSTER_ALL]))
    if roster != [ROSTER_ALL]:
        out["roster"] = roster      # stored only when it is not "everyone": a document without it reads as everyone
    add_extras(raw, out)
    return out


def _one_ring_page_per_talk(pages: list[dict[str, Any]]) -> None:
    """Strict: two Ring pages for one Talk element are an error naming both. A restored pre-migration document may hold
    several: run `portico/content/ring_pages_migrate` on it first."""
    owner: dict[str, str] = {}
    for page in pages:
        if page.get("kind") != KIND_RING:
            continue
        other = owner.setdefault(page["talk"], page["id"])
        if other != page["id"]:
            raise ProfileError(f"pages {other!r} and {page['id']!r} are both the ring page of {page['talk']!r}; "
                               "a Talk element has one ring page (an old document: run the ring pages migration)")


def normalize_dashboards(dashboards_in: Any) -> dict[str, dict[str, Any]]:
    """The screens a document declares (shared by both models)."""
    from .content import MAX_LABEL

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
    return dashboards


def normalize_declarations(declarations_in: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(declarations_in, dict):
        raise ProfileError("endpoints must be an object")
    declarations: dict[str, dict[str, Any]] = {}
    for key, declaration in declarations_in.items():
        _endpoint_key(key, "endpoints")
        declarations[key] = normalize_endpoint_declaration(declaration)
    return declarations


def add_extras(raw: dict[str, Any], out: dict[str, Any]) -> None:
    """`home_name` and `show_fps` into `out` as the strict save stores them (shared by both models)."""
    home_name = normalize_home_name(raw.get("home_name"))
    if home_name:
        out["home_name"] = home_name
    if "show_fps" in raw:
        show_fps = raw["show_fps"]
        if not isinstance(show_fps, bool):
            raise ProfileError("show_fps must be true or false")
        if show_fps:
            out["show_fps"] = True      # stored only when true: absent is false, and a document without it is unchanged


def refuse_flat_pages_v6(raw: Any) -> None:
    """The v6 twin of `content.refuse_flat_pages`: a library page that carries flat `cameras`/`actions` (not a Ring page's own
    page-level `cameras`) is an editor from before blocks."""
    pages = raw.get("pages") if isinstance(raw, dict) else None
    for page in pages if isinstance(pages, list) else ():
        if isinstance(page, dict) and ("actions" in page or ("cameras" in page and page.get("kind") != KIND_RING)):
            raise ProfileError(f"page {page.get('id')!r} carries flat cameras/actions, which this version of Portico no "
                               "longer takes; update the Portico panel to edit it")


# ---- the lenient load -------------------------------------------------------------------------------------------------

def load_pages_model(data: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], list[str]]:
    """`(library, devices, roster)` from a stored schema 6 file, leniently: an invalid page is dropped with one warning; a device
    keeps every well-formed entry even when its page is gone (nothing is lost silently; the resolver skips what is not
    there). A device that is not an object is dropped with a warning."""
    pages: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in data.get("pages") if isinstance(data.get("pages"), list) else ():
        try:
            pages.append(normalize_library_page(raw, seen, strict=False))
        except ProfileError as err:
            _LOGGER.warning("portico content: dropped an invalid stored page (%s)", err)
    devices: dict[str, dict[str, Any]] = {}
    for key, raw in (data.get("devices").items() if isinstance(data.get("devices"), dict) else ()):
        if not isinstance(raw, dict):
            _LOGGER.warning("portico content: dropped an invalid stored device (not an object)")
            continue
        entries = []
        for item in raw.get("pages") if isinstance(raw.get("pages"), list) else ():
            try:
                entries.append(_entry(item, f"device {key!r}"))
            except ProfileError as err:
                _LOGGER.warning("portico content: dropped a stored page entry (%s)", err)
        ring_in = raw.get("ring")
        ring = {t: p for t, p in (ring_in.items() if isinstance(ring_in, dict) else ())
                if isinstance(t, str) and isinstance(p, str)}
        hide = raw.get("roster_hide")
        devices[key] = {"pages": entries, "ring": ring,
                        "roster_hide": [h for h in hide if isinstance(h, str)] if isinstance(hide, list) else []}
    if not any(d["ring"] for d in devices.values()):     # a surviving legacy `ring` map: keep every page for the migration to analyse
        first: dict[str, str] = {}
        kept = []
        for page in pages:
            if page.get("kind") == KIND_RING and first.setdefault(page["talk"], page["id"]) != page["id"]:
                _LOGGER.warning("portico content: ring page %r dropped, %r is already the ring page of %r",
                                page["id"], first[page["talk"]], page["talk"])
                continue
            kept.append(page)
        pages = kept
    try:
        roster = normalize_roster(data.get("roster", [ROSTER_ALL]))
    except ProfileError as err:
        _LOGGER.warning("portico content: the stored roster does not validate (%s); using everyone", err)
        roster = [ROSTER_ALL]
    return pages, devices, roster


# ---- ids a device's grant sees twice ----------------------------------------------------------------------------------

def duplicate_action_ids_v6(document: dict[str, Any]) -> set[tuple[str, str]]:
    """`{(device key, action id)}` for every action id that two DIFFERENT buttons share among the pages one device shows and the
    Ring pages (every stored one). `content/run` names a button by id alone, so a clash is ambiguous. The copies a migration makes
    (a Ring page's button is a copy of one on the device's content page, same id, same content) are not a clash."""
    found: set[tuple[str, str]] = set()
    for key in (document.get("devices") or {}):
        seen: dict[str, str] = {}
        for page in [*device_pages(document, key), *assigned_ring_pages(document, key)]:
            for block in page.get("blocks") or ():
                for item in block_actions(block) if isinstance(block, dict) else ():
                    if isinstance(item, dict) and isinstance(item.get("id"), str):
                        blob = json.dumps(item, sort_keys=True)
                        if seen.setdefault(item["id"], blob) != blob:
                            found.add((key, item["id"]))
    return found
