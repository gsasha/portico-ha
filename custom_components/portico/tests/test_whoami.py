"""DEV-798 (SEC-02): `POST /api/portico/whoami`, Home Assistant proves itself to an enrolled phone.

HA-harness session. Real http app, real `hass.auth` tokens, a phone enrolled through the real flow and redeemed through
the real view. The fixture `contract/whoami_v1.json` carries a test vector portico-app computes the same way."""
from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import pytest

from custom_components.portico import whoami
from custom_components.portico.whoami import WHOAMI_PATH, hint_of, proof_of

from .redeem_helpers import redeemed_token
from .test_enroll_redeem import _an_owner_exists, client, clock, draw  # noqa: F401 - fixtures by name
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
CONTRACT_PATH = ROOT / "contract" / "whoami_v1.json"
# The fixture's sha256. portico-app vendors the file byte for byte and pins the same value.
CONTRACT_SHA256 = "880ecdb4e76fa95ac868d5c2b3a144bf88994e99d6bd60efdce3cd70e0bfa107"

ERR = {"error": "invalid_or_expired"}
NONCE = "whoami-nonce-0123456789"


async def ask(client, body=None, *, raw=None, content_type="application/json"):
    data = raw if raw is not None else json.dumps(body)
    return await client.post(WHOAMI_PATH, data=data, headers={"Content-Type": content_type})


# ---- the contract ---------------------------------------------------------------------------------------------


def test_the_fixture_is_the_one_both_sides_pin():
    assert hashlib.sha256(CONTRACT_PATH.read_bytes()).hexdigest() == CONTRACT_SHA256


def test_the_vector_in_the_fixture_is_what_the_code_computes():
    vector = json.loads(CONTRACT_PATH.read_text())["vector"]
    assert hint_of(vector["refresh_token"]) == vector["hint"]
    assert proof_of(vector["refresh_token"], vector["nonce"], vector["hint"]) == vector["proof"]


def test_the_proof_depends_on_the_token_the_nonce_and_the_hint():
    base = proof_of("token-a" * 8, NONCE, "0123456789abcdef")
    assert base == hmac.new(b"token-a" * 8, b"portico-whoami-v1\0" + NONCE.encode() + b"\0" + b"0123456789abcdef",
                            hashlib.sha256).hexdigest()
    assert len(base) == 64 and base == base.lower()
    assert proof_of("token-b" * 8, NONCE, "0123456789abcdef") != base
    assert proof_of("token-a" * 8, NONCE + "x", "0123456789abcdef") != base
    assert proof_of("token-a" * 8, NONCE, "fedcba9876543210") != base


# ---- the endpoint ---------------------------------------------------------------------------------------------


async def test_the_view_needs_no_login_and_only_posts():
    assert whoami.WhoamiView.requires_auth is False
    assert not hasattr(whoami.WhoamiView, "get")


async def test_an_enrolled_phone_gets_the_proof_for_its_own_token(hass, qr_payloads, client):
    payload, entry = await draw(hass, qr_payloads)
    token, _access = await redeemed_token(hass, entry.data["ha_user_id"], payload)
    hint = hint_of(token.token)
    resp = await ask(client, {"nonce": NONCE, "hint": hint})
    assert resp.status == 200
    assert resp.headers["Cache-Control"] == "no-store"
    assert await resp.json() == {"proof": proof_of(token.token, NONCE, hint)}


async def test_the_proof_changes_with_every_nonce(hass, qr_payloads, client):
    payload, entry = await draw(hass, qr_payloads)
    token, _ = await redeemed_token(hass, entry.data["ha_user_id"], payload)
    hint = hint_of(token.token)
    first = await (await ask(client, {"nonce": NONCE, "hint": hint})).json()
    second = await (await ask(client, {"nonce": NONCE + "2", "hint": hint})).json()
    assert first != second


