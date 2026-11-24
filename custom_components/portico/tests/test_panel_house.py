"""DEV-807 panel mode, server slice 2: the house-call side of a panel (design section 5 items 2 to 4, section 9).

A panel is a phone (its house identity) and an app screen (its wall identity). Pinned here: house.py never offers the screen as
a callee and resolves a caller named by the screen's slug to its phone (R1, R3: `original_kind` for HC-9), `receives` False for the
screen, the Everyone key's busy and expected set (R3, R4), detach over every endpoint of the closing user (R5), door ringing by
`wake` (R6), the server boolean `house_calls` and its announcements, Talk keying (item 4), the heartbeat and the readiness sensor
(section 9). House calls stay off for a house with no panel: every test of an ordinary house is in test_house.py, unchanged.
"""
from __future__ import annotations

import dataclasses
from unittest.mock import patch

import pytest
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from .. import house as house_module
from .. import panel_wall
from ..call_endpoints import REASON_BUSY
from ..config_flow import DOMAIN
from ..content import RunRefused, get_content_store
from ..endpoints import get_endpoint_registry, phone_of_user
from ..house import CODE_FORBIDDEN, CODE_NOT_CALLABLE, async_setup_house
from ..phone_entry import CONF_PANEL, async_set_house_receive, phone_entry_for_user
from ..panel_wall import get_panel_wall
from .house_env import House, phone_entry
from .test_endpoints import _provide
from .test_enrollment_flow import _qr_capture, qr_payloads  # noqa: F401 - fixtures by name
from .test_phone_entry import _bare_push_entry, _enroll_phone
from .test_push_registry import _custom_integrations, _phone_user  # noqa: F401 - the first is an autouse fixture

pytestmark = pytest.mark.asyncio

CONF_PANEL_SCREEN = "panel_screen"        # a legacy option an old entry may still carry: it is ignored

SLUG = "panel-app"
PANEL_USER = "u-panel"


def _doc(dashboards):
    return {"version": 1, "minor_version": 1, "key": "portico.content",
            "data": {"schema_version": 3, "template": {"pages": [], "rules": [], "roster": ["*"]}, "overrides": {},
                     "dashboards": dashboards, "endpoints": {}}}


async def _world(hass, hass_storage, *, extra_dashboards=None, panel_receive=True) -> House:
    """Phones `alice` and `dina` (receive on), the panel phone `panel` (user u-panel) with its app screen `panel-app`, the plain
    screen `kitchen`, one fake door `gate-1`, and the house registry on."""
    dashboards = {"kitchen": {"name": "Kitchen", "ha_user_id": "u-kitchen", "room": "Kitchen", "cameras": []},
                  SLUG: {"name": "Kitchen panel", "ha_user_id": PANEL_USER, "cameras": []}, **(extra_dashboards or {})}
    hass_storage["portico.content"] = _doc(dashboards)
    for endpoint in ("alice", "dina"):
        phone_entry(hass, f"u-{endpoint}", endpoint, endpoint.capitalize())
    entry = phone_entry(hass, PANEL_USER, "panel", "Panel", receive=panel_receive)
    hass.config_entries.async_update_entry(entry, options={**entry.options, CONF_PANEL: True, CONF_PANEL_SCREEN: SLUG})
    await get_content_store(hass).async_ensure_loaded()
    fake = _provide(hass, "fake", "gate-1")
    registry = async_setup_house(hass, enabled=True, id_factory=iter(f"hso-{n}" for n in range(1, 99)).__next__)
    users = {"alice": "u-alice", "dina": "u-dina", "panel": PANEL_USER, SLUG: PANEL_USER, "kitchen": "u-kitchen"}
    house = House(hass=hass, registry=registry, door=fake, users=users)
    house.entry = entry
    return house


@pytest.fixture
async def w(hass, hass_storage):
    h = await _world(hass, hass_storage)
    for who in ("alice", "dina", "panel", "kitchen"):
        h.online(who)
    for conn in h.conns.values():
        conn.clear()
    return h


# ---- R1: the screen is never offered; its phone is --------------------------------------------------------------------------

