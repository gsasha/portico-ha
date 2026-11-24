"""The ring-pages migration: from a Ring page assigned per device (`devices[*].ring`) to one Ring page per Talk element
(design `portico_ring_pages_per_talk_design.md` 5, the owner's D-20261122-4aec; Q5 decided as recommended, D-20261122-5b24).

- `analyse(document, parties)`: per Talk element that has a stored Ring page, the case of design 5.1 (`consistent`, `mixed`,
  `conflict`, `orphan`), the devices that assign each page and the pages that would be dropped. Pure.
- `migrate_document(document, choices)`: the document with every `devices[*].ring` map emptied and the losing pages of each
  conflict deleted. Pure.
- `differences(before, after, endpoints, parties)`: every (callee, caller) pair whose Ring page result (cameras, ring and call
  button ids) differs between the old and the new resolver. Listed, never refused: the mixed case is expected to differ.
- `run_ring_migration` and the admin command `portico/content/ring_pages_migrate`: dry run (the default), then apply with
  `choices` `{<talk element id>: <ring page id>}` for every conflict. The apply refuses until each conflict has a chosen page,
  leaves the one-time copy `.storage/portico.content.v6-before-ring-per-talk` (never overwritten, and no copy refuses the
  write), then makes exactly ONE `async_save_document` write. The Elements store is not written.
"""
from __future__ import annotations

import copy
import logging
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store

from . import auth
from .content import STORE_KEY, ProfileError, SaveConflict, get_content_store
from .content_resolve import default_buttons, endpoint_table, ring_call_actions
from .endpoints import KIND_DOOR, Endpoint
from .pages_migrate import MigrationRefused
from .pages_resolve import KIND_RING, assigned_ring_page, device_of, device_pages, is_pages_model, library, library_by_id, page_kind, party_matches, talk_parties

_LOGGER = logging.getLogger(__name__)

BACKUP_SUFFIX = "v6-before-ring-per-talk"

CASE_CONSISTENT, CASE_MIXED, CASE_CONFLICT, CASE_ORPHAN = "consistent", "mixed", "conflict", "orphan"


def analyse(document: dict[str, Any], parties: dict[str, tuple[str, str]]) -> list[dict[str, Any]]:
    """One entry per Talk element that has at least one stored Ring page, in library order of its first page:
    `{talk, case, pages: [{id, label, devices: [<device key>], content}], unassigned_devices: [..]}`. `devices` are the devices
    whose `ring[talk]` names the page. Talk elements with no stored page need nothing and are not listed."""
    devices = document.get("devices") if isinstance(document.get("devices"), dict) else {}
    by_talk: dict[str, list[dict[str, Any]]] = {}
    for page in library(document):
        if page_kind(page) == KIND_RING:
            by_talk.setdefault(page.get("talk"), []).append(page)
    out: list[dict[str, Any]] = []
    for talk, pages in by_talk.items():
        entry_pages = []
        assigned: set[str] = set()
        for page in pages:
            users = sorted(k for k, d in devices.items()
                           if isinstance(d, dict) and isinstance(d.get("ring"), dict) and d["ring"].get(talk) == page["id"])
            assigned.update(users)
            entry_pages.append({"id": page["id"], "label": page.get("label"), "devices": users, "content": copy.deepcopy(page)})
        unassigned = sorted(k for k in devices if k not in assigned)
        if len(pages) > 1:
            case = CASE_CONFLICT
        elif talk not in parties:
            case = CASE_ORPHAN
        elif unassigned:
            case = CASE_MIXED
        else:
            case = CASE_CONSISTENT
        out.append({"talk": talk, "case": case, "pages": entry_pages, "unassigned_devices": unassigned})
    return out


def _choice_errors(cases: list[dict[str, Any]], choices: dict[str, str]) -> list[str]:
    errors = []
    conflicts = {c["talk"]: c for c in cases if c["case"] == CASE_CONFLICT}
    for talk, case in conflicts.items():
        ids = [p["id"] for p in case["pages"]]
        if talk not in choices:
            errors.append(f"{talk}: choose one of {', '.join(ids)}")
        elif choices[talk] not in ids:
            errors.append(f"{talk}: {choices[talk]!r} is not one of its ring pages ({', '.join(ids)})")
    for talk in choices:
        if talk not in conflicts:
            errors.append(f"{talk}: has no conflict, nothing to choose")
    return errors


