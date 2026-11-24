"""DEV-824 P1: the YAML tree under /config/portico/ (config_yaml.py, the pure half) and `portico.validate_config`
(config_service.py). The pure half needs no Home Assistant object; the service test reads a real directory."""
from __future__ import annotations

import copy
import os

import pytest

from .. import config_yaml as cy
from ..config_flow import DOMAIN
from ..config_service import async_setup_config_service, validate_directory
from ..pages_model import normalize_document_v6

TALK = "el-00000001"


def _action(action_id, kind="lock_open", **extra):
    return {"id": action_id, "label": action_id, "kind": kind, "entity_id": "lock.gate", **extra}


def _content(page_id="home"):
    return {"id": page_id, "label": "Home", "kind": "content",
            "blocks": [{"id": "b", "title": "Home", "cameras": [], "actions": [_action("open_gate")]}]}


def _ring(page_id="ring-gate"):
    return {"id": page_id, "label": "Ring", "kind": "ring", "talk": TALK, "cameras": ["camera.gate"],
            "blocks": [{"id": "r", "title": "R", "cameras": [], "actions": [_action("ring_open")]}]}


def _document():
    raw = {"schema_version": 6, "home_name": "My Home", "show_fps": True,
           "pages": [_content("home"), _content("garden"), _ring()],
           "devices": {"ep-kitchen": {"pages": [{"page": "home"}, {"page": "garden", "as": "yard"}], "ring": {},
                                      "roster_hide": []},
                       "gate:http://10.1.2.3/x": {"pages": [{"page": "home"}], "ring": {}, "roster_hide": []}},
           "dashboards": {}, "endpoints": {}}
    return normalize_document_v6(raw)


ELEMENTS = [
    {"id": "el-00000002", "kind": "camera", "name": "Gate cam", "entity_id": "camera.gate", "options": {}},
    {"id": TALK, "kind": "talk", "name": "Gate", "icon": "phone", "options": {"door": "gate-1"}},
]
SETTINGS = {"ring_enabled": True, "ring_window": 45, "new_phones_enabled": False, "test_phones": ["alice"],
            "live_rtsp_base": "rtsp://10.3.0.2:8554"}


def _files():
    return cy.to_files(_document(), copy.deepcopy(ELEMENTS), dict(SETTINGS))


def _errors(files):
    return cy.validate_files(files).errors


# ---- emitting -------------------------------------------------------------------------------------------------------------

def test_layout_and_canonical_order():
    files = _files()
    plain = sorted(p for p in files if "~" not in p)
    assert plain == ["devices/ep-kitchen.yaml", "elements/el-00000001.yaml", "elements/el-00000002.yaml", "index.yaml",
                     "pages/garden.yaml", "pages/home.yaml", "pages/ring-gate.yaml", "portico.yaml"]
    assert len(files) == 9
    assert files["portico.yaml"].startswith("portico_config: 1\nhome_name: My Home\nshow_fps: true\nendpoints: {}\nring:\n  enabled: true\n")
    assert "live:\n  rtsp_base: rtsp://10.3.0.2:8554\n" in files["portico.yaml"]
    assert files["elements/el-00000002.yaml"] == ("id: el-00000002\nkind: camera\nname: Gate cam\nentity_id: camera.gate\n"
                                                  "options: {}\n")
    assert files["index.yaml"] == "portico_config: 1\npages:\n- home\n- garden\n- ring-gate\nelements:\n- el-00000002\n- el-00000001\n"


def test_the_endpoint_id_with_odd_characters_gets_a_safe_name_that_the_id_still_matches():
    files = _files()
    (path,) = [p for p in files if p.startswith("devices/gate")]
    assert ":" not in path and "/" not in path[len("devices/"):]
    assert "id: gate:http://10.1.2.3/x" in files[path]
    assert cy.file_name("devices", "a") != cy.file_name("devices", "a?")


