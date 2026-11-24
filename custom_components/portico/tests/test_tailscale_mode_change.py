"""Changing the Tailscale enrollment mode of an existing device entry
(Alice, 2026-10-08: his entry holds `manual`, he wants `approve`, and removing
and re-adding the entry would disturb the live gate observer it also is).

With a complete answer, Configure opens a menu: "Enroll a device" (the old
path) or "Change the Tailscale enrollment mode". The change asks the same
question with the current mode preselected, reuses what is already stored
(the `approve` OAuth app while its redirect URI is still the current one;
`auto` shows its credentials step pre-filled, a blank secret keeping the
stored one: test_tailscale_auto_credentials.py), saves the mode only together with what it needs, and
ends the dialog with a confirmation. Switching away keeps the stored
credentials and the OAuth app's Store; abandoning changes nothing.

HA-harness session. Hermetic: every Tailscale call goes to `aioclient_mock`.
"""
from __future__ import annotations

import json
import logging

import pytest
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.portico import tailscale_approve
from custom_components.portico.config_flow import DOMAIN
from custom_components.portico.diagnostics import async_get_config_entry_diagnostics

from .test_enrollment_flow import _portico_users, _set_urls, qr_payloads, _qr_capture, _start_enroll  # noqa: F401 - fixtures
from .test_push_registry import _custom_integrations  # noqa: F401 - autouse fixture by name
from .phone_helpers import enrolled

EXTERNAL = "https://ha.example.org"
REDIRECT = EXTERNAL + tailscale_approve.CALLBACK_PATH
OLD_REDIRECT = "https://old-ha.example.org" + tailscale_approve.CALLBACK_PATH
CLIENT_ID = "kAbC123CNTRL"
SECRET = f"tskey-app-{CLIENT_ID}-SENTINELSECRETxyz0123456789"
NEW_CLIENT_ID = "nEw456CNTRL"
NEW_SECRET = f"tskey-app-{NEW_CLIENT_ID}-SENTINELNEWSECRET0123456789"
API_KEY = "tskey-api-SENTINEL-APIKEY-0123456789"
API_TOKEN = "tskey-api-SENTINEL-ADMIN-0123456789"
TAILNET = "example.com"
CREDS = {"tailscale_api_key": API_KEY, "tailscale_tailnet": TAILNET, "tailscale_tag": "tag:portico-phone"}
APP = {"tailscale_client_id": CLIENT_ID, "tailscale_redirect_uri": REDIRECT}
SECRETS = (SECRET, NEW_SECRET, "SENTINELSECRET", "SENTINELNEWSECRET", API_KEY, API_TOKEN)

MENU = ["push_settings", "push_phones", "push_test_phones", "push_key", "add_phone_registration", "enroll_phone",
        "tailscale_change_mode"]


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


def _assert_no_secret(text: str):
    for secret in SECRETS:
        assert secret not in text


async def _menu(hass, entry):
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU, result
    assert result["step_id"] == "init"
    assert result["menu_options"] == MENU
    return result


async def _change(hass, entry):
    """Configure > "Change the Tailscale enrollment mode": the question."""
    result = await _menu(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "tailscale_change_mode"})
    assert result["type"] is FlowResultType.FORM, result
    assert result["step_id"] == "tailscale_change_mode"
    return result


def _default(result):
    (key,) = result["data_schema"].schema
    assert str(key) == "tailscale_mode"
    return key.default()


def _saved(result, mode):
    assert result["type"] is FlowResultType.ABORT, result
    assert result["reason"] == f"tailscale_mode_saved_{mode}"


# --- the menu ----------------------------------------------------------------


@pytest.mark.parametrize("data", [
    {"tailscale_mode": "manual"},
    {"tailscale_mode": "auto", **CREDS},
    {**CREDS},  # a legacy entry: backfilled to auto, complete
    {"tailscale_mode": "approve", **APP},  # with its secret (below)
])
async def test_a_complete_answer_opens_the_menu(hass, hass_storage, data):
    entry = _entry(hass, **data)
    _put_secret(hass_storage, entry)
    await _menu(hass, entry)


