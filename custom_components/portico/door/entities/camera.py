"""One camera entity per device — backs the BROWSER video path only (§4.4).

Wraps HA's own bundled go2rtc stream via the standard camera platform; the
native app never uses this (it pulls RTSP directly from whichever host
actually serves it, bypassing go2rtc's loopback-bound instance, §4.4).

A config-flow option (config_flow.py's camera_config step) can point this
device's camera at an already-configured camera entity instead, to avoid
duplicating an existing UniFi Protect or Frigate camera for the same
physical device (§9.2) — when that's set, async_setup_entry below creates
nothing at all for this device.

Verified against the real installed homeassistant package (not assumed
from the API surface): implementing `stream_source()` + advertising
`CameraEntityFeature.STREAM` is the *entire* integration-side contract —
`homeassistant.components.go2rtc` registers itself as a WebRTC provider
globally for any STREAM-capable camera entity (it's imported by
`camera/__init__.py` as `async_register_webrtc_provider`); there is no
go2rtc-specific call this entity needs to make itself. `Camera.stream()`
(the base class's own `async_create_stream`) calls `await
self.stream_source()` and hands the result straight to
`homeassistant.components.stream.create_stream` — confirming a plain RTSP
URL string is exactly what's expected here, the same shape HA's own
`generic` camera integration returns.
"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from ..transport import TransportError

_LOGGER = logging.getLogger(__name__)

# Local, config-flow-independent copy of the stream URL template — the
# same shape as config_flow.py's own _UNIQUE_ID_FIELD mapping. The real
# Capabilities.RTSPTemplate (§4.2) is Go's to own, returned by
# transport.py's `configure` round-trip — not wired up yet (same TODO
# lock.py's HasRelay gate calls out), but this entity needs a
# stream_source *now*, so this mirrors
# the dahua driver's literal template string (transport-rs/src/driver/dahua/) by hand
# until that round-trip exists. It diverges from Go's own template in one
# place: it embeds rtsp_username/rtsp_password, since HA's stream/go2rtc
# source needs credentials in the URL itself to authenticate, whereas
# Go's template has no auth placeholder — the native app mints and stores
# its own distinct per-phone RTSP credential separately (§4.4) rather than
# reusing this one.
#
# unifi_protect has NO entry here: its RTSPS share needs a per-camera stream
# token minted through the Protect console's own API, which only the Go
# transport can do (driver.StreamSourcer, the IPC `stream_url` request). It is
# asked for on every stream_source() call, never cached or logged: the URL
# carries the token.
_RTSP_TEMPLATE = {
    "dahua_sip": "rtsp://{rtsp_username}:{rtsp_password}@{host}/cam/realmonitor?channel=1&subtype=1",
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Add this device's camera entity — unless the installer pointed it at
    an already-configured camera entity instead (§9.2, config_flow.py's
    camera_config step), in which case Portico creates nothing here and
    that existing entity is left alone."""
    if entry.data.get("existing_camera_entity"):
        _LOGGER.debug(
            "Portico device %s: reusing existing camera entity %s, not "
            "creating a Portico-owned one",
            entry.entry_id,
            entry.data["existing_camera_entity"],
        )
        return
    async_add_entities([PorticoCamera(hass, entry)])


class PorticoCamera(Camera):
    """A door station's video feed, browser-client path only (§4.4)."""

    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__()
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_camera"
        self._attr_name = "Camera"
        self._attr_has_entity_name = True

    async def stream_source(self) -> str | None:
        """Return the go2rtc/HA-stream-consumable RTSP source URL.

        unifi_protect asks the transport to mint its RTSPS URL (see
        _minted_stream_source). None (no stream) if a driver has no
        known-working template, or if the
        config entry is missing a field the template needs — logged, not
        raised, matching stream_source()'s own contract (HA treats a None
        return as "no stream available" rather than an error, per
        Camera.async_create_stream).
        """
        driver: str = self._entry.data["driver"]
        if driver == "unifi_protect":
            return await self._minted_stream_source()
        template = _RTSP_TEMPLATE.get(driver)
        if template is None:
            _LOGGER.warning(
                "Portico device %s (%s): no browser stream source available "
                "yet for this driver — point this device's camera at an "
                "existing camera entity instead (Settings > Devices > this "
                "device > Reconfigure)",
                self._entry.entry_id,
                driver,
            )
            return None
        try:
            return template.format(**self._entry.data)
        except KeyError as err:
            _LOGGER.warning(
                "Portico device %s (%s): can't build a stream URL, config "
                "is missing field %s",
                self._entry.entry_id,
                driver,
                err,
            )
            return None

    async def _minted_stream_source(self) -> str | None:
        """Ask the transport to mint this device's RTSPS URL (token included).

        None, logged without the URL or any secret, when the transport is not
        up or the console refuses: HA treats None as "no stream available".
        """
        runtime = getattr(self._entry, "runtime_data", None)
        client = getattr(runtime, "client", None)
        if client is None or not getattr(runtime, "transport_available", True):
            _LOGGER.warning(
                "Portico device %s: the transport is not running, so no stream "
                "URL can be minted yet",
                self._entry.entry_id,
            )
            return None
        try:
            result = await client.stream_url(runtime.device_id)
        except (TransportError, ConnectionError, OSError) as err:
            _LOGGER.warning(
                "Portico device %s: could not mint a stream URL (%s)",
                self._entry.entry_id,
                err,
            )
            return None
        url = result.get("url")
        return url if isinstance(url, str) and url.startswith("rtsps://") else None