def test_key_order_does_not_change_the_bytes():
    a = {"b": 1, "kind": "x", "id": "i", "a": {"z": 1, "y": 2}}
    b = {"a": {"y": 2, "z": 1}, "id": "i", "kind": "x", "b": 1}
    assert cy.dump_yaml(a) == cy.dump_yaml(b) == "id: i\nkind: x\na:\n  y: 2\n  z: 1\nb: 1\n"


def test_unicode_and_text_that_looks_like_other_types_survive():
    value = {"id": "x", "name": "Кухня", "options": {"a": "yes", "b": "123", "c": "null", "d": "2026-01-01", "e": "a: b"}}
    data, _ = cy.load_yaml(cy.dump_yaml(value))
    assert data == value


# ---- the round trip -------------------------------------------------------------------------------------------------------

def test_round_trip_is_exact_and_byte_stable():
    assert cy.prove_roundtrip(_document(), copy.deepcopy(ELEMENTS), dict(SETTINGS)) == []
    result = cy.validate_files(_files())
    assert result.ok, result.errors
    assert result.document == _document() and result.elements == ELEMENTS and result.settings == SETTINGS
    assert cy.to_files(result.document, result.elements, result.settings) == _files()


def test_page_and_element_order_follows_the_index_not_the_file_names():
    files = _files()
    files["index.yaml"] = files["index.yaml"].replace("- home\n- garden\n", "- garden\n- home\n")
    result = cy.validate_files(files)
    assert [p["id"] for p in result.document["pages"]] == ["garden", "home", "ring-gate"]


def test_the_proof_catches_a_change():
    doc = _document()
    doc["home_name"] = "x: &y"                    # still a valid string: must survive
    assert cy.prove_roundtrip(doc, ELEMENTS, SETTINGS) == []
    broken = {**doc, "pages": [{**doc["pages"][0], "label": ""}] + doc["pages"][1:]}
    assert cy.prove_roundtrip(broken, ELEMENTS, SETTINGS) != []


def test_managed_talk_elements_are_written_to_managed_and_ignored_on_load():
    managed = {"id": "talk-phone-alice", "kind": "talk", "name": "Alice", "managed": True, "icon": "phone",
               "options": {"phone": "alice"}}
    files = cy.to_files(_document(), [*ELEMENTS, managed], SETTINGS)
    assert "managed/talk-phone-alice.yaml" in files and "elements/talk-phone-alice.yaml" not in files
    assert cy.validate_files(files).ok and "talk-phone-alice" not in files["index.yaml"]


def test_an_unset_live_setting_is_absent_not_empty():
    files = cy.to_files(_document(), ELEMENTS, {"ring_enabled": True})
    assert "live:" not in files["portico.yaml"] and cy.validate_files(files).settings == {"ring_enabled": True}


# ---- the hardened loader --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,code,line", [
    ("a: 1\na: 2\n", "duplicate_key", 2),
    ("a: &x 1\nb: *x\n", "anchor", 1),
    ("a: !!python/object/apply:os.system [x]\n", "tag", 1),
    ("a: !custom 1\n", "tag", 1),
    ("a: [1, 2\n", "invalid_yaml", None),
    ("", "empty", 1),
    ("1: x\n", "bad_key", 1),
    ("a: " + "[" * 14 + "]" * 14 + "\n", "too_deep", 1),
    ("a: " + "x" * 70000 + "\n", "too_big", 1),
])
def test_loader_refuses_with_a_position(text, code, line):
    with pytest.raises(cy.YamlFileError) as err:
        cy.load_yaml(text)
    assert err.value.code == code
    if line is not None:
        assert err.value.line == line and err.value.col >= 1


def test_a_date_stays_text_and_an_object_value_is_refused():
    assert cy.load_yaml("a: 2026-01-01\n")[0] == {"a": "2026-01-01"}


def test_loader_never_runs_code():
    with pytest.raises(cy.YamlFileError):
        cy.load_yaml("a: !!python/name:os.system\n")


