# ruff: noqa: F811 - test parameters named after the imported fixtures
"""The staged real-device test (sender design §8.3; P2 plan gap G6): the push
entry's TEST allow-list and the admin-only `portico.push_test` service.

- `push_test` sends ONE `kind: "test"` message to each allow-listed phone,
  whether or not that phone is switched on for the push ring notification, and to nothing
  else; an empty allow-list sends nothing and says why.
- While the allow-list is set, a REAL ring goes only to allow-listed phones
  that are also switched on, so a staged test cannot ring the household.
- A phone that is not switched on never gets a real ring, allow-listed or not.

HA-harness session; the only network peer is the local fake Google, and every
assertion about "never contacted" is made against its request log.
"""
from __future__ import annotations

import json
import logging

import pytest
from homeassistant.auth.const import GROUP_ID_USER
from homeassistant.core import Context
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError, Unauthorized

from custom_components.portico.config_flow import DOMAIN
from custom_components.portico.diagnostics import async_get_config_entry_diagnostics
from custom_components.portico.push import const, notifier, payload

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
REG_A = REG + "-alice"
REG_B = REG + "-bob"


async def _two_phones(hass, hass_storage, hass_ws_client, google, *, test_phones=("A",), enable=(), **options):
    """Alice and Bob registered (both OFF unless named in `enable`); the
    allow-list holds the named ones. Returns ({"A": reg_id, "B": reg_id}, push)."""
    alice, alice_token = await _phone_user(hass, "Portico — alice")
    bob, bob_token = await _phone_user(hass, "Portico — bob")
    _d, push = await _setup_all(hass, hass_storage, google, enrolled=[(alice.id, "Alice"), (bob.id, "Bob")],
                                **options)
    ids = {}
    for key, token, reg in (("A", alice_token, REG_A), ("B", bob_token, REG_B)):
        client = await hass_ws_client(hass, access_token=token)
        await client.send_json(_register_msg(registration=reg))
        ids[key] = (await client.receive_json())["result"]["reg_id"]
    registry = _manager(hass).registry
    for key in enable:
        await registry.async_set_enabled(ids[key], True)
    hass.config_entries.async_update_entry(
        push, options={**push.options, const.CONF_TEST_PHONES: [ids[k] for k in test_phones]})
    await hass.async_block_till_done()
    return ids, push


async def _push_test(hass, **data):
    return await hass.services.async_call(DOMAIN, "push_test", data, blocking=True, return_response=True)


def _sent_registrations(google) -> list[str]:
    return [m.get("fid") or m.get("token") for m in google.sent_messages()]


async def _ring(hass, fake_transport, call_id):
    fake_transport.instances[-1].ring(call_id)
    await hass.async_block_till_done()
    await _manager(hass).notifier.async_drain()


# --------------------------------------------------------------------------
# The service
# --------------------------------------------------------------------------


async def test_the_service_exists_only_with_the_push_entry(hass, hass_storage, google, fake_transport):
    assert not hass.services.has_service(DOMAIN, "push_test")
    _d, push = await _setup_all(hass, hass_storage, google)
    assert hass.services.has_service(DOMAIN, "push_test")
    assert await hass.config_entries.async_unload(push.entry_id)
    await hass.async_block_till_done()
    assert not hass.services.has_service(DOMAIN, "push_test")


async def test_allow_listed_phone_gets_one_test_push_even_when_off(hass, hass_storage, hass_ws_client, google,
                                                                    fake_transport):
    ids, _push = await _two_phones(hass, hass_storage, hass_ws_client, google, test_phones=("A",))
    assert _manager(hass).registry.get(ids["A"])["push_enabled"] is False
    response = await _push_test(hass)
    assert _sent_registrations(google) == [REG_A]
    message = google.sent_messages()[0]
    # Exactly the contract's `test` case shape, and the builder's.
    assert set(message["data"]) == {"pv", "kind", "t"}
    assert message["data"] == payload.build_test(int(message["data"]["t"]))
    assert message["android"]["priority"] == "HIGH" and message["android"]["ttl"] == "60s"
    assert message["android"]["restricted_package_name"] == const.PORTICO_PACKAGE
    assert response == {"sent": 1, "results": [{"phone": "Alice", "result": "ok"}]}
    assert REG_A not in json.dumps(response)


