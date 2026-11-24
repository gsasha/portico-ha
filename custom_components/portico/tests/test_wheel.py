# ruff: noqa: F811 - test parameters named after the imported fixtures
"""The transport wheel is no longer a manifest requirement: it is installed lazily, by the hub, only when needed.

Design (portico_unify_design.md section 2): the pin is a constant in `door/wheel.py`; `async_ensure_wheel` runs in
`hub.async_attach` BEFORE `_check_wheel`; it installs only when the installed wheel is older than the pin (a version
compare, no network on a normal boot); it clears HA's sticky install-failure history first and calls
`importlib.invalidate_caches()` after; a failure raises a repair issue and `ConfigEntryNotReady` (the door waits,
everything else runs). `pip` itself is stubbed in every test (`test_observer._custom_integrations`).
"""
from __future__ import annotations

import importlib.metadata

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import issue_registry as ir
from homeassistant.requirements import RequirementsNotFound

from custom_components.portico.door import hub as portico_hub
from custom_components.portico.door import wheel as wheel_module
from custom_components.portico.door.const import DOMAIN
from custom_components.portico.door.wheel import WHEEL_PIN_VERSION, WHEEL_REQUIREMENT

from .test_push_registry import _push_entry, google  # noqa: F401 - fixture by name
from .test_observer import (  # noqa: F401 - fixtures by name
    WHEEL_INSTALLS,
    _custom_integrations,
    _observer_entry,
    fake_transport,
)


class _Installed:
    """The installed wheel's version, as `importlib.metadata.version` reports it, and what a `pip install` does to it."""

    def __init__(self, monkeypatch, version, *, install_to=None, fail=None):
        self.version = version            # None: not installed (PackageNotFoundError)
        self.events: list[str] = []
        self.fail = fail

        def _version(name):
            if self.version is None:
                raise importlib.metadata.PackageNotFoundError(name)
            return self.version

        async def _pip(hass, name, requirements, is_built_in=True):
            self.events.append("pip")
            WHEEL_INSTALLS.append((name, list(requirements)))
            if self.fail is not None:
                raise self.fail
            if install_to is not None:
                self.version = install_to

        monkeypatch.setattr(portico_hub.importlib.metadata, "version", _version)
        monkeypatch.setattr(wheel_module, "async_process_requirements", _pip)
        monkeypatch.setattr(wheel_module, "async_clear_install_history",
                            lambda hass: self.events.append("clear_history"))
        monkeypatch.setattr(wheel_module.importlib, "invalidate_caches", lambda: self.events.append("invalidate"))


async def test_a_wheel_at_the_pin_costs_no_install_and_no_network(hass, fake_transport, monkeypatch):
    """The normal boot: the pinned wheel is installed, so a Gitea outage at the restart costs nothing."""
    wheel = _Installed(monkeypatch, WHEEL_PIN_VERSION)
    entry = _observer_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is True
    assert wheel.events == [] and WHEEL_INSTALLS == []
    assert entry.state is ConfigEntryState.LOADED


@pytest.mark.parametrize("version", ["0.7.4", "1.0.0"])
async def test_a_newer_wheel_is_left_alone(hass, fake_transport, monkeypatch, version):
    wheel = _Installed(monkeypatch, version)
    entry = _observer_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is True
    assert wheel.events == [] and WHEEL_INSTALLS == []


@pytest.mark.parametrize("version", ["0.4.1", "0.5.0", "0.5.1", "0.6.0", "0.6.1", "0.6.2", "0.6.3", "0.6.4", "0.6.5", "0.6.6", "0.6.7", "0.6.8", "0.6.9", "0.6.10", "0.7.0", "0.7.1", "0.7.2", "0.7.3"])
async def test_an_older_wheel_is_installed_before_the_floor_check_so_it_never_trips_it(
        hass, fake_transport, monkeypatch, version):
    """0.4.1 is below the floor (`WheelTooOld` would refuse every door); the install runs FIRST, so the door starts."""
    wheel = _Installed(monkeypatch, version, install_to=WHEEL_PIN_VERSION)
    entry = _observer_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is True
    assert WHEEL_INSTALLS == [("portico_transport", [WHEEL_REQUIREMENT])]
    assert wheel.events == ["clear_history", "pip", "invalidate"]       # sticky failures forgotten first, caches after
    assert ir.async_get(hass).async_get_issue(DOMAIN, "wheel_too_old") is None
    assert ir.async_get(hass).async_get_issue(DOMAIN, "wheel_install_failed") is None


async def test_a_missing_wheel_is_installed(hass, fake_transport, monkeypatch):
    wheel = _Installed(monkeypatch, None, install_to=WHEEL_PIN_VERSION)
    entry = _observer_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is True
    assert WHEEL_INSTALLS == [("portico_transport", [WHEEL_REQUIREMENT])] and wheel.events[0] == "clear_history"


async def test_an_install_that_leaves_an_old_wheel_still_hits_the_floor_check(hass, fake_transport, monkeypatch):
    """pip bypassed (`skip_pip`) or a stale index: the install returns, the wheel is still 0.4.1, and REL-04 refuses."""
    _Installed(monkeypatch, "0.4.1")
    entry = _observer_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is False
    assert ir.async_get(hass).async_get_issue(DOMAIN, "wheel_too_old") is not None
    assert fake_transport.spawned == []


