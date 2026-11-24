# ruff: noqa: F811 - test parameters named after the imported fixtures
"""P-4: each enrolled phone is one config entry of the UI part (ENROLL-01..05, PUSH-04, ENDPOINT-02), the
adoption of the rows that predate it, and the per-phone door scoping (R4-04).

HA-harness session, run on both HA versions. SPK-6 (a config entry created from an options flow through the
config flow's own `phone` source) and SPK-8 (token revocation and `async_remove_user`, with the owner) are
settled here by tests, not by reading HA's source.
"""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.portico import phone_entry
from custom_components.portico.config_flow import DOMAIN
from custom_components.portico.diagnostics import async_get_config_entry_diagnostics
from custom_components.portico.phone_entry import PhoneRecord, async_adopt_phones, phone_entries
from custom_components.portico.push import const
from custom_components.portico.push.registration import _phones, enrolled_phone_name
from custom_components.portico.signaling import _phone_ids

from .driver_part import _add_device
from .phone_helpers import enrolled
from .redeem_helpers import redeemed_token
from .test_enrollment_flow import (  # noqa: F401 - fixtures by name
    EXTERNAL,
    _enroll,
    _push_entry as _bare_push_entry,
    _qr_capture,
    _set_urls,
    qr_payloads,
    qr_texts,
)
from .test_push_registry import (  # noqa: F401 - fixtures by name
    REG,
    _custom_integrations,
    _manager,
    _phone_user,
    _register_msg,
    _setup_all,
    fake_transport,
    google,
)

PKG = Path(__file__).resolve().parent.parent


def _record(user_id="u-1", name="Pixel", enroll_id="enroll-1", **extra) -> PhoneRecord:
    return PhoneRecord(ha_user_id=user_id, name=name, enroll_id=enroll_id, enrolled_at="2026-10-08T10:00:00+00:00",
                       **extra)


def _phone_entry_by_name(hass, name):
    (entry,) = [e for e in phone_entries(hass) if e.data["name"] == name]
    return entry


async def _enroll_phone(hass, entry, name="Alice's Pixel"):
    _set_urls(hass, external=EXTERNAL)
    hass.config_entries.async_update_entry(entry, data={**entry.data, "tailscale_mode": "manual"})
    result = await _enroll(hass, entry, name=name)
    assert result["step_id"] == "show_qr", result
    await hass.async_block_till_done()
    return _phone_entry_by_name(hass, name)


# --------------------------------------------------------------------------
# SPK-6: the creation path
# --------------------------------------------------------------------------


async def test_spk6_enrollment_from_the_options_flow_creates_a_phone_entry_through_the_phone_source(
        hass, qr_payloads):
    """The options flow of the settings entry calls `flow.async_init(DOMAIN, context={"source": "phone"})`; the
    config flow's `async_step_phone` creates the entry. The settings entry (unique id `push`) does not make the
    flow abort with `single_instance_allowed`."""
    push = _bare_push_entry(hass, firebase=None)
    assert push.unique_id == "push"
    phone = await _enroll_phone(hass, push)

    assert phone.source == "phone" and phone.domain == DOMAIN
    assert phone.unique_id == f"phone:{qr_payloads[-1]['enroll_id']}"
    assert phone.title == "Alice's Pixel"
    assert phone.state is ConfigEntryState.LOADED
    assert len(hass.config_entries.async_entries(DOMAIN)) == 2        # the settings entry and the phone


def test_spk6_the_manifest_does_not_say_single_config_entry():
    manifest = json.loads((PKG / "manifest.json").read_text(encoding="utf-8"))
    assert "single_config_entry" not in manifest


async def test_a_second_flow_for_the_same_phone_aborts_and_creates_no_second_entry(hass, qr_payloads):
    push = _bare_push_entry(hass, firebase=None)
    phone = await _enroll_phone(hass, push)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "phone"}, data=phone.data | {"name": "Another name"})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"
    assert len(phone_entries(hass)) == 1


async def test_the_phone_source_refuses_data_with_no_ha_user(hass):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "phone"}, data={"name": "x"})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "invalid_phone"
    assert phone_entries(hass) == []


