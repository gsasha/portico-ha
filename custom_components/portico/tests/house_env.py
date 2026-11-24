"""A scripted house for the house-call tests (M7 3a): phones with `receive` on or off, a kitchen screen, a fake door, fake
websocket connections, and the registry. No media, no network, no HA user accounts needed for the registry-level tests
(an endpoint's HA user is a plain id)."""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from ..config_flow import DOMAIN
from ..content import get_content_store
from ..house import HouseCallRegistry, async_setup_house
from ..phone_entry import CONF_HOUSE_RECEIVE, PhoneRecord
from .fake_provider import FakeProvider
from .test_endpoints import _provide

KITCHEN_USER = "u-kitchen"


class FakeConn:
    """Stands in for a `websocket_api.ActiveConnection`: a user and the events sent to it."""

    def __init__(self, user_id: str, *, admin: bool = False, msg_id: int = 1) -> None:
        self.user = SimpleNamespace(id=user_id, is_admin=admin, name=user_id)
        self.msg_id = msg_id
        self.events: list[dict] = []
        self.subscriptions: dict[int, Any] = {}      # like ActiveConnection.subscriptions: msg id -> remover

    def send_event(self, msg_id: int, event: dict) -> None:
        assert msg_id == self.msg_id
        self.events.append(event)

    def house(self) -> list[tuple]:
        """The `house_call` events as (state, to, reason)."""
        return [(e["state"], e["to"], e.get("reason")) for e in self.events if e["type"] == "house_call"]

    def types(self) -> list[str]:
        return [e["type"] for e in self.events]

    def clear(self) -> None:
        self.events.clear()


@dataclass
class House:
    hass: Any
    registry: HouseCallRegistry
    door: FakeProvider
    conns: dict[str, FakeConn] = field(default_factory=dict)
    handles: dict[str, Any] = field(default_factory=dict)
    users: dict[str, str] = field(default_factory=dict)        # endpoint id -> HA user id
    _start: Any = field(default_factory=dt_util.utcnow)
    _elapsed: float = 0.0

    def online(self, endpoint: str, *, dnd: bool | None = None) -> FakeConn:
        """The endpoint's user opens `calls/subscribe` (HC-3: that is what makes it online)."""
        conn = FakeConn(self.users[endpoint])
        handle = self.registry.attach(conn, conn.msg_id)
        assert handle is not None
        self.conns[endpoint] = conn
        self.handles[endpoint] = handle
        if dnd is not None:
            assert self.registry.set_dnd(conn, dnd)
        return conn

    def offline(self, endpoint: str) -> None:
        self.handles.pop(endpoint).detach()
        self.conns.pop(endpoint)

    def user(self, endpoint: str) -> str:
        return self.users[endpoint]

    def place(self, caller: str, target_type: str, target_id: str, **kw):
        return self.registry.place_call(caller_user_id=self.users[caller], target_type=target_type,
                                        target_id=target_id, caller_endpoint=caller, **kw)

    def dial(self, caller: str, target: str):
        return self.place(caller, "dashboard" if target == "kitchen" else "phone", target)

    def everyone(self, caller: str = "kitchen"):
        return self.place(caller, "group", "everyone")

    async def advance(self, seconds: float) -> None:
        """Move the clock `seconds` on from where the last call left it (cumulative, like real time)."""
        self._elapsed += seconds
        async_fire_time_changed(self.hass, self._start + timedelta(seconds=self._elapsed))
        await self.hass.async_block_till_done()


def phone_entry(hass, user_id: str, enroll_id: str, name: str, *, receive: bool | None = True, doors=None):
    record = PhoneRecord(ha_user_id=user_id, name=name, enroll_id=enroll_id, enrolled_at="2026-10-06T00:00:00+00:00")
    options: dict[str, Any] = {}
    if receive is not None:
        options[CONF_HOUSE_RECEIVE] = receive
    if doors is not None:
        options["doors"] = doors
    entry = MockConfigEntry(domain=DOMAIN, title=name, data=record.to_data(), options=options,
                            unique_id=f"phone:{enroll_id}")
    entry.add_to_hass(hass)
    return entry


async def build_house(hass, hass_storage, *, phones=(("alice", True), ("dina", True)), kitchen=True,
                      door="gate-1") -> House:
    """Phones `(endpoint id, receive)`, the screen `kitchen` and one fake door, with the registry switched on."""
    users: dict[str, str] = {}
    for endpoint, receive in phones:
        users[endpoint] = f"u-{endpoint}"
        phone_entry(hass, users[endpoint], endpoint, endpoint.capitalize(), receive=receive)
    if kitchen:
        users["kitchen"] = KITCHEN_USER
        hass_storage["portico.content"] = {
            "version": 1, "minor_version": 1, "key": "portico.content",
            "data": {"schema_version": 3, "template": {"pages": [], "rules": [], "roster": ["*"]}, "overrides": {},
                     "dashboards": {"kitchen": {"name": "Kitchen", "ha_user_id": KITCHEN_USER, "room": "Kitchen",
                                                "cameras": []}}, "endpoints": {}}}
        await get_content_store(hass).async_ensure_loaded()
    fake = _provide(hass, "fake", door)
    counter = itertools.count(1)
    registry = async_setup_house(hass, enabled=True, id_factory=lambda: f"hso-{next(counter)}")
    assert registry is not None
    return House(hass=hass, registry=registry, door=fake, users=users)
