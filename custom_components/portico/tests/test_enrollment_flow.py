"""The phone enrollment options flow's QR payload (§4.7): the `portico`
format marker, the choice of `ha_url` (https first; plain http only when the
installer ticks the box), the refusal to mint a QR without a URL the phone can
use, the push ring notification `fcm` block (sender design §5.1) and the QR text itself
(compact, UTF-8, unescaped).

HA-harness session. The flow runs through HA's real options-flow manager;
`tailscale_mode: manual` keeps it off Tailscale's API, and the QR builder is
wrapped so the test reads the exact text the QR would carry.
"""
from __future__ import annotations

import json
import logging
import re
import threading

import pytest
import qrcode
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.portico import enrollment_flow
from custom_components.portico.config_flow import DOMAIN

from .test_push_registry import (
    _custom_integrations,  # noqa: F401 - autouse fixture by name
)
from .phone_helpers import enrolled

EXTERNAL = "https://ha.example.org"
INTERNAL = "http://ha.lan:8123"
EXTERNAL_HTTP = "http://ha.example.org:8123"
INTERNAL_HTTPS = "https://ha.lan:8443"

# The four Firebase client values (not secret, sender design §5.1). Fakes in
# the shape portico-app's FirebaseConfig.create accepts.
FIREBASE = {
    "project_id": "portico-test",
    "sender_id": "123456789012",
    "app_id": "1:123456789012:android:0123abcd4567ef89",
    "api_key": "AIzaSyFAKE-not-a-real-key_0123456789abc",
}
# What else a push entry holds; none of it may reach the QR.
SA_EMAIL = "sentinel-sa@portico-test.iam.gserviceaccount.example"
SA_KEY_ID = "SENTINEL-PRIVATE-KEY-ID"
HEBREW_NAME = "הטלפון של יעל בן דוד אהרוני"
# A Hebrew name inside the 24-character limit (UI split P2: the limit was 40); HEBREW_NAME above is only a payload-size fixture.
HEBREW_NAME_SHORT = "הטלפון של יעל"
# What auto-detection yields on pytest-homeassistant's own mocked network
# (mock_network / mock_get_source_ip: one adapter, eth0, 10.10.10.10) for an
# https URL without a port.
HARNESS_DIAL = "10.10.10.10:443"


@pytest.fixture
def _qr_capture(monkeypatch):
    """Every text the QR builder encoded, exactly as the QR holds it, and the
    same parsed."""
    texts: list[str] = []
    payloads: list[dict] = []
    real = enrollment_flow._make_qr

    def _capture(text):
        texts.append(text)
        payloads.append(json.loads(text))
        return real(text)

    monkeypatch.setattr(enrollment_flow, "_make_qr", _capture)
    return texts, payloads


@pytest.fixture
def qr_texts(_qr_capture):
    return _qr_capture[0]


@pytest.fixture
def qr_payloads(_qr_capture):
    return _qr_capture[1]


def _device_entry(hass) -> MockConfigEntry:
    """The host enrollment runs from since S-2: the push entry (the install-wide settings host, S-1), with the
    Tailscale answer `manual` (so the flow stays off Tailscale's API). A test that already made its push entry
    gets that one back."""
    existing = [e for e in hass.config_entries.async_entries(DOMAIN) if e.data.get("entry_type") == "push"]
    if existing:
        entry = existing[0]
        hass.config_entries.async_update_entry(entry, data={**entry.data, "tailscale_mode": "manual"})
        return entry
    return _push_entry(hass, firebase=None, tailscale_mode="manual")


def _push_entry(hass, firebase=FIREBASE, **extra) -> MockConfigEntry:
    """A push entry as push_flow stores it (not set up: the enrollment flow
    only reads its data)."""
    data = {
        "entry_type": "push",
        "project_id": "portico-test",
        "client_email": SA_EMAIL,
        "private_key_id": SA_KEY_ID,
        "package": "com.meda.portico",
        **extra,
    }
    if firebase is not None:
        data["firebase"] = firebase
    entry = MockConfigEntry(domain=DOMAIN, title="Portico push ring notification", data=data, unique_id="push")
    entry.add_to_hass(hass)
    return entry


def _set_urls(hass, external=None, internal=None):
    hass.config.external_url = external
    hass.config.internal_url = internal


async def _open_phone_form(hass, entry):
    """Configure on an entry whose Tailscale answer is complete: the menu,
    then "Enroll a device"."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert (result["type"], result["step_id"]) == (FlowResultType.MENU, "init"), result
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "enroll_phone"})
    assert result["step_id"] == "phone_details", result
    return result


async def _start_enroll(hass, entry):
    """The push entry's Configure > "Enroll a device": whatever the flow shows first (the Tailscale question when
    the answer is incomplete, else the phone form)."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert (result["type"], result["step_id"]) == (FlowResultType.MENU, "init"), result
    return await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "enroll_phone"})


async def _enroll(hass, entry, name="Alice's Pixel", allow_http=None, ha_dial=None):
    result = await _open_phone_form(hass, entry)
    user_input = {"phone_name": name}
    if allow_http is not None:
        user_input["allow_http"] = allow_http
    if ha_dial is not None:
        user_input["ha_dial"] = ha_dial
    return await hass.config_entries.options.async_configure(result["flow_id"], user_input)


def _portico_users(hass):
    return [u for u in hass.auth._store._users.values() if u.name and u.name.startswith("Portico")]


def _strings(value):
    """Every string leaf of a JSON value."""
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return [value] if isinstance(value, str) else []


async def test_payload_carries_the_portico_format_marker(hass, qr_payloads):
    _set_urls(hass, external=EXTERNAL)
    entry = _device_entry(hass)
    result = await _enroll(hass, entry)
    assert result["step_id"] == "show_qr", result

    (payload,) = qr_payloads
    # The number 2 (DEV-544: a one-time `code`, no token), never `true` (json.dumps(True) is `true`, which the app
    # refuses) and never the number 1 of the old ten-year-token format.
    assert payload["portico"] == 2 and type(payload["portico"]) is int
    # manual mode: no tailscale_authkey; ha_dial from the harness's network
    # adapter (pytest-homeassistant's mock_network: eth0, default, 10.10.10.10)
    assert set(payload) == {"portico", "ha_url", "code", "phone_name", "enroll_id", "ha_dial"}
    assert payload["ha_url"] == EXTERNAL
    assert payload["phone_name"] == "Alice's Pixel"
    assert payload["code"]


