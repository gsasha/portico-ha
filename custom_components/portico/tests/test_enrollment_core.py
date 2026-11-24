"""UI split P1: the enrolment core (`enrollment_core.create_enrollment`) called directly, without the dialog.

HA-harness session. What is pinned: one test per result type; every early exit and that it happens BEFORE the thing
it guards (a key, a user); the order of operations (confirm before mint before user); the user removed on EVERY
failure path; the pending cap and where it is checked; the lock; the persistent notice (id, no code, dismissed);
that the helpers are reached through `enrollment_flow`'s namespace (so the existing monkeypatches keep working);
`EnrollRegistry.seconds_left`; and the cap and the name rule as the dialog shows them.
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
import struct
from datetime import timedelta
from pathlib import Path

import pytest
from aiohttp import ClientError
from homeassistant.auth.const import GROUP_ID_USER
from homeassistant.auth.models import TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN
from homeassistant.components.persistent_notification import _async_get_or_create_notifications
from homeassistant.setup import async_setup_component

from custom_components.portico import enroll_redeem, enrollment_core, enrollment_flow, phone_entry
from custom_components.portico.config_flow import DOMAIN
from custom_components.portico.enrollment_core import (
    Created,
    Error,
    NeedsApproval,
    NeedsConfirmReenroll,
    NeedsTailscaleSetup,
    create_enrollment,
)
from custom_components.portico.enroll_redeem import EnrollRegistry, get_registry
from custom_components.portico.phone_entry import PhoneRecord, async_create_phone_entry, phone_entries

from .redeem_helpers import redeem
from .test_enrollment_flow import (  # noqa: F401 - fixtures by name
    CREDS,
    EXTERNAL,
    FIREBASE,
    MINTED,
    _device_entry,
    _entry,
    _open_phone_form,
    _portico_users,
    _push_entry,
    _qr_capture,
    _set_urls,
    qr_payloads,
    qr_texts,
)
from .test_push_registry import _custom_integrations  # noqa: F401 - autouse fixture by name
from .test_tailscale_approve import _approve_entry


@pytest.fixture(autouse=True)
def _an_owner_exists(hass_owner_user):
    """A real install has its owner before any phone (Portico never removes an owner)."""


@pytest.fixture
def entry(hass):
    _set_urls(hass, external=EXTERNAL)
    return _device_entry(hass)


async def call(hass, entry, name="Alice's Pixel", **overrides):
    kwargs = {"name": name, "allow_http": False, "ha_dial_raw": None, "confirmed_entry_id": None}
    kwargs.update(overrides)
    return await create_enrollment(hass, entry, **kwargs)


def png_width(png: bytes) -> int:
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">I", png[16:20])[0]


def notices(hass) -> dict:
    return dict(_async_get_or_create_notifications(hass))


async def legacy_phone(hass, name="Old Pixel"):
    """A phone enrolled before DEV-544: a Portico user with a long-lived token and an entry that says `llat`."""
    assert await async_setup_component(hass, DOMAIN, {})
    user = await hass.auth.async_create_user(name=f"Portico — {name}", group_ids=[GROUP_ID_USER])
    await hass.auth.async_create_refresh_token(
        user, client_name=f"portico-oldentry-{user.id}", token_type=TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN,
        access_token_expiration=timedelta(days=3650))
    record = PhoneRecord(ha_user_id=user.id, name=name, enroll_id="legacy-enroll-id-0123456789abcdef",
                         enrolled_at="2026-10-08T10:00:00+00:00", token_kind="llat", refresh_state="none")
    return user, await async_create_phone_entry(hass, record)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(hass):
    fake = Clock()
    get_registry(hass).clock = fake
    return fake


# ===========================================================================================================
# One test per result type
# ===========================================================================================================


async def test_created_for_a_new_phone(hass, entry, qr_payloads):
    result = await call(hass, entry)
    assert isinstance(result, Created)
    (phone,) = phone_entries(hass)
    (user,) = _portico_users(hass)
    assert result.entry_id == phone.entry_id and result.ha_user_id == user.id == phone.data["ha_user_id"]
    assert result.created is True and result.mode == "manual" and result.notes == ("manual_tailscale",)
    assert phone.data["state"] == "pending" and phone.data["name"] == "Alice's Pixel"
    assert 595 <= result.expires_in <= 600 and result.expires_at > enrollment_core.dt_util.utcnow()
    assert png_width(result.qr_png) == (result.modules + 8) * enrollment_flow.QR_BOX_SIZE
    assert result.modules > 0 and result.dial_target == qr_payloads[-1]["ha_dial"]
    # The code in the QR is live, and it is the one the phone entry's enroll id belongs to.
    assert qr_payloads[-1]["enroll_id"] == phone.data["enroll_id"]
    assert get_registry(hass).seconds_left(phone.data["enroll_id"]) is not None
    assert "qr_png" not in repr(result)                                  # a repr (a log line) never carries the image


async def test_the_png_box_size_is_a_parameter_and_the_dialogs_stays_pinned(hass, entry):
    default = await call(hass, entry, "a")
    one = await call(hass, entry, "b", qr_box_size=1)
    assert png_width(default.qr_png) == (default.modules + 8) * 4 == (default.modules + 8) * enrollment_flow.QR_BOX_SIZE
    assert png_width(one.qr_png) == one.modules + 8
    assert enrollment_flow.QR_BOX_SIZE == 4
    # The same payload shape gives the same module count whatever the box.
    assert default.modules == one.modules


async def test_created_again_for_a_pending_phone_replaces_the_earlier_code(hass, entry, qr_payloads):
    first = await call(hass, entry, "Pixel")
    again = await call(hass, entry, "Pixel")
    assert isinstance(again, Created) and again.created is False
    assert again.notes == ("replaces_earlier_qr", "manual_tailscale")
    assert again.entry_id == first.entry_id and again.ha_user_id == first.ha_user_id
    assert len(_portico_users(hass)) == 1 and len(phone_entries(hass)) == 1
    assert (await redeem_outcome(hass, qr_payloads[0])).kind in ("wrong_code", "unknown")
    assert (await redeem_outcome(hass, qr_payloads[1])).ok


async def redeem_outcome(hass, payload):
    return await get_registry(hass).async_redeem(
        hass, enroll_id=payload["enroll_id"], code=payload["code"], installation_id="inst-0123456789ab",
        nonce="nonce-0123456789ab")


async def test_notes_carry_the_fcm_and_replacement_facts(hass, qr_payloads):
    _set_urls(hass, external=EXTERNAL)
    push = _push_entry(hass, firebase={**FIREBASE, "api_key": "has a space"}, tailscale_mode="manual")
    result = await call(hass, push, "Pixel")
    assert result.notes == ("fcm_skipped", "manual_tailscale")
    again = await call(hass, push, "Pixel")
    assert again.notes == ("replaces_earlier_qr", "fcm_skipped", "manual_tailscale")


async def test_needs_confirm_reenroll_for_a_long_lived_phone_then_created(hass, entry, qr_payloads):
    user, old = await legacy_phone(hass)
    ask = await call(hass, entry, "Old Pixel")
    assert ask == NeedsConfirmReenroll(old.entry_id, "Old Pixel")
    assert qr_payloads == [] and len(_portico_users(hass)) == 1
    # A confirmation for another entry id is not this phone's.
    assert isinstance(await call(hass, entry, "Old Pixel", confirmed_entry_id="someone-else"), NeedsConfirmReenroll)
    done = await call(hass, entry, "Old Pixel", confirmed_entry_id=old.entry_id)
    assert isinstance(done, Created) and done.created is False and done.ha_user_id == user.id
    assert done.notes == ("reenroll_long_lived", "manual_tailscale")
    assert PhoneRecord.from_data(old.data).state == "enrolled"        # the old token keeps working until redeemed


async def test_needs_tailscale_setup_for_an_entry_with_no_answer(hass):
    _set_urls(hass, external=EXTERNAL)
    bare = _entry(hass)                                               # no mode, no credentials
    assert await call(hass, bare) == NeedsTailscaleSetup(None)
    assert "tailscale_mode" not in bare.data and _portico_users(hass) == []


async def test_needs_tailscale_setup_for_an_unfinished_auto_answer(hass):
    _set_urls(hass, external=EXTERNAL)
    half = _entry(hass, tailscale_mode="auto")
    assert await call(hass, half) == NeedsTailscaleSetup("auto")
    assert _portico_users(hass) == []


async def test_an_entry_from_before_the_option_is_backfilled_to_auto(hass, monkeypatch):
    _set_urls(hass, external=EXTERNAL)

    async def _mint(hass_, *, api_key, tailnet, tag):
        return MINTED

    monkeypatch.setattr(enrollment_flow, "_async_mint_tailscale_authkey", _mint)
    old = _entry(hass, **CREDS)
    assert "tailscale_mode" not in old.data
    result = await call(hass, old)
    assert isinstance(result, Created) and result.mode == "auto"
    assert old.data["tailscale_mode"] == "auto"
    (phone,) = phone_entries(hass)
    assert phone.data["tailscale_mode"] == "auto"


async def test_needs_approval_without_a_key_then_created_with_one(hass, hass_storage, qr_payloads):
    _set_urls(hass, external=EXTERNAL)
    approve = _approve_entry(hass, hass_storage)
    assert await call(hass, approve) == NeedsApproval()
    assert _portico_users(hass) == [] and qr_payloads == []
    result = await call(hass, approve, approved_authkey="tskey-auth-APPROVED-0123456789")
    assert isinstance(result, Created) and result.mode == "approve"
    assert qr_payloads[-1]["tailscale_authkey"] == "tskey-auth-APPROVED-0123456789"


# ===========================================================================================================
# Every early exit, and that it comes before what it guards
# ===========================================================================================================


@pytest.fixture
def spies(hass, monkeypatch):
    """Record the key mints and the user creations."""
    calls: list[str] = []

    async def _mint(hass_, *, api_key, tailnet, tag):
        calls.append("mint")
        return MINTED

    real_create = hass.auth.async_create_user

    async def _create_user(*args, **kwargs):
        calls.append("create_user")
        return await real_create(*args, **kwargs)

    monkeypatch.setattr(enrollment_flow, "_async_mint_tailscale_authkey", _mint)
    monkeypatch.setattr(hass.auth, "async_create_user", _create_user)
    return calls


@pytest.mark.parametrize("name", ["", "   ", "\t\n", "x" * 25, "  " + "y" * 25 + "  ", None, 7])
async def test_a_bad_name_is_refused_before_anything(hass, spies, name):
    _set_urls(hass, external=EXTERNAL)
    auto = _entry(hass, tailscale_mode="auto", **CREDS)
    assert await call(hass, auto, name) == Error("bad_name", "phone_name")
    assert spies == [] and _portico_users(hass) == [] and phone_entries(hass) == []


async def test_the_name_is_stripped_and_forty_characters_are_allowed(hass, entry, qr_payloads):
    result = await call(hass, entry, "   " + "n" * 24 + "  ")
    assert isinstance(result, Created)
    (phone,) = phone_entries(hass)
    assert phone.data["name"] == "n" * 24 and qr_payloads[-1]["phone_name"] == "n" * 24
    assert _portico_users(hass)[0].name == "Portico — " + "n" * 24
    assert isinstance(await call(hass, entry, " Pixel "), Created)
    assert [e.data["name"] for e in phone_entries(hass)] == ["n" * 24, "Pixel"]


async def test_a_bad_dial_is_refused_with_its_field_before_anything(hass, spies):
    _set_urls(hass, external=EXTERNAL)
    auto = _entry(hass, tailscale_mode="auto", **CREDS)
    assert await call(hass, auto, ha_dial_raw="ha.local:443") == Error("invalid_ha_dial", "ha_dial")
    assert spies == [] and _portico_users(hass) == []


async def test_a_typed_dial_is_kept_and_a_blank_one_forgets_it(hass, entry, qr_payloads):
    assert isinstance(await call(hass, entry, "a", ha_dial_raw="  10.6.0.5:443 "), Created)
    assert entry.data["ha_dial"] == "10.6.0.5:443" and qr_payloads[-1]["ha_dial"] == "10.6.0.5:443"
    assert isinstance(await call(hass, entry, "b", ha_dial_raw=""), Created)
    assert "ha_dial" not in entry.data


async def test_the_url_checks_come_before_a_key_or_a_user(hass, spies):
    auto = _entry(hass, tailscale_mode="auto", **CREDS)
    _set_urls(hass)
    assert await call(hass, auto) == Error("no_ha_url")
    _set_urls(hass, internal="http://ha.lan:8123")
    assert await call(hass, auto) == Error("no_https_url")
    assert spies == [] and _portico_users(hass) == []
    assert isinstance(await call(hass, auto, allow_http=True), Created)
    assert spies == ["mint", "create_user"]


async def test_no_push_entry(hass, spies):
    _set_urls(hass, external=EXTERNAL)
    assert await call(hass, None) == Error("no_push_entry")
    assert spies == [] and _portico_users(hass) == []


async def test_a_failed_mint_is_a_typed_error_and_no_user(hass, caplog, monkeypatch):
    _set_urls(hass, external=EXTERNAL)

    async def _mint(hass_, *, api_key, tailnet, tag):
        raise ClientError("tskey-api-SENTINEL")

    monkeypatch.setattr(enrollment_flow, "_async_mint_tailscale_authkey", _mint)
    auto = _entry(hass, tailscale_mode="auto", **CREDS)
    assert await call(hass, auto) == Error("tailscale_mint_failed")
    assert _portico_users(hass) == [] and phone_entries(hass) == []
    assert "SENTINEL" not in caplog.text and "ClientError" in caplog.text


async def test_the_order_confirm_then_mint_then_user(hass, spies):
    _set_urls(hass, external=EXTERNAL)
    auto = _entry(hass, tailscale_mode="auto", **CREDS)
    _user, old = await legacy_phone(hass)
    spies.clear()
    assert isinstance(await call(hass, auto, "Old Pixel"), NeedsConfirmReenroll)
    assert spies == []                                                # nothing minted for an unconfirmed replacement
    assert isinstance(await call(hass, auto, "Old Pixel", confirmed_entry_id=old.entry_id), Created)
    assert spies == ["mint"]                                          # the phone reuses its user
    assert isinstance(await call(hass, auto, "New"), Created)
    assert spies == ["mint", "mint", "create_user"]


# ===========================================================================================================
# The user is removed on EVERY failure path
# ===========================================================================================================


def _fail_fcm(hass, monkeypatch):
    def _boom(hass_, domain):
        raise RuntimeError("fcm")
    monkeypatch.setattr(enrollment_flow, "_fcm_block", _boom)


def _fail_qr(hass, monkeypatch):
    def _boom(payload):
        raise RuntimeError("qr")
    monkeypatch.setattr(enrollment_flow, "_build_qr_data_uri", _boom)


def _bad_uri(hass, monkeypatch):
    monkeypatch.setattr(enrollment_flow, "_build_qr_data_uri", lambda payload: "data:image/png;base64,@@@@")


def _fail_code(hass, monkeypatch):
    def _boom():
        raise RuntimeError("code")
    monkeypatch.setattr(enroll_redeem, "new_code", _boom)


def _fail_write_back(hass, monkeypatch):
    real = hass.config_entries.async_update_entry

    def _update(entry, **kwargs):
        if entry.data.get("entry_type") == "push":
            raise RuntimeError("write-back")
        return real(entry, **kwargs)

    monkeypatch.setattr(hass.config_entries, "async_update_entry", _update)


def _fail_phone_entry(hass, monkeypatch):
    async def _boom(hass_, record):
        raise RuntimeError("entry")
    monkeypatch.setattr(phone_entry, "async_create_phone_entry", _boom)


def _refuse_phone_entry(hass, monkeypatch):
    """HA's flow refusing to create the entry (async_create_phone_entry raises RuntimeError itself)."""
    async def _abort(*args, **kwargs):
        return {"type": "abort", "reason": "nope"}
    monkeypatch.setattr(hass.config_entries.flow, "async_init", _abort)


