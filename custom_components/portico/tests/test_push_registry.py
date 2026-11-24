# ruff: noqa: F811 - test parameters named after the imported fixture
"""The push registry, its three WebSocket commands, and the integration wiring
end to end (sender design §2.2, §3.2, §3.3, §8.1).

HA-harness session. The integration is loaded the way HA loads it
(`custom_components.portico`) with a real door entry (`entry_type: "door"`, the door side of the same integration),
the transport is the door side's fake (driver_part.py), and the only network peer is the
local fake Google.
"""
from __future__ import annotations

import os
import stat
from datetime import timedelta

import pytest
from freezegun import freeze_time
from homeassistant.auth.const import GROUP_ID_USER
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.portico.config_flow import DOMAIN
from custom_components.portico.provider import DATA_PROVIDERS
from custom_components.portico.push import const
from custom_components.portico.push.registry import MAX_ROWS_PER_USER, PushRegistry

from .fake_google import CLIENT_EMAIL, KEY_ID, FakeGoogle, service_account_info
from .driver_part import FakeGoTransport, _observer_entry, fake_transport, phones_host  # noqa: F401 - fixture by name

REG = "fRegistrationValue-0123456789abcdefghijklmnopqrstuvwxyz"


@pytest.fixture(autouse=True)
async def _custom_integrations(hass, enable_custom_integrations, tmp_path, monkeypatch):
    from homeassistant.components.frontend import DATA_EXTRA_MODULE_URL
    from homeassistant.setup import async_setup_component

    monkeypatch.setattr(hass.config, "path", lambda *parts: str(tmp_path.joinpath(*parts)))
    assert await async_setup_component(hass, "http", {})
    hass.data[DATA_EXTRA_MODULE_URL] = set()
    yield


@pytest.fixture
async def google(monkeypatch, socket_enabled):
    fake = await FakeGoogle().start()
    monkeypatch.setattr(const, "ENDPOINTS", const.Endpoints(token_uri=fake.token_uri, fcm_base_url=fake.base))
    yield fake
    await fake.close()


def _push_entry(hass, hass_storage, google, *, phones=None, **options) -> MockConfigEntry:
    hass_storage[const.CREDENTIALS_STORE_KEY] = {
        "version": 1, "minor_version": 1, "key": const.CREDENTIALS_STORE_KEY,
        "data": service_account_info(google.token_uri),
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Portico push ring notification",
        data={const.ENTRY_TYPE: const.ENTRY_TYPE_PUSH, "project_id": "portico-test",
              "client_email": CLIENT_EMAIL, "private_key_id": KEY_ID, "package": const.PORTICO_PACKAGE,
              **({"enrolled_phones": phones} if phones is not None else {})},
        options=options,
        unique_id=const.PUSH_UNIQUE_ID,
    )
    entry.add_to_hass(hass)
    return entry


async def _phone_user(hass, name="Portico — test phone"):
    user = await hass.auth.async_create_user(name=name, group_ids=[GROUP_ID_USER])
    refresh = await hass.auth.async_create_refresh_token(user, client_name="t", token_type="long_lived_access_token",
                                                         access_token_expiration=timedelta(days=1))
    return user, hass.auth.async_create_access_token(refresh)


async def _setup_all(hass, hass_storage, google, *, enrolled=(), push_first=False, **options):
    """Add and set up the two entries one at a time, in either order (HA sets
    up every entry already added when the domain loads)."""
    phones = [{"ha_user_id": uid, "name": name, "enrolled_at": "2026-10-06T00:00:00+00:00"}
              for uid, name in enrolled]
    makers = [lambda: _observer_entry(hass),
              lambda: _push_entry(hass, hass_storage, google, phones=phones, **options)]
    if push_first:
        makers.reverse()
    made = []
    for make in makers:
        entry = make()
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        made.append(entry)
    device, push = (made[1], made[0]) if push_first else (made[0], made[1])
    return device, push


