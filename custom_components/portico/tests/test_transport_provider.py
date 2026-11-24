# ruff: noqa: F811 - test parameters named after the imported fixtures
"""TransportProvider, the driver part's own tests: its doors seen through the provider protocol, the call half
(dial, hangup, keepalive, webrtc_*, answer), the conformance suite (TEST-06), the bus event and the media_*
methods. HA-harness session; the Go transport is the fake of test_observer.py, or an AsyncMock client where a
test needs no process. Nothing here loads the UI part: the tests that read the provider THROUGH the UI part's
registry (the websocket, the push sender, content) are the UI part's `tests/test_door_through_the_ui_part.py`.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.portico.door.call_state import CallStateTracker
from custom_components.portico.door.const import DOMAIN, DRIVER_ID, ENTRY_TYPE, ENTRY_TYPE_DOOR
from custom_components.portico.door.provider_impl import DATA_TRANSPORT_PROVIDER, TransportProvider, install_provider
from custom_components.portico.door.door import DoorRuntime

from .fake_provider import FakeProvider
from .door_provider_conformance import CHECKS, check_provider
from .stub_hub import StubHub
from .test_observer import GATE_SIP_URL, _custom_integrations, _observer_entry, fake_transport  # noqa: F401 - fixtures


def _add_device(hass, *, device_id="127.0.0.1", driver="dahua_sip", capabilities=None, client=None,
                title="Mock Title", attach=True):
    """A door entry with a runtime built by hand (a mocked client, no process), attached to the provider."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=title,
        data={ENTRY_TYPE: ENTRY_TYPE_DOOR, "driver": driver, "host": device_id, "sip_user": "9999#0",
              "sip_password": "x", "rtsp_username": "u", "rtsp_password": "p"},
        unique_id=f"{driver}:{device_id}",
    )
    entry.add_to_hass(hass)
    runtime = DoorRuntime(hass, entry, StubHub())
    runtime.capabilities = capabilities or {"DirectCapable": True, "HasRelay": True}
    runtime._hub.client = client or AsyncMock()
    runtime.call_state = CallStateTracker(device_id)
    entry.runtime_data = runtime
    if attach:
        install_provider(hass).attach(entry)
    return entry, runtime


async def _setup_door(hass, **extra):
    entry = _observer_entry(hass, **extra)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _provider(hass) -> TransportProvider:
    return hass.data[DRIVER_ID][DATA_TRANSPORT_PROVIDER]


async def test_the_provider_is_created_at_domain_setup_before_any_entry(hass):
    """R1-21: `async_setup` makes the provider and registers THAT object with the registry. Its `domain` is the door
    provider id (`portico_intercom`, persisted in endpoint ids), not the integration's HA domain `portico`."""
    from homeassistant.setup import async_setup_component

    from custom_components.portico.provider import DATA_PROVIDERS

    assert await async_setup_component(hass, DOMAIN, {})
    provider = _provider(hass)
    assert isinstance(provider, TransportProvider) and provider.domain == DRIVER_ID != DOMAIN
    assert install_provider(hass) is provider
    assert hass.data[DOMAIN][DATA_PROVIDERS].provider(DRIVER_ID) is provider
    assert provider.doors() == []


async def test_a_door_is_listed_with_its_fields_and_no_ref(hass, fake_transport):
    entry = await _setup_door(hass)
    (door,) = _provider(hass).doors()
    assert door["id"] == GATE_SIP_URL and door["provider"] == DRIVER_ID and door["name"] == entry.title
    assert "ref" not in door                                   # the push door_ref is derived by the UI part (push/v2)
    assert (door["driver"], door["camera_entity_id"], door["available"], door["away_media"]) == (
        "gate_sip_observer", "camera.gate", True, True)
    assert door["capabilities"] == entry.runtime_data.capabilities
    assert _provider(hass).call_state(GATE_SIP_URL) == {"door_id": GATE_SIP_URL, "call_id": None, "state": "idle"}
    assert _provider(hass).call_state("no-such") is None


