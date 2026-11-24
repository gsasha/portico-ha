#!/usr/bin/env python3
"""Offline migration for the unification of `portico_intercom` into `portico` (DEV-686, Option B).

Run it with Home Assistant STOPPED, after a dated copy of the storage files (docs/history/unify_runbook.md). It edits two
files of `<config>/.storage/` and nothing else:

* `core.config_entries`: every config entry of domain `portico_intercom` that is a door (it has `data.driver`) becomes an
  entry of domain `portico`, and its `data` gains `entry_type: "door"`. The `entry_id` is KEPT, and so are the
  `unique_id`, the title, the options and every other field.
* `core.entity_registry`: the `platform` of the rows that belong to those entries (`config_entry_id` equal to a door's
  entry id, or a unique id that starts with `<entry id>_`) becomes `portico`. The entity ids, unique ids, names, areas
  and labels are untouched, so `sensor.call_state` and `sensor.listen_leg` keep their ids and their history.

Because the entry id is kept, nothing else moves: the peer-token Store `.storage/portico.gate_sip_peer.<entry id>` is
read under the same name by the unified code, the repair-issue ids (`driver_error_<entry id>`) are unchanged, and the
hub socket directory `.storage/portico_intercom/` keeps its name.

Safety, in the order it is applied (a refusal exits 2 and changes nothing):

1. nothing to do (no door entry of domain `portico_intercom` and no registry row left to move) is success and a no-op;
   a second run is therefore always a no-op;
2. Home Assistant must not look running: the single-instance lock `<config>/.ha_run.lock` must be free (HA holds an
   exclusive `flock` on it for as long as it runs, from any container sharing the config directory), the hub socket
   `.storage/portico_intercom/hub.sock` must not accept a connection, the two files must not have been modified in the
   last `--quiet-seconds` (60) seconds (a stopping HA flushes its registries late), and there must be no `.HA_RESTORE`
   (the next start would restore a backup over the migration);
3. a real run needs `--backup-dir`: a folder holding byte-identical copies of the two files, of every peer-token Store
   of a migrated door and of `.storage/portico_intercom/` (see the runbook);
4. an existing `portico` entry with the same unique id as a door to migrate is a refusal (Home Assistant was started
   with the new code and the door added again; delete that entry first);
5. everything is computed and checked in memory first (counts, ids, that only the intended keys differ), written with a
   temporary file and an atomic rename (`core.entity_registry` first, `core.config_entries` last), then read back from
   disk and compared; on a mismatch the original bytes are put back.

`--dry-run` does steps 1, 4 and the computation, prints what it would change and writes nothing; step 2 is only a warning
there. Standard library only (it runs where Home Assistant is not).
"""
from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import os
import socket
import stat
import sys
import time

OLD_DOMAIN = "portico_intercom"
NEW_DOMAIN = "portico"
ENTRY_TYPE = "entry_type"
ENTRY_TYPE_DOOR = "door"

STORAGE = ".storage"
CONFIG_ENTRIES = "core.config_entries"
ENTITY_REGISTRY = "core.entity_registry"
DEVICE_REGISTRY = "core.device_registry"
PEER_STORE_PREFIX = "portico.gate_sip_peer."
SOCKET_DIR = "portico_intercom"
SOCKET_NAME = "hub.sock"
LOCK_FILE = ".ha_run.lock"
RESTORE_FILE = ".HA_RESTORE"
DEFAULT_QUIET_SECONDS = 60

EXIT_OK, EXIT_ERROR, EXIT_REFUSED = 0, 1, 2


class Refused(Exception):
    """A safety refusal: nothing was changed (exit 2)."""


class Failure(Exception):
    """A malformed input or a failed write (exit 1)."""


# ---- reading --------------------------------------------------------------------------------------------------------


class Stored:
    """One `.storage` file: its path, bytes, parsed document and whether it ended with a newline."""

    def __init__(self, path: str, raw: bytes, doc: dict) -> None:
        self.path, self.raw, self.doc = path, raw, doc
        self.trailing_newline = raw.endswith(b"\n")