async def test_each_enrollment_gets_its_own_enroll_id_stored_with_the_phone(hass, qr_texts, qr_payloads, caplog):
    """Offline registration (reg_string_spec.md): the QR carries a random,
    non-secret `enroll_id`, stored beside the phone's HA user, so a pasted
    registration string can be matched to the phone. Never logged."""
    caplog.set_level(logging.DEBUG)
    _set_urls(hass, external=EXTERNAL)
    entry = _device_entry(hass)
    for name in ("Pixel A", "Pixel B"):
        result = await _enroll(hass, entry, name=name)
        assert result["step_id"] == "show_qr", result
    ids = [p["enroll_id"] for p in qr_payloads]
    assert all(re.fullmatch(r"[A-Za-z0-9_-]{43}", i) for i in ids) and ids[0] != ids[1]
    phones = enrolled(hass)
    assert [(p["name"], p["enroll_id"]) for p in phones] == [("Pixel A", ids[0]), ("Pixel B", ids[1])]
    assert all(p["ha_user_id"] for p in phones)
    # Only what the integration itself logs counts. The test harness's mock storage logs every write at DEBUG
    # ("Writing data to portico.elements: ..."), and since DEVICES-10 (e8a434a) the managed Talk element of each phone
    # carries the enroll_id in its options; real Home Assistant's Store never logs the data it writes.
    ours = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("custom_components.portico"))
    for i in ids:
        assert i not in ours
    assert json.loads(qr_texts[0])["enroll_id"] == ids[0]


# --- ha_url: https first; http only with the box ticked (Alice, S8) ---------


async def test_the_plain_http_box_is_off_by_default(hass):
    _set_urls(hass, external=EXTERNAL)
    result = await _open_phone_form(hass, _device_entry(hass))
    schema = result["data_schema"]
    assert schema({"phone_name": "p"}) == {"phone_name": "p", "allow_http": False}


@pytest.mark.parametrize(("external", "internal", "allow_http", "chosen"), [
    # https wins wherever it is, external first; the box changes nothing then.
    (EXTERNAL, INTERNAL, False, EXTERNAL),
    (EXTERNAL, INTERNAL, True, EXTERNAL),
    (EXTERNAL_HTTP, INTERNAL_HTTPS, False, INTERNAL_HTTPS),
    (EXTERNAL_HTTP, INTERNAL_HTTPS, True, INTERNAL_HTTPS),
    (EXTERNAL, INTERNAL_HTTPS, False, EXTERNAL),
    (None, INTERNAL_HTTPS, False, INTERNAL_HTTPS),
    (EXTERNAL, None, False, EXTERNAL),
    ("HTTPS://HA.example.org", None, False, "HTTPS://HA.example.org"),
    # Only http: used when, and only when, the installer ticked the box.
    (EXTERNAL_HTTP, INTERNAL, True, EXTERNAL_HTTP),
    (None, INTERNAL, True, INTERNAL),
    (EXTERNAL_HTTP, None, True, EXTERNAL_HTTP),
    ("  " + INTERNAL + " ", None, True, INTERNAL),
])
async def test_the_chosen_url_is_the_one_in_the_payload(hass, qr_payloads, external, internal, allow_http, chosen):
    _set_urls(hass, external=external, internal=internal)
    result = await _enroll(hass, _device_entry(hass), allow_http=allow_http)
    assert result["step_id"] == "show_qr", result
    assert qr_payloads[0]["ha_url"] == chosen


@pytest.mark.parametrize(("external", "internal"), [
    (EXTERNAL_HTTP, INTERNAL),
    (None, INTERNAL),
    (EXTERNAL_HTTP, None),
    (EXTERNAL_HTTP, "  "),
])
async def test_only_http_without_the_box_is_refused(hass, qr_payloads, external, internal):
    _set_urls(hass, external=external, internal=internal)
    entry = _device_entry(hass)
    result = await _enroll(hass, entry)  # box left at its default (off)

    assert result["type"] is FlowResultType.FORM
    assert (result["step_id"], result["errors"]) == ("phone_details", {"base": "no_https_url"})
    assert qr_payloads == [] and _portico_users(hass) == []
    assert enrolled(hass) == []

    # Ticking the box on the same form goes on, with the http URL.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"phone_name": "p", "allow_http": True})
    assert result["step_id"] == "show_qr", result
    assert qr_payloads[0]["ha_url"].startswith("http://")
    assert len(_portico_users(hass)) == 1


@pytest.mark.parametrize("allow_http", [False, True])
@pytest.mark.parametrize(("external", "internal"), [(None, None), ("", ""), ("  ", None)])
async def test_no_url_refuses_to_mint_a_qr(hass, qr_payloads, external, internal, allow_http):
    _set_urls(hass, external=external, internal=internal)
    entry = _device_entry(hass)
    result = await _enroll(hass, entry, allow_http=allow_http)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "phone_details"
    assert result["errors"] == {"base": "no_ha_url"}
    # Nothing was minted or recorded: no QR, no HA user, no enrolled phone.
    assert qr_payloads == []
    assert _portico_users(hass) == []
    assert enrolled(hass) == []

    # Once a URL is set, the same flow goes on.
    _set_urls(hass, external=EXTERNAL)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"phone_name": "p"})
    assert result["step_id"] == "show_qr", result
    assert qr_payloads[0]["ha_url"] == EXTERNAL
    assert len(_portico_users(hass)) == 1


# --- the push ring notification `fcm` block (sender design §5.1, server P3) --------------


async def test_no_push_entry_means_no_fcm_key(hass, qr_texts, qr_payloads):
    """Absent, never null or empty: portico-app reads absent as "leave the
    phone's Firebase settings alone"."""
    _set_urls(hass, external=EXTERNAL)
    result = await _enroll(hass, _device_entry(hass))
    assert result["step_id"] == "show_qr", result
    assert "fcm" not in qr_payloads[0]
    assert '"fcm"' not in qr_texts[0]


async def test_a_push_entry_without_firebase_values_means_no_fcm_key(hass, qr_payloads):
    _set_urls(hass, external=EXTERNAL)
    _push_entry(hass, firebase=None)
    result = await _enroll(hass, _device_entry(hass))
    assert result["step_id"] == "show_qr", result  # nothing to warn about: none were given
    assert "fcm" not in qr_payloads[0]


async def test_the_fcm_block_has_the_apps_shape(hass, qr_payloads):
    """portico-app EnrollmentPayload.parseFcm: an object, `v` the NUMBER 1,
    and the four values as strings under FirebaseField's keys."""
    _set_urls(hass, external=EXTERNAL)
    _push_entry(hass)
    result = await _enroll(hass, _device_entry(hass))
    assert result["step_id"] == "show_qr", result

    (payload,) = qr_payloads
    assert set(payload) == {"portico", "ha_url", "code", "phone_name", "enroll_id", "ha_dial", "fcm"}
    fcm = payload["fcm"]
    assert fcm == {"v": 1, **FIREBASE}
    assert type(fcm["v"]) is int
    assert all(type(fcm[k]) is str for k in FIREBASE)