async def test_a_phone_off_the_allow_list_is_never_contacted(hass, hass_storage, hass_ws_client, google,
                                                              fake_transport):
    ids, _push = await _two_phones(hass, hass_storage, hass_ws_client, google, test_phones=("A",), enable=("B",))
    # Bob is switched ON for the push ring notification, but not allow-listed: no test.
    await _push_test(hass)
    assert REG_B not in json.dumps(google.fcm_requests)
    # Naming him explicitly is refused, and nothing at all is sent.
    google.fcm_requests.clear()
    _manager(hass).notifier._test_sends.clear()
    with pytest.raises(ServiceValidationError) as err:
        await _push_test(hass, phones=[ids["B"]])
    assert err.value.translation_key == "push_test_not_allowed"
    with pytest.raises(ServiceValidationError):
        await _push_test(hass, phones=[ids["A"], ids["B"]])
    assert google.fcm_requests == []
    # Naming only allow-listed phones works.
    await _push_test(hass, phones=[ids["A"]])
    assert _sent_registrations(google) == [REG_A]


async def test_an_empty_allow_list_sends_nothing(hass, hass_storage, hass_ws_client, google, fake_transport):
    await _two_phones(hass, hass_storage, hass_ws_client, google, test_phones=(), enable=("A", "B"))
    with pytest.raises(ServiceValidationError) as err:
        await _push_test(hass)
    assert err.value.translation_key == "push_test_no_allow_list"
    assert google.fcm_requests == []


async def test_a_missing_allow_list_option_is_empty(hass, hass_storage, google, fake_transport):
    """A push entry from before this option has none stored: empty."""
    _d, push = await _setup_all(hass, hass_storage, google)
    assert const.CONF_TEST_PHONES not in push.options
    assert _manager(hass).test_phones == []
    with pytest.raises(ServiceValidationError):
        await _push_test(hass)
    assert google.fcm_requests == []


async def test_an_allow_listed_phone_without_a_live_registration(hass, hass_storage, hass_ws_client, google,
                                                                 fake_transport):
    ids, _push = await _two_phones(hass, hass_storage, hass_ws_client, google, test_phones=("A",))
    _manager(hass).registry.mark_dead(ids["A"], "unregistered")
    with pytest.raises(ServiceValidationError) as err:
        await _push_test(hass)
    assert err.value.translation_key == "push_test_no_live_phone"
    assert google.fcm_requests == []


async def test_only_an_admin_may_call_it(hass, hass_storage, hass_ws_client, google, fake_transport):
    await _two_phones(hass, hass_storage, hass_ws_client, google, test_phones=("A",))
    user = await hass.auth.async_create_user(name="not admin", group_ids=[GROUP_ID_USER])
    with pytest.raises(Unauthorized):
        await hass.services.async_call(DOMAIN, "push_test", {}, blocking=True, return_response=True,
                                       context=Context(user_id=user.id))
    assert google.fcm_requests == []


async def test_caps(hass, hass_storage, hass_ws_client, google, fake_transport, monkeypatch):
    await _two_phones(hass, hass_storage, hass_ws_client, google, test_phones=("A",))
    await _push_test(hass)
    # At most one test call per spacing ...
    with pytest.raises(ServiceValidationError) as err:
        await _push_test(hass)
    assert err.value.translation_key == "push_test_rate_limited"
    assert len(google.fcm_requests) == 1
    # ... and at most TEST_PER_HOUR an hour.
    monkeypatch.setattr(notifier, "TEST_MIN_SPACING", 0.0)
    for _ in range(notifier.TEST_PER_HOUR - 1):
        await _push_test(hass)
    with pytest.raises(ServiceValidationError):
        await _push_test(hass)
    assert len(google.fcm_requests) == notifier.TEST_PER_HOUR


async def test_a_test_counts_against_the_global_circuit(hass, hass_storage, hass_ws_client, google, fake_transport,
                                                        monkeypatch):
    monkeypatch.setattr(notifier, "GLOBAL_PER_MINUTE", 0)
    await _two_phones(hass, hass_storage, hass_ws_client, google, test_phones=("A",))
    response = await _push_test(hass)
    assert response == {"sent": 0, "results": [{"phone": "Alice", "result": "rate_limited"}]}
    assert google.fcm_requests == []


