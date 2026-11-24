"""DEV-518 C2: the `house_calls` option on a gate_sip_server entry (the observer is being retired, and the hub's `-house-calls`
flag is read from ANY door entry). Pinned here:
* `house_calls_wanted()` is true for a server entry that carries the option, exactly as for the observer's (observer unchanged);
* the server entry's Reconfigure form shows the switch only on a wheel with the bridge (or when already on), stores it in the
  options ONLY when on (the dialog's shape), keeps every other option, and refuses ON on an old wheel (`house_needs_wheel`);
* saving it never reloads the entry (the hub reads it once, when its process starts: a reload would only drop the SIP
  registration), while a `dial_enabled` change in the same save still does."""
from __future__ import annotations

import pytest
from homeassistant.data_entry_flow import FlowResultType

from custom_components.portico.door import hub as portico_hub

from .driver_part import _server_entry
from .test_gate_sip_server_w5 import _Reloads, _reconfigure
from .test_house_hub import _flagged, _install, _start  # noqa: F401
from .test_observer import _custom_integrations, fake_transport, get_hub  # noqa: F401 - fixtures by name

pytestmark = pytest.mark.asyncio
FLAGS = {"-socket", "-house-calls"}


async def test_a_server_entry_with_the_option_gets_the_flag(hass, fake_transport, monkeypatch):
    _install(monkeypatch, "0.7.4", known=FLAGS)
    entry = _server_entry(hass)
    hass.config_entries.async_update_entry(entry, options={"house_calls": True})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert _flagged(fake_transport) == [True] and get_hub(hass).house_calls_active is True


async def test_a_server_entry_without_it_and_no_observer_gets_no_flag(hass, fake_transport, monkeypatch):
    _install(monkeypatch, "0.7.4", known=FLAGS)
    assert await hass.config_entries.async_setup(_server_entry(hass).entry_id)
    await hass.async_block_till_done()
    assert _flagged(fake_transport) == [False] and get_hub(hass).house_calls_wanted() is False


async def test_the_observer_option_still_works_and_a_server_beside_it_does_not_change_that(hass, fake_transport, monkeypatch):
    _install(monkeypatch, "0.7.4", known=FLAGS)
    await _start(hass, house_calls=True)                                   # the observer alone: as before
    assert _flagged(fake_transport) == [True]
    srv = _server_entry(hass)                                              # a second door without the option
    assert await hass.config_entries.async_setup(srv.entry_id)
    await hass.async_block_till_done()
    assert get_hub(hass).house_calls_active is True and len(fake_transport.spawned) == 1   # the process was not restarted


def _house_field(form):
    return [k for k in form["data_schema"].schema if str(k) == "house_calls"]


async def test_the_form_shows_the_switch_only_on_a_wheel_with_the_bridge(hass, fake_transport, monkeypatch):
    entry = _server_entry(hass)
    monkeypatch.setattr(portico_hub.importlib.metadata, "version", lambda name: "0.5.1")
    assert _house_field(await _reconfigure(hass, entry)) == []
    monkeypatch.setattr(portico_hub.importlib.metadata, "version", lambda name: "0.7.4")
    (key,) = _house_field(await _reconfigure(hass, entry))
    assert key.default() is False
    hass.config_entries.async_update_entry(entry, options={"house_calls": True})
    monkeypatch.setattr(portico_hub.importlib.metadata, "version", lambda name: "0.5.1")   # rolled back: still shown, can be turned off
    (key,) = _house_field(await _reconfigure(hass, entry))
    assert key.default() is True


async def test_saving_it_stores_it_only_when_on_keeps_other_options_and_does_not_reload(hass, fake_transport, monkeypatch):
    monkeypatch.setattr(portico_hub.importlib.metadata, "version", lambda name: "0.7.4")
    entry = _server_entry(hass)
    hass.config_entries.async_update_entry(entry, options={"away_media": False})
    with _Reloads(hass) as reloads:
        r = await _reconfigure(hass, entry, {"dial_enabled": False, "house_calls": True})
        assert r["type"] is FlowResultType.ABORT and r["reason"] == "reconfigure_successful"
        assert dict(entry.options) == {"away_media": False, "house_calls": True}
        r = await _reconfigure(hass, entry, {"dial_enabled": False, "house_calls": False})
        assert dict(entry.options) == {"away_media": False}                # stored only when ON
        await _reconfigure(hass, entry, {"dial_enabled": False})           # a form that does not name it leaves it alone
        assert dict(entry.options) == {"away_media": False}
    assert reloads.calls == []


async def test_a_dial_change_in_the_same_save_still_reloads(hass, fake_transport, monkeypatch):
    monkeypatch.setattr(portico_hub.importlib.metadata, "version", lambda name: "0.7.4")
    entry = _server_entry(hass)
    with _Reloads(hass) as reloads:
        await _reconfigure(hass, entry, {"dial_enabled": True, "house_calls": True})
    assert reloads.calls and entry.options["house_calls"] is True and entry.data["dial_enabled"] is True


async def test_on_is_refused_on_an_old_wheel_and_nothing_is_written(hass, fake_transport, monkeypatch):
    monkeypatch.setattr(portico_hub.importlib.metadata, "version", lambda name: "0.7.4")
    entry = _server_entry(hass)
    form = await _reconfigure(hass, entry)                                   # shown while the wheel is new ...
    monkeypatch.setattr(portico_hub.importlib.metadata, "version", lambda name: "0.5.1")   # ... rolled back before the save
    r = await hass.config_entries.flow.async_configure(form["flow_id"], {"dial_enabled": False, "house_calls": True})
    assert r["type"] is FlowResultType.FORM and r["errors"] == {"house_calls": "house_needs_wheel"}
    assert dict(entry.options) == {} and entry.data["open_relay_action"] == []


async def test_the_texts_exist():
    import json
    from pathlib import Path
    root = Path(portico_hub.__file__).parent.parent
    for name in ("strings.json", "translations/en.json"):
        assert "house_calls" in json.loads((root / name).read_text(encoding="utf-8"))["config"]["step"]["reconfigure_server"]["data"]