async def test_nothing_secret_but_the_one_time_code_reaches_the_qr(hass, qr_texts, qr_payloads):
    """Only the four non-secret client values join the payload: no service-
    account field, no private key, no token at all, only the one-time `code` (sender design
    §5.1, DEV-544). Extra keys a stored `firebase` dict might carry are dropped."""
    _set_urls(hass, external=EXTERNAL)
    leaky = {**FIREBASE, "private_key": "-----BEGIN PRIVATE KEY-----SENTINEL", "client_email": SA_EMAIL,
             "access_token": "SENTINEL-ACCESS-TOKEN", "fcm_token": "SENTINEL-REG-TOKEN"}
    _push_entry(hass, firebase=leaky, private_key="SENTINEL-IN-ENTRY-DATA", token_uri="https://example.invalid/t")
    result = await _enroll(hass, _device_entry(hass))
    assert result["step_id"] == "show_qr", result

    (text,), (payload,) = qr_texts, qr_payloads
    for secret in (SA_EMAIL, SA_KEY_ID, "PRIVATE", "SENTINEL", "client_email", "private_key", "token_uri",
                   "example.invalid"):
        assert secret not in text
    expected = {EXTERNAL, payload["code"], "Alice's Pixel", payload["enroll_id"], HARNESS_DIAL,
                *FIREBASE.values()}
    assert set(_strings(payload)) == expected
    # No key of the payload is a token: the phone gets its own sign-in by redeeming the code.
    assert [k for k in payload if "token" in k] == []
    assert [k for k in payload["fcm"] if "token" in k or "key" in k] == ["api_key"]


@pytest.mark.parametrize("firebase", [
    {**FIREBASE, "project_id": "unknown"},
    {**FIREBASE, "app_id": "1:999:android:0123abcd"},  # an app of another project
    {**FIREBASE, "api_key": "has a space"},
    {**FIREBASE, "sender_id": "12345678901x"},
    {**FIREBASE, "project_id": "portico-test\n"},
    {k: v for k, v in FIREBASE.items() if k != "api_key"},
    {**FIREBASE, "api_key": None},
    "not-a-dict",
])
async def test_values_the_app_would_refuse_are_left_out_with_a_warning(hass, qr_payloads, firebase, caplog):
    """A block portico-app refuses would refuse the WHOLE enrollment: leave
    it out, still show the QR, and warn the installer."""
    _set_urls(hass, external=EXTERNAL)
    _push_entry(hass, firebase=firebase)
    entry = _device_entry(hass)
    result = await _enroll(hass, entry)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "show_qr_fcm_skipped", result
    assert "fcm" not in qr_payloads[0]
    assert "Enrollment QR" in result["description_placeholders"]["qr_image"]
    # The phone was still enrolled, and the warning step finishes the flow.
    assert len(enrolled(hass)) == 1
    done = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert done["type"] is FlowResultType.CREATE_ENTRY
    # The log names the problem, never a value.
    assert "left out" in caplog.text
    assert "AIzaSy" not in caplog.text and "0123abcd" not in caplog.text


async def test_finishing_the_qr_step_keeps_the_push_entrys_options(hass, qr_payloads):
    """The options flow's final async_create_entry replaces entry.options
    wholesale: the push ring notification's settings must survive an enrollment."""
    _set_urls(hass, external=EXTERNAL)
    entry = _device_entry(hass)
    options = {"enabled": True, "ring_window": 90, "new_phones_enabled": True, "test_phones": ["phone-entry-1"]}
    hass.config_entries.async_update_entry(entry, options=options)
    result = await _enroll(hass, entry)
    assert result["step_id"] == "show_qr", result

    done = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert done["type"] is FlowResultType.CREATE_ENTRY
    assert dict(entry.options) == options


# --- the QR text: compact, unescaped UTF-8 (S8) ----------------------------


def test_the_qr_text_is_compact_and_keeps_hebrew_unescaped():
    payload = {"portico": 2, "ha_url": EXTERNAL, "code": "c" * 32, "phone_name": HEBREW_NAME}
    text = enrollment_flow._qr_text(payload)
    assert HEBREW_NAME in text and "\\u" not in text
    assert ", " not in text and ": " not in text
    assert json.loads(text) == payload
    # 27 characters, 22 of them Hebrew letters: 2 bytes each in UTF-8 against 6
    # as a `\\u05xx` escape (DEV-544: the one-time code replaced the 180-character token, so the QR is much smaller).
    assert len(text.encode("utf-8")) == 154
    assert enrollment_flow._make_qr(text).version == 9


def test_the_qr_bytes_are_the_utf8_of_the_text():
    """What a decoder gets back: the byte-mode segments, concatenated, are
    exactly the UTF-8 encoding of the text (portico-app's ZXing reads valid
    multi-byte UTF-8 in a byte segment as UTF-8)."""
    text = enrollment_flow._qr_text({"portico": 2, "ha_url": EXTERNAL, "code": "t0k",
                                     "phone_name": HEBREW_NAME, "fcm": {"v": 1, **FIREBASE}})
    qr = enrollment_flow._make_qr(text)
    assert b"".join(chunk.data for chunk in qr.data_list) == text.encode("utf-8")
    assert qr.error_correction == qrcode.constants.ERROR_CORRECT_M


