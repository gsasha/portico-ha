"""The house-call model stays out of the old system and the door path, and is switched off, on the SOURCE (M7 3a; design
15.4 rules 1 to 3, 15.14). Read with `ast`, so a docstring that says what the module must never touch does not trip it; only
code does (an import, a name, a string that is not a docstring)."""
from __future__ import annotations

import ast
import re
from pathlib import Path

PART = Path(__file__).resolve().parent.parent
HOUSE_MODULES = ("house.py", "house_ledger.py", "house_commands.py", "house_bridge.py", "house_attach.py")

# Everything of the old intercom system (design 15.14): helpers, services, surfaces, automations.
OLD_SYSTEM = (
    "intercom_house_call", "intercom_house_declined", "intercom_house_roster", "intercom_call_id",
    "intercom_call_the_phones", "gate_ring_answered", "input_text", "input_button", "input_boolean", "house-talk",
    "bell-talk", "gate-auth", "home-house", "home-intercom", "tablet-screensaver", "kitchen-panel", "intercom_the_kitchen_panel",
    "panel_reload_the_hidden_webview", "automation.",
)
# The door path: nothing a house call may feed or call.
DOOR_PATH = ("CallStateTracker", "note_call", "_require_call", "RingNotifier", "handle_transition", "apply_event",
             "open_relay", "provider_impl", "add_outbound_listener")


