"""away_media.py: call audio over the signaling websocket (design: home
automation/portico_away_media_design.md). Real HA websocket connections; the transport is a mock.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from .. import auth, away_media
from .driver_part import get_provider
from .driver_part import TransportClient, TransportError
from .test_signaling import _add_device, _recv, _setup_signaling

pytestmark = pytest.mark.asyncio

OPEN = {"type": "portico/media_open", "door_id": "127.0.0.1", "call_id": "c1", "codec": "pcmu"}


def _device(hass, user, *, enabled=True, state="ringing", legacy_option=None, driver="dahua_sip"):
    entry, runtime = _add_device(hass, driver=driver, capabilities={"DirectCapable": False},
                                 enrolled_phones=[{"ha_user_id": user.id, "name": "p"}])
    # The door's `away_media` flag is a capability now (always on, no option): False only for a ring-only door without
    # talk-back. `legacy_option` stores the retired option on the entry, which must change nothing.
    runtime.ring_only = not enabled
    runtime.talk_attached = False
    if legacy_option is not None:
        hass.config_entries.async_update_entry(entry, options={"away_media": legacy_option})
    runtime.client.answer = AsyncMock(return_value={})
    async def _media_open(_call_id, codec="pcmu", **_kwargs):      # `stats=True` when the wheel lists call_stats (DEV-811 P3)
        # The real transport echoes the codec and reports that codec's frame size (contract/ws_media_v2.json: 320 / 1280).
        return {"codec": codec, "frame_bytes": 1280 if codec == "l16" else 320, "frame_ms": 40}

    runtime.client.media_open = AsyncMock(side_effect=_media_open)
    runtime.client.media_close = AsyncMock(return_value={"ok": True})
    runtime.client.media_frame_nowait = MagicMock()
    runtime.call_state.apply_event({"call_id": "c1", "state": state})
    return entry, runtime


async def _answer(client, msg_id=1):
    await client.send_json({"id": msg_id, "type": "portico/answer", "door_id": "127.0.0.1",
                            "call_id": "c1", "client_kind": "browser"})
    resp = await _recv(client)
    assert resp["success"], resp


async def _open(client, msg_id=2, **over):
    await client.send_json({"id": msg_id, **OPEN, **over})
    return await _recv(client)


async def test_a_door_that_cannot_carry_audio_answers_away_media_disabled(hass, hass_ws_client, hass_admin_user):
    _entry, runtime = _device(hass, hass_admin_user, enabled=False)
    await _setup_signaling(hass)
    client = await hass_ws_client(hass)
    await _answer(client)
    resp = await _open(client)
    assert resp["success"] is False and resp["error"]["code"] == "away_media_disabled"
    runtime.client.media_open.assert_not_awaited()
    assert away_media._registry(hass).claims == {}


async def test_a_legacy_entry_that_stored_away_media_false_still_opens_call_audio(hass, hass_ws_client, hass_admin_user):
    """The option is retired (always on): a stored False is ignored, the command is not answered away_media_disabled."""
    _entry, runtime = _device(hass, hass_admin_user, legacy_option=False)
    await _setup_signaling(hass)
    client = await hass_ws_client(hass)
    await _answer(client)
    resp = await _open(client)
    assert resp["success"] is True, resp
    runtime.client.media_open.assert_awaited_once()


async def test_a_caller_who_is_not_an_enrolled_phone_is_refused(hass, hass_ws_client, hass_admin_user):
    entry, runtime = _device(hass, hass_admin_user)
    (host,) = [e for e in hass.config_entries.async_entries("portico") if e.unique_id == "phones-host"]
    hass.config_entries.async_update_entry(host, data={"enrolled_phones": []})      # the phone is un-enrolled
    await _setup_signaling(hass)
    client = await hass_ws_client(hass)
    await _answer(client)
    resp = await _open(client)
    assert resp["error"]["code"] == "not_enrolled"
    runtime.client.media_open.assert_not_awaited()


async def test_a_call_nobody_claimed_is_refused(hass, hass_ws_client, hass_admin_user):
    _device(hass, hass_admin_user)
    await _setup_signaling(hass)
    client = await hass_ws_client(hass)
    resp = await _open(client)  # still ringing
    assert resp["error"]["code"] == "unknown_call"


async def test_a_claimed_ringing_gate_sip_server_call_may_open_audio(hass, hass_ws_client, hass_admin_user):
    # DEV-518: the hub answers a gate_sip_server call when the audio leg attaches, so the tracker is still ringing
    _entry, runtime = _device(hass, hass_admin_user, driver="gate_sip_server")
    await _setup_signaling(hass)
    client = await hass_ws_client(hass)
    await _answer(client)
    assert runtime.call_state.state == "ringing"
    resp = await _open(client)
    assert resp["success"], resp
    runtime.client.media_open.assert_awaited_once()


async def test_a_ringing_gate_sip_server_call_still_needs_the_claim(hass, hass_ws_client, hass_admin_user):
    _entry, runtime = _device(hass, hass_admin_user, driver="gate_sip_server")
    await _setup_signaling(hass)
    claimer = await hass_ws_client(hass)
    other = await hass_ws_client(hass)
    assert (await _open(other))["error"]["code"] == "unknown_call"       # nobody claimed it
    await _answer(claimer)
    assert (await _open(other, 3))["error"]["code"] == "unknown_call"    # claimed by someone else
    runtime.client.media_open.assert_not_awaited()


async def test_a_claimed_ringing_call_on_another_driver_is_still_unknown(hass, hass_ws_client, hass_admin_user):
    _entry, runtime = _device(hass, hass_admin_user)                      # dahua_sip: answer moves the tracker itself
    await _setup_signaling(hass)
    client = await hass_ws_client(hass)
    await _answer(client)
    runtime.call_state.apply_event({"call_id": "c1", "state": "ringing"})
    runtime.call_state.state = "ringing"
    resp = await _open(client)
    assert resp["error"]["code"] == "unknown_call"


async def test_only_the_connection_that_claimed_the_call_may_open_audio(hass, hass_ws_client, hass_admin_user):
    _entry, runtime = _device(hass, hass_admin_user)
    await _setup_signaling(hass)
    claimer = await hass_ws_client(hass)
    other = await hass_ws_client(hass)
    await _answer(claimer)
    resp = await _open(other)
    assert resp["error"]["code"] == "not_claimer"
    runtime.client.media_open.assert_not_awaited()


async def test_open_returns_the_frame_shape_and_asks_the_transport(hass, hass_ws_client, hass_admin_user):
    _entry, runtime = _device(hass, hass_admin_user)
    await _setup_signaling(hass)
    client = await hass_ws_client(hass)
    await _answer(client)
    resp = await _open(client)
    assert resp["success"], resp
    assert resp["result"] == {"codec": "pcmu", "frame_bytes": 320, "frame_ms": 40}
    runtime.client.media_open.assert_awaited_once_with("c1", "pcmu")


async def test_a_second_open_for_the_same_call_is_refused(hass, hass_ws_client, hass_admin_user):
    _device(hass, hass_admin_user)
    await _setup_signaling(hass)
    client = await hass_ws_client(hass)
    await _answer(client)
    assert (await _open(client, 2))["success"]
    assert (await _open(client, 3))["error"]["code"] == "already_open"


async def test_a_transport_failure_leaves_nothing_open(hass, hass_ws_client, hass_admin_user):
    _entry, runtime = _device(hass, hass_admin_user)
    runtime.client.media_open = AsyncMock(side_effect=TransportError("bad_params", "nope"))
    await _setup_signaling(hass)
    client = await hass_ws_client(hass)
    await _answer(client)
    assert (await _open(client, 2))["success"] is False
    assert away_media._registry(hass).channels == {}
    runtime.client.media_open = AsyncMock(return_value={"codec": "pcmu"})
    assert (await _open(client, 3))["success"]


async def test_downlink_frames_arrive_as_events_on_the_open_command(hass, hass_ws_client, hass_admin_user):
    _device(hass, hass_admin_user)
    await _setup_signaling(hass)
    client = await hass_ws_client(hass)
    await _answer(client)
    await _open(client, 2)
    get_provider(hass).on_media_notification("media_frame", {"call_id": "c1", "d": "QUJD"})
    ev = await _recv(client)
    assert ev["type"] == "event" and ev["id"] == 2 and ev["event"] == {"t": "f", "d": "QUJD"}
    # a frame for a call with no channel is ignored
    get_provider(hass).on_media_notification("media_frame", {"call_id": "zzz", "d": "QUJD"})


async def test_uplink_frames_are_forwarded_in_order_and_unanswered(hass, hass_ws_client, hass_admin_user):
    _entry, runtime = _device(hass, hass_admin_user)
    await _setup_signaling(hass)
    client = await hass_ws_client(hass)
    await _answer(client)
    await _open(client, 2)
    for i in range(3):
        await client.send_json({"id": 10 + i, "type": "portico/media_frame", "call_id": "c1", "d": f"F{i}AA"})
    await client.send_json({"id": 20, "type": "portico/media_close", "call_id": "c1"})
    for _ in range(2):  # drain the close's end event and result
        await _recv(client)
    sent = [c.args for c in runtime.client.media_frame_nowait.call_args_list]
    assert sent == [("c1", "F0AA"), ("c1", "F1AA"), ("c1", "F2AA")]


async def test_the_owners_frames_do_not_rescan_the_enrolled_phones(hass, hass_ws_client, hass_admin_user,
                                                                   monkeypatch):
    """R3-04: 25 frames a second per call. The owner passed the table at media_open; a frame from anyone
    else still meets it."""
    _entry, runtime = _device(hass, hass_admin_user)
    await _setup_signaling(hass)
    owner = await hass_ws_client(hass)
    other = await hass_ws_client(hass)
    await _answer(owner)
    await _open(owner, 2)
    calls: list[str] = []
    real = auth.caller_classes
    monkeypatch.setattr(auth, "caller_classes", lambda *a: calls.append("scan") or real(*a))
    for i in range(3):
        await owner.send_json({"id": 10 + i, "type": "portico/media_frame", "call_id": "c1", "d": f"F{i}AA"})
    await owner.send_json({"id": 20, "type": "portico/media_close", "call_id": "c1"})
    for _ in range(2):
        await _recv(owner)
    assert [c.args for c in runtime.client.media_frame_nowait.call_args_list] == [
        ("c1", "F0AA"), ("c1", "F1AA"), ("c1", "F2AA")]
    assert calls == ["scan"], "only media_close scans; the three owner frames did not"
    await other.send_json({"id": 5, "type": "portico/media_frame", "call_id": "nope", "d": "AAAA"})
    assert (await _recv(other))["error"]["code"] == "unknown_session"
    assert calls == ["scan", "scan"]


async def test_a_frame_from_another_connection_is_dropped(hass, hass_ws_client, hass_admin_user):
    _entry, runtime = _device(hass, hass_admin_user)
    await _setup_signaling(hass)
    owner = await hass_ws_client(hass)
    other = await hass_ws_client(hass)
    await _answer(owner)
    await _open(owner, 2)
    await other.send_json({"id": 5, "type": "portico/media_frame", "call_id": "c1", "d": "AAAA"})
    await other.send_json({"id": 6, "type": "portico/media_close", "call_id": "c1"})
    assert (await _recv(other))["success"]
    runtime.client.media_frame_nowait.assert_not_called()
    assert "c1" in away_media._registry(hass).channels  # another connection's close changed nothing


async def test_a_frame_with_no_channel_is_an_error(hass, hass_ws_client, hass_admin_user):
    _device(hass, hass_admin_user)
    await _setup_signaling(hass)
    client = await hass_ws_client(hass)
    await client.send_json({"id": 5, "type": "portico/media_frame", "call_id": "c1", "d": "AAAA"})
    assert (await _recv(client))["error"]["code"] == "unknown_session"


async def test_the_phone_closing_ends_the_channel_and_tells_the_transport(hass, hass_ws_client, hass_admin_user):
    _entry, runtime = _device(hass, hass_admin_user)
    await _setup_signaling(hass)
    client = await hass_ws_client(hass)
    await _answer(client)
    await _open(client, 2)
    await client.send_json({"id": 3, "type": "portico/media_close", "call_id": "c1"})
    msgs = [await _recv(client), await _recv(client)]
    assert any(m["type"] == "event" and m["event"]["t"] == "end" for m in msgs)
    await hass.async_block_till_done()
    runtime.client.media_close.assert_awaited_once()
    assert away_media._registry(hass).channels == {}


async def test_the_call_ending_closes_the_channel(hass, hass_ws_client, hass_admin_user):
    _entry, runtime = _device(hass, hass_admin_user)
    await _setup_signaling(hass)
    client = await hass_ws_client(hass)
    await _answer(client)
    await _open(client, 2)
    runtime.call_state.apply_event({"call_id": "c1", "state": "ended"})
    ev = await _recv(client)
    assert ev["event"] == {"t": "end", "reason": "call_ended"}
    await hass.async_block_till_done()
    runtime.client.media_close.assert_awaited_once_with("c1", "call_ended")
    assert len(runtime.call_state._transition_listeners) == 1  # only the provider's own: the channel's is gone


async def test_the_leg_closing_in_the_transport_ends_the_channel(hass, hass_ws_client, hass_admin_user):
    _entry, runtime = _device(hass, hass_admin_user)
    await _setup_signaling(hass)
    client = await hass_ws_client(hass)
    await _answer(client)
    await _open(client, 2)
    get_provider(hass).on_media_notification("media_closed", {"call_id": "c1", "reason": "idle"})
    ev = await _recv(client)
    assert ev["event"] == {"t": "end", "reason": "idle"}
    await hass.async_block_till_done()
    runtime.client.media_close.assert_not_awaited()  # it told us; no echo back
    assert away_media._registry(hass).channels == {}


async def test_closing_the_connection_closes_the_channel(hass, hass_ws_client, hass_admin_user):
    _entry, runtime = _device(hass, hass_admin_user)
    await _setup_signaling(hass)
    client = await hass_ws_client(hass)
    await _answer(client)
    await _open(client, 2)
    await client.close()
    await hass.async_block_till_done()
    runtime.client.media_close.assert_awaited_once()
    assert away_media._registry(hass).channels == {}
    assert len(runtime.call_state._transition_listeners) == 1  # only the provider's own: the channel's is gone


async def test_away_media_works_through_a_provider_that_is_not_this_integrations_own(hass, hass_ws_client,
                                                                                  hass_admin_user):
    """PH-3b: away_media.py talks to the door's provider only (Door.away_media, secrets_for, call_state,
    media_*), so a second provider's door serves call audio the same way, with its own callbacks."""
    from .fake_provider import FakeProvider
    from .test_signaling import _registry

    _add_device(hass, enrolled_phones=[{"ha_user_id": hass_admin_user.id, "name": "p"}])   # the user is a phone
    await _setup_signaling(hass)
    fake = FakeProvider("fake")
    fake.add_door("gate-1", away_media=True)
    fake.answer_mode = "relay"
    fake.secrets[("gate-1", hass_admin_user.id)] = {"host": "gate-1"}      # this user is enrolled for the door
    _registry(hass).register(fake)
    fake.ring("gate-1", "f1")
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "portico/answer", "door_id": "gate-1", "call_id": "f1",
                            "client_kind": "browser"})
    assert (await _recv(client))["result"] == {"mode": "relay"}
    await client.send_json({"id": 2, "type": "portico/media_open", "door_id": "gate-1", "call_id": "f1",
                            "codec": "l16"})
    assert (await _recv(client))["success"]
    assert fake.calls_to("media_open") == [(("f1", "l16"), {})]

    fake.media["f1"]["on_frame"]("QUJD")
    ev = await _recv(client)
    assert ev["type"] == "event" and ev["id"] == 2 and ev["event"] == {"t": "f", "d": "QUJD"}
    await client.send_json({"id": 3, "type": "portico/media_frame", "call_id": "f1", "d": "AAAA"})
    await client.send_json({"id": 4, "type": "portico/media_close", "call_id": "f1"})
    msgs = [await _recv(client), await _recv(client)]
    assert any(m["type"] == "event" and m["event"] == {"t": "end", "reason": "closed_by_phone"} for m in msgs)
    await hass.async_block_till_done()
    assert fake.calls_to("media_frame") == [(("f1", "AAAA"), {})]
    assert fake.calls_to("media_close") == [(("f1", "closed_by_phone"), {})]

    # a user the provider has no secrets for is not enrolled; a door that does not publish away_media is refused
    fake.secrets.clear()
    fake.ring("gate-1", "f2")
    await client.send_json({"id": 5, "type": "portico/answer", "door_id": "gate-1", "call_id": "f2",
                            "client_kind": "browser"})
    assert (await _recv(client))["success"]
    await client.send_json({"id": 6, "type": "portico/media_open", "door_id": "gate-1", "call_id": "f2"})
    assert (await _recv(client))["error"]["code"] == "not_enrolled"
    fake.add_door("gate-2", away_media=False)
    await client.send_json({"id": 7, "type": "portico/media_open", "door_id": "gate-2", "call_id": "x"})
    assert (await _recv(client))["error"]["code"] == "away_media_disabled"


