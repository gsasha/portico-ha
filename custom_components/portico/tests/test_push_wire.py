"""The push wire contract `push/v2` (M6 E3; contract file `contract/push_v2.json`, recorded here) and the
credential parsers (sender design §2.3, §4.1, §4.2, §5.2). Plain pytest, no HA harness: these are pure
functions. Run in the plain session (docs/development/running-tests.md).

push/v2 is push/v1 with `pv: "2"`, the door ref derived from the door's endpoint id (`d-` and 32 hex, never an entry
id, never a URL), and two optional reserved keys, `ct` and `from_ref`, for M7's house calls."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from custom_components.portico.house import house_from_ref
from custom_components.portico.provider import Door
from custom_components.portico.push import payload
from custom_components.portico.push.const import GOOGLE_TOKEN_URI, PORTICO_PACKAGE
from custom_components.portico.push.credentials import (
    CredentialError,
    GoogleServicesError,
    parse_google_services,
    parse_service_account,
)

from .fake_google import KEY_ID, service_account_info

# A door whose id is a URL (the live gate's is): the wire carries only the ref derived from its endpoint id.
GATE_URL = "http://10.6.0.7:8091"
GATE = Door.from_dict("portico_intercom", {
    "id": GATE_URL, "name": "Vehicle gate", "driver": "gate_sip_observer", "capabilities": {},
    "camera_entity_id": None, "available": True, "away_media": False})
OTHER = Door.from_dict("portico_intercom", {
    "id": "vto-front.example", "name": "Front door", "driver": "dahua_sip", "capabilities": {},
    "camera_entity_id": None, "available": True, "away_media": False})
DOOR = GATE.door_ref
CALL = "gso-1759700000000000000-1-a1b2c3"
T = 1_759_700_000_000
CONTRACT_PATH = Path(__file__).resolve().parents[3] / "contract" / "push_v2.json"
REGENERATE_ENV = "PORTICO_REGENERATE_WS_CONTRACT"

# --------------------------------------------------------------------------
# §4.1 golden messages
# --------------------------------------------------------------------------


def test_the_door_ref_is_derived_from_the_endpoint_id_and_fits_the_id_grammar():
    assert DOOR == "d-" + hashlib.sha256(f"portico_intercom:{GATE_URL}".encode()).hexdigest()[:32]
    assert len(DOOR) == 34 and payload.build_ended(DOOR, CALL, T)["door_ref"] == DOOR
    assert OTHER.door_ref != DOOR
    # the URL itself is not an id the wire accepts: only the derived ref is
    with pytest.raises(payload.PayloadError):
        payload.build_ring(GATE_URL, CALL, T, 45)


def test_golden_ring():
    assert payload.build_ring(DOOR, CALL, T, 45) == {
        "pv": "2", "kind": "ring", "door_ref": DOOR, "call_id": CALL,
        "seq": "1", "t": str(T), "exp": str(T + 45_000),
    }


def test_the_wire_version_is_2():
    assert payload.PV == "2"


def test_golden_ring_for_a_house_call_reserves_ct_and_from_ref():
    """M7 (HC-8): `ct: house` and an opaque `from_ref`; absent, `ct` is `door`. A 0.5.0 app ignores `ct: house`."""
    assert payload.build_ring(DOOR, CALL, T, 45, ct="house", from_ref="ep-dina") == {
        "pv": "2", "kind": "ring", "door_ref": DOOR, "call_id": CALL,
        "seq": "1", "t": str(T), "exp": str(T + 45_000), "ct": "house", "from_ref": "ep-dina",
    }
    assert payload.build_ended(DOOR, CALL, T, ct="door")["ct"] == "door"
    assert "ct" not in payload.build_ended(DOOR, CALL, T) and "from_ref" not in payload.build_answered(DOOR, CALL, T)


@pytest.mark.parametrize("bad", ["video", "", "HOUSE", 1])
def test_ct_is_door_or_house_only(bad):
    with pytest.raises(payload.PayloadError):
        payload.build_ring(DOOR, CALL, T, 45, ct=bad)


@pytest.mark.parametrize("bad", ["", "has space", "a/b", "x" * 129, "http://10.6.0.7:8091"])
def test_from_ref_is_an_opaque_id(bad):
    with pytest.raises(payload.PayloadError):
        payload.build_answered(DOOR, CALL, T, from_ref=bad)


DINA_REF = house_from_ref("ep-dina")
HOUSE_CALL = "hso-1759700000000000000-1-a1b2c3"


def test_golden_house_ring_is_ct_house_with_a_from_ref_and_no_door_ref():
    """M7 3c (15.5): the push of a house ring for an away phone. `door_ref` is ABSENT (never None, never empty)."""
    assert payload.build_house_ring(DINA_REF, HOUSE_CALL, T, 45) == {
        "pv": "2", "kind": "ring", "call_id": HOUSE_CALL, "seq": "1", "t": str(T), "ct": "house",
        "from_ref": DINA_REF, "exp": str(T + 45_000),
    }
    assert payload.build_house_answered(DINA_REF, HOUSE_CALL, T) == {
        "pv": "2", "kind": "answered", "call_id": HOUSE_CALL, "seq": "2", "t": str(T), "ct": "house",
        "from_ref": DINA_REF, "by": "other",
    }
    assert payload.build_house_ended(DINA_REF, HOUSE_CALL, T) == {
        "pv": "2", "kind": "ended", "call_id": HOUSE_CALL, "seq": "3", "t": str(T), "ct": "house", "from_ref": DINA_REF,
    }


def test_the_from_ref_is_opaque_and_never_the_endpoint_id():
    assert DINA_REF.startswith("e-") and len(DINA_REF) == 34 and "dina" not in DINA_REF
    assert DINA_REF == "e-" + hashlib.sha256(b"ep-dina").hexdigest()[:32] and house_from_ref("ep-alice") != DINA_REF


@pytest.mark.parametrize("build", [payload.build_house_ring, payload.build_house_answered, payload.build_house_ended])
@pytest.mark.parametrize("bad", ["", None, "Jane Doe", "http://10.6.0.7:8091", "x" * 129, "a b"])
def test_a_house_message_refuses_anything_but_an_opaque_from_ref(build, bad):
    args = (bad, HOUSE_CALL, T, 45) if build is payload.build_house_ring else (bad, HOUSE_CALL, T)
    with pytest.raises(payload.PayloadError):
        build(*args)


def test_a_house_message_never_carries_a_door_ref_or_a_name():
    for data in (payload.build_house_ring(DINA_REF, HOUSE_CALL, T, 45), payload.build_house_answered(DINA_REF, HOUSE_CALL, T),
                 payload.build_house_ended(DINA_REF, HOUSE_CALL, T)):
        assert "door_ref" not in data and data["ct"] == "house" and data["pv"] == "2"
        assert set(data) <= {"pv", "kind", "call_id", "seq", "t", "exp", "by", "ct", "from_ref"}
        assert payload.data_size(data) < 400
    # and a door message built by the door builders still never has ct or from_ref
    for data in (payload.build_ring(DOOR, CALL, T, 45), payload.build_answered(DOOR, CALL, T),
                 payload.build_ended(DOOR, CALL, T)):
        assert "ct" not in data and "from_ref" not in data and data["door_ref"] == DOOR


def test_golden_answered():
    assert payload.build_answered(DOOR, CALL, T) == {
        "pv": "2", "kind": "answered", "door_ref": DOOR, "call_id": CALL,
        "seq": "2", "t": str(T), "by": "other",
    }


def test_golden_ended():
    assert payload.build_ended(DOOR, CALL, T) == {
        "pv": "2", "kind": "ended", "door_ref": DOOR, "call_id": CALL,
        "seq": "3", "t": str(T),
    }


def test_golden_test_message():
    """v1.1 `kind: "test"` (spec §8.3 step 2; P2 plan G5, Q6): what
    portico-app's `PushMessage.parse` reads for a Test, `pv` and `kind`, plus
    the optional `t`. No door, no call id, no seq: it touches no call state."""
    assert payload.build_test(T) == {"pv": "2", "kind": "test", "t": str(T)}


@pytest.mark.parametrize("data", [
    payload.build_ring(DOOR, CALL, T, 45),
    payload.build_answered(DOOR, CALL, T),
    payload.build_ended(DOOR, CALL, T),
    payload.build_test(T),
])
def test_every_value_is_a_string_and_no_reserved_key(data):
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in data.items())
    for key in data:
        assert key not in {"from", "message_type", "collapse_key"}
        assert not key.startswith(("google.", "gcm."))


@pytest.mark.parametrize("bad", ["", None, 5, "x" * 129, "has space", "a/b"])
def test_builders_refuse_a_malformed_id(bad):
    with pytest.raises(payload.PayloadError):
        payload.build_ring(DOOR, bad, T, 45)
    with pytest.raises(payload.PayloadError):
        payload.build_ended(bad, CALL, T)


# --------------------------------------------------------------------------
# §4.2 Android options
# --------------------------------------------------------------------------


def test_ring_message_is_data_only_high_priority_no_collapse_key_ttl_to_exp():
    data = payload.build_ring(DOOR, CALL, T, 45)
    msg = payload.build_message(kind="fid", registration="FID-abc", package=PORTICO_PACKAGE,
                                data=data, now_ms=T + 10_000, ring_window_s=45)
    m = msg["message"]
    assert set(m) == {"fid", "data", "android"}
    assert m["fid"] == "FID-abc"
    assert "notification" not in m
    assert m["android"] == {
        "priority": "HIGH",
        "ttl": "35s",
        "restricted_package_name": PORTICO_PACKAGE,
        "direct_boot_ok": False,
    }
    assert "collapse_key" not in m["android"]


@pytest.mark.parametrize(("now_offset_ms", "ttl"), [
    (0, "45s"),            # at the transition: the whole window
    (-60_000, "45s"),      # never more than ring_window, even on a clock step
    (44_500, "1s"),        # ceil, and never below 1 s
    (90_000, "1s"),        # past exp: 1 s, not 0 (TTL 0 rejected, §4.2)
])
def test_ring_ttl(now_offset_ms, ttl):
    data = payload.build_ring(DOOR, CALL, T, 45)
    msg = payload.build_message(kind="token", registration="tok", package=PORTICO_PACKAGE,
                                data=data, now_ms=T + now_offset_ms, ring_window_s=45)
    assert msg["message"]["android"]["ttl"] == ttl


@pytest.mark.parametrize("builder", [payload.build_answered, payload.build_ended])
def test_dismiss_ttl_is_60s_and_high(builder):
    msg = payload.build_message(kind="token", registration="tok", package=PORTICO_PACKAGE,
                                data=builder(DOOR, CALL, T), now_ms=T, ring_window_s=45)
    assert msg["message"]["token"] == "tok"
    assert "fid" not in msg["message"]
    assert msg["message"]["android"]["ttl"] == "60s"
    assert msg["message"]["android"]["priority"] == "HIGH"


def test_test_message_is_data_only_high_priority_60s():
    """Same Android rules as a dismiss: HIGH (the app posts a visible
    notification for it), TTL 60 s, data only, the package pinned."""
    msg = payload.build_message(kind="fid", registration="FID-abc", package=PORTICO_PACKAGE,
                                data=payload.build_test(T), now_ms=T, ring_window_s=45)
    m = msg["message"]
    assert set(m) == {"fid", "data", "android"}
    assert m["data"] == {"pv": "2", "kind": "test", "t": str(T)}
    assert m["android"] == {"priority": "HIGH", "ttl": "60s", "restricted_package_name": PORTICO_PACKAGE,
                            "direct_boot_ok": False}
    assert payload.data_size(m["data"]) < 100


def test_registration_kind_must_be_fid_or_token():
    with pytest.raises(payload.PayloadError):
        payload.build_message(kind="topic", registration="x", package=PORTICO_PACKAGE,
                              data=payload.build_ended(DOOR, CALL, T), now_ms=T, ring_window_s=45)


def test_a_ring_is_far_under_the_4096_byte_limit():
    data = payload.build_ring(DOOR, "c" * 128, T, 45)
    size = payload.data_size(data)
    assert size < 400, size
    assert size <= payload.MAX_DATA_BYTES == 4096


def test_an_oversized_payload_is_refused():
    with pytest.raises(payload.PayloadError):
        payload.check_size({"k": "v" * 5000})


def test_no_secret_url_or_name_can_reach_the_payload():
    """Only the §4.1 keys: no URL, host, name or token field exists to fill."""
    for data in (payload.build_ring(DOOR, CALL, T, 45), payload.build_answered(DOOR, CALL, T),
                 payload.build_ended(DOOR, CALL, T), payload.build_test(T)):
        assert set(data) <= {"pv", "kind", "door_ref", "call_id", "seq", "t", "exp", "by"}
        blob = json.dumps(data)
        assert "http" not in blob and "@" not in blob


# --------------------------------------------------------------------------
# §2.3 service-account validation
# --------------------------------------------------------------------------


def test_a_valid_service_account_parses_and_hides_its_key():
    info = service_account_info(GOOGLE_TOKEN_URI)
    account = parse_service_account(json.dumps(info))
    assert account.project_id == "portico-test"
    assert account.private_key_id == KEY_ID
    assert account.token_uri == GOOGLE_TOKEN_URI
    assert "PRIVATE KEY" not in repr(account)
    assert "PRIVATE KEY" not in str(account)


@pytest.mark.parametrize(("mutate", "code"), [
    (lambda i: "{not json", "invalid_json"),
    (lambda i: json.dumps([i]), "invalid_json"),
    (lambda i: json.dumps({**i, "type": "authorized_user"}), "not_service_account"),
    (lambda i: json.dumps({k: v for k, v in i.items() if k != "client_email"}), "missing_fields"),
    (lambda i: json.dumps({k: v for k, v in i.items() if k != "private_key"}), "missing_fields"),
    (lambda i: json.dumps({**i, "private_key": "-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n"}),
     "invalid_key"),
    (lambda i: json.dumps({**i, "token_uri": "https://evil.example/token"}), "token_uri_not_allowed"),
    (lambda i: json.dumps({**i, "project_id": "../../x"}), "invalid_project_id"),
])
def test_an_invalid_service_account_is_refused_without_echoing_it(mutate, code):
    info = service_account_info(GOOGLE_TOKEN_URI)
    with pytest.raises(CredentialError) as err:
        parse_service_account(mutate(info))
    assert err.value.code == code
    assert str(err.value) == code
    assert "PRIVATE KEY" not in str(err.value) and err.value.__cause__ is None


def test_the_production_token_uri_pin():
    """A crafted JSON cannot point the signed assertion elsewhere: by default
    only Google's own token URI is accepted (§2.3)."""
    info = service_account_info("https://oauth2.googleapis.com.evil.example/token")
    with pytest.raises(CredentialError):
        parse_service_account(json.dumps(info))
    assert parse_service_account(json.dumps(service_account_info(GOOGLE_TOKEN_URI)))


