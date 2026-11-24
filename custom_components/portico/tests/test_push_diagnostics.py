# ruff: noqa: F811 - test parameters named after the imported fixtures
"""The push ring notification diagnostic sensor, repair issues and diagnostics download
(sender design §6, §7), and the no-secrets guarantee across logs and
diagnostics during failing sends.

HA-harness session; the only network peer is the local fake Google.
"""
from __future__ import annotations

import json
import logging

from homeassistant.const import EntityCategory
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from custom_components.portico.config_flow import DOMAIN
from custom_components.portico.diagnostics import async_get_config_entry_diagnostics
from custom_components.portico.push import const

from .fake_google import fcm_error, service_account_info
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

SENSOR = "sensor.portico_push_ring_notification"


async def _enabled_phone(hass, hass_storage, hass_ws_client, google, **options):
    user, token = await _phone_user(hass)
    device, push = await _setup_all(hass, hass_storage, google, enrolled=[(user.id, "Alice's Pixel")], **options)
    client = await hass_ws_client(hass, access_token=token)
    await client.send_json(_register_msg())
    reg_id = (await client.receive_json())["result"]["reg_id"]
    await _manager(hass).registry.async_set_enabled(reg_id, True)
    await hass.async_block_till_done()
    return device, push, reg_id


async def _ring(hass, fake_transport, call_id):
    fake_transport.instances[-1].ring(call_id)
    await hass.async_block_till_done()
    await _manager(hass).notifier.async_drain()
    await hass.async_block_till_done()


async def test_sensor_ok_with_no_phone(hass, hass_storage, google, fake_transport):
    _device, push = await _setup_all(hass, hass_storage, google)
    state = hass.states.get(SENSOR)
    assert state is not None
    assert state.state == "ok"
    assert state.attributes["registrations"] == 0
    assert state.attributes["devices"] == []
    entity = er.async_get(hass).async_get(SENSOR)
    assert entity.entity_category is EntityCategory.DIAGNOSTIC
    assert entity.config_entry_id == push.entry_id


async def test_sensor_after_a_ring(hass, hass_storage, hass_ws_client, google, fake_transport):
    await _enabled_phone(hass, hass_storage, hass_ws_client, google)
    await _ring(hass, fake_transport, "gso-1")
    attrs = hass.states.get(SENSOR).attributes
    assert hass.states.get(SENSOR).state == "ok"
    assert attrs["last_call_id"] == "gso-1"
    assert attrs["last_send_at"]
    assert (attrs["sent_24h"], attrs["failed_24h"], attrs["registrations"]) == (1, 0, 1)
    assert attrs["devices"] == [{
        "name": "Alice's Pixel", "state": "ok", "push_enabled": True, "last_result": "ok",
        "last_ok_at": attrs["devices"][0]["last_ok_at"],
        "registration_updated_at": attrs["devices"][0]["registration_updated_at"], "stale": False,
    }]
    assert attrs["devices"][0]["last_ok_at"]
    assert REG not in json.dumps(dict(attrs))


async def test_disabled_switch_shows_disabled(hass, hass_storage, google, fake_transport):
    await _setup_all(hass, hass_storage, google, **{const.CONF_ENABLED: False})
    assert hass.states.get(SENSOR).state == "disabled"


async def test_transient_failure_is_degraded_without_a_repair_issue(hass, hass_storage, hass_ws_client, google,
                                                                     fake_transport, monkeypatch):
    from custom_components.portico.push import notifier

    monkeypatch.setattr(notifier, "BACKOFF", (0.01,))
    await _enabled_phone(hass, hass_storage, hass_ws_client, google, **{const.CONF_RING_WINDOW: 10})
    monkeypatch.setattr(notifier, "RING_DEADLINE_MARGIN", 9.95)  # retry for ~50 ms only
    google.fcm_default = lambda record: fcm_error(503, "UNAVAILABLE")
    await _ring(hass, fake_transport, "gso-1")
    assert hass.states.get(SENSOR).state == "degraded"
    assert hass.states.get(SENSOR).attributes["failed_24h"] >= 1
    assert not [i for i in ir.async_get(hass).issues.values() if i.domain == DOMAIN]

    google.fcm_default = None
    fake_transport.instances[-1].state("gso-1", "ended")
    google.fcm_script.clear()
    await hass.async_block_till_done()
    # The next call after the 5 s device spacing.
    _manager(hass).notifier._device_rings.clear()
    await _ring(hass, fake_transport, "gso-2")
    assert hass.states.get(SENSOR).state == "ok"