def _storage_path(config_dir: str, name: str) -> str:
    return os.path.join(config_dir, STORAGE, name)


def _read_json(path: str, key: str) -> Stored:
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as err:
        raise Failure(f"cannot read {path}: {err}") from err
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as err:
        raise Failure(f"{path} is not valid JSON: {err}") from err
    if not isinstance(doc, dict) or doc.get("key") != key or not isinstance(doc.get("data"), dict):
        raise Failure(f"{path} is not a Home Assistant '{key}' storage file (missing key or data)")
    return Stored(path, raw, doc)


def _dumps(stored: Stored, doc: dict) -> bytes:
    """Serialise the way Home Assistant does (orjson, two-space indent, no ASCII escaping), keeping the original's
    trailing newline convention."""
    text = json.dumps(doc, indent=2, ensure_ascii=False)
    if stored.trailing_newline:
        text += "\n"
    return text.encode("utf-8")


# ---- the plan: computed in memory, checked, only then written ---------------------------------------------------------


class Plan:
    def __init__(self) -> None:
        self.entries: Stored | None = None
        self.registry: Stored | None = None
        self.new_entries_doc: dict | None = None
        self.new_registry_doc: dict | None = None
        self.migrated: list[dict] = []            # the entries that change domain (their ORIGINAL dicts)
        self.skipped: list[tuple[dict, str]] = []
        self.rows_moved: list[str] = []           # entity ids whose platform changes
        self.deleted_rows_moved: list[str] = []
        self.rows_left: list[str] = []            # platform portico_intercom rows not tied to a door
        self.warnings: list[str] = []
        self.door_ids: set[str] = set()

    @property
    def has_work(self) -> bool:
        return bool(self.migrated or self.rows_moved or self.deleted_rows_moved)


def _row_name(row: dict) -> str:
    return str(row.get("entity_id") or row.get("unique_id") or row.get("id") or "?")


def _belongs_to(row: dict, door_ids: set[str]) -> bool:
    if row.get("config_entry_id") in door_ids:
        return True
    unique_id = row.get("unique_id")
    return isinstance(unique_id, str) and any(unique_id.startswith(f"{door}_") for door in door_ids)