def test_a_dsa_or_ec_key_is_not_rsa():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    info = {**service_account_info(GOOGLE_TOKEN_URI), "private_key": pem}
    with pytest.raises(CredentialError) as err:
        parse_service_account(json.dumps(info))
    assert err.value.code == "invalid_key"


# --------------------------------------------------------------------------
# §5.2 google-services.json extraction
# --------------------------------------------------------------------------

GOOGLE_SERVICES = {
    "project_info": {"project_number": "123456789012", "project_id": "portico-test",
                     "storage_bucket": "portico-test.appspot.com"},
    "client": [
        {"client_info": {"mobilesdk_app_id": "1:123456789012:android:aaaa",
                         "android_client_info": {"package_name": "com.meda.ha_intercom"}},
         "api_key": [{"current_key": "AIza-old-app-key"}]},
        {"client_info": {"mobilesdk_app_id": "1:123456789012:android:bbbb",
                         "android_client_info": {"package_name": PORTICO_PACKAGE}},
         "api_key": [{"current_key": "AIza-portico-key"}]},
    ],
    "configuration_version": "1",
}


def test_google_services_extracts_the_four_values_for_portico_only():
    values = parse_google_services(json.dumps(GOOGLE_SERVICES), PORTICO_PACKAGE)
    assert values == {
        "project_id": "portico-test",
        "sender_id": "123456789012",
        "app_id": "1:123456789012:android:bbbb",
        "api_key": "AIza-portico-key",
    }


