# ruff: noqa: F811 - test parameters named after the imported fixtures
"""Offline registration (contract `reg_string_spec.md` v1, Alice 2026-10-07).

A phone that cannot reach Home Assistant shows a registration string; the
admin pastes it under the push ring notification entry's Configure, "Add a phone's
registration", and HA registers the phone exactly as the phone's own
`portico/push/register` would.

The string is ONE base64url token: [0x01][CRC-32 BE of the JSON][compact JSON].
Tests build it with their own encoder below, independent of the parser.

HA-harness session; the only network peer is the local fake Google.
"""
from __future__ import annotations

import base64
import json
import logging
import random
import zlib

import pytest
from homeassistant.data_entry_flow import FlowResultType

from custom_components.portico import push_flow
from custom_components.portico.push import const, reg_string

from .test_push_flow import _options
from .test_push_registry import (  # noqa: F401 - fixtures by name
    _custom_integrations,
    _manager,
    _observer_entry,
    _phone_user,
    _push_entry,
    _register_msg,
    fake_transport,
    google,
)

# The contract's golden vector (both sides MUST pass it).
GOLDEN_OBJ = {"v": 1, "e": "E0123456789abcdefABCDEF-_xyz0123456789AB", "i": "inst-0f1e2d3c4b5a", "k": "fid",
              "r": "cFAKEfid0000000000001A", "p": "com.meda.portico", "f": "portico-test", "a": "0.1.0"}
GOLDEN_JSON = ('{"v":1,"e":"E0123456789abcdefABCDEF-_xyz0123456789AB","i":"inst-0f1e2d3c4b5a","k":"fid",'
               '"r":"cFAKEfid0000000000001A","p":"com.meda.portico","f":"portico-test","a":"0.1.0"}')
GOLDEN = ("AWDBl1t7InYiOjEsImUiOiJFMDEyMzQ1Njc4OWFiY2RlZkFCQ0RFRi1feHl6MDEyMzQ1Njc4OUFCIiwiaSI6Imluc3QtMGYx"
          "ZTJkM2M0YjVhIiwiayI6ImZpZCIsInIiOiJjRkFLRWZpZDAwMDAwMDAwMDAwMDFBIiwicCI6ImNvbS5tZWRhLnBvcnRpY28i"
          "LCJmIjoicG9ydGljby10ZXN0IiwiYSI6IjAuMS4wIn0")
ALICE_EID = GOLDEN_OBJ["e"]
BOB_EID = "B" * 43
F_REG = push_flow.F_REGISTRATION


def _encode(obj=None, *, version=1, crc=None, raw: bytes | None = None, **changes) -> str:
    """The contract's writer: compact JSON in the given key order, then
    [version][CRC-32 BE][JSON] as unpadded base64url."""
    if raw is None:
        body = dict(GOLDEN_OBJ if obj is None else obj)
        body.update(changes)
        raw = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    c = zlib.crc32(raw) if crc is None else crc
    return base64.urlsafe_b64encode(bytes([version]) + c.to_bytes(4, "big") + raw).decode("ascii").rstrip("=")


def _without(key):
    return {k: v for k, v in GOLDEN_OBJ.items() if k != key}


def _code(text):
    with pytest.raises(reg_string.RegStringError) as err:
        reg_string.parse(text)
    return err.value.code


# --------------------------------------------------------------------------
# The parser
# --------------------------------------------------------------------------


def test_golden_vector():
    assert len(GOLDEN) == 235
    assert len(GOLDEN_JSON.encode()) == 171 and f"{zlib.crc32(GOLDEN_JSON.encode()):08x}" == "60c1975b"
    assert _encode() == GOLDEN  # this file's own writer agrees with the contract
    parsed = reg_string.parse(GOLDEN)
    assert (parsed.enroll_id, parsed.installation_id, parsed.kind, parsed.registration, parsed.package,
            parsed.fcm_project_id, parsed.app_version) == (
        GOLDEN_OBJ["e"], "inst-0f1e2d3c4b5a", "fid", "cFAKEfid0000000000001A", "com.meda.portico",
        "portico-test", "0.1.0")


