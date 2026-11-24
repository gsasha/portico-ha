"""UI split P6: `portico/ui_settings/get | set` (ui_settings.py) over a real HA websocket.

HA-harness session. What is pinned: administrators only; the four settings are the Push ring notification dialog's own options, written to the
keys the push manager reads and applied at once; everything but the ring window needs `confirm: true` (checked in the handler);
a missing option reads as the dialog reads it; a test phone must be a registered phone; other options and house calls are not
touched; no reply carries an exception's text.
"""
from __future__ import annotations

import json
import logging

import pytest
from homeassistant.setup import async_setup_component

from custom_components.portico import ui_settings
from custom_components.portico.config_flow import DOMAIN
from custom_components.portico.house import OPTION_HOUSE_CALLS
from custom_components.portico.push import const

from .test_push_registry import REG, _custom_integrations, _manager, _phone_user, _push_entry, google  # noqa: F401 - fixtures by name


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


GET = {"type": "portico/ui_settings/get"}


def put(patch, **extra):
    return {"type": "portico/ui_settings/set", "patch": patch, **extra}


@pytest.fixture(autouse=True)
def _an_owner_exists(hass_owner_user):
    """A real install has its owner before any phone."""


@pytest.fixture
async def running(hass, hass_storage, google, hass_ws_client):
    """A set-up settings entry (its manager runs) and an administrator's websocket."""
    entry = _push_entry(hass, hass_storage, google, enabled=True, ring_window=45, new_phones_enabled=False, test_phones=[])
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, Ws(await hass_ws_client(hass))


async def register(hass, name="Pixel", installation="inst-1", user="u1"):
    row = await _manager(hass).registry.async_register(
        ha_user_id=user, installation_id=installation, kind="fid", registration=REG, package=const.PORTICO_PACKAGE,
        fcm_project_id="portico-test", app_version="1", name=name)
    return row["reg_id"]


async def test_the_settings_read_as_the_dialog_reads_them(hass, running):
    entry, ws = running
    reg_id = await register(hass)
    reply = ok(await ws.send(GET))
    assert reply == {
        "push_entry": True, "running": True, "ring_enabled": True, "ring_window": 45, "ring_window_min": 10, "ring_window_max": 120,
        "new_phones_enabled": False, "test_phones": [],
        "phones": [{"reg_id": reg_id, "name": "Pixel", "away_ring": False}],
    }
    assert ok(await ws.send(GET)) == reply, "a read changes nothing"


async def test_an_entry_from_before_the_options_reads_as_the_dialog_reads_it(hass, hass_ws_client):
    from .test_enrollment_flow import _push_entry as bare_entry

    entry = bare_entry(hass, firebase=None)             # no options at all, and not set up
    assert await async_setup_component(hass, DOMAIN, {})
    ws = Ws(await hass_ws_client(hass))
    reply = ok(await ws.send(GET))
    assert (reply["ring_enabled"], reply["ring_window"], reply["new_phones_enabled"], reply["test_phones"]) == (True, 45, False, [])
    assert (reply["push_entry"], reply["running"], reply["phones"]) == (True, False, [])
    assert err(await ws.send(put({"test_phones": ["x"]}, confirm=True)))["code"] == "push_not_loaded"
    assert dict(entry.options) == {}
    ok(await ws.send(put({"ring_window": 60})))
    assert dict(entry.options) == {"ring_window": 60}, "the other settings do not need the push ring notification to be running"


async def test_everything_but_the_ring_window_needs_the_servers_own_confirmation(hass, running):
    entry, ws = running
    reg_id = await register(hass)
    before = dict(entry.options)
    for patch in ({"ring_enabled": False}, {"new_phones_enabled": True}, {"test_phones": [reg_id]},
                  {"ring_window": 60, "ring_enabled": False}):
        for extra in ({}, {"confirm": False}):
            assert err(await ws.send(put(patch, **extra)))["code"] == "confirmation_required", (patch, extra)
    assert dict(entry.options) == before
    reply = ok(await ws.send(put({"ring_window": 60})))                    # no confirmation: nobody is left un-rung by a number
    assert (reply["result"], reply["changes"]) == ("applied", ["ring_window"])
    assert entry.options["ring_window"] == 60