@pytest.mark.parametrize(("raw", "code"), [
    ("nope", "invalid_json"),
    (json.dumps({**GOOGLE_SERVICES, "client": GOOGLE_SERVICES["client"][:1]}), "package_not_found"),
    (json.dumps({**GOOGLE_SERVICES, "project_info": {}}), "missing_fields"),
])
def test_google_services_errors(raw, code):
    with pytest.raises(GoogleServicesError) as err:
        parse_google_services(raw, PORTICO_PACKAGE)
    assert err.value.code == code


# --------------------------------------------------------------------------
# §8.2 the contract file shared with portico-app: contract/push_v2.json, recorded here
# --------------------------------------------------------------------------

KNOWN = [DOOR]
RING_WINDOW = 45
SKEW = 15_000
# What portico-app calls each outcome (`Outcome.contractName`): ring, silent, dismiss, missed, and v1.1's quiet (the `test`
# kind). A `ct: house` message is `silent` on 0.5.0 (`Ignored(UNKNOWN_KIND)`).
EXPECTS = {"ring", "silent", "dismiss", "missed", "quiet"}


def _ring(t=T, call=CALL, door=DOOR, **kw):
    return payload.build_ring(door, call, t, RING_WINDOW, **kw)


def _answered(t=T + 5_000, call=CALL, door=DOOR, **kw):
    return payload.build_answered(door, call, t, **kw)


