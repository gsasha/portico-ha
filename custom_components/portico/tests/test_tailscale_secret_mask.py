"""A stored secret shows as a row of stars (Alice, 2026-10-09: "The secret
field should be a row of stars to indicate that it's already filled in").

The three secret inputs of the Tailscale steps use HA's password selector, so
a typed secret is masked in the browser: `tailscale_api_key` (auto),
`client_secret` (approve: the OAuth app's secret) and `api_access_token`
(approve: the one-time admin token, never stored).

While a secret is stored, its field is optional and pre-filled (as a
suggested value) with `SECRET_MASK`, twelve bullets whatever the real length.
On submit the mask, or a blank, keeps the stored secret; anything else
replaces it after the usual stripping and checks. A value that still holds a
bullet (typed after the stars without clearing them) is refused, so the mask
is never stored, never sent to Tailscale and never logged. No secret stored:
the field is empty and required, and the mask is refused like a blank.

The mask cannot be a real secret: every Tailscale credential starts `tskey-`.

The form is also rendered through HA's real flow HTTP API, so the schema is
serialised exactly as the frontend receives it.

HA-harness session. Hermetic: every Tailscale call goes to `aioclient_mock`.
"""
from __future__ import annotations

import json
import logging
from urllib.parse import parse_qs, urlsplit

import pytest
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.portico import enrollment_flow, tailscale_approve
from custom_components.portico.config_flow import DOMAIN
from custom_components.portico.diagnostics import async_get_config_entry_diagnostics

from .test_enrollment_flow import _portico_users, _set_urls, qr_payloads, _qr_capture, _start_enroll  # noqa: F401 - fixtures
from .test_push_registry import _custom_integrations  # noqa: F401 - autouse fixture by name

MASK = "•" * 12
EXTERNAL = "https://ha.example.org"
REDIRECT = EXTERNAL + tailscale_approve.CALLBACK_PATH
OLD_REDIRECT = "https://old-ha.example.org" + tailscale_approve.CALLBACK_PATH
# auto
API_KEY = "tskey-client-kSENTINELCID-SENTINELAPIKEY0123456789"
NEW_API_KEY = "tskey-client-kSENTINELCID-SENTINELNEWAPIKEY0123456789"
TAILNET = "-"
TAG = "tag:portico-phone"
CREDS = {"tailscale_api_key": API_KEY, "tailscale_tailnet": TAILNET, "tailscale_tag": TAG}
MINT_URL = f"{enrollment_flow.TAILSCALE_API_BASE}/tailnet/{TAILNET}/keys"
AUTO_TOKEN_URL = "https://api.tailscale.com/api/v2/oauth/token"
ACCESS = "SENTINEL-ACCESS-TOKEN-0123456789"
MINTED = "tskey-auth-MINTED-0123456789"
# approve
CLIENT_ID = "kAbC123CNTRL"
SECRET = f"tskey-app-{CLIENT_ID}-SENTINELSECRETxyz0123456789"
NEW_CLIENT_ID = "nEw456CNTRL"
NEW_SECRET = f"tskey-app-{NEW_CLIENT_ID}-SENTINELNEWSECRET0123456789"
CODE = "SENTINEL-CODE-abc123"

SECRET_FIELDS = ("tailscale_api_key", "client_secret", "api_access_token")


def test_the_mask_is_twelve_bullets_and_no_secret():
    assert enrollment_flow.SECRET_MASK == MASK
    assert not MASK.startswith("tskey-")


def _entry(hass, **data) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, title="Portico push ring notification", unique_id="push",
        data={"entry_type": "push", "project_id": "portico-test", **data},
    )
    entry.add_to_hass(hass)
    return entry


def _store_key(entry) -> str:
    return f"{tailscale_approve.STORE_KEY_PREFIX}{entry.entry_id}"


def _put_secret(hass_storage, entry, secret=SECRET):
    key = _store_key(entry)
    hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": {"client_secret": secret}}


def _stored(hass_storage, entry):
    item = hass_storage.get(_store_key(entry))
    return None if item is None else item["data"]


def _log_text(caplog) -> str:
    return "\n".join(
        r.getMessage() for r in caplog.records
        if not r.name.startswith(("pytest_homeassistant_custom_component", "aiohttp.access"))
    )


def _key(result, field):
    for key in result["data_schema"].schema:
        if key == field:
            return key
    raise AssertionError(f"no {field} in the form")


