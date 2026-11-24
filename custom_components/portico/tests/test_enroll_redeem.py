"""DEV-544: the one-time enrollment code and its redemption.

HA-harness session. The enrollment flow runs through HA's real options-flow manager (`tailscale_mode: manual`), the
redemption through HA's real http app (`hass_client_no_auth`), tokens through the real `hass.auth`, the refresh grant
through HA's real `/auth/token`, and the repair issues through the real issue registry. Time is the registry's
injectable monotonic clock, so an expiry is a number, not a sleep.

What is pinned here, in order: the QR (format 2, a code, no token anywhere), the redemption's happy path and what HA
accepts afterwards, the expiry boundary, single use (also under concurrent calls), the replay window and the
"someone else holds the QR" path, the limits and the lockout, the uniform refusal, constant-time comparison, the body
cap, revocation (a removed phone, a re-drawn QR, a restart, the 24-hour sweep), the phones enrolled before this change
(untouched until THAT phone is enrolled again), and that no secret reaches a log line, diagnostics or an issue.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
from datetime import timedelta
from pathlib import Path

import pytest
from homeassistant.auth.const import GROUP_ID_USER
from homeassistant.auth.models import TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN, TOKEN_TYPE_NORMAL
from homeassistant.components.auth import indieauth
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.portico import enroll_redeem, enrollment_flow
from custom_components.portico.config_flow import DOMAIN
from custom_components.portico.diagnostics import async_get_config_entry_diagnostics
from custom_components.portico.enroll_redeem import (
    ACCESS_TOKEN_LIFETIME,
    PORTICO_CLIENT_ID,
    REDEEM_PATH,
    get_registry,
)
from custom_components.portico.phone_entry import (
    PhoneRecord,
    async_create_phone_entry,
    async_update_phone,
    new_record,
    phone_entries,
)
from custom_components.portico.push import const as push_const

from .phone_helpers import enrolled
from .redeem_helpers import INSTALLATION, NONCE, redeem
from .test_enrollment_flow import (  # noqa: F401 - fixtures by name
    EXTERNAL,
    _device_entry,
    _enroll,
    _portico_users,
    _qr_capture,
    _set_urls,
    qr_payloads,
    qr_texts,
)
from .test_push_registry import _custom_integrations  # noqa: F401 - autouse fixture by name

ROOT = Path(__file__).resolve().parents[3]
CONTRACT_PATH = ROOT / "contract" / "enroll_v1.json"
# The fixture's sha256. portico-app vendors the file byte for byte and pins the same value: a change fails here
# first, and is a contract bump on both sides.
CONTRACT_SHA256 = "9cd7ed7ada9a3c7bae5ecd90ea5f603a0059c22b1b663fc717adb406729d4335"

ERR = {"error": "invalid_or_expired"}
OTHER_INSTALLATION = "inst-other-0987654321"
OTHER_NONCE = "nonce-other-fedcba9876"


class Clock:
    """The registry's monotonic clock, set by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def _an_owner_exists(hass_owner_user):
    """A real install has its owner before any phone; the first user HA makes is the owner, and Portico never
    removes an owner (phone_entry._made_by_portico)."""


@pytest.fixture
def clock(hass):
    fake = Clock()
    get_registry(hass).clock = fake
    return fake


@pytest.fixture
async def client(hass, clock, hass_client_no_auth):
    _set_urls(hass, external=EXTERNAL)
    assert await async_setup_component(hass, DOMAIN, {})
    return await hass_client_no_auth()


async def draw(hass, qr_payloads, name="Alice's Pixel"):
    """The enrollment flow up to the QR; returns (the QR payload, the phone's entry)."""
    _set_urls(hass, external=EXTERNAL)
    result = await _enroll(hass, _device_entry(hass), name=name)
    if result["step_id"] == "confirm_reenroll":
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"confirm": True})
    assert result["step_id"] == "show_qr", result
    await hass.async_block_till_done()
    payload = qr_payloads[-1]
    (entry,) = [e for e in phone_entries(hass) if e.data["enroll_id"] == payload["enroll_id"]]
    return payload, entry


def body_of(payload, **override):
    base = {"code": payload["code"], "enroll_id": payload["enroll_id"], "installation_id": INSTALLATION, "nonce": NONCE}
    return {**base, **override}


async def post(client, body=None, *, raw=None, headers=None):
    data = raw if raw is not None else json.dumps(body)
    return await client.post(REDEEM_PATH, data=data, headers={"Content-Type": "application/json", **(headers or {})})


async def refused(resp):
    """Assert a response is the uniform refusal."""
    assert (resp.status, await resp.json()) == (400, ERR)


def phone_state(hass, entry):
    return PhoneRecord.from_data(hass.config_entries.async_get_entry(entry.entry_id).data)


def issue(hass, kind, user_id):
    return ir.async_get(hass).async_get_issue(DOMAIN, f"{kind}_{user_id}")


async def legacy_phone(hass, name="Old Pixel", enroll_id="legacy-enroll-id-0123456789abcdef"):
    """A phone enrolled before DEV-544: a Portico user with a ten-year long-lived token and a phone entry that says
    `llat` (no `state` key at all, as in the entries of that time)."""
    assert await async_setup_component(hass, DOMAIN, {})
    user = await hass.auth.async_create_user(name=f"Portico — {name}", group_ids=[GROUP_ID_USER])
    llat = await hass.auth.async_create_refresh_token(
        user, client_name=f"portico-oldentry-{user.id}", token_type=TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN,
        access_token_expiration=timedelta(days=3650))
    access = hass.auth.async_create_access_token(llat)
    record = PhoneRecord(ha_user_id=user.id, name=name, enroll_id=enroll_id,
                         enrolled_at="2026-10-08T10:00:00+00:00", token_kind="llat", refresh_state="none")
    entry = await async_create_phone_entry(hass, record)
    old = {k: v for k, v in entry.data.items() if k not in ("state", "pending_since")}
    hass.config_entries.async_update_entry(entry, data=old)       # an entry of that time has neither key
    assert "state" not in entry.data
    return user, llat, access, entry


# ===========================================================================================================
# The QR
# ===========================================================================================================


async def test_the_qr_is_format_2_with_a_code_and_no_token(hass, qr_payloads, qr_texts):
    payload, _entry = await draw(hass, qr_payloads)
    assert payload["portico"] == 2 and type(payload["portico"]) is int
    assert set(payload) == {"portico", "ha_url", "code", "phone_name", "enroll_id", "ha_dial"}
    assert not [k for k in payload if "token" in k]
    assert "ha_token" not in qr_texts[-1]
    assert enrollment_flow.PAYLOAD_FORMAT == 2


async def test_the_code_has_at_least_128_bits_and_is_url_safe(hass, qr_payloads):
    codes = []
    for name in ("A", "B", "C"):
        payload, _ = await draw(hass, qr_payloads, name=name)
        codes.append(payload["code"])
    assert len(set(codes)) == 3
    for code in codes:
        assert re.fullmatch(r"[A-Za-z0-9_-]{22,128}", code)
        raw = base64.urlsafe_b64decode(code + "=" * (-len(code) % 4))
        assert len(raw) * 8 >= 128
    assert enroll_redeem.CODE_BYTES * 8 >= 128


async def test_a_new_enrollment_leaves_the_phone_with_no_token_at_all(hass, qr_payloads):
    payload, entry = await draw(hass, qr_payloads)
    user = await hass.auth.async_get_user(entry.data["ha_user_id"])
    assert user.refresh_tokens == {}
    assert [g.id for g in user.groups] == [GROUP_ID_USER] and not user.is_admin and not user.system_generated
    record = phone_state(hass, entry)
    assert (record.state, record.token_kind, record.refresh_state) == ("pending", "refresh", "pending")
    assert record.pending_since is not None


async def test_the_old_ten_year_token_is_not_made_any_more(hass, qr_payloads, monkeypatch):
    calls = []
    real = hass.auth.async_create_refresh_token

    async def spy(*args, **kwargs):
        calls.append(kwargs.get("token_type"))
        return await real(*args, **kwargs)

    monkeypatch.setattr(hass.auth, "async_create_refresh_token", spy)
    payload, _entry = await draw(hass, qr_payloads)
    assert calls == []
    assert "ha_token" not in payload
    assert not hasattr(enrollment_flow, "_LLAT_LIFESPAN")
    # and no entry setting brings it back
    assert "ha_token" not in json.dumps(dict(_device_entry(hass).data))


async def test_the_code_is_kept_only_as_a_keyed_digest(hass, qr_payloads, clock):
    payload, entry = await draw(hass, qr_payloads)
    registry = get_registry(hass)
    assert len(registry) == 1
    dump = repr(registry.__dict__) + repr(registry._records)
    for secret in (payload["code"], payload["enroll_id"]):
        assert secret not in dump
        assert secret.encode() not in dump.encode()
    # nor in anything HA persists about the phone
    assert payload["code"] not in json.dumps(dict(entry.data))
    assert payload["code"] not in json.dumps(dict(_device_entry(hass).data))
    assert payload["code"] not in json.dumps(dict(hass.config_entries.async_get_entry(entry.entry_id).options))


