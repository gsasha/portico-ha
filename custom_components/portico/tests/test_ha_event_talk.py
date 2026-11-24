# ruff: noqa: F811 - test parameters named after the imported fixtures
"""Front-door talk, T6 (portico_front_door_talk_plan.md): an `ha_event` door whose `talkback` option is on attaches to
the hub as the Go driver `unifi_protect_talk`; a press also sends `external_ring`; the provider's answer is the
connect-answer verdict; a failure on the Go side is readable on the Python side. The transport is faked (as in
test_ha_event_door); nothing here talks to a console, a camera or a door.
"""
from __future__ import annotations

import logging
from typing import Any

import pytest
from homeassistant.util import dt as dt_util
from datetime import timedelta
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.portico.door import hub as portico_hub
from custom_components.portico.door.drivers.ha_event import CAPABILITIES, DEFAULT_HOLD, TALKBACK_CAPABILITIES
from custom_components.portico.door.provider_impl import get_provider
from custom_components.portico.door.transport import TransportError
from custom_components.portico.signaling import _answerable

from .test_ha_event_door import BELL, _front_entry, _now_iso, _press, _Rings, _setup
from .test_observer import FakeGoTransport, _custom_integrations, fake_transport  # noqa: F401 - fixtures by name

KEY = "SECRET-PROTECT-KEY-1234"
TALK_CAPS = {**TALKBACK_CAPABILITIES}


class TalkFake(FakeGoTransport):
    """The Go side of `unifi_protect_talk`: configure registers the door under the config's device_id and returns the
    driver's own capabilities; external_ring / hangup / talk_health are recorded."""

    configure_error: TransportError | None = None
    ring_error: TransportError | None = None

    async def configure(self, driver: str, config) -> dict[str, Any]:
        config = dict(config)
        self.configured.append((driver, config))
        if self.configure_error is not None:
            raise self.configure_error
        self.registry_ids.append(config["device_id"])
        return dict(TALK_CAPS)

    async def external_ring(self, device_id: str, call_id: str, hold: float) -> dict[str, Any]:
        self.requests.append(("external_ring", (device_id, call_id, hold)))
        if self.ring_error is not None:
            raise self.ring_error
        return {"ok": True}

    async def hangup(self, call_id: str, reason: str) -> dict[str, Any]:
        self.requests.append(("hangup", (call_id, reason)))
        return {"ok": True}

    async def talk_health(self, device_id: str) -> dict[str, Any]:
        self.requests.append(("talk_health", (device_id,)))
        return {"session_up": False, "bytes_to_camera": 0, "frames_from_camera": 0, "drops_up": 0, "drops_down": 0,
                "last_ffmpeg": "", "last_end": "hung up"}

    async def answer(self, *args, **kwargs):             # the legacy IPC answer must NOT be used by this door
        self.requests.append(("answer", args))
        raise AssertionError("IPC answer called for a connect-answer door")


@pytest.fixture
def talk_transport(fake_transport, monkeypatch):
    TalkFake.configure_error = None
    TalkFake.ring_error = None
    monkeypatch.setattr(portico_hub, "TransportClient", TalkFake)
    return TalkFake


def _talk_entry(hass, **extra):
    return _front_entry(hass, talkback=True, protect_api_key=KEY, **extra)


@pytest.fixture
async def talk_front(hass, talk_transport):
    _press(hass, "old-1", stamp=_now_iso(-3600))
    entry = _talk_entry(hass)
    await _setup(hass, entry)
    return entry, entry.runtime_data, _Rings(hass), talk_transport.instances[-1]


async def test_talkback_door_attaches_as_the_go_driver_with_the_options_over_defaults(talk_front):
    entry, door, rings, go = talk_front
    (driver, config), = go.configured
    assert driver == "unifi_protect_talk"
    assert config["device_id"] == f"ha_event:{BELL}" == door.device_id
    assert config["protect_api_key"] == KEY and config["protect_host"] == "192.168.2.1"
    assert config["downlink_url"] == "rtsp://192.168.2.2:8554/doorbell" and config["camera_id"] == "cam-test-1" and config["guard_url"] == ""
    assert config["verify_ssl"] is False and config["gain"] == 1
    assert door.talk_attached is True and door.capabilities == TALK_CAPS


