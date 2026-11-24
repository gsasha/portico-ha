"""`portico/camera/webrtc_offer`: the panel's live window asks FRIGATE's own go2rtc for the WebRTC answer.

Why. For a plain (generic) camera entity Home Assistant's built-in go2rtc must spawn ffmpeg on `rtsp://<frigate>:8554/<name>`
and probe it before it can answer, so `camera/webrtc/offer` takes 2 to 3 s. Frigate already restreams that very stream through
its own go2rtc, which answers in about 10 ms (what the Frigate integration's own camera entity does: `POST
{frigate_url}/api/go2rtc/webrtc?src=<camera>` with `{"type": "offer", "sdp": ...}`, answer `{"type": "answer", "sdp": ...}`).

The command (administrators only, panel only): `{entity_id, sdp}` -> `{answer: sdp}`.

  * Frigate camera name. A camera of the Frigate integration gives its `camera_name` attribute. Any other camera gives its
    stream source: `rtsp://<host>:8554/<name>` (an `ffmpeg:` prefix is ignored) whose host is the Frigate config entry's host
    (the same text, or an address that host resolves to) is the Frigate stream `<name>`. Anything else is the error
    `not_frigate_stream`, and the panel falls back to Home Assistant's own `camera/webrtc/offer`.
  * The request goes to the Frigate config entry's `url` (5 s) with the headers the Frigate integration's own client would
    send (`get_auth_headers()`): this module never reads, stores or logs a credential. Only the camera name and the
    outcome are ever logged, never a URL, an address or an SDP.
  * Non-trickle: Frigate's answer already carries its candidates, so there is no candidate relay for this path.
  * The LAN candidate. Frigate's go2rtc runs in a container and advertises only what it learnt from STUN: the WAN address
    (`<wan>:8555`), which no client on the LAN can reach (no port forward, no hairpin), so ICE never connected and the live window
    fell back to a still picture (measured 2026-11-11: pair state `checking` for 30 s, 0 bytes; the same answer plus a
    `10.6.0.6:8555` host candidate connected in 2 s). The answer therefore gets a UDP and a TCP host candidate for the Frigate
    entry's own host on port 8555 (go2rtc's WebRTC port), unless it already names that address. A client that cannot reach it
    simply tries the others.
  * Frigate's :5000 is unauthenticated where it is exposed; the entry's url decides what is used, nothing is assumed.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import socket
from typing import Any
from urllib.parse import quote, urlsplit

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from . import auth
from .live_rtsp import camera_name_of, valid_stream_name

_LOGGER = logging.getLogger(__name__)

FRIGATE_DOMAIN = "frigate"
RTSP_PORT = 8554               # Frigate's go2rtc restream port
WEBRTC_PORT = 8555             # Frigate's go2rtc WebRTC port (udp and tcp)
FRIGATE_TIMEOUT = 5.0          # the whole POST
RESOLVE_TIMEOUT = 2.0
MAX_SDP = 32_000               # an offer or an answer is a few KB
MAX_ANSWER_BYTES = 64_000
_CAMERA_ID = re.compile(r"camera\.[a-z0-9_]+")


class WebRTCRefused(Exception):
    """The command's error: `.code` is what the panel sees, `.text` the human sentence."""

    def __init__(self, code: str, text: str) -> None:
        super().__init__(code)
        self.code = code
        self.text = text


def _entry_base(entry) -> str:
    return str(entry.data.get("url") or "").rstrip("/")


def _entry_host(entry) -> str | None:
    try:
        return urlsplit(_entry_base(entry)).hostname
    except ValueError:
        return None


async def _host_matches(host: str, entry_host: str) -> bool:
    """True when the stream's `host` is the Frigate entry's host: the same text, or an address the entry's host resolves to."""
    if host.lower() == entry_host.lower():
        return True
    try:
        loop = asyncio.get_running_loop()
        async with asyncio.timeout(RESOLVE_TIMEOUT):
            infos = await loop.getaddrinfo(entry_host, None, type=socket.SOCK_STREAM)
    except (OSError, TimeoutError, asyncio.TimeoutError):
        return False
    return any(info[4][0] == host for info in infos)