async def test_a_change_is_written_to_the_keys_the_manager_reads_and_applies_at_once(hass, running):
    entry, ws = running
    reg_id = await register(hass)
    manager = _manager(hass)
    reply = ok(await ws.send(put({"ring_enabled": False, "ring_window": 90, "new_phones_enabled": True, "test_phones": [reg_id]}, confirm=True)))
    assert reply["result"] == "applied" and reply["changes"] == ["new_phones_enabled", "ring_enabled", "ring_window", "test_phones"]
    assert dict(entry.options) == {"enabled": False, "ring_window": 90, "new_phones_enabled": True, "test_phones": [reg_id]}
    assert (manager.enabled, manager.ring_window, manager.new_phones_enabled, manager.test_allow_list) == (False, 90, True, [reg_id])
    assert reply["settings"]["ring_enabled"] is False and reply["settings"]["test_phones"] == [reg_id]
    assert ok(await ws.send(GET)) == reply["settings"]
    ok(await ws.send(put({"test_phones": []}, confirm=True)))                 # empty: back to every phone
    assert entry.options["test_phones"] == [] and manager.test_allow_list == []


async def test_other_options_and_house_calls_are_left_alone(hass, running):
    entry, ws = running
    hass.config_entries.async_update_entry(entry, options={**entry.options, OPTION_HOUSE_CALLS: True, "somebody_elses": 1})
    ok(await ws.send(put({"ring_enabled": False}, confirm=True)))
    assert entry.options[OPTION_HOUSE_CALLS] is True and entry.options["somebody_elses"] == 1
    assert "house_calls" not in json.dumps(ok(await ws.send(GET)))
    hass.config_entries.async_update_entry(entry, options={k: v for k, v in entry.options.items() if k != OPTION_HOUSE_CALLS})
    ok(await ws.send(put({"ring_enabled": True}, confirm=True)))
    assert OPTION_HOUSE_CALLS not in entry.options, "this tab never writes the old house calls option"


async def test_a_test_phone_must_be_a_registered_phone(hass, running):
    entry, ws = running
    reg_id = await register(hass)
    bad = err(await ws.send(put({"test_phones": [reg_id, "no-such-registration"]}, confirm=True)))
    assert (bad["code"], bad["field"]) == ("unknown_phone", "test_phones")
    assert entry.options["test_phones"] == []
    ok(await ws.send(put({"test_phones": [reg_id, reg_id]}, confirm=True)))
    assert entry.options["test_phones"] == [reg_id], "listed once"
    # a listed phone that has since gone stays on the stored list (a staged test fails closed) and is shown
    await _manager(hass).registry.async_remove(reg_id)
    assert ok(await ws.send(GET))["test_phones"] == [reg_id] and ok(await ws.send(GET))["phones"] == []


async def test_a_bad_patch_is_refused_before_anything_else(hass, running):
    entry, ws = running
    before = dict(entry.options)
    for patch in ({}, {"nonsense": True}, {"ring_enabled": 1}, {"ring_enabled": None}, {"ring_window": 9}, {"ring_window": 121},
                  {"ring_window": True}, {"ring_window": 45.5}, {"ring_window": "45"}, {"new_phones_enabled": "yes"},
                  {"test_phones": "abc"}, {"test_phones": [1]}, {"test_phones": [""]}, {"test_phones": ["a"] * 51},
                  {"house_calls": True}, {"enabled": False}, {"firebase": {}}):
        assert err(await ws.send(put(patch, confirm=True)))["code"] == "bad_patch", patch
    assert err(await ws.send({**put({"ring_window": 60}), "patch": [1]}))["code"] == "invalid_format"
    assert err(await ws.send(put({"ring_window": 60}, confirm="yes")))["code"] == "invalid_format"
    assert dict(entry.options) == before


