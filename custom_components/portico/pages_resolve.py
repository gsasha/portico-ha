"""The schema 6 document ("Pages unified", P-A): the pure accessors every resolver, grant and walker shares.

    {schema_version: 6, home_name?, show_fps?, roster?,
     pages:   [{id, label, kind: "content", blocks} | {id, label, kind: "ring", talk, cameras, blocks}],     # the one library
     devices: {<endpoint id>: {pages: [{page, as?}], ring: {<talk element id>: <ring page id>}, roster_hide}},
     dashboards, endpoints}

There is no `template` and no `overrides`: a page stores nothing about where it lands, and each device lists its own pages
and says which Ring page answers which Talk element (design `portico_pages_unify_design.md` 2 and 3.0).

This module reads nothing but the document dict: no Home Assistant, no store, no clock, no import of `content` or
`content_resolve` (both import it). A document of schema 4 or 5 is never a schema 6 document, so every function here is
inert for it and the legacy resolvers run exactly as before (the P-A promise: dormant until the migration is applied).
"""
from __future__ import annotations

import re
from typing import Any

SCHEMA_PAGES = 6
KIND_CONTENT = "content"
KIND_RING = "ring"
PAGE_KINDS = (KIND_CONTENT, KIND_RING)
WHEN_RING = "ring"          # a Ring page block shown only while the phone rings
WHEN_CALL = "call"          # ... only during the answered call; absent = both
WHENS = (WHEN_RING, WHEN_CALL)
ROSTER_ALL = "*"

# A Talk element id: a minted `el-xxxxxxxx`, or a managed one (`talk-phone-<enrolment id>`, `talk-screen-<slug>`; the Devices
# worker's managed Talk elements, home `portico_devices_design.md` 4.4).
TALK_ID_RE = re.compile(r"^(?:el-[a-z0-9-]{3,40}|talk-(?:screen|phone)-[A-Za-z0-9_-]{1,128})$")

# What a Talk element stands for: (party kind, party id). `door`: a door reference as a block's `door`; `phone`: a phone
# endpoint id; `screen`: a dashboard slug.
PARTY_DOOR, PARTY_PHONE, PARTY_SCREEN = "door", "phone", "screen"


def is_pages_model(document: Any) -> bool:
    """True for a schema 6 document (the new model); False for 4, 5, and anything that is not a document."""
    return isinstance(document, dict) and document.get("schema_version") == SCHEMA_PAGES


def is_talk_id(value: Any) -> bool:
    return isinstance(value, str) and TALK_ID_RE.match(value) is not None


def library(document: dict[str, Any]) -> list[dict[str, Any]]:
    pages = document.get("pages")
    return [p for p in pages if isinstance(p, dict)] if isinstance(pages, list) else []