FAILURES = [_fail_fcm, _fail_qr, _bad_uri, _fail_code, _fail_write_back, _fail_phone_entry, _refuse_phone_entry]


@pytest.mark.parametrize("fail", FAILURES, ids=lambda f: f.__name__)
async def test_every_failure_after_the_user_exists_removes_it(hass, entry, monkeypatch, fail):
    users_before = len(hass.auth._store._users)
    fail(hass, monkeypatch)
    result = await call(hass, entry)
    assert isinstance(result, Error) and result.code == "unknown" and isinstance(result.exception, Exception)
    assert len(hass.auth._store._users) == users_before and _portico_users(hass) == []
    assert phone_entries(hass) == [] and len(get_registry(hass)) == 0
    assert notices(hass) == {}


async def test_a_cancellation_after_the_user_exists_removes_it_and_propagates(hass, entry, monkeypatch):
    def _cancel(payload):
        raise asyncio.CancelledError

    monkeypatch.setattr(enrollment_flow, "_build_qr_data_uri", _cancel)
    with pytest.raises(asyncio.CancelledError):
        await call(hass, entry)
    assert _portico_users(hass) == [] and phone_entries(hass) == [] and len(get_registry(hass)) == 0


async def test_a_failure_when_drawing_again_keeps_the_phone_its_user_and_its_earlier_code(hass, entry, qr_payloads,
                                                                                        monkeypatch):
    first = await call(hass, entry, "Pixel")
    _fail_qr(hass, monkeypatch)
    result = await call(hass, entry, "Pixel")
    assert isinstance(result, Error) and result.code == "unknown"
    assert len(_portico_users(hass)) == 1 and len(phone_entries(hass)) == 1
    assert (await redeem_outcome(hass, qr_payloads[0])).ok                 # the earlier QR still works
    assert first.created is True