def _manager(hass):
    return hass.data[DOMAIN][const.DATA_PUSH]


def _register_msg(n=1, **overrides):
    msg = {"id": n, "type": "portico/push/register", "installation_id": "inst-1", "kind": "fid",
           "registration": REG, "package": const.PORTICO_PACKAGE, "fcm_project_id": "portico-test",
           "app_version": "0.5.0-alpha"}
    msg.update(overrides)
    return msg


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------


async def _reg_at(reg, user, inst, when, **kw):
    with freeze_time(when):
        return await reg.async_register(ha_user_id=user, installation_id=inst, kind="fid", registration=f"R-{inst}",
                                        package=const.PORTICO_PACKAGE, fcm_project_id="p", app_version="1",
                                        name=inst, **kw)


async def test_rows_per_user_capped_evicting_oldest_last_seen(hass, caplog):
    reg = PushRegistry(hass)
    await reg.async_load()
    cap = MAX_ROWS_PER_USER
    assert cap == 3
    for n in range(cap):
        await _reg_at(reg, "u1", f"i{n}", f"2026-01-0{n + 1}T00:00:00+00:00")
    # i0 re-registers (becomes the newest); an EXISTING installation never evicts anything.
    again = await _reg_at(reg, "u1", "i0", "2026-02-01T00:00:00+00:00")
    assert len(reg.rows_for_user("u1")) == cap
    # A new installation evicts the oldest by last_seen_at: i1 (not i0, which was just seen).
    await _reg_at(reg, "u1", "i3", "2026-02-02T00:00:00+00:00")
    assert {r["installation_id"] for r in reg.rows_for_user("u1")} == {"i0", "i2", "i3"}
    assert reg.get(again["reg_id"]) is not None
    evictions = [r.getMessage() for r in caplog.records if r.name == "custom_components.portico.push.registry"]
    assert len(evictions) == 1 and "evicted the least recently seen" in evictions[0] and "R-i1" not in evictions[0]
    # Another new one evicts the next oldest (i2).
    await _reg_at(reg, "u1", "i4", "2026-02-03T00:00:00+00:00")
    assert {r["installation_id"] for r in reg.rows_for_user("u1")} == {"i0", "i3", "i4"}


async def test_upsert_at_cap_evicts_nothing(hass):
    reg = PushRegistry(hass)
    await reg.async_load()
    for n in range(MAX_ROWS_PER_USER):
        await _reg_at(reg, "u1", f"i{n}", f"2026-01-0{n + 1}T00:00:00+00:00")
    before = {r["reg_id"] for r in reg.rows()}
    for n in range(MAX_ROWS_PER_USER):
        await _reg_at(reg, "u1", f"i{n}", "2026-03-01T00:00:00+00:00")
    assert {r["reg_id"] for r in reg.rows()} == before


async def test_cap_is_per_user(hass):
    reg = PushRegistry(hass)
    await reg.async_load()
    for n in range(MAX_ROWS_PER_USER):
        await _reg_at(reg, "u2", f"j{n}", f"2026-01-0{n + 1}T00:00:00+00:00")
    for n in range(MAX_ROWS_PER_USER + 2):
        await _reg_at(reg, "u1", f"i{n}", f"2026-02-0{n + 1}T00:00:00+00:00")
    assert len(reg.rows_for_user("u1")) == MAX_ROWS_PER_USER
    assert {r["installation_id"] for r in reg.rows_for_user("u2")} == {"j0", "j1", "j2"}


