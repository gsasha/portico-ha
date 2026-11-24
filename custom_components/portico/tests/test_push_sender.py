"""OAuth (AccessTokenSource) and the FCM v1 client (FcmClient), against the
local fake Google (fake_google.py): sender design §2.4, §8.1.

HA-harness session (it needs a real `hass` for the shared aiohttp session and
async_call_later). Nothing leaves 127.0.0.1: the endpoints are the fake's.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import timedelta

import jwt
import pytest
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.portico.push import const, payload
from custom_components.portico.push.credentials import parse_service_account
from custom_components.portico.push.fcm import FcmClient, Outcome
from custom_components.portico.push.oauth import AccessTokenSource, OAuthError

from .fake_google import KEY_ID, SCOPE, FakeGoogle, fcm_error, service_account_info

REG = "fid-REGISTRATION-VALUE-0123456789abcdef"


@pytest.fixture
async def google(monkeypatch, socket_enabled):
    """`socket_enabled` lifts the harness's socket block for this test so the
    client can reach the fake on 127.0.0.1 (the harness still restricts
    connects to 127.0.0.1, and the test container runs with --network none)."""
    fake = await FakeGoogle().start()
    monkeypatch.setattr(const, "ENDPOINTS", const.Endpoints(token_uri=fake.token_uri, fcm_base_url=fake.base))
    yield fake
    await fake.close()


def _account(google):
    return parse_service_account(json.dumps(service_account_info(google.token_uri)))


def _tokens(hass, google, **kw) -> AccessTokenSource:
    return AccessTokenSource(hass, _account(google), token_uri=google.token_uri, **kw)


def _client(hass, google, tokens, **kw) -> FcmClient:
    return FcmClient(hass, tokens, "portico-test", base_url=google.base, **kw)


def _message(kind="fid", registration=REG):
    data = payload.build_ring("entry-1", "call-1", 1_759_700_000_000, 45)
    return payload.build_message(kind=kind, registration=registration, package=const.PORTICO_PACKAGE,
                                 data=data, now_ms=1_759_700_000_000, ring_window_s=45)


# --------------------------------------------------------------------------
# OAuth
# --------------------------------------------------------------------------


async def test_jwt_claims_are_what_google_requires(hass, google):
    tokens = _tokens(hass, google)
    token = await tokens.async_get_token()
    assert token == google.issued[0]
    assert google.jwt_errors == []
    assertion = google.token_requests[0]["assertion"]
    assert google.token_requests[0]["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
    header = jwt.get_unverified_header(assertion)
    claims = jwt.decode(assertion, options={"verify_signature": False})
    assert header["alg"] == "RS256" and header["kid"] == KEY_ID
    assert claims["iss"] == "fcm-sender@portico-test.iam.example.invalid"
    assert claims["scope"] == SCOPE
    assert claims["aud"] == google.token_uri
    assert claims["exp"] - claims["iat"] == 3600
    tokens.async_stop()


async def test_token_is_fetched_once_and_cached(hass, google):
    tokens = _tokens(hass, google)
    first = await tokens.async_get_token()
    for _ in range(5):
        assert await tokens.async_get_token() == first
    assert len(google.token_requests) == 1
    tokens.async_stop()


async def test_single_flight_under_20_concurrent_callers(hass, google):
    google.token_delay = 0.05
    tokens = _tokens(hass, google)
    results = await asyncio.gather(*(tokens.async_get_token() for _ in range(20)))
    assert len(set(results)) == 1
    assert len(google.token_requests) == 1
    tokens.async_stop()


async def test_token_is_refetched_inside_the_300s_margin(hass, google):
    now = [1000.0]
    tokens = _tokens(hass, google, clock=lambda: now[0])
    await tokens.async_get_token()
    now[0] += 3600 - 301
    await tokens.async_get_token()
    assert len(google.token_requests) == 1
    now[0] += 2
    await tokens.async_get_token()
    assert len(google.token_requests) == 2
    tokens.async_stop()


async def test_background_refresh_before_expiry(hass, google):
    """A ring never waits for an OAuth round trip: a timer refreshes the token
    before the cache would consider it stale."""
    tokens = _tokens(hass, google)
    await tokens.async_start()
    await hass.async_block_till_done()
    assert len(google.token_requests) == 1

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=3600 - 300))
    await hass.async_block_till_done()
    assert len(google.token_requests) == 2
    # The send path now uses the refreshed token without fetching again.
    assert await tokens.async_get_token() == google.issued[1]
    assert len(google.token_requests) == 2
    tokens.async_stop()


async def test_stop_cancels_the_refresh_timer(hass, google):
    tokens = _tokens(hass, google)
    await tokens.async_start()
    tokens.async_stop()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(hours=2))
    await hass.async_block_till_done()
    assert len(google.token_requests) == 1


@pytest.mark.parametrize(("response", "code"), [
    ((400, {"error": "invalid_grant", "error_description": "Invalid JWT Signature."}, {}), "credentials_invalid"),
    ((401, {"error": "unauthorized_client"}, {}), "credentials_invalid"),
    ((503, {"error": "backend"}, {}), "transient"),
    ((200, {"no_token": True}, {}), "bad_response"),
])
async def test_a_failing_token_endpoint(hass, google, response, code):
    google.token_script.append(response)
    tokens = _tokens(hass, google)
    with pytest.raises(OAuthError) as err:
        await tokens.async_get_token()
    assert err.value.code == code
    assert "Invalid JWT" not in str(err.value)
    tokens.async_stop()


async def test_token_endpoint_unreachable_is_transient(hass, google):
    tokens = _tokens(hass, google)
    await google.close()
    with pytest.raises(OAuthError) as err:
        await tokens.async_get_token()
    assert err.value.code == "transient"
    tokens.async_stop()


# --------------------------------------------------------------------------
# FCM v1 send + §2.4 result classification
# --------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["fid", "token"])
async def test_send_uses_the_matching_registration_field(hass, google, kind):
    tokens = _tokens(hass, google)
    client = _client(hass, google, tokens)
    result = await client.async_send(_message(kind=kind))
    assert result.outcome is Outcome.OK
    sent = google.sent_messages()[0]
    assert sent[kind] == REG
    assert ({"fid", "token"} - {kind}).isdisjoint(sent)
    assert sent["android"]["restricted_package_name"] == const.PORTICO_PACKAGE
    assert google.fcm_requests[0]["project"] == "portico-test"
    assert google.fcm_requests[0]["auth"] == f"Bearer {google.issued[0]}"
    tokens.async_stop()


async def test_401_invalidates_the_token_and_retries_once(hass, google):
    tokens = _tokens(hass, google)
    client = _client(hass, google, tokens)
    await tokens.async_get_token()
    google.revoked.add(google.issued[0])
    result = await client.async_send(_message())
    assert result.outcome is Outcome.OK
    assert len(google.fcm_requests) == 2
    assert len(google.token_requests) == 2
    assert google.fcm_requests[1]["auth"] == f"Bearer {google.issued[1]}"
    tokens.async_stop()


async def test_a_second_401_is_not_retried_again(hass, google):
    tokens = _tokens(hass, google)
    client = _client(hass, google, tokens)
    google.fcm_script.extend([fcm_error(401, "UNAUTHENTICATED")] * 3)
    result = await client.async_send(_message())
    assert result.outcome is Outcome.CREDENTIALS_INVALID
    assert len(google.fcm_requests) == 2
    tokens.async_stop()


@pytest.mark.parametrize(("response", "outcome", "error"), [
    ((200, {"name": "projects/p/messages/1"}, {}), Outcome.OK, ""),
    (fcm_error(404, "NOT_FOUND", error_code="UNREGISTERED"), Outcome.DEAD, "UNREGISTERED"),
    (fcm_error(404, "NOT_FOUND"), Outcome.DEAD, "NOT_FOUND"),
    (fcm_error(400, "INVALID_ARGUMENT", error_code="INVALID_ARGUMENT", field="message.token"),
     Outcome.DEAD, "INVALID_ARGUMENT"),
    (fcm_error(400, "INVALID_ARGUMENT", field="message.fid"), Outcome.DEAD, "INVALID_ARGUMENT"),
    (fcm_error(400, "INVALID_ARGUMENT", error_code="INVALID_ARGUMENT", field="message.data[0].value"),
     Outcome.PAYLOAD_REJECTED, "INVALID_ARGUMENT"),
    (fcm_error(400, "INVALID_ARGUMENT"), Outcome.PAYLOAD_REJECTED, "INVALID_ARGUMENT"),
    (fcm_error(403, "PERMISSION_DENIED", error_code="SENDER_ID_MISMATCH"),
     Outcome.PROJECT_MISMATCH, "SENDER_ID_MISMATCH"),
    (fcm_error(403, "PERMISSION_DENIED"), Outcome.CREDENTIALS_INVALID, "PERMISSION_DENIED"),
    (fcm_error(429, "RESOURCE_EXHAUSTED", error_code="QUOTA_EXCEEDED", headers={"Retry-After": "7"}),
     Outcome.QUOTA, "QUOTA_EXCEEDED"),
    (fcm_error(503, "UNAVAILABLE", error_code="UNAVAILABLE"), Outcome.TRANSIENT, "UNAVAILABLE"),
    (fcm_error(500, "INTERNAL", error_code="INTERNAL"), Outcome.TRANSIENT, "INTERNAL"),
    ((502, {"garbage": True}, {}), Outcome.TRANSIENT, "HTTP_502"),
])
async def test_result_classification(hass, google, response, outcome, error):
    tokens = _tokens(hass, google)
    client = _client(hass, google, tokens)
    google.fcm_script.append(response)
    result = await client.async_send(_message())
    assert result.outcome is outcome
    assert result.error == error
    if outcome is Outcome.QUOTA:
        assert result.retry_after == 7.0
    tokens.async_stop()


async def test_a_timeout_is_transient(hass, google):
    tokens = _tokens(hass, google)
    client = _client(hass, google, tokens, timeout=0.2)
    google.fcm_delay = 1.0
    result = await client.async_send(_message())
    assert result.outcome is Outcome.TRANSIENT
    assert result.error == "TIMEOUT"
    tokens.async_stop()


async def test_google_down_is_transient(hass, google):
    tokens = _tokens(hass, google)
    await tokens.async_get_token()
    client = _client(hass, google, tokens)
    await google.close()
    result = await client.async_send(_message())
    assert result.outcome is Outcome.TRANSIENT
    tokens.async_stop()


async def test_a_token_failure_maps_onto_the_send_outcome(hass, google):
    google.token_script.append((400, {"error": "invalid_grant"}, {}))
    tokens = _tokens(hass, google)
    client = _client(hass, google, tokens)
    result = await client.async_send(_message())
    assert result.outcome is Outcome.CREDENTIALS_INVALID
    assert google.fcm_requests == []
    tokens.async_stop()


async def test_no_secret_reaches_the_log_on_any_failure(hass, google, caplog):
    caplog.set_level(logging.DEBUG)
    info = service_account_info(google.token_uri)
    key_body = info["private_key"].splitlines()[1]
    tokens = _tokens(hass, google)
    client = _client(hass, google, tokens)
    for response in (fcm_error(400, "INVALID_ARGUMENT"), fcm_error(503, "UNAVAILABLE"),
                     fcm_error(404, "NOT_FOUND", error_code="UNREGISTERED")):
        google.fcm_script.append(response)
        await client.async_send(_message())
    google.token_script.append((400, {"error": "invalid_grant"}, {}))
    tokens.invalidate(google.issued[0])
    await client.async_send(_message())
    text = caplog.text
    assert key_body not in text
    assert REG not in text and REG[:12] not in text
    for issued in google.issued:
        assert issued not in text
    tokens.async_stop()