async def test_the_qr_page_says_once_and_ten_minutes_and_when(hass, qr_payloads):
    _set_urls(hass, external=EXTERNAL)
    result = await _enroll(hass, _device_entry(hass), name="Pixel")
    assert result["step_id"] == "show_qr"
    placeholders = result["description_placeholders"]
    assert re.fullmatch(r"\d\d:\d\d", placeholders["expires_at"])
    strings = json.loads((Path(enrollment_flow.__file__).parent / "strings.json").read_text(encoding="utf-8"))
    for step in ("show_qr", "show_qr_fcm_skipped"):
        text = strings["options"]["step"][step]["description"]
        assert "once, for 10 minutes" in text and "{expires_at}" in text and "ha_token" not in text
        assert text.format(**placeholders)
    assert enroll_redeem.CODE_LIFETIME == timedelta(minutes=10)


async def test_show_a_new_qr_for_a_pending_phone_keeps_the_user_and_kills_the_old_code(hass, qr_payloads, client):
    first, entry = await draw(hass, qr_payloads, name="Pixel")
    second, again = await draw(hass, qr_payloads, name="Pixel")
    assert again.entry_id == entry.entry_id and len(phone_entries(hass)) == 1
    assert len(_portico_users(hass)) == 1
    assert second["enroll_id"] == first["enroll_id"] and second["code"] != first["code"]
    assert len(get_registry(hass)) == 1
    await refused(await post(client, body_of(first)))
    resp = await post(client, body_of(second))
    assert resp.status == 200


async def test_a_new_qr_for_a_compromised_phone_makes_it_pending_again(hass, qr_payloads, client, clock):
    first, entry = await draw(hass, qr_payloads, name="Pixel")
    assert (await post(client, body_of(first))).status == 200
    await refused(await post(client, body_of(first, installation_id=OTHER_INSTALLATION)))
    assert phone_state(hass, entry).state == "compromised"
    second, again = await draw(hass, qr_payloads, name="Pixel")
    assert again.entry_id == entry.entry_id
    assert phone_state(hass, entry).state == "pending"
    assert (await post(client, body_of(second))).status == 200
    assert phone_state(hass, entry).state == "enrolled"


async def test_two_names_are_two_phones(hass, qr_payloads):
    _a, ea = await draw(hass, qr_payloads, name="A")
    _b, eb = await draw(hass, qr_payloads, name="B")
    assert ea.entry_id != eb.entry_id and len(_portico_users(hass)) == 2 and len(get_registry(hass)) == 2


async def test_an_enrolled_phone_on_the_new_scheme_is_not_reused_by_name(hass, qr_payloads, client):
    first, entry = await draw(hass, qr_payloads, name="Pixel")
    assert (await post(client, body_of(first))).status == 200
    second, other = await draw(hass, qr_payloads, name="Pixel")
    assert other.entry_id != entry.entry_id and second["enroll_id"] != first["enroll_id"]
    assert len(phone_entries(hass)) == 2


async def test_a_failure_while_drawing_a_new_phones_qr_leaves_no_user_no_entry_and_no_code(hass, monkeypatch):
    def boom(_payload):
        raise RuntimeError("the QR could not be built")

    monkeypatch.setattr(enrollment_flow, "_build_qr_data_uri", boom)
    _set_urls(hass, external=EXTERNAL)
    with pytest.raises(RuntimeError):
        await _enroll(hass, _device_entry(hass), name="Never made")
    assert _portico_users(hass) == [] and phone_entries(hass) == [] and len(get_registry(hass)) == 0


async def test_a_failure_while_drawing_again_keeps_the_phone_and_its_earlier_code(hass, qr_payloads, client, monkeypatch):
    first, entry = await draw(hass, qr_payloads, name="Pixel")

    def boom(_payload):
        raise RuntimeError("the QR could not be built")

    monkeypatch.setattr(enrollment_flow, "_build_qr_data_uri", boom)
    with pytest.raises(RuntimeError):
        await _enroll(hass, _device_entry(hass), name="Pixel")
    assert len(_portico_users(hass)) == 1 and len(phone_entries(hass)) == 1
    assert (await post(client, body_of(first))).status == 200            # the earlier QR still works


# ===========================================================================================================
# Redemption: the happy path, and what Home Assistant accepts afterwards
# ===========================================================================================================


async def test_redeem_returns_a_refresh_token_for_the_phones_user(hass, qr_payloads, client):
    payload, entry = await draw(hass, qr_payloads)
    resp = await post(client, body_of(payload))
    assert resp.status == 200
    assert resp.headers["Cache-Control"] == "no-store" and resp.headers["Pragma"] == "no-cache"
    body = await resp.json()
    assert set(body) == {"refresh_token", "access_token", "token_type", "expires_in", "client_id"}
    assert body["token_type"] == "Bearer" and body["expires_in"] == 86400 and body["client_id"] == PORTICO_CLIENT_ID

    user = await hass.auth.async_get_user(entry.data["ha_user_id"])
    (token,) = user.refresh_tokens.values()
    assert token.token == body["refresh_token"]
    assert token.token_type == TOKEN_TYPE_NORMAL and token.client_id == PORTICO_CLIENT_ID
    assert token.access_token_expiration == ACCESS_TOKEN_LIFETIME == timedelta(hours=24)
    assert token.client_name.startswith("portico-") and entry.entry_id in token.client_name
    assert hass.auth.async_validate_access_token(body["access_token"]) is token
    claims = json.loads(base64.urlsafe_b64decode(body["access_token"].split(".")[1] + "=="))
    assert claims["exp"] - claims["iat"] == 86400 and claims["iss"] == token.id


async def test_home_assistant_expires_an_unused_refresh_token_after_90_days(hass, qr_payloads, client):
    """Documented in the contract (`refresh_token_idle_expiry_days`): HA's own rule for a NORMAL refresh token, which
    Portico leaves as it is. If HA changes it, the contract text and the app's wording need to follow."""
    import time

    payload, entry = await draw(hass, qr_payloads)
    assert (await post(client, body_of(payload))).status == 200
    (token,) = (await hass.auth.async_get_user(entry.data["ha_user_id"])).refresh_tokens.values()
    assert token.expire_at is not None
    assert 89 * 86400 < token.expire_at - time.time() <= 90 * 86400 + 5


async def test_redeem_marks_the_phone_enrolled(hass, qr_payloads, client):
    payload, entry = await draw(hass, qr_payloads)
    assert (await post(client, body_of(payload))).status == 200
    record = phone_state(hass, entry)
    assert (record.state, record.token_kind, record.refresh_state) == ("enrolled", "refresh", "active")
    assert record.pending_since is None and record.token_expires_at is None
    assert [p["name"] for p in enrolled(hass)] == ["Alice's Pixel"]


async def test_the_client_id_passes_homes_own_check():
    assert indieauth.verify_client_id(PORTICO_CLIENT_ID)


async def test_home_assistants_auth_token_endpoint_accepts_the_refresh_grant(hass, qr_payloads, client):
    """The exact flow the app follows after redemption, against HA's real `/auth/token`."""
    assert await async_setup_component(hass, "auth", {})
    payload, entry = await draw(hass, qr_payloads)
    body = await (await post(client, body_of(payload))).json()
    form = {"grant_type": "refresh_token", "refresh_token": body["refresh_token"], "client_id": body["client_id"]}
    resp = await client.post("/auth/token", data=form)
    assert resp.status == 200
    refreshed = await resp.json()
    assert set(refreshed) == {"access_token", "token_type", "expires_in"} and refreshed["expires_in"] == 86400
    user = await hass.auth.async_get_user(entry.data["ha_user_id"])
    assert hass.auth.async_validate_access_token(refreshed["access_token"]).user is user
    # the client id is not optional: the token has one
    resp = await client.post("/auth/token", data={k: v for k, v in form.items() if k != "client_id"})
    assert resp.status == 400
    # a revoked refresh token is invalid_grant, which the app turns into "signed out"
    hass.auth.async_remove_refresh_token(next(iter(user.refresh_tokens.values())))
    resp = await client.post("/auth/token", data=form)
    assert (resp.status, (await resp.json())["error"]) == (400, "invalid_grant")


async def test_the_access_token_opens_a_websocket_and_portico_commands(hass, qr_payloads, client, hass_ws_client):
    payload, _entry = await draw(hass, qr_payloads)
    body = await (await post(client, body_of(payload))).json()
    ws = await hass_ws_client(hass, access_token=body["access_token"])
    await ws.send_json_auto_id({"type": "portico/version"})
    reply = await ws.receive_json()
    assert reply["success"], reply
    assert reply["result"]["contract"] == 3
    await ws.close()