async def test_upsert_per_installation_and_persistence(hass, hass_storage):
    reg = PushRegistry(hass)
    await reg.async_load()
    a = await reg.async_register(ha_user_id="u1", installation_id="i1", kind="fid", registration="R1",
                                 package=const.PORTICO_PACKAGE, fcm_project_id="p", app_version="1", name="A")
    assert a["push_enabled"] is False and a["state"] == "never_sent"
    await reg.async_set_enabled(a["reg_id"], True)
    again = await reg.async_register(ha_user_id="u1", installation_id="i1", kind="token", registration="R2",
                                     package=const.PORTICO_PACKAGE, fcm_project_id="p", app_version="2", name="A")
    assert again["reg_id"] == a["reg_id"]
    assert (again["kind"], again["registration"], again["push_enabled"]) == ("token", "R2", True)
    other = await reg.async_register(ha_user_id="u2", installation_id="i1", kind="fid", registration="R3",
                                     package=const.PORTICO_PACKAGE, fcm_project_id="p", app_version="1", name="B")
    assert other["reg_id"] != a["reg_id"]
    assert len(reg.rows()) == 2

    reloaded = PushRegistry(hass)
    await reloaded.async_load()
    assert {r["registration"] for r in reloaded.rows()} == {"R2", "R3"}
    assert reloaded.get(a["reg_id"])["push_enabled"] is True

    assert await reloaded.async_unregister("u1", "i1") is True
    assert await reloaded.async_unregister("u1", "i1") is False
    assert [r["ha_user_id"] for r in reloaded.rows()] == ["u2"]


async def test_aging_marks_stale_and_deletes_expired(hass):
    reg = PushRegistry(hass)
    await reg.async_load()
    rows = []
    for n in range(3):
        rows.append(await reg.async_register(ha_user_id=f"u{n}", installation_id="i", kind="fid",
                                             registration=f"R{n}", package=const.PORTICO_PACKAGE,
                                             fcm_project_id="p", app_version="1", name=str(n)))
    now = dt_util.utcnow()
    reg._rows[rows[1]["reg_id"]]["last_seen_at"] = (now - timedelta(days=61)).isoformat()
    reg._rows[rows[2]["reg_id"]]["last_seen_at"] = (now - timedelta(days=271)).isoformat()
    removed = await reg.async_age(now)
    assert removed == 1
    assert reg.get(rows[2]["reg_id"]) is None
    assert reg.is_stale(reg.get(rows[1]["reg_id"]), now) is True
    assert reg.is_stale(reg.get(rows[0]["reg_id"]), now) is False


async def test_private_stores_are_written_0600(hass, tmp_path):
    """The harness mocks Store writes in memory; run HA's REAL writer on these
    two store instances so the mode on disk is what production gets (§2.3:
    0600, with a permissive umask in force)."""
    import types

    from custom_components.portico.push.credentials import CredentialStore, parse_service_account

    async def _real_write(self, data):
        await self.hass.async_add_executor_job(self._write_data, data)

    creds = CredentialStore(hass)
    reg = PushRegistry(hass)
    for store in (creds._store, reg._store):
        store._async_write_data = types.MethodType(_real_write, store)
    old_umask = os.umask(0o022)
    try:
        await creds.async_save(parse_service_account(service_account_info(const.GOOGLE_TOKEN_URI)))
        await reg.async_load()
        await reg.async_register(ha_user_id="u1", installation_id="i1", kind="fid", registration="R1",
                                 package=const.PORTICO_PACKAGE, fcm_project_id="p", app_version="1", name="A")
        await hass.async_block_till_done()
    finally:
        os.umask(old_umask)
    for key in (const.CREDENTIALS_STORE_KEY, const.REGISTRY_STORE_KEY):
        path = tmp_path / ".storage" / key
        assert path.exists(), path
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, (key, oct(path.stat().st_mode))
    assert creds.path == str(tmp_path / ".storage" / const.CREDENTIALS_STORE_KEY)


# --------------------------------------------------------------------------
# WebSocket commands (§3.3)
# --------------------------------------------------------------------------


