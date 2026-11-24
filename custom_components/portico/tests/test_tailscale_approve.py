"""DEV-556: the "approve in Tailscale" enrollment mode (`tailscale_mode:
approve`). The HA admin approves each phone on Tailscale's consent screen
(an OAuth app with the `auth_keys:create:once` scope); Tailscale redirects to
Portico's own callback view, the flow exchanges the code for a single-use
auth key and only then creates the phone's HA user.

HA-harness session. Hermetic: every Tailscale call goes to `aioclient_mock`
(an unmatched request fails the test), the callback is driven through HA's
real http app with `hass_client_no_auth`.
"""
from __future__ import annotations

import json
import logging
from urllib.parse import parse_qs, urlsplit

import pytest
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.portico import enrollment_flow, tailscale_approve
from custom_components.portico.config_flow import DOMAIN
from custom_components.portico.diagnostics import async_get_config_entry_diagnostics

from .test_enrollment_flow import _open_phone_form, _portico_users, _set_urls, qr_payloads, _qr_capture, _start_enroll  # noqa: F401 - fixtures
from .test_push_registry import _custom_integrations  # noqa: F401 - autouse fixture by name
from .phone_helpers import enrolled

EXTERNAL = "https://ha.example.org"
REDIRECT = EXTERNAL + tailscale_approve.CALLBACK_PATH
CLIENT_ID = "kAbC123CNTRL"
SECRET = f"tskey-app-{CLIENT_ID}-SENTINELSECRETxyz0123456789"
CODE = "SENTINEL-CODE-abc123"
ACCESS = "SENTINEL-ACCESS-TOKEN-0123456789"
API_TOKEN = "tskey-api-SENTINEL-ADMIN-0123456789"
BODY_MARKER = "SENTINEL-BODY"
SECRETS = (SECRET, "SENTINELSECRET", CODE, ACCESS, API_TOKEN, BODY_MARKER)


def _entry(hass, **data) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, title="Portico push ring notification", unique_id="push",
        data={"entry_type": "push", "project_id": "portico-test", **data},
    )
    entry.add_to_hass(hass)
    return entry


def _store_key(entry) -> str:
    return f"{tailscale_approve.STORE_KEY_PREFIX}{entry.entry_id}"


def _approve_entry(hass, hass_storage, *, with_secret=True) -> MockConfigEntry:
    entry = _entry(hass, tailscale_mode="approve", tailscale_client_id=CLIENT_ID, tailscale_redirect_uri=REDIRECT)
    if with_secret:
        key = _store_key(entry)
        hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": {"client_secret": SECRET}}
    return entry


def _query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


async def _to_approval(hass, entry, name="Alice's Pixel"):
    """Enroll up to the external (approval) step."""
    result = await _open_phone_form(hass, entry)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"phone_name": name})
    assert result["type"] is FlowResultType.EXTERNAL_STEP, result
    assert result["step_id"] == "tailscale_approve"
    return result


_NOT_HA_LOGS = (
    # The test harness's mock-storage writer prints what a test Store saves;
    # HA's real Store writes no data to the log.
    "pytest_homeassistant_custom_component",
    # aiohttp's access log of the test server. HA's bootstrap pins
    # `aiohttp.access` to WARNING, so a running HA never writes the callback
    # URL (docs/guides/tailscale.md: the code is single-use and useless without the secret).
    "aiohttp.access",
)


def _log_text(caplog) -> str:
    """Every log line at DEBUG and up, but the two loggers above."""
    return "\n".join(r.getMessage() for r in caplog.records if not r.name.startswith(_NOT_HA_LOGS))


def _assert_no_secret(text: str):
    for secret in SECRETS:
        assert secret not in text


# --- the mode question -------------------------------------------------------


async def test_the_mode_question_lists_approve_first_and_defaults_to_it(hass):
    entry = _entry(hass)
    result = await _start_enroll(hass, entry)
    assert result["step_id"] == "tailscale_mode", result
    schema = result["data_schema"]
    (key,) = schema.schema
    assert key.default() == "approve"
    selector = schema.schema[key]
    assert selector.config["options"] == ["approve", "auto", "manual"]
    assert selector.config["translation_key"] == "tailscale_mode"
    assert schema({}) == {"tailscale_mode": "approve"}