def migrate_document(document: dict[str, Any], choices: dict[str, str],
                     cases: list[dict[str, Any]] | None = None, *, provisional: bool = False) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """`(new document, dropped pages)`. Every device's `ring` becomes `{}`; for a conflict the page named in `choices` stays and
    the others are deleted (returned whole). With `provisional` a conflict without a choice keeps its first page (the dry run's
    stand-in so the differences can be listed); otherwise a missing choice is a ProfileError."""
    cases = analyse(document, {}) if cases is None else cases
    dropped_ids: set[str] = set()
    for case in cases:
        if case["case"] != CASE_CONFLICT:
            continue
        ids = [p["id"] for p in case["pages"]]
        keep = choices.get(case["talk"])
        if keep is None and provisional:
            keep = ids[0]
        if keep not in ids:
            raise ProfileError(f"{case['talk']}: choose one of its ring pages ({', '.join(ids)})")
        dropped_ids.update(i for i in ids if i != keep)
    new = copy.deepcopy(document)
    dropped = [p for p in new.get("pages") or () if p.get("id") in dropped_ids]
    new["pages"] = [p for p in new.get("pages") or () if p.get("id") not in dropped_ids]
    for device in (new.get("devices") or {}).values():
        if isinstance(device, dict):
            device["ring"] = {}
    return new, dropped


def has_legacy_ring(document: dict[str, Any]) -> bool:
    """True while any device carries a non-empty `devices[*].ring` map (the retired per-device form, kept here only so a restored
    pre-migration document can be analysed)."""
    devices = document.get("devices")
    return isinstance(devices, dict) and any(isinstance(d, dict) and d.get("ring") for d in devices.values())


def _legacy_ring_page(document: dict[str, Any], callee_id: str, caller: Any, registry: Any) -> dict[str, Any] | None:
    """The retired resolver, for the dry run's "before" side only: the page `callee_id` assigned to the Talk element standing
    for `caller` (a Ring page of that same Talk element; the first assignment in its map wins)."""
    ring = device_of(document, callee_id).get("ring")
    if caller is None or not isinstance(ring, dict) or not ring:
        return None
    parties = talk_parties(registry)
    by_id = library_by_id(document)
    for talk_id, page_id in ring.items():
        if not party_matches(parties.get(talk_id), caller):
            continue
        page = by_id.get(page_id)
        if page is not None and page_kind(page) == KIND_RING and page.get("talk") == talk_id:
            return page
    return None


def _effective(document: dict[str, Any], callee: str, caller: Endpoint, registry: Any) -> dict[str, Any]:
    page = (_legacy_ring_page if has_legacy_ring(document) else assigned_ring_page)(document, callee, caller, registry)
    if page is None:
        pages = device_pages(document, callee)
        buttons = [a["id"] for a in default_buttons(pages, caller)] if caller.kind == KIND_DOOR else []
        return {"page": None, "cameras": list(caller.cameras), "ring_buttons": buttons, "call_buttons": list(buttons)}
    ring, call = ring_call_actions(page)
    cameras = list(caller.cameras) if page.get("cameras") is None else list(page["cameras"])
    return {"page": page["id"], "cameras": cameras, "ring_buttons": [a["id"] for a in ring],
            "call_buttons": [a["id"] for a in call]}


def differences(before: dict[str, Any], after: dict[str, Any], endpoints: list[Endpoint],
                parties: dict[str, tuple[str, str]], expand: Callable[[dict[str, Any]], dict[str, Any]] | None = None
                ) -> list[dict[str, Any]]:
    """Every (callee, caller) whose ring result differs between the old resolver on `before` and the new one on `after`.
    The real resolver reads the EXPANDED document (element references replaced by the actions they stand for: `ContentStore.
    _expanded`), and a stored action may be a bare reference with no `kind`; `expand` (`ContentStore.expand`) gives both sides
    that same view. Without it the documents are read as they are (a document with no reference)."""
    if expand is not None:
        before, after = expand(before), expand(after)
    registry = SimpleNamespace(talk_parties=lambda: parties)
    out = []
    for callee in sorted((before.get("devices") or {})):
        for caller in endpoints:
            if caller.id == callee:
                continue
            was, now = _effective(before, callee, caller, registry), _effective(after, callee, caller, registry)
            if {k: v for k, v in was.items() if k != "page"} != {k: v for k, v in now.items() if k != "page"}:
                out.append({"callee": callee, "caller": caller.id, "caller_name": caller.name, "before": was, "after": now})
    return out


def build_report(document: dict[str, Any], parties: dict[str, tuple[str, str]], endpoints: list[Endpoint],
                 choices: dict[str, str], rev: str,
                 expand: Callable[[dict[str, Any]], dict[str, Any]] | None = None) -> dict[str, Any]:
    cases = analyse(document, parties)
    errors = _choice_errors(cases, choices)
    conflicts = [c for c in cases if c["case"] == CASE_CONFLICT]
    unresolved = [c["talk"] for c in conflicts if c["talk"] not in choices]
    valid = {t: c for t, c in choices.items()
             if any(case["talk"] == t and case["case"] == CASE_CONFLICT and c in [p["id"] for p in case["pages"]] for case in cases)}
    after, dropped = migrate_document(document, valid, cases, provisional=True)     # a bad choice is in `problems`, not applied
    return {
        "doc_rev": rev,
        "nothing_to_do": not has_legacy_ring(document) and not any(c["case"] == CASE_CONFLICT for c in cases),
        "can_apply": not errors,
        "problems": errors,
        "talk_elements": cases,
        "conflicts": [{"talk": c["talk"], "pages": c["pages"], "chosen": choices.get(c["talk"])} for c in conflicts],
        "unresolved_conflicts": unresolved,
        "dropped_pages": dropped,
        "differences": differences(document, after, endpoints, parties, expand),
        "differences_note": ("an unresolved conflict is shown with its first page standing in" if unresolved else ""),
        "counts": {"talk_elements": len(cases), "conflicts": len(conflicts), "dropped_pages": len(dropped),
                   **{f"case_{k}": sum(1 for c in cases if c["case"] == k)
                      for k in (CASE_CONSISTENT, CASE_MIXED, CASE_CONFLICT, CASE_ORPHAN)}},
    }


