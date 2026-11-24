"""UI split P4 and P5: `portico/door_status` and `portico/door_settings/get | set` (door_commands.py) over a real HA websocket,
with a real door entry (the transport faked, driver_part.py).

HA-harness session. What is pinned:
* every command is administrator-only and a refused caller changes nothing;
* the status is a read, shows what the door and the hub report, and never carries a token, a password or a URL;
* the former gate-observer settings are still published as null and refused as `not_for_this_driver` (DEV-518 C5: the observer
  door is retired), the deprecated `away_media` is accepted and ignored, a stale revision writes nothing.
"""
from __future__ import annotations

import json

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.setup import async_setup_component

from custom_components.portico import door_commands
from custom_components.portico.config_flow import DOMAIN

from .driver_part import FakeGoTransport, _dahua_entry, fake_transport  # noqa: F401 - fixture by name
from .test_push_registry import _custom_integrations, _phone_user  # noqa: F401 - autouse fixture by name

HELPER = "input_boolean.gate_ring_answered"


class Ws:
    def __init__(self, client) -> None:
        self.client = client
        self.n = 0

    async def send(self, body: dict) -> dict:
        self.n += 1
        await self.client.send_json({"id": self.n, **body})
        return await self.client.receive_json()


def ok(frame):
    assert frame["success"], frame
    return frame["result"]


def err(frame):
    assert frame["success"] is False, frame
    return frame["error"]


STATUS = {"type": "portico/door_status"}
GET = {"type": "portico/door_settings/get"}


def put(entry, revision, patch, **extra):
    return {"type": "portico/door_settings/set", "entry_id": entry.entry_id, "revision": revision, "patch": patch, **extra}


@pytest.fixture
def helper(hass):
    hass.states.async_set(HELPER, "off", {"friendly_name": "Gate ring answered"})
    return HELPER


