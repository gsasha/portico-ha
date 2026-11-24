"""Invariants of the door side that the driver part's `test_boundary.py` pinned and that outlive the split (PROC-01,
PROC-02, S-2). The boundary tests between the two parts themselves are gone with the seam; these three are about the
door package alone. A tree is read with `ast`, so a comment or a docstring that explains an invariant does not trip it.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

DOOR = Path(__file__).resolve().parent.parent / "door"


def _docstrings(tree: ast.AST) -> set[int]:
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                out.add(id(body[0].value))
    return out


def _source_files():
    return [p for p in sorted(DOOR.rglob("*.py")) if "tests" not in p.relative_to(DOOR).parts]


def test_the_door_package_is_where_the_door_code_is():
    assert {p.name for p in DOOR.glob("*.py")} >= {"hub.py", "door.py", "transport.py", "call_state.py", "wheel.py",
                                                   "provider_impl.py", "flow.py", "const.py"}
    assert {p.stem for p in (DOOR / "drivers").glob("*.py")} == {"__init__", "dahua_sip", "gate_sip_server",
                                                                          "ha_event", "unifi_protect"}


def test_only_the_server_drivers_own_module_names_the_sip_server_mode():
    """DEV-518 W2 turned the E1 guard around: the SIP-server mode now has a Python driver entry, but only its own
    module (`drivers/gate_sip_server.py`) may name the key; the other drivers must not, and the entry stays hidden
    from the "Add a door" picker (it has its own explicit menu item)."""
    for path in sorted((DOOR / "drivers").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if path.name == "gate_sip_server.py":
            assert 'KEY = "gate_sip_server"' in text and "hidden=True" in text
        elif path.name != "__init__.py":    # __init__ only imports the module for registration
            assert "gate_sip_server" not in text, path.name


def test_process_and_socket_code_lives_only_in_hub_py():
    """P-3 (PROC-01, PROC-02): one process per HA instance, owned by hub.py. No other module spawns a process or
    names the socket directory, and door.py (the per-door runtime) has no process code at all."""
    words = ("subprocess", "create_subprocess_exec", "SOCKET_DIR", "SOCKET_NAME", "hub.sock", "binary_path")
    offences = []
    for path in _source_files():
        if path.name == "hub.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docs = _docstrings(tree)
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Name):
                names = [node.id]
            elif isinstance(node, ast.Attribute):
                names = [node.attr]
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
                names = [node.value]
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names] + [getattr(node, "module", None) or ""]
            offences += [f"{path.relative_to(DOOR)}: {w}" for n in names for w in words if w in n]
    assert offences == []


def test_no_entries_zero_anywhere():
    """PROC-02: nothing picks 'the first entry' or 'the first door'; every lookup is by door id or call id."""
    pattern = re.compile(r"entries\[0\]|_doors\.values\(\)\)\[0\]|_attached\.values\(\)\)\[0\]|next\(iter\(")
    offences = [str(p.relative_to(DOOR)) for p in _source_files() if pattern.search(p.read_text(encoding="utf-8"))]
    assert offences == []
