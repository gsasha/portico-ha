"""Elements store (DEV-760, PR E1): validators, KIND_SPECS, the store, persistence, the schema hook. Offline, no websocket."""
from __future__ import annotations

import copy
import logging

import pytest

from custom_components.portico import elements as el
from custom_components.portico.elements import ElementError, ElementStore, get_elements_store, validate_element

from .test_push_registry import _custom_integrations  # noqa: F401 - autouse fixture by name

pytestmark = pytest.mark.asyncio

MINIMAL = {
    "camera": {"kind": "camera", "name": "Gate cam", "entity_id": "camera.gate"},
    "button": {"kind": "button", "name": "Lock it", "entity_id": "lock.front", "options": {"action": "lock_lock"}},
    "status": {"kind": "status", "name": "Gate state", "entity_id": "cover.gate"},
    "chip": {"kind": "chip", "name": "Door chip", "entity_id": "lock.front"},
    "talk": {"kind": "talk", "name": "Gate talk", "options": {"door": "gate-1"}},
}


def _code(raw):
    with pytest.raises(ElementError) as err:
        validate_element(raw)
    return err.value


def _with(base_kind, /, **changes):
    base = copy.deepcopy(MINIMAL[base_kind])
    base.update(changes)
    return base


# ---- validators ----

@pytest.mark.parametrize("kind", sorted(MINIMAL))
async def test_each_kind_accepts_a_minimal_element(kind):
    clean = validate_element(MINIMAL[kind])
    assert clean["kind"] == kind and clean["options"] is not None
    assert set(el.KIND_SPECS) == {"camera", "button", "talk", "status", "chip"}


async def test_unknown_keys_and_kinds_are_refused():
    assert _code(_with("status", color="red")).field == "element"
    assert _code(_with("status", options={"nope": 1})).field == "options"
    assert _code(_with("status", kind="widget")).field == "kind"
    assert _code("x").field == "element"
    assert _code(_with("status", options="x")).field == "options"


async def test_button_entity_must_match_the_action_domain():
    assert _code(_with("button", entity_id="switch.front")).field == "entity_id"
    assert _code(_with("button", entity_id=None)).field == "entity_id"
    assert _code(_with("button", options={"action": "nope"})).field == "options.action"
    assert _code(_with("button", options={})).field == "options.action"
    ok = validate_element(_with("button", entity_id="cover.gate", options={"action": "cover_open", "color": "red"}))
    assert ok["options"]["color"] == "red"
    assert _code(_with("button", entity_id="cover.gate", options={"action": "cover_open", "color": "mauve"})).field == "options.color"


async def test_targeted_actions_take_no_entity_and_a_bounded_target():
    for action in ("portico_open", "call", "talk"):
        ok = validate_element(_with("button", entity_id=None, options={"action": action, "target": "t"}))
        assert "entity_id" not in ok
        assert _code(_with("button", entity_id="lock.front", options={"action": action})).field == "entity_id"
    assert _code(_with("button", entity_id=None, options={"action": "call", "target": "x" * 161})).field == "options.target"
    assert _code(_with("button", options={"action": "lock_lock", "target": "t"})).field == "options.target"


async def test_camera_has_exactly_one_of_entity_and_stream():
    both = _with("camera", options={"stream": "rtsp://10.6.0.6:8554/gate"})
    assert _code(both).field == "entity_id"
    assert _code({"kind": "camera", "name": "c"}).field == "entity_id"
    ok = validate_element({"kind": "camera", "name": "c", "options": {"stream": "rtsps://h:8554/a/b", "size": "big"}})
    assert "entity_id" not in ok
    assert _code(_with("camera", entity_id="light.x")).field == "entity_id"
    assert _code(_with("camera", options={"size": "huge"})).field == "options.size"


@pytest.mark.parametrize("stream", [
    "rtsp://user:pw@10.0.0.1:554/a", "rtsp://user@10.0.0.1/a", "rtsp://h/a?token=1", "rtsp://h/a#f",
    "rtsp:///a", "rtsp://h", "rtsp://h/", "http://h/a", "10.0.0.1/a", "rtsp://h:99999/a", "rtsp://h/a b", 5,
    "rtsp://h/" + "a" * 300,
])
async def test_bad_streams_are_refused(stream):
    assert _code({"kind": "camera", "name": "c", "options": {"stream": stream}}).field == "options.stream"


