"""The outbound half of the YAML configuration: the files under `/config/portico/` follow the live state (DEV-824 P3; design
`portico_config_yaml_design.md` section 5.2, amended text).

What it does, and when:

* After every editor save of the document (`portico/content/save`), of an element (`portico/elements/set` / `delete`) or of the
  push settings (`portico/ui_settings/set`) the save handler calls `async_schedule`. A background task takes ONE synchronous
  snapshot of the live state, serialises it with P1's deterministic emitter (`config_yaml.to_files`), and writes each file whose
  bytes differ from disk (temp file in the same directory, then `os.replace`). An identical file is not touched: no diff, no
  write, no event. If anything was written the HA event `portico_config_changed` is fired with `{changed, added, removed, pending,
  source, doc_rev}`: file PATHS only, never contents, never a secret.
* A failure of any of it never fails or delays the save: the task runs after the reply, every exception is caught, logged, and
  shown as a repair issue (`config_files_not_written`) that the next good write clears.
* Pending hand edits (design 5.2.3): `.storage/portico.files` keeps the hash of every file as last written by Portico or applied
  by `portico.reload_config`. A file whose bytes on disk differ from that hash AND from the new text is a pending edit: it is left
  exactly as its author wrote it, reported in the event (`pending`) and in a repair issue (`config_files_pending`) until a reload
  adopts it or `portico.export_config` with `overwrite: true` rewrites the files from live.
* `portico.export_config` (`dry_run`, `overwrite`): the initial export. It writes the first files so `reload_config` round-trips.
  It refuses when the directory already holds files unless `overwrite` is true. `dry_run` writes nothing and lists what it would do.

The switch: the settings entry's option `config_files` (default ON). Writing happens only while it is on AND `/config/portico/`
exists, so an install that never ran the export is untouched; the export creates the directory. Set the option to `false` to stop
all writing (the editor then behaves exactly as before; `reload_config` still works).

THE APPLY NEVER WRITES FILES OR FIRES THE EVENT. `config_apply` only calls `record_applied`, which updates the hash state (so a
file the apply has just adopted is not later mistaken for a pending edit) and clears the pending issue. It fires nothing and
touches no file. That is the loop break (design 5.4).

Hygiene: the root must be a real directory directly under the HA config directory, never a symlink; every path written is relative,
free of `..`, and every directory on the way and every existing target is checked not to be a symlink; nothing is ever written
outside the root. The settings written are only the `ui_settings` keys; `open_relay_action` and every secret-looking key are
refused by `config_yaml.validate_files`, which every tree passes before a single byte is written.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import SIGNAL_CONFIG_ENTRY_CHANGED
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store

from . import config_options, config_yaml, ui_settings
from .content import get_content_store
from .elements import get_elements_store
from .phones_commands import push_entry
from .provider import DOMAIN

_LOGGER = logging.getLogger(__name__)

EVENT = "portico_config_changed"
SERVICE_EXPORT_CONFIG = "export_config"
CONFIG_DIR = "portico"
OPTION = "config_files"                 # the settings entry's option; absent = on
ISSUE_PENDING = "config_files_pending"
ISSUE_NOT_WRITTEN = "config_files_not_written"
_STORE_KEY = "portico.files"
_MANAGED_DIRS = ("elements", "pages", "devices", "dashboards", "doors", "phones")
_NEVER_EMPTIED = ("elements", "pages", "devices")   # one save never removes every file of these unless the stores were loaded
_DATA = "config_sync"

EXPORT_SCHEMA = vol.Schema({vol.Optional("dry_run", default=False): bool, vol.Optional("overwrite", default=False): bool})


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class _State:
    """Per-hass: the write lock (saves and the export never write at once), the coalescing flags and the hash store."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.lock = asyncio.Lock()
        self.dirty = False
        self.task: asyncio.Task | None = None
        self.store = Store(hass, 1, _STORE_KEY)
        self.hashes: dict[str, str] | None = None
        self.write_errors: list[dict[str, str]] = []        # the last write's failures, memory only (the editor banner reads them)

    async def load(self) -> dict[str, str]:
        if self.hashes is None:
            data = await self.store.async_load()
            raw = (data or {}).get("hashes") if isinstance(data, dict) else None
            self.hashes = {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)} if isinstance(raw, dict) else {}
        return self.hashes

    async def save(self, hashes: dict[str, str]) -> None:
        self.hashes = dict(hashes)
        await self.store.async_save({"hashes": self.hashes})