# --------------------------------------------------------------------------
# ENROLL-01..04
# --------------------------------------------------------------------------


async def test_enrollment_creates_one_entry_and_one_device(hass, qr_payloads):
    """ENROLL-01: one entry per phone, its data the identity, one HA device."""
    push = _bare_push_entry(hass, firebase=None)
    phone = await _enroll_phone(hass, push)

    payload = qr_payloads[-1]
    assert phone.data["name"] == "Alice's Pixel" and phone.data["enroll_id"] == payload["enroll_id"]
    assert phone.data["ha_user_id"] and phone.data["enrolled_at"]
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), phone.entry_id)
    assert len(devices) == 1 and devices[0].name == "Alice's Pixel"
    # a second enrollment is a second entry and a second device
    other = await _enroll_phone(hass, push, name="Dana's Pixel")
    assert other.entry_id != phone.entry_id
    assert len(dr.async_entries_for_config_entry(dr.async_get(hass), other.entry_id)) == 1
    assert [p["name"] for p in enrolled(hass)] == ["Alice's Pixel", "Dana's Pixel"]


async def test_entities_are_named_after_the_phone(hass, qr_payloads):
    """ENROLL-02: push registration state and last connection; the entity ids come from the phone's name, never
    from the entry id."""
    push = _bare_push_entry(hass, firebase=None)
    phone = await _enroll_phone(hass, push)

    registry = er.async_get(hass)
    entities = er.async_entries_for_config_entry(registry, phone.entry_id)
    ids = sorted(e.entity_id for e in entities)
    assert ids == ["sensor.alice_s_pixel_last_connection", "sensor.alice_s_pixel_push_registration"]
    assert all(phone.entry_id not in i and phone.entry_id.lower() not in i for i in ids)
    assert hass.states.get("sensor.alice_s_pixel_push_registration").state == "away_ring_off"   # no push ring notification loaded
    assert hass.states.get("sensor.alice_s_pixel_last_connection").state == "unknown"


async def test_the_sensors_follow_the_away_ring_registration(hass, hass_storage, hass_ws_client, google,
                                                              fake_transport):
    user, token = await _phone_user(hass, "Portico — Alice")
    _d, push = await _setup_all(hass, hass_storage, google, enrolled=[(user.id, "Alice")])
    await hass.async_block_till_done()
    assert hass.states.get("sensor.alice_push_registration").state == "not_registered"
    assert hass.states.get("sensor.alice_last_connection").state == "unknown"

    client = await hass_ws_client(hass, access_token=token)
    await client.send_json(_register_msg())
    assert (await client.receive_json())["success"]
    await hass.async_block_till_done()

    state = hass.states.get("sensor.alice_push_registration")
    assert state.state == "never_sent" and state.attributes["registrations"] == 1
    assert REG not in json.dumps(dict(state.attributes))
    seen = dt_util.parse_datetime(hass.states.get("sensor.alice_last_connection").state)
    assert seen is not None and abs(dt_util.utcnow() - seen) < timedelta(minutes=1)

    assert await hass.config_entries.async_unload(push.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.alice_push_registration").state == "away_ring_off"


def test_no_enrolled_phones_key_anywhere():
    """ENROLL-03: enrollment writes no `enrolled_phones` row, and no code outside `phone_entry.py` (the legacy
    reader and the adoption) touches the key: only prose mentions it (backticked, in docstrings)."""
    for path in PKG.rglob("*.py"):
        if "tests" in path.parts or path.name == "phone_entry.py":
            continue
        text = path.read_text(encoding="utf-8")
        assert '"enrolled_phones"' not in text and "'enrolled_phones'" not in text, path
    assert "enrolled_phones" not in (PKG / "enrollment_flow.py").read_text(encoding="utf-8")


async def test_schema_has_token_kind_and_expiry(hass, qr_payloads):
    """ENROLL-04 / DEV-544: a new phone is `pending` with a `refresh` token kind until its one-time code is
    redeemed; no token (and no code) value is ever stored in the entry."""
    push = _bare_push_entry(hass, firebase=None)
    phone = await _enroll_phone(hass, push)

    assert phone.data["token_kind"] == "refresh" and phone.data["refresh_state"] == "pending"
    assert phone.data["state"] == "pending" and phone.data["token_expires_at"] is None
    assert dt_util.parse_datetime(phone.data["pending_since"]) is not None
    assert phone.data["tailscale_mode"] == "manual" and phone.data["tailscale_node"] is None
    blob = json.dumps(dict(phone.data)) + json.dumps(dict(phone.options))
    assert qr_payloads[-1]["code"] not in blob
    # a record from before DEV-544 has no state: it reads as enrolled
    assert PhoneRecord.from_data({"ha_user_id": "u"}).state == "enrolled"
    assert PhoneRecord.from_data({"ha_user_id": "u", "state": "weird"}).state == "enrolled"
    # a record with an unknown kind is read as today's long-lived token, never refused
    assert PhoneRecord.from_data({"ha_user_id": "u", "token_kind": "weird"}).token_kind == "llat"
    assert PhoneRecord.from_data({"ha_user_id": "u", "token_kind": "refresh"}).token_kind == "refresh"


async def test_second_enrollment_asks_for_no_secret(hass, qr_payloads):
    """ENROLL-06: the Tailscale answer is the settings entry's, asked once; the second phone goes straight to the
    phone form."""
    push = _bare_push_entry(hass, firebase=None)
    await _enroll_phone(hass, push)
    result = await hass.config_entries.options.async_init(push.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "enroll_phone"})
    assert result["step_id"] == "phone_details"


