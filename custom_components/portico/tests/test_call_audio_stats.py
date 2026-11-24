# ruff: noqa: F811 - test parameters named after the imported fixtures
"""Call audio counters, the integration half (DEV-811 P3): what the transport's `media_closed.stats` and
`call_audio_stats` carry reaches `call_stats_parts`, and so the `portico_call_stats` event; what is sent to the wheel
(`call_stats` in `configure`, `stats` in `media_open`) is sent only to a wheel that can take it. Fakes only."""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_capture_events, async_fire_time_changed

from .. import away_media
from ..call_events import (
    EVENT_CALL_STATS,
    STATS_DELAY_S,
    CallEvents,
    add_stats_part,
    add_wire_stats,
    call_stats_parts,
)
from ..door import gate_sip_listen
from ..door.hub import TransportHub
from ..door.transport import TransportClient
from .driver_part import SERVER_DEVICE_ID, _server_entry, get_provider
from .test_away_media import OPEN, _answer, _device, _open
from .test_call_events import _rig
from .test_signaling import _recv, _setup_signaling
from .test_transport_provider import _add_device, _provider

pytestmark = pytest.mark.asyncio

HIST = [0] * 31
LEG = {"uplink_frames": 480, "uplink_bad_frame": 0, "uplink_full_drops": 1, "downlink_frames": 470,
       "downlink_send_ms_max": 3, "uplink_gap_ms_max": 90, "uplink_gap_ms_p95": 45}
GATE = {"station_rate": 16000, "station_frames": 470, "pre_gain_hist": HIST, "would_clip_frames": 2,
        "saturated_samples": 17, "queued_to_app": 470, "dropped_to_app": 0, "received_from_app": 480, "dropped_from_app": 1}


# ---- add_wire_stats: what may ride a counter ----------------------------------------------------------------------------


async def test_only_numbers_and_short_integer_lists_become_a_part(hass):
    add_wire_stats(hass, "c1", {
        "call_id": "c1", "device_id": "gate-1", "reason": "idle",       # routing and words: never a counter
        "uplink_frames": 5, "ratio": 0.5, "flag": True, "addr": "10.0.0.4", "hist": [1, 2, 3],
        "mixed": [1, "x"], "long": list(range(65)), "nested": {"a": 1}, "k" * 41: 1})
    assert call_stats_parts(hass, "c1") == [{"uplink_frames": 5, "ratio": 0.5, "hist": [1, 2, 3]}]


@pytest.mark.parametrize("raw", [None, "x", 5, [], {}, {"call_id": "c1"}, {"flag": True}])
async def test_a_bad_or_empty_payload_is_dropped_quietly(hass, raw):
    add_wire_stats(hass, "c1", raw)
    add_wire_stats(hass, None, {"uplink_frames": 1})
    add_wire_stats(hass, "", {"uplink_frames": 1})
    assert call_stats_parts(hass, "c1") == [] and call_stats_parts(hass, "") == []


# ---- the provider: both close orders deliver the parts once -----------------------------------------------------------


async def test_the_phone_hanging_up_first_still_delivers_the_legs_counters(hass):
    """`media_close` pops the leg; the `media_closed` that follows carries the counters and must not be lost with it."""
    _, runtime = _add_device(hass, device_id="10.3.0.20")
    runtime.client.media_open = AsyncMock(return_value={"codec": "l16", "frame_bytes": 1280, "frame_ms": 40})
    runtime.client.media_close = AsyncMock(return_value={"ok": True})
    runtime.call_state.apply_event({"call_id": "c1", "state": "ringing"})
    provider, closed = _provider(hass), []
    await provider.media_open("c1", "l16", lambda d: None, closed.append)
    await provider.media_close("c1", "closed_by_phone")                  # the leg is gone from the provider
    provider.on_media_notification("media_closed", {"call_id": "c1", "reason": "closed_by_phone", "stats": LEG})
    assert closed == []                                                  # no leg: the callback is not called twice
    assert call_stats_parts(hass, "c1") == [LEG]