async def test_a_failure_does_not_use_up_the_cap(hass, entry, monkeypatch):
    for fail in FAILURES * 2:
        with monkeypatch.context() as m:
            fail(hass, m)
            assert (await call(hass, entry)).code == "unknown"
    for i in range(enrollment_core.PENDING_CAP):
        assert isinstance(await call(hass, entry, f"p{i}"), Created)


# ===========================================================================================================
# The cap
# ===========================================================================================================


async def test_the_sixth_pending_phone_is_refused_before_a_key_or_a_user(hass, spies, qr_payloads):
    _set_urls(hass, external=EXTERNAL)
    auto = _entry(hass, tailscale_mode="auto", **CREDS)
    for i in range(5):
        assert isinstance(await call(hass, auto, f"p{i}"), Created)
    assert spies == ["mint", "create_user"] * 5
    spies.clear()
    assert await call(hass, auto, "p5") == Error("pending_cap")
    assert spies == [] and len(_portico_users(hass)) == 5 and len(phone_entries(hass)) == 5 and len(qr_payloads) == 5


async def test_drawing_the_same_phone_again_at_the_cap_is_not_a_sixth(hass, entry):
    for i in range(5):
        assert isinstance(await call(hass, entry, f"p{i}"), Created)
    assert (await call(hass, entry, "p6")).code == "pending_cap"
    again = await call(hass, entry, "p2")
    assert isinstance(again, Created) and again.created is False
    again = await call(hass, entry, "p2")
    assert isinstance(again, Created)
    assert len(phone_entries(hass)) == 5 and len(_portico_users(hass)) == 5


