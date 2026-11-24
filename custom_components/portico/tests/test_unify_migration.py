"""The offline migration script `scripts/migrate_unify.py` (DEV-686, Option B), on fixtures.

No Home Assistant is needed: the script is standard library only and so are these tests (they run in the second pytest
session, the one that allows a real Unix socket, for the refusal that probes the hub socket).

The fixtures are `fixtures/unify/core.config_entries.old.json` and `core.entity_registry.old.json`: the storage that Home
Assistant ITSELF wrote for the live door (the real unified integration was run in HA's harness; its door rows were then put
back into the pre-fold shape: domain `portico_intercom`, no `entry_type`, platform `portico_intercom`). The door is the
live shape: entry id 01M5A7DXY2YXMJYQ0HZ9FXNWSS, title "Portico (gate_sip_observer)", data keys
device_id / driver / existing_camera_entity / gate_sip_listen / gate_sip_url / interval / open_relay_action, owning exactly
`sensor.call_state` and `sensor.listen_leg` (unique ids `<entry id>_call_state` and `_listen_leg`). Beside it: a push entry,
a phone entry, an entry of another integration, and their registry rows, which must come out byte-for-byte as they went in.
Every value is fake of the same shape.
"""
from __future__ import annotations

import copy
import fcntl
import importlib.util
import io
import json
import os
import socket
import time
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "unify"
SCRIPT = Path(__file__).resolve().parent.parent.parent.parent / "scripts" / "migrate_unify.py"
DOOR_ID = "01M5A7DXY2YXMJYQ0HZ9FXNWSS"
PEER_TOKEN = "FAKE-peer-token-0123456789"
PEER_KEY = f"portico.gate_sip_peer.{DOOR_ID}"