@pytest.mark.parametrize("data", [
    {},  # fresh
    {"tailscale_mode": "auto"},  # the self-heal case
    {"tailscale_mode": "auto", "tailscale_api_key": API_KEY},
    {"tailscale_mode": "approve", **APP},  # no secret in the Store
    {"tailscale_mode": "approve", "tailscale_client_id": CLIENT_ID},
])
async def test_an_unfinished_answer_goes_straight_to_the_question(hass, hass_storage, data):
    entry = _entry(hass, **data)
    before = dict(entry.data)
    result = await _start_enroll(hass, entry)
    assert (result["type"], result["step_id"]) == (FlowResultType.FORM, "tailscale_mode")
    assert dict(entry.data) == before


async def test_enroll_a_phone_goes_where_it_always_went(hass, qr_payloads):
    _set_urls(hass, external=EXTERNAL)
    entry = _entry(hass, tailscale_mode="manual")
    result = await _menu(hass, entry)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "enroll_phone"})
    assert (result["type"], result["step_id"]) == (FlowResultType.FORM, "phone_details")
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"phone_name": "p"})
    assert result["step_id"] == "show_qr", result
    assert len(enrolled(hass)) == 1 and "tailscale_authkey" not in qr_payloads[0]


@pytest.mark.parametrize(("data", "current"), [
    ({"tailscale_mode": "manual"}, "manual"),
    ({"tailscale_mode": "auto", **CREDS}, "auto"),
    ({"tailscale_mode": "approve", **APP}, "approve"),
])
async def test_the_change_question_preselects_the_current_mode(hass, hass_storage, data, current):
    entry = _entry(hass, **data)
    _put_secret(hass_storage, entry)
    result = await _change(hass, entry)
    assert _default(result) == current
    (key,) = result["data_schema"].schema
    selector = result["data_schema"].schema[key]
    assert selector.config["options"] == ["approve", "auto", "manual"]
    assert selector.config["translation_key"] == "tailscale_mode"


# --- to approve -----------------------------------------------------------------


async def test_manual_to_approve_without_a_stored_app_sets_it_up(hass, hass_storage, caplog):
    caplog.set_level(logging.DEBUG)
    _set_urls(hass, external=EXTERNAL)
    entry = _entry(hass, tailscale_mode="manual")
    result = await _change(hass, entry)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"tailscale_mode": "approve"})
    assert (result["type"], result["step_id"]) == (FlowResultType.MENU, "tailscale_oauth_app_choice")
    assert result["description_placeholders"]["redirect_uri"] == REDIRECT
    assert entry.data["tailscale_mode"] == "manual"  # nothing yet
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "tailscale_oauth_app"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"client_secret": SECRET})
    _saved(result, "approve")

    assert entry.data["tailscale_mode"] == "approve"
    assert entry.data["tailscale_client_id"] == CLIENT_ID
    assert entry.data["tailscale_redirect_uri"] == REDIRECT
    assert _stored(hass_storage, entry) == {"client_secret": SECRET}
    # The dialog ended: no phone, no user.
    assert enrolled(hass) == [] and _portico_users(hass) == []
    _assert_no_secret(json.dumps(dict(entry.data)))
    _assert_no_secret(json.dumps(await async_get_config_entry_diagnostics(hass, entry), default=str))
    _assert_no_secret(_log_text(caplog))

    # The next Configure opens the menu, and enrolling uses approve.
    result = await _menu(hass, entry)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "enroll_phone"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"phone_name": "p"})
    assert (result["type"], result["step_id"]) == (FlowResultType.EXTERNAL_STEP, "tailscale_approve")