async def test_the_transport_client_writes_an_unawaited_frame_line():
    client = TransportClient("/nonexistent")
    client._writer = MagicMock()
    client._writer.transport.get_write_buffer_size.return_value = 0
    client.media_frame_nowait("c1", "QUJD")
    (line,) = client._writer.write.call_args.args
    msg = json.loads(line)
    assert msg["method"] == "media_frame" and msg["params"] == {"call_id": "c1", "d": "QUJD"}
    assert client._pending == {} and line.endswith(b"\n")
    client._writer = None
    with pytest.raises(ConnectionError):
        client.media_frame_nowait("c1", "QUJD")


# ---- contract/ws_media_v2.json, replayed against the real handlers --------------------------------

from pathlib import Path  # noqa: E402

_CONTRACT = json.loads((Path(__file__).resolve().parents[3] / "contract" / "ws_media_v2.json").read_text())


@pytest.mark.parametrize("case", _CONTRACT["cases"], ids=[c["name"] for c in _CONTRACT["cases"]])
async def test_contract_case(case, hass, hass_ws_client, hass_admin_user):
    _entry, runtime = _device(hass, hass_admin_user, enabled=case["flag"])
    await _setup_signaling(hass)
    client = await hass_ws_client(hass)
    for step in case["steps"]:
        if "notify" in step:
            get_provider(hass).on_media_notification(step["notify"]["method"], step["notify"]["params"])
        else:
            await client.send_json(step["send"])
        for expected in step["recv"]:
            assert await _recv(client) == expected
        if not step["recv"]:
            await hass.async_block_till_done()
    # nothing left over
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(client.receive_json(), timeout=0.2)