@pytest.mark.parametrize(("name", "with_fcm", "size", "version"), [
    # Worst case with the offline-registration enroll_id (57 bytes) and a
    # Tailscale key (about 80): still at or under about 20 (17 since DEV-544 replaced the token with a code).
    (HEBREW_NAME, "enroll+ts", 487, 17),
    # ... plus `ha_dial` (DEV-557): a typical LAN address adds 25 bytes, the
    # longest IPv4 form 34; both stay at version 17 or 18 (python-qrcode's segment
    # optimisation packs the alphanumeric runs). Even a long typed IPv6
    # target, which auto-detection never produces, stays at 18.
    (HEBREW_NAME, "enroll+ts+dial", 512, 17),
    (HEBREW_NAME, "enroll+ts+dial-v4max", 521, 18),
    (HEBREW_NAME, "enroll+ts+dial-v6", 547, 18),
    ("Alice's Pixel", False, 118, 7),
    ("Alice's Pixel", True, 290, 13),
    (HEBREW_NAME, False, 154, 9),
    (HEBREW_NAME, True, 326, 13),
])
def test_qr_size_with_and_without_the_fcm_block(name, with_fcm, size, version):
    """Sender design §5.4: measure the real version at ECC M and keep it at
    or under about 20. A Tailscale key (auto mode) adds about 80 bytes."""
    payload = {"portico": 2, "ha_url": EXTERNAL, "code": "c" * 32, "phone_name": name}
    if with_fcm:
        payload["fcm"] = {"v": 1, **FIREBASE}
    if isinstance(with_fcm, str) and with_fcm.startswith("enroll+ts"):
        payload["enroll_id"] = "E" * 43
        payload["tailscale_authkey"] = "tskey-auth-" + "k" * 69
    dial = {"enroll+ts+dial": "10.6.0.5:443", "enroll+ts+dial-v4max": "192.168.255.255:65535",
            "enroll+ts+dial-v6": "[fd7a:115c:a1e0:ab12:4843:cd96:6258:b240]:65535"}.get(with_fcm)
    if dial is not None:
        assert enrollment_flow.parse_ha_dial(dial) == dial
        payload["ha_dial"] = dial
    text = enrollment_flow._qr_text(payload)
    assert (len(text.encode("utf-8")), enrollment_flow._make_qr(text).version) == (size, version)


async def test_a_hebrew_name_through_the_flow(hass, qr_texts):
    _set_urls(hass, external=EXTERNAL)
    _push_entry(hass)
    result = await _enroll(hass, _device_entry(hass), name=HEBREW_NAME_SHORT)
    assert result["step_id"] == "show_qr", result
    (text,) = qr_texts
    assert HEBREW_NAME_SHORT in text and "\\u" not in text
    assert json.loads(text)["phone_name"] == HEBREW_NAME_SHORT
    assert enrollment_flow._make_qr(text).version <= 20


# --- tailscale_mode is persisted only once its answer is complete -----------
# (Alice, 2026-10-07: picking `auto` and closing the dialog on the credentials
# page left `tailscale_mode: auto` with no credentials; the question never came
# back and every enrollment died on a KeyError AFTER creating an HA user.)

API_KEY = "tskey-api-SENTINEL-APIKEY-0123456789"
TAILNET = "example.com"
MINT_URL = f"{enrollment_flow.TAILSCALE_API_BASE}/tailnet/{TAILNET}/keys"
MINTED = "tskey-auth-MINTED-0123456789"
CREDS = {"tailscale_api_key": API_KEY, "tailscale_tailnet": TAILNET, "tailscale_tag": "tag:portico-phone"}


def _entry(hass, **tailscale) -> MockConfigEntry:
    """The push entry (the enrollment host) with only the given Tailscale fields (no mode by default)."""
    return _push_entry(hass, firebase=None, **tailscale)


async def test_choosing_auto_does_not_persist_the_mode_yet(hass):
    entry = _entry(hass)
    result = await _start_enroll(hass, entry)
    assert result["step_id"] == "tailscale_mode", result
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"tailscale_mode": "auto"})
    assert result["step_id"] == "tailscale_credentials", result
    assert "tailscale_mode" not in entry.data

    # Abandoned on the credentials page: the question comes back next time.
    hass.config_entries.options.async_abort(result["flow_id"])
    assert "tailscale_mode" not in entry.data
    result = await _start_enroll(hass, entry)
    assert result["step_id"] == "tailscale_mode", result


async def test_the_credentials_step_persists_mode_and_credentials_together(hass):
    entry = _entry(hass)
    result = await _start_enroll(hass, entry)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"tailscale_mode": "auto"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], dict(CREDS))
    assert result["step_id"] == "phone_details", result
    assert entry.data["tailscale_mode"] == "auto"
    assert {k: entry.data[k] for k in CREDS} == CREDS


async def test_choosing_manual_persists_at_once(hass):
    entry = _entry(hass)
    result = await _start_enroll(hass, entry)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"tailscale_mode": "manual"})
    assert result["step_id"] == "phone_details", result
    assert entry.data["tailscale_mode"] == "manual"


@pytest.mark.parametrize("partial", [
    {},  # the live state, 2026-10-07
    {"tailscale_tailnet": TAILNET},
    {"tailscale_api_key": API_KEY},
    {"tailscale_api_key": "  ", "tailscale_tailnet": TAILNET},
    {"tailscale_api_key": API_KEY, "tailscale_tailnet": ""},
])
async def test_auto_without_credentials_asks_the_question_again(hass, partial):
    entry = _entry(hass, tailscale_mode="auto", **partial)
    result = await _start_enroll(hass, entry)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "tailscale_mode", result
    assert entry.data["tailscale_mode"] == "auto"  # not flipped by merely asking

    result = await hass.config_entries.options.async_configure(result["flow_id"], {"tailscale_mode": "auto"})
    assert result["step_id"] == "tailscale_credentials", result
    result = await hass.config_entries.options.async_configure(result["flow_id"], dict(CREDS))
    assert result["step_id"] == "phone_details", result
    assert {k: entry.data[k] for k in CREDS} == CREDS


async def test_the_live_state_can_switch_to_manual_and_enroll(hass, qr_payloads):
    _set_urls(hass, external=EXTERNAL)
    entry = _entry(hass, tailscale_mode="auto")
    result = await _start_enroll(hass, entry)
    assert result["step_id"] == "tailscale_mode", result
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"tailscale_mode": "manual"})
    assert result["step_id"] == "phone_details", result
    assert entry.data["tailscale_mode"] == "manual"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"phone_name": "p"})
    assert result["step_id"] == "show_qr", result
    assert "tailscale_authkey" not in qr_payloads[0]
    assert len(_portico_users(hass)) == 1


async def test_a_legacy_entry_is_backfilled_to_auto(hass):
    entry = _entry(hass, **CREDS)
    await _open_phone_form(hass, entry)
    assert entry.data["tailscale_mode"] == "auto"


async def test_a_complete_auto_entry_enrolls_with_a_minted_key(hass, qr_payloads, aioclient_mock):
    _set_urls(hass, external=EXTERNAL)
    aioclient_mock.post(MINT_URL, json={"key": MINTED})
    entry = _entry(hass, tailscale_mode="auto", **CREDS)
    result = await _enroll(hass, entry)
    assert result["step_id"] == "show_qr", result
    assert qr_payloads[0]["tailscale_authkey"] == MINTED
    assert aioclient_mock.call_count == 1
    assert len(_portico_users(hass)) == 1


