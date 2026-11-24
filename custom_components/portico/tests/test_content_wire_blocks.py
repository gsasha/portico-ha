# ruff: noqa: F811 - test parameters named after the imported fixtures
"""Blocks on the wire (DEV-658 B2, `portico_blocks_design.md` sections 3.6, 4.1, 4.2): the resolver, `content/get`,
`content/document | preview | save`, the re-wrap rule for a flat edit from the panel on disk, `door_available` and the
snapshot width.

What is pinned:
- `content/get` pages carry `blocks` (every kind, effective `size`, `color`, `show_label`, the block's door) AND the
  derived flat `cameras` / `actions` an app 0.5.2 and the card on disk read, exactly as they were;
- `size` auto = the first camera of a block big, the rest small; `door_available` is live and not part of `rev`;
- the panel on disk (which knows no blocks) edits `page.cameras` / `page.actions`: that edit lands in the page's one
  block, keeps what the flat form cannot hold, and is refused on a page of several blocks;
- a save takes schema 4 only; a stale base still conflicts first.
"""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from homeassistant.components.camera import Image
from pytest_homeassistant_custom_component.common import async_mock_service

from .. import content
from ..content import ProfileError, normalize_document, refuse_flat_pages
from ..content_resolve import (
    effective_size, endpoint_rev, resolve_block, resolve_endpoint, resolve_page,
)
from ..endpoints import Endpoint
from ..provider import DATA_PROVIDERS, DOMAIN
from .test_editor_commands import (  # noqa: F401 - fixtures by name
    PHONE_ID, _client, _document, _send, world,
)

pytestmark = pytest.mark.asyncio

DOOR = "fake:gate-1"


# ---- documents ---------------------------------------------------------------------------------------------------

def _cam(cid, entity=None, **extra):
    return {"id": cid, "label": cid.title(), "entity_id": entity or f"camera.{cid}", **extra}


def _entrance():
    return {"id": "entrance", "title": "Entrance", "door": DOOR,
            "cameras": [_cam("gate"), _cam("yard", size="big"), _cam("side")],
            "actions": [{"id": "open-door", "label": "Open the door", "kind": "portico_open", "confirm": True},
                        {"id": "unlock", "label": "Unlock", "kind": "lock_unlock", "entity_id": "lock.gate",
                         "confirm": True, "color": "red", "icon": "lock-open-variant", "show_label": False},
                        {"id": "talk", "label": "Talk", "kind": "talk", "confirm": False},
                        {"id": "evening", "label": "Evening", "kind": "scene_activate", "entity_id": "scene.evening",
                         "confirm": False}]}


def _garden():
    return {"id": "garden", "title": "Garden", "cameras": [_cam("lawn")],
            "actions": [{"id": "gate-open", "label": "Open the gate", "kind": "cover_open", "entity_id": "cover.gate",
                         "confirm": True, "color": "green"}]}


def _doc4(*blocks, rules=(), overrides=None):
    return {"schema_version": 4,
            "template": {"pages": [{"id": "home", "label": "Home", "blocks": list(blocks)}],
                         "rules": list(rules), "roster": ["*"]},
            "overrides": overrides or {}, "dashboards": {}, "endpoints": {}}


def _states(hass):
    for entity, state in (("camera.gate", "idle"), ("camera.yard", "idle"), ("camera.side", "unavailable"),
                          ("camera.lawn", "idle"), ("lock.gate", "locked"), ("scene.evening", "x"),
                          ("cover.gate", "closed")):
        hass.states.async_set(entity, state, {"friendly_name": entity.split(".")[1].title() + " (entity)"})


async def _save(world, document):
    _states(world.hass)
    return await world.store.async_save_document(document, world.store.doc_rev)


def _fake(world):
    return world.hass.data[DOMAIN][DATA_PROVIDERS]._regs["fake"].provider


