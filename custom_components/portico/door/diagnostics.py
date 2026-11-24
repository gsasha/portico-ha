"""A door entry's diagnostics sections: the runtime and the hub (the entry itself is added by the integration's
`diagnostics.py`, which redacts the whole download with ONE merged `TO_REDACT` set: every credential a door entry can
hold (the device's SIP/RTSP/NVR credentials) and anything named like a key, token or registration).

The gate-sip peer token lives in a private Store and is never in an entry to begin with; it is in the set in case one
ever is. The merged set is a superset of the former door-only set: a diagnostics file must never leak what the old
one hid.
"""
from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry


def door_diagnostics_sections(entry: ConfigEntry) -> dict[str, Any]:
    """`runtime` and `hub` for a loaded door; {} for one that is not (no runtime_data yet)."""
    out: dict[str, Any] = {}
    runtime = getattr(entry, "runtime_data", None)
    if runtime is not None:
        out["runtime"] = {
            "transport_available": runtime.transport_available,
            "driver_available": runtime.driver_available,
            "capabilities": runtime.capabilities,
            "call_state": {"state": runtime.call_state.state, "call_id": runtime.call_state.call_id},
        }
        if getattr(runtime, "talk_wanted", False):
            out["runtime"]["talk"] = {"attached": runtime.talk_attached, "last_call_reason": runtime.last_call_reason}
        hub = getattr(runtime, "_hub", None)
        if hub is not None:
            out["hub"] = hub.diagnostics()
            link = hub.remote_link(runtime.device_id) if getattr(runtime, "remote", False) else None
            if link is not None:
                out["remote"] = link.diagnostics()
    return out
