# ruff: noqa: F811 - test parameters named after the imported fixtures
"""Talk to the door, the server gate (DEV-658 B7a, `portico_blocks_design.md` 7.3, 7.4, 7.6): INERT.

No shipped driver reports `CanDial`, so on a real install the Talk button is off (`talk_off`) and a press dials nothing.
Everything that happens past that gate is tested here with a FAKE door that does report it. What is pinned:

- the reasons (`talk_off`, `no_door`, `not_connected`, `busy`, `phone_only`) and the states of a call in progress
  (`connecting`, `in_call`, `ended`), in the order the design gives, `talk_off` first among the door's;
- `content/run` of a Talk id: the id IS in the grant, and the provider's `dial` is called only when the caller is the
  phone endpoint's own login, the block's door exists, reports `CanDial` exactly True, is up and has no call going;
  a dashboard login and an administrator naming a phone get `not_allowed`; a start returns the real call id, a second
  press hangs up by it; a dial with no call id is `failed`;
- nothing else reachable from `content/run` dials, no shipped driver can, and the rule/save refusals still hold.
"""
from __future__ import annotations

import ast
import asyncio
import hashlib
import re
import time
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest

from .. import content as content_module
from ..content import RunRefused, TALK_CAP_S, get_content_store
from ..content_resolve import endpoint_rev, resolve_action
from ..endpoints import KIND_DASHBOARD, KIND_PHONE
from ..provider import DATA_PROVIDERS, DOMAIN, ProviderError
from .driver_part import CallStateTracker, TransportProvider, _add_device
from .test_content_wire_blocks import _doc4, _entrance, _fake, _garden, _save, _states
from .test_editor_commands import PHONE_ID, _client, _send, world  # noqa: F401 - fixtures by name

pytestmark = pytest.mark.asyncio

DOOR = "fake:gate-1"
KEY = ("fake", "gate-1")
REPO = Path(__file__).resolve().parents[3]


# ---- helpers ------------------------------------------------------------------------------------------------------

def _arm(world, *, can_dial=True, available=True, caps=None):
    """The world's fake door, re-announced with (or without) `CanDial`."""
    capabilities = dict(caps or {})
    if can_dial:
        capabilities["CanDial"] = True
    _fake(world).add_door("gate-1", name="Gate", camera_entity_id="camera.gate", capabilities=capabilities,
                          available=available)


def _publish_outbound(fake):
    """Make the fake provider publish the tracker's outbound state in its call snapshot (P2: `outbound_state`,
    `outbound_call_id`), as a real provider would once it exposes the tracker. `fake.outbound = (state, call_id)`."""
    fake.outbound = ("idle", None)
    original = type(fake).call_state

    def call_state(self, door_id):
        snapshot = original(self, door_id)
        if snapshot is not None:
            snapshot = {**snapshot, "outbound_state": self.outbound[0], "outbound_call_id": self.outbound[1]}
        return snapshot

    fake.call_state = MethodType(call_state, fake)


async def _ready(world, *, with_screen=False, doc=None, **arm):
    """Save a document with a Talk button in the entrance block and arm the door (`CanDial` unless told not to)."""
    document = doc or _doc4(_entrance(), _garden())
    if with_screen:
        document["dashboards"] = {"kitchen": {"name": "Kitchen tablet", "ha_user_id": world.tablet.id, "room": "Kitchen",
                                              "cameras": []}}
    await _save(world, document)
    _arm(world, **arm)
    world.store._last_run.clear()


def _talk_of(world, endpoint=PHONE_ID):
    page = world.store.resolve_document(endpoint)["pages"][0]
    (talk,) = [a for b in page["blocks"] for a in b["actions"] if a["kind"] == "talk"]
    return talk


def _phone_endpoint(world):
    return world.store._endpoint_by_id(PHONE_ID)


async def _press(world, caller="phone", *, msg_id=1, endpoint=None, hass_ws_client=None):
    clients = world.__dict__.setdefault("_talk_clients", {})
    key = caller
    if key not in clients:
        clients[key] = await _client(world, hass_ws_client, caller)
    body = {"action_id": "talk"}
    if endpoint:
        body["endpoint"] = endpoint
    return await _send(clients[key], "portico/content/run", msg_id, **body)


# ---- today: nothing can dial ------------------------------------------------------------------------------------------

async def test_today_the_id_is_in_the_grant_the_button_is_off_and_a_press_dials_nothing(world, hass_ws_client):
    await _save(world, _doc4(_entrance(), _garden()))                       # the door reports no CanDial, as every door today
    assert "talk" in [a["id"] for a in world.store._all_actions(PHONE_ID)]  # the id is known: a refusal is not "unknown_action"
    phone = await _client(world, hass_ws_client, "phone")
    got = (await _send(phone, "portico/content/get", 1))["result"]
    (talk,) = [a for b in got["pages"][0]["blocks"] for a in b["actions"] if a["kind"] == "talk"]
    assert talk["available"] is False and talk["reason"] == "talk_off"
    assert "actions" not in got["pages"][0] and "cameras" not in got["pages"][0]      # no flat form at all (B8): Talk is in the blocks only
    resp = await _send(phone, "portico/content/run", 2, action_id="talk")
    assert resp["success"] is False and resp["error"]["code"] == "not_available"
    assert resp["error"]["message"] == "Talk to the door is switched off"
    fake = _fake(world)
    assert fake.calls_to("dial") == [] and fake.calls_to("hangup") == []
    assert world.store._talk_calls == {}