def _state(hass: HomeAssistant) -> _State:
    domain = hass.data.setdefault(DOMAIN, {})
    state = domain.get(_DATA)
    if state is None:
        state = domain[_DATA] = _State(hass)
    return state


def enabled(hass: HomeAssistant) -> bool:
    entry = push_entry(hass)
    return True if entry is None else bool(entry.options.get(OPTION, True))


def root_path(hass: HomeAssistant) -> str:
    return hass.config.path(CONFIG_DIR)


# ---- the snapshot ---------------------------------------------------------------------------------------------------------

def snapshot(hass: HomeAssistant) -> tuple[dict[str, str], str]:
    """The live state as a tree, and the document revision. Synchronous: one consistent read, nothing awaited. Raises
    `ValueError` for an install that does not use the Pages model, or a tree that fails validation (so nothing is written)."""
    content = get_content_store(hass)
    if not content.uses_pages_model:
        raise ValueError("this install still uses the template-and-overrides model (schema 6 is needed)")
    facts = ui_settings.facts(hass)
    settings = {k: facts[k] for k in ui_settings.KEYS if k in facts}
    elements = [e for e in get_elements_store(hass).stored() if not e.get("managed")]
    files = config_yaml.to_files(content.document(), elements, settings, config_options.door_records(hass),
                                 config_options.phone_records(hass))
    result = config_yaml.validate_files(files)
    if not result.ok:
        first = result.errors[0]
        raise ValueError(f"the live state does not serialise to a valid tree ({first.file}:{first.line}: {first.message})")
    return files, content.doc_rev


# ---- the blocking file work -----------------------------------------------------------------------------------------------

def _clean_rel(rel: str) -> bool:
    return bool(rel) and not rel.startswith("/") and "\\" not in rel and all(p not in ("", ".", "..") for p in rel.split("/"))


def check_root(root: str, *, create: bool) -> str | None:
    """None when `root` may be written, else why not. The root is a real directory (never a symlink); `create` makes it."""
    if os.path.islink(root):
        return "the portico directory is a symbolic link"
    if not os.path.exists(root):
        if not create:
            return "there is no portico directory in the Home Assistant config"
        parent = os.path.dirname(root)
        if os.path.islink(parent) or not os.path.isdir(parent):
            return "the Home Assistant config directory is not usable"
        os.mkdir(root)
        return None
    if not os.path.isdir(root):
        return "the portico path is not a directory"
    return None


def existing_yaml(root: str) -> list[str]:
    """Every regular `.yaml` file (or link) under `root`, as relative paths. Blocking."""
    found: list[str] = []
    for base, dirs, names in os.walk(root, followlinks=False):
        dirs.sort()
        for name in sorted(names):
            if name.endswith(".yaml"):
                found.append(os.path.relpath(os.path.join(base, name), root).replace(os.sep, "/"))
    return found


def _write_atomic(dest: str, text: str) -> None:
    tmp = dest + ".tmp"                                 # never `.yaml`: the reader skips it
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _ensure_dirs(root: str, rel: str) -> None:
    current = root
    for part in rel.split("/")[:-1]:
        current = os.path.join(current, part)
        if os.path.islink(current):
            raise OSError("a symbolic link on the way")
        if not os.path.isdir(current):
            os.mkdir(current)


