# ruff: noqa: F811 - test parameters named after the imported fixtures
"""No regression on the real ring path (P2 plan G5/G6 PR, part d).

A fake call goes through the WHOLE path: the fake Go transport's events ->
the device entry's CallStateTracker -> the transition listener -> the
RingNotifier -> FcmClient -> the local fake Google. The requests Google
receives must be exactly the P1 messages, byte for byte in shape: the v1.1
additions (the `test` kind, the allow-list, `portico.push_test`) change
nothing for a real call while the allow-list is empty.

HA-harness session; the only network peer is the local fake Google.
"""
from __future__ import annotations

from custom_components.portico.config_flow import DOMAIN
from custom_components.portico.provider import DATA_PROVIDERS
from custom_components.portico.push import const, payload

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

WINDOW = const.DEFAULT_RING_WINDOW


def _expected(kind, registration, data, *, ttl_s):
    return {
        kind: registration,
        "data": data,
        "android": {"priority": "HIGH", "ttl": f"{ttl_s}s", "restricted_package_name": const.PORTICO_PACKAGE,
                    "direct_boot_ok": False},
    }


async def _settle(hass):
    await hass.async_block_till_done()
    await _manager(hass).notifier.async_drain()
    await hass.async_block_till_done()


async def test_full_path_produces_exactly_the_p1_messages(hass, hass_storage, hass_ws_client, google,
                                                          fake_transport):
    alice, alice_token = await _phone_user(hass, "Portico — alice")
    bob, bob_token = await _phone_user(hass, "Portico — bob")
    device, _push = await _setup_all(
        hass, hass_storage, google, enrolled=[(alice.id, "Alice"), (bob.id, "Bob")],
        # "test_phones" spelled out, not const.CONF_TEST_PHONES: this test
        # also runs unchanged against P1 (which ignores the key), proving the
        # same messages before and after.
        **{const.CONF_ENABLED: True, const.CONF_RING_WINDOW: WINDOW, "test_phones": []})
    phones = {}
    for name, token, kind in (("Alice", alice_token, "fid"), ("Bob", bob_token, "token")):
        client = await hass_ws_client(hass, access_token=token)
        await client.send_json(_register_msg(registration=f"{REG}-{name}", kind=kind))
        reg_id = (await client.receive_json())["result"]["reg_id"]
        await _manager(hass).registry.async_set_enabled(reg_id, True)
        phones[name] = (kind, f"{REG}-{name}")
    go = fake_transport.instances[-1]
    door = hass.data[DOMAIN][DATA_PROVIDERS].doors()[0].door_ref      # derived from the endpoint id (push/v2)

    # Call 1: rings, then answered (answered goes to both; no ended after it).
    go.ring("gso-1")
    await _settle(hass)
    go.state("gso-1", "answered")
    await _settle(hass)
    go.state("gso-1", "ended")
    await _settle(hass)
    # Call 2 (after the 5 s per-device spacing): rings, then ends unanswered.
    _manager(hass).notifier._device_rings.clear()
    go.ring("gso-2")
    await _settle(hass)
    go.state("gso-2", "ended")
    await _settle(hass)

    sent = google.sent_messages()
    assert {r["project"] for r in google.fcm_requests} == {"portico-test"}
    by_phone: dict[str, list[dict]] = {}
    for message in sent:
        registration = message.get("fid") or message.get("token")
        by_phone.setdefault(registration, []).append(message)
    assert set(by_phone) == {reg for _k, reg in phones.values()}

    for kind, registration in phones.values():
        got = by_phone[registration]
        assert [(m["data"]["call_id"], m["data"]["kind"]) for m in got] == [
            ("gso-1", "ring"), ("gso-1", "answered"), ("gso-2", "ring"), ("gso-2", "ended")]
        ring1, answered1, ring2, ended2 = got
        for ring, call in ((ring1, "gso-1"), (ring2, "gso-2")):
            t = int(ring["data"]["t"])
            assert ring["data"] == payload.build_ring(door, call, t, WINDOW)
            # The TTL counts down from the transition (§4.2): the whole window,
            # less the second or so the test took to get it to Google.
            ttl = int(ring["android"]["ttl"].removesuffix("s"))
            assert WINDOW - 2 <= ttl <= WINDOW
            assert ring == _expected(kind, registration, ring["data"], ttl_s=ttl)
        assert answered1 == _expected(kind, registration,
                                      payload.build_answered(door, "gso-1", int(answered1["data"]["t"])), ttl_s=60)
        assert ended2 == _expected(kind, registration,
                                   payload.build_ended(door, "gso-2", int(ended2["data"]["t"])), ttl_s=60)
        for message in got:
            assert message["data"]["pv"] == "2"
            assert message["data"]["kind"] != "test"
    assert len(sent) == 8