async def test_the_app_screen_is_not_a_callee_and_does_not_receive(w):
    reg, hass = w.registry, w.hass
    screen = get_endpoint_registry(hass).get(SLUG)
    assert screen is not None and screen.kind == "dashboard"
    assert reg.receives(screen) is False
    assert reg._endpoint(SLUG) is None and reg._endpoint("panel").id == "panel"
    assert SLUG not in {e.id for e in reg._endpoints()} and {"panel", "kitchen"} <= {e.id for e in reg._endpoints()}
    assert reg.target_state("dashboard", SLUG) is None and reg.presence(SLUG) is None
    assert SLUG not in {t.id for t in reg.endpoints(caller_user_id="u-alice")}
    assert reg.receives(get_endpoint_registry(hass).get("kitchen")) is True      # a plain screen still receives


async def test_a_call_addressed_to_the_screen_id_is_refused_and_one_to_its_phone_rings_it(w):
    refused = w.place("alice", "dashboard", SLUG)
    assert not refused.ok and refused.code == CODE_NOT_CALLABLE
    refused = w.place("alice", "phone", SLUG)
    assert not refused.ok and refused.code == CODE_NOT_CALLABLE
    result = w.place("alice", "phone", "panel")
    assert result.ok
    assert [e["state"] for e in w.conns["panel"].events if e["type"] == "house_call"] == ["ringing"]


async def test_the_roster_of_another_device_lacks_the_screen(w, hass):
    alice = get_content_store(hass).resolve_for_endpoint("alice")
    assert SLUG not in {r["id"] for r in alice["roster"]} and "panel" in {r["id"] for r in alice["roster"]}


# ---- R3: the caller is normalised, in order -----------------------------------------------------------------------------------

async def test_the_panel_places_a_call_through_its_slug_as_its_phone(w):
    result = w.place(SLUG, "phone", "alice")
    assert result.ok
    events = [e for e in w.conns["alice"].events if e["type"] == "house_call"]
    assert events[0]["from"] == "panel" and events[0]["state"] == "ringing"        # the phone id, never the slug
    assert SLUG not in str(w.conns["panel"].events)
    # the panel's own call is the phone's: the app's `e.from == me` compares phone ids
    assert {e["from"] for e in w.conns["panel"].events if e["type"] == "house_call"} == {"panel"}


async def test_the_panel_places_a_call_as_its_phone_with_no_endpoint_named(w):
    result = w.registry.place_call(caller_user_id=PANEL_USER, target_type="phone", target_id="dina")
    assert result.ok


async def test_everyone_from_the_panel_rings_the_others_not_the_panel(w):
    result = w.everyone(SLUG)
    assert result.ok
    assert [e["state"] for e in w.conns["alice"].events if e["type"] == "house_call"] == ["ringing"]
    assert [e["state"] for e in w.conns["dina"].events if e["type"] == "house_call"] == ["ringing"]
    mine = [e for e in w.conns["panel"].events if e["type"] == "house_call"]
    assert [e["state"] for e in mine] == ["ringing"] and mine[0]["to"] == "everyone" and mine[0]["from"] == "panel"
    answered, err = w.registry.answer(result.call_id, "u-alice")
    assert err is None and answered["call_id"] == result.call_id                 # the id is answerable by the others


async def test_hc9_is_read_on_the_kind_the_caller_was_named_with(w):
    first = w.everyone(SLUG)
    assert first.ok is True                                              # named by the former screen: a dashboard
    assert w.registry.hangup(first.call_id, PANEL_USER)[0] is True
    second = w.everyone("panel")                                         # named by its phone: the panel is a screen for groups
    assert second.ok is True
    assert w.registry.hangup(second.call_id, PANEL_USER)[0] is True
    assert w.registry.place_call(caller_user_id=PANEL_USER, target_type="group", target_id="everyone").ok is True
    plain = w.everyone("alice")
    assert not plain.ok and plain.code == CODE_FORBIDDEN


async def test_a_caller_naming_somebody_elses_slug_is_refused(w):
    result = w.registry.place_call(caller_user_id="u-alice", target_type="phone", target_id="dina", caller_endpoint=SLUG)
    assert not result.ok and result.code == CODE_NOT_CALLABLE


