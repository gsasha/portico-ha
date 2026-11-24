"""DEV-824 P2: `portico.reload_config` (config_apply.py, config_service.py): the YAML tree applied to the live stores, all or nothing.

HA-harness session. Pinned: a clean round trip changes nothing; an edit is applied to both stores and announced; a dry run reports
and writes nothing; every live check (door, phone, entity, name, element reference, user, test phone) refuses with `file:line:col`
and nothing is applied; a failed write puts both stores back; the apply never fires `portico_config_changed`; the Q5 ids; and the
exact secret-key rule.
"""
from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .. import config_apply, config_yaml as cy
from ..config_flow import DOMAIN
from ..config_service import async_setup_config_service, read_tree
from ..content import ContentStore, SaveConflict, get_content_store
from ..elements import ElementError, get_elements_store, validate_element
from ..push import const as push_const
from ..ui_settings import KEYS, facts
from .test_endpoints import _phone_entry, _provide
from .test_push_registry import _custom_integrations  # noqa: F401 - autouse fixture by name

pytestmark = pytest.mark.asyncio

PHONE_ID = "ep-alice"


def _action(action_id, kind="lock_open", **extra):
    return {"id": action_id, "label": action_id, "kind": kind, "entity_id": "lock.gate", **extra}


def _page(page_id="home", label="Home", actions=None):
    return {"id": page_id, "label": label, "kind": "content",
            "blocks": [{"id": "b", "title": "Home", "cameras": [], "actions": actions if actions is not None else [_action("open_gate")]}]}


class _Registry:
    def __init__(self, ids=("reg-1",)):
        self.ids = set(ids)

    def get(self, reg_id):
        return {"reg_id": reg_id} if reg_id in self.ids else None

    def rows(self):
        return [{"reg_id": r, "name": "Pixel", "push_enabled": False} for r in sorted(self.ids)]


class _Manager:
    def __init__(self):
        self.registry = _Registry()
        self.test_allow_list = []


@pytest.fixture
async def world(hass, hass_storage, tmp_path):
    _phone_entry(hass, [{"ha_user_id": "u-alice", "name": "Alice's phone", "enroll_id": PHONE_ID}])
    _provide(hass, "fake", "gate-1", name="Gate", camera_entity_id="camera.gate")
    for entity in ("camera.gate", "camera.yard"):
        hass.states.async_set(entity, "idle", {"friendly_name": entity})
    hass.states.async_set("lock.gate", "locked", {"friendly_name": "Gate lock"})
    settings = MockConfigEntry(domain=DOMAIN, title="Portico", data={push_const.ENTRY_TYPE: push_const.ENTRY_TYPE_PUSH},
                               options={push_const.CONF_ENABLED: True, push_const.CONF_RING_WINDOW: 45,
                                        push_const.CONF_NEW_PHONES_ENABLED: False, push_const.CONF_TEST_PHONES: []})
    settings.add_to_hass(hass)
    manager = _Manager()
    hass.data.setdefault("portico", {})[push_const.DATA_PUSH] = manager
    hass_storage["portico.content"] = {"version": 1, "minor_version": 1, "key": "portico.content", "data": {
        "schema_version": 6, "pages": [_page("home")], "devices": {PHONE_ID: {"pages": [{"page": "home"}], "ring": {}, "roster_hide": []}},
        "dashboards": {}, "endpoints": {}, "home_name": "My Home"}}
    content = ContentStore(hass)
    hass.data["portico"]["content"] = content
    await content.async_ensure_loaded()
    elements = get_elements_store(hass)
    await elements.async_ensure_loaded()
    talk = (await elements.async_set({"kind": "talk", "name": "Gate", "options": {"door": "gate-1"}}, elements.rev))["element"]["id"]
    await elements.async_set({"kind": "camera", "name": "Gate cam", "entity_id": "camera.gate"}, elements.rev)
    events = []
    hass.bus.async_listen("portico_config_changed", lambda e: events.append(e))
    announced = []
    content.add_home_listener(lambda: announced.append(1))
    return SimpleNamespace(hass=hass, storage=hass_storage, content=content, elements=elements, talk=talk, settings=settings,
                           manager=manager, events=events, announced=announced, root=tmp_path / "portico")


def _tree(world):
    """The files of the live configuration, as an agent would find them."""
    return cy.to_files(world.content.document(), [e for e in world.elements.stored() if not e.get("managed")],
                       _settings(world))


def _settings(world):
    f = facts(world.hass)
    return {k: f[k] for k in KEYS if k in f}