async def test_an_expired_code_or_an_enrolled_phone_frees_a_place(hass, entry, qr_payloads, clock):
    for i in range(5):
        assert isinstance(await call(hass, entry, f"p{i}"), Created)
    assert (await call(hass, entry, "x")).code == "pending_cap"
    assert (await redeem_outcome(hass, qr_payloads[0])).ok                 # p0 enrols: no longer pending
    assert isinstance(await call(hass, entry, "x"), Created)
    assert (await call(hass, entry, "y")).code == "pending_cap"
    clock.now += enroll_redeem.CODE_LIFETIME.total_seconds() + 1           # every code is past its expiry
    assert isinstance(await call(hass, entry, "y"), Created)


async def test_a_re_draw_of_an_expired_pending_phone_counts_as_one_more(hass, entry, clock):
    for i in range(5):
        assert isinstance(await call(hass, entry, f"p{i}"), Created)
    clock.now += enroll_redeem.CODE_LIFETIME.total_seconds() + 1
    for i in range(1, 5):
        assert isinstance(await call(hass, entry, f"q{i}"), Created)       # four others live
    again = await call(hass, entry, "p0")                                  # p0's re-draw makes the fifth live one
    assert isinstance(again, Created) and again.created is False
    assert (await call(hass, entry, "p0")).created is False                # a live one drawn again: not a sixth
    assert (await call(hass, entry, "p9")).code == "pending_cap"
    assert (await call(hass, entry, "p1")).code == "pending_cap"           # p1 is expired, so its re-draw would be one