def _ended(t=T + 6_000, call=CALL, door=DOOR, **kw):
    return payload.build_ended(door, call, t, **kw)


def _house_ring(t=T, call=HOUSE_CALL, ref=DINA_REF):
    return payload.build_house_ring(ref, call, t, RING_WINDOW)


def _house_answered(t=T + 5_000, call=HOUSE_CALL, ref=DINA_REF):
    return payload.build_house_answered(ref, call, t)


def _house_ended(t=T + 6_000, call=HOUSE_CALL, ref=DINA_REF):
    return payload.build_house_ended(ref, call, t)


def _cases() -> list[dict]:
    """The cases, built by the real builders (the messages are what the sender emits, byte for byte); `expect` is the
    reaction the app's decision function must give to the LAST message (the app owns the real one; `_reference_decide`
    below is only a coherence check)."""
    other_call = "gso-1759700100000000000-2-d4e5f6"
    return [
        {"name": "ring", "messages": [_ring()], "now_ms": T + 1_000, "expect": "ring"},
        {"name": "ring delivered late but inside the skew allowance", "messages": [_ring()], "now_ms": T + 60_000,
         "expect": "ring"},
        {"name": "late ring", "messages": [_ring()], "now_ms": T + 60_001, "expect": "missed"},
        {"name": "duplicate ring", "messages": [_ring(), _ring()], "now_ms": T + 2_000, "expect": "silent"},
        {"name": "answered after ring", "messages": [_ring(), _answered()], "now_ms": T + 6_000, "expect": "dismiss"},
        {"name": "ended after ring", "messages": [_ring(), _ended()], "now_ms": T + 7_000, "expect": "missed"},
        {"name": "ended after answered", "messages": [_ring(), _answered(), _ended(T + 9_000)],
         "now_ms": T + 10_000, "expect": "silent"},
        {"name": "duplicate answered", "messages": [_ring(), _answered(), _answered(T + 5_500)],
         "now_ms": T + 7_000, "expect": "silent"},
        {"name": "answered before ring", "messages": [_answered(), _ring()], "now_ms": T + 6_000, "expect": "silent"},
        {"name": "ended before ring", "messages": [_ended(), _ring()], "now_ms": T + 7_000, "expect": "silent"},
        {"name": "answered with no ring", "messages": [_answered()], "now_ms": T + 6_000, "expect": "silent"},
        {"name": "second call after the first ended",
         "messages": [_ring(), _ended(), _ring(T + 100_000_000, call=other_call)],
         "now_ms": T + 100_001_000, "expect": "ring"},
        {"name": "unknown pv (the v1 shape is not sent and not read)",
         "messages": [{**_ring(), "pv": "1"}], "now_ms": T + 1_000, "expect": "silent"},
        {"name": "unknown pv (a later version)", "messages": [{**_ring(), "pv": "3"}], "now_ms": T + 1_000,
         "expect": "silent"},
        {"name": "missing keys", "messages": [{k: v for k, v in _ring().items() if k != "call_id"}],
         "now_ms": T + 1_000, "expect": "silent"},
        {"name": "wrong door_ref", "messages": [_ring(door=OTHER.door_ref)], "now_ms": T + 1_000, "expect": "silent"},
        {"name": "ended with no ring seen", "messages": [_ended()], "now_ms": T + 7_000, "expect": "silent"},
        {"name": "test", "messages": [payload.build_test(T)], "now_ms": T + 1_000, "expect": "quiet"},
        {"name": "ring for a door whose id is a URL (the wire carries the derived ref)",
         "messages": [_ring()], "now_ms": T + 1_000, "expect": "ring", "door_id": GATE_URL},
        {"name": "ring with ct door spelled out", "messages": [_ring(ct="door")], "now_ms": T + 1_000,
         "expect": "ring"},
        {"name": "a house call is ignored on 0.5.0 (ct house, from_ref)",
         "messages": [_ring(ct="house", from_ref="ep-dina")], "now_ms": T + 1_000, "expect": "silent"},
        {"name": "a house call does not block a door call with the same ids",
         "messages": [_ring(ct="house", from_ref="ep-dina"), _ring()], "now_ms": T + 1_000, "expect": "ring"},
        {"name": "a house call's answered and ended are ignored too",
         "messages": [_ring(), _answered(ct="house", from_ref="ep-dina")], "now_ms": T + 6_000, "expect": "silent"},
        # ---- v2.1 (M7 3c, 15.5): what the sender sends for an AWAY phone. APPENDED; the 23 above are untouched. No
        # `door_ref`. App 0.5.0 is silent on every one of them (ct is checked before door_ref is required); app 0.6.0's
        # house tail will change these expectations in the same PR that re-vendors the file.
        {"name": "v2.1 a house ring for an away phone (ct house, from_ref, no door_ref)",
         "messages": [_house_ring()], "now_ms": T + 1_000, "expect": "silent"},
        {"name": "v2.1 a house ring that arrives late is silent too",
         "messages": [_house_ring()], "now_ms": T + 60_001, "expect": "silent"},
        {"name": "v2.1 a house ring then answered elsewhere",
         "messages": [_house_ring(), _house_answered()], "now_ms": T + 6_000, "expect": "silent"},
        {"name": "v2.1 a house ring then ended",
         "messages": [_house_ring(), _house_ended()], "now_ms": T + 7_000, "expect": "silent"},
        {"name": "v2.1 a house ring does not block a door ring with the same call id",
         "messages": [_house_ring(call=CALL), _ring()], "now_ms": T + 1_000, "expect": "ring"},
        {"name": "v2.1 a house ended does not end a door call with the same call id",
         "messages": [_ring(), _house_ended(call=CALL), _answered()], "now_ms": T + 6_000, "expect": "dismiss"},
    ]


