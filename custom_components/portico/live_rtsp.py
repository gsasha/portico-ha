"""The RTSP stream hint of a live camera tile, for the phone app's away-from-home route (DEV-713, S1b-1).

Away from home the app cannot use Home Assistant's WebRTC (`live.py`); it pulls the camera's go2rtc stream directly over RTSP
through its tailnet. The one thing it needs from the server is WHICH stream belongs to which tile. This module computes that,
per request, and the resolver carries it on a camera item as `live_rtsp`:

    "live_rtsp": {"main": "rtsp://10.6.0.6:8554/gate_intercom", "sub": "rtsp://10.6.0.6:8554/gate_intercom_sub"}

`sub` is present only when the camera is listed in `live_sub_streams`. The field is NEVER stored in the document (a document
that carries one has it dropped by the validator), is never an entity id, and exists only for a Frigate camera tile with
`live: true` (and, DEV-713 S2-1, for a Frigate camera of a caller's ring/in-call block, which has no `live` flag and needs none)
and only while the install-wide option `live_rtsp_base` is set. Two options of the settings (push) entry, edited by
`portico/ui_settings/set` (ui_settings.py):

  live_rtsp_base       the address of the camera server's RTSP port, `rtsp://host[:port]`, nothing else. EMPTY (the default) is
                       OFF: no `live_rtsp` anywhere. No credentials, path or query are accepted.
  live_sub_streams     the Frigate camera names that have a go2rtc stream called `<camera name>_sub`; empty (the default) means no
                       `sub` is given, so a Small tile uses the main stream until the list is filled.

DEV-715: a third option, `live_away_streams`, gives ONE camera entity its own away addresses whatever the brand or naming:
`{"camera.porch": {"main": "rtsp://10.6.0.9:554/ch1", "sub": "rtsp://10.6.0.9:554/ch2"}}` (`sub` optional). Each is a full
`rtsp://host[:port][/path]` with no credentials and no query; an explicit entry wins over the Frigate convention and needs no
`live_rtsp_base` (the others stay off while it is empty). It is as much outside Portico's per-camera grant as the base.

The stream name is the Frigate camera name, read from the camera entity's `camera_name` attribute (the Frigate integration sets
it, with `client_id`, on every camera entity it makes; go2rtc streams in Frigate are named after the camera). An entity without
both is not a Frigate camera and gets nothing. The last name seen is remembered, so a camera that blinks to `unavailable`
(its attributes gone) does not change the endpoint's `rev`.

NOTE: the RTSP port is not behind Portico's per-camera grant (design Q7): anyone on the tailnet allowed to reach that port can pull
any stream on it. The settings text says so in plain words.
"""
from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping
from typing import Any

CONF_LIVE_RTSP_BASE = "live_rtsp_base"
CONF_LIVE_SUB_STREAMS = "live_sub_streams"
CONF_LIVE_AWAY_STREAMS = "live_away_streams"

MAX_BASE_LENGTH = 128
MAX_SUB_STREAMS = 64
MAX_AWAY_STREAMS = 64
MAX_STREAM_URL_LENGTH = 256
SUB_SUFFIX = "_sub"

_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_BASE = re.compile(rf"rtsp://(?P<host>{_LABEL}(?:\.{_LABEL})*)(?::(?P<port>[0-9]{{1,5}}))?/?", re.IGNORECASE)
_ENTITY_ID = re.compile(r"camera\.[a-z0-9_]{1,100}")
_STREAM_URL = re.compile(rf"rtsp://{_LABEL}(?:\.{_LABEL})*(?::[0-9]{{1,5}})?(?:/[A-Za-z0-9._~%!$&'()*+,;=:@/-]*)?", re.IGNORECASE)
_DOTTED_DIGITS = re.compile(r"[0-9.]+")


def valid_stream_name(value: Any) -> bool:
    """A Frigate camera name safe to put in a URL path: letters, digits, `_` and `-`, 1 to 64 of them."""
    return isinstance(value, str) and _NAME.fullmatch(value) is not None


