"""What the panel's camera picker shows beside each camera: the picture size and, when HA knows it, the frame rate.

`portico/cameras/info` (editor_commands.py, administrators only) answers `{entity_id, width, height, fps, source}`
for the camera entities it is asked about:

  attributes   the entity's own state attributes carry numeric `width` and `height` (UniFi Protect cameras do);
               `fps` is the attribute `fps` when there is one, else null
  still        the entity gives no size, so the picture it returns right now is fetched and ONLY ITS HEADER is read
               (JPEG SOF, PNG IHDR, WebP VP8/VP8L/VP8X, pure Python, no imaging library): the size of the full-size
               still the phone receives from `content/snapshot`. The bytes are dropped at once, never stored, never
               logged; `fps` is null, because HA does not know it and a still cannot tell it
  unavailable  the camera is `unavailable`/`unknown`, or no longer in `hass.states`: nulls, nothing probed
  failed       the camera is up but gave no readable picture in time: nulls

  frigate      a camera of the Frigate integration (its config entry's domain is `frigate`) gets, on top of the row above,
               `fps` = Frigate's MEASURED rate of that camera (`/api/stats` cameras.<name>.camera_fps, rounded to the
               nearest 0.5), `fps_source: "frigate-stats"` and, when `/api/config` has it, `detect: {fps, width, height}`
               (the configured detect stream). Two read-only GETs per refresh for all cameras, 5 s each, kept 60 s
               (a failure too); a failure or an unknown camera leaves `fps` as it was (null) and never raises. The
               camera is mapped by its `camera_name` state attribute and the base URL is the Frigate entry's `url`.
               No credential is sent; the URL never appears in a reply or a log.

  stream       (DEV-766) a camera whose entity offers a plain `rtsp://` live stream (Frigate's restream, a Generic Camera
               on it) also gets `stream: {width, height}`, the size of that LIVE stream, read from the stream's own
               RTSP description (camera_stream_size.py: one DESCRIBE, no ffmpeg). For a Frigate camera the still is the
               640x480 detect frame while the stream is the main restream, so the picker shows `stream` when it is
               there. The row keeps `width`/`height` as the still (what `content/snapshot` returns); a stream that says
               nothing leaves the key out. Kept 10 minutes (a failure 1 minute); a camera that already reports a size
               in its attributes is not asked.

A frame rate is never guessed and stills are never timed. The probes cost the camera one snapshot each, so they are
bounded: a result is kept 10 minutes (a failure 1 minute), at most 3 probes run at once, one probe waits 5 s and the
whole call 15 s, and only the cameras asked for are touched. Nothing in a reply or a log is a URL, a token or an
`access_token` attribute.
"""
from __future__ import annotations

import asyncio
import json
import logging
import struct
import time
from dataclasses import dataclass
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .camera_stream_size import describe_size
from .provider import DOMAIN

_LOGGER = logging.getLogger(__name__)

DATA_CAMERA_INFO = "camera_info"
MAX_IDS = 20               # entity ids per call
CACHE_SECONDS = 600.0      # a probed size is kept this long (10 minutes)
FAILURE_SECONDS = 60.0     # a failed probe is kept this long, so a dead camera is not hammered
PROBE_TIMEOUT = 5.0        # one picture
CALL_BUDGET = 15.0         # one command
MAX_IN_FLIGHT = 3          # probes at once
STREAM_TIMEOUT = 4.0       # one RTSP description (camera_stream_size.py)
STREAM_BUDGET = 6.0        # all the descriptions of one command
FRIGATE_DOMAIN = "frigate"
FRIGATE_TTL = 60.0         # Frigate's stats and config are kept this long (success or failure)
FRIGATE_TIMEOUT = 5.0      # one GET
FRIGATE_MAX_BYTES = 4_000_000
SOURCE_FRIGATE_STATS = "frigate-stats"
MAX_DIMENSION = 100_000    # a header claiming more is garbage

SOURCE_ATTRIBUTES = "attributes"
SOURCE_STILL = "still"
SOURCE_UNAVAILABLE = "unavailable"
SOURCE_FAILED = "failed"

_DEAD_STATES = ("unavailable", "unknown")


# --- header parsers ------------------------------------------------------------------------------------------------