def build_plan(config_dir: str) -> Plan:
    plan = Plan()
    plan.entries = _read_json(_storage_path(config_dir, CONFIG_ENTRIES), CONFIG_ENTRIES)
    plan.registry = _read_json(_storage_path(config_dir, ENTITY_REGISTRY), ENTITY_REGISTRY)

    entries_doc = copy.deepcopy(plan.entries.doc)
    entries = entries_doc["data"].get("entries")
    if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
        raise Failure(f"{plan.entries.path}: data.entries is not a list of entries")
    new_domain_uids = {e.get("unique_id") for e in entries if e.get("domain") == NEW_DOMAIN and e.get("unique_id")}

    for entry in entries:
        if entry.get("domain") != OLD_DOMAIN:
            continue
        data = entry.get("data")
        if not isinstance(data, dict) or not data.get("driver"):
            plan.skipped.append((entry, "it has no data.driver, so it is not a door; left as it is"))
            continue
        if data.get(ENTRY_TYPE) not in (None, ENTRY_TYPE_DOOR):
            raise Refused(f"entry {entry.get('entry_id')} already has data.entry_type={data.get(ENTRY_TYPE)!r}; "
                          "not a door entry of the old integration, refusing to guess")
        unique_id = entry.get("unique_id")
        if unique_id and unique_id in new_domain_uids:
            raise Refused(
                f"a '{NEW_DOMAIN}' entry with unique id {unique_id!r} already exists (Home Assistant was started with the "
                f"new code and the door was added again?). Migrating {entry.get('entry_id')} would create a duplicate. "
                "Delete the new entry in Home Assistant (or restore the saved files) and run again. Nothing was changed.")
        plan.migrated.append(copy.deepcopy(entry))
        entry["domain"] = NEW_DOMAIN
        entry["data"] = {ENTRY_TYPE: ENTRY_TYPE_DOOR, **data}
    plan.new_entries_doc = entries_doc

    # the door entry ids the registry rows are matched against: those being migrated now, and those migrated by an
    # earlier (possibly cut short) run, so a half-run state is finished by running again
    plan.door_ids = {e["entry_id"] for e in plan.migrated if e.get("entry_id")} | {
        e["entry_id"] for e in entries
        if e.get("domain") == NEW_DOMAIN and isinstance(e.get("data"), dict)
        and e["data"].get(ENTRY_TYPE) == ENTRY_TYPE_DOOR and e.get("entry_id")}

    registry_doc = copy.deepcopy(plan.registry.doc)
    reg_data = registry_doc["data"]
    for section, moved in (("entities", plan.rows_moved), ("deleted_entities", plan.deleted_rows_moved)):
        rows = reg_data.get(section, [])
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            raise Failure(f"{plan.registry.path}: data.{section} is not a list of rows")
        for row in rows:
            if row.get("platform") != OLD_DOMAIN:
                continue
            if _belongs_to(row, plan.door_ids):
                row["platform"] = NEW_DOMAIN
                moved.append(_row_name(row))
            else:
                plan.rows_left.append(f"{section}: {_row_name(row)}")
    _check_no_clash(reg_data.get("entities", []), "entities")
    plan.new_registry_doc = registry_doc

    _check_postconditions(plan)
    _device_registry_warnings(config_dir, plan)
    for stored in (plan.entries, plan.registry):
        if _dumps(stored, stored.doc) != stored.raw:
            plan.warnings.append(f"{os.path.basename(stored.path)} is not formatted the way this script writes it "
                                 "(Home Assistant's own writer: two-space indent, UTF-8, no newline); the content is "
                                 "identical, only the layout of the rewritten file will differ")
    return plan


def _check_no_clash(rows: list[dict], section: str) -> None:
    seen: dict[tuple, str] = {}
    for row in rows:
        entity_id = str(row.get("entity_id", ""))
        key = (entity_id.split(".", 1)[0], row.get("platform"), row.get("unique_id"))
        if key in seen:
            raise Refused(f"after the move two {section} rows would share (domain, platform, unique id) = {key}: "
                          f"{seen[key]} and {entity_id}. Nothing was changed.")
        seen[key] = entity_id


def _check_postconditions(plan: Plan) -> None:
    """Assert, on the in-memory result, that ONLY the intended keys differ. Any failure is a bug here, not input."""
    old, new = plan.entries.doc, plan.new_entries_doc
    assert [k for k in old] == [k for k in new] and old["data"].keys() == new["data"].keys()
    old_entries, new_entries = old["data"]["entries"], new["data"]["entries"]
    assert len(old_entries) == len(new_entries)
    migrating = {e["entry_id"] for e in plan.migrated}
    for before, after in zip(old_entries, new_entries):
        assert before.get("entry_id") == after.get("entry_id")
        if before.get("entry_id") in migrating and before.get("domain") == OLD_DOMAIN and before.get("data", {}).get("driver"):
            changed = {k for k in set(before) | set(after) if before.get(k) != after.get(k)}
            assert changed == {"domain", "data"}, changed
            assert after["domain"] == NEW_DOMAIN and after["data"][ENTRY_TYPE] == ENTRY_TYPE_DOOR
            assert {k: v for k, v in after["data"].items() if k != ENTRY_TYPE} == before["data"]
        else:
            assert before == after
    assert {k: v for k, v in old["data"].items() if k != "entries"} == {k: v for k, v in new["data"].items() if k != "entries"}
    assert {k: v for k, v in old.items() if k != "data"} == {k: v for k, v in new.items() if k != "data"}

    reg_old, reg_new = plan.registry.doc, plan.new_registry_doc
    assert {k: v for k, v in reg_old.items() if k != "data"} == {k: v for k, v in reg_new.items() if k != "data"}
    for section in set(reg_old["data"]) | set(reg_new["data"]):
        before, after = reg_old["data"].get(section), reg_new["data"].get(section)
        if section not in ("entities", "deleted_entities"):
            assert before == after, section
            continue
        assert len(before) == len(after)
        for row_before, row_after in zip(before, after):
            changed = {k for k in set(row_before) | set(row_after) if row_before.get(k) != row_after.get(k)}
            assert changed <= {"platform"}, changed
            if changed:
                assert row_before["platform"] == OLD_DOMAIN and row_after["platform"] == NEW_DOMAIN