async def test_talk_off_is_every_state_a_door_without_candial_can_be_in(world):
    """Down, busy, ringing: whatever else is true, a door that cannot dial says `talk_off` (the one state today)."""
    await _save(world, _doc4(_entrance(), _garden()))
    fake = _fake(world)
    assert _talk_of(world)["reason"] == "talk_off"
    fake.set_available("gate-1", False)
    assert _talk_of(world)["reason"] == "talk_off"
    fake.set_available("gate-1", True)
    fake.ring("gate-1", "visitor-1")
    assert _talk_of(world)["reason"] == "talk_off" and _talk_of(world)["available"] is False


@pytest.mark.parametrize("capabilities", [{}, {"CanDial": False}, {"CanDial": None}, {"CanDial": "true"}, {"CanDial": 1},
                                           {"can_dial": True}, {"candial": True}, {"HasRelay": True}])
async def test_only_candial_exactly_true_opens_the_gate(world, hass_ws_client, capabilities):
    await _save(world, _doc4(_entrance(), _garden()))
    _arm(world, can_dial=False, caps=capabilities)
    assert _talk_of(world)["reason"] == "talk_off"
    resp = await _press(world, hass_ws_client=hass_ws_client)
    assert resp["error"]["code"] == "not_available" and _fake(world).calls_to("dial") == []


async def test_a_door_that_reports_candial_is_ready(world):
    await _ready(world)
    assert _talk_of(world)["available"] is True and _talk_of(world)["reason"] is None


# ---- the reasons --------------------------------------------------------------------------------------------------------

def _scenario_ready(world):
    pass


def _scenario_not_connected(world):
    _fake(world).set_available("gate-1", False)


def _scenario_busy_visitor(world):
    _fake(world).ring("gate-1", "visitor-1")


def _scenario_busy_outbound(world):
    _publish_outbound(_fake(world))
    _fake(world).outbound = ("dialing", "out-someone")          # a call nobody here started (another app, a future G2)


def _scenario_busy_other_phones_call(world):
    world.store._talk_calls[KEY] = ("ep-someone-else", "out-1", time.monotonic())


def _scenario_busy_dial_in_flight(world):
    world.store._talk_dialing.add(KEY)


SCENARIOS = {
    "not_connected": (_scenario_not_connected, "not_connected", "not_connected", "The gate is not connected"),
    "busy visitor ringing": (_scenario_busy_visitor, "busy", "busy", "The gate is busy"),
    "busy outbound call": (_scenario_busy_outbound, "busy", "busy", "The gate is busy"),
    "busy another phone's call": (_scenario_busy_other_phones_call, "busy", "busy", "The gate is busy"),
    "busy dial in flight": (_scenario_busy_dial_in_flight, "busy", "busy", "The gate is busy"),
}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
async def test_each_reason_shows_on_the_wire_and_refuses_the_press_without_dialling(world, hass_ws_client, name):
    scenario, reason, code, message = SCENARIOS[name]
    await _ready(world)
    scenario(world)
    talk = _talk_of(world)
    assert (talk["available"], talk["reason"]) == (False, reason)
    resp = await _press(world, hass_ws_client=hass_ws_client)
    assert resp["success"] is False and resp["error"]["code"] == code and resp["error"]["message"] == message
    assert _fake(world).calls_to("dial") == []


async def test_a_door_that_is_not_there_is_no_door_and_the_press_is_refused(world, hass_ws_client):
    document = _doc4(_entrance(), _garden())
    document["template"]["pages"][0]["blocks"][0]["door"] = "fake:nobody"
    await _ready(world, doc=document)
    assert (_talk_of(world)["available"], _talk_of(world)["reason"]) == (False, "no_door")
    resp = await _press(world, hass_ws_client=hass_ws_client)
    assert resp["error"]["code"] == "no_door" and resp["error"]["message"] == "Door removed"
    assert _fake(world).calls_to("dial") == []


async def test_a_door_removed_after_the_save_is_no_door(world):
    await _ready(world)
    _fake(world).remove_door("gate-1")
    assert _talk_of(world)["reason"] == "no_door"


async def test_no_door_comes_before_talk_off(world):
    document = _doc4(_entrance(), _garden())
    document["template"]["pages"][0]["blocks"][0]["door"] = "fake:nobody"
    await _save(world, document)                              # and no CanDial anywhere
    assert _talk_of(world)["reason"] == "no_door"


async def test_a_dashboard_sees_phone_only_whatever_the_door_says(world):
    await _ready(world, with_screen=True)
    assert _talk_of(world)["reason"] is None                                  # the phone: ready
    screen = _talk_of(world, "kitchen")
    assert (screen["available"], screen["reason"]) == (False, "phone_only")
    _arm(world, can_dial=False)
    assert _talk_of(world, "kitchen")["reason"] == "phone_only"


async def test_the_reason_is_live_state_and_never_part_of_rev(world):
    await _ready(world)
    ready_rev = world.store.resolve_document(PHONE_ID)["rev"]
    _scenario_not_connected(world)
    assert world.store.resolve_document(PHONE_ID)["rev"] == ready_rev
    _arm(world, can_dial=False)
    document = world.store.resolve_document(PHONE_ID)
    assert document["rev"] == ready_rev and endpoint_rev(document) == ready_rev


async def test_the_resolver_fails_closed_when_the_registry_cannot_say():
    action = {"id": "talk", "label": "Talk", "kind": "talk", "confirm": False}

    class NoTalkState:
        pass

    class Raises:
        def talk_state(self, door, endpoint):
            raise RuntimeError("boom")

    class Truthy:
        def talk_state(self, door, endpoint):
            return {"available": 1, "reason": None}                           # truthy is not True

    for registry in (NoTalkState(), Raises(), Truthy()):
        out = resolve_action(action, states=None, registry=registry, door=DOOR, endpoint=SimpleNamespace(kind="phone"))
        assert out["available"] is False and out["reason"] == "talk_off", registry
    assert resolve_action(action, states=None, registry=NoTalkState())["name"] == "Talk"