def _dims(width: int, height: int) -> tuple[int, int] | None:
    if 1 <= width <= MAX_DIMENSION and 1 <= height <= MAX_DIMENSION:
        return width, height
    return None


# JPEG start-of-frame markers: C0-CF except C4 (DHT), C8 (JPG) and CC (DAC)
_SOF = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 4 or data[0] != 0xFF or data[1] != 0xD8:
        return None
    pos = 2
    end = len(data)
    while pos < end:
        # a marker is FF, any number of FF fill bytes, then the code
        if data[pos] != 0xFF:
            return None
        while pos < end and data[pos] == 0xFF:
            pos += 1
        if pos >= end:
            return None
        code = data[pos]
        pos += 1
        if code == 0x00 or code == 0x01 or 0xD0 <= code <= 0xD8:      # stuffed byte, TEM, RSTn, SOI: no length
            continue
        if code == 0xD9 or code == 0xDA:       # EOI, or the scan: the frame header should have come before
            return None
        if pos + 2 > end:
            return None
        (length,) = struct.unpack_from(">H", data, pos)
        if length < 2:
            return None
        if code in _SOF:
            if pos + 7 > end:                  # length, precision, height, width
                return None
            height, width = struct.unpack_from(">HH", data, pos + 3)
            return _dims(width, height)        # height 0 (defined later by a DNL marker) is refused
        pos += length                          # APPn, DQT, DHT, COM...: skipped by length, never searched inside
    return None


def _png_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return None
    (length,) = struct.unpack_from(">I", data, 8)
    if length < 8:
        return None
    width, height = struct.unpack_from(">II", data, 16)
    return _dims(width, height)


