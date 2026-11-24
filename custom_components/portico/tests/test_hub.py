"""The one hub (P-3 / PH-11): ONE transport process per HA instance, however many doors (PROC-01, PROC-02, REL-04).

HA-harness session, `-o asyncio_mode=auto`. The wheel, the subprocess and the IPC client are the same fakes the
observer tests use (`FakeGoTransport` is one process: it keeps a registry of the ids it was told to configure and
stamps `device_id` on events the way PH-10's Go sink does). The hub, every `DoorRuntime` and the provider run for
real.
"""
from __future__ import annotations

import asyncio
import threading

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, STATE_UNAVAILABLE
from homeassistant.helpers import issue_registry as ir

from custom_components.portico.door import hub as portico_hub
from custom_components.portico.door.const import DOMAIN, DRIVER_ID
from custom_components.portico.diagnostics import async_get_config_entry_diagnostics
from custom_components.portico.door.provider_impl import DATA_TRANSPORT_PROVIDER
from custom_components.portico.door.transport import TransportError
from custom_components.portico.door.wheel import WHEEL_PIN_VERSION

from .test_observer import (  # noqa: F401 - fixtures by name
    DAHUA_HOST,
    GATE_SIP_URL,
    FakeGoTransport,
    _custom_integrations,
    _dahua_entry,
    _observer_entry,
    _sensor_id,
    fake_transport,
    get_hub,
    wait_for,
)


async def _setup_two(hass):
    """The observer and a dahua door, both set up on the one hub."""
    observer, dahua = _observer_entry(hass), _dahua_entry(hass)
    assert await hass.config_entries.async_setup(observer.entry_id)      # sets the whole domain up: both entries
    await hass.async_block_till_done()
    assert observer.state.value == dahua.state.value == "loaded"
    return observer, dahua


def _provider(hass):
    return hass.data[DRIVER_ID][DATA_TRANSPORT_PROVIDER]


def _argv_socket(argv: tuple) -> str:
    return argv[argv.index("-socket") + 1]


async def test_two_doors_one_process_one_socket(hass, fake_transport):
    """PROC-01: two entries, one `create_subprocess_exec`, one IPC client, one `configure` per door on it."""
    observer, dahua = await _setup_two(hass)

    assert len(fake_transport.spawned) == 1
    assert len(fake_transport.instances) == 1
    go = fake_transport.instances[0]
    assert sorted(driver for driver, _ in go.configured) == ["dahua_sip", "gate_sip_observer"]      # the fake door's driver key
    assert sorted(go.registry_ids) == sorted([GATE_SIP_URL, DAHUA_HOST])
    assert {d.device_id for d in get_hub(hass).doors} == {GATE_SIP_URL, DAHUA_HOST}
    # both doors are on the provider, each with its own tracker, on the one client
    assert sorted(door["id"] for door in _provider(hass).doors()) == sorted([GATE_SIP_URL, DAHUA_HOST])
    assert observer.runtime_data.client is dahua.runtime_data.client is go


async def test_socket_path_is_one_per_instance(hass, fake_transport, tmp_path):
    observer, dahua = await _setup_two(hass)
    expected = str(tmp_path / ".storage" / "portico_intercom" / "hub.sock")
    assert get_hub(hass).socket_path == expected
    (argv, _), = fake_transport.spawned
    assert _argv_socket(argv) == expected
    assert fake_transport.instances[0].socket_path == expected
    # no per-entry socket: the directory holds nothing named for an entry id
    assert not list((tmp_path / ".storage" / "portico_intercom").glob(f"{observer.entry_id}*"))


async def test_the_socket_directory_is_made_in_the_executor(hass, fake_transport, monkeypatch):
    """`os.makedirs` is blocking I/O: it ran on the event loop in the per-entry runtime (P-3 moves it off)."""
    loop_thread = threading.get_ident()
    calls: list[int] = []
    real = portico_hub.os.makedirs

    def _spy(*args, **kwargs):
        calls.append(threading.get_ident())
        return real(*args, **kwargs)

    monkeypatch.setattr(portico_hub.os, "makedirs", _spy)
    entry = _observer_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert calls and loop_thread not in calls