async def test_a_long_lived_phone_does_not_count_and_is_not_refused_below_the_cap(hass, entry):
    _user, old = await legacy_phone(hass)
    for i in range(5):
        assert isinstance(await call(hass, entry, f"p{i}"), Created)
    assert isinstance(await call(hass, entry, "Old Pixel", confirmed_entry_id=old.entry_id), Created)


# ===========================================================================================================
# The lock
# ===========================================================================================================


async def test_two_concurrent_calls_with_one_name_create_one_user(hass, entry):
    results = await asyncio.gather(call(hass, entry, "Pixel"), call(hass, entry, "Pixel"))
    assert all(isinstance(r, Created) for r in results)
    assert sorted(r.created for r in results) == [False, True]
    assert len(_portico_users(hass)) == 1 and len(phone_entries(hass)) == 1
    assert results[0].ha_user_id == results[1].ha_user_id


async def test_concurrent_calls_cannot_pass_the_cap_together(hass, entry):
    results = await asyncio.gather(*(call(hass, entry, f"p{i}") for i in range(8)))
    assert sum(isinstance(r, Created) for r in results) == 5
    assert sum(r == Error("pending_cap") for r in results) == 3
    assert len(_portico_users(hass)) == 5 and len(phone_entries(hass)) == 5


# ===========================================================================================================
# The notice
# ===========================================================================================================