def _webp_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 20 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    kind = data[12:16]
    body = 20                                  # the first chunk's data
    if kind == b"VP8 ":                        # lossy: 3-byte frame tag, start code 9D 01 2A, 14-bit sizes
        if len(data) < body + 10 or data[body + 3:body + 6] != b"\x9d\x01\x2a":
            return None
        width, height = struct.unpack_from("<HH", data, body + 6)
        return _dims(width & 0x3FFF, height & 0x3FFF)
    if kind == b"VP8L":                        # lossless: signature 2F, then 14 + 14 bits of size minus one
        if len(data) < body + 5 or data[body] != 0x2F:
            return None
        (bits,) = struct.unpack_from("<I", data, body + 1)
        return _dims((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
    if kind == b"VP8X":                        # extended: flags(1) reserved(3) canvas width-1 (24 bits) height-1 (24 bits)
        if len(data) < body + 10:
            return None
        width = int.from_bytes(data[body + 4:body + 7], "little") + 1
        height = int.from_bytes(data[body + 7:body + 10], "little") + 1
        return _dims(width, height)
    return None


def image_size(data: bytes | bytearray | memoryview | None) -> tuple[int, int] | None:
    """`(width, height)` read from the HEADER of a JPEG, PNG or WebP picture, or None when it is none of those, is
    truncated before the size, or states a size that cannot be real. Never decodes pixels; never raises."""
    if not data:
        return None
    try:
        data = bytes(data) if not isinstance(data, bytes) else data
        for parse in (_jpeg_size, _png_size, _webp_size):
            size = parse(data)
            if size:
                return size
    except (struct.error, IndexError, ValueError, TypeError):
        return None
    return None


# --- the answer ----------------------------------------------------------------------------------------------------

def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value <= 0 or value == float("inf"):
        return None
    return float(value)


def _fps(value: Any) -> int | float | None:
    number = _number(value)
    if number is None or number > 1000:
        return None
    return int(number) if number == int(number) else round(number, 1)


def _row(entity_id: str, width, height, fps, source: str) -> dict:
    return {"entity_id": entity_id, "width": width, "height": height, "fps": fps, "source": source}


def from_attributes(entity_id: str, state) -> dict | None:
    """The row when the state's own attributes carry a numeric size, else None."""
    attrs = getattr(state, "attributes", None) or {}
    width, height = _number(attrs.get("width")), _number(attrs.get("height"))
    if width is None or height is None:
        return None
    size = _dims(int(width), int(height))
    if size is None:
        return None
    return _row(entity_id, size[0], size[1], _fps(attrs.get("fps")), SOURCE_ATTRIBUTES)


def _half(value: Any) -> int | float | None:
    """A measured rate rounded to the nearest 0.5 (an int when whole); None for anything that is not a rate."""
    number = _number(value)
    if number is None or number > 1000:
        return None
    halves = round(number * 2) / 2
    if halves <= 0:
        return None
    return int(halves) if halves == int(halves) else halves


def _detect(value: Any) -> dict | None:
    if not isinstance(value, dict):
        return None
    out = {}
    for key in ("fps", "width", "height"):
        number = _number(value.get(key))
        if number is not None and number <= MAX_DIMENSION:
            out[key] = int(number) if number == int(number) else round(number, 1)
    return out or None


def frigate_cameras(stats: Any, config: Any) -> dict[str, dict]:
    """`{camera name: {"fps": measured | None, "detect": {...} | None}}` from Frigate's two documents (0.14/0.15
    shapes: `cameras.<name>.camera_fps` in stats, `cameras.<name>.detect.{fps,width,height}` in config). Either
    document may be None; anything of another shape is ignored."""
    out: dict[str, dict] = {}
    if isinstance(stats, dict) and isinstance(stats.get("cameras"), dict):
        for name, cam in stats["cameras"].items():
            if isinstance(cam, dict):
                out.setdefault(name, {"fps": None, "detect": None})["fps"] = _half(cam.get("camera_fps"))
    if isinstance(config, dict) and isinstance(config.get("cameras"), dict):
        for name, cam in config["cameras"].items():
            if isinstance(cam, dict):
                out.setdefault(name, {"fps": None, "detect": None})["detect"] = _detect(cam.get("detect"))
    return out


@dataclass
class _Cached:
    at: float
    row: dict


class CameraInfo:
    """The probe cache and the bounds, one per HA instance (`get_camera_info`)."""

    def __init__(self) -> None:
        self._cache: dict[str, _Cached] = {}
        self._gate: asyncio.Semaphore | None = None
        self._frigate: dict[str, tuple[float, dict[str, dict]]] = {}     # config entry id -> (at, cameras)
        self._streams: dict[str, tuple[float, tuple[int, int] | None]] = {}   # entity id -> (at, live size | None)
        self._known: dict[str, tuple[int, int]] = {}      # entity id -> last live size learned (content documents)
        self.frigate_fetches = 0              # GETs made to Frigate since start (tests read it)
        self.probes = 0                       # how many pictures were fetched since start (tests read it)
        self.stream_reads = 0                 # RTSP descriptions asked for since start (tests read it)

    def _semaphore(self) -> asyncio.Semaphore:
        if self._gate is None:
            self._gate = asyncio.Semaphore(MAX_IN_FLIGHT)
        return self._gate

    def _cached(self, entity_id: str, now: float) -> dict | None:
        hit = self._cache.get(entity_id)
        if hit is None:
            return None
        ttl = FAILURE_SECONDS if hit.row["source"] == SOURCE_FAILED else CACHE_SECONDS
        if now - hit.at >= ttl:
            del self._cache[entity_id]
            return None
        return dict(hit.row)

    def _prune(self, now: float) -> None:
        for entity_id in [e for e, c in self._cache.items() if now - c.at >= CACHE_SECONDS]:
            del self._cache[entity_id]
        for entity_id in [e for e, (at, _size) in self._streams.items() if now - at >= CACHE_SECONDS]:
            del self._streams[entity_id]

    async def _stream_size(self, hass: HomeAssistant, entity_id: str) -> tuple[int, int] | None:
        """The live size of one camera's stream, from its RTSP description (camera_stream_size.py); kept 10 minutes, a
        failure one minute. None when the entity has no plain rtsp:// address or the stream says nothing. Never raises."""
        from homeassistant.components import camera as camera_component

        now = time.monotonic()
        hit = self._streams.get(entity_id)
        if hit is not None and now - hit[0] < (CACHE_SECONDS if hit[1] else FAILURE_SECONDS):
            return hit[1]
        size = None
        async with self._semaphore():
            self.stream_reads += 1
            try:
                address = await camera_component.async_get_stream_source(hass, entity_id)
                size = await describe_size(address, STREAM_TIMEOUT)
            except Exception as err:   # noqa: BLE001 - an entity with no stream is "no live size", not a crash
                _LOGGER.debug("portico cameras/info: the stream of %s failed: %s", entity_id, type(err).__name__)
        self._streams[entity_id] = (time.monotonic(), size)
        return size

    async def _add_streams(self, hass: HomeAssistant, rows: dict[str, dict]) -> None:
        """Add `stream: {width, height}` to every row whose camera's live stream size could be read, in place. A row
        whose size already came from the stream's own attributes is left alone. Bounded by STREAM_BUDGET in all."""
        wanted = [e for e, r in rows.items() if r["source"] not in (SOURCE_UNAVAILABLE, SOURCE_ATTRIBUTES)]
        if not wanted:
            return
        tasks = {e: asyncio.ensure_future(self._stream_size(hass, e)) for e in wanted}
        await asyncio.wait(tasks.values(), timeout=STREAM_BUDGET)
        for entity_id, task in tasks.items():
            if not task.done():
                task.cancel()
                continue
            if task.cancelled() or task.exception() is not None:
                continue
            size = task.result()
            if size is not None:
                rows[entity_id]["stream"] = {"width": size[0], "height": size[1]}

    async def _frigate_get(self, hass: HomeAssistant, entry, path: str) -> Any:
        """One read-only GET of `path` on the Frigate entry's URL; raises on any failure (the caller degrades)."""
        base = str(entry.data.get("url") or "").rstrip("/")
        if not base:
            raise ValueError("no url")
        session = async_get_clientsession(hass, verify_ssl=bool(entry.data.get("validate_ssl", True)))
        self.frigate_fetches += 1
        async with asyncio.timeout(FRIGATE_TIMEOUT):
            async with session.get(base + path) as response:
                if response.status != 200:
                    raise ValueError(f"status {response.status}")
                body = await response.content.read(FRIGATE_MAX_BYTES + 1)
        if len(body) > FRIGATE_MAX_BYTES:
            raise ValueError("too large")
        return json.loads(body)

    async def _frigate_cameras(self, hass: HomeAssistant, entry, now: float) -> dict[str, dict]:
        """Frigate's cameras for this entry, from the 60 s cache or from one GET each of stats and config."""
        hit = self._frigate.get(entry.entry_id)
        if hit is not None and now - hit[0] < FRIGATE_TTL:
            return hit[1]
        docs = []
        for path in ("/api/stats", "/api/config"):
            try:
                docs.append(await self._frigate_get(hass, entry, path))
            except Exception as err:   # noqa: BLE001 - Frigate down, slow or refusing is "fps unknown", not a crash
                _LOGGER.debug("portico cameras/info: Frigate %s failed: %s", path, type(err).__name__)
                docs.append(None)
        cameras = frigate_cameras(*docs)
        self._frigate[entry.entry_id] = (time.monotonic(), cameras)
        return cameras

    @staticmethod
    def _frigate_entry(hass: HomeAssistant, entity_id: str):
        """The Frigate config entry this camera entity belongs to, else None (any other integration)."""
        reg_entry = er.async_get(hass).async_get(entity_id)
        entry_id = getattr(reg_entry, "config_entry_id", None)
        entry = hass.config_entries.async_get_entry(entry_id) if entry_id else None
        if entry is None or entry.domain != FRIGATE_DOMAIN:
            return None
        return entry

    def known_size(self, entity_id: str) -> tuple[int, int] | None:
        """The last live size learned for `entity_id` (by `async_refresh_sizes`), or None. Synchronous and free: the
        content document reads this on every build and never probes. A failed refresh keeps the old value."""
        return self._known.get(entity_id)

    async def async_refresh_sizes(self, hass: HomeAssistant, entity_ids: list[str]) -> bool:
        """Learn the live size of each camera entity (the state's own width/height attributes, else the RTSP description
        through the same 10-minute cache as the Pages editor) and remember it for `known_size`. True when any remembered
        size is new or changed. Never raises; a camera that is down or says nothing keeps its previous value."""
        changed = False
        for entity_id in dict.fromkeys(entity_ids):
            try:
                state = hass.states.get(entity_id)
                if state is None or state.state in _DEAD_STATES:
                    continue
                row = from_attributes(entity_id, state)
                size = (int(row["width"]), int(row["height"])) if row else await self._stream_size(hass, entity_id)
            except Exception:   # noqa: BLE001 - "no size" is the answer, never a crash
                continue
            if size is not None and self._known.get(entity_id) != size:
                self._known[entity_id] = size
                changed = True
        return changed

    async def _add_frigate(self, hass: HomeAssistant, rows: dict[str, dict]) -> None:
        """Enrich the rows of Frigate cameras in place. Never raises; leaves a row alone it cannot enrich."""
        by_entry: dict[str, tuple[Any, list[tuple[str, str]]]] = {}
        for entity_id, row in rows.items():
            if row["source"] == SOURCE_UNAVAILABLE:
                continue
            try:
                entry = self._frigate_entry(hass, entity_id)
                state = hass.states.get(entity_id)
                name = (getattr(state, "attributes", None) or {}).get("camera_name")
            except Exception:   # noqa: BLE001
                continue
            if entry is None or not isinstance(name, str) or not name:
                continue
            by_entry.setdefault(entry.entry_id, (entry, []))[1].append((entity_id, name))
        for entry, members in by_entry.values():
            try:
                cameras = await self._frigate_cameras(hass, entry, time.monotonic())
            except Exception:   # noqa: BLE001
                continue
            for entity_id, name in members:
                known = cameras.get(name)
                if not known:
                    continue
                if known["fps"] is not None:
                    rows[entity_id]["fps"] = known["fps"]
                    rows[entity_id]["fps_source"] = SOURCE_FRIGATE_STATS
                if known["detect"]:
                    rows[entity_id]["detect"] = dict(known["detect"])

    async def _probe(self, hass: HomeAssistant, entity_id: str) -> dict:
        """One picture, header only. The picture is a local that goes out of scope here: nothing keeps it."""
        from homeassistant.components import camera as camera_component

        async with self._semaphore():
            self.probes += 1
            try:
                async with asyncio.timeout(PROBE_TIMEOUT):
                    image = await camera_component.async_get_image(hass, entity_id, timeout=int(PROBE_TIMEOUT))
                size = image_size(getattr(image, "content", None))
                del image
            except Exception as err:   # noqa: BLE001 - a camera that errors or times out is a row, not a crash
                _LOGGER.debug("portico cameras/info: the picture of %s failed: %s", entity_id, type(err).__name__)
                return _row(entity_id, None, None, None, SOURCE_FAILED)
        if size is None:
            return _row(entity_id, None, None, None, SOURCE_FAILED)
        return _row(entity_id, size[0], size[1], None, SOURCE_STILL)

    async def async_info(self, hass: HomeAssistant, entity_ids: list[str]) -> list[dict]:
        """One row per distinct id, in the order asked. The caller has checked every id is a `camera.*` id."""
        now = time.monotonic()
        self._prune(now)
        rows: dict[str, dict] = {}
        todo: list[str] = []
        for entity_id in dict.fromkeys(entity_ids):
            state = hass.states.get(entity_id)
            if state is None or state.state in _DEAD_STATES:
                rows[entity_id] = _row(entity_id, None, None, None, SOURCE_UNAVAILABLE)
                continue
            row = from_attributes(entity_id, state)
            if row is not None:
                rows[entity_id] = row
                continue
            cached = self._cached(entity_id, now)
            if cached is not None:
                rows[entity_id] = cached
                continue
            todo.append(entity_id)

        if todo:
            tasks = {entity_id: asyncio.ensure_future(self._probe(hass, entity_id)) for entity_id in todo}
            await asyncio.wait(tasks.values(), timeout=CALL_BUDGET)
            stamp = time.monotonic()
            for entity_id, task in tasks.items():
                if task.done() and not task.cancelled() and task.exception() is None:
                    row = task.result()
                    self._cache[entity_id] = _Cached(stamp, row)
                    rows[entity_id] = dict(row)
                else:                                # over the call's budget: stop it, say so, keep nothing
                    task.cancel()
                    rows[entity_id] = _row(entity_id, None, None, None, SOURCE_FAILED)
        await self._add_frigate(hass, rows)
        await self._add_streams(hass, rows)
        return [rows[entity_id] for entity_id in dict.fromkeys(entity_ids)]


def get_camera_info(hass: HomeAssistant) -> CameraInfo:
    data = hass.data.setdefault(DOMAIN, {})
    info = data.get(DATA_CAMERA_INFO)
    if info is None:
        info = data[DATA_CAMERA_INFO] = CameraInfo()
    return info