async def test_talk_state_never_raises(world):
    await _ready(world)
    endpoint = _phone_endpoint(world)
    world.store._portico_door = lambda door_id: (_ for _ in ()).throw(RuntimeError("boom"))
    assert world.store.talk_state(DOOR, endpoint) == {"available": False, "reason": "talk_off"}


# ---- who may press -------------------------------------------------------------------------------------------------------

CALLERS = [
    # (who, endpoint it names, error code or None for a start)
    ("phone", None, None),
    ("phone", PHONE_ID, None),
    ("admin", PHONE_ID, "not_allowed"),
    ("admin", "kitchen", "not_allowed"),
    ("tablet", None, "not_allowed"),
    ("tablet", "kitchen", "not_allowed"),
    ("plain", None, "unauthorized"),
    ("plain", PHONE_ID, "unauthorized"),
]


@pytest.mark.parametrize("who,endpoint,expected", CALLERS)
async def test_the_caller_class_matrix_with_a_door_that_can_dial(world, hass_ws_client, who, endpoint, expected):
    """Everything else is in order (door there, up, idle, CanDial true): only the caller decides."""
    await _ready(world, with_screen=True)
    assert "talk" in [a["id"] for a in world.store._all_actions(PHONE_ID)]       # the id is in the grant of the phone
    fake = _fake(world)
    resp = await _press(world, who, endpoint=endpoint, hass_ws_client=hass_ws_client)
    if expected is None:
        assert resp["success"] is True and resp["result"] == {"ran": True, "call_id": "fake-dial-1", "media": None}
        assert fake.calls_to("dial") == [(("gate-1", "", 0), {})]
    else:
        assert resp["success"] is False and resp["error"]["code"] == expected
        assert fake.calls_to("dial") == [] and world.store._talk_calls == {}


async def test_the_phones_own_user_is_the_whole_test_no_role_grants_or_denies_it(world, hass_ws_client):
    """`caller == endpoint's user` is the whole test: no role grants it, none denies it."""
    await _ready(world)
    store = world.store
    phone = _phone_endpoint(world)
    out = await store.async_run(phone.ha_user_id, "Alice", "talk", caller_user_id=phone.ha_user_id, endpoint_id=PHONE_ID)
    assert out["call_id"] == "fake-dial-1"


@pytest.mark.parametrize("caller", [None, "", "somebody-else"])
async def test_the_store_refuses_a_missing_or_foreign_caller(world, caller):
    await _ready(world)
    phone = _phone_endpoint(world)
    with pytest.raises(RunRefused) as refused:
        await world.store.async_run(phone.ha_user_id, "Alice", "talk", caller_user_id=caller, endpoint_id=PHONE_ID)
    assert refused.value.code == "not_allowed"
    assert _fake(world).calls_to("dial") == []


async def test_the_store_refuses_a_user_that_is_not_the_endpoints_user(world):
    await _ready(world)
    with pytest.raises(RunRefused) as refused:           # caller == user_id, but that user is not the phone's
        await world.store.async_run("not-the-phone-user", "X", "talk", caller_user_id="not-the-phone-user",
                                    endpoint_id=PHONE_ID)
    assert refused.value.code == "not_allowed"
    with pytest.raises(RunRefused) as refused:           # an endpoint id the registry does not know
        await world.store.async_run("u", "X", "talk", caller_user_id="u", endpoint_id="nobody")
    assert refused.value.code == "not_allowed" and _fake(world).calls_to("dial") == []


async def test_the_dashboard_endpoint_is_refused_even_for_its_own_user_at_the_store(world):
    await _ready(world, with_screen=True)
    with pytest.raises(RunRefused) as refused:
        await world.store.async_run(world.tablet.id, "Kitchen tablet", "talk", caller_user_id=world.tablet.id,
                                    endpoint_id="kitchen")
    assert refused.value.code == "not_allowed" and _fake(world).calls_to("dial") == []
    assert world.store._endpoint_by_id("kitchen").kind == KIND_DASHBOARD and _phone_endpoint(world).kind == KIND_PHONE


# ---- the call itself --------------------------------------------------------------------------------------------------

async def test_a_start_dials_once_with_empty_host_and_port_zero_and_remembers_the_call(world, hass_ws_client):
    await _ready(world)
    fake = _fake(world)
    resp = await _press(world, hass_ws_client=hass_ws_client)
    assert resp["result"] == {"ran": True, "call_id": "fake-dial-1", "media": None}
    assert fake.calls_to("dial") == [(("gate-1", "", 0), {})]
    registry = world.hass.data[DOMAIN][DATA_PROVIDERS]
    assert registry.for_call("fake-dial-1") is fake and registry.door_of_call("fake-dial-1") == "gate-1"   # portico/hangup works
    assert _talk_of(world)["reason"] == "in_call" and _talk_of(world)["available"] is True
    assert world.store._talk_dialing == set()


async def test_a_second_press_hangs_up_by_the_id_and_dials_nothing(world, hass_ws_client):
    await _ready(world)
    fake = _fake(world)
    await _press(world, msg_id=1, hass_ws_client=hass_ws_client)
    resp = await _press(world, msg_id=2, hass_ws_client=hass_ws_client)                  # inside the rate limit: a stop is exempt
    assert resp["success"] is True and resp["result"] == {"ran": True, "call_id": "fake-dial-1", "hung_up": True}
    assert fake.calls_to("hangup") == [(("fake-dial-1", "hangup"), {})]
    assert len(fake.calls_to("dial")) == 1 and world.store._talk_calls == {}
    assert _talk_of(world)["reason"] is None
    world.store._last_run.clear()
    resp = await _press(world, msg_id=3, hass_ws_client=hass_ws_client)                  # and a third press starts a new call
    assert resp["result"]["call_id"] == "fake-dial-2" and len(fake.calls_to("dial")) == 2


