# ruff: noqa: F811 - test parameters named after the imported fixtures
"""The gate as a roster endpoint and a call target (M7 3f, `portico_house_calls_design.md` 14e and 15.15): INERT.

No door reports `CanDial` today, so on a real install the gate's roster entry and a stored `call` button that targets it
are exactly what they were (unavailable, `not_available`), no new key appears anywhere, a press dials nothing. Everything past that gate is tested here with a FAKE door that does report it.
What is pinned:

- OFF: the roster entry, the button state, the run refusal, the editor's endpoint rows, the house picker and the house
  roster revision are what they were; a stored reference to the gate is kept and shown "not available";
- ON: the roster entry says `can_dial`, the state of the button is Talk's (ready, busy, not connected, phone only), a press
  of the button, or of the gate's `r-...` roster key, goes through Talk's path: ONE `dial` of the provider, with
  `("", 0)`, the `talk_state` frames to the placing phone and to nobody else, a second press hangs up; a screen, an
  administrator and a foreign login are `not_allowed` and dial nothing; the placement registry is not asked to place it;
- the call-target picker (the editor's endpoint rows, the house list) offers the gate only while it can dial; the save is
  not tightened and a stored reference survives the flip both ways;
- the house roster `rev` moves when `CanDial` flips and the attached clients are told (`house_roster_changed`); the document
  `rev` does not (live state, as Talk's availability never is); nothing moves when the gate merely goes up or down or
  rings, and nothing is announced while nothing can dial.
"""
from __future__ import annotations

import copy
import re
from pathlib import Path

import pytest

from ..call_endpoints import NullCallEndpointRegistry, PlaceResult, get_call_registry, set_call_registry
from ..content import announce_gate_change, get_content_store
from ..content_resolve import endpoint_rev
from ..endpoints import get_endpoint_registry
from ..gate_target import async_setup_gate_watch, dialable_doors
from ..house import async_setup_house
from ..talk_relay import get_talk_relay
from .fake_call_registry import FakeCallEndpointRegistry
from .test_content_wire_blocks import _doc4, _entrance, _fake, _garden, _save
from .test_editor_commands import PHONE_ID, _client, _send, world  # noqa: F401 - fixtures by name
from .test_push_registry import _phone_user
from .test_talk_gate import DOOR, _arm, _publish_outbound
from .test_talk_relay import CALL, GATE, _barrier, _call, _script_dial, _states, _subscribe, _talks

pytestmark = pytest.mark.asyncio

REPO = Path(__file__).resolve().parents[3]
CALL_BUTTON = {"id": "call-gate", "label": "Gate", "kind": "call", "target": {"type": "door", "id": DOOR}, "confirm": False}


def _watch(world):
    return async_setup_gate_watch(world.hass, lambda: announce_gate_change(world.hass))


def _doc_with_button(*, target=None):
    block = copy.deepcopy(_entrance())
    block["actions"].append({**CALL_BUTTON, **({"target": target} if target else {})})
    return _doc4(block, _garden())


async def _ready_gate(world, *, can_dial=True, with_screen=False, **arm):
    """A document whose entrance block holds the call button, saved while the gate CAN dial (so the save is allowed), then
    the door armed as asked."""
    document = _doc_with_button()
    if with_screen:
        document["dashboards"] = {"kitchen": {"name": "Kitchen tablet", "ha_user_id": world.tablet.id, "room": "Kitchen",
                                              "cameras": []}}
    _arm(world, can_dial=True)
    await _save(world, document)
    _arm(world, can_dial=can_dial, **arm)
    world.store._last_run.clear()


def _roster_entry(world, viewer=PHONE_ID):
    (entry,) = [e for e in world.store.resolve_document(viewer)["roster"] if e["id"] == DOOR]
    return entry


def _button(world, viewer=PHONE_ID):
    page = world.store.resolve_document(viewer)["pages"][0]
    (button,) = [a for b in page["blocks"] for a in b["actions"] if a["id"] == "call-gate"]
    return button


def _phone_ep(world):
    return world.store._endpoint_by_id(PHONE_ID)


# ---- OFF: today ---------------------------------------------------------------------------------------------------------