async def test_the_phone_endpoint_id_is_the_enroll_id(hass, qr_payloads):
    """ENDPOINT-02: stable across restarts and re-adds."""
    from custom_components.portico.endpoints import get_endpoint_registry

    push = _bare_push_entry(hass, firebase=None)
    phone = await _enroll_phone(hass, push)
    (endpoint,) = get_endpoint_registry(hass).endpoints(kind="phone")
    assert endpoint.id == qr_payloads[-1]["enroll_id"] and endpoint.ha_user_id == phone.data["ha_user_id"]


# --------------------------------------------------------------------------
# ENROLL-05, PUSH-04, SPK-8: revocation
# --------------------------------------------------------------------------


async def test_deleting_a_phone_entry_revokes_tokens_and_removes_registration(
        hass, hass_storage, hass_ws_client, google, fake_transport, qr_payloads):
    """Real `hass.auth`, real registry, real content store, no mock: the phone's token stops validating, its HA user
    goes, its push registration goes, its content override goes, and a notice names what Portico leaves."""
    from homeassistant.components import persistent_notification

    from custom_components.portico.content import get_content_store

    _d, push = await _setup_all(hass, hass_storage, google)
    phone = await _enroll_phone(hass, push, name="Revoke me")
    user_id = phone.data["ha_user_id"]
    user = await hass.auth.async_get_user(user_id)
    assert user is not None and user.name == "Portico — Revoke me"
    assert user.refresh_tokens == {}                      # DEV-544: a phone holds no token until it redeems its code
    token, access = await redeemed_token(hass, user_id, qr_payloads[-1])
    assert hass.auth.async_validate_access_token(access) is not None

    client = await hass_ws_client(hass, access_token=access)
    await client.send_json(_register_msg())
    assert (await client.receive_json())["success"]
    assert [r["enroll_id"] for r in _manager(hass).registry.rows()] == [phone.data["enroll_id"]]   # PUSH-04 stamp
    await client.close()
    store = get_content_store(hass)
    await store.async_set(user_id, {"cameras": [], "actions": []})
    assert phone.data["enroll_id"] in store._overrides

    assert await hass.config_entries.async_remove(phone.entry_id) == {"require_restart": False}
    await hass.async_block_till_done()

    assert hass.auth.async_validate_access_token(access) is None
    assert await hass.auth.async_get_user(user_id) is None
    assert _manager(hass).registry.rows() == []
    assert phone.data["enroll_id"] not in store._overrides and user_id not in store._overrides
    assert enrolled_phone_name(hass, user_id) is None
    assert phone_entries(hass) == []
    notices = persistent_notification._async_get_or_create_notifications(hass)
    (notice,) = [n for k, n in notices.items() if k.startswith("portico_phone_removed_")]
    assert "Revoke me" in notice["message"] and "Tailscale" in notice["message"]
    assert "manual" in notice["message"]                       # the phone was enrolled in manual mode here