async def test_the_hint_of_one_phone_never_gets_the_proof_of_another(hass, qr_payloads, client):
    first, first_entry = await draw(hass, qr_payloads, name="Alice's Pixel")
    first_token, _ = await redeemed_token(hass, first_entry.data["ha_user_id"], first)
    second, second_entry = await draw(hass, qr_payloads, name="Dina's Pixel")
    second_token, _ = await redeemed_token(hass, second_entry.data["ha_user_id"], second)
    for token in (first_token, second_token):
        hint = hint_of(token.token)
        assert await (await ask(client, {"nonce": NONCE, "hint": hint})).json() == {
            "proof": proof_of(token.token, NONCE, hint)}


async def test_an_unknown_hint_is_the_uniform_refusal(hass, qr_payloads, client):
    payload, entry = await draw(hass, qr_payloads)
    await redeemed_token(hass, entry.data["ha_user_id"], payload)
    resp = await ask(client, {"nonce": NONCE, "hint": "0" * 16})
    assert (resp.status, await resp.json()) == (400, ERR)


async def test_a_removed_phone_has_no_proof_any_more(hass, qr_payloads, client):
    payload, entry = await draw(hass, qr_payloads)
    user_id = entry.data["ha_user_id"]
    token, _ = await redeemed_token(hass, user_id, payload)
    hint = hint_of(token.token)
    assert (await ask(client, {"nonce": NONCE, "hint": hint})).status == 200
    hass.auth.async_remove_refresh_token(token)
    resp = await ask(client, {"nonce": NONCE, "hint": hint})
    assert (resp.status, await resp.json()) == (400, ERR)


async def test_a_phone_that_never_redeemed_has_no_proof(hass, qr_payloads, client):
    await draw(hass, qr_payloads)
    resp = await ask(client, {"nonce": NONCE, "hint": hint_of("anything")})
    assert (resp.status, await resp.json()) == (400, ERR)


async def test_a_token_that_is_not_a_phones_gets_no_proof(hass, qr_payloads, client):
    """A refresh token of an ordinary HA user (not Portico's client id) must never be provable here."""
    await draw(hass, qr_payloads)
    user = await hass.auth.async_create_user("someone", group_ids=["system-users"])
    other = await hass.auth.async_create_refresh_token(user, client_id="https://example.invalid/app")
    resp = await ask(client, {"nonce": NONCE, "hint": hint_of(other.token)})
    assert (resp.status, await resp.json()) == (400, ERR)


@pytest.mark.parametrize("body", [
    {}, {"nonce": NONCE}, {"hint": "0" * 16}, {"nonce": NONCE, "hint": "0" * 16, "extra": 1},
    {"nonce": "short", "hint": "0" * 16}, {"nonce": "x" * 65, "hint": "0" * 16},
    {"nonce": NONCE, "hint": "0" * 15}, {"nonce": NONCE, "hint": "G" * 16}, {"nonce": NONCE, "hint": "A" * 16},
    {"nonce": 12345678901234567, "hint": "0" * 16}, {"nonce": NONCE, "hint": None}, [], "text", 7,
])
async def test_a_malformed_body_is_the_uniform_refusal(hass, client, body):
    resp = await ask(client, body)
    assert (resp.status, await resp.json()) == (400, ERR)


async def test_not_json_is_refused_and_a_big_body_is_not_read(hass, client):
    assert (await ask(client, raw="not json")).status == 400
    assert (await ask(client, raw=json.dumps({"nonce": "n" * 5000, "hint": "0" * 16}))).status == 400
    assert (await ask(client, {"nonce": NONCE, "hint": "0" * 16}, content_type="text/plain")).status == 415
    assert (await ask(client, {"nonce": NONCE, "hint": "0" * 16},
                      content_type="application/x-www-form-urlencoded")).status == 415


async def test_the_proof_is_the_only_thing_the_answer_holds(hass, qr_payloads, client):
    payload, entry = await draw(hass, qr_payloads)
    token, access = await redeemed_token(hass, entry.data["ha_user_id"], payload)
    resp = await ask(client, {"nonce": NONCE, "hint": hint_of(token.token)})
    text = await resp.text()
    assert set(json.loads(text)) == {"proof"}
    assert token.token not in text and access not in text
