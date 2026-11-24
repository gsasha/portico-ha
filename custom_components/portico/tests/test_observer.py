"""The shared door-test fixtures and the generic door tests (sensor, lock, availability, hub lifecycle, device id round trip).

The gate_sip_observer door was retired (DEV-518 C5), but `_observer_entry` and the fake transport still stand in for "a door whose
driver reports its device's availability and has no relay of its own": the driver key is only a fake name here, no production code
knows it. (Phase 1b, design doc §4.2a, §9.2, §9.4.)

Runs in the HA-harness session (pytest-homeassistant-custom-component,
`-o asyncio_mode=auto`). The integration is loaded
the way HA loads it (custom_components.portico via `enable_custom_integrations`; a door is a `portico` entry with
`entry_type: "door"`), so these tests exercise the real config-flow manager and the real entity
platforms. Nothing here touches the network: the transport binary, its
subprocess and its socket are all faked.
"""
from __future__ import annotations

import asyncio
import sys
from types import ModuleType
from typing import Any, ClassVar

import pytest
from homeassistant import config_entries
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.portico.door import hub as portico_hub
from custom_components.portico.door.const import DOMAIN, DRIVER_ID, ENTRY_TYPE, ENTRY_TYPE_DOOR
from custom_components.portico.door.transport import TransportError
from custom_components.portico.door import wheel as wheel_module
from custom_components.portico.door.wheel import WHEEL_PIN_VERSION

GATE_SIP_URL = "http://gate-sip.example:8091"
DAHUA_HOST = "station.example"

# Every (label, requirements) the stubbed `async_process_requirements` was asked to install in this test.
WHEEL_INSTALLS: list[tuple[str, list[str]]] = []


@pytest.fixture(autouse=True)
async def _custom_integrations(hass, enable_custom_integrations, tmp_path, monkeypatch):
    """The unified integration needs `http` (its static paths) like every other Portico test. The runtime creates
    `.storage/portico_intercom/` for its socket: point the config path at a temp dir rather than the harness's
    own testing_config."""
    from homeassistant.components.frontend import DATA_EXTRA_MODULE_URL
    from homeassistant.setup import async_setup_component

    monkeypatch.setattr(hass.config, "path", lambda *parts: str(tmp_path.joinpath(*parts)))
    assert await async_setup_component(hass, "http", {})
    hass.data[DATA_EXTRA_MODULE_URL] = set()
    # Nothing here may reach pip: a test that reports a wheel older than the pin gets the install STUBBED (it records
    # the request and installs nothing), so the hub's own floor check then sees the old version.
    WHEEL_INSTALLS.clear()

    async def _no_pip(_hass, name, requirements, is_built_in=True):
        WHEEL_INSTALLS.append((name, list(requirements)))

    monkeypatch.setattr(wheel_module, "async_process_requirements", _no_pip)
    yield


# --------------------------------------------------------------------------
# A fake transport binary: the Go side's `configure` contract, no network.
# --------------------------------------------------------------------------

_OBSERVER_CAPS = {"InboundAudio": 3, "OutboundAudio": 3, "DirectCapable": False,
                  "HasRelay": False, "RTSPTemplate": "", "ReportsAvailability": True}
_DAHUA_CAPS = {"InboundAudio": 0, "OutboundAudio": 0, "DirectCapable": True,
               "HasRelay": True, "RTSPTemplate": "", "ReportsAvailability": False}


