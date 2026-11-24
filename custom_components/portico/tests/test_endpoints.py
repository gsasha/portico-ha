"""The endpoint registry (PH-4; requirements ENDPOINT-01..03, PROV-03, PROV-06; plan 2.6).

Doors come from the provider registry, phones from the enrolled rows; ids are stable across a door
re-add; a call button resolves its name and availability through the registry while placement stays on
the call-endpoint seam.
"""
from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from ..call_endpoints import set_call_registry
from ..content import get_content_store
from ..endpoints import EndpointRegistry, get_endpoint_registry
from ..provider import DATA_PROVIDERS, DOMAIN, ProviderRegistry
from .fake_call_registry import FakeCallEndpointRegistry
from .fake_provider import FakeProvider
from .blocks_view import resolved as _resolved
from .test_call_buttons import _call
from .test_push_registry import _custom_integrations  # noqa: F401 - autouse fixture by name

pytestmark = pytest.mark.asyncio


async def _phone_user(hass, name):
    return await hass.auth.async_create_user(name, group_ids=["system-users"])


def _providers(hass) -> ProviderRegistry:
    data = hass.data.setdefault(DOMAIN, {})
    if data.get(DATA_PROVIDERS) is None:
        data[DATA_PROVIDERS] = ProviderRegistry(hass)
    return data[DATA_PROVIDERS]


def _provide(hass, domain="fake", *doors, **door_kwargs) -> FakeProvider:
    fake = FakeProvider(domain)
    for door_id in doors:
        fake.add_door(door_id, **door_kwargs)
    _providers(hass).register(fake)
    return fake


def _phone_entry(hass, rows):
    entry = MockConfigEntry(domain=DOMAIN, title="Gate", unique_id="gate_sip_observer:gate.example",
                            data={"driver": "gate_sip_observer", "host": "gate.example", "enrolled_phones": rows})
    entry.add_to_hass(hass)
    return entry


async def test_doors_come_from_providers_and_go_with_them(hass):
    reg = get_endpoint_registry(hass)
    assert isinstance(reg, EndpointRegistry) and reg.endpoints() == []
    fake = _provide(hass, "fake", "gate-1", name="Gate", camera_entity_id="camera.gate")
    (door,) = reg.endpoints(kind="door")
    assert (door.id, door.kind, door.provider, door.device_id, door.name) == ("fake:gate-1", "door", "fake", "gate-1", "Gate")
    assert door.cameras == ("camera.gate",) and door.available and door.reason is None
    assert door.ha_user_id is None and door.room is None and door.override is None
    assert reg.get("fake:gate-1") == door

    fake.set_available("gate-1", False)
    assert reg.get("fake:gate-1").available is False and reg.get("fake:gate-1").reason == "offline"
    fake.set_available("gate-1", True)

    fake.remove_door("gate-1")
    assert reg.endpoints() == [] and reg.get("fake:gate-1") is None
    fake.add_door("gate-1")
    _providers(hass).unregister("fake")        # the whole provider unloads
    assert reg.endpoints(kind="door") == []


async def test_a_door_with_no_camera_has_no_cameras(hass):
    _provide(hass, "fake", "gate-1")
    assert get_endpoint_registry(hass).get("fake:gate-1").cameras == ()


async def test_two_providers_with_the_same_device_id_are_two_endpoints(hass):
    _provide(hass, "fake_a", "gate")
    _provide(hass, "fake_b", "gate")
    ids = [e.id for e in get_endpoint_registry(hass).endpoints(kind="door")]
    assert ids == ["fake_a:gate", "fake_b:gate"]


async def test_a_url_device_id_splits_on_the_first_colon_only(hass):
    _provide(hass, "fake", "http://10.6.0.7:8091")
    endpoint = get_endpoint_registry(hass).get("fake:http://10.6.0.7:8091")
    assert endpoint.provider == "fake" and endpoint.device_id == "http://10.6.0.7:8091"


async def test_phone_ids_are_enroll_ids(hass):
    dina = await _phone_user(hass, "Dina")
    alice = await _phone_user(hass, "Alice")
    _phone_entry(hass, [
        {"ha_user_id": dina.id, "name": "Dina phone", "enroll_id": "enr-dina"},
        {"ha_user_id": alice.id, "name": "Alice phone"},      # enrolled before enroll ids: the user id stands in
    ])
    reg = get_endpoint_registry(hass)
    phones = reg.endpoints(kind="phone")
    assert [(p.id, p.name, p.ha_user_id, p.kind) for p in phones] == [
        ("enr-dina", "Dina phone", dina.id, "phone"), (alice.id, "Alice phone", alice.id, "phone")]
    assert all(p.provider is None and p.device_id is None and p.available for p in phones)
    assert reg.get("enr-dina").name == "Dina phone"
    assert reg.for_user(dina.id).id == "enr-dina"
    assert reg.for_user("nobody") is None


async def test_one_phone_on_two_entries_is_one_endpoint(hass):
    user = await _phone_user(hass, "Dina")
    row = {"ha_user_id": user.id, "name": "Dina phone", "enroll_id": "enr-dina"}
    _phone_entry(hass, [row])
    MockConfigEntry(domain=DOMAIN, title="Gate 2", unique_id="gate_sip_observer:g2",
                    data={"driver": "gate_sip_observer", "host": "g2", "enrolled_phones": [row]}).add_to_hass(hass)
    assert len(get_endpoint_registry(hass).endpoints(kind="phone")) == 1