async def test_off_the_gate_is_in_the_roster_as_it_was_and_carries_no_new_key(world):
    await _save(world, _doc4(_entrance(), _garden()))
    assert _roster_entry(world) == {"action_id": _roster_entry(world)["action_id"], "id": DOOR, "kind": "door",
                                    "name": "Gate", "available": False, "reason": "not_available"}
    assert "can_dial" not in _roster_entry(world)
    assert dialable_doors(world.hass) == []


async def test_off_a_stored_button_to_the_gate_is_kept_and_shows_not_available_never_an_error(world, hass_ws_client):
    await _ready_gate(world, can_dial=False)
    button = _button(world)
    assert button["target"] == {"type": "door", "id": DOOR}                      # kept
    assert button["target_state"] == {"label": "Gate", "available": False, "reason": "not_available"}
    assert (button["available"], button["reason"]) == (False, "not_available")
    phone = await _client(world, hass_ws_client, "phone")
    got = await _send(phone, "portico/content/get", 1)
    assert got["success"] is True                                                # shown, not an error
    resp = await _send(phone, "portico/content/run", 2, action_id="call-gate")
    assert resp["success"] is False and resp["error"]["code"] == "not_available"
    assert resp["error"]["message"] == "this Home Assistant cannot place calls yet"     # the Null registry's, as before
    assert _fake(world).calls_to("dial") == []


async def test_off_the_roster_key_of_the_gate_is_refused_as_before(world, hass_ws_client):
    await _save(world, _doc4(_entrance(), _garden()))
    phone = await _client(world, hass_ws_client, "phone")
    resp = await _send(phone, "portico/content/run", 1, action_id=_roster_entry(world)["action_id"])
    assert resp["success"] is False and resp["error"]["code"] == "not_available"
    assert _fake(world).calls_to("dial") == []


async def test_off_the_editor_rows_the_house_picker_and_the_gate_watch_say_nothing(world, hass_ws_client):
    from ..editor_commands import _endpoint_rows

    await _save(world, _doc4(_entrance(), _garden()))
    (row,) = [r for r in _endpoint_rows(world.hass) if r["kind"] == "door"]
    assert "can_dial" not in row
    house = async_setup_house(world.hass, enabled=True)
    assert [e.type for e in house.endpoints(caller_user_id=world.phone.id)] == []
    told = []
    watch = _watch(world)
    watch._announce = lambda: told.append(1)
    _fake(world).add_door("gate-1", name="Gate", capabilities={"HasRelay": True})
    _fake(world).remove_door("gate-1")
    _fake(world).add_door("gate-1", name="Gate", capabilities={"HasRelay": True})
    assert told == [] and watch.check() is False


async def test_off_a_save_naming_the_gate_is_accepted_as_it_always_was(world):
    """3f does not tighten the save: a known door endpoint can be named (test_endpoints pins it); only the PICKER is gated."""
    _arm(world, can_dial=False)
    await _save(world, _doc_with_button())
    assert _button(world)["target"] == {"type": "door", "id": DOOR}


async def test_off_the_house_roster_rev_is_what_it_was(world):
    house = async_setup_house(world.hass, enabled=True)
    before = house.roster_rev()
    _arm(world, can_dial=False)
    assert house.roster_rev() == before


# ---- ON: the roster, the picker, the state -------------------------------------------------------------------------------

async def test_on_the_roster_entry_of_the_gate_is_callable_and_says_so(world):
    await _save(world, _doc4(_entrance(), _garden()))
    _arm(world)
    entry = _roster_entry(world)
    assert entry["kind"] == "door" and entry["id"] == DOOR and entry["name"] == "Gate"
    assert entry["can_dial"] is True and entry["available"] is True and entry["reason"] is None
    assert [e.id for e in dialable_doors(world.hass)] == [DOOR]


@pytest.mark.parametrize("capabilities", [{"CanDial": False}, {"CanDial": None}, {"CanDial": "true"}, {"CanDial": 1}, {}])
async def test_only_candial_exactly_true_makes_the_gate_callable(world, capabilities):
    await _save(world, _doc4(_entrance(), _garden()))
    _arm(world, can_dial=False, caps=capabilities)
    assert "can_dial" not in _roster_entry(world) and _roster_entry(world)["available"] is False
    assert dialable_doors(world.hass) == []