async def test_credentials_invalid_raises_and_clears_its_issue(hass, hass_storage, hass_ws_client, google,
                                                               fake_transport):
    await _enabled_phone(hass, hass_storage, hass_ws_client, google)
    google.fcm_script.append(fcm_error(403, "PERMISSION_DENIED"))
    await _ring(hass, fake_transport, "gso-1")
    assert hass.states.get(SENSOR).state == "credentials_invalid"
    issue = ir.async_get(hass).async_get_issue(DOMAIN, const.ISSUE_CREDENTIALS_INVALID)
    assert issue is not None and issue.translation_key == const.ISSUE_CREDENTIALS_INVALID

    fake_transport.instances[-1].state("gso-1", "ended")
    await hass.async_block_till_done()
    _manager(hass).notifier._device_rings.clear()
    await _ring(hass, fake_transport, "gso-2")
    assert hass.states.get(SENSOR).state == "ok"
    assert ir.async_get(hass).async_get_issue(DOMAIN, const.ISSUE_CREDENTIALS_INVALID) is None


async def test_oauth_invalid_grant_is_credentials_invalid(hass, hass_storage, hass_ws_client, google,
                                                          fake_transport):
    google.token_script.append((400, {"error": "invalid_grant"}, {}))
    await _setup_all(hass, hass_storage, google)
    await hass.async_block_till_done(wait_background_tasks=True)  # the token prefetch
    assert hass.states.get(SENSOR).state == "credentials_invalid"
    assert ir.async_get(hass).async_get_issue(DOMAIN, const.ISSUE_CREDENTIALS_INVALID) is not None


async def test_project_mismatch_issue_names_the_phone(hass, hass_storage, hass_ws_client, google, fake_transport):
    _d, _p, reg_id = await _enabled_phone(hass, hass_storage, hass_ws_client, google)
    google.fcm_script.append(fcm_error(403, "PERMISSION_DENIED", error_code="SENDER_ID_MISMATCH"))
    await _ring(hass, fake_transport, "gso-1")
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"{const.ISSUE_PROJECT_MISMATCH}_{reg_id}")
    assert issue is not None
    assert issue.translation_placeholders == {"phone": "Alice's Pixel"}
    assert hass.states.get(SENSOR).attributes["devices"][0]["state"] == "project_mismatch"


async def test_payload_rejected_issue(hass, hass_storage, hass_ws_client, google, fake_transport, caplog):
    await _enabled_phone(hass, hass_storage, hass_ws_client, google)
    google.fcm_script.append(fcm_error(400, "INVALID_ARGUMENT", field="message.data"))
    await _ring(hass, fake_transport, "gso-1")
    assert ir.async_get(hass).async_get_issue(DOMAIN, const.ISSUE_PAYLOAD_REJECTED) is not None
    assert "Portico bug" in caplog.text


async def test_rate_limit_circuit_raises_an_issue(hass, hass_storage, hass_ws_client, google, fake_transport,
                                                  monkeypatch):
    from custom_components.portico.push import notifier

    monkeypatch.setattr(notifier, "GLOBAL_PER_MINUTE", 1)
    await _enabled_phone(hass, hass_storage, hass_ws_client, google)
    await _ring(hass, fake_transport, "gso-1")
    fake_transport.instances[-1].state("gso-1", "ended")  # a 2nd message in the minute
    await hass.async_block_till_done()
    await _manager(hass).notifier.async_drain()
    assert ir.async_get(hass).async_get_issue(DOMAIN, const.ISSUE_RATE_LIMITED) is not None
    assert _manager(hass).counters["skipped_rate_limited"] == 1


