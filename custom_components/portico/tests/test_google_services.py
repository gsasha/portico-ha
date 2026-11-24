"""`parse_google_services` against hostile and malformed google-services.json
shapes (sender design §5.2). Plain session, no HA harness.

The four values reach a phone (the enrollment QR's `fcm` block, §5.1) and
portico-app's `GoogleServicesParser` + `FirebaseConfig.create` refuse the
same shapes, with the same codes: `invalid_json`, `missing_fields`,
`package_not_found`, and `invalid_values` for a value that is present but
unusable (a placeholder, a malformed id, an app id from another project).
"""
from __future__ import annotations

import copy
import json

import pytest

from custom_components.portico.push.const import PORTICO_PACKAGE
from custom_components.portico.push.credentials import (
    GoogleServicesError,
    app_firebase_values,
    parse_google_services,
)

PROJECT_ID = "portico-test"
SENDER_ID = "123456789012"
APP_ID = f"1:{SENDER_ID}:android:0123abcd4567ef89"
API_KEY = "AIzaSyFAKE-not-a-real-key_0123456789abc"

# The shape Firebase's console downloads, with two Android apps.
GOLDEN = {
    "project_info": {"project_number": SENDER_ID, "project_id": PROJECT_ID,
                     "storage_bucket": f"{PROJECT_ID}.appspot.com"},
    "client": [
        {"client_info": {"mobilesdk_app_id": f"1:{SENDER_ID}:android:ffff0000ffff0000",
                         "android_client_info": {"package_name": "com.example.other"}},
         "oauth_client": [], "api_key": [{"current_key": "AIzaSyFAKE-other-app"}],
         "services": {"appinvite_service": {"other_platform_oauth_client": []}}},
        {"client_info": {"mobilesdk_app_id": APP_ID,
                         "android_client_info": {"package_name": PORTICO_PACKAGE}},
         "oauth_client": [], "api_key": [{"current_key": API_KEY}],
         "services": {"appinvite_service": {"other_platform_oauth_client": []}}},
    ],
    "configuration_version": "1",
}


def _doc(**edits) -> dict:
    """GOLDEN with edits: `info__<key>`, `portico__<key>` on Portico's client
    (`app_id`, `api_key`, `client_info`, `android`), or `client`."""
    doc = copy.deepcopy(GOLDEN)
    portico = doc["client"][1]
    for key, value in edits.items():
        if key.startswith("info__"):
            doc["project_info"][key[6:]] = value
        elif key == "portico__app_id":
            portico["client_info"]["mobilesdk_app_id"] = value
        elif key == "portico__api_key":
            portico["api_key"] = value
        elif key == "portico__client_info":
            portico["client_info"] = value
        elif key == "portico__android":
            portico["client_info"]["android_client_info"] = value
        elif key == "client":
            doc["client"] = value
        else:
            raise AssertionError(key)
    return doc


def _code(doc) -> str:
    raw = doc if isinstance(doc, str) else json.dumps(doc)
    with pytest.raises(GoogleServicesError) as err:
        parse_google_services(raw, PORTICO_PACKAGE)
    return err.value.code


def test_golden_file_gives_the_four_values():
    assert parse_google_services(json.dumps(GOLDEN), PORTICO_PACKAGE) == {
        "project_id": PROJECT_ID, "sender_id": SENDER_ID, "app_id": APP_ID, "api_key": API_KEY,
    }


def test_a_dict_is_accepted_as_well_as_text():
    assert parse_google_services(copy.deepcopy(GOLDEN), PORTICO_PACKAGE)["app_id"] == APP_ID


def test_integer_project_number_is_its_digits():
    """The app reads an integer `project_number` as its digits (`pyStrOrNull`);
    so does the server."""
    assert parse_google_services(json.dumps(_doc(info__project_number=int(SENDER_ID))),
                                 PORTICO_PACKAGE)["sender_id"] == SENDER_ID


# --------------------------------------------------------------------------
# A value that is not a non-empty string: missing_fields, never "None"/"True"
# --------------------------------------------------------------------------

_NOT_A_STRING = [None, "", "   ", True, False, 0, 12, 1.5, [], ["x"], {}, {"a": 1}]


@pytest.mark.parametrize("value", _NOT_A_STRING)
def test_app_id_must_be_a_non_empty_string(value):
    assert _code(_doc(portico__app_id=value)) == "missing_fields"


@pytest.mark.parametrize("value", _NOT_A_STRING)
def test_api_key_must_be_a_non_empty_string(value):
    assert _code(_doc(portico__api_key=[{"current_key": value}])) == "missing_fields"


@pytest.mark.parametrize("value", _NOT_A_STRING)
def test_project_id_must_be_a_non_empty_string(value):
    assert _code(_doc(info__project_id=value)) == "missing_fields"


@pytest.mark.parametrize("value", [None, "", "   ", True, False, 0, 1.5, [], {}])
def test_project_number_must_be_digits_text_or_a_positive_integer(value):
    assert _code(_doc(info__project_number=value)) == "missing_fields"


# --------------------------------------------------------------------------
# Odd shapes: never an uncaught exception
# --------------------------------------------------------------------------


@pytest.mark.parametrize("client", [True, 1, 1.5, "client", {"a": 1}])
def test_a_truthy_client_that_is_not_a_list_is_missing_fields(client):
    """`for client in data.get("client") or []` used to raise an uncaught
    TypeError for `true` or a number (outside the try)."""
    assert _code(_doc(client=client)) == "missing_fields"


@pytest.mark.parametrize("client", [None, False, 0, "", [], {}])
def test_an_empty_client_is_package_not_found(client):
    assert _code(_doc(client=client)) == "package_not_found"