async def keep_backup(hass: HomeAssistant, stored: dict[str, Any]) -> bool:
    """Leave `stored` (the document as read) as `.storage/portico.content.v6-before-ring-per-talk`; never overwritten once
    present (False then). A copy that cannot be written refuses the apply: ProfileError, nothing changed."""
    try:
        backup: Store[dict] = Store(hass, 1, f"{STORE_KEY}.{BACKUP_SUFFIX}", atomic_writes=True)
        if await backup.async_load() is not None:
            return False
        await backup.async_save(copy.deepcopy(stored))
        _LOGGER.info("portico content: kept a copy of the file as %s.%s before the ring-pages migration", STORE_KEY, BACKUP_SUFFIX)
        return True
    except Exception as err:  # noqa: BLE001 - re-raised as the refusal it is
        _LOGGER.warning("portico content: could not keep the pre-ring-migration copy (%s); not migrating", err)
        raise ProfileError("could not keep a copy of the current content file; nothing was changed") from err


async def run_ring_migration(store: Any, hass: HomeAssistant, *, apply: bool, base_doc_rev: str | None,
                             choices: dict[str, str]) -> dict[str, Any]:
    await store.async_ensure_loaded()
    if not store.uses_pages_model:
        raise MigrationRefused("not_pages_model", "the document is not the unified Pages model (schema 6) yet; run the Pages migration first")
    document = store._data()
    parties = store.talk_parties()
    endpoints = endpoint_table(store._expanded(), store)
    report = build_report(document, parties, endpoints, choices, store.doc_rev, store.expand)
    if not apply:
        return report
    if report["nothing_to_do"]:
        return {**report, "applied": False}
    store.check_writable()
    if base_doc_rev != store.doc_rev:
        raise SaveConflict("the document changed since the dry run")
    if report["problems"]:
        raise MigrationRefused("conflict_unresolved", "; ".join(report["problems"][:3]), report)
    new, _dropped = migrate_document(document, choices, provisional=False)
    store.prepare_document(new)                       # strict validation first: a refusal leaves no copy behind
    on_disk = await Store(hass, 1, STORE_KEY, atomic_writes=True).async_load()      # the file exactly as it is now
    backed_up = await keep_backup(hass, on_disk if isinstance(on_disk, dict) else store._disk_data())
    new_rev = await store.async_save_document(new, base_doc_rev)
    return {**report, "applied": True, "doc_rev": new_rev, "backup_written": backed_up,
            "backup": f"{STORE_KEY}.{BACKUP_SUFFIX}"}


_REV = vol.All(str, vol.Length(min=1, max=64))


@websocket_api.websocket_command({
    vol.Required("type"): "portico/content/ring_pages_migrate",
    vol.Optional("dry_run", default=True): bool,
    vol.Optional("base_doc_rev"): _REV,
    vol.Optional("choices", default={}): {vol.All(str, vol.Length(min=1, max=64)): vol.All(str, vol.Length(min=1, max=64))},
})
@websocket_api.async_response
async def handle_ring_pages_migrate(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """The ring-pages migration (admin). `dry_run: true` (the default) writes nothing and returns the report; `dry_run: false`
    applies it, needs the `base_doc_rev` the dry run returned as `doc_rev` and a `choices` entry per conflict. Errors:
    `conflict_unresolved`, `not_pages_model`, `conflict` (the document moved), `invalid`."""
    if not auth.require(connection, msg, "portico/content/ring_pages_migrate"):
        return
    apply = not msg["dry_run"]
    if apply and not msg.get("base_doc_rev"):
        connection.send_error(msg["id"], "invalid", "an apply needs the doc_rev the dry run returned (base_doc_rev)")
        return
    try:
        report = await run_ring_migration(get_content_store(hass), hass, apply=apply, base_doc_rev=msg.get("base_doc_rev"),
                                          choices=msg["choices"])
    except SaveConflict as err:
        connection.send_error(msg["id"], "conflict", str(err))
        return
    except MigrationRefused as err:
        connection.send_error(msg["id"], err.code, str(err))
        return
    except ProfileError as err:
        connection.send_error(msg["id"], "invalid", str(err))
        return
    connection.send_result(msg["id"], report)


@callback
def async_setup_ring_pages_migrate(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, handle_ring_pages_migrate)
