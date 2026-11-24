# ruff: noqa: F811 - test parameters named after the imported fixtures
"""gate_sip_server, the call path and the two entries side by side (DEV-518 W5). Offline: a fake Go transport (driver_part.py)
flips `CanDial` by `capabilities_changed` the way the W4 server will; nothing here depends on the Go side, touches the
station, gate-sip or a real Home Assistant.

Pinned: the `dial_enabled` switch and the ring-answered helper of the server entry; `CanDial` read from `capabilities_changed`;
the legacy IPC `answer` never sent for the server (connect-first); the helper written when the call really becomes
answered; the Reconfigure form of the server entry. (The one-gate rule and the observer-beside-the-server tests went with the
observer door, DEV-518 C5.)"""
from __future__ import annotations

from unittest.mock import patch

import pytest
import voluptuous as vol
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from custom_components.portico.config_flow import DOMAIN as UI_DOMAIN
from custom_components.portico.door import gate_sip_listen
from custom_components.portico.door.drivers.gate_sip_server import CONFIG_SCHEMA, normalize
from custom_components.portico.endpoints import get_endpoint_registry
from custom_components.portico.gate_target import dialable_doors
from custom_components.portico.provider import DATA_PROVIDERS, DOMAIN

from .driver_part import (GATE_SIP_URL, SERVER_DEVICE_ID, FakeGoTransport, _dahua_entry, _server_entry,  # noqa: F401
                          fake_transport, get_provider)
from .test_door_through_the_ui_part import _custom_integrations  # noqa: F401 - fixture by name
from .test_content_wire_blocks import _doc4, _entrance, _fake, _garden, _save
from .test_editor_commands import PHONE_ID, world  # noqa: F401 - fixtures by name

pytestmark = pytest.mark.asyncio

OK = {"password": "12345678", "confirm": True}
HELPER = "input_boolean.gate_ring_answered"


# ---- the form switch and the helper, pure ---------------------------------------------------------------------------

async def test_dial_enabled_defaults_off_and_is_stored_only_when_on():
    assert CONFIG_SCHEMA({"password": "12345678"})["dial_enabled"] is False
    off = normalize(OK)
    assert "dial_enabled" not in off and "gate_sip_listen" not in off     # an entry that never uses them is what it was
    assert normalize({**OK, "dial_enabled": False}) == off
    assert normalize({**OK, "dial_enabled": True})["dial_enabled"] is True


async def test_dial_needs_nothing_else_on_the_server():
    """The server's switch needs nothing else (no claims, no listen leg)."""
    out = normalize({**OK, "dial_enabled": True})        # no claims, no listen leg, no helper
    assert out["dial_enabled"] is True


@pytest.mark.parametrize("bad", ["yes", 1, 0, "true", [True]])
async def test_dial_enabled_must_be_a_real_bool(bad):
    with pytest.raises(vol.Invalid) as e:
        normalize({**OK, "dial_enabled": bad})
    assert (str(e.value.path[0]), e.value.error_message) == ("dial_enabled", "invalid_dial")


async def test_the_ring_helper_is_an_input_boolean_stored_under_the_listen_key():
    assert normalize({**OK, "ring_answered_entity": HELPER})["gate_sip_listen"] == {"ring_answered_entity": HELPER}
    assert "gate_sip_listen" not in normalize({**OK, "ring_answered_entity": ""})
    for bad in ("light.kitchen", "input_boolean.", 5):
        with pytest.raises(vol.Invalid) as e:
            normalize({**OK, "ring_answered_entity": bad})
        assert e.value.error_message == "invalid_ring_entity"


async def test_the_configure_payload_carries_dial_enabled_and_never_the_helper(hass):
    entry = _server_entry(hass, dial_enabled=True, gate_sip_listen={"ring_answered_entity": HELPER})
    config = await gate_sip_listen.transport_config(hass, entry, SERVER_DEVICE_ID)
    assert config["dial_enabled"] is True and "gate_sip_listen" not in config and "entry_type" not in config
    assert "listen_enabled" not in config and "claims_enabled" not in config and "peer_token" not in config
    plain = await gate_sip_listen.transport_config(hass, _server_entry(hass, device_id="gate-sip-server:127.0.0.1:15061"),
                                                   "x")
    assert "dial_enabled" not in plain