async def test_a_phone_enrolled_before_this_change_still_authenticates_over_its_old_token(
        hass, qr_payloads, client, hass_ws_client):
    """The live phone's websocket auth path is unchanged, through every step of another phone's enrollment."""
    user, llat, access, _entry = await legacy_phone(hass)
    ws = await hass_ws_client(hass, access_token=access)
    await ws.send_json_auto_id({"type": "portico/version"})
    reply = await ws.receive_json()
    assert reply["success"], reply
    await ws.close()

    other, _ = await draw(hass, qr_payloads, name="Brand new")
    assert (await post(client, body_of(other))).status == 200
    assert hass.auth.async_validate_access_token(access) is llat
    assert llat.id in (await hass.auth.async_get_user(user.id)).refresh_tokens
    ws = await hass_ws_client(hass, access_token=access)
    await ws.send_json_auto_id({"type": "portico/version"})
    assert (await ws.receive_json())["success"]
    await ws.close()


async def test_the_unauthenticated_view_needs_no_login_and_only_posts(hass, client):
    assert enroll_redeem.EnrollRedeemView.requires_auth is False
    assert (await client.get(REDEEM_PATH)).status == 405
    assert (await client.put(REDEEM_PATH, data="{}")).status == 405


# ===========================================================================================================
# Expiry
# ===========================================================================================================


async def test_a_code_works_until_exactly_ten_minutes(hass, qr_payloads, client, clock):
    payload, _ = await draw(hass, qr_payloads)
    clock.advance(599.999)
    assert (await post(client, body_of(payload))).status == 200


async def test_a_code_does_not_work_at_ten_minutes(hass, qr_payloads, client, clock):
    payload, entry = await draw(hass, qr_payloads)
    clock.advance(600.0)
    await refused(await post(client, body_of(payload)))
    assert (await hass.auth.async_get_user(entry.data["ha_user_id"])).refresh_tokens == {}


async def test_a_code_long_expired_and_purged_is_the_same_refusal(hass, qr_payloads, client, clock):
    payload, _ = await draw(hass, qr_payloads)
    clock.advance(3600 * 5)
    await refused(await post(client, body_of(payload)))
    assert len(get_registry(hass)) == 0


# ===========================================================================================================
# Single use, the replay window, and "someone else holds the QR"
# ===========================================================================================================


async def test_a_code_cannot_be_used_twice_after_the_window(hass, qr_payloads, client, clock):
    payload, entry = await draw(hass, qr_payloads)
    first = await (await post(client, body_of(payload))).json()
    clock.advance(121)
    await refused(await post(client, body_of(payload)))
    user = await hass.auth.async_get_user(entry.data["ha_user_id"])
    assert [t.token for t in user.refresh_tokens.values()] == [first["refresh_token"]]   # still the first, intact


async def test_the_same_phone_retrying_inside_the_window_gets_the_same_refresh_token(hass, qr_payloads, client, clock):
    payload, entry = await draw(hass, qr_payloads)
    first = await (await post(client, body_of(payload))).json()
    clock.advance(119)
    resp = await post(client, body_of(payload))
    assert resp.status == 200
    second = await resp.json()
    assert second["refresh_token"] == first["refresh_token"]
    user = await hass.auth.async_get_user(entry.data["ha_user_id"])
    assert len(user.refresh_tokens) == 1
    assert hass.auth.async_validate_access_token(second["access_token"]) is not None


async def test_the_replay_window_closes_at_two_minutes(hass, qr_payloads, client, clock):
    payload, _ = await draw(hass, qr_payloads)
    assert (await post(client, body_of(payload))).status == 200
    clock.advance(120)
    await refused(await post(client, body_of(payload)))


@pytest.mark.parametrize("override", [
    {"installation_id": OTHER_INSTALLATION},
    {"nonce": OTHER_NONCE},
    {"installation_id": OTHER_INSTALLATION, "nonce": OTHER_NONCE},
])
async def test_another_holder_inside_the_window_revokes_the_token_and_flags_the_phone(
        hass, qr_payloads, client, clock, override):
    payload, entry = await draw(hass, qr_payloads, name="Hall tablet")
    first = await (await post(client, body_of(payload))).json()
    user_id = entry.data["ha_user_id"]
    assert hass.auth.async_validate_access_token(first["access_token"]) is not None

    await refused(await post(client, body_of(payload, **override)))

    assert hass.auth.async_validate_access_token(first["access_token"]) is None
    assert (await hass.auth.async_get_user(user_id)).refresh_tokens == {}
    record = phone_state(hass, entry)
    assert (record.state, record.refresh_state) == ("compromised", "none")
    found = issue(hass, "enroll_qr_reused", user_id)
    assert found is not None and found.translation_placeholders == {"phone": "Hall tablet"}
    # the real phone, trying again, is refused too (it must be given a new QR)
    await refused(await post(client, body_of(payload)))
    assert (await hass.auth.async_get_user(user_id)).refresh_tokens == {}


async def test_a_thief_who_redeems_first_loses_the_token_when_the_real_phone_arrives(hass, qr_payloads, client, clock):
    payload, entry = await draw(hass, qr_payloads)
    thief = await (await post(client, body_of(payload, installation_id=OTHER_INSTALLATION))).json()
    await refused(await post(client, body_of(payload)))                   # the real phone
    assert hass.auth.async_validate_access_token(thief["access_token"]) is None


async def test_a_new_qr_clears_the_reused_issue_once_redeemed(hass, qr_payloads, client, clock):
    payload, entry = await draw(hass, qr_payloads)
    assert (await post(client, body_of(payload))).status == 200
    await refused(await post(client, body_of(payload, nonce=OTHER_NONCE)))
    user_id = entry.data["ha_user_id"]
    assert issue(hass, "enroll_qr_reused", user_id) is not None
    fresh, _ = await draw(hass, qr_payloads)
    assert (await post(client, body_of(fresh))).status == 200
    assert issue(hass, "enroll_qr_reused", user_id) is None


async def test_ten_simultaneous_redemptions_make_exactly_one_refresh_token(hass, qr_payloads, clock, monkeypatch):
    """Single use under concurrency, at the registry (no HTTP): the token is minted once, however many race."""
    payload, entry = await draw(hass, qr_payloads)
    real = hass.auth.async_create_refresh_token
    created = []

    async def counting(*args, **kwargs):
        created.append(1)
        await asyncio.sleep(0)                      # let the others run while this one is mid-flight
        return await real(*args, **kwargs)

    monkeypatch.setattr(hass.auth, "async_create_refresh_token", counting)
    registry = get_registry(hass)
    outcomes = await asyncio.gather(*[
        registry.async_redeem(hass, enroll_id=payload["enroll_id"], code=payload["code"],
                              installation_id=f"inst-{i:02d}", nonce=f"nonce-{i:02d}-0123456789abcdef")
        for i in range(10)])
    assert len(created) == 1
    assert sum(1 for o in outcomes if o.ok) <= 1


async def test_two_simultaneous_calls_over_http_never_leave_two_live_tokens(hass, qr_payloads, client, clock):
    payload, entry = await draw(hass, qr_payloads)
    a, b = await asyncio.gather(
        post(client, body_of(payload, installation_id="inst-A-0123456789", nonce="nonce-A-0123456789abcdef")),
        post(client, body_of(payload, installation_id="inst-B-0123456789", nonce="nonce-B-0123456789abcdef")))
    assert sorted([a.status, b.status]) == [200, 400]
    user = await hass.auth.async_get_user(entry.data["ha_user_id"])
    # the second holder was a different phone: the safe outcome is that neither keeps a token
    assert user.refresh_tokens == {}
    assert phone_state(hass, entry).state == "compromised"


async def test_two_simultaneous_retries_from_the_same_phone_share_one_token(hass, qr_payloads, client, clock):
    payload, entry = await draw(hass, qr_payloads)
    a, b = await asyncio.gather(post(client, body_of(payload)), post(client, body_of(payload)))
    assert (a.status, b.status) == (200, 200)
    one, two = await a.json(), await b.json()
    assert one["refresh_token"] == two["refresh_token"]
    assert len((await hass.auth.async_get_user(entry.data["ha_user_id"])).refresh_tokens) == 1


async def test_a_failure_while_issuing_spends_the_code(hass, qr_payloads, client, clock, monkeypatch, caplog):
    payload, entry = await draw(hass, qr_payloads)

    async def boom(*args, **kwargs):
        raise ValueError("SECRET-DETAIL-" + payload["code"])

    real = hass.auth.async_create_refresh_token
    monkeypatch.setattr(hass.auth, "async_create_refresh_token", boom)
    await refused(await post(client, body_of(payload)))
    monkeypatch.setattr(hass.auth, "async_create_refresh_token", real)
    await refused(await post(client, body_of(payload)))                    # not usable again; a new QR is needed
    assert (await hass.auth.async_get_user(entry.data["ha_user_id"])).refresh_tokens == {}
    assert "SECRET-DETAIL" not in caplog.text and payload["code"] not in caplog.text
    assert phone_state(hass, entry).state == "pending"


# ===========================================================================================================
# The refusals: one answer for every cause
# ===========================================================================================================