@pytest.mark.parametrize(("response", "body_marker"), [
    ({"status": 401, "text": "SENTINEL-BODY unauthorized"}, "SENTINEL-BODY"),
    ({"exc": TimeoutError()}, None),
    ({"text": "SENTINEL-BODY <html>not json"}, "SENTINEL-BODY"),
    ({"json": {"SENTINEL-BODY": "no key here"}}, "SENTINEL-BODY"),
])
async def test_a_failed_mint_creates_no_user(hass, qr_payloads, aioclient_mock, caplog, response, body_marker):
    _set_urls(hass, external=EXTERNAL)
    aioclient_mock.post(MINT_URL, **response)
    entry = _entry(hass, tailscale_mode="auto", **CREDS)
    users_before = len(hass.auth._store._users)
    result = await _enroll(hass, entry)

    assert result["type"] is FlowResultType.FORM
    assert (result["step_id"], result["errors"]) == ("phone_details", {"base": "tailscale_mint_failed"})
    assert len(hass.auth._store._users) == users_before
    assert _portico_users(hass) == [] and qr_payloads == []
    assert enrolled(hass) == []
    assert API_KEY not in caplog.text
    if body_marker:
        assert body_marker not in caplog.text

    # Fixed upstream: the same form goes on.
    aioclient_mock.clear_requests()
    aioclient_mock.post(MINT_URL, json={"key": MINTED})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"phone_name": "p"})
    assert result["step_id"] == "show_qr", result
    assert len(_portico_users(hass)) == 1


async def test_a_failure_after_the_user_exists_removes_it(hass, hass_owner_user, monkeypatch):
    _set_urls(hass, external=EXTERNAL)

    def _boom(payload):
        raise RuntimeError("QR failed")

    monkeypatch.setattr(enrollment_flow, "_build_qr_data_uri", _boom)
    entry = _entry(hass, tailscale_mode="manual")
    users_before = len(hass.auth._store._users)
    result = await _open_phone_form(hass, entry)
    with pytest.raises(RuntimeError, match="QR failed"):
        await hass.config_entries.options.async_configure(result["flow_id"], {"phone_name": "p"})
    assert len(hass.auth._store._users) == users_before
    assert _portico_users(hass) == []
    assert enrolled(hass) == []


async def test_the_qr_is_built_off_the_event_loop(hass, hass_owner_user, monkeypatch):
    _set_urls(hass, external=EXTERNAL)
    loop_thread = threading.get_ident()
    seen: list[int] = []
    real = enrollment_flow._build_qr_data_uri

    def _spy(payload):
        seen.append(threading.get_ident())
        return real(payload)

    monkeypatch.setattr(enrollment_flow, "_build_qr_data_uri", _spy)
    entry = _entry(hass, tailscale_mode="manual")
    result = await _open_phone_form(hass, entry)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"phone_name": "p"})
    assert result["step_id"] == "show_qr", result
    assert seen and loop_thread not in seen


async def test_the_key_is_minted_before_the_user_is_created(hass, monkeypatch):
    _set_urls(hass, external=EXTERNAL)
    calls: list[str] = []

    async def _mint(hass_, *, api_key, tailnet, tag):
        calls.append("mint")
        assert (api_key, tailnet, tag) == (API_KEY, TAILNET, "tag:portico-phone")
        return MINTED

    real_create = hass.auth.async_create_user

    async def _create_user(*args, **kwargs):
        calls.append("create_user")
        return await real_create(*args, **kwargs)

    monkeypatch.setattr(enrollment_flow, "_async_mint_tailscale_authkey", _mint)
    monkeypatch.setattr(hass.auth, "async_create_user", _create_user)
    result = await _enroll(hass, _entry(hass, tailscale_mode="auto", **CREDS))
    assert result["step_id"] == "show_qr", result
    assert calls == ["mint", "create_user"]


# --- auto with an OAuth client secret (2026-10-08) ---------------------------
# Tailscale's OAuth clients (docs/features/oauth-clients): the client secret
# (`tskey-client-...`) is not itself an API credential. It is exchanged at
# POST /api/v2/oauth/token (client credentials grant, form fields client_id +
# client_secret) for a one-hour access token, which then creates the key, with
# the tag named in the request. An API access token (`tskey-api-...`) is used
# on the keys endpoint directly, as before. Either way the minted key is NOT
# ephemeral: Tailscale removes an ephemeral node 30-60 minutes after its last
# activity (docs/features/ephemeral-nodes), and a phone asleep for an hour
# would then be gone with no way back on its single-use key.

TOKEN_URL = "https://api.tailscale.com/api/v2/oauth/token"
CLIENT_ID = "kSENTINELCID"
CLIENT_SECRET = f"tskey-client-{CLIENT_ID}-SENTINELCLIENTSECRET0123456789"
ACCESS_TOKEN = "tskey-SENTINEL-ACCESS-TOKEN-0123456789"
CLIENT_CREDS = {**CREDS, "tailscale_api_key": CLIENT_SECRET}


def _create_body(call):
    """The JSON body of a recorded keys-endpoint call."""
    return call[2]


def _assert_key_request(body):
    create = body["capabilities"]["devices"]["create"]
    assert create == {"reusable": False, "ephemeral": False, "preauthorized": True,
                      "tags": ["tag:portico-phone"]}
    assert body["expirySeconds"] == 600          # the life of the one-time code in the same QR (DEV-544)


async def test_an_api_access_token_creates_a_non_ephemeral_key_directly(hass, qr_payloads, aioclient_mock):
    _set_urls(hass, external=EXTERNAL)
    aioclient_mock.post(MINT_URL, json={"key": MINTED})
    result = await _enroll(hass, _entry(hass, tailscale_mode="auto", **CREDS))
    assert result["step_id"] == "show_qr", result
    assert qr_payloads[0]["tailscale_authkey"] == MINTED
    assert aioclient_mock.call_count == 1  # no token exchange
    method, url, body, headers = aioclient_mock.mock_calls[0]
    assert (method.upper(), str(url)) == ("POST", MINT_URL)
    expected = "Basic " + __import__("base64").b64encode(f"{API_KEY}:".encode()).decode()
    assert headers["Authorization"] == expected
    _assert_key_request(body)


