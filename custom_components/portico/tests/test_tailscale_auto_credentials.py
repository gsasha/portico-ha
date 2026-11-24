"""Editing the `auto` credentials from the UI (2026-10-08, live).

The installer chose `auto`, typed a client secret and left the pre-filled tag
`tag:intercom-phone` (the old intercom app's tag) while his OAuth client only
allows `tag:portico-phone`. Tailscale refused every key (HTTP 400, tag "not
permitted"), and the UI could not fix it: "Change the Tailscale enrollment
mode" > auto reused the complete stored credentials and never showed the
credentials form again.

Now choosing `auto` under "Change the Tailscale enrollment mode" always shows
the credentials step: tailnet and tag pre-filled with the stored values, the
secret optional while one is stored (blank keeps it, a typed one replaces it).
The mode and the three fields are written in one `async_update_entry` call on
submit; abandoning changes nothing. The tag must look like `tag:<letters,
digits, dashes>`, the tailnet and tag must not be blank, and every field is
stripped. A new answer's default tag is `tag:portico-phone`.

A 4xx from Tailscale's key request or token exchange logs one extra warning
with the status and Tailscale's own `message` (JSON string only), redacted and
truncated, so the next failure says why.

HA-harness session. Hermetic: every Tailscale call goes to `aioclient_mock`.
"""
from __future__ import annotations

import json
import logging

import pytest
import voluptuous as vol
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.portico import enrollment_flow
from custom_components.portico.config_flow import DOMAIN
from custom_components.portico.diagnostics import async_get_config_entry_diagnostics

from .test_enrollment_flow import _portico_users, _set_urls, qr_payloads, _qr_capture, _start_enroll  # noqa: F401 - fixtures
from .test_push_registry import _custom_integrations  # noqa: F401 - autouse fixture by name

EXTERNAL = "https://ha.example.org"
CLIENT_ID = "kSENTINELCID"
SECRET = f"tskey-client-{CLIENT_ID}-SENTINELCLIENTSECRET0123456789"
NEW_SECRET = f"tskey-client-{CLIENT_ID}-SENTINELNEWSECRET0123456789"
ACCESS_TOKEN = "tskey-SENTINEL-ACCESS-TOKEN-0123456789"
TAILNET = "-"
OLD_TAG = "tag:intercom-phone"
NEW_TAG = "tag:portico-phone"
MINT_URL = f"{enrollment_flow.TAILSCALE_API_BASE}/tailnet/{TAILNET}/keys"
TOKEN_URL = "https://api.tailscale.com/api/v2/oauth/token"
MINTED = "tskey-auth-MINTED-0123456789"
# The live entry, 2026-10-08.
LIVE = {"tailscale_mode": "auto", "tailscale_api_key": SECRET, "tailscale_tailnet": TAILNET,
        "tailscale_tag": OLD_TAG}
SECRETS = (SECRET, NEW_SECRET, "SENTINELCLIENTSECRET", "SENTINELNEWSECRET", ACCESS_TOKEN)
FIELDS = ("tailscale_api_key", "tailscale_tailnet", "tailscale_tag")


def _entry(hass, **data) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, title="Portico push ring notification", unique_id="push",
        data={"entry_type": "push", "project_id": "portico-test", **data},
    )
    entry.add_to_hass(hass)
    return entry


def _key(result, field):
    for key in result["data_schema"].schema:
        if key == field:
            return key
    raise AssertionError(f"no {field} in the form")


def _suggested(result, field):
    return (_key(result, field).description or {}).get("suggested_value")


def _has_default(key) -> bool:
    return key.default is not vol.UNDEFINED


def _log_text(caplog) -> str:
    return "\n".join(
        r.getMessage() for r in caplog.records
        if not r.name.startswith(("pytest_homeassistant_custom_component", "aiohttp.access"))
    )


def _assert_no_secret(text: str):
    for secret in SECRETS:
        assert secret not in text