class FakeGoTransport:
    """Stands in for TransportClient + the Go binary behind it.

    configure() mirrors main.go + the Go drivers: the device is registered
    under the driver's DeviceID() for gate_sip_observer (config `device_id`,
    else the gate-sip base URL, which is what Go defaults to) and under
    `host`/`nvr_host` otherwise; rings carry that same id, exactly as
    ipcEventSink.Ringing does."""

    instances: ClassVar[list[FakeGoTransport]] = []
    spawned: ClassVar[list[tuple[tuple, "_FakeProcess"]]] = []     # (argv, process) of every create_subprocess_exec
    # Like the real observer, report gate-sip available from configure: the
    # first poll runs as soon as Start returns, so the notification can (and
    # here does) reach Python BEFORE the configure response.
    report_available_on_configure: ClassVar[bool] = True

    def __init__(self, socket_path: str = "") -> None:
        self.socket_path = socket_path
        self.handlers: list = []
        self.configured: list[tuple[str, dict]] = []
        # One process serves every door (P-3): the ids it holds, like Go's deviceRegistry, and the ids it was
        # told to drop. `registry_id` is the id of the door configured last (the single-door tests' name for it).
        self.registry_ids: list[str] = []
        self.deconfigured: list[str] = []
        self.calls: dict[str, str] = {}      # call_id -> device_id, like ipcEventSink's callDeviceMap
        self.requests: list[tuple[str, tuple]] = []
        FakeGoTransport.instances.append(self)

    @property
    def registry_id(self) -> str | None:
        return self.registry_ids[-1] if self.registry_ids else None

    def on_notification(self, handler) -> None:
        self.handlers.append(handler)

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def configure(self, driver: str, config) -> dict[str, Any]:
        config = dict(config)
        self.configured.append((driver, config))
        if driver == "gate_sip_observer":
            device_id = config.get("device_id") or config["gate_sip_url"].rstrip("/")
        else:
            device_id = config.get("host") or config.get("nvr_host")
        if device_id in self.registry_ids:
            raise TransportError("already_configured", device_id)
        self.registry_ids.append(device_id)
        if driver == "gate_sip_observer":
            if self.report_available_on_configure:
                self.availability(True, device_id=device_id)
            return dict(_OBSERVER_CAPS)
        return dict(_DAHUA_CAPS)

    async def dial(self, device_id: str, host: str, port: int) -> dict[str, Any]:
        self.requests.append(("dial", (device_id, host, port)))
        return {"call_id": f"dial-{device_id}"}

    async def webrtc_offer(self, call_id: str, sdp: str) -> dict[str, Any]:
        self.requests.append(("webrtc_offer", (call_id, sdp)))
        return {"sdp": f"answer-for-{self.calls.get(call_id) or call_id}"}

    async def deconfigure(self, device_id: str) -> dict[str, Any]:
        self.requests.append(("deconfigure", (device_id,)))
        if device_id not in self.registry_ids:
            raise TransportError("unknown_device", device_id)
        self.registry_ids.remove(device_id)
        self.deconfigured.append(device_id)
        return {"ok": True}

    def ring(self, call_id: str, device_id: str | None = None) -> None:
        device_id = device_id or self.registry_id
        self.calls[call_id] = device_id
        self.notify("ringing", {"call_id": call_id, "device_id": device_id})

    def state(self, call_id: str, state: str, device_id: str | None = None) -> None:
        """call_state_changed as PH-10's sink sends it: stamped with the ring's device id, or with none for a
        call it never saw ring (an outbound dial). Pass `device_id=""` to send it unstamped."""
        stamp = self.calls.get(call_id, "") if device_id is None else device_id
        self.notify("call_state_changed", {"call_id": call_id, "device_id": stamp, "state": state})

    def availability(self, available: bool, reason: str = "", device_id: str | None = None) -> None:
        self.notify("driver_availability",
                    {"device_id": device_id or self.registry_id, "available": available, "reason": reason})

    def driver_error(self, message: str, code: str = "", device_id: str | None = None) -> None:
        self.notify("driver_error", {"device_id": device_id or self.registry_id, "message": message, "code": code})

    def notify(self, method: str, params: dict) -> None:
        for handler in self.handlers:
            handler(method, params)


class _FakeProcess:
    returncode = None

    def __init__(self) -> None:
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_eof()
        self._exit = asyncio.Event()

    async def wait(self) -> int:
        await self._exit.wait()
        return 0

    def terminate(self) -> None:
        self.returncode = 0
        self._exit.set()

    def kill(self) -> None:
        self.terminate()