async def test_an_oauth_client_secret_is_exchanged_then_creates_the_key(hass, qr_payloads, aioclient_mock):
    _set_urls(hass, external=EXTERNAL)
    aioclient_mock.post(TOKEN_URL, json={"access_token": ACCESS_TOKEN, "token_type": "Bearer", "expires_in": 3600})
    aioclient_mock.post(MINT_URL, json={"key": MINTED})
    entry = _entry(hass, tailscale_mode="auto", **CLIENT_CREDS)
    result = await _enroll(hass, entry)
    assert result["step_id"] == "show_qr", result
    assert qr_payloads[0]["tailscale_authkey"] == MINTED
    assert len(_portico_users(hass)) == 1

    assert aioclient_mock.call_count == 2
    (m1, u1, form, _h1), (m2, u2, body, h2) = aioclient_mock.mock_calls
    assert (m1.upper(), str(u1)) == ("POST", TOKEN_URL)
    assert form == {"grant_type": "client_credentials", "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}
    assert (m2.upper(), str(u2)) == ("POST", MINT_URL)
    assert h2["Authorization"] == f"Bearer {ACCESS_TOKEN}"
    _assert_key_request(body)
    # The access token is used once and kept nowhere.
    assert ACCESS_TOKEN not in json.dumps(dict(entry.data))


@pytest.mark.parametrize("bad", [
    "tskey-client-",
    "tskey-client-onlyonepart",
    "tskey-client--secret",
    "tskey-client-id-",
])
async def test_a_malformed_client_secret_is_a_mint_failure(hass, qr_payloads, aioclient_mock, bad):
    _set_urls(hass, external=EXTERNAL)
    entry = _entry(hass, tailscale_mode="auto", **{**CREDS, "tailscale_api_key": bad})
    result = await _enroll(hass, entry)
    assert (result["step_id"], result["errors"]) == ("phone_details", {"base": "tailscale_mint_failed"})
    assert aioclient_mock.call_count == 0
    assert _portico_users(hass) == [] and qr_payloads == []


@pytest.mark.parametrize(("response", "status"), [
    ({"status": 400, "text": "SENTINEL-BODY invalid_client"}, "400"),
    ({"status": 401, "text": "SENTINEL-BODY unauthorized"}, "401"),
    ({"status": 403, "text": "SENTINEL-BODY forbidden"}, "403"),
    ({"status": 500, "text": "SENTINEL-BODY oops"}, "500"),
    ({"exc": TimeoutError()}, "None"),
    ({"text": "SENTINEL-BODY <html>not json"}, "None"),
    ({"json": {"SENTINEL-BODY": "no token here"}}, "None"),
    ({"json": {"access_token": ""}}, "None"),
    ({"json": {"access_token": 12345}}, "None"),
])
async def test_a_failed_exchange_creates_no_user(hass, qr_payloads, aioclient_mock, caplog, response, status):
    caplog.set_level(logging.DEBUG)
    _set_urls(hass, external=EXTERNAL)
    aioclient_mock.post(TOKEN_URL, **response)
    aioclient_mock.post(MINT_URL, json={"key": MINTED})
    entry = _entry(hass, tailscale_mode="auto", **CLIENT_CREDS)
    users_before = len(hass.auth._store._users)
    result = await _enroll(hass, entry)

    assert (result["step_id"], result["errors"]) == ("phone_details", {"base": "tailscale_mint_failed"})
    assert len(hass.auth._store._users) == users_before
    assert _portico_users(hass) == [] and qr_payloads == []
    assert enrolled(hass) == []
    # The keys endpoint was never reached.
    assert [str(c[1]) for c in aioclient_mock.mock_calls] == [TOKEN_URL]
    # Logged: the exception class and the HTTP status, nothing else.
    assert f"HTTP status {status}" in caplog.text
    for secret in (CLIENT_SECRET, "SENTINELCLIENTSECRET", ACCESS_TOKEN, "SENTINEL-BODY"):
        assert secret not in caplog.text


async def test_a_rejected_exchange_is_told_apart_in_the_log(hass, aioclient_mock, caplog):
    _set_urls(hass, external=EXTERNAL)
    aioclient_mock.post(TOKEN_URL, status=401)
    await _enroll(hass, _entry(hass, tailscale_mode="auto", **CLIENT_CREDS))
    assert "TailscaleRejected" in caplog.text


async def test_a_failed_key_call_after_the_exchange_leaks_nothing(hass, qr_payloads, aioclient_mock, caplog):
    caplog.set_level(logging.DEBUG)
    _set_urls(hass, external=EXTERNAL)
    aioclient_mock.post(TOKEN_URL, json={"access_token": ACCESS_TOKEN})
    aioclient_mock.post(MINT_URL, status=403, text="SENTINEL-BODY tag not permitted")
    entry = _entry(hass, tailscale_mode="auto", **CLIENT_CREDS)
    result = await _enroll(hass, entry)
    assert (result["step_id"], result["errors"]) == ("phone_details", {"base": "tailscale_mint_failed"})
    assert _portico_users(hass) == [] and qr_payloads == []
    assert "HTTP status 403" in caplog.text
    for secret in (CLIENT_SECRET, ACCESS_TOKEN, "SENTINEL-BODY"):
        assert secret not in caplog.text


async def test_the_client_secret_is_redacted_in_diagnostics(hass, aioclient_mock):
    from custom_components.portico.diagnostics import async_get_config_entry_diagnostics

    _set_urls(hass, external=EXTERNAL)
    aioclient_mock.post(TOKEN_URL, json={"access_token": ACCESS_TOKEN})
    aioclient_mock.post(MINT_URL, json={"key": MINTED})
    entry = _entry(hass, tailscale_mode="auto", **CLIENT_CREDS)
    await _enroll(hass, entry)
    diag = json.dumps(await async_get_config_entry_diagnostics(hass, entry))
    for secret in (CLIENT_SECRET, "SENTINELCLIENTSECRET", ACCESS_TOKEN, MINTED):
        assert secret not in diag


# --- ha_dial: where the tailnet path dials (DEV-557, portico-app #16) -------
# tsnet never uses split DNS, so away from home the phone needs an ADDRESS to
# dial through the tailnet. Typed on the phone form, or HA's own LAN IPv4 on
# its default adapter joined with the port of `ha_url`; omitted when neither
# gives a value portico-app's TailnetTarget.parseHaDial accepts.


def _adapter(name, *, default=False, enabled=True, ipv4=(), ipv6=()):
    """An adapter as homeassistant.components.network reports it."""
    return {
        "name": name,
        "index": 0,
        "enabled": enabled,
        "auto": default,
        "default": default,
        "ipv4": [{"address": a, "network_prefix": 24} for a in ipv4],
        "ipv6": [{"address": a, "flowinfo": 0, "scope_id": 0, "network_prefix": 64} for a in ipv6],
    }


@pytest.fixture
def adapters(monkeypatch):
    """The adapters HA's network helper returns; a test fills the list."""
    current: list = []

    async def _get(hass):
        return current

    monkeypatch.setattr("homeassistant.components.network.async_get_adapters", _get)
    return current


async def test_the_harness_network_yields_its_own_address(hass, qr_payloads):
    """Unmocked here: pytest-homeassistant's session-wide mock_network (eth0,
    10.10.10.10, the default route's source address) through HA's real
    network helper."""
    _set_urls(hass, external=EXTERNAL)
    result = await _enroll(hass, _device_entry(hass))
    assert result["step_id"] == "show_qr", result
    assert qr_payloads[0]["ha_dial"] == HARNESS_DIAL


@pytest.mark.parametrize(("found", "expected"), [
    # one default adapter with one private IPv4
    ([_adapter("eth0", default=True, ipv4=["10.6.0.5"])], "10.6.0.5:443"),
    # several adapters: the default one's, never docker0's or another LAN's
    ([_adapter("docker0", ipv4=["172.17.0.1"]),
      _adapter("eth0", default=True, ipv4=["192.168.1.10"], ipv6=["fd00::10"]),
      _adapter("wlan0", ipv4=["10.0.0.3"])], "192.168.1.10:443"),
    # loopback and link-local on the default adapter are skipped
    ([_adapter("eth0", default=True, ipv4=["169.254.3.3", "10.6.0.5", "127.0.0.2"])], "10.6.0.5:443"),
    # none
    ([], None),
    # IPv6 only (even a ULA: auto-detection is IPv4 only)
    ([_adapter("eth0", default=True, ipv6=["fd7a:115c:a1e0::5", "fe80::1"])], None),
    # never a public address
    ([_adapter("eth0", default=True, ipv4=["203.0.113.5"])], None),
    # ambiguous: two private IPv4 on the default adapter
    ([_adapter("eth0", default=True, ipv4=["10.6.0.5", "192.168.1.10"])], None),
    # ambiguous: two default adapters
    ([_adapter("eth0", default=True, ipv4=["10.6.0.5"]), _adapter("eth1", default=True, ipv4=["10.7.0.5"])],
     None),
    # no default adapter: a private address elsewhere is not a guess we make
    ([_adapter("eth0", ipv4=["10.6.0.5"])], None),
    # the default adapter disabled in Settings > System > Network
    ([_adapter("eth0", default=True, enabled=False, ipv4=["10.6.0.5"]), _adapter("eth1", ipv4=["10.7.0.5"])],
     None),
])
async def test_auto_detection(hass, qr_texts, qr_payloads, adapters, found, expected):
    adapters.extend(found)
    _set_urls(hass, external=EXTERNAL)
    result = await _enroll(hass, _device_entry(hass))
    assert result["step_id"] == "show_qr", result  # silently omitted: still a QR
    if expected is None:
        assert "ha_dial" not in qr_payloads[0] and '"ha_dial"' not in qr_texts[0]
    else:
        assert qr_payloads[0]["ha_dial"] == expected
    assert "203.0.113" not in qr_texts[0]


async def test_a_failing_network_helper_omits_it(hass, qr_payloads, monkeypatch):
    async def _boom(hass_):
        raise OSError("no adapters")

    monkeypatch.setattr("homeassistant.components.network.async_get_adapters", _boom)
    _set_urls(hass, external=EXTERNAL)
    result = await _enroll(hass, _device_entry(hass))
    assert result["step_id"] == "show_qr", result
    assert "ha_dial" not in qr_payloads[0]


@pytest.mark.parametrize(("external", "internal", "allow_http", "expected"), [
    (EXTERNAL, None, False, "10.6.0.5:443"),
    ("HTTPS://HA.example.org/", None, False, "10.6.0.5:443"),
    (None, INTERNAL_HTTPS, False, "10.6.0.5:8443"),
    ("http://ha.lan", None, True, "10.6.0.5:80"),
    (None, INTERNAL, True, "10.6.0.5:8123"),
])
async def test_the_port_is_the_ha_urls(hass, qr_payloads, adapters, external, internal, allow_http, expected):
    adapters.append(_adapter("eth0", default=True, ipv4=["10.6.0.5"]))
    _set_urls(hass, external=external, internal=internal)
    result = await _enroll(hass, _device_entry(hass), allow_http=allow_http)
    assert result["step_id"] == "show_qr", result
    assert qr_payloads[0]["ha_dial"] == expected


@pytest.mark.parametrize(("typed", "expected"), [
    ("10.6.0.9:443", "10.6.0.9:443"),
    ("  [FD7A:115C:A1E0::5]:8443 ", "[fd7a:115c:a1e0::5]:8443"),
    ("100.101.102.103:443", "100.101.102.103:443"),
])
async def test_a_typed_address_wins(hass, qr_payloads, adapters, typed, expected):
    adapters.append(_adapter("eth0", default=True, ipv4=["192.168.1.10"]))
    _set_urls(hass, external=EXTERNAL)
    entry = _device_entry(hass)
    result = await _enroll(hass, entry, ha_dial=typed)
    assert result["step_id"] == "show_qr", result
    assert qr_payloads[0]["ha_dial"] == expected
    assert result["description_placeholders"]["ha_dial"] == f"`{expected}`"


@pytest.mark.parametrize("typed", ["", "   "])
async def test_a_blank_field_means_automatic(hass, qr_payloads, adapters, typed):
    adapters.append(_adapter("eth0", default=True, ipv4=["10.6.0.5"]))
    _set_urls(hass, external=EXTERNAL)
    result = await _enroll(hass, _device_entry(hass), ha_dial=typed)
    assert result["step_id"] == "show_qr", result
    assert qr_payloads[0]["ha_dial"] == "10.6.0.5:443"


@pytest.mark.parametrize("typed", [
    "haos.home.example:443", "8.8.8.8:443", "10.6.0.5", "127.0.0.1:443", "[::ffff:10.6.0.5]:443",
    "10.6.0.5:0443", "https://10.6.0.5",
])
async def test_a_bad_typed_address_creates_nothing(hass, hass_owner_user, qr_payloads, adapters, typed):
    adapters.append(_adapter("eth0", default=True, ipv4=["10.6.0.5"]))
    _set_urls(hass, external=EXTERNAL)
    entry = _device_entry(hass)
    users_before = len(hass.auth._store._users)
    result = await _enroll(hass, entry, ha_dial=typed)

    assert result["type"] is FlowResultType.FORM
    # Before any key, user or token.
    assert (result["step_id"], result["errors"]) == ("phone_details", {"ha_dial": "invalid_ha_dial"})
    assert qr_payloads == [] and _portico_users(hass) == []
    assert len(hass.auth._store._users) == users_before
    assert enrolled(hass) == [] and "ha_dial" not in entry.data

    # Cleared: automatic, and the flow goes on.
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"phone_name": "p"})
    assert result["step_id"] == "show_qr", result
    assert qr_payloads[0]["ha_dial"] == "10.6.0.5:443"