async def test_another_phone_cannot_stop_my_call_it_sees_busy(world):
    await _ready(world)
    await world.store.async_run(world.phone.id, "Alice", "talk", caller_user_id=world.phone.id, endpoint_id=PHONE_ID)
    other = SimpleNamespace(kind=KIND_PHONE, id="ep-other")
    assert world.store.talk_state(DOOR, other) == {"available": False, "reason": "busy"}
    assert world.store.talk_state(DOOR, _phone_endpoint(world)) == {"available": True, "reason": "in_call"}


async def test_losing_candial_mid_call_does_not_trap_the_call(world, hass_ws_client):
    await _ready(world)
    await _press(world, msg_id=1, hass_ws_client=hass_ws_client)
    _arm(world, can_dial=False)                                         # the capability goes away mid-call
    assert _talk_of(world)["reason"] == "in_call"                       # still stoppable
    resp = await _press(world, msg_id=2, hass_ws_client=hass_ws_client)
    assert resp["success"] is True and resp["result"]["hung_up"] is True
    resp = await _press(world, msg_id=3, hass_ws_client=hass_ws_client)
    assert resp["error"]["code"] == "not_available" and len(_fake(world).calls_to("dial")) == 1


async def test_a_dial_result_without_a_call_id_is_a_failure_and_nothing_is_remembered(world, hass_ws_client, caplog):
    await _ready(world)
    fake = _fake(world)
    results = ({"ok": True}, {"ok": True, "call_id": ""}, {"ok": True, "call_id": 7}, {"ok": True, "call_id": None}, "ok", None)
    for n, result in enumerate(results, 1):
        async def dial(door_id, host, port, result=result):
            fake.calls.append(("dial", (door_id, host, port), {}))
            return result

        fake.dial = dial
        world.store._last_run.clear()
        caplog.clear()
        resp = await _press(world, msg_id=n, hass_ws_client=hass_ws_client)
        assert resp["success"] is False and resp["error"]["code"] == "failed", result
        assert "returned no call id" in caplog.text
        assert world.store._talk_calls == {} and world.store._talk_dialing == set()


async def test_a_dial_that_raises_is_a_refusal_with_its_code(world, hass_ws_client):
    await _ready(world)
    _fake(world).raise_on["dial"] = ProviderError("busy", "ERR busy")
    resp = await _press(world, hass_ws_client=hass_ws_client)
    assert resp["error"]["code"] == "busy" and resp["error"]["message"] == "ERR busy"
    assert world.store._talk_calls == {} and world.store._talk_dialing == set()
    _fake(world).raise_on["dial"] = RuntimeError("socket")
    world.store._last_run.clear()
    resp = await _press(world, msg_id=2, hass_ws_client=hass_ws_client)
    assert resp["error"]["code"] == "failed" and "socket" not in str(resp)


async def test_a_press_inside_the_rate_limit_is_refused_before_the_dial(world, hass_ws_client):
    await _ready(world)
    fake = _fake(world)

    async def no_id(door_id, host, port):
        fake.calls.append(("dial", (door_id, host, port), {}))
        return {"ok": True}

    fake.dial = no_id
    first = await _press(world, msg_id=1, hass_ws_client=hass_ws_client)
    second = await _press(world, msg_id=2, hass_ws_client=hass_ws_client)
    assert first["error"]["code"] == "failed" and second["error"]["code"] == "rate_limited"
    assert len(fake.calls_to("dial")) == 1


async def test_two_presses_at_once_dial_once(world):
    await _ready(world)
    fake = _fake(world)
    release, entered = asyncio.Event(), asyncio.Event()

    async def slow_dial(door_id, host, port):
        fake.calls.append(("dial", (door_id, host, port), {}))
        entered.set()
        await release.wait()
        return {"ok": True, "call_id": "out-slow"}

    fake.dial = slow_dial
    phone = _phone_endpoint(world)

    def press():
        return world.store.async_run(phone.ha_user_id, "Alice", "talk", caller_user_id=phone.ha_user_id, endpoint_id=PHONE_ID)

    first = asyncio.ensure_future(press())
    await entered.wait()
    assert _talk_of(world)["reason"] == "busy"                              # the dial is in flight
    world.store._last_run.clear()
    with pytest.raises(RunRefused) as refused:
        await press()
    assert refused.value.code == "busy"
    release.set()
    assert (await first)["call_id"] == "out-slow" and len(fake.calls_to("dial")) == 1


# ---- a call in progress, from the provider's own outbound state (P2) -------------------------------------------------------

@pytest.mark.parametrize("outbound,phase", [("dialing", "connecting"), ("in_call", "in_call")])
async def test_the_providers_outbound_state_gives_the_phase(world, outbound, phase):
    await _ready(world)
    fake = _fake(world)
    _publish_outbound(fake)
    phone = _phone_endpoint(world)
    out = await world.store.async_run(phone.ha_user_id, "S", "talk", caller_user_id=phone.ha_user_id, endpoint_id=PHONE_ID)
    fake.outbound = (outbound, out["call_id"])
    assert world.store.talk_state(DOOR, phone) == {"available": True, "reason": phase}
    # the stop hangs up the id the provider publishes
    again = await world.store.async_run(phone.ha_user_id, "S", "talk", caller_user_id=phone.ha_user_id, endpoint_id=PHONE_ID)
    assert again == {"call_id": out["call_id"], "hung_up": True}
    assert fake.calls_to("hangup") == [((out["call_id"], "hangup"), {})]