def _suggested(result, field):
    return (_key(result, field).description or {}).get("suggested_value")


def _is_password(result, field) -> bool:
    selector = result["data_schema"].schema[_key(result, field)]
    return getattr(selector, "config", {}).get("type") == "password"


def _assert_mask_nowhere(*texts: str):
    for text in texts:
        assert MASK not in text
        assert "•" not in text


# --- reaching each form --------------------------------------------------------


async def _change_to(hass, entry, mode):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert (result["type"], result["step_id"]) == (FlowResultType.MENU, "init"), result
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "tailscale_change_mode"})
    return await hass.config_entries.options.async_configure(result["flow_id"], {"tailscale_mode": mode})


async def _auto_form(hass, entry):
    result = await _change_to(hass, entry, "auto")
    assert (result["type"], result["step_id"]) == (FlowResultType.FORM, "tailscale_credentials"), result
    return result


async def _redirect_changed_form(hass, hass_storage, step="tailscale_oauth_app"):
    """An approve entry whose app was registered with the OLD https URL: the
    app is set up again, the stored secret still in the Store."""
    _set_urls(hass, external=EXTERNAL)
    entry = _entry(hass, tailscale_mode="approve", tailscale_client_id=CLIENT_ID, tailscale_redirect_uri=OLD_REDIRECT)
    _put_secret(hass_storage, entry)
    result = await _change_to(hass, entry, "approve")
    assert result["step_id"] == "tailscale_oauth_app_choice_redirect_changed", result
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": step})
    assert (result["type"], result["step_id"]) == (FlowResultType.FORM, step), result
    return entry, result


async def _fresh_approve_form(hass, step="tailscale_oauth_app"):
    _set_urls(hass, external=EXTERNAL)
    entry = _entry(hass)
    result = await _start_enroll(hass, entry)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"tailscale_mode": "approve"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": step})
    assert (result["type"], result["step_id"]) == (FlowResultType.FORM, step), result
    return entry, result


def _saved(result, mode):
    assert result["type"] is FlowResultType.ABORT, result
    assert result["reason"] == f"tailscale_mode_saved_{mode}"


# --- the selector ---------------------------------------------------------------


async def test_auto_secret_is_a_password_field_the_first_time_and_on_a_change(hass):
    entry = _entry(hass)
    result = await _start_enroll(hass, entry)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"tailscale_mode": "auto"})
    assert _is_password(result, "tailscale_api_key")
    assert not _is_password(result, "tailscale_tailnet") and not _is_password(result, "tailscale_tag")
    hass.config_entries.options.async_abort(result["flow_id"])

    other = _entry(hass, tailscale_mode="auto", **CREDS)
    other_result = await _auto_form(hass, other)
    assert _is_password(other_result, "tailscale_api_key")


async def test_approve_secrets_are_password_fields(hass, hass_storage):
    _entry_, result = await _fresh_approve_form(hass)
    assert _is_password(result, "client_secret")
    assert not _is_password(result, "client_id")
    hass.config_entries.options.async_abort(result["flow_id"])
    _entry_, result = await _fresh_approve_form(hass, "tailscale_oauth_app_create")
    assert _is_password(result, "api_access_token")


# --- the stars, auto ---------------------------------------------------------------


async def test_auto_with_a_stored_secret_shows_the_mask(hass):
    entry = _entry(hass, tailscale_mode="auto", **CREDS)
    result = await _auto_form(hass, entry)
    key = _key(result, "tailscale_api_key")
    assert str(key) == "tailscale_api_key" and type(key).__name__ == "Optional"
    assert _suggested(result, "tailscale_api_key") == MASK


