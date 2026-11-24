# ruff: noqa: F811 - test parameters named after the imported fixtures
"""The ring-only front door: an `ha_event` door rings from a Home Assistant event entity (portico_front_door_design.md,
option B). Python only: no Go driver, no transport process for this door, no talkback and no way to open anything.

Real config entries, the real hub (transport faked, as test_observer), the real provider; the event entity is
`hass.states`. Every "does not ring" case is a way the doorbell's event entity changes without a press.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import CoreState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.portico.door import hub as portico_hub
from custom_components.portico.door.const import DOMAIN, ENTRY_TYPE, ENTRY_TYPE_DOOR
from custom_components.portico.door.drivers.ha_event import CAPABILITIES, DEFAULT_HOLD, normalize
from custom_components.portico.door.ha_event_ring import FRESH_SECONDS, MIN_GAP_SECONDS
from custom_components.portico.door.provider_impl import get_provider
from custom_components.portico.door.transport import TransportError

from .test_observer import (  # noqa: F401 - fixtures by name
    GATE_SIP_URL,
    _custom_integrations,
    _observer_entry,
    fake_transport,
    wait_for,
)

BELL = "event.doorbell"


def _now_iso(offset: float = 0.0) -> str:
    return (dt_util.utcnow() + timedelta(seconds=offset)).isoformat()


def _press(hass, event_id: str, *, event_type: str = "ring", stamp: str | None = None, entity: str = BELL) -> None:
    hass.states.async_set(entity, stamp or _now_iso(), {"event_type": event_type, "event_id": event_id,
                                                        "event_types": ["ring"]})


# The talk options have no default (DEV-836): a talkback entry states them. Example LAN addresses.
TALK_ESTATE = {"protect_host": "192.168.2.1", "camera_id": "cam-test-1", "downlink_url": "rtsp://192.168.2.2:8554/doorbell"}


def _front_entry(hass, **extra) -> MockConfigEntry:
    if extra.get("talkback") is True:
        extra = {**TALK_ESTATE, **extra}
    data = {
        ENTRY_TYPE: ENTRY_TYPE_DOOR, "driver": "ha_event", "name": "front", "event_entity": BELL,
        "event_type": "ring", "ring_hold": DEFAULT_HOLD, "device_id": f"ha_event:{BELL}",
        "existing_camera_entity": "camera.doorbell", "open_relay_action": [], **extra,
    }
    entry = MockConfigEntry(domain=DOMAIN, title="front", data=data, unique_id=f"ha_event:{BELL}")
    entry.add_to_hass(hass)
    return entry


async def _setup(hass, entry) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


class _Rings:
    """What the push sender and the phones' stream hear: the provider's transitions."""

    def __init__(self, hass) -> None:
        self.events: list[dict] = []
        get_provider(hass).subscribe(self.events.append)

    @property
    def ringing(self) -> list[dict]:
        return [e for e in self.events if e.get("state") == "ringing"]


@pytest.fixture
async def front(hass, fake_transport):
    """A started front door with the doorbell entity already holding an OLD event (a restart's restored state)."""
    _press(hass, "old-1", stamp=_now_iso(-3600))
    entry = _front_entry(hass)
    await _setup(hass, entry)
    rings = _Rings(hass)
    return entry, entry.runtime_data, rings


# ---- a real press ----------------------------------------------------------------------------------------

async def test_a_real_press_rings_once_with_the_ha_event_source(hass, front):
    entry, door, rings = front
    assert door.call_state.state == "idle"
    _press(hass, "e-1")
    await hass.async_block_till_done()
    assert door.call_state.state == "ringing" and door.call_state.call_id == "ha-event-e-1"
    assert len(rings.ringing) == 1
    ring = rings.ringing[0]
    assert ring["door_id"] == door.device_id and ring["call_id"] == "ha-event-e-1"


async def test_the_ring_reaches_the_call_state_sensor_and_the_endpoint_camera(hass, front):
    entry, door, rings = front
    _press(hass, "e-1")
    await hass.async_block_till_done()
    reg = er.async_get(hass)
    sensor_id = reg.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_call_state")
    assert sensor_id == "sensor.front_call_state" and hass.states.get(sensor_id).state == "ringing"
    (door_row,) = [d for d in get_provider(hass).doors() if d["id"] == door.device_id]
    assert door_row["camera_entity_id"] == "camera.doorbell" and door_row["name"] == "front"


async def test_the_ring_ends_after_the_hold_if_nobody_dismisses_it(hass, front):
    entry, door, rings = front
    _press(hass, "e-1")
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=DEFAULT_HOLD + 1))
    await hass.async_block_till_done()
    assert door.call_state.state == "idle"
    assert [e["state"] for e in rings.events] == ["ringing", "ended"]