def _recorded() -> dict:
    return {
        "contract": "push/v2",
        "about": (
            "Shared fixture between portico-ha (sender) and portico-app (receiver); sender design §4, §8.2. Each case is "
            "a sequence of FCM data maps as delivered to the phone, the phone's clock (now_ms) when the LAST one arrives, "
            "and the expected reaction to that last message: ring (full-screen ring), silent (nothing), dismiss (stop "
            "ringing, 'Answered elsewhere'), missed (stop or skip the ring, 'Missed call'), quiet (one quiet "
            "notification, never a ring, no call state). RECORDED by portico-ha's tests/test_push_wire.py from the real "
            "builders; portico-app vendors this file with a pinned sha256 and runs it against its pure decision function. "
            "Any change is a contract version bump on both sides. push/v2 (2026-10-20, M6 E3) is v1.1 with pv \"2\", "
            "`door_ref` derived from the door's endpoint id (`d-` and 32 hex: never an entry id, never a URL, so a door whose "
            "id is a URL still fits the id grammar ^[A-Za-z0-9._:-]{1,128}$), and two optional keys reserved for the house "
            "calls (M7): `ct`, the call type, `door` when absent or `house`, and `from_ref`, an opaque caller reference. "
            "The app checks `ct` BEFORE it requires `door_ref`: `ct: house` is ignored on 0.5.0 (Ignored(UNKNOWN_KIND), "
            "journalled, not rung). From M7 3c (v2.1, cases appended, no version change) the sender sends both keys for "
            "an away phone's house ring: ct \"house\", an opaque from_ref and NO door_ref; the v2.1 cases are silent on "
            "0.5.0. kind \"test\" carries only pv, kind and t. A message with pv \"1\" is unknown and ignored."
        ),
        "ring_window_s": RING_WINDOW,
        "clock_skew_ms": SKEW,
        "known_door_refs": KNOWN,
        "cases": [{k: v for k, v in case.items() if k != "door_id"} for case in _cases()],
    }


