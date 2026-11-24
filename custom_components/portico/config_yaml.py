"""Portico configuration as YAML files (DEV-824, design `portico_config_yaml_design.md` sections 4, 5 and 8; P1, the base layer).

The declared configuration (the content document, the declared elements, the push and live settings) maps to a tree of small
YAML files under `/config/portico/` and back. This module is the PURE part: no Home Assistant object, no file or network I/O,
nothing applied. It gives

- `dump_yaml` / `load_yaml`: a canonical emitter (fixed key order, block style, so re-serialising unchanged data gives
  byte-identical text) and a hardened loader (no tags, anchors, aliases or duplicate keys; size and depth caps; every error
  carries file, line and column);
- `to_files(document, elements, settings)` and `from_files(files)`: the layout of section 4. `from_files(to_files(x)) == x`;
- `validate_files(files)`: load, then run the SAME pure validators the editor's save path uses (`normalize_document_v6`,
  `validate_element`, the settings patch checks), all errors at once. All or nothing is the caller's: this writes nothing.

P6 adds `doors/<device id>.yaml` and `phones/<key>.yaml`: the non-secret options of the door and phone config entries (a
gate_sip_server door's `dial_enabled`, `ring_answered_entity`, `house_bridge`; a phone's `doors`, `house_receive`, read-only `panel`).
Their live checks and apply are `config_options.py`.

Never serialised: secrets, generated state, `rev` fields, `open_relay_action`. A key that names a secret is refused in a file
(`secret_in_yaml`). Managed Talk elements are written to `managed/` for reference and ignored on load.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

import yaml

from .content import ProfileError
from .elements import ElementError, MANAGED_ID_RE, validate_element
from .pages_model import normalize_document_v6
from .ui_settings import KEYS as SETTING_KEYS
from .ui_settings import parse_patch

CONFIG_VERSION = 1
MAX_FILE_BYTES = 64 * 1024
MAX_FILES = 400
MAX_TREE_BYTES = 2 * 1024 * 1024
MAX_DEPTH = 12

ROOT_FILE = "portico.yaml"
INDEX_FILE = "index.yaml"
DIR_FILES = {"elements": "element", "managed": "managed", "pages": "page", "devices": "device", "dashboards": "dashboard",
             "doors": "door", "phones": "phone"}

# document keys that live in portico.yaml; the collections have their own files
_ROOT_DOC_KEYS = ("home_name", "show_fps", "roster", "endpoints")
_COLLECTIONS = ("pages", "devices", "dashboards")
_SKIP_DOC_KEYS = frozenset({"schema_version", *_COLLECTIONS})

# portico.yaml `ring` / `live` blocks <-> the settings keys of `ui_settings`
_SETTING_PATHS = {
    "ring_enabled": ("ring", "enabled"), "ring_window": ("ring", "window"), "new_phones_enabled": ("ring", "new_phones_enabled"),
    "test_phones": ("ring", "test_phones"), "live_rtsp_base": ("live", "rtsp_base"),
    "live_sub_streams": ("live", "sub_streams"), "live_away_streams": ("live", "away_streams"),
}
_SETTING_BY_PATH = {v: k for k, v in _SETTING_PATHS.items()}

# key order inside a mapping: these first, in this order, then the rest alphabetically
_KEY_ORDER = ("portico_config", "schema_version", "id", "kind", "label", "name", "icon", "entity_id", "talk", "cameras", "when",
              "page", "as", "pages", "blocks", "options", "home_name", "show_fps", "roster", "endpoints", "ring", "live")
_RANK = {k: i for i, k in enumerate(_KEY_ORDER)}

# A key that names a secret, EXACT (DEV-824 P2: the P1 pattern matched the word anywhere in a key and would refuse a legitimate option
# such as `token_ttl` or `passwordless`). The known secret fields of this integration's records are listed by name; beyond them only
# the unmistakable SUFFIX forms (`<anything>_password`, `_secret`, `_api_key`, `_private_key`, `_token`) are refused. Keys are
# compared lower-case with `-` as `_`.
_SECRET_NAMES = frozenset({
    "password", "passwd", "passphrase", "secret", "token", "api_key", "apikey", "private_key", "credential", "credentials",
    "authorization", "sip_password", "rtsp_password", "protect_api_key", "remote_token", "tailscale_api_key", "access_token",
    "refresh_token", "auth_token", "owner_token", "peer_token", "client_secret", "service_account", "fcm_token", "bearer"})
_SECRET_SUFFIXES = ("_password", "_passwd", "_secret", "_api_key", "_apikey", "_private_key", "_token")


def is_secret_key(key: str) -> bool:
    name = key.strip().lower().replace("-", "_")
    return name in _SECRET_NAMES or name.endswith(_SECRET_SUFFIXES)


_FORBIDDEN_KEYS = frozenset({"open_relay_action"})
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")


@dataclass(frozen=True)
class ConfigError:
    file: str
    line: int
    col: int
    message: str
    code: str = "invalid"

    def as_dict(self) -> dict[str, Any]:
        return {"file": self.file, "line": self.line, "col": self.col, "message": self.message, "code": self.code}


class YamlFileError(Exception):
    def __init__(self, line: int, col: int, message: str, code: str = "invalid_yaml") -> None:
        super().__init__(message)
        self.line, self.col, self.message, self.code = line, col, message, code


# ---- emitting -------------------------------------------------------------------------------------------------------------

class _Dumper(yaml.SafeDumper):
    def ignore_aliases(self, data: Any) -> bool:
        return True


def _canon(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _canon(value[k]) for k in sorted(value, key=lambda k: (_RANK.get(k, len(_RANK)), str(k)))}
    if isinstance(value, (list, tuple)):
        return [_canon(v) for v in value]
    return value


def dump_yaml(value: Any) -> str:
    """The canonical text of `value`: fixed key order, block style, one trailing newline. Same data, same bytes."""
    return yaml.dump(_canon(value), Dumper=_Dumper, sort_keys=False, default_flow_style=False, allow_unicode=True,
                     width=1_000_000, indent=2)


# ---- loading --------------------------------------------------------------------------------------------------------------

class _Loader(yaml.SafeLoader):
    """SafeLoader without implicit timestamps, and that refuses a duplicate key or a non-string key."""

    def construct_mapping(self, node, deep=False):  # noqa: ANN001
        seen: set[Any] = set()
        for key_node, _ in node.value:
            if not isinstance(key_node, yaml.ScalarNode):
                raise YamlFileError(key_node.start_mark.line + 1, key_node.start_mark.column + 1, "a key must be plain text")
            key = self.construct_object(key_node, deep=True)
            if not isinstance(key, str):
                raise YamlFileError(key_node.start_mark.line + 1, key_node.start_mark.column + 1,
                                    f"the key {key!r} must be quoted text", "bad_key")
            if key in seen:
                raise YamlFileError(key_node.start_mark.line + 1, key_node.start_mark.column + 1,
                                    f"the key {key!r} appears twice", "duplicate_key")
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


_Loader.yaml_implicit_resolvers = {
    ch: [(tag, rx) for tag, rx in rules if tag != "tag:yaml.org,2002:timestamp"]
    for ch, rules in yaml.SafeLoader.yaml_implicit_resolvers.items()}


def _check_events(text: str) -> None:
    depth = 0
    try:
        for event in yaml.parse(text, Loader=yaml.SafeLoader):
            mark = event.start_mark
            if isinstance(event, yaml.AliasEvent):
                raise YamlFileError(mark.line + 1, mark.column + 1, "aliases (*name) are not allowed", "alias")
            if getattr(event, "anchor", None):
                raise YamlFileError(mark.line + 1, mark.column + 1, "anchors (&name) are not allowed", "anchor")
            if getattr(event, "tag", None):
                raise YamlFileError(mark.line + 1, mark.column + 1, f"tags ({event.tag}) are not allowed", "tag")
            if isinstance(event, (yaml.MappingStartEvent, yaml.SequenceStartEvent)):
                depth += 1
                if depth > MAX_DEPTH:
                    raise YamlFileError(mark.line + 1, mark.column + 1, f"nested more than {MAX_DEPTH} levels", "too_deep")
            elif isinstance(event, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
                depth -= 1
    except yaml.YAMLError as err:
        raise _from_yaml(err) from None


def _from_yaml(err: yaml.YAMLError) -> YamlFileError:
    mark = getattr(err, "problem_mark", None)
    problem = getattr(err, "problem", None) or "not valid YAML"
    return YamlFileError(mark.line + 1 if mark else 1, mark.column + 1 if mark else 1, str(problem))


def _json_types(value: Any, where: tuple[Any, ...] = ()) -> None:
    if isinstance(value, dict):
        for v in value.values():
            _json_types(v)
    elif isinstance(value, list):
        for v in value:
            _json_types(v)
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        raise YamlFileError(1, 1, f"a value of type {type(value).__name__} is not allowed", "bad_value")


def load_yaml(text: str) -> tuple[Any, dict[str, tuple[int, int]]]:
    """`(data, {top-level key: (line, col)})`. Raises YamlFileError (line and column are 1-based)."""
    if len(text.encode("utf-8")) > MAX_FILE_BYTES:
        raise YamlFileError(1, 1, f"the file is larger than {MAX_FILE_BYTES // 1024} KB", "too_big")
    _check_events(text)
    lines: dict[str, tuple[int, int]] = {}
    try:
        loader = _Loader(text)
        try:
            node = loader.get_single_node()
            if node is None:
                raise YamlFileError(1, 1, "the file is empty", "empty")
            if isinstance(node, yaml.MappingNode):
                for key_node, _ in node.value:
                    if isinstance(key_node, yaml.ScalarNode):
                        lines[str(key_node.value)] = (key_node.start_mark.line + 1, key_node.start_mark.column + 1)
            data = loader.construct_document(node)
        finally:
            loader.dispose()
    except yaml.YAMLError as err:
        raise _from_yaml(err) from None
    _json_types(data)
    return data, lines


# ---- file names -----------------------------------------------------------------------------------------------------------

def file_name(directory: str, ident: str) -> str:
    """The path of the file for `ident` (an endpoint id may hold `:` or `/`; a changed name gets a short hash so two ids never
    share a file). The id stays inside the file; the name is only checked against it."""
    safe = _SAFE_NAME.sub("_", ident)[:80]
    if safe != ident or not safe:
        safe = f"{safe}~{hashlib.sha1(ident.encode()).hexdigest()[:6]}"
    return f"{directory}/{safe}.yaml"


# ---- to_files -------------------------------------------------------------------------------------------------------------

def settings_to_blocks(settings: dict[str, Any]) -> dict[str, dict[str, Any]]:
    blocks: dict[str, dict[str, Any]] = {}
    for key, value in settings.items():
        if key in _SETTING_PATHS:
            group, name = _SETTING_PATHS[key]
            blocks.setdefault(group, {})[name] = value
    return blocks


def to_files(document: dict[str, Any], elements: list[dict[str, Any]], settings: dict[str, Any],
             doors: dict[str, dict[str, Any]] | None = None, phones: dict[str, dict[str, Any]] | None = None) -> dict[str, str]:
    """The tree for a live configuration: `{path: text}`. `document` is the stored schema 6 content document, `elements` the
    stored elements in display order, `settings` the `ui_settings` keys (an unset live key is simply absent). `doors` and `phones`
    (P6) are the non-secret options of the door and phone config entries, `{key: record}` (see `validate_door` / `validate_phone`);
    left out, no `doors/` or `phones/` file is written. Pure."""
    files: dict[str, str] = {}
    root: dict[str, Any] = {"portico_config": CONFIG_VERSION}
    for key in _ROOT_DOC_KEYS:
        if key in document:
            root[key] = copy.deepcopy(document[key])
    for key, value in document.items():
        if key not in _SKIP_DOC_KEYS and key not in root:
            root[key] = copy.deepcopy(value)       # an extra the document keeps (add_extras): not lost
    root.update(settings_to_blocks(settings))
    files[ROOT_FILE] = dump_yaml(root)
    pages = document.get("pages") or []
    element_ids = []
    for page in pages:
        files[file_name("pages", page["id"])] = dump_yaml(page)
    for element in elements:
        directory = "managed" if element.get("managed") else "elements"
        files[file_name(directory, element["id"])] = dump_yaml(element)
        if directory == "elements":
            element_ids.append(element["id"])
    for key, device in (document.get("devices") or {}).items():
        files[file_name("devices", key)] = dump_yaml({"id": key, **device})
    for slug, dashboard in (document.get("dashboards") or {}).items():
        files[file_name("dashboards", slug)] = dump_yaml({"id": slug, **dashboard})
    for key, record in (doors or {}).items():
        files[file_name("doors", key)] = dump_yaml({"id": key, **record})
    for key, record in (phones or {}).items():
        files[file_name("phones", key)] = dump_yaml({"id": key, **record})
    files[INDEX_FILE] = dump_yaml({"portico_config": CONFIG_VERSION, "elements": element_ids,
                                   "pages": [p["id"] for p in pages]})
    return files


# ---- from_files -----------------------------------------------------------------------------------------------------------

@dataclass
class Loaded:
    document: dict[str, Any] = field(default_factory=dict)
    elements: list[dict[str, Any]] = field(default_factory=list)
    managed: list[dict[str, Any]] = field(default_factory=list)
    settings: dict[str, Any] = field(default_factory=dict)
    doors: dict[str, dict[str, Any]] = field(default_factory=dict)        # P6: door entry options, by door key
    phones: dict[str, dict[str, Any]] = field(default_factory=dict)       # P6: phone entry options, by phone key
    errors: list[ConfigError] = field(default_factory=list)
    lines: dict[str, dict[str, tuple[int, int]]] = field(default_factory=dict)      # file -> top-level key -> (line, col)


def _scan_keys(value: Any) -> tuple[str, str] | None:
    """The first key that names a secret or a forbidden action: `(code, key)`."""
    if isinstance(value, dict):
        for k, v in value.items():
            if k in _FORBIDDEN_KEYS:
                return "forbidden_key", k
            if is_secret_key(k):
                return "secret_in_yaml", k
            hit = _scan_keys(v)
            if hit:
                return hit
    elif isinstance(value, list):
        for v in value:
            hit = _scan_keys(v)
            if hit:
                return hit
    return None


def from_files(files: dict[str, str]) -> Loaded:
    """Parse and assemble a tree; every problem found is in `errors` (a file that fails to load is skipped, the rest are still
    read). The pieces are NOT yet validated: see `validate_files`."""
    out = Loaded()
    err = out.errors

    def fail(path: str, line: int, col: int, message: str, code: str = "invalid") -> None:
        err.append(ConfigError(path, line, col, message, code))

    if len(files) > MAX_FILES:
        fail("", 1, 1, f"{len(files)} files: at most {MAX_FILES}", "too_many_files")
        return out
    if sum(len(t.encode("utf-8")) for t in files.values()) > MAX_TREE_BYTES:
        fail("", 1, 1, f"the tree is larger than {MAX_TREE_BYTES // (1024 * 1024)} MB", "too_big")
        return out

    parsed: dict[str, Any] = {}
    failed: set[str] = set()
    for path in sorted(files):
        parts = path.split("/")
        allowed = (path in (ROOT_FILE, INDEX_FILE)
                   or (len(parts) == 2 and parts[0] in DIR_FILES and parts[1].endswith(".yaml") and parts[1] != ".yaml"))
        if any(p in ("", ".", "..") for p in parts) or path.startswith("/") or not allowed:
            if path == "README.md" or path.startswith(("schema/", "examples/")):
                continue            # generated or documentation files that sit beside the data
            fail(path, 1, 1, "this path is not part of the Portico layout", "bad_path")
            continue
        try:
            data, lines = load_yaml(files[path])
        except YamlFileError as e:
            fail(path, e.line, e.col, e.message, e.code)
            failed.add(path)
            continue
        if not isinstance(data, dict):
            fail(path, 1, 1, "the file must be a mapping", "not_a_mapping")
            failed.add(path)
            continue
        hit = _scan_keys(data)
        if hit:
            code, key = hit
            line, col = lines.get(key, (1, 1))
            fail(path, line, col, f"the key {key!r} is not allowed here" + (
                "; secrets are never kept in these files" if code == "secret_in_yaml" else ""), code)
            failed.add(path)
            continue
        parsed[path] = data
        out.lines[path] = lines

    root = parsed.get(ROOT_FILE)
    if root is None:
        if ROOT_FILE not in files:
            fail(ROOT_FILE, 1, 1, f"{ROOT_FILE} is missing", "missing_root")
        return out
    if root.get("portico_config") != CONFIG_VERSION:
        fail(ROOT_FILE, *out.lines[ROOT_FILE].get("portico_config", (1, 1)),
             f"portico_config must be {CONFIG_VERSION}", "bad_version")
    index = parsed.get(INDEX_FILE)

    document: dict[str, Any] = {"schema_version": 6}
    for key, value in root.items():
        if key in ("portico_config", "ring", "live"):
            continue
        document[key] = value
    for group in ("ring", "live"):
        block = root.get(group)
        if block is None:
            continue
        if not isinstance(block, dict):
            fail(ROOT_FILE, *out.lines[ROOT_FILE].get(group, (1, 1)), f"{group} must be a mapping")
            continue
        for name, value in block.items():
            setting = _SETTING_BY_PATH.get((group, name))
            if setting is None:
                fail(ROOT_FILE, *out.lines[ROOT_FILE].get(group, (1, 1)), f"{group}.{name} is not a setting")
            else:
                out.settings[setting] = value

    def collect(directory: str) -> dict[str, tuple[str, dict[str, Any]]]:
        """`{id: (path, data)}` of one directory, checking each file's name against the id inside it."""
        found: dict[str, tuple[str, dict[str, Any]]] = {}
        for path, data in parsed.items():
            if not path.startswith(directory + "/"):
                continue
            ident = data.get("id")
            if not isinstance(ident, str) or not ident:
                fail(path, *out.lines[path].get("id", (1, 1)), "the file needs an id", "no_id")
            elif path != file_name(directory, ident):
                fail(path, *out.lines[path].get("id", (1, 1)), f"the file name does not match the id {ident!r}", "name_mismatch")
            elif ident in found:
                fail(path, 1, 1, f"the id {ident!r} is used by two files", "duplicate_id")
            else:
                found[ident] = (path, data)
        return found

    pages = collect("pages")
    devices = collect("devices")
    dashboards = collect("dashboards")
    elements = collect("elements")
    managed = collect("managed")
    out.doors = {i: {k: v for k, v in data.items() if k != "id"} for i, (_, data) in sorted(collect("doors").items())}
    out.phones = {i: {k: v for k, v in data.items() if k != "id"} for i, (_, data) in sorted(collect("phones").items())}

    def ordered(found: dict[str, tuple[str, dict[str, Any]]], wanted: Any, label: str) -> list[str]:
        """The ids in the order index.yaml gives; anything the index leaves out follows alphabetically."""
        names = []
        if wanted is not None:
            if not isinstance(wanted, list) or not all(isinstance(w, str) for w in wanted):
                fail(INDEX_FILE, *out.lines[INDEX_FILE].get(label, (1, 1)), f"{label} must be a list of ids")
                wanted = []
            for w in dict.fromkeys(wanted):
                if w in found:
                    names.append(w)
                elif file_name(label, w) not in failed:         # a file that failed to load is already reported
                    fail(INDEX_FILE, *out.lines[INDEX_FILE].get(label, (1, 1)), f"{label}: there is no file for {w!r}", "unknown_id")
        return names + sorted(i for i in found if i not in names)

    idx = index or {}
    if index is not None and index.get("portico_config") != CONFIG_VERSION:
        fail(INDEX_FILE, *out.lines[INDEX_FILE].get("portico_config", (1, 1)), f"portico_config must be {CONFIG_VERSION}", "bad_version")
    document["pages"] = [{k: v for k, v in pages[i][1].items()} for i in ordered(pages, idx.get("pages"), "pages")]
    document["devices"] = {i: {k: v for k, v in data.items() if k != "id"} for i, (_, data) in sorted(devices.items())}
    document["dashboards"] = {i: {k: v for k, v in data.items() if k != "id"} for i, (_, data) in sorted(dashboards.items())}
    out.document = document
    out.elements = [dict(elements[i][1]) for i in ordered(elements, idx.get("elements"), "elements")]
    out.managed = [dict(managed[i][1]) for i in sorted(managed)]
    return out