async def test_talkback_door_is_answerable_and_has_no_open_path(hass, talk_front):
    entry, door, rings, go = talk_front
    (row,) = get_provider(hass).doors()
    assert _answerable(row["capabilities"]) is True
    assert row["capabilities"]["HasRelay"] is False and row["capabilities"]["DirectCapable"] is False
    # A talk-attached door publishes away_media (the option defaults on): a phone away from home carries a Talk call's
    # audio as frames (away_media.py, out- ids). The ring-only door WITHOUT talk-back stays false (test_ha_event_door).
    assert row["away_media"] is True


async def test_a_press_rings_the_phones_and_sends_external_ring_first(hass, talk_front):
    entry, door, rings, go = talk_front
    _press(hass, "e-1")
    await hass.async_block_till_done()
    assert door.call_state.state == "ringing" and len(rings.ringing) == 1
    assert ("external_ring", (door.device_id, "ha-event-e-1", float(DEFAULT_HOLD))) in go.requests


async def test_a_press_during_my_call_out_hangs_it_up_and_rings_nothing(hass, talk_front, caplog):
    """Alice, 2026-11-11: a bell press while my call out is live closes that call and nothing else: no ring in the
    tracker, none for the phones, no external_ring to Go; Go is told to hang up the dialled call with `bell_pressed`."""
    entry, door, rings, go = talk_front
    go.state("out-47", "ringing", device_id=door.device_id)
    go.state("out-47", "active", device_id=door.device_id)
    assert door.call_state.outbound_state == "in_call"
    with caplog.at_level(logging.INFO):
        _press(hass, "e-1")
        await hass.async_block_till_done()
    assert ("hangup", ("out-47", "bell_pressed")) in go.requests
    assert not [r for r in go.requests if r[0] == "external_ring"]
    assert door.call_state.state == "idle" and not rings.ringing
    assert "bell during own call: hung up, no ring" in caplog.text
    go.state("out-47", "ended", device_id=door.device_id)       # what Go reports once the dial is hung up
    assert door.call_state.outbound_call_id is None and door.call_state.outbound_state == "idle"


async def test_a_press_after_the_call_out_ended_rings_as_usual(hass, talk_front):
    entry, door, rings, go = talk_front
    go.state("out-47", "ringing", device_id=door.device_id)
    go.state("out-47", "ended", device_id=door.device_id)
    _press(hass, "e-1")
    await hass.async_block_till_done()
    assert door.call_state.state == "ringing" and len(rings.ringing) == 1
    assert ("external_ring", (door.device_id, "ha-event-e-1", float(DEFAULT_HOLD))) in go.requests


async def test_answer_is_the_connect_answer_verdict_and_never_the_ipc_answer(hass, talk_front):
    entry, door, rings, go = talk_front
    _press(hass, "e-1")
    await hass.async_block_till_done()
    verdict = await get_provider(hass).answer(door.device_id, "ha-event-e-1", client_kind="app", claimant="u1")
    assert verdict["verdict"] == "answered" and verdict["mode"] == "relay"
    assert not [r for r in go.requests if r[0] == "answer"]
    assert door.call_state.state == "ringing"       # moves when Go reports `answered`, once the leg is up
    go.state("ha-event-e-1", "answered", device_id=door.device_id)
    go.state("ha-event-e-1", "active", device_id=door.device_id)
    assert door.call_state.state == "active"


async def test_python_expiry_hangs_the_go_call_up_too(hass, talk_front):
    entry, door, rings, go = talk_front
    _press(hass, "e-1")
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=DEFAULT_HOLD + 1))
    await hass.async_block_till_done()
    assert door.call_state.state == "idle"
    assert ("hangup", ("ha-event-e-1", "ring_expired")) in go.requests


async def test_a_refused_external_ring_still_rings_the_phones(hass, talk_front, caplog):
    entry, door, rings, go = talk_front
    go.ring_error = TransportError("busy", "already holding a call")
    with caplog.at_level(logging.WARNING):
        _press(hass, "e-1")
        await hass.async_block_till_done()
    assert len(rings.ringing) == 1
    assert "external_ring" in caplog.text and "busy" in caplog.text and KEY not in caplog.text