async def test_register_by_an_enrolled_non_admin_phone(hass, hass_storage, hass_ws_client, google, fake_transport):
    user, token = await _phone_user(hass)
    await _setup_all(hass, hass_storage, google, enrolled=[(user.id, "Alice's Pixel")])
    client = await hass_ws_client(hass, access_token=token)
    await client.send_json(_register_msg())
    resp = await client.receive_json()
    assert resp["success"], resp
    assert resp["result"]["push_enabled"] is False
    row = _manager(hass).registry.get(resp["result"]["reg_id"])
    assert (row["ha_user_id"], row["name"], row["kind"]) == (user.id, "Alice's Pixel", "fid")
    assert REG not in str(resp)


async def test_register_rotation_keeps_the_row_and_its_switch(hass, hass_storage, hass_ws_client, google,
                                                              fake_transport):
    user, token = await _phone_user(hass)
    await _setup_all(hass, hass_storage, google, enrolled=[(user.id, "p")])
    client = await hass_ws_client(hass, access_token=token)
    await client.send_json(_register_msg(1))
    first = (await client.receive_json())["result"]
    await _manager(hass).registry.async_set_enabled(first["reg_id"], True)
    await client.send_json(_register_msg(2, registration=REG + "-rotated", kind="token"))
    second = (await client.receive_json())["result"]
    assert second == {"reg_id": first["reg_id"], "push_enabled": True}
    assert len(_manager(hass).registry.rows()) == 1


async def test_a_non_enrolled_user_is_refused(hass, hass_storage, hass_ws_client, google, fake_transport):
    """The admin (the default test client) is not an enrolled phone."""
    await _setup_all(hass, hass_storage, google)
    client = await hass_ws_client(hass)
    await client.send_json(_register_msg())
    resp = await client.receive_json()
    assert resp["success"] is False and resp["error"]["code"] == "unauthorized"
    assert _manager(hass).registry.rows() == []


@pytest.mark.parametrize(("overrides", "code"), [
    ({"fcm_project_id": "another-project"}, "project_mismatch"),
    ({"package": "com.meda.ha_intercom"}, "package_mismatch"),
    ({"registration": "   "}, "invalid_registration"),
    ({"registration": "unknown"}, "invalid_registration"),
    ({"kind": "topic"}, "invalid_format"),
    ({"installation_id": ""}, "invalid_format"),
    ({"registration": "x" * 5000}, "invalid_format"),
])
async def test_register_refusals(hass, hass_storage, hass_ws_client, google, fake_transport, overrides, code):
    user, token = await _phone_user(hass)
    await _setup_all(hass, hass_storage, google, enrolled=[(user.id, "p")])
    client = await hass_ws_client(hass, access_token=token)
    await client.send_json(_register_msg(**overrides))
    resp = await client.receive_json()
    assert resp["success"] is False
    assert resp["error"]["code"] == code
    assert _manager(hass).registry.rows() == []


@pytest.mark.parametrize("app_version", ["0.4.3-alpha", "0.4.3", "0.1.0", "0.4.99-alpha", "", "garbage"])
async def test_register_refuses_an_app_older_than_0_5_0_with_app_too_old(
        hass, hass_storage, hass_ws_client, google, fake_transport, app_version):
    """M6 E3 (review 7, decision 2): app 0.4.3 speaks contract 2 and drops a `pv: "2"` push, so its registration is
    refused with a typed error and no row is written; `sensor.portico_push_ring_notification` then counts zero until 0.5.0 registers."""
    user, token = await _phone_user(hass)
    await _setup_all(hass, hass_storage, google, enrolled=[(user.id, "p")])
    client = await hass_ws_client(hass, access_token=token)
    await client.send_json(_register_msg(app_version=app_version))
    resp = await client.receive_json()
    assert resp["success"] is False and resp["error"]["code"] == "app_too_old"
    assert "0.5.0" in resp["error"]["message"]
    assert _manager(hass).registry.rows() == []


