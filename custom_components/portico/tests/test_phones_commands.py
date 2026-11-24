"""UI split P2 and P3: `portico/phones/list | enroll | cancel | revoke | set` (phones_commands.py) over a real HA websocket.

HA-harness session. What is pinned: the header and the rows of `list` (legacy rows included, nothing written, keyed by
the entry id and never the `enroll_id`, an expired code flagged); `enroll`'s `created` reply and every typed error of the
design (4.2), `needs_confirm` as a RESULT; `cancel`'s rule that an entry is removed only when THIS server remembers this
enrolment created it and it is still waiting with a live code, and that a flag from the client is never read; that no
reply carries the QR; that a non-admin changes nothing.
"""
from __future__ import annotations

import json

import pytest
from aiohttp import ClientError
from homeassistant.setup import async_setup_component

from custom_components.portico import enroll_redeem, enrollment_core, enrollment_flow, phone_entry, phones_commands
from custom_components.portico.config_flow import DOMAIN
from custom_components.portico.enroll_qr import get_qr_store
from custom_components.portico.enroll_redeem import get_registry
from custom_components.portico.phone_entry import PhoneRecord, async_create_phone_entry, phone_entries
from custom_components.portico.provider import DATA_PROVIDERS
from custom_components.portico.push.registry import PushRegistry

from .redeem_helpers import redeem
from .test_enrollment_core import legacy_phone
from .test_enrollment_flow import (  # noqa: F401 - fixtures by name
    CREDS,
    EXTERNAL,
    FIREBASE,
    MINTED,
    _device_entry,
    _entry,
    _portico_users,
    _push_entry,
    _set_urls,
)
from .test_push_registry import _custom_integrations  # noqa: F401 - autouse fixture by name
from .test_push_registry import _phone_user
from .test_tailscale_approve import _approve_entry

pytestmark = pytest.mark.asyncio


@pytest.fixture
def qr_payloads(monkeypatch):
    """Every payload the QR builder encoded (a spy: the real builder still runs). The panel path asks for box 1, so the
    spy takes the optional box size the dialog's one-argument capture fixture does not."""
    payloads: list[dict] = []
    real = enrollment_flow._make_qr

    def _spy(text, box_size=None):
        payloads.append(json.loads(text))
        return real(text, box_size)

    monkeypatch.setattr(enrollment_flow, "_make_qr", _spy)
    return payloads


@pytest.fixture(autouse=True)
def _an_owner_exists(hass_owner_user):
    """A real install has its owner before any phone."""


@pytest.fixture
async def admin(hass, hass_ws_client):
    assert await async_setup_component(hass, DOMAIN, {})
    return await hass_ws_client(hass)


@pytest.fixture
def entry(hass):
    _set_urls(hass, external=EXTERNAL)
    return _device_entry(hass)           # the push entry, `manual`


class Ws:
    """One connection with a message counter."""

    def __init__(self, client) -> None:
        self.client = client
        self.n = 0

    async def send(self, body: dict) -> dict:
        self.n += 1
        await self.client.send_json({"id": self.n, **body})
        return await self.client.receive_json()


@pytest.fixture
def ws(admin):
    return Ws(admin)


def enrol_msg(name="Alice's Pixel", **over):
    return {"type": "portico/phones/enroll", "name": name, "allow_http": False, "ha_dial": "", **over}


def ok(frame):
    assert frame["success"], frame
    return frame["result"]


def err(frame):
    assert frame["success"] is False, frame
    return frame["error"]


# ===========================================================================================================
# list
# ===========================================================================================================


async def test_list_with_no_push_entry(hass, ws):
    result = ok(await ws.send({"type": "portico/phones/list"}))
    assert result["phones"] == []
    assert result["header"] == {
        "push_entry": False, "tailscale_mode": None, "tailscale_complete": False, "enrol_here": False,
        "ha_dial": None, "pending": 0, "pending_cap": 5, "cap_reached": False}


async def test_list_header_for_each_tailscale_state(hass, hass_storage, ws):
    push = _entry(hass)                                                    # no answer at all
    header = ok(await ws.send({"type": "portico/phones/list"}))["header"]
    assert (header["push_entry"], header["tailscale_mode"], header["enrol_here"]) == (True, None, False)
    hass.config_entries.async_update_entry(push, data={**push.data, "tailscale_mode": "auto"})   # unfinished auto
    header = ok(await ws.send({"type": "portico/phones/list"}))["header"]
    assert (header["tailscale_mode"], header["tailscale_complete"], header["enrol_here"]) == ("auto", False, False)
    hass.config_entries.async_update_entry(push, data={**push.data, **CREDS})
    header = ok(await ws.send({"type": "portico/phones/list"}))["header"]
    assert (header["tailscale_mode"], header["tailscale_complete"], header["enrol_here"]) == ("auto", True, True)
    hass.config_entries.async_update_entry(push, data={**push.data, "tailscale_mode": "manual"})
    assert ok(await ws.send({"type": "portico/phones/list"}))["header"]["enrol_here"] is True


async def test_list_header_for_approve_mode_cannot_enrol_here(hass, hass_storage, ws):
    _approve_entry(hass, hass_storage)
    header = ok(await ws.send({"type": "portico/phones/list"}))["header"]
    assert (header["tailscale_mode"], header["tailscale_complete"], header["enrol_here"]) == ("approve", True, False)


async def test_list_reads_an_entry_from_before_the_option_as_auto_without_writing(hass, ws):
    push = _entry(hass, **CREDS)                                           # credentials, no `tailscale_mode`
    before = dict(push.data)
    header = ok(await ws.send({"type": "portico/phones/list"}))["header"]
    assert (header["tailscale_mode"], header["tailscale_complete"]) == ("auto", True)
    assert dict(push.data) == before                                       # a read command writes nothing


async def test_list_header_remembers_ha_dial_and_counts_pending(hass, entry, ws):
    ok(await ws.send(enrol_msg("A", ha_dial="10.6.0.5:443")))
    ok(await ws.send(enrol_msg("B", ha_dial="10.6.0.5:443")))
    header = ok(await ws.send({"type": "portico/phones/list"}))["header"]
    assert header["ha_dial"] == "10.6.0.5:443" and header["pending"] == 2 and header["cap_reached"] is False