# ---- away Talk: the phone placed the call (out- id), the talk relay is the owner (plan section 13) ------------------

OUT = "out-aaaa1111"


async def _talk_call(hass, client, user, runtime, *, subscribe=True, state="answered"):
    """The phone has a calls stream open and a Talk call placed: relay placement, tracker outbound slot, call id memory."""
    from ..talk_relay import get_talk_relay

    provider = get_provider(hass)
    if subscribe:
        await client.send_json({"id": 1, "type": "portico/calls/subscribe"})
        assert (await _recv(client))["success"]
        while True:       # the door snapshots
            try:
                await asyncio.wait_for(client.receive_json(), 0.2)
            except asyncio.TimeoutError:
                break
    provider._remember_call(OUT, "127.0.0.1")
    runtime.call_state.apply_outbound_event({"call_id": OUT, "state": state})
    relay = get_talk_relay(hass)
    placement = relay.placing("ep-1", user.id, provider, "127.0.0.1")
    relay.placed(placement, OUT)
    return relay, placement


async def _open_out(client, msg_id=2, **over):
    await client.send_json({"id": msg_id, **OPEN, "call_id": OUT, **over})
    while True:
        frame = await _recv(client)
        if frame.get("type") == "result" and frame.get("id") == msg_id:
            return frame