def _contract() -> dict:
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def test_push_v2_contract_matches_the_builders():
    text = json.dumps(_recorded(), indent=2, ensure_ascii=False) + "\n"
    if os.environ.get(REGENERATE_ENV) == "1":
        CONTRACT_PATH.write_text(text, encoding="utf-8")
    assert CONTRACT_PATH.exists(), f"{CONTRACT_PATH} is missing; run with {REGENERATE_ENV}=1"
    assert json.loads(text) == _contract(), (
        f"{CONTRACT_PATH.name} differs from what the builders emit; if intended, regenerate it with {REGENERATE_ENV}=1, "
        "update CONTRACT_SHA256 and re-vendor it in portico-app")


def test_contract_messages_are_exactly_what_the_builders_emit():
    contract = _contract()
    window = contract["ring_window_s"]
    well_formed = 0
    house_built = 0
    for case in contract["cases"]:
        for msg in case["messages"]:
            if msg.get("pv") == "2" and msg.get("kind") == "test":
                assert msg == payload.build_test(int(msg["t"])), case["name"]
                well_formed += 1
                continue
            if msg.get("pv") == "2" and msg.get("ct") == "house" and "door_ref" not in msg and "from_ref" in msg:
                t, call, ref = int(msg["t"]), msg["call_id"], msg["from_ref"]
                if msg["kind"] == "ring":
                    built = payload.build_house_ring(ref, call, t, window)
                elif msg["kind"] == "answered":
                    built = payload.build_house_answered(ref, call, t)
                else:
                    built = payload.build_house_ended(ref, call, t)
                assert msg == built, case["name"]
                well_formed += 1
                house_built += 1
                continue
            if msg.get("pv") != "2" or not {"kind", "door_ref", "call_id", "t"} <= msg.keys():
                continue
            t, door, call = int(msg["t"]), msg["door_ref"], msg["call_id"]
            extra = {k: msg[k] for k in ("ct", "from_ref") if k in msg}
            if msg["kind"] == "ring":
                built = payload.build_ring(door, call, t, window, **extra)
            elif msg["kind"] == "answered":
                built = payload.build_answered(door, call, t, **extra)
            else:
                built = payload.build_ended(door, call, t, **extra)
            assert msg == built, case["name"]
            well_formed += 1
    assert well_formed >= 20
    assert house_built >= 8       # the v2.1 cases are built by the house builders, not typed