async def test_the_notice_has_the_entry_id_and_never_the_code(hass, entry, qr_payloads):
    result = await call(hass, entry, "Pixel", ha_dial_raw="10.6.0.5:443")
    (nid, notice) = next(iter(notices(hass).items()))
    assert nid == f"portico_enroll_{result.entry_id}" and list(notices(hass)) == [nid]
    payload = qr_payloads[-1]
    text = json.dumps(notice, default=str)
    for secret in (payload["code"], payload["enroll_id"], base64.b64encode(result.qr_png).decode(), result.ha_user_id):
        assert secret not in text
    assert "Pixel" in notice["message"] and re.search(r"\d\d:\d\d", notice["message"])


async def test_the_notice_names_no_key_in_auto_mode(hass, qr_payloads, monkeypatch):
    _set_urls(hass, external=EXTERNAL)

    async def _mint(hass_, *, api_key, tailnet, tag):
        return MINTED

    monkeypatch.setattr(enrollment_flow, "_async_mint_tailscale_authkey", _mint)
    await call(hass, _entry(hass, tailscale_mode="auto", **CREDS))
    assert MINTED in json.dumps(qr_payloads[-1]) and MINTED not in json.dumps(notices(hass), default=str)


async def test_a_re_draw_replaces_the_notice_and_never_adds_one(hass, entry):
    first = await call(hass, entry, "Pixel")
    await call(hass, entry, "Pixel")
    assert list(notices(hass)) == [f"portico_enroll_{first.entry_id}"]
    await call(hass, entry, "Other")
    assert len(notices(hass)) == 2