async def test_the_station_ending_the_call_delivers_the_counters_and_closes_the_leg_once(hass):
    _, runtime = _add_device(hass, device_id="10.3.0.20")
    runtime.client.media_open = AsyncMock(return_value={"codec": "l16", "frame_bytes": 1280, "frame_ms": 40})
    runtime.call_state.apply_event({"call_id": "c1", "state": "ringing"})
    provider, closed = _provider(hass), []
    await provider.media_open("c1", "l16", lambda d: None, closed.append)
    provider.on_media_notification("media_closed", {"call_id": "c1", "reason": "idle", "stats": LEG})
    provider.on_media_notification("call_audio_stats", {"call_id": "c1"})   # not a media notification: ignored here
    provider.on_call_audio_stats({"device_id": "10.3.0.20", "call_id": "c1", **GATE})
    assert closed == ["idle"]
    assert call_stats_parts(hass, "c1") == [LEG, GATE]                      # each part exactly once, in arrival order


async def test_a_media_closed_without_stats_adds_nothing_old_wheel(hass):
    _, runtime = _add_device(hass, device_id="10.3.0.20")
    runtime.client.media_open = AsyncMock(return_value={"codec": "pcmu", "frame_bytes": 320, "frame_ms": 40})
    runtime.call_state.apply_event({"call_id": "c1", "state": "ringing"})
    provider, closed = _provider(hass), []
    await provider.media_open("c1", "pcmu", lambda d: None, closed.append)
    provider.on_media_notification("media_closed", {"call_id": "c1", "reason": "idle"})
    assert closed == ["idle"] and call_stats_parts(hass, "c1") == []


async def test_the_hub_routes_call_audio_stats_by_call_id_to_the_provider(hass):
    _add_device(hass, device_id="10.3.0.20")
    hub = TransportHub(hass)                                             # no doors attached: the route does not need one
    hub._handle_notification("call_audio_stats", {"device_id": "10.3.0.20", "call_id": "c9", **GATE})
    hub._handle_notification("call_audio_stats", {"device_id": "10.3.0.20", "call_id": 7, "uplink_frames": 1})
    hub._handle_notification("call_audio_stats", {"call_id": "c9"})
    assert call_stats_parts(hass, "c9") == [GATE]


# ---- what is sent to the wheel -------------------------------------------------------------------------------------------


async def _ringing_server_door(hass, *, features):
    caps = {"HasRelay": False, "StationRegistered": True}
    if features is not None:
        caps["Features"] = features
    entry, runtime = _add_device(hass, device_id="gate-1", driver="gate_sip_server", capabilities=caps)
    runtime.client.media_open = AsyncMock(return_value={"codec": "l16", "frame_bytes": 1280, "frame_ms": 40})
    runtime.call_state.apply_event({"call_id": "c1", "state": "ringing"})
    return entry, runtime


@pytest.mark.parametrize("features", [None, [], ["something_else"], "call_stats"])
async def test_an_old_wheel_or_a_revert_wheel_gets_the_media_open_of_old(hass, features):
    _, runtime = await _ringing_server_door(hass, features=features)
    await _provider(hass).media_open("c1", "l16", lambda d: None, lambda r: None)
    runtime.client.media_open.assert_awaited_once_with("c1", "l16")          # no `stats` argument at all


async def test_a_wheel_that_lists_call_stats_is_asked_for_the_legs_counters(hass):
    _, runtime = await _ringing_server_door(hass, features=["call_stats"])
    await _provider(hass).media_open("c1", "l16", lambda d: None, lambda r: None)
    runtime.client.media_open.assert_awaited_once_with("c1", "l16", stats=True)


async def test_another_driver_is_never_asked_even_if_it_lists_the_feature(hass):
    _, runtime = _add_device(hass, device_id="10.3.0.20", capabilities={"Features": ["call_stats"]})   # dahua_sip
    runtime.client.media_open = AsyncMock(return_value={"codec": "l16", "frame_bytes": 1280, "frame_ms": 40})
    runtime.call_state.apply_event({"call_id": "c1", "state": "ringing"})
    await _provider(hass).media_open("c1", "l16", lambda d: None, lambda r: None)
    runtime.client.media_open.assert_awaited_once_with("c1", "l16")