async def test_a_failed_install_waits_with_a_repair_issue_and_spawns_nothing(hass, fake_transport, monkeypatch):
    wheel = _Installed(monkeypatch, "0.6.1", fail=RequirementsNotFound("portico_transport", [WHEEL_REQUIREMENT]))
    entry = _observer_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is False
    assert entry.state is ConfigEntryState.SETUP_RETRY                   # HA retries with its own backoff
    issue = ir.async_get(hass).async_get_issue(DOMAIN, "wheel_install_failed")
    assert issue is not None and issue.severity == ir.IssueSeverity.ERROR
    assert issue.translation_key == "wheel_install_failed"
    assert issue.translation_placeholders == {"required": WHEEL_PIN_VERSION, "reason": "RequirementsNotFound"}
    assert fake_transport.spawned == [] and fake_transport.instances == []
    assert wheel.events == ["clear_history", "pip"]                      # no `invalidate`: nothing was installed


async def test_a_retry_clears_the_sticky_failure_first_and_then_succeeds(hass, fake_transport, monkeypatch):
    """HA caches a failed requirement for the run: a retry that did not clear it first would fail without trying."""
    wheel = _Installed(monkeypatch, "0.6.1", fail=RequirementsNotFound("portico_transport", [WHEEL_REQUIREMENT]))
    entry = _observer_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is False
    assert ir.async_get(hass).async_get_issue(DOMAIN, "wheel_install_failed") is not None

    wheel.fail, wheel.events = None, []
    wheel.version = "0.6.1"
    monkeypatch.setattr(wheel_module, "async_process_requirements", _succeeding_pip(wheel))
    await hass.config_entries.async_unload(entry.entry_id)               # SETUP_RETRY: reload by hand, like the retry timer
    assert await hass.config_entries.async_setup(entry.entry_id) is True
    assert wheel.events == ["clear_history", "pip", "invalidate"]
    assert ir.async_get(hass).async_get_issue(DOMAIN, "wheel_install_failed") is None
    assert entry.state is ConfigEntryState.LOADED


def _succeeding_pip(wheel):
    async def _pip(hass, name, requirements, is_built_in=True):
        wheel.events.append("pip")
        wheel.version = WHEEL_PIN_VERSION

    return _pip


async def test_a_failed_door_install_leaves_the_other_entry_kinds_running(hass, hass_storage, fake_transport,
                                                                           google, monkeypatch):
    """Phones, push and the panel must not depend on the package host: the integration loads and the push ring notification serves
    while the door waits for its wheel."""
    _Installed(monkeypatch, "0.6.1", fail=RequirementsNotFound("portico_transport", [WHEEL_REQUIREMENT]))
    door = _observer_entry(hass)
    assert await hass.config_entries.async_setup(door.entry_id) is False      # loads the whole domain, the door waits
    assert door.state is ConfigEntryState.SETUP_RETRY and DOMAIN in hass.config.components
    push = _push_entry(hass, hass_storage, google)
    assert await hass.config_entries.async_setup(push.entry_id)
    await hass.async_block_till_done()
    assert push.state is ConfigEntryState.LOADED and hass.states.get("sensor.portico_push_ring_notification").state == "ok"


def test_the_requirement_reaches_pip_as_a_label_and_a_list():
    """`async_process_requirements(hass, name, requirements, is_built_in)`: a label, a list, and not-built-in."""
    import inspect

    from homeassistant.requirements import async_process_requirements

    assert list(inspect.signature(async_process_requirements).parameters) == ["hass", "name", "requirements", "is_built_in"]


# --- Rust transport rollback (DEV-764 Phase 4). Nothing here changes the pin: the constants are patched per test. ---

_RUST_INSTALLED = "0.7.0"                    # a Rust wheel (packaging/rust) is the installed lineage
_GO_ROLLBACK = "0.9.0"                       # the Go build re-released above every Rust version (the old Go rollback line, retired: D-20261119-c1af)


def _pin_to(monkeypatch, version, tag_family="transport-v"):
    url = (f"https://github.com/gsasha/portico-ha/releases/download/{tag_family}{version}/"
           f"portico_transport-{version}-py3-none-linux_x86_64.whl")
    monkeypatch.setattr(wheel_module, "WHEEL_PIN_VERSION", version)
    monkeypatch.setattr(wheel_module, "WHEEL_PIN_URL", url)
    monkeypatch.setattr(wheel_module, "WHEEL_REQUIREMENT", f"portico_transport @ {url}")
    return f"portico_transport @ {url}"


async def test_repinning_to_the_old_go_wheel_over_a_rust_wheel_does_nothing(hass, fake_transport, monkeypatch):
    """THE TRAP the plan found: the installed wheel is Rust 0.7.0, the pin goes back to Go 0.6.10 and the installer
    says 'installed >= pin', so nothing is installed and the Rust binary keeps running. Repinning is not a rollback."""
    wheel = _Installed(monkeypatch, _RUST_INSTALLED)
    _pin_to(monkeypatch, "0.6.10")
    entry = _observer_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is True
    assert wheel.events == [] and WHEEL_INSTALLS == []
    assert wheel.version == _RUST_INSTALLED


