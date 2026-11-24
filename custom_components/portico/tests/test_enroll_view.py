"""UI split P2: `GET /api/portico/enroll/qr/<token>` (enroll_view.py), driven through HA's real http app.

HA-harness session. What is pinned: an administrator's bearer gets `200 image/png` with `Cache-Control: no-store`;
a non-administrator gets `403` and an unknown / cancelled / expired / redeemed / superseded token `404`, NEVER `401` (a
401 from a view feeds HA's IP-ban counter); a request with no login is refused by HA; a read does not consume the token;
the PNG is the box-1 code with the 4-module quiet zone, and the view serves the very bytes the core made.
"""
from __future__ import annotations

import io
import json

import pytest
from homeassistant.setup import async_setup_component

from custom_components.portico import enroll_view
from custom_components.portico.config_flow import DOMAIN
from custom_components.portico.enroll_qr import QrStore, get_qr_store
from custom_components.portico.enroll_redeem import get_registry
from custom_components.portico.enroll_view import QR_PATH, TEXT_PATH

from .redeem_helpers import redeem
from .test_enrollment_flow import EXTERNAL, _device_entry, _set_urls
from .test_phones_commands import enrol_msg, err, ok, qr_payloads, Ws  # noqa: F401 - fixtures by name
from .test_push_registry import _custom_integrations  # noqa: F401 - autouse fixture by name
from .test_push_registry import _phone_user

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _an_owner_exists(hass_owner_user):
    """A real install has its owner before any phone."""


@pytest.fixture
async def world(hass, hass_ws_client, hass_client):
    assert await async_setup_component(hass, DOMAIN, {})
    _set_urls(hass, external=EXTERNAL)
    _device_entry(hass)
    return {"ws": Ws(await hass_ws_client(hass)), "admin": await hass_client()}


async def enrol(world, name="Alice's Pixel", **over):
    return ok(await world["ws"].send(enrol_msg(name, **over)))


async def test_an_administrators_bearer_gets_the_png_with_no_store(hass, world):
    created = await enrol(world)
    resp = await world["admin"].get(f"{QR_PATH}/{created['qr_token']}")
    assert resp.status == 200
    assert resp.content_type == "image/png"
    assert resp.headers["Cache-Control"] == "no-store"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    body = await resp.read()
    assert body == get_qr_store(hass).png_for(hass, created["qr_token"])
    assert body[:8] == b"\x89PNG\r\n\x1a\n"


async def test_the_png_decodes_to_the_box_1_code_with_the_quiet_zone(hass, world):
    from PIL import Image

    created = await enrol(world)
    body = await (await world["admin"].get(f"{QR_PATH}/{created['qr_token']}")).read()
    image = Image.open(io.BytesIO(body)).convert("L")
    modules = created["modules"]
    assert image.size == (modules + 8, modules + 8)                         # one pixel per module + 4 modules each side
    dark = lambda x, y: image.getpixel((x, y)) < 128                        # noqa: E731
    for i in range(modules + 8):                                            # the quiet zone is white all round
        for edge in range(4):
            assert not dark(i, edge) and not dark(i, modules + 7 - edge)
            assert not dark(edge, i) and not dark(modules + 7 - edge, i)
    for ox, oy in ((4, 4), (4 + modules - 7, 4), (4, 4 + modules - 7)):     # the three finder patterns
        assert all(dark(ox + dx, oy + dy) for dx in range(7) for dy in (0, 6))
        assert all(dark(ox + dx, oy + dy) for dy in range(7) for dx in (0, 6))
        assert not dark(ox + 1, oy + 1) and dark(ox + 3, oy + 3)
    assert [dark(4 + i, 4 + 6) for i in range(8, modules - 8)] == [i % 2 == 0 for i in range(8, modules - 8)]  # timing row


async def test_a_non_administrator_is_forbidden_and_never_unauthorized(hass, world, hass_client):
    created = await enrol(world)
    _user, token = await _phone_user(hass, name="plain user")
    plain = await hass_client(token)
    resp = await plain.get(f"{QR_PATH}/{created['qr_token']}")
    assert resp.status == 403 and resp.status != 401
    assert resp.headers["Cache-Control"] == "no-store"
    assert (await resp.read()) == b""                                       # nothing of the QR
    # even with the right token a non-administrator learns nothing, and with a wrong one the same 403
    assert (await plain.get(f"{QR_PATH}/nope")).status == 403