def _page_of(world, endpoint=PHONE_ID):
    return world.store.resolve_document(endpoint)["pages"][0]


# ---- the resolver, pure ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("camera,first,expected", [
    ({}, True, "big"), ({}, False, "small"), ({"size": "auto"}, True, "big"), ({"size": "auto"}, False, "small"),
    ({"size": "big"}, False, "big"), ({"size": "small"}, True, "small")])
async def test_effective_size_auto_is_big_for_the_first_camera_of_a_block_and_small_for_the_rest(camera, first, expected):
    assert effective_size(camera, first) == expected


class _States:
    def __init__(self, **states):
        self._states = states

    def get(self, entity_id):
        value = self._states.get(entity_id)
        return None if value is None else SimpleNamespace(state=value, attributes={"friendly_name": entity_id.upper()})


class _Registry:
    def __init__(self, doors=None):
        self.doors = doors or {}

    def door(self, device_id):
        return self.doors.get(device_id)

    def call_state(self, action):
        return {"label": action["label"], "available": False, "reason": "not_available"}

    def endpoints(self):
        return []


async def test_resolve_block_resolves_every_kind_with_the_effective_size_and_the_blocks_door():
    registry = _Registry({DOOR: SimpleNamespace(name="Gate", available=True)})
    out = resolve_block(_entrance(), states=_States(**{"camera.gate": "idle", "lock.gate": "locked"}), registry=registry)
    assert [(c["id"], c["size"], c["available"]) for c in out["cameras"]] == [
        ("gate", "big", True), ("yard", "big", False), ("side", "small", False)]
    assert out["cameras"][0]["name"] == "CAMERA.GATE"
    assert out["door"] == DOOR and out["door_available"] is True and out["title"] == "Entrance"
    by_id = {a["id"]: a for a in out["actions"]}
    assert by_id["open-door"]["device_id"] == DOOR and by_id["open-door"]["name"] == "Gate" and by_id["open-door"]["available"]
    assert by_id["unlock"]["color"] == "red" and by_id["unlock"]["show_label"] is False
    assert by_id["unlock"]["available"] is True and by_id["unlock"]["kind"] == "lock_unlock"
    assert by_id["talk"]["available"] is False and by_id["talk"]["reason"] == "talk_off"
    assert [a["id"] for a in out["actions"]] == ["open-door", "unlock", "talk", "evening"]        # every kind, in order


async def test_a_block_without_a_door_has_no_door_keys_and_a_block_whose_door_is_gone_or_down_says_not_available():
    states = _States()
    assert "door" not in resolve_block(_garden(), states=states, registry=_Registry()) \
        and "door_available" not in resolve_block(_garden(), states=states, registry=_Registry())
    gone = resolve_block(_entrance(), states=states, registry=_Registry())
    assert gone["door"] == DOOR and gone["door_available"] is False
    down = resolve_block(_entrance(), states=states, registry=_Registry({DOOR: SimpleNamespace(name="G", available=False)}))
    assert down["door_available"] is False
    # a bare portico_open of a door that is gone still resolves (unavailable, named by its id)
    assert gone["actions"][0]["available"] is False and gone["actions"][0]["name"] == DOOR


async def test_resolve_page_serves_blocks_and_nothing_flat_every_kind_present():
    page = {"id": "home", "label": "Home", "blocks": [_entrance(), _garden()]}
    resolved = resolve_page(page, states=_States(), registry=_Registry())
    assert set(resolved) == {"blocks"}                               # B8: no derived `cameras` / `actions`
    assert [b["id"] for b in resolved["blocks"]] == ["entrance", "garden"]
    entrance, garden = resolved["blocks"]
    assert [c["id"] for c in entrance["cameras"] + garden["cameras"]] == ["gate", "yard", "side", "lawn"]
    # the kinds a schema-3 reader never knew (Talk, lock_unlock, cover_open) are all served now, with their v4 fields
    assert [a["id"] for a in entrance["actions"] + garden["actions"]] == ["open-door", "unlock", "talk", "evening", "gate-open"]
    by_id = {a["id"]: a for a in entrance["actions"] + garden["actions"]}
    assert by_id["unlock"]["color"] == "red" and by_id["unlock"]["show_label"] is False and by_id["talk"]["kind"] == "talk"
    assert entrance["cameras"][1]["size"] == "big"