async def test_a_new_phone_is_a_pending_row_keyed_by_its_entry_id_and_never_the_enroll_id(hass, entry, ws):
    created = ok(await ws.send(enrol_msg()))
    (phone,) = phone_entries(hass)
    reply = await ws.send({"type": "portico/phones/list"})
    (row,) = ok(reply)["phones"]
    assert row["entry_id"] == phone.entry_id == created["entry_id"]
    assert row["name"] == "Alice's Pixel" and row["state"] == "pending" and row["expired"] is False
    assert 0 < row["code_expires_in"] <= 600 and row["legacy"] is False
    assert row["doors"] is None and row["house_receive"] is False
    assert row["push_registered"] is None and row["away_ring"] is None and row["last_connection"] is None
    assert row["enrolled_at"]
    # the enroll id (a credential for offline registration) and the HA user id are nowhere in the reply
    text = json.dumps(reply)
    assert phone.data["enroll_id"] not in text and phone.data["ha_user_id"] not in text


async def test_a_pending_phone_whose_code_was_lost_is_flagged_expired(hass, entry, ws):
    ok(await ws.send(enrol_msg()))
    get_registry(hass)._records.clear()                                    # what an HA restart does
    (row,) = ok(await ws.send({"type": "portico/phones/list"}))["phones"]
    assert row["state"] == "pending" and row["expired"] is True and row["code_expires_in"] is None


async def test_list_shows_registration_last_connection_away_ring_doors_and_house_receive(hass, entry, ws, monkeypatch):
    ok(await ws.send(enrol_msg()))
    (phone,) = phone_entries(hass)
    hass.config_entries.async_update_entry(phone, options={"doors": ["d1", "d2"], "house_receive": True})

    class Registry:
        def rows_for_phone(self, enroll_id, user_id):
            assert enroll_id == phone.data["enroll_id"] and user_id == phone.data["ha_user_id"]
            return [{"last_seen_at": "2026-10-20T08:00:00+00:00", "push_enabled": False},
                    {"last_seen_at": "2026-10-22T09:30:00+00:00", "push_enabled": True}]

    monkeypatch.setattr(phones_commands, "get_manager", lambda _hass: type("M", (), {"registry": Registry()})())
    (row,) = ok(await ws.send({"type": "portico/phones/list"}))["phones"]
    assert row["push_registered"] is True and row["away_ring"] is True
    assert row["last_connection"] == "2026-10-22T09:30:00+00:00"
    assert row["doors"] == ["d1", "d2"] and row["house_receive"] is True

    monkeypatch.setattr(phones_commands, "get_manager", lambda _hass: type("M", (), {"registry": type("R", (), {
        "rows_for_phone": lambda self, e, u: []})()})())
    (row,) = ok(await ws.send({"type": "portico/phones/list"}))["phones"]
    assert row["push_registered"] is False and row["away_ring"] is None and row["last_connection"] is None


async def test_list_includes_legacy_rows_and_writes_nothing(hass, ws):
    legacy = {"ha_user_id": "u-legacy", "name": "Old Samsung", "enroll_id": "LEGACY-ENROLL-ID-0123456789",
              "enrolled_at": "2026-09-01T10:00:00+00:00"}
    push = _push_entry(hass, firebase=None, tailscale_mode="manual", enrolled_phones=[legacy])
    before = json.dumps(dict(push.data), sort_keys=True)
    entries_before = len(hass.config_entries.async_entries(DOMAIN))
    reply = await ws.send({"type": "portico/phones/list"})
    (row,) = ok(reply)["phones"]
    assert row["legacy"] is True and row["name"] == "Old Samsung" and row["state"] == "enrolled"
    assert row["entry_id"].startswith("legacy-") and "LEGACY-ENROLL-ID" not in json.dumps(reply)
    assert row["enrolled_at"] == "2026-09-01T10:00:00+00:00"
    # the same row twice gives the same id (P3 will name it again), and nothing was adopted or written
    (again,) = ok(await ws.send({"type": "portico/phones/list"}))["phones"]
    assert again["entry_id"] == row["entry_id"]
    assert json.dumps(dict(push.data), sort_keys=True) == before
    assert len(hass.config_entries.async_entries(DOMAIN)) == entries_before and phone_entries(hass) == []


async def test_list_lists_a_legacy_row_with_no_user_and_does_not_fail(hass, ws):
    _push_entry(hass, firebase=None, enrolled_phones=[{"name": "No user"}])
    (row,) = ok(await ws.send({"type": "portico/phones/list"}))["phones"]
    assert row["name"] == "No user" and row["legacy"] is True and row["push_registered"] is None


async def test_list_survives_an_unexpected_failure_as_unknown(hass, entry, ws, monkeypatch):
    async def boom(_hass):
        raise RuntimeError("SECRET-in-an-exception")

    monkeypatch.setattr(phones_commands, "_header", boom)
    error = err(await ws.send({"type": "portico/phones/list"}))
    assert error["code"] == "unknown" and "SECRET" not in json.dumps(error)


# ===========================================================================================================
# enroll
# ===========================================================================================================


async def test_enroll_replies_created_with_the_token_and_the_size_and_never_the_qr(hass, entry, ws, qr_payloads):
    frame = await ws.send(enrol_msg(ha_dial="10.6.0.5:443"))
    reply = ok(frame)
    (phone,) = phone_entries(hass)
    assert set(reply) == {"result", "entry_id", "qr_token", "modules", "expires_in", "expires_at", "notes",
                          "dial_target", "created"}
    assert reply["result"] == "created" and reply["created"] is True and reply["entry_id"] == phone.entry_id
    assert reply["notes"] == ["manual_tailscale"] and reply["dial_target"] == "10.6.0.5:443"
    assert 595 <= reply["expires_in"] <= 600 and reply["expires_at"].endswith("+00:00")
    assert reply["modules"] == 57 or reply["modules"] > 20
    # the token is 32 random bytes, urlsafe (43 characters); it is not derived from anything in the QR
    assert len(reply["qr_token"]) == 43
    # no QR, no code, no enroll id on the wire (HA logs every frame at DEBUG)
    text = json.dumps(frame)
    payload = qr_payloads[-1]
    assert payload["code"] not in text and payload["enroll_id"] not in text and "iVBOR" not in text and "png" not in text.lower()
    # the QR the view will serve is the box-1 PNG with the 4-module quiet zone
    png = get_qr_store(hass).png_for(hass, reply["qr_token"])
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and int.from_bytes(png[16:20], "big") == reply["modules"] + 8


