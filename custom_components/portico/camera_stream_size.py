"""The size of a camera's LIVE stream, read from the RTSP restream's own description (DEV-766).

`camera_info.py` measures the size of the still a camera entity returns. For a Frigate camera that is Frigate's
detect frame (the sub stream, 640x480), while the live stream the same entity offers is the main restream
(2560x1440), so the picker told the owner the wrong thing. The truth is in the stream: an RTSP DESCRIBE answers with an
SDP whose H.264 `sprop-parameter-sets` is the stream's SPS, and the SPS carries the picture size. One request, no
ffmpeg, no wait for a keyframe, about ten milliseconds against a go2rtc restream.

Pinned here: only `rtsp://host[:port]/path` with NO credentials, query or fragment is ever contacted (a URL with a
login is skipped, never used); the request is a single DESCRIBE with no authentication and no body; the reply is read
up to 64 KB and the connection is closed; H.264 is read, any other codec (or no SPS) is "unknown", never a guess;
every failure (refused, timeout, a 401, bad SDP) is `None` and never raises; nothing here logs a URL.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
from urllib.parse import urlsplit

MAX_REPLY = 65536
MAX_DIMENSION = 100_000
_HIGH_PROFILES = (100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135)


class _Bits:
    """An MSB-first bit reader over bytes, with the Exp-Golomb codes the SPS uses."""

    def __init__(self, data: bytes) -> None:
        self._value = int.from_bytes(data, "big")
        self._length = len(data) * 8
        self._pos = 0

    def bit(self) -> int:
        if self._pos >= self._length:
            raise ValueError("short")
        out = (self._value >> (self._length - 1 - self._pos)) & 1
        self._pos += 1
        return out

    def bits(self, count: int) -> int:
        out = 0
        for _ in range(count):
            out = (out << 1) | self.bit()
        return out

    def ue(self) -> int:
        zeros = 0
        while self.bit() == 0:
            zeros += 1
            if zeros > 32:
                raise ValueError("bad exp-golomb")
        return (1 << zeros) - 1 + self.bits(zeros)

    def se(self) -> int:
        k = self.ue()
        return (k + 1) // 2 if k % 2 else -(k // 2)


def _unescape(nal: bytes) -> bytes:
    """Remove the emulation-prevention bytes (00 00 03 -> 00 00)."""
    out = bytearray()
    zeros = 0
    for byte in nal:
        if zeros >= 2 and byte == 3:
            zeros = 0
            continue
        out.append(byte)
        zeros = zeros + 1 if byte == 0 else 0
    return bytes(out)


def _skip_scaling_list(bits: _Bits, size: int) -> None:
    last = nxt = 8
    for _ in range(size):
        if nxt != 0:
            nxt = (last + bits.se() + 256) % 256
        last = last if nxt == 0 else nxt


def sps_size(b64: str) -> tuple[int, int] | None:
    """The picture size (cropping applied) of one base64 H.264 SPS NAL unit, or None for anything else."""
    try:
        nal = _unescape(base64.b64decode(b64, validate=True))
        if not nal or nal[0] & 0x1F != 7:
            return None
        bits = _Bits(nal[1:])
        profile = bits.bits(8)
        bits.bits(16)                      # constraint flags and level
        bits.ue()                          # seq_parameter_set_id
        chroma = 1
        if profile in _HIGH_PROFILES:
            chroma = bits.ue()
            if chroma == 3:
                bits.bit()                 # separate_colour_plane_flag
            bits.ue()                      # bit depth luma
            bits.ue()                      # bit depth chroma
            bits.bit()                     # qpprime_y_zero_transform_bypass_flag
            if bits.bit():                 # seq_scaling_matrix_present_flag
                for i in range(8 if chroma != 3 else 12):
                    if bits.bit():
                        _skip_scaling_list(bits, 16 if i < 6 else 64)
        bits.ue()                          # log2_max_frame_num_minus4
        poc_type = bits.ue()
        if poc_type == 0:
            bits.ue()
        elif poc_type == 1:
            bits.bit()
            bits.se()
            bits.se()
            for _ in range(bits.ue()):
                bits.se()
        bits.ue()                          # max_num_ref_frames
        bits.bit()                         # gaps_in_frame_num_value_allowed_flag
        width_mbs = bits.ue() + 1
        height_map_units = bits.ue() + 1
        frame_mbs_only = bits.bit()
        if not frame_mbs_only:
            bits.bit()                     # mb_adaptive_frame_field_flag
        bits.bit()                         # direct_8x8_inference_flag
        width = width_mbs * 16
        height = (2 - frame_mbs_only) * height_map_units * 16
        if bits.bit():                     # frame_cropping_flag
            left, right, top, bottom = bits.ue(), bits.ue(), bits.ue(), bits.ue()
            unit_x = 2 if chroma in (1, 2) else 1
            unit_y = (2 if chroma == 1 else 1) * (2 - frame_mbs_only)
            width -= (left + right) * unit_x
            height -= (top + bottom) * unit_y
    except (ValueError, binascii.Error):
        return None
    if not (0 < width <= MAX_DIMENSION and 0 < height <= MAX_DIMENSION):
        return None
    return width, height


def sdp_video_size(sdp: str) -> tuple[int, int] | None:
    """The size of the first H.264 video section of an SDP, from its `sprop-parameter-sets`; None when there is none."""
    in_video = False
    for line in sdp.replace("\r\n", "\n").split("\n"):
        if line.startswith("m="):
            in_video = line.startswith("m=video")
        elif in_video and line.startswith("a=fmtp:"):
            for part in line.split(" ", 1)[-1].replace(";", " ").split():
                if part.startswith("sprop-parameter-sets="):
                    for unit in part.split("=", 1)[1].split(","):
                        size = sps_size(unit)
                        if size is not None:
                            return size
    return None


def clean_rtsp_target(url: object) -> tuple[str, int, str] | None:
    """(host, port, url without anything extra) of a plain `rtsp://host[:port]/path`; None for any other address, in
    particular one carrying a login, a query or a fragment (never contacted)."""
    if not isinstance(url, str) or not url or len(url) > 512 or any(c.isspace() for c in url):
        return None
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if parts.scheme != "rtsp" or not host or parts.username is not None or parts.password is not None:
        return None
    if parts.query or parts.fragment or not parts.path or parts.path == "/":
        return None
    if port is None:
        port = 554
    if not 0 < port < 65536:
        return None
    authority = f"[{host}]" if ":" in host else host
    return host, port, f"rtsp://{authority}:{port}{parts.path}"


async def describe_size(url: object, timeout: float = 4.0) -> tuple[int, int] | None:
    """The live size of the H.264 stream at `url`, or None. Never raises."""
    target = clean_rtsp_target(url)
    if target is None:
        return None
    host, port, address = target
    writer = None
    try:
        async with asyncio.timeout(timeout):
            reader, writer = await asyncio.open_connection(host, port)
            writer.write(f"DESCRIBE {address} RTSP/1.0\r\nCSeq: 1\r\nAccept: application/sdp\r\n\r\n".encode())
            await writer.drain()
            data = b""
            while b"\r\n\r\n" not in data and len(data) < MAX_REPLY:
                chunk = await reader.read(4096)
                if not chunk:
                    break
                data += chunk
            head, _, body = data.partition(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            if len(lines[0].split()) < 2 or lines[0].split()[1] != "200":
                return None
            length = 0
            for line in lines[1:]:
                if line.lower().startswith("content-length:"):
                    length = int(line.split(":", 1)[1])
            length = min(length, MAX_REPLY)
            while len(body) < length:
                chunk = await reader.read(4096)
                if not chunk:
                    break
                body += chunk
        return sdp_video_size(body.decode("latin-1"))
    except (OSError, ValueError, TimeoutError, asyncio.TimeoutError):
        return None
    finally:
        if writer is not None:
            writer.close()