def _causes(payload):
    return {
        "wrong_code": body_of(payload, code="W" * 32),
        "unknown_id": body_of(payload, enroll_id="u" * 43),
        "extra_key": {**body_of(payload), "extra": "x"},
        "missing_key": {k: v for k, v in body_of(payload).items() if k != "nonce"},
        "short_nonce": body_of(payload, nonce="short"),
        "bad_chars": body_of(payload, installation_id="bad id!"),
        "wrong_type": body_of(payload, nonce=12345678901234567890),
        "short_code": body_of(payload, code="abc"),
    }


@pytest.fixture
def no_lockout(monkeypatch):
    """Far more failures than the limits, so a test of the refusals is not answered by a lockout."""
    monkeypatch.setattr(enroll_redeem, "IP_FAILURES", 1000)
    monkeypatch.setattr(enroll_redeem, "GLOBAL_FAILURES", 1000)


async def test_every_refusal_is_byte_identical(hass, qr_payloads, client, clock, no_lockout):
    payload, _ = await draw(hass, qr_payloads)
    seen = {}
    for name, body in _causes(payload).items():
        seen[name] = await post(client, body)
    seen["not_json"] = await post(client, raw="not json{")
    seen["array"] = await post(client, raw="[1,2]")
    seen["empty"] = await post(client, raw="")
    seen["oversized"] = await post(client, raw=json.dumps({**body_of(payload), "pad": "x" * 600}))
    # expired, used (after the window) and burned are refusals too
    other, _ = await draw(hass, qr_payloads, name="Other")
    clock.advance(601)
    seen["expired"] = await post(client, body_of(other))
    reference = None
    for name, resp in seen.items():
        raw = await resp.read()
        shown = (resp.status, raw, resp.headers["Content-Type"], resp.headers["Cache-Control"])
        reference = reference or shown
        assert shown == reference, name
    assert json.loads(reference[1]) == ERR and reference[0] == 400


async def test_a_used_code_and_a_burned_code_are_the_same_refusal_as_an_unknown_one(
        hass, qr_payloads, client, clock, no_lockout):
    used, _ = await draw(hass, qr_payloads, name="Used")
    assert (await post(client, body_of(used))).status == 200
    clock.advance(121)                                  # the used code's replay window is over
    burned, _ = await draw(hass, qr_payloads, name="Burned")
    for _ in range(5):
        await refused(await post(client, body_of(burned, code="X" * 32)))
    unknown = await post(client, body_of(used, enroll_id="n" * 43))
    expected = (unknown.status, await unknown.read(), unknown.headers["Content-Type"])
    for resp in (await post(client, body_of(used)), await post(client, body_of(burned))):
        assert (resp.status, await resp.read(), resp.headers["Content-Type"]) == expected


async def test_five_wrong_codes_burn_the_record_even_for_the_right_code(hass, qr_payloads, client, clock):
    payload, entry = await draw(hass, qr_payloads, name="Hall tablet")
    for _ in range(enroll_redeem.MAX_WRONG_CODES):
        await refused(await post(client, body_of(payload, code="Z" * 32)))
    await refused(await post(client, body_of(payload)))                  # the RIGHT code, now burned
    assert (await hass.auth.async_get_user(entry.data["ha_user_id"])).refresh_tokens == {}
    found = issue(hass, "enroll_code_burned", entry.data["ha_user_id"])
    assert found is not None and found.translation_placeholders == {"phone": "Hall tablet"}
    # a new QR for the same phone works
    fresh, _ = await draw(hass, qr_payloads, name="Hall tablet")
    assert (await post(client, body_of(fresh))).status == 200


async def test_four_wrong_codes_leave_the_record_usable(hass, qr_payloads, client, clock):
    payload, _ = await draw(hass, qr_payloads)
    for _ in range(4):
        await refused(await post(client, body_of(payload, code="Z" * 32)))
    assert (await post(client, body_of(payload))).status == 200


async def test_wrong_codes_against_an_unknown_id_burn_nothing(hass, qr_payloads, client, clock):
    payload, _ = await draw(hass, qr_payloads)
    for _ in range(9):
        await refused(await post(client, body_of(payload, enroll_id="q" * 43, code="Z" * 32)))
    assert (await post(client, body_of(payload))).status == 200


# ===========================================================================================================
# Constant time, and no work before the body is checked
# ===========================================================================================================


class _SpyHmac:
    """`hmac` as the module sees it, counting `compare_digest` calls."""

    def __init__(self):
        self.compares = 0

    def compare_digest(self, a, b):
        self.compares += 1
        return hmac.compare_digest(a, b)

    new = staticmethod(hmac.new)


async def test_an_unknown_id_and_a_wrong_code_each_cost_one_code_compare(hass, qr_payloads, client, clock, monkeypatch):
    payload, _ = await draw(hass, qr_payloads)
    await draw(hass, qr_payloads, name="Second")
    spy = _SpyHmac()
    monkeypatch.setattr(enroll_redeem, "hmac", spy)
    records = len(get_registry(hass))
    await post(client, body_of(payload, enroll_id="u" * 43))                 # unknown id
    unknown = spy.compares
    spy.compares = 0
    await post(client, body_of(payload, code="W" * 32))                      # wrong code
    wrong = spy.compares
    spy.compares = 0
    await post(client, body_of(payload))                                     # the right one
    right = spy.compares
    # every stored id is compared (no early exit), then exactly one code compare, whatever the outcome
    assert unknown == wrong == right == records + 1


async def test_an_oversized_or_malformed_body_is_refused_before_any_hashing(hass, qr_payloads, client, clock, monkeypatch):
    payload, _ = await draw(hass, qr_payloads)
    digests = []
    real = enroll_redeem.EnrollRegistry._digest

    def counting(self, *args):
        digests.append(1)
        return real(self, *args)

    monkeypatch.setattr(enroll_redeem.EnrollRegistry, "_digest", counting)
    await refused(await post(client, raw=json.dumps({**body_of(payload), "pad": "x" * 600})))
    await refused(await post(client, raw="x" * 5000))
    await refused(await post(client, raw="{not json"))
    await refused(await post(client, body_of(payload, nonce="short")))

    async def chunks():                                   # no Content-Length: the cap is applied while reading
        for _ in range(40):
            yield b"x" * 100

    await refused(await client.post(REDEEM_PATH, data=chunks(), headers={"Content-Type": "application/json"}))
    assert digests == []
    assert enroll_redeem.MAX_BODY_BYTES == 640


async def test_the_cap_is_a_cap_on_bytes_not_on_what_the_fields_allow(hass, qr_payloads, client, clock):
    """A VALID body padded with whitespace is refused one byte past the cap, whether or not it announces its length,
    and accepted at exactly the cap. (Without the cap the padded body would parse and redeem.)"""
    payload, _ = await draw(hass, qr_payloads)
    raw = json.dumps(body_of(payload))
    assert len(raw) < enroll_redeem.MAX_BODY_BYTES
    over = raw + " " * (enroll_redeem.MAX_BODY_BYTES + 1 - len(raw))
    exact = raw + " " * (enroll_redeem.MAX_BODY_BYTES - len(raw))
    assert (len(over), len(exact)) == (enroll_redeem.MAX_BODY_BYTES + 1, enroll_redeem.MAX_BODY_BYTES)
    await refused(await post(client, raw=over))                              # with a Content-Length

    async def chunks():                                                       # without one
        for i in range(0, len(over), 100):
            yield over[i:i + 100].encode()

    await refused(await client.post(REDEEM_PATH, data=chunks(), headers={"Content-Type": "application/json"}))
    assert get_registry(hass)._records[0].state == "pending"                  # none of it spent the code
    assert (await post(client, raw=exact)).status == 200


def test_every_valid_body_fits_in_the_cap():
    """The longest body the field shapes allow is under the cap, so the cap never refuses a real phone."""
    longest = {"code": "c" * 128, "enroll_id": "e" * 128, "installation_id": "i" * 128, "nonce": "n" * 64}
    assert enroll_redeem.parse_body(json.dumps(longest, separators=(",", ":")).encode()) == longest
    assert len(json.dumps(longest).encode()) <= enroll_redeem.MAX_BODY_BYTES


# ===========================================================================================================
# Rate limiting: a throttle, never a wall
# ===========================================================================================================


def _wrong(payload, **over):
    """A well-formed redemption with a wrong code against an id nobody has (it burns nothing)."""
    return body_of(payload, enroll_id="u" * 43, code="Z" * 32, **over)


async def test_ten_failures_from_one_address_throttle_it_and_the_code_is_not_looked_at(
        hass, qr_payloads, client, clock):
    payload, _ = await draw(hass, qr_payloads)
    for _ in range(enroll_redeem.IP_FAILURES):
        await refused(await post(client, _wrong(payload)))
    throttled = await post(client, body_of(payload))                         # the RIGHT code, inside the throttle
    assert throttled.status == 429 and await throttled.json() == {"error": "rate_limited"}
    assert throttled.headers["Retry-After"] == "2"
    record = get_registry(hass)._records[0]
    assert (record.state, record.wrong) == ("pending", 0)                    # nothing was evaluated
    clock.advance(1.5)
    assert (await post(client, body_of(payload))).status == 429              # throttled requests are not counted
    clock.advance(0.6)
    assert (await post(client, body_of(payload))).status == 200              # no wall: two seconds later it gets in


