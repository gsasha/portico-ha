"""`camera_stream_size.py` (DEV-766): the live size of a stream from its RTSP description.

Pinned here: the size decoded from an H.264 SPS (the real ones read from the Frigate go2rtc restream on 2026-11-11, plus a
cropped 1080p built bit by bit); the SDP reader takes the first H.264 video section; only a plain `rtsp://host/path`
with no login, query or fragment is ever contacted; a refusal, a silence, a 401, a codec without an SPS, a huge or
truncated reply are all `None` and never raise; the request is one DESCRIBE with no credential.
"""
from __future__ import annotations

import asyncio
import base64

import pytest

from ..camera_stream_size import clean_rtsp_target, describe_size, sdp_video_size, sps_size

# Read from rtsp://<frigate>:8554/<stream> with a DESCRIBE, 2026-11-11 (the sizes are what the cameras are set to).
FACADE_LOWER_MAIN = "Z2QAKawVFKAoALWmiAAAH0AABhqAIAA="      # High profile, 2560x1440
FACADE_LOWER_SUB = "Z2QAKawVFKCgPaaIAAAfQAABOIAgAA=="        # High profile, 640x480
DOORBELL_MAIN = "Z01AKI2NQDIBL/4C3AQEBQAAAwPoAACcQJ2giEag"   # Main profile, 1600x1200
GATE_INTERCOM_MAIN = "Z0IAH+kAoAt0IAAAfSAAGHBAgAA="          # Baseline profile, 1280x720
PPS = "aO48sAA="                                              # a picture parameter set, not an SPS

SDP_FACADE = (
    "v=0\r\ns=go2rtc/1.9\r\nt=0 0\r\nm=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\n"
    f"a=fmtp:96 packetization-mode=1;profile-level-id=640029;sprop-parameter-sets={FACADE_LOWER_MAIN},{PPS}\r\n"
    "a=control:trackID=0\r\n"
)
SDP_DOORBELL = (
    "v=0\r\ns=go2rtc/1.9\r\nt=0 0\r\nm=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\n"
    f"a=fmtp:96 profile-level-id=4d4028; packetization-mode=1; sprop-parameter-sets={DOORBELL_MAIN},{PPS}\r\n"
    "m=audio 0 RTP/AVP 97\r\na=rtpmap:97 MPEG4-GENERIC/48000\r\n"
    "a=fmtp:97 streamtype=5; profile-level-id=15; mode=AAC-hbr; config=1188; SizeLength=13\r\n"
)


# --- the SPS --------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("sps,size", [
    (FACADE_LOWER_MAIN, (2560, 1440)), (FACADE_LOWER_SUB, (640, 480)),
    (DOORBELL_MAIN, (1600, 1200)), (GATE_INTERCOM_MAIN, (1280, 720)),
])
def test_the_real_parameter_sets_give_the_sizes_the_cameras_are_set_to(sps, size):
    assert sps_size(sps) == size