def _load_script():
    spec = importlib.util.spec_from_file_location("migrate_unify", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mig = _load_script()


# ---- a config directory in the old shape --------------------------------------------------------------------------


def _write_fixture(config: Path, *, entries=None, registry=None, age: float = 3600.0) -> None:
    storage = config / ".storage"
    (storage / "portico_intercom").mkdir(parents=True, exist_ok=True)
    texts = {
        "core.config_entries": entries if entries is not None else (FIXTURES / "core.config_entries.old.json").read_text("utf-8"),
        "core.entity_registry": registry if registry is not None else (FIXTURES / "core.entity_registry.old.json").read_text("utf-8"),
    }
    for name, text in texts.items():
        (storage / name).write_text(text, encoding="utf-8")
    (storage / PEER_KEY).write_text(json.dumps({"version": 1, "minor_version": 1, "key": PEER_KEY,
                                                "data": {"peer_token": PEER_TOKEN}}, indent=2), encoding="utf-8")
    (storage / "portico_intercom" / "notes.txt").write_text("a file the hub dir may hold", encoding="utf-8")
    _age(config, age)


def _age(config: Path, seconds: float) -> None:
    then = time.time() - seconds
    for name in ("core.config_entries", "core.entity_registry"):
        os.utime(config / ".storage" / name, (then, then))


def _backup(config: Path, target: Path) -> Path:
    """What the runbook's `cp` does: the four items, flat, into a dated folder."""
    target.mkdir(parents=True, exist_ok=True)
    storage = config / ".storage"
    for name in ("core.config_entries", "core.entity_registry", PEER_KEY):
        (target / name).write_bytes((storage / name).read_bytes())
    (target / "portico_intercom").mkdir(exist_ok=True)
    for item in (storage / "portico_intercom").iterdir():
        if item.is_file():
            (target / "portico_intercom" / item.name).write_bytes(item.read_bytes())
    return target


@pytest.fixture
def config(tmp_path):
    root = tmp_path / "config"
    _write_fixture(root)
    return root


@pytest.fixture
def backup(config, tmp_path):
    return _backup(config, tmp_path / "backup-2026-10-24")


def _run(config: Path, *extra: str, backup: Path | None = None, quiet: str | None = None):
    args = ["--config", str(config), *extra]
    if backup is not None:
        args += ["--backup-dir", str(backup)]
    if quiet is not None:
        args += ["--quiet-seconds", quiet]
    out, err = io.StringIO(), io.StringIO()
    import contextlib

    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = mig.main(args)
    return code, out.getvalue(), err.getvalue()


def _read(config: Path, name: str) -> dict:
    return json.loads((config / ".storage" / name).read_text("utf-8"))


def _snapshot(config: Path) -> dict[str, tuple[bytes, float]]:
    out = {}
    for path in sorted((config / ".storage").rglob("*")):
        if path.is_file():
            out[str(path.relative_to(config))] = (path.read_bytes(), path.stat().st_mtime)
    return out


def _entry(doc: dict, entry_id: str) -> dict:
    return next(e for e in doc["data"]["entries"] if e["entry_id"] == entry_id)


# ---- the fixtures are what they claim -------------------------------------------------------------------------------


def test_the_fixture_is_the_live_door_shape(config):
    entries = _read(config, "core.config_entries")
    door = _entry(entries, DOOR_ID)
    assert door["domain"] == "portico_intercom" and door["title"] == "Portico (gate_sip_observer)"
    assert sorted(door["data"]) == ["device_id", "driver", "existing_camera_entity", "gate_sip_listen", "gate_sip_url",
                                    "interval", "open_relay_action"]
    registry = _read(config, "core.entity_registry")
    rows = [r for r in registry["data"]["entities"] if r["config_entry_id"] == DOOR_ID]
    assert sorted((r["entity_id"], r["unique_id"], r["platform"]) for r in rows) == [
        ("sensor.call_state", f"{DOOR_ID}_call_state", "portico_intercom"),
        ("sensor.listen_leg", f"{DOOR_ID}_listen_leg", "portico_intercom")]
    domains = sorted(e["domain"] for e in entries["data"]["entries"])
    assert domains == ["mqtt", "portico", "portico", "portico_intercom"]          # beside the door: push, phone, a foreign one


# ---- the migration ----------------------------------------------------------------------------------------------------


def test_the_door_entry_changes_domain_and_gets_its_kind_and_keeps_everything_else(config, backup):
    before = _read(config, "core.config_entries")
    code, out, err = _run(config, backup=backup)
    assert code == 0 and err == "", (out, err)
    after = _read(config, "core.config_entries")
    old, new = _entry(before, DOOR_ID), _entry(after, DOOR_ID)
    assert new["domain"] == "portico" and new["data"]["entry_type"] == "door"
    assert list(new["data"])[0] == "entry_type"
    assert {k: v for k, v in new["data"].items() if k != "entry_type"} == old["data"]
    assert {k: v for k, v in new.items() if k not in ("domain", "data")} == {k: v for k, v in old.items() if k not in ("domain", "data")}
    assert new["entry_id"] == DOOR_ID and new["unique_id"] == old["unique_id"] and new["options"] == old["options"]
    assert [e["entry_id"] for e in after["data"]["entries"]] == [e["entry_id"] for e in before["data"]["entries"]]


def test_the_registry_rows_of_the_door_change_platform_and_nothing_else(config, backup):
    before = _read(config, "core.entity_registry")
    assert _run(config, backup=backup)[0] == 0
    after = _read(config, "core.entity_registry")
    assert len(after["data"]["entities"]) == len(before["data"]["entities"])
    changed = []
    for old, new in zip(before["data"]["entities"], after["data"]["entities"]):
        if old != new:
            changed.append(new["entity_id"])
            assert old["platform"] == "portico_intercom" and new["platform"] == "portico"
            assert {k: v for k, v in old.items() if k != "platform"} == {k: v for k, v in new.items() if k != "platform"}
    assert sorted(changed) == ["sensor.call_state", "sensor.listen_leg"]
    ids = {r["entity_id"]: r["unique_id"] for r in after["data"]["entities"]}
    assert ids["sensor.call_state"] == f"{DOOR_ID}_call_state" and ids["sensor.listen_leg"] == f"{DOOR_ID}_listen_leg"
    assert not any(i.endswith("_2") for i in ids)


def test_push_phone_and_foreign_entries_and_rows_are_untouched(config, backup):
    before_e, before_r = _read(config, "core.config_entries"), _read(config, "core.entity_registry")
    assert _run(config, backup=backup)[0] == 0
    after_e, after_r = _read(config, "core.config_entries"), _read(config, "core.entity_registry")
    door_row_ids = {r["id"] for r in before_r["data"]["entities"] if r["config_entry_id"] == DOOR_ID}
    for entry in before_e["data"]["entries"]:
        if entry["entry_id"] != DOOR_ID:
            assert _entry(after_e, entry["entry_id"]) == entry, entry["domain"]
    other_before = [r for r in before_r["data"]["entities"] if r["id"] not in door_row_ids]
    other_after = [r for r in after_r["data"]["entities"] if r["id"] not in door_row_ids]
    assert other_before == other_after and len(other_before) >= 3
    assert {k: v for k, v in before_r.items() if k != "data"} == {k: v for k, v in after_r.items() if k != "data"}


def test_the_peer_token_store_and_the_hub_folder_are_not_touched(config, backup):
    store = config / ".storage" / PEER_KEY
    hub_file = config / ".storage" / "portico_intercom" / "notes.txt"
    before = (store.read_bytes(), hub_file.read_bytes())
    code, out, _ = _run(config, backup=backup)
    assert code == 0
    assert (store.read_bytes(), hub_file.read_bytes()) == before
    assert f"peer-token Store {PEER_KEY}: present" in out and "keeps its name" in out
    assert not [p for p in (config / ".storage").iterdir() if "migrate-tmp" in p.name]      # no temporary file left


def test_the_output_is_written_the_way_home_assistant_writes_storage(config, backup):
    """orjson, two-space indent, UTF-8, no trailing newline: the same bytes HA's own writer would produce."""
    orjson = pytest.importorskip("orjson")
    assert _run(config, backup=backup)[0] == 0
    for name in ("core.config_entries", "core.entity_registry"):
        raw = (config / ".storage" / name).read_bytes()
        assert raw == orjson.dumps(json.loads(raw), option=orjson.OPT_INDENT_2 | orjson.OPT_NON_STR_KEYS), name
        assert not raw.endswith(b"\n")


def test_the_file_mode_is_kept(config, backup):
    path = config / ".storage" / "core.config_entries"
    os.chmod(path, 0o600)
    _age(config, 3600)
    assert _run(config, backup=_backup(config, backup.parent / "b2"))[0] == 0
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_a_deleted_entity_row_of_the_door_moves_too(config, backup, tmp_path):
    registry = _read(config, "core.entity_registry")
    registry["data"].setdefault("deleted_entities", []).append(
        {"config_entry_id": DOOR_ID, "entity_id": "sensor.old_listen", "platform": "portico_intercom",
         "unique_id": f"{DOOR_ID}_listen_leg_old", "id": "deadbeef", "orphaned_timestamp": None})
    (config / ".storage" / "core.entity_registry").write_text(json.dumps(registry, indent=2, ensure_ascii=False))
    _age(config, 3600)
    assert _run(config, backup=_backup(config, tmp_path / "b3"))[0] == 0
    assert _read(config, "core.entity_registry")["data"]["deleted_entities"][0]["platform"] == "portico"


# ---- idempotence, dry run -----------------------------------------------------------------------------------------------


def test_a_second_run_is_a_no_op(config, backup):
    assert _run(config, backup=backup)[0] == 0
    first = _snapshot(config)
    code, out, err = _run(config, backup=backup)
    assert code == 0 and "nothing to do" in out and err == ""
    assert _snapshot(config) == first                                    # bytes AND mtimes: not even rewritten


def test_a_second_run_needs_no_backup_and_does_not_check_for_a_running_ha(config, backup):
    assert _run(config, backup=backup)[0] == 0
    (config / ".storage" / "core.config_entries").touch()                 # "just modified" would refuse a real run
    code, out, _ = _run(config)
    assert code == 0 and "nothing to do" in out


def test_dry_run_changes_nothing_and_says_what_it_would_do(config):
    before = _snapshot(config)
    code, out, err = _run(config, "--dry-run")
    assert code == 0 and err == ""
    assert _snapshot(config) == before                                   # bytes and mtimes
    assert f"entry {DOOR_ID}" in out and "portico_intercom -> portico" in out
    assert "sensor.call_state" in out and "sensor.listen_leg" in out and "dry run: nothing was written" in out


def test_dry_run_needs_no_backup_and_only_warns_about_a_running_ha(config):
    (config / ".HA_RESTORE").write_text("")
    code, out, _ = _run(config, "--dry-run")
    assert code == 0 and "WARNING (a real run would refuse)" in out and ".HA_RESTORE" in out


# ---- the refusals: each leaves every file as it was ---------------------------------------------------------------------


def _refused(config: Path, backup: Path | None, *extra: str, quiet: str | None = None):
    before = _snapshot(config)
    code, out, err = _run(config, *extra, backup=backup, quiet=quiet)
    assert code == 2, (code, out, err)
    assert _snapshot(config) == before
    assert err.startswith("REFUSED")
    return err


def test_refuses_while_home_assistant_holds_the_single_instance_lock(config, backup):
    lock = config / ".ha_run.lock"
    lock.write_text(json.dumps({"pid": 4242, "version": 1, "ha_version": "2026.10.2", "start_ts": 1.0}))
    with open(lock, "a+") as holder:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)         # what a running HA holds
        err = _refused(config, backup)
    assert ".ha_run.lock is locked by a running Home Assistant" in err and "4242" in err
    # released (HA stopped, the file stays behind): the very same directory migrates
    assert _run(config, backup=backup)[0] == 0