async def test_the_server_has_no_listen_leg_and_its_own_dial_switch_is_all_it_needs(hass):
    entry = _server_entry(hass, dial_enabled=True)
    assert gate_sip_listen.settings(entry.data) == {"ring_answered_entity": None}
    from custom_components.portico.door.door import DoorRuntime
    from .driver_part import StubHub
    runtime = DoorRuntime(hass, entry, StubHub())
    assert runtime.dial_configured is True                      # the server's own switch
    assert DoorRuntime(hass, _server_entry(hass, device_id="gate-sip-server:127.0.0.1:15062"), StubHub()).dial_configured is False


# ---- the content store: the Talk path over a fake door of the server driver ---------------------------------------------

async def test_a_lone_gate_entry_talks_when_it_reports_candial(world):
    _fake(world).add_door("gate-1", driver="gate_sip_server", capabilities={"CanDial": True}, camera_entity_id="camera.gate")
    assert world.store.talk_state("fake:gate-1", world.store._endpoint_by_id(PHONE_ID)) == {"available": True, "reason": None}
    _fake(world).add_door("gate-1", driver="gate_sip_server", capabilities={}, camera_entity_id="camera.gate")
    assert world.store.talk_state("fake:gate-1", world.store._endpoint_by_id(PHONE_ID)) == {"available": False, "reason": "talk_off"}


async def test_candial_follows_capabilities_changed(hass, fake_transport):
    srv = _server_entry(hass)
    assert await hass.config_entries.async_setup(srv.entry_id)
    await hass.async_block_till_done()
    assert await async_setup_component(hass, UI_DOMAIN, {})
    await hass.async_block_till_done()
    go = FakeGoTransport.instances[-1]
    ep = lambda: next(e for e in get_endpoint_registry(hass).endpoints(kind="door") if e.device_id == SERVER_DEVICE_ID)  # noqa: E731
    assert srv.runtime_data.can_dial is False and ep().can_dial is False
    go.notify("capabilities_changed", {"device_id": SERVER_DEVICE_ID,
                                       "capabilities": {**(srv.runtime_data.capabilities or {}), "CanDial": True}})
    assert srv.runtime_data.can_dial is True and srv.runtime_data.entities_available is True
    assert ep().can_dial is True
    assert [e.device_id for e in dialable_doors(hass)] == [SERVER_DEVICE_ID]
    off = {k: v for k, v in srv.runtime_data.capabilities.items() if k != "CanDial"}
    go.notify("capabilities_changed", {"device_id": SERVER_DEVICE_ID, "capabilities": off})
    assert ep().can_dial is False
    # the provider publishes the outbound state for a server door that is dial-configured or can dial
    assert "outbound_state" not in get_provider(hass).call_state(SERVER_DEVICE_ID)


# ---- answering: connect-first, and the helper --------------------------------------------------------------------------

async def _ringing(hass, fake_transport, **extra):
    srv = _server_entry(hass, **extra)
    assert await hass.config_entries.async_setup(srv.entry_id)
    await hass.async_block_till_done()
    go = FakeGoTransport.instances[-1]
    go.ring("srv-1", device_id=SERVER_DEVICE_ID)
    return srv, go


async def test_the_browser_path_never_sends_the_legacy_answer_for_the_server(hass, fake_transport):
    """The W3 note: the IPC `answer` answers AT ONCE, before the browser has connected. The browser's `portico/answer` reaches
    `provider.answer` (relay mode); for the server it must be a permission only, with the answering left to `webrtc_offer`."""
    srv, go = await _ringing(hass, fake_transport)
    calls = async_mock_service(hass, "input_boolean", "turn_on")
    verdict = await get_provider(hass).answer(SERVER_DEVICE_ID, "srv-1", client_kind="browser", claimant="u:1")
    await hass.async_block_till_done()
    assert (verdict["verdict"], verdict["mode"]) == ("answered", "relay")
    assert go.answers == []                                           # the legacy verb was never sent
    assert srv.runtime_data.call_state.state == "ringing"            # nothing is answered until the audio is up (Go says so)
    assert calls == []                                                # and the helper is not written on a mere permission
    # a ring that is gone is still refused
    gone = await get_provider(hass).answer(SERVER_DEVICE_ID, "srv-nope", client_kind="browser", claimant="u:1")
    assert gone["verdict"] == "not_available" and go.answers == []