async def test_a_flood_never_blocks_for_more_than_one_slot(hass, qr_payloads, client, clock):
    """Sustained junk keeps the scope hot, yet a request is admitted every two seconds, for as long as it lasts."""
    payload, _ = await draw(hass, qr_payloads)
    for _ in range(enroll_redeem.IP_FAILURES):
        await refused(await post(client, _wrong(payload)))
    for _ in range(5):
        clock.advance(2)
        await refused(await post(client, _wrong(payload)))                    # admitted (and refused) every 2 s
        assert (await post(client, _wrong(payload))).status == 429            # the next one in the same slot waits
    clock.advance(2)
    assert (await post(client, body_of(payload))).status == 200


async def test_nine_failures_do_not_throttle(hass, qr_payloads, client, clock):
    payload, _ = await draw(hass, qr_payloads)
    for _ in range(enroll_redeem.IP_FAILURES - 1):
        await refused(await post(client, _wrong(payload)))
    assert (await post(client, body_of(payload))).status == 200


async def test_failures_age_out_of_the_window(hass, qr_payloads, client, clock):
    payload, _ = await draw(hass, qr_payloads)
    for _ in range(enroll_redeem.IP_FAILURES - 1):
        await refused(await post(client, _wrong(payload)))
    clock.advance(60)
    await refused(await post(client, _wrong(payload)))                        # the 10th, but the others aged out
    assert (await post(client, body_of(payload))).status == 200


async def test_twenty_wrong_codes_throttle_every_caller_to_one_request_in_two_seconds(
        hass, qr_payloads, client, clock, monkeypatch):
    """The global rule, with the per-address rule out of the way (one test client has one address)."""
    monkeypatch.setattr(enroll_redeem, "IP_FAILURES", 1000)
    payload, _ = await draw(hass, qr_payloads)
    for _ in range(enroll_redeem.GLOBAL_FAILURES):
        await refused(await post(client, _wrong(payload)))
    throttled = await post(client, body_of(payload))
    assert (throttled.status, throttled.headers["Retry-After"]) == (429, "2")
    clock.advance(2)
    assert (await post(client, body_of(payload))).status == 200                # a throttle, not a 60-second wall


async def test_malformed_bodies_never_count_toward_the_global_window(hass, qr_payloads, client, clock, monkeypatch):
    """Junk costs an attacker nothing and is no guess at a code: it must not slow everyone else down."""
    monkeypatch.setattr(enroll_redeem, "IP_FAILURES", 1000)
    payload, _ = await draw(hass, qr_payloads)
    for _ in range(enroll_redeem.GLOBAL_FAILURES * 3):
        await refused(await post(client, raw="{}"))
        await refused(await post(client, raw=json.dumps({**body_of(payload), "pad": "x" * 700})))   # oversized
    assert len(get_registry(hass).limiter._global) == 0
    assert (await post(client, body_of(payload))).status == 200


async def test_malformed_bodies_do_count_toward_their_own_address(hass, qr_payloads, client, clock):
    payload, _ = await draw(hass, qr_payloads)
    for _ in range(enroll_redeem.IP_FAILURES):
        await refused(await post(client, raw="{}"))
    assert (await post(client, body_of(payload))).status == 429


async def test_a_wrong_content_type_is_a_415_that_nothing_counts(hass, qr_payloads, client, clock, monkeypatch):
    payload, _ = await draw(hass, qr_payloads)
    for ctype in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data; boundary=x", ""):
        for _ in range(enroll_redeem.GLOBAL_FAILURES):
            resp = await client.post(REDEEM_PATH, data=json.dumps(body_of(payload)),
                                     headers={"Content-Type": ctype} if ctype else {})
            assert resp.status == 415 and (await resp.json()) == {"error": "unsupported_media_type"}
    limiter = get_registry(hass).limiter
    assert len(limiter._global) == 0 and limiter._ips == {}
    # not even the code was looked at, and a correct request is untouched
    assert get_registry(hass)._records[0].wrong == 0
    assert (await post(client, body_of(payload))).status == 200
    # `application/json; charset=utf-8` is JSON
    other, _ = await draw(hass, qr_payloads, name="Other")
    resp = await client.post(REDEEM_PATH, data=json.dumps(body_of(other)),
                             headers={"Content-Type": "application/json; charset=utf-8"})
    assert resp.status == 200


async def test_a_forwarding_header_puts_the_request_in_the_shared_proxied_bucket(hass, qr_payloads, client, clock):
    """Neither a forged X-Forwarded-For nor a Forwarded header gives a request an address of its own."""
    payload, _ = await draw(hass, qr_payloads)
    # (HA's own middleware answers an X-Forwarded-For from an untrusted peer itself, before the view; X-Real-IP is
    # a forwarding header only this view knows about, and the unit test below covers the other two.)
    for i in range(enroll_redeem.IP_FAILURES):
        await refused(await post(client, _wrong(payload), headers={"X-Real-IP": f"203.0.113.{i}"}))
    limiter = get_registry(hass).limiter
    assert set(limiter._ips) == {enroll_redeem.PROXIED_KEY}                   # no per-claimed-address windows at all
    # a request with no header is a different bucket and is untouched
    assert (await post(client, body_of(payload))).status == 200
    # while any proxied request is throttled, whatever address it claims
    other, _ = await draw(hass, qr_payloads, name="Other")
    assert (await post(client, body_of(other), headers={"X-Real-IP": "198.51.100.9"})).status == 429


async def test_a_forged_address_cannot_lock_another_address_out(hass, qr_payloads, client, clock):
    """The attacker claims the victim's address in X-Forwarded-For; the victim (no header) is not affected."""
    payload, _ = await draw(hass, qr_payloads)
    for _ in range(enroll_redeem.IP_FAILURES * 2):
        await post(client, _wrong(payload), headers={"X-Real-IP": "127.0.0.1"})
        clock.advance(2)
    assert "127.0.0.1" not in get_registry(hass).limiter._ips
    assert (await post(client, body_of(payload))).status == 200


def _request_via(peer: str, **headers):
    """A request that arrived from `peer` carrying `headers`, after HA's forwarded middleware has rewritten
    `request.remote` from the X-Forwarded-For chain (as it does for a trusted proxy such as the subnet router)."""
    from unittest.mock import MagicMock

    from aiohttp.test_utils import make_mocked_request

    transport = MagicMock()
    transport.get_extra_info.return_value = (peer, 5555)
    request = make_mocked_request("POST", REDEEM_PATH, headers=headers, transport=transport)
    return request.clone(remote=headers.get("X-Forwarded-For", peer))


@pytest.mark.parametrize("header", ["X-Forwarded-For", "Forwarded", "X-Real-IP"])
def test_a_header_is_never_believed_for_the_peer_or_the_limiter_key(header):
    request = _request_via("10.6.0.3", **{header: "203.0.113.77"})
    assert request.remote == "203.0.113.77" if header == "X-Forwarded-For" else True
    assert enroll_redeem._peer(request) == "10.6.0.3"                          # what is kept as last_used_ip
    assert enroll_redeem._limiter_key(request, enroll_redeem._peer(request)) == enroll_redeem.PROXIED_KEY


def test_a_request_without_a_forwarding_header_is_keyed_by_its_peer():
    request = _request_via("192.168.1.20")
    assert enroll_redeem._limiter_key(request, enroll_redeem._peer(request)) == "192.168.1.20"


async def test_the_last_used_address_is_the_connected_peer(hass, qr_payloads, client, clock):
    payload, entry = await draw(hass, qr_payloads)
    assert (await post(client, body_of(payload))).status == 200
    (token,) = (await hass.auth.async_get_user(entry.data["ha_user_id"])).refresh_tokens.values()
    assert token.last_used_ip == "127.0.0.1"                                   # the test client's own socket


async def test_the_limiter_throttles_each_address_on_its_own_and_everyone_on_the_global_rule():
    now = Clock()
    limiter = enroll_redeem.FailureLimiter(now)
    for _ in range(enroll_redeem.IP_FAILURES):
        limiter.record_failure("10.0.0.1", count_global=False)
    assert limiter.retry_after("10.0.0.1") == 2
    assert limiter.retry_after("10.0.0.2") is None                             # another address is fine
    now.advance(2)
    assert limiter.retry_after("10.0.0.1") is None                             # one slot, then the next waits
    assert limiter.retry_after("10.0.0.1") == 2
    now.advance(61)
    assert limiter.retry_after("10.0.0.1") is None and limiter.retry_after("10.0.0.1") is None
    tripped = [limiter.record_failure(f"10.1.0.{i}", count_global=True) for i in range(enroll_redeem.GLOBAL_FAILURES)]
    assert tripped.count("global") == 1 and tripped[-1] == "global"
    assert limiter.retry_after("10.9.9.9") == 2
    now.advance(2)
    assert limiter.retry_after("10.9.9.9") is None
    now.advance(60)
    assert limiter.retry_after("10.9.9.9") is None and limiter.retry_after("10.9.9.9") is None