async def _reload(world, files, *, dry_run=False):
    return await config_apply.reload_config(world.hass, files, [], dry_run=dry_run)


def _edited(world, **changes):
    """The live tree with file changes: `{path: new text or None to delete}`."""
    files = _tree(world)
    for path, text in changes.items():
        path = path.replace("__", "/")
        if text is None:
            files.pop(path, None)
        else:
            files[path] = text
    return files


def _with_element(files, element):
    files = dict(files)
    path = cy.file_name("elements", element["id"])
    files[path] = cy.dump_yaml(element)
    index = cy.load_yaml(files["index.yaml"])[0]
    if element["id"] not in index["elements"]:
        index["elements"].append(element["id"])
    files["index.yaml"] = cy.dump_yaml(index)
    return files


def _with_page(files, page, *, device=None):
    files = dict(files)
    files[cy.file_name("pages", page["id"])] = cy.dump_yaml(page)
    index = cy.load_yaml(files["index.yaml"])[0]
    index["pages"].append(page["id"])
    files["index.yaml"] = cy.dump_yaml(index)
    if device:
        path = cy.file_name("devices", device)
        data = cy.load_yaml(files[path])[0]
        data["pages"].append({"page": page["id"]})
        files[path] = cy.dump_yaml(data)
    return files


CAMERA = {"id": "el-yard-cam", "kind": "camera", "name": "Yard cam", "entity_id": "camera.yard", "options": {}}


# ---- the clean paths -----------------------------------------------------------------------------------------------------------

async def test_the_live_configuration_written_out_and_read_back_is_a_no_op(world):
    before = (world.content.doc_rev, world.elements.rev)
    reply = await _reload(world, _tree(world))
    assert reply["ok"] and reply["errors"] == [] and reply["changed"] is False and reply["applied"] is False
    assert (world.content.doc_rev, world.elements.rev) == before
    assert "portico.content" in world.storage and world.announced == []


async def test_an_edit_is_applied_to_both_stores_written_and_announced_and_fires_no_config_event(world):
    files = _with_page(_with_element(_tree(world), CAMERA), _page("el-yard", "Yard", [_action("open_yard")]), device=PHONE_ID)
    old_rev = world.content.doc_rev
    reply = await _reload(world, files)
    assert reply["ok"] and reply["applied"] and reply["changed"] and reply["doc_rev"] != old_rev
    assert "el-yard-cam" in world.elements.elements_by_id()
    assert [p["id"] for p in world.content.library] == ["home", "el-yard"]
    assert [e["id"] for e in world.storage["portico.elements"]["data"]["elements"]].count("el-yard-cam") == 1
    assert [p["id"] for p in world.storage["portico.content"]["data"]["pages"]] == ["home", "el-yard"]
    assert world.announced                               # every app refetches
    await world.hass.async_block_till_done()
    assert world.events == []                            # the apply never fires portico_config_changed (loop break)
    assert config_apply.HOOK_POINT == "portico_config_changed"
    again = await _reload(world, _tree(world))           # and what it wrote reads back as the same configuration
    assert again["ok"] and again["changed"] is False


async def test_a_dry_run_reports_and_applies_nothing(world):
    files = _with_page(_with_element(_tree(world), CAMERA), _page("yard", "Yard"), device=PHONE_ID)
    files["portico.yaml"] = files["portico.yaml"].replace("window: 45", "window: 60").replace("enabled: true", "enabled: false")
    before = (world.content.doc_rev, world.elements.rev, dict(world.settings.options))
    reply = await _reload(world, files, dry_run=True)
    assert reply["ok"] and reply["dry_run"] and reply["applied"] is False and reply["changed"]
    assert (world.content.doc_rev, world.elements.rev, dict(world.settings.options)) == before
    report = reply["report"]
    assert [e["id"] for e in report["elements"]["added"]] == ["el-yard-cam"]
    assert [p["id"] for p in report["pages"]["added"]] == ["yard"]
    alice = next(r for r in report["per_device"] if r["endpoint"] == PHONE_ID)
    assert alice["changed"] and alice["rev_before"] != alice["rev_after"] and alice["pages_after"] == ["home", "yard"]
    kinds = {s["kind"] for s in report["sensitive"]}
    assert "ring" in kinds
    assert report["settings"]["changed"] == ["ring_enabled", "ring_window"]


