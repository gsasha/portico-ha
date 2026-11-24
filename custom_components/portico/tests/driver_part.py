"""The websocket/content/push tests stand a REAL door side up beside them.

A door is a `portico` config entry with `entry_type: "door"` whose runtime is the door side's own `DoorRuntime` on its
own hub, the transport binary and the IPC socket faked exactly as the door tests fake them (a copy of
`test_observer.fake_transport`), and the provider it registers is the door side's `TransportProvider` handed to the
`ProviderRegistry`, the way `async_setup` does it at runtime. What a test asserts is what the registry's consumers do
with that provider: the websocket, the push sender, content.

Phones are the phone entries' (STATE-01): a test that needs an enrolled phone puts its row on a `portico` entry (the
push entry, or `phones_host` where a test has none), never on a door.
"""
from __future__ import annotations

import asyncio
import sys
from types import ModuleType
from typing import Any, ClassVar
from unittest.mock import AsyncMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .. import provider as ui_provider
from custom_components.portico.door import hub as driver_hub
from custom_components.portico.door.call_state import CallStateTracker
from custom_components.portico.door.const import DRIVER_ID, ENTRY_TYPE, ENTRY_TYPE_DOOR
from custom_components.portico.door.provider_impl import (
    DATA_TRANSPORT_PROVIDER,
    TransportProvider,
    get_provider,
)
from custom_components.portico.door.provider_impl import install_provider as _driver_install_provider
from custom_components.portico.door.door import DoorRuntime
from custom_components.portico.door.transport import TransportClient, TransportError
from custom_components.portico.door.wheel import WHEEL_PIN_VERSION

DRIVER_DOMAIN = DRIVER_ID       # the door PROVIDER id (`portico_intercom`, persisted in endpoint ids), not a domain
UI_DOMAIN = "portico"           # the one HA domain: door entries live in it too

GATE_SIP_URL = "http://gate-sip.example:8091"
DAHUA_HOST = "station.example"

__all__ = ["CallStateTracker", "DATA_TRANSPORT_PROVIDER", "DRIVER_DOMAIN", "FakeGoTransport", "GATE_SIP_URL", "DoorRuntime",
           "TransportClient", "TransportError", "TransportProvider", "get_provider", "install_provider", "phones_host", "_add_device",
           "_dahua_entry", "_observer_entry", "_server_entry", "SERVER_DEVICE_ID", "fake_transport"]

_OBSERVER_CAPS = {"InboundAudio": 3, "OutboundAudio": 3, "DirectCapable": False,
                  "HasRelay": False, "RTSPTemplate": "", "ReportsAvailability": True}
_SERVER_CAPS = {"InboundAudio": 0, "OutboundAudio": 0, "DirectCapable": False,
                "HasRelay": False, "RTSPTemplate": "", "ReportsAvailability": False}
_DAHUA_CAPS = {"InboundAudio": 0, "OutboundAudio": 0, "DirectCapable": True,
               "HasRelay": True, "RTSPTemplate": "", "ReportsAvailability": False}


class FakeGoTransport:
    """Stands in for TransportClient + the Go binary behind it (a copy of the driver part's test double)."""

    instances: ClassVar[list[FakeGoTransport]] = []
    report_available_on_configure: ClassVar[bool] = True

    def __init__(self, socket_path: str = "") -> None:
        self.socket_path = socket_path
        self.handlers: list = []
        self.configured: list[tuple[str, dict]] = []
        self.registry_ids: list[str] = []
        self.deconfigured: list[str] = []
        self.answers: list[tuple[str, str, int]] = []      # IPC `answer` requests (the legacy, answers-at-once verb)
        self.calls: dict[str, str] = {}
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
        elif driver == "gate_sip_server":
            device_id = config["device_id"]
        else:
            device_id = config.get("host") or config.get("nvr_host")
        if device_id in self.registry_ids:
            raise TransportError("already_configured", device_id)
        self.registry_ids.append(device_id)
        if driver == "gate_sip_observer":
            if self.report_available_on_configure:
                self.availability(True, device_id=device_id)
            return dict(_OBSERVER_CAPS)
        if driver == "gate_sip_server":
            return dict(_SERVER_CAPS)     # CanDial arrives later, by `capabilities_changed` (the station registering)
        return dict(_DAHUA_CAPS)

    async def answer(self, device_id: str, host: str, port: int) -> dict[str, Any]:
        self.answers.append((device_id, host, port))
        return {"ok": True}

    async def deconfigure(self, device_id: str) -> dict[str, Any]:
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
        stamp = self.calls.get(call_id, "") if device_id is None else device_id
        self.notify("call_state_changed", {"call_id": call_id, "device_id": stamp, "state": state})

    def availability(self, available: bool, reason: str = "", device_id: str | None = None) -> None:
        self.notify("driver_availability",
                    {"device_id": device_id or self.registry_id, "available": available, "reason": reason})

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
    """Replace the wheel, the subprocess and the IPC client of the driver part's runtime — nothing else."""
    FakeGoTransport.instances = []
    FakeGoTransport.report_available_on_configure = True
    wheel = ModuleType("portico_transport")
    wheel.binary_path = lambda: "/nonexistent/portico-transport"
    monkeypatch.setitem(sys.modules, "portico_transport", wheel)

    async def _fake_exec(*args, **kwargs):
        return _FakeProcess()

    monkeypatch.setattr(driver_hub.asyncio, "create_subprocess_exec", _fake_exec)
    monkeypatch.setattr(driver_hub, "TransportClient", FakeGoTransport)
    monkeypatch.setattr(driver_hub.importlib.metadata, "version", lambda name: WHEEL_PIN_VERSION)
    return FakeGoTransport