async def test_validate_only_checks_and_writes_nothing_and_needs_no_confirmation(hass, running):
    entry, ws = running
    before = dict(entry.options)
    reply = ok(await ws.send(put({"ring_enabled": False, "ring_window": 70}, validate_only=True)))
    assert reply["result"] == "valid" and reply["changes"] == ["ring_enabled", "ring_window"] and reply["settings"]["ring_enabled"] is True
    assert err(await ws.send(put({"test_phones": ["nope"]}, validate_only=True)))["code"] == "unknown_phone"
    assert err(await ws.send(put({"ring_window": 3}, validate_only=True)))["code"] == "bad_patch"
    assert dict(entry.options) == before


async def test_no_change_is_unchanged_and_needs_no_confirmation(hass, running):
    entry, ws = running
    before = dict(entry.options)
    reply = ok(await ws.send(put({"ring_enabled": True, "ring_window": 45, "new_phones_enabled": False, "test_phones": []})))
    assert (reply["result"], reply["changes"]) == ("unchanged", [])
    assert dict(entry.options) == before


async def test_without_a_settings_entry_nothing_is_written(hass, hass_ws_client):
    assert await async_setup_component(hass, DOMAIN, {})
    ws = Ws(await hass_ws_client(hass))
    assert ok(await ws.send(GET))["push_entry"] is False
    assert err(await ws.send(put({"ring_window": 60}, confirm=True)))["code"] == "no_push_entry"
    assert err(await ws.send(put({}, confirm=True)))["code"] == "bad_patch"


async def test_a_non_admin_cannot_read_or_change_anything(hass, hass_ws_client, running):
    entry, ws = running
    before = dict(entry.options)
    for who in ("plain", "enrolled"):
        _user, token = await _phone_user(hass, name=who)
        client = Ws(await hass_ws_client(hass, access_token=token))
        for body in (GET, put({"ring_window": 60}), put({"ring_enabled": False}, confirm=True), put({"ring_window": 60}, validate_only=True)):
            assert err(await client.send(body))["code"] == "unauthorized", (who, body["type"])
    assert dict(entry.options) == before


async def test_a_failure_is_unknown_and_never_the_exceptions_text(hass, running, monkeypatch, caplog):
    entry, ws = running
    caplog.set_level(logging.DEBUG)

    def boom(_hass):
        raise RuntimeError("SECRET")

    monkeypatch.setattr(ui_settings, "facts", boom)
    for body in (GET, put({"ring_window": 60})):
        bad = err(await ws.send(body))
        assert bad["code"] == "unknown" and "SECRET" not in json.dumps(bad)
    assert "SECRET" not in "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("custom_components.portico"))
    assert entry.options["ring_window"] == 45


async def test_a_write_logs_the_key_names_only(hass, running, caplog):
    entry, ws = running
    reg_id = await register(hass)
    caplog.set_level(logging.INFO)
    ok(await ws.send(put({"test_phones": [reg_id], "ring_window": 33}, confirm=True)))
    lines = [r.getMessage() for r in caplog.records if r.name == "custom_components.portico.ui_settings"]
    assert lines == ["Portico: an administrator changed the push ring notification settings: ring_window, test_phones"]


# ---- live video away from home (DEV-713 S1b-1): live_rtsp_base, live_sub_streams ------------------------------------------------------

BASE = "rtsp://10.6.0.6:8554"


async def test_the_live_options_are_omitted_from_a_read_while_unset(hass, running):
    entry, ws = running
    reply = ok(await ws.send(GET))
    assert "live_rtsp_base" not in reply and "live_sub_streams" not in reply, "a reply from before the options is unchanged"