async def test_unloading_one_door_deconfigures_it_and_keeps_the_process(hass, fake_transport):
    observer, dahua = await _setup_two(hass)
    go = fake_transport.instances[0]
    (_, process), = fake_transport.spawned

    assert await hass.config_entries.async_unload(observer.entry_id)
    await hass.async_block_till_done()

    assert go.deconfigured == [GATE_SIP_URL] and go.registry_ids == [DAHUA_HOST]
    assert process.returncode is None and len(fake_transport.spawned) == 1
    assert get_hub(hass).client is go
    # the other door is untouched: still available, still rings
    dahua_sensor = _sensor_id(hass, dahua)
    assert hass.states.get(dahua_sensor).state == "idle"
    go.ring("d-1", device_id=DAHUA_HOST)
    await hass.async_block_till_done()
    assert hass.states.get(dahua_sensor).state == "ringing"
    # and a re-setup of the first one reuses the process (a reload is not a restart)
    assert await hass.config_entries.async_setup(observer.entry_id)
    await hass.async_block_till_done()
    assert len(fake_transport.spawned) == 1 and go.registry_ids == [DAHUA_HOST, GATE_SIP_URL]
    assert hass.states.get(dahua_sensor).state == "ringing"


async def test_last_door_stops_the_process(hass, fake_transport):
    observer, dahua = await _setup_two(hass)
    go = fake_transport.instances[0]
    (_, process), = fake_transport.spawned
    for entry in (observer, dahua):
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
    assert go.deconfigured == [GATE_SIP_URL, DAHUA_HOST]
    assert process.returncode == 0                      # terminated
    assert get_hub(hass).client is None and get_hub(hass).doors == []
    # the next door starts a fresh process (a clean start)
    again = _observer_entry(hass, gate_sip_url="http://other.example:8091", device_id="http://other.example:8091")
    assert await hass.config_entries.async_setup(again.entry_id)
    await hass.async_block_till_done()
    assert len(fake_transport.spawned) == 2 and len(fake_transport.instances) == 2


async def test_rings_are_routed_by_device_id(hass, fake_transport):
    observer, dahua = await _setup_two(hass)
    go = fake_transport.instances[0]
    obs_sensor, dahua_sensor = _sensor_id(hass, observer), _sensor_id(hass, dahua)

    go.ring("gso-1", device_id=GATE_SIP_URL)
    await hass.async_block_till_done()
    assert hass.states.get(obs_sensor).state == "ringing"
    assert hass.states.get(dahua_sensor).state == "idle"
    assert dahua.runtime_data.call_state.call_id is None

    go.ring("dh-1", device_id=DAHUA_HOST)
    await hass.async_block_till_done()
    assert hass.states.get(dahua_sensor).attributes.get("call_id") == "dh-1"
    assert hass.states.get(obs_sensor).attributes.get("call_id") == "gso-1"

    go.state("dh-1", "ended")                  # stamped by the Go sink with the ring's device id
    await hass.async_block_till_done()
    assert hass.states.get(dahua_sensor).state == "idle"
    assert hass.states.get(obs_sensor).state == "ringing"

    go.availability(False, "down", device_id=GATE_SIP_URL)       # availability is per device too
    await hass.async_block_till_done()
    assert hass.states.get(obs_sensor).state == STATE_UNAVAILABLE
    assert hass.states.get(dahua_sensor).state == "idle"


async def test_a_transition_without_a_device_id_goes_by_the_call_id_the_hub_learned(hass, fake_transport):
    """R2-08: an outbound `dial` produces no ring, so its transitions carry an empty device id; the provider
    tells the hub which door the `dial` result belongs to, and nothing is ever given to 'the first door'."""
    observer, dahua = await _setup_two(hass)
    go = fake_transport.instances[0]
    result = await _provider(hass).dial(DAHUA_HOST, "10.0.0.9", 5060)
    assert result == {"call_id": f"dial-{DAHUA_HOST}"}
    call_id = result["call_id"]

    go.state(call_id, "answered", device_id="")
    await hass.async_block_till_done()
    assert dahua.runtime_data.call_state.state == "answered"
    assert observer.runtime_data.call_state.state == "idle"

    go.state("never-seen", "answered", device_id="")           # no owner: dropped, not handed to a door
    await hass.async_block_till_done()
    assert observer.runtime_data.call_state.state == "idle"
    assert dahua.runtime_data.call_state.call_id == call_id


async def test_webrtc_on_second_door(hass, fake_transport):
    """PROC-02: a call of the second door is answered/offered through that door, on the one client."""
    observer, dahua = await _setup_two(hass)
    go = fake_transport.instances[0]
    go.ring("gso-1", device_id=GATE_SIP_URL)
    go.ring("dh-1", device_id=DAHUA_HOST)
    await hass.async_block_till_done()

    answer = await _provider(hass).webrtc_offer("dh-1", "v=0 offer")
    assert answer == {"sdp": f"answer-for-{DAHUA_HOST}"}
    assert ("webrtc_offer", ("dh-1", "v=0 offer")) in go.requests
    assert _provider(hass)._require_call("dh-1") is dahua
    assert _provider(hass)._require_call("gso-1") is observer
    with pytest.raises(TransportError) as err:
        await _provider(hass).webrtc_offer("nobody-rang", "v=0")
    assert err.value.code == "unknown_call"