def test_the_parsed_value_shows_nothing_in_its_repr():
    text = repr(reg_string.parse(GOLDEN)) + str(reg_string.parse(GOLDEN))
    for value in GOLDEN_OBJ.values():
        if isinstance(value, str):
            assert value not in text


@pytest.mark.parametrize("seed", range(25))
def test_whitespace_anywhere_is_ignored(seed):
    rnd = random.Random(seed)
    chars = list(GOLDEN)
    for _ in range(rnd.randint(1, 60)):
        chars.insert(rnd.randint(0, len(chars)), rnd.choice([" ", "\n", "\t", "\r\n", "  ", " ", " "]))
    assert reg_string.parse("".join(chars)).registration == GOLDEN_OBJ["r"]


def test_standard_base64_and_padding_are_accepted():
    # Find an installation id whose encoding has both '-' and '_' in it.
    for n in range(500):
        text = _encode(i=f"inst-{n:04d}-ÿþ>?")
        if "-" in text and "_" in text:
            break
    else:
        raise AssertionError("no sample with both url-safe characters")
    std = text.replace("-", "+").replace("_", "/") + "=" * (-len(text) % 4)
    assert std != text
    assert reg_string.parse(std).installation_id == reg_string.parse(text).installation_id
    assert reg_string.parse(text + "==").installation_id == f"inst-{n:04d}-ÿþ>?"


@pytest.mark.parametrize("cut", [1, 2, 3, 120])
def test_a_truncated_paste_is_damaged(cut):
    assert _code(GOLDEN[:-cut]) in ("damaged", "not_a_string")
    if cut in (1, 3):  # 234 / 232 chars: still decodable, the CRC catches it
        assert _code(GOLDEN[:-cut]) == "damaged"


@pytest.mark.parametrize("pos", [10, 100, 200, 234])
def test_one_lost_or_changed_character_is_damaged(pos):
    assert _code(GOLDEN[:pos] + GOLDEN[pos + 1:]) == "damaged"
    swapped = "A" if GOLDEN[pos] != "A" else "B"
    assert _code(GOLDEN[:pos] + swapped + GOLDEN[pos + 1:]) == "damaged"


def test_a_wrong_crc_is_damaged():
    assert _code(_encode(crc=zlib.crc32(GOLDEN_JSON.encode()) ^ 1)) == "damaged"


@pytest.mark.parametrize("version", [2, 3, 9])
def test_a_newer_format_is_a_version_error(version):
    assert _code(_encode(version=version, v=version)) == "version"


@pytest.mark.parametrize("version", [0, 10, 0x7B, 0xFF])
def test_an_unknown_leading_byte_is_not_a_registration_string(version):
    assert _code(_encode(version=version)) == "not_a_string"


def test_v_must_agree_with_the_leading_byte():
    assert _code(_encode(v=2)) == "invalid"
    assert _code(_encode(version=2)) == "version"  # the leading byte decides first


@pytest.mark.parametrize("text", [
    "", "   \n\t ", "hello world", "AAAA", "AQ", "Hi! can you add my phone?",
    '{"v":1,"e":"x"}', "https://ha.example.org/", "PORTICO-REG1:eyJ2IjoxfQ.60c1975b",
    "home-intercom/1|https://ha|token", "שלום",
])
def test_random_text_is_not_a_registration_string(text):
    assert _code(text) == "not_a_string"


def test_too_long():
    assert _code("A" * 2049) == "too_long"
    assert _code(_encode(a="x" * 1600)) == "too_long"
    # Whitespace does not count towards the limit.
    assert reg_string.parse(" \n".join(GOLDEN) + " " * 5000).kind == "fid"