def test_errors_carry_file_line_and_column_of_the_field():
    files = _files()
    files["elements/el-00000002.yaml"] = files["elements/el-00000002.yaml"].replace("kind: camera", "kind: banana")
    (err,) = _errors(files)
    assert (err.file, err.line) == ("elements/el-00000002.yaml", 2) and "kind" in err.message


def test_a_syntax_error_in_one_file_does_not_hide_a_second_file_error():
    files = _files()
    files["pages/home.yaml"] = "id: home\n  bad: [\n"
    files["elements/el-00000002.yaml"] = files["elements/el-00000002.yaml"].replace("camera", "banana")
    assert {e.file for e in _errors(files)} == {"pages/home.yaml", "elements/el-00000002.yaml"}


# ---- layout, secrets, limits ----------------------------------------------------------------------------------------------

def test_paths_outside_the_layout_are_refused():
    for bad in ("evil.yaml", "../x.yaml", "pages/../../x.yaml", "pages/a/b.yaml", "/abs.yaml", "locks/x.yaml", "pages/.yaml"):
        files = {**_files(), bad: "id: x\n"}
        assert any(e.code == "bad_path" for e in _errors(files)), bad
    files = {**_files(), "README.md": "hello", "schema/portico-config.schema.json": "{}", "examples/a.yaml": "a: 1\n"}
    assert cy.validate_files(files).ok


def test_a_secret_key_is_refused_wherever_it_sits():
    for key in ("password", "api_key", "sip_password", "remote_token", "Secret"):
        files = _files()
        files["elements/el-00000002.yaml"] += f"{key}: hunter2\n"
        (err,) = _errors(files)
        assert err.code == "secret_in_yaml" and err.line >= 1 and "hunter2" not in err.message
    files = _files()
    files["portico.yaml"] += "ring_extra:\n  deep:\n    - tailscale_api_key: x\n"
    assert _errors(files)[0].code == "secret_in_yaml"


def test_open_relay_action_is_not_in_the_schema():
    files = _files()
    files["elements/el-00000002.yaml"] += "open_relay_action: [{service: lock.open}]\n"
    assert _errors(files)[0].code == "forbidden_key"


def test_a_managed_talk_element_cannot_be_written_as_a_declared_one():
    files = _files()
    files["elements/talk-phone-x.yaml"] = ("id: talk-phone-x\nkind: talk\nname: X\nmanaged: true\noptions:\n  phone: x\n")
    assert any("managed" in e.message for e in _errors(files))


def test_file_count_and_tree_size_caps():
    files = _files()
    for n in range(cy.MAX_FILES):
        files[f"pages/p{n}.yaml"] = f"id: p{n}\n"
    assert _errors(files)[0].code == "too_many_files"
    big = {"portico.yaml": "portico_config: 1\n", **{f"pages/p{n}.yaml": "id: p\nnote: " + "x" * 60000 + "\n" for n in range(40)}}
    assert _errors(big)[0].code == "too_big"


def test_name_must_match_id_and_ids_must_be_unique():
    files = _files()
    files["pages/other.yaml"] = files["pages/home.yaml"]
    codes = {e.code for e in _errors(files)}
    assert "name_mismatch" in codes
    files = _files()
    files["pages/home.yaml"] = files["pages/home.yaml"].replace("id: home", "id: garden")
    assert "name_mismatch" in {e.code for e in _errors(files)}


def test_missing_root_wrong_version_and_unknown_index_ids():
    files = _files()
    del files["portico.yaml"]
    assert _errors(files)[0].code == "missing_root"
    files = _files()
    files["portico.yaml"] = files["portico.yaml"].replace("portico_config: 1", "portico_config: 2")
    assert _errors(files)[0].code == "bad_version"
    files = _files()
    files["index.yaml"] = files["index.yaml"].replace("- home\n", "- nope\n")
    assert any(e.code == "unknown_id" for e in _errors(files))


# ---- the editor's validators, through the files ---------------------------------------------------------------------------