async def make_door(hass):
    """A loaded dahua_sip door."""
    entry = _dahua_entry(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert await async_setup_component(hass, DOMAIN, {})
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


@pytest.fixture
async def ws(hass, hass_ws_client):
    # the websocket client needs the integration set up; a door is made by each test
    assert await async_setup_component(hass, DOMAIN, {})
    return Ws(await hass_ws_client(hass))


async def revision_of(ws, entry):
    (door,) = [d for d in ok(await ws.send(GET))["doors"] if d["entry_id"] == entry.entry_id]
    return door["revision"]


def configures(entry):
    """What the transport has been told for this door, in order (the `configure` payloads)."""
    return [config for transport in FakeGoTransport.instances for (_, config) in transport.configured]


# ---- who may ask -------------------------------------------------------------------------------------------------


async def test_a_non_admin_is_refused_every_command_and_nothing_changes(hass, hass_ws_client, ws, fake_transport, helper):
    entry = await make_door(hass)
    revision = await revision_of(ws, entry)
    before = (dict(entry.data), dict(entry.options))
    callers = {}
    for who in ("plain", "enrolled"):
        _user, token = await _phone_user(hass, name=who)
        callers[who] = Ws(await hass_ws_client(hass, access_token=token))
    bodies = (STATUS, GET, put(entry, revision, {"away_media": True}),
              put(entry, revision, {"claims_enabled": True, "ring_answered_entity": HELPER}, confirm=True),
              put(entry, revision, {"away_media": True}, validate_only=True))
    for who, client in callers.items():
        for body in bodies:
            assert err(await client.send(body))["code"] == "unauthorized", (who, body["type"])
    assert (dict(entry.data), dict(entry.options)) == before
    assert len(configures(entry)) == 1, "and reloads nothing"


# ---- P4: the status ----------------------------------------------------------------------------------------------


async def test_the_status_is_a_card_per_door_with_what_the_door_and_the_hub_report(hass, ws, fake_transport):
    entry = await make_door(hass)
    reply = ok(await ws.send(STATUS))
    (door,) = reply["doors"]
    assert door["entry_id"] == entry.entry_id and door["driver"] == "dahua_sip" and door["loaded"] is True
    assert door["connected"] is True and door["call_state"] == "idle" and door["outbound_state"] == "idle"
    assert door["settings"] is None and door["gate_sip"] is None and door["listen_state"] is None
    assert reply["hub"]["doors_attached"] == 1 and "as_of" in reply
    assert ok(await ws.send(STATUS))["doors"][0]["call_state"] == "idle", "a read changes nothing"


async def test_the_status_never_carries_a_secret_or_an_address(hass, ws, fake_transport):
    await make_door(hass)
    text = json.dumps(ok(await ws.send(STATUS)))
    for needle in ("password", "token", "http://", "station.example"):
        assert needle not in text, needle


async def test_a_door_that_is_not_loaded_is_listed_and_says_so(hass, ws, fake_transport):
    entry = _dahua_entry(hass)            # added, never set up
    (door,) = ok(await ws.send(STATUS))["doors"]
    assert door["entry_id"] == entry.entry_id and door["loaded"] is False and door["connected"] is False
    assert door["entry_state"] == "not_loaded" and door["call_state"] is None and door["listen_state"] is None
    (settings,) = ok(await ws.send(GET))["doors"]
    assert settings["loaded"] is False and settings["revision"]


# ---- P5: the settings --------------------------------------------------------------------------------------------


async def test_a_door_has_only_the_deprecated_away_audio_setting_and_the_listen_part_is_null(hass, ws, fake_transport, helper):
    entry = await make_door(hass)
    reply = ok(await ws.send(GET))
    (door,) = reply["doors"]
    assert door["listen"] is None and door["away_media"] is True and door["name"] == entry.title and len(door["revision"]) == 16
    assert reply["helpers"] == [{"entity_id": HELPER, "name": "Gate ring answered"}]
    assert reply["effects"] == {"away_media": "live", "listen_enabled": "reload", "audio_port": "reload", "claims_enabled": "reload",
                                "ring_answered_entity": "reload", "dial_enabled": "reload"}
    assert ok(await ws.send(GET))["doors"][0]["revision"] == door["revision"], "a read changes nothing"


async def test_away_audio_is_accepted_and_ignored_and_changes_nothing(hass, ws, fake_transport):
    entry = await make_door(hass)
    revision = await revision_of(ws, entry)
    before = (dict(entry.data), dict(entry.options))
    for flag in (True, False):
        reply = ok(await ws.send(put(entry, revision, {"away_media": flag})))
        assert reply["result"] == "unchanged" and reply["reloaded"] is False and reply["changes"] == []
    assert ok(await ws.send(put(entry, revision, {"away_media": False}, validate_only=True)))["result"] == "valid"
    assert (dict(entry.data), dict(entry.options)) == before
    assert len(configures(entry)) == 1


@pytest.mark.parametrize("patch", [{"listen_enabled": True}, {"claims_enabled": True}, {"dial_enabled": True},
                                   {"house_calls": True}, {"audio_port": 9000}, {"ring_answered_entity": HELPER}])
async def test_a_former_observer_setting_is_not_for_this_driver_and_writes_nothing(hass, ws, fake_transport, helper, patch):
    entry = await make_door(hass)
    revision = await revision_of(ws, entry)
    before = (dict(entry.data), dict(entry.options))
    assert err(await ws.send(put(entry, revision, patch, confirm=True)))["code"] == "not_for_this_driver"
    assert (dict(entry.data), dict(entry.options)) == before
    assert len(configures(entry)) == 1, "and reloads nothing"


async def test_a_bad_patch_is_refused_before_anything_else(hass, ws, fake_transport):
    entry = await make_door(hass)
    revision = await revision_of(ws, entry)
    for patch in ({}, {"peer_token": "x"}, {"unknown": True}, {"listen_enabled": "yes"}, {"audio_port": True},
                  {"audio_port": 70000}, {"ring_answered_entity": "light.kitchen"}):
        assert err(await ws.send(put(entry, revision, patch)))["code"] == "bad_patch", patch
    assert err(await ws.send(put(entry, revision, {"away_media": True} | {"x": 1})))["code"] == "bad_patch"
    assert err(await ws.send({"type": "portico/door_settings/set", "entry_id": "no-such-door", "revision": "r",
                              "patch": {"away_media": True}}))["code"] == "unknown_door"


async def test_a_stale_revision_writes_nothing(hass, ws, fake_transport):
    entry = await make_door(hass)
    revision = await revision_of(ws, entry)
    hass.config_entries.async_update_entry(entry, options={**entry.options, "marker": 1})
    assert err(await ws.send(put(entry, revision, {"away_media": True})))["code"] == "stale_revision"
    assert ok(await ws.send(put(entry, await revision_of(ws, entry), {"away_media": True})))["result"] == "unchanged"


def test_every_portico_command_the_settings_module_names_is_a_command_the_server_registers():
    """The panel module's `portico/...` strings are only text to Python: this reads the file and compares them with the
    table, so a renamed command cannot leave the page calling nothing."""
    import re
    from pathlib import Path

    from custom_components.portico import auth

    source = (Path(door_commands.__file__).parent / "www" / "portico-settings.js").read_text(encoding="utf-8")
    named = set(re.findall(r"portico/[a-z_]+(?:/[a-z_]+)?", source))
    # the door_settings commands stay on the server for the wire contract; the page stopped calling them with the observer's controls
    assert named == {"portico/door_status", "portico/ui_settings/get", "portico/ui_settings/set"}
    for command in named:
        assert auth.COMMANDS[command] == frozenset({auth.ADMIN}), command


def test_every_error_code_the_server_can_send_has_a_text_in_the_page():
    """The page words each refusal itself (the server's text names no menu): a code added on the server without a text there
    would show the server's English instead."""
    import re
    from pathlib import Path

    from custom_components.portico import ui_settings

    source = (Path(door_commands.__file__).parent / "www" / "portico-settings.js").read_text(encoding="utf-8")
    block = source[source.index("export const ERROR_TEXT = {"):]
    block = block[:block.index("\n};")]
    texts = set(re.findall(r"^  (\w+):", block, re.M))
    codes = set(ui_settings._MESSAGES)
    assert codes <= texts, sorted(codes - texts)