async def test_endpoint_ids_are_stable_across_a_door_readd(hass):
    """ENDPOINT-02's acceptance: the provider unregisters and re-registers the same door; a page that names
    `fake:gate-1` resolves before, is unknown while the door is away, and resolves again."""
    reg = get_endpoint_registry(hass)
    store = get_content_store(hass)
    _provide(hass, "fake", "gate-1", name="Gate")
    page = {"actions": [_call(id="gate", label="Mine", target={"type": "door", "id": "fake:gate-1"})]}
    await store.async_set("u1", page)                 # a known door endpoint can be named in a profile
    before = reg.get("fake:gate-1")

    _providers(hass).unregister("fake")
    assert reg.get("fake:gate-1") is None
    state = _resolved(store, "u1")["actions"][0]["target_state"]
    assert state == {"label": "Mine", "available": False, "reason": "not_available"}   # the button's own label

    _provide(hass, "fake", "gate-1", name="Gate")
    assert reg.get("fake:gate-1") == before
    assert _resolved(store, "u1")["actions"][0]["target_state"] == {"label": "Gate", "available": False, "reason": "not_available"}
    assert store.profile_for("u1")["actions"][0]["target"] == {"type": "door", "id": "fake:gate-1"}


async def test_listeners_hear_a_door_leave_and_can_unsubscribe(hass):
    reg = get_endpoint_registry(hass)
    fake = _provide(hass, "fake", "gate-1", "gate-2")
    heard = []
    unsub = reg.add_listener(lambda: heard.append([e.id for e in reg.endpoints()]))
    fake.remove_door("gate-1")
    assert heard == [["fake:gate-2"]]
    reg.add_listener(lambda: 1 / 0)               # a raising listener is skipped, the others still run
    fake.remove_door("gate-2")
    assert heard[-1] == []
    unsub()
    reg.notify()
    assert len(heard) == 2


async def test_a_door_arriving_after_setup_notifies_endpoint_listeners(hass):
    reg = get_endpoint_registry(hass)
    fake = _provide(hass, "fake", "gate-1")
    heard = []
    reg.add_listener(lambda: heard.append([e.id for e in reg.endpoints()]))
    fake.add_door("gate-2")
    assert heard == [["fake:gate-1", "fake:gate-2"]]


async def test_a_provider_registering_later_notifies(hass):
    reg = get_endpoint_registry(hass)
    _provide(hass, "fake", "gate-1")
    heard = []
    reg.add_listener(lambda: heard.append([e.id for e in reg.endpoints()]))
    _provide(hass, "other", "door-9")
    assert heard == [["fake:gate-1", "other:door-9"]]


async def test_unloading_stops_notifications(hass):
    reg = get_endpoint_registry(hass)
    fake = _provide(hass, "fake", "gate-1")
    heard = []
    unsub = reg.add_listener(lambda: heard.append(1))
    unsub()
    assert reg._unsub_arrivals is None and reg._unsub_providers is None
    fake.add_door("gate-2")
    fake.remove_door("gate-1")
    assert heard == []
    assert _providers(hass)._arrival_listeners == []          # nothing of the registry's stays attached


# ---- the call button's view ---------------------------------------------------------------------------

async def test_a_door_call_button_takes_name_and_availability_from_the_registry_not_the_placement_seam(hass):
    store = get_content_store(hass)
    fake = _provide(hass, "fake", "gate-1", name="Front gate")
    await store.async_set("u1", {"actions": [_call(id="gate", label="Mine", target={"type": "door", "id": "fake:gate-1"})]})
    assert _resolved(store, "u1")["actions"][0]["target_state"] == {
        "label": "Front gate", "available": False, "reason": "not_available"}      # up, but no call can be placed yet
    fake.set_available("gate-1", False)
    action = _resolved(store, "u1")["actions"][0]
    assert action["target_state"] == {"label": "Front gate", "available": False, "reason": "offline"}
    assert action["name"] == "Front gate" and action["available"] is False


async def test_the_placement_registry_still_wins_for_what_it_knows(hass):
    store = get_content_store(hass)
    _provide(hass, "fake", "gate-1", name="Front gate")
    fake = FakeCallEndpointRegistry()
    fake.add("door", "fake:gate-1", "Gate (placement)")
    set_call_registry(hass, fake)
    await store.async_set("u1", {"actions": [_call(id="gate", label="Mine", target={"type": "door", "id": "fake:gate-1"})]})
    assert _resolved(store, "u1")["actions"][0]["target_state"] == {"label": "Gate (placement)", "available": True, "reason": None}


async def test_placing_a_door_call_stays_not_available_until_a_registry_places_it(hass):
    store = get_content_store(hass)
    _provide(hass, "fake", "gate-1")
    await store.async_set("u1", {"actions": [_call(id="gate", label="Mine", target={"type": "door", "id": "fake:gate-1"})]})
    from ..content import RunRefused
    with pytest.raises(RunRefused) as err:
        await store.async_run("u1", "Phone", "gate")
    assert err.value.code == "not_available"


async def test_a_phone_kind_id_does_not_resolve_as_a_door(hass):
    user = await _phone_user(hass, "Dina")
    _phone_entry(hass, [{"ha_user_id": user.id, "name": "Dina phone", "enroll_id": "enr-dina"}])
    reg = get_endpoint_registry(hass)
    assert reg.get("enr-dina", kind="door") is None and reg.get("enr-dina", kind="phone") is not None