def write_tree(root: str, files: dict[str, str], known: dict[str, str], *, overwrite: bool = False,
               state_loaded: bool = False) -> dict[str, Any]:
    """Make the files under `root` equal `files`, around pending edits. Blocking. Returns `{changed, added, removed, pending,
    errors, hashes}`: lists of relative paths (`errors` as `{path, code}`), and the new hash state. `known` is the hash of every
    file as last written or applied. With `overwrite` a pending edit is replaced too and stray `.yaml` files in the managed
    directories are removed (the resync). Safety net (DEV-824): unless `state_loaded` (the stores were awaited loaded), a write that
    would remove every file of `elements/`, `pages/` or `devices/` removes none of that directory and lists it in `blocked`."""
    added: list[str] = []
    changed: list[str] = []
    removed: list[str] = []
    pending: list[str] = []
    errors: list[dict[str, str]] = []
    hashes: dict[str, str] = {}
    real_root = os.path.realpath(root)
    for rel in sorted(files):
        text = files[rel]
        if not _clean_rel(rel):
            errors.append({"path": rel, "code": "bad_path"})
            continue
        dest = os.path.join(root, *rel.split("/"))
        try:
            if os.path.islink(dest):
                errors.append({"path": rel, "code": "symlink"})
                continue
            if os.path.lexists(dest):
                if not os.path.isfile(dest):
                    errors.append({"path": rel, "code": "not_a_file"})
                    continue
                with open(dest, encoding="utf-8") as handle:
                    disk = handle.read()
                if disk == text:
                    hashes[rel] = _sha(text)
                    continue
                if not overwrite and _sha(disk) != known.get(rel):
                    pending.append(rel)                 # a hand edit, not yet reloaded: left as its author wrote it
                    if rel in known:
                        hashes[rel] = known[rel]
                    continue
                _ensure_dirs(root, rel)
                if not os.path.realpath(dest).startswith(real_root + os.sep):
                    errors.append({"path": rel, "code": "outside_root"})
                    continue
                _write_atomic(dest, text)
                changed.append(rel)
            else:
                _ensure_dirs(root, rel)
                if not os.path.realpath(os.path.dirname(dest)).startswith(real_root):
                    errors.append({"path": rel, "code": "outside_root"})
                    continue
                _write_atomic(dest, text)
                added.append(rel)
            hashes[rel] = _sha(text)
        except (OSError, UnicodeDecodeError) as err:
            errors.append({"path": rel, "code": type(err).__name__})
            if rel in known:
                hashes[rel] = known[rel]
    # files Portico wrote before that the live state no longer has (an element deleted)
    stale = set(known) - set(files)
    if overwrite:
        stale |= {p for p in existing_yaml(root) if p.split("/")[0] in _MANAGED_DIRS and p not in files}
    blocked: list[str] = []
    if not state_loaded:
        for top in _NEVER_EMPTIED:
            if not any(p.startswith(top + "/") for p in files) and any(p.startswith(top + "/") for p in stale):
                blocked.append(top)
    for rel in sorted(stale):
        if not _clean_rel(rel):
            continue
        if rel.split("/")[0] in blocked:
            if rel in known:
                hashes[rel] = known[rel]
            continue
        dest = os.path.join(root, *rel.split("/"))
        try:
            if not os.path.lexists(dest):
                continue
            if os.path.islink(dest) or not os.path.isfile(dest):
                errors.append({"path": rel, "code": "symlink" if os.path.islink(dest) else "not_a_file"})
                continue
            with open(dest, encoding="utf-8") as handle:
                disk = handle.read()
            if not overwrite and _sha(disk) != known.get(rel):
                pending.append(rel)
                hashes[rel] = known[rel]
                continue
            os.unlink(dest)
            removed.append(rel)
        except (OSError, UnicodeDecodeError) as err:
            errors.append({"path": rel, "code": type(err).__name__})
            if rel in known:
                hashes[rel] = known[rel]
    return {"changed": changed, "added": added, "removed": removed, "pending": sorted(pending), "errors": errors, "hashes": hashes,
            "blocked": blocked}


# ---- issues ---------------------------------------------------------------------------------------------------------------

def _issues(hass: HomeAssistant, *, pending: list[str], errors: list[Any]) -> None:
    _state(hass).write_errors = [e if isinstance(e, dict) else {"path": "", "code": str(e)} for e in errors]
    for key, bad, placeholder in ((ISSUE_PENDING, pending, str(len(pending))), (ISSUE_NOT_WRITTEN, errors, str(len(errors)))):
        if bad:
            ir.async_create_issue(hass, DOMAIN, key, is_fixable=False, severity=ir.IssueSeverity.WARNING, translation_key=key,
                                  translation_placeholders={"count": placeholder})
        else:
            ir.async_delete_issue(hass, DOMAIN, key)


# ---- what the editor shows (P5) --------------------------------------------------------------------------------------------

STATUS_CAP = 20                                  # paths / errors the editor banner is sent; the totals are exact


def _pending_on_disk(root: str, known: dict[str, str]) -> list[str]:
    """The files in the layout whose bytes are not what Portico last wrote or applied (a hand edit, or a hand-made file). Blocking,
    read-only; the same test `write_tree` makes, but now rather than at the next save."""
    found: list[str] = []
    for rel in existing_yaml(root):
        if not (rel == config_yaml.ROOT_FILE or rel.split("/")[0] in _MANAGED_DIRS) or not _clean_rel(rel):
            continue
        dest = os.path.join(root, *rel.split("/"))
        try:
            if os.path.islink(dest):
                continue
            with open(dest, encoding="utf-8") as handle:
                if _sha(handle.read()) != known.get(rel):
                    found.append(rel)
        except (OSError, UnicodeDecodeError):
            found.append(rel)                    # unreadable is not what Portico wrote either
    return sorted(found)