async def test_a_page_without_blocks_resolves_to_no_blocks_not_to_a_flat_page():
    out = resolve_page({"cameras": [_cam("gate")], "actions": []}, states=_States(), registry=_Registry())
    assert out == {"blocks": []}


def _doc_for_rev(*blocks):
    return {"template": {"pages": [{"id": "home", "label": "Home", "blocks": list(blocks)}], "rules": [], "roster": ["*"]},
            "overrides": {}, "dashboards": {}, "endpoints": {}}


async def test_rev_changes_with_content_and_not_with_live_state_or_the_doors_availability():
    registry_up = _Registry({DOOR: SimpleNamespace(name="Gate", available=True)})
    registry_down = _Registry({DOOR: SimpleNamespace(name="Gate", available=False)})
    document = _doc_for_rev(_entrance())

    def rev(doc, states=None, registry=registry_up):
        return resolve_endpoint(doc, "ep", states=states or _States(), registry=registry)["rev"]

    base = rev(document)
    assert rev(document, _States(**{"camera.gate": "idle", "lock.gate": "locked"})) == base         # a camera blinks
    assert rev(document, registry=registry_down) == base                                       # the gate goes down
    resolved_up = resolve_endpoint(document, "ep", states=_States(), registry=registry_up)
    resolved_down = resolve_endpoint(document, "ep", states=_States(), registry=registry_down)
    assert resolved_up["pages"][0]["blocks"][0]["door_available"] is True
    assert resolved_down["pages"][0]["blocks"][0]["door_available"] is False                   # ... and still says so
    assert endpoint_rev(resolved_up) == endpoint_rev(resolved_down)
    for change in (lambda b: b["cameras"][0].update(size="small"), lambda b: b["actions"][1].update(color="blue"),
                   lambda b: b["actions"][1].update(show_label=True), lambda b: b.update(title="Front"),
                   lambda b: b.update(door="fake:other"), lambda b: b["cameras"].reverse()):
        block = _entrance()
        change(block)
        assert rev(_doc_for_rev(block)) != base


async def test_a_door_default_button_and_a_ring_rule_find_buttons_in_any_block():
    """The callers' lists read the blocks: a door's default ring buttons include a bare portico_open of a block whose
    door is THIS door; a rule may name a button of the second block (a lock_unlock, which a v3 reader does not know)."""
    door_ep = Endpoint(id=DOOR, kind="door", provider="fake", device_id=DOOR, name="Gate", ha_user_id=None,
                       cameras=("camera.gate",), room=None, override=None, available=True, reason=None)

    class Registry(_Registry):
        def endpoints(self):
            return [door_ep]

    registry = Registry({DOOR: SimpleNamespace(name="Gate", available=True)})
    garden = _garden()
    garden["actions"].append({"id": "ul2", "label": "Unlock 2", "kind": "lock_unlock", "entity_id": "lock.gate",
                              "confirm": True})
    document = _doc_for_rev(_entrance(), garden)
    out = resolve_endpoint(document, "ep", states=_States(), registry=registry)
    assert [a["id"] for a in out["callers"][DOOR]["ring_buttons"]] == ["open-door"]
    document["template"]["rules"] = [{"id": "r", "caller": f"kind:door", "cameras": None,
                                      "ring_buttons": ["ul2", "talk", "open-door"], "call_buttons": ["gate-open"]}]
    out = resolve_endpoint(document, "ep", states=_States(), registry=registry)
    assert [a["id"] for a in out["callers"][DOOR]["ring_buttons"]] == ["ul2", "open-door"]       # talk never rings
    assert [a["id"] for a in out["callers"][DOOR]["call_buttons"]] == ["gate-open"]
    # the callers' camera and button shapes are untouched by blocks (D4)
    assert set(out["callers"][DOOR]) == {"name", "kind", "cameras", "ring_buttons", "call_buttons"}