def normalize_base(value: Any) -> str | None:
    """`rtsp://host[:port]` in its one canonical form (scheme lower case, no trailing slash), "" for empty (the feature is off),
    or None when it is not acceptable: another scheme, user info (`user:pass@`), a path, query or fragment, an IPv6 or a
    malformed host, a port outside 1 to 65535, spaces or control characters, or longer than MAX_BASE_LENGTH."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return ""
    if len(text) > MAX_BASE_LENGTH or not text.isascii() or not text.isprintable() or " " in text:
        return None
    match = _BASE.fullmatch(text)
    if match is None:
        return None
    host = match.group("host")
    if _DOTTED_DIGITS.fullmatch(host):                  # an address made of digits and dots must be a real IPv4
        try:
            ipaddress.IPv4Address(host)
        except ValueError:
            return None
    elif len(host) > 253:
        return None
    port = match.group("port")
    if port is not None and not 1 <= int(port) <= 65535:
        return None
    return f"rtsp://{host.lower()}" + (f":{int(port)}" if port is not None else "")


def normalize_sub_streams(value: Any) -> list[str] | None:
    """The list of camera names, listed once each in the order given, or None when it is not a list of valid names (or longer
    than MAX_SUB_STREAMS)."""
    if not isinstance(value, list) or len(value) > MAX_SUB_STREAMS or not all(valid_stream_name(v) for v in value):
        return None
    return list(dict.fromkeys(value))


def normalize_stream_url(value: Any) -> str | None:
    """A full away address `rtsp://host[:port][/path]` (host as `normalize_base`; no user info, query or fragment), or None."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > MAX_STREAM_URL_LENGTH or not text.isascii() or not text.isprintable() or " " in text:
        return None
    if _STREAM_URL.fullmatch(text) is None:
        return None
    host_part, slash, path = text[len("rtsp://"):].partition("/")
    base = normalize_base("rtsp://" + host_part)
    if not base:
        return None
    return base + slash + path


def normalize_away_streams(value: Any) -> dict[str, dict[str, str]] | None:
    """`{camera entity id: {"main": url, "sub"?: url}}` cleaned, or None when anything in it is not acceptable."""
    if not isinstance(value, dict) or len(value) > MAX_AWAY_STREAMS:
        return None
    out: dict[str, dict[str, str]] = {}
    for entity_id, pair in value.items():
        if not isinstance(entity_id, str) or _ENTITY_ID.fullmatch(entity_id) is None or not isinstance(pair, dict):
            return None
        if set(pair) - {"main", "sub"} or "main" not in pair:
            return None
        clean = {}
        for kind in ("main", "sub"):
            if kind in pair:
                url = normalize_stream_url(pair[kind])
                if url is None:
                    return None
                clean[kind] = url
        out[entity_id] = clean
    return out


def read_away_streams(options: Mapping[str, Any] | None) -> dict[str, dict[str, str]]:
    return normalize_away_streams((options or {}).get(CONF_LIVE_AWAY_STREAMS, {})) or {}


def read_settings(options: Mapping[str, Any] | None) -> tuple[str, list[str]]:
    """(base, sub names) as stored. A value that does not validate (it can only have been written by hand) reads as off/empty."""
    options = options or {}
    base = normalize_base(options.get(CONF_LIVE_RTSP_BASE, ""))
    subs = normalize_sub_streams(options.get(CONF_LIVE_SUB_STREAMS, []))
    return base or "", subs or []


def camera_name_of(attributes: Mapping[str, Any]) -> str | None:
    """The Frigate camera name an entity's attributes carry (`camera_name` together with `client_id`), or None."""
    name = attributes.get("camera_name")
    if valid_stream_name(name) and isinstance(attributes.get("client_id"), str):
        return name
    return None


def hint(base: str, subs: list[str], camera_name: str | None, has_small: bool = False) -> dict[str, str] | None:
    """The `live_rtsp` object for a Frigate camera, or None (feature off, or no camera name). `has_small` (DEV-768): the
    element has a small stream, so `sub` is given by the Frigate convention without the name being listed in `live_sub_streams`."""
    if not base or not valid_stream_name(camera_name):
        return None
    out = {"main": f"{base}/{camera_name}"}
    if has_small or camera_name in subs:
        out["sub"] = f"{base}/{camera_name}{SUB_SUFFIX}"
    return out


def push_entry_options(hass: Any) -> Mapping[str, Any]:
    """The options of the install-wide settings (push) entry, or {} when there is none. Imported late: the push package is heavy
    and imports this one's neighbours."""
    from .push import const as push_const
    from .provider import DOMAIN

    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.data.get(push_const.ENTRY_TYPE) == push_const.ENTRY_TYPE_PUSH:
            return entry.options
    return {}