async def status(hass: HomeAssistant) -> dict[str, Any]:
    """The state of the file configuration for the editor's banner. Read-only: nothing is written, no event, no issue touched.
    `{enabled, dir_exists, pending: [paths, capped], pending_total, last_write_error: null | {count, errors: [{path, code}]}}`."""
    on = enabled(hass)
    root = root_path(hass)
    exists = await hass.async_add_executor_job(os.path.isdir, root)
    state = _state(hass)
    pending: list[str] = []
    if on and exists:
        known = await state.load()
        pending = await hass.async_add_executor_job(_pending_on_disk, root, dict(known))
    errors = list(state.write_errors)
    return {"enabled": on, "dir_exists": exists, "pending": pending[:STATUS_CAP], "pending_total": len(pending),
            "last_write_error": {"count": len(errors), "errors": errors[:STATUS_CAP]} if errors else None}


# ---- the editor save path -------------------------------------------------------------------------------------------------

@callback
def async_schedule(hass: HomeAssistant) -> None:
    """Called by a save handler after the save succeeded. Never raises, never waits. Saves close together coalesce into one write."""
    try:
        if not enabled(hass):
            return
        state = _state(hass)
        state.dirty = True
        if state.task is None or state.task.done():
            state.task = hass.async_create_background_task(_run(hass, state), "portico config files")
    except Exception:  # noqa: BLE001 - a failure here never reaches the save
        _LOGGER.warning("Portico: could not schedule the YAML files write", exc_info=True)


async def _run(hass: HomeAssistant, state: _State) -> None:
    while state.dirty:
        state.dirty = False
        try:
            await sync_files(hass, source="editor")
        except Exception:  # noqa: BLE001 - log and show it, never lose the save
            _LOGGER.warning("Portico: writing the YAML files failed", exc_info=True)
            try:
                _issues(hass, pending=[], errors=["write"])
            except Exception:  # noqa: BLE001
                pass


async def sync_files(hass: HomeAssistant, *, source: str) -> dict[str, Any] | None:
    """Write the live state to the files if writing is on and the directory exists; fire the event when anything was written.
    Returns the write result, or None when there was nothing to do."""
    if not enabled(hass):
        return None
    root = root_path(hass)
    if not await hass.async_add_executor_job(os.path.isdir, root):
        return None
    state = _state(hass)
    async with state.lock:
        result, doc_rev = await _write(hass, state, root, overwrite=False)
    _publish(hass, result, doc_rev, source)
    return result


async def _ensure_stores(hass: HomeAssistant) -> bool:
    """Await both stores loaded before any snapshot (after an HA restart they are not until something reads them: a snapshot of an
    unloaded store has no elements). True when both are loaded; never raises."""
    content, elements = get_content_store(hass), get_elements_store(hass)
    try:
        await content.async_ensure_loaded()
        await elements.async_ensure_loaded()
        await elements.async_ensure_content()
    except Exception:  # noqa: BLE001 - the write_tree safety net then refuses to empty a directory
        _LOGGER.warning("Portico: the stores could not be loaded before the YAML files write", exc_info=True)
    return bool(content.loaded and elements.loaded)


async def _write(hass: HomeAssistant, state: _State, root: str, *, overwrite: bool, create: bool = False) -> tuple[dict[str, Any], str]:
    loaded = await _ensure_stores(hass)
    files, doc_rev = snapshot(hass)
    known = await state.load()
    problem = await hass.async_add_executor_job(lambda: check_root(root, create=create))
    if problem:
        raise ValueError(problem)
    result = await hass.async_add_executor_job(lambda: write_tree(root, files, known, overwrite=overwrite, state_loaded=loaded))
    _refused(result)
    await state.save(result["hashes"])
    return result, doc_rev


def _refused(result: dict[str, Any]) -> None:
    """A refused emptying is an error of the write (the not-written issue, the log), not a silent skip."""
    for top in result.get("blocked", []):
        result["errors"].append({"path": top + "/", "code": "refused_empty_dir"})
        _LOGGER.error("Portico: refused to remove every file of %s/ (the live state may not be loaded); nothing was removed there", top)


def _publish(hass: HomeAssistant, result: dict[str, Any], doc_rev: str, source: str) -> None:
    _issues(hass, pending=result["pending"], errors=result["errors"])
    if result["errors"]:
        _LOGGER.warning("Portico: %d YAML file(s) could not be written: %s", len(result["errors"]),
                        ", ".join(f"{e['path']} ({e['code']})" for e in result["errors"][:5]))
    if result["changed"] or result["added"] or result["removed"]:
        hass.bus.async_fire(EVENT, {"changed": result["changed"], "added": result["added"], "removed": result["removed"],
                                    "pending": result["pending"], "source": source, "doc_rev": doc_rev})