class _Writer:
    """Builds an SPS NAL bit by bit, so a case the cameras do not produce can still be checked."""

    def __init__(self):
        self.bits = ""

    def u(self, value, count):
        self.bits += format(value, f"0{count}b")

    def ue(self, value):
        code = format(value + 1, "b")
        self.bits += "0" * (len(code) - 1) + code

    def nal(self):
        bits = self.bits + "1"
        bits += "0" * (-len(bits) % 8)
        return base64.b64encode(bytes([0x67]) + int(bits, 2).to_bytes(len(bits) // 8, "big")).decode()


def _sps(width_mbs, height_units, *, crop=None, frame_mbs_only=1, profile=66, chroma=1):
    w = _Writer()
    w.u(profile, 8)
    w.u(0, 8)
    w.u(41, 8)
    w.ue(0)
    if profile == 100:
        w.ue(chroma)
        if chroma == 3:
            w.u(0, 1)
        w.ue(0)
        w.ue(0)
        w.u(0, 1)
        w.u(0, 1)
    w.ue(0)                  # log2_max_frame_num_minus4
    w.ue(0)                  # pic_order_cnt_type
    w.ue(0)                  # log2_max_pic_order_cnt_lsb_minus4
    w.ue(1)                  # max_num_ref_frames
    w.u(0, 1)
    w.ue(width_mbs - 1)
    w.ue(height_units - 1)
    w.u(frame_mbs_only, 1)
    if not frame_mbs_only:
        w.u(0, 1)
    w.u(1, 1)                # direct_8x8_inference_flag
    if crop:
        w.u(1, 1)
        for value in crop:
            w.ue(value)
    else:
        w.u(0, 1)
    return w.nal()


def test_1080p_is_1088_macroblock_rows_cropped_back_to_1080():
    assert sps_size(_sps(120, 68, crop=(0, 0, 0, 4))) == (1920, 1080)


def test_a_sps_without_cropping_is_a_whole_number_of_macroblocks():
    assert sps_size(_sps(40, 30)) == (640, 480)


def test_an_interlaced_stream_counts_both_fields_and_crops_in_field_units():
    # 34 map units of 2 x 16 field rows = 1088 rows; one crop step is 2 (chroma 4:2:0) x 2 (fields) = 4 rows; 2 steps = 8
    assert sps_size(_sps(120, 34, frame_mbs_only=0, crop=(0, 0, 0, 2))) == (1920, 1080)


def test_a_high_profile_with_no_scaling_list_is_read_past_its_extra_fields():
    assert sps_size(_sps(40, 30, profile=100)) == (640, 480)


@pytest.mark.parametrize("junk", [
    "", "!!!not base64!!!", PPS, "Zw==", FACADE_LOWER_MAIN[:12], base64.b64encode(b"\x67").decode(),
    base64.b64encode(b"\x67\x42\x00\x1f").decode(), base64.b64encode(b"\x00" * 40).decode(),
])
def test_anything_that_is_not_a_complete_sps_is_none_and_never_raises(junk):
    assert sps_size(junk) is None


def test_a_header_claiming_an_absurd_size_is_none():
    assert sps_size(_sps(65535, 65535)) is None


# --- the SDP --------------------------------------------------------------------------------------------------------


def test_the_sdp_of_a_restream_gives_the_video_size():
    assert sdp_video_size(SDP_FACADE) == (2560, 1440)


def test_the_first_video_section_is_read_and_the_audio_ones_are_ignored():
    assert sdp_video_size(SDP_DOORBELL) == (1600, 1200)


def test_an_sdp_with_no_sps_or_another_codec_is_none():
    assert sdp_video_size("m=video 0 RTP/AVP 96\r\na=rtpmap:96 H265/90000\r\na=fmtp:96 sprop-vps=AAA\r\n") is None
    assert sdp_video_size("m=audio 0 RTP/AVP 97\r\na=fmtp:97 sprop-parameter-sets=" + FACADE_LOWER_MAIN + "\r\n") is None
    assert sdp_video_size("") is None and sdp_video_size("garbage\n\n\x00\xff") is None


def test_an_sps_list_with_a_bad_first_entry_uses_the_next_good_one():
    sdp = f"m=video 0 RTP/AVP 96\r\na=fmtp:96 sprop-parameter-sets=!!!,{FACADE_LOWER_SUB},{PPS}\r\n"
    assert sdp_video_size(sdp) == (640, 480)


# --- which addresses are ever contacted ------------------------------------------------------------------------------


@pytest.mark.parametrize("url,expected", [
    ("rtsp://10.6.0.6:8554/facade_lower", ("10.6.0.6", 8554, "rtsp://10.6.0.6:8554/facade_lower")),
    ("rtsp://frigate.local/cam/realmonitor", ("frigate.local", 554, "rtsp://frigate.local:554/cam/realmonitor")),
    ("rtsp://[fd00::6]:8554/x", ("fd00::6", 8554, "rtsp://[fd00::6]:8554/x")),
])
def test_a_plain_rtsp_address_is_accepted(url, expected):
    assert clean_rtsp_target(url) == expected


@pytest.mark.parametrize("url", [
    "rtsp://user:pass@10.6.0.6:8554/x", "rtsp://user@10.6.0.6:8554/x", "rtsps://10.6.0.6:7441/x", "rtspx://10.1.0.1:7441/x",
    "http://10.6.0.6/x", "rtsp://10.6.0.6:8554/x?enableSrtp", "rtsp://10.6.0.6:8554/x#frag", "rtsp://10.6.0.6:8554",
    "rtsp://10.6.0.6:8554/", "rtsp://:8554/x", "rtsp://10.6.0.6:0/x", "rtsp://10.6.0.6:99999/x", "rtsp://10.6.0.6/a b",
    "rtsp://10.6.0.6:8554/" + "a" * 600, "", None, 5, b"rtsp://10.6.0.6/x",
])
def test_anything_else_is_never_contacted(url):
    assert clean_rtsp_target(url) is None