def library_by_id(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {p["id"]: p for p in library(document) if isinstance(p.get("id"), str)}


def page_kind(page: dict[str, Any]) -> str:
    """A page's kind; absent means content (every page of schema 4/5 is already a valid content page)."""
    return KIND_RING if page.get("kind") == KIND_RING else KIND_CONTENT


def device_of(document: dict[str, Any], endpoint_id: str) -> dict[str, Any]:
    devices = document.get("devices")
    entry = devices.get(endpoint_id) if isinstance(devices, dict) else None
    return entry if isinstance(entry, dict) else {}


def wire_id(entry: dict[str, Any]) -> str:
    """The id a device's page goes by on the wire: `as` when set, else the library id."""
    return entry.get("as") or entry["page"]


def device_pages(document: dict[str, Any], endpoint_id: str) -> list[dict[str, Any]]:
    """A device's own content pages, in its own order (the first is Home), each emitted as `{id, label, blocks}` with the wire
    id. A reference to a page that is gone or is a Ring page is skipped (fail closed). This replaces `merged_pages` for a
    schema 6 document: there is no merging."""
    by_id = library_by_id(document)
    out: list[dict[str, Any]] = []
    for entry in device_of(document, endpoint_id).get("pages") or ():
        page = by_id.get(entry.get("page")) if isinstance(entry, dict) else None
        if page is None or page_kind(page) != KIND_CONTENT:
            continue
        out.append({"id": wire_id(entry), "label": page.get("label") or page["id"], "blocks": page.get("blocks", [])})
    return out


def roster_of(document: dict[str, Any]) -> list[str]:
    """The roster list: `template.roster` of a legacy document, the top-level `roster` of a schema 6 one (default everyone)."""
    if is_pages_model(document):
        roster = document.get("roster")
        return list(roster) if isinstance(roster, list) else [ROSTER_ALL]
    roster = (document.get("template") or {}).get("roster", [ROSTER_ALL])
    return roster


def roster_hide_of(document: dict[str, Any], endpoint_id: str) -> list[str]:
    if is_pages_model(document):
        return list(device_of(document, endpoint_id).get("roster_hide") or ())
    return list(((document.get("overrides") or {}).get(endpoint_id) or {}).get("roster_hide", []))


def party_matches(party: tuple[str, str] | None, caller: Any) -> bool:
    """Whether a Talk element's party is `caller` (an Endpoint): a door by the door rules (`Endpoint.names_door`), a phone or a
    screen by its endpoint id and kind."""
    if party is None or caller is None:
        return False
    kind, ref = party
    if kind == PARTY_DOOR:
        return bool(caller.names_door(ref))
    if kind == PARTY_PHONE:
        return caller.kind == "phone" and caller.id == ref
    if kind == PARTY_SCREEN:
        return caller.kind == "dashboard" and caller.id == ref
    return False


def talk_parties(registry: Any) -> dict[str, tuple[str, str]]:
    """`{talk element id: (party kind, party id)}` from the registry (`ContentStore.talk_parties`). A registry that has none (a
    test double, an old build) answers {}: no Talk element stands for anybody, so no Ring page matches and the built-in
    default rings (fail closed to the default, never to a stale page)."""
    ask = getattr(registry, "talk_parties", None)
    if not callable(ask):
        return {}
    try:
        found = ask()
    except Exception:  # noqa: BLE001 - a registry bug must not take content/get down
        return {}
    return found if isinstance(found, dict) else {}


def ring_page_of_talk(document: dict[str, Any], registry: Any, caller: Any) -> dict[str, Any] | None:
    """The one stored Ring page that rings for `caller` in the per-Talk form: among the Ring pages whose `talk` stands for the
    caller, the one of the lowest Talk element id (Q6: two Talk elements for one door). None = the built-in default rings."""
    if caller is None:
        return None
    parties = talk_parties(registry)
    found = [p for p in library(document) if page_kind(p) == KIND_RING and party_matches(parties.get(p.get("talk")), caller)]
    return min(found, key=lambda p: str(p.get("talk"))) if found else None


def assigned_ring_page(document: dict[str, Any], callee_id: str, caller: Any, registry: Any) -> dict[str, Any] | None:
    """The Ring page that rings `callee_id` for a call from `caller`, or None (then the built-in default rings): the stored
    Ring page of the Talk element standing for the caller, the same for every callee (`ring_page_of_talk`). A stale
    `devices[*].ring` map in a restored old document is not read (design `portico_ring_pages_per_talk_design.md`, R-D)."""
    return ring_page_of_talk(document, registry, caller)


def assigned_ring_pages(document: dict[str, Any], endpoint_id: str, registry: Any = None) -> list[dict[str, Any]]:
    """The Ring pages whose buttons and camera `endpoint_id` may run (the `content/run` and `content/snapshot` grant's source):
    every stored Ring page, to every endpoint (Q3), since the resolved document already shows each endpoint every caller's
    ring buttons; when a `registry` is given a page whose Talk element stands for nobody (an orphan, never ringing) is not
    granted (fail closed)."""
    parties = talk_parties(registry) if registry is not None else None
    return [p for p in library(document)
            if page_kind(p) == KIND_RING and (parties is None or p.get("talk") in parties)]


def all_pages(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Every stored page of the document, whatever the model: the library, or the template's and every override's."""
    if is_pages_model(document):
        return library(document)
    out: list[dict[str, Any]] = []
    template = document.get("template")
    if isinstance(template, dict):
        out.extend(p for p in (template.get("pages") or ()) if isinstance(p, dict))
    for override in (document.get("overrides") or {}).values():
        if isinstance(override, dict):
            out.extend(p for p in (override.get("pages") or ()) if isinstance(p, dict))
    return out


def parties_of(elements: dict[str, Any]) -> dict[str, tuple[str, str]]:
    """`{talk element id: (party kind, party id)}` from `{element id: stored element}`: a Talk element names exactly one of
    `options.door`, `options.phone`, `options.screen` (the Devices worker's managed ones carry the last two). Pure."""
    out: dict[str, tuple[str, str]] = {}
    for element_id, element in elements.items():
        if not isinstance(element, dict) or element.get("kind") != "talk":
            continue
        options = element.get("options") or {}
        for kind in (PARTY_DOOR, PARTY_PHONE, PARTY_SCREEN):
            if isinstance(options.get(kind), str) and options[kind]:
                out[element_id] = (kind, options[kind])
                break
    return out