async def test_entity_only_is_a_one_flag_change(monkeypatch):
    monkeypatch.setattr(el, "CAMERA_ALLOWS_STREAM", False)
    assert _code({"kind": "camera", "name": "c", "options": {"stream": "rtsp://h/a"}}).field == "options"
    assert _code({"kind": "camera", "name": "c"}).field == "entity_id"
    assert validate_element(MINIMAL["camera"])["entity_id"] == "camera.gate"


async def test_status_states_map_limits():
    states = {f"s{i}": {"label": "L", "color": "green"} for i in range(16)}
    assert validate_element(_with("status", options={"states": states}))["options"]["states"] == states
    assert _code(_with("status", options={"states": {**states, "x": {}}})).field == "options.states"
    assert _code(_with("status", options={"states": {"s" * 41: {}}})).field == "options.states"
    assert _code(_with("status", options={"states": {"a": {"label": "x" * 41}}})).field == "options.states"
    assert _code(_with("status", options={"states": {"a": {"color": "mauve"}}})).field == "options.states"
    assert _code(_with("status", options={"states": {"a": {"icon": "x"}}})).field == "options.states"
    assert _code(_with("status", options={"short": "x"})).field == "options"
    assert _code(_with("status", entity_id=None)).field == "entity_id"
    assert _code(_with("status", entity_id="Bad Id")).field == "entity_id"


async def test_chip_short_is_bounded_and_status_has_none():
    assert validate_element(_with("chip", options={"short": " Door "}))["options"]["short"] == "Door"
    assert _code(_with("chip", options={"short": "x" * 13})).field == "options.short"


async def test_name_and_icon():
    assert _code(_with("status", name="   ")).field == "name"
    assert _code(_with("status", name="x" * 41)).field == "name"
    assert _code(_with("status", name=None)).field == "name"
    assert validate_element(_with("status", name="  Gate  "))["name"] == "Gate"
    icon = el.ICON_NAMES[0]
    assert validate_element(_with("status", icon="mdi:" + icon))["icon"] == icon
    assert _code(_with("status", id="bad")).field == "id"


@pytest.mark.parametrize("kind", ["status", "chip"])
@pytest.mark.parametrize("given,kept", [("lock-open-variant", "lock-open-variant"), ("gate-arrow-right", "gate-arrow-right"),
                                        ("mdi:thermometer", "thermometer"), ("numeric-1-box", "numeric-1-box"), ("x" * 64, "x" * 64)])
async def test_status_and_chip_accept_any_wellformed_mdi_name(kind, given, kept):
    assert validate_element(_with(kind, icon=given))["icon"] == kept


@pytest.mark.parametrize("kind", ["status", "chip"])
@pytest.mark.parametrize("bad", ["Bad Name", "../x", "", "mdi:", "x" * 65, "-a", "a--b", "a-", "mdi:mdi:a", "lock open", 5, ["a"]])
async def test_status_and_chip_refuse_a_malformed_icon(kind, bad):
    assert _code(_with(kind, icon=bad)).field == "icon"
    assert "unknown icon" in str(_code(_with(kind, icon="Bad Name")))


@pytest.mark.parametrize("kind", ["button", "camera"])
async def test_button_and_camera_keep_the_fixed_icon_set(kind):
    ok = el.ICON_NAMES[0]
    assert validate_element(_with(kind, icon="mdi:" + ok))["icon"] == ok
    for bad in ("lock-open-variant-not-listed", "thermometer-lines-x", "Bad Name", "../x", ""):
        assert bad not in el.ICON_NAMES
        assert _code(_with(kind, icon=bad)).field == "icon"


async def test_icon_policy_names_each_kind():
    assert el.icon_policy() == {"camera": "fixed", "button": "fixed", "talk": "fixed", "status": "mdi", "chip": "mdi"}


async def test_editing_a_used_element_keeps_its_guards_with_a_free_icon(hass, monkeypatch):
    store = ElementStore(hass)
    a = await store.async_set(MINIMAL["status"], store.rev)
    monkeypatch.setattr(store, "used_by", lambda _id: [{"endpoint": "e1", "label": "Kitchen"}])
    done = await store.async_set({**a["element"], "icon": "mdi:gate-arrow-right"}, store.rev)
    assert done["element"]["icon"] == "gate-arrow-right"
    with pytest.raises(ElementError) as err:
        await store.async_set({**a["element"], "kind": "chip", "icon": "gate-arrow-right"}, store.rev)
    assert err.value.code == "kind_locked"