async def test_the_everyone_key_reads_the_screens_original_kind_and_the_phones_busy(w):
    screen = get_endpoint_registry(w.hass).get(SLUG)
    entries = w.registry.group_entries(screen)
    assert len(entries) == 1 and entries[0]["id"] == "everyone" and entries[0]["available"] is True and entries[0]["count"] == 2
    assert w.registry.group_entries(get_endpoint_registry(w.hass).get("alice")) == []        # a plain phone callee: nothing (HC-9)
    assert w.registry.group_entries(get_endpoint_registry(w.hass).get("panel"))[0]["id"] == "everyone"   # a panel's phone gets it
    assert w.registry.group_entries(None) == []
    # a door rings: the panel phone is busy, so its Everyone key shows `busy`
    w.door.ring("gate-1", "g1")
    entry = w.registry.group_entries(screen)[0]
    assert entry["available"] is False and entry["reason"] == REASON_BUSY


async def test_the_everyone_key_shows_busy_while_the_panel_is_on_a_call(w):
    assert w.place("alice", "phone", "panel").ok
    entry = w.registry.group_entries(get_endpoint_registry(w.hass).get(SLUG))[0]
    assert entry["available"] is False and entry["reason"] == REASON_BUSY


# ---- R4: every endpoint of the caller's user is skipped --------------------------------------------------------------------

async def test_expected_everyone_skips_every_endpoint_of_the_callers_user(w):
    phone = get_endpoint_registry(w.hass).get("panel")
    sibling = dataclasses.replace(phone, id="panel-twin")                         # another phone of the same user
    reg = w.registry
    with patch.object(reg, "_endpoints", return_value=[*reg._endpoints(), sibling]):
        rung, _ = reg._expected_everyone(phone)
        assert {e.id for e in rung} == {"alice", "dina"}
        assert "panel-twin" not in {e.id for e in rung}


# ---- R5: detach over every endpoint of the user -----------------------------------------------------------------------------

async def test_the_last_socket_of_a_user_with_two_endpoints_ends_the_ring(hass, hass_storage):
    h = await _world(hass, hass_storage, extra_dashboards={"scr-x": {"name": "X", "ha_user_id": "u-x", "cameras": []},
                                                           "scr-y": {"name": "Y", "ha_user_id": "u-x", "cameras": []}})
    phone_entry(hass, "u-x", "px", "Px")
    await get_content_store(hass).async_ensure_loaded()
    h.users["px"] = "u-x"
    h.online("alice")
    h.online("px")
    assert h.registry._endpoint_for_user("u-x") is None                             # a login with several endpoints names none
    result = h.place("alice", "phone", "px")
    assert result.ok
    h.conns["alice"].clear()
    h.offline("px")
    assert [e["state"] for e in h.conns["alice"].events if e["type"] == "house_call"] == ["ended"]


async def test_the_last_socket_closing_after_the_screen_was_deleted_mid_ring_ends_the_call(w, hass):
    assert w.place("alice", "phone", "panel").ok
    w.conns["alice"].clear()
    store = get_content_store(hass)
    store._dashboards = {k: v for k, v in store.dashboards.items() if k != SLUG}      # the screen is deleted while it rings
    assert phone_of_user(hass, PANEL_USER).id == "panel"                       # the panel's identity is its phone, whatever its screen does
    w.offline("panel")
    assert [e["state"] for e in w.conns["alice"].events if e["type"] == "house_call"] == ["ended"]


# ---- R6: a door outside `wake` does not touch a house call to the panel -------------------------------------------------------

async def test_a_door_outside_wake_leaves_a_house_call_to_the_panel_alone(w):
    alice = phone_entry_for_user(w.hass, "u-alice")
    w.hass.config_entries.async_update_entry(alice, options={**alice.options, "doors": ["gate-2"]})   # the caller is not rung by gate-1 either
    assert w.place("alice", "phone", "panel").ok
    with patch.object(house_module, "panel_wake_doors", return_value=["gate-2"]):
        w.door.ring("gate-1", "g1")
        assert w.registry.busy_state(get_endpoint_registry(w.hass).get("panel")) != "door_ringing"
        assert w.registry.active_count() == 1