def test_a_stale_lock_file_that_nobody_holds_does_not_block(config, backup):
    (config / ".ha_run.lock").write_text(json.dumps({"pid": 1, "start_ts": 1.0}))
    assert _run(config, backup=backup)[0] == 0


def test_refuses_while_the_hub_socket_accepts_connections(config, backup):
    sock_path = config / ".storage" / "portico_intercom" / "hub.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    server.listen(1)
    try:
        err = _refused(config, backup)
        assert "transport hub accepts connections" in err
    finally:
        server.close()
    # the socket file is still there, nobody listens: a stale file left by a crash is not a reason to refuse
    assert sock_path.exists()
    assert _run(config, backup=backup)[0] == 0


@pytest.mark.parametrize("name", ["core.config_entries", "core.entity_registry"])
def test_refuses_when_either_file_was_modified_in_the_last_sixty_seconds(config, backup, name):
    now = time.time() - 5
    os.utime(config / ".storage" / name, (now, now))
    err = _refused(config, backup)
    assert name in err and "modified" in err and "60 s" in err


def test_the_quiet_window_is_a_parameter(config, backup):
    now = time.time() - 5
    os.utime(config / ".storage" / "core.config_entries", (now, now))
    _refused(config, backup, quiet="60")
    assert _run(config, backup=backup, quiet="1")[0] == 0