def stream_name_of(address: Any) -> tuple[str, str] | None:
    """(host, camera name) of `rtsp://host:8554/<name>` (an `ffmpeg:` prefix ignored), else None. A login, query or
    fragment, another port or a nested path is not a Frigate restream address."""
    if not isinstance(address, str) or not address or len(address) > 512:
        return None
    if address.startswith("ffmpeg:"):
        address = address[len("ffmpeg:"):]
    try:
        parts = urlsplit(address)
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if parts.scheme != "rtsp" or not host or parts.username is not None or parts.password is not None:
        return None
    if parts.query or parts.fragment or port != RTSP_PORT:
        return None
    name = parts.path[1:] if parts.path.startswith("/") else ""
    if not valid_stream_name(name):
        return None
    return host, name


def _frigate_entry_of(hass: HomeAssistant, entity_id: str):
    reg = er.async_get(hass).async_get(entity_id)
    entry_id = getattr(reg, "config_entry_id", None)
    entry = hass.config_entries.async_get_entry(entry_id) if entry_id else None
    return entry if entry is not None and entry.domain == FRIGATE_DOMAIN else None


async def resolve_frigate_stream(hass: HomeAssistant, entity_id: str):
    """(Frigate config entry, go2rtc stream name) for a camera entity, or raise WebRTCRefused('not_frigate_stream' | ...)."""
    state = hass.states.get(entity_id)
    if state is None:
        raise WebRTCRefused("not_found", "That camera is not in Home Assistant")
    entry = _frigate_entry_of(hass, entity_id)
    if entry is not None:
        name = camera_name_of(state.attributes)
        if name is not None and _entry_base(entry):
            return entry, name
    entries = [e for e in hass.config_entries.async_entries(FRIGATE_DOMAIN) if _entry_base(e)]
    if not entries:
        raise WebRTCRefused("not_frigate_stream", "No Frigate integration is set up")
    from homeassistant.components import camera as camera_component

    try:
        address = await camera_component.async_get_stream_source(hass, entity_id)
    except Exception as err:   # noqa: BLE001 - an entity with no stream is "not a Frigate stream"
        _LOGGER.debug("portico camera/webrtc_offer: the stream source of %s failed: %s", entity_id, type(err).__name__)
        address = None
    parsed = stream_name_of(address)
    if parsed is None:
        raise WebRTCRefused("not_frigate_stream", "That camera's stream is not a Frigate restream")
    host, name = parsed
    for candidate in entries:
        entry_host = _entry_host(candidate)
        if entry_host and await _host_matches(host, entry_host):
            return candidate, name
    raise WebRTCRefused("not_frigate_stream", "That camera's stream is not on the Frigate host")


async def _entry_ipv4s(entry) -> list[str]:
    """The IPv4 addresses of the Frigate entry's host (the host itself when it is one), never loopback; [] when unknown."""
    host = _entry_host(entry)
    if not host:
        return []
    try:
        ipaddress.IPv4Address(host)
        found = [host]
    except ValueError:
        try:
            loop = asyncio.get_running_loop()
            async with asyncio.timeout(RESOLVE_TIMEOUT):
                infos = await loop.getaddrinfo(host, None, family=socket.AF_INET, type=socket.SOCK_STREAM)
        except Exception:   # noqa: BLE001 - a best-effort extra: whatever goes wrong, the answer goes out as Frigate gave it
            return []
        found = [info[4][0] for info in infos]
    out: list[str] = []
    for ip in found:
        try:
            addr = ipaddress.IPv4Address(ip)
        except ValueError:
            continue
        if not (addr.is_loopback or addr.is_unspecified or addr.is_multicast) and ip not in out:
            out.append(ip)
    return out[:2]


def add_lan_candidates(sdp: str, addresses: list[str]) -> str:
    """`sdp` with a UDP and a TCP host candidate (port 8555) per address, unless it already names that address and port.
    An SDP with no media section is returned unchanged."""
    lines = sdp.replace("\r\n", "\n").rstrip("\n").split("\n")
    if not any(line.startswith("m=") for line in lines):
        return sdp
    extra: list[str] = []
    for n, ip in enumerate(addresses):
        if any(line.startswith("a=candidate:") and f" {ip} {WEBRTC_PORT} " in line for line in lines):
            continue
        extra.append(f"a=candidate:{9000 + 2 * n} 1 udp 2130706431 {ip} {WEBRTC_PORT} typ host")
        extra.append(f"a=candidate:{9001 + 2 * n} 1 tcp 1671430143 {ip} {WEBRTC_PORT} typ host tcptype passive")
    if not extra:
        return sdp
    return "\r\n".join(lines + extra) + "\r\n"