async def test_a_full_address_table_counts_new_addresses_in_the_shared_bucket():
    now = Clock()
    limiter = enroll_redeem.FailureLimiter(now)
    for i in range(enroll_redeem.MAX_TRACKED_ADDRESSES):
        limiter.record_failure(f"fd00::{i:x}", count_global=False)
    assert len(limiter._ips) == enroll_redeem.MAX_TRACKED_ADDRESSES
    for i in range(enroll_redeem.IP_FAILURES):                                  # new addresses, table full of live windows
        limiter.record_failure(f"fd01::{i:x}", count_global=False)
    assert len(limiter._ips) <= enroll_redeem.MAX_TRACKED_ADDRESSES + 1
    assert len(limiter._ips[enroll_redeem.PROXIED_KEY]) == enroll_redeem.IP_FAILURES   # counted, not dropped
    assert limiter.retry_after("fd01::ffff") is None                           # unknown address: not itself hot
    assert limiter.retry_after(enroll_redeem.PROXIED_KEY) == 2                 # the shared bucket is
    now.advance(120)
    limiter.record_failure("fd02::1", count_global=False)
    assert len(limiter._ips) <= 3                                              # old windows were pruned


async def test_tripping_the_throttle_raises_a_repair_issue_that_a_redemption_clears(hass, qr_payloads, client, clock):
    payload, _ = await draw(hass, qr_payloads)
    for _ in range(enroll_redeem.IP_FAILURES - 1):
        await refused(await post(client, _wrong(payload)))
    assert ir.async_get(hass).async_get_issue(DOMAIN, "enroll_endpoint_throttled") is None
    await refused(await post(client, _wrong(payload)))
    found = ir.async_get(hass).async_get_issue(DOMAIN, "enroll_endpoint_throttled")
    assert found is not None and found.translation_key == "enroll_endpoint_throttled" and not found.is_fixable
    assert found.translation_placeholders is None                              # names no phone, no address, no secret
    clock.advance(2)
    assert (await post(client, body_of(payload))).status == 200
    assert ir.async_get(hass).async_get_issue(DOMAIN, "enroll_endpoint_throttled") is None


async def test_the_throttle_issue_clears_after_an_hour_of_quiet(hass, qr_payloads, client, clock):
    payload, _ = await draw(hass, qr_payloads)
    for _ in range(enroll_redeem.IP_FAILURES):
        await refused(await post(client, _wrong(payload)))
    assert ir.async_get(hass).async_get_issue(DOMAIN, "enroll_endpoint_throttled") is not None
    clock.advance(enroll_redeem.THROTTLE_ISSUE_AGE - 1)
    await enroll_redeem.async_sweep_pending(hass)
    assert ir.async_get(hass).async_get_issue(DOMAIN, "enroll_endpoint_throttled") is not None
    clock.advance(1)
    await enroll_redeem.async_sweep_pending(hass)
    assert ir.async_get(hass).async_get_issue(DOMAIN, "enroll_endpoint_throttled") is None


async def test_the_global_trip_also_raises_the_issue(hass, qr_payloads, client, clock, monkeypatch):
    monkeypatch.setattr(enroll_redeem, "IP_FAILURES", 1000)
    payload, _ = await draw(hass, qr_payloads)
    for _ in range(enroll_redeem.GLOBAL_FAILURES):
        await refused(await post(client, _wrong(payload)))
    assert ir.async_get(hass).async_get_issue(DOMAIN, "enroll_endpoint_throttled") is not None


async def test_the_view_never_feeds_homes_ip_ban(hass, qr_payloads, client, clock, monkeypatch):
    from unittest.mock import AsyncMock

    from homeassistant.components.http import ban

    spy = AsyncMock()
    monkeypatch.setattr(ban, "process_wrong_login", spy)
    payload, _ = await draw(hass, qr_payloads)
    for _ in range(enroll_redeem.IP_FAILURES + 2):
        await post(client, body_of(payload, enroll_id="u" * 43))
    spy.assert_not_awaited()


# ===========================================================================================================
# Revocation, restarts, the sweep
# ===========================================================================================================


async def test_removing_a_redeemed_phone_revokes_its_tokens_and_its_push_row(hass, qr_payloads, client, clock, hass_storage):
    from custom_components.portico.push.registry import PushRegistry

    payload, entry = await draw(hass, qr_payloads, name="Revoke me")
    body = await (await post(client, body_of(payload))).json()
    user_id = entry.data["ha_user_id"]
    registry = PushRegistry(hass)
    await registry.async_load()
    await registry.async_register(
        ha_user_id=user_id, installation_id=INSTALLATION, kind="fid", registration="R", package=push_const.PORTICO_PACKAGE,
        fcm_project_id="p", app_version="1", name="Revoke me", enroll_id=payload["enroll_id"])
    await registry._async_save()
    assert len(registry.rows()) == 1

    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    assert hass.auth.async_validate_access_token(body["access_token"]) is None
    assert await hass.auth.async_get_user(user_id) is None
    reloaded = PushRegistry(hass)
    await reloaded.async_load()
    assert reloaded.rows() == []
    from homeassistant.components import persistent_notification

    notices = persistent_notification._async_get_or_create_notifications(hass)
    (notice,) = [n for k, n in notices.items() if k.startswith("portico_phone_removed_")]
    assert "Revoke me" in notice["message"] and "Machines" in notice["message"] and "does not delete" in notice["message"]


async def test_removing_a_pending_phone_kills_its_code_and_removes_its_user(hass, qr_payloads, client, clock):
    payload, entry = await draw(hass, qr_payloads)
    user_id = entry.data["ha_user_id"]
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    await refused(await post(client, body_of(payload)))
    assert await hass.auth.async_get_user(user_id) is None and len(get_registry(hass)) == 0


async def test_a_home_assistant_restart_drops_pending_codes_and_a_new_qr_works(hass, qr_payloads, client, clock):
    payload, entry = await draw(hass, qr_payloads, name="Pixel")
    hass.data.pop("portico_enroll_registry")                       # what a restart does: memory is gone
    get_registry(hass).clock = clock
    await refused(await post(client, body_of(payload)))
    again, entry2 = await draw(hass, qr_payloads, name="Pixel")
    assert entry2.entry_id == entry.entry_id and again["enroll_id"] == payload["enroll_id"]
    assert (await post(client, body_of(again))).status == 200


async def test_the_sweep_removes_a_phone_pending_for_more_than_24_hours(hass, qr_payloads, client, clock):
    payload, entry = await draw(hass, qr_payloads, name="Abandoned")
    user_id = entry.data["ha_user_id"]
    async_update_phone(hass, entry, pending_since=(dt_util.utcnow() - timedelta(hours=24, minutes=1)).isoformat())
    assert await enroll_redeem.async_sweep_pending(hass) == ["Abandoned"]
    await hass.async_block_till_done()
    assert phone_entries(hass) == [] and await hass.auth.async_get_user(user_id) is None
    found = issue(hass, "enroll_pending_expired", user_id)
    assert found is not None and found.translation_placeholders == {"phone": "Abandoned"}
    await refused(await post(client, body_of(payload)))


async def test_the_sweep_keeps_a_phone_pending_for_less_than_24_hours(hass, qr_payloads):
    _payload, entry = await draw(hass, qr_payloads)
    async_update_phone(hass, entry, pending_since=(dt_util.utcnow() - timedelta(hours=23, minutes=59)).isoformat())
    assert await enroll_redeem.async_sweep_pending(hass) == []
    assert len(phone_entries(hass)) == 1 and len(_portico_users(hass)) == 1


async def test_the_sweep_never_touches_an_enrolled_phone_however_old(hass, qr_payloads, client, clock):
    payload, entry = await draw(hass, qr_payloads)
    assert (await post(client, body_of(payload))).status == 200
    user, _llat, _access, _old = await legacy_phone(hass)
    far = dt_util.utcnow() + timedelta(days=400)
    assert await enroll_redeem.async_sweep_pending(hass, now=far) == []
    assert len(phone_entries(hass)) == 2


async def test_the_sweep_removes_a_pending_phones_push_row_with_it(hass, qr_payloads):
    from custom_components.portico.push.registry import PushRegistry

    payload, entry = await draw(hass, qr_payloads)
    registry = PushRegistry(hass)
    await registry.async_load()
    await registry.async_register(
        ha_user_id=entry.data["ha_user_id"], installation_id=INSTALLATION, kind="fid", registration="R",
        package=push_const.PORTICO_PACKAGE, fcm_project_id="p", app_version="1", name="x", enroll_id=payload["enroll_id"])
    await registry._async_save()
    # registered through a pasted string, still pending: it survives the first day...
    assert await enroll_redeem.async_sweep_pending(hass, now=dt_util.utcnow() + timedelta(hours=23)) == []
    assert len(phone_entries(hass)) == 1
    # ...and goes, row and all, after it
    assert await enroll_redeem.async_sweep_pending(hass, now=dt_util.utcnow() + timedelta(hours=25)) == ["Alice's Pixel"]
    await hass.async_block_till_done()
    reloaded = PushRegistry(hass)
    await reloaded.async_load()
    assert reloaded.rows() == []