def test_refuses_with_a_pending_restore(config, backup):
    (config / ".HA_RESTORE").write_text("")
    assert "restore a backup over this migration" in _refused(config, backup)


def test_refuses_without_a_backup_dir(config):
    assert "--backup-dir" in _refused(config, None)


def test_refuses_when_the_backup_dir_is_not_a_directory(config, tmp_path):
    assert "not a directory" in _refused(config, tmp_path / "missing")


@pytest.mark.parametrize("victim", ["core.config_entries", "core.entity_registry", PEER_KEY])
def test_refuses_when_the_backup_lacks_or_differs_from_a_live_file(config, backup, victim):
    (backup / victim).write_text("not the same")
    err = _refused(config, backup)
    assert victim in err
    (backup / victim).unlink()
    assert victim in _refused(config, backup)


def test_refuses_when_the_hub_folder_was_not_copied(config, backup):
    for item in (backup / "portico_intercom").iterdir():
        item.unlink()
    (backup / "portico_intercom").rmdir()
    assert "portico_intercom/ folder" in _refused(config, backup)


def test_refuses_when_a_portico_entry_already_has_the_doors_unique_id(config, backup):
    entries = _read(config, "core.config_entries")
    twin = copy.deepcopy(_entry(entries, DOOR_ID))
    twin.update(domain="portico", entry_id="01M5ZZZZZZZZZZZZZZZZZZZZZZ")
    twin["data"] = {"entry_type": "door", **twin["data"]}
    entries["data"]["entries"].append(twin)
    (config / ".storage" / "core.config_entries").write_text(json.dumps(entries, indent=2, ensure_ascii=False))
    _age(config, 3600)
    backup = _backup(config, backup.parent / "b4")
    err = _refused(config, backup)
    assert "already exists" in err and "duplicate" in err


