"""The transport wheel: the pin, and the lazy install of it.

The pre-fold driver integration pinned the wheel as a URL requirement in its manifest. Home Assistant hands a URL
requirement to pip on EVERY load (its `is_installed` is False for one), so the pin re-installed from the Gitea host on
every boot, and a manifest pin in the unified integration would make the WHOLE integration (phones, push, the panel)
fail to load whenever that host is unreachable. The pin therefore lives here, in code, and the wheel is installed only
when the installed one is older than the pin (`async_ensure_wheel`, called by the hub before its own version check).
A normal boot with the wheel already installed makes no network call.

Bumping the wheel is a change to the two constants below (plus `hub.MIN_WHEEL_VERSION` only when this Python starts to
rely on something the newer wheel adds), not a manifest edit.

The install is `homeassistant.requirements.async_process_requirements`, the call HA itself makes for a manifest
requirement: it needs no manifest (`name` is only a label in its log lines), runs `uv pip install` in the executor under
HA's global pip lock, and caches a FAILURE in `install_failure_history` for the rest of the run. So a retry is fake
unless `async_clear_install_history` is called first, and it is, before every attempt. After a successful install
`importlib.invalidate_caches()` is called (core never does), so the new wheel is importable without a restart.
"""
from __future__ import annotations

import importlib
import logging
import re

from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import issue_registry as ir
from homeassistant.requirements import async_clear_install_history, async_process_requirements

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# The pinned wheel (the transport-rs-v0.7.4 release: the Rust transport with the tablet call audio changes, portico-ha #372: 140 ms uplink jitter delay, safe lost-slot concealment, soft limiter, bounded uplink AGC; 0.7.3 added per-call audio counters, DEV-811 P3; 0.7.2 added the gate_sip_server StationRegistered and LastRegisterAt capabilities, DEV-518; 0.7.1 added diagnostic logging, 0.7.0 was the cut-over wheel; DEV-764). Each
# earlier release that moved the pin is in git history (0.5.0 per-door `deconfigure`, 0.5.1 claims and the dial
# capability, 0.6.0 the house-call bridge, 0.6.1 the phone-uplink jitter buffer,
# 0.6.2 PCMU-only answer, gate-microphone gain, sfu logging, 0.6.3 CanDial republish on a dial-switch flip, 0.6.4 the gate_sip_server driver, 0.6.5 the front-door talk driver unifi_protect_talk, 0.6.6 a repeated ring supersedes a still-ringing held call, 0.6.7 the front door can be dialled out: unifi_protect_talk CanDial, DialCall, ClaimedAudio, 0.6.8 hangup-reason diagnostics, 0.6.9 the adaptive jitter buffer / pacer for front-door talk audio).
WHEEL_PIN_VERSION = "0.7.4"
# One wheel URL per architecture (`platform.machine()` spelling), so an aarch64 host is not handed the x86_64 wheel once the
# pinned lineage ships both (the Rust wheels do: packaging/rust). The release is `transport-v<ver>` (Go) or
# `transport-rs-v<ver>` (Rust), see packaging/rust/release_scheme.py; this file does not care which. `WHEEL_PIN_URL` stays the
# x86_64 URL under its old name. A machine with no entry gets the x86_64 one, which is what every host got before the map.
WHEEL_PIN_URLS = {
    "x86_64": (
        "https://github.com/gsasha/portico-ha/releases/download/transport-rs-v0.7.4/"
        "portico_transport-0.7.4-py3-none-linux_x86_64.whl#sha256=a2a1022baad06ffe35c9c73f0c95b80219fcea526bc2d7017fdeee093faa5a20"
    ),
    "aarch64": (
        "https://github.com/gsasha/portico-ha/releases/download/transport-rs-v0.7.4/"
        "portico_transport-0.7.4-py3-none-linux_aarch64.whl#sha256=c8a237011b91ce8a3ec7bfc972b44d2fa8b943d0c734bc92ccb9b1d709da8009"
    ),
}
WHEEL_PIN_URL = WHEEL_PIN_URLS["x86_64"]

# Where the wheels are hosted (DEV-836). The URLs above name the public GitHub release; an install that fetches the same
# release files from another web root (a private mirror, a Gitea) sets the `wheel_base_url` option of the Portico settings
# entry to that repo web root, for example `https://git.example.com/owner/portico-ha`. The path after the base
# (`/releases/download/<tag>/<file>#sha256=...`) is kept, so the pinned hash still applies. Empty or unset = the default.
WHEEL_DEFAULT_BASE = "https://github.com/gsasha/portico-ha"
CONF_WHEEL_BASE_URL = "wheel_base_url"