async def test_enroll_creates_exactly_what_the_dialog_does(hass, entry, ws, qr_payloads):
    ok(await ws.send(enrol_msg("Yael's phone")))
    (phone,) = phone_entries(hass)
    (user,) = _portico_users(hass)
    record = PhoneRecord.from_data(phone.data)
    assert (record.name, record.state, record.ha_user_id) == ("Yael's phone", "pending", user.id)
    assert qr_payloads[-1]["enroll_id"] == record.enroll_id and qr_payloads[-1]["phone_name"] == "Yael's phone"


async def test_the_name_is_stripped_and_the_remembered_ha_dial_is_written_or_forgotten(hass, entry, ws):
    ok(await ws.send(enrol_msg("  Pixel  ", ha_dial="10.6.0.5:443")))
    assert phone_entries(hass)[0].data["name"] == "Pixel" and entry.data["ha_dial"] == "10.6.0.5:443"
    ok(await ws.send(enrol_msg("Pixel 2", ha_dial="")))                    # a blank forgets it
    assert "ha_dial" not in entry.data


async def test_enroll_the_same_name_again_is_a_new_code_not_a_second_phone(hass, entry, ws):
    first = ok(await ws.send(enrol_msg("Pixel")))
    again = ok(await ws.send(enrol_msg("Pixel")))
    assert again["created"] is False and again["entry_id"] == first["entry_id"]
    assert again["notes"] == ["replaces_earlier_qr", "manual_tailscale"]
    assert len(phone_entries(hass)) == 1 and len(_portico_users(hass)) == 1
    assert first["qr_token"] != again["qr_token"]
    store = get_qr_store(hass)
    assert store.png_for(hass, first["qr_token"]) is None                 # the earlier QR is dead
    assert store.png_for(hass, again["qr_token"]) is not None


async def test_needs_confirm_is_a_result_not_an_error_and_confirming_resends_the_id(hass, entry, ws):
    user, old = await legacy_phone(hass)
    frame = await ws.send(enrol_msg("Old Pixel"))
    assert frame["success"] is True
    assert frame["result"] == {"result": "needs_confirm", "entry_id": old.entry_id, "name": "Old Pixel"}
    assert _portico_users(hass) == [user]                                  # nothing was made
    # another phone's id does not confirm this one
    assert ok(await ws.send(enrol_msg("Old Pixel", confirmed_entry_id="someone-else")))["result"] == "needs_confirm"
    done = ok(await ws.send(enrol_msg("Old Pixel", confirmed_entry_id=old.entry_id)))
    assert done["result"] == "created" and done["created"] is False and done["entry_id"] == old.entry_id
    assert done["notes"] == ["reenroll_long_lived", "manual_tailscale"]


@pytest.mark.parametrize("body, code, field", [
    ({"name": "   "}, "bad_name", "phone_name"),
    ({"name": "x" * 25}, "bad_name", "phone_name"),
    ({"ha_dial": "8.8.8.8:443"}, "invalid_ha_dial", "ha_dial"),
    ({"ha_dial": "not an address"}, "invalid_ha_dial", "ha_dial"),
])
async def test_enroll_name_and_address_errors_name_the_field_and_make_nothing(hass, entry, ws, body, code, field):
    error = err(await ws.send(enrol_msg(**body)))
    assert error["code"] == code and error["field"] == field
    assert phone_entries(hass) == [] and _portico_users(hass) == []


async def test_enroll_with_no_push_entry(hass, ws):
    _set_urls(hass, external=EXTERNAL)
    error = err(await ws.send(enrol_msg()))
    assert error["code"] == "no_push_entry" and _portico_users(hass) == []


async def test_enroll_url_errors(hass, entry, ws):
    _set_urls(hass)
    assert err(await ws.send(enrol_msg()))["code"] == "no_ha_url"
    _set_urls(hass, external="http://ha.example.org:8123")
    assert err(await ws.send(enrol_msg()))["code"] == "no_https_url"
    assert ok(await ws.send(enrol_msg(allow_http=True)))["result"] == "created"      # the box ticked


async def test_enroll_needs_tailscale_setup_for_a_blank_and_an_unfinished_answer(hass, ws):
    _set_urls(hass, external=EXTERNAL)
    push = _entry(hass)
    assert err(await ws.send(enrol_msg()))["code"] == "needs_tailscale_setup"
    hass.config_entries.async_update_entry(push, data={**push.data, "tailscale_mode": "auto"})
    assert err(await ws.send(enrol_msg()))["code"] == "needs_tailscale_setup"
    assert _portico_users(hass) == [] and phone_entries(hass) == []


async def test_enroll_in_approve_mode_is_needs_approval_and_nothing_is_made(hass, hass_storage, ws):
    _set_urls(hass, external=EXTERNAL)
    _approve_entry(hass, hass_storage)
    assert err(await ws.send(enrol_msg()))["code"] == "needs_approval"
    assert _portico_users(hass) == [] and phone_entries(hass) == []