async def test_ended_is_one_look_then_ready_and_the_press_starts_again(world):
    await _ready(world)
    fake = _fake(world)
    _publish_outbound(fake)
    phone = _phone_endpoint(world)
    await world.store.async_run(phone.ha_user_id, "S", "talk", caller_user_id=phone.ha_user_id, endpoint_id=PHONE_ID)
    fake.outbound = ("ended", "fake-dial-1")
    assert world.store.talk_state(DOOR, phone) == {"available": True, "reason": "ended"}
    assert world.store.talk_state(DOOR, phone) == {"available": True, "reason": None}   # the memory went with the end
    fake.outbound = ("idle", None)
    world.store._last_run.clear()
    out = await world.store.async_run(phone.ha_user_id, "S", "talk", caller_user_id=phone.ha_user_id, endpoint_id=PHONE_ID)
    assert out["call_id"] == "fake-dial-2" and len(fake.calls_to("dial")) == 2


async def test_an_ended_call_does_not_skip_the_other_checks(world):
    await _ready(world)
    fake = _fake(world)
    _publish_outbound(fake)
    phone = _phone_endpoint(world)
    await world.store.async_run(phone.ha_user_id, "S", "talk", caller_user_id=phone.ha_user_id, endpoint_id=PHONE_ID)
    fake.outbound = ("ended", "fake-dial-1")
    fake.ring("gate-1", "visitor-1")                                         # a visitor rings right after
    world.store._last_run.clear()
    with pytest.raises(RunRefused) as refused:
        await world.store.async_run(phone.ha_user_id, "S", "talk", caller_user_id=phone.ha_user_id, endpoint_id=PHONE_ID)
    assert refused.value.code == "busy" and len(fake.calls_to("dial")) == 1


async def test_the_providers_idle_outbound_state_drops_a_remembered_call(world):
    await _ready(world)
    fake = _fake(world)
    phone = _phone_endpoint(world)
    await world.store.async_run(phone.ha_user_id, "S", "talk", caller_user_id=phone.ha_user_id, endpoint_id=PHONE_ID)
    _publish_outbound(fake)                                                   # from now the provider is believed: idle
    assert world.store.talk_state(DOOR, phone)["reason"] is None and world.store._talk_calls == {}


async def test_without_an_outbound_feed_a_remembered_call_expires_at_the_cap(world):
    await _ready(world)
    phone = _phone_endpoint(world)
    await world.store.async_run(phone.ha_user_id, "S", "talk", caller_user_id=phone.ha_user_id, endpoint_id=PHONE_ID)
    owner, call_id, started = world.store._talk_calls[KEY]
    world.store._talk_calls[KEY] = (owner, call_id, started - TALK_CAP_S - 1)
    assert world.store.talk_state(DOOR, phone) == {"available": True, "reason": None} and world.store._talk_calls == {}
    world.store._talk_calls[KEY] = ("ep-other", "x", time.monotonic() - TALK_CAP_S - 1)       # someone else's stale one too
    assert world.store.talk_state(DOOR, phone)["reason"] is None and world.store._talk_calls == {}


async def test_a_hangup_that_says_the_call_is_gone_forgets_it_and_any_other_failure_keeps_it(world):
    await _ready(world)
    fake = _fake(world)
    phone = _phone_endpoint(world)

    def press():
        return world.store.async_run(phone.ha_user_id, "S", "talk", caller_user_id=phone.ha_user_id, endpoint_id=PHONE_ID)

    await press()
    fake.raise_on["hangup"] = ProviderError("hub_down", "no hub")
    with pytest.raises(RunRefused) as refused:
        await press()
    assert refused.value.code == "hub_down" and KEY in world.store._talk_calls
    fake.raise_on["hangup"] = ProviderError("unknown_call", "gone")
    with pytest.raises(RunRefused) as refused:
        await press()
    assert refused.value.code == "unknown_call" and world.store._talk_calls == {}
    assert len(fake.calls_to("dial")) == 1


# ---- nothing else dials ----------------------------------------------------------------------------------------------------

async def test_no_other_action_of_content_run_ever_dials_even_with_a_door_that_can_dial(world, hass_ws_client):
    await _ready(world)
    fake = _fake(world)
    from pytest_homeassistant_custom_component.common import async_mock_service
    async_mock_service(world.hass, "lock", "unlock")
    async_mock_service(world.hass, "cover", "open_cover")
    async_mock_service(world.hass, "scene", "turn_on")
    phone = await _client(world, hass_ws_client, "phone")
    ids = [a["id"] for a in world.store._all_actions(PHONE_ID) if a["kind"] != "talk"]
    assert ids == ["open-door", "unlock", "evening", "gate-open"]
    for n, action_id in enumerate(ids, 1):
        world.store._last_run.clear()
        await _send(phone, "portico/content/run", n, action_id=action_id)
    assert fake.calls_to("open_relay") == [(("gate-1",), {})]
    assert fake.calls_to("dial") == [] and fake.calls_to("hangup") == []


@pytest.mark.parametrize("state", ["no_candial", "not_connected", "busy", "no_door"])
@pytest.mark.parametrize("who,endpoint", [("phone", None), ("admin", PHONE_ID), ("tablet", None), ("tablet", "kitchen")])
async def test_provider_dial_is_never_called_on_a_refusal_path(world, hass_ws_client, state, who, endpoint):
    """A provider whose `dial` explodes: no caller class in a refusing state reaches it."""
    document = _doc4(_entrance(), _garden())
    if state == "no_door":
        document["template"]["pages"][0]["blocks"][0]["door"] = "fake:nobody"
    await _ready(world, with_screen=True, doc=document, can_dial=(state != "no_candial"))
    fake = _fake(world)

    async def explode(door_id, host, port):
        raise AssertionError("provider.dial was called on a refusal path")

    fake.dial = explode
    if state == "not_connected":
        fake.set_available("gate-1", False)
    if state == "busy":
        fake.ring("gate-1", "visitor-1")
    resp = await _press(world, who, endpoint=endpoint, hass_ws_client=hass_ws_client)
    assert resp["success"] is False and resp["error"]["code"] != "internal_error", resp
    assert fake.calls_to("dial") == []