async def test_unrouted_notifications_are_dropped_and_logged(hass, fake_transport, caplog):
    observer, dahua = await _setup_two(hass)
    go = fake_transport.instances[0]
    go.ring("x-1", device_id="no-such-door")
    go.notify("ringing", {"call_id": "x-2"})                    # no device id and no known call: no shortcut
    go.notify("driver_availability", {"available": False, "reason": "x"})
    await hass.async_block_till_done()
    for entry in (observer, dahua):
        assert entry.runtime_data.call_state.state == "idle" and entry.runtime_data.driver_available in (None, True)
    assert "dropping ringing" in caplog.text


async def test_driver_error_lands_on_the_door_it_names(hass, fake_transport):
    observer, dahua = await _setup_two(hass)
    go = fake_transport.instances[0]
    go.driver_error("register refused", code="sip", device_id=DAHUA_HOST)
    await hass.async_block_till_done()
    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, f"driver_error_{dahua.entry_id}") is not None
    assert registry.async_get_issue(DOMAIN, f"driver_error_{observer.entry_id}") is None


async def test_crash_restarts_once_and_reconfigures_every_door(hass, fake_transport):
    observer, dahua = await _setup_two(hass)
    first = fake_transport.instances[0]
    obs_sensor, dahua_sensor = _sensor_id(hass, observer), _sensor_id(hass, dahua)
    first.ring("gso-1", device_id=GATE_SIP_URL)
    await hass.async_block_till_done()

    get_hub(hass)._process.terminate()                          # exits "unexpectedly"
    await wait_for(hass, lambda: len(fake_transport.instances) == 2 and dahua.runtime_data.transport_available,
                   "the restart")
    second = fake_transport.instances[1]
    assert len(fake_transport.spawned) == 2
    assert sorted(second.registry_ids) == sorted([GATE_SIP_URL, DAHUA_HOST])    # every door re-configured
    assert observer.runtime_data.call_state.state == "idle"                     # the old process's call is gone
    assert observer.runtime_data.client is second is dahua.runtime_data.client
    await hass.async_block_till_done(wait_background_tasks=False)
    assert hass.states.get(dahua_sensor).state == "idle"
    second.ring("gso-2", device_id=GATE_SIP_URL)
    await hass.async_block_till_done()
    assert hass.states.get(obs_sensor).state == "ringing"
    # a straggling line from the dead process changes nothing
    first.ring("gso-stale", device_id=GATE_SIP_URL)
    await hass.async_block_till_done()
    assert observer.runtime_data.call_state.call_id == "gso-2"


async def test_a_second_crash_is_not_restarted_and_a_new_door_starts_clean(hass, fake_transport, caplog):
    """The restart-once policy counts per process lifetime and starts over when a door attaches to a process that
    is not running."""
    observer = _observer_entry(hass)
    assert await hass.config_entries.async_setup(observer.entry_id)
    await hass.async_block_till_done()
    hub = get_hub(hass)
    hub._process.terminate()
    await wait_for(hass, lambda: len(fake_transport.spawned) == 2 and observer.runtime_data.transport_available,
                   "the restart")
    hub._process.terminate()                                    # dies again: no second restart
    await wait_for(hass, lambda: not observer.runtime_data.transport_available, "the give-up")
    await asyncio.sleep(0.05)
    assert len(fake_transport.spawned) == 2
    assert "not restarting" in caplog.text
    assert hass.states.get(_sensor_id(hass, observer)).state == STATE_UNAVAILABLE

    dahua = _dahua_entry(hass)                                  # a door attaches: clean start, every door re-configured
    assert await hass.config_entries.async_setup(dahua.entry_id)
    await hass.async_block_till_done()
    assert len(fake_transport.spawned) == 3
    assert sorted(fake_transport.instances[-1].registry_ids) == sorted([GATE_SIP_URL, DAHUA_HOST])
    assert observer.runtime_data.transport_available and dahua.runtime_data.transport_available
    hub._process.terminate()                                    # and it gets its one restart again
    await wait_for(hass, lambda: len(fake_transport.spawned) == 4 and dahua.runtime_data.transport_available,
                   "the restart")