@pytest.mark.parametrize("typed", [MASK, f"  {MASK} ", "", "   "])
async def test_auto_the_mask_or_a_blank_keeps_the_stored_secret(hass, qr_payloads, aioclient_mock, caplog, typed):
    caplog.set_level(logging.DEBUG)
    _set_urls(hass, external=EXTERNAL)
    entry = _entry(hass, tailscale_mode="manual", **CREDS)
    result = await _auto_form(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tailscale_api_key": typed, "tailscale_tailnet": TAILNET, "tailscale_tag": TAG})
    _saved(result, "auto")
    assert entry.data["tailscale_api_key"] == API_KEY

    # The next mint exchanges the OLD secret.
    aioclient_mock.post(AUTO_TOKEN_URL, json={"access_token": ACCESS})
    aioclient_mock.post(MINT_URL, json={"key": MINTED})
    result = await _start_enroll(hass, entry)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"phone_name": "p"})
    assert result["step_id"] == "show_qr", result
    (_m1, _u1, form, _h1), (_m2, _u2, body, _h2) = aioclient_mock.mock_calls
    assert form["client_secret"] == API_KEY
    _assert_mask_nowhere(
        json.dumps(dict(entry.data)),
        json.dumps([c[1:3] for c in aioclient_mock.mock_calls], default=str),
        json.dumps(await async_get_config_entry_diagnostics(hass, entry), default=str),
        _log_text(caplog),
    )


async def test_auto_a_new_secret_replaces_the_stored_one(hass):
    entry = _entry(hass, tailscale_mode="auto", **CREDS)
    result = await _auto_form(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tailscale_api_key": f" {NEW_API_KEY} ", "tailscale_tailnet": TAILNET, "tailscale_tag": TAG})
    _saved(result, "auto")
    assert entry.data["tailscale_api_key"] == NEW_API_KEY


@pytest.mark.parametrize("typed", [MASK + NEW_API_KEY, NEW_API_KEY + MASK, "•" * 11, "•" * 13,
                                   MASK[:6] + "x" + MASK[6:]])
async def test_auto_an_edited_mask_is_refused(hass, typed):
    """Typing after the stars without clearing them, or deleting some: never
    stored as the secret."""
    entry = _entry(hass, tailscale_mode="auto", **CREDS)
    before = dict(entry.data)
    result = await _auto_form(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tailscale_api_key": typed, "tailscale_tailnet": TAILNET, "tailscale_tag": TAG})
    assert (result["step_id"], result["errors"]) == (
        "tailscale_credentials", {"tailscale_api_key": "secret_mask_edited"})
    assert dict(entry.data) == before
    assert _suggested(result, "tailscale_api_key") == MASK  # the stars again, never what was typed


async def test_auto_a_refused_form_shows_the_mask_again(hass):
    entry = _entry(hass, tailscale_mode="auto", **CREDS)
    result = await _auto_form(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tailscale_api_key": MASK, "tailscale_tailnet": TAILNET, "tailscale_tag": "bad"})
    assert result["errors"] == {"tailscale_tag": "tailscale_tag_invalid"}
    assert _suggested(result, "tailscale_api_key") == MASK


@pytest.mark.parametrize("stored", [{}, {"tailscale_api_key": "  "}])
async def test_auto_without_a_stored_secret_the_field_is_empty_and_required(hass, stored):
    entry = _entry(hass, tailscale_mode="manual", tailscale_tailnet=TAILNET, tailscale_tag=TAG, **stored)
    result = await _auto_form(hass, entry)
    assert type(_key(result, "tailscale_api_key")).__name__ == "Required"
    assert _suggested(result, "tailscale_api_key") is None
    # The mask, with nothing stored, is no secret.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tailscale_api_key": MASK, "tailscale_tailnet": TAILNET, "tailscale_tag": TAG})
    assert result["errors"] == {"tailscale_api_key": "tailscale_api_key_blank"}
    assert entry.data["tailscale_mode"] == "manual"
    assert MASK not in json.dumps(dict(entry.data))


async def test_auto_first_time_form_has_no_mask(hass):
    entry = _entry(hass)
    result = await _start_enroll(hass, entry)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"tailscale_mode": "auto"})
    assert type(_key(result, "tailscale_api_key")).__name__ == "Required"
    assert _suggested(result, "tailscale_api_key") is None


# --- the stars, approve ------------------------------------------------------------


async def test_approve_with_a_stored_secret_shows_the_mask(hass, hass_storage):
    _entry_, result = await _redirect_changed_form(hass, hass_storage)
    assert type(_key(result, "client_secret")).__name__ == "Optional"
    assert _suggested(result, "client_secret") == MASK
    assert _suggested(result, "client_id") is None


@pytest.mark.parametrize("typed", [{"client_secret": MASK}, {"client_secret": f" {MASK}\n"}, {"client_secret": ""},
                                   {}, {"client_secret": MASK, "client_id": CLIENT_ID}])
async def test_approve_the_mask_or_a_blank_keeps_the_stored_secret(
    hass, hass_storage, aioclient_mock, hass_client_no_auth, qr_payloads, caplog, typed
):
    """Kept: the Store still holds the OLD secret, the client id stays, the
    new redirect URI is saved, and the next exchange sends the OLD secret."""
    caplog.set_level(logging.DEBUG)
    entry, result = await _redirect_changed_form(hass, hass_storage)
    result = await hass.config_entries.options.async_configure(result["flow_id"], typed)
    _saved(result, "approve")
    assert _stored(hass_storage, entry) == {"client_secret": SECRET}
    assert entry.data["tailscale_client_id"] == CLIENT_ID
    assert entry.data["tailscale_redirect_uri"] == REDIRECT

    aioclient_mock.post(tailscale_approve.TOKEN_URL, json={"access_token": ACCESS, "token_type": "Bearer",
                                                           "expires_in": 3600})
    result = await _start_enroll(hass, entry)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"phone_name": "p"})
    assert result["type"] is FlowResultType.EXTERNAL_STEP, result
    query = {k: v[0] for k, v in parse_qs(urlsplit(result["url"]).query).items()}
    assert (query["client_id"], query["redirect_uri"]) == (CLIENT_ID, REDIRECT)
    client = await hass_client_no_auth()
    resp = await client.get(tailscale_approve.CALLBACK_PATH, params={"code": CODE, "state": query["state"]})
    assert resp.status == 200
    result = await hass.config_entries.options.async_configure(result["flow_id"])
    assert result["step_id"] == "show_qr", result
    (_m, _url, form, _h), = aioclient_mock.mock_calls
    assert form["client_secret"] == SECRET and form["client_id"] == CLIENT_ID
    _assert_mask_nowhere(
        json.dumps(dict(entry.data)),
        json.dumps(_stored(hass_storage, entry)),
        json.dumps(await async_get_config_entry_diagnostics(hass, entry), default=str),
        _log_text(caplog),
    )


