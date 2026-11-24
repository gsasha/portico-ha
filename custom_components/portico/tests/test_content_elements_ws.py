"""DEV-760 placements P1 over a real websocket: `content/preview` expands references, `content/save` stamps 5 and refuses a bad
reference with the validator's text, and `elements/list | set | delete` answer with the real screens that use an element."""
from __future__ import annotations

import copy

import pytest

from ..content import get_content_store
from ..elements import get_elements_store
from .test_editor_commands import PHONE_ID, _client, _doc, _document, _page, _send, world  # noqa: F401 - fixture by name
from .test_push_registry import _custom_integrations  # noqa: F401 - autouse fixture by name

pytestmark = pytest.mark.asyncio


async def _element(world, name="Yard", entity="camera.yard"):
    elements = get_elements_store(world.hass)
    await elements.async_ensure_loaded()
    return (await elements.async_set({"kind": "camera", "name": name, "entity_id": entity,
                                      "options": {"size": "big"}}, elements.rev))["element"]["id"]


def _with_ref(document, element_id):
    doc = copy.deepcopy(document)
    doc["template"]["pages"][0]["blocks"][0]["cameras"].append({"id": "yard", "element": element_id})
    return doc


async def test_preview_resolves_a_reference_exactly_as_the_save_will_serve_it(world, hass_ws_client):
    world.hass.states.async_set("camera.yard", "idle", {"friendly_name": "Yard camera"})
    element_id = await _element(world)
    admin, got = await _document(world, hass_ws_client)
    draft = _with_ref(_doc(), element_id)
    preview = await _send(admin, "portico/content/preview", 2, document=draft, endpoint=PHONE_ID)
    assert preview["success"], preview
    cams = preview["result"]["pages"][0]["blocks"][0]["cameras"]
    yard = next(c for c in cams if c["id"] == "yard")
    assert yard["label"] == "Yard" and yard["size"] == "big" and yard["entity_id"] == "camera.yard"
    assert yard["name"] == "Yard camera" and "element" not in yard           # the wire is the inline camera's
    saved = await _send(admin, "portico/content/save", 3, document=draft, base_doc_rev=got["doc_rev"])
    assert saved["success"], saved
    assert world.storage["portico.content"]["data"]["schema_version"] == 5
    assert preview["result"] == world.store.resolve_document(PHONE_ID)         # preview == what the phone gets after the save
    reread = await _send(admin, "portico/content/document", 4)
    assert reread["result"]["document"]["schema_version"] == 5
    assert reread["result"]["document"]["template"]["pages"][0]["blocks"][0]["cameras"][-1] == {"id": "yard", "element": element_id}


async def test_a_bad_reference_is_invalid_with_the_item_and_the_reason_on_preview_and_save(world, hass_ws_client):
    admin, got = await _document(world, hass_ws_client)
    draft = _with_ref(_doc(), "el-deadbeef")
    for n, (type_, extra) in enumerate((("portico/content/preview", {"endpoint": PHONE_ID}),
                                        ("portico/content/save", {"base_doc_rev": got["doc_rev"]})), start=2):
        reply = await _send(admin, type_, n, document=draft, **extra)
        assert reply["success"] is False and reply["error"]["code"] == "invalid"
        assert "element_missing" in reply["error"]["message"] and "yard" in reply["error"]["message"]
    assert world.storage.get("portico.content.v4") is None
    assert get_content_store(world.hass).document()["schema_version"] == 4


async def test_elements_list_and_delete_report_the_real_screens_and_refuse_a_used_element(world, hass_ws_client):
    element_id = await _element(world)
    admin, got = await _document(world, hass_ws_client)
    await _send(admin, "portico/content/save", 2, document=_with_ref(_doc(), element_id), base_doc_rev=got["doc_rev"])
    listed = (await _send(admin, "portico/elements/list", 3))["result"]
    assert listed["used_by"][element_id] == [{"endpoint": "template", "label": "Home > Home", "page": "home", "block": "home"}]
    refused = await _send(admin, "portico/elements/delete", 4, element_id=element_id, base_rev=listed["rev"])
    assert refused["success"] is False and refused["error"]["code"] == "in_use"
    assert refused["error"]["used_by"] == listed["used_by"][element_id]
    forced = await _send(admin, "portico/elements/delete", 5, element_id=element_id, base_rev=listed["rev"], confirm=True)
    assert forced["success"] is False and forced["error"]["code"] == "in_use"
    assert [e["id"] for e in (await _send(admin, "portico/elements/list", 6))["result"]["elements"]] == [element_id]


async def test_elements_set_over_the_wire_warns_with_the_screens_when_the_entity_of_a_used_element_changes(world, hass_ws_client):
    element_id = await _element(world)
    admin, got = await _document(world, hass_ws_client)
    await _send(admin, "portico/content/save", 2, document=_with_ref(_doc(), element_id), base_doc_rev=got["doc_rev"])
    listed = (await _send(admin, "portico/elements/list", 3))["result"]
    element = next(e for e in listed["elements"] if e["id"] == element_id)
    changed = {**element, "entity_id": "camera.garage"}
    checked = await _send(admin, "portico/elements/set", 4, element=changed, base_rev=listed["rev"], validate_only=True)
    assert checked["success"] is False and checked["error"]["code"] == "in_use_retarget"
    assert checked["error"]["used_by"] == listed["used_by"][element_id]
    sure = await _send(admin, "portico/elements/set", 5, element=changed, base_rev=listed["rev"], validate_only=True, confirm=True)
    assert sure["success"] and sure["result"]["valid"] is True
    stream_only = {"id": element_id, "kind": "camera", "name": "Yard", "options": {"stream": "rtsp://10.6.0.6:8554/y"}}
    refused = await _send(admin, "portico/elements/set", 6, element=stream_only, base_rev=listed["rev"])
    assert refused["success"] is False and refused["error"]["code"] == "in_use_stream"
    assert refused["error"]["used_by"] == listed["used_by"][element_id]
