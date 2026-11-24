"""Which screen owns which Talk element, and which screens a call wakes (Devices D2, "Pages unified" P-C).

Pure: it reads the stored schema 6 document (Talk elements still as references), the Talk parties
(`pages_resolve.parties_of`) and nothing else. No Home Assistant, no store.

    contains(S) = { door Talk elements referenced on the content pages in S's OWN pages list }
                + { the Talk element(s) whose target is S }                                (design 4.2, option A)

A page stores nothing about where it lands, so a Talk element is "on a screen" exactly when a page in that screen's own
`devices[S].pages` holds it. A door Talk element may be contained by any number of screens (D-20261122-42b1, which
superseded the old one-screen rule): a call for it wakes every one of them. A screen-target element has one owner by construction (its target) and a phone-target element has none.
"""
from __future__ import annotations

from typing import Any

from .pages_resolve import (
    KIND_CONTENT, PARTY_DOOR, PARTY_SCREEN, device_of, is_pages_model, library_by_id, page_kind,
)


def _talk_ids_on_page(page: dict[str, Any], parties: dict[str, tuple[str, str]]) -> list[str]:
    out: list[str] = []
    for block in page.get("blocks") or ():
        if not isinstance(block, dict):
            continue
        for item in block.get("actions") or ():
            element = item.get("element") if isinstance(item, dict) else None
            if isinstance(element, str) and element in parties and element not in out:
                out.append(element)
    return out


def owner_ids(document: dict[str, Any], panels: dict[str, str] | None = None) -> list[str]:
    """The device keys that can own a door Talk element: every screen declared in `document["dashboards"]`, except a FORMER
    panel screen (its `ha_user_id` is a key of `panels`, `{HA user id: phone endpoint id}`), plus every panel's phone endpoint
    id. A device key that is no screen and no panel (a plain phone, a stale one) owns nothing."""
    panels = panels or {}
    out = [slug for slug, decl in (document.get("dashboards") or {}).items()
           if not (isinstance(decl, dict) and decl.get("ha_user_id") in panels)]
    out.extend(phone_id for phone_id in panels.values() if phone_id not in out)
    return out


def door_owners(document: dict[str, Any], parties: dict[str, tuple[str, str]],
                panels: dict[str, str] | None = None) -> dict[str, list[str]]:
    """`{door Talk element id: [owner keys that hold it on their own pages]}`, keys sorted. The owners are `owner_ids`: the
    declared screens (bar a former panel screen) and the panel phones; a plain phone or a stale device key owns nothing."""
    if not is_pages_model(document):
        return {}
    by_id = library_by_id(document)
    owners: dict[str, set[str]] = {}
    for slug in owner_ids(document, panels):
        for entry in device_of(document, slug).get("pages") or ():
            page = by_id.get(entry.get("page")) if isinstance(entry, dict) else None
            if page is None or page_kind(page) != KIND_CONTENT:
                continue
            for element in _talk_ids_on_page(page, parties):
                if parties[element][0] == PARTY_DOOR:
                    owners.setdefault(element, set()).add(slug)
    return {element: sorted(slugs) for element, slugs in owners.items()}


def wake_map(document: dict[str, Any], parties: dict[str, tuple[str, str]], panels: dict[str, str] | None = None) -> dict[str, dict[str, Any]]:
    """For every declared screen: `{doors: [door ref...], elements: [talk id...], screen_call: True}`.

    `doors`/`elements` are the door Talk elements on the screen's own pages (a shared one wakes every screen that holds it). `screen_call` is true because a screen contains the Talk element that targets it (a house call to it always wakes
    it); `self_elements` lists those elements. Sorted, stable."""
    if not is_pages_model(document):
        return {}
    owners = door_owners(document, parties, panels)
    out: dict[str, dict[str, Any]] = {}
    for slug in [*(document.get("dashboards") or {}), *(i for i in (panels or {}).values() if i not in (document.get("dashboards") or {}))]:
        elements = sorted(e for e, slugs in owners.items() if slug in slugs)
        out[slug] = {
            "doors": sorted({parties[e][1] for e in elements}),
            "elements": elements,
            "self_elements": sorted(e for e, (kind, ref) in parties.items() if kind == PARTY_SCREEN and ref == slug),
            "screen_call": True,
        }
    return out


def wake_for_element(document: dict[str, Any], parties: dict[str, tuple[str, str]], element_id: str,
                     panels: dict[str, str] | None = None) -> list[str]:
    """The screen endpoint ids a call for the Talk element `element_id` wakes: every screen that holds it (a door element),
    or the screen it targets (a screen element); a phone element wakes nobody."""
    party = parties.get(element_id)
    if party is None or not is_pages_model(document):
        return []
    if party[0] == PARTY_SCREEN:
        return [party[1]] if party[1] in (document.get("dashboards") or {}) else []
    owners = door_owners(document, parties, panels).get(element_id, [])
    return list(owners)


def screens_for_door(document: dict[str, Any], parties: dict[str, tuple[str, str]], door_id: str,
                     panels: dict[str, str] | None = None) -> list[str]:
    """The screens a call at the door `door_id` wakes: every screen with a door Talk element for it. None = phones only."""
    return sorted(slug for slug, row in wake_map(document, parties, panels).items() if door_id in row["doors"])


def device_users_of_page(document: dict[str, Any], page_id: str) -> list[str]:
    """The device keys that show the library page `page_id` (their pages list), in key order. A Ring page belongs to its Talk
    element, not to a device."""
    out: list[str] = []
    for key, device in sorted((document.get("devices") or {}).items()):
        if not isinstance(device, dict):
            continue
        shown = any(isinstance(e, dict) and e.get("page") == page_id for e in device.get("pages") or ())
        if shown:
            out.append(key)
    return out
