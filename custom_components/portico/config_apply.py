"""`portico.reload_config`: apply the YAML tree under `/config/portico/` to the live configuration, all or nothing (DEV-824 P2;
design `portico_config_yaml_design.md` section 5.3 and 12, amended text).

The steps, in order, and what each may do:

1. READ the tree and run P1's pure validation (`config_yaml.validate_files`: structure, the editor's own field rules, secrets).
2. CHECK against the live house, still writing nothing: the whole document through the editor's `ContentStore.prepare_document`
   (call targets, doors and phones, screens and their HA users, button ids, element references) but against the elements the
   files WOULD leave; each element's door / phone / screen target and entity, name uniqueness and the element limit; each setting's
   own rule (a `test_phones` entry must be a registered phone). Every refusal is `file:line:col`.
3. REPORT what would change (`effect_report`): elements and pages added, changed and removed; per device the resolved `rev` before
   and after (`content_resolve.resolve_endpoint`, as the ring-pages migration proves equality); who is woken per door; deletions;
   devices left with no page; and a `sensitive` list (ring on or off, `test_phones`, the live RTSP base, house calls, a retargeted
   element) that a person or a pipeline should read before it stays applied.
4. APPLY, unless `dry_run`: under the element store's lock (every element write waits), re-check the revisions, swap the elements and
   the content document in memory in one synchronous step, write both `.storage` files, then the settings options, then announce
   `content_changed` so every app refetches. A failed write puts memory back and raises; nothing is half applied. No HA restart.

P6: the door and phone entry options in `doors/` and `phones/` are checked by `config_options.check` and written in the same synchronous
step as the content swap (undone if a later write fails). While a call is live a tree that changes any of them is DEFERRED whole
(`deferred: true`, nothing applied, not adopted); a `dial_enabled` change reloads that one door entry after the lock is released.

THE APPLY NEVER FIRES `portico_config_changed`. That event is the editor's save announcing "the files should be rewritten" (P3);
firing it from here would loop (apply, rewrite, event, ...). P3's `config_sync.py` is the save path that fires it; this module only calls
`config_sync.record_applied` (hash state, no file, no event).

Preserved, never taken from the files: the managed Talk elements (`talk-screen-*`, `talk-phone-*`, made by the server) and an
element of a kind this build does not know. A setting the files do not mention is left alone, except the live-video keys, which
`to_files` omits while unset: an absent live key clears it.
"""
from __future__ import annotations

import copy
import logging
import re
from datetime import datetime, timezone
from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from . import config_options, config_sync, config_yaml, ui_settings
from .config_yaml import ConfigError
from .content import ProfileError, SaveConflict, get_content_store
from .content_elements import expand_document, stored_ref_keys
from .content_resolve import endpoint_table, resolve_endpoint
from .elements import KIND_SPECS, MAX_ELEMENTS, ElementError, get_elements_store, name_key
from .endpoints import KIND_DOOR, get_endpoint_registry, panel_users
from .pages_migrate import _differences
from .pages_ownership import screens_for_door
from .pages_resolve import is_pages_model, parties_of
from .phone_entry import may_open, phone_rows
from .phones_commands import push_entry
from .provider import DOMAIN
from .push import get_manager

_LOGGER = logging.getLogger(__name__)

# P3 attaches here: the editor's save path fires `portico_config_changed` after it rewrites the files. The apply must not.
HOOK_POINT = "portico_config_changed"


