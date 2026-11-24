# ruff: noqa: F811 - test parameters named after the imported fixtures
"""S-1 of the Portico split ("enrollment has a home"): the push entry holds
the enrollment state a device entry holds today, copied once and never taken
away, and enrollment, content and phone matching work from either host.

HA-harness session. Hermetic: nothing here reaches Tailscale or Google.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.portico import enrollment_adopt, tailscale_approve
from custom_components.portico.config_flow import DOMAIN
from custom_components.portico.enrollment_adopt import async_adopt_enrollment, oauth_copy_is_readable
from custom_components.portico.push import const
from custom_components.portico.push.registration import (
    _phones,
    async_find_phone_by_enroll_id,
    enrolled_phone_name,
)

from .test_enrollment_flow import _push_entry, _set_urls, qr_payloads, _qr_capture  # noqa: F401 - fixtures
from .test_push_registry import _custom_integrations, google, _push_entry as _live_push_entry  # noqa: F401
from .driver_part import fake_transport, get_provider  # noqa: F401 - fixture by name

CLIENT_ID = "kAbC123CNTRL"
SECRET = f"tskey-app-{CLIENT_ID}-SENTINELSECRETxyz0123456789"
REDIRECT = "https://ha.example.org" + tailscale_approve.CALLBACK_PATH
APPROVE = {"tailscale_mode": "approve", "tailscale_client_id": CLIENT_ID, "tailscale_redirect_uri": REDIRECT}
PHONE = {"ha_user_id": "user-door", "name": "Door phone", "enrolled_at": "2026-10-01T00:00:00+00:00",
         "enroll_id": "enroll-door"}


def _door(hass, *, driver="gate_sip_observer", host="gate.example", **data) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, title=f"Portico ({driver})", unique_id=f"{driver}:{host}",
                            data={"driver": driver, "host": host, "sip_user": "9901#0", **data})
    entry.add_to_hass(hass)
    return entry


def _put_secret(hass_storage, entry, secret=SECRET) -> None:
    key = f"{tailscale_approve.STORE_KEY_PREFIX}{entry.entry_id}"
    hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": {"client_secret": secret}}


def _secret_of(hass, entry):
    return tailscale_approve.OAuthAppStore(hass, entry.entry_id).async_load_secret()


# --- (c) the adoption ----------------------------------------------------------------------------


async def test_adopt_copies_once_and_is_idempotent(hass, hass_storage):
    door = _door(hass, ha_dial="10.6.0.5:443", enrolled_phones=[PHONE], **APPROVE)
    _put_secret(hass_storage, door)
    push = _push_entry(hass)
    door_before = dict(door.data)

    assert set(await async_adopt_enrollment(hass, push)) == {"oauth_app_secret", "tailscale_answer", "ha_dial"}
    for key, value in {**APPROVE, "ha_dial": "10.6.0.5:443"}.items():
        assert push.data[key] == value
    assert await _secret_of(hass, push) == SECRET
    # a copy: the push entry did not take the phones, the door lost nothing
    assert "enrolled_phones" not in push.data
    assert dict(door.data) == door_before and await _secret_of(hass, door) == SECRET

    after_first = dict(push.data)
    assert await async_adopt_enrollment(hass, push) == []
    assert dict(push.data) == after_first and await _secret_of(hass, push) == SECRET


async def test_adopt_never_overwrites_what_the_push_entry_has(hass, hass_storage):
    door = _door(hass, ha_dial="10.6.0.5:443", **APPROVE)
    _put_secret(hass_storage, door)
    push = _push_entry(hass, tailscale_mode="manual", ha_dial="10.9.9.9:443")
    assert await async_adopt_enrollment(hass, push) == []
    assert push.data["tailscale_mode"] == "manual" and push.data["ha_dial"] == "10.9.9.9:443"
    assert "tailscale_client_id" not in push.data
    assert await _secret_of(hass, push) is None       # a `manual` answer needs no secret there


async def test_adopt_keeps_a_secret_the_push_entry_already_has(hass, hass_storage):
    door = _door(hass, **APPROVE)
    _put_secret(hass_storage, door)
    push = _push_entry(hass, tailscale_mode="approve", tailscale_client_id="mine", tailscale_redirect_uri=REDIRECT)
    _put_secret(hass_storage, push, "tskey-app-mine-ITSOWN")
    assert await async_adopt_enrollment(hass, push) == []
    assert await _secret_of(hass, push) == "tskey-app-mine-ITSOWN" and push.data["tailscale_client_id"] == "mine"


async def test_adopt_copies_only_a_complete_answer_and_nothing_without_a_device_entry(hass, hass_storage, caplog):
    push = _push_entry(hass)
    assert await async_adopt_enrollment(hass, push) == []        # no device entry at all
    _door(hass, host="a.example", tailscale_mode="auto", tailscale_api_key="tskey-api-x")   # no tailnet: unfinished
    _door(hass, host="b.example", **APPROVE)                      # `approve` without its secret in the Store
    assert await async_adopt_enrollment(hass, push) == []
    assert "tailscale_mode" not in push.data
    # a later device entry that IS complete is the source, the unfinished ones are skipped
    good = _door(hass, host="c.example", tailscale_mode="manual")
    assert set(await async_adopt_enrollment(hass, push)) == {"tailscale_answer"}
    assert push.data["tailscale_mode"] == "manual" and good.data["tailscale_mode"] == "manual"


async def test_adopt_takes_a_pre_mode_entry_as_auto_like_enrollment_does(hass):
    _door(hass, tailscale_api_key="tskey-api-abc", tailscale_tailnet="example.ts.net")
    push = _push_entry(hass)
    assert "tailscale_answer" in await async_adopt_enrollment(hass, push)
    assert push.data["tailscale_api_key"] == "tskey-api-abc" and "tailscale_mode" not in push.data


async def test_adopt_repairs_an_approve_answer_whose_secret_is_missing(hass, hass_storage):
    door = _door(hass, **APPROVE)
    _put_secret(hass_storage, door)
    push = _push_entry(hass, **APPROVE)       # an earlier copy was cut short after the data write
    assert await async_adopt_enrollment(hass, push) == ["oauth_app_secret"]
    assert await _secret_of(hass, push) == SECRET


async def test_adopt_logs_what_it_did_and_no_secret(hass, hass_storage, caplog):
    caplog.set_level(logging.DEBUG)
    door = _door(hass, ha_dial="10.6.0.5:443", tailscale_api_key="tskey-api-SENTINEL", tailscale_tailnet="t.ts.net",
                 **APPROVE)
    _put_secret(hass_storage, door)
    await async_adopt_enrollment(hass, _push_entry(hass))
    text = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("custom_components.portico"))
    assert "adopted enrollment settings" in text
    for sentinel in (SECRET, "SENTINELSECRET", "tskey-api-SENTINEL", "10.6.0.5", CLIENT_ID):
        assert sentinel not in text


async def test_a_failed_store_write_leaves_the_push_entry_untouched(hass, hass_storage, monkeypatch):
    door = _door(hass, **APPROVE)
    _put_secret(hass_storage, door)
    push = _push_entry(hass)

    async def _boom(self, secret):
        raise OSError("disk full")

    monkeypatch.setattr(tailscale_approve.OAuthAppStore, "async_save_secret", _boom)
    with pytest.raises(OSError):
        await async_adopt_enrollment(hass, push)
    assert "tailscale_mode" not in push.data         # no `approve` answer without its secret
    monkeypatch.undo()
    assert "tailscale_answer" in await async_adopt_enrollment(hass, push)   # the next setup retries


# --- the OAuth copy: the owner does not hold the secret ------------------------------------------------


async def test_oauth_copy_is_readable_by_the_code_path_enrollment_uses(hass, hass_storage):
    """The owner has no other copy of the client secret. The copy must come back through
    `OAuthAppStore.async_load_secret`, the load the enrollment gate and the approve step call, equal to the
    original; `oauth_copy_is_readable` is the runbook's call for exactly that."""
    door = _door(hass, **APPROVE)
    _put_secret(hass_storage, door)
    push = _push_entry(hass)
    assert await oauth_copy_is_readable(hass) is False        # before the copy
    await async_adopt_enrollment(hass, push)

    assert await tailscale_approve.OAuthAppStore(hass, push.entry_id).async_load_secret() == SECRET
    assert await oauth_copy_is_readable(hass) is True
    # the Store file really is private and under the push entry's id
    assert hass_storage[f"{tailscale_approve.STORE_KEY_PREFIX}{push.entry_id}"]["data"] == {"client_secret": SECRET}

    # a copy that differs from the original is not a verified copy
    _put_secret(hass_storage, push, "tskey-app-other-DIFFERENT")
    assert await oauth_copy_is_readable(hass) is False
    # and with the original gone (S-2 deleted it) a readable copy stands alone
    _put_secret(hass_storage, push)
    await hass.config_entries.async_remove(door.entry_id)
    assert await oauth_copy_is_readable(hass) is True


