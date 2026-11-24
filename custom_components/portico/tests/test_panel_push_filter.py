"""DEV-807 design section 5 item 8: the server skips a panel's push registration for a door outside its screen's `wake`."""
from __future__ import annotations

import pytest
from types import SimpleNamespace
from unittest.mock import patch

from custom_components.portico import endpoints
from .test_push_notifier import ENTRY, _kinds, _phone, _settle, google, rig  # noqa: F401  (fixtures)

PANEL_USER = "panel-user"


def _panel(wake, *, panel=True, screen=True):
    """Patch the three lookups the filter reads: whether the user is a panel, its phone (the panel's identity), and the phone's `wake` doors."""
    return (
        patch.object(endpoints, "is_panel_user", lambda hass, uid: panel and uid == PANEL_USER),
        patch.object(endpoints, "phone_of_user", lambda hass, uid: SimpleNamespace(id="ep-panel-phone") if screen else None),
        patch.object(endpoints, "panel_wake_doors", lambda hass, phone_id: wake if phone_id == "ep-panel-phone" else "WRONG KEY"),
    )


async def _ring(rig, wake, **kw):
    other = await _phone(rig.registry, 1)
    panel = await _phone(rig.registry, 2, user=PANEL_USER)
    a, b, c = _panel(wake, **kw)
    with a, b, c:
        rig.tracker().apply_event({"call_id": "c1", "state": "ringing"})
        await rig.hass.async_block_till_done()
        await _settle(rig)
    return other, panel


async def test_a_door_outside_wake_is_not_pushed_to_the_panel_but_is_to_everyone_else(rig):
    other, panel = await _ring(rig, ["gate-2"])
    regs = {m.get("fid") or m.get("token") for m in rig.google.sent_messages()}
    assert regs == {other["registration"]}


@pytest.mark.parametrize("ref", [f"device-{ENTRY}", f"fake:device-{ENTRY}"])
async def test_a_door_in_wake_is_pushed_to_the_panel_in_either_spelling(rig, ref):
    other, panel = await _ring(rig, [ref])
    regs = {m.get("fid") or m.get("token") for m in rig.google.sent_messages()}
    assert regs == {panel["registration"], other["registration"]}


@pytest.mark.parametrize("kw,wake", [({}, None), ({"screen": False}, ["gate-2"]), ({"panel": False}, ["gate-2"])])
async def test_a_panel_with_no_wake_or_no_screen_or_not_a_panel_gets_every_door(rig, kw, wake):
    other, panel = await _ring(rig, wake, **kw)
    regs = {m.get("fid") or m.get("token") for m in rig.google.sent_messages()}
    assert regs == {panel["registration"], other["registration"]}


async def test_an_empty_wake_pushes_no_door_to_the_panel(rig):
    other, panel = await _ring(rig, [])
    regs = {m.get("fid") or m.get("token") for m in rig.google.sent_messages()}
    assert panel["registration"] not in regs and other["registration"] in regs


def test_the_filter_fails_open(hass):
    with patch.object(endpoints, "is_panel_user", side_effect=RuntimeError("boom")):
        assert endpoints.panel_door_push_allowed(hass, PANEL_USER, "gate-1") is True


def test_the_filter_passes_a_ring_with_no_door(hass):
    a, b, c = _panel(["gate-2"])
    with a, b, c:
        assert endpoints.panel_door_push_allowed(hass, PANEL_USER, None) is True
        assert endpoints.panel_door_push_allowed(hass, PANEL_USER, "gate-1") is False
        assert endpoints.panel_door_push_allowed(hass, "someone-else", "gate-1") is True