async def test_approve_needs_an_https_url_for_the_redirect(hass):
    _set_urls(hass, internal="http://ha.lan:8123")
    entry = _entry(hass)
    result = await _start_enroll(hass, entry)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"tailscale_mode": "approve"})
    assert (result["step_id"], result["errors"]) == ("tailscale_mode", {"base": "no_https_url"})
    assert "tailscale_mode" not in entry.data


# --- the one-time OAuth-app step ---------------------------------------------


async def _to_oauth_app_form(hass, entry):
    result = await _start_enroll(hass, entry)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"tailscale_mode": "approve"})
    assert result["type"] is FlowResultType.MENU, result
    assert result["step_id"] == "tailscale_oauth_app_choice"
    assert result["menu_options"] == ["tailscale_oauth_app", "tailscale_oauth_app_create"]
    assert result["description_placeholders"]["redirect_uri"] == REDIRECT
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "tailscale_oauth_app"})
    assert result["step_id"] == "tailscale_oauth_app", result
    assert result["description_placeholders"]["redirect_uri"] == REDIRECT
    return result


async def test_the_oauth_app_step_stores_the_secret_privately(hass, hass_storage, caplog):
    caplog.set_level(logging.DEBUG)
    _set_urls(hass, external=EXTERNAL)
    entry = _entry(hass)
    result = await _to_oauth_app_form(hass, entry)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"client_secret": SECRET})
    assert result["step_id"] == "phone_details", result

    assert entry.data["tailscale_mode"] == "approve"
    assert entry.data["tailscale_client_id"] == CLIENT_ID
    assert entry.data["tailscale_redirect_uri"] == REDIRECT
    _assert_no_secret(json.dumps(dict(entry.data)))
    assert hass_storage[_store_key(entry)]["data"] == {"client_secret": SECRET}
    _assert_no_secret(_log_text(caplog))

    diag = await async_get_config_entry_diagnostics(hass, entry)
    _assert_no_secret(json.dumps(diag, default=str))


@pytest.mark.parametrize(("user_input", "field"), [
    ({"client_secret": "tskey-auth-notanappsecret-0123"}, "client_secret"),
    ({"client_secret": "tskey-app-"}, "client_secret"),
    ({"client_secret": "tskey-app-abc"}, "client_secret"),
    ({"client_secret": "tskey-app-abc-def ghi"}, "client_secret"),
    ({"client_secret": SECRET, "client_id": "someoneElse"}, "client_id"),
    ({"client_secret": SECRET, "client_id": "bad id/../"}, "client_id"),
])
async def test_a_malformed_secret_is_refused_and_not_echoed(hass, hass_storage, user_input, field):
    _set_urls(hass, external=EXTERNAL)
    entry = _entry(hass)
    result = await _to_oauth_app_form(hass, entry)
    result = await hass.config_entries.options.async_configure(result["flow_id"], user_input)
    assert result["step_id"] == "tailscale_oauth_app"
    assert result["errors"] == {field: f"tailscale_{field}_invalid"}
    assert "tailscale_mode" not in entry.data
    assert _store_key(entry) not in hass_storage
    # Never offered back as a form default.
    _assert_no_secret(json.dumps(_defaults(result["data_schema"]), default=str))


def _defaults(schema) -> dict:
    import voluptuous as vol

    return {str(k): k.default() for k in schema.schema if k.default is not vol.UNDEFINED}


def test_parse_client_secret():
    assert tailscale_approve.parse_client_secret(SECRET) == (CLIENT_ID, SECRET)
    assert tailscale_approve.parse_client_secret("  " + SECRET + "\n") == (CLIENT_ID, SECRET)
    # An explicit client id that the secret embeds (it may itself hold a dash).
    dashed = "tskey-app-app-123456-SENTINELsecret0123"
    assert tailscale_approve.parse_client_secret(dashed, "app-123456") == ("app-123456", dashed)
    assert tailscale_approve.parse_client_secret(dashed) == ("app", dashed)
    for bad in ("", "tskey-client-x-y", "tskey-app-x-", "tskey-app--y", "tskey-app-x-y\x00"):
        with pytest.raises(ValueError):
            tailscale_approve.parse_client_secret(bad)