@pytest.mark.parametrize("body", [
    {**GOLDEN_OBJ, "x": 1},                       # extra field
    {**GOLDEN_OBJ, "h": "https://ha.example"},    # extra field
    _without("e"), _without("i"), _without("k"), _without("r"), _without("p"), _without("f"), _without("a"),
    _without("v"),
    {**GOLDEN_OBJ, "v": "1"}, {**GOLDEN_OBJ, "v": True}, {**GOLDEN_OBJ, "v": 1.0},
    {**GOLDEN_OBJ, "i": 5}, {**GOLDEN_OBJ, "r": None}, {**GOLDEN_OBJ, "a": 1}, {**GOLDEN_OBJ, "e": ["x"]},
    {**GOLDEN_OBJ, "k": "topic"}, {**GOLDEN_OBJ, "k": "FID"},
    {**GOLDEN_OBJ, "e": ""}, {**GOLDEN_OBJ, "i": ""}, {**GOLDEN_OBJ, "r": ""}, {**GOLDEN_OBJ, "p": ""},
    {**GOLDEN_OBJ, "f": ""},
    {**GOLDEN_OBJ, "e": "x" * 129}, {**GOLDEN_OBJ, "i": "x" * 129}, {**GOLDEN_OBJ, "f": "x" * 129},
    {**GOLDEN_OBJ, "a": "x" * 65},
])
def test_missing_extra_or_wrong_typed_fields_are_invalid(body):
    assert _code(_encode(body)) == "invalid"


@pytest.mark.parametrize("raw", [
    b"[1,2]", b'"text"', b"{not json", b"\xff\xfe{}",
    GOLDEN_JSON.replace('"a":"0.1.0"', '"a":"0.1.0","a":"0.2"').encode(),  # a duplicate key
])
def test_a_body_that_is_not_one_json_object_is_invalid(raw):
    assert _code(_encode(raw=raw)) == "invalid"


# --------------------------------------------------------------------------
# The options-flow step: Portico push ring notification > Configure > Add a phone's registration
# --------------------------------------------------------------------------


async def _setup(hass, hass_storage, google, *, phones=(("Alice", ALICE_EID),), **options):
    """A door (the driver part's) and the push entry holding the enrolled phones
    (each with an enroll_id). Returns {name: (user, token)} and the push entry."""
    users, enrolled = {}, []
    for name, eid in phones:
        user, token = await _phone_user(hass, f"Portico — {name}")
        users[name] = (user, token)
        phone = {"ha_user_id": user.id, "name": name, "enrolled_at": "2026-10-07T00:00:00+00:00"}
        if eid is not None:
            phone["enroll_id"] = eid
        enrolled.append(phone)
    device = _observer_entry(hass)
    assert await hass.config_entries.async_setup(device.entry_id)
    await hass.async_block_till_done()
    push = _push_entry(hass, hass_storage, google, phones=enrolled, **options)
    assert await hass.config_entries.async_setup(push.entry_id)
    await hass.async_block_till_done()
    return users, push


async def _paste(hass, push, text):
    result = await _options(hass, push, "add_phone_registration")
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "add_phone_registration"
    return await hass.config_entries.options.async_configure(result["flow_id"], {F_REG: text})


async def test_the_menu_offers_the_step(hass, hass_storage, google, fake_transport):
    _users, push = await _setup(hass, hass_storage, google)
    result = await hass.config_entries.options.async_init(push.entry_id)
    assert "add_phone_registration" in result["menu_options"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "add_phone_registration"})
    assert list(result["data_schema"].schema) == [F_REG]


async def test_a_pasted_string_registers_the_enrolled_phone(hass, hass_storage, google, fake_transport):
    users, push = await _setup(hass, hass_storage, google)
    result = await _paste(hass, push, GOLDEN)
    assert result["type"] is FlowResultType.ABORT
    # The entry predates "New phones get the push ring notification": a new row is OFF.
    assert result["reason"] == "phone_registered_off"
    assert result["description_placeholders"] == {"phone": "Alice"}
    (row,) = _manager(hass).registry.rows()
    alice = users["Alice"][0]
    assert (row["ha_user_id"], row["name"], row["installation_id"], row["kind"], row["registration"],
            row["package"], row["fcm_project_id"], row["app_version"], row["push_enabled"]) == (
        alice.id, "Alice", "inst-0f1e2d3c4b5a", "fid", "cFAKEfid0000000000001A", "com.meda.portico",
        "portico-test", "0.1.0", False)
    assert push.options == {}  # untouched