async def test_a_door_in_wake_displaces_a_house_call_to_the_panel_and_so_does_an_absent_wake(w):
    for wake in (["gate-1"], ["fake:gate-1"], None):
        assert w.place("alice", "phone", "panel").ok
        with patch.object(house_module, "panel_wake_doors", return_value=wake):
            w.door.ring("gate-1", "g1")
            assert w.registry.active_count() == 0, wake
        w.door.state("g1", "ended")
        for conn in w.conns.values():
            conn.clear()


async def test_a_house_call_then_a_door_and_the_reverse(w):
    assert w.place("panel", "phone", "alice").ok
    w.door.ring("gate-1", "g1")                                    # the door outranks the house call
    assert w.registry.active_count() == 0
    w.door.state("g1", "ended")
    w.door.ring("gate-1", "g2")
    busy = w.place("alice", "phone", "panel")
    assert not busy.ok and busy.code == "busy"


# ---- the server boolean `house_calls` and its announcements ---------------------------------------------------------------

async def test_house_calls_is_on_for_a_panel_only_with_receive(w, hass):
    store = get_content_store(hass)
    assert store.resolve_for_endpoint(SLUG)["house_calls"] is True
    assert store.resolve_for_endpoint("panel")["house_calls"] is True
    async_set_house_receive(hass, w.entry, False)
    assert store.resolve_for_endpoint(SLUG)["house_calls"] is False
    async_set_house_receive(hass, w.entry, True)
    assert "house_calls" not in store.resolve_for_endpoint("alice") and "house_calls" not in store.resolve_for_endpoint("kitchen")


async def test_a_receive_toggle_announces_every_panel(w, hass):
    store = get_content_store(hass)
    heard: list[str] = []
    store.add_listener(heard.append)
    async_set_house_receive(hass, w.entry, False)
    assert heard == ["panel"]                                    # the panel is addressed by its phone endpoint id
    from ..endpoints import announce_panels

    announce_panels(hass, PANEL_USER)
    announce_panels(hass, "u-nobody")
    assert heard == ["panel", "panel"]


# ---- Talk keying (item 4) -------------------------------------------------------------------------------------------------------

async def test_the_screens_talk_button_is_keyed_by_its_phone_and_a_browser_screen_is_not(w, hass):
    store = get_content_store(hass)
    reg = get_endpoint_registry(hass)
    screen, phone, kitchen = reg.get(SLUG), reg.get("panel"), reg.get("kitchen")
    assert store._talk_keyed(screen).id == "panel" and store._talk_keyed(phone).id == "panel"
    assert store._talk_keyed(kitchen).id == "kitchen" and store._talk_keyed(None) is None
    assert store.talk_state("gate-1", kitchen)["reason"] == "phone_only"           # a browser kiosk card on another login
    assert store.talk_state("gate-1", screen)["reason"] != "phone_only"            # the panel's own button is not `phone_only`


async def test_a_talk_press_through_the_screen_passes_the_who_check_and_a_browser_screen_does_not(w, hass):
    store = get_content_store(hass)
    action = {"id": "a1", "label": "Talk", "kind": "talk"}
    with pytest.raises(RunRefused) as plain:
        await store._run_talk("u-kitchen", "Kitchen", action, "kitchen", None, "u-kitchen", door_id="gate-1", explicit=True)
    assert plain.value.code == "not_allowed"
    with pytest.raises(RunRefused) as panel:
        await store._run_talk(PANEL_USER, "Panel", action, SLUG, None, PANEL_USER, door_id="gate-1", explicit=True)
    assert panel.value.code != "not_allowed"                       # the who-check passed on the phone; the door is the next gate
    with pytest.raises(RunRefused) as other:                       # somebody else's login still may not press for the panel
        await store._run_talk("u-alice", "Alice", action, SLUG, None, "u-alice", door_id="gate-1", explicit=True)
    assert other.value.code == "not_allowed"


async def test_a_dial_through_the_screen_is_owned_and_rate_keyed_by_the_phone(w, hass):
    store = get_content_store(hass)
    w.door.can_dial = True
    action = {"id": "a1", "label": "Talk", "kind": "talk"}
    door = _FakeDoor()
    with patch.object(store, "_talk_view", return_value={"available": True, "reason": None, "provider": _FakeProvider(),
                                                         "door": door, "mine": None}):
        result = await store._run_talk(PANEL_USER, "Panel", action, SLUG, None, PANEL_USER, door_id="gate-1", explicit=True)
    assert result["call_id"] == "out-1"
    assert store._talk_calls[("fake", "gate-1")][0] == "panel"             # the owner: the phone's id, not the slug
    assert ("panel", "a1") in store._last_run and (SLUG, "a1") not in store._last_run