# ---- store ----

async def test_create_mints_distinct_ids_and_keeps_order(hass):
    store = ElementStore(hass)
    listed = await store.async_list()
    assert listed["elements"] == [] and listed["schema_version"] == 1
    ids = []
    for kind in ("status", "camera", "chip", "button"):
        res = await store.async_set(MINIMAL[kind], store.rev)
        assert el.ID_RE.match(res["element"]["id"])
        ids.append(res["element"]["id"])
    assert len(set(ids)) == 4
    assert [e["id"] for e in (await store.async_list())["elements"]] == ids


async def test_rename_keeps_the_id_and_names_are_unique_case_insensitively(hass):
    store = ElementStore(hass)
    a = (await store.async_set(MINIMAL["status"], store.rev))["element"]
    await store.async_set(MINIMAL["chip"], store.rev)
    with pytest.raises(ElementError) as err:
        await store.async_set(_with("camera", name="  gate STATE "), store.rev)
    assert err.value.code == "name_taken"
    renamed = (await store.async_set({**a, "name": "GATE state"}, store.rev))["element"]   # own name, other case
    assert renamed["id"] == a["id"] and renamed["name"] == "GATE state"
    with pytest.raises(ElementError) as err:
        await store.async_set({**a, "name": "door chip"}, store.rev)
    assert err.value.code == "name_taken"


async def test_replace_cannot_change_kind_and_unknown_id(hass):
    store = ElementStore(hass)
    a = (await store.async_set(MINIMAL["status"], store.rev))["element"]
    with pytest.raises(ElementError) as err:
        await store.async_set({**MINIMAL["chip"], "id": a["id"]}, store.rev)
    assert err.value.code == "kind_locked"
    with pytest.raises(ElementError) as err:
        await store.async_set({**MINIMAL["status"], "id": "el-00000000", "name": "z"}, store.rev)
    assert err.value.code == "unknown_element"


async def test_cap_of_200(hass):
    store = ElementStore(hass)
    for i in range(el.MAX_ELEMENTS):
        await store.async_set(_with("status", name=f"s{i}"), store.rev)
    with pytest.raises(ElementError) as err:
        await store.async_set(_with("status", name="one more"), store.rev)
    assert err.value.code == "too_many"


async def test_stale_rev_conflicts_and_validate_only_writes_nothing(hass, hass_storage):
    store = ElementStore(hass)
    rev0 = store.rev
    res = await store.async_set(MINIMAL["status"], rev0, validate_only=True)
    assert res["element"]["id"].startswith("el-") and "portico.elements" not in hass_storage
    assert (await store.async_list())["elements"] == []
    await store.async_set(MINIMAL["status"], rev0)
    with pytest.raises(ElementError) as err:
        await store.async_set(MINIMAL["chip"], rev0)
    assert err.value.code == "conflict"
    with pytest.raises(ElementError) as err:
        await store.async_delete("el-00000000", store.rev)
    assert err.value.code == "unknown_element"
    with pytest.raises(ElementError) as err:
        await store.async_delete((await store.async_list())["elements"][0]["id"], rev0)
    assert err.value.code == "conflict"


async def test_delete_and_the_used_by_seam(hass, monkeypatch):
    store = ElementStore(hass)
    a = (await store.async_set(MINIMAL["status"], store.rev))["element"]
    assert store.used_by(a["id"]) == []
    monkeypatch.setattr(store, "used_by", lambda _id: [{"endpoint": "e1", "label": "Kitchen"}])
    with pytest.raises(ElementError) as err:
        await store.async_delete(a["id"], store.rev)
    assert err.value.code == "in_use" and err.value.used_by == [{"endpoint": "e1", "label": "Kitchen"}]
    assert len((await store.async_list())["elements"]) == 1
    with pytest.raises(TypeError):                       # there is no confirm override any more (Alice, 2026-11-09)
        await store.async_delete(a["id"], store.rev, confirm=True)
    monkeypatch.setattr(store, "used_by", lambda _id: [])
    await store.async_delete(a["id"], store.rev)
    assert (await store.async_list())["elements"] == []


async def test_get_elements_store_is_a_singleton(hass):
    assert get_elements_store(hass) is get_elements_store(hass)


# ---- persistence ----