async def test_no_ordinary_unavailability_issue(hass, hass_storage, hass_ws_client, google, fake_transport):
    """§7: the device or transport going away raises no push issue."""
    await _enabled_phone(hass, hass_storage, hass_ws_client, google)
    await _ring(hass, fake_transport, "gso-1")
    fake_transport.instances[-1].availability(False, "down")
    await hass.async_block_till_done()
    await _manager(hass).notifier.async_drain()
    assert not [i for i in ir.async_get(hass).issues.values() if i.domain == DOMAIN]


# --------------------------------------------------------------------------
# Diagnostics, and no secret anywhere during failures
# --------------------------------------------------------------------------


async def test_diagnostics_and_logs_carry_no_secret(hass, hass_storage, hass_ws_client, google, fake_transport,
                                                    caplog, monkeypatch):
    from custom_components.portico.push import notifier

    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(notifier, "BACKOFF", (0.01,))
    device, push, _reg = await _enabled_phone(hass, hass_storage, hass_ws_client, google,
                                              **{const.CONF_RING_WINDOW: 10})
    monkeypatch.setattr(notifier, "RING_DEADLINE_MARGIN", 9.95)
    google.fcm_script.extend([fcm_error(503, "UNAVAILABLE"), fcm_error(400, "INVALID_ARGUMENT"),
                              fcm_error(401, "UNAUTHENTICATED")])
    await _ring(hass, fake_transport, "gso-1")
    fake_transport.instances[-1].state("gso-1", "ended")
    await hass.async_block_till_done()
    await _manager(hass).notifier.async_drain()

    push_diag = await async_get_config_entry_diagnostics(hass, push)
    device_diag = await async_get_config_entry_diagnostics(hass, device)
    # Left out, each for a stated reason, and nothing else:
    # - the harness's in-memory Store mock logs every value it loads at DEBUG
    #   (HA's real Store logs only keys and paths);
    # - the fake Google's own aiohttp access log (test-only server);
    # - HA core's websocket_api, which at DEBUG logs every message a client
    #   sends, the phone's own register call included. That is HA's logging,
    #   off by default, not Portico's; the PR notes it.
    excluded = ("pytest_homeassistant_custom_component.common", "aiohttp.access",
                "homeassistant.components.websocket_api")
    logs = "\n".join(r.getMessage() for r in caplog.records if not r.name.startswith(excluded))
    assert any(r.name.startswith("custom_components.portico") for r in caplog.records)
    blob = json.dumps([push_diag, device_diag], default=str) + logs

    key = service_account_info(google.token_uri)["private_key"]
    for line in key.splitlines()[1:-1]:
        assert line not in blob
    assert "PRIVATE KEY" not in blob
    for token in google.issued:
        assert token not in blob
        assert token[:20] not in blob
    assert REG not in blob and REG[:10] not in blob
    assert "Bearer" not in blob
    assert google.base not in blob  # no URL of the (fake) Google

    assert push_diag["push"]["registrations"][0]["name"].startswith("redacted-")      # DEV-700 A: a phone's name
    assert "registration" not in push_diag["push"]["registrations"][0]
    assert push_diag["push"]["counters"]["sent"] >= 1


async def test_a_missing_key_raises_an_issue_that_a_good_setup_clears(hass, hass_storage, google, fake_transport):
    from homeassistant.config_entries import ConfigEntryState

    from .test_push_registry import _push_entry

    entry = _push_entry(hass, hass_storage, google)
    del hass_storage[const.CREDENTIALS_STORE_KEY]
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert ir.async_get(hass).async_get_issue(DOMAIN, const.ISSUE_CREDENTIALS_INVALID) is not None
    assert hass.states.get(SENSOR) is None

    hass_storage[const.CREDENTIALS_STORE_KEY] = {"version": 1, "minor_version": 1, "key": const.CREDENTIALS_STORE_KEY,
                                                 "data": service_account_info(google.token_uri)}
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert ir.async_get(hass).async_get_issue(DOMAIN, const.ISSUE_CREDENTIALS_INVALID) is None
    assert hass.states.get(SENSOR).state == "ok"