async def test_rolling_back_means_pinning_the_go_build_re_released_above_the_rust_version(hass, fake_transport, monkeypatch):
    """The mechanism that works without touching the installer: pin the Go re-release (0.9.0 > every Rust 0.7.x). The
    installer sees installed < pin, clears the sticky history, installs the Go requirement and invalidates caches."""
    wheel = _Installed(monkeypatch, _RUST_INSTALLED, install_to=_GO_ROLLBACK)
    requirement = _pin_to(monkeypatch, _GO_ROLLBACK)
    entry = _observer_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is True
    assert WHEEL_INSTALLS == [("portico_transport", [requirement])]
    assert "transport-v0.9.0/" in requirement                            # the Go tag family, the rollback version
    assert wheel.events == ["clear_history", "pip", "invalidate"]
    assert wheel.version == _GO_ROLLBACK and entry.state is ConfigEntryState.LOADED


async def test_after_a_rollback_a_later_rust_fix_still_installs(hass, fake_transport, monkeypatch):
    """Rolling back does not dead-end the line: Go 0.9.0 is installed, the pin moves to Rust 0.10.0 (the line resumes above
    the rollback; a Rust 0.7.x would be below 0.9.0 and declined), it installs."""
    wheel = _Installed(monkeypatch, _GO_ROLLBACK, install_to="0.10.0")
    requirement = _pin_to(monkeypatch, "0.10.0", tag_family="transport-rs-v")
    entry = _observer_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is True
    assert WHEEL_INSTALLS == [("portico_transport", [requirement])] and wheel.version == "0.10.0"


def test_the_pin_url_is_chosen_per_architecture_and_x86_64_is_the_fallback(monkeypatch):
    """The pin names one wheel per architecture (the Rust lineage ships both). A machine with no entry gets the x86_64 wheel,
    exactly what every host got before the map existed; the install rule itself is not touched."""
    monkeypatch.setattr(wheel_module, "WHEEL_PIN_URLS", {"x86_64": "http://h/x86.whl", "aarch64": "http://h/arm.whl"})
    assert wheel_module.pin_url_for("x86_64") == "http://h/x86.whl"
    assert wheel_module.pin_url_for("aarch64") == "http://h/arm.whl"
    assert wheel_module.pin_url_for("armv7l") == "http://h/x86.whl"
    monkeypatch.setattr("platform.machine", lambda: "aarch64")
    assert wheel_module.pin_url_for() == "http://h/arm.whl"


def test_the_live_pin_is_the_x86_64_url_under_its_old_name():
    assert wheel_module.WHEEL_PIN_URL == wheel_module.WHEEL_PIN_URLS["x86_64"]
    assert wheel_module.WHEEL_REQUIREMENT == f"portico_transport @ {wheel_module.pin_url_for()}"


def test_the_wheel_base_url_swaps_the_web_root_and_keeps_the_path_and_hash():
    """DEV-836: the default is the public release; an install can fetch the same files from another web root."""
    default = wheel_module.pin_url_for("x86_64")
    assert default.startswith("https://github.com/gsasha/portico-ha/releases/download/transport-rs-v")
    other = wheel_module.pin_url_for("x86_64", base="https://git.example.com/o/portico-ha/")
    assert other == "https://git.example.com/o/portico-ha" + default[len(wheel_module.WHEEL_DEFAULT_BASE):]
    assert other.split("#")[1] == default.split("#")[1]                              # the pinned hash still applies
    assert wheel_module.pin_url_for("x86_64", base="ftp://x/y") == default and wheel_module.pin_url_for(base="") == default
    assert [wheel_module.valid_wheel_base(v) for v in ("http://h:3000/o/r", "https://h", "h/o", "http://h/o r", "http://h/o#x", None)] == [True, True, False, False, False, False]


async def test_the_installer_uses_the_configured_web_root(hass, fake_transport, monkeypatch):
    from custom_components.portico.door import wheel as w
    from custom_components.portico.push import const as push_const
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.portico.provider import DOMAIN
    assert w.configured_requirement(hass) == w.WHEEL_REQUIREMENT                       # no settings entry: the default
    entry = MockConfigEntry(domain=DOMAIN, data={push_const.ENTRY_TYPE: push_const.ENTRY_TYPE_PUSH},
                            options={w.CONF_WHEEL_BASE_URL: "http://192.168.2.5:3000/o/portico-ha"})
    entry.add_to_hass(hass)
    got = w.configured_requirement(hass)
    assert got.startswith("portico_transport @ http://192.168.2.5:3000/o/portico-ha/releases/download/") and "#sha256=" in got
    hass.config_entries.async_update_entry(entry, options={w.CONF_WHEEL_BASE_URL: "not a url"})
    assert w.configured_requirement(hass) == w.WHEEL_REQUIREMENT                       # a bad value falls back to the default