async def test_the_client_sends_the_stats_key_only_when_asked():
    client = TransportClient("/nonexistent")
    client._request = AsyncMock(return_value={})
    await client.media_open("c1", "l16")
    client._request.assert_awaited_with("media_open", {"call_id": "c1", "codec": "l16"})
    await client.media_open("c1", "l16", stats=True)
    client._request.assert_awaited_with("media_open", {"call_id": "c1", "codec": "l16", "stats": True})


async def test_the_server_entrys_configure_payload_asks_for_call_stats_and_no_other_driver_does(hass):
    server = await gate_sip_listen.transport_config(hass, _server_entry(hass), SERVER_DEVICE_ID)
    assert server["call_stats"] is True and server["device_id"] == SERVER_DEVICE_ID
    other = _server_entry(hass, driver="dahua_sip", device_id="x:1")
    assert "call_stats" not in await gate_sip_listen.transport_config(hass, other, "x:1")


# ---- away_media: the integration's own numbers --------------------------------------------------------------------------


async def test_a_phone_channel_on_a_wheel_with_call_stats_records_the_integrations_numbers(hass, hass_ws_client, hass_admin_user):
    _entry, runtime = _device(hass, hass_admin_user, driver="gate_sip_server")
    runtime.capabilities = {**runtime.capabilities, "Features": ["call_stats"]}
    await _setup_signaling(hass)
    client = await hass_ws_client(hass)
    await _answer(client)
    resp = await _open(client, 2, codec="l16")
    assert resp["success"], resp
    runtime.client.media_open.assert_awaited_once_with("c1", "l16", stats=True)
    for i in range(2):
        get_provider(hass).on_media_notification("media_frame", {"call_id": "c1", "d": f"F{i}AA"})
        await _recv(client)                                              # the downlink event on the open command
    for i in range(3):
        await client.send_json({"id": 10 + i, "type": "portico/media_frame", "call_id": "c1", "d": f"F{i}AA"})
    await client.send_json({"id": 20, "type": "portico/media_close", "call_id": "c1"})
    for _ in range(2):
        await _recv(client)
    await hass.async_block_till_done()
    (part,) = call_stats_parts(hass, "c1")
    assert part["ha_frames_down"] == 2 and part["ha_frames_up"] == 3, part
    assert away_media._registry(hass).channels == {}


async def test_without_the_feature_the_channel_records_nothing(hass, hass_ws_client, hass_admin_user):
    _entry, runtime = _device(hass, hass_admin_user, driver="gate_sip_server")
    await _setup_signaling(hass)
    client = await hass_ws_client(hass)
    await _answer(client)
    await _open(client, 2)
    await client.send_json({"id": 20, "type": "portico/media_close", "call_id": "c1"})
    for _ in range(2):
        await _recv(client)
    await hass.async_block_till_done()
    assert call_stats_parts(hass, "c1") == []
    runtime.client.media_open.assert_awaited_once_with("c1", "pcmu")


# ---- the loop-lag sampler, on a fake clock -------------------------------------------------------------------------------


class FakeLoop:
    """A clock and a sleep that the test drives: each sleep takes its 40 ms plus the next scripted lateness."""

    def __init__(self, lates_ms):
        self.now = 100.0
        self._lates = list(lates_ms)

    def clock(self):
        return self.now

    async def sleep(self, seconds):
        if not self._lates:
            raise asyncio.CancelledError
        self.now += seconds + self._lates.pop(0) / 1000.0


async def test_the_lag_sampler_reports_the_max_and_a_p95_from_the_late_wakes():
    loop = FakeLoop([0.0] * 17 + [150.0, 3.0, 12.0])                    # 20 wakes: one 150 ms stall
    lag = away_media.LoopLag(clock=loop.clock)
    with pytest.raises(asyncio.CancelledError):
        await lag.run(sleep=loop.sleep)
    assert lag.samples == 20
    out = lag.summary()
    assert out["loop_lag_ms_max"] == pytest.approx(150.0, abs=0.2)
    # ceil(0.95 * 20) = 19: reached in the 20..40 bin (the 12.0), whose bound is 20; the stall is the 20th sample
    assert out["loop_lag_ms_p95"] == 20.0