@pytest.mark.parametrize("entry", [None, "x", 1, True, [], ["client_info"]])
def test_a_client_entry_that_is_not_an_object_is_missing_fields(entry):
    assert _code(_doc(client=[entry, GOLDEN["client"][1]])) == "missing_fields"


@pytest.mark.parametrize("client_info", [None, "x", 1, [], {}])
def test_a_bad_client_info_is_missing_fields(client_info):
    assert _code(_doc(portico__client_info=client_info)) == "missing_fields"


@pytest.mark.parametrize("android", [None, "x", 1, [], {}])
def test_a_bad_android_client_info_is_missing_fields(android):
    assert _code(_doc(portico__android=android)) == "missing_fields"


@pytest.mark.parametrize("api_key", [None, "AIza-a-bare-string", 1, True, {}, [], [None], ["AIza-x"], [{}],
                                     [{"current_key": None}]])
def test_an_odd_api_key_list_is_missing_fields(api_key):
    assert _code(_doc(portico__api_key=api_key)) == "missing_fields"


def test_a_non_string_package_name_is_not_a_match():
    assert _code(_doc(portico__android={"package_name": 1})) == "package_not_found"


@pytest.mark.parametrize("info", [None, "x", 1, [], {}])
def test_a_bad_project_info_is_missing_fields(info):
    doc = copy.deepcopy(GOLDEN)
    doc["project_info"] = info
    assert _code(doc) == "missing_fields"


@pytest.mark.parametrize("raw", ["", "nope", "[]", "1", "null", "true", '"s"', "{"])
def test_not_a_json_object_is_invalid_json(raw):
    assert _code(raw) == "invalid_json"


# --------------------------------------------------------------------------
# Present but unusable: invalid_values (the app's FirebaseConfig.create)
# --------------------------------------------------------------------------


def test_app_id_from_another_project_is_refused():
    """`mobilesdk_app_id` is `<version>:<project_number>:android:<hash>`; its
    project number must be the file's `project_number` (the sender id), or
    the phone can never register."""
    assert _code(_doc(portico__app_id="1:999999999999:android:0123abcd4567ef89")) == "invalid_values"


@pytest.mark.parametrize("app_id", ["0123abcd", "1:123456789012:ios:0123abcd", "1::android:0123abcd",
                                    "1:123456789012:android:", "1:123456789012:android:01-23",
                                    "x:123456789012:android:0123abcd", f" {APP_ID}"])
def test_a_malformed_app_id_is_refused(app_id):
    assert _code(_doc(portico__app_id=app_id)) == "invalid_values"


@pytest.mark.parametrize("edits", [
    {"info__project_id": "Portico_Test"},          # not a Google project id
    {"info__project_number": "12ab"},              # not digits
    {"portico__api_key": [{"current_key": "AIza with space"}]},
    {"portico__api_key": [{"current_key": "unknown"}]},   # a placeholder
    {"info__project_id": "undefined"},
    # Python's `$` also matches before a final newline; the checks must not.
    {"info__project_id": PROJECT_ID + "\n"},
    {"info__project_number": SENDER_ID + "\n"},
    {"portico__app_id": APP_ID + "\n"},
    {"portico__api_key": [{"current_key": API_KEY + "\n"}]},
])
def test_other_unusable_values_are_refused(edits):
    assert _code(_doc(**edits)) == "invalid_values"


def test_the_codes_never_carry_the_document():
    with pytest.raises(GoogleServicesError) as err:
        parse_google_services(json.dumps(_doc(portico__app_id="1:999999999999:android:0123abcd4567ef89")),
                              PORTICO_PACKAGE)
    assert str(err.value) == "invalid_values"
    assert API_KEY not in repr(err.value) and err.value.__cause__ is None


# The enrollment QR's `fcm` block (S8): `app_firebase_values` decides whether
# the values a push entry holds are ones portico-app's `FirebaseConfig.create`
# accepts. A block the app refuses would refuse the WHOLE enrollment, so the
# server leaves such values out instead.
VALUES = {"project_id": PROJECT_ID, "sender_id": SENDER_ID, "app_id": APP_ID, "api_key": API_KEY}


def test_valid_values_are_the_four_and_nothing_else():
    stored = {**VALUES, "private_key": "SENTINEL-PRIVATE", "client_email": "x@example.invalid"}
    assert app_firebase_values(stored) == VALUES


@pytest.mark.parametrize("edits", [
    {"project_id": "unknown"},  # a placeholder that also fits the project id pattern
    {"project_id": "Portico-Test"},
    {"project_id": "portico-test\n"},
    {"project_id": "abc"},
    {"sender_id": "12345678901x"},
    {"sender_id": " 123456789012"},
    {"sender_id": "null"},
    {"app_id": "1:999:android:0123abcd"},  # another project
    {"app_id": f"1:{SENDER_ID}:ios:0123abcd"},
    {"app_id": APP_ID + "\n"},
    {"api_key": "has space"},
    {"api_key": "x" * 257},
    {"api_key": API_KEY + "\n"},
    {"api_key": "undefined"},
    {"api_key": ""},
    {"api_key": "   "},
    {"api_key": None},
    {"api_key": 12345},
    {"sender_id": int(SENDER_ID)},  # stored values are text; the app reads strings only
])
def test_values_the_app_would_refuse_are_none(edits):
    assert app_firebase_values({**VALUES, **edits}) is None


@pytest.mark.parametrize("stored", [None, "", [], "text", {}, {"project_id": PROJECT_ID}])
def test_missing_or_odd_storage_is_none(stored):
    assert app_firebase_values(stored) is None


def test_what_parse_google_services_returns_is_accepted():
    assert app_firebase_values(parse_google_services(json.dumps(GOLDEN), PORTICO_PACKAGE)) == VALUES
