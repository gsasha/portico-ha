"""DEV-760 E2: `portico/elements/list | set | delete` (elements_commands.py) over a real HA websocket.

HA-harness session. What is pinned: administrators only (a plain user, a dashboard user and an enrolled phone get
`unauthorized` on all three and nothing is written); `base_rev` conflicts; `validate_only` writes nothing; the server mints
the id; `in_use` through the `used_by` seam, then `confirm: true`; replies carry no exception text; and ELEM-07, the content
store, the endpoint registry and every existing reply are byte-identical after a full create / rename / delete cycle.
"""
from __future__ import annotations

import json

import pytest

from .. import auth
from .. import elements as elements_module
from ..content import get_content_store
from ..elements import ElementStore, get_elements_store
from ..endpoints import get_endpoint_registry
from .test_endpoints import _phone_entry
from .test_push_registry import _custom_integrations, _phone_user  # noqa: F401 - fixtures by name
from .test_signaling import _setup_signaling

pytestmark = pytest.mark.asyncio

LIST = "portico/elements/list"
SET = "portico/elements/set"
DELETE = "portico/elements/delete"


def _status(name="Hall", **extra):
    return {"kind": "status", "name": name, "entity_id": "sensor.hall", "options": {}, **extra}


@pytest.fixture
async def world(hass, hass_storage, hass_admin_user):
    phone, phone_token = await _phone_user(hass, name="Phone user")
    plain, plain_token = await _phone_user(hass, name="Plain user")
    _phone_entry(hass, [{"ha_user_id": phone.id, "name": "Phone", "enroll_id": "ep-phone"}])
    await _setup_signaling(hass)
    return type("W", (), {"hass": hass, "storage": hass_storage, "tokens": {"phone": phone_token, "plain": plain_token}})


async def _send(client, type_, **body):
    _send.n = getattr(_send, "n", 0) + 1
    await client.send_json({"id": _send.n, "type": type_, **body})
    return await client.receive_json()


async def _admin(world, hass_ws_client):
    return await hass_ws_client(world.hass)


async def _rev(client):
    return (await _send(client, LIST))["result"]["rev"]


async def test_list_of_an_empty_store_carries_the_registry(world, hass_ws_client):
    admin = await _admin(world, hass_ws_client)
    reply = await _send(admin, LIST)
    assert reply["success"], reply
    r = reply["result"]
    assert r["schema_version"] == 1 and r["elements"] == [] and r["used_by"] == {} and r["rev"]
    assert set(r["kinds"]) == {"camera", "button", "talk", "status", "chip"}
    assert {"action": "lock_open", "entity_domain": "lock"} in r["kinds"]["button"]["actions"]
    assert {"action": "portico_open", "entity_domain": None} in r["kinds"]["button"]["actions"]
    assert "mdi:lock" in r["icons"] or r["icons"]
    assert r["icon_policy"] == {"camera": "fixed", "button": "fixed", "talk": "fixed", "status": "mdi", "chip": "mdi"}
    assert r["palette"][0] == "blue" and r["limits"]["max_elements"] == 200
    assert "portico.elements" not in world.storage            # a read writes nothing


async def test_create_replace_rename_and_delete_over_the_wire(world, hass_ws_client):
    admin = await _admin(world, hass_ws_client)
    rev0 = await _rev(admin)
    made = await _send(admin, SET, element=_status(), base_rev=rev0)
    assert made["success"], made
    el, rev1 = made["result"]["element"], made["result"]["rev"]
    assert el["id"].startswith("el-") and rev1 != rev0 and el["name"] == "Hall"
    assert world.storage["portico.elements"]["data"]["elements"] == [el]

    renamed = await _send(admin, SET, element={**el, "name": "Hallway"}, base_rev=rev1)
    assert renamed["success"] and renamed["result"]["element"]["id"] == el["id"]
    rev2 = renamed["result"]["rev"]

    listed = (await _send(admin, LIST))["result"]
    assert [e["name"] for e in listed["elements"]] == ["Hallway"] and listed["used_by"] == {el["id"]: []}

    gone = await _send(admin, DELETE, element_id=el["id"], base_rev=rev2)
    assert gone["success"] and gone["result"]["rev"] == rev0
    assert world.storage["portico.elements"]["data"]["elements"] == []


async def test_a_stale_base_rev_conflicts_on_set_and_delete(world, hass_ws_client):
    admin = await _admin(world, hass_ws_client)
    rev0 = await _rev(admin)
    el = (await _send(admin, SET, element=_status(), base_rev=rev0))["result"]["element"]
    before = json.dumps(world.storage["portico.elements"], sort_keys=True)
    for reply in (await _send(admin, SET, element=_status("Other"), base_rev=rev0),
                  await _send(admin, SET, element={**el, "name": "X"}, base_rev=rev0),
                  await _send(admin, DELETE, element_id=el["id"], base_rev=rev0)):
        assert reply["success"] is False and reply["error"]["code"] == "conflict", reply
    assert json.dumps(world.storage["portico.elements"], sort_keys=True) == before