async def test_deleting_the_entry_removes_the_registration_when_the_away_ring_is_not_loaded(
        hass, hass_storage, hass_ws_client, google, fake_transport, qr_payloads):
    _d, push = await _setup_all(hass, hass_storage, google)
    phone = await _enroll_phone(hass, push, name="Offline")
    _token, token = await redeemed_token(hass, phone.data["ha_user_id"], qr_payloads[-1])
    client = await hass_ws_client(hass, access_token=token)
    await client.send_json(_register_msg())
    assert (await client.receive_json())["success"]
    await client.close()
    await hass.async_block_till_done()
    registry = _manager(hass).registry
    await registry._store.async_save(registry._data())           # on disk, as the push ring notification would leave it
    assert await hass.config_entries.async_unload(push.entry_id)
    await hass.async_block_till_done()

    assert await hass.config_entries.async_remove(phone.entry_id)
    await hass.async_block_till_done()
    assert await hass.auth.async_get_user(phone.data["ha_user_id"]) is None
    from custom_components.portico.push.registry import PushRegistry

    reloaded = PushRegistry(hass)
    await reloaded.async_load()
    assert reloaded.rows() == []


async def test_a_registration_older_than_the_enroll_id_stamp_is_removed_by_its_user(hass):
    """A row registered before the stamp existed has no `enroll_id`: removal falls back to the HA user, and never
    takes another phone's row."""
    from custom_components.portico.push.registry import PushRegistry

    registry = PushRegistry(hass)
    await registry.async_load()
    common = dict(installation_id="i", kind="fid", registration="R", package=const.PORTICO_PACKAGE,
                  fcm_project_id="p", app_version="1")
    await registry.async_register(ha_user_id="u-old", name="old", **common)                    # no stamp
    await registry.async_register(ha_user_id="u-new", name="new", enroll_id="e-new", **common)
    await registry.async_register(ha_user_id="u-other", name="other", enroll_id="e-other", **common)
    assert await registry.async_remove_phone("e-new", "u-new") == 1
    assert await registry.async_remove_phone(None, "u-old") == 1
    assert [r["name"] for r in registry.rows()] == ["other"]
    assert registry.rows_for_phone("e-other", "u-other")[0]["name"] == "other"


async def test_spk8_the_owner_survives_and_only_enrollment_tokens_go(hass, hass_owner_user):
    """2026.10 refuses to remove the owner. A phone entry that names a user Portico did not make loses only the
    tokens enrollment made (`portico-...`); the user and its other tokens stay. Same on both HA versions."""
    llat = dict(token_type="long_lived_access_token", access_token_expiration=timedelta(days=1))
    other = await hass.auth.async_create_refresh_token(hass_owner_user, client_name="a browser", **llat)
    mine = await hass.auth.async_create_refresh_token(hass_owner_user, client_name="portico-entry-user", **llat)
    entry = MockConfigEntry(domain=DOMAIN, title="Owner phone", unique_id="phone:e-owner",
                            data=_record(hass_owner_user.id, "Owner phone", "e-owner").to_data())
    entry.add_to_hass(hass)
    await phone_entry.async_revoke_phone(hass, entry)

    kept = await hass.auth.async_get_user(hass_owner_user.id)
    assert kept is not None
    assert other.id in kept.refresh_tokens and mine.id not in kept.refresh_tokens


async def test_a_portico_user_who_is_an_admin_is_not_removed(hass, hass_admin_user):
    await hass.auth.async_update_user(hass_admin_user, name="Portico — admin by mistake")
    entry = MockConfigEntry(domain=DOMAIN, title="x", unique_id="phone:e-adm",
                            data=_record(hass_admin_user.id, "x", "e-adm").to_data())
    entry.add_to_hass(hass)
    await phone_entry.async_revoke_phone(hass, entry)
    assert await hass.auth.async_get_user(hass_admin_user.id) is not None