async def test_enrollment_gate_accepts_the_adopted_answer_without_the_device_entry(hass, hass_storage, qr_payloads):
    """From the push entry, with the device entry already deleted, "Enroll a device" goes straight to the phone
    form (the gate's own check of answer + stored secret passes) and on to Tailscale's approval step."""
    door = _door(hass, **APPROVE)
    _put_secret(hass_storage, door)
    push = _push_entry(hass)
    await async_adopt_enrollment(hass, push)
    await hass.config_entries.async_remove(door.entry_id)
    _set_urls(hass, external="https://ha.example.org")

    result = await hass.config_entries.options.async_init(push.entry_id)
    assert (result["type"], result["step_id"]) == (FlowResultType.MENU, "init")
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "enroll_phone"})
    assert result["step_id"] == "phone_details", result
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"phone_name": "New phone"})
    assert (result["type"], result["step_id"]) == (FlowResultType.EXTERNAL_STEP, "tailscale_approve")
    assert CLIENT_ID in result["url"]


async def test_removing_the_push_entry_removes_its_adopted_secret(hass, hass_storage):
    from custom_components.portico import async_remove_entry

    door = _door(hass, **APPROVE)
    _put_secret(hass_storage, door)
    push = _push_entry(hass)
    await async_adopt_enrollment(hass, push)
    await async_remove_entry(hass, push)
    assert await _secret_of(hass, push) is None
    assert await _secret_of(hass, door) == SECRET