async def test_the_sweep_runs_on_a_timer(hass, qr_payloads, client):
    _payload, entry = await draw(hass, qr_payloads)
    async_update_phone(hass, entry, pending_since=(dt_util.utcnow() - timedelta(hours=30)).isoformat())
    async_fire_time_changed(hass, dt_util.utcnow() + enroll_redeem.SWEEP_INTERVAL + timedelta(seconds=1))
    await hass.async_block_till_done()
    assert phone_entries(hass) == []


async def test_a_pending_date_that_cannot_be_read_is_left_alone(hass, qr_payloads):
    _payload, entry = await draw(hass, qr_payloads)
    async_update_phone(hass, entry, pending_since="not a date", enrolled_at="also not")
    assert await enroll_redeem.async_sweep_pending(hass, now=dt_util.utcnow() + timedelta(days=9)) == []


# ===========================================================================================================
# Phones enrolled before this change
# ===========================================================================================================


async def test_enrolling_a_legacy_phone_again_keeps_it_working_until_the_new_qr_is_redeemed(
        hass, qr_payloads, client, clock):
    user, llat, access, entry = await legacy_phone(hass, name="Old Pixel")
    payload, again = await draw(hass, qr_payloads, name="Old Pixel")
    assert again.entry_id == entry.entry_id and len(phone_entries(hass)) == 1
    assert payload["enroll_id"] == "legacy-enroll-id-0123456789abcdef"
    assert len(_portico_users(hass)) == 1
    # drawing did nothing to the phone
    record = phone_state(hass, entry)
    assert (record.state, record.token_kind) == ("enrolled", "llat")
    assert hass.auth.async_validate_access_token(access) is llat

    body = await (await post(client, body_of(payload))).json()
    assert hass.auth.async_validate_access_token(access) is None                       # the old token is revoked
    user = await hass.auth.async_get_user(user.id)
    (token,) = user.refresh_tokens.values()
    assert token.token == body["refresh_token"] and token.token_type == TOKEN_TYPE_NORMAL
    record = phone_state(hass, entry)
    assert (record.state, record.token_kind, record.refresh_state) == ("enrolled", "refresh", "active")


async def test_an_unredeemed_legacy_re_enrollment_changes_nothing(hass, qr_payloads, client, clock):
    user, llat, access, entry = await legacy_phone(hass)
    payload, _ = await draw(hass, qr_payloads, name="Old Pixel")
    clock.advance(601)
    await refused(await post(client, body_of(payload)))
    assert hass.auth.async_validate_access_token(access) is llat
    assert await enroll_redeem.async_sweep_pending(hass, now=dt_util.utcnow() + timedelta(days=9)) == []
    assert phone_state(hass, entry).token_kind == "llat"


async def test_re_enrolling_removes_only_the_tokens_enrollment_made(hass, qr_payloads, client, clock):
    user, llat, access, entry = await legacy_phone(hass)
    by_hand = await hass.auth.async_create_refresh_token(
        user, client_name="made by an administrator", token_type=TOKEN_TYPE_LONG_LIVED_ACCESS_TOKEN,
        access_token_expiration=timedelta(days=30))
    payload, _ = await draw(hass, qr_payloads, name="Old Pixel")
    assert (await post(client, body_of(payload))).status == 200
    live = (await hass.auth.async_get_user(user.id)).refresh_tokens
    assert llat.id not in live and by_hand.id in live


async def test_another_phones_redemption_leaves_a_legacy_phone_alone(hass, qr_payloads, client, clock):
    user, llat, access, entry = await legacy_phone(hass)
    payload, _ = await draw(hass, qr_payloads, name="Somebody else")
    assert (await post(client, body_of(payload))).status == 200
    assert hass.auth.async_validate_access_token(access) is llat


async def test_a_legacy_row_without_an_enroll_id_is_not_reused(hass, qr_payloads):
    """Nothing to keep its identity by: a new phone is made, the old one stays as it was."""
    assert await async_setup_component(hass, DOMAIN, {})
    user = await hass.auth.async_create_user(name="Portico — Ancient", group_ids=[GROUP_ID_USER])
    entry = await async_create_phone_entry(hass, PhoneRecord(ha_user_id=user.id, name="Ancient"))
    payload, other = await draw(hass, qr_payloads, name="Ancient")
    assert other.entry_id != entry.entry_id and len(phone_entries(hass)) == 2


async def test_the_enrollment_flow_asks_before_replacing_an_enrolled_phone(hass, qr_payloads, client, clock):
    user, llat, access, entry = await legacy_phone(hass, name="Old Pixel")
    _set_urls(hass, external=EXTERNAL)
    result = await _enroll(hass, _device_entry(hass), name="Old Pixel")
    assert result["step_id"] == "confirm_reenroll", result
    placeholders = result["description_placeholders"]
    assert placeholders["phone_name"] == "Old Pixel" and placeholders["enrolled_on"] == "2026-10-08"
    assert placeholders["last_seen"]
    strings = json.loads((Path(enrollment_flow.__file__).parent / "strings.json").read_text(encoding="utf-8"))
    text = strings["options"]["step"]["confirm_reenroll"]["description"]
    assert "stops working" in text and text.format(**placeholders)
    assert qr_payloads == [] and len(get_registry(hass)) == 0                    # nothing was made yet
    # declining goes back to the name, and still makes nothing
    back = await hass.config_entries.options.async_configure(result["flow_id"], {"confirm": False})
    assert back["step_id"] == "phone_details" and qr_payloads == [] and len(get_registry(hass)) == 0
    # confirming makes the QR for that phone
    result = await _enroll(hass, _device_entry(hass), name="Old Pixel")
    done = await hass.config_entries.options.async_configure(result["flow_id"], {"confirm": True})
    assert done["step_id"] == "show_qr" and qr_payloads[-1]["enroll_id"] == entry.data["enroll_id"]
    assert hass.auth.async_validate_access_token(access) is llat


async def test_the_confirm_step_says_unknown_when_the_away_ring_is_not_loaded(hass, qr_payloads):
    await legacy_phone(hass, name="Old Pixel")
    _set_urls(hass, external=EXTERNAL)
    result = await _enroll(hass, _device_entry(hass), name="Old Pixel")
    assert result["step_id"] == "confirm_reenroll"
    assert result["description_placeholders"]["last_seen"] == "never or unknown"


async def test_a_pending_phone_is_renewed_without_asking(hass, qr_payloads):
    _first, _entry = await draw(hass, qr_payloads, name="Pixel")
    _set_urls(hass, external=EXTERNAL)
    result = await _enroll(hass, _device_entry(hass), name="Pixel")
    assert result["step_id"] == "show_qr"                                          # nothing to revoke: no question


async def test_the_sweep_repairs_a_pending_phone_that_already_holds_a_token(hass, qr_payloads, client, clock):
    """A crash between the auth store's save and the entry's: the token survived, the entry still says pending."""
    payload, entry = await draw(hass, qr_payloads, name="Crashed")
    body = await (await post(client, body_of(payload))).json()
    async_update_phone(hass, entry, state="pending", token_kind="refresh", refresh_state="pending",
                       pending_since=(dt_util.utcnow() - timedelta(hours=30)).isoformat())
    assert await enroll_redeem.async_sweep_pending(hass) == []                      # old enough to remove, and kept
    record = phone_state(hass, entry)
    assert (record.state, record.refresh_state, record.pending_since) == ("enrolled", "active", None)
    assert hass.auth.async_validate_access_token(body["access_token"]) is not None   # the phone still works
    assert len(phone_entries(hass)) == 1


async def test_the_sweep_still_removes_a_pending_phone_whose_user_holds_only_an_old_token(hass, qr_payloads):
    """A ten-year token (a legacy phone being enrolled again) is not a redemption."""
    user, llat, access, entry = await legacy_phone(hass, name="Old Pixel")
    async_update_phone(hass, entry, state="pending", pending_since=(dt_util.utcnow() - timedelta(hours=30)).isoformat())
    assert await enroll_redeem.async_sweep_pending(hass) == ["Old Pixel"]


# ===========================================================================================================
# Nothing secret anywhere it should not be
# ===========================================================================================================


_NOT_HA_LOGS = ("pytest_homeassistant_custom_component", "aiohttp.access")


def _log_text(caplog) -> str:
    return "\n".join(r.getMessage() for r in caplog.records if not r.name.startswith(_NOT_HA_LOGS))