async def test_new_phones_option_on_gives_a_switched_on_row(hass, hass_storage, google, fake_transport):
    _users, push = await _setup(hass, hass_storage, google, **{const.CONF_NEW_PHONES_ENABLED: True})
    result = await _paste(hass, push, GOLDEN)
    assert result["reason"] == "phone_registered_on"
    assert _manager(hass).registry.rows()[0]["push_enabled"] is True


async def test_a_test_allow_list_is_named_on_the_success_screen(hass, hass_storage, google, fake_transport):
    _users, push = await _setup(hass, hass_storage, google, **{const.CONF_NEW_PHONES_ENABLED: True,
                                                                const.CONF_TEST_PHONES: ["someone-else"]})
    result = await _paste(hass, push, GOLDEN)
    assert result["reason"] == "phone_registered_not_test_listed"
    row = _manager(hass).registry.rows()[0]
    assert row["push_enabled"] is True and row["reg_id"] not in _manager(hass).test_allow_list


async def test_a_whitespace_wrapped_paste_through_the_flow(hass, hass_storage, google, fake_transport):
    _users, push = await _setup(hass, hass_storage, google)
    wrapped = "\n  " + "\n".join(GOLDEN[i:i + 40] for i in range(0, len(GOLDEN), 40)) + " \t\n"
    result = await _paste(hass, push, wrapped)
    assert result["reason"] == "phone_registered_off"


async def test_the_enroll_id_picks_the_phone(hass, hass_storage, google, fake_transport):
    users, push = await _setup(hass, hass_storage, google, phones=(("Alice", ALICE_EID), ("Bob", BOB_EID)))
    result = await _paste(hass, push, _encode(e=BOB_EID))
    assert result["description_placeholders"] == {"phone": "Bob"}
    (row,) = _manager(hass).registry.rows()
    assert (row["ha_user_id"], row["name"]) == (users["Bob"][0].id, "Bob")


async def test_pasting_twice_and_a_later_ws_register_update_one_row(hass, hass_storage, hass_ws_client, google,
                                                                     fake_transport):
    """Replay is idempotent, and the phone's own WebSocket registration
    later (same installation id) UPDATES the pasted row, never adds one."""
    users, push = await _setup(hass, hass_storage, google)
    await _paste(hass, push, GOLDEN)
    (first,) = _manager(hass).registry.rows()
    await _manager(hass).registry.async_set_enabled(first["reg_id"], True)
    await _paste(hass, push, GOLDEN)
    assert [r["reg_id"] for r in _manager(hass).registry.rows()] == [first["reg_id"]]

    client = await hass_ws_client(hass, access_token=users["Alice"][1])
    await client.send_json(_register_msg(installation_id="inst-0f1e2d3c4b5a", kind="token",
                                         registration="ws-token-rotated-0123456789"))
    resp = await client.receive_json()
    assert resp["success"], resp
    assert resp["result"] == {"reg_id": first["reg_id"], "push_enabled": True}
    (row,) = _manager(hass).registry.rows()
    assert (row["kind"], row["registration"]) == ("token", "ws-token-rotated-0123456789")


async def test_a_ws_registered_phone_then_a_paste_updates_the_same_row(hass, hass_storage, hass_ws_client, google,
                                                                       fake_transport):
    users, push = await _setup(hass, hass_storage, google)
    client = await hass_ws_client(hass, access_token=users["Alice"][1])
    await client.send_json(_register_msg(installation_id="inst-0f1e2d3c4b5a"))
    reg_id = (await client.receive_json())["result"]["reg_id"]
    await _paste(hass, push, GOLDEN)
    (row,) = _manager(hass).registry.rows()
    assert row["reg_id"] == reg_id and row["registration"] == "cFAKEfid0000000000001A"


