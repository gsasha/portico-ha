"""`portico.call_everyone` (panel_call.py): the server asks a panel's app to place the Everyone call and waits for its answer.

The app side is not here (portico-app's PanelCallCoordinatorTest); this pins the server half: who is asked, every refusal the server can
decide alone, the event on the panel's calls stream, the answer command's guards, the rate limit, one request at a time, a timeout."""
from __future__ import annotations

import asyncio

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from ..config_flow import DOMAIN
from ..panel_call import EVENT_TYPE, PanelCalls, async_setup_panel_call, get_panel_calls
from ..panel_wall import get_panel_wall
from .house_env import FakeConn
from .test_panel_house import GOOD, PANEL_USER, _world
from .test_push_registry import _custom_integrations  # noqa: F401 - autouse fixture

pytestmark = pytest.mark.asyncio

BEAT = {**GOOD, "panel_call": True}


@pytest.fixture
async def w(hass, hass_storage):
    h = await _world(hass, hass_storage)
    for who in ("alice", "dina", "panel", "kitchen"):
        h.online(who)
    async_setup_panel_call(hass)
    return h


def _panel_online(hass, *, beat=BEAT, conn: FakeConn | None = None) -> FakeConn:
    """The panel app: an open calls stream (what `signaling.handle_calls_subscribe` registers) and a heartbeat."""
    conn = conn or FakeConn(PANEL_USER, msg_id=7)
    get_panel_calls(hass).attach(conn, conn.msg_id)
    wall = get_panel_wall(hass)
    wall.socket_opened(PANEL_USER)
    if beat is not None:
        wall.heartbeat(PANEL_USER, dict(beat))
    return conn


async def _ask(hass, **data):
    return await hass.services.async_call(DOMAIN, "call_everyone", data, blocking=True, return_response=True)


async def _answer(hass, conn: FakeConn, **kw) -> str:
    """Wait for the event, answer it as the app would, return the request id."""
    for _ in range(50):
        await asyncio.sleep(0)
        if conn.events:
            break
    event = conn.events[-1]
    assert event["type"] == EVENT_TYPE and event["target"] == "everyone"
    rid = event["request_id"]
    assert get_panel_calls(hass).result(PANEL_USER, rid, kw.get("ok", True), kw.get("code"), kw.get("text"))
    return rid


async def test_the_happy_path_sends_one_event_and_returns_ringing(hass, w):
    conn = _panel_online(hass)
    task = hass.async_create_task(_ask(hass))
    await _answer(hass, conn)
    assert await task == {"ok": True, "state": "ringing", "panel": "Panel"}
    assert [e["type"] for e in conn.events] == [EVENT_TYPE]
    assert set(conn.events[0]) == {"type", "request_id", "target"}        # nothing else travels: no text, no id of a phone


async def test_the_panel_may_be_named_by_name_or_endpoint_id_and_any_case(hass, w):
    conn = _panel_online(hass)
    for n, name in enumerate(("PANEL", "panel")):
        get_panel_calls(hass)._state(PANEL_USER).last_request = None
        conn.events.clear()
        task = hass.async_create_task(_ask(hass, panel=name))
        await _answer(hass, conn)
        assert (await task)["ok"] is True
    with pytest.raises(ServiceValidationError, match="no_panel"):
        get_panel_calls(hass)._state(PANEL_USER).last_request = None
        await _ask(hass, panel="kitchen")


async def test_a_context_less_or_non_admin_caller_is_allowed_on_purpose(hass, w, hass_read_only_user):
    conn = _panel_online(hass)
    task = hass.async_create_task(hass.services.async_call(
        DOMAIN, "call_everyone", {}, blocking=True, return_response=True,
        context=__import__("homeassistant.core", fromlist=["Context"]).Context(user_id=hass_read_only_user.id)))
    await _answer(hass, conn)
    assert (await task)["ok"] is True


@pytest.mark.parametrize("patch_, code", [
    ({"panel_mode": False}, "not_panel_mode"),
    ({"overlay_ok": False}, "no_front"),
    ({"microphone_ok": False}, "no_microphone"),
    ({"panel_call": False}, "app_too_old"),
    ({"front_reason": "call"}, "busy"),
    ({"front_reason": "ring"}, "busy"),
])
async def test_the_servers_own_refusals_send_nothing_to_the_app(hass, w, patch_, code):
    conn = _panel_online(hass, beat={**BEAT, **patch_})
    with pytest.raises(ServiceValidationError, match=code):
        await _ask(hass)
    assert conn.events == []


async def test_a_panel_that_is_not_connected_is_a_clear_error(hass, w):
    with pytest.raises(HomeAssistantError, match="not_connected"):        # no socket, no heartbeat
        await _ask(hass)
    conn = _panel_online(hass)
    wall = get_panel_wall(hass)
    wall._users[PANEL_USER].beat_at -= 400                                # heartbeat older than 3 minutes
    with pytest.raises(HomeAssistantError, match="not_connected"):
        await _ask(hass)
    assert conn.events == []
    wall._users[PANEL_USER].beat_at += 400
    wall._users[PANEL_USER].sockets = 0
    with pytest.raises(HomeAssistantError, match="not_connected"):
        await _ask(hass)