# ---- content/get over the websocket ------------------------------------------------------------------------------

def _no_flat_keys(value, path="reply"):
    """No dict anywhere in a reply is a page-shaped dict with the flat lists: a `cameras` or `actions` next to `blocks`
    (or next to a page `label`). Callers' and blocks' own `cameras` / `actions` are not pages and are not touched."""
    if isinstance(value, dict):
        if "blocks" in value:
            assert "cameras" not in value and "actions" not in value, f"flat lists beside blocks at {path}"
        for key, item in value.items():
            _no_flat_keys(item, f"{path}.{key}")
    elif isinstance(value, list):
        for n, item in enumerate(value):
            _no_flat_keys(item, f"{path}[{n}]")


async def test_content_get_carries_blocks_only_every_kind_and_no_flat_form(world, hass_ws_client):
    await _save(world, _doc4(_entrance(), _garden()))
    phone = await _client(world, hass_ws_client, "phone")
    resp = await _send(phone, "portico/content/get")
    assert resp["success"], resp
    (page,) = resp["result"]["pages"]
    assert set(page) == {"id", "label", "blocks"}
    _no_flat_keys(resp["result"])
    entrance, garden = page["blocks"]
    assert set(entrance) == {"id", "title", "door", "door_available", "cameras", "actions"}
    assert set(garden) == {"id", "title", "cameras", "actions"}
    assert [(c["id"], c["size"]) for c in entrance["cameras"]] == [("gate", "big"), ("yard", "big"), ("side", "small")]
    assert [(c["id"], c["size"]) for c in garden["cameras"]] == [("lawn", "big")]       # first of ITS block
    assert entrance["door_available"] is True
    actions = {a["id"]: a for a in entrance["actions"] + garden["actions"]}
    assert actions["open-door"]["device_id"] == DOOR and actions["unlock"]["show_label"] is False
    assert actions["gate-open"]["color"] == "green" and actions["talk"]["reason"] == "talk_off"
    # the blocks are COMPLETE on their own: every camera and every button of every kind, which the flat form never was
    assert [c["id"] for b in page["blocks"] for c in b["cameras"]] == ["gate", "yard", "side", "lawn"]
    assert [a["id"] for b in page["blocks"] for a in b["actions"]] == ["open-door", "unlock", "talk", "evening", "gate-open"]
    assert set(resolve := resp["result"]) >= {"endpoint", "kind", "pages", "callers", "roster", "rev"} and resolve["kind"] == "phone"
    assert "cameras" not in resp["result"] and "actions" not in resp["result"]


async def test_a_migrated_v3_page_is_served_as_one_block_and_nothing_else(world, hass_ws_client):
    """A v3 page, migrated to one block, reaches the phone as that block; the flat lists it came from are gone."""
    _states(world.hass)
    only = {"id": "home", "label": "Home", "blocks": [
        {"id": "home", "title": "Home", "cameras": [{"id": "gate", "label": "Gate", "entity_id": "camera.gate"}],
         "actions": [{"id": "lk", "label": "Front", "kind": "lock_open", "entity_id": "lock.gate", "confirm": True}]}]}
    await world.store.async_save_document(
        {"schema_version": 4, "template": {"pages": [only], "rules": [], "roster": ["*"]}, "overrides": {},
         "dashboards": {}, "endpoints": {}}, world.store.doc_rev)
    page = _page_of(world)
    assert set(page) == {"id", "label", "blocks"}
    (block,) = page["blocks"]
    assert block["id"] == "home" and block["title"] == "Home" and "door" not in block
    assert [c["size"] for c in block["cameras"]] == ["big"]
    assert [a["id"] for a in block["actions"]] == ["lk"] and block["actions"][0]["available"] is True