async def test_abandoning_the_oauth_app_step_persists_nothing(hass, hass_storage):
    _set_urls(hass, external=EXTERNAL)
    entry = _entry(hass)
    result = await _to_oauth_app_form(hass, entry)
    hass.config_entries.options.async_abort(result["flow_id"])
    assert "tailscale_mode" not in entry.data and _store_key(entry) not in hass_storage
    result = await _start_enroll(hass, entry)
    assert result["step_id"] == "tailscale_mode"


async def test_an_approve_entry_without_its_stored_secret_asks_again(hass, hass_storage):
    entry = _approve_entry(hass, hass_storage, with_secret=False)
    result = await _start_enroll(hass, entry)
    assert result["step_id"] == "tailscale_mode", result
    assert entry.data["tailscale_mode"] == "approve"  # not flipped by asking


@pytest.mark.parametrize("missing", ["tailscale_client_id", "tailscale_redirect_uri"])
async def test_an_approve_entry_without_its_client_id_asks_again(hass, hass_storage, missing):
    entry = _approve_entry(hass, hass_storage)
    hass.config_entries.async_update_entry(entry, data={k: v for k, v in entry.data.items() if k != missing})
    result = await _start_enroll(hass, entry)
    assert result["step_id"] == "tailscale_mode", result


# --- the optional "create the OAuth app for me" step -------------------------


async def test_create_the_oauth_app_with_a_one_time_api_token(hass, hass_storage, aioclient_mock, caplog):
    caplog.set_level(logging.DEBUG)
    _set_urls(hass, external=EXTERNAL)
    aioclient_mock.post(tailscale_approve.OAUTH_APPS_URL, json={"id": "app-1", "clientSecret": SECRET})
    entry = _entry(hass)
    result = await _start_enroll(hass, entry)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"tailscale_mode": "approve"})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "tailscale_oauth_app_create"})
    assert result["step_id"] == "tailscale_oauth_app_create", result
    assert result["description_placeholders"]["redirect_uri"] == REDIRECT
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"api_access_token": API_TOKEN})
    assert result["step_id"] == "phone_details", result

    (method, url, body, headers), = aioclient_mock.mock_calls
    assert body == {"name": "Portico (Home Assistant)", "redirectUris": [REDIRECT],
                    "scopes": ["auth_keys:create:once"]}
    import base64
    assert headers["Authorization"] == "Basic " + base64.b64encode(f"{API_TOKEN}:".encode()).decode()

    assert entry.data["tailscale_client_id"] == CLIENT_ID
    assert hass_storage[_store_key(entry)]["data"] == {"client_secret": SECRET}
    # The admin API token is used once and kept nowhere.
    assert API_TOKEN not in json.dumps(dict(entry.data))
    assert API_TOKEN not in json.dumps(hass_storage, default=str)
    _assert_no_secret(_log_text(caplog))


@pytest.mark.parametrize(("response", "error"), [
    ({"status": 401, "text": BODY_MARKER}, "tailscale_api_token_rejected"),
    ({"status": 403, "text": BODY_MARKER}, "tailscale_api_token_rejected"),
    ({"status": 500, "text": BODY_MARKER}, "tailscale_create_app_failed"),
    ({"exc": TimeoutError()}, "tailscale_create_app_failed"),
    ({"text": BODY_MARKER + " not json"}, "tailscale_create_app_failed"),
    ({"json": {"id": "app-1", BODY_MARKER: 1}}, "tailscale_create_app_failed"),
    ({"json": {"clientSecret": "tskey-auth-wrong-kind"}}, "tailscale_create_app_failed"),
])
async def test_create_failures_persist_nothing(hass, hass_storage, aioclient_mock, caplog, response, error):
    _set_urls(hass, external=EXTERNAL)
    aioclient_mock.post(tailscale_approve.OAUTH_APPS_URL, **response)
    entry = _entry(hass)
    result = await _start_enroll(hass, entry)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"tailscale_mode": "approve"})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "tailscale_oauth_app_create"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"api_access_token": API_TOKEN})
    assert (result["step_id"], result["errors"]) == ("tailscale_oauth_app_create", {"base": error})
    assert "tailscale_mode" not in entry.data and _store_key(entry) not in hass_storage
    _assert_no_secret(_log_text(caplog))