async def test_manual_to_approve_through_create_the_app(hass, hass_storage, aioclient_mock, caplog):
    caplog.set_level(logging.DEBUG)
    _set_urls(hass, external=EXTERNAL)
    aioclient_mock.post(tailscale_approve.OAUTH_APPS_URL, json={"id": CLIENT_ID, "clientSecret": SECRET})
    entry = _entry(hass, tailscale_mode="manual")
    result = await _change(hass, entry)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"tailscale_mode": "approve"})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "tailscale_oauth_app_create"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"api_access_token": API_TOKEN})
    _saved(result, "approve")
    (_m, _url, body, _h), = aioclient_mock.mock_calls
    assert body["redirectUris"] == [REDIRECT]
    assert (entry.data["tailscale_mode"], entry.data["tailscale_client_id"]) == ("approve", CLIENT_ID)
    assert _stored(hass_storage, entry) == {"client_secret": SECRET}
    assert API_TOKEN not in json.dumps(hass_storage, default=str)
    _assert_no_secret(json.dumps(dict(entry.data)))
    _assert_no_secret(_log_text(caplog))


async def test_manual_to_approve_with_the_app_still_stored_asks_nothing(hass, hass_storage, aioclient_mock):
    """Switching back to approve: the app (secret in its Store, client id and
    a redirect URI that is still the current one) is reused as is."""
    _set_urls(hass, external=EXTERNAL)
    entry = _entry(hass, tailscale_mode="manual", **APP)
    _put_secret(hass_storage, entry)
    result = await _change(hass, entry)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"tailscale_mode": "approve"})
    _saved(result, "approve")
    assert {k: entry.data[k] for k in ("tailscale_mode", *APP)} == {"tailscale_mode": "approve", **APP}
    assert _stored(hass_storage, entry) == {"client_secret": SECRET}
    assert aioclient_mock.call_count == 0


async def test_a_changed_https_url_asks_for_the_app_again(hass, hass_storage):
    """The registered redirect URI is the OLD one: Tailscale would refuse it,
    so the app setup is asked again, saying why, with the current URI."""
    _set_urls(hass, external=EXTERNAL)
    entry = _entry(hass, tailscale_mode="manual", tailscale_client_id=CLIENT_ID, tailscale_redirect_uri=OLD_REDIRECT)
    _put_secret(hass_storage, entry)
    result = await _change(hass, entry)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"tailscale_mode": "approve"})
    assert result["type"] is FlowResultType.MENU, result
    assert result["step_id"] == "tailscale_oauth_app_choice_redirect_changed"
    assert result["menu_options"] == ["tailscale_oauth_app", "tailscale_oauth_app_create"]
    assert result["description_placeholders"]["redirect_uri"] == REDIRECT
    assert result["description_placeholders"]["old_redirect_uri"] == OLD_REDIRECT
    assert entry.data["tailscale_mode"] == "manual"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "tailscale_oauth_app"})
    assert result["description_placeholders"]["redirect_uri"] == REDIRECT
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"client_secret": NEW_SECRET})
    _saved(result, "approve")
    assert entry.data["tailscale_redirect_uri"] == REDIRECT
    assert entry.data["tailscale_client_id"] == NEW_CLIENT_ID
    assert _stored(hass_storage, entry) == {"client_secret": NEW_SECRET}


async def test_approve_to_approve_with_a_changed_url_asks_again(hass, hass_storage):
    _set_urls(hass, external=EXTERNAL)
    entry = _entry(hass, tailscale_mode="approve", tailscale_client_id=CLIENT_ID, tailscale_redirect_uri=OLD_REDIRECT)
    _put_secret(hass_storage, entry)
    result = await _change(hass, entry)
    assert _default(result) == "approve"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"tailscale_mode": "approve"})
    assert result["step_id"] == "tailscale_oauth_app_choice_redirect_changed"
    # Abandoned there: the old app stays exactly as it was.
    hass.config_entries.options.async_abort(result["flow_id"])
    assert entry.data["tailscale_redirect_uri"] == OLD_REDIRECT
    assert _stored(hass_storage, entry) == {"client_secret": SECRET}


async def test_approve_needs_an_https_url(hass, hass_storage):
    _set_urls(hass, internal="http://ha.lan:8123")
    entry = _entry(hass, tailscale_mode="manual", **APP)
    _put_secret(hass_storage, entry)
    result = await _change(hass, entry)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"tailscale_mode": "approve"})
    assert (result["step_id"], result["errors"]) == ("tailscale_change_mode", {"base": "no_https_url"})
    assert entry.data["tailscale_mode"] == "manual"