async def test_the_live_options_need_the_servers_own_confirmation_and_then_read_back(hass, running):
    entry, ws = running
    before = dict(entry.options)
    patch = {"live_rtsp_base": BASE, "live_sub_streams": ["gate_intercom"]}
    for one in (patch, {"live_rtsp_base": BASE}, {"live_sub_streams": ["gate_intercom"]}):
        for extra in ({}, {"confirm": False}):
            assert err(await ws.send(put(one, **extra)))["code"] == "confirmation_required", (one, extra)
    assert dict(entry.options) == before
    valid = ok(await ws.send(put(patch, validate_only=True)))
    assert (valid["result"], valid["changes"]) == ("valid", ["live_rtsp_base", "live_sub_streams"]) and dict(entry.options) == before
    reply = ok(await ws.send(put(patch, confirm=True)))
    assert (reply["result"], reply["changes"]) == ("applied", ["live_rtsp_base", "live_sub_streams"])
    assert dict(entry.options)["live_rtsp_base"] == BASE and entry.options["live_sub_streams"] == ["gate_intercom"]
    assert reply["settings"]["live_rtsp_base"] == BASE and reply["settings"]["live_sub_streams"] == ["gate_intercom"]
    assert ok(await ws.send(GET)) == reply["settings"]
    again = ok(await ws.send(put(patch)))
    assert (again["result"], again["changes"]) == ("unchanged", []), "the same values need no confirmation"
    assert entry.options["ring_window"] == 45, "the push ring notification options are left alone"


async def test_the_address_is_stored_in_its_canonical_form(hass, running):
    entry, ws = running
    ok(await ws.send(put({"live_rtsp_base": "  RTSP://10.6.0.6:8554/ "}, confirm=True)))
    assert entry.options["live_rtsp_base"] == BASE
    assert ok(await ws.send(put({"live_rtsp_base": BASE})))["result"] == "unchanged"


async def test_clearing_a_live_option_removes_the_key(hass, running):
    entry, ws = running
    ok(await ws.send(put({"live_rtsp_base": BASE, "live_sub_streams": ["a", "b"]}, confirm=True)))
    assert err(await ws.send(put({"live_rtsp_base": ""})))["code"] == "confirmation_required", "turning it off is confirmed like the rest"
    reply = ok(await ws.send(put({"live_rtsp_base": "", "live_sub_streams": []}, confirm=True)))
    assert reply["changes"] == ["live_rtsp_base", "live_sub_streams"]
    assert "live_rtsp_base" not in entry.options and "live_sub_streams" not in entry.options
    assert "live_rtsp_base" not in reply["settings"] and "live_sub_streams" not in reply["settings"]
    assert ok(await ws.send(put({"live_rtsp_base": "", "live_sub_streams": []})))["result"] == "unchanged"


async def test_an_address_with_credentials_or_a_path_is_refused_and_nothing_is_written(hass, running):
    entry, ws = running
    before = dict(entry.options)
    for patch in ({"live_rtsp_base": "rtsp://user:pass@10.6.0.6:8554"}, {"live_rtsp_base": "rtsp://10.6.0.6:8554/gate"},
                  {"live_rtsp_base": "http://10.6.0.6:8554"}, {"live_rtsp_base": "rtsp://[::1]:8554"}, {"live_rtsp_base": "rtsp://" + "a" * 130},
                  {"live_rtsp_base": "rtsp://10.6.0.6:99999"}, {"live_rtsp_base": 5}, {"live_rtsp_base": None},
                  {"live_sub_streams": "gate"}, {"live_sub_streams": ["gate/intercom"]}, {"live_sub_streams": ["a b"]},
                  {"live_sub_streams": [f"n{i}" for i in range(65)]}, {"live_sub_streams": [3]},
                  {"live_rtsp_base": BASE, "live_sub_streams": ["../x"]}):
        for extra in ({}, {"validate_only": True}):
            assert err(await ws.send(put(patch, confirm=True, **extra)))["code"] == "bad_patch", (patch, extra)
    assert dict(entry.options) == before