async def test_the_run_and_snapshot_grants_read_blocks_with_no_flat_form_anywhere(world, hass_ws_client):
    """B8 must not touch the grant: a button of a kind the flat form never held, in the second block, still runs."""
    await _save(world, _doc4(_entrance(), _garden()))
    _states(world.hass)
    covered = async_mock_service(world.hass, "cover", "open_cover")
    unlocked = async_mock_service(world.hass, "lock", "unlock")
    phone = await _client(world, hass_ws_client, "phone")
    assert (await _send(phone, "portico/content/run", 2, action_id="gate-open"))["success"]
    world.store._last_run.clear()
    assert (await _send(phone, "portico/content/run", 3, action_id="unlock"))["success"]
    assert len(covered) == 1 and len(unlocked) == 1
    world.store._last_run.clear()
    stranger = await _send(phone, "portico/content/run", 4, action_id="not-a-button")
    assert stranger["success"] is False and stranger["error"]["code"] == "unknown_action"


async def test_door_available_follows_the_provider_live_and_never_moves_the_rev(world, hass_ws_client):
    await _save(world, _doc4(_entrance()))
    phone = await _client(world, hass_ws_client, "phone")
    first = (await _send(phone, "portico/content/get"))["result"]
    assert first["pages"][0]["blocks"][0]["door_available"] is True
    _fake(world).set_available("gate-1", False)
    second = (await _send(phone, "portico/content/get", 2))["result"]
    assert second["pages"][0]["blocks"][0]["door_available"] is False
    assert second["rev"] == first["rev"]
    _fake(world).set_available("gate-1", True)
    world.hass.states.async_set("camera.side", "idle")
    assert (await _send(phone, "portico/content/get", 3))["result"]["rev"] == first["rev"]
    # a content change is what moves it
    document = world.store.document()
    document["template"]["pages"][0]["blocks"][0]["cameras"][0]["size"] = "small"
    await world.store.async_save_document(document, world.store.doc_rev)
    assert (await _send(phone, "portico/content/get", 4))["result"]["rev"] != first["rev"]


async def test_an_override_replaces_the_template_page_by_id_and_each_endpoint_gets_its_own_blocks(world, hass_ws_client):
    override = {"pages": [{"id": "home", "label": "Mine", "blocks": [_garden()]}], "rules": [], "roster_hide": []}
    await _save(world, _doc4(_entrance(), overrides={PHONE_ID: override}))
    assert [b["id"] for b in _page_of(world)["blocks"]] == ["garden"]
    assert _page_of(world)["label"] == "Mine"
    other = world.store.resolve_document("somebody-else")["pages"][0]
    assert [b["id"] for b in other["blocks"]] == ["entrance"]
    # a "Not attached" override (a key no endpoint answers to) is kept, resolved for nobody
    orphan = {"pages": [{"id": "home", "label": "Old", "blocks": [_garden()]}], "rules": [], "roster_hide": []}
    document = world.store.document()
    document["overrides"]["old-key"] = orphan
    await world.store.async_save_document(document, world.store.doc_rev)
    assert world.store.orphan_keys() == ["old-key"]
    assert [b["id"] for b in _page_of(world)["blocks"]] == ["garden"]


