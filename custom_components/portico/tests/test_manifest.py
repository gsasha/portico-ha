"""The one manifest (STRUCT-01, STRUCT-03, STRUCT-05) and the wheel pin that used to live in the driver manifest.

The fold of `portico_intercom` into `portico` (DEV-686) leaves ONE manifest, domain `portico`. The transport wheel is
NOT a requirement of it: a URL requirement is handed to pip on every load, which would make the whole integration (phones,
push, the panel) fail to load whenever the package host is unreachable. The pin is a constant in `door/wheel.py` and the
hub installs the wheel lazily, only when the installed one is older (`test_wheel.py`).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

PART = Path(__file__).resolve().parent.parent


def _manifest() -> dict:
    return json.loads((PART / "manifest.json").read_text(encoding="utf-8"))


def test_the_domain_equals_the_folder():
    assert _manifest()["domain"] == PART.name == "portico"


def test_the_only_requirement_is_qrcode_and_there_is_no_wheel_pin():
    """STRUCT-03 after the fold: no compiled wheel in the manifest (it would be re-installed from the package host on
    every boot, and a down host would block the whole integration); the pin is in door/wheel.py."""
    assert _manifest()["requirements"] == ["qrcode>=7.4"]
    assert "portico_transport" not in json.dumps(_manifest())


def test_the_pin_constants_agree_and_are_not_below_the_floor():
    """REL-04: the pin is never below MIN_WHEEL_VERSION (hub.py; a pin below it would refuse every door), and the tag,
    the file name and the version in the pin say the same thing. MIN_WHEEL_VERSION rises only when this Python relies
    on something the newer wheel adds, so the pin may be ahead of it. (Bumping the wheel is these two constants.)"""
    from custom_components.portico.door.hub import MIN_WHEEL_VERSION
    from custom_components.portico.door.wheel import WHEEL_PIN_URL, WHEEL_PIN_URLS, WHEEL_PIN_VERSION, WHEEL_REQUIREMENT, pin_url_for

    # Every per-architecture URL says the same version in tag and file name, in either tag family (Go `transport-v`,
    # Rust `transport-rs-v`), and ends in its own platform tag (plain `linux_<arch>`).
    assert "x86_64" in WHEEL_PIN_URLS and WHEEL_PIN_URL == WHEEL_PIN_URLS["x86_64"]
    for arch, url in WHEEL_PIN_URLS.items():
        found = re.search(r"/transport(?:-rs)?-v(\d+\.\d+\.\d+)/portico_transport-(\d+\.\d+\.\d+)-", url)
        assert found and found.group(1) == found.group(2) == WHEEL_PIN_VERSION, (arch, url)
        assert url.split("#")[0].endswith(f"-py3-none-linux_{arch}.whl"), (arch, url)
    assert pin_url_for("x86_64") == WHEEL_PIN_URL and pin_url_for("riscv64") == WHEEL_PIN_URL    # unknown machine: x86_64's
    assert WHEEL_REQUIREMENT == f"portico_transport @ {pin_url_for()}"
    assert tuple(map(int, WHEEL_PIN_VERSION.split("."))) >= tuple(map(int, MIN_WHEEL_VERSION.split(".")))


def test_dependencies_and_after_dependencies():
    manifest = _manifest()
    assert manifest["dependencies"] == ["file_upload"]
    assert manifest["after_dependencies"] == ["network", "frontend"]       # no `portico_intercom`: it is gone


def test_identity_fields():
    manifest = _manifest()
    assert (manifest["name"], manifest["config_flow"], manifest["iot_class"]) == ("Portico", True, "local_push")
    assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"])
    assert manifest["codeowners"] == ["@gsasha"]


def test_the_folder_has_what_a_custom_integration_needs():
    for name in ("__init__.py", "config_flow.py", "diagnostics.py", "strings.json", "translations/en.json",
                 "services.yaml", "brand/icon.png", "brand/icon@2x.png", "tests/__init__.py", "camera.py", "lock.py",
                 "sensor.py", "door/__init__.py", "door/hub.py", "door/wheel.py", "door/drivers/__init__.py"):
        assert (PART / name).exists(), name


def test_the_old_integration_folder_is_gone():
    """A stale copy of custom_components/portico_intercom would load beside this one and fight for the hub socket."""
    assert not (PART.parent / "portico_intercom").exists()