class _FakeDoor:
    id = "gate-1"
    can_dial = True
    available = True


class _FakeProvider:
    domain = "fake"

    async def dial(self, door_id, host, port):
        return {"call_id": "out-1"}


# ---- the heartbeat command (section 9) ------------------------------------------------------------------------------------------

GOOD = {"app_version": "0.6.0-alpha", "panel_mode": True, "endpoint_ok": True, "overlay_ok": True, "fsi_ok": True,
        "microphone_ok": True, "notifications_ok": True, "ring_audible": True, "battery_exempt": True, "stay_connected": True,
        "ring_level_ok": True, "wake_doors": 2, "front_reason": "none", "foreground": False, "overdue": False}


async def _ws(hass, hass_ws_client, user_id_options=None):
    from .test_signaling import _setup_signaling

    user, token = await _phone_user(hass, name="Portico — Kitchen panel")
    entry = phone_entry(hass, user.id, "ep-p", "Kitchen panel", receive=False)
    await _setup_signaling(hass)
    return user, token, entry, await hass_ws_client(hass, access_token=token)


async def test_a_heartbeat_from_a_phone_that_is_not_a_panel_is_refused(hass, hass_ws_client):
    user, token, entry, client = await _ws(hass, hass_ws_client)
    await client.send_json({"id": 1, "type": "portico/wall/heartbeat", **GOOD})
    reply = await client.receive_json()
    assert reply["success"] is False and reply["error"]["code"] == "not_a_panel"


async def test_a_panels_heartbeat_is_answered_with_house_ok_and_the_servers_phone_endpoint(hass, hass_storage, hass_ws_client):
    user, token, entry, client = await _ws(hass, hass_ws_client)
    hass_storage["portico.content"] = _doc({SLUG: {"name": "P", "ha_user_id": user.id, "cameras": []}})
    await get_content_store(hass).async_ensure_loaded()
    hass.config_entries.async_update_entry(entry, options={CONF_PANEL: True, CONF_PANEL_SCREEN: SLUG})
    await client.send_json({"id": 1, "type": "portico/wall/heartbeat", **GOOD})
    reply = await client.receive_json()
    assert reply["success"] and reply["result"] == {"house_ok": False, "phone_endpoint": "ep-p"}     # receive is off
    async_set_house_receive(hass, entry, True)
    await client.send_json({"id": 2, "type": "portico/wall/heartbeat", **GOOD, "phone_endpoint": "ep-p",
                            "house_switch": True, "house_gate": True})
    assert (await client.receive_json())["result"] == {"house_ok": True, "phone_endpoint": "ep-p"}
    for n, patch_ in enumerate(({"phone_endpoint": "ep-other"}, {"phone_endpoint": None}), start=10):
        await client.send_json({"id": n, "type": "portico/wall/heartbeat", **GOOD, "phone_endpoint": "ep-p", "house_switch": True,
                                "house_gate": True, **patch_})
        assert (await client.receive_json())["result"]["house_ok"] is False, patch_
    await client.send_json({"id": 4, "type": "portico/wall/heartbeat", **GOOD, "last_ring_call": "bad id with spaces"})
    assert (await client.receive_json())["success"] is False


# ---- the sensor ------------------------------------------------------------------------------------------------------------------

async def _panel_sensor(hass, qr_payloads):
    push = _bare_push_entry(hass, firebase=None)
    phone = await _enroll_phone(hass, push, name="Kitchen panel")
    user_id = phone.data["ha_user_id"]
    return phone, user_id


async def _make_panel(hass, phone, user_id):
    """The phone becomes a panel with the screen SLUG declared for its user (the store is already loaded by the enrolment)."""
    store = get_content_store(hass)
    await store.async_ensure_loaded()
    store._dashboards = {**store.dashboards, SLUG: {"name": "P", "ha_user_id": user_id, "cameras": []}}
    hass.config_entries.async_update_entry(phone, options={CONF_PANEL: True, CONF_PANEL_SCREEN: SLUG})