async def test_content_run_and_snapshot_reach_the_items_of_every_block(world, hass_ws_client):
    unlock = async_mock_service(world.hass, "lock", "unlock")
    open_cover = async_mock_service(world.hass, "cover", "open_cover")
    await _save(world, _doc4(_entrance(), _garden()))
    phone = await _client(world, hass_ws_client, "phone")
    assert (await _send(phone, "portico/content/run", 1, action_id="unlock"))["success"]
    assert (await _send(phone, "portico/content/run", 2, action_id="gate-open"))["success"]
    await world.hass.async_block_till_done()
    assert len(unlock) == 1 and unlock[0].data == {"entity_id": "lock.gate"} and unlock[0].context.user_id == world.phone.id
    assert len(open_cover) == 1 and open_cover[0].data == {"entity_id": "cover.gate"}
    refused = await _send(phone, "portico/content/run", 3, action_id="talk")
    assert refused["success"] is False and refused["error"]["code"] == "not_available"
    assert "switched off" in refused["error"]["message"]
    fake = _fake(world)
    assert [c for c in fake.calls if c[0] == "dial"] == []


async def test_snapshot_width_cap_is_1920(world, hass_ws_client, monkeypatch):
    widths = []

    async def image(hass, entity_id, timeout=10, width=None, height=None):
        widths.append(width)
        return Image(content_type="image/jpeg", content=b"JPEG")

    monkeypatch.setattr("homeassistant.components.camera.async_get_image", image)
    await _save(world, _doc4(_entrance(), _garden()))
    phone = await _client(world, hass_ws_client, "phone")
    assert content.SNAPSHOT_WIDTH_MAX == 1920
    assert (await _send(phone, "portico/content/snapshot", 1, camera_id="gate", width=1920))["success"]
    over = await _send(phone, "portico/content/snapshot", 2, camera_id="yard", width=1921)
    assert over["success"] is False and over["error"]["code"] == "invalid_format"
    assert widths == [1920]
    # the default is unchanged
    world.store._last_run.clear()
    assert (await _send(phone, "portico/content/snapshot", 3, camera_id="lawn"))["success"]
    assert widths == [1920, 640]


# ---- content/document, preview, save ------------------------------------------------------------------------------

async def test_document_is_schema_4_blocks_only_and_doc_rev_is_the_hash_of_it(world, hass_ws_client):
    await _save(world, _doc4(_entrance(), _garden()))
    _admin, result = await _document(world, hass_ws_client)
    document = result["document"]
    assert document["schema_version"] == 4
    (page,) = document["template"]["pages"]
    assert set(page) == {"id", "label", "blocks"}
    _no_flat_keys(document)
    assert [b["id"] for b in page["blocks"]] == ["entrance", "garden"]
    assert document == world.store.document() and result["doc_rev"] == content.doc_rev(document)
    for resolved_doc in result["resolved"].values():
        _no_flat_keys(resolved_doc)
    # resolved carries blocks, too
    assert result["resolved"][PHONE_ID]["pages"][0]["blocks"][0]["id"] == "entrance"


async def test_saving_the_document_back_unchanged_is_a_no_op_and_blocks_edits_save(world, hass_ws_client):
    await _save(world, _doc4(_entrance(), _garden()))
    admin, result = await _document(world, hass_ws_client)
    rev = result["doc_rev"]
    resp = await _send(admin, "portico/content/save", 2, document=result["document"], base_doc_rev=rev)
    assert resp["success"] and resp["result"]["doc_rev"] == rev
    edited = copy.deepcopy(result["document"])
    edited["template"]["pages"][0]["blocks"][1]["title"] = "Back garden"
    edited["template"]["pages"][0]["blocks"][0]["cameras"][2]["size"] = "big"
    resp = await _send(admin, "portico/content/save", 3, document=edited, base_doc_rev=rev)
    assert resp["success"] and resp["result"]["doc_rev"] != rev
    blocks = world.store.template["pages"][0]["blocks"]
    assert blocks[1]["title"] == "Back garden" and blocks[0]["cameras"][2]["size"] == "big"
    # and the file on disk holds blocks only
    disk = world.storage["portico.content"]["data"]["template"]["pages"][0]
    assert set(disk) == {"id", "label", "blocks"}