async def test_enroll_in_auto_mode_mints_a_key_and_a_failed_mint_is_tailscale_mint_failed(hass, ws, monkeypatch, qr_payloads):
    _set_urls(hass, external=EXTERNAL)
    _entry(hass, tailscale_mode="auto", **CREDS)

    async def _mint(hass_, *, api_key, tailnet, tag):
        return MINTED

    monkeypatch.setattr(enrollment_flow, "_async_mint_tailscale_authkey", _mint)
    reply = ok(await ws.send(enrol_msg("Auto phone")))
    assert reply["notes"] == [] and qr_payloads[-1]["tailscale_authkey"] == MINTED

    async def _fail(hass_, *, api_key, tailnet, tag):
        raise ClientError("tskey-api-SENTINEL")

    monkeypatch.setattr(enrollment_flow, "_async_mint_tailscale_authkey", _fail)
    frame = await ws.send(enrol_msg("Another phone"))
    assert err(frame)["code"] == "tailscale_mint_failed" and "SENTINEL" not in json.dumps(frame)
    assert [p.data["name"] for p in phone_entries(hass)] == ["Auto phone"]     # the failed one left nothing


async def test_enroll_pending_cap(hass, entry, ws):
    for n in range(5):
        ok(await ws.send(enrol_msg(f"Phone {n}")))
    header = ok(await ws.send({"type": "portico/phones/list"}))["header"]
    assert header["pending"] == 5 and header["cap_reached"] is True
    assert err(await ws.send(enrol_msg("Phone 6")))["code"] == "pending_cap"
    assert len(_portico_users(hass)) == 5
    assert ok(await ws.send(enrol_msg("Phone 0")))["created"] is False             # drawing one again is not a sixth