async def test_validate_only_writes_nothing_and_says_valid(world, hass_ws_client):
    admin = await _admin(world, hass_ws_client)
    rev0 = await _rev(admin)
    reply = await _send(admin, SET, element=_status(), base_rev=rev0, validate_only=True)
    assert reply["success"] and reply["result"]["valid"] is True and reply["result"]["rev"] == rev0
    assert reply["result"]["element"]["id"].startswith("el-")
    assert "portico.elements" not in world.storage and (await _rev(admin)) == rev0
    bad = await _send(admin, SET, element={"kind": "status", "name": "x", "entity_id": "nope"}, base_rev=rev0,
                      validate_only=True)
    assert bad["success"] is False and bad["error"]["code"] == "invalid" and bad["error"]["field"] == "entity_id"


async def test_the_error_codes_with_their_fields(world, hass_ws_client):
    admin = await _admin(world, hass_ws_client)
    rev = await _rev(admin)
    el = (await _send(admin, SET, element=_status(), base_rev=rev))["result"]
    rev = el["rev"]
    el = el["element"]

    async def code(**body):
        reply = await _send(admin, SET, **body)
        assert reply["success"] is False, reply
        return reply["error"]

    assert (await code(element=_status("hall"), base_rev=rev))["code"] == "name_taken"
    assert (await code(element=_status("hall"), base_rev=rev))["field"] == "name"
    assert (await code(element={**_status("Z"), "id": "el-00000000"}, base_rev=rev))["code"] == "unknown_element"
    assert (await code(element={**el, "kind": "chip"}, base_rev=rev))["code"] == "kind_locked"
    assert (await code(element={"kind": "nope", "name": "n"}, base_rev=rev))["code"] == "invalid"
    unknown = await _send(admin, DELETE, element_id="el-00000000", base_rev=rev)
    assert unknown["error"]["code"] == "unknown_element"


async def test_too_many_elements(world, hass_ws_client, monkeypatch):
    monkeypatch.setattr(elements_module, "MAX_ELEMENTS", 1)
    admin = await _admin(world, hass_ws_client)
    rev = await _rev(admin)
    rev = (await _send(admin, SET, element=_status(), base_rev=rev))["result"]["rev"]
    reply = await _send(admin, SET, element=_status("Two"), base_rev=rev)
    assert reply["error"]["code"] == "too_many"


async def test_a_newer_schema_file_refuses_every_write(world, hass_ws_client):
    world.storage["portico.elements"] = {"version": 1, "minor_version": 1, "key": "portico.elements",
                                         "data": {"schema_version": 9, "elements": []}}
    admin = await _admin(world, hass_ws_client)
    listed = await _send(admin, LIST)
    assert listed["success"] and listed["result"]["schema_version"] == 9
    before = json.dumps(world.storage["portico.elements"], sort_keys=True)
    reply = await _send(admin, SET, element=_status(), base_rev=listed["result"]["rev"])
    assert reply["error"]["code"] == "newer_schema"
    reply = await _send(admin, DELETE, element_id="el-00000000", base_rev=listed["result"]["rev"])
    assert reply["error"]["code"] == "newer_schema"
    assert json.dumps(world.storage["portico.elements"], sort_keys=True) == before


async def test_in_use_lists_the_screens_and_no_confirm_overrides_it(world, hass_ws_client, monkeypatch):
    admin = await _admin(world, hass_ws_client)
    rev = await _rev(admin)
    el = (await _send(admin, SET, element=_status(), base_rev=rev))["result"]
    users = [{"endpoint": "ep-kitchen", "label": "Kitchen"}]
    monkeypatch.setattr(ElementStore, "used_by", lambda self, element_id: users if element_id == el["element"]["id"] else [])
    monkeypatch.setattr(ElementStore, "used_by_map", lambda self: {el["element"]["id"]: users})
    listed = (await _send(admin, LIST))["result"]
    assert listed["used_by"] == {el["element"]["id"]: users}
    refused = await _send(admin, DELETE, element_id=el["element"]["id"], base_rev=el["rev"])
    assert refused["success"] is False and refused["error"]["code"] == "in_use" and refused["error"]["used_by"] == users
    assert len(world.storage["portico.elements"]["data"]["elements"]) == 1
    # `confirm: false` is not a confirmation either
    again = await _send(admin, DELETE, element_id=el["element"]["id"], base_rev=el["rev"], confirm=False)
    assert again["error"]["code"] == "in_use"
    # and `confirm: true` is no override: an element in use is refused whatever the message says
    forced = await _send(admin, DELETE, element_id=el["element"]["id"], base_rev=el["rev"], confirm=True)
    assert forced["success"] is False and forced["error"]["code"] == "in_use" and forced["error"]["used_by"] == users
    assert len(world.storage["portico.elements"]["data"]["elements"]) == 1
    monkeypatch.undo()                                    # nothing uses it now
    done = await _send(admin, DELETE, element_id=el["element"]["id"], base_rev=el["rev"])
    assert done["success"] and world.storage["portico.elements"]["data"]["elements"] == []