async def test_on_the_editor_rows_and_the_house_picker_offer_the_gate(world):
    from ..editor_commands import _endpoint_rows

    await _save(world, _doc4(_entrance(), _garden()))
    _arm(world)
    (row,) = [r for r in _endpoint_rows(world.hass) if r["kind"] == "door"]
    assert row["can_dial"] is True
    house = async_setup_house(world.hass, enabled=True)
    offered = [(e.type, e.id, e.label) for e in house.endpoints(caller_user_id=world.phone.id)]
    assert offered == [("door", DOOR, "Gate")]
    assert house.target_state("door", DOOR) is None                              # the content store places it, not the house


async def test_on_the_picker_does_not_offer_the_gate_to_a_screen(world):
    await _save(world, _doc4(_entrance(), _garden()))
    _arm(world)
    screen_doc = _doc_with_button()
    screen_doc["dashboards"] = {"kitchen": {"name": "Kitchen tablet", "ha_user_id": world.tablet.id, "room": "Kitchen",
                                            "cameras": []}}
    await _save(world, screen_doc)
    house = async_setup_house(world.hass, enabled=True)
    assert [e.id for e in house.endpoints(caller_user_id=world.tablet.id) if e.type == "door"] == []


async def test_on_a_save_may_name_the_gate_and_the_state_is_talks(world):
    await _ready_gate(world)
    button = _button(world)
    assert button["target_state"] == {"label": "Gate", "available": True, "reason": None}
    fake = _fake(world)
    _publish_outbound(fake)
    fake.ring("gate-1", "visitor-1")
    assert _button(world)["target_state"]["reason"] == "busy"
    fake.state("visitor-1", "ended")
    fake.set_available("gate-1", False)
    assert _button(world)["target_state"] == {"label": "Gate", "available": False, "reason": "offline"}
    fake.set_available("gate-1", True)
    assert _button(world)["available"] is True


async def test_on_the_button_agrees_with_a_talk_button_in_every_state(world):
    document = _doc_with_button()
    _arm(world)
    await _save(world, document)
    fake = _fake(world)
    for setup in (lambda: None, lambda: fake.set_available("gate-1", False), lambda: fake.set_available("gate-1", True),
                  lambda: fake.ring("gate-1", "v1")):
        setup()
        page = world.store.resolve_document(PHONE_ID)["pages"][0]
        actions = {a["id"]: a for b in page["blocks"] for a in b["actions"]}
        assert actions["talk"]["available"] == actions["call-gate"]["available"]


async def test_on_a_screen_sees_the_gate_off_with_phone_only(world):
    document = _doc_with_button()
    document["dashboards"] = {"kitchen": {"name": "Kitchen tablet", "ha_user_id": world.tablet.id, "room": "Kitchen",
                                          "cameras": []}}
    _arm(world)
    await _save(world, document)
    entry = _roster_entry(world, "kitchen")
    assert (entry["available"], entry["reason"], entry["can_dial"]) == (False, "phone_only", True)


# ---- ON: placing -------------------------------------------------------------------------------------------------------

async def test_on_the_button_dials_through_talks_path_once_and_the_phone_hears_talk_state(world, hass_ws_client):
    await _ready_gate(world)
    phone = await _client(world, hass_ws_client, "phone")
    await _subscribe(phone)
    _script_dial(world)
    result, events = await _call(phone, "portico/content/run", action_id="call-gate")
    assert result["success"], result
    assert result["result"] == {"ran": True, "call_id": CALL, "media": None}
    assert _states(events) == [("dialing", None), ("in_call", CALL)]
    fake = _fake(world)
    assert fake.calls_to("dial") == [(("gate-1", "", 0), {})]                    # ONE dial, Talk's arguments
    _fake(world).outbound(GATE, CALL, "ended", "in_call")
    assert [e["state"] for e in _talks(await _barrier(phone))] == ["ended"]
    assert get_talk_relay(world.hass)._by_door == {}