async def test_sensor_and_diagnostics_after_a_test(hass, hass_storage, hass_ws_client, google, fake_transport,
                                                   caplog):
    caplog.set_level(logging.DEBUG)
    _ids, push = await _two_phones(hass, hass_storage, hass_ws_client, google, test_phones=("A",))
    await _push_test(hass)
    await hass.async_block_till_done()
    attrs = hass.states.get(SENSOR).attributes
    assert attrs["sent_24h"] == 1 and attrs["failed_24h"] == 0
    assert attrs["last_send_at"]
    assert attrs["last_call_id"] is None  # a test is not a call
    assert attrs["test_phones"] == 1
    alice = next(d for d in attrs["devices"] if d["name"] == "Alice")
    assert alice["last_result"] == "ok" and alice["last_ok_at"] and alice["state"] == "ok"
    diag = await async_get_config_entry_diagnostics(hass, push)
    assert diag["push"]["counters"]["test"] == 1
    assert diag["push"]["test_phones"] == 1
    # Nothing sensitive logged by Portico (HA core's websocket_api logs the
    # phone's own register message at DEBUG; that is not ours).
    ours = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("custom_components.portico"))
    assert "test push" in ours
    assert REG_A not in ours and REG_B not in ours and "Alice" not in ours
    assert REG_A not in json.dumps(diag, default=str)


async def test_a_failed_test_is_reported_not_raised(hass, hass_storage, hass_ws_client, google, fake_transport):
    from .fake_google import fcm_error

    await _two_phones(hass, hass_storage, hass_ws_client, google, test_phones=("A",))
    google.fcm_script.append(fcm_error(404, "NOT_FOUND", error_code="UNREGISTERED"))
    response = await _push_test(hass)
    assert response == {"sent": 1, "results": [{"phone": "Alice", "result": "dead"}]}
    assert len(google.fcm_requests) == 1  # no retry of a test


# --------------------------------------------------------------------------
# The allow-list and REAL rings
# --------------------------------------------------------------------------


async def test_while_the_allow_list_is_set_a_real_ring_reaches_only_allowed_enabled_phones(
        hass, hass_storage, hass_ws_client, google, fake_transport):
    await _two_phones(hass, hass_storage, hass_ws_client, google, test_phones=("A",), enable=("A", "B"))
    await _ring(hass, fake_transport, "gso-1")
    assert _sent_registrations(google) == [REG_A]


async def test_an_allow_listed_phone_that_is_off_gets_no_real_ring(hass, hass_storage, hass_ws_client, google,
                                                                   fake_transport):
    await _two_phones(hass, hass_storage, hass_ws_client, google, test_phones=("A", "B"), enable=("B",))
    await _ring(hass, fake_transport, "gso-1")
    assert _sent_registrations(google) == [REG_B]
    fake_transport.instances[-1].state("gso-1", "ended")
    await hass.async_block_till_done()
    await _manager(hass).notifier.async_drain()
    assert REG_A not in json.dumps(google.fcm_requests)


async def test_with_no_allow_list_rings_go_to_every_enabled_phone(hass, hass_storage, hass_ws_client, google,
                                                                  fake_transport):
    await _two_phones(hass, hass_storage, hass_ws_client, google, test_phones=(), enable=("A", "B"))
    await _ring(hass, fake_transport, "gso-1")
    assert sorted(_sent_registrations(google)) == sorted([REG_A, REG_B])


# --------------------------------------------------------------------------
# Config flow and options flow
# --------------------------------------------------------------------------


async def test_config_flow_starts_with_an_empty_allow_list(hass, google):
    from .test_push_flow import _sa, _start

    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"service_account_json": _sa(google)})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["result"].options[const.CONF_TEST_PHONES] == []


async def test_options_flow_edits_the_allow_list(hass, hass_storage, hass_ws_client, google, fake_transport):
    ids, push = await _two_phones(hass, hass_storage, hass_ws_client, google, test_phones=())
    result = await hass.config_entries.options.async_init(push.entry_id)
    assert result["type"] is FlowResultType.MENU
    assert "push_test_phones" in result["menu_options"]
    result = await hass.config_entries.options.async_configure(result["flow_id"],
                                                                {"next_step_id": "push_test_phones"})
    assert result["step_id"] == "push_test_phones"
    field = next(k for k in result["data_schema"].schema if k == const.CONF_TEST_PHONES)
    assert field.default() == []
    assert set(result["data_schema"].schema[field].options) == set(ids.values())
    result = await hass.config_entries.options.async_configure(result["flow_id"],
                                                                {const.CONF_TEST_PHONES: [ids["B"]]})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert push.options[const.CONF_TEST_PHONES] == [ids["B"]]
    assert _manager(hass).test_phones == [ids["B"]]
    # A removed phone drops out of the allow-list as it is read.
    await _manager(hass).registry.async_remove(ids["B"])
    assert _manager(hass).test_phones == []
