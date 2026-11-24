# ruff: noqa: F811 - test parameters named after the imported fixtures
"""gate_sip_server in REMOTE mode, the HA side (DEV-518 W8b): the entry's fields and guards, the add form and the Reconfigure form,
the hub connecting instead of spawning, availability = the connection, and a remote server beside a local door. Offline: the TCP client is replaced by a double (the real sockets are test_remote_link.py's), so nothing connects
anywhere and no process starts. No secret may appear in an entry's diagnostics or in a log line."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, ClassVar

import pytest
import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.portico.diagnostics import async_get_config_entry_diagnostics
from custom_components.portico.door import hub as hub_module
from custom_components.portico.door import remote as remote_module
from custom_components.portico.door.const import DOMAIN, ENTRY_TYPE, ENTRY_TYPE_DOOR
from custom_components.portico.door.drivers.gate_sip_server import normalize_remote
from custom_components.portico.door.hub import get_hub
from custom_components.portico.door.remote_cfg import check_remote, is_remote
from custom_components.portico.door.transport import TransportError
from custom_components.portico.endpoints import get_endpoint_registry
from custom_components.portico.provider import DOMAIN as UI_DOMAIN

from .driver_part import (DAHUA_HOST, FakeGoTransport, _SERVER_CAPS, _dahua_entry,  # noqa: F401
                          fake_transport)
from .test_door_through_the_ui_part import _custom_integrations  # noqa: F401 - fixture by name

pytestmark = pytest.mark.asyncio

TOKEN = "t0ken-0123456789-abcdefghij-XYZ"
DEVICE = "gate-sip-server:10.6.0.7:5060"
GOOD = {"remote_host": "10.6.0.7", "remote_port": 9100, "remote_token": TOKEN, "sip_host": "10.6.0.7", "sip_port": 5060,
        "confirm": True}


class FakeRemote(FakeGoTransport):
    """A TcpTransportClient double: connects unless told not to, configures like the container (adopts, CanDial set)."""

    made: ClassVar[list["FakeRemote"]] = []
    refuse: ClassVar[str | None] = None        # "refused" | "unauthorized"
    can_dial: ClassVar[bool] = False
    wheel_calls: ClassVar[int] = 0
    spawned: ClassVar[int] = 0

    def __init__(self, host: str, port: int, token: str) -> None:
        super().__init__(f"tcp://{host}:{port}")
        self.host, self.port, self.token = host, port, token
        self.closed = asyncio.Event()
        FakeRemote.made.append(self)

    async def connect(self) -> None:
        if FakeRemote.refuse == "refused":
            raise ConnectionRefusedError
        if FakeRemote.refuse == "unauthorized":
            raise TransportError("unauthorized", "bad token")

    async def close(self) -> None:
        self.closed.set()

    async def configure(self, driver, config):
        caps = await super().configure(driver, config)
        return {**caps, "CanDial": FakeRemote.can_dial}

    def drop(self) -> None:
        self.closed.set()


@pytest.fixture
def remote_env(monkeypatch):
    FakeRemote.made, FakeRemote.refuse, FakeRemote.can_dial = [], None, False
    monkeypatch.setattr(remote_module, "TcpTransportClient", FakeRemote)
    from functools import partial
    monkeypatch.setattr(hub_module, "RemoteLink", partial(remote_module.RemoteLink, backoff=(0.01, 0.02)))

    FakeRemote.spawned = 0
    real_exec = hub_module.asyncio.create_subprocess_exec

    async def _spy_exec(*a, **k):
        FakeRemote.spawned += 1
        return await real_exec(*a, **k)

    monkeypatch.setattr(hub_module.asyncio, "create_subprocess_exec", _spy_exec)

    FakeRemote.wheel_calls = 0
    real_ensure = hub_module.async_ensure_wheel

    async def _spy_wheel(*a, **k):
        FakeRemote.wheel_calls += 1
        return await real_ensure(*a, **k)

    monkeypatch.setattr(hub_module, "async_ensure_wheel", _spy_wheel)
    return FakeRemote


def _remote_entry(hass, **extra) -> MockConfigEntry:
    data = {ENTRY_TYPE: ENTRY_TYPE_DOOR, "driver": "gate_sip_server", "remote_host": "10.6.0.7", "remote_port": 9100,
            "remote_token": TOKEN, "sip_host": "10.6.0.7", "sip_port": 5060, "sip_listen": "10.6.0.7:5060",
            "device_id": DEVICE, "existing_camera_entity": "camera.gate", "open_relay_action": [], **extra}
    entry = MockConfigEntry(domain=UI_DOMAIN, title="Portico (gate_sip_server)", data=data, unique_id=f"gate_sip_server:{DEVICE}")
    entry.add_to_hass(hass)
    return entry


async def _until(hass, predicate, timeout=3.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not predicate():
        assert loop.time() < end, "condition not reached"
        await asyncio.sleep(0.01)
    await hass.async_block_till_done()


async def _load(hass, entry):
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


# ---- the fields and their guards, pure ---------------------------------------------------------------------------------

@pytest.mark.parametrize("host", ["10.6.0.7", "10.0.0.1", "172.16.0.5", "172.31.255.1", "192.168.1.9", "127.0.0.1", "::1"])
async def test_remote_host_private_and_loopback_accepted(host):
    assert check_remote(host, 9100, TOKEN) is None


@pytest.mark.parametrize("host, err", [
    ("8.8.8.8", "remote_host_not_private"), ("172.32.0.1", "remote_host_not_private"), ("172.15.0.1", "remote_host_not_private"),
    ("100.64.0.1", "remote_host_not_private"), ("169.254.1.1", "remote_host_not_private"), ("fd00::1", "remote_host_not_private"),
    ("192.169.0.1", "remote_host_not_private"), ("0.0.0.0", "invalid_remote_host"), ("gate.example", "invalid_remote_host"),
    ("", "invalid_remote_host"), (None, "invalid_remote_host"), (5, "invalid_remote_host"), ("10.6.0.7/24", "invalid_remote_host"),
])
async def test_remote_host_refused(host, err):
    assert check_remote(host, 9100, TOKEN) == ("remote_host", err)


@pytest.mark.parametrize("port, err", [(8091, "remote_port_is_gate_sip"), (8092, "remote_port_is_gate_sip"),
                                       (8095, "remote_port_is_gate_sip"), (0, "invalid_port"), (65536, "invalid_port"),
                                       (True, "invalid_port"), ("9100", "invalid_port"), (None, "invalid_port")])
async def test_remote_port_refused(port, err):
    assert check_remote("10.6.0.7", port, TOKEN) == ("remote_port", err)


async def test_remote_port_5060_and_40000_are_not_gate_sips_ipc_ports():
    assert check_remote("10.6.0.7", 9100, TOKEN) is None and check_remote("10.6.0.7", 1024, TOKEN) is None


@pytest.mark.parametrize("token, err", [("a" * 23, "remote_token_length"), ("", "remote_token_length"), (None, "remote_token_length"),
                                        ("a" * 257, "remote_token_length"), ("a" * 12 + " " + "b" * 12, "remote_token_chars"),
                                        ("a" * 24 + "\n", "remote_token_chars")])
async def test_remote_token_refused(token, err):
    assert check_remote("10.6.0.7", 9100, token) == ("remote_token", err)


async def test_remote_token_24_accepted():
    assert check_remote("10.6.0.7", 9100, "a" * 24) is None and check_remote("10.6.0.7", 9100, "a" * 256) is None


async def test_remote_normalize_shape_has_no_password_and_a_default_device_id():
    out = normalize_remote(GOOD)
    assert out == {"remote_host": "10.6.0.7", "remote_port": 9100, "remote_token": TOKEN, "sip_host": "10.6.0.7",
                   "sip_port": 5060, "sip_listen": "10.6.0.7:5060", "device_id": DEVICE}
    assert not any("password" in k for k in out) and "confirm" not in out
    on = normalize_remote({**GOOD, "dial_enabled": True, "ring_answered_entity": "input_boolean.gate_ring_answered",
                           "device_id": "front-gate"})
    assert on["dial_enabled"] is True and on["device_id"] == "front-gate"
    assert on["gate_sip_listen"] == {"ring_answered_entity": "input_boolean.gate_ring_answered"}


@pytest.mark.parametrize("patch, field, err", [
    ({"confirm": False}, "confirm", "confirm_required"), ({"remote_host": "8.8.8.8"}, "remote_host", "remote_host_not_private"),
    ({"remote_port": 8095}, "remote_port", "remote_port_is_gate_sip"), ({"remote_token": "short"}, "remote_token", "remote_token_length"),
    ({"sip_host": "gate.example"}, "sip_host", "invalid_sip_host"), ({"sip_port": 0}, "sip_port", "invalid_port"),
    ({"device_id": "has space"}, "device_id", "invalid_device_id"), ({"dial_enabled": "yes"}, "dial_enabled", "invalid_dial"),
    ({"ring_answered_entity": "light.x"}, "ring_answered_entity", "invalid_ring_entity"),
])
async def test_remote_normalize_refuses(patch, field, err):
    with pytest.raises(vol.Invalid) as e:
        normalize_remote({**GOOD, **patch})
    assert (str(e.value.path[0]), e.value.error_message) == (field, err)


async def test_remote_is_remote_only_with_a_host():
    assert is_remote({"driver": "gate_sip_server", "remote_host": "10.6.0.7"})
    assert not is_remote({"driver": "gate_sip_server", "sip_listen": "127.0.0.1:15060"})
    assert not is_remote({"driver": "dahua_sip", "remote_host": "10.6.0.7"})


# ---- the add form -----------------------------------------------------------------------------------------------------

async def _to_remote_form(hass):
    r = await hass.config_entries.flow.async_init(UI_DOMAIN, context={"source": "user"})
    assert "sip_server_remote" in r["menu_options"]
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"next_step_id": "sip_server_remote"})
    assert r["type"] is FlowResultType.FORM and r["step_id"] == "sip_server_remote"
    return r


async def test_remote_form_creates_the_entry_without_connecting(hass, remote_env):
    r = await _to_remote_form(hass)
    r = await hass.config_entries.flow.async_configure(r["flow_id"], dict(GOOD))
    assert r["step_id"] == "camera_config"
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"existing_camera_entity": "camera.gate"})
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {})
    assert r["type"] is FlowResultType.CREATE_ENTRY
    data = r["result"].data
    assert r["result"].unique_id == f"gate_sip_server:{DEVICE}"
    assert data["remote_host"] == "10.6.0.7" and data["remote_token"] == TOKEN and data["device_id"] == DEVICE
    assert not any("password" in k for k in data) and "confirm" not in data
    assert data[ENTRY_TYPE] == ENTRY_TYPE_DOOR and data["driver"] == "gate_sip_server"


@pytest.mark.parametrize("patch, field, err", [
    ({"confirm": False}, "confirm", "confirm_required"), ({"remote_host": "8.8.8.8"}, "remote_host", "remote_host_not_private"),
    ({"remote_port": 8091}, "remote_port", "remote_port_is_gate_sip"), ({"remote_token": "x" * 23}, "remote_token", "remote_token_length"),
])
async def test_remote_form_refuses_and_creates_nothing(hass, remote_env, patch, field, err):
    r = await _to_remote_form(hass)
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {**GOOD, **patch})
    assert r["type"] is FlowResultType.FORM and r["errors"] == {field: err}
    assert hass.config_entries.async_entries(UI_DOMAIN) == []
    assert FakeRemote.made == []                                # a form never connects


async def test_remote_form_same_device_aborts_and_same_container_is_refused(hass, remote_env):
    _remote_entry(hass)
    r = await _to_remote_form(hass)
    r = await hass.config_entries.flow.async_configure(r["flow_id"], dict(GOOD))
    assert r["type"] is FlowResultType.FORM and r["errors"] == {"remote_port": "remote_in_use_by_entry"}
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {**GOOD, "remote_port": 9101})
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "already_configured"        # the same device id


async def test_remote_form_coexists_with_another_door_entry(hass, remote_env):
    _dahua_entry(hass)
    r = await _to_remote_form(hass)
    r = await hass.config_entries.flow.async_configure(r["flow_id"], dict(GOOD))      # same host as gate-sip, port 5060: allowed
    assert r["step_id"] == "camera_config"


async def test_remote_texts_exist_in_both_files():
    root = Path(remote_module.__file__).parent.parent
    for name in ("strings.json", "translations/en.json"):
        cfg = json.loads((root / name).read_text(encoding="utf-8"))["config"]
        assert cfg["step"]["user"]["menu_options"]["sip_server_remote"]
        assert {"remote_host", "remote_port", "remote_token", "sip_host", "sip_port", "confirm"} <= set(cfg["step"]["sip_server_remote"]["data"])
        assert {"remote_host", "remote_port", "remote_token"} <= set(cfg["step"]["reconfigure_server"]["data"])
        assert {"invalid_remote_host", "remote_host_not_private", "remote_port_is_gate_sip", "remote_token_length",
                "remote_token_chars", "remote_in_use_by_entry", "invalid_device_id"} <= set(cfg["error"])


# ---- the hub connects instead of spawning -----------------------------------------------------------------------------

async def test_remote_door_connects_configures_the_adopted_device_and_spawns_nothing(hass, remote_env):
    entry = _remote_entry(hass)
    await _load(hass, entry)
    rt = entry.runtime_data
    assert entry.state is ConfigEntryState.LOADED and rt.remote
    await _until(hass, lambda: rt.transport_available)
    (c,) = FakeRemote.made
    assert (c.host, c.port, c.token) == ("10.6.0.7", 9100, TOKEN)
    assert c.configured == [("gate_sip_server", {"device_id": DEVICE})]            # no password, no token, nothing else
    assert rt.capabilities["HasRelay"] is False and rt.entities_available is True
    assert get_hub(hass)._process is None and get_hub(hass).client is None         # the hub's own process was never started
    assert FakeRemote.spawned == 0 and FakeRemote.wheel_calls == 0                                              # and the wheel was never looked at
    assert rt.client is c


async def test_remote_door_unreachable_container_does_not_fail_the_entry_and_comes_up_later(hass, remote_env):
    FakeRemote.refuse = "refused"
    entry = _remote_entry(hass)
    await _load(hass, entry)
    rt = entry.runtime_data
    assert entry.state is ConfigEntryState.LOADED
    await asyncio.sleep(0.1)
    assert rt.transport_available is False and rt.entities_available is False and rt.client is None
    FakeRemote.refuse = None
    await _until(hass, lambda: rt.transport_available)
    assert rt.entities_available is True


async def test_remote_door_availability_follows_the_connection_and_a_call_ends_with_it(hass, remote_env):
    entry = _remote_entry(hass)
    await _load(hass, entry)
    rt = entry.runtime_data
    await _until(hass, lambda: rt.transport_available)
    (first,) = FakeRemote.made
    first.ring("c1", device_id=DEVICE)
    await hass.async_block_till_done()
    assert rt.call_state.state == "ringing"
    FakeRemote.refuse = "refused"
    first.drop()
    await _until(hass, lambda: not rt.transport_available)
    assert rt.entities_available is False and rt.call_state.state == "idle"          # the ring does not outlive the link
    assert rt.capabilities is not None                                                # ... and is not replayed on reconnect
    FakeRemote.refuse = None
    await _until(hass, lambda: rt.transport_available and FakeRemote.made[-1] is not first)
    assert rt.call_state.state == "idle"


async def test_remote_door_wrong_token_stays_unavailable_and_the_entry_loads(hass, remote_env, caplog):
    caplog.set_level(logging.DEBUG)
    FakeRemote.refuse = "unauthorized"
    entry = _remote_entry(hass)
    await _load(hass, entry)
    await asyncio.sleep(0.1)
    assert entry.state is ConfigEntryState.LOADED and entry.runtime_data.transport_available is False
    assert TOKEN not in caplog.text


async def test_remote_door_unload_detaches_and_a_second_unload_path_is_clean(hass, remote_env):
    entry = _remote_entry(hass)
    await _load(hass, entry)
    await _until(hass, lambda: entry.runtime_data.transport_available)
    (c,) = FakeRemote.made
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert c.deconfigured == [DEVICE] and c.closed.is_set()                    # detach only; the container keeps its registration
    assert get_hub(hass).remote_link(DEVICE) is None
    assert await hass.config_entries.async_setup(entry.entry_id)               # a reload works
    await _until(hass, lambda: entry.runtime_data.transport_available)


async def test_remote_door_hand_edited_bad_data_is_refused_by_the_hub(hass, remote_env):
    entry = _remote_entry(hass, remote_host="8.8.8.8")
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert FakeRemote.made == [] and get_hub(hass).remote_link(DEVICE) is None


async def test_remote_door_hub_stop_closes_the_link_without_deconfigure(hass, remote_env):
    entry = _remote_entry(hass)
    await _load(hass, entry)
    await _until(hass, lambda: entry.runtime_data.transport_available)
    (c,) = FakeRemote.made
    await get_hub(hass).async_stop()
    assert c.closed.is_set() and c.deconfigured == []


# ---- another door and a remote server side by side --------------------------------------------------------------------

def _ep(hass, device_id):
    return next(e for e in get_endpoint_registry(hass).endpoints(kind="door") if e.device_id == device_id)


async def _both(hass):
    obs = _dahua_entry(hass)
    srv = _remote_entry(hass)
    assert await hass.config_entries.async_setup(obs.entry_id)
    await hass.async_block_till_done()
    assert await async_setup_component(hass, UI_DOMAIN, {})
    await hass.async_block_till_done()
    await _until(hass, lambda: srv.runtime_data.transport_available)
    return obs, srv, next(i for i in FakeGoTransport.instances if not isinstance(i, FakeRemote))


async def test_remote_and_a_local_door_coexist_in_one_hub_without_sharing_a_client(hass, fake_transport, remote_env):
    obs, srv, go = await _both(hass)
    assert len([i for i in fake_transport.instances if not isinstance(i, FakeRemote)]) == 1     # the hub's one process: the local door only
    assert [d for _, c in go.configured for d in [c["device_id"]]] == [DAHUA_HOST]
    assert obs.runtime_data.client is go and srv.runtime_data.client is FakeRemote.made[-1]
    FakeRemote.made[-1].ring("r1", device_id=DEVICE)
    go.ring("o1", device_id=DAHUA_HOST)
    await hass.async_block_till_done()
    assert srv.runtime_data.call_state.call_id == "r1" and obs.runtime_data.call_state.call_id == "o1"


async def test_remote_door_capabilities_changed_updates_can_dial(hass, remote_env):
    entry = _remote_entry(hass)
    await _load(hass, entry)
    await _until(hass, lambda: entry.runtime_data.transport_available)
    rt = entry.runtime_data
    assert rt.can_dial is False
    FakeRemote.made[-1].notify("capabilities_changed", {"device_id": DEVICE, "capabilities": {**_SERVER_CAPS, "CanDial": True}})
    assert rt.can_dial is True


# ---- diagnostics: no secret ---------------------------------------------------------------------------------------------

async def test_remote_diagnostics_redact_the_token_and_the_address_and_hold_the_link_state(hass, remote_env):
    entry = _remote_entry(hass)
    await _load(hass, entry)
    await _until(hass, lambda: entry.runtime_data.transport_available)
    diag = await async_get_config_entry_diagnostics(hass, entry)
    text = json.dumps(diag, default=str)
    data = diag["entry"]["data"]
    assert TOKEN not in text and data["remote_token"] == "**REDACTED**" and data["remote_host"].startswith("redacted-")
    assert "10.6.0.9" not in text and "9100" in text.replace('"remote_port": 9100', '9100')
    # the container's SIP address stays readable in `sip_listen` / `device_id`, exactly as a local server entry's does
    assert diag["remote"] == {"mode": "remote", "connected": True, "connects": 1, "last_error": None}


# ---- the Reconfigure form -----------------------------------------------------------------------------------------------

async def _reconfigure(hass, entry, user_input=None):
    r = await entry.start_reconfigure_flow(hass)
    return r if user_input is None else await hass.config_entries.flow.async_configure(r["flow_id"], user_input)


async def test_remote_reconfigure_shows_the_remote_fields_with_the_token_as_stars(hass, remote_env):
    entry = _remote_entry(hass)
    form = await _reconfigure(hass, entry)
    assert form["step_id"] == "reconfigure_server"
    assert {str(k) for k in form["data_schema"].schema} == {"remote_host", "remote_port", "remote_token", "dial_enabled",
                                                           "ring_answered_entity", "open_relay_action", "house_calls"}
    token_key = next(k for k in form["data_schema"].schema if str(k) == "remote_token")
    assert token_key.description["suggested_value"] != TOKEN and TOKEN not in json.dumps(str(form["data_schema"].schema))


async def test_remote_reconfigure_keeps_the_token_on_stars_and_replaces_it_when_typed(hass, remote_env):
    from custom_components.portico.enrollment_flow import SECRET_MASK
    entry = _remote_entry(hass)
    r = await _reconfigure(hass, entry, {"remote_host": "10.6.0.9", "remote_port": 9200.0, "remote_token": SECRET_MASK,
                                         "dial_enabled": True})
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "reconfigure_successful"
    assert entry.data["remote_host"] == "10.6.0.9" and entry.data["remote_port"] == 9200 and entry.data["remote_token"] == TOKEN
    assert entry.data["dial_enabled"] is True and entry.data["device_id"] == DEVICE
    await hass.async_block_till_done()
    new = "n" * 30
    r = await _reconfigure(hass, entry, {"remote_host": "10.6.0.9", "remote_port": 9200, "remote_token": new})
    assert entry.data["remote_token"] == new
    await hass.async_block_till_done()
    for e in hass.config_entries.async_entries(UI_DOMAIN):
        await hass.config_entries.async_unload(e.entry_id)


@pytest.mark.parametrize("patch, field, err", [
    ({"remote_host": "8.8.8.8"}, "remote_host", "remote_host_not_private"), ({"remote_port": 8092}, "remote_port", "remote_port_is_gate_sip"),
    ({"remote_token": "short"}, "remote_token", "remote_token_length"), ({"remote_token": "ab•cd"}, "remote_token", "secret_mask_edited"),
    ({"remote_port": 9100.5}, "remote_port", "invalid_port"),
])
async def test_remote_reconfigure_refuses_and_changes_nothing(hass, remote_env, patch, field, err):
    entry = _remote_entry(hass)
    before = dict(entry.data)
    r = await _reconfigure(hass, entry, {"remote_host": "10.6.0.7", "remote_port": 9100, **patch})
    assert r["type"] is FlowResultType.FORM and r["errors"] == {field: err}
    assert dict(entry.data) == before