def test_refuses_when_two_registry_rows_would_share_domain_platform_and_unique_id(config, backup):
    registry = _read(config, "core.entity_registry")
    row = copy.deepcopy(next(r for r in registry["data"]["entities"] if r["entity_id"] == "sensor.call_state"))
    row.update(platform="portico", entity_id="sensor.call_state_2", id="feedface")
    registry["data"]["entities"].append(row)
    (config / ".storage" / "core.entity_registry").write_text(json.dumps(registry, indent=2, ensure_ascii=False))
    _age(config, 3600)
    err = _refused(config, _backup(config, backup.parent / "b5"))
    assert "share (domain, platform, unique id)" in err


# ---- shapes it must not touch or accept -----------------------------------------------------------------------------------


def test_an_old_entry_with_no_driver_is_not_a_door_and_is_left_alone(config, tmp_path):
    entries = _read(config, "core.config_entries")
    strange = copy.deepcopy(_entry(entries, DOOR_ID))
    strange.update(entry_id="01M5STRANGE00000000000000A", unique_id=None)
    strange["data"] = {"something": "else"}
    entries["data"]["entries"].append(strange)
    (config / ".storage" / "core.config_entries").write_text(json.dumps(entries, indent=2, ensure_ascii=False))
    _age(config, 3600)
    code, out, _ = _run(config, backup=_backup(config, tmp_path / "b6"))
    assert code == 0 and "01M5STRANGE00000000000000A" in out and "not a door" in out
    after = _read(config, "core.config_entries")
    assert _entry(after, "01M5STRANGE00000000000000A") == strange
    assert _entry(after, DOOR_ID)["domain"] == "portico"


def test_an_old_entry_that_already_has_another_kind_is_refused(config, backup):
    entries = _read(config, "core.config_entries")
    _entry(entries, DOOR_ID)["data"]["entry_type"] = "phone"
    (config / ".storage" / "core.config_entries").write_text(json.dumps(entries, indent=2, ensure_ascii=False))
    _age(config, 3600)
    assert "refusing to guess" in _refused(config, _backup(config, backup.parent / "b7"))


def test_a_device_row_for_the_door_is_reported_not_touched(config, backup):
    devices = {"version": 1, "minor_version": 11, "key": "core.device_registry",
               "data": {"devices": [{"id": "dev1", "config_entries": [DOOR_ID], "identifiers": [["portico_intercom", "x"]]}],
                        "deleted_devices": []}}
    path = config / ".storage" / "core.device_registry"
    path.write_text(json.dumps(devices))
    before = path.read_bytes()
    code, out, _ = _run(config, backup=backup)
    assert code == 0 and "device registry row(s) reference" in out
    assert path.read_bytes() == before