async def test_the_notice_goes_when_the_phone_enrols(hass, entry, qr_payloads):
    kept = await call(hass, entry, "Kept")
    done = await call(hass, entry, "Pixel")
    assert len(notices(hass)) == 2
    assert (await redeem_outcome(hass, qr_payloads[-1])).ok
    assert list(notices(hass)) == [f"portico_enroll_{kept.entry_id}"]
    assert done.entry_id not in "".join(notices(hass))


async def test_the_notice_goes_when_the_entry_is_removed(hass, entry):
    assert await async_setup_component(hass, DOMAIN, {})
    result = await call(hass, entry, "Pixel")
    assert list(notices(hass)) == [f"portico_enroll_{result.entry_id}"]
    await hass.config_entries.async_remove(result.entry_id)
    await hass.async_block_till_done()
    assert not [n for n in notices(hass) if n.startswith("portico_enroll_")]      # only the "phone removed" notice is left


async def test_a_failing_notice_does_not_fail_the_enrolment(hass, entry, monkeypatch):
    from homeassistant.components import persistent_notification

    def _boom(*args, **kwargs):
        raise RuntimeError("no notice")

    monkeypatch.setattr(persistent_notification, "async_create", _boom)
    assert isinstance(await call(hass, entry), Created)


# ===========================================================================================================
# The helpers are reached through enrollment_flow's namespace
# ===========================================================================================================


async def test_the_core_calls_its_helpers_through_the_flow_modules_namespace(hass, entry, monkeypatch):
    seen: list[str] = []
    real_make, real_build = enrollment_flow._make_qr, enrollment_flow._build_qr_data_uri

    def _make(text, *args):
        seen.append("make")
        return real_make(text, *args)

    def _build(payload, *args):
        seen.append("build")
        return real_build(payload, *args)

    async def _auto(hass_, ha_url):
        seen.append("auto_dial")
        return "10.1.1.1:443"

    def _fcm(hass_, domain):
        seen.append("fcm")
        return ({"v": 1, **FIREBASE}, True)

    def _target(url):
        seen.append("target")
        return "T"

    monkeypatch.setattr(enrollment_flow, "_make_qr", _make)
    monkeypatch.setattr(enrollment_flow, "_build_qr_data_uri", _build)
    monkeypatch.setattr(enrollment_flow, "_auto_ha_dial", _auto)
    monkeypatch.setattr(enrollment_flow, "_fcm_block", _fcm)
    monkeypatch.setattr(enrollment_flow, "_url_target", _target)
    result = await call(hass, entry)
    assert set(seen) == {"make", "build", "auto_dial", "fcm"}
    assert "fcm_skipped" in result.notes and result.dial_target == "10.1.1.1:443"

    async def _no_dial(hass_, ha_url):
        return None

    monkeypatch.setattr(enrollment_flow, "_auto_ha_dial", _no_dial)
    assert (await call(hass, entry, "two")).dial_target == "T" and "target" in seen

    monkeypatch.undo()
    monkeypatch.setattr(enrollment_flow, "_select_ha_url", lambda hass_, *, allow_http: (None, "no_ha_url"))
    assert await call(hass, entry, "next") == Error("no_ha_url")
    monkeypatch.undo()

    async def _not_complete(hass_, entry_, mode=None):
        return False

    monkeypatch.setattr(enrollment_flow, "_async_tailscale_answer_complete", _not_complete)
    assert await call(hass, entry, "later") == NeedsTailscaleSetup("manual")


# ===========================================================================================================
# EnrollRegistry.seconds_left
# ===========================================================================================================