@pytest.fixture
def fake_transport(monkeypatch):
    """Replace the wheel, the subprocess and the IPC client — nothing else.
    The hub's own spawn/configure path and every DoorRuntime run for real. The wheel reports the version
    the code pins (wheel.py), so neither the lazy install nor the REL-04 check does anything."""
    FakeGoTransport.instances = []
    FakeGoTransport.spawned = []
    FakeGoTransport.report_available_on_configure = True
    wheel = ModuleType("portico_transport")
    wheel.binary_path = lambda: "/nonexistent/portico-transport"
    monkeypatch.setitem(sys.modules, "portico_transport", wheel)

    async def _fake_exec(*args, **kwargs):
        process = _FakeProcess()
        FakeGoTransport.spawned.append((args, process))
        return process

    monkeypatch.setattr(portico_hub.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(portico_hub, "TransportClient", FakeGoTransport)
    monkeypatch.setattr(portico_hub.importlib.metadata, "version", lambda name: WHEEL_PIN_VERSION)
    return FakeGoTransport


async def wait_for(hass, predicate, what="condition"):
    """The hub's restart runs in a background task and an executor job: poll instead of counting loop turns."""
    for _ in range(300):
        if predicate():
            return
        await asyncio.sleep(0.01)
    await hass.async_block_till_done(wait_background_tasks=False)
    assert predicate(), f"timed out waiting for {what}"


def get_hub(hass):
    return portico_hub.get_hub(hass)


def _observer_entry(hass, **extra) -> MockConfigEntry:
    data = {
        ENTRY_TYPE: ENTRY_TYPE_DOOR,
        "driver": "gate_sip_observer",
        "gate_sip_url": GATE_SIP_URL,
        "interval": 1.0,
        "device_id": GATE_SIP_URL,
        "existing_camera_entity": "camera.gate",
        "open_relay_action": [],
        **extra,
    }
    entry = MockConfigEntry(domain=DOMAIN, data=data, unique_id=f"gate_sip_observer:{data['device_id']}")
    entry.add_to_hass(hass)
    return entry


def _dahua_entry(hass) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={ENTRY_TYPE: ENTRY_TYPE_DOOR, "driver": "dahua_sip", "host": DAHUA_HOST, "sip_user": "9901#0",
              "sip_password": "x", "rtsp_username": "u", "rtsp_password": "p",
              "existing_camera_entity": "camera.gate", "open_relay_action": []},
        unique_id=f"dahua_sip:{DAHUA_HOST}",
    )
    entry.add_to_hass(hass)
    return entry


def _entity_ids(hass, entry, domain: str) -> list[str]:
    return [
        e.entity_id
        for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if e.domain == domain
    ]


# --------------------------------------------------------------------------
# 1. Config flow
# --------------------------------------------------------------------------