# ---- door and phone entry options (P6) ----------------------------------------------------------------------------------------
# The files carry only what the entries' own forms let a person set and that is not a secret and not an action. What a door OPENS
# (`open_relay_action`), every password, key and token, and the entry's identity stay out (section 8: a merged file must not be
# able to change what opens a door). A key a file leaves out means "leave the live value alone"; `null` is a value
# (`ring_answered_entity: null` = no helper, `doors: null` = every door), so a short file never switches anything off by omission.

SERVER_DRIVER = "gate_sip_server"            # the only door driver whose settings a person edits (door/gate_sip_listen.py)
DOOR_EDITABLE = ("dial_enabled", "house_bridge", "ring_answered_entity")
_DOOR_KEYS = frozenset({"name", "driver", *DOOR_EDITABLE})
_PHONE_KEYS = frozenset({"name", "doors", "house_receive", "panel"})
_HELPER = re.compile(r"^input_boolean\.[a-z0-9_]{1,200}$")
MAX_PHONE_DOORS = 64


class EntryOptionError(Exception):
    def __init__(self, code: str, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.code, self.field = code, field


def _unknown_keys(raw: dict[str, Any], allowed: frozenset[str]) -> None:
    for key in raw:
        if key not in allowed:
            raise EntryOptionError("unknown_key", f"{key!r} is not a setting here", key)


def _bool(raw: dict[str, Any], key: str) -> None:
    if key in raw and not isinstance(raw[key], bool):
        raise EntryOptionError("invalid_value", "must be true or false", key)


def _short_text(raw: dict[str, Any], key: str, limit: int) -> None:
    if key in raw and not (isinstance(raw[key], str) and 0 < len(raw[key]) <= limit):
        raise EntryOptionError("invalid_value", f"must be text of 1 to {limit} characters", key)


def validate_door(raw: dict[str, Any]) -> dict[str, Any]:
    """One `doors/<key>.yaml` record (without its `id`): `name` and `driver` say which door it is (read-only, checked against the
    live entry), the rest are the editable settings of a gate_sip_server door. Pure: whether the helper exists, the wheel has the
    bridge or the driver matches is the apply's check against the house."""
    _unknown_keys(raw, _DOOR_KEYS)
    _short_text(raw, "name", 120)
    _short_text(raw, "driver", 60)
    _bool(raw, "dial_enabled")
    _bool(raw, "house_bridge")
    if "ring_answered_entity" in raw and raw["ring_answered_entity"] is not None and not (
            isinstance(raw["ring_answered_entity"], str) and _HELPER.match(raw["ring_answered_entity"])):
        raise EntryOptionError("invalid_value", "must be an input_boolean entity id or null", "ring_answered_entity")
    if "driver" not in raw:
        raise EntryOptionError("missing_key", "the driver is needed (it says which kind of door this is)", "driver")
    if raw["driver"] != SERVER_DRIVER and any(k in raw for k in DOOR_EDITABLE):
        raise EntryOptionError("not_editable", f"a {raw['driver']} door has no settings here", next(k for k in DOOR_EDITABLE if k in raw))
    return copy.deepcopy(raw)


def validate_phone(raw: dict[str, Any]) -> dict[str, Any]:
    """One `phones/<key>.yaml` record (without its `id`): `doors` (a list of door ids, or null for every door), `house_receive`,
    and `panel` (read-only: making a phone a panel does more than set the flag, so it stays the Devices tab's action)."""
    _unknown_keys(raw, _PHONE_KEYS)
    _short_text(raw, "name", 120)
    _bool(raw, "house_receive")
    _bool(raw, "panel")
    doors = raw.get("doors")
    if "doors" in raw and doors is not None:
        if not isinstance(doors, list) or len(doors) > MAX_PHONE_DOORS or not all(isinstance(d, str) and 0 < len(d) <= 200 for d in doors):
            raise EntryOptionError("invalid_value", f"must be null or a list of at most {MAX_PHONE_DOORS} door ids", "doors")
        if len(set(doors)) != len(doors):
            raise EntryOptionError("duplicate_id", "a door is listed twice", "doors")
    if raw.get("panel") is True and doors is not None:
        raise EntryOptionError("invalid_value", "a panel answers every door: doors must be null", "doors")
    return copy.deepcopy(raw)


# ---- validation -----------------------------------------------------------------------------------------------------------

@dataclass
class Validation:
    ok: bool
    errors: list[ConfigError]
    loaded: Loaded
    counts: dict[str, int]
    document: dict[str, Any] | None = None       # the validated, normalised form (None unless ok)
    elements: list[dict[str, Any]] | None = None
    settings: dict[str, Any] | None = None
    doors: dict[str, dict[str, Any]] | None = None      # P6: the validated door and phone records
    phones: dict[str, dict[str, Any]] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "errors": [e.as_dict() for e in self.errors], "counts": self.counts}