async def test_approve_a_new_secret_replaces_the_stored_one(hass, hass_storage):
    entry, result = await _redirect_changed_form(hass, hass_storage)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"client_secret": NEW_SECRET})
    _saved(result, "approve")
    assert _stored(hass_storage, entry) == {"client_secret": NEW_SECRET}
    assert entry.data["tailscale_client_id"] == NEW_CLIENT_ID


async def test_approve_keeping_with_a_wrong_client_id_is_refused(hass, hass_storage):
    entry, result = await _redirect_changed_form(hass, hass_storage)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"client_secret": MASK, "client_id": "someoneElse"})
    assert result["errors"] == {"client_id": "tailscale_client_id_invalid"}
    assert entry.data["tailscale_redirect_uri"] == OLD_REDIRECT
    assert _stored(hass_storage, entry) == {"client_secret": SECRET}
    assert _suggested(result, "client_secret") == MASK


@pytest.mark.parametrize("typed", [MASK + NEW_SECRET, "•" * 5])
async def test_approve_an_edited_mask_is_refused(hass, hass_storage, typed):
    entry, result = await _redirect_changed_form(hass, hass_storage)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"client_secret": typed})
    assert result["errors"] == {"client_secret": "secret_mask_edited"}
    assert entry.data["tailscale_redirect_uri"] == OLD_REDIRECT
    assert _stored(hass_storage, entry) == {"client_secret": SECRET}


async def test_approve_without_a_stored_secret_the_field_is_empty_and_required(hass, hass_storage):
    entry, result = await _fresh_approve_form(hass)
    assert type(_key(result, "client_secret")).__name__ == "Required"
    assert _suggested(result, "client_secret") is None
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"client_secret": MASK})
    assert result["errors"] == {"client_secret": "tailscale_client_secret_invalid"}
    assert "tailscale_mode" not in entry.data and _store_key(entry) not in hass_storage


async def test_the_one_time_token_is_never_prefilled(hass, hass_storage):
    """Stored nowhere, so never stars, even with an app secret in the Store."""
    _entry_, result = await _redirect_changed_form(hass, hass_storage, "tailscale_oauth_app_create")
    assert type(_key(result, "api_access_token")).__name__ == "Required"
    assert _suggested(result, "api_access_token") is None


# --- what the frontend receives (HA's real flow HTTP API) -----------------------------