async def test_the_helper_is_written_once_when_the_call_really_becomes_answered(hass, fake_transport):
    srv, go = await _ringing(hass, fake_transport, gate_sip_listen={"ring_answered_entity": HELPER})
    calls = async_mock_service(hass, "input_boolean", "turn_on")
    await hass.async_block_till_done()
    assert calls == []                                                # a ring writes nothing
    go.state("srv-1", "answered", device_id=SERVER_DEVICE_ID)         # Go: the browser leg connected and the call was answered
    await hass.async_block_till_done()
    assert [(c.data["entity_id"]) for c in calls] == [HELPER]
    go.state("srv-1", "active", device_id=SERVER_DEVICE_ID)
    go.state("srv-1", "ended", device_id=SERVER_DEVICE_ID)
    await hass.async_block_till_done()
    assert len(calls) == 1


async def test_no_helper_no_write_and_a_call_that_was_never_answered_writes_nothing(hass, fake_transport):
    srv, go = await _ringing(hass, fake_transport)                    # no helper chosen
    calls = async_mock_service(hass, "input_boolean", "turn_on")
    go.state("srv-1", "answered", device_id=SERVER_DEVICE_ID)
    await hass.async_block_till_done()
    assert calls == []
    srv2, go2 = srv, go
    go2.ring("srv-9", device_id=SERVER_DEVICE_ID)
    go2.state("srv-9", "ended", device_id=SERVER_DEVICE_ID)
    await hass.async_block_till_done()
    assert calls == []


async def test_a_dial_placed_by_portico_does_not_write_the_helper(hass, fake_transport):
    srv, go = await _ringing(hass, fake_transport, gate_sip_listen={"ring_answered_entity": HELPER})
    go.state("srv-1", "ended", device_id=SERVER_DEVICE_ID)
    calls = async_mock_service(hass, "input_boolean", "turn_on")
    go.ring("out-0123456789abcdef", device_id=SERVER_DEVICE_ID)
    go.state("out-0123456789abcdef", "answered", device_id=SERVER_DEVICE_ID)
    await hass.async_block_till_done()
    assert calls == []                                                # an outbound call takes the other slot, never a ring


async def _create(hass, **extra):
    from homeassistant import config_entries
    from homeassistant.data_entry_flow import FlowResultType
    r = await hass.config_entries.flow.async_init(UI_DOMAIN, context={"source": config_entries.SOURCE_USER})
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"next_step_id": "sip_server_door"})
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {**OK, **extra})
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"existing_camera_entity": "camera.gate"})
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {})
    assert r["type"] is FlowResultType.CREATE_ENTRY
    return r["result"]


async def test_the_create_form_stores_the_switch_and_the_helper_only_when_given(hass):
    plain = await _create(hass)
    assert "dial_enabled" not in plain.data and "gate_sip_listen" not in plain.data
    await hass.config_entries.async_remove(plain.entry_id)
    on = await _create(hass, dial_enabled=True, ring_answered_entity=HELPER)
    assert on.data["dial_enabled"] is True and on.data["gate_sip_listen"] == {"ring_answered_entity": HELPER}


async def _reconfigure(hass, entry, user_input=None):
    r = await entry.start_reconfigure_flow(hass)
    if user_input is None:
        return r
    return await hass.config_entries.flow.async_configure(r["flow_id"], user_input)


async def test_reconfigure_of_a_server_entry_offers_the_switch_the_helper_and_the_action_and_saves_them(hass, fake_transport):
    from homeassistant.data_entry_flow import FlowResultType
    entry = _server_entry(hass)
    form = await _reconfigure(hass, entry)
    assert form["type"] is FlowResultType.FORM and form["step_id"] == "reconfigure_server"
    assert {str(k) for k in form["data_schema"].schema} == {"dial_enabled", "ring_answered_entity", "open_relay_action", "house_calls"}  # no claims, no listen leg; house_calls as the wheel has the bridge
    (key,) = [k for k in form["data_schema"].schema if str(k) == "dial_enabled"]
    assert key.default() is False
    before = dict(entry.data)
    r = await _reconfigure(hass, entry, {"dial_enabled": True, "ring_answered_entity": HELPER})
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "reconfigure_successful"
    assert entry.data["dial_enabled"] is True and entry.data["gate_sip_listen"] == {"ring_answered_entity": HELPER}
    assert {k: v for k, v in entry.data.items() if k not in ("dial_enabled", "gate_sip_listen")} == before   # ports, password: untouched
    await hass.async_block_till_done()
    r = await _reconfigure(hass, entry, {"dial_enabled": False})
    assert "dial_enabled" not in entry.data and "gate_sip_listen" not in entry.data
    await hass.async_block_till_done()
    for e in hass.config_entries.async_entries(UI_DOMAIN):
        await hass.config_entries.async_unload(e.entry_id)
    await hass.async_block_till_done()