async def test_a_phone_can_dismiss_the_ring(hass, front):
    entry, door, rings = front
    _press(hass, "e-1")
    await hass.async_block_till_done()
    result = await get_provider(hass).hangup("ha-event-e-1", "dismiss")
    assert result == {"ok": True} and door.call_state.state == "idle"


async def test_two_presses_ring_twice(hass, front):
    entry, door, rings = front
    clock = [100.0]
    door._ring_source._clock = lambda: clock[0]
    _press(hass, "e-1")
    await hass.async_block_till_done()
    clock[0] += MIN_GAP_SECONDS + 1
    _press(hass, "e-2")
    await hass.async_block_till_done()
    assert [e["call_id"] for e in rings.ringing] == ["ha-event-e-1", "ha-event-e-2"]
    assert door.call_state.call_id == "ha-event-e-2"


# ---- changes that are not a press ------------------------------------------------------------------------

async def test_the_event_held_at_startup_does_not_ring(hass, fake_transport):
    """The entity already holds a FRESH event when the door starts (a reload seconds after a press): it is history."""
    _press(hass, "held-1")
    entry = _front_entry(hass)
    await _setup(hass, entry)
    rings = _Rings(hass)
    assert entry.runtime_data.call_state.state == "idle" and not rings.events
    _press(hass, "held-1")           # the same event re-published (an attribute write): still not a press
    await hass.async_block_till_done()
    assert not rings.events


async def test_a_restore_after_unavailable_does_not_ring(hass, front):
    """unavailable -> value with an OLD timestamp and an id never seen: HA's restored state, not a press."""
    entry, door, rings = front
    hass.states.async_set(BELL, "unavailable")
    await hass.async_block_till_done()
    _press(hass, "restored-9", stamp=_now_iso(-FRESH_SECONDS - 60))
    await hass.async_block_till_done()
    assert not rings.ringing and door.call_state.state == "idle"


async def test_the_first_press_after_a_restart_rings(hass, front):
    """unknown -> a fresh event (the entity reads `unknown` right after a restart): a real press."""
    entry, door, rings = front
    hass.states.async_set(BELL, "unknown")
    await hass.async_block_till_done()
    _press(hass, "after-restart")
    await hass.async_block_till_done()
    assert len(rings.ringing) == 1


async def test_a_duplicate_event_id_rings_once(hass, front):
    entry, door, rings = front
    _press(hass, "e-1")
    await hass.async_block_till_done()
    _press(hass, "e-1", stamp=_now_iso(1))       # same id, state moved on (a re-publish)
    await hass.async_block_till_done()
    assert len(rings.ringing) == 1


async def test_a_bounce_inside_the_rate_limit_is_one_ring(hass, front):
    entry, door, rings = front
    door._ring_source._clock = lambda: 500.0
    _press(hass, "e-1")
    await hass.async_block_till_done()
    _press(hass, "e-2")
    await hass.async_block_till_done()
    assert len(rings.ringing) == 1 and door.call_state.call_id == "ha-event-e-1"


async def test_another_event_type_does_not_ring(hass, front):
    entry, door, rings = front
    _press(hass, "m-1", event_type="motion")
    await hass.async_block_till_done()
    assert not rings.ringing