def _device_registry_warnings(config_dir: str, plan: Plan) -> None:
    """Read-only: the design says a door creates no device. If one exists, say so (it is not touched)."""
    path = _storage_path(config_dir, DEVICE_REGISTRY)
    if not os.path.exists(path) or not plan.door_ids:
        return
    try:
        doc = _read_json(path, DEVICE_REGISTRY).doc
    except Failure as err:
        plan.warnings.append(f"device registry not checked: {err}")
        return
    devices = doc["data"].get("devices", []) if isinstance(doc["data"], dict) else []
    hits = [d for d in devices if isinstance(d, dict) and (
        set(d.get("config_entries") or []) & plan.door_ids
        or any(isinstance(i, (list, tuple)) and i and i[0] == OLD_DOMAIN for i in d.get("identifiers") or []))]
    if hits:
        plan.warnings.append(
            f"{len(hits)} device registry row(s) reference a door entry or the domain '{OLD_DOMAIN}'; the design says "
            "none exist, and they are NOT touched. Look at them before starting Home Assistant.")


# ---- is Home Assistant running? ----------------------------------------------------------------------------------------


def running_reasons(config_dir: str, quiet_seconds: float, now: float | None = None) -> list[str]:
    """Reasons to believe Home Assistant (or its transport, or a pending restore) is live. Empty means it looks stopped."""
    now = time.time() if now is None else now
    reasons: list[str] = []

    lock = os.path.join(config_dir, LOCK_FILE)
    if os.path.exists(lock):
        try:
            with open(lock, "rb") as handle:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    detail = ""
                    try:
                        handle.seek(0)
                        info = json.loads(handle.read().decode("utf-8") or "{}")
                        detail = f" (pid {info.get('pid')}, started {info.get('start_ts')})"
                    except (OSError, ValueError):
                        pass
                    reasons.append(f"{lock} is locked by a running Home Assistant{detail}")
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError as err:
            reasons.append(f"cannot test the single-instance lock {lock}: {err}")

    sock_path = os.path.join(config_dir, STORAGE, SOCKET_DIR, SOCKET_NAME)
    try:
        mode = os.stat(sock_path).st_mode
    except FileNotFoundError:
        mode = None
    except OSError as err:
        reasons.append(f"cannot look at the hub socket {sock_path}: {err}")
        mode = None
    if mode is not None and stat.S_ISSOCK(mode):
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(1.0)
        try:
            probe.connect(sock_path)
        except (ConnectionRefusedError, FileNotFoundError):
            pass                                  # a stale socket file left by a crash: nobody is listening
        except OSError as err:
            reasons.append(f"cannot tell whether the hub socket {sock_path} is live ({err}); refusing to guess")
        else:
            reasons.append(f"the transport hub accepts connections on {sock_path}: Home Assistant is running")
        finally:
            probe.close()

    for name in (CONFIG_ENTRIES, ENTITY_REGISTRY):
        path = _storage_path(config_dir, name)
        try:
            age = now - os.stat(path).st_mtime
        except OSError:
            continue
        if age < quiet_seconds:
            reasons.append(f"{name} was modified {age:.0f} s ago (less than {quiet_seconds:g} s): Home Assistant may "
                           "still be writing; wait, or check it is really stopped")

    if os.path.exists(os.path.join(config_dir, RESTORE_FILE)):
        reasons.append(f"{RESTORE_FILE} exists: the next start would restore a backup over this migration")
    return reasons