async def test_enroll_unexpected_failures_are_unknown_and_leak_nothing(hass, entry, ws, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("SECRET-inside-the-exception")

    monkeypatch.setattr(enrollment_core, "create_enrollment", boom)
    frame = await ws.send(enrol_msg())
    assert err(frame)["code"] == "unknown" and "SECRET" not in json.dumps(frame)

    monkeypatch.undo()

    async def phone_entry_fails(*a, **k):
        raise RuntimeError("no entry for you")

    monkeypatch.setattr(phone_entry, "async_create_phone_entry", phone_entry_fails)
    frame = await ws.send(enrol_msg("Pixel"))
    assert err(frame)["code"] == "unknown"
    assert _portico_users(hass) == [] and phone_entries(hass) == []             # the core rolled the user back


async def test_enroll_schema(hass, entry, ws):
    for body in ({"type": "portico/phones/enroll", "allow_http": False, "ha_dial": ""},
                 {"type": "portico/phones/enroll", "name": "p", "ha_dial": ""},
                 {"type": "portico/phones/enroll", "name": "p", "allow_http": False},
                 enrol_msg(allow_http="yes"), enrol_msg(name="p" * 201), enrol_msg(ha_dial="1" * 201),
                 enrol_msg(confirmed_entry_id=""), enrol_msg(unexpected="x")):
        assert err(await ws.send(body))["code"] == "invalid_format", body
    assert _portico_users(hass) == []


# ===========================================================================================================
# cancel
# ===========================================================================================================


def cancel_msg(entry_id, **over):
    return {"type": "portico/phones/cancel", "entry_id": entry_id, **over}


async def test_cancel_removes_the_phone_this_enrolment_created(hass, entry, ws, qr_payloads):
    created = ok(await ws.send(enrol_msg()))
    (phone,) = phone_entries(hass)
    enroll_id = phone.data["enroll_id"]
    assert get_registry(hass).seconds_left(enroll_id) is not None
    assert ok(await ws.send(cancel_msg(created["entry_id"]))) == {"result": "cancelled", "removed": True}
    await hass.async_block_till_done()
    assert phone_entries(hass) == [] and _portico_users(hass) == []             # the entry and its HA user are gone
    assert get_registry(hass).seconds_left(enroll_id) is None
    assert get_qr_store(hass).png_for(hass, created["qr_token"]) is None
    # the code in the QR no longer redeems
    outcome = await get_registry(hass).async_redeem(
        hass, enroll_id=enroll_id, code=qr_payloads[-1]["code"], installation_id="inst-0123456789ab", nonce="nonce-0123456789ab")
    assert not outcome.ok


async def test_cancel_after_new_code_still_removes_the_phone_the_first_draw_created(hass, entry, ws):
    first = ok(await ws.send(enrol_msg("Pixel")))
    again = ok(await ws.send(enrol_msg("Pixel")))
    assert again["created"] is False
    assert ok(await ws.send(cancel_msg(again["entry_id"])))["removed"] is True
    assert first["entry_id"] == again["entry_id"] and phone_entries(hass) == []


async def test_cancel_of_a_redraw_of_an_old_phone_kills_the_code_and_keeps_the_phone(hass, entry, ws):
    user, old = await legacy_phone(hass)
    drawn = ok(await ws.send(enrol_msg("Old Pixel", confirmed_entry_id=old.entry_id)))
    assert drawn["created"] is False
    assert ok(await ws.send(cancel_msg(old.entry_id))) == {"result": "cancelled", "removed": False}
    assert [e.entry_id for e in phone_entries(hass)] == [old.entry_id] and _portico_users(hass) == [user]
    assert PhoneRecord.from_data(old.data).state == "enrolled"
    assert get_registry(hass).seconds_left(old.data["enroll_id"]) is None
    assert get_qr_store(hass).png_for(hass, drawn["qr_token"]) is None


async def test_cancel_of_a_pending_phone_this_server_did_not_just_draw_is_refused_and_changes_nothing(hass, entry, ws):
    # a pending phone with a live code, made by the core directly: the command never saw this enrolment
    result = await enrollment_core.create_enrollment(
        hass, entry, name="Dialog phone", allow_http=False, ha_dial_raw=None, confirmed_entry_id=None)
    assert result.created
    error = err(await ws.send(cancel_msg(result.entry_id)))
    assert error["code"] == "unknown_phone"
    assert len(phone_entries(hass)) == 1 and len(_portico_users(hass)) == 1
    assert get_registry(hass).seconds_left(phone_entries(hass)[0].data["enroll_id"]) is not None


async def test_cancel_of_a_phone_that_has_enrolled_is_refused(hass, entry, ws, qr_payloads):
    created = ok(await ws.send(enrol_msg()))
    await redeem(hass, qr_payloads[-1])                                         # the phone scanned it
    error = err(await ws.send(cancel_msg(created["entry_id"])))
    assert error["code"] == "unknown_phone"
    (phone,) = phone_entries(hass)
    assert PhoneRecord.from_data(phone.data).state == "enrolled" and len(_portico_users(hass)) == 1


async def test_cancel_when_the_code_has_expired_is_refused_and_keeps_the_entry(hass, entry, ws):
    created = ok(await ws.send(enrol_msg()))
    get_registry(hass)._records.clear()
    assert err(await ws.send(cancel_msg(created["entry_id"])))["code"] == "unknown_phone"
    assert len(phone_entries(hass)) == 1


async def test_cancel_never_trusts_a_flag_from_the_client(hass, entry, ws):
    # an old pending phone the server does not remember: a client claiming it created it changes nothing
    result = await enrollment_core.create_enrollment(
        hass, entry, name="Dialog phone", allow_http=False, ha_dial_raw=None, confirmed_entry_id=None)
    for flag in ("created", "removed", "force", "remove"):
        error = err(await ws.send(cancel_msg(result.entry_id, **{flag: True})))
        assert error["code"] == "invalid_format"                                # the schema takes the entry id alone
    assert len(phone_entries(hass)) == 1


async def test_cancel_is_for_phone_entries_only(hass, entry, ws):
    ok(await ws.send(enrol_msg()))
    assert err(await ws.send(cancel_msg(entry.entry_id)))["code"] == "unknown_phone"      # the push entry
    assert err(await ws.send(cancel_msg("no-such-entry")))["code"] == "unknown_phone"
    assert hass.config_entries.async_get_entry(entry.entry_id) is not None and len(phone_entries(hass)) == 1


async def test_cancel_dismisses_the_notice_and_a_second_cancel_is_refused(hass, entry, ws):
    from homeassistant.components.persistent_notification import _async_get_or_create_notifications

    created = ok(await ws.send(enrol_msg()))
    assert phone_entry.enroll_notification_id(created["entry_id"]) in _async_get_or_create_notifications(hass)
    ok(await ws.send(cancel_msg(created["entry_id"])))
    await hass.async_block_till_done()
    assert phone_entry.enroll_notification_id(created["entry_id"]) not in _async_get_or_create_notifications(hass)
    assert err(await ws.send(cancel_msg(created["entry_id"])))["code"] == "unknown_phone"


async def test_cancel_unexpected_failure_is_unknown(hass, entry, ws, monkeypatch):
    created = ok(await ws.send(enrol_msg()))

    async def boom(*a, **k):
        raise RuntimeError("SECRET")

    monkeypatch.setattr(phones_commands, "_cancel_locked", boom)
    frame = await ws.send(cancel_msg(created["entry_id"]))
    assert err(frame)["code"] == "unknown" and "SECRET" not in json.dumps(frame)


# ===========================================================================================================
# a non-admin changes nothing
# ===========================================================================================================


async def test_a_non_admin_cannot_list_enroll_or_cancel_and_nothing_changes(hass, hass_ws_client, entry):
    assert await async_setup_component(hass, DOMAIN, {})
    plain, token = await _phone_user(hass, name="plain")
    client = Ws(await hass_ws_client(hass, access_token=token))
    admin_ws = Ws(await hass_ws_client(hass))
    created = ok(await admin_ws.send(enrol_msg("Admin's phone")))
    users_before = len(_portico_users(hass))
    for body in ({"type": "portico/phones/list"}, enrol_msg("Intruder"), cancel_msg(created["entry_id"])):
        error = err(await client.send(body))
        assert error["code"] == "unauthorized", body
    assert len(_portico_users(hass)) == users_before and [e.data["name"] for e in phone_entries(hass)] == ["Admin's phone"]
    assert get_qr_store(hass).png_for(hass, created["qr_token"]) is not None


# ===========================================================================================================
# UI split P3: revoke
# ===========================================================================================================


def revoke_msg(entry_id, confirm=True, **over):
    return {"type": "portico/phones/revoke", "entry_id": entry_id, "confirm": confirm, **over}


def set_msg(entry_id, patch):
    return {"type": "portico/phones/set", "entry_id": entry_id, "patch": patch}


async def push_registry(hass, *rows):
    """A real push registry (what the push ring notification manager holds), with one registration per `(user_id, enroll_id)`."""
    registry = PushRegistry(hass)
    await registry.async_load()
    for n, (user_id, enroll_id) in enumerate(rows):
        await registry.async_register(
            ha_user_id=user_id, installation_id=f"inst-{n}", kind="fid", registration="R" * 20, package="com.meda.portico",
            fcm_project_id="p", app_version="1", name=f"phone {n}", enroll_id=enroll_id)
    hass.data.setdefault(DOMAIN, {})["push"] = type("Manager", (), {"registry": registry})()
    return registry


async def enrolled_phone(hass, name):
    """An enrolled phone entry with its own Portico user and its own enroll id (`legacy_phone` has one fixed id)."""
    from datetime import timedelta

    from homeassistant.auth.models import TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN

    assert await async_setup_component(hass, DOMAIN, {})
    user = await hass.auth.async_create_user(name=f"Portico — {name}", group_ids=["system-users"])
    await hass.auth.async_create_refresh_token(
        user, client_name=f"portico-{name}", token_type=TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN,
        access_token_expiration=timedelta(days=3650))
    record = PhoneRecord(ha_user_id=user.id, name=name, enroll_id=f"enroll-id-of-{name}-0123456789",
                         enrolled_at="2026-10-08T10:00:00+00:00", token_kind="llat", refresh_state="none")
    return user, await async_create_phone_entry(hass, record)


def fake_doors(hass, *names):
    """Register a provider with these doors; return their ids as Portico names them."""
    from .fake_provider import FakeProvider

    fake = FakeProvider("fake")
    for name in names:
        fake.add_door(name, name=name)
    registry = hass.data[DOMAIN][DATA_PROVIDERS]
    registry.register(fake)
    return [d.id for d in registry.doors()]


async def test_revoke_without_confirmation_is_refused_by_the_server_and_changes_nothing(hass, entry, ws):
    user, old = await legacy_phone(hass)
    registry = await push_registry(hass, (user.id, old.data["enroll_id"]))
    for body in (revoke_msg(old.entry_id, confirm=False),):
        assert err(await ws.send(body))["code"] == "confirmation_required"
    # `confirm` is required, and only the boolean true counts
    for body in ({"type": "portico/phones/revoke", "entry_id": old.entry_id},
                 revoke_msg(old.entry_id, confirm="true"), revoke_msg(old.entry_id, confirm=1)):
        assert err(await ws.send(body))["code"] == "invalid_format", body
    assert [e.entry_id for e in phone_entries(hass)] == [old.entry_id] and _portico_users(hass) == [user]
    assert len(registry.rows()) == 1


async def test_revoke_removes_the_phone_its_user_its_tokens_and_its_push_rows(hass, entry, ws):
    user, old = await legacy_phone(hass)
    registry = await push_registry(hass, (user.id, old.data["enroll_id"]))
    (token,) = user.refresh_tokens.values()
    assert ok(await ws.send(revoke_msg(old.entry_id))) == {"result": "revoked", "legacy": False}
    await hass.async_block_till_done()
    assert phone_entries(hass) == []
    assert await hass.auth.async_get_user(user.id) is None and _portico_users(hass) == []
    assert hass.auth.async_get_refresh_token(token.id) is None
    assert registry.rows() == []
    assert ok(await ws.send({"type": "portico/phones/list"}))["phones"] == []


async def test_revoke_a_pending_phone_kills_its_code_and_its_qr(hass, entry, ws, qr_payloads):
    created = ok(await ws.send(enrol_msg()))
    (phone,) = phone_entries(hass)
    enroll_id = phone.data["enroll_id"]
    assert ok(await ws.send(revoke_msg(created["entry_id"])))["result"] == "revoked"
    await hass.async_block_till_done()
    assert phone_entries(hass) == [] and _portico_users(hass) == []
    assert get_registry(hass).seconds_left(enroll_id) is None
    assert get_qr_store(hass).png_for(hass, created["qr_token"]) is None
    outcome = await get_registry(hass).async_redeem(
        hass, enroll_id=enroll_id, code=qr_payloads[-1]["code"], installation_id="inst-0123456789ab", nonce="nonce-0123456789ab")
    assert not outcome.ok


async def test_revoke_deletes_only_the_user_of_that_phone(hass, entry, ws):
    a_user, a = await enrolled_phone(hass, "Phone A")
    b_user, b = await enrolled_phone(hass, "Phone B")
    other = await hass.auth.async_create_user(name="Portico — Not A Phone", group_ids=["system-users"])
    admin = await hass.auth.async_create_user(name="Portico — admin lookalike", group_ids=["system-admin"])
    registry = await push_registry(hass, (a_user.id, a.data["enroll_id"]), (b_user.id, b.data["enroll_id"]))
    ok(await ws.send(revoke_msg(a.entry_id)))
    await hass.async_block_till_done()
    assert await hass.auth.async_get_user(a_user.id) is None
    for survivor in (b_user, other, admin):
        assert await hass.auth.async_get_user(survivor.id) is not None, survivor.name
    assert [e.entry_id for e in phone_entries(hass)] == [b.entry_id]
    assert len(b_user.refresh_tokens) == 1
    assert [r["ha_user_id"] for r in registry.rows()] == [b_user.id]


async def test_revoke_never_deletes_an_administrator_even_if_the_row_names_one(hass, entry, ws):
    admin = await hass.auth.async_create_user(name="Portico — Alice", group_ids=["system-admin"])
    from homeassistant.auth.models import TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN as LLAT

    portico_token = await hass.auth.async_create_refresh_token(admin, client_name="portico-enroll-abc", token_type=LLAT)
    own_token = await hass.auth.async_create_refresh_token(admin, client_name="Home Assistant Companion", token_type=LLAT)
    record = PhoneRecord(ha_user_id=admin.id, name="Tampered", enroll_id="tampered-enroll-id-0123456789",
                         enrolled_at="2026-10-08T10:00:00+00:00")
    phone = await async_create_phone_entry(hass, record)
    ok(await ws.send(revoke_msg(phone.entry_id)))
    await hass.async_block_till_done()
    assert phone_entries(hass) == []
    assert await hass.auth.async_get_user(admin.id) is not None          # the account stays
    assert hass.auth.async_get_refresh_token(own_token.id) is not None   # its own logins stay
    assert hass.auth.async_get_refresh_token(portico_token.id) is None   # only what enrolment made goes


async def test_revoke_never_deletes_the_owner_even_with_the_portico_name(hass, entry, ws):
    owner = await hass.auth.async_create_user(name="Portico — Owner lookalike", group_ids=["system-users"])
    owner.is_owner = True                      # named like a phone's user, and the owner: the guard's last word
    from homeassistant.auth.models import TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN as LLAT

    login = await hass.auth.async_create_refresh_token(owner, client_name="owner-login", token_type=LLAT)
    record = PhoneRecord(ha_user_id=owner.id, name="Owner row", enroll_id="owner-enroll-id-0123456789",
                         enrolled_at="2026-10-08T10:00:00+00:00")
    phone = await async_create_phone_entry(hass, record)
    ok(await ws.send(revoke_msg(phone.entry_id)))
    await hass.async_block_till_done()
    assert phone_entries(hass) == []
    assert await hass.auth.async_get_user(owner.id) is not None
    assert hass.auth.async_get_refresh_token(login.id) is not None         # HA refuses to delete the owner, but not to drop its tokens


async def test_revoke_unknown_or_non_phone_ids_change_nothing(hass, entry, ws):
    user, old = await legacy_phone(hass)
    for target in ("no-such-entry", entry.entry_id, "legacy-0123456789abcdef", user.id):
        assert err(await ws.send(revoke_msg(target)))["code"] == "unknown_phone", target
    assert hass.config_entries.async_get_entry(entry.entry_id) is not None
    assert [e.entry_id for e in phone_entries(hass)] == [old.entry_id] and _portico_users(hass) == [user]


LEGACY_A = {"ha_user_id": "set-in-test", "name": "Old Samsung", "enroll_id": "LEGACY-A-ENROLL-0123456789",
            "enrolled_at": "2026-09-01T10:00:00+00:00"}
LEGACY_B = {"ha_user_id": "set-in-test-b", "name": "Old Nokia", "enroll_id": "LEGACY-B-ENROLL-0123456789",
            "enrolled_at": "2026-09-02T10:00:00+00:00"}


async def _legacy_world(hass):
    assert await async_setup_component(hass, DOMAIN, {})
    a_user = await hass.auth.async_create_user(name="Portico — Old Samsung", group_ids=["system-users"])
    b_user = await hass.auth.async_create_user(name="Portico — Old Nokia", group_ids=["system-users"])
    rows = [{**LEGACY_A, "ha_user_id": a_user.id}, {**LEGACY_B, "ha_user_id": b_user.id}]
    push = _push_entry(hass, firebase=None, tailscale_mode="manual", enrolled_phones=rows)
    return push, a_user, b_user, rows


async def test_revoke_a_legacy_row_takes_it_off_its_entry_and_revokes_its_user_and_push_rows(hass, ws):
    push, a_user, b_user, rows = await _legacy_world(hass)
    registry = await push_registry(hass, (a_user.id, rows[0]["enroll_id"]), (b_user.id, rows[1]["enroll_id"]))
    listed = ok(await ws.send({"type": "portico/phones/list"}))["phones"]
    row_a = next(r for r in listed if r["name"] == "Old Samsung")
    assert row_a["legacy"] is True
    assert ok(await ws.send(revoke_msg(row_a["entry_id"]))) == {"result": "revoked", "legacy": True}
    await hass.async_block_till_done()
    assert [r["name"] for r in push.data["enrolled_phones"]] == ["Old Nokia"]
    assert await hass.auth.async_get_user(a_user.id) is None and await hass.auth.async_get_user(b_user.id) is not None
    assert [r["ha_user_id"] for r in registry.rows()] == [b_user.id]
    assert [r["name"] for r in ok(await ws.send({"type": "portico/phones/list"}))["phones"]] == ["Old Nokia"]
    # the last row: the key goes with it
    (row_b,) = ok(await ws.send({"type": "portico/phones/list"}))["phones"]
    ok(await ws.send(revoke_msg(row_b["entry_id"])))
    assert "enrolled_phones" not in push.data and registry.rows() == []


async def test_revoke_a_legacy_row_with_no_ha_user_drops_the_row_and_its_registrations(hass, ws):
    assert await async_setup_component(hass, DOMAIN, {})
    bystander = await hass.auth.async_create_user(name="Portico — Bystander", group_ids=["system-users"])
    push = _push_entry(hass, firebase=None, tailscale_mode="manual",
                       enrolled_phones=[{"name": "No user", "enroll_id": "NO-USER-ENROLL-0123456789"}])
    registry = await push_registry(hass, (bystander.id, "NO-USER-ENROLL-0123456789"), (bystander.id, "OTHER-ENROLL-0123456789"))
    (row,) = ok(await ws.send({"type": "portico/phones/list"}))["phones"]
    ok(await ws.send(revoke_msg(row["entry_id"])))
    assert "enrolled_phones" not in push.data
    assert await hass.auth.async_get_user(bystander.id) is not None
    assert [r["enroll_id"] for r in registry.rows()] == ["OTHER-ENROLL-0123456789"]


async def test_revoke_unexpected_failure_is_unknown(hass, entry, ws, monkeypatch):
    _user, old = await legacy_phone(hass)

    async def boom(*a, **k):
        raise RuntimeError("SECRET")

    monkeypatch.setattr(phones_commands, "_revoke_locked", boom)
    frame = await ws.send(revoke_msg(old.entry_id))
    assert err(frame)["code"] == "unknown" and "SECRET" not in json.dumps(frame)
    assert len(phone_entries(hass)) == 1


# ===========================================================================================================
# UI split P3: set
# ===========================================================================================================


async def test_set_doors_validates_against_the_doors_portico_has_now(hass, entry, ws):
    _user, old = await legacy_phone(hass)
    door_a, door_b = fake_doors(hass, "gate", "front")
    assert phone_entry.phone_dict(old)["doors"] is None
    reply = ok(await ws.send(set_msg(old.entry_id, {"doors": [door_b, door_a, door_b]})))
    assert reply["result"] == "updated" and reply["phone"]["doors"] == [door_b, door_a]       # once each, in order
    assert old.options["doors"] == [door_b, door_a]
    assert not phone_entry.may_open(phone_entry.phone_dict(old), "elsewhere") and phone_entry.may_open(phone_entry.phone_dict(old), door_a)
    # one unknown id refuses the whole patch and writes nothing, even beside a valid change
    error = err(await ws.send(set_msg(old.entry_id, {"doors": [door_a, "no-such-door"], "house_receive": True})))
    assert error["code"] == "unknown_door" and error["field"] == "doors"
    assert old.options["doors"] == [door_b, door_a] and "house_receive" not in old.options
    # null is every door again; an empty list is no door (as the dialog allows)
    assert ok(await ws.send(set_msg(old.entry_id, {"doors": None})))["phone"]["doors"] is None
    assert old.options["doors"] is None
    assert ok(await ws.send(set_msg(old.entry_id, {"doors": []})))["phone"]["doors"] == []


async def test_set_doors_with_no_doors_known_refuses_every_id(hass, entry, ws):
    _user, old = await legacy_phone(hass)
    assert err(await ws.send(set_msg(old.entry_id, {"doors": ["gate"]})))["code"] == "unknown_door"
    assert "doors" not in old.options


async def test_what_set_writes_the_dialog_reads_back(hass, entry, ws):
    _user, old = await legacy_phone(hass)
    door_a, door_b = fake_doors(hass, "gate", "front")
    ok(await ws.send(set_msg(old.entry_id, {"doors": [door_a], "house_receive": True})))
    assert phone_entry.phone_house_receive(hass, old.data["ha_user_id"]) is True
    flow = await hass.config_entries.options.async_init(old.entry_id)
    defaults = {str(k): k.default() for k in flow["data_schema"].schema}
    assert defaults == {"all_doors": False, "doors": [door_a]}
    created = await hass.config_entries.options.async_configure(flow["flow_id"], {"all_doors": False, "doors": [door_a, door_b]})
    assert created["data"] == {"doors": [door_a, door_b], "house_receive": True}     # the dialog keeps the switch we wrote
    (row,) = ok(await ws.send({"type": "portico/phones/list"}))["phones"]
    assert row["doors"] == [door_a, door_b] and row["house_receive"] is True


async def test_set_house_receive_on_and_off_leaves_the_doors_alone(hass, entry, ws):
    _user, old = await legacy_phone(hass)
    (door,) = fake_doors(hass, "gate")
    ok(await ws.send(set_msg(old.entry_id, {"doors": [door]})))
    assert ok(await ws.send(set_msg(old.entry_id, {"house_receive": True})))["phone"]["house_receive"] is True
    assert old.options == {"doors": [door], "house_receive": True}
    assert ok(await ws.send(set_msg(old.entry_id, {"house_receive": False})))["phone"]["house_receive"] is False
    assert old.options == {"doors": [door], "house_receive": False}


async def test_set_away_ring_switches_every_registration_of_that_phone_and_no_other(hass, entry, ws):
    a_user, a = await enrolled_phone(hass, "Phone A")
    b_user, b = await enrolled_phone(hass, "Phone B")
    registry = await push_registry(hass, (a_user.id, a.data["enroll_id"]), (a_user.id, a.data["enroll_id"]),
                                   (b_user.id, b.data["enroll_id"]))
    by_user = lambda uid: [r["push_enabled"] for r in registry.rows() if r["ha_user_id"] == uid]  # noqa: E731
    reply = ok(await ws.send(set_msg(a.entry_id, {"away_ring": True})))
    assert reply["phone"]["away_ring"] is True
    assert by_user(a_user.id) == [True, True] and by_user(b_user.id) == [False]
    assert ok(await ws.send(set_msg(a.entry_id, {"away_ring": False})))["phone"]["away_ring"] is False
    assert by_user(a_user.id) == [False, False]


async def test_set_away_ring_needs_a_registration(hass, entry, ws):
    _user, old = await legacy_phone(hass)
    await push_registry(hass)                                               # a manager with no rows
    assert err(await ws.send(set_msg(old.entry_id, {"away_ring": True})))["code"] == "no_registration"
    hass.data[DOMAIN].pop("push")                                           # no manager at all
    assert err(await ws.send(set_msg(old.entry_id, {"away_ring": True})))["code"] == "no_registration"


async def test_set_away_ring_works_on_a_legacy_row_but_doors_and_house_calls_do_not(hass, ws):
    push, a_user, _b_user, rows = await _legacy_world(hass)
    registry = await push_registry(hass, (a_user.id, rows[0]["enroll_id"]))
    row_a = next(r for r in ok(await ws.send({"type": "portico/phones/list"}))["phones"] if r["name"] == "Old Samsung")
    assert ok(await ws.send(set_msg(row_a["entry_id"], {"away_ring": True})))["phone"]["away_ring"] is True
    assert registry.rows()[0]["push_enabled"] is True
    before = json.dumps(dict(push.data), sort_keys=True)
    for patch in ({"doors": None}, {"house_receive": True}, {"away_ring": True, "doors": None}):
        assert err(await ws.send(set_msg(row_a["entry_id"], patch)))["code"] == "legacy_phone", patch
    assert json.dumps(dict(push.data), sort_keys=True) == before


@pytest.mark.parametrize("patch", [
    {}, {"nonsense": True}, {"away_ring": "yes"}, {"house_receive": 1}, {"doors": "gate"}, {"doors": [1]},
    {"doors": [""]}, {"doors": ["d"] * 65}, {"away_ring": True, "extra": 1},
])
async def test_set_a_bad_patch_is_refused_and_writes_nothing(hass, entry, ws, patch):
    _user, old = await legacy_phone(hass)
    assert err(await ws.send(set_msg(old.entry_id, patch)))["code"] == "bad_patch"
    assert dict(old.options) == {}


async def test_set_unknown_phone_and_missing_fields(hass, entry, ws):
    await legacy_phone(hass)
    assert err(await ws.send(set_msg("no-such-entry", {"house_receive": True})))["code"] == "unknown_phone"
    assert err(await ws.send(set_msg(entry.entry_id, {"house_receive": True})))["code"] == "unknown_phone"
    assert err(await ws.send({"type": "portico/phones/set", "entry_id": "x"}))["code"] == "invalid_format"
    assert err(await ws.send({"type": "portico/phones/set", "patch": {"away_ring": True}}))["code"] == "invalid_format"


async def test_set_unexpected_failure_is_unknown(hass, entry, ws, monkeypatch):
    _user, old = await legacy_phone(hass)

    async def boom(*a, **k):
        raise RuntimeError("SECRET")

    monkeypatch.setattr(phones_commands, "_set_locked", boom)
    frame = await ws.send(set_msg(old.entry_id, {"house_receive": True}))
    assert err(frame)["code"] == "unknown" and "SECRET" not in json.dumps(frame)


async def test_a_non_admin_cannot_revoke_or_set_and_nothing_changes(hass, hass_ws_client, entry):
    user, old = await legacy_phone(hass)
    (door,) = fake_doors(hass, "gate")
    registry = await push_registry(hass, (user.id, old.data["enroll_id"]))
    callers = {}
    for who in ("plain", "enrolled"):
        _u, token = await _phone_user(hass, name=who)
        callers[who] = Ws(await hass_ws_client(hass, access_token=token))
    for who, client in callers.items():
        for body in (revoke_msg(old.entry_id), set_msg(old.entry_id, {"doors": [door], "house_receive": True, "away_ring": True})):
            assert err(await client.send(body))["code"] == "unauthorized", (who, body)
    assert [e.entry_id for e in phone_entries(hass)] == [old.entry_id] and await hass.auth.async_get_user(user.id) is not None
    assert dict(old.options) == {} and [r["push_enabled"] for r in registry.rows()] == [False]