async def test_on_nobody_else_hears_the_talk_state_of_a_gate_call(world, hass_ws_client):
    await _ready_gate(world, with_screen=True)
    phone = await _client(world, hass_ws_client, "phone")
    tablet = await _client(world, hass_ws_client, "tablet")
    admin = await _client(world, hass_ws_client)
    for client in (phone, tablet, admin):
        await _subscribe(client)
    _script_dial(world)
    await _call(phone, "portico/content/run", action_id="call-gate")
    _fake(world).outbound(GATE, CALL, "ended", "in_call")
    for bystander in (tablet, admin):
        assert await _barrier(bystander) == []


async def test_on_the_roster_key_of_the_gate_dials_the_same_way(world, hass_ws_client):
    await _save(world, _doc4(_entrance(), _garden()))
    _arm(world)
    world.store._last_run.clear()
    phone = await _client(world, hass_ws_client, "phone")
    await _subscribe(phone)
    _script_dial(world)
    result, events = await _call(phone, "portico/content/run", action_id=_roster_entry(world)["action_id"])
    assert result["success"] and result["result"]["call_id"] == CALL
    assert _states(events) == [("dialing", None), ("in_call", CALL)]
    assert _fake(world).calls_to("dial") == [(("gate-1", "", 0), {})]


async def test_on_a_second_press_hangs_up_by_the_id_and_dials_nothing_more(world, hass_ws_client):
    await _ready_gate(world)
    phone = await _client(world, hass_ws_client, "phone")
    _script_dial(world)
    first, _ = await _call(phone, "portico/content/run", action_id="call-gate")
    second, _ = await _call(phone, "portico/content/run", action_id="call-gate")
    assert first["success"] and second["result"] == {"ran": True, "call_id": CALL, "hung_up": True}
    assert len(_fake(world).calls_to("dial")) == 1 and _fake(world).calls_to("hangup") == [((CALL, "hangup"), {})]


async def test_on_the_gate_button_and_the_talk_button_share_one_call(world, hass_ws_client):
    """A call placed by the Talk button is the call the gate button sees: the second dial is refused (busy), not placed."""
    await _ready_gate(world)
    phone = await _client(world, hass_ws_client, "phone")
    _script_dial(world)
    await _call(phone, "portico/content/run", action_id="talk")
    world.store._last_run.clear()
    again, _ = await _call(phone, "portico/content/run", action_id="call-gate")      # this endpoint's own call: it stops it
    assert again["result"].get("hung_up") is True and len(_fake(world).calls_to("dial")) == 1


async def test_on_a_screen_an_administrator_and_a_foreign_login_cannot_place_it(world, hass_ws_client):
    await _ready_gate(world, with_screen=True)
    tablet = await _client(world, hass_ws_client, "tablet")
    admin = await _client(world, hass_ws_client)
    other = await _client(world, hass_ws_client, "plain")
    for client, extra in ((tablet, {"endpoint": "kitchen", "action_id": _roster_entry(world, "kitchen")["action_id"]}),
                          (admin, {"endpoint": PHONE_ID, "action_id": "call-gate"}),
                          (other, {"action_id": "call-gate"})):
        resp = await _send(client, "portico/content/run", 1, **extra)
        assert resp["success"] is False, resp
        assert resp["error"]["code"] in ("not_allowed", "unauthorized"), resp
    assert _fake(world).calls_to("dial") == []


async def test_on_a_busy_or_down_gate_refuses_with_talks_codes_and_dials_nothing(world, hass_ws_client):
    await _ready_gate(world)
    phone = await _client(world, hass_ws_client, "phone")
    fake = _fake(world)
    _publish_outbound(fake)
    fake.ring("gate-1", "visitor-1")
    busy, _ = await _call(phone, "portico/content/run", action_id="call-gate")
    assert busy["error"]["code"] == "busy"
    fake.state("visitor-1", "ended")
    fake.set_available("gate-1", False)
    world.store._last_run.clear()
    down, _ = await _call(phone, "portico/content/run", action_id="call-gate")
    assert down["error"]["code"] == "not_connected"
    assert fake.calls_to("dial") == []


async def test_on_a_gate_that_loses_candial_between_get_and_run_is_refused_and_dials_nothing(world, hass_ws_client):
    await _ready_gate(world)
    assert _button(world)["available"] is True
    _arm(world, can_dial=False)
    phone = await _client(world, hass_ws_client, "phone")
    resp, _ = await _call(phone, "portico/content/run", action_id="call-gate")
    assert resp["error"]["code"] == "not_available" and _fake(world).calls_to("dial") == []