def _reference_decide(contract: dict, case: dict) -> str:
    """A reference for portico-app's pure decision function (§4.3), only to
    prove the fixture's expectations are coherent. The app owns the real one;
    this returns the reaction to the LAST message of the case."""
    store: dict[tuple, set] = {}
    outcome = "silent"
    skew = contract["clock_skew_ms"]
    for msg in case["messages"]:
        outcome = "silent"
        if msg.get("pv") != "2":
            continue
        if msg.get("kind") == "test":
            outcome = "quiet"  # one quiet notification, no call state
            continue
        if msg.get("ct", "door") != "door":
            continue  # push/v2: `ct` is checked before `door_ref` is required; a house call is ignored on 0.5.0
        if not {"kind", "door_ref", "call_id", "seq", "t"} <= msg.keys():
            continue
        if msg["door_ref"] not in contract["known_door_refs"]:
            continue
        seen = store.setdefault((msg["door_ref"], msg["call_id"]), set())
        kind = msg["kind"]
        if kind == "ring":
            if seen:
                continue
            seen.add("ring")
            outcome = "missed" if case["now_ms"] > int(msg["exp"]) + skew else "ring"
        elif kind in ("answered", "ended"):
            terminal = seen & {"answered", "ended"}
            rang = "ring" in seen
            seen.add(kind)
            if terminal or not rang:
                continue
            outcome = "dismiss" if kind == "answered" else "missed"
    return outcome


# The file's sha256. portico-app vendors the file byte for byte and pins the
# same value (its ContractTest.PINNED_SHA256): a change to the file fails here
# first, and is a contract bump on both sides (§8.2).
CONTRACT_SHA256 = "aeb2b1ddaa09067f178ec54410ff55abf4d2f663b6603a2e81e97f55df71e8cb"
CONTRACT_VERSION = "push/v2"


def test_contract_file_is_pinned():
    assert hashlib.sha256(CONTRACT_PATH.read_bytes()).hexdigest() == CONTRACT_SHA256, (
        "contract/push_v2.json changed: bump its version, update CONTRACT_SHA256 and re-vendor it in portico-app")