async def test_save_then_reload_gives_the_same_list_and_rev(hass, hass_storage):
    store = ElementStore(hass)
    await store.async_set(MINIMAL["status"], store.rev)
    await store.async_set(MINIMAL["button"], store.rev)
    assert hass_storage["portico.elements"]["data"]["schema_version"] == 1
    again = ElementStore(hass)
    assert (await again.async_list()) == (await store.async_list())
    rev = again.rev
    await again.async_set(MINIMAL["chip"], rev)
    assert again.rev != rev


async def test_an_invalid_stored_element_is_dropped_with_a_warning_without_values(hass, hass_storage, caplog):
    good = validate_element({**MINIMAL["status"], "id": "el-aaaaaaaa"})
    bad = {"id": "el-bbbbbbbb", "kind": "status", "name": "SECRETNAME", "entity_id": "NOT AN ENTITY SECRET", "options": {}}
    hass_storage["portico.elements"] = {"version": 1, "minor_version": 1, "key": "portico.elements",
                                        "data": {"schema_version": 1, "elements": [good, bad, "junk", {"id": "x"}]}}
    caplog.set_level(logging.WARNING)
    store = ElementStore(hass)
    assert [e["id"] for e in (await store.async_list())["elements"]] == ["el-aaaaaaaa"]
    assert "el-bbbbbbbb" in caplog.text and "SECRET" not in caplog.text


async def test_an_unknown_kind_survives_a_round_trip_and_is_deletable(hass, hass_storage):
    odd = {"id": "el-cccccccc", "kind": "gauge", "name": "Future", "options": {"x": [1, 2]}, "extra": True}
    good = validate_element({**MINIMAL["status"], "id": "el-aaaaaaaa"})
    hass_storage["portico.elements"] = {"version": 1, "minor_version": 1, "key": "portico.elements",
                                        "data": {"schema_version": 1, "elements": [odd, good]}}
    store = ElementStore(hass)
    listed = (await store.async_list())["elements"]
    assert listed[0]["kind_unknown"] is True and "kind_unknown" not in listed[1]
    await store.async_set({**good, "name": "Renamed"}, store.rev)
    assert hass_storage["portico.elements"]["data"]["elements"][0] == odd           # verbatim, no marker
    with pytest.raises(ElementError) as err:
        await store.async_set({**odd, "name": "Mine"}, store.rev)
    assert err.value.code == "invalid"                                              # not editable
    await store.async_delete("el-cccccccc", store.rev)
    assert [e["id"] for e in hass_storage["portico.elements"]["data"]["elements"]] == ["el-aaaaaaaa"]


# ---- schema ----

def _seed(hass_storage, version, elements):
    hass_storage["portico.elements"] = {"version": 1, "minor_version": 1, "key": "portico.elements",
                                        "data": {"schema_version": version, "elements": elements}}


async def test_migration_chain_is_in_memory_until_the_first_save_then_one_copy(hass, hass_storage, monkeypatch):
    monkeypatch.setattr(el, "SCHEMA_VERSION", 2)
    monkeypatch.setitem(el.MIGRATIONS, 1, lambda d: {**d, "schema_version": 2, "migrated": True})
    old = [validate_element({**MINIMAL["status"], "id": "el-aaaaaaaa"})]
    _seed(hass_storage, 1, old)
    store = ElementStore(hass)
    assert (await store.async_list())["schema_version"] == 2
    assert hass_storage["portico.elements"]["data"]["schema_version"] == 1 and "portico.elements.v1" not in hass_storage
    await store.async_set(MINIMAL["chip"], store.rev)
    assert hass_storage["portico.elements"]["data"]["schema_version"] == 2
    assert hass_storage["portico.elements.v1"]["data"] == {"schema_version": 1, "elements": old}
    await store.async_set(_with("camera"), store.rev)                                # no second copy attempt
    assert hass_storage["portico.elements.v1"]["data"]["elements"] == old


async def test_an_existing_copy_is_never_overwritten(hass, hass_storage, monkeypatch):
    monkeypatch.setattr(el, "SCHEMA_VERSION", 2)
    monkeypatch.setitem(el.MIGRATIONS, 1, lambda d: {**d, "schema_version": 2})
    _seed(hass_storage, 1, [])
    hass_storage["portico.elements.v1"] = {"version": 1, "minor_version": 1, "key": "portico.elements.v1",
                                           "data": {"keep": "me"}}
    store = ElementStore(hass)
    await store.async_set(MINIMAL["status"], store.rev)
    assert hass_storage["portico.elements.v1"]["data"] == {"keep": "me"}
    assert hass_storage["portico.elements"]["data"]["schema_version"] == 2