async def test_the_report_lists_deletions_and_a_device_left_with_no_page(world):
    files = _tree(world)
    files.pop("pages/home.yaml")
    index = cy.load_yaml(files["index.yaml"])[0]
    index["pages"] = []
    files["index.yaml"] = cy.dump_yaml(index)
    device = cy.load_yaml(files[cy.file_name("devices", PHONE_ID)])[0]
    device["pages"] = []
    files[cy.file_name("devices", PHONE_ID)] = cy.dump_yaml(device)
    reply = await _reload(world, files, dry_run=True)
    assert reply["ok"], reply["errors"]
    report = reply["report"]
    assert {"kind": "page", "id": "home", "label": "Home"} in report["deletions"]
    assert report["devices_without_page"] == [PHONE_ID]
    assert {"no_page", "deletions"} <= {s["kind"] for s in report["sensitive"]}


async def test_doors_who_is_woken_is_reported_before_and_after(world):
    talk = world.talk
    ring = {"id": "ring-gate", "label": "Ring", "kind": "ring", "talk": talk, "cameras": None, "blocks": [
        {"id": "r", "title": "R", "cameras": [], "actions": [_action("ring_open")]}]}
    files = _with_page(_tree(world), ring)
    reply = await _reload(world, files, dry_run=True)
    assert reply["ok"], reply["errors"]
    assert reply["report"]["doors"] == {}               # a Ring page changes what rings show, not who is woken


async def test_the_settings_are_applied_and_an_unset_live_key_is_cleared(world):
    world.hass.config_entries.async_update_entry(world.settings, options={
        **world.settings.options, "live_rtsp_base": "rtsp://10.3.0.2:8554"})
    files = _tree(world)
    assert "rtsp_base" in files["portico.yaml"]
    files["portico.yaml"] = files["portico.yaml"].replace("window: 45", "window: 90").replace(
        "live:\n  rtsp_base: rtsp://10.3.0.2:8554\n", "")
    reply = await _reload(world, files)
    assert reply["ok"] and reply["applied"], reply["errors"]
    assert world.settings.options[push_const.CONF_RING_WINDOW] == 90
    assert "live_rtsp_base" not in world.settings.options
    assert {"live_rtsp", "ring"} - {s["kind"] for s in reply["report"]["sensitive"]} == {"ring"}


async def test_managed_talk_elements_and_unknown_kinds_are_kept(world):
    stored = world.elements.stored()
    managed = {"id": "talk-phone-ep-alice", "kind": "talk", "name": "Alice's phone", "icon": "phone", "managed": True,
               "options": {"phone": "ep-alice"}}
    await world.elements.async_add_managed([managed])
    reply = await _reload(world, _with_element(_tree(world), CAMERA))
    assert reply["ok"] and reply["applied"], reply["errors"]
    ids = [e["id"] for e in world.elements.stored()]
    assert "talk-phone-ep-alice" in ids and "el-yard-cam" in ids and len(ids) == len(stored) + 2


# ---- refusals, each with file:line:col, nothing applied -------------------------------------------------------------------------

def _one_error(reply, code, path):
    assert not reply["ok"] and reply["applied"] is False
    (err,) = [e for e in reply["errors"] if e["code"] == code]
    assert err["file"] == path and err["line"] >= 1 and err["col"] >= 1 and err["at"] == f"{path}:{err['line']}:{err['col']}"
    return err


async def test_every_live_refusal_names_a_file_and_applies_nothing(world):
    before = (world.content.doc_rev, world.elements.rev)
    base = _tree(world)
    cases = {
        "unknown_entity": ({**CAMERA, "id": "el-ghost", "name": "Ghost", "entity_id": "camera.ghost"}, "elements/el-ghost.yaml"),
        "name_taken": ({**CAMERA, "name": "gate CAM"}, "elements/el-yard-cam.yaml"),
        "invalid": ({"id": "el-no-door", "kind": "talk", "name": "No door", "options": {"door": "nope"}}, "elements/el-no-door.yaml"),
        "unknown_phone": ({"id": "el-no-phone", "kind": "talk", "name": "No phone", "options": {"phone": "nobody"}}, "elements/el-no-phone.yaml"),
    }
    for code, (element, path) in cases.items():
        reply = await _reload(world, _with_element(base, element))
        if code in ("invalid", "unknown_phone"):
            err = [e for e in reply["errors"] if e["file"] == path]
            assert err and not reply["ok"], reply
        else:
            _one_error(reply, code, path)
    assert (world.content.doc_rev, world.elements.rev) == before


async def test_a_changed_kind_and_a_reserved_id_are_refused(world):
    (cam,) = [e for e in world.elements.stored() if e["kind"] == "camera"]
    files = _with_element(_tree(world), {"id": cam["id"], "kind": "status", "name": "Gate cam", "entity_id": "camera.gate", "options": {}})
    _one_error(await _reload(world, files), "kind_locked", cy.file_name("elements", cam["id"]))