def _suggested(result, field):
    for key in result["data_schema"].schema:
        if key == field:
            return (key.description or {}).get("suggested_value")
    raise AssertionError(f"no {field} in the form")


async def test_a_typed_address_is_remembered_and_a_blank_one_forgets_it(hass, qr_payloads, adapters):
    """Not secret: the typed value is kept in entry data as the next
    enrollment's suggestion; a blank field (automatic) forgets it."""
    adapters.append(_adapter("eth0", default=True, ipv4=["10.6.0.5"]))
    _set_urls(hass, external=EXTERNAL)
    entry = _device_entry(hass)
    result = await _open_phone_form(hass, entry)
    assert _suggested(result, "ha_dial") is None
    # An optional field: leaving it out is valid.
    assert result["data_schema"]({"phone_name": "p"}) == {"phone_name": "p", "allow_http": False}

    await hass.config_entries.options.async_configure(
        result["flow_id"], {"phone_name": "a", "ha_dial": " [FD7A::5]:443 "})
    assert entry.data["ha_dial"] == "[fd7a::5]:443"

    result = await _open_phone_form(hass, entry)
    assert _suggested(result, "ha_dial") == "[fd7a::5]:443"
    await hass.config_entries.options.async_configure(result["flow_id"], {"phone_name": "b"})
    assert "ha_dial" not in entry.data
    assert [p.get("ha_dial") for p in qr_payloads] == ["[fd7a::5]:443", "10.6.0.5:443"]
    result = await _open_phone_form(hass, entry)
    assert _suggested(result, "ha_dial") is None