async def test_a_live_change_tells_every_subscriber_to_refetch_and_a_no_change_tells_nobody(hass, running):
    from custom_components.portico.content import get_content_store

    entry, ws = running
    told = []
    store = get_content_store(hass)
    store.add_home_listener(lambda: told.append("home"))
    ok(await ws.send(put({"ring_window": 60})))
    assert told == [], "the push ring notification's own options do not touch the resolved document"
    ok(await ws.send(put({"live_rtsp_base": BASE}, confirm=True)))
    assert told == ["home"]
    ok(await ws.send(put({"live_rtsp_base": BASE})))
    assert told == ["home"], "unchanged: nothing is announced"
    ok(await ws.send(put({"live_sub_streams": ["a"]}, confirm=True)))
    ok(await ws.send(put({"live_rtsp_base": ""}, confirm=True)))
    assert told == ["home", "home", "home"]


async def test_a_live_write_logs_the_key_names_only(hass, running, caplog):
    entry, ws = running
    caplog.set_level(logging.DEBUG)
    ok(await ws.send(put({"live_rtsp_base": BASE, "live_sub_streams": ["gate_intercom"]}, confirm=True)))
    text = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("custom_components.portico"))
    assert "live_rtsp_base, live_sub_streams" in text and "10.6.0.6" not in text and "gate_intercom" not in text


# ---- DEV-715: live_away_streams, a camera's own away addresses --------------------------------------------------------------------------

AWAY = {"camera.porch": {"main": "rtsp://10.6.0.9:554/ch1", "sub": "rtsp://10.6.0.9:554/ch2"}, "camera.yard": {"main": "rtsp://cam.lan/live"}}


async def test_per_camera_away_streams_are_confirmed_stored_read_back_and_cleared(hass, running):
    entry, ws = running
    assert "live_away_streams" not in ok(await ws.send(GET))
    assert err(await ws.send(put({"live_away_streams": AWAY})))["code"] == "confirmation_required"
    reply = ok(await ws.send(put({"live_away_streams": AWAY}, confirm=True)))
    assert reply["changes"] == ["live_away_streams"] and entry.options["live_away_streams"] == AWAY
    assert ok(await ws.send(GET))["live_away_streams"] == AWAY
    assert ok(await ws.send(put({"live_away_streams": AWAY})))["result"] == "unchanged"
    ok(await ws.send(put({"live_away_streams": {}}, confirm=True)))
    assert "live_away_streams" not in entry.options and "live_away_streams" not in ok(await ws.send(GET))


async def test_bad_per_camera_away_streams_are_refused_and_nothing_is_written(hass, running):
    entry, ws = running
    before = dict(entry.options)
    good = {"main": "rtsp://10.6.0.9/a"}
    for bad in ({"light.porch": good}, {"camera.porch": {}}, {"camera.porch": {"sub": "rtsp://10.6.0.9/a"}},
                {"camera.porch": {"main": "rtsp://user:pw@10.6.0.9/a"}}, {"camera.porch": {"main": "rtsp://10.6.0.9/a?token=1"}},
                {"camera.porch": {"main": "http://10.6.0.9/a"}}, {"camera.porch": {"main": "rtsp://10.6.0.9:99999/a"}},
                {"camera.porch": {"main": "rtsp://10.6.0.9:554@evil.example/a"}}, {"camera.porch": {"main": "rtsp://10.6.0.9/a", "x": "y"}},
                {"camera.porch": "rtsp://10.6.0.9/a"}, ["camera.porch"], {f"camera.c{i}": good for i in range(65)}):
        for extra in ({}, {"validate_only": True}):
            assert err(await ws.send(put({"live_away_streams": bad}, confirm=True, **extra)))["code"] == "bad_patch", (bad, extra)
    assert dict(entry.options) == before