async def test_a_stale_event_does_not_ring_even_with_a_new_id(hass, front):
    entry, door, rings = front
    _press(hass, "late-1", stamp=_now_iso(-FRESH_SECONDS - 5))
    await hass.async_block_till_done()
    assert not rings.ringing


async def test_nothing_rings_while_home_assistant_is_starting(hass, front):
    entry, door, rings = front
    hass.set_state(CoreState.starting)
    _press(hass, "boot-1")
    await hass.async_block_till_done()
    hass.set_state(CoreState.running)
    assert not rings.ringing


async def test_an_unparseable_state_does_not_ring(hass, front):
    entry, door, rings = front
    _press(hass, "x-1", stamp="not a time")
    await hass.async_block_till_done()
    assert not rings.ringing


# ---- availability ----------------------------------------------------------------------------------------

async def test_the_door_follows_its_event_entity_and_a_ring_ends_when_it_goes(hass, front):
    entry, door, rings = front
    assert door.entities_available is True
    _press(hass, "e-1")
    await hass.async_block_till_done()
    hass.states.async_set(BELL, "unavailable")
    await hass.async_block_till_done()
    assert door.entities_available is False and door.call_state.state == "idle"
    _press(hass, "e-2", stamp=_now_iso(-3600))
    await hass.async_block_till_done()
    assert door.entities_available is True


async def test_a_missing_event_entity_is_unavailable_not_idle(hass, fake_transport):
    entry = _front_entry(hass)
    await _setup(hass, entry)
    assert entry.runtime_data.entities_available is False


# ---- ring-only: nothing to talk through, nothing to open -------------------------------------------------

async def test_the_door_states_no_audio_no_relay_and_no_lock_entity(hass, front):
    entry, door, rings = front
    assert door.capabilities == CAPABILITIES
    assert door.capabilities["HasRelay"] is False and door.capabilities["DirectCapable"] is False
    assert door.capabilities["InboundAudio"] == 3 and door.capabilities["OutboundAudio"] == 3
    assert er.async_get(hass).async_get_entity_id("lock", DOMAIN, f"{entry.entry_id}_relay") is None
    assert not [e for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id) if e.domain == "lock"]
    (door_row,) = [d for d in get_provider(hass).doors() if d["id"] == door.device_id]
    assert door_row["away_media"] is False and door_row["capabilities"]["HasRelay"] is False


async def test_answering_is_refused_and_the_ring_stays(hass, front):
    entry, door, rings = front
    _press(hass, "e-1")
    await hass.async_block_till_done()
    verdict = await get_provider(hass).answer(door.device_id, "ha-event-e-1", client_kind="app", claimant="u1")
    assert verdict["verdict"] == "refused"
    assert door.call_state.state == "ringing"


async def test_open_relay_is_refused_even_if_an_action_is_stored_on_the_entry(hass, fake_transport):
    """No door-open capability by construction: a stored open_relay_action is never run."""
    calls = []
    hass.services.async_register("test", "open_it", lambda call: calls.append(call))
    _press(hass, "old-1", stamp=_now_iso(-3600))
    entry = _front_entry(hass, open_relay_action=[{"action": "test.open_it"}])
    await _setup(hass, entry)
    with pytest.raises(TransportError) as err:
        await get_provider(hass).open_relay(entry.runtime_data.device_id)
    assert err.value.code == "not_supported" and calls == []


@pytest.mark.parametrize("method,args", [("webrtc_offer", ("sdp",)), ("webrtc_answer", ("sdp",)),
                                         ("webrtc_ice", ("cand",))])
async def test_no_media_path_is_offered(hass, front, method, args):
    entry, door, rings = front
    _press(hass, "e-1")
    await hass.async_block_till_done()
    with pytest.raises(TransportError) as err:
        await getattr(get_provider(hass), method)("ha-event-e-1", *args)
    assert err.value.code == "not_supported"