async def test_a_phone_whose_user_is_already_gone_is_revoked_without_error(hass, caplog):
    entry = MockConfigEntry(domain=DOMAIN, title="x", unique_id="phone:e-gone",
                            data=_record("no-such-user", "x", "e-gone").to_data())
    entry.add_to_hass(hass)
    await phone_entry.async_revoke_phone(hass, entry)
    assert "failed" not in caplog.text


async def test_deleting_the_ha_user_removes_the_phone_entry(hass, hass_owner_user, qr_payloads):
    """M5.8 deletes the old phones' users: their entries go with them. (`hass_owner_user`: on 2026.10 the first
    user an empty instance creates is the owner, whom HA refuses to remove; a real instance has its owner.)"""
    push = _bare_push_entry(hass, firebase=None)
    phone = await _enroll_phone(hass, push)
    user = await hass.auth.async_get_user(phone.data["ha_user_id"])
    await hass.auth.async_remove_user(user)
    await hass.async_block_till_done()
    assert phone_entries(hass) == []
    assert hass.states.get("sensor.alice_s_pixel_push_registration") is None


# --------------------------------------------------------------------------
# Adoption of the rows that predate phone entries
# --------------------------------------------------------------------------


async def test_rows_on_the_settings_entry_become_entries_once_and_a_restart_is_harmless(
        hass, hass_storage, google, fake_transport):
    alice, _t = await _phone_user(hass, "Portico — Alice")
    bob, _t = await _phone_user(hass, "Portico — Bob")
    rows = [{"ha_user_id": alice.id, "name": "Alice", "enrolled_at": "2026-10-01T00:00:00+00:00", "enroll_id": "ea"},
            {"ha_user_id": bob.id, "name": "Bob", "enrolled_at": "2026-10-02T00:00:00+00:00"}]   # no enroll id
    # before anything runs the rows are already phones (a surprise restart of the old code path)
    from .test_push_registry import _push_entry

    push = _push_entry(hass, hass_storage, google, phones=rows)
    assert {p["name"] for p in enrolled(hass)} == {"Alice", "Bob"}
    assert enrolled_phone_name(hass, alice.id) == "Alice"

    assert await hass.config_entries.async_setup(push.entry_id)
    await hass.async_block_till_done()

    assert "enrolled_phones" not in push.data
    entries = phone_entries(hass)
    assert sorted(e.unique_id for e in entries) == sorted(["phone:ea", f"phone:{bob.id}"])
    alice_entry = _phone_entry_by_name(hass, "Alice")
    assert alice_entry.data["ha_user_id"] == alice.id and alice_entry.data["enrolled_at"].startswith("2026-10-01")
    assert alice_entry.data["token_kind"] == "llat"
    assert sorted(p["name"] for p in enrolled(hass)) == ["Alice", "Bob"]      # each phone once
    assert enrolled_phone_name(hass, bob.id) == "Bob"

    ids_before = sorted(e.entry_id for e in phone_entries(hass))
    # the restart: unload and set up again, then the adopter by hand
    assert await hass.config_entries.async_unload(push.entry_id)
    assert await hass.config_entries.async_setup(push.entry_id)
    await hass.async_block_till_done()
    assert await async_adopt_phones(hass, push) == []
    assert sorted(e.entry_id for e in phone_entries(hass)) == ids_before
    assert len(enrolled(hass)) == 2


async def test_an_interrupted_adoption_neither_duplicates_nor_loses_a_phone(hass):
    """The entry exists but the row is still there (a crash between the two writes): the next run drops the row and
    creates nothing."""
    push = MockConfigEntry(domain=DOMAIN, title="Portico push ring notification", unique_id="push", data={
        const.ENTRY_TYPE: const.ENTRY_TYPE_PUSH,
        "enrolled_phones": [{"ha_user_id": "u1", "name": "One", "enroll_id": "e1"},
                            {"ha_user_id": "u2", "name": "Two", "enroll_id": "e2"}]})
    push.add_to_hass(hass)
    MockConfigEntry(domain=DOMAIN, title="One", unique_id="phone:e1",
                    data=_record("u1", "One", "e1").to_data()).add_to_hass(hass)
    assert len(enrolled(hass)) == 2
    assert await async_adopt_phones(hass, push) == ["Two"]
    assert "enrolled_phones" not in push.data
    assert sorted(e.unique_id for e in phone_entries(hass)) == ["phone:e1", "phone:e2"]
    assert len(enrolled(hass)) == 2