async def test_a_panel_on_a_house_call_is_refused_by_the_server(hass, w):
    conn = _panel_online(hass)
    assert w.place("alice", "phone", "panel").ok            # the panel's phone is ringing for alice's call
    with pytest.raises(ServiceValidationError, match="busy"):
        await _ask(hass)
    assert conn.events == []


async def test_no_receiving_phone_is_a_clear_error_and_no_request(hass, w):
    conn = _panel_online(hass)
    w.offline("alice")
    w.offline("dina")
    with pytest.raises(ServiceValidationError, match="no_everyone"):
        await _ask(hass)
    assert conn.events == []


async def test_the_apps_old_house_gate_is_not_a_refusal(hass, w):
    """D-20261122-aafb: no global House calls switch; an app that still reports `house_gate` false is not refused for it."""
    conn = _panel_online(hass, beat={**BEAT, "house_gate": False, "house_switch": False})
    task = hass.async_create_task(_ask(hass))
    await _answer(hass, conn)
    assert (await task)["ok"] is True


async def test_one_request_in_flight_and_one_per_cooldown(hass, w):
    conn = _panel_online(hass)
    first = hass.async_create_task(_ask(hass))
    for _ in range(20):
        await asyncio.sleep(0)
    with pytest.raises(ServiceValidationError, match="in_progress"):
        await _ask(hass)
    await _answer(hass, conn)
    await first
    with pytest.raises(ServiceValidationError, match="rate_limited"):      # just asked: whatever the outcome
        await _ask(hass)
    assert len(conn.events) == 1


async def test_the_apps_refusal_is_the_scripts_error(hass, w):
    conn = _panel_online(hass)
    task = hass.async_create_task(_ask(hass))
    await _answer(hass, conn, ok=False, code="no_front", text="The panel app could not come to the front")
    with pytest.raises(ServiceValidationError, match="no_front: The panel app could not come to the front"):
        await task


async def test_an_app_that_never_answers_times_out_and_a_late_answer_is_unknown(hass, hass_storage):
    h = await _world(hass, hass_storage)
    for who in ("alice", "dina", "panel"):
        h.online(who)
    calls = PanelCalls(hass, ack_timeout=0.05)
    hass.data.setdefault(DOMAIN, {})["panel_call"] = calls
    async_setup_panel_call(hass)
    conn = _panel_online(hass)
    with pytest.raises(HomeAssistantError, match="no_answer"):
        await _ask(hass)
    rid = conn.events[0]["request_id"]
    assert calls.result(PANEL_USER, rid, True, None, None) is False         # late: nothing is waiting any more


async def test_no_panel_and_two_panels(hass, hass_storage):
    h = await _world(hass, hass_storage)
    async_setup_panel_call(hass)
    calls = get_panel_calls(hass)
    from ..phone_entry import CONF_PANEL

    entry = h.entry
    hass.config_entries.async_update_entry(entry, options={**entry.options, CONF_PANEL: False})
    with pytest.raises(ServiceValidationError, match="no_panel"):
        await calls.async_call_everyone(None, caller=None)


async def test_the_answer_command_guards(hass, w, hass_ws_client):
    from .test_panel_house import _ws
    from .test_push_registry import _phone_user

    user, token, entry, client = await _ws(hass, hass_ws_client)          # an enrolled phone that is NOT a panel
    await client.send_json({"id": 1, "type": "portico/panel/call_result", "request_id": "0123456789abcdef", "ok": True})
    msg = await client.receive_json()
    assert msg["success"] is False and msg["error"]["code"] == "not_a_panel"
    await client.send_json({"id": 2, "type": "portico/panel/call_result", "request_id": "short", "ok": True})
    assert (await client.receive_json())["success"] is False               # the id's shape is checked by the schema
    # an ordinary (non-phone) login
    ordinary = await hass.auth.async_create_user(name="Someone", group_ids=["system-users"])
    refresh = await hass.auth.async_create_refresh_token(ordinary, client_name="t", token_type="long_lived_access_token", access_token_expiration=__import__("datetime").timedelta(days=1))
    other = await hass_ws_client(hass, access_token=hass.auth.async_create_access_token(refresh))
    await other.send_json({"id": 1, "type": "portico/panel/call_result", "request_id": "0123456789abcdef", "ok": True})
    msg = await other.receive_json()
    assert msg["success"] is False and msg["error"]["code"] == "unauthorized"


async def test_another_logins_answer_does_not_resolve_a_request(hass, w):
    conn = _panel_online(hass)
    task = hass.async_create_task(_ask(hass))
    for _ in range(20):
        await asyncio.sleep(0)
    rid = conn.events[-1]["request_id"]
    calls = get_panel_calls(hass)
    assert calls.result("u-alice", rid, True, None, None) is False         # not the panel's login
    assert calls.result(PANEL_USER, "f" * 16, True, None, None) is False   # not this request
    assert calls.result(PANEL_USER, rid, True, None, None) is True
    assert calls.result(PANEL_USER, rid, True, None, None) is False        # once
    assert (await task)["ok"] is True