async def _auth_headers(hass: HomeAssistant, entry) -> dict[str, str]:
    """What the Frigate integration's own client sends (a bearer token or a proxy header), else nothing. Never logged."""
    try:
        client = hass.data.get(FRIGATE_DOMAIN, {}).get(entry.entry_id, {}).get("client")
        if client is not None:
            headers = await client.get_auth_headers()
            return {str(k): str(v) for k, v in headers.items()}
    except Exception as err:   # noqa: BLE001
        _LOGGER.debug("portico camera/webrtc_offer: no Frigate auth headers (%s)", type(err).__name__)
    return {}


async def frigate_answer(hass: HomeAssistant, entity_id: str, sdp: str) -> str:
    """The SDP answer of Frigate's go2rtc to `sdp` for the camera's stream. Raises WebRTCRefused."""
    entry, name = await resolve_frigate_stream(hass, entity_id)
    url = f"{_entry_base(entry)}/api/go2rtc/webrtc?src={quote(name, safe='')}"
    headers = await _auth_headers(hass, entry)
    session = async_get_clientsession(hass, verify_ssl=bool(entry.data.get("validate_ssl", True)))
    try:
        async with asyncio.timeout(FRIGATE_TIMEOUT):
            async with session.post(url, json={"type": "offer", "sdp": sdp}, headers=headers) as response:
                if response.status != 200:
                    raise WebRTCRefused("frigate_failed", f"Frigate answered {response.status}")
                body = await response.content.read(MAX_ANSWER_BYTES + 1)
    except WebRTCRefused:
        raise
    except (OSError, TimeoutError, asyncio.TimeoutError) as err:
        _LOGGER.debug("portico camera/webrtc_offer: Frigate unreachable for %s (%s)", name, type(err).__name__)
        raise WebRTCRefused("frigate_failed", "Frigate did not answer in time") from err
    except Exception as err:   # noqa: BLE001 - aiohttp errors: never the exception's text (it carries the URL)
        _LOGGER.debug("portico camera/webrtc_offer: Frigate request failed for %s (%s)", name, type(err).__name__)
        raise WebRTCRefused("frigate_failed", "Frigate could not be reached") from err
    if len(body) > MAX_ANSWER_BYTES:
        raise WebRTCRefused("frigate_failed", "Frigate's answer was too large")
    try:
        answer = json.loads(body)["sdp"]
    except (ValueError, KeyError, TypeError) as err:
        raise WebRTCRefused("frigate_failed", "Frigate's answer was not understood") from err
    if not isinstance(answer, str) or not answer.startswith("v=0"):
        raise WebRTCRefused("frigate_failed", "Frigate's answer was not an SDP")
    return add_lan_candidates(answer, await _entry_ipv4s(entry))


@websocket_api.websocket_command({
    vol.Required("type"): "portico/camera/webrtc_offer",
    vol.Required("entity_id"): vol.All(str, vol.Length(min=1, max=255)),
    vol.Required("sdp"): vol.All(str, vol.Length(min=1, max=MAX_SDP)),
})
@websocket_api.async_response
async def handle_camera_webrtc_offer(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """`{entity_id, sdp}` -> `{answer}` from Frigate's go2rtc; the error `not_frigate_stream` means "use HA's own offer"."""
    if not auth.require(connection, msg, "portico/camera/webrtc_offer"):
        return
    if not _CAMERA_ID.fullmatch(msg["entity_id"]):
        connection.send_error(msg["id"], "invalid_entity", "only camera entities can be asked about")
        return
    try:
        answer = await frigate_answer(hass, msg["entity_id"], msg["sdp"])
    except WebRTCRefused as err:
        connection.send_error(msg["id"], err.code, err.text)
        return
    except Exception as err:   # noqa: BLE001
        _LOGGER.warning("Portico: camera/webrtc_offer failed (%s)", type(err).__name__)
        connection.send_error(msg["id"], "unknown", "The live offer failed")
        return
    connection.send_result(msg["id"], {"answer": answer})


@callback
def async_setup_frigate_webrtc(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, handle_camera_webrtc_offer)