async def test_a_page_that_references_an_element_the_files_remove_is_refused(world):
    camera = {"id": "el-yard-cam", "kind": "camera", "name": "Yard cam", "entity_id": "camera.yard", "options": {}}
    ok = _with_element(_tree(world), camera)
    page = _page("home", actions=[_action("open_gate")])
    page["blocks"][0]["cameras"] = [{"id": "yard", "element": "el-yard-cam"}]
    ok[cy.file_name("pages", "home")] = cy.dump_yaml(page)
    first = await _reload(world, ok)
    assert first["applied"], str(first["errors"])
    gone = dict(ok)
    gone.pop(cy.file_name("elements", "el-yard-cam"))
    index = cy.load_yaml(gone["index.yaml"])[0]
    index["elements"].remove("el-yard-cam")
    gone["index.yaml"] = cy.dump_yaml(index)
    reply = await _reload(world, gone)
    assert not reply["ok"] and "element_missing" in reply["errors"][0]["message"]
    assert "el-yard-cam" in world.elements.elements_by_id()


async def test_an_unknown_door_phone_or_target_in_a_page_is_refused_by_the_editors_own_check(world):
    page = _page("home", actions=[{"id": "call", "label": "Call", "kind": "call", "target": {"type": "phone", "id": "nobody"}}])
    reply = await _reload(world, _edited(world, **{"pages__home.yaml": cy.dump_yaml(page)}))
    assert not reply["ok"], reply
    assert reply["errors"][0]["file"] == "pages/home.yaml"


async def test_a_screen_for_a_login_that_does_not_exist_is_refused(world):
    files = _tree(world)
    files[cy.file_name("dashboards", "kitchen")] = cy.dump_yaml(
        {"id": "kitchen", "name": "Kitchen", "ha_user_id": "no-such-user", "cameras": []})
    reply = await _reload(world, files)
    _one_error(reply, "unknown_user", "dashboards/kitchen.yaml")


async def test_a_test_phone_must_be_a_registered_phone(world):
    files = _tree(world)
    files["portico.yaml"] = files["portico.yaml"].replace("test_phones: []", "test_phones:\n  - stranger")
    assert "stranger" in files["portico.yaml"]
    reply = await _reload(world, files)
    _one_error(reply, "unknown_phone", "portico.yaml")
    files["portico.yaml"] = files["portico.yaml"].replace("stranger", "reg-1")
    reply = await _reload(world, files)
    assert reply["ok"] and reply["applied"] and world.settings.options[push_const.CONF_TEST_PHONES] == ["reg-1"]
    assert any(s["kind"] == "test_phones" and s["added"] == ["reg-1"] for s in reply["report"]["sensitive"]) or reply["applied"]


async def test_one_bad_file_applies_none_of_the_good_ones(world):
    files = _with_page(_with_element(_tree(world), CAMERA), _page("yard"), device=PHONE_ID)
    files["pages/yard.yaml"] += "bogus: 1\n"
    before = (world.content.doc_rev, world.elements.rev)
    reply = await _reload(world, files)
    assert not reply["ok"] and (world.content.doc_rev, world.elements.rev) == before
    assert "el-yard-cam" not in world.elements.elements_by_id()


async def test_an_install_on_the_old_model_is_refused(world):
    files = _tree(world)
    world.content._model6 = False
    reply = await _reload(world, files)
    assert not reply["ok"] and reply["errors"][0]["code"] == "not_schema_6"


async def test_a_stale_revision_is_a_conflict_and_a_held_lock_makes_the_apply_wait(world):
    files = _with_element(_tree(world), CAMERA)
    async with world.elements.lock:
        task = asyncio.ensure_future(_reload(world, files))
        await asyncio.sleep(0.05)
        assert not task.done()                          # waits for an element write in progress
    reply = await task
    assert reply["applied"]


# ---- a failed write puts both stores back --------------------------------------------------------------------------------------

async def test_a_failed_content_write_puts_the_elements_and_the_document_back(world, monkeypatch):
    files = _with_page(_with_element(_tree(world), CAMERA), _page("yard"), device=PHONE_ID)
    before = (world.content.document(), world.elements.stored(), copy.deepcopy(world.storage["portico.elements"]))

    async def boom():
        raise OSError("disk full")

    monkeypatch.setattr(world.content, "_save", boom)
    with pytest.raises(OSError):
        await _reload(world, files)
    assert world.content.document() == before[0] and world.elements.stored() == before[1]
    assert world.storage["portico.elements"] == before[2]
    assert world.settings.options[push_const.CONF_RING_WINDOW] == 45 and world.events == []