async def test_the_only_dial_call_in_the_ui_part_reachable_from_content_run_is_in_run_talk():
    """Static: the UI part calls `.dial(` in exactly two places: `portico/dial` (the app's own command, `admin, phone`,
    which `content/run` cannot reach) and `ContentStore._run_talk`, after the gate."""
    sites = []
    for path in sorted((REPO / "custom_components" / "portico").glob("*.py")):
        tree = ast.parse(path.read_text())
        parents = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "dial":
                fn = node
                while fn in parents and not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    fn = parents[fn]
                sites.append((path.name, fn.name))
    assert sorted(sites) == [("content.py", "_run_talk"), ("signaling.py", "handle_dial")], sites
    assert "get_call_registry().place" not in (REPO / "custom_components/portico/content.py").read_text().split("async def _run_talk")[1].split("async def _talk_hangup")[0]


async def test_run_talk_calls_dial_after_every_refusal_in_source_order():
    """Static: inside `_run_talk` the `.dial(` call comes after each `raise RunRefused` of the gate."""
    source = (REPO / "custom_components/portico/content.py").read_text()
    body = source.split("async def _run_talk")[1].split("async def _talk_hangup")[0]
    dial_at = body.index(".dial(")
    for needle in ('"not_allowed"', '_TALK_REFUSALS', 'getattr(door, "can_dial", False) is not True', '"rate_limited"'):
        assert body.index(needle) < dial_at, needle
    assert body.count(".dial(") == 1


async def test_only_the_gate_drivers_can_report_candial():
    """P3-A: the Rust capability struct has `can_dial: bool`, written on the wire as `"CanDial":true` only when true (so no
    other driver's configure reply gains a key), and only the gate_sip_server driver (behind its off-by-default
    `dial_enabled`), the unifi_protect_talk driver (its own `dial_enabled`, D-20261111-b558), the struct itself, the
    dispatch tests and the dahua driver's explicit `can_dial: false` name it. No other driver's source does, so none can
    report it. B7a's gate is still what stands between a driver that can and a live call."""
    src = REPO / "transport-rs/src"
    hits = sorted(str(p.relative_to(src)) for p in src.rglob("*.rs") if re.search(r"can_dial|CanDial", p.read_text()))
    allowed_prefixes = ("driver/mod.rs", "dispatch/tests/",
                        "driver/dahua/mod.rs",            # sets it to false only (checked below)
                        "driver/gatesipserver/",          # W4 (DEV-518): the server's own dial, behind its `dial_enabled`
                        "driver/unifiprotect/")           # D-20261111-b558: dial out to the front door, behind its `dial_enabled`
    assert hits and all(h.startswith(allowed_prefixes) for h in hits), hits
    driver = (src / "driver/mod.rs").read_text()
    struct = re.search(r"pub struct Capabilities \{(.*?)\n\}", driver, re.S).group(1)
    assert "pub can_dial: bool," in struct
    assert re.search(r'if self\.can_dial \{\s*s\.push_str\(",\\"CanDial\\":true"\);', driver), "CanDial must be written only when true"
    dahua = (src / "driver/dahua/mod.rs").read_text()
    assert re.findall(r"can_dial\b[^\n]*", dahua) == ["can_dial: false,"]


SHIPPED_CAPABILITIES = {       # what each shipped driver's `Capabilities()` returns today (driver_part.py, unifiprotect.go)
    "gate_sip_server": {"InboundAudio": 0, "OutboundAudio": 0, "DirectCapable": False, "HasRelay": False,
                        "RTSPTemplate": "", "ReportsAvailability": False},     # gatesipserver (capabilities_golden_test.go)
    "dahua_sip": {"InboundAudio": 0, "OutboundAudio": 0, "DirectCapable": True, "HasRelay": True,
                  "RTSPTemplate": "", "ReportsAvailability": False},
    "unifi_protect": {"InboundAudio": 2, "OutboundAudio": 4, "DirectCapable": False, "HasRelay": True,
                      "RTSPTemplate": "rtsps://{nvr_host}/{camera_id}", "ReportsAvailability": True},
    # ha_event has no Go driver: its capabilities are the Python driver's own (drivers/ha_event.py), ring-only
    "ha_event": {"InboundAudio": 3, "OutboundAudio": 3, "DirectCapable": False, "HasRelay": False,
                 "RTSPTemplate": "", "ReportsAvailability": True},
}


async def test_every_shipped_driver_is_covered_by_the_walk_below():
    drivers = {p.stem for p in (REPO / "custom_components/portico/door/drivers").glob("*.py") if p.stem != "__init__"}
    assert drivers == set(SHIPPED_CAPABILITIES)                 # a new driver must be added to the walk (and to the check above)