async def test_media_open_is_refused(hass, front):
    entry, door, rings = front
    _press(hass, "e-1")
    await hass.async_block_till_done()
    with pytest.raises(TransportError) as err:
        await get_provider(hass).media_open("ha-event-e-1", "pcm", lambda d: None, lambda r: None)
    assert err.value.code == "not_supported"


def test_the_driver_module_has_no_open_or_talk_path():
    import inspect

    from custom_components.portico.door import ha_event_ring
    from custom_components.portico.door.drivers import ha_event

    source = inspect.getsource(ha_event_ring) + inspect.getsource(ha_event)
    for forbidden in ("async_call(", "services.", "run_relay_action", "has_relay_action", "open_relay("):
        assert forbidden not in source, forbidden


# ---- the gate is unaffected ------------------------------------------------------------------------------

async def test_the_front_door_never_touches_the_hub_and_the_gate_is_unaffected(hass, fake_transport):
    gate = _observer_entry(hass)
    await _setup(hass, gate)
    _press(hass, "old-1", stamp=_now_iso(-3600))
    front_entry = _front_entry(hass)
    await _setup(hass, front_entry)
    hub = portico_hub.get_hub(hass)
    (transport,) = fake_transport.instances
    assert [driver for driver, _ in transport.configured] == ["gate_sip_observer"]
    assert [d.device_id for d in hub.doors] == [GATE_SIP_URL]
    gate_door = gate.runtime_data
    _press(hass, "e-1")
    await hass.async_block_till_done()
    assert front_entry.runtime_data.call_state.state == "ringing"
    assert gate_door.call_state.state == "idle"
    # a Go ring for the gate still reaches the gate only
    transport.ring("gate-call-1", GATE_SIP_URL)
    await hass.async_block_till_done()
    assert gate_door.call_state.call_id == "gate-call-1"
    assert front_entry.runtime_data.call_state.call_id == "ha-event-e-1"
    # the front door leaving does not stop the process or detach the gate
    front_id = front_entry.runtime_data.device_id
    assert await hass.config_entries.async_unload(front_entry.entry_id)
    assert [d.device_id for d in hub.doors] == [GATE_SIP_URL] and transport.deconfigured == []
    assert get_provider(hass).call_state(front_id) is None


async def test_a_front_door_alone_starts_no_transport_process(hass, front):
    assert portico_hub.get_hub(hass).client is None
    assert not [e for e in hass.config_entries.async_entries(DOMAIN) if e.runtime_data is None]


async def test_unloading_stops_the_listener(hass, front):
    entry, door, rings = front
    assert await hass.config_entries.async_unload(entry.entry_id)
    _press(hass, "after-unload")
    await hass.async_block_till_done()
    assert not rings.ringing


# ---- config flow -----------------------------------------------------------------------------------------

async def _flow_to_driver_config(hass):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "door"})
    return await hass.config_entries.flow.async_configure(result["flow_id"], {"driver": "ha_event"})


async def test_flow_creates_the_front_door_without_a_relay_step(hass):
    result = await _flow_to_driver_config(hass)
    assert result["step_id"] == "driver_config"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"event_entity": " Event.Doorbell "})
    assert result["step_id"] == "camera_config"
    with pytest.raises(vol.Invalid):         # no video of its own: the camera is required
        result["data_schema"]({})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"existing_camera_entity": "camera.doorbell"})
    assert result["type"] is FlowResultType.CREATE_ENTRY and result["title"] == "front"
    data = result["data"]
    assert data["driver"] == "ha_event" and data["event_entity"] == "event.doorbell" and data["event_type"] == "ring"
    assert data["device_id"] == "ha_event:event.doorbell" and data["open_relay_action"] == []
    assert data["existing_camera_entity"] == "camera.doorbell" and data["ring_hold"] == DEFAULT_HOLD
    assert data[ENTRY_TYPE] == ENTRY_TYPE_DOOR