async def test_register_accepts_0_5_0_and_newer_and_a_missing_version_is_too_old(
        hass, hass_storage, hass_ws_client, google, fake_transport):
    user, token = await _phone_user(hass)
    await _setup_all(hass, hass_storage, google, enrolled=[(user.id, "p")])
    client = await hass_ws_client(hass, access_token=token)
    for n, version in enumerate(("0.5.0-alpha", "0.5.2-alpha", "0.6.1-beta"), start=1):
        await client.send_json(_register_msg(n, installation_id=f"inst-{n}", app_version=version))
        assert (await client.receive_json())["success"], version
    assert [r["app_version"] for r in _manager(hass).registry.rows()] == ["0.5.0-alpha", "0.5.2-alpha", "0.6.1-beta"]
    # Three rows = the per-user cap; the refusals below are checked before anything is written.
    msg = _register_msg(9, installation_id="inst-9")
    del msg["app_version"]                          # the schema default is blank: too old, never a pass
    await client.send_json(msg)
    assert (await client.receive_json())["error"]["code"] == "app_too_old"
    await client.send_json(_register_msg(10, installation_id="inst-10", app_version="0.4.9"))
    assert (await client.receive_json())["error"]["code"] == "app_too_old"
    assert len(_manager(hass).registry.rows()) == 3


async def test_register_accepts_each_version_shape_for_different_users(hass, hass_storage, hass_ws_client, google,
                                                                       fake_transport):
    versions = ("0.5.0", "0.5.2-alpha", "1.2.3", "0.6.1-beta")
    users = [await _phone_user(hass, f"Portico — v{n}") for n in range(len(versions))]
    await _setup_all(hass, hass_storage, google, enrolled=[(u.id, f"P{n}") for n, (u, _) in enumerate(users)])
    for n, ((user, token), version) in enumerate(zip(users, versions)):
        client = await hass_ws_client(hass, access_token=token)
        await client.send_json(_register_msg(1, registration=f"{REG}-{n}", app_version=version))
        assert (await client.receive_json())["success"], version
    assert sorted(r["app_version"] for r in _manager(hass).registry.rows()) == sorted(versions)


async def test_register_without_push_configured(hass, hass_ws_client, fake_transport):
    user, token = await _phone_user(hass)
    device = _observer_entry(hass)
    phones_host(hass, [{"ha_user_id": user.id, "name": "p"}])
    assert await hass.config_entries.async_setup(device.entry_id)
    from homeassistant.setup import async_setup_component

    assert await async_setup_component(hass, DOMAIN, {})      # the UI part: its websocket commands, no entry
    client = await hass_ws_client(hass, access_token=token)
    await client.send_json(_register_msg())
    resp = await client.receive_json()
    assert resp["error"]["code"] == "push_not_configured"


async def test_unregister_and_status_are_scoped_to_the_caller(hass, hass_storage, hass_ws_client, google,
                                                              fake_transport):
    alice, alice_token = await _phone_user(hass, "Portico — alice")
    bob, bob_token = await _phone_user(hass, "Portico — bob")
    await _setup_all(hass, hass_storage, google, enrolled=[(alice.id, "Alice"), (bob.id, "Bob")])
    a = await hass_ws_client(hass, access_token=alice_token)
    b = await hass_ws_client(hass, access_token=bob_token)
    await a.send_json(_register_msg(1))
    assert (await a.receive_json())["success"]
    # Bob uses the SAME installation id: his own row, Alice's untouched.
    await b.send_json(_register_msg(1, registration=REG + "-bob"))
    assert (await b.receive_json())["success"]

    await b.send_json({"id": 2, "type": "portico/push/status"})
    status = (await b.receive_json())["result"]
    assert [r["name"] for r in status["registrations"]] == ["Bob"]
    row = status["registrations"][0]
    assert "registration" not in row and REG not in str(status)
    assert set(row) == {"reg_id", "installation_id", "name", "kind", "state", "push_enabled", "last_result",
                        "last_ok_at", "last_seen_at", "registration_updated_at", "stale"}

    await b.send_json({"id": 3, "type": "portico/push/unregister", "installation_id": "inst-1"})
    assert (await b.receive_json())["result"] == {"removed": True}
    await b.send_json({"id": 4, "type": "portico/push/unregister", "installation_id": "inst-1"})
    resp = await b.receive_json()
    assert resp["success"] is False and resp["error"]["code"] == "not_found"
    assert [r["name"] for r in _manager(hass).registry.rows()] == ["Alice"]