async def test_the_phone_that_placed_a_talk_call_may_carry_its_audio(hass, hass_ws_client, hass_admin_user):
    _entry, runtime = _device(hass, hass_admin_user, state="idle")
    await _setup_signaling(hass)
    client = await hass_ws_client(hass)
    await _talk_call(hass, client, hass_admin_user, runtime)
    resp = await _open_out(client)       # no portico/answer: nothing was claimed
    assert resp["success"], resp
    runtime.client.media_open.assert_awaited_once_with(OUT, "pcmu")
    get_provider(hass).on_media_notification("media_frame", {"call_id": OUT, "d": "QUJD"})
    ev = await _recv(client)
    assert ev["event"] == {"t": "f", "d": "QUJD"}
    await client.send_json({"id": 11, "type": "portico/media_frame", "call_id": OUT, "d": "F0AA"})
    await client.send_json({"id": 12, "type": "portico/media_close", "call_id": OUT})
    for _ in range(2):
        await _recv(client)
    runtime.client.media_frame_nowait.assert_called_once_with(OUT, "F0AA")
    runtime.client.media_close.assert_awaited_once_with(OUT, "closed_by_phone")


async def test_a_connection_with_no_calls_stream_does_not_own_the_talk_call(hass, hass_ws_client, hass_admin_user):
    _entry, runtime = _device(hass, hass_admin_user, state="idle")
    await _setup_signaling(hass)
    placer = await hass_ws_client(hass)
    other = await hass_ws_client(hass)       # same HA user, never subscribed
    await _talk_call(hass, placer, hass_admin_user, runtime)
    resp = await _open_out(other)
    assert resp["error"]["code"] == "not_claimer"
    runtime.client.media_open.assert_not_awaited()