# --- per-enrollment: approval, callback, exchange ----------------------------


async def test_the_approval_link_creates_nothing_yet(hass, hass_storage, aioclient_mock):
    _set_urls(hass, external=EXTERNAL)
    entry = _approve_entry(hass, hass_storage)
    result = await _to_approval(hass, entry)
    url = result["url"]
    assert url.startswith(tailscale_approve.AUTHORIZE_URL + "?")
    q = _query(url)
    assert q["client_id"] == CLIENT_ID
    assert q["redirect_uri"] == REDIRECT
    assert q["scope"] == "auth_keys:create:once"
    assert q["response_type"] == "code"
    assert len(q["state"]) >= 43  # 32 random bytes, url-safe base64
    assert SECRET not in url
    assert _portico_users(hass) == [] and aioclient_mock.call_count == 0
    assert enrolled(hass) == []


async def test_approve_success_path(hass, hass_storage, aioclient_mock, hass_client_no_auth, qr_payloads,
                                    monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    _set_urls(hass, external=EXTERNAL)
    calls: list[tuple[str, int]] = []
    aioclient_mock.post(tailscale_approve.TOKEN_URL, json={"access_token": ACCESS, "token_type": "Bearer",
                                                           "expires_in": 3600})
    real_create = hass.auth.async_create_user

    async def _create_user(*args, **kwargs):
        # How many Tailscale calls had happened when the user was created.
        calls.append(("create_user", aioclient_mock.call_count))
        return await real_create(*args, **kwargs)

    monkeypatch.setattr(hass.auth, "async_create_user", _create_user)
    entry = _approve_entry(hass, hass_storage)
    result = await _to_approval(hass, entry)
    flow_id, state = result["flow_id"], _query(result["url"])["state"]

    client = await hass_client_no_auth()
    resp = await client.get(tailscale_approve.CALLBACK_PATH, params={"code": CODE, "state": state})
    assert resp.status == 200
    assert resp.headers["Content-Type"].startswith("text/plain")
    assert resp.headers["Cache-Control"] == "no-store"
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    body = await resp.text()
    _assert_no_secret(body)
    assert state not in body
    # The callback only records the result; nothing is exchanged or created yet.
    assert calls == [] and _portico_users(hass) == [] and aioclient_mock.call_count == 0

    result = await hass.config_entries.options.async_configure(flow_id)
    assert result["step_id"] == "show_qr", result
    assert calls == [("create_user", 1)]  # created only after the exchange
    assert qr_payloads[0]["tailscale_authkey"] == ACCESS
    assert len(_portico_users(hass)) == 1 and len(enrolled(hass)) == 1

    (_m, url, form, _h), = aioclient_mock.mock_calls
    assert str(url) == tailscale_approve.TOKEN_URL
    assert form == {"grant_type": "authorization_code", "code": CODE, "client_id": CLIENT_ID,
                    "client_secret": SECRET, "redirect_uri": REDIRECT}
    _assert_no_secret(_log_text(caplog))
    assert len(tailscale_approve.get_registry(hass)) == 0


async def test_a_replayed_callback_is_refused(hass, hass_storage, aioclient_mock, hass_client_no_auth):
    _set_urls(hass, external=EXTERNAL)
    aioclient_mock.post(tailscale_approve.TOKEN_URL, json={"access_token": ACCESS})
    entry = _approve_entry(hass, hass_storage)
    result = await _to_approval(hass, entry)
    state = _query(result["url"])["state"]
    client = await hass_client_no_auth()
    assert (await client.get(tailscale_approve.CALLBACK_PATH, params={"code": CODE, "state": state})).status == 200
    # A second tab, or an attacker who saw the URL: refused, flow untouched.
    resp = await client.get(tailscale_approve.CALLBACK_PATH, params={"code": "OTHER-CODE", "state": state})
    assert resp.status == 400
    result = await hass.config_entries.options.async_configure(result["flow_id"])
    assert result["step_id"] == "show_qr"
    assert aioclient_mock.mock_calls[0][2]["code"] == CODE
    assert aioclient_mock.call_count == 1


@pytest.mark.parametrize("params", [
    {"code": CODE, "state": "A" * 43},  # well-formed, never issued
    {"code": CODE, "state": "<script>alert(1)</script>"},
    {"code": CODE},
    {"state": ""},
    {"error": "access_denied"},  # denial without a state routes nowhere
    {},
])
async def test_an_unknown_state_touches_nothing(hass, hass_storage, aioclient_mock, hass_client_no_auth, params):
    _set_urls(hass, external=EXTERNAL)
    entry = _approve_entry(hass, hass_storage)
    result = await _to_approval(hass, entry)
    client = await hass_client_no_auth()
    resp = await client.get(tailscale_approve.CALLBACK_PATH, params=params)
    assert resp.status == 400
    assert resp.headers["Content-Type"].startswith("text/plain")
    body = await resp.text()
    assert "<script>" not in body and CODE not in body and "AAAA" not in body
    # The real flow is still waiting on its approval, its state still valid.
    assert len(tailscale_approve.get_registry(hass)) == 1
    assert hass.config_entries.options.async_get(result["flow_id"])["step_id"] == "tailscale_approve"
    assert aioclient_mock.call_count == 0 and _portico_users(hass) == []


async def test_an_expired_state_is_refused(hass, hass_storage, aioclient_mock, hass_client_no_auth, monkeypatch):
    _set_urls(hass, external=EXTERNAL)
    now = [1000.0]
    monkeypatch.setattr(tailscale_approve, "_monotonic", lambda: now[0])
    entry = _approve_entry(hass, hass_storage)
    result = await _to_approval(hass, entry)
    state = _query(result["url"])["state"]
    now[0] += tailscale_approve.STATE_TTL_SECONDS + 1
    client = await hass_client_no_auth()
    resp = await client.get(tailscale_approve.CALLBACK_PATH, params={"code": CODE, "state": state})
    assert resp.status == 400
    assert aioclient_mock.call_count == 0 and _portico_users(hass) == []


@pytest.mark.parametrize("error", ["access_denied", "server_error", "<b>x</b>"])
async def test_denied_consent_creates_no_user(hass, hass_storage, aioclient_mock, hass_client_no_auth, error):
    _set_urls(hass, external=EXTERNAL)
    entry = _approve_entry(hass, hass_storage)
    result = await _to_approval(hass, entry)
    state = _query(result["url"])["state"]
    client = await hass_client_no_auth()
    resp = await client.get(tailscale_approve.CALLBACK_PATH, params={"error": error, "state": state})
    assert resp.status == 200
    assert "<b>" not in await resp.text()
    result = await hass.config_entries.options.async_configure(result["flow_id"])
    assert (result["step_id"], result["errors"]) == ("phone_details", {"base": "tailscale_approval_denied"})
    assert aioclient_mock.call_count == 0 and _portico_users(hass) == []

    # The same form starts a fresh approval with a new state.
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"phone_name": "p"})
    assert result["type"] is FlowResultType.EXTERNAL_STEP
    assert _query(result["url"])["state"] != state