async def test_a_transition_reaches_a_subscriber_with_the_door_id(hass, fake_transport):
    await _setup_door(hass)
    seen: list[dict] = []
    _provider(hass).subscribe(seen.append)
    go = fake_transport.instances[-1]
    go.ring("gso-1")
    go.state("gso-1", "ended")
    assert [(t["door_id"], t["call_id"], t["prev_state"], t["state"]) for t in seen] == [
        (GATE_SIP_URL, "gso-1", "idle", "ringing"), (GATE_SIP_URL, "gso-1", "ringing", "ended")]


async def test_unloading_a_door_detaches_first_then_the_listeners_run(hass, fake_transport):
    entry = await _setup_door(hass)
    tracker = entry.runtime_data.call_state
    assert len(tracker._transition_listeners) == 1
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert _provider(hass).doors() == []
    assert tracker._transition_listeners == []
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert [d["id"] for d in _provider(hass).doors()] == [GATE_SIP_URL]


async def test_a_door_leaving_mid_ring_ends_the_call_for_subscribers_then_goes(hass, fake_transport):
    entry = await _setup_door(hass)
    seen: list[dict] = []
    _provider(hass).subscribe(seen.append)
    fake_transport.instances[-1].ring("gso-1")
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert [(t["state"], t["call_id"]) for t in seen] == [("ringing", "gso-1"), ("ended", "gso-1")]
    assert _provider(hass).call_state(GATE_SIP_URL) is None


async def test_door_listeners_run_after_attach_and_after_detach(hass, fake_transport):
    entry = await _setup_door(hass)
    provider = _provider(hass)
    states = []
    provider.subscribe_doors(lambda: states.append([d["id"] for d in provider.doors()]))
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert states == [[], [GATE_SIP_URL]]    # detach first (doors already lacks it), attach first (already has it)


async def test_open_relay_goes_to_the_doors_client_and_an_unknown_door_has_a_code(hass, fake_transport):
    entry = await _setup_door(hass)
    entry.runtime_data.client.open_relay = AsyncMock(return_value={"ok": True})
    assert await _provider(hass).open_relay(GATE_SIP_URL) == {"ok": True}
    entry.runtime_data.client.open_relay.assert_awaited_once_with(GATE_SIP_URL)
    with pytest.raises(Exception) as err:
        await _provider(hass).open_relay("no-such")
    assert err.value.code == "not_available"


async def test_secrets_for_a_known_door_and_a_named_phone_only(hass, fake_transport):
    """A driver knows no phones (STATE-01): the UI part asks only for the `phone_id` of a phone it enrolled, and
    that is PROV-07's gate (the UI part's `test_door_through_the_ui_part.py` proves it end to end)."""
    await _setup_door(hass, sip_user="9999#0", sip_password="x", rtsp_username="u", rtsp_password="p")
    provider = _provider(hass)
    assert provider.secrets_for(GATE_SIP_URL, "enroll-1") == {
        "host": GATE_SIP_URL, "sip_user": "9999#0", "sip_password": "x", "rtsp_username": "u", "rtsp_password": "p"}
    assert provider.secrets_for(GATE_SIP_URL, "") is None            # no phone named
    assert provider.secrets_for("no-such", "enroll-1") is None       # no such door


def _make_harness(hass):
    """The levers the conformance suite pulls, on TransportProvider with mocked-transport entries
    (a real TransportClient is not needed: the provider only awaits the entry's client)."""
    from .door_provider_conformance import ProviderHarness

    provider = install_provider(hass)
    entries: dict[str, tuple] = {}

    def add_door(door_id):
        if door_id in entries:
            provider.attach(entries[door_id][0])        # the same entry set up again
            return
        entry, runtime = _add_device(hass, device_id=door_id)
        runtime.client.answer = AsyncMock(return_value={"ok": True})
        entries[door_id] = (entry, runtime)

    def remove_door(door_id):
        provider.detach(entries[door_id][0])

    def unload():
        for entry, _runtime in entries.values():
            provider.detach(entry)

    def ring(door_id, call_id):
        entries[door_id][1].call_state.apply_event({"call_id": call_id, "state": "ringing"})

    return ProviderHarness(provider, add_door, remove_door, unload, ring)