async def test_on_the_placement_registry_is_not_asked_to_place_the_gate(world, hass_ws_client):
    await _ready_gate(world)
    house = async_setup_house(world.hass, enabled=True)
    placed = []
    original = house.place_call

    def spy(**kwargs):
        placed.append(kwargs)
        return original(**kwargs)

    house.place_call = spy
    phone = await _client(world, hass_ws_client, "phone")
    _script_dial(world)
    resp, _ = await _call(phone, "portico/content/run", action_id="call-gate")
    assert resp["success"] and placed == []
    assert len(_fake(world).calls_to("dial")) == 1


async def test_a_registry_that_knows_the_door_target_still_owns_its_placement(world, hass_ws_client):
    await _ready_gate(world)
    fake_registry = FakeCallEndpointRegistry()
    fake_registry.add("door", DOOR, "Gate (registry)")
    set_call_registry(world.hass, fake_registry)
    assert _button(world)["target_state"]["label"] == "Gate (registry)"
    phone = await _client(world, hass_ws_client, "phone")
    resp, _ = await _call(phone, "portico/content/run", action_id="call-gate")
    assert resp["success"] and len(fake_registry.placed) == 1
    assert _fake(world).calls_to("dial") == []


async def test_a_registry_that_raises_on_the_door_target_means_the_old_path(world, hass_ws_client):
    await _ready_gate(world)
    broken = FakeCallEndpointRegistry()

    def boom(*_a):
        raise RuntimeError("backend bug")

    broken.target_state = boom
    set_call_registry(world.hass, broken)
    assert world.store._gate_door_id({"type": "door", "id": DOOR}) is None
    phone = await _client(world, hass_ws_client, "phone")
    resp, _ = await _call(phone, "portico/content/run", action_id="call-gate")
    assert len(broken.placed) == 1 and _fake(world).calls_to("dial") == []


async def test_a_call_to_anything_but_the_gate_is_placed_as_before_even_with_the_dial_on(world, hass_ws_client):
    _arm(world)
    document = _doc_with_button(target={"type": "dashboard", "id": "kitchen"})
    document["dashboards"] = {"kitchen": {"name": "Kitchen tablet", "ha_user_id": world.tablet.id, "room": "Kitchen",
                                          "cameras": []}}
    await _save(world, document)
    fake_registry = FakeCallEndpointRegistry()
    fake_registry.add("dashboard", "kitchen", "Kitchen tablet")
    set_call_registry(world.hass, fake_registry)
    phone = await _client(world, hass_ws_client, "phone")
    resp, _ = await _call(phone, "portico/content/run", action_id="call-gate")
    assert resp["success"] and fake_registry.placed[0]["target_type"] == "dashboard"
    assert _fake(world).calls_to("dial") == []


# ---- saving: the picker is gated, the save is not ----------------------------------------------------------------------------------------------------------------

async def test_a_stored_reference_survives_the_flip_both_ways_and_a_resave(world):
    await _ready_gate(world)                                  # saved while it could dial
    stored = copy.deepcopy(world.store.document())
    _arm(world, can_dial=False)
    assert _button(world)["target"] == {"type": "door", "id": DOOR}
    await _save(world, stored)                                # saved again while it cannot: kept, not refused, not dropped
    assert world.store.document()["template"]["pages"][0]["blocks"][0]["actions"][-1]["target"] == {"type": "door", "id": DOOR}
    _arm(world)
    assert _button(world)["available"] is True               # and it works again when the dial comes back


async def test_the_save_is_the_same_whether_the_gate_can_dial_or_not(world):
    for can_dial in (False, True, False):
        _arm(world, can_dial=can_dial)
        await _save(world, _doc_with_button())


async def test_a_phone_or_screen_target_is_unaffected_by_the_gate_rule(world):
    document = _doc_with_button(target={"type": "dashboard", "id": "kitchen"})
    document["dashboards"] = {"kitchen": {"name": "Kitchen tablet", "ha_user_id": world.tablet.id, "room": "Kitchen",
                                          "cameras": []}}
    _arm(world, can_dial=False)
    await _save(world, document)