# --- to auto ------------------------------------------------------------------


async def _keep_auto_credentials(hass, result):
    """`auto` always shows its credentials step (2026-10-08), pre-filled; a
    blank secret keeps the stored one."""
    assert (result["type"], result["step_id"]) == (FlowResultType.FORM, "tailscale_credentials"), result
    return await hass.config_entries.options.async_configure(
        result["flow_id"], {k: v for k, v in CREDS.items() if k != "tailscale_api_key"})


async def test_manual_to_auto_with_stored_credentials_keeps_them(hass, aioclient_mock):
    entry = _entry(hass, tailscale_mode="manual", **CREDS)
    result = await _change(hass, entry)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"tailscale_mode": "auto"})
    _saved(await _keep_auto_credentials(hass, result), "auto")
    assert entry.data["tailscale_mode"] == "auto"
    assert {k: entry.data[k] for k in CREDS} == CREDS
    assert aioclient_mock.call_count == 0


@pytest.mark.parametrize("partial", [{}, {"tailscale_tailnet": TAILNET}, {"tailscale_api_key": " ", "tailscale_tailnet": TAILNET}])
async def test_manual_to_auto_without_credentials_asks_for_them(hass, caplog, partial):
    caplog.set_level(logging.DEBUG)
    entry = _entry(hass, tailscale_mode="manual", **partial)
    result = await _change(hass, entry)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"tailscale_mode": "auto"})
    assert result["step_id"] == "tailscale_credentials", result
    assert entry.data["tailscale_mode"] == "manual"  # pending on the flow only
    result = await hass.config_entries.options.async_configure(result["flow_id"], dict(CREDS))
    _saved(result, "auto")
    assert entry.data["tailscale_mode"] == "auto"
    assert {k: entry.data[k] for k in CREDS} == CREDS
    assert enrolled(hass) == [] and _portico_users(hass) == []
    _assert_no_secret(_log_text(caplog))


async def test_approve_to_auto_keeps_the_oauth_app(hass, hass_storage):
    _set_urls(hass, external=EXTERNAL)
    entry = _entry(hass, tailscale_mode="approve", **APP, **CREDS)
    _put_secret(hass_storage, entry)
    result = await _change(hass, entry)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"tailscale_mode": "auto"})
    _saved(await _keep_auto_credentials(hass, result), "auto")
    assert entry.data["tailscale_mode"] == "auto"
    assert _stored(hass_storage, entry) == {"client_secret": SECRET}
    assert {k: entry.data[k] for k in APP} == APP


# --- to manual ----------------------------------------------------------------


async def test_auto_to_manual_keeps_the_credentials(hass):
    entry = _entry(hass, tailscale_mode="auto", **CREDS)
    result = await _change(hass, entry)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"tailscale_mode": "manual"})
    _saved(result, "manual")
    assert entry.data["tailscale_mode"] == "manual"
    assert {k: entry.data[k] for k in CREDS} == CREDS

    # And back again: the stored credentials are kept.
    result = await _change(hass, entry)
    assert _default(result) == "manual"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"tailscale_mode": "auto"})
    _saved(await _keep_auto_credentials(hass, result), "auto")
    assert {k: entry.data[k] for k in CREDS} == CREDS


async def test_approve_to_manual_keeps_the_store_and_can_switch_back(hass, hass_storage):
    _set_urls(hass, external=EXTERNAL)
    entry = _entry(hass, tailscale_mode="approve", **APP)
    _put_secret(hass_storage, entry)
    result = await _change(hass, entry)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"tailscale_mode": "manual"})
    _saved(result, "manual")
    assert entry.data["tailscale_mode"] == "manual"
    assert _stored(hass_storage, entry) == {"client_secret": SECRET}

    result = await _change(hass, entry)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"tailscale_mode": "approve"})
    _saved(result, "approve")