def _owner_file(loaded: Loaded, message: str) -> tuple[str, int, int]:
    """Which file a document-level refusal is about: the file of a page, device or screen the message names, else portico.yaml."""
    best: tuple[int, str] | None = None
    for directory, items in (("pages", loaded.document.get("pages") or []), ("devices", loaded.document.get("devices") or {}),
                             ("dashboards", loaded.document.get("dashboards") or {})):
        ids = [p["id"] for p in items] if isinstance(items, list) else list(items)
        for ident in ids:
            hit = re.search(rf"(?<![A-Za-z0-9_-]){re.escape(ident)}(?![A-Za-z0-9_-])", message) if isinstance(ident, str) else None
            path = file_name(directory, ident) if hit else None
            if hit and path in loaded.lines and (best is None or hit.start() < best[0]):
                best = (hit.start(), path)       # the subject of a message is named first
    return (best[1], 1, 1) if best else (ROOT_FILE, 1, 1)


def validate_files(files: dict[str, str], *, document_validator: Callable[[Any], dict[str, Any]] | None = None) -> Validation:
    """Load and validate a tree with the editor's own pure validators. `document_validator` replaces the pure document check
    (a caller with a live store passes `ContentStore.prepare_document`). Writes nothing. All problems are collected."""
    loaded = from_files(files)
    errors = list(loaded.errors)

    def add(path: str, key: str | None, message: str, code: str = "invalid") -> None:
        line, col = loaded.lines.get(path, {}).get(key, (1, 1)) if key else (1, 1)
        errors.append(ConfigError(path, line, col, message, code))

    elements: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in loaded.elements:
        path = file_name("elements", raw["id"]) if isinstance(raw.get("id"), str) else "elements"
        try:
            if "managed" in raw or MANAGED_ID_RE.match(str(raw.get("id", ""))):
                raise ElementError("invalid", "the managed Talk elements are made by the server and cannot be written here", field="id")
            clean = validate_element(raw)
        except ElementError as e:
            add(path, e.field, f"{e.field or 'element'}: {e}", e.code)
            continue
        if "id" not in clean or clean["id"] in seen:
            add(path, "id", "an element needs a unique id", "duplicate_id")
            continue
        seen.add(clean["id"])
        elements.append(clean)

    settings: dict[str, Any] = {}
    for key, value in loaded.settings.items():
        group, name = _SETTING_PATHS[key]
        clean = parse_patch({key: value})
        if clean is None:
            add(ROOT_FILE, group, f"{group}.{name}: not a valid value", "invalid_setting")
        else:
            settings.update(clean)

    document = None
    if not loaded.errors:                       # a document built from files that did not load would only add noise
        try:
            document = (document_validator or normalize_document_v6)(copy.deepcopy(loaded.document))
        except ProfileError as e:
            path, line, col = _owner_file(loaded, str(e))
            errors.append(ConfigError(path, line, col, str(e), "invalid_document"))

    counts = {"pages": len(loaded.document.get("pages") or []), "devices": len(loaded.document.get("devices") or {}),
              "dashboards": len(loaded.document.get("dashboards") or {}), "elements": len(loaded.elements),
              "settings": len(loaded.settings), "doors": len(loaded.doors), "phones": len(loaded.phones)}
    doors: dict[str, dict[str, Any]] = {}
    for key, raw in loaded.doors.items():
        path = file_name("doors", key)
        try:
            doors[key] = validate_door(raw)
        except EntryOptionError as e:
            add(path, e.field, f"{e.field or 'door'}: {e}", e.code)
    phones: dict[str, dict[str, Any]] = {}
    for key, raw in loaded.phones.items():
        path = file_name("phones", key)
        try:
            phones[key] = validate_phone(raw)
        except EntryOptionError as e:
            add(path, e.field, f"{e.field or 'phone'}: {e}", e.code)
    ok = not errors
    return Validation(ok, errors, loaded, counts, document if ok else None, elements if ok else None, settings if ok else None,
                      doors if ok else None, phones if ok else None)


# ---- the equality proof ---------------------------------------------------------------------------------------------------

def _same(a: Any, b: Any) -> bool:
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def prove_roundtrip(document: dict[str, Any], elements: list[dict[str, Any]], settings: dict[str, Any]) -> list[str]:
    """Problems found, empty when the configuration survives `to_files` then `validate_files` unchanged and the text is a fixed
    point (a second emit is byte-identical). Writes nothing. (The per-endpoint `rev` proof needs the live resolver: P2.)"""
    problems: list[str] = []
    files = to_files(document, [e for e in elements if not e.get("managed")], settings)
    result = validate_files(files)
    if not result.ok:
        return [f"{e.file}:{e.line}:{e.col}: {e.message}" for e in result.errors]
    if not _same(result.document, document):
        problems.append("the document changed on the way through the files")
    if not _same(result.elements, [e for e in elements if not e.get("managed")]):
        problems.append("the elements changed on the way through the files")
    if not _same(result.settings, settings):
        problems.append("the settings changed on the way through the files")
    if to_files(result.document, result.elements, result.settings) != files:
        problems.append("the files are not a fixed point: a second emit differs")
    return problems