async def test_an_unknown_token_is_404_and_never_401(hass, world):
    for token in ("nope", "A" * 43, "x" * 500, "%20"):
        resp = await world["admin"].get(f"{QR_PATH}/{token}")
        assert resp.status == 404, token
        assert resp.headers["Cache-Control"] == "no-store"
    assert (await world["admin"].get(f"{QR_PATH}/")).status in (404, 405)


async def test_no_login_is_refused_by_home_assistant(hass, world, hass_client_no_auth):
    created = await enrol(world)
    anonymous = await hass_client_no_auth()
    resp = await anonymous.get(f"{QR_PATH}/{created['qr_token']}")
    assert resp.status == 401                                               # HA's own answer, before this view runs
    assert b"PNG" not in await resp.read()
    bad = await anonymous.get(f"{QR_PATH}/{created['qr_token']}", headers={"Authorization": "Bearer not-a-token"})
    assert bad.status == 401


async def test_a_read_does_not_consume_the_token(hass, world):
    created = await enrol(world)
    first = await (await world["admin"].get(f"{QR_PATH}/{created['qr_token']}")).read()
    for _ in range(3):
        resp = await world["admin"].get(f"{QR_PATH}/{created['qr_token']}")
        assert resp.status == 200 and (await resp.read()) == first


async def test_the_token_stops_working_at_cancel(hass, world):
    created = await enrol(world)
    assert (await world["admin"].get(f"{QR_PATH}/{created['qr_token']}")).status == 200
    ok(await world["ws"].send({"type": "portico/phones/cancel", "entry_id": created["entry_id"]}))
    await hass.async_block_till_done()
    assert (await world["admin"].get(f"{QR_PATH}/{created['qr_token']}")).status == 404


async def test_the_token_stops_working_when_the_code_expires(hass, world):
    from .test_enrollment_core import Clock

    clock = Clock()
    get_registry(hass).clock = clock
    created = await enrol(world)
    assert (await world["admin"].get(f"{QR_PATH}/{created['qr_token']}")).status == 200
    clock.now += 599
    assert (await world["admin"].get(f"{QR_PATH}/{created['qr_token']}")).status == 200
    clock.now += 2                                                          # past CODE_LIFETIME (10 minutes)
    assert (await world["admin"].get(f"{QR_PATH}/{created['qr_token']}")).status == 404
    assert len(get_qr_store(hass)) == 0                                     # and the PNG is forgotten, not just hidden


async def test_the_token_stops_working_when_the_phone_redeems_the_code(hass, world, qr_payloads):
    created = await enrol(world)
    await redeem(hass, qr_payloads[-1])
    assert (await world["admin"].get(f"{QR_PATH}/{created['qr_token']}")).status == 404


async def test_a_new_code_for_the_same_phone_kills_the_earlier_token(hass, world):
    first = await enrol(world, "Pixel")
    second = await enrol(world, "Pixel")
    assert (await world["admin"].get(f"{QR_PATH}/{first['qr_token']}")).status == 404
    assert (await world["admin"].get(f"{QR_PATH}/{second['qr_token']}")).status == 200


async def test_the_store_holds_only_a_digest_of_the_token_and_is_bounded(hass, world):
    created = await enrol(world)
    store = get_qr_store(hass)
    record = store.record_for_entry(created["entry_id"])
    assert created["qr_token"].encode() not in repr(record).encode() and created["qr_token"] not in json.dumps(
        {"digest": record.digest.hex()})
    fresh = QrStore()
    for n in range(40):
        fresh.put(hass, entry_id=f"e{n}", enroll_id=record.enroll_id, png=b"x", created=True)
    assert len(fresh) == 16


async def test_the_view_is_registered_once_beside_the_redeem_view(hass, world):
    enroll_view.async_register_view(hass)                                   # a second call is a no-op, not a route clash
    resp = await world["admin"].get(f"{QR_PATH}/nope")
    assert resp.status == 404


# ---- the text view: what the QR encodes, for the panel's "Copy enrolment text" ----------------------------------------------------