@pytest.mark.parametrize(("response", "error"), [
    ({"status": 401, "text": BODY_MARKER + " invalid_client"}, "tailscale_exchange_rejected"),
    ({"status": 400, "json": {"error": "invalid_grant", "x": BODY_MARKER}}, "tailscale_exchange_rejected"),
    ({"status": 503, "text": BODY_MARKER}, "tailscale_exchange_failed"),
    ({"exc": TimeoutError()}, "tailscale_exchange_failed"),
    ({"text": BODY_MARKER + " <html>"}, "tailscale_exchange_failed"),
    ({"json": {"token_type": "Bearer", BODY_MARKER: 1}}, "tailscale_exchange_failed"),
    ({"json": {"access_token": ""}}, "tailscale_exchange_failed"),
    ({"json": {"access_token": "has space"}}, "tailscale_exchange_failed"),
    ({"json": ["not", "an", "object"]}, "tailscale_exchange_failed"),
])
async def test_exchange_failures_create_no_user(hass, hass_storage, aioclient_mock, hass_client_no_auth, caplog,
                                                qr_payloads, response, error):
    caplog.set_level(logging.DEBUG)
    _set_urls(hass, external=EXTERNAL)
    aioclient_mock.post(tailscale_approve.TOKEN_URL, **response)
    entry = _approve_entry(hass, hass_storage)
    users_before = len(hass.auth._store._users)
    result = await _to_approval(hass, entry)
    state = _query(result["url"])["state"]
    client = await hass_client_no_auth()
    await client.get(tailscale_approve.CALLBACK_PATH, params={"code": CODE, "state": state})
    result = await hass.config_entries.options.async_configure(result["flow_id"])
    assert (result["step_id"], result["errors"]) == ("phone_details", {"base": error})
    assert len(hass.auth._store._users) == users_before and qr_payloads == []
    assert enrolled(hass) == []
    _assert_no_secret(_log_text(caplog))