async def test_a_go_failure_is_readable_on_the_python_side(hass, talk_front, caplog):
    entry, door, rings, go = talk_front
    with caplog.at_level(logging.INFO):
        go.notify("call_reason", {"device_id": door.device_id, "call_id": "ha-event-e-1", "kind": "answer_failed",
                                  "reason": "the old intercom holds the talkback session: busy"})
    assert door.last_call_reason == {"call_id": "ha-event-e-1", "kind": "answer_failed",
                                     "reason": "the old intercom holds the talkback session: busy"}
    assert any(r.levelno == logging.WARNING and "answer_failed" in r.getMessage() for r in caplog.records)
    with caplog.at_level(logging.INFO):    # who hung up (Go's "hung up (<why>)") is recorded, and is not a warning
        caplog.clear()
        go.notify("call_reason", {"device_id": door.device_id, "call_id": "ha-event-e-1", "kind": "ended",
                                  "reason": "hung up (cancelled)"})
    assert door.last_call_reason["reason"] == "hung up (cancelled)"
    assert not any(r.levelno >= logging.WARNING for r in caplog.records)
    go.notify("call_reason", {"device_id": door.device_id, "call_id": "ha-event-e-1", "kind": "answer_failed",
                              "reason": "the old intercom holds the talkback session: busy"})
    go.notify("call_reason", {"device_id": door.device_id, "call_id": "c", "kind": "ended", "reason": 5})   # malformed
    assert door.last_call_reason["call_id"] == "ha-event-e-1"
    assert await door.talk_health() == {"session_up": False, "bytes_to_camera": 0, "frames_from_camera": 0,
                                        "drops_up": 0, "drops_down": 0, "last_ffmpeg": "", "last_end": "hung up"}
    from custom_components.portico.door.diagnostics import door_diagnostics_sections
    talk = door_diagnostics_sections(entry)["runtime"]["talk"]
    assert talk["attached"] is True and talk["last_call_reason"]["kind"] == "answer_failed"


async def test_the_key_is_redacted_in_the_diagnostics_download(hass, talk_front):
    from custom_components.portico.diagnostics import async_get_config_entry_diagnostics

    entry, door, rings, go = talk_front
    dump = await async_get_config_entry_diagnostics(hass, entry)
    assert KEY not in repr(dump)


@pytest.mark.parametrize("error", [TransportError("unknown_driver", "unifi_protect_talk"), ConnectionError("down")])
async def test_when_the_go_driver_cannot_attach_the_door_stays_ring_only(hass, talk_transport, caplog, error):
    talk_transport.configure_error = error
    _press(hass, "old-1", stamp=_now_iso(-3600))
    entry = _talk_entry(hass)
    with caplog.at_level(logging.WARNING):
        await _setup(hass, entry)
    door = entry.runtime_data
    assert door.talk_attached is False and door.capabilities == CAPABILITIES
    assert _answerable(get_provider(hass).doors()[0]["capabilities"]) is False
    assert "ring-only" in caplog.text and KEY not in caplog.text
    rings = _Rings(hass)
    _press(hass, "e-1")
    await hass.async_block_till_done()
    assert len(rings.ringing) == 1
    verdict = await get_provider(hass).answer(door.device_id, "ha-event-e-1", client_kind="app", claimant="u1")
    assert verdict["verdict"] == "refused"
    assert not [r for t in talk_transport.instances for r in t.requests if r[0] == "external_ring"]


async def test_an_entry_without_talkback_never_touches_the_hub_or_sends_external_ring(hass, fake_transport):
    _press(hass, "old-1", stamp=_now_iso(-3600))
    entry = _front_entry(hass)
    await _setup(hass, entry)
    rings = _Rings(hass)
    _press(hass, "e-1")
    await hass.async_block_till_done()
    assert len(rings.ringing) == 1
    assert fake_transport.instances == [] and entry.runtime_data.talk_attached is False
    assert entry.runtime_data.capabilities == CAPABILITIES


async def test_unloading_a_talkback_door_deconfigures_it(hass, talk_front):
    entry, door, rings, go = talk_front
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert ("deconfigure", (door.device_id,)) in go.requests