async def test_an_editor_made_talk_button_is_in_the_grant_greyed_and_a_forged_run_dials_nothing(world, hass_ws_client):
    """DEV-692 T1: the editor now creates Talk buttons (portico-panel.js `addAction`): the tile it writes is
    `{id, label, kind: "talk", icon, color, show_label, confirm: false}` and no `device_id`. Saved through the real
    save path, in a block with a door, while the door reports no `CanDial`: the id IS in the grant (so a refusal is not
    "unknown_action"), the button resolves `available: false, reason: talk_off`, and a forged `content/run` of it, or
    dials nothing and hangs up nothing. Then the door gets `CanDial`: the same saved
    tile resolves ready, which proves the document was valid all along and only the gate held it off."""
    block = _entrance()
    block["actions"] = [{"id": "talk-to-gate", "label": "Talk to gate", "kind": "talk", "icon": "account-voice",
                         "color": "blue", "show_label": False, "confirm": False}]
    await _save(world, _doc4(block, _garden()))
    assert "talk-to-gate" in [a["id"] for a in world.store._all_actions(PHONE_ID)]       # the id is known
    (talk,) = [a for p in world.store.resolve_document(PHONE_ID)["pages"] for b in p["blocks"] for a in b["actions"]
               if a["id"] == "talk-to-gate"]
    assert (talk["available"], talk["reason"]) == (False, "talk_off")
    phone = await _client(world, hass_ws_client, "phone")
    resp = await _send(phone, "portico/content/run", 1, action_id="talk-to-gate")
    assert resp["success"] is False and resp["error"]["code"] == "not_available", resp      # refused by the gate, not unknown
    assert resp["error"]["message"] == "Talk to the door is switched off"
    fake = _fake(world)
    assert fake.calls_to("dial") == [] and fake.calls_to("hangup") == []
    assert world.store._talk_calls == {} and world.store._talk_dialing == set()
    _arm(world)                                                              # the door now reports CanDial
    (ready,) = [a for p in world.store.resolve_document(PHONE_ID)["pages"] for b in p["blocks"] for a in b["actions"]
                if a["id"] == "talk-to-gate"]
    assert (ready["available"], ready["reason"]) == (True, None)


@pytest.mark.parametrize("driver", sorted(SHIPPED_CAPABILITIES))
async def test_every_shipped_driver_door_cannot_dial_and_a_press_is_refused(world, hass_ws_client, driver):
    """A REAL `portico_intercom` door per shipped driver, attached to the real provider: `can_dial` is False on the
    runtime and on the door the registry serves, the button is off, a press is refused and the transport's `dial` is
    never reached."""
    from unittest.mock import AsyncMock

    client = AsyncMock()
    device_id = f"10.5.0.{40 + sorted(SHIPPED_CAPABILITIES).index(driver)}"
    _entry, runtime = _add_device(world.hass, device_id=device_id, driver=driver,
                                  capabilities=dict(SHIPPED_CAPABILITIES[driver]), client=client)
    assert runtime.can_dial is False
    registry = world.hass.data[DOMAIN][DATA_PROVIDERS]
    provider, door = registry.door(device_id)
    assert door.can_dial is False and "CanDial" not in door.capabilities
    document = _doc4({"id": "front", "title": "Front", "door": device_id,
                      "cameras": [], "actions": [{"id": "talk", "label": "Talk", "kind": "talk", "confirm": False}]})
    await _save(world, document)
    assert (_talk_of(world)["available"], _talk_of(world)["reason"]) == (False, "talk_off")
    resp = await _press(world, hass_ws_client=hass_ws_client)
    assert resp["success"] is False and resp["error"]["code"] == "not_available"
    assert resp["error"]["message"] == "Talk to the door is switched off"
    client.dial.assert_not_called()
    client.hangup.assert_not_called()
    assert world.store._talk_calls == {}


@pytest.mark.parametrize("driver", ["gate_sip_server"])
async def test_a_real_door_that_reports_candial_would_be_dialled_through_the_provider_once(world, driver):
    """The same real door with the capability forced on: the gate lets exactly one dial through to the transport.
    (Not a shipped state: it shows the gate is the only thing that stood in the way.)"""
    from unittest.mock import AsyncMock

    client = AsyncMock()
    client.dial.return_value = {"ok": True, "call_id": "out-0123456789abcdef"}
    _entry, runtime = _add_device(world.hass, device_id="10.3.0.50", driver=driver,
                                  capabilities={**SHIPPED_CAPABILITIES[driver], "CanDial": True},
                                  client=client)
    assert runtime.can_dial is True
    runtime.transport_available = True                   # the transport is up and the server reports its device up
    runtime.driver_available = True
    assert world.hass.data[DOMAIN][DATA_PROVIDERS].door("10.3.0.50")[1].available is True
    await _save(world, _doc4({"id": "front", "title": "Front", "door": "10.3.0.50", "cameras": [],
                              "actions": [{"id": "talk", "label": "Talk", "kind": "talk", "confirm": False}]}))
    phone = _phone_endpoint(world)
    out = await world.store.async_run(phone.ha_user_id, "S", "talk", caller_user_id=phone.ha_user_id, endpoint_id=PHONE_ID)
    assert out == {"call_id": "out-0123456789abcdef", "media": None}
    client.dial.assert_awaited_once_with("10.3.0.50", "", 0)


# ---- the rest of the design's guarantees -----------------------------------------------------------------------------------

async def test_a_talk_button_in_a_rule_is_still_refused_on_save_and_never_rings(world):
    from ..content import ProfileError

    document = _doc4(_entrance(), _garden(), rules=[
        {"id": "r", "caller": "kind:door", "cameras": None, "ring_buttons": ["talk"], "call_buttons": []}])
    with pytest.raises(ProfileError, match="a talk button; a ring is not the moment to start a talk"):
        await world.store.async_save_document(document, world.store.doc_rev)