@pytest.mark.parametrize("name", ["core.config_entries", "core.entity_registry"])
def test_malformed_input_is_an_error_not_a_write(config, backup, name):
    path = config / ".storage" / name
    for text in ("{not json", json.dumps({"key": "something.else", "data": {}}), json.dumps({"key": name})):
        path.write_text(text)
        _age(config, 3600)
        before = _snapshot(config)
        code, out, err = _run(config, backup=backup)
        assert code == 1 and err.startswith("ERROR"), (text, code, err)
        assert _snapshot(config) == before


def test_a_missing_storage_file_is_an_error(tmp_path):
    (tmp_path / ".storage").mkdir()
    code, _, err = _run(tmp_path, "--dry-run")
    assert code == 1 and "cannot read" in err


# ---- finishing a half-run, and failing safely -----------------------------------------------------------------------------


def test_a_run_cut_short_after_the_registry_is_finished_by_running_again(config, backup, monkeypatch):
    """The registry is written first and the entries last: if the second write fails, the first is put back, so the
    directory is exactly as it was and the next run does the whole job."""
    real = mig._atomic_write
    calls = []

    def flaky(path, data, like):
        calls.append(os.path.basename(path))
        if len(calls) == 2:
            raise OSError("disk full")
        return real(path, data, like)

    before = _snapshot(config)
    monkeypatch.setattr(mig, "_atomic_write", flaky)
    code, _, err = _run(config, backup=backup)
    monkeypatch.undo()
    assert code == 1 and "original files were put back" in err
    assert calls[:2] == ["core.entity_registry", "core.config_entries"]
    after = _snapshot(config)
    assert {k: v[0] for k, v in after.items()} == {k: v[0] for k, v in before.items()}      # bytes restored
    assert not [p for p in (config / ".storage").iterdir() if "migrate-tmp" in p.name]
    _age(config, 3600)
    assert _run(config, backup=backup)[0] == 0


def test_a_half_migrated_state_is_finished(config, backup):
    """Entries already moved to portico/door but the registry rows still platform portico_intercom (the other order of a
    cut-short run, or a hand edit): running again moves the rows."""
    entries = _read(config, "core.config_entries")
    door = _entry(entries, DOOR_ID)
    door["domain"], door["data"] = "portico", {"entry_type": "door", **door["data"]}
    (config / ".storage" / "core.config_entries").write_text(json.dumps(entries, indent=2, ensure_ascii=False))
    _age(config, 3600)
    code, out, _ = _run(config, backup=_backup(config, backup.parent / "b8"))
    assert code == 0 and "nothing to do" not in out
    rows = [r for r in _read(config, "core.entity_registry")["data"]["entities"] if r["config_entry_id"] == DOOR_ID]
    assert {r["platform"] for r in rows} == {"portico"}


def test_a_registry_row_that_belongs_to_no_door_is_left_and_reported(config, backup, tmp_path):
    registry = _read(config, "core.entity_registry")
    stray = copy.deepcopy(next(r for r in registry["data"]["entities"] if r["entity_id"] == "sensor.call_state"))
    stray.update(entity_id="sensor.stray", unique_id="01M5NOSUCHENTRY_call_state", config_entry_id="01M5NOSUCHENTRY", id="c0ffee")
    registry["data"]["entities"].append(stray)
    (config / ".storage" / "core.entity_registry").write_text(json.dumps(registry, indent=2, ensure_ascii=False))
    _age(config, 3600)
    code, out, _ = _run(config, backup=_backup(config, tmp_path / "b9"))
    assert code == 0 and "sensor.stray" in out and "left as it is" in out
    after = _read(config, "core.entity_registry")
    assert next(r for r in after["data"]["entities"] if r["entity_id"] == "sensor.stray")["platform"] == "portico_intercom"