class ApplyRefused(Exception):
    """The apply cannot proceed for a reason that is not one file's: `code`, and the errors (possibly empty)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _where(error: ConfigError) -> dict[str, Any]:
    return {**error.as_dict(), "at": f"{error.file}:{error.line}:{error.col}"}


def _preserved(element: dict[str, Any]) -> bool:
    """An element the files never carry: a managed Talk element, or a kind this build does not know."""
    return element.get("managed") is True or element.get("kind") not in KIND_SPECS


def _entity_exists(hass: HomeAssistant, entity_id: str) -> bool:
    return hass.states.get(entity_id) is not None or er.async_get(hass).async_get(entity_id) is not None


def _doc_entities(document: dict[str, Any]) -> set[str]:
    """Every entity id a document names directly (buttons, cameras of a block, a ring page, a screen or a phone declaration)."""
    found: set[str] = set()

    def walk(value: Any, key: str | None = None) -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                walk(v, k)
        elif isinstance(value, list):
            for v in value:
                walk(v, key)
        elif isinstance(value, str) and key in ("entity_id", "cameras") and "." in value and " " not in value:
            found.add(value)

    walk(document)
    return found


def blame(result: config_yaml.Validation, files: dict[str, str], message: str) -> tuple[str, int, int]:
    """The file, line and column a document refusal is about. P1's `_owner_file` finds a page, device or screen the message names;
    the editor's messages for a block or a button name `block 'b'` or `button 'x'` instead, so the pages are searched for that id
    and the line of its `id:` is the position."""
    path, line, col = config_yaml._owner_file(result.loaded, message)
    if path != config_yaml.ROOT_FILE:
        return path, line, col
    for ident in re.findall(r"(?:block|button|action|camera|page) '([^']+)'", message):
        needle = re.compile(rf"^(\s*)(?:- )?id: ['\"]?{re.escape(ident)}['\"]?\s*$")
        for page in sorted(p for p in files if p.startswith("pages/")):
            for number, text in enumerate(files[page].splitlines(), 1):
                hit = needle.match(text)
                if hit:
                    return page, number, len(hit.group(1)) + 1
    return path, line, col


# ---- the live checks ------------------------------------------------------------------------------------------------------

def _file_of_element(element_id: Any) -> str:
    return config_yaml.file_name("elements", element_id) if isinstance(element_id, str) else "elements"


def check_elements(hass: HomeAssistant, result: config_yaml.Validation, errors: list[ConfigError]) -> list[dict[str, Any]]:
    """The elements the files would leave, in order (declared ones in file order, then the preserved ones as stored), or errors.
    Applies the editor's per-element checks that need the house: kind may not change, a Talk door / phone / screen must exist
    (and a door is stored as the door's own id), an entity of a new or changed element must exist, names are unique, at most
    MAX_ELEMENTS. Synchronous."""
    store = get_elements_store(hass)
    stored = store.stored()
    old_by_id = {e["id"]: e for e in stored if isinstance(e.get("id"), str)}
    kept = [e for e in stored if _preserved(e)]
    kept_ids = {e["id"] for e in kept}
    taken = {name_key(e["name"]): e["id"] for e in kept if isinstance(e.get("name"), str)}
    declared: list[dict[str, Any]] = []
    for clean in copy.deepcopy(result.elements or []):
        path = _file_of_element(clean["id"])

        def refuse(field: str | None, message: str, code: str) -> None:
            line, col = result.loaded.lines.get(path, {}).get(field.split(".")[0] if field else "id", (1, 1))
            errors.append(ConfigError(path, line, col, message, code))

        old = old_by_id.get(clean["id"])
        if clean["id"] in kept_ids:
            refuse("id", "that id belongs to an element the server keeps (managed or of a newer kind)", "id_reserved")
            continue
        if old is not None and old.get("kind") != clean["kind"]:
            refuse("kind", "an element's kind cannot change; give the new element another id", "kind_locked")
            continue
        try:
            store._check_talk_door(clean, old)           # a new or changed door must exist; stored as the door's own id
            store._check_talk_endpoint(clean, old)       # a new or changed phone or screen must exist
        except ElementError as err:
            refuse(err.field, f"{err.field or 'element'}: {err}", err.code)
            continue
        entity = clean.get("entity_id")
        if entity and (old is None or old.get("entity_id") != entity) and not _entity_exists(hass, entity):
            refuse("entity_id", f"{entity}: there is no such entity in Home Assistant", "unknown_entity")
            continue
        key = name_key(clean["name"])
        if key in taken:
            refuse("name", f"the name {clean['name']!r} is already used by {taken[key]}", "name_taken")
            continue
        taken[key] = clean["id"]
        declared.append(clean)
    if len(declared) + len(kept) > MAX_ELEMENTS:
        errors.append(ConfigError("index.yaml", 1, 1, f"at most {MAX_ELEMENTS} elements", "too_many"))
    return declared + kept


def check_settings(hass: HomeAssistant, settings: dict[str, Any], errors: list[ConfigError]) -> dict[str, Any]:
    """The patch the files make: the keys they give, plus a cleared live key for each live key they leave out. Errors for a
    `test_phones` entry that is not a registered phone (the same refusal as `portico/ui_settings/set`)."""
    patch = dict(settings)
    for key in ui_settings.LIVE_KEYS:
        patch.setdefault(key, ui_settings._UNSET[key])
    if push_entry(hass) is None:
        if settings:
            errors.append(ConfigError(config_yaml.ROOT_FILE, 1, 1, "Portico's settings entry is missing, so the settings have nowhere "
                                      "to be kept", "no_push_entry"))
        return {}
    wanted = patch.get("test_phones")
    if wanted is not None and wanted != list(ui_settings.facts(hass).get("test_phones", [])):
        manager = get_manager(hass)
        if manager is None:
            errors.append(ConfigError(config_yaml.ROOT_FILE, 1, 1, "the push ring notification is not running, so "
                                      "test_phones cannot be checked", "push_not_loaded"))
        else:
            for reg_id in wanted:
                if manager.registry.get(reg_id) is None:
                    errors.append(ConfigError(config_yaml.ROOT_FILE, 1, 1, f"ring.test_phones: {reg_id!r} is not a registered phone",
                                              "unknown_phone"))
    return patch


# ---- the effect report ----------------------------------------------------------------------------------------------------

class _NewWorld:
    """The registry the NEW configuration is resolved against: the live store for everything but who each Talk element stands for,
    which is read from the elements the files would leave (as `pages_migrate._Overlay` does for its dry run)."""

    def __init__(self, base: Any, elements: dict[str, Any]) -> None:
        self._base = base
        self._elements = elements

    def talk_parties(self) -> dict[str, tuple[str, str]]:
        return parties_of(self._elements)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)


def _by_id(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {i["id"]: i for i in items if isinstance(i.get("id"), str)}


def _diff(old: dict[str, Any], new: dict[str, Any], label: str = "name") -> dict[str, list[Any]]:
    def row(key: str, item: dict[str, Any]) -> dict[str, Any]:
        out = {"id": key}
        if isinstance(item, dict) and item.get(label):
            out[label] = item[label]
        return out

    return {"added": [row(k, new[k]) for k in new if k not in old],
            "changed": [row(k, new[k]) for k in new if k in old and old[k] != new[k]],
            "removed": [row(k, old[k]) for k in old if k not in new]}


def _door_rings(hass: HomeAssistant, document: dict[str, Any], elements: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Per door (its endpoint id): the screens a call at it wakes, and the phones enrolled for it."""
    registry = get_endpoint_registry(hass)
    phones = [phone for _entry, phone in phone_rows(hass)]
    parties = parties_of(elements)
    panels = panel_users(hass)
    out: dict[str, dict[str, Any]] = {}
    for door in registry.endpoints(kind=KIND_DOOR):
        screens = screens_for_door(document, parties, door.device_id or door.id, panels) if is_pages_model(document) else []
        out[door.id] = {"name": door.name, "screens": screens,
                        "phones": sorted(str(p.get("name") or "phone") for p in phones if may_open(p, door.device_id or door.id))}
    return out


def effect_report(hass: HomeAssistant, *, old_document: dict[str, Any], new_document: dict[str, Any],
                  old_elements: list[dict[str, Any]], new_elements: list[dict[str, Any]], settings_before: dict[str, Any],
                  patch: dict[str, Any], changes: list[str], dropped_orphans: list[str]) -> dict[str, Any]:
    """What applying would change. Pure over its arguments plus the live registries; writes nothing. Never carries a secret (the files
    hold none): ids, names and the settings values a person must see."""
    store = get_content_store(hass)
    old_by, new_by = _by_id(old_elements), _by_id(new_elements)
    declared_old = {k: v for k, v in old_by.items() if not _preserved(v)}
    declared_new = {k: v for k, v in new_by.items() if not _preserved(v)}
    elements = _diff(declared_old, declared_new)
    pages = _diff(_by_id(old_document.get("pages") or []), _by_id(new_document.get("pages") or []), "label")
    devices = _diff(old_document.get("devices") or {}, new_document.get("devices") or {})
    dashboards = _diff(old_document.get("dashboards") or {}, new_document.get("dashboards") or {})

    # per device: the resolved document before and after, `rev` included (the ring-pages migration's own proof, one registry each)
    old_x, new_x = expand_document(old_document, old_by), expand_document(new_document, new_by)
    new_registry = _NewWorld(store, new_by)
    keys = sorted({e.id for e in endpoint_table(old_x, store)} | {e.id for e in endpoint_table(new_x, store)}
                  | set(old_document.get("devices") or {}) | set(new_document.get("devices") or {}))
    per_device: list[dict[str, Any]] = []
    for key in keys:
        before = resolve_endpoint(old_x, key, states=hass.states, registry=store)
        after = resolve_endpoint(new_x, key, states=hass.states, registry=new_registry)
        equal = before == after
        if before.get("kind") is None and after.get("kind") is None and equal:
            continue                    # a key nobody is (an orphan override that did not change)
        per_device.append({"endpoint": key, "kind": after.get("kind") or before.get("kind"), "changed": not equal,
                           "rev_before": before["rev"], "rev_after": after["rev"],
                           "pages_before": [p["id"] for p in before["pages"]], "pages_after": [p["id"] for p in after["pages"]],
                           "differences": [] if equal else _differences(before, after)})
    no_page = [r["endpoint"] for r in per_device if r["pages_before"] and not r["pages_after"]]

    rings_before, rings_after = _door_rings(hass, old_document, old_by), _door_rings(hass, new_document, new_by)
    doors = {k: {"name": rings_after.get(k, rings_before.get(k))["name"], "before": rings_before.get(k), "after": rings_after.get(k)}
             for k in sorted(set(rings_before) | set(rings_after)) if rings_before.get(k) != rings_after.get(k)}

    deletions = (
        [{"kind": "element", **r} for r in elements["removed"]] + [{"kind": "page", **r} for r in pages["removed"]]
        + [{"kind": "device", **r} for r in devices["removed"]] + [{"kind": "screen", **r} for r in dashboards["removed"]]
        + [{"kind": "orphan_override", "id": k} for k in dropped_orphans])

    sensitive: list[dict[str, Any]] = []
    for key in changes:
        value = patch.get(key)
        before = settings_before.get(key, ui_settings._UNSET.get(key))
        if key == "ring_enabled":
            sensitive.append({"kind": "ring", "detail": "the push ring notification is turned " + ("on" if value else "off")})
        elif key == "new_phones_enabled":
            sensitive.append({"kind": "ring", "detail": "new phones " + ("may" if value else "may not") + " receive the ring"})
        elif key == "test_phones":
            was = set(before or [])
            sensitive.append({"kind": "test_phones", "detail": "the staged test list changes",
                              "added": sorted(set(value) - was), "removed": sorted(was - set(value))})
        elif key == "live_rtsp_base":
            sensitive.append({"kind": "live_rtsp", "detail": f"the live RTSP base changes to {value!r}" if value else
                              "the live RTSP base is cleared"})
        elif key in ("live_sub_streams", "live_away_streams"):
            sensitive.append({"kind": "live_rtsp", "detail": f"{key} changes"})
    if (old_document.get("roster") or ["all"]) != (new_document.get("roster") or ["all"]) or any(
            (old_document.get("devices") or {}).get(k, {}).get("roster_hide") != (new_document.get("devices") or {}).get(k, {}).get("roster_hide")
            for k in set(old_document.get("devices") or {}) & set(new_document.get("devices") or {})):
        sensitive.append({"kind": "house_roster", "detail": "who is listed to be called through the house roster changes"})
    if doors:
        sensitive.append({"kind": "ring_who", "detail": "who is woken when a door rings changes", "doors": sorted(doors)})
    for item in elements["changed"]:
        was, now = declared_old[item["id"]], declared_new[item["id"]]
        if (was.get("entity_id"), (was.get("options") or {}).get("action"), (was.get("options") or {}).get("door"),
                (was.get("options") or {}).get("target")) != (now.get("entity_id"), (now.get("options") or {}).get("action"),
                                                               (now.get("options") or {}).get("door"), (now.get("options") or {}).get("target")):
            sensitive.append({"kind": "element_retarget", "detail": "an element points at something else", "id": item["id"]})
    if deletions:
        sensitive.append({"kind": "deletions", "detail": f"{len(deletions)} item(s) are removed"})
    if no_page:
        sensitive.append({"kind": "no_page", "detail": "a device is left with no page", "endpoints": no_page})

    return {"elements": elements, "pages": pages, "devices": devices, "screens": dashboards, "settings": {"changed": changes},
            "per_device": per_device, "doors": doors, "deletions": deletions, "devices_without_page": no_page, "sensitive": sensitive}


# ---- the entry point ------------------------------------------------------------------------------------------------------

async def reload_config(hass: HomeAssistant, files: dict[str, str], read_errors: list[ConfigError], *, dry_run: bool) -> dict[str, Any]:
    """Validate, check and (unless `dry_run`) apply the tree `files`. Always returns a reply (never raises for a refusal):
    `{ok, applied, dry_run, changed, errors, warnings, report, doc_rev}`."""
    content, elements_store = get_content_store(hass), get_elements_store(hass)
    reply: dict[str, Any] = {"ok": False, "applied": False, "deferred": False, "dry_run": dry_run, "changed": False,
                             "checked": "full", "errors": [], "warnings": [], "report": None}

    def refused(errors: list[ConfigError]) -> dict[str, Any]:
        reply["errors"] = [_where(e) for e in errors]
        return reply

    result = config_yaml.validate_files(files)
    errors = [*read_errors, *result.errors]
    if errors:
        return refused(errors)
    await content.async_ensure_loaded()
    await elements_store.async_ensure_loaded()
    await elements_store.async_ensure_content()
    reloads: list[Any] = []
    async with elements_store.lock:
        try:
            done = await _locked(hass, content, elements_store, result, files, dry_run, reply, reloads)
            if done["ok"] and not dry_run and not done["deferred"]:
                # P3: remember the tree as adopted so none of its files is later a "pending edit". Writes no file, fires nothing.
                # A DEFERRED tree was not adopted: it stays a pending edit until the reload is run again when the house is idle.
                await config_sync.record_applied(hass, files)
        except SaveConflict:
            return refused([ConfigError("", 1, 1, "the configuration changed while the files were being applied; run it again", "conflict")])
        except ApplyRefused as err:
            return refused([ConfigError("", 1, 1, str(err), err.code)])
        except (ProfileError, ElementError) as err:
            return refused([ConfigError(config_yaml.ROOT_FILE, 1, 1, str(err), "refused")])
    return await _after_lock(hass, done, reloads)


async def _after_lock(hass: HomeAssistant, reply: dict[str, Any], reloads: list[Any]) -> dict[str, Any]:
    """The door entries a `dial_enabled` change must reload (P6), one at a time, outside the elements lock. The saved value is what the
    entry holds whether or not the reload succeeds, so a failure is a warning, not a rollback."""
    for reload in reloads:
        try:
            await reload()
        except Exception as err:  # noqa: BLE001 - reported, never raised: the options are already saved
            _LOGGER.warning("Portico: a door entry did not reload after its settings changed (%s)", type(err).__name__)
            reply["warnings"].append({"code": "reload_failed", "message": "a door entry did not reload after its settings changed; "
                                      "reload it from Settings, Devices & services"})
    return reply


async def _locked(hass: HomeAssistant, content: Any, elements_store: Any, result: config_yaml.Validation, files: dict[str, str],
                  dry_run: bool, reply: dict[str, Any], reloads: list[Any]) -> dict[str, Any]:
    errors: list[ConfigError] = []
    if not content.uses_pages_model:
        raise ApplyRefused("not_schema_6", "this install still uses the template-and-overrides model; the YAML files describe the "
                           "unified Pages model (schema 6)")
    content.check_writable()
    elements_store.check_writable()
    base_rev, base_el = content.doc_rev, elements_store.rev
    old_document = content.document()
    old_elements = elements_store.stored()

    new_elements = check_elements(hass, result, errors)
    by_id = _by_id(new_elements)
    old_by_id = _by_id(old_elements)
    # a reference the stored document already holds to an element that was ALREADY missing stays tolerated, as in the editor; a
    # reference to an element this apply removes is refused
    tolerated = frozenset(k for k in stored_ref_keys(old_document) if k[-1] not in old_by_id)
    document: dict[str, Any] | None = None
    if not errors:
        try:
            document = content.prepare_document(copy.deepcopy(result.document), elements_by_id=by_id, stored_refs=tolerated)
        except ProfileError as err:
            path, line, col = blame(result, files, str(err))
            errors.append(ConfigError(path, line, col, str(err), "invalid_document"))
    patch = check_settings(hass, result.settings or {}, errors)
    plan = await config_options.check(hass, result, errors)
    if errors or document is None:
        reply["errors"] = [_where(e) for e in errors]
        return reply

    # a screen declared for a login that does not exist
    for slug, dashboard in document["dashboards"].items():
        stored = content.dashboards.get(slug)
        if stored is None or stored.get("ha_user_id") != dashboard["ha_user_id"]:
            if await hass.auth.async_get_user(dashboard["ha_user_id"]) is None:
                path = config_yaml.file_name("dashboards", slug)
                line, col = result.loaded.lines.get(path, {}).get("ha_user_id", (1, 1))
                errors.append(ConfigError(path, line, col, f"dashboard {slug!r}: there is no Home Assistant user "
                                          f"{dashboard['ha_user_id']!r}", "unknown_user"))
    if errors:
        reply["errors"] = [_where(e) for e in errors]
        return reply

    orphans = set(content.orphan_keys())
    dropped = sorted(k for k in orphans if k not in document["devices"])
    changes = ui_settings.changed_keys(hass, patch) if patch else []
    settings_before = ui_settings.facts(hass) if patch else {}
    unchanged = (document == content.document() and new_elements == old_elements and not changes and not plan.changed)
    known_before = {e for e in _doc_entities(old_document)}
    reply["warnings"] = [{"code": "unknown_entity", "message": f"{e}: not found in Home Assistant now"}
                         for e in sorted(_doc_entities(document) - known_before) if not _entity_exists(hass, e)]
    reply["report"] = effect_report(hass, old_document=old_document, new_document=document, old_elements=old_elements,
                                    new_elements=new_elements, settings_before=settings_before, patch=patch, changes=changes,
                                    dropped_orphans=dropped)
    reply["report"]["entries"] = plan.report()
    reply["report"]["sensitive"].extend(plan.sensitive())
    reply["ok"] = True
    reply["changed"] = not unchanged
    reply["doc_rev"] = content.doc_rev
    busy = plan.changed and not config_options.calls_idle(hass)
    reply["report"]["deferred_until_idle"] = busy
    if dry_run or unchanged:
        return reply
    if busy:
        # door and phone options change who can open a door, who is rung and (a door reload) drop a SIP registration: never during a
        # call. All or nothing: the content is not applied either, so the tree stays one pending edit to run again when idle.
        reply["deferred"] = True
        reply["warnings"].append({"code": "deferred", "message": "a call is live, so nothing was applied; the door and phone settings "
                                  "in these files change who can open a door or be rung. Run the reload again when the house is idle"})
        return reply

    # the apply: one synchronous swap (nothing awaits between the revision check and it), then the writes
    if content.doc_rev != base_rev or elements_store.rev != base_el:
        raise SaveConflict("changed")
    content.check_writable()
    elements_store.check_writable()
    before_content = content.stage_applied(document)
    before_elements = elements_store.swap_in_memory(new_elements)
    undo_entries = None
    if plan.changed:
        # door and phone options too, in this same synchronous step: no call can start between the idle check and these writes
        entry_reloads, undo_entries = config_options.apply(hass, plan)
        reloads.extend(entry_reloads)           # the entry reloads run after the lock is released
    elements_written = False
    try:
        await elements_store.write_all(new_elements)
        elements_written = True
        await content.commit_applied(before_content)
    except BaseException:
        content.restore_applied(before_content)
        elements_store.swap_in_memory(before_elements)
        if undo_entries is not None:
            undo_entries()
            reloads.clear()
        if elements_written:
            try:
                await elements_store.write_all(before_elements)
            except Exception:  # noqa: BLE001 - the original failure is the one to raise
                _LOGGER.error("Portico: could not put the elements file back after a failed apply")
        raise
    if changes:
        ui_settings.write_settings(hass, push_entry(hass), patch, changes)
    content.announce_applied()
    reply["applied"] = True
    reply["doc_rev"] = content.doc_rev
    _LOGGER.info("Portico: applied the YAML configuration (%d element(s), %d page(s), %d setting(s) changed)",
                 len(new_elements), len(document["pages"]), len(changes))
    return reply


SERVICE_SCHEMA = vol.Schema({vol.Optional("dry_run", default=False): bool})


# ---- what the editor shows (P5) -------------------------------------------------------------------------------------------

_LAST_RELOAD = "config_last_reload"
REPORT_ERRORS_CAP = 20


def record_reload(hass: HomeAssistant, reply: dict[str, Any]) -> None:
    """Remember the outcome of the last REAL reload (a dry run changes nothing, so it is not "the last reload"), in memory only (gone
    at a restart; no storage). The editor's banner reads it through `last_reload`. Never raises."""
    try:
        if reply.get("dry_run"):
            return
        errors = [{k: e.get(k) for k in ("file", "line", "col", "message")} for e in reply.get("errors") or [] if isinstance(e, dict)]
        hass.data.setdefault(DOMAIN, {})[_LAST_RELOAD] = {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "ok": bool(reply.get("ok")), "applied": bool(reply.get("applied")),
            "deferred": bool(reply.get("deferred")), "error_total": len(errors), "errors": errors[:REPORT_ERRORS_CAP]}
    except Exception:  # noqa: BLE001
        _LOGGER.warning("Portico: could not record the reload result", exc_info=True)


def last_reload(hass: HomeAssistant) -> dict[str, Any] | None:
    """The last real reload's `{at, ok, applied, deferred, error_total, errors}`, or None since the restart."""
    found = hass.data.get(DOMAIN, {}).get(_LAST_RELOAD)
    return dict(found) if found else None