async def test_the_sensor_exists_only_for_a_panel_and_walks_its_four_states(hass, hass_storage, qr_payloads):
    phone, user_id = await _panel_sensor(hass, qr_payloads)
    assert hass.states.get("sensor.portico_panel_kitchen_panel") is None              # an ordinary phone has none
    await _make_panel(hass, phone, user_id)
    from ..entities.sensor import async_ensure_panel_sensor

    assert async_ensure_panel_sensor(hass, phone) is True and async_ensure_panel_sensor(hass, phone) is False
    await hass.async_block_till_done()
    entity_id = "sensor.portico_panel_kitchen_panel"
    assert hass.states.get(entity_id).state == "unknown"                              # from start until the first heartbeat
    wall = get_panel_wall(hass)
    wall.started -= panel_wall.UNKNOWN_S + 1
    wall._changed(user_id)
    assert hass.states.get(entity_id).state == "not_ready"                            # 150 s and no heartbeat
    wall.socket_opened(user_id)
    wall.heartbeat(user_id, dict(GOOD))
    state = hass.states.get(entity_id)
    assert state.state == "ready" and "phone_endpoint" not in state.attributes and state.attributes["house_receive"] is False
    wall.heartbeat(user_id, {**GOOD, "wake_doors": 0})
    assert hass.states.get(entity_id).state == "ready_no_doors"
    wall.heartbeat(user_id, {**GOOD, "wake_doors": 0, "wake_all": True})
    assert hass.states.get(entity_id).state == "ready"                                # an absent `wake` counts as all doors
    for key in panel_wall.READY_BOOLS:
        wall.heartbeat(user_id, {**GOOD, key: False})
        assert hass.states.get(entity_id).state == "not_ready", key
    wall.heartbeat(user_id, {**GOOD, "app_version": "0.4.0"})
    assert hass.states.get(entity_id).state == "not_ready"
    wall.heartbeat(user_id, dict(GOOD))
    assert hass.states.get(entity_id).state == "ready"
    wall.socket_closed(user_id)
    assert hass.states.get(entity_id).state == "not_ready"
    wall.socket_opened(user_id)
    assert hass.states.get(entity_id).state == "ready"


async def test_ready_requires_house_ok_whenever_receive_is_on(hass, hass_storage, qr_payloads):
    phone, user_id = await _panel_sensor(hass, qr_payloads)
    await _make_panel(hass, phone, user_id)
    from ..entities.sensor import async_ensure_panel_sensor

    async_ensure_panel_sensor(hass, phone)
    await hass.async_block_till_done()
    wall = get_panel_wall(hass)
    entity_id = "sensor.portico_panel_kitchen_panel"
    server_id = wall.server_phone_endpoint(user_id)
    assert server_id == phone.data["enroll_id"]
    wall.socket_opened(user_id)
    wall.heartbeat(user_id, dict(GOOD))
    assert hass.states.get(entity_id).state == "ready"                                # receive off: doors-only readiness
    async_set_house_receive(hass, phone, True)
    await hass.async_block_till_done()
    wall._changed(user_id)
    assert hass.states.get(entity_id).state == "not_ready" and hass.states.get(entity_id).attributes["house_ok"] is False
    wall.heartbeat(user_id, {**GOOD, "phone_endpoint": server_id, "house_switch": True, "house_gate": True})
    assert hass.states.get(entity_id).state == "ready" and hass.states.get(entity_id).attributes["house_ok"] is True
    # D-20261122-aafb: the app's old house_switch / house_gate no longer matter, even when false
    wall.heartbeat(user_id, {**GOOD, "phone_endpoint": server_id, "house_switch": False, "house_gate": False})
    assert hass.states.get(entity_id).state == "ready"
    for bad in ({"phone_endpoint": "someone-else"}, {"phone_endpoint": None}):
        wall.heartbeat(user_id, {**GOOD, "phone_endpoint": server_id, "house_switch": True, "house_gate": True, **bad})
        assert hass.states.get(entity_id).state == "not_ready", bad
    assert server_id not in str(hass.states.get(entity_id).attributes)               # the id is never in the attributes