@pytest.mark.parametrize("check", CHECKS, ids=lambda c: c.__name__)
async def test_transport_provider_passes_the_conformance_suite(hass, check):
    """TEST-06: every check of the suite, each on a fresh harness (a fresh hass: entries and ids start clean)."""
    await check(_make_harness(hass))


async def test_dial_hangup_and_keepalive_go_to_the_owning_doors_client(hass):
    _, first = _add_device(hass, device_id="10.3.0.20")
    _, second = _add_device(hass, device_id="10.3.0.21")
    for runtime in (first, second):
        runtime.client.dial = AsyncMock(return_value={"call_id": f"dial-{runtime.device_id}"})
        runtime.client.hangup = AsyncMock(return_value={"ok": True})
        runtime.client.keepalive = AsyncMock(return_value={"ok": True})
    provider = _provider(hass)

    dialled = await provider.dial("10.3.0.21", "10.9.0.1", 7)
    second.client.dial.assert_awaited_once_with("10.3.0.21", "10.9.0.1", 7)
    first.client.dial.assert_not_awaited()
    assert dialled == {"call_id": "dial-10.3.0.21"}
    # the dialled call has no ring: its id came from dial()'s result
    await provider.keepalive("dial-10.3.0.21")
    second.client.keepalive.assert_awaited_once_with("dial-10.3.0.21")
    await provider.hangup("dial-10.3.0.21", "bye")
    second.client.hangup.assert_awaited_once_with("dial-10.3.0.21", "bye")
    first.client.hangup.assert_not_awaited()
    with pytest.raises(Exception) as err:
        await provider.hangup("never-seen", "x")
    assert err.value.code == "unknown_call"


async def test_a_call_already_ringing_at_attach_is_routable(hass):
    """R3-02: a ring that reached the tracker before the provider attached the door produced no transition the
    provider heard; hangup, keepalive and webrtc_* for that call still reach the door's client (v1 forwarded
    them), and a call of no door is still `unknown_call`."""
    entry, runtime = _add_device(hass, device_id="10.3.0.20", attach=False)
    other_entry, other = _add_device(hass, device_id="10.3.0.21", attach=False)
    runtime.call_state.apply_event({"call_id": "early", "state": "ringing"})   # before any attach
    provider = install_provider(hass)
    provider.attach(entry)
    provider.attach(other_entry)
    for rt in (runtime, other):
        rt.client.hangup = AsyncMock(return_value={"ok": True})
        rt.client.keepalive = AsyncMock(return_value={"ok": True})
        rt.client.webrtc_offer = AsyncMock(return_value={"sdp": "a"})
        rt.client.webrtc_answer = AsyncMock(return_value={"ok": True})
        rt.client.webrtc_ice = AsyncMock(return_value={"ok": True})
    await provider.keepalive("early")
    await provider.webrtc_offer("early", "sdp")
    await provider.webrtc_answer("early", "sdp")
    await provider.webrtc_ice("early", "cand")
    runtime.client.keepalive.assert_awaited_once_with("early")
    runtime.client.webrtc_offer.assert_awaited_once_with("early", "sdp")
    runtime.client.webrtc_answer.assert_awaited_once_with("early", "sdp")
    runtime.client.webrtc_ice.assert_awaited_once_with("early", "cand")
    await provider.hangup("early", "bye")
    runtime.client.hangup.assert_awaited_once_with("early", "bye")
    other.client.hangup.assert_not_awaited()
    other.client.keepalive.assert_not_awaited()
    with pytest.raises(Exception) as err:
        await provider.keepalive("never-seen")
    assert err.value.code == "unknown_call"