# --- the adoption at the push entry's setup --------------------------------------------------------------


async def test_the_push_entry_adopts_at_setup_and_survives_a_failed_adoption(
        hass, hass_storage, google, fake_transport, monkeypatch, caplog):
    # S-2: an old door entry (`portico` domain, `data.driver`) is refused at setup, and adoption reads it all the same
    door = _door(hass, tailscale_mode="manual", ha_dial="10.6.0.5:443", enrolled_phones=[PHONE])
    push = _live_push_entry(hass, hass_storage, google)
    assert await hass.config_entries.async_setup(push.entry_id)     # HA sets up the domain's every entry
    await hass.async_block_till_done()
    assert push.data["tailscale_mode"] == "manual" and push.data["ha_dial"] == "10.6.0.5:443"
    # the old entry is exactly as it was: refused (not loaded), nothing taken from it
    assert door.data["tailscale_mode"] == "manual" and door.data["enrolled_phones"] == [PHONE]
    assert door.state.value == "setup_error" and push.state.value == "loaded"

    # a second setup (a restart) changes nothing
    snapshot = dict(push.data)
    assert await hass.config_entries.async_unload(push.entry_id)
    assert await hass.config_entries.async_setup(push.entry_id)
    await hass.async_block_till_done()
    assert dict(push.data) == snapshot

    # an adoption that raises is logged and the push sender still starts
    async def _boom(hass, entry):
        raise RuntimeError("boom")

    monkeypatch.setattr(enrollment_adopt, "async_adopt_enrollment", _boom)
    assert await hass.config_entries.async_unload(push.entry_id)
    assert await hass.config_entries.async_setup(push.entry_id)
    await hass.async_block_till_done()
    assert push.state.value == "loaded"
    assert "adopting the enrollment settings" in caplog.text


# --- (b) phones are read from every entry -----------------------------------------------------------


async def test_phones_are_read_from_every_entry_without_duplicates(hass):
    on_push = {"ha_user_id": "user-push", "name": "Push phone", "enroll_id": "enroll-push"}
    door = _door(hass, tailscale_mode="manual", enrolled_phones=[PHONE])
    push = _push_entry(hass, tailscale_mode="manual", enrolled_phones=[on_push, dict(PHONE)])   # PHONE on both
    rows = list(_phones(hass))
    assert [p["ha_user_id"] for _e, p in rows] == ["user-door", "user-push"]
    assert rows[0][0] is door and rows[1][0] is push                 # the device entry's row wins a duplicate
    assert enrolled_phone_name(hass, "user-push") == "Push phone"
    assert enrolled_phone_name(hass, "user-door") == "Door phone"
    assert enrolled_phone_name(hass, "nobody") is None


async def test_find_phone_by_enroll_id_sees_both_hosts(hass):
    user_a = await hass.auth.async_create_user(name="A", group_ids=["system-users"])
    user_b = await hass.auth.async_create_user(name="B", group_ids=["system-users"])
    _door(hass, enrolled_phones=[{"ha_user_id": user_a.id, "name": "On door", "enroll_id": "enroll-A"}])
    _push_entry(hass, enrolled_phones=[{"ha_user_id": user_b.id, "name": "On push", "enroll_id": "enroll-B"}])
    assert await async_find_phone_by_enroll_id(hass, "enroll-A") == (user_a.id, "On door")
    assert await async_find_phone_by_enroll_id(hass, "enroll-B") == (user_b.id, "On push")
    assert await async_find_phone_by_enroll_id(hass, "enroll-C") is None