async def test_no_secret_reaches_any_log_line(hass, qr_payloads, client, clock, caplog):
    caplog.set_level(logging.DEBUG)
    payload, entry = await draw(hass, qr_payloads, name="Quiet")
    secrets = [payload["code"], payload["enroll_id"], INSTALLATION, NONCE, OTHER_INSTALLATION, OTHER_NONCE]
    # every outcome the endpoint has: refused, the real redemption, the lost-response replay, the second holder,
    # the burn, a lockout, a malformed body
    await post(client, body_of(payload, code="W" * 32))
    first = await (await post(client, body_of(payload))).json()
    again = await (await post(client, body_of(payload))).json()
    await post(client, body_of(payload, installation_id=OTHER_INSTALLATION, nonce=OTHER_NONCE))
    other, _ = await draw(hass, qr_payloads, name="Quiet2")
    secrets += [other["code"], other["enroll_id"]]
    for _ in range(6):
        await post(client, body_of(other, code="B" * 32))
    for _ in range(enroll_redeem.IP_FAILURES):
        await post(client, raw="{bad")
    await post(client, body_of(other))
    secrets += [first["refresh_token"], first["access_token"], again["access_token"], "W" * 32, "B" * 32]
    text = _log_text(caplog)
    assert "enrollment redemption" in text                       # the outcomes were logged...
    for secret in secrets:
        assert secret not in text, secret[:6]                     # ...without any of them
    # what a log line may carry about an enroll id: four hex characters of its hash
    assert hashlib.sha256(payload["enroll_id"].encode()).hexdigest()[:4] in text


async def test_diagnostics_and_issues_carry_no_secret(hass, qr_payloads, client, clock):
    payload, entry = await draw(hass, qr_payloads, name="Diag")
    first = await (await post(client, body_of(payload))).json()
    await post(client, body_of(payload, nonce=OTHER_NONCE))               # raises the reuse issue
    registry_dump = json.dumps([str(i) for i in ir.async_get(hass).issues.values()])
    assert "enroll_qr_reused" in registry_dump
    fresh, _ = await draw(hass, qr_payloads, name="Diag")
    second = await (await post(client, body_of(fresh))).json()
    secrets = [payload["code"], fresh["code"], payload["enroll_id"], first["refresh_token"], first["access_token"],
               second["refresh_token"], second["access_token"], NONCE, OTHER_NONCE]
    dumps = []
    for e in hass.config_entries.async_entries(DOMAIN):
        dumps.append(json.dumps(await async_get_config_entry_diagnostics(hass, e), default=str))
        dumps.append(json.dumps(dict(e.data), default=str) if e.data.get("entry_type") != "phone" else "")
        dumps.append(json.dumps(dict(e.options), default=str))
    for secret in secrets:
        for dump in dumps:
            assert secret not in dump, secret[:6]
        assert secret not in registry_dump, secret[:6]
    # the phone's diagnostics redact the enroll id
    phone = next(e for e in phone_entries(hass))
    assert (await async_get_config_entry_diagnostics(hass, phone))["entry"]["data"]["enroll_id"] == "**REDACTED**"


# ===========================================================================================================
# Texts
# ===========================================================================================================


def test_the_repair_issues_have_text_and_name_the_phone():
    strings = json.loads((Path(enrollment_flow.__file__).parent / "strings.json").read_text(encoding="utf-8"))
    for kind in (enroll_redeem.ISSUE_QR_REUSED, enroll_redeem.ISSUE_CODE_BURNED, enroll_redeem.ISSUE_PENDING_EXPIRED):
        assert "{phone}" in strings["issues"][kind]["title"] and strings["issues"][kind]["description"]
        assert "{phone}" in strings["issues"][kind]["description"]
        for text in (strings["issues"][kind]["title"], strings["issues"][kind]["description"]):
            assert set(re.findall(r"\{(\w+)\}", text)) <= {"phone"}


def test_the_translation_matches_strings_json():
    base = Path(enrollment_flow.__file__).parent
    assert json.loads((base / "translations" / "en.json").read_text(encoding="utf-8")) == \
        json.loads((base / "strings.json").read_text(encoding="utf-8"))


# ===========================================================================================================
# The contract fixture
# ===========================================================================================================


def test_the_contract_file_is_pinned():
    assert hashlib.sha256(CONTRACT_PATH.read_bytes()).hexdigest() == CONTRACT_SHA256, (
        "contract/enroll_v1.json changed: bump its version, update CONTRACT_SHA256 and re-vendor it in portico-app")


def test_the_contract_states_what_the_server_does():
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert contract["contract"] == "enroll/v1"
    assert contract["qr"]["format"] == enrollment_flow.PAYLOAD_FORMAT and contract["qr"]["removed"] == ["ha_token"]
    assert contract["qr"]["ttl_s"] == enroll_redeem.CODE_LIFETIME.total_seconds()
    redeem_spec = contract["redeem"]
    assert redeem_spec["path"] == REDEEM_PATH and redeem_spec["max_body_bytes"] == enroll_redeem.MAX_BODY_BYTES
    assert redeem_spec["success"]["body"]["client_id"] == PORTICO_CLIENT_ID
    assert redeem_spec["success"]["body"]["expires_in"] == ACCESS_TOKEN_LIFETIME.total_seconds()
    assert redeem_spec["retry"]["window_s"] == enroll_redeem.REPLAY_WINDOW.total_seconds()
    assert redeem_spec["limits"] == {
        "wrong_codes_per_qr": enroll_redeem.MAX_WRONG_CODES, "address_failures": enroll_redeem.IP_FAILURES,
        "global_failures": enroll_redeem.GLOBAL_FAILURES, "window_s": enroll_redeem.WINDOW_SECONDS,
        "throttle_s": enroll_redeem.THROTTLE_SECONDS}
    assert redeem_spec["errors"]["rate_limited"]["headers"]["Retry-After"] == "2"
    assert redeem_spec["errors"]["unsupported_media_type"]["status"] == 415
    assert redeem_spec["errors"]["invalid_or_expired"]["body"] == ERR
    assert contract["refresh"]["form"]["client_id"] == PORTICO_CLIENT_ID
    assert contract["refresh"]["refresh_token_idle_expiry_days"] == 90
    for key, field_pattern in redeem_spec["request"].items():
        assert key in enroll_redeem._FIELDS and field_pattern.split(" ")[0] == "^" + enroll_redeem._FIELDS[key].pattern + "$"
    values = contract["fixture_values"]
    for key in ("code", "enroll_id", "installation_id", "nonce", "other_installation_id", "other_nonce"):
        assert enroll_redeem._FIELDS.get(key if key in enroll_redeem._FIELDS else key.split("_", 1)[1]).fullmatch(values[key])


def _case_body(contract, case):
    values = contract["fixture_values"]
    return {k: (values[v[1:]] if isinstance(v, str) and v.startswith("$") else v) for k, v in case["send"].items()}


async def _pending_for_case(hass, contract):
    values = contract["fixture_values"]
    assert await async_setup_component(hass, DOMAIN, {})
    user = await hass.auth.async_create_user(name="Portico — Fixture", group_ids=[GROUP_ID_USER])
    entry = await async_create_phone_entry(hass, new_record(
        ha_user_id=user.id, name="Fixture", enroll_id=values["enroll_id"], tailscale_mode="manual"))
    get_registry(hass).register(values["enroll_id"], values["code"], user.id, "Fixture")
    return user, entry


@pytest.mark.parametrize("index", range(12))
async def test_every_contract_case_replays_against_the_real_view(hass, client, clock, index):
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    case = contract["cases"][index]
    values = contract["fixture_values"]
    user, entry = await _pending_for_case(hass, contract)
    registry = get_registry(hass)
    first_token = None

    def mine(**over):
        return {"code": values["code"], "enroll_id": values["enroll_id"], "installation_id": values["installation_id"],
                "nonce": values["nonce"], **over}

    given = case["given"]
    if given == "nothing":
        hass.data.pop("portico_enroll_registry")
        get_registry(hass).clock = clock
    elif given == "expired":
        clock.advance(600)
    elif given in ("redeemed_by_this_phone", "redeemed_long_ago"):
        first_token = (await (await post(client, mine())).json())["refresh_token"]
        if given == "redeemed_long_ago":
            clock.advance(121)
    elif given == "redeemed_by_another_phone":
        first_token = (await (await post(client, mine(installation_id=values["other_installation_id"],
                                                     nonce=values["other_nonce"]))).json())["refresh_token"]
    elif given == "throttled":
        for _ in range(enroll_redeem.IP_FAILURES):
            await post(client, raw="{}")
    else:
        assert given == "pending", given

    if "content_type" in case:
        resp = await client.post(REDEEM_PATH, data=json.dumps(_case_body(contract, case)),
                                 headers={"Content-Type": case["content_type"]})
    else:
        resp = await post(client, _case_body(contract, case))
    expect = case["expect"]
    assert resp.status == expect["status"], case["name"]
    body = await resp.json()
    if "body" in expect:
        assert body == expect["body"], case["name"]
    if "body_keys" in expect:
        assert sorted(body) == expect["body_keys"], case["name"]
    if expect.get("same_refresh_token"):
        assert body["refresh_token"] == first_token
    if expect.get("token_revoked"):
        assert all(t.token != first_token for t in (await hass.auth.async_get_user(user.id)).refresh_tokens.values())
    if "retry_after" in expect:
        assert int(resp.headers["Retry-After"]) == expect["retry_after"]
    if expect.get("code_not_looked_at"):
        assert registry._records[0].state == "pending"