# ---- the revision and the announcement ------------------------------------------------------------------------------------

async def test_the_document_rev_does_not_move_when_candial_flips_it_is_live_state_like_talks(world):
    """test_talk_gate pins that Talk's availability is never part of `rev`; the gate's roster flag follows it, so a reload of
    the gate that turns the dial on does not make every phone and the kiosk refetch. The house roster rev is what moves."""
    await _save(world, _doc4(_entrance(), _garden()))
    _arm(world, can_dial=False)
    off = world.store.resolve_document(PHONE_ID)["rev"]
    _arm(world)
    assert "can_dial" in _roster_entry(world)
    assert world.store.resolve_document(PHONE_ID)["rev"] == off
    _fake(world).set_available("gate-1", False)
    _fake(world).set_available("gate-1", True)
    assert world.store.resolve_document(PHONE_ID)["rev"] == off


async def test_the_pure_rev_ignores_the_can_dial_key():
    base = {"roster": [{"id": "g", "kind": "door", "available": False, "reason": "not_available"}]}
    flipped = {"roster": [{"id": "g", "kind": "door", "available": True, "reason": None, "can_dial": True}]}
    assert endpoint_rev(base) == endpoint_rev(flipped)


async def test_the_gate_watch_announces_a_flip_once_and_a_door_that_leaves_and_returns(world):
    await _save(world, _doc4(_entrance(), _garden()))
    watch = _watch(world)
    told = []
    watch._announce = lambda: told.append(1)
    _arm(world, can_dial=False, caps={"HasRelay": True})     # a door change that leaves the set empty: nothing to say
    assert told == [] and watch.check() is False
    _arm(world)                                              # the capability flips on a door already known
    assert told == [1] and watch.check() is False            # told once, by the provider registry's hook
    fake = _fake(world)
    fake.remove_door("gate-1")                               # a reload: the door leaves ...
    assert told == [1, 1]                                    # ... and the set changed
    _arm(world)                                              # ... and returns dialable
    assert told == [1, 1, 1]
    _arm(world, can_dial=False)
    assert told == [1, 1, 1, 1]
    fake.set_available("gate-1", False)                      # a door merely going down changes no set
    assert told == [1, 1, 1, 1]


async def test_the_watch_adds_no_listener_to_the_provider_registry(world):
    from ..provider import DATA_PROVIDERS, DOMAIN

    registry = world.hass.data[DOMAIN][DATA_PROVIDERS]
    before = (len(registry._listeners), len(registry._arrival_listeners), len(registry._outbound_listeners))
    watch = _watch(world)
    assert (len(registry._listeners), len(registry._arrival_listeners), len(registry._outbound_listeners)) == before
    assert len(registry._doors_hooks) == 1
    watch.stop()
    assert registry._doors_hooks == []


async def test_a_flip_sends_content_changed_so_an_open_app_refetches_talk_state(world, hass_ws_client):
    """The document `rev` does not move, but Talk's availability is live state read at resolve time: a CanDial flip (gate-sip's
    runtime dial switch, republished by the driver) tells every content subscriber to refetch, else an open app keeps a
    disabled (or enabled) Talk button until it is restarted."""
    _watch(world)
    await _save(world, _doc4(_entrance(), _garden()))
    phone = await _client(world, hass_ws_client, "phone")
    await phone.send_json({"id": 7, "type": "portico/content/subscribe"})
    assert (await phone.receive_json())["success"]
    await _barrier(phone)
    _fake(world).remove_door("gate-1")
    _arm(world)
    events = [f["event"] for f in await _barrier(phone) if f.get("type") == "event"]
    assert [e for e in events if e.get("type") == "content_changed"] != []


async def test_the_house_roster_rev_moves_with_candial_and_clients_get_house_roster_changed(world, hass_ws_client):
    _watch(world)
    house = async_setup_house(world.hass, enabled=True)
    phone = await _client(world, hass_ws_client, "phone")
    await _subscribe(phone)
    before = house.roster_rev()
    _fake(world).remove_door("gate-1")
    _arm(world)
    assert house.roster_rev() != before
    events = [f["event"] for f in await _barrier(phone) if f.get("type") == "event"]
    assert [e["type"] for e in events if e["type"] == "house_roster_changed"]
    assert house.roster_rev() == house.roster_rev()
    _arm(world, can_dial=False)
    assert house.roster_rev() == before