@pytest.mark.parametrize(("text", "error"), [
    (GOLDEN[:-1], "reg_damaged"),
    ("please add my phone", "reg_not_a_string"),
    (_encode(version=2, v=2), "reg_version"),
    (_encode(k="topic"), "reg_invalid"),
    ("A" * 3000, "reg_too_long"),
    (_encode(f="another-project"), "reg_project_mismatch"),
    (_encode(p="com.meda.ha_intercom"), "reg_package_mismatch"),
    (_encode(r="unknown"), "reg_invalid_registration"),
    (_encode(e="F" * 43), "reg_unknown_enrollment"),
])
async def test_every_refusal_is_a_translated_error_and_writes_nothing(hass, hass_storage, google, fake_transport,
                                                                      text, error):
    _users, push = await _setup(hass, hass_storage, google)
    result = await _paste(hass, push, text)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": error}
    assert _manager(hass).registry.rows() == []
    assert not result.get("description_placeholders")


async def test_a_phone_enrolled_before_enroll_ids_is_unknown(hass, hass_storage, google, fake_transport):
    _users, push = await _setup(hass, hass_storage, google, phones=(("Old", None),))
    for eid in (ALICE_EID, "", "None"):
        text = _encode(e=eid) if eid else GOLDEN
        result = await _paste(hass, push, text)
        assert result["errors"] == {"base": "reg_unknown_enrollment"}
    assert _manager(hass).registry.rows() == []


async def test_a_deleted_phone_user_is_unknown(hass, hass_owner_user, hass_storage, google, fake_transport):
    """The enrolled_phones record outlives the HA user (Settings > People):
    a string for it must not resurrect a row for a user that is gone."""
    users, push = await _setup(hass, hass_storage, google)
    await hass.auth.async_remove_user(users["Alice"][0])
    await hass.async_block_till_done()
    result = await _paste(hass, push, GOLDEN)
    assert result["errors"] == {"base": "reg_unknown_enrollment"}
    assert _manager(hass).registry.rows() == []


async def test_a_deactivated_phone_user_is_unknown(hass, hass_storage, google, fake_transport):
    users, push = await _setup(hass, hass_storage, google)
    # Store level: the first user the test made is the owner, whom HA's
    # own async_deactivate_user refuses.
    await hass.auth._store.async_deactivate_user(users["Alice"][0])
    result = await _paste(hass, push, GOLDEN)
    assert result["errors"] == {"base": "reg_unknown_enrollment"}


async def test_the_push_entry_not_running_is_an_error(hass, hass_storage, google, fake_transport, monkeypatch):
    _users, push = await _setup(hass, hass_storage, google)
    monkeypatch.setattr(push_flow, "get_manager", lambda _hass: None)
    result = await _paste(hass, push, GOLDEN)
    assert result["errors"] == {"base": "push_not_configured"}


async def test_nothing_pasted_lands_in_the_log_or_the_flow(hass, hass_storage, google, fake_transport, caplog):
    caplog.set_level(logging.DEBUG)
    _users, push = await _setup(hass, hass_storage, google)
    pasted = [GOLDEN[:-1], _encode(f="another-project"), _encode(e="F" * 43), _encode(k="topic"),
              _encode(version=2, v=2), "not a string at all", GOLDEN]
    results = [await _paste(hass, push, text) for text in pasted]
    # Every log line but the test harness's own mock-storage dump (it prints
    # whatever a Store writes, in tests only; HA's real Store does not).
    logged = "\n".join(r.getMessage() for r in caplog.records
                       if not r.name.startswith("pytest_homeassistant_custom_component"))
    seen = logged + json.dumps(results, default=str)
    for secret in (GOLDEN[:40], GOLDEN[100:140], ALICE_EID, "F" * 43, "inst-0f1e2d3c4b5a", "cFAKEfid",
                   "another-project", "not a string at all"):
        assert secret not in seen, secret
    # The failure class is logged.
    assert "damaged" in logged and "unknown_enrollment" in logged