OPEN_ACTION = [{"action": "button.press", "target": {"entity_id": "button.example_intercom_ex_gate_open_door"}}]


class _Reloads:
    """Counts the ways an entry gets reloaded, so a test can say "no reload"."""

    def __init__(self, hass):
        self.calls = []
        cfg = hass.config_entries
        self._patches = [patch.object(type(cfg), n, side_effect=lambda *a, _n=n, **k: self.calls.append(_n))
                         for n in ("async_reload", "async_schedule_reload")]

    def __enter__(self):
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()


async def test_an_action_only_edit_is_saved_without_a_reload(hass, fake_transport):
    """DEV-518 C1: a reload drops the SIP registration, so an edit that touches neither `dial_enabled` nor the remote
    fields (the action, the helper) only writes the entry."""
    from homeassistant.data_entry_flow import FlowResultType
    entry = _server_entry(hass, existing_camera_entity="camera.gate_intercom", allow_from=["10.6.0.5"])
    before = dict(entry.data)
    with _Reloads(hass) as reloads:
        r = await _reconfigure(hass, entry, {"dial_enabled": False, "open_relay_action": OPEN_ACTION,
                                             "ring_answered_entity": HELPER})
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "reconfigure_successful"
    assert reloads.calls == []
    assert entry.data["open_relay_action"] == OPEN_ACTION
    assert entry.data["gate_sip_listen"] == {"ring_answered_entity": HELPER}
    rest = ("open_relay_action", "gate_sip_listen")
    assert {k: v for k, v in entry.data.items() if k not in rest} == {k: v for k, v in before.items() if k not in rest}
    # clearing the action keeps the key as [] (the add flow's shape) and does not reload either
    with _Reloads(hass) as reloads:
        await _reconfigure(hass, entry, {"dial_enabled": False})
    assert reloads.calls == [] and entry.data["open_relay_action"] == [] and "gate_sip_listen" not in entry.data


async def test_the_form_prefills_the_action_and_a_dial_change_still_reloads(hass, fake_transport):
    entry = _server_entry(hass, open_relay_action=OPEN_ACTION)
    form = await _reconfigure(hass, entry)
    (key,) = [k for k in form["data_schema"].schema if str(k) == "open_relay_action"]
    assert key.description == {"suggested_value": OPEN_ACTION}
    with _Reloads(hass) as reloads:
        await hass.config_entries.flow.async_configure(
            form["flow_id"], {"dial_enabled": True, "open_relay_action": OPEN_ACTION})
    assert reloads.calls                                  # a transport-visible key changed: the transport must be told
    assert entry.data["dial_enabled"] is True and entry.data["open_relay_action"] == OPEN_ACTION


async def test_a_malformed_action_is_refused(hass, fake_transport):
    entry = _server_entry(hass)
    form = await _reconfigure(hass, entry)
    r = await hass.config_entries.flow.async_configure(form["flow_id"], {"dial_enabled": False, "open_relay_action": ["nope"]})
    assert r["type"].value == "form" and r["errors"] == {"open_relay_action": "invalid_action"}
    assert entry.data["open_relay_action"] == []


async def test_open_relay_on_a_server_door_runs_the_action_read_at_call_time(hass, fake_transport):
    """The action saved by Reconfigure (no reload) is the one `portico_open` runs next: same code path as a door action."""
    from homeassistant.core import Context
    calls = async_mock_service(hass, "button", "press")
    entry = _server_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    provider = get_provider(hass)
    with _Reloads(hass):
        await _reconfigure(hass, entry, {"dial_enabled": False, "open_relay_action": OPEN_ACTION})
    ctx = Context()
    assert await provider.open_relay(SERVER_DEVICE_ID, ctx) == {"ok": True}
    assert [c.data["entity_id"] for c in calls] == [["button.example_intercom_ex_gate_open_door"]]
    assert calls[0].context.parent_id == ctx.id or calls[0].context.id == ctx.id      # the press runs under the caller's Context
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_the_new_form_texts_exist():
    import json
    from pathlib import Path
    root = Path(gate_sip_listen.__file__).parent.parent
    for name in ("strings.json", "translations/en.json"):
        strings = json.loads((root / name).read_text(encoding="utf-8"))["config"]
        assert {"dial_enabled", "ring_answered_entity"} <= set(strings["step"]["driver_config"]["data"])
        assert {"dial_enabled", "ring_answered_entity"} <= set(strings["step"]["reconfigure_server"]["data"])
        assert {"invalid_dial", "invalid_ring_entity"} <= set(strings["error"])