def valid_wheel_base(value) -> bool:
    """An http(s) repo web root: no spaces or control characters, no query or fragment."""
    return (isinstance(value, str) and value.isascii() and value.isprintable() and " " not in value
            and re.fullmatch(r"https?://[^/?#\s]+(?:/[^?#\s]*)?", value) is not None)


def pin_url_for(machine: str | None = None, base: str | None = None) -> str:
    """The pinned wheel URL for this machine (`platform.machine()` by default); the x86_64 one when there is no entry.
    With a valid `base` the default web root is swapped for it (a URL that does not start with the default is left as is)."""
    import platform

    url = WHEEL_PIN_URLS.get(machine or platform.machine(), WHEEL_PIN_URLS["x86_64"])
    if base and valid_wheel_base(base) and url.startswith(WHEEL_DEFAULT_BASE + "/"):
        url = base.rstrip("/") + url[len(WHEEL_DEFAULT_BASE):]
    return url


def configured_requirement(hass) -> str:
    """`WHEEL_REQUIREMENT`, from the configured web root when the settings entry has a valid `wheel_base_url`."""
    from ..live_rtsp import push_entry_options

    base = push_entry_options(hass).get(CONF_WHEEL_BASE_URL)
    if not base:
        return WHEEL_REQUIREMENT
    if not valid_wheel_base(base):
        _LOGGER.warning("portico: the wheel_base_url option is not an http(s) address; using the default wheel location")
        return WHEEL_REQUIREMENT
    return f"portico_transport @ {pin_url_for(base=base)}"


WHEEL_REQUIREMENT = f"portico_transport @ {pin_url_for()}"

ISSUE_WHEEL_INSTALL_FAILED = "wheel_install_failed"
# The label HA prints in its install log lines and exceptions (there is no integration of this name).
_REQUIREMENTS_LABEL = "portico_transport"


def _version_tuple(text: str | None):
    from packaging.version import InvalidVersion, Version

    if not text:
        return None
    try:
        return Version(text)
    except InvalidVersion:
        return None


async def async_ensure_wheel(hass) -> None:
    """Install the pinned wheel when the installed one is older than the pin, or missing.

    No network on a boot that already has the pin or newer. Raises ConfigEntryNotReady (after raising the repair issue
    `wheel_install_failed`) when the install fails, so Home Assistant retries the door with its own backoff; a door
    that cannot start leaves phones, push and the panel running. An unreadable version means the wheel is not
    installed (the distribution metadata is the only way it is installed at all) and is installed.
    """
    # Read through hub's helper so one function owns "which wheel is installed" (and the tests patch one place).
    from .hub import read_installed_wheel_version

    installed = await hass.async_add_executor_job(read_installed_wheel_version)
    have = _version_tuple(installed)
    if have is not None and have >= _version_tuple(WHEEL_PIN_VERSION):
        ir.async_delete_issue(hass, DOMAIN, ISSUE_WHEEL_INSTALL_FAILED)
        return
    _LOGGER.info("portico: installing the transport wheel %s (installed: %s)", WHEEL_PIN_VERSION, installed or "none")
    async_clear_install_history(hass)
    try:
        await async_process_requirements(hass, _REQUIREMENTS_LABEL, [configured_requirement(hass)], False)
    except Exception as err:  # noqa: BLE001 - RequirementsNotFound, a timeout, anything pip's wrapper raises
        _LOGGER.error("portico: the transport wheel %s could not be installed: %s", WHEEL_PIN_VERSION, err)
        ir.async_create_issue(
            hass, DOMAIN, ISSUE_WHEEL_INSTALL_FAILED,
            is_fixable=False, is_persistent=False, severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_WHEEL_INSTALL_FAILED,
            translation_placeholders={"required": WHEEL_PIN_VERSION, "reason": type(err).__name__},
        )
        raise ConfigEntryNotReady(f"the transport wheel {WHEEL_PIN_VERSION} could not be installed") from err
    importlib.invalidate_caches()
    ir.async_delete_issue(hass, DOMAIN, ISSUE_WHEEL_INSTALL_FAILED)