async def _start_door_flow(hass):
    """The first menu, then "Add a door": the driver picker form."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.MENU and result["step_id"] == "user"
    assert result["menu_options"] == ["door", "push_credentials", "sip_server_door", "sip_server_remote"]
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "door"})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "door"
    return result


async def test_dahua_flow_unchanged(hass):
    result = await _start_door_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"driver": "dahua_sip"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"host": DAHUA_HOST, "sip_user": "9901#0", "sip_password": "x", "rtsp_username": "u", "rtsp_password": "p"},
    )
    assert result["step_id"] == "camera_config"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})  # still optional
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].unique_id == f"dahua_sip:{DAHUA_HOST}"
    assert "device_id" not in result["result"].data


# --------------------------------------------------------------------------
# 2. device_id round trip: Python's id == the id Go registers and rings under
# --------------------------------------------------------------------------

async def test_device_id_round_trip_with_a_fake_transport(hass, fake_transport):
    entry = _observer_entry(hass, device_id=GATE_SIP_URL)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    go = fake_transport.instances[-1]
    driver, sent = go.configured[-1]
    assert driver == "gate_sip_observer"
    assert sent["device_id"] == GATE_SIP_URL  # sent explicitly, not left to Go's default
    runtime = entry.runtime_data
    assert runtime.device_id == GATE_SIP_URL == go.registry_id

    # A ring from Go lands on THIS entry's tracker...
    go.ring("gso-1")
    assert runtime.call_state.state == "ringing"
    assert runtime.call_state.call_id == "gso-1"

    # ...and the provider finds the door by the id the ring carried.
    provider = hass.data[DRIVER_ID]["provider"]
    assert provider.call_state(go.registry_id) == {"door_id": go.registry_id, "call_id": "gso-1", "state": "ringing"}


async def test_dahua_device_id_is_still_the_host(hass, fake_transport):
    entry = _dahua_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    go = fake_transport.instances[-1]
    assert go.configured[-1][1]["device_id"] == DAHUA_HOST
    assert entry.runtime_data.device_id == DAHUA_HOST == go.registry_id


async def test_a_ring_for_another_device_id_is_not_applied(hass, fake_transport):
    entry = _observer_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    go = fake_transport.instances[-1]
    go.notify("ringing", {"call_id": "x", "device_id": "http://someone-else:8091"})
    assert entry.runtime_data.call_state.state == "idle"


# --------------------------------------------------------------------------
# 3. The call-state sensor, driven by pushed events only
# --------------------------------------------------------------------------

def _sensor_id(hass, entry) -> str:
    ids = _entity_ids(hass, entry, "sensor")
    assert len(ids) == 1, ids
    return ids[0]


async def test_call_state_sensor_follows_ringing_and_call_state_changed(hass, fake_transport):
    entry = _observer_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    sensor = _sensor_id(hass, entry)
    go = fake_transport.instances[-1]

    state = hass.states.get(sensor)
    assert state.state == "idle"
    assert state.attributes["options"] == ["idle", "ringing", "answered", "active", "ended"]
    assert state.attributes["device_class"] == "enum"
    assert state.attributes.get("call_id") is None

    go.ring("gso-1")
    await hass.async_block_till_done()
    state = hass.states.get(sensor)
    assert state.state == "ringing"
    assert state.attributes["call_id"] == "gso-1"

    go.state("gso-1", "answered")
    await hass.async_block_till_done()
    assert hass.states.get(sensor).state == "answered"

    go.state("gso-1", "ended")  # §4.5: Ended -> idle is immediate
    await hass.async_block_till_done()
    state = hass.states.get(sensor)
    assert state.state == "idle"
    assert state.attributes.get("call_id") is None


async def test_call_state_sensor_does_not_poll(hass, fake_transport):
    entry = _observer_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    component = hass.data["entity_components"]["sensor"]
    entity = component.get_entity(_sensor_id(hass, entry))
    assert entity.should_poll is False


async def test_call_state_sensor_is_unavailable_while_the_transport_is_down(hass, fake_transport):
    entry = _observer_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    sensor = _sensor_id(hass, entry)

    entry.runtime_data.set_transport_available(False)
    await hass.async_block_till_done()
    assert hass.states.get(sensor).state == STATE_UNAVAILABLE

    entry.runtime_data.set_transport_available(True)
    await hass.async_block_till_done()
    assert hass.states.get(sensor).state == "idle"


async def test_unload_removes_the_sensor_listener(hass, fake_transport):
    entry = _observer_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    tracker = entry.runtime_data.call_state
    assert tracker._listeners
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert tracker._listeners == []


# --------------------------------------------------------------------------
# 4. The lock honours Capabilities.HasRelay from `configure`
# --------------------------------------------------------------------------

async def test_no_lock_when_the_driver_reports_no_relay(hass, fake_transport):
    entry = _observer_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.capabilities["HasRelay"] is False
    assert _entity_ids(hass, entry, "lock") == []


async def test_lock_still_created_when_the_driver_reports_a_relay(hass, fake_transport):
    entry = _dahua_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert len(_entity_ids(hass, entry, "lock")) == 1


async def test_lock_gate_reads_capabilities_not_the_driver_name(hass, fake_transport, monkeypatch):
    """A dahua_sip entry whose transport reports HasRelay false gets no lock:
    the gate is the configure response (§9.4), not a list of driver names."""
    monkeypatch.setitem(_DAHUA_CAPS, "HasRelay", False)
    entry = _dahua_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert _entity_ids(hass, entry, "lock") == []


async def test_observer_creates_no_camera_of_its_own(hass, fake_transport):
    entry = _observer_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert _entity_ids(hass, entry, "camera") == []


# --------------------------------------------------------------------------
# 5b. driver_error -> an HA repair issue (Python receiving side only)
# --------------------------------------------------------------------------

async def test_driver_error_raises_and_configure_clears_a_repair_issue(hass, fake_transport):
    entry = _observer_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    issue_id = f"driver_error_{entry.entry_id}"
    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, issue_id) is None

    fake_transport.instances[-1].driver_error("gate-sip unreachable", code="unavailable")
    await hass.async_block_till_done()
    issue = registry.async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.severity == ir.IssueSeverity.ERROR
    assert issue.translation_key == "driver_error"
    assert issue.translation_placeholders["message"] == "gate-sip unreachable"

    # A successful (re)configure of the transport clears it (a reconfigure of a held id is refused by Go, so the
    # hub deconfigures first, as a reload does).
    go = fake_transport.instances[-1]
    await go.deconfigure(go.registry_id)
    await entry.runtime_data.async_configure()
    assert registry.async_get_issue(DOMAIN, issue_id) is None


async def test_unload_clears_the_driver_error_issue(hass, fake_transport):
    entry = _observer_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    fake_transport.instances[-1].driver_error("boom")
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"driver_error_{entry.entry_id}") is None


async def test_unexpected_transport_exit_marks_the_device_unavailable(hass, fake_transport):
    entry = _observer_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    sensor = _sensor_id(hass, entry)
    runtime = entry.runtime_data
    hub = get_hub(hass)

    # Hold the restart so the gap is observable.
    gate = asyncio.Event()
    real_spawn = hub._start_process

    async def _slow_spawn():
        await gate.wait()
        await real_spawn()

    hub._start_process = _slow_spawn
    hub._process.terminate()  # exits "unexpectedly": not via async_stop
    await hass.async_block_till_done(wait_background_tasks=False)
    await asyncio.sleep(0)
    assert hass.states.get(sensor).state == STATE_UNAVAILABLE

    gate.set()
    await wait_for(hass, lambda: runtime.transport_available, "the restart")
    await hass.async_block_till_done(wait_background_tasks=False)
    assert hass.states.get(sensor).state == "idle"
    await runtime.async_stop()



# --------------------------------------------------------------------------
# 6. driver_availability: the driver's own view of its device (gate-sip)
# --------------------------------------------------------------------------

async def _setup(hass, entry):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return _sensor_id(hass, entry), fake_go()


def fake_go() -> FakeGoTransport:
    return FakeGoTransport.instances[-1]


async def test_observer_sensor_is_unavailable_until_the_driver_reports_available(hass, fake_transport):
    """Before the first report the sensor must not claim `idle`: gate-sip
    may be unreachable from the start."""
    fake_transport.report_available_on_configure = False
    entry = _observer_entry(hass)
    sensor, go = await _setup(hass, entry)
    assert entry.runtime_data.transport_available is True
    assert hass.states.get(sensor).state == STATE_UNAVAILABLE

    go.availability(False, "GET /health: HTTP 500")  # still down: stays unavailable
    await hass.async_block_till_done()
    assert hass.states.get(sensor).state == STATE_UNAVAILABLE

    go.availability(True)
    await hass.async_block_till_done()
    assert hass.states.get(sensor).state == "idle"


async def test_an_availability_report_before_the_configure_response_is_kept(hass, fake_transport):
    """The fake reports available from inside configure, as the real binary
    can: the configure response must not wipe it."""
    entry = _observer_entry(hass)
    sensor, _ = await _setup(hass, entry)
    assert entry.runtime_data.driver_available is True
    assert hass.states.get(sensor).state == "idle"


async def test_driver_unavailable_resets_a_ringing_call_and_recovers_to_idle(hass, fake_transport):
    entry = _observer_entry(hass)
    sensor, go = await _setup(hass, entry)
    go.ring("gso-1")
    await hass.async_block_till_done()
    assert hass.states.get(sensor).state == "ringing"

    go.availability(False, "GET /health: dial tcp: connection refused")
    await hass.async_block_till_done()
    assert hass.states.get(sensor).state == STATE_UNAVAILABLE
    tracker = entry.runtime_data.call_state
    assert (tracker.state, tracker.call_id) == ("idle", None)

    go.availability(True)
    await hass.async_block_till_done()
    state = hass.states.get(sensor)
    assert state.state == "idle"
    assert state.attributes.get("call_id") is None


async def test_driver_unavailable_resets_an_answered_call(hass, fake_transport):
    entry = _observer_entry(hass)
    sensor, go = await _setup(hass, entry)
    go.ring("gso-1")
    go.state("gso-1", "answered")
    go.availability(False, "x")
    go.availability(True)
    await hass.async_block_till_done()
    assert hass.states.get(sensor).state == "idle"


async def test_late_events_for_a_reset_call_are_ignored(hass, fake_transport):
    """Go keeps following its call across an outage and may answer/end it
    after recovery; Python already ended it."""
    entry = _observer_entry(hass)
    sensor, go = await _setup(hass, entry)
    go.ring("gso-1")
    go.availability(False, "GET /health: HTTP 503")
    go.availability(True)
    go.state("gso-1", "answered")
    await hass.async_block_till_done()
    assert hass.states.get(sensor).state == "idle"
    go.state("gso-1", "ended")
    await hass.async_block_till_done()
    state = hass.states.get(sensor)
    assert state.state == "idle"
    assert state.attributes.get("call_id") is None

    go.ring("gso-2")  # the next call still rings
    await hass.async_block_till_done()
    assert hass.states.get(sensor).state == "ringing"


async def test_ordinary_unavailability_raises_no_repair_issue(hass, fake_transport):
    entry = _observer_entry(hass)
    _, go = await _setup(hass, entry)
    go.availability(False, "GET /health: HTTP 502")
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"driver_error_{entry.entry_id}") is None


async def test_availability_for_another_device_or_malformed_is_ignored(hass, fake_transport):
    entry = _observer_entry(hass)
    sensor, go = await _setup(hass, entry)
    go.ring("gso-1")
    go.notify("driver_availability", {"device_id": "http://someone-else:8091", "available": False, "reason": "x"})
    go.notify("driver_availability", {"device_id": go.registry_id, "available": "false", "reason": "x"})
    go.notify("driver_availability", {"device_id": go.registry_id})
    await hass.async_block_till_done()
    assert hass.states.get(sensor).state == "ringing"


async def test_dahua_entities_ignore_availability_they_never_get(hass, fake_transport):
    """dahua_sip sends no driver_availability: its entities follow the
    transport alone, as before."""
    entry = _dahua_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.capabilities.get("ReportsAvailability") is False
    sensor = _sensor_id(hass, entry)
    assert hass.states.get(sensor).state == "idle"
    lock = _entity_ids(hass, entry, "lock")[0]
    assert hass.states.get(lock).state != STATE_UNAVAILABLE

    entry.runtime_data.set_transport_available(False)
    await hass.async_block_till_done()
    assert hass.states.get(sensor).state == STATE_UNAVAILABLE
    entry.runtime_data.set_transport_available(True)
    await hass.async_block_till_done()
    assert hass.states.get(sensor).state == "idle"


async def test_dahua_capabilities_without_the_field_are_unchanged(hass, fake_transport, monkeypatch):
    """A transport older than this change returns no ReportsAvailability at
    all: treated as false."""
    monkeypatch.delitem(_DAHUA_CAPS, "ReportsAvailability")
    entry = _dahua_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(_sensor_id(hass, entry)).state == "idle"


async def test_transport_exit_resets_the_call_and_the_driver_availability(hass, fake_transport):
    """The noted gap: on a transport crash the tracker was never reset, so a
    ringing call outlived the process that was following it."""
    entry = _observer_entry(hass)
    sensor, go = await _setup(hass, entry)
    runtime = entry.runtime_data
    hub = get_hub(hass)
    go.ring("gso-1")
    await hass.async_block_till_done()

    gate = asyncio.Event()
    real_spawn = hub._start_process

    async def _slow_spawn():
        # By the time the supervisor respawns, the old process's call and
        # availability are already gone.
        assert (runtime.call_state.state, runtime.call_state.call_id) == ("idle", None)
        assert runtime.driver_available is None
        await gate.wait()
        await real_spawn()

    fake_transport.report_available_on_configure = False
    hub._start_process = _slow_spawn
    hub._process.terminate()
    await hass.async_block_till_done(wait_background_tasks=False)
    await asyncio.sleep(0)
    assert hass.states.get(sensor).state == STATE_UNAVAILABLE
    assert runtime.call_state.state == "idle"

    gate.set()
    await wait_for(hass, lambda: runtime.transport_available, "the restart")
    await hass.async_block_till_done(wait_background_tasks=False)
    # Restarted, but the new driver has not reported yet: still unavailable.
    assert runtime.transport_available is True
    assert hass.states.get(sensor).state == STATE_UNAVAILABLE
    fake_go().availability(True)
    await hass.async_block_till_done(wait_background_tasks=False)
    assert hass.states.get(sensor).state == "idle"
    await runtime.async_stop()


async def test_stopping_the_runtime_resets_the_call(hass, fake_transport):
    entry = _observer_entry(hass)
    _, go = await _setup(hass, entry)
    go.ring("gso-1")
    tracker = entry.runtime_data.call_state
    seen = []
    tracker.add_listener(lambda: seen.append(tracker.state))
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert (tracker.state, tracker.call_id) == ("idle", None)
    assert seen == ["idle"]


async def test_a_dead_transports_straggling_notification_is_dropped(hass, fake_transport):
    """A line the dead process wrote before exiting can be read after the
    supervisor noticed the exit; it must not resurrect a call."""
    entry = _observer_entry(hass)
    sensor, old = await _setup(hass, entry)
    runtime = entry.runtime_data
    get_hub(hass)._process.terminate()
    await wait_for(hass, lambda: fake_go() is not old and runtime.transport_available, "the restart")
    await hass.async_block_till_done(wait_background_tasks=False)
    assert fake_go() is not old  # restarted with a new client
    assert hass.states.get(sensor).state == "idle"

    old.ring("gso-stale")
    old.availability(False, "stale")
    await hass.async_block_till_done(wait_background_tasks=False)
    assert hass.states.get(sensor).state == "idle"
    assert runtime.call_state.call_id is None
    await runtime.async_stop()