async def test_flow_rejects_a_non_event_entity(hass):
    result = await _flow_to_driver_config(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"event_entity": "sensor.doorbell"})
    assert result["type"] is FlowResultType.FORM and result["errors"] == {"event_entity": "invalid_event_entity"}


async def test_flow_aborts_a_second_door_for_the_same_event_entity(hass):
    _front_entry(hass)
    result = await _flow_to_driver_config(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"event_entity": BELL})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"


def test_normalize_bounds():
    with pytest.raises(vol.Invalid):
        normalize({"event_entity": "light.x"})
    with pytest.raises(vol.Invalid):
        normalize({"event_entity": "event.x", "ring_hold": 1})
    assert normalize({"event_entity": "event.x"})["name"] == "front"


# ---- talkback option (portico_front_door_talk_plan.md, T1): default off, refused while the backend is not built ----

def test_talkback_defaults_off_and_the_door_stays_ring_only():
    from custom_components.portico.door.drivers import ha_event
    from custom_components.portico.signaling import _answerable

    data = normalize({"event_entity": "event.x"})
    assert data["talkback"] is False
    assert ha_event.capabilities_for(data) == CAPABILITIES and _answerable(ha_event.capabilities_for(data)) is False
    # a hand-edited entry without a key cannot make the door claim audio
    assert ha_event.capabilities_for({**data, "talkback": True}) == CAPABILITIES


def test_talkback_on_needs_a_key_and_is_refused_if_the_backend_is_not_ready(monkeypatch):
    from custom_components.portico.door.drivers import ha_event

    assert ha_event.TALKBACK_BACKEND_READY is True       # T6 wired it; the option still defaults off
    with pytest.raises(vol.Invalid) as err:
        normalize({"event_entity": "event.x", "talkback": True})
    assert err.value.error_message == "protect_api_key_required"
    with pytest.raises(vol.Invalid) as err:
        normalize({"event_entity": "event.x", "talkback": True, "protect_api_key": "  "})
    assert err.value.error_message == "protect_api_key_required"
    with pytest.raises(vol.Invalid) as err:         # no estate default: the host, camera id and audio URL are stated
        normalize({"event_entity": "event.x", "talkback": True, "protect_api_key": "k"})
    assert err.value.error_message == "invalid_talkback_option" and err.value.path == ["protect_host"]
    assert normalize({"event_entity": "event.x", "talkback": True, "protect_api_key": "k", **TALK_ESTATE})["talkback"] is True
    monkeypatch.setattr(ha_event, "TALKBACK_BACKEND_READY", False)
    with pytest.raises(vol.Invalid) as err:
        normalize({"event_entity": "event.x", "talkback": True, "protect_api_key": "k", **TALK_ESTATE})
    assert err.value.error_message == "talkback_not_ready"
    assert ha_event.capabilities_for({"talkback": True, "protect_api_key": "k", **TALK_ESTATE}) == CAPABILITIES
    with pytest.raises(vol.Invalid) as err:
        normalize({"event_entity": "event.x", "talkback": "yes"})
    assert err.value.error_message == "invalid_talkback"


def test_talkback_capabilities_when_ready_make_the_door_answerable_without_any_open_path():
    from custom_components.portico.door.drivers import ha_event
    from custom_components.portico.signaling import _answerable

    data = normalize({"event_entity": "event.x", "talkback": True, "protect_api_key": "k", **TALK_ESTATE})
    caps = ha_event.capabilities_for(data)
    assert caps["InboundAudio"] == 1 and caps["OutboundAudio"] == 2
    assert _answerable(caps) is True
    assert caps["HasRelay"] is False and caps["DirectCapable"] is False       # nothing to open or unlock with
    assert ha_event.capabilities_for({**data, "talkback": False}) == CAPABILITIES


async def test_a_default_entry_is_still_ring_only_and_unanswerable(hass, fake_transport):
    entry = _front_entry(hass)
    await _setup(hass, entry)
    assert entry.runtime_data.capabilities == CAPABILITIES
    assert get_provider(hass).doors()[0]["capabilities"] == CAPABILITIES