async def test_an_administrator_gets_the_qr_text_as_plain_text_with_no_store(hass, world):
    created = await enrol(world)
    resp = await world["admin"].get(f"{TEXT_PATH}/{created['qr_token']}")
    assert resp.status == 200
    assert resp.content_type == "text/plain"
    assert resp.headers["Cache-Control"] == "no-store"
    assert resp.headers["Pragma"] == "no-cache"
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    body = await resp.text()
    payload = json.loads(body)
    assert set(payload) >= {"ha_url", "code", "phone_name", "enroll_id"}
    assert "access_token" not in body and "refresh_token" not in body                  # no HA token in it


async def test_the_served_text_is_exactly_what_the_png_encodes(hass, world):
    """No QR decoder in this venv (test_qr_decode.py runs one when configured): the encoder is deterministic, so the PNG
    rebuilt from the served text is byte-identical to the served PNG only if that text is what was encoded."""
    from custom_components.portico import enrollment_flow

    created = await enrol(world)
    token = created["qr_token"]
    text = await (await world["admin"].get(f"{TEXT_PATH}/{token}")).text()
    png = await (await world["admin"].get(f"{QR_PATH}/{token}")).read()
    from custom_components.portico.phones_commands import PANEL_QR_BOX

    rebuilt = io.BytesIO()
    enrollment_flow._make_qr(text, PANEL_QR_BOX).make_image().save(rebuilt, format="PNG")
    assert rebuilt.getvalue() == png
    assert text == enrollment_flow._qr_text(json.loads(text))                       # compact JSON, the dialog's serialiser


async def test_a_non_administrator_is_403_on_the_text_and_never_401(hass, world, hass_client):
    created = await enrol(world)
    from homeassistant.auth.const import GROUP_ID_USER

    user = await hass.auth.async_create_user("Plain", group_ids=[GROUP_ID_USER])
    token = await hass.auth.async_create_refresh_token(user, client_id="http://example.test/")
    access = hass.auth.async_create_access_token(token)
    client = await hass_client(access)
    resp = await client.get(f"{TEXT_PATH}/{created['qr_token']}")
    assert resp.status == 403
    assert resp.headers["Cache-Control"] == "no-store"
    assert "code" not in (await resp.text())


async def test_the_text_is_404_for_an_unknown_token(hass, world):
    resp = await world["admin"].get(f"{TEXT_PATH}/nope")
    assert resp.status == 404
    assert resp.headers["Cache-Control"] == "no-store"


async def test_the_text_dies_with_the_code_like_the_png(hass, world):
    from .test_enrollment_core import Clock

    clock = Clock()
    get_registry(hass).clock = clock
    created = await enrol(world)
    url = f"{TEXT_PATH}/{created['qr_token']}"
    assert (await world["admin"].get(url)).status == 200
    clock.now += 601
    assert (await world["admin"].get(url)).status == 404
    assert len(get_qr_store(hass)) == 0                                             # the record, text included, is dropped


async def test_the_text_dies_on_cancel_redeem_and_a_new_code(hass, world, qr_payloads):
    first = await enrol(world, "Pixel")
    second = await enrol(world, "Pixel")
    assert (await world["admin"].get(f"{TEXT_PATH}/{first['qr_token']}")).status == 404
    await redeem(hass, qr_payloads[-1])
    assert (await world["admin"].get(f"{TEXT_PATH}/{second['qr_token']}")).status == 404
    third = await enrol(world, "Other")
    ok(await world["ws"].send({"type": "portico/phones/cancel", "entry_id": third["entry_id"]}))
    await hass.async_block_till_done()
    assert (await world["admin"].get(f"{TEXT_PATH}/{third['qr_token']}")).status == 404


async def test_the_text_is_never_in_a_websocket_reply_a_repr_or_the_log(hass, world, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    reply = await world["ws"].send(enrol_msg("Quiet"))
    created = ok(reply)
    text = await (await world["admin"].get(f"{TEXT_PATH}/{created['qr_token']}")).text()
    secret = json.loads(text)["code"]
    assert secret not in json.dumps(reply)
    record = get_qr_store(hass).record_for_entry(created["entry_id"])
    assert secret not in repr(record) and text not in repr(record) and text not in str(record)
    assert secret not in caplog.text and text not in caplog.text