async def test_a_row_that_cannot_become_an_entry_stays_and_is_still_a_phone(hass):
    push = MockConfigEntry(domain=DOMAIN, title="Portico push ring notification", unique_id="push", data={
        const.ENTRY_TYPE: const.ENTRY_TYPE_PUSH,
        "enrolled_phones": [{"name": "no user"}, {"ha_user_id": "u2", "name": "Two", "enroll_id": "e2"}]})
    push.add_to_hass(hass)
    assert await async_adopt_phones(hass, push) == ["Two"]
    assert push.data["enrolled_phones"] == [{"name": "no user"}]
    assert [p["name"] for p in enrolled(hass)] == ["Two", "no user"]


async def test_legacy_rows_on_an_old_door_entry_are_read_but_not_adopted(hass):
    MockConfigEntry(domain=DOMAIN, title="old door", unique_id="old", data={
        "driver": "dahua_sip", "enrolled_phones": [{"ha_user_id": "u9", "name": "Old", "enroll_id": "e9"}]}
    ).add_to_hass(hass)
    assert enrolled_phone_name(hass, "u9") == "Old"
    push = _bare_push_entry(hass, firebase=None)
    assert await async_adopt_phones(hass, push) == []
    assert phone_entries(hass) == [] and enrolled_phone_name(hass, "u9") == "Old"


async def test_a_phone_entry_wins_over_a_legacy_row_of_the_same_user(hass):
    MockConfigEntry(domain=DOMAIN, title="old door", unique_id="old", data={
        "driver": "dahua_sip", "enrolled_phones": [{"ha_user_id": "u9", "name": "Stale name", "enroll_id": "e9"}]}
    ).add_to_hass(hass)
    MockConfigEntry(domain=DOMAIN, title="Fresh", unique_id="phone:e9",
                    data=_record("u9", "Fresh name", "e9").to_data()).add_to_hass(hass)
    assert [p["name"] for p in enrolled(hass)] == ["Fresh name"]


# --------------------------------------------------------------------------
# R4-04: a phone is enrolled for some doors only. Contract 3 hands out no secrets (`portico/doors` has none), so the
# door list is enforced where it still matters: `media_open`, the one command that is door-scoped by enrollment.
# --------------------------------------------------------------------------


def _phone_for(hass, user_id, doors=None, name="p"):
    entry = MockConfigEntry(domain=DOMAIN, title=name, unique_id=f"phone:{user_id}",
                            data=_record(user_id, name, f"e-{user_id}").to_data(),
                            options={} if doors is None else {"doors": doors})
    entry.add_to_hass(hass)
    return entry


async def _media_open_codes(hass, hass_ws_client, doors):
    """`portico/media_open` on each door with away media on and no call: the code says how far the caller got
    (`not_enrolled` is refused at the phone's door list; `unknown_call` is past it)."""
    from .test_signaling import _setup_signaling

    await _setup_signaling(hass)
    client = await hass_ws_client(hass)
    out = {}
    for n, door in enumerate(doors, start=1):
        await client.send_json({"id": n, "type": "portico/media_open", "door_id": door, "call_id": "c"})
        out[door] = (await client.receive_json())["error"]["code"]
    return out


def _doors_with_away_media(hass, *door_ids):
    for door_id in door_ids:
        entry, _runtime = _add_device(hass, device_id=door_id)
        hass.config_entries.async_update_entry(entry, options={"away_media": True})


