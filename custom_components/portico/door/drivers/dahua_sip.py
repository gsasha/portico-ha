"""dahua_sip driver metadata (config-flow schema only, §4.2).

Generalizes the estate's own hard-won Dahua VTO lessons into config
fields: SIP `user#index` registration form, 16 kHz little-endian L16 PCM,
40 ms ptime, session-quota "486 Busy Here" handling, `Local SIP Server`
must-never-be-on. All runtime protocol logic lives in the Rust transport
binary (transport-rs/src/driver/dahua/, §9.3) — this file is schema only.

DirectCapable: true — the native app can dial this driver's devices
directly, reusing the same pkg/driver/dahua dial logic (§9.6, §9.8), as
long as the installer has a tailnet subnet route to the station (§4.8,
§9.9, hard prerequisite for the app's direct path on this driver, no
relay fallback in v1).
"""
from __future__ import annotations

import voluptuous as vol

from . import DriverMeta, DRIVERS

CONFIG_SCHEMA = vol.Schema(
    {
        vol.Required("host"): str,
        vol.Required("sip_user"): str,   # e.g. "9901#0" — the user#index form
        vol.Required("sip_password"): str,
        vol.Required("rtsp_username"): str,
        vol.Required("rtsp_password"): str,
    }
)

META = DriverMeta(key="dahua_sip", config_schema=CONFIG_SCHEMA)
DRIVERS[META.key] = META