# ---- the apply's only touch -----------------------------------------------------------------------------------------------

async def record_applied(hass: HomeAssistant, files: dict[str, str]) -> None:
    """After `reload_config` applied (or found nothing to change in) the tree `files`: remember each file's hash so none of them is
    later taken for a pending edit, and clear the pending issue. Writes no file, fires no event. Never raises."""
    try:
        state = _state(hass)
        async with state.lock:
            await state.save({path: _sha(text) for path, text in files.items()})
        ir.async_delete_issue(hass, DOMAIN, ISSUE_PENDING)
    except Exception:  # noqa: BLE001
        _LOGGER.warning("Portico: could not record the applied YAML tree", exc_info=True)


# ---- the initial export ---------------------------------------------------------------------------------------------------

async def export_config(hass: HomeAssistant, *, dry_run: bool, overwrite: bool) -> dict[str, Any]:
    """The service's reply: `{ok, dry_run, overwrite, written, files, errors}`. A refusal is `ok: false` with `errors`."""
    reply: dict[str, Any] = {"ok": False, "dry_run": dry_run, "overwrite": overwrite, "written": False, "files": [], "errors": []}

    def refuse(code: str, message: str) -> dict[str, Any]:
        reply["errors"].append({"code": code, "message": message})
        return reply

    content, elements = get_content_store(hass), get_elements_store(hass)
    await content.async_ensure_loaded()
    await elements.async_ensure_loaded()
    await elements.async_ensure_content()
    root = root_path(hass)
    state = _state(hass)
    async with state.lock:
        try:
            files, doc_rev = snapshot(hass)
        except ValueError as err:
            return refuse("not_exportable", str(err))
        reply["files"] = sorted(files)
        problem = await hass.async_add_executor_job(lambda: check_root(root, create=not dry_run))
        if problem and not (dry_run and "no portico directory" in problem):
            return refuse("bad_root", problem)
        present = [] if problem else await hass.async_add_executor_job(existing_yaml, root)
        if present and not overwrite:
            return refuse("exists", f"{root} already holds {len(present)} file(s); pass overwrite: true to rewrite them from the live "
                                    "state (a hand edit there is replaced)")
        if dry_run:
            reply["ok"] = True
            reply["would_remove"] = sorted(p for p in present if p not in files and p.split("/")[0] in _MANAGED_DIRS) if overwrite else []
            return reply
        known = await state.load()
        loaded = bool(content.loaded and elements.loaded)
        result = await hass.async_add_executor_job(lambda: write_tree(root, files, known, overwrite=overwrite, state_loaded=loaded))
        _refused(result)
        await state.save(result["hashes"])
    reply.update(ok=not result["errors"], written=True, added=result["added"], changed=result["changed"], removed=result["removed"],
                 errors=[{"code": e["code"], "message": f"{e['path']}: could not be written"} for e in result["errors"]])
    _publish(hass, result, doc_rev, "export")
    _LOGGER.info("Portico: exported the live configuration to %s (%d added, %d changed, %d removed)", root, len(result["added"]),
                 len(result["changed"]), len(result["removed"]))
    return reply


def async_setup_config_sync(hass: HomeAssistant) -> None:
    @callback
    def _entry_changed(_change: Any, entry: Any) -> None:
        """P6: a door or phone entry changed (its own form, the Devices tab, an enrolment): the files that mirror its options are
        rewritten like after an editor save, so a stale file never waits to undo the change at the next reload. A write that finds the
        files already equal changes nothing and fires nothing (the apply's own option writes land here too, harmlessly)."""
        if getattr(entry, "domain", None) == DOMAIN and (
                config_options.is_door_entry(entry) or config_options.is_phone_entry(entry)):
            async_schedule(hass)

    async_dispatcher_connect(hass, SIGNAL_CONFIG_ENTRY_CHANGED, _entry_changed)

    async def _export(call: ServiceCall) -> ServiceResponse:
        return await export_config(hass, dry_run=call.data["dry_run"], overwrite=call.data["overwrite"])

    hass.services.async_register(DOMAIN, SERVICE_EXPORT_CONFIG, _export, schema=EXPORT_SCHEMA, supports_response=SupportsResponse.ONLY)
