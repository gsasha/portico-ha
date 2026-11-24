"""unifi_protect driver metadata (config-flow schema only, §4.2).

Generalizes the estate's own UniFi Protect doorbell talkback path — not
SIP or ONVIF at all, but Protect's own RTP talkback-session endpoint.
InboundAudio: InVideoStream (rides the RTSP feed, no relay needed).
OutboundAudio: TalkbackPush, always relay-mediated via
internal/call/relay.go, for every client including the native app
(DirectCapable: false — no SIP dialog with the device to join). RTSP is
served by the Protect console/NVR, not the camera itself (§4.4).

All runtime protocol logic lives in the Rust transport binary
(transport-rs/src/driver/unifiprotect/, §9.3) — this file is schema only.
"""
from __future__ import annotations

import voluptuous as vol

from . import DriverMeta, DRIVERS

CONFIG_SCHEMA = vol.Schema(
    {
        vol.Required("nvr_host"): str,          # the Protect console/NVR, not the camera (§4.4)
        vol.Required("nvr_api_key"): str,
        vol.Required("camera_id"): str,
        # Optional with no default: left out, the transport verifies the
        # console's certificate. A console with its default self-signed one
        # needs it set false.
        vol.Optional("verify_ssl"): bool,
        # Off unless set. On, the transport opens the console's public events
        # websocket on start (real traffic to the console) to hear the doorbell
        # ring (design doc §9.3). Left out, the driver makes no connection.
        vol.Optional("ring_events"): bool,
    }
)

META = DriverMeta(key="unifi_protect", config_schema=CONFIG_SCHEMA)
DRIVERS[META.key] = META