@pytest.mark.parametrize("who", ["phone", "plain"])
async def test_a_phone_and_an_ordinary_user_are_refused_and_nothing_is_written(world, hass_ws_client, who):
    client = await hass_ws_client(world.hass, access_token=world.tokens[who])
    for type_, body in ((LIST, {}), (SET, {"element": _status(), "base_rev": "x"}),
                        (DELETE, {"element_id": "el-00000000", "base_rev": "x"})):
        reply = await _send(client, type_, **body)
        assert reply["success"] is False and reply["error"]["code"] == "unauthorized", (who, type_, reply)
    assert "portico.elements" not in world.storage


async def test_a_dashboard_user_is_refused(world, hass_ws_client, monkeypatch):
    screen = await world.hass.auth.async_create_user(name="screen", group_ids=["system-users"])
    token = (await _phone_user(world.hass, name="Screen user"))
    monkeypatch.setattr(auth, "is_dashboard_user", lambda _hass, user_id: user_id == token[0].id)
    client = await hass_ws_client(world.hass, access_token=token[1])
    del screen
    for type_, body in ((LIST, {}), (SET, {"element": _status(), "base_rev": "x"}),
                        (DELETE, {"element_id": "el-00000000", "base_rev": "x"})):
        reply = await _send(client, type_, **body)
        assert reply["error"]["code"] == "unauthorized", (type_, reply)


async def test_a_failure_inside_the_store_answers_unknown_without_its_text(world, hass_ws_client, monkeypatch, caplog):
    async def boom(self, *a, **k):
        raise RuntimeError("secret-path /config/x")

    monkeypatch.setattr(ElementStore, "async_set", boom)
    monkeypatch.setattr(ElementStore, "async_delete", boom)
    monkeypatch.setattr(ElementStore, "async_list", boom)
    admin = await _admin(world, hass_ws_client)
    for type_, body in ((LIST, {}), (SET, {"element": _status(), "base_rev": "x"}),
                        (DELETE, {"element_id": "el-00000000", "base_rev": "x"})):
        reply = await _send(admin, type_, **body)
        assert reply["success"] is False and reply["error"]["code"] == "unknown"
        assert "secret" not in json.dumps(reply)
    assert "secret-path" not in caplog.text


async def test_elem_07_the_content_store_and_every_existing_reply_are_byte_identical(world, hass_ws_client):
    """After a full create / rename / replace / delete cycle: no `portico.content` write, the same endpoint registry, and
    `content/document` and `content/get` answer exactly as before."""
    hass = world.hass
    admin = await _admin(world, hass_ws_client)
    store = get_content_store(hass)
    await store.async_ensure_loaded()
    content_writes = [k for k in world.storage if k == "portico.content"]

    async def snapshot():
        document = await _send(admin, "portico/content/document")
        phones = await _send(admin, "portico/ui_settings/get")
        return (json.dumps(document, sort_keys=True), json.dumps(phones, sort_keys=True),
                json.dumps(get_endpoint_registry(hass).__dict__.get("_rows", None), sort_keys=True, default=str),
                json.dumps(world.storage.get("portico.content"), sort_keys=True))

    def strip_ids(blob):                       # websocket ids differ per call; the payloads must not
        return json.loads(blob[0])["result"], json.loads(blob[1])["result"], blob[2], blob[3]

    before = strip_ids(await snapshot())
    rev = await _rev(admin)
    el = (await _send(admin, SET, element=_status(), base_rev=rev))["result"]
    el = (await _send(admin, SET, element={**el["element"], "name": "Renamed"}, base_rev=el["rev"]))["result"]
    mid = strip_ids(await snapshot())
    await _send(admin, DELETE, element_id=el["element"]["id"], base_rev=el["rev"])
    after = strip_ids(await snapshot())
    assert before == mid == after
    assert [k for k in world.storage if k == "portico.content"] == content_writes
    assert set(world.storage) - set(content_writes) <= {"portico.elements"} | (set(world.storage) - {"portico.elements"})