def _code_tokens(path: Path) -> tuple[set[str], set[str], set[str]]:
    """(identifiers and attribute names, non-docstring strings, imported modules) of one source file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))
    names, strings, modules = set(), set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            strings.add(node.value)
        elif isinstance(node, ast.Import):
            modules.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.add(("." * node.level) + (node.module or ""))
            modules.update(a.name for a in node.names)
    return names, strings, modules


def test_the_house_modules_never_name_the_old_system():
    for name in HOUSE_MODULES:
        names, strings, modules = _code_tokens(PART / name)
        for old in OLD_SYSTEM:
            assert not [s for s in strings if old in s], (name, old, "as a string")
            assert not [m for m in modules if old.rstrip(".") in m], (name, old, "as an import")
            assert old.rstrip(".") not in names, (name, old, "as a name")


def test_the_house_modules_call_no_service_and_write_no_state():
    for name in HOUSE_MODULES:
        names, _strings, _modules = _code_tokens(PART / name)
        assert not names & {"async_call", "services", "async_set", "async_remove", "async_fire"}, name


def test_the_house_modules_never_touch_the_door_path():
    for name in HOUSE_MODULES:
        names, strings, modules = _code_tokens(PART / name)
        assert not names & set(DOOR_PATH), (name, names & set(DOOR_PATH))
        assert not [m for m in modules if "notifier" in m or "call_state" in m or "portico_intercom" in m], name


def test_the_door_path_and_the_driver_part_never_import_the_house_modules():
    for path in PART.glob("*.py"):
        if path.name in HOUSE_MODULES:
            continue
        _names, _strings, modules = _code_tokens(path)
        importing = {m for m in modules if m.endswith(("house", "house_ledger", "house_commands", "house_bridge"))}
        # the only importers: the wiring that must know about house calls (domain setup, signaling, the auth table's
        # command names are strings, the phone entry's lazy hook, content's lazy roster hook)
        # away_media.py: 3b-S hands an `hso-` media_frame / media_close to house_commands (lazy import, house ids only)
        # door_commands.py (UI split P5): the idle rule asks the registry whether a house call is up (active_count), nothing else
        assert path.name in {"__init__.py", "signaling.py", "phone_entry.py", "content.py", "away_media.py",
                             "door_commands.py", "panel_call.py"} or not importing, (
            path.name, importing)
    for path in (PART / "push").glob("*.py"):
        assert not {m for m in _code_tokens(path)[2] if "house" in m}, path.name
    driver = PART / "door"
    for path in driver.rglob("*.py"):
        if "tests" in path.parts:
            continue
        assert "portico.house" not in path.read_text(encoding="utf-8"), path.name


def test_nothing_in_the_source_switches_house_calls_on():
    """The option is read in one place and written in none; no source file passes `enabled=True`. (The door entry's own
    `house_calls` option, the hub's, is written by the Settings tab through the driver part's constant `CONF_HOUSE_CALLS`, never a
    string of this directory's; it is not the settings entry's option that builds the registry.)"""
    holders = set()
    for path in PART.glob("*.py"):
        _names, strings, _modules = _code_tokens(path)
        if "house_calls" in strings:
            holders.add(path.name)
    assert holders == {"house.py"}
    for path in PART.rglob("*.py"):
        if "tests" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.keyword) and node.arg == "enabled" and isinstance(node.value, ast.Constant):
                raise AssertionError(f"{path.name}:{node.value.lineno}: a literal `enabled=` in source switches it on")
    assert 'OPTION_HOUSE_CALLS = "house_calls"' in (PART / "house.py").read_text(encoding="utf-8")
    # and no www file but the kiosk card can send the commands (M7 3d: the card's answer / decline / hang up; it has no way
    # to place a call or switch the model on, and draws nothing until the server's roster carries the Everyone key)
    for path in (PART / "www").glob("*.js"):
        if path.name == "portico-card.js":
            continue
        assert "portico/house/" not in path.read_text(encoding="utf-8"), path.name
    card = (PART / "www" / "portico-card.js").read_text(encoding="utf-8")
    assert set(re.findall(r'"portico/house/(\w+)"', card)) == {"answer", "decline", "hangup"}


def _function_names_in(path: Path, class_name: str) -> dict[str, ast.AST]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    (cls,) = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == class_name]
    return {n.name: n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def test_the_house_message_path_in_the_notifier_is_separate_from_the_door_path():
    """3c sends house messages (payload.build_house_*) through methods of their own. The door path (the transition handler and
    everything it reaches before the fan-out) names no house builder and passes no `ct` / `from_ref`; the house methods never
    call the door builders; and the house path is called by nothing in `push/` but the manager's thin delegates."""
    methods = _function_names_in(PART / "push" / "notifier.py", "RingNotifier")
    house = {name for name in methods if name.startswith("house_") or name == "_house_ring"}
    assert house >= {"house_ring", "house_answered", "house_left", "house_ended", "house_targets", "_house_ring"}
    door = ("handle_transition", "_handle", "_finish", "_start_ring", "_device_allows")
    for name in door:
        seen = {n.id for n in ast.walk(methods[name]) if isinstance(n, ast.Name)} | \
               {n.attr for n in ast.walk(methods[name]) if isinstance(n, ast.Attribute)} | \
               {n.arg for n in ast.walk(methods[name]) if isinstance(n, ast.keyword) and n.arg}
        assert not {x for x in seen if "house" in x.lower()} and not seen & {"ct", "from_ref"}, (name, seen)
    for name in house:
        seen = {n.attr for n in ast.walk(methods[name]) if isinstance(n, ast.Attribute)}
        assert not seen & {"build_ring", "build_answered", "build_ended", "_start_ring", "_handle", "_finish"}, (name, seen)
    # the notifier keeps house calls out of the door tables
    for name in house:
        seen = {n.attr for n in ast.walk(methods[name]) if isinstance(n, ast.Attribute)}
        assert not seen & {"_calls", "_finished"}, (name, seen)


def test_nothing_but_the_house_registry_and_the_manager_calls_the_house_message_path():
    users = set()
    for path in PART.rglob("*.py"):
        if "tests" in path.parts:
            continue
        attrs = _code_tokens(path)[0]
        if attrs & {"house_ring", "house_answered", "house_left", "house_ended"}:
            users.add(path.relative_to(PART).as_posix())
    assert users == {"house.py", "push/__init__.py"}