# --- DEV-518: the "SIP station registered" diagnostic ----------------------------------------------------------------


def _registered(hass, srv):
    reg = er.async_get(hass)
    eid = reg.async_get_entity_id("binary_sensor", UI_DOMAIN, f"{srv.entry_id}_station_registered")
    return eid, (hass.states.get(eid) if eid else None)


def _caps(runtime, can_dial):
    base = {k: v for k, v in (runtime.capabilities or {}).items() if k != "CanDial"}
    return {**base, **({"CanDial": True} if can_dial else {})}


async def test_station_registered_follows_cando_and_expiry(hass, fake_transport):
    srv = _server_entry(hass, dial_enabled=True)
    assert await hass.config_entries.async_setup(srv.entry_id)
    await hass.async_block_till_done()
    go = FakeGoTransport.instances[-1]
    eid, st = _registered(hass, srv)
    assert eid == "binary_sensor.sip_station_registered" and st.state == "off"       # up, nobody registered yet
    assert st.attributes["device_class"] == "connectivity"
    ent = er.async_get(hass).async_get(eid)
    assert ent.entity_category == er.EntityCategory.DIAGNOSTIC
    go.notify("capabilities_changed", {"device_id": SERVER_DEVICE_ID, "capabilities": _caps(srv.runtime_data, True)})
    await hass.async_block_till_done()
    assert hass.states.get(eid).state == "on"
    # the registration expired (the transport republishes without CanDial)
    go.notify("capabilities_changed", {"device_id": SERVER_DEVICE_ID, "capabilities": _caps(srv.runtime_data, False)})
    await hass.async_block_till_done()
    assert hass.states.get(eid).state == "off"


async def test_station_registered_is_unavailable_when_the_transport_is_down(hass, fake_transport):
    srv = _server_entry(hass, dial_enabled=True)
    assert await hass.config_entries.async_setup(srv.entry_id)
    await hass.async_block_till_done()
    eid, _ = _registered(hass, srv)
    runtime = srv.runtime_data
    runtime.set_transport_available(False)
    await hass.async_block_till_done()
    assert hass.states.get(eid).state == "unavailable"


async def test_station_registered_with_dial_off_follows_the_new_fields_and_shows_last_register(hass, fake_transport):
    srv = _server_entry(hass)                       # dial_enabled off
    assert await hass.config_entries.async_setup(srv.entry_id)
    await hass.async_block_till_done()
    go = FakeGoTransport.instances[-1]
    eid, st = _registered(hass, srv)
    assert st.state == "unavailable"                # an older wheel: no StationRegistered, no signal
    base = {k: v for k, v in (srv.runtime_data.capabilities or {}).items() if k != "CanDial"}
    go.notify("capabilities_changed", {"device_id": SERVER_DEVICE_ID, "capabilities": {**base, "StationRegistered": False}})
    await hass.async_block_till_done()
    st = hass.states.get(eid)
    assert st.state == "off" and "last_register" not in st.attributes
    caps = {**base, "StationRegistered": True, "LastRegisterAt": 1_800_000_000}
    go.notify("capabilities_changed", {"device_id": SERVER_DEVICE_ID, "capabilities": caps})
    await hass.async_block_till_done()
    st = hass.states.get(eid)
    assert st.state == "on" and st.attributes["last_register"] == "2027-01-15T08:00:00+00:00"
    caps = {**base, "StationRegistered": False, "LastRegisterAt": 1_800_000_000}      # expired: the time stays
    go.notify("capabilities_changed", {"device_id": SERVER_DEVICE_ID, "capabilities": caps})
    await hass.async_block_till_done()
    st = hass.states.get(eid)
    assert st.state == "off" and st.attributes["last_register"] == "2027-01-15T08:00:00+00:00"


async def test_station_registered_is_unavailable_not_off_with_dial_off_and_absent_on_other_drivers(hass, fake_transport):
    srv = _server_entry(hass)                       # dial_enabled off: an older wheel never reports registration
    other = _dahua_entry(hass)
    assert await hass.config_entries.async_setup(other.entry_id)
    await hass.async_block_till_done()
    eid, st = _registered(hass, srv)
    assert st.state == "unavailable"
    reg = er.async_get(hass)
    assert reg.async_get_entity_id("binary_sensor", UI_DOMAIN, f"{other.entry_id}_station_registered") is None