async def test_abandoning_at_approval_creates_nothing(hass, hass_storage, aioclient_mock, hass_client_no_auth):
    _set_urls(hass, external=EXTERNAL)
    entry = _approve_entry(hass, hass_storage)
    result = await _to_approval(hass, entry)
    state = _query(result["url"])["state"]
    hass.config_entries.options.async_abort(result["flow_id"])
    assert len(tailscale_approve.get_registry(hass)) == 0
    client = await hass_client_no_auth()
    resp = await client.get(tailscale_approve.CALLBACK_PATH, params={"code": CODE, "state": state})
    assert resp.status == 400
    assert aioclient_mock.call_count == 0 and _portico_users(hass) == []


async def test_concurrent_flows_each_resume_only_their_own(hass, hass_storage, aioclient_mock,
                                                         hass_client_no_auth, qr_payloads):
    _set_urls(hass, external=EXTERNAL)
    aioclient_mock.post(tailscale_approve.TOKEN_URL, json={"access_token": ACCESS})
    entry = _approve_entry(hass, hass_storage)
    first = await _to_approval(hass, entry, name="A")
    second = await _to_approval(hass, entry, name="B")
    state_a, state_b = _query(first["url"])["state"], _query(second["url"])["state"]
    assert state_a != state_b
    client = await hass_client_no_auth()
    assert (await client.get(tailscale_approve.CALLBACK_PATH, params={"code": CODE, "state": state_b})).status == 200
    assert hass.config_entries.options.async_get(first["flow_id"])["step_id"] == "tailscale_approve"
    result = await hass.config_entries.options.async_configure(second["flow_id"])
    assert result["step_id"] == "show_qr" and qr_payloads[0]["phone_name"] == "B"
    # Flow A is still waiting, its own state still valid.
    assert len(tailscale_approve.get_registry(hass)) == 1


async def test_the_flow_ignores_user_input_on_the_approval_step(hass, hass_storage, aioclient_mock):
    """Only the callback view can complete the approval: input posted to the
    external step through the flow API (even by an admin) carries nothing."""
    _set_urls(hass, external=EXTERNAL)
    entry = _approve_entry(hass, hass_storage)
    result = await _to_approval(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"code": CODE, "state": _query(result["url"])["state"], "access_token": ACCESS})
    assert result["type"] is FlowResultType.EXTERNAL_STEP
    assert aioclient_mock.call_count == 0 and _portico_users(hass) == []