async def test_a_phone_enrolled_on_the_push_host_is_a_phone_to_the_provider_and_gets_no_secrets_on_the_wire(
        hass, hass_ws_client, hass_admin_user):
    """The enrollment check is this part's `_phone_ids`, which matches the asking user against the phones of EVERY
    entry; the provider's `secrets_for` hands a door's credentials to the phone id it is asked for, and
    `portico/doors` (contract 3) hands them to no one."""
    from .test_signaling import _add_device, _setup_signaling

    _add_device(hass, device_id="10.3.0.20")
    _push_entry(hass, enrolled_phones=[{"ha_user_id": hass_admin_user.id, "name": "p", "enroll_id": "enroll-P"}])
    await _setup_signaling(hass)
    from custom_components.portico.signaling import _phone_ids

    assert _phone_ids(hass, hass_admin_user.id, "10.3.0.20") == ["enroll-P"]
    expected = {"host": "10.3.0.20", "sip_user": "9999#0", "sip_password": "x",
                "rtsp_username": "u", "rtsp_password": "p"}
    assert get_provider(hass).secrets_for("10.3.0.20", "enroll-P") == expected
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "portico/doors"})
    doors = {d["door_id"]: d for d in (await client.receive_json())["result"]["doors"]}
    assert "secrets" not in doors["10.3.0.20"]


# --- (a) enrollment and content from the push entry --------------------------------------------------


async def test_the_push_menu_joins_push_enrollment_items(hass):
    push = _push_entry(hass, tailscale_mode="manual")
    result = await hass.config_entries.options.async_init(push.entry_id)
    assert (result["type"], result["step_id"]) == (FlowResultType.MENU, "init")
    assert result["menu_options"] == [
        "push_settings", "push_phones", "push_test_phones", "push_key", "add_phone_registration",
        "enroll_phone", "tailscale_change_mode"]
    # an old door entry's Configure only points at Portico Intercom (S-2)
    door = _door(hass, tailscale_mode="manual")
    result = await hass.config_entries.options.async_init(door.entry_id)
    assert (result["type"], result["reason"]) == (FlowResultType.ABORT, "door_entry_moved")


async def test_enrollment_from_the_push_entry_writes_its_row_there(hass, qr_payloads):
    door = _door(hass, tailscale_mode="manual", enrolled_phones=[PHONE])
    push = _push_entry(hass, tailscale_mode="manual")
    _set_urls(hass, external="https://ha.example.org")
    door_before = dict(door.data)

    result = await hass.config_entries.options.async_init(push.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "enroll_phone"})
    assert result["step_id"] == "phone_details"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"phone_name": "New phone", "ha_dial": "10.6.0.5:443"})
    assert result["step_id"] == "show_qr", result

    # P-4: the new phone is its own entry, not a row on the push entry
    assert "enrolled_phones" not in push.data
    (phone_entry,) = [e for e in hass.config_entries.async_entries("portico") if e.data.get("entry_type") == "phone"]
    assert phone_entry.data["name"] == "New phone" and phone_entry.data["enroll_id"] == qr_payloads[-1]["enroll_id"]
    assert push.data["ha_dial"] == "10.6.0.5:443"
    assert dict(door.data) == door_before                             # the old host was not touched
    # both phones are phones now
    assert {p["name"] for _e, p in _phones(hass)} == {"Door phone", "New phone"}
    assert enrolled_phone_name(hass, phone_entry.data["ha_user_id"]) == "New phone"
    assert enrolled_phone_name(hass, "user-door") == "Door phone"


async def test_enroll_phone_asks_the_tailscale_question_first_when_there_is_no_answer(hass):
    push = _push_entry(hass)
    result = await hass.config_entries.options.async_init(push.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "enroll_phone"})
    assert result["step_id"] == "tailscale_mode"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"tailscale_mode": "manual"})
    assert result["step_id"] == "phone_details" and push.data["tailscale_mode"] == "manual"




def test_the_strings_name_every_push_menu_item():
    root = Path(__file__).resolve().parent.parent
    for path in ("strings.json", "translations/en.json"):
        menu = json.loads((root / path).read_text(encoding="utf-8"))["options"]["step"]["init"]["menu_options"]
        assert {"enroll_phone", "tailscale_change_mode"} <= set(menu)
        assert not {"content_phone", "home_name"} & set(menu)