async def test_a_failed_elements_write_changes_nothing(world, monkeypatch):
    files = _with_element(_tree(world), CAMERA)
    before = (world.content.document(), world.elements.stored())

    async def boom(_elements):
        raise OSError("disk full")

    monkeypatch.setattr(world.elements, "write_all", boom)
    with pytest.raises(OSError):
        await _reload(world, files)
    assert (world.content.document(), world.elements.stored()) == before


# ---- the service ---------------------------------------------------------------------------------------------------------------

async def test_the_service_has_a_response_a_dry_run_and_reads_the_directory(hass, world):
    async_setup_config_service(hass)
    root = world.root
    for path, text in _with_element(_tree(world), CAMERA).items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    dry = await hass.services.async_call(DOMAIN, "reload_config", {"dry_run": True}, blocking=True, return_response=True)
    assert dry["ok"] and dry["applied"] is False and "el-yard-cam" not in world.elements.elements_by_id()
    done = await hass.services.async_call(DOMAIN, "reload_config", {}, blocking=True, return_response=True)
    assert done["ok"] and done["applied"] and "el-yard-cam" in world.elements.elements_by_id()
    (root / "pages" / "home.yaml").write_text("id: home\nkind: nonsense\n")
    bad = await hass.services.async_call(DOMAIN, "reload_config", {}, blocking=True, return_response=True)
    assert not bad["ok"] and bad["errors"][0]["at"].startswith("pages/home.yaml:")


async def test_the_service_without_a_directory_is_an_error_reply(hass, world):
    async_setup_config_service(hass)
    reply = await hass.services.async_call(DOMAIN, "reload_config", {}, blocking=True, return_response=True)
    assert reply["ok"] is False and reply["errors"][0]["code"] == "no_directory"


# ---- Q5: author-chosen ids, and the exact secret rule (pure) -----------------------------------------------------------------

@pytest.mark.parametrize("element_id", ["el-yard-cam", "el-abc", "el-" + "a" * 40, "el-0a1b2c3d"])
async def test_an_author_chosen_element_id_is_valid_and_the_minted_shape_stays_valid(element_id):
    assert validate_element({"id": element_id, "kind": "status", "name": "x", "entity_id": "sensor.x"})["id"] == element_id


@pytest.mark.parametrize("element_id", ["el-ab", "el-" + "a" * 41, "el-Yard", "el-yard_cam", "el-", "yard-cam", "EL-yard-cam", "el-a b"])
async def test_other_ids_are_still_refused(element_id):
    with pytest.raises(ElementError):
        validate_element({"id": element_id, "kind": "status", "name": "x", "entity_id": "sensor.x"})


async def test_a_page_may_be_called_el_slug():
    from ..pages_model import normalize_document_v6

    doc = normalize_document_v6({"schema_version": 6, "pages": [_page("el-yard-page")], "devices": {}, "dashboards": {}, "endpoints": {}})
    assert doc["pages"][0]["id"] == "el-yard-page"


@pytest.mark.parametrize("key", ["password", "sip_password", "rtsp_password", "protect_api_key", "remote_token", "tailscale_api_key",
                                 "Token", "API-Key", "owner_token", "client_secret", "my_private_key", "wifi_password"])
async def test_known_secret_keys_are_refused(key):
    assert cy.is_secret_key(key)


@pytest.mark.parametrize("key", ["token_ttl", "passwordless", "secretary", "keyboard", "tokens_left", "monkey", "label", "entity_id",
                                 "ring_window", "hold", "credit", "api_version", "private_label", "tokenizer"])
async def test_a_legitimate_option_that_only_contains_the_word_is_not_refused(key):
    assert not cy.is_secret_key(key)
    files = cy.to_files(cy.load_yaml("portico_config: 1\n")[0] and {"schema_version": 6, "pages": [], "devices": {}, "dashboards": {},
                                                                   "endpoints": {}}, [], {})
    files["portico.yaml"] += f"{key}: 1\n"
    assert not [e for e in cy.from_files(files).errors if e.code == "secret_in_yaml"]


async def test_a_secret_key_in_a_file_is_still_refused_with_its_position():
    files = cy.to_files({"schema_version": 6, "pages": [], "devices": {}, "dashboards": {}, "endpoints": {}}, [], {})
    files["portico.yaml"] += "sip_password: hunter2\n"
    (err,) = [e for e in cy.from_files(files).errors if e.code == "secret_in_yaml"]
    assert err.file == "portico.yaml" and err.line == 3 and "hunter2" not in err.message