# ---- the backup check ---------------------------------------------------------------------------------------------------


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_backup(config_dir: str, backup_dir: str, plan: Plan) -> None:
    """The dated copy must exist and be byte-identical to what is about to change (and present for what stays)."""
    if not os.path.isdir(backup_dir):
        raise Refused(f"--backup-dir {backup_dir} is not a directory; take the dated copy first (docs/history/unify_runbook.md)")
    storage = os.path.join(config_dir, STORAGE)
    required = [CONFIG_ENTRIES, ENTITY_REGISTRY]
    required += [f"{PEER_STORE_PREFIX}{e['entry_id']}" for e in plan.migrated
                 if os.path.exists(os.path.join(storage, f"{PEER_STORE_PREFIX}{e['entry_id']}"))]
    for name in required:
        original, copy_path = os.path.join(storage, name), os.path.join(backup_dir, name)
        if not os.path.isfile(copy_path):
            raise Refused(f"the backup has no copy of {name} ({copy_path}); take the dated copy first")
        if _sha256(original) != _sha256(copy_path):
            raise Refused(f"the backup's {name} differs from the live file; take the dated copy again")
    socket_dir = os.path.join(storage, SOCKET_DIR)
    if os.path.isdir(socket_dir):
        if not os.path.isdir(os.path.join(backup_dir, SOCKET_DIR)):
            raise Refused(f"the backup has no copy of the {SOCKET_DIR}/ folder; take the dated copy first")
        for name in sorted(os.listdir(socket_dir)):
            original = os.path.join(socket_dir, name)
            if not os.path.isfile(original) or os.path.islink(original):
                continue                              # the socket itself cannot be copied and is recreated at start
            copy_path = os.path.join(backup_dir, SOCKET_DIR, name)
            if not os.path.isfile(copy_path) or _sha256(original) != _sha256(copy_path):
                raise Refused(f"the backup's {SOCKET_DIR}/{name} is missing or differs; take the dated copy again")


# ---- writing ----------------------------------------------------------------------------------------------------------


def _atomic_write(path: str, data: bytes, like: str) -> None:
    """Temporary file in the same directory, fsync, rename over the target, fsync the directory. Mode and owner follow
    the original."""
    directory = os.path.dirname(path)
    tmp = os.path.join(directory, f".{os.path.basename(path)}.migrate-tmp")
    st = os.stat(like)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IMODE(st.st_mode))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, stat.S_IMODE(st.st_mode))
        try:
            os.chown(tmp, st.st_uid, st.st_gid)
        except PermissionError:
            pass                                      # not root: the file is ours, which is the best that can be done
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    dir_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def apply_plan(plan: Plan) -> None:
    """Write the entity registry first and the config entries last (the entries are the marker: a run cut short in
    between is finished by running again), then read both back and compare; restore the originals on a mismatch."""
    outputs = [(plan.registry, plan.new_registry_doc), (plan.entries, plan.new_entries_doc)]
    payloads = []
    for stored, new_doc in outputs:
        data = _dumps(stored, new_doc)
        if json.loads(data.decode("utf-8")) != new_doc:
            raise Failure(f"internal error: {stored.path} does not round-trip; nothing was written")
        payloads.append((stored, new_doc, data))
    written: list[Stored] = []
    try:
        for stored, new_doc, data in payloads:
            _atomic_write(stored.path, data, stored.path)
            written.append(stored)
        for stored, new_doc, data in payloads:
            with open(stored.path, "rb") as handle:
                if handle.read() != data:
                    raise Failure(f"{stored.path} does not read back as written")
    except BaseException as err:
        for stored in reversed(written):
            try:
                _atomic_write(stored.path, stored.raw, stored.path)
            except OSError:
                print(f"!! could not restore {stored.path}; restore it from the dated copy", file=sys.stderr)
        if isinstance(err, Failure):
            raise
        raise Failure(f"write failed ({err}); the original files were put back") from err


# ---- the command line -----------------------------------------------------------------------------------------------