async def test_a_stale_heartbeat_turns_the_sensor_not_ready_on_the_server_timer(hass, hass_storage, qr_payloads):
    phone, user_id = await _panel_sensor(hass, qr_payloads)
    await _make_panel(hass, phone, user_id)
    from ..entities.sensor import PANEL_TICK, async_ensure_panel_sensor

    async_ensure_panel_sensor(hass, phone)
    await hass.async_block_till_done()
    wall = get_panel_wall(hass)
    wall.socket_opened(user_id)
    wall.heartbeat(user_id, dict(GOOD))
    entity_id = "sensor.portico_panel_kitchen_panel"
    assert hass.states.get(entity_id).state == "ready"
    wall._users[user_id].beat_at -= panel_wall.HEARTBEAT_STALE_S + 1
    async_fire_time_changed(hass, dt_util.utcnow() + PANEL_TICK)
    await hass.async_block_till_done()
    state = hass.states.get(entity_id)
    assert state.state == "not_ready" and state.attributes["heartbeat_stale"] is True


async def test_ring_sent_not_shown_is_matched_by_the_full_call_id(hass, hass_storage, qr_payloads):
    phone, user_id = await _panel_sensor(hass, qr_payloads)
    await _make_panel(hass, phone, user_id)
    from ..entities.sensor import async_ensure_panel_sensor

    async_ensure_panel_sensor(hass, phone)
    await hass.async_block_till_done()
    wall = get_panel_wall(hass)
    entity_id = "sensor.portico_panel_kitchen_panel"
    wall.socket_opened(user_id)
    wall.heartbeat(user_id, dict(GOOD))

    async def later(seconds):
        async_fire_time_changed(hass, dt_util.utcnow() + __import__("datetime").timedelta(seconds=seconds))
        await hass.async_block_till_done()

    # two consecutive `gso-` ids: the first is shown (direct), the second is not
    wall.note_ring(user_id, "gso-1700000000000-1-aaaa", "gate-1")
    wall.heartbeat(user_id, {**GOOD, "last_ring_call": "gso-1700000000000-1-aaaa", "last_ring_path": "direct"})
    wall.note_ring(user_id, "gso-1700000000001-2-bbbb", "gate-1")
    await later(panel_wall.RING_SHOWN_WITHIN_S + 1)
    assert hass.states.get(entity_id).attributes["ring_sent_not_shown"] is True        # only the second is unshown
    wall.heartbeat(user_id, {**GOOD, "last_ring_call": "gso-1700000000000-1-aaaa", "last_ring_path": "direct"})
    assert hass.states.get(entity_id).attributes["ring_sent_not_shown"] is True        # the first id does not clear the second
    wall.heartbeat(user_id, {**GOOD, "last_ring_call": "gso-1700000000001-2-bbbb", "last_ring_path": "fsi"})
    assert hass.states.get(entity_id).attributes["ring_sent_not_shown"] is False       # cleared by the matching id
    # a banner report counts as not shown
    wall.note_ring(user_id, "gso-1700000000002-3-cccc", "gate-1")
    wall.heartbeat(user_id, {**GOOD, "last_ring_call": "gso-1700000000002-3-cccc", "last_ring_path": "banner"})
    await later(2 * panel_wall.RING_SHOWN_WITHIN_S + 2)
    assert hass.states.get(entity_id).attributes["ring_sent_not_shown"] is True
    # a door outside the screen's `wake` is not a ring the panel was sent
    wall.heartbeat(user_id, {**GOOD, "last_ring_call": "gso-1700000000002-3-cccc", "last_ring_path": "direct"})
    assert hass.states.get(entity_id).attributes["ring_sent_not_shown"] is False
    with patch.object(panel_wall, "panel_wake_doors", return_value=["gate-2"]):
        wall.note_ring(user_id, "gso-1700000000003-4-dddd", "gate-1")
    await later(3 * panel_wall.RING_SHOWN_WITHIN_S + 3)
    assert hass.states.get(entity_id).attributes["ring_sent_not_shown"] is False


async def test_the_wall_journal_word_is_accepted():
    from ..connlog import JOURNAL_WORDS

    assert "wall" in JOURNAL_WORDS
