"""DEV-824 P6: the non-secret options of the door and phone config entries as `doors/` and `phones/` YAML files.

Pure part (config_yaml): layout, round trip, the validators, and that no secret and no `open_relay_action` can be written. HA part
(config_options, config_apply, config_sync): the export carries no secret; an apply changes only the keys the files name, writes what
each entry's own form writes, reloads a door only for `dial_enabled`, never during a call (the whole reload is DEFERRED, nothing applied,
the tree not adopted), is refused with `file:line:col` for every unknown door, phone or helper, and is undone if a later write fails.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .. import config_apply, config_options, config_sync, config_yaml as cy
from ..config_flow import DOMAIN
from ..phone_entry import PhoneRecord
from .test_config_apply import PHONE_ID, _page, _with_page, world  # noqa: F401 - the world fixture, by name
from .test_push_registry import _custom_integrations  # noqa: F401 - autouse fixture by name

DOOR_KEY = "gate-1"
SECRET = "hunter2-sip"
ACTION = [{"action": "script.turn_on", "target": {"entity_id": "script.open_gate"}}]


# ---- the pure part -------------------------------------------------------------------------------------------------------------

DOORS = {"gate-1": {"name": "Gate", "driver": "gate_sip_server", "dial_enabled": True, "house_bridge": False,
                    "ring_answered_entity": "input_boolean.gate_taken"},
         "10.3.0.9": {"name": "Front", "driver": "dahua_sip"}}
PHONES = {"enr-1": {"name": "Alice's phone", "doors": ["gate-1"], "house_receive": True, "panel": False},
          "enr-2": {"name": "Kitchen", "doors": None, "house_receive": False, "panel": True}}


_BASE = {"schema_version": 6, "pages": [], "devices": {}, "dashboards": {}, "endpoints": {}}


def _files(doors=None, phones=None):
    return cy.to_files(_BASE, [], {}, doors, phones)


def test_doors_and_phones_round_trip_to_byte_identical_files():
    files = _files(DOORS, PHONES)
    assert {"doors/gate-1.yaml", "doors/10.3.0.9.yaml", "phones/enr-1.yaml", "phones/enr-2.yaml"} <= set(files)
    result = cy.validate_files(files)
    assert result.ok, result.errors
    assert result.doors == DOORS and result.phones == PHONES
    assert result.counts["doors"] == 2 and result.counts["phones"] == 2
    again = cy.to_files(result.document, result.elements, result.settings, result.doors, result.phones)
    assert again == files                                   # a fixed point: no diff, so no write and no event


def test_a_tree_without_doors_or_phones_is_unchanged_by_p6():
    files = _files()
    assert not [p for p in files if p.startswith(("doors/", "phones/"))]
    result = cy.validate_files(files)
    assert result.ok and result.doors == {} and result.phones == {}


def _refused(files):
    result = cy.validate_files(files)
    assert not result.ok
    return result.errors


@pytest.mark.parametrize("path, text, code", [
    ("doors/gate-1.yaml", "id: gate-1\ndriver: gate_sip_server\nsip_password: x\n", "secret_in_yaml"),
    ("doors/gate-1.yaml", "id: gate-1\ndriver: gate_sip_server\nopen_relay_action: []\n", "forbidden_key"),
    ("doors/gate-1.yaml", "id: gate-1\ndriver: gate_sip_server\nhost: 10.0.0.1\n", "unknown_key"),
    ("doors/gate-1.yaml", "id: gate-1\ndriver: gate_sip_server\ndial_enabled: yes please\n", "invalid_value"),
    ("doors/gate-1.yaml", "id: gate-1\ndriver: gate_sip_server\nring_answered_entity: switch.gate\n", "invalid_value"),
    ("doors/gate-1.yaml", "id: gate-1\nname: Gate\n", "missing_key"),
    ("doors/gate-1.yaml", "id: gate-1\ndriver: dahua_sip\ndial_enabled: true\n", "not_editable"),
    ("doors/other.yaml", "id: gate-1\ndriver: gate_sip_server\n", "name_mismatch"),
    ("phones/enr-1.yaml", "id: enr-1\nremote_token: x\n", "secret_in_yaml"),
    ("phones/enr-1.yaml", "id: enr-1\ntailscale_node: n\n", "unknown_key"),
    ("phones/enr-1.yaml", "id: enr-1\ndoors: gate-1\n", "invalid_value"),
    ("phones/enr-1.yaml", "id: enr-1\ndoors: [a, a]\n", "duplicate_id"),
    ("phones/enr-1.yaml", "id: enr-1\npanel: true\ndoors: [a]\n", "invalid_value"),
    ("phones/enr-1.yaml", "id: enr-1\nhouse_receive: 1\n", "invalid_value"),
])
def test_a_bad_door_or_phone_file_is_refused_with_its_place(path, text, code):
    files = _files()
    files[path] = text
    errors = _refused(files)
    assert [e.code for e in errors if e.file == path] == [code], errors
    assert all(e.line >= 1 and e.col >= 1 for e in errors)


def test_a_door_or_phone_file_outside_the_layout_is_refused():
    files = _files()
    files["doors/sub/x.yaml"] = "id: x\ndriver: d\n"
    assert [e.code for e in _refused(files)] == ["bad_path"]


def test_the_layout_names_no_secret_even_for_a_door_that_holds_one():
    # the record builders are the only door of live data into a file: they name no key outside the allow-list
    entry = SimpleNamespace(title="Gate", data={"driver": "gate_sip_server", "device_id": "g", "sip_password": SECRET,
                                                 "remote_token": SECRET, "open_relay_action": ACTION, "dial_enabled": True},
                            options={"house_calls": True}, entry_id="e1")
    record = config_options.door_record(entry)
    assert set(record) <= {"name", "driver", "dial_enabled", "ring_answered_entity", "house_bridge"}
    assert SECRET not in cy.dump_yaml(record) and "script.open_gate" not in cy.dump_yaml(record)


# ---- the live part -------------------------------------------------------------------------------------------------------------

def _door_entry(world, **data):
    base = {"entry_type": "door", "driver": "gate_sip_server", "device_id": DOOR_KEY, "dial_enabled": False,
            "sip_password": SECRET, "remote_token": SECRET, "open_relay_action": ACTION}
    entry = MockConfigEntry(domain=DOMAIN, title="Gate", unique_id=f"gate_sip_server:{DOOR_KEY}", data={**base, **data})
    entry.add_to_hass(world.hass)
    return entry


def _phone(world, key="enr-1", **options):
    record = PhoneRecord(ha_user_id=f"u-{key}", name="Alice's phone", enroll_id=key)
    entry = MockConfigEntry(domain=DOMAIN, title="Alice's phone", unique_id=f"phone:{key}", data=record.to_data(), options=options)
    entry.add_to_hass(world.hass)
    return entry


@pytest.fixture
def house(world, monkeypatch):
    world.door = _door_entry(world)
    world.phone = _phone(world)
    world.hass.states.async_set("input_boolean.gate_taken", "off", {})
    world.reloaded = AsyncMock()
    monkeypatch.setattr(world.hass.config_entries, "async_reload", world.reloaded)
    monkeypatch.setattr(config_options, "read_installed_wheel_version", lambda: "0.6.10")
    return world


def _tree(world):
    return config_sync.snapshot(world.hass)[0]


def _edit(files, path, **changes):
    data = cy.load_yaml(files[path])[0]
    data.update(changes)
    files = dict(files)
    files[path] = cy.dump_yaml(data)
    return files


async def _reload(world, files, *, dry_run=False):
    return await config_apply.reload_config(world.hass, files, [], dry_run=dry_run)


def _calls(world, state):
    world.hass.data[DOMAIN]["call_events"] = SimpleNamespace(state=state)


async def test_the_export_carries_the_options_and_none_of_the_secrets(house):
    files = _tree(house)
    assert "doors/gate-1.yaml" in files and "phones/enr-1.yaml" in files
    door = cy.load_yaml(files["doors/gate-1.yaml"])[0]
    assert door == {"id": "gate-1", "name": "Gate", "driver": "gate_sip_server", "dial_enabled": False, "house_bridge": False,
                    "ring_answered_entity": None}
    phone = cy.load_yaml(files["phones/enr-1.yaml"])[0]
    assert phone == {"id": "enr-1", "name": "Alice's phone", "doors": None, "house_receive": False, "panel": False}
    text = "\n".join(files.values())
    assert SECRET not in text and "script.open_gate" not in text and "open_relay_action" not in text and "u-enr-1" not in text
    reply = await _reload(house, files)                     # and the tree reads back as no change at all
    assert reply["ok"] and reply["changed"] is False and reply["deferred"] is False and reply["report"]["entries"] == {"doors": [], "phones": []}


async def test_a_door_edit_changes_only_the_named_keys_and_reloads_only_for_dial(house):
    files = _edit(_tree(house), "doors/gate-1.yaml", dial_enabled=True, ring_answered_entity="input_boolean.gate_taken", house_bridge=True)
    before = dict(house.door.data)
    reply = await _reload(house, files)
    assert reply["ok"] and reply["applied"] and reply["deferred"] is False, reply
    data = dict(house.door.data)
    assert data["dial_enabled"] is True and data["gate_sip_listen"] == {"ring_answered_entity": "input_boolean.gate_taken"}
    assert {k: v for k, v in data.items() if k not in ("dial_enabled", "gate_sip_listen")} == {k: v for k, v in before.items() if k != "dial_enabled"}
    assert data["open_relay_action"] == ACTION and data["sip_password"] == SECRET        # never touched
    assert house.door.options == {"house_calls": True}                                  # the hub's option, stored only when on
    house.reloaded.assert_awaited_once_with(house.door.entry_id)
    kinds = [(s["kind"], s["option"]) for s in reply["report"]["sensitive"] if s["kind"] == "door_option"]
    assert sorted(kinds) == [("door_option", "dial_enabled"), ("door_option", "house_bridge"), ("door_option", "ring_answered_entity")]
    again = await _reload(house, _tree(house))
    assert again["changed"] is False and house.reloaded.await_count == 1


async def test_the_house_bridge_alone_never_reloads_the_door_and_switching_it_off_removes_the_option(house):
    reply = await _reload(house, _edit(_tree(house), "doors/gate-1.yaml", house_bridge=True))
    assert reply["applied"] and house.door.options == {"house_calls": True}
    reply = await _reload(house, _edit(_tree(house), "doors/gate-1.yaml", house_bridge=False))
    assert reply["applied"] and house.door.options == {}
    house.reloaded.assert_not_awaited()


async def test_a_phone_edit_writes_its_options_and_announces(house):
    house.phone_before = dict(house.phone.data)
    files = _edit(_tree(house), "phones/enr-1.yaml", doors=["gate-1"], house_receive=True)
    reply = await _reload(house, files)
    assert reply["ok"] and reply["applied"], reply["errors"]
    assert house.phone.options == {"doors": ["gate-1"], "house_receive": True}
    assert dict(house.phone.data) == house.phone_before                                 # identity untouched
    assert house.announced
    assert {s["kind"] for s in reply["report"]["sensitive"]} >= {"phone_doors", "phone_receive"}
    reply = await _reload(house, _edit(_tree(house), "phones/enr-1.yaml", doors=None))
    assert reply["applied"] and "doors" not in house.phone.options                      # null = every door again


async def test_a_dry_run_reports_the_entries_and_writes_nothing(house):
    files = _edit(_edit(_tree(house), "doors/gate-1.yaml", dial_enabled=True), "phones/enr-1.yaml", house_receive=True)
    before = (dict(house.door.data), dict(house.door.options), dict(house.phone.options))
    reply = await _reload(house, files, dry_run=True)
    assert reply["ok"] and reply["dry_run"] and reply["applied"] is False and reply["changed"]
    assert (dict(house.door.data), dict(house.door.options), dict(house.phone.options)) == before
    entries = reply["report"]["entries"]
    assert entries["doors"][0]["changes"] == {"dial_enabled": {"before": False, "after": True}} and entries["doors"][0]["reloads_entry"]
    assert entries["phones"][0]["changes"] == {"house_receive": {"before": False, "after": True}}
    house.reloaded.assert_not_awaited()


async def test_nothing_is_applied_while_a_call_is_live_and_the_tree_stays_pending(house):
    _calls(house, "talking")
    files = _with_page(_edit(_tree(house), "doors/gate-1.yaml", dial_enabled=True), _page("yard", "Yard"), device=PHONE_ID)
    rev, hashes = house.content.doc_rev, dict(config_sync._state(house.hass).hashes or {})
    dry = await _reload(house, files, dry_run=True)
    assert dry["ok"] and dry["deferred"] is False and dry["report"]["deferred_until_idle"] is True
    reply = await _reload(house, files)
    assert reply["ok"] and reply["applied"] is False and reply["deferred"] is True and reply["changed"]
    assert [w["code"] for w in reply["warnings"]] == ["deferred"]
    assert house.content.doc_rev == rev and "yard" not in [p["id"] for p in house.content.library]     # all or nothing
    assert house.door.data["dial_enabled"] is False and house.reloaded.await_count == 0
    assert (config_sync._state(house.hass).hashes or {}) == hashes                    # not adopted: still a pending edit
    _calls(house, "idle")
    done = await _reload(house, files)                                                  # the same call, once idle, applies it all
    assert done["applied"] and done["deferred"] is False
    assert "yard" in [p["id"] for p in house.content.library] and house.door.data["dial_enabled"] is True


async def test_a_live_call_does_not_hold_back_an_edit_that_touches_no_entry(house):
    _calls(house, "ringing")
    reply = await _reload(house, _with_page(_tree(house), _page("yard", "Yard"), device=PHONE_ID))
    assert reply["applied"] and reply["deferred"] is False and reply["report"]["deferred_until_idle"] is False


async def test_a_door_or_phone_that_does_not_exist_is_refused_and_nothing_is_applied(house):
    files = dict(_tree(house))
    files["doors/ghost.yaml"] = "id: ghost\ndriver: gate_sip_server\ndial_enabled: true\n"
    files["phones/ghost.yaml"] = "id: ghost\nhouse_receive: true\n"
    reply = await _reload(house, _with_page(files, _page("yard", "Yard"), device=PHONE_ID))
    assert not reply["ok"] and not reply["applied"]
    assert {(e["file"], e["code"]) for e in reply["errors"]} == {("doors/ghost.yaml", "unknown_door"), ("phones/ghost.yaml", "unknown_phone")}
    assert all(e["line"] >= 1 for e in reply["errors"]) and "yard" not in [p["id"] for p in house.content.library]


@pytest.mark.parametrize("path, change, code", [
    ("doors/gate-1.yaml", {"driver": "dahua_sip", "dial_enabled": None}, "driver_readonly"),
    ("doors/gate-1.yaml", {"ring_answered_entity": "input_boolean.nope"}, "unknown_entity"),
    ("phones/enr-1.yaml", {"panel": True}, "panel_readonly"),
    ("phones/enr-1.yaml", {"doors": ["nope"]}, "unknown_door"),
])
async def test_the_live_checks_refuse_with_file_and_code(house, path, change, code):
    files = _edit(_tree(house), path, **change)
    if change.get("dial_enabled", 0) is None:                 # a door of another driver carries no settings: the file names the driver only
        files[path] = "id: gate-1\ndriver: dahua_sip\n"
    reply = await _reload(house, files)
    assert not reply["ok"] and [(e["file"], e["code"]) for e in reply["errors"]] == [(path, code)]
    assert dict(house.door.data)["dial_enabled"] is False and house.reloaded.await_count == 0


async def test_the_bridge_needs_a_wheel_that_has_it(house, monkeypatch):
    monkeypatch.setattr(config_options, "read_installed_wheel_version", lambda: "0.5.9")
    reply = await _reload(house, _edit(_tree(house), "doors/gate-1.yaml", house_bridge=True))
    assert [e["code"] for e in reply["errors"]] == ["house_needs_wheel"] and house.door.options == {}
    off = await _reload(house, _edit(_tree(house), "doors/gate-1.yaml", house_bridge=False))
    assert off["ok"] and off["changed"] is False


async def test_a_key_the_file_leaves_out_leaves_the_live_value_alone(house):
    house.hass.config_entries.async_update_entry(house.door, data={**house.door.data, "dial_enabled": True})
    house.hass.config_entries.async_update_entry(house.phone, options={"house_receive": True, "doors": ["gate-1"]})
    files = dict(_tree(house))
    files["doors/gate-1.yaml"] = "id: gate-1\ndriver: gate_sip_server\n"
    files["phones/enr-1.yaml"] = "id: enr-1\n"
    reply = await _reload(house, files)
    assert reply["ok"] and reply["changed"] is False
    assert house.door.data["dial_enabled"] is True and house.phone.options == {"house_receive": True, "doors": ["gate-1"]}
    cleared = await _reload(house, _edit(files, "doors/gate-1.yaml", dial_enabled=False))
    assert cleared["applied"] and "dial_enabled" not in house.door.data


async def test_a_door_or_phone_with_no_file_is_left_alone(house):
    files = {p: t for p, t in _tree(house).items() if not p.startswith(("doors/", "phones/"))}
    reply = await _reload(house, files)
    assert reply["ok"] and reply["changed"] is False


async def test_a_failed_write_puts_the_entries_back_and_reloads_nothing(house, monkeypatch):
    files = _edit(_edit(_tree(house), "doors/gate-1.yaml", dial_enabled=True), "phones/enr-1.yaml", house_receive=True)
    before = (dict(house.door.data), dict(house.door.options), dict(house.phone.options))

    async def boom(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(house.elements, "write_all", boom)
    with pytest.raises(OSError):
        await _reload(house, _with_page(files, _page("yard", "Yard"), device=PHONE_ID))
    assert (dict(house.door.data), dict(house.door.options), dict(house.phone.options)) == before
    house.reloaded.assert_not_awaited()


async def test_a_door_that_will_not_reload_is_a_warning_and_the_value_stays_saved(house):
    house.reloaded.side_effect = RuntimeError("transport down")
    reply = await _reload(house, _edit(_tree(house), "doors/gate-1.yaml", dial_enabled=True))
    assert reply["ok"] and reply["applied"] and house.door.data["dial_enabled"] is True
    assert [w["code"] for w in reply["warnings"]] == ["reload_failed"]


async def test_a_change_to_a_door_or_phone_entry_schedules_the_files_rewrite(house, monkeypatch):
    seen = []
    monkeypatch.setattr(config_sync, "async_schedule", lambda hass: seen.append(1))
    config_sync.async_setup_config_sync(house.hass)
    house.hass.config_entries.async_update_entry(house.phone, options={"house_receive": True})
    await house.hass.async_block_till_done()
    assert seen
    seen.clear()
    house.hass.config_entries.async_update_entry(house.settings, options={**house.settings.options, "ring_window": 50})
    await house.hass.async_block_till_done()
    assert seen == []                                       # the settings entry has its own path (ui_settings)