def test_seconds_left_follows_the_registry_clock():
    clock = Clock()
    registry = EnrollRegistry(clock)
    assert registry.seconds_left("nobody") is None
    registry.register("e1", "code-1", "u1", "One")
    assert registry.seconds_left("e1") == 600
    clock.now += 125.5
    assert registry.seconds_left("e1") == 474.5
    assert registry.seconds_left("e2") is None
    registry.register("e1", "code-2", "u1", "One")                         # a re-draw starts the clock again
    assert registry.seconds_left("e1") == 600
    clock.now += 599.9
    assert 0 < registry.seconds_left("e1") < 0.2
    clock.now += 0.1
    assert registry.seconds_left("e1") is None                              # at the expiry: not live


def test_seconds_left_is_none_once_discarded_and_isolated_per_id():
    clock = Clock()
    registry = EnrollRegistry(clock)
    registry.register("e1", "c1", "u1", "One")
    registry.register("e2", "c2", "u2", "Two")
    clock.now += 100
    registry.discard("e1")
    assert registry.seconds_left("e1") is None and registry.seconds_left("e2") == 500


async def test_seconds_left_is_none_for_a_spent_code(hass, entry, qr_payloads):
    result = await call(hass, entry)
    enroll_id = qr_payloads[-1]["enroll_id"]
    registry = get_registry(hass)
    assert registry.seconds_left(enroll_id) is not None
    await redeem(hass, qr_payloads[-1])
    assert registry.seconds_left(enroll_id) is None
    assert result.created


async def test_seconds_left_is_none_for_a_burned_code(hass, entry, qr_payloads):
    await call(hass, entry)
    payload = qr_payloads[-1]
    registry = get_registry(hass)
    for _ in range(enroll_redeem.MAX_WRONG_CODES):
        await registry.async_redeem(hass, enroll_id=payload["enroll_id"], code="wrong", installation_id="i" * 12,
                                    nonce="n" * 12)
    assert registry.seconds_left(payload["enroll_id"]) is None


# ===========================================================================================================
# The dialog shows the new refusals
# ===========================================================================================================


async def test_the_dialog_refuses_the_sixth_phone_with_its_own_message(hass, entry):
    for i in range(5):
        assert isinstance(await call(hass, entry, f"p{i}"), Created)
    result = await _open_phone_form(hass, entry)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"phone_name": "p6"})
    assert (result["step_id"], result["errors"]) == ("phone_details", {"base": "pending_cap"})
    assert len(_portico_users(hass)) == 5
    strings = json.loads((Path(enrollment_flow.__file__).parent / "strings.json").read_text(encoding="utf-8"))
    assert "pending_cap" in strings["options"]["error"] and "bad_name" in strings["options"]["error"]


async def test_the_dialog_shows_a_bad_name_on_the_name_field(hass, entry):
    result = await _open_phone_form(hass, entry)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"phone_name": "z" * 41})
    assert (result["step_id"], result["errors"]) == ("phone_details", {"phone_name": "bad_name"})
    assert _portico_users(hass) == []


async def test_the_dialog_strips_the_name_and_raises_the_notice(hass, entry):
    result = await _open_phone_form(hass, entry)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"phone_name": "  Pixel  "})
    assert result["step_id"] == "show_qr" and result["description_placeholders"]["phone_name"] == "  Pixel  "
    (phone,) = phone_entries(hass)
    assert phone.data["name"] == "Pixel"
    assert list(notices(hass)) == [f"portico_enroll_{phone.entry_id}"]


async def test_a_phone_that_became_long_lived_while_the_key_was_minted_is_asked_about(hass, spies, monkeypatch):
    """The confirm check is made again after the mint: the phone may have changed during the network call."""
    _set_urls(hass, external=EXTERNAL)
    auto = _entry(hass, tailscale_mode="auto", **CREDS)
    first = await call(hass, auto, "Pixel")
    (phone,) = phone_entries(hass)

    async def _mint_and_change(hass_, **kwargs):
        phone_entry.async_update_phone(hass, phone, state="enrolled", token_kind="llat")
        return MINTED

    monkeypatch.setattr(enrollment_flow, "_async_mint_tailscale_authkey", _mint_and_change)
    users = len(_portico_users(hass))
    assert await call(hass, auto, "Pixel") == NeedsConfirmReenroll(first.entry_id, "Pixel")
    assert len(_portico_users(hass)) == users