async def _http_flow(hass, hass_client, entry, steps):
    """Drive the options flow through /api/config/config_entries/options/flow
    and return the last JSON result (the schema serialised by HA itself)."""
    assert await async_setup_component(hass, "config", {})
    # portico's manifest depends on file_upload; HA 2026.10 sets a dependency up lazily
    # when the flow handler loads, and file_upload registers a view, which fails once
    # hass_client() has frozen the router. Set it up first.
    assert await async_setup_component(hass, "file_upload", {})
    client = await hass_client()
    resp = await client.post("/api/config/config_entries/options/flow", json={"handler": entry.entry_id})
    assert resp.status == 200, await resp.text()
    data = await resp.json()
    for step in steps:
        resp = await client.post(f"/api/config/config_entries/options/flow/{data['flow_id']}", json=step)
        assert resp.status == 200, await resp.text()
        data = await resp.json()
    return data


def _field(data, name):
    (field,) = [f for f in data["data_schema"] if f["name"] == name]
    return field


def _assert_password(field):
    assert field["selector"] == {"text": field["selector"]["text"]}
    assert field["selector"]["text"]["type"] == "password"


CHANGE = [{"next_step_id": "tailscale_change_mode"}]


async def test_serialised_auto_form_with_a_stored_secret(hass, hass_client):
    entry = _entry(hass, tailscale_mode="auto", **CREDS)
    data = await _http_flow(hass, hass_client, entry, [*CHANGE, {"tailscale_mode": "auto"}])
    assert data["step_id"] == "tailscale_credentials", data
    field = _field(data, "tailscale_api_key")
    _assert_password(field)
    assert field["optional"] is True
    assert field["description"] == {"suggested_value": MASK}
    assert "default" not in field
    assert API_KEY not in json.dumps(data)
    assert _field(data, "tailscale_tailnet")["description"] == {"suggested_value": TAILNET}


async def test_serialised_auto_form_without_a_stored_secret(hass, hass_client):
    entry = _entry(hass)
    data = await _http_flow(hass, hass_client, entry, [{"next_step_id": "enroll_phone"}, {"tailscale_mode": "auto"}])
    field = _field(data, "tailscale_api_key")
    _assert_password(field)
    assert field["required"] is True
    assert "description" not in field and "default" not in field


async def test_serialised_approve_forms(hass, hass_client, hass_storage):
    _set_urls(hass, external=EXTERNAL)
    entry = _entry(hass, tailscale_mode="approve", tailscale_client_id=CLIENT_ID, tailscale_redirect_uri=OLD_REDIRECT)
    _put_secret(hass_storage, entry)
    data = await _http_flow(hass, hass_client, entry, [
        *CHANGE, {"tailscale_mode": "approve"}, {"next_step_id": "tailscale_oauth_app"}])
    assert data["step_id"] == "tailscale_oauth_app", data
    field = _field(data, "client_secret")
    _assert_password(field)
    assert field["optional"] is True and field["description"] == {"suggested_value": MASK}
    assert SECRET not in json.dumps(data)
    assert "description" not in _field(data, "client_id")

    other = _entry(hass)
    client = await hass_client()
    resp = await client.post("/api/config/config_entries/options/flow", json={"handler": other.entry_id})
    flow = await resp.json()
    for step in ({"next_step_id": "enroll_phone"}, {"tailscale_mode": "approve"}, {"next_step_id": "tailscale_oauth_app_create"}):
        resp = await client.post(f"/api/config/config_entries/options/flow/{flow['flow_id']}", json=step)
        flow = await resp.json()
    assert flow["step_id"] == "tailscale_oauth_app_create", flow
    field = _field(flow, "api_access_token")
    _assert_password(field)
    assert field["required"] is True and "description" not in field


async def test_serialised_submit_of_the_mask_keeps_the_secret(hass, hass_client):
    """The mask as the frontend would post it back, through the real API."""
    entry = _entry(hass, tailscale_mode="auto", **CREDS)
    data = await _http_flow(hass, hass_client, entry, [
        *CHANGE, {"tailscale_mode": "auto"},
        {"tailscale_api_key": MASK, "tailscale_tailnet": TAILNET, "tailscale_tag": TAG}])
    assert data["type"] == "abort" and data["reason"] == "tailscale_mode_saved_auto", data
    assert entry.data["tailscale_api_key"] == API_KEY