async def test_a_failing_configure_leaves_no_door_and_no_process(hass, fake_transport, monkeypatch):
    async def _refuse(self, driver, config):
        raise TransportError("bad_config", "nope")

    monkeypatch.setattr(FakeGoTransport, "configure", _refuse)
    entry = _observer_entry(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    hub = get_hub(hass)
    assert hub.doors == [] and hub.client is None
    assert fake_transport.spawned[0][1].returncode == 0         # the process it started was stopped again


async def test_cancel_during_start_kills_the_process(hass, fake_transport, monkeypatch):
    """R5-02: a cancellation inside the connect-retry loop (HA stopping during the restart's start, an entry
    setup cancelled) must not leave the spawned process or its drain task behind."""
    async def _never_up(self):
        raise ConnectionRefusedError()

    monkeypatch.setattr(FakeGoTransport, "connect", _never_up)
    hub = get_hub(hass)
    drains: list[asyncio.Future] = []

    async def _drain(process):
        try:
            await asyncio.sleep(3600)
        finally:
            drains.append(asyncio.current_task())

    monkeypatch.setattr(hub, "_drain_subprocess_output", _drain)
    start = asyncio.ensure_future(hub._start_process())
    await wait_for(hass, lambda: len(fake_transport.spawned) == 1, "the spawn")
    await asyncio.sleep(0.05)                                   # inside the retry loop now
    start.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start
    (_, process), = fake_transport.spawned
    assert process.returncode == 0                              # terminated, not orphaned
    await wait_for(hass, lambda: len(drains) == 1, "the drain task's cancellation")
    assert hub._process is None and hub.client is None


async def test_a_crash_during_an_attach_waits_for_the_attach(hass, fake_transport, monkeypatch):
    """R5-03: `configure` of the attaching door runs under the hub lock, so the restart cannot run between the
    door entering the table and its configure finishing (it used to configure the door a second time)."""
    gate, reached = asyncio.Event(), asyncio.Event()
    real = FakeGoTransport.configure

    async def _gated(self, driver, config):
        if self is fake_transport.instances[0]:
            reached.set()
            await gate.wait()
        return await real(self, driver, config)

    monkeypatch.setattr(FakeGoTransport, "configure", _gated)
    observer = _observer_entry(hass)
    setup = asyncio.ensure_future(hass.config_entries.async_setup(observer.entry_id))
    await asyncio.wait_for(reached.wait(), 5)
    get_hub(hass)._process.terminate()                          # the process dies while the door is configuring
    await asyncio.sleep(0.2)
    assert len(fake_transport.spawned) == 1                     # the restart waits for the lock
    gate.set()
    assert await setup
    await wait_for(hass, lambda: len(fake_transport.instances) == 2 and observer.runtime_data.transport_available,
                   "the restart")
    assert [driver for driver, _ in fake_transport.instances[1].configured] == ["gate_sip_observer"]   # once
    assert fake_transport.instances[1].registry_ids == [GATE_SIP_URL]
    assert get_hub(hass).doors == [observer.runtime_data]


async def test_already_configured_on_a_restart_is_done_not_an_error(hass, fake_transport, monkeypatch, caplog):
    """R5-03: Go answers `already_configured` for a door the process holds; re-configuring after a restart treats
    that as done (and still configures the other doors)."""
    observer, dahua = await _setup_two(hass)
    real = FakeGoTransport.configure

    async def _already(self, driver, config):
        if driver == "gate_sip_observer" and len(fake_transport.instances) > 1:
            raise TransportError("already_configured", GATE_SIP_URL)
        return await real(self, driver, config)

    monkeypatch.setattr(FakeGoTransport, "configure", _already)
    get_hub(hass)._process.terminate()
    await wait_for(hass, lambda: len(fake_transport.instances) == 2 and dahua.runtime_data.transport_available,
                   "the restart")
    assert fake_transport.instances[1].registry_ids == [DAHUA_HOST]
    assert "could not configure" not in caplog.text
    assert {d.device_id for d in get_hub(hass).doors} == {GATE_SIP_URL, DAHUA_HOST}


async def test_ha_stop_stops_the_hub_once(hass, fake_transport):
    observer, dahua = await _setup_two(hass)
    (_, process), = fake_transport.spawned
    tracker = observer.runtime_data.call_state
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await wait_for(hass, lambda: process.returncode == 0, "the stop")
    assert get_hub(hass).client is None
    # a door unloading after the stop event is a no-op on the hub (no deconfigure on a stopped transport)
    assert await hass.config_entries.async_unload(observer.entry_id)
    await hass.async_block_till_done()
    assert fake_transport.instances[0].deconfigured == []
    assert tracker.state == "idle"


# ---- REL-04: the wheel version check --------------------------------------------------------------------------

async def test_older_wheel_raises_repair_not_exception(hass, fake_transport, monkeypatch, caplog):
    """The Python is ahead of the installed wheel (dev-sync skew): a repair issue naming both versions, no process,
    no door, and `async_setup_entry` returns False instead of raising or failing later on a missing verb."""
    monkeypatch.setattr(portico_hub.importlib.metadata, "version", lambda name: "0.4.1")
    entry = _observer_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is False
    await hass.async_block_till_done()

    issue = ir.async_get(hass).async_get_issue(DOMAIN, "wheel_too_old")
    assert issue is not None and issue.severity == ir.IssueSeverity.ERROR
    assert issue.translation_key == "wheel_too_old"
    assert issue.translation_placeholders == {"installed": "0.4.1", "required": portico_hub.MIN_WHEEL_VERSION}
    assert fake_transport.spawned == [] and fake_transport.instances == []
    assert get_hub(hass).doors == []
    assert "0.4.1" in caplog.text and portico_hub.MIN_WHEEL_VERSION in caplog.text
    # every door is refused the same way (final: not a retry loop)
    other = _dahua_entry(hass)
    assert await hass.config_entries.async_setup(other.entry_id) is False


async def test_a_current_wheel_clears_the_issue(hass, fake_transport, monkeypatch):
    monkeypatch.setattr(portico_hub.importlib.metadata, "version", lambda name: "0.4.1")
    entry = _observer_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is False
    assert ir.async_get(hass).async_get_issue(DOMAIN, "wheel_too_old") is not None
    monkeypatch.setattr(portico_hub.importlib.metadata, "version", lambda name: "0.5.0")
    await hass.config_entries.async_unload(entry.entry_id)
    assert await hass.config_entries.async_setup(entry.entry_id) is True
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, "wheel_too_old") is None