async def test_a_missing_store_secret_at_exchange_time_asks_again(hass, hass_storage, aioclient_mock,
                                                                 hass_client_no_auth):
    _set_urls(hass, external=EXTERNAL)
    entry = _approve_entry(hass, hass_storage)
    result = await _to_approval(hass, entry)
    del hass_storage[_store_key(entry)]
    client = await hass_client_no_auth()
    await client.get(tailscale_approve.CALLBACK_PATH, params={"code": CODE, "state": _query(result["url"])["state"]})
    result = await hass.config_entries.options.async_configure(result["flow_id"])
    assert result["step_id"] == "tailscale_mode"
    assert aioclient_mock.call_count == 0 and _portico_users(hass) == []


async def test_a_failure_after_the_user_exists_removes_it(hass, hass_owner_user, hass_storage, aioclient_mock,
                                                          hass_client_no_auth, monkeypatch):
    _set_urls(hass, external=EXTERNAL)
    aioclient_mock.post(tailscale_approve.TOKEN_URL, json={"access_token": ACCESS})

    def _boom(payload):
        raise RuntimeError("QR failed")

    monkeypatch.setattr(enrollment_flow, "_build_qr_data_uri", _boom)
    entry = _approve_entry(hass, hass_storage)
    users_before = len(hass.auth._store._users)
    result = await _to_approval(hass, entry)
    client = await hass_client_no_auth()
    await client.get(tailscale_approve.CALLBACK_PATH, params={"code": CODE, "state": _query(result["url"])["state"]})
    with pytest.raises(RuntimeError, match="QR failed"):
        await hass.config_entries.options.async_configure(result["flow_id"])
    assert len(hass.auth._store._users) == users_before


async def test_removing_the_entry_removes_the_stored_secret(hass, hass_storage):
    from custom_components.portico import async_remove_entry

    entry = _approve_entry(hass, hass_storage)
    assert await tailscale_approve.OAuthAppStore(hass, entry.entry_id).async_load_secret() == SECRET
    await async_remove_entry(hass, entry)
    assert await tailscale_approve.OAuthAppStore(hass, entry.entry_id).async_load_secret() is None


# --- the state registry ------------------------------------------------------


def test_registry_state_is_random_bound_and_single_use():
    reg = tailscale_approve.ApprovalRegistry()
    s1, r1 = reg.issue("flow-1")
    s2, r2 = reg.issue("flow-2")
    assert s1 != s2 and len(s1) >= 43 and r1.flow_id == "flow-1"
    assert reg.consume(s2) is r2
    assert reg.consume(s2) is None  # single use
    # One live state per flow: a re-issue revokes the old one.
    s3, _ = reg.issue("flow-1")
    assert reg.consume(s1) is None and reg.consume(s3).flow_id == "flow-1"
    reg.issue("flow-4")
    reg.revoke_flow("flow-4")
    assert len(reg) == 0


def test_registry_compares_every_live_state_in_constant_time(monkeypatch):
    compared: list[tuple[bytes, bytes]] = []
    real = tailscale_approve.hmac.compare_digest

    def _spy(a, b):
        compared.append((a, b))
        return real(a, b)

    monkeypatch.setattr(tailscale_approve.hmac, "compare_digest", _spy)
    reg = tailscale_approve.ApprovalRegistry()
    states = [reg.issue(f"flow-{i}")[0] for i in range(3)]
    assert reg.consume(states[0]) is not None
    # No early exit: every live record was compared, as fixed-length digests.
    assert len(compared) == 3
    assert all(len(a) == len(b) == 32 for a, b in compared)
    compared.clear()
    assert reg.consume("nope") is None
    assert len(compared) == 2


def test_registry_expiry(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(tailscale_approve, "_monotonic", lambda: now[0])
    reg = tailscale_approve.ApprovalRegistry()
    state, _ = reg.issue("flow-1")
    now[0] = tailscale_approve.STATE_TTL_SECONDS - 1
    s2, _ = reg.issue("flow-2")
    now[0] = tailscale_approve.STATE_TTL_SECONDS + 1
    assert reg.consume(state) is None
    assert len(reg) == 1  # expired records are purged
    assert reg.consume(s2) is not None
    assert tailscale_approve.STATE_TTL_SECONDS == 600