def install_provider(hass) -> TransportProvider:
    """What the two parts' domain setups and `portico.py` do between them: this part's registry exists, and the
    driver part's one provider is registered with it. Idempotent (the registry registers a domain once)."""
    data = hass.data.setdefault(UI_DOMAIN, {})
    registry = data.get(ui_provider.DATA_PROVIDERS)
    if registry is None:
        registry = data[ui_provider.DATA_PROVIDERS] = ui_provider.ProviderRegistry(hass)
    provider = _driver_install_provider(hass)
    if registry.provider(provider.domain) is not provider:
        registry.register(provider)
    return provider


def phones_host(hass, rows: list[dict]) -> MockConfigEntry:
    """A `portico` entry that only holds enrolled-phone rows, for a test with no push entry. It is never set up
    here (these tests do not load the `portico` domain); `_phones()` reads every entry of the domain."""
    entry = MockConfigEntry(domain=UI_DOMAIN, title="phones", data={"enrolled_phones": rows}, unique_id="phones-host")
    entry.add_to_hass(hass)
    return entry


def _observer_entry(hass, **extra) -> MockConfigEntry:
    """A gate_sip_observer door of the driver part. `enrolled_phones` is refused: phones are not a door's."""
    assert "enrolled_phones" not in extra, "phones are the UI part's: pass them to the push entry or phones_host"
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
    entry = MockConfigEntry(domain=UI_DOMAIN, data=data, unique_id=f"gate_sip_observer:{data['device_id']}")
    entry.add_to_hass(hass)
    return entry


SERVER_DEVICE_ID = "gate-sip-server:127.0.0.1:15060"


def _server_entry(hass, **extra) -> MockConfigEntry:
    """A gate_sip_server door (DEV-518 W2/W5), data as `normalize` writes it plus the camera step's entity."""
    data = {
        ENTRY_TYPE: ENTRY_TYPE_DOOR,
        "driver": "gate_sip_server",
        "sip_host": "127.0.0.1", "sip_port": 15060, "sip_listen": "127.0.0.1:15060", "rtp_port": 45000,
        "password": "12345678", "user": "8001", "realm": "VDP", "allow_non_loopback": False, "allow_from": [],
        "device_id": SERVER_DEVICE_ID,
        "existing_camera_entity": "camera.gate",
        "open_relay_action": [],
        **extra,
    }
    entry = MockConfigEntry(domain=UI_DOMAIN, title="Portico (gate_sip_server)", data=data,
                            unique_id=f"gate_sip_server:{data['device_id']}")
    entry.add_to_hass(hass)
    return entry


def _dahua_entry(hass) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=UI_DOMAIN,
        data={ENTRY_TYPE: ENTRY_TYPE_DOOR, "driver": "dahua_sip", "host": DAHUA_HOST, "sip_user": "9901#0",
              "sip_password": "x", "rtsp_username": "u", "rtsp_password": "p",
              "existing_camera_entity": "camera.gate", "open_relay_action": []},
        unique_id=f"dahua_sip:{DAHUA_HOST}",
    )
    entry.add_to_hass(hass)
    return entry


class StubHub:
    """The part of the hub a hand-built DoorRuntime touches (a mocked client, no process): `client`, and the
    call-ownership hook the provider calls after a `dial`. The real hub is exercised by test_hub.py."""

    def __init__(self, client=None) -> None:
        self.client = client
        self.noted: list[tuple[str, str]] = []

    def note_call(self, call_id: str, device_id: str) -> None:
        self.noted.append((call_id, device_id))



def _add_device(hass, *, device_id="127.0.0.1", driver="dahua_sip", capabilities=None,
                enrolled_phones=None, client=None, title="Mock Title", attach=True):
    """A door whose runtime is built by hand (a mocked client, no process), attached to the driver part's provider
    and registered with this part's registry. `enrolled_phones` become a `phones_host` entry."""
    entry = MockConfigEntry(
        domain=UI_DOMAIN,
        title=title,
        data={
            ENTRY_TYPE: ENTRY_TYPE_DOOR, "driver": driver, "host": device_id,
            "sip_user": "9999#0", "sip_password": "x",
            "rtsp_username": "u", "rtsp_password": "p",
        },
        unique_id=f"{driver}:{device_id}",
    )
    entry.add_to_hass(hass)
    runtime = DoorRuntime(hass, entry, StubHub())
    runtime.capabilities = capabilities or {"DirectCapable": True, "HasRelay": True}
    runtime._hub.client = client or AsyncMock()
    runtime.call_state = CallStateTracker(device_id)
    entry.runtime_data = runtime
    if enrolled_phones:
        existing = [e for e in hass.config_entries.async_entries(UI_DOMAIN) if e.unique_id == "phones-host"]
        if existing:
            hass.config_entries.async_update_entry(
                existing[0], data={"enrolled_phones": existing[0].data["enrolled_phones"] + list(enrolled_phones)})
        else:
            phones_host(hass, list(enrolled_phones))
    if attach:      # what async_setup_entry does once the transport is up; False for a test that supplies the door itself
        install_provider(hass).attach(entry)
    return entry, runtime