def describe(plan: Plan, out) -> None:
    for entry in plan.migrated:
        print(f"entry {entry.get('entry_id')} {entry.get('title')!r} (driver {entry['data'].get('driver')}): "
              f"domain {OLD_DOMAIN} -> {NEW_DOMAIN}, data.{ENTRY_TYPE} = {ENTRY_TYPE_DOOR}; entry id, unique id "
              f"{entry.get('unique_id')!r}, options and every other field kept", file=out)
    if plan.rows_moved:
        print(f"entity registry: platform {OLD_DOMAIN} -> {NEW_DOMAIN} on {len(plan.rows_moved)} row(s): "
              f"{', '.join(plan.rows_moved)} (entity ids and unique ids unchanged)", file=out)
    if plan.deleted_rows_moved:
        print(f"entity registry (deleted rows): platform {OLD_DOMAIN} -> {NEW_DOMAIN} on {len(plan.deleted_rows_moved)} "
              f"row(s): {', '.join(plan.deleted_rows_moved)}", file=out)
    for entry, why in plan.skipped:
        print(f"WARNING: entry {entry.get('entry_id')} of domain {OLD_DOMAIN} is not migrated: {why}", file=out)
    for row in plan.rows_left:
        print(f"WARNING: registry row not tied to a migrated door, left as it is: {row}", file=out)
    for warning in plan.warnings:
        print(f"WARNING: {warning}", file=out)


def peer_store_note(config_dir: str, plan: Plan, out) -> None:
    for entry in plan.migrated:
        name = f"{PEER_STORE_PREFIX}{entry['entry_id']}"
        present = os.path.exists(_storage_path(config_dir, name))
        print(f"peer-token Store {name}: {'present' if present else 'ABSENT (the listen leg is off or never configured)'}"
              "; it keeps its name because the entry id is kept", file=out)


def run(config_dir: str, *, dry_run: bool, backup_dir: str | None, quiet_seconds: float, out=None) -> int:
    out = out or sys.stdout                # looked up now, not at import (a caller may have redirected stdout)
    config_dir = os.path.abspath(config_dir)
    plan = build_plan(config_dir)
    describe(plan, out)
    if not plan.has_work:
        print("nothing to do: no door entry of domain portico_intercom and no registry row left to move "
              "(already migrated, or never needed)", file=out)
        return EXIT_OK
    peer_store_note(config_dir, plan, out)
    reasons = running_reasons(config_dir, quiet_seconds)
    if dry_run:
        for reason in reasons:
            print(f"WARNING (a real run would refuse): {reason}", file=out)
        print("dry run: nothing was written", file=out)
        return EXIT_OK
    if reasons:
        raise Refused("Home Assistant looks running or just stopped:\n  - " + "\n  - ".join(reasons)
                      + "\nStop it (ha core stop), wait, and run again. Nothing was changed.")
    if not backup_dir:
        raise Refused("a real run needs --backup-dir <the dated copy> (docs/history/unify_runbook.md); nothing was changed")
    verify_backup(config_dir, os.path.abspath(backup_dir), plan)
    apply_plan(plan)
    print(f"done: {len(plan.migrated)} door entry/entries and "
          f"{len(plan.rows_moved) + len(plan.deleted_rows_moved)} registry row(s) migrated. "
          "Start Home Assistant and run the checks in the runbook.", file=out)
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", required=True, help="the Home Assistant config directory (holds .storage/)")
    parser.add_argument("--backup-dir", help="the dated copy of the storage files (required for a real run)")
    parser.add_argument("--dry-run", action="store_true", help="print what would change and write nothing")
    parser.add_argument("--quiet-seconds", type=float, default=DEFAULT_QUIET_SECONDS,
                        help="refuse if the two files were modified more recently than this (default 60)")
    args = parser.parse_args(argv)
    try:
        return run(args.config, dry_run=args.dry_run, backup_dir=args.backup_dir, quiet_seconds=args.quiet_seconds)
    except Refused as err:
        print(f"REFUSED: {err}", file=sys.stderr)
        return EXIT_REFUSED
    except Failure as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