# ---- one dial path, statically ---------------------------------------------------------------------------------------------

async def test_there_is_one_dial_call_in_the_ui_part_and_the_new_module_has_none():
    for path in (REPO / "custom_components/portico").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        if path.name == "provider.py":
            continue
        dials = re.findall(r"\.dial\(", text)
        if path.name == "signaling.py":
            assert len(dials) == 1, path.name                       # handle_dial, which content/run cannot reach
        elif path.name == "content.py":
            assert len(dials) == 1, path.name                       # _run_talk
        else:
            assert dials == [], path.name
    source = (REPO / "custom_components/portico/content.py").read_text(encoding="utf-8")
    run_call = source.split("async def _run_call")[1].split("async def async_snapshot")[0]
    assert ".dial(" not in run_call and "_run_talk" not in run_call


async def test_the_gate_dispatch_goes_to_run_talk_and_only_from_async_run():
    source = (REPO / "custom_components/portico/content.py").read_text(encoding="utf-8")
    assert source.count("self._run_talk(") == 3         # the block's Talk, a Talk element (DEV-518, explicit door), the gate call
    assert "door_id=gate_door" in source
    assert 'door_id=action["door"], explicit=True' in source


async def test_the_call_registry_default_is_untouched(hass):
    assert isinstance(get_call_registry(hass), NullCallEndpointRegistry)
    assert PlaceResult(ok=False).ok is False
    assert get_endpoint_registry(hass).endpoints() == []


# ---- DEV-695: the call button says it is the gate call itself, whatever the roster shows -----------------------------------

def _gate_doc(*, hide=False, roster_list=None):
    """The document with the call button, and the gate hidden from the phone's roster by `roster_hide` or by a template
    roster list that leaves it out."""
    document = _doc_with_button()
    if hide:
        document["overrides"] = {PHONE_ID: {"pages": [], "rules": [], "roster_hide": [DOOR]}}
    if roster_list is not None:
        document["template"]["roster"] = roster_list
    return document


def _roster_ids(world, viewer=PHONE_ID):
    return [e["id"] for e in world.store.resolve_document(viewer)["roster"]]


async def test_on_the_gate_call_button_carries_the_marker_and_nothing_else_new(world):
    await _ready_gate(world)
    button = _button(world)
    assert button["gate_call"] is True
    assert button["target"] == {"type": "door", "id": DOOR}              # no id beyond the one the action already carried
    assert set(button) == {"id", "label", "kind", "confirm", "target", "name", "available", "reason", "target_state",
                           "gate_call"}


@pytest.mark.parametrize("how", ["roster_hide", "roster_list", "both"])
async def test_on_the_marker_is_there_when_the_roster_hides_the_gate(world, how):
    document = _gate_doc(hide=how in ("roster_hide", "both"), roster_list=[] if how in ("roster_list", "both") else None)
    _arm(world, can_dial=True)
    await _save(world, document)
    assert DOOR not in _roster_ids(world)                               # the phone's roster has no gate row to detect
    assert _button(world)["gate_call"] is True                          # the action says it itself


async def test_off_the_marker_is_absent_the_dial_off_changes_nothing(world):
    await _ready_gate(world, can_dial=False)
    assert "gate_call" not in _button(world)
    assert _button(world)["target_state"] == {"label": "Gate", "available": False, "reason": "not_available"}


async def test_the_marker_follows_candial_both_ways_and_never_moves_the_rev(world):
    await _ready_gate(world)
    on = world.store.resolve_document(PHONE_ID)["rev"]
    assert _button(world)["gate_call"] is True
    _arm(world, can_dial=False)
    assert "gate_call" not in _button(world)
    assert world.store.resolve_document(PHONE_ID)["rev"] == on          # live state, like can_dial and the availability
    _arm(world)
    assert _button(world)["gate_call"] is True