async def test_the_apps_command_list_and_the_commands_sha_are_unchanged():
    commands = (REPO / "contract/app_commands_v3.txt").read_bytes()
    assert hashlib.sha256(commands).hexdigest() == "66a0bf014b1aa91e8622fb299335552aa7066d6cc193753ea28683157d4a4ab2"   # D-20261121-c98a: + portico/panel/call_result (was 1f9521c5...). DEV-807 slice 2: + portico/wall/heartbeat (was b201a602...; the app's vendored copy moves with the app slice). DEV-713 S0: + portico/content/live, live_ice, live_close (was 216c66dc...). DEV-705: + portico/connlog/report (was 30af9a06...). M7 3b-S: + portico/house/media_open (was 7b317e3d... after 3a's four portico/house/* commands, 2556d11a... before)
    assert b"talk" not in commands.lower()


async def test_content_module_has_no_import_of_the_driver_part():
    """The UI part reads the door through the provider protocol only (boundary rule 4.3-3): B7a adds no import."""
    source = (REPO / "custom_components/portico/content.py").read_text()
    assert "portico_intercom" not in source
    assert content_module.TALK_CAP_S == 300.0


# ---- the real provider's snapshot feeds the gate (DEV-658: a dial placed by another app makes the door busy) ------------------

def _real_snapshots(fake, can_dial=True):
    """Make the fake door's `call_state` the driver part's own `TransportProvider.call_state` over a real tracker, the
    way the shipped provider builds it. Returns the tracker."""
    tracker = CallStateTracker("gate-1")
    runtime = SimpleNamespace(call_state=tracker, can_dial=can_dial)
    entry = SimpleNamespace(runtime_data=runtime)
    stub = SimpleNamespace(_entry_for_door=lambda door_id: entry)
    fake.call_state = lambda door_id: TransportProvider.call_state(stub, door_id)
    return tracker


async def test_a_call_another_app_placed_makes_the_door_busy_from_the_real_snapshot(world):
    await _ready(world)
    tracker = _real_snapshots(_fake(world))
    phone = _phone_endpoint(world)
    assert world.store.talk_state(DOOR, phone) == {"available": True, "reason": None}
    tracker.apply_event({"call_id": "out-app", "state": "ringing", "outbound": True})     # nobody here dialled it
    assert world.store.talk_state(DOOR, phone) == {"available": False, "reason": "busy"}
    tracker.apply_event({"call_id": "out-app", "state": "answered", "outbound": True})
    assert world.store.talk_state(DOOR, phone)["reason"] == "busy"
    tracker.apply_event({"call_id": "out-app", "state": "ended", "outbound": True})
    assert world.store.talk_state(DOOR, phone) == {"available": True, "reason": None}


async def test_the_real_snapshot_ends_our_own_remembered_dial_when_the_far_end_hangs_up(world):
    await _ready(world)
    fake = _fake(world)
    tracker = _real_snapshots(fake)
    phone = _phone_endpoint(world)
    out = await world.store.async_run(phone.ha_user_id, "S", "talk", caller_user_id=phone.ha_user_id, endpoint_id=PHONE_ID)
    tracker.apply_event({"call_id": out["call_id"], "state": "answered", "outbound": True})
    assert world.store.talk_state(DOOR, phone)["reason"] == "in_call"
    tracker.apply_event({"call_id": out["call_id"], "state": "ended", "outbound": True})   # the door side hung up
    assert world.store.talk_state(DOOR, phone) == {"available": True, "reason": None} and world.store._talk_calls == {}


async def test_a_visitor_ring_still_makes_the_door_busy_beside_the_real_snapshot(world):
    await _ready(world)
    tracker = _real_snapshots(_fake(world))
    tracker.apply_event({"call_id": "v1", "state": "ringing"})
    assert world.store.talk_state(DOOR, _phone_endpoint(world))["reason"] == "busy"


# ---- a call that ended clears the memory at once (2026-11-09: Talk stuck on `in_call` after the calls had ended) ----------

def _press_fn(world):
    phone = _phone_endpoint(world)

    def press():
        return world.store.async_run(phone.ha_user_id, "S", "talk", caller_user_id=phone.ha_user_id, endpoint_id=PHONE_ID)

    return phone, press


@pytest.mark.parametrize("feed", [True, False])
async def test_dial_then_hangup_is_pressable_at_once(world, feed):
    await _ready(world)
    fake = _fake(world)
    if feed:
        _publish_outbound(fake)
    phone, press = _press_fn(world)
    out = await press()
    if feed:
        fake.outbound = ("in_call", out["call_id"])
    assert world.store.talk_state(DOOR, phone)["reason"] == "in_call"
    world.store._last_run.clear()
    assert (await press())["hung_up"] is True
    if feed:
        fake.outbound = ("idle", None)
    assert world.store.talk_state(DOOR, phone) == {"available": True, "reason": None}
    assert world.store._talk_calls == {}
    world.store._last_run.clear()
    assert (await press())["call_id"] == "fake-dial-2"


async def test_dial_then_the_provider_reports_ended_is_pressable_at_once(world):
    await _ready(world)
    fake = _fake(world)
    _publish_outbound(fake)
    phone, press = _press_fn(world)
    out = await press()
    fake.outbound = ("in_call", out["call_id"])
    assert world.store.talk_state(DOOR, phone)["reason"] == "in_call"
    fake.outbound = ("idle", None)         # the tracker after the driver's `ended`
    assert world.store.talk_state(DOOR, phone) == {"available": True, "reason": None}
    assert world.store._talk_calls == {}


async def test_a_call_that_ended_before_the_dial_returned_does_not_stick(world):
    """`ended` can reach the tracker before the dial's reply does: the memory is written after the tracker is idle."""
    await _ready(world)
    fake = _fake(world)
    _publish_outbound(fake)                 # the feed says idle from the start: the end was already applied
    phone, press = _press_fn(world)
    await press()
    assert world.store.talk_state(DOOR, phone) == {"available": True, "reason": None}