def test_document_rules_are_the_editors_rules():
    files = _files()
    files["devices/ep-kitchen.yaml"] = files["devices/ep-kitchen.yaml"].replace("page: garden", "page: ring-gate")
    (err,) = _errors(files)
    assert err.code == "invalid_document" and err.file == "devices/ep-kitchen.yaml" and "Ring page" in err.message
    files = _files()
    files["pages/home.yaml"] = files["pages/home.yaml"].replace("kind: lock_open", "kind: nonsense")
    assert _errors(files)[0].code == "invalid_document"
    files = _files()
    files["portico.yaml"] += "surprise: 1\n"
    assert _errors(files)[0].code == "invalid_document"          # an unknown top-level field is refused, never lost


def test_setting_rules_are_the_editors_rules():
    for old, new in (("window: 45", "window: 5"), ("enabled: true", "enabled: 1"), ("rtsp_base: rtsp://10.3.0.2:8554",
                                                                                    "rtsp_base: http://x"),
                     ("- alice", "- ''")):
        files = _files()
        assert old in files["portico.yaml"], old
        files["portico.yaml"] = files["portico.yaml"].replace(old, new)
        (err,) = _errors(files)
        assert err.code == "invalid_setting" and err.file == "portico.yaml" and err.line > 1, (old, err)
    files = _files()
    files["portico.yaml"] = files["portico.yaml"].replace("window: 45", "wibble: 45")
    assert any("not a setting" in e.message for e in _errors(files))


def test_a_custom_document_validator_replaces_the_pure_one():
    seen = []

    def validator(doc):
        seen.append(doc)
        return doc
    result = cy.validate_files(_files(), document_validator=validator)
    assert result.ok and len(seen) == 1 and seen[0]["pages"][0]["id"] == "home"


def test_validation_writes_nothing_and_does_not_mutate_its_input():
    files = _files()
    before = dict(files)
    cy.validate_files(files)
    assert files == before


# ---- the service ----------------------------------------------------------------------------------------------------------

def _write(root, files):
    for rel, text in files.items():
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)


def test_validate_directory_reports_ok_and_counts(tmp_path):
    _write(tmp_path, _files())
    reply = validate_directory(str(tmp_path))
    assert reply == {"ok": True, "checked": "structure", "errors": [],
                     "counts": {"pages": 3, "devices": 2, "dashboards": 0, "elements": 2, "settings": 5, "doors": 0, "phones": 0}}


def test_validate_directory_reports_errors_and_missing_directory(tmp_path):
    files = _files()
    files["pages/home.yaml"] = "id: home\nid: home\n"
    _write(tmp_path, files)
    reply = validate_directory(str(tmp_path))
    assert not reply["ok"] and reply["errors"][0]["file"] == "pages/home.yaml" and reply["errors"][0]["line"] == 2
    missing = validate_directory(str(tmp_path / "nope"))
    assert not missing["ok"] and missing["errors"][0]["code"] == "no_directory"


def test_validate_directory_refuses_a_symlink_and_leaves_the_files_alone(tmp_path):
    _write(tmp_path, _files())
    os.symlink("/etc/passwd", tmp_path / "pages" / "evil.yaml")
    before = {p: (tmp_path / p).read_text() for p in _files()}
    reply = validate_directory(str(tmp_path))
    assert [e["code"] for e in reply["errors"]] == ["symlink"]
    assert {p: (tmp_path / p).read_text() for p in _files()} == before


@pytest.mark.asyncio
async def test_the_service_answers_with_a_response_and_applies_nothing(hass, tmp_path):
    hass.config.config_dir = str(tmp_path)
    _write(tmp_path / "portico", _files())
    async_setup_config_service(hass)
    reply = await hass.services.async_call(DOMAIN, "validate_config", {}, blocking=True, return_response=True)
    assert reply["ok"] and reply["checked"] == "structure"
    (tmp_path / "portico" / "pages" / "home.yaml").write_text("id: home\nkind: nonsense\n")
    reply = await hass.services.async_call(DOMAIN, "validate_config", {}, blocking=True, return_response=True)
    assert not reply["ok"] and reply["errors"]
