"""Driver metadata registry.

Design doc §4.2. DriverMeta carries config-flow concerns ONLY — no
runtime protocol logic, and no copy of Capabilities either. Go is the
single source of truth for Capabilities (returned by transport.py's
`configure` call, §9.4); Python only carries what the config flow and
the dashboard need before a device is even configured.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any


@dataclass
class DriverMeta:
    key: str                 # "dahua_sip", "unifi_protect", "gate_sip_server"
    config_schema: Any       # a voluptuous schema for the config-flow form (§4.7)
    # Optional config-flow-time check + normalisation of the submitted form,
    # for checks a serialisable voluptuous schema cannot express (the
    # frontend has to render config_schema, so it cannot hold a custom
    # validator function). Returns the normalised data, or raises
    # vol.Invalid whose `path` names the offending field and whose message
    # is the error key shown under it (strings.json config.error.*).
    # Pure: never touches the network.
    normalize: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    # False for a driver with no video of its own (gate_sip_server):
    # the config flow's existing-camera step (§9.2) then requires
    # an existing HA camera entity instead of offering to create one.
    has_own_video: bool = True
    # True for a driver that must never be offered by the "Add a door" picker (gate_sip_server, DEV-518 W2): it has
    # its own explicit menu item, so it cannot be picked by accident among the ordinary doors.
    hidden: bool = False
    # A Python-only driver (ha_event): no Go driver behind it. Its Capabilities are stated here, not returned by
    # the transport's `configure`, and its door never attaches to the transport hub. None for every Go-backed driver.
    local_capabilities: dict[str, Any] | None = None
    # Optional: the capabilities for one entry's data, when they depend on an option (ha_event: `talkback`). Wins over
    # `local_capabilities` when set.
    local_capabilities_for: Callable[[dict[str, Any]], dict[str, Any]] | None = None


DRIVERS: dict[str, DriverMeta] = {}

# Driver keys with no Go driver (see DriverMeta.local_capabilities). A constant, not a lookup in DRIVERS, because
# DRIVERS fills only once load_drivers() has run.
LOCAL_DRIVERS = frozenset({"ha_event"})


def is_local_driver(key: Any) -> bool:
    return key in LOCAL_DRIVERS


def device_id_for(data: Mapping[str, Any], entry_id: str) -> str:
    """The one device id a config entry is known by, on both sides of IPC.

    An explicit `device_id` in the entry data wins (gate_sip_server stores
    its own device id there); otherwise the driver's own
    host field (dahua_sip `host`, unifi_protect `nvr_host`), else the entry
    id. __init__.py sends this value as `device_id` in `configure`, so the Go
    side registers the device under exactly the id Python looks it up by
    (signaling.py) and the id it stamps on `ringing` notifications.
    """
    return data.get("device_id") or data.get("host") or data.get("nvr_host") or entry_id


def load_drivers() -> None:
    """Import every driver module so it registers itself into DRIVERS.

    Deferred to call time (config-flow's driver-selection step, §4.7)
    rather than done at package-import time, since each driver module
    itself needs to import DriverMeta/DRIVERS from this package first —
    importing them here unconditionally would be circular.
    """
    from . import (  # noqa: F401  (import for registration side effect)
        dahua_sip,
        gate_sip_server,
        ha_event,
        unifi_protect,
    )