async def test_deleting_the_phone_user_deletes_its_rows(hass, hass_storage, hass_ws_client, google, fake_transport):
    user, token = await _phone_user(hass)
    await _setup_all(hass, hass_storage, google, enrolled=[(user.id, "p")])
    client = await hass_ws_client(hass, access_token=token)
    await client.send_json(_register_msg())
    assert (await client.receive_json())["success"]
    await client.close()
    await hass.auth.async_remove_user(user)
    await hass.async_block_till_done()
    assert _manager(hass).registry.rows() == []


async def test_device_commands_do_not_see_the_push_entry(hass, hass_storage, hass_ws_client, google, fake_transport):
    await _setup_all(hass, hass_storage, google)
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "portico/doors"})
    resp = await client.receive_json()
    assert [d["driver"] for d in resp["result"]["doors"]] == ["gate_sip_observer"]


# --------------------------------------------------------------------------
# End to end: a ring through the tracker -> exactly these requests at Google
# --------------------------------------------------------------------------


@pytest.mark.parametrize("push_first", [False, True])
async def test_end_to_end_ring_answer(hass, hass_storage, hass_ws_client, google, fake_transport, push_first):
    user, token = await _phone_user(hass)
    device, _push = await _setup_all(hass, hass_storage, google, enrolled=[(user.id, "p")], push_first=push_first)
    client = await hass_ws_client(hass, access_token=token)
    await client.send_json(_register_msg())
    reg_id = (await client.receive_json())["result"]["reg_id"]
    manager = _manager(hass)

    go: FakeGoTransport = fake_transport.instances[-1]
    go.ring("gso-1")  # push_enabled still OFF: nothing may be sent
    go.state("gso-1", "ended")
    await hass.async_block_till_done()
    await manager.notifier.async_drain()
    assert google.fcm_requests == []

    await manager.registry.async_set_enabled(reg_id, True)
    go.ring("gso-2")
    await hass.async_block_till_done()
    await manager.notifier.async_drain()
    go.state("gso-2", "answered")
    await hass.async_block_till_done()
    await manager.notifier.async_drain()

    assert len(google.fcm_requests) == 2
    ring, answered = (r["json"]["message"] for r in google.fcm_requests)
    assert ring["fid"] == REG and answered["fid"] == REG
    assert ring["data"]["kind"] == "ring" and answered["data"]["kind"] == "answered"
    door_ref = hass.data[DOMAIN][DATA_PROVIDERS].doors()[0].door_ref
    assert ring["data"]["door_ref"] == door_ref != device.entry_id  # derived; never the gate-sip URL (§4.1) or an entry id
    assert ring["data"]["pv"] == "2" and "ct" not in ring["data"] and "from_ref" not in ring["data"]
    assert ring["data"]["call_id"] == "gso-2"
    assert "gate-sip" not in str(google.fcm_requests)
    assert {r["project"] for r in google.fcm_requests} == {"portico-test"}
    assert len(google.token_requests) == 1  # prefetched once, reused


async def test_unavailable_device_ends_and_dismisses(hass, hass_storage, hass_ws_client, google, fake_transport):
    user, token = await _phone_user(hass)
    await _setup_all(hass, hass_storage, google, enrolled=[(user.id, "p")])
    client = await hass_ws_client(hass, access_token=token)
    await client.send_json(_register_msg())
    reg_id = (await client.receive_json())["result"]["reg_id"]
    manager = _manager(hass)
    await manager.registry.async_set_enabled(reg_id, True)
    go = fake_transport.instances[-1]
    go.ring("gso-9")
    await hass.async_block_till_done()
    go.availability(False, "gate-sip down")
    await hass.async_block_till_done()
    await manager.notifier.async_drain()
    assert [r["json"]["message"]["data"]["kind"] for r in google.fcm_requests] == ["ring", "ended"]