async def test_another_users_stream_does_not_own_the_talk_call(hass, hass_ws_client, hass_admin_user):
    _entry, runtime = _device(hass, hass_admin_user, state="idle")
    await _setup_signaling(hass)
    client = await hass_ws_client(hass)
    # The placement belongs to someone else's endpoint: this phone's stream is not its audience.
    await _talk_call(hass, client, hass_admin_user, runtime)
    from ..talk_relay import get_talk_relay
    relay = get_talk_relay(hass)
    (placement,) = relay._by_door.values()
    placement.user_id = "someone-else"
    assert (await _open_out(client))["error"]["code"] == "not_claimer"
    runtime.client.media_open.assert_not_awaited()


async def test_a_talk_call_that_is_not_going_is_unknown(hass, hass_ws_client, hass_admin_user):
    _entry, runtime = _device(hass, hass_admin_user, state="idle")
    await _setup_signaling(hass)
    client = await hass_ws_client(hass)
    await _talk_call(hass, client, hass_admin_user, runtime, state="ended")
    assert (await _open_out(client))["error"]["code"] == "unknown_call"
    assert (await _open_out(client, 3, call_id="out-other"))["error"]["code"] == "unknown_call"
    runtime.client.media_open.assert_not_awaited()


async def test_an_inbound_id_cannot_borrow_the_talk_path(hass, hass_ws_client, hass_admin_user):
    """Only the `out-` prefix takes the relay path: an inbound call still needs its claim."""
    _entry, runtime = _device(hass, hass_admin_user)       # c1 ringing, nobody answered
    await _setup_signaling(hass)
    client = await hass_ws_client(hass)
    await _talk_call(hass, client, hass_admin_user, runtime)
    assert (await _open_out(client, 4, call_id="c1"))["error"]["code"] == "unknown_call"


async def test_the_talk_call_ending_closes_the_channel(hass, hass_ws_client, hass_admin_user):
    _entry, runtime = _device(hass, hass_admin_user, state="idle")
    await _setup_signaling(hass)
    client = await hass_ws_client(hass)
    await _talk_call(hass, client, hass_admin_user, runtime)
    assert (await _open_out(client))["success"]
    runtime.call_state.apply_outbound_event({"call_id": OUT, "state": "ended"})
    while True:
        ev = await _recv(client)
        if ev.get("event") == {"t": "end", "reason": "call_ended"}:
            break
    runtime.client.media_close.assert_awaited_once_with(OUT, "call_ended")
    assert away_media._registry(hass).channels == {}


async def test_a_door_without_the_capability_still_refuses_a_talk_call(hass, hass_ws_client, hass_admin_user):
    _entry, runtime = _device(hass, hass_admin_user, enabled=False, state="idle")
    await _setup_signaling(hass)
    client = await hass_ws_client(hass)
    await _talk_call(hass, client, hass_admin_user, runtime)
    assert (await _open_out(client))["error"]["code"] == "away_media_disabled"