async def test_a_phone_may_open_call_audio_only_on_the_doors_it_is_enrolled_for(hass, hass_ws_client, hass_admin_user):
    _doors_with_away_media(hass, "door-a", "door-b")
    _phone_for(hass, hass_admin_user.id, doors=["door-a"])
    codes = await _media_open_codes(hass, hass_ws_client, ["door-a", "door-b"])
    assert codes == {"door-a": "unknown_call", "door-b": "not_enrolled"}


async def test_a_phone_with_no_door_list_may_open_call_audio_on_every_door(hass, hass_ws_client, hass_admin_user):
    _doors_with_away_media(hass, "door-a", "door-b")
    _phone_for(hass, hass_admin_user.id, doors=None)
    codes = await _media_open_codes(hass, hass_ws_client, ["door-a", "door-b"])
    assert codes == {"door-a": "unknown_call", "door-b": "unknown_call"}


async def test_a_non_enrolled_caller_may_open_call_audio_on_no_door(hass, hass_ws_client, hass_admin_user):
    _doors_with_away_media(hass, "door-a", "door-b")
    _phone_for(hass, "somebody-else", doors=None)                    # a phone exists, it is not this caller
    codes = await _media_open_codes(hass, hass_ws_client, ["door-a", "door-b"])
    assert codes == {"door-a": "not_enrolled", "door-b": "not_enrolled"}


async def test_the_phone_ids_are_scoped_by_door(hass):
    _phone_for(hass, "u1", doors=["door-a"])
    assert _phone_ids(hass, "u1") == ["e-u1"]
    assert _phone_ids(hass, "u1", "door-a") == ["e-u1"]
    assert _phone_ids(hass, "u1", "door-b") == []
    assert _phone_ids(hass, "nobody", "door-a") == []
    assert _phone_ids(hass, None, "door-a") == []


async def test_the_phone_options_flow_sets_and_clears_the_door_list(hass, qr_payloads, fake_transport):
    _add_device(hass, device_id="door-a", title="Front door")
    _add_device(hass, device_id="door-b", title="Gate")
    push = _bare_push_entry(hass, firebase=None)
    phone = await _enroll_phone(hass, push)
    assert [p["doors"] for p in enrolled(hass)] == [None]

    result = await hass.config_entries.options.async_init(phone.entry_id)
    assert (result["type"], result["step_id"]) == (FlowResultType.FORM, "phone_doors")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"all_doors": False, "doors": ["door-b"]})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert phone.options["doors"] == ["door-b"] and [p["doors"] for p in enrolled(hass)] == [["door-b"]]

    result = await hass.config_entries.options.async_init(phone.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"all_doors": True})
    assert phone.options["doors"] is None and [p["doors"] for p in enrolled(hass)] == [None]


# --------------------------------------------------------------------------
# Diagnostics
# --------------------------------------------------------------------------


async def test_a_phone_entrys_diagnostics_carry_no_secret(hass, hass_storage, hass_ws_client, google, fake_transport):
    user, token = await _phone_user(hass, "Portico — Alice")
    _d, push = await _setup_all(hass, hass_storage, google, enrolled=[(user.id, "Alice")])
    await hass.async_block_till_done()
    client = await hass_ws_client(hass, access_token=token)
    await client.send_json(_register_msg())
    assert (await client.receive_json())["success"]
    phone = _phone_entry_by_name(hass, "Alice")

    # HA-L10: `enroll_id` is the key a pasted registration string is matched on, so a bundle must not carry it.
    hass.config_entries.async_update_entry(phone, data={**phone.data, "enroll_id": "enroll-secret-1"})
    diag = await async_get_config_entry_diagnostics(hass, phone)
    text = json.dumps(diag, default=str)
    assert REG not in text and token not in text and "enroll-secret-1" not in text
    assert diag["entry"]["data"]["enroll_id"] == "**REDACTED**"
    # DEV-700 A: a phone's name is personal, pseudonymised in the entry and in the registrations
    assert diag["entry"]["data"]["name"].startswith("redacted-") and "Alice" not in text
    assert len(diag["push_registrations"]) == 1
    assert diag["push_registrations"][0]["state"] == "never_sent"
    assert "registration" not in diag["push_registrations"][0]                # the public projection has no value