async def test_a_missing_migration_step_leaves_the_file_alone(hass, hass_storage, monkeypatch):
    monkeypatch.setattr(el, "SCHEMA_VERSION", 2)
    _seed(hass_storage, 1, [])
    store = ElementStore(hass)
    assert (await store.async_list())["elements"] == []
    with pytest.raises(ElementError) as err:
        await store.async_set(MINIMAL["status"], store.rev)
    assert err.value.code == "migration_missing"
    assert hass_storage["portico.elements"]["data"]["schema_version"] == 1


async def test_a_newer_file_is_read_and_every_write_refused(hass, hass_storage):
    elems = [validate_element({**MINIMAL["status"], "id": "el-aaaaaaaa"})]
    _seed(hass_storage, 9, elems)
    before = copy.deepcopy(hass_storage["portico.elements"])
    store = ElementStore(hass)
    listed = await store.async_list()
    assert [e["id"] for e in listed["elements"]] == ["el-aaaaaaaa"] and listed["schema_version"] == 9
    for call in (lambda: store.async_set(MINIMAL["chip"], store.rev),
                 lambda: store.async_delete("el-aaaaaaaa", store.rev)):
        with pytest.raises(ElementError) as err:
            await call()
        assert err.value.code == "newer_schema"
    assert hass_storage["portico.elements"] == before


async def test_kinds_description_covers_the_registry():
    desc = el.kinds_description()
    assert set(desc) == set(el.KIND_SPECS) and desc["camera"]["entity_domain"] == "camera"
    assert all(f["name"] for d in desc.values() for f in d["fields"])


async def test_the_module_touches_no_content_store_and_nothing_imports_it():
    import pathlib
    root = pathlib.Path(el.__file__).parent
    src = (root / "elements.py").read_text()
    # Placements P1: the store READS the content store (the usage scan behind used_by) and never writes it.
    assert "ContentStore" not in src
    for write in ("async_save_document", "async_set_home_name", "async_move_orphan", "async_delete_orphan", "async_remove", "_save_content"):
        assert write not in src.replace("async_remove_listener", "")
    importers = sorted(p.name for p in root.glob("*.py") if p.name != "elements.py" and "elements" in p.read_text()
                       and "from .elements" in p.read_text())
    assert importers == ["config_apply.py", "config_sync.py", "config_yaml.py", "content.py", "elements_commands.py", "managed_talk.py", "pages_migrate.py", "signaling.py"]       # config_sync: the YAML files follow the store (DEV-824 P3); config_apply: the YAML apply swaps the element store (DEV-824 P2); (phone_entry no longer: a panel has no screen and no screen element to drop); pages_migrate: the Talk create-if-missing; config_yaml: the YAML tree's pure element validation (DEV-824); + the content store's lazy import of the element store; managed_talk is the reconcile (DEVICES-10)


# ---- DEV-768: an optional small stream on a camera element -------------------------------------------------------------

async def test_a_camera_may_carry_a_small_entity_and_it_is_kept():
    ok = validate_element(_with("camera", options={"small_entity_id": "camera.gate_sub", "size": "big"}))
    assert ok["entity_id"] == "camera.gate" and ok["options"] == {"small_entity_id": "camera.gate_sub", "size": "big"}
    assert "small_entity_id" not in validate_element(_with("camera"))["options"]          # absent stays absent


@pytest.mark.parametrize("small", ["camera.gate", "light.gate_sub", "gate_sub", 5, ""])
async def test_a_bad_small_entity_is_refused(small):
    assert _code(_with("camera", options={"small_entity_id": small})).field == "options.small_entity_id"


async def test_a_small_stream_needs_a_camera_entity_not_a_stream_address():
    err = _code({"kind": "camera", "name": "c", "options": {"stream": "rtsp://h/a", "small_entity_id": "camera.x"}})
    assert err.field == "options.small_entity_id"
    assert _code({"kind": "camera", "name": "c", "options": {"small_entity_id": "camera.x"}}).field in ("entity_id", "options.small_entity_id")


async def test_only_a_camera_takes_a_small_entity():
    assert _code(_with("button", entity_id="cover.gate", options={"action": "cover_open", "small_entity_id": "camera.x"})).field == "options"