async def test_the_qr_page_names_the_target(hass, adapters):
    """One line on the QR page: the address the phone dials over Tailscale,
    or, with no `ha_dial`, the URL's own host and port (what the app dials)."""
    _set_urls(hass, external=EXTERNAL)
    result = await _enroll(hass, _device_entry(hass))
    assert result["description_placeholders"]["ha_dial"] == "`ha.example.org:443`"
    adapters.append(_adapter("eth0", default=True, ipv4=["10.6.0.5"]))
    result = await _enroll(hass, _device_entry(hass))
    assert result["description_placeholders"]["ha_dial"] == "`10.6.0.5:443`"


# --- the QR fits the flow dialog (DEV-673) ----------------------------------

GUARD_LINE = "Make sure the whole code is on screen (scroll up) before scanning."


def _realistic_payload() -> dict:
    """The owner's real shape (Alice, 2026-10-22): format 2 with a one-time code, enroll_id, an auto-minted Tailscale
    key, ha_dial and the fcm block: 477 bytes of QR text, version 17, 85 modules."""
    return {"portico": 2, "ha_url": "https://haos.myhouse-01.example.org", "code": "c" * 32, "phone_name": "AlicePixel10",
            "enroll_id": "E" * 43, "tailscale_authkey": "tskey-auth-k" + "k" * 11 + "CNTRL-" + "t" * 40,
            "ha_dial": "10.6.0.5:443", "fcm": {"v": 1, **FIREBASE}}


def _png_size(data_uri: str) -> tuple[int, int]:
    import base64
    import struct

    prefix = "data:image/png;base64,"
    assert data_uri.startswith(prefix)
    png = base64.b64decode(data_uri[len(prefix):])
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and png[12:16] == b"IHDR"
    return struct.unpack(">II", png[16:24])


def _qr_step_text(step: str) -> str:
    from pathlib import Path

    strings = json.loads((Path(enrollment_flow.__file__).parent / "strings.json").read_text(encoding="utf-8"))
    return strings["options"]["step"][step]["description"]


def test_the_realistic_qr_png_is_small_enough_for_the_dialog():
    """Before: 930 x 930 px (box 10), which HA's dialog scaled to ~500 px with the lower part below the fold. The code
    itself did not change: still 477 bytes, version 17, 85 modules; only the pixels per module did."""
    payload = _realistic_payload()
    text = enrollment_flow._qr_text(payload)
    qr = enrollment_flow._make_qr(text)
    assert (len(text.encode("utf-8")), qr.version, qr.modules_count) == (477, 17, 85)
    assert qr.error_correction == qrcode.constants.ERROR_CORRECT_M
    assert qr.border == 4
    width, height = _png_size(enrollment_flow._build_qr_data_uri(payload))
    assert width == height == (85 + 2 * 4) * enrollment_flow.QR_BOX_SIZE == 372
    assert width <= 420


@pytest.mark.parametrize("version_payload", [
    {"portico": 2, "ha_url": EXTERNAL, "code": "c" * 32, "phone_name": "p"},
    {"portico": 2, "ha_url": EXTERNAL, "code": "c" * 32, "phone_name": HEBREW_NAME, "fcm": {"v": 1, **FIREBASE}},
])
def test_the_png_is_exactly_modules_plus_border_times_the_box(version_payload):
    """The size is a pure function of the module count, so a bigger payload shows its growth here instead of in a
    dialog nobody can see from CI; and it never exceeds the realistic one."""
    qr = enrollment_flow._make_qr(enrollment_flow._qr_text(version_payload))
    width, height = _png_size(enrollment_flow._build_qr_data_uri(version_payload))
    assert width == height == (qr.modules_count + 8) * enrollment_flow.QR_BOX_SIZE
    assert width <= 372


@pytest.mark.parametrize("step", ["show_qr", "show_qr_fcm_skipped"])
def test_the_step_text_opens_with_the_guard_line_then_the_image_then_the_notes(step):
    text = _qr_step_text(step)
    assert text.startswith(GUARD_LINE + "\n\n{qr_image}\n\n")
    assert text.count("{qr_image}") == 1
    # Everything long comes below the image.
    assert text.index("{qr_image}") < min(text.index(p) for p in ("{expires_at}", "{phone_name}", "{ha_dial}"))


async def test_the_rendered_step_puts_the_image_right_after_the_guard_line(hass):
    _set_urls(hass, external=EXTERNAL)
    _push_entry(hass)
    result = await _enroll(hass, _device_entry(hass))
    assert result["step_id"] == "show_qr", result
    rendered = _qr_step_text("show_qr").format(**result["description_placeholders"])
    assert rendered.startswith(GUARD_LINE + "\n\n![Enrollment QR](data:image/png;base64,")
    below = rendered[rendered.index(")\n\n", len(GUARD_LINE)) + 3:]
    assert "scroll" not in below
    assert len(below.split("\n\n")) <= 4  # a few short paragraphs under the image, not the old wall of text
    assert len(below) < 1400