# sha256 of the first 23 cases (the v2 cases) as `json.dumps(cases, separators=(",", ":"), ensure_ascii=False)`, recorded from
# main before 3c. v2.1 only APPENDS: these never change.
V2_CASES_SHA256 = "22c8c4738069aaa885161744cf278dcd06be9fa1d1ba457b0f4bbd0d7641e4ac"
V2_CASE_COUNT = 23


def test_the_v2_cases_are_untouched_and_v2_1_only_appends():
    contract = _contract()
    first = json.dumps(contract["cases"][:V2_CASE_COUNT], separators=(",", ":"), ensure_ascii=False)
    assert hashlib.sha256(first.encode()).hexdigest() == V2_CASES_SHA256
    assert len(contract["cases"]) > V2_CASE_COUNT
    assert all(c["name"].startswith("v2.1 ") for c in contract["cases"][V2_CASE_COUNT:])
    assert not any(c["name"].startswith("v2.1 ") for c in contract["cases"][:V2_CASE_COUNT])
    assert contract["contract"] == "push/v2"      # no version bump: the app checks this name


def test_contract_expectations_are_coherent():
    contract = _contract()
    assert contract["contract"] == CONTRACT_VERSION
    assert payload.PV == "2"
    assert set(contract) == {"contract", "about", "ring_window_s", "clock_skew_ms", "known_door_refs", "cases"}
    assert contract["known_door_refs"] == [DOOR] and all(r.startswith("d-") and len(r) == 34 for r in KNOWN)
    names = [c["name"] for c in contract["cases"]]
    assert len(names) == len(set(names)) == len(_cases())
    for required in ("ring", "duplicate ring", "late ring", "answered before ring",
                     "ended before ring", "wrong door_ref", "ended with no ring seen", "test",
                     "unknown pv (the v1 shape is not sent and not read)",
                     "ring for a door whose id is a URL (the wire carries the derived ref)",
                     "a house call is ignored on 0.5.0 (ct house, from_ref)",
                     "v2.1 a house ring for an away phone (ct house, from_ref, no door_ref)"):
        assert required in names
    for case in contract["cases"]:
        assert case["expect"] in EXPECTS, case["name"]
        assert _reference_decide(contract, case) == case["expect"], case["name"]
    # no case carries a URL, an entry id or anything but a derived ref as a door ref
    refs = {m["door_ref"] for c in contract["cases"] for m in c["messages"] if "door_ref" in m}
    assert all(r.startswith("d-") and len(r) == 34 for r in refs)
    # a house message (v2.1) has an opaque from_ref and never a door_ref; no case carries a name in either
    house = [m for c in contract["cases"] for m in c["messages"] if m.get("ct") == "house" and "door_ref" not in m]
    assert len(house) >= 8 and all(m["ct"] == "house" and "door_ref" not in m for m in house)
    assert all(m["from_ref"] == DINA_REF for m in house) and "dina" not in DINA_REF
    assert GATE_URL not in json.dumps(contract)



@pytest.mark.parametrize("project_id", ["portico-test\n", "portico-test\r\n", "\nportico-test"])
def test_a_project_id_with_a_line_break_is_refused(project_id):
    """The project id goes into the FCM URL path. `re.match` with `$` accepted
    a trailing newline; only a full match is a project id."""
    info = {**service_account_info(GOOGLE_TOKEN_URI), "project_id": project_id}
    with pytest.raises(CredentialError) as err:
        parse_service_account(json.dumps(info))
    assert err.value.code == "invalid_project_id"


@pytest.mark.parametrize("value", ["door-1\n", "gso-1-2-abc\n"])
def test_a_message_id_with_a_trailing_newline_is_refused(value):
    with pytest.raises(payload.PayloadError):
        payload.build_ring(value, "gso-1-2-abc", 1, 45)
    with pytest.raises(payload.PayloadError):
        payload.build_ring("door-1", value, 1, 45)


def test_an_error_code_with_a_trailing_newline_is_not_a_code():
    from custom_components.portico.push import fcm, oauth

    assert fcm._code("UNREGISTERED") == "UNREGISTERED"
    assert fcm._code("UNREGISTERED\n") == ""
    assert oauth._short_code("invalid_grant") == "invalid_grant"
    assert oauth._short_code("invalid_grant\n") == ""