async def test_an_idle_door_seeds_no_call_at_attach(hass):
    entry, runtime = _add_device(hass, device_id="10.3.0.20", attach=False)
    runtime.call_state.apply_event({"call_id": "old", "state": "ringing"})
    runtime.call_state.apply_event({"call_id": "old", "state": "ended"})   # idle again
    provider = install_provider(hass)
    provider.attach(entry)
    assert len(provider._call_doors) == 0
    with pytest.raises(Exception) as err:
        await provider.hangup("old", "x")
    assert err.value.code == "unknown_call"


async def test_seeding_at_attach_stays_bounded(hass):
    from custom_components.portico.door import provider_impl

    provider = install_provider(hass)
    for i in range(provider_impl._MAX_REMEMBERED_CALLS + 5):
        entry, runtime = _add_device(hass, device_id=f"10.6.0.{i}", attach=False)
        runtime.call_state.apply_event({"call_id": f"c{i}", "state": "ringing"})
        provider.attach(entry)
    assert len(provider._call_doors) == provider_impl._MAX_REMEMBERED_CALLS


async def test_answer_verdicts_follow_the_arbitration_the_wire_has_today(hass):
    _, runtime = _add_device(hass, device_id="10.3.0.20", capabilities={"DirectCapable": True})
    runtime.client.answer = AsyncMock(return_value={"ok": True})
    provider = _provider(hass)
    assert (await provider.answer("nope", "c1", client_kind="app", claimant="u:1"))["verdict"] == "not_available"
    assert (await provider.answer("10.3.0.20", "c1", client_kind="app", claimant="u:1"))["verdict"] == "not_available"
    runtime.call_state.apply_event({"call_id": "c1", "state": "ringing"})
    won = await provider.answer("10.3.0.20", "c1", client_kind="app", claimant="u:1")
    assert (won["verdict"], won["mode"]) == ("answered", "direct")      # an app against a direct-capable driver
    runtime.client.answer.assert_not_awaited()
    assert (await provider.answer("10.3.0.20", "c1", client_kind="app", claimant="u:2"))["verdict"] == "taken_elsewhere"

    runtime.call_state.apply_event({"call_id": "c2", "state": "ringing"})
    relayed = await provider.answer("10.3.0.20", "c2", client_kind="browser", claimant="u:1", host="h", port=4)
    assert (relayed["verdict"], relayed["mode"], relayed["detail"]) == ("answered", "relay", {"ok": True})
    runtime.client.answer.assert_awaited_once_with("10.3.0.20", "h", 4)


@pytest.mark.parametrize("code, verdict", [("already_answered", "taken_elsewhere"), ("claims_disabled", "refused")])
async def test_a_transport_refusal_becomes_a_verdict(hass, code, verdict):
    from custom_components.portico.door.transport import TransportError

    _, runtime = _add_device(hass, device_id="10.3.0.20", capabilities={"DirectCapable": False})
    runtime.client.answer = AsyncMock(side_effect=TransportError(code, "no"))
    runtime.call_state.apply_event({"call_id": "c1", "state": "ringing"})
    result = await _provider(hass).answer("10.3.0.20", "c1", client_kind="app", claimant="u:1")
    assert result["verdict"] == verdict and runtime.call_state.state == "ringing"