async def test_a_schema_3_document_is_refused_and_a_stale_base_still_conflicts_first(world, hass_ws_client):
    await _save(world, _doc4(_entrance()))
    admin, result = await _document(world, hass_ws_client)
    old = copy.deepcopy(result["document"])
    old["schema_version"] = 3
    before = world.store.doc_rev
    resp = await _send(admin, "portico/content/save", 2, document=old, base_doc_rev=before)
    assert resp["success"] is False and resp["error"]["code"] == "invalid"
    assert "schema_version must be 4" in resp["error"]["message"] and world.store.doc_rev == before
    resp = await _send(admin, "portico/content/save", 3, document=old, base_doc_rev="stale")
    assert resp["success"] is False and resp["error"]["code"] == "conflict"
    resp = await _send(admin, "portico/content/preview", 4, document=old, endpoint=PHONE_ID)
    assert resp["success"] is False and resp["error"]["code"] == "invalid"


async def test_preview_resolves_blocks_of_a_draft_with_live_state_and_writes_nothing(world, hass_ws_client):
    _states(world.hass)
    admin = await _client(world, hass_ws_client)
    draft = _doc4(_entrance(), _garden())
    draft["template"]["pages"][0]["blocks"][0]["door"] = "fake:nobody"       # a door that is not there
    before = world.store.doc_rev
    resp = await _send(admin, "portico/content/preview", 1, document=draft, endpoint=PHONE_ID)
    assert resp["success"], resp
    (page,) = resp["result"]["pages"]
    assert page["blocks"][0]["door_available"] is False and page["blocks"][0]["actions"][0]["device_id"] == "fake:nobody"
    assert page["blocks"][0]["actions"][2]["reason"] == "no_door"        # the door is not there: not "switched off"
    assert [c["size"] for c in page["blocks"][0]["cameras"]] == ["big", "big", "small"]
    assert set(page) == {"id", "label", "blocks"}
    assert world.store.doc_rev == before and "portico.content" not in world.storage


async def test_preview_of_a_draft_equals_the_get_after_the_save(world, hass_ws_client):
    admin = await _client(world, hass_ws_client)
    draft = _doc4(_entrance(), _garden())
    _states(world.hass)
    preview = (await _send(admin, "portico/content/preview", 1, document=draft, endpoint=PHONE_ID))["result"]
    await _save(world, draft)
    phone = await _client(world, hass_ws_client, "phone")
    got = (await _send(phone, "portico/content/get"))["result"]
    assert preview["pages"] == got["pages"] and preview["rev"] == got["rev"]


# ---- a flat edit is refused: the panel on disk (before B3) sent one ------------------------------------------------------

def _one_block_page():
    block = {"id": "home", "title": "Home", "door": DOOR,
             "cameras": [_cam("gate", size="big"), _cam("lobby")],
             "actions": [{"id": "open", "label": "Open", "kind": "portico_open", "confirm": True},
                         {"id": "talk", "label": "Talk", "kind": "talk", "confirm": False}]}
    return {"id": "home", "label": "Home", "blocks": [block]}


def _doc_of(page, *, overrides=None):
    return {"schema_version": 4, "template": {"pages": [page], "rules": [], "roster": ["*"]},
            "overrides": overrides or {}, "dashboards": {}, "endpoints": {}}


async def test_a_document_of_blocks_only_passes_the_refusal_untouched():
    document = _doc_of(_one_block_page(), overrides={"ep": {"pages": [_one_block_page()], "rules": [], "roster_hide": []}})
    assert refuse_flat_pages(document) is None
    assert normalize_document(document)["template"]["pages"][0]["blocks"][0]["actions"][1]["kind"] == "talk"


@pytest.mark.parametrize("flat", [{"cameras": [_cam("gate")]}, {"actions": []}, {"cameras": [], "actions": []}])
async def test_a_page_with_flat_cameras_or_actions_beside_blocks_is_refused(flat):
    with pytest.raises(ProfileError, match="update the Portico panel") as err:
        refuse_flat_pages(_doc_of({**_one_block_page(), **flat}))
    assert "template page 'home'" in str(err.value)