@pytest.mark.parametrize("mode", ["manual", "auto", "approve"])
async def test_choosing_the_current_mode_again_changes_nothing(hass, hass_storage, aioclient_mock, mode):
    _set_urls(hass, external=EXTERNAL)
    entry = _entry(hass, tailscale_mode=mode, **APP, **CREDS)
    _put_secret(hass_storage, entry)
    before = dict(entry.data)
    result = await _change(hass, entry)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"tailscale_mode": mode})
    if mode == "auto":
        result = await _keep_auto_credentials(hass, result)
    _saved(result, mode)
    assert dict(entry.data) == before
    assert _stored(hass_storage, entry) == {"client_secret": SECRET}
    assert aioclient_mock.call_count == 0


# --- abandoning ---------------------------------------------------------------


@pytest.mark.parametrize(("start", "steps"), [
    ("manual", []),  # on the question
    ("manual", [{"tailscale_mode": "approve"}]),  # on the app choice
    ("manual", [{"tailscale_mode": "approve"}, {"next_step_id": "tailscale_oauth_app"}]),
    ("manual", [{"tailscale_mode": "approve"}, {"next_step_id": "tailscale_oauth_app_create"}]),
    ("manual", [{"tailscale_mode": "approve"}, {"next_step_id": "tailscale_oauth_app"},
                {"client_secret": "tskey-auth-wrong-kind"}]),  # a refused secret
    ("manual", [{"tailscale_mode": "auto"}]),  # on the credentials
    ("approve", [{"tailscale_mode": "auto"}]),
    ("approve-old-uri", [{"tailscale_mode": "approve"}]),
])
async def test_abandoning_anywhere_changes_nothing(hass, hass_storage, aioclient_mock, start, steps):
    _set_urls(hass, external=EXTERNAL)
    if start == "approve-old-uri":
        entry = _entry(hass, tailscale_mode="approve", tailscale_client_id=CLIENT_ID,
                       tailscale_redirect_uri=OLD_REDIRECT)
        _put_secret(hass_storage, entry)
    elif start == "approve":
        entry = _entry(hass, tailscale_mode="approve", **APP)
        _put_secret(hass_storage, entry)
    else:
        entry = _entry(hass, tailscale_mode=start)
    before, stored_before = dict(entry.data), _stored(hass_storage, entry)
    result = await _change(hass, entry)
    for step in steps:
        result = await hass.config_entries.options.async_configure(result["flow_id"], step)
        assert result["type"] in (FlowResultType.FORM, FlowResultType.MENU), result
    hass.config_entries.options.async_abort(result["flow_id"])
    assert dict(entry.data) == before
    assert _stored(hass_storage, entry) == stored_before
    assert aioclient_mock.call_count == 0
    # The answer is still the old, complete one: the menu again.
    await _menu(hass, entry)


async def test_removing_the_entry_still_removes_the_store_after_a_switch(hass, hass_storage):
    from custom_components.portico import async_remove_entry

    _set_urls(hass, external=EXTERNAL)
    entry = _entry(hass, tailscale_mode="approve", **APP)
    _put_secret(hass_storage, entry)
    result = await _change(hass, entry)
    _saved(await hass.config_entries.options.async_configure(result["flow_id"], {"tailscale_mode": "manual"}),
           "manual")
    await async_remove_entry(hass, entry)
    assert await tailscale_approve.OAuthAppStore(hass, entry.entry_id).async_load_secret() is None


async def test_the_manual_qr_note_promises_no_key_field(hass, qr_payloads):
    """portico-app has no field to type a Tailscale key in: the manual QR page
    says the phone does not join the tailnet through Portico."""
    _set_urls(hass, external=EXTERNAL)
    entry = _entry(hass, tailscale_mode="manual")
    result = await _menu(hass, entry)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "enroll_phone"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"phone_name": "p"})
    note = result["description_placeholders"]["manual_note"]
    assert "Setup screen" not in note and "paste" not in note.lower()
    assert "no Tailscale key" in note and "official Tailscale app" in note
    assert "Change the Tailscale enrollment mode" in note