async def test_the_lag_p95_sees_a_stall_in_more_than_five_percent_of_the_wakes():
    lag = away_media.LoopLag()
    for late in [0] * 8 + [300, 300]:
        lag.note(late)
    assert lag.summary() == {"loop_lag_ms_max": 300.0, "loop_lag_ms_p95": 300.0}
    assert away_media.LoopLag().summary() == {}                         # no samples: nothing to say


# ---- the whole seam: parts of a call become one portico_call_stats event ------------------------------------------------


async def test_the_legs_the_gate_and_the_integration_parts_fire_one_stats_event_and_an_info_line(hass, caplog):
    fake, _events, _clock = _rig(hass)
    got = async_capture_events(hass, EVENT_CALL_STATS)
    fake.ring("gate-1", "c3")
    fake.state("c3", "answered")
    fake.state("c3", "ended")
    add_wire_stats(hass, "c3", LEG)
    add_wire_stats(hass, "c3", GATE)
    add_stats_part(hass, "c3", {"ha_frames_down": 470, "ha_frames_up": 480, "loop_lag_ms_max": 12.0})
    with caplog.at_level(logging.INFO, logger="custom_components.portico.call_events"):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=STATS_DELAY_S + 1))
        await hass.async_block_till_done()
    (event,) = got
    assert event.data["call_id"] == "c3" and event.data["door_id"] == "gate-1"
    for key in ("uplink_frames", "uplink_gap_ms_p95", "station_rate", "would_clip_frames", "saturated_samples",
                "ha_frames_up", "loop_lag_ms_max"):
        assert key in event.data, key
    assert event.data["pre_gain_hist"] == HIST
    line = next(r.getMessage() for r in caplog.records if "audio" in r.getMessage() and "c3" in r.getMessage())
    assert "uplink_frames" in line and "pre_gain_hist" not in line      # the INFO line keeps to the scalars


async def test_the_gain_limiter_and_level_scalars_ride_into_the_info_line_but_the_histograms_do_not(hass, caplog):
    """D-20261121: the transport's gain, limiter and level scalars (and the arrival-gap histogram) are ordinary counters:
    scalars reach the INFO line, lists stay in the event."""
    fake, _events, _clock = _rig(hass)
    got = async_capture_events(hass, EVENT_CALL_STATS)
    fake.ring("gate-1", "c4")
    fake.state("c4", "answered")
    fake.state("c4", "ended")
    add_wire_stats(hass, "c4", {**GATE, "down_gain": 4, "down_limited_ratio": 0.27, "up_gain": 3, "up_limited_ratio": 0.0,
                                "up_rms_pre_dbfs": -31.5, "up_peak_post_dbfs": -2.9, "pre_gain_p50_dbfs": -30,
                                "pre_gain_p95_dbfs": -12, "pre_gain_min_dbfs": -60})
    add_wire_stats(hass, "c4", {**LEG, "uplink_gap_bins": [10, 2, 1, 0, 0, 0, 0, 0, 1]})
    with caplog.at_level(logging.INFO, logger="custom_components.portico.call_events"):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=STATS_DELAY_S + 1))
        await hass.async_block_till_done()
    (event,) = got
    assert event.data["up_gain"] == 3 and event.data["uplink_gap_bins"] == [10, 2, 1, 0, 0, 0, 0, 0, 1]
    line = next(r.getMessage() for r in caplog.records if "audio" in r.getMessage() and "c4" in r.getMessage())
    for key in ("down_gain", "down_limited_ratio", "up_gain", "up_rms_pre_dbfs", "pre_gain_p50_dbfs", "pre_gain_p95_dbfs"):
        assert key in line, key
    assert "uplink_gap_bins" not in line and "pre_gain_hist" not in line