async def test_two_concurrent_relay_answers_grant_only_one(hass):
    """HA-M1: the check and the transport await were not atomic; both claimants saw `ringing` and both won."""
    _, runtime = _add_device(hass, device_id="10.3.0.20", capabilities={"DirectCapable": False})
    gate = asyncio.Event()

    async def slow_answer(*_args):
        await gate.wait()
        return {"ok": True}

    runtime.client.answer = AsyncMock(side_effect=slow_answer)
    runtime.call_state.apply_event({"call_id": "c1", "state": "ringing"})
    provider = _provider(hass)
    first = asyncio.ensure_future(provider.answer("10.3.0.20", "c1", client_kind="browser", claimant="u:1"))
    second = asyncio.ensure_future(provider.answer("10.3.0.20", "c1", client_kind="browser", claimant="u:2"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    gate.set()
    verdicts = sorted(r["verdict"] for r in await asyncio.gather(first, second))
    assert verdicts == ["answered", "taken_elsewhere"]
    runtime.client.answer.assert_awaited_once()


# --------------------------------------------------------------------------
# PH-3b: one arbitration, the bus event, away media through the provider
# --------------------------------------------------------------------------
# The first block was test_gate_sip_claims.py's answer tests: the answer path of a relay door. The ring-answered helper is
# written for a gate_sip_server call only, on the transition (test_gate_sip_server_w5.py), never by `answer()`.

HELPER = "input_boolean.gate_ring_answered"


async def _claims_door(hass, *, answer=None):
    """A relay door (dahua_sip) with a mocked client and a ringing call c1, attached to the provider."""
    data = {ENTRY_TYPE: ENTRY_TYPE_DOOR, "driver": "dahua_sip", "host": "gate"}
    entry = MockConfigEntry(domain=DOMAIN, data=data, unique_id="dahua_sip:gate")
    entry.add_to_hass(hass)
    runtime = DoorRuntime(hass, entry, StubHub())
    runtime.capabilities = {"DirectCapable": False, "HasRelay": False, "OutboundAudio": 0, "InboundAudio": 0}
    runtime._hub.client = AsyncMock()
    runtime.client.answer = answer or AsyncMock(return_value={"ok": True})
    runtime.call_state = CallStateTracker("gate")
    runtime.call_state.apply_event({"call_id": "c1", "state": "ringing"})
    entry.runtime_data = runtime
    install_provider(hass).attach(entry)
    return entry, runtime


async def _answer(hass):
    return await _provider(hass).answer("gate", "c1", client_kind="browser", claimant="u:1")


@pytest.mark.parametrize("code", ["already_answered", "claims_disabled", "answer_failed", "not_supported"])
async def test_a_refused_or_failed_answer_writes_nothing(hass, code):
    from pytest_homeassistant_custom_component.common import async_mock_service

    from custom_components.portico.door.transport import TransportError

    calls = async_mock_service(hass, "input_boolean", "turn_on")
    await _claims_door(hass, answer=AsyncMock(side_effect=TransportError(code, "no")))
    provider = _provider(hass)
    if code in ("already_answered", "claims_disabled"):
        result = await _answer(hass)
        assert result["verdict"] == {"already_answered": "taken_elsewhere", "claims_disabled": "refused"}[code]
    else:
        with pytest.raises(TransportError) as err:
            await _answer(hass)
        assert err.value.code == code
    await hass.async_block_till_done()
    assert calls == []
    assert provider.call_state("gate")["state"] == "ringing"


async def test_a_granted_relay_answer_writes_no_helper_for_a_door_that_is_not_the_sip_server(hass):
    from pytest_homeassistant_custom_component.common import async_mock_service

    calls = async_mock_service(hass, "input_boolean", "turn_on")
    entry, _ = await _claims_door(hass)
    hass.config_entries.async_update_entry(entry, data={**entry.data, "gate_sip_listen": {"ring_answered_entity": HELPER}})
    assert (await _answer(hass))["verdict"] == "answered"
    await hass.async_block_till_done()
    assert calls == []


async def test_answer_is_otherwise_what_it_was(hass):
    entry, runtime = await _claims_door(hass)
    result = await _answer(hass)
    assert result == {"verdict": "answered", "mode": "relay", "detail": {"ok": True}}
    runtime.client.answer.assert_awaited_once_with("gate", "", 0)
    assert runtime.call_state.state == "answered"


async def test_a_transition_fires_one_bus_event_with_device_state_call_and_prev(hass):
    """PROV-04 / STRUCT-08: the driver's own fan-out fires portico_intercom_call, with no registry and no UI
    part in play (a bare TransportProvider attached to a door), once per transition."""
    from pytest_homeassistant_custom_component.common import async_capture_events

    from custom_components.portico.door.provider_impl import EVENT_INTERCOM_CALL

    assert EVENT_INTERCOM_CALL == "portico_intercom_call"
    events = async_capture_events(hass, "portico_intercom_call")
    entry = MockConfigEntry(domain=DOMAIN, data={ENTRY_TYPE: ENTRY_TYPE_DOOR, "driver": "dahua_sip", "host": "10.3.0.20"},
                            unique_id="dahua_sip:10.3.0.20")
    entry.add_to_hass(hass)
    runtime = DoorRuntime(hass, entry, StubHub())
    runtime._hub.client = AsyncMock()
    runtime.call_state = CallStateTracker("10.3.0.20")
    entry.runtime_data = runtime
    provider = TransportProvider(hass)          # the UI part (and its registry) is absent
    provider.attach(entry)

    runtime.call_state.apply_event({"call_id": "c1", "state": "ringing"})
    await hass.async_block_till_done()
    assert len(events) == 1
    data = dict(events[0].data)
    at_wall = data.pop("at_wall")
    assert isinstance(at_wall, float)
    assert data == {"device_id": "10.3.0.20", "state": "ringing", "call_id": "c1",
                    "prev_state": "idle", "prev_call_id": None}

    runtime.call_state.apply_event({"call_id": "c1", "state": "ended"})
    await hass.async_block_till_done()
    assert len(events) == 2
    ended = dict(events[1].data)
    assert (ended["state"], ended["call_id"], ended["prev_state"], ended["prev_call_id"]) == (
        "ended", "c1", "ringing", "c1")
    # a detached door fires nothing more
    provider.detach(entry)
    runtime.call_state.apply_event({"call_id": "c2", "state": "ringing"})
    await hass.async_block_till_done()
    assert len(events) == 2


async def _ringing_door(hass):
    entry, runtime = _add_device(hass, device_id="10.3.0.20")
    runtime.client.media_open = AsyncMock(return_value={"codec": "pcmu", "frame_bytes": 320, "frame_ms": 40})
    runtime.client.media_close = AsyncMock(return_value={"ok": True})
    runtime.client.media_frame_nowait = MagicMock()
    runtime.call_state.apply_event({"call_id": "c1", "state": "ringing"})
    return entry, runtime


async def test_media_open_registers_the_callbacks_and_asks_the_doors_client(hass):
    _, runtime = await _ringing_door(hass)
    frames, closed = [], []
    provider = _provider(hass)
    result = await provider.media_open("c1", "pcmu", frames.append, closed.append)
    assert result == {"codec": "pcmu", "frame_bytes": 320, "frame_ms": 40}
    runtime.client.media_open.assert_awaited_once_with("c1", "pcmu")

    provider.on_media_notification("media_frame", {"call_id": "c1", "d": "QUJD"})
    provider.on_media_notification("media_frame", {"call_id": "other", "d": "QUJD"})      # no leg: ignored
    provider.on_media_notification("media_frame", {"call_id": "c1", "d": 7})              # not a string: ignored
    assert frames == ["QUJD"]

    provider.media_frame("c1", "F0AA")
    runtime.client.media_frame_nowait.assert_called_once_with("c1", "F0AA")

    provider.on_media_notification("media_closed", {"call_id": "c1", "reason": "idle"})
    provider.on_media_notification("media_closed", {"call_id": "c1", "reason": "again"})   # once only
    assert closed == ["idle"]
    provider.on_media_notification("media_frame", {"call_id": "c1", "d": "late"})
    assert frames == ["QUJD"]
    runtime.client.media_close.assert_not_awaited()          # the transport told us; no echo back


async def test_media_close_drops_the_leg_and_tells_the_client(hass):
    _, runtime = await _ringing_door(hass)
    frames = []
    provider = _provider(hass)
    await provider.media_open("c1", "pcmu", frames.append, lambda reason: None)
    assert await provider.media_close("c1", "closed_by_phone") == {"ok": True}
    runtime.client.media_close.assert_awaited_once_with("c1", "closed_by_phone")
    provider.on_media_notification("media_frame", {"call_id": "c1", "d": "QUJD"})
    assert frames == []


async def test_a_failed_media_open_leaves_no_leg_and_an_unknown_call_has_a_code(hass):
    from custom_components.portico.door.transport import TransportError

    _, runtime = await _ringing_door(hass)
    runtime.client.media_open = AsyncMock(side_effect=TransportError("bad_params", "nope"))
    frames = []
    provider = _provider(hass)
    with pytest.raises(TransportError) as err:
        await provider.media_open("c1", "pcmu", frames.append, lambda reason: None)
    assert err.value.code == "bad_params"
    provider.on_media_notification("media_frame", {"call_id": "c1", "d": "QUJD"})
    assert frames == []
    with pytest.raises(Exception) as unknown:
        await provider.media_open("never-seen", "pcmu", frames.append, lambda reason: None)
    assert unknown.value.code == "unknown_call"
    provider.media_frame("never-seen", "AAAA")       # fire and forget: never raises


# ---- open_relay: the installer's HA action for a driver with no relay of its own -------------------------------
# SAFETY: the action opens a real gate. Every test here records a fake `button.press`; none runs a real action.

GATE_BUTTON = "button.example_intercom_ex_gate_open_door"
GATE_ACTION = [{"action": "button.press", "target": {"entity_id": GATE_BUTTON}}]


def _with_action(hass, entry, action=GATE_ACTION):
    hass.config_entries.async_update_entry(entry, data={**entry.data, "open_relay_action": action})


async def test_open_relay_runs_the_configured_ha_action_when_the_driver_has_no_relay(hass):
    from pytest_homeassistant_custom_component.common import async_mock_service
    entry, runtime = _add_device(hass, capabilities={"DirectCapable": False, "HasRelay": False})
    _with_action(hass, entry)
    presses = async_mock_service(hass, "button", "press")

    assert await _provider(hass).open_relay("127.0.0.1") == {"ok": True}
    await hass.async_block_till_done()

    assert len(presses) == 1 and presses[0].data == {"entity_id": [GATE_BUTTON]}
    runtime.client.open_relay.assert_not_awaited()


async def test_open_relay_without_action_and_without_relay_keeps_the_error(hass):
    from custom_components.portico.door.transport import TransportError
    entry, runtime = _add_device(hass, capabilities={"HasRelay": False})
    runtime.client.open_relay = AsyncMock(side_effect=TransportError("not_supported", "no relay"))
    with pytest.raises(TransportError) as err:
        await _provider(hass).open_relay("127.0.0.1")
    assert err.value.code == "not_supported"
    runtime.client.open_relay.assert_awaited_once_with("127.0.0.1")


async def test_open_relay_with_a_driver_relay_still_uses_the_driver(hass):
    from pytest_homeassistant_custom_component.common import async_mock_service
    entry, runtime = _add_device(hass, capabilities={"HasRelay": True})
    _with_action(hass, entry)     # even with an action configured, a driver relay is the driver's to open
    presses = async_mock_service(hass, "button", "press")
    runtime.client.open_relay = AsyncMock(return_value={"ok": True})

    assert await _provider(hass).open_relay("127.0.0.1") == {"ok": True}
    await hass.async_block_till_done()

    runtime.client.open_relay.assert_awaited_once_with("127.0.0.1")
    assert presses == []


async def test_a_failing_action_maps_to_the_existing_error(hass):
    from custom_components.portico.door.transport import TransportError
    entry, runtime = _add_device(hass, capabilities={"HasRelay": False})
    _with_action(hass, entry)

    async def boom(call):
        raise RuntimeError("shelly unreachable")

    hass.services.async_register("button", "press", boom)
    with pytest.raises(TransportError) as err:
        await _provider(hass).open_relay("127.0.0.1")
    assert err.value.code == "internal" and "RuntimeError" in err.value.message
    runtime.client.open_relay.assert_not_awaited()


async def test_the_relay_lock_runs_the_action_through_the_shared_helper(hass):
    from pytest_homeassistant_custom_component.common import async_mock_service
    from custom_components.portico.door.entities.lock import PorticoRelayLock
    entry, _runtime = _add_device(hass, capabilities={"HasRelay": True})
    _with_action(hass, entry)
    presses = async_mock_service(hass, "button", "press")

    await PorticoRelayLock(hass, entry).async_open()
    await hass.async_block_till_done()

    assert len(presses) == 1 and presses[0].data == {"entity_id": [GATE_BUTTON]}


# ---- the outbound call in the snapshot (DEV-658: Talk's `busy` sees a dial placed by another app) ------------------------

OUT = "out-1"
_VISITOR_ONLY = {"door_id": "127.0.0.1", "call_id": None, "state": "idle"}


def _snapshot(hass, device_id="127.0.0.1"):
    return _provider(hass).call_state(device_id)


async def test_a_door_with_no_outbound_call_has_exactly_the_visitor_snapshot(hass):
    _, runtime = _add_device(hass)
    assert _snapshot(hass) == _VISITOR_ONLY and list(_snapshot(hass)) == ["door_id", "call_id", "state"]
    runtime.call_state.apply_event({"call_id": "v1", "state": "ringing"})
    assert _snapshot(hass) == {"door_id": "127.0.0.1", "call_id": "v1", "state": "ringing"}      # a visitor ring: same three keys


async def test_the_snapshot_carries_the_outbound_call_while_it_is_going_and_not_after(hass):
    _, runtime = _add_device(hass)
    tracker = runtime.call_state
    tracker.apply_event({"call_id": OUT, "state": "ringing", "outbound": True})
    assert _snapshot(hass) == {**_VISITOR_ONLY, "outbound_state": "dialing", "outbound_call_id": OUT}
    tracker.apply_event({"call_id": OUT, "state": "answered", "outbound": True})
    assert _snapshot(hass) == {**_VISITOR_ONLY, "outbound_state": "in_call", "outbound_call_id": OUT}
    tracker.apply_event({"call_id": OUT, "state": "ended", "outbound": True})
    assert _snapshot(hass) == _VISITOR_ONLY and list(_snapshot(hass)) == ["door_id", "call_id", "state"]


async def test_an_outbound_call_leaves_the_visitor_part_of_the_snapshot_alone(hass):
    _, runtime = _add_device(hass)
    tracker = runtime.call_state
    tracker.apply_event({"call_id": "v1", "state": "ringing"})
    tracker.apply_event({"call_id": OUT, "state": "ringing", "outbound": True})
    assert _snapshot(hass) == {"door_id": "127.0.0.1", "call_id": "v1", "state": "ringing",
                               "outbound_state": "dialing", "outbound_call_id": OUT}
    tracker.apply_event({"call_id": "v1", "state": "ended"})
    assert _snapshot(hass) == {**_VISITOR_ONLY, "outbound_state": "dialing", "outbound_call_id": OUT}   # ours survives theirs


async def test_a_door_that_can_dial_always_states_the_outbound_call_idle_included(hass):
    _, runtime = _add_device(hass, capabilities={"CanDial": True, "DirectCapable": True})
    tracker = runtime.call_state
    assert _snapshot(hass) == {**_VISITOR_ONLY, "outbound_state": "idle", "outbound_call_id": None}
    tracker.apply_event({"call_id": OUT, "state": "answered", "outbound": True})
    assert _snapshot(hass)["outbound_state"] == "in_call"
    tracker.apply_event({"call_id": OUT, "state": "ended", "outbound": True})
    assert _snapshot(hass) == {**_VISITOR_ONLY, "outbound_state": "idle", "outbound_call_id": None}


async def test_no_shipped_door_can_dial_so_no_shipped_snapshot_gains_a_key(hass, fake_transport):
    await _setup_door(hass)                                       # the real gate-sip observer entry
    assert _snapshot(hass, GATE_SIP_URL) == {"door_id": GATE_SIP_URL, "call_id": None, "state": "idle"}
    assert _provider(hass)._entry_for_door(GATE_SIP_URL).runtime_data.can_dial is False