async def test_an_unchanged_flat_form_is_refused_too_nothing_is_the_mirror_any_more():
    """Before B8 a flat form equal to the mirror of the blocks beside it passed. There is no mirror now, so any flat key
    is an old editor talking: refused, never silently dropped."""
    page = {**_one_block_page(), "cameras": [_cam("gate"), _cam("lobby")], "actions": []}
    with pytest.raises(ProfileError, match="update the Portico panel"):
        refuse_flat_pages(_doc_of(page))


async def test_a_new_page_with_flat_lists_and_no_blocks_is_refused_where_the_old_panel_added_one():
    new = {"id": "garage", "label": "Garage", "cameras": [_cam("g1")], "actions": []}
    with pytest.raises(ProfileError, match="template page 'garage'.*update the Portico panel"):
        refuse_flat_pages(_doc_of(_one_block_page()) | {"template": {"pages": [_one_block_page(), new], "rules": [],
                                                                      "roster": ["*"]}})


async def test_an_override_page_with_flat_lists_is_refused_and_named():
    bad = {**_one_block_page(), "actions": []}
    with pytest.raises(ProfileError, match=r"override 'ep' page 'home'"):
        refuse_flat_pages(_doc_of(_one_block_page(), overrides={"ep": {"pages": [bad], "rules": [], "roster_hide": []}}))


async def test_malformed_input_is_left_for_the_validator_to_name():
    for junk in (None, "x", [], 3, {"template": {"pages": "x"}, "overrides": {"k": 5, "j": {"pages": 1}}},
                 {"template": {"pages": [5, "x", None]}}):
        assert refuse_flat_pages(junk) is None
    page = {"id": "home", "label": "Home", "blocks": "nope"}
    with pytest.raises(ProfileError, match="blocks must be a list"):
        normalize_document(_doc_of(page))


async def test_a_flat_edit_over_save_and_preview_is_refused_with_the_panel_message_and_writes_nothing(world, hass_ws_client):
    await _save(world, _doc4(_entrance()))
    admin, result = await _document(world, hass_ws_client)
    edited = copy.deepcopy(result["document"])
    edited["template"]["pages"][0]["cameras"] = [_cam("gate")]       # what the panel from before B3 sent
    before = world.store.document()
    resp = await _send(admin, "portico/content/save", 2, document=edited, base_doc_rev=result["doc_rev"])
    assert resp["success"] is False and resp["error"]["code"] == "invalid"
    assert "update the Portico panel" in resp["error"]["message"] and "'home'" in resp["error"]["message"]
    assert world.store.document() == before and world.store.doc_rev == result["doc_rev"]
    resp = await _send(admin, "portico/content/preview", 3, document=edited, endpoint=PHONE_ID)
    assert resp["success"] is False and resp["error"]["code"] == "invalid"
    assert "update the Portico panel" in resp["error"]["message"]
    # the same edit through blocks is taken
    edited["template"]["pages"][0].pop("cameras")
    edited["template"]["pages"][0]["blocks"][0]["cameras"][0]["label"] = "Renamed"
    assert (await _send(admin, "portico/content/save", 4, document=edited, base_doc_rev=result["doc_rev"]))["success"]
    assert world.store.template["pages"][0]["blocks"][0]["cameras"][0]["label"] == "Renamed"


async def test_the_resolved_endpoint_document_has_no_flat_keys_on_the_store_either(world):
    await _save(world, _doc4(_entrance(), _garden()))
    reply = world.store.resolve_for_endpoint(PHONE_ID)
    _no_flat_keys(reply)
    assert [set(p) for p in reply["pages"]] == [{"id", "label", "blocks"}]
    assert not hasattr(world.store, "resolve") and not hasattr(world.store, "served_profile_for")