@pytest.mark.parametrize("version,ok", [("0.5.0", True), ("0.5.1", True), ("0.10.0", True), ("1.0.0", True),
                                        ("0.4.9", False), ("0.4.1", False), ("0.0.1", False)])
async def test_the_comparison_is_numeric_not_textual(hass, fake_transport, monkeypatch, version, ok):
    monkeypatch.setattr(portico_hub.importlib.metadata, "version", lambda name: version)
    entry = _observer_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is ok


async def test_an_unreadable_wheel_version_does_not_block_doors(hass, fake_transport, monkeypatch):
    """SPK-5: the metadata read can fail (no dist-info); the wheel import already proved it is installed."""
    def _missing(name):
        raise portico_hub.importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(portico_hub.importlib.metadata, "version", _missing)
    entry = _observer_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is True
    assert get_hub(hass).installed_wheel_version() is None
    assert ir.async_get(hass).async_get_issue(DOMAIN, "wheel_too_old") is None


async def test_the_installed_wheel_is_read_from_the_portico_transport_distribution(hass, monkeypatch):
    asked = []
    monkeypatch.setattr(portico_hub.importlib.metadata, "version", lambda name: asked.append(name) or "0.5.0")
    assert get_hub(hass).installed_wheel_version() == "0.5.0" and asked == ["portico-transport"]


async def test_diagnostics_show_the_hub_process_and_wheel_version(hass, fake_transport):
    observer, dahua = await _setup_two(hass)
    diag = await async_get_config_entry_diagnostics(hass, observer)
    assert diag["hub"] == {"process": "running", "doors_attached": 2, "restarted_once": False,
                           "wheel_version": WHEEL_PIN_VERSION,
                           "min_wheel_version": portico_hub.MIN_WHEEL_VERSION,
                           "socket": ".storage/portico_intercom/hub.sock"}


async def test_the_wheel_version_is_read_off_the_event_loop(hass, fake_transport, monkeypatch):
    """importlib.metadata.version lists site-packages; on the loop HA logs 'Detected blocking call to listdir'
    (seen live on HA 2026.10.2). The read must run in an executor thread, and its result still be used."""
    import asyncio
    import threading

    loop_thread = threading.current_thread()
    seen = []

    def _version(name):
        try:
            asyncio.get_running_loop()
            on_loop = True
        except RuntimeError:
            on_loop = False
        seen.append((threading.current_thread() is loop_thread, on_loop))
        return "0.5.0"

    monkeypatch.setattr(portico_hub.importlib.metadata, "version", _version)
    entry = _observer_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id) is True
    assert seen and all(not same_thread and not on_loop for same_thread, on_loop in seen)
    assert get_hub(hass).diagnostics()["wheel_version"] == "0.5.0"