async def _change_to_auto(hass, entry):
    """Configure > "Change the Tailscale enrollment mode" > auto."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert (result["type"], result["step_id"]) == (FlowResultType.MENU, "init"), result
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "tailscale_change_mode"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"tailscale_mode": "auto"})
    assert (result["type"], result["step_id"]) == (FlowResultType.FORM, "tailscale_credentials"), result
    return result


def _saved(result):
    assert result["type"] is FlowResultType.ABORT, result
    assert result["reason"] == "tailscale_mode_saved_auto"


# --- the form, with stored credentials ---------------------------------------


@pytest.mark.parametrize("mode", ["auto", "manual"])
async def test_auto_with_stored_credentials_shows_them_prefilled(hass, mode):
    entry = _entry(hass, **{**LIVE, "tailscale_mode": mode})
    before = dict(entry.data)
    result = await _change_to_auto(hass, entry)
    assert _suggested(result, "tailscale_tailnet") == TAILNET
    assert _suggested(result, "tailscale_tag") == OLD_TAG  # the stored one, not the new default
    assert not _has_default(_key(result, "tailscale_tag"))
    # A stored secret: the field is optional, never offered back, showing
    # the stars (2026-10-09, test_tailscale_secret_mask.py).
    secret = _key(result, "tailscale_api_key")
    assert isinstance(secret, vol.Optional)
    assert _suggested(result, "tailscale_api_key") == enrollment_flow.SECRET_MASK and not _has_default(secret)
    _assert_no_secret(json.dumps(result.get("description_placeholders") or {}))
    assert dict(entry.data) == before  # nothing written by showing it


async def test_a_blank_secret_keeps_the_stored_one(hass):
    entry = _entry(hass, **LIVE)
    result = await _change_to_auto(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tailscale_tailnet": TAILNET, "tailscale_tag": NEW_TAG})
    _saved(result)
    assert entry.data["tailscale_api_key"] == SECRET
    assert entry.data["tailscale_tag"] == NEW_TAG


async def test_a_whitespace_secret_keeps_the_stored_one(hass):
    entry = _entry(hass, **LIVE)
    result = await _change_to_auto(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tailscale_api_key": "   ", "tailscale_tailnet": TAILNET, "tailscale_tag": OLD_TAG})
    _saved(result)
    assert {k: entry.data[k] for k in LIVE} == LIVE


async def test_a_typed_secret_replaces_the_stored_one(hass, caplog):
    caplog.set_level(logging.DEBUG)
    entry = _entry(hass, **LIVE)
    result = await _change_to_auto(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"tailscale_api_key": f"  {NEW_SECRET}\n", "tailscale_tailnet": f" {TAILNET} ", "tailscale_tag": NEW_TAG})
    _saved(result)
    assert entry.data["tailscale_api_key"] == NEW_SECRET  # stripped
    assert entry.data["tailscale_tailnet"] == TAILNET
    assert SECRET not in json.dumps(dict(entry.data))
    _assert_no_secret(_log_text(caplog))


async def test_the_edited_tag_is_saved_and_the_next_mint_uses_it(hass, qr_payloads, aioclient_mock):
    """The live fix: tag:intercom-phone -> tag:portico-phone, then enroll."""
    _set_urls(hass, external=EXTERNAL)
    entry = _entry(hass, **LIVE)
    result = await _change_to_auto(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tailscale_tailnet": TAILNET, "tailscale_tag": f" {NEW_TAG} "})
    _saved(result)
    assert entry.data["tailscale_tag"] == NEW_TAG
    assert aioclient_mock.call_count == 0  # saving calls nothing

    aioclient_mock.post(TOKEN_URL, json={"access_token": ACCESS_TOKEN})
    aioclient_mock.post(MINT_URL, json={"key": MINTED})
    result = await _start_enroll(hass, entry)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"phone_name": "p"})
    assert result["step_id"] == "show_qr", result
    (_m1, _u1, form, _h1), (_m2, url, body, _h2) = aioclient_mock.mock_calls
    assert form["client_secret"] == SECRET
    assert str(url) == MINT_URL
    assert body["capabilities"]["devices"]["create"]["tags"] == [NEW_TAG]
    assert qr_payloads[0]["tailscale_authkey"] == MINTED


async def test_the_mode_and_credentials_are_one_write(hass, monkeypatch):
    entry = _entry(hass, tailscale_mode="manual", **{k: LIVE[k] for k in FIELDS})
    result = await _change_to_auto(hass, entry)
    writes: list[dict] = []
    real = hass.config_entries.async_update_entry

    def _spy(e, **kwargs):
        if e is entry:
            writes.append(dict(kwargs.get("data") or {}))
        return real(e, **kwargs)

    monkeypatch.setattr(hass.config_entries, "async_update_entry", _spy)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tailscale_api_key": NEW_SECRET, "tailscale_tailnet": "example.com",
                            "tailscale_tag": NEW_TAG})
    _saved(result)
    assert len(writes) == 1
    assert {k: writes[0][k] for k in ("tailscale_mode", *FIELDS)} == {
        "tailscale_mode": "auto", "tailscale_api_key": NEW_SECRET, "tailscale_tailnet": "example.com",
        "tailscale_tag": NEW_TAG}


async def test_nothing_new_is_stored_and_secrets_stay_out_of_diagnostics_and_logs(hass, caplog):
    caplog.set_level(logging.DEBUG)
    entry = _entry(hass, **LIVE)
    keys_before = set(entry.data)
    result = await _change_to_auto(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tailscale_api_key": NEW_SECRET, "tailscale_tailnet": TAILNET, "tailscale_tag": NEW_TAG})
    _saved(result)
    assert set(entry.data) == keys_before
    # The secret is where it always was (tailscale_api_key) and nowhere else.
    others = {k: v for k, v in entry.data.items() if k != "tailscale_api_key"}
    _assert_no_secret(json.dumps(others))
    _assert_no_secret(json.dumps(await async_get_config_entry_diagnostics(hass, entry), default=str))
    _assert_no_secret(_log_text(caplog))


# --- refused answers -----------------------------------------------------------


@pytest.mark.parametrize("tag", [
    "portico-phone", "tag:", "tag:portico phone", "tag:portico_phone", "tag:a,tag:b", "tag:pörtico",
    "TAG:portico-phone", "tag:portico-phone\ntag:x", "tag::x",
])
async def test_a_tag_of_the_wrong_shape_is_refused(hass, tag):
    entry = _entry(hass, **LIVE)
    before = dict(entry.data)
    result = await _change_to_auto(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tailscale_tailnet": TAILNET, "tailscale_tag": tag})
    assert (result["type"], result["step_id"]) == (FlowResultType.FORM, "tailscale_credentials"), result
    assert result["errors"] == {"tailscale_tag": "tailscale_tag_invalid"}
    assert dict(entry.data) == before


@pytest.mark.parametrize(("user_input", "errors"), [
    ({"tailscale_tailnet": "   ", "tailscale_tag": NEW_TAG}, {"tailscale_tailnet": "tailscale_tailnet_blank"}),
    ({"tailscale_tailnet": "", "tailscale_tag": NEW_TAG}, {"tailscale_tailnet": "tailscale_tailnet_blank"}),
    ({"tailscale_tailnet": TAILNET, "tailscale_tag": "  "}, {"tailscale_tag": "tailscale_tag_blank"}),
    ({"tailscale_tailnet": TAILNET}, {"tailscale_tag": "tailscale_tag_blank"}),  # the field cleared
])
async def test_a_blank_tailnet_or_tag_is_refused(hass, user_input, errors):
    entry = _entry(hass, **LIVE)
    before = dict(entry.data)
    result = await _change_to_auto(hass, entry)
    result = await hass.config_entries.options.async_configure(result["flow_id"], user_input)
    assert (result["step_id"], result["errors"]) == ("tailscale_credentials", errors)
    assert dict(entry.data) == before


async def test_a_refused_form_keeps_what_was_typed_but_never_the_secret(hass):
    entry = _entry(hass, **LIVE)
    result = await _change_to_auto(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tailscale_api_key": NEW_SECRET, "tailscale_tailnet": "example.com",
                            "tailscale_tag": "portico-phone"})
    assert result["errors"] == {"tailscale_tag": "tailscale_tag_invalid"}
    assert _suggested(result, "tailscale_tailnet") == "example.com"
    assert _suggested(result, "tailscale_tag") == "portico-phone"
    assert _suggested(result, "tailscale_api_key") == enrollment_flow.SECRET_MASK  # the stars, not what was typed


@pytest.mark.parametrize("steps", [
    [],  # on the form
    [{"tailscale_tailnet": TAILNET, "tailscale_tag": "bad"}],  # after a refusal
    [{"tailscale_api_key": NEW_SECRET, "tailscale_tailnet": " ", "tailscale_tag": NEW_TAG}],
])
async def test_abandoning_the_form_changes_nothing(hass, aioclient_mock, steps):
    entry = _entry(hass, **LIVE)
    before = dict(entry.data)
    result = await _change_to_auto(hass, entry)
    for step in steps:
        result = await hass.config_entries.options.async_configure(result["flow_id"], step)
        assert result["type"] is FlowResultType.FORM, result
    hass.config_entries.options.async_abort(result["flow_id"])
    assert dict(entry.data) == before
    assert aioclient_mock.call_count == 0


# --- no secret stored ----------------------------------------------------------


@pytest.mark.parametrize("stored", [
    {"tailscale_tailnet": TAILNET, "tailscale_tag": OLD_TAG},
    {"tailscale_api_key": "  ", "tailscale_tailnet": TAILNET, "tailscale_tag": OLD_TAG},
])
async def test_without_a_stored_secret_the_secret_is_required(hass, stored):
    entry = _entry(hass, tailscale_mode="manual", **stored)
    result = await _change_to_auto(hass, entry)
    assert isinstance(_key(result, "tailscale_api_key"), vol.Required)
    assert _suggested(result, "tailscale_tailnet") == TAILNET
    assert _suggested(result, "tailscale_tag") == OLD_TAG
    # Whitespace only is no secret either.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tailscale_api_key": "   ", "tailscale_tailnet": TAILNET, "tailscale_tag": NEW_TAG})
    assert result["errors"] == {"tailscale_api_key": "tailscale_api_key_blank"}
    assert entry.data["tailscale_mode"] == "manual"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tailscale_api_key": SECRET, "tailscale_tailnet": TAILNET, "tailscale_tag": NEW_TAG})
    _saved(result)
    assert (entry.data["tailscale_api_key"], entry.data["tailscale_tag"]) == (SECRET, NEW_TAG)


# --- the first time --------------------------------------------------------------


async def test_the_first_time_form_is_unchanged_but_suggests_the_portico_tag(hass):
    entry = _entry(hass)
    result = await _start_enroll(hass, entry)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"tailscale_mode": "auto"})
    assert (result["type"], result["step_id"]) == (FlowResultType.FORM, "tailscale_credentials"), result
    assert isinstance(_key(result, "tailscale_api_key"), vol.Required)
    assert isinstance(_key(result, "tailscale_tailnet"), vol.Required)
    tag = _key(result, "tailscale_tag")
    assert tag.default() == NEW_TAG
    for field in FIELDS:
        assert _suggested(result, field) is None
    # The tag left at its default.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tailscale_api_key": f" {SECRET} ", "tailscale_tailnet": f"{TAILNET} "})
    assert result["step_id"] == "phone_details", result
    assert {k: entry.data[k] for k in ("tailscale_mode", *FIELDS)} == {
        "tailscale_mode": "auto", "tailscale_api_key": SECRET, "tailscale_tailnet": TAILNET,
        "tailscale_tag": NEW_TAG}


async def test_the_first_time_form_refuses_a_bad_tag_too(hass):
    entry = _entry(hass)
    result = await _start_enroll(hass, entry)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"tailscale_mode": "auto"})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"tailscale_api_key": SECRET, "tailscale_tailnet": TAILNET, "tailscale_tag": "phones"})
    assert (result["step_id"], result["errors"]) == ("tailscale_credentials", {"tailscale_tag": "tailscale_tag_invalid"})
    assert "tailscale_mode" not in entry.data


async def test_an_existing_entry_keeps_its_stored_tag(hass, qr_payloads, aioclient_mock):
    """The new default is for new answers only: an entry that stores the old
    tag mints with it until the installer edits it."""
    _set_urls(hass, external=EXTERNAL)
    aioclient_mock.post(TOKEN_URL, json={"access_token": ACCESS_TOKEN})
    aioclient_mock.post(MINT_URL, json={"key": MINTED})
    entry = _entry(hass, **LIVE)
    result = await _start_enroll(hass, entry)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"phone_name": "p"})
    assert result["step_id"] == "show_qr", result
    assert aioclient_mock.mock_calls[1][2]["capabilities"]["devices"]["create"]["tags"] == [OLD_TAG]
    assert entry.data["tailscale_tag"] == OLD_TAG


# --- what a refusal logs ------------------------------------------------------


async def _enroll(hass, entry):
    result = await _start_enroll(hass, entry)
    return await hass.config_entries.options.async_configure(result["flow_id"], {"phone_name": "p"})


def _refusal_lines(caplog) -> list[str]:
    return [
        r.getMessage() for r in caplog.records
        if r.name == enrollment_flow.__name__ and r.levelno == logging.WARNING
        and r.getMessage().startswith("Portico: Tailscale's answer to the")
    ]


TAG_MESSAGE = "requested tags [tag:x] are invalid or not permitted"


async def test_a_refused_key_request_logs_tailscales_message(hass, qr_payloads, aioclient_mock, caplog):
    _set_urls(hass, external=EXTERNAL)
    aioclient_mock.post(TOKEN_URL, json={"access_token": ACCESS_TOKEN})
    aioclient_mock.post(MINT_URL, status=400, json={"message": TAG_MESSAGE})
    result = await _enroll(hass, _entry(hass, **LIVE))
    # The form error is the same as before.
    assert (result["step_id"], result["errors"]) == ("phone_details", {"base": "tailscale_mint_failed"})
    assert _portico_users(hass) == [] and qr_payloads == []
    (line,) = _refusal_lines(caplog)
    assert line == f"Portico: Tailscale's answer to the key request (HTTP status 400): {TAG_MESSAGE}"
    _assert_no_secret(_log_text(caplog))


async def test_a_refused_token_exchange_logs_tailscales_message(hass, aioclient_mock, caplog):
    _set_urls(hass, external=EXTERNAL)
    aioclient_mock.post(TOKEN_URL, status=401, json={"message": "invalid client credentials"})
    result = await _enroll(hass, _entry(hass, **LIVE))
    assert (result["step_id"], result["errors"]) == ("phone_details", {"base": "tailscale_mint_failed"})
    (line,) = _refusal_lines(caplog)
    assert line == "Portico: Tailscale's answer to the token exchange (HTTP status 401): invalid client credentials"
    assert "TailscaleRejected" in caplog.text


@pytest.mark.parametrize("url", [MINT_URL, TOKEN_URL])
async def test_secrets_in_tailscales_message_are_redacted(hass, aioclient_mock, caplog, url):
    caplog.set_level(logging.DEBUG)
    _set_urls(hass, external=EXTERNAL)
    message = (f"bad credential {SECRET} or key tskey-auth-kSOMETHING-SENTINELAUTHKEY_99, "
               f"Authorization: Bearer SENTINELBEARERTOKENabc.def-ghi and {ACCESS_TOKEN}, "
               "token SENTINELOPAQUEaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
    if url == TOKEN_URL:
        aioclient_mock.post(TOKEN_URL, status=400, json={"message": message})
    else:
        aioclient_mock.post(TOKEN_URL, json={"access_token": ACCESS_TOKEN})
        aioclient_mock.post(MINT_URL, status=403, json={"message": message})
    await _enroll(hass, _entry(hass, **LIVE))
    (line,) = _refusal_lines(caplog)
    assert "[redacted]" in line and "bad credential" in line
    text = _log_text(caplog)
    for leaked in (*SECRETS, "tskey-", "SENTINELAUTHKEY", "SENTINELBEARERTOKEN", "SENTINELOPAQUE"):
        assert leaked not in text, leaked


async def test_a_long_message_is_truncated(hass, aioclient_mock, caplog):
    _set_urls(hass, external=EXTERNAL)
    aioclient_mock.post(TOKEN_URL, json={"access_token": ACCESS_TOKEN})
    aioclient_mock.post(MINT_URL, status=400, json={"message": "word " * 200})
    await _enroll(hass, _entry(hass, **LIVE))
    (line,) = _refusal_lines(caplog)
    logged = line.split("): ", 1)[1]
    assert 0 < len(logged) <= 200


@pytest.mark.parametrize("response", [
    {"status": 400, "text": "SENTINEL-BODY not json"},
    {"status": 400, "json": {"error": "SENTINEL-BODY"}},
    {"status": 400, "json": {"message": {"nested": "SENTINEL-BODY"}}},
    {"status": 400, "json": ["SENTINEL-BODY"]},
    {"status": 400, "json": {"message": 42}},
    {"status": 500, "json": {"message": "SENTINEL-BODY server"}},  # 4xx only
])
async def test_any_other_answer_logs_nothing_extra(hass, aioclient_mock, caplog, response):
    _set_urls(hass, external=EXTERNAL)
    aioclient_mock.post(TOKEN_URL, json={"access_token": ACCESS_TOKEN})
    aioclient_mock.post(MINT_URL, **response)
    result = await _enroll(hass, _entry(hass, **LIVE))
    assert result["errors"] == {"base": "tailscale_mint_failed"}
    assert _refusal_lines(caplog) == []
    assert "SENTINEL-BODY" not in caplog.text
    assert f"HTTP status {response['status']}" in caplog.text  # the existing line