async def test_unloading_the_push_entry_detaches_and_leaves_rings_alone(hass, hass_storage, google, fake_transport):
    device, push = await _setup_all(hass, hass_storage, google)
    assert await hass.config_entries.async_unload(push.entry_id)
    await hass.async_block_till_done()
    assert const.DATA_PUSH not in hass.data.get(DOMAIN, {})
    tracker = device.runtime_data.call_state
    # the push sender no longer follows the registry; the door's own tracker listener is the provider's
    # (the endpoint registry's always-on subscription, managed Talk, DEVICES-10, stays: it is not the push sender's)
    from custom_components.portico.endpoints import EndpointRegistry
    from custom_components.portico.call_events import CallEvents
    from custom_components.portico.house import HouseCallRegistry

    assert [cb for cb in hass.data[DOMAIN][DATA_PROVIDERS]._listeners
            if not isinstance(getattr(cb, "__self__", None), (EndpointRegistry, HouseCallRegistry, CallEvents))] == []
    assert len(tracker._transition_listeners) == 1
    fake_transport.instances[-1].ring("gso-3")
    await hass.async_block_till_done()
    assert tracker.state == "ringing"
    assert google.fcm_requests == []


async def test_the_old_apps_helpers_are_never_touched(hass, hass_storage, hass_ws_client, google, fake_transport):
    """§0.2: no read or write of any input_text, in particular never
    input_text.intercom_call_id (that would ring the house twice)."""
    calls = []
    hass.bus.async_listen("call_service", lambda e: calls.append(e.data))
    user, token = await _phone_user(hass)
    await _setup_all(hass, hass_storage, google, enrolled=[(user.id, "p")])
    client = await hass_ws_client(hass, access_token=token)
    await client.send_json(_register_msg())
    reg_id = (await client.receive_json())["result"]["reg_id"]
    await _manager(hass).registry.async_set_enabled(reg_id, True)
    fake_transport.instances[-1].ring("gso-4")
    await hass.async_block_till_done()
    await _manager(hass).notifier.async_drain()
    assert calls == []
    assert not [s for s in hass.states.async_all() if s.entity_id.startswith("input_text.")]


# --------------------------------------------------------------------------
# G2 / decision Q3: "New phones get the push ring notification" applies to NEW rows only
# --------------------------------------------------------------------------


async def test_new_rows_take_the_default_and_existing_rows_keep_theirs(hass, hass_storage):
    default = {"on": False}
    reg = PushRegistry(hass, new_row_enabled=lambda: default["on"])
    await reg.async_load()

    def _register(user, registration):
        return reg.async_register(ha_user_id=user, installation_id="i", kind="fid", registration=registration,
                                  package=const.PORTICO_PACKAGE, fcm_project_id="p", app_version="1", name=user)

    off = await _register("u1", "R1")
    assert off["push_enabled"] is False
    default["on"] = True
    on = await _register("u2", "R2")
    assert on["push_enabled"] is True
    # The option changed, but a row that exists keeps its switch, even across
    # a rotation (a re-register of the same installation).
    rotated = await _register("u1", "R1-rotated")
    assert rotated["reg_id"] == off["reg_id"] and rotated["push_enabled"] is False
    default["on"] = False
    again = await _register("u2", "R2-rotated")
    assert again["reg_id"] == on["reg_id"] and again["push_enabled"] is True


async def test_a_bare_registry_keeps_the_old_off_default(hass, hass_storage):
    """No default given (an entry from before the option): OFF, as P1 shipped."""
    reg = PushRegistry(hass)
    await reg.async_load()
    row = await reg.async_register(ha_user_id="u", installation_id="i", kind="fid", registration="R",
                                   package=const.PORTICO_PACKAGE, fcm_project_id="p", app_version="1", name="n")
    assert row["push_enabled"] is False