async def test_the_marker_is_present_for_a_down_gate_it_names_the_route_not_the_state(world):
    await _ready_gate(world, available=False)
    button = _button(world)
    assert button["gate_call"] is True and button["available"] is False


async def test_a_screen_sees_the_marker_too_the_route_is_not_the_viewer(world):
    document = _doc_with_button()
    document["dashboards"] = {"kitchen": {"name": "Kitchen tablet", "ha_user_id": world.tablet.id, "room": "Kitchen",
                                          "cameras": []}}
    _arm(world)
    await _save(world, document)
    button = _button(world, "kitchen")
    assert button["gate_call"] is True and button["reason"] == "phone_only"


async def test_a_registry_that_owns_the_door_target_has_no_marker_its_press_is_not_a_gate_call(world):
    await _ready_gate(world)
    fake_registry = FakeCallEndpointRegistry()
    fake_registry.add("door", DOOR, "Gate (registry)")
    set_call_registry(world.hass, fake_registry)
    assert "gate_call" not in _button(world)


async def test_a_call_to_anything_but_the_gate_has_no_marker_with_the_dial_on(world):
    _arm(world)
    document = _doc_with_button(target={"type": "dashboard", "id": "kitchen"})
    document["dashboards"] = {"kitchen": {"name": "Kitchen tablet", "ha_user_id": world.tablet.id, "room": "Kitchen",
                                          "cameras": []}}
    await _save(world, document)
    assert "gate_call" not in _button(world)


async def test_the_marker_and_the_press_agree(world, hass_ws_client):
    """The marker is the very test the press makes: a marked button dials once through Talk's path."""
    await _ready_gate(world)
    assert _button(world)["gate_call"] is True
    phone = await _client(world, hass_ws_client, "phone")
    resp = await _send(phone, "portico/content/run", 1, action_id="call-gate")
    assert resp["success"] is True and len(_fake(world).calls_to("dial")) == 1


async def test_the_pure_resolver_marks_nothing_for_a_registry_without_the_hook_or_one_that_raises():
    from ..content_resolve import resolve_action

    class _States:
        def get(self, _entity_id):
            return None

    action = {"id": "c", "label": "Gate", "kind": "call", "target": {"type": "door", "id": "g"}, "confirm": False}

    class _Plain:
        def call_state(self, _action):
            return {"label": "Gate", "available": True, "reason": None}

    class _Boom(_Plain):
        def gate_call(self, _target):
            raise RuntimeError("bug")

    class _Yes(_Plain):
        def gate_call(self, _target):
            return True

    class _Truthy(_Plain):
        def gate_call(self, _target):
            return "yes"

    assert "gate_call" not in resolve_action(action, states=_States(), registry=_Plain())
    assert "gate_call" not in resolve_action(action, states=_States(), registry=_Boom())
    assert "gate_call" not in resolve_action(action, states=_States(), registry=_Truthy())     # exactly True, as CanDial is
    assert resolve_action(action, states=_States(), registry=_Yes())["gate_call"] is True


async def test_the_pure_rev_ignores_the_gate_call_key():
    base = {"pages": [{"blocks": [{"actions": [{"id": "c", "kind": "call", "available": False}]}]}]}
    marked = {"pages": [{"blocks": [{"actions": [{"id": "c", "kind": "call", "available": True, "gate_call": True}]}]}]}
    assert endpoint_rev(base) == endpoint_rev(marked)


async def test_live_announcement_reaches_a_scoped_phone_with_a_null_rev(world, hass_ws_client):
    """DEV-806: a scoped phone is told of a `content_changed` with the endpoint's rev, which the app skips when it holds it.
    Live state that is not in the rev (Talk's `busy` while a visitor call runs) is announced with `rev: null`: always refetch."""
    await _save(world, _doc4(_entrance(), _garden()))
    phone = await _client(world, hass_ws_client, "phone")
    await phone.send_json({"id": 7, "type": "portico/content/subscribe"})
    assert (await phone.receive_json())["success"]
    await _barrier(phone)
    get_content_store(world.hass).announce_live()
    events = [f["event"] for f in await _barrier(phone) if f.get("type") == "event"]
    changed = [e for e in events if e.get("type") == "content_changed"]
    assert changed and all(e["rev"] is None and e["endpoint"] for e in changed)
