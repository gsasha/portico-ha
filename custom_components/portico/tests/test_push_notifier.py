"""RingNotifier: call transitions -> push messages, caps and deadline-bounded
retries (sender design §2.2, §2.4, §2.5, §8.1).

HA-harness session. The notifier is built directly over a real PushRegistry,
a real FcmClient and AccessTokenSource, and the local fake Google; its clock
and sleep are injected so retries, the 5 s / hourly device caps and the global
circuit run in fake time. Nothing leaves 127.0.0.1.
"""
from __future__ import annotations

import json

import pytest

from custom_components.portico.provider import CallTransition
from custom_components.portico.push import const
from custom_components.portico.push.credentials import parse_service_account
from custom_components.portico.push.fcm import FcmClient, Outcome
from custom_components.portico.push.notifier import RingNotifier
from custom_components.portico.push.oauth import AccessTokenSource
from custom_components.portico.push.registry import PushRegistry

from .driver_part import CallStateTracker
from .fake_google import FakeGoogle, fcm_error, service_account_info

ENTRY = "01JENTRYAAAAAAAAAAAAAAAAAA"
ENTRY_B = "01JENTRYBBBBBBBBBBBBBBBBBB"


class FakeTime:
    def __init__(self) -> None:
        self.mono = 10_000.0
        self.wall = 1_759_700_000.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.mono

    def time(self) -> float:
        return self.wall

    def advance(self, seconds: float) -> None:
        self.mono += seconds
        self.wall += seconds

    async def sleep(self, seconds: float) -> None:
        import asyncio

        self.slept.append(seconds)
        self.advance(seconds)
        await asyncio.sleep(0)


class Events:
    def __init__(self) -> None:
        self.results: list[tuple[str, str, Outcome]] = []
        self.skips: list[str] = []
        self.circuit: list[bool] = []
        self.summaries: list[tuple[str, str, dict]] = []

    def on_result(self, row, kind, call_id, result):
        self.results.append((row["reg_id"], kind, result.outcome))

    def on_skip(self, reason, entry_id, call_id):
        self.skips.append(reason)

    def on_circuit(self, is_open):
        self.circuit.append(is_open)

    def on_summary(self, kind, call_id, counts):
        self.summaries.append((kind, call_id, counts))


def _as_provider_transition(tr):
    """What the provider registry hands the notifier for a tracker's transition."""
    return CallTransition.from_dict({
        "door_id": tr.device_id, "call_id": tr.call_id, "prev_state": tr.prev_state, "state": tr.state,
        "prev_call_id": tr.prev_call_id, "at_wall": tr.at_wall})


@pytest.fixture
async def google(monkeypatch, socket_enabled):
    fake = await FakeGoogle().start()
    monkeypatch.setattr(const, "ENDPOINTS", const.Endpoints(token_uri=fake.token_uri, fcm_base_url=fake.base))
    yield fake
    await fake.close()


@pytest.fixture
async def rig(hass, google):
    clock = FakeTime()
    registry = PushRegistry(hass)
    await registry.async_load()
    account = parse_service_account(json.dumps(service_account_info(google.token_uri)))
    tokens = AccessTokenSource(hass, account)
    client = FcmClient(hass, tokens, account.project_id)
    events = Events()
    state = {"enabled": True, "window": 45}
    notifier = RingNotifier(
        hass, registry, client,
        ring_window_s=lambda: state["window"], enabled=lambda: state["enabled"], events=events,
        monotonic=clock.monotonic, wall=clock.time, sleep=clock.sleep,
        wire_id=lambda door_id: ENTRY if door_id == "d" else door_id.removeprefix("device-"),
    )
    trackers = {}

    def tracker(entry_id=ENTRY):
        if entry_id not in trackers:
            t = CallStateTracker(f"device-{entry_id}", entry_id)
            t.add_transition_listener(lambda tr: notifier.handle_transition(_as_provider_transition(tr)))
            trackers[entry_id] = t
        return trackers[entry_id]

    class Rig:
        pass

    r = Rig()
    r.hass, r.google, r.clock, r.registry, r.notifier, r.events, r.state = (
        hass, google, clock, registry, notifier, events, state)
    r.tracker = tracker
    yield r
    notifier.async_stop()
    tokens.async_stop()


async def _phone(registry, n, *, enabled=True, kind="fid", user=None):
    row = await registry.async_register(
        ha_user_id=user or f"user-{n}", installation_id=f"inst-{n}", kind=kind,
        registration=f"REG-VALUE-{n:03d}-0123456789abcdef", package=const.PORTICO_PACKAGE,
        fcm_project_id="portico-test", app_version="0.1.0", name=f"phone {n}",
    )
    if enabled:
        await registry.async_set_enabled(row["reg_id"], True)
    return row


def _kinds(google, *, reg=None):
    out = []
    for m in google.sent_messages():
        value = m.get("fid") or m.get("token")
        if reg is None or value == reg:
            out.append((m["data"]["kind"], m["data"]["call_id"]))
    return out


async def _settle(rig):
    await rig.notifier.async_drain()


# --------------------------------------------------------------------------
# Trigger (§2.2)
# --------------------------------------------------------------------------


async def test_ring_goes_only_to_enabled_registrations(rig):
    on = await _phone(rig.registry, 1)
    await _phone(rig.registry, 2, enabled=False)  # decision 4: default OFF
    rig.tracker().apply_event({"call_id": "c1", "state": "ringing"})
    await _settle(rig)
    assert _kinds(rig.google) == [("ring", "c1")]
    assert rig.google.sent_messages()[0]["fid"] == on["registration"]
    assert rig.google.sent_messages()[0]["data"]["door_ref"] == ENTRY


async def test_the_notifier_sends_push_v2_and_neither_ct_nor_from_ref_until_m7(rig):
    """push/v2 reserves `ct` (door|house) and `from_ref` for the house calls (M7, HC-8). The notifier knows only door
    calls: every message it sends is `pv: "2"`, v1's keys and no more, whatever a door's driver or id looks like."""
    await _phone(rig.registry, 1)
    rig.tracker().apply_event({"call_id": "c1", "state": "ringing"})
    await _settle(rig)
    rig.tracker().apply_event({"call_id": "c1", "state": "answered"})
    await _settle(rig)
    rig.tracker().apply_event({"call_id": "c1", "state": "ended"})
    await _settle(rig)
    messages = [m["data"] for m in rig.google.sent_messages()]
    assert [m["kind"] for m in messages] == ["ring", "answered"]     # ended after answered is not sent to a phone that heard it
    for data in messages:
        assert data["pv"] == "2" and "ct" not in data and "from_ref" not in data
        assert set(data) <= {"pv", "kind", "door_ref", "call_id", "seq", "t", "exp", "by"}


async def test_a_new_registration_is_off_by_default(rig):
    row = await _phone(rig.registry, 1, enabled=False)
    assert row["push_enabled"] is False
    rig.tracker().apply_event({"call_id": "c1", "state": "ringing"})
    await _settle(rig)
    assert rig.google.fcm_requests == []


async def test_global_switch_off_sends_nothing(rig):
    await _phone(rig.registry, 1)
    rig.state["enabled"] = False
    rig.tracker().apply_event({"call_id": "c1", "state": "ringing"})
    rig.tracker().apply_event({"call_id": "c1", "state": "ended"})
    await _settle(rig)
    assert rig.google.fcm_requests == []


async def test_answered_then_ended(rig):
    await _phone(rig.registry, 1)
    t = rig.tracker()
    t.apply_event({"call_id": "c1", "state": "ringing"})
    await _settle(rig)
    t.apply_event({"call_id": "c1", "state": "answered"})
    t.apply_event({"call_id": "c1", "state": "active"})
    t.apply_event({"call_id": "c1", "state": "ended"})
    await _settle(rig)
    # `ended` after an accepted `answered` is not sent: the app ignores it
    # (contract case "ended after answered") and a HIGH message with nothing
    # visible counts against the phone (§4.2).
    assert _kinds(rig.google) == [("ring", "c1"), ("answered", "c1")]
    answered = rig.google.sent_messages()[1]
    assert answered["data"]["by"] == "other"
    assert answered["android"]["ttl"] == "60s"


async def test_ended_without_answer_dismisses_as_missed(rig):
    await _phone(rig.registry, 1)
    t = rig.tracker()
    t.apply_event({"call_id": "c1", "state": "ringing"})
    t.apply_event({"call_id": "c1", "state": "ended"})
    await _settle(rig)
    assert _kinds(rig.google) == [("ring", "c1"), ("ended", "c1")]


async def test_dismiss_goes_only_to_phones_that_got_the_ring(rig):
    a = await _phone(rig.registry, 1)
    b = await _phone(rig.registry, 2)
    late = await _phone(rig.registry, 3, enabled=False)
    rig.google.fcm_default = lambda record: (
        fcm_error(404, "NOT_FOUND", error_code="UNREGISTERED")
        if record["json"]["message"]["fid"] == b["registration"] else (200, {"name": "x"}, {}))
    t = rig.tracker()
    t.apply_event({"call_id": "c1", "state": "ringing"})
    await _settle(rig)
    await rig.registry.async_set_enabled(late["reg_id"], True)  # enabled after the ring
    t.apply_event({"call_id": "c1", "state": "answered"})
    await _settle(rig)
    assert _kinds(rig.google, reg=a["registration"]) == [("ring", "c1"), ("answered", "c1")]
    assert _kinds(rig.google, reg=b["registration"]) == [("ring", "c1")]
    assert _kinds(rig.google, reg=late["registration"]) == []


async def test_released_claim_answered_to_ringing_sends_nothing(rig):
    await _phone(rig.registry, 1)
    t = rig.tracker()
    t.apply_event({"call_id": "c1", "state": "ringing"})
    await _settle(rig)
    t.apply_event({"call_id": "c1", "state": "answered"})
    await _settle(rig)
    t.apply_event({"call_id": "c1", "state": "ringing"})
    t.apply_event({"call_id": "c1", "state": "answered"})
    await _settle(rig)
    assert _kinds(rig.google) == [("ring", "c1"), ("answered", "c1")]


async def test_duplicate_and_late_events_are_ignored(rig):
    await _phone(rig.registry, 1)
    t = rig.tracker()
    t.apply_event({"call_id": "c1", "state": "ringing"})
    t.apply_event({"call_id": "c1", "state": "ringing"})
    t.apply_event({"call_id": "c1", "state": "ended"})
    # Late: the tracker drops these; and a straight notifier call is ignored too.
    t.apply_event({"call_id": "c1", "state": "answered"})
    t.apply_event({"call_id": "c1", "state": "ringing"})
    await _settle(rig)
    rig.notifier.handle_transition(CallTransition("d", "c1", "idle", "ringing", None, 0.0))
    await _settle(rig)
    assert _kinds(rig.google) == [("ring", "c1"), ("ended", "c1")]


async def test_two_quick_calls(rig):
    """A second call replacing the first with no `ended` between them: the
    first one's phones are dismissed, then the second rings (after the 5 s
    device spacing)."""
    await _phone(rig.registry, 1)
    t = rig.tracker()
    t.apply_event({"call_id": "c1", "state": "ringing"})
    await _settle(rig)
    rig.clock.advance(6)
    t.apply_event({"call_id": "c2", "state": "ringing"})
    await _settle(rig)
    t.apply_event({"call_id": "c2", "state": "ended"})
    await _settle(rig)
    sent = _kinds(rig.google)
    assert sent[0] == ("ring", "c1")
    assert sorted(sent[1:3]) == [("ended", "c1"), ("ring", "c2")]  # two calls, in parallel
    assert sent[3:] == [("ended", "c2")]


async def test_transport_death_ends_the_call_and_dismisses(rig):
    """Python-side ends (the device went unavailable, the transport died) pass
    through apply_event and so reach the sender as `ended` (§2.2)."""
    await _phone(rig.registry, 1)
    t = rig.tracker()
    t.apply_event({"call_id": "c1", "state": "ringing"})
    await _settle(rig)
    t.apply_event({"call_id": t.call_id, "state": "ended"})  # what _end_tracked_call does
    await _settle(rig)
    assert _kinds(rig.google) == [("ring", "c1"), ("ended", "c1")]


async def test_ring_message_options(rig):
    await _phone(rig.registry, 1, kind="token")
    rig.tracker().apply_event({"call_id": "c1", "state": "ringing"})
    await _settle(rig)
    m = rig.google.sent_messages()[0]
    assert "token" in m and "fid" not in m
    assert m["android"] == {"priority": "HIGH", "ttl": "45s",
                            "restricted_package_name": const.PORTICO_PACKAGE, "direct_boot_ok": False}
    assert int(m["data"]["exp"]) - int(m["data"]["t"]) == 45_000


# --------------------------------------------------------------------------
# Failures (§2.4)
# --------------------------------------------------------------------------


async def test_5xx_is_retried_until_the_ring_deadline_then_dropped(rig):
    await _phone(rig.registry, 1)
    rig.google.fcm_default = lambda record: fcm_error(503, "UNAVAILABLE", error_code="UNAVAILABLE")
    rig.tracker().apply_event({"call_id": "c1", "state": "ringing"})
    await _settle(rig)
    # deadline = 45 - 5 = 40 s: 0.5, 1, 2, 4, 4, ... never past 40 s.
    assert rig.clock.slept[:4] == [0.5, 1, 2, 4]
    assert sum(rig.clock.slept) <= 40
    assert 5 <= len(rig.google.fcm_requests) <= 14
    assert rig.events.results[-1][2] is Outcome.TRANSIENT


async def test_a_transient_failure_then_success(rig):
    await _phone(rig.registry, 1)
    rig.google.fcm_script.extend([fcm_error(500, "INTERNAL"), fcm_error(503, "UNAVAILABLE")])
    rig.tracker().apply_event({"call_id": "c1", "state": "ringing"})
    await _settle(rig)
    assert len(rig.google.fcm_requests) == 3
    assert rig.clock.slept == [0.5, 1]
    row = rig.registry.rows()[0]
    assert row["state"] == "ok" and row["last_result"] == "ok"


async def test_retry_after_beyond_the_deadline_drops_the_ring(rig):
    await _phone(rig.registry, 1)
    rig.google.fcm_script.append(fcm_error(503, "UNAVAILABLE", headers={"Retry-After": "120"}))
    rig.tracker().apply_event({"call_id": "c1", "state": "ringing"})
    await _settle(rig)
    assert len(rig.google.fcm_requests) == 1
    assert rig.clock.slept == []


async def test_retry_after_inside_the_deadline_is_honoured(rig):
    await _phone(rig.registry, 1)
    rig.google.fcm_script.append(fcm_error(503, "UNAVAILABLE", headers={"Retry-After": "7"}))
    rig.tracker().apply_event({"call_id": "c1", "state": "ringing"})
    await _settle(rig)
    assert rig.clock.slept == [7.0]
    assert len(rig.google.fcm_requests) == 2


async def test_quota_without_retry_after_drops_the_ring(rig):
    """Google's 429 advice is a minimum 1-minute delay: past any ring (§2.4)."""
    await _phone(rig.registry, 1)
    rig.google.fcm_script.append(fcm_error(429, "RESOURCE_EXHAUSTED", error_code="QUOTA_EXCEEDED"))
    rig.tracker().apply_event({"call_id": "c1", "state": "ringing"})
    await _settle(rig)
    assert len(rig.google.fcm_requests) == 1
    assert rig.events.results == [(rig.registry.rows()[0]["reg_id"], "ring", Outcome.QUOTA)]


@pytest.mark.parametrize("response", [
    fcm_error(404, "NOT_FOUND", error_code="UNREGISTERED"),
    fcm_error(404, "NOT_FOUND"),
    fcm_error(400, "INVALID_ARGUMENT", field="message.fid"),
])
async def test_a_dead_registration_is_pruned_and_never_retried(rig, response):
    row = await _phone(rig.registry, 1)
    rig.google.fcm_script.append(response)
    t = rig.tracker()
    t.apply_event({"call_id": "c1", "state": "ringing"})
    t.apply_event({"call_id": "c1", "state": "ended"})
    await _settle(rig)
    assert len(rig.google.fcm_requests) == 1
    kept = rig.registry.get(row["reg_id"])
    assert kept["state"] == "unregistered"
    assert kept["registration"] is None  # cleared, row kept for the UI
    rig.clock.advance(10)
    t.apply_event({"call_id": "c2", "state": "ringing"})
    await _settle(rig)
    assert len(rig.google.fcm_requests) == 1


async def test_payload_rejection_is_not_a_dead_registration(rig):
    row = await _phone(rig.registry, 1)
    rig.google.fcm_script.append(fcm_error(400, "INVALID_ARGUMENT", field="message.data"))
    rig.tracker().apply_event({"call_id": "c1", "state": "ringing"})
    await _settle(rig)
    kept = rig.registry.get(row["reg_id"])
    assert kept["registration"] == row["registration"]
    assert kept["last_result"] == "payload_rejected"


async def test_sender_id_mismatch_marks_the_row(rig):
    row = await _phone(rig.registry, 1)
    rig.google.fcm_script.append(fcm_error(403, "PERMISSION_DENIED", error_code="SENDER_ID_MISMATCH"))
    rig.tracker().apply_event({"call_id": "c1", "state": "ringing"})
    await _settle(rig)
    assert rig.registry.get(row["reg_id"])["state"] == "project_mismatch"
    rig.clock.advance(10)
    rig.tracker().apply_event({"call_id": "c2", "state": "ringing"})
    await _settle(rig)
    assert len(rig.google.fcm_requests) == 1  # not a target until it re-registers


async def test_google_down_fails_inside_the_deadline(rig):
    await _phone(rig.registry, 1)
    await rig.google.close()
    rig.tracker().apply_event({"call_id": "c1", "state": "ringing"})
    await _settle(rig)
    assert sum(rig.clock.slept) <= 40
    assert rig.events.results and all(r[2] is Outcome.TRANSIENT for r in rig.events.results)


# --------------------------------------------------------------------------
# Caps (§2.5)
# --------------------------------------------------------------------------


async def test_device_spacing_5s(rig):
    await _phone(rig.registry, 1)
    t = rig.tracker()
    for n in range(3):
        t.apply_event({"call_id": f"c{n}", "state": "ringing"})
        rig.clock.advance(2)
    await _settle(rig)
    assert [k for k, _ in _kinds(rig.google)].count("ring") == 1
    assert rig.events.skips.count("device_rate") == 2


async def test_a_stuck_loop_of_1000_rings_is_bounded_per_hour(rig):
    """§8.1: 1,000 rings yield at most 20 ring pushes per hour per device."""
    phone = await _phone(rig.registry, 1)
    t = rig.tracker()
    for n in range(1000):
        t.apply_event({"call_id": f"loop-{n}", "state": "ringing"})
        t.apply_event({"call_id": f"loop-{n}", "state": "ended"})
        rig.clock.advance(1)  # 1,000 s: well inside one hour
    await _settle(rig)
    rings = [k for k, _ in _kinds(rig.google, reg=phone["registration"]) if k == "ring"]
    assert len(rings) <= 20
    assert len(rig.google.fcm_requests) <= 40  # each ring plus at most its ended


async def test_the_hourly_device_cap_recovers(rig):
    await _phone(rig.registry, 1)
    t = rig.tracker()
    for n in range(25):
        t.apply_event({"call_id": f"c{n}", "state": "ringing"})
        rig.clock.advance(6)
    await _settle(rig)
    assert [k for k, _ in _kinds(rig.google)].count("ring") == 20
    rig.clock.advance(3600)
    t.apply_event({"call_id": "later", "state": "ringing"})
    await _settle(rig)
    assert _kinds(rig.google)[-1] == ("ring", "later")


async def test_device_caps_are_per_device_entry(rig):
    await _phone(rig.registry, 1)
    rig.tracker(ENTRY).apply_event({"call_id": "a1", "state": "ringing"})
    rig.tracker(ENTRY_B).apply_event({"call_id": "b1", "state": "ringing"})
    await _settle(rig)
    assert sorted(_kinds(rig.google)) == [("ring", "a1"), ("ring", "b1")]


async def test_global_circuit_opens_at_60_per_minute_and_closes(rig):
    for n in range(70):
        await _phone(rig.registry, n)
    rig.tracker().apply_event({"call_id": "c1", "state": "ringing"})
    await _settle(rig)
    assert len(rig.google.fcm_requests) == 60
    assert rig.events.circuit == [True]
    assert rig.events.skips.count("rate_limited") == 10

    rig.clock.advance(120)  # still open (5 min)
    rig.tracker().apply_event({"call_id": "c2", "state": "ringing"})
    await _settle(rig)
    assert len(rig.google.fcm_requests) == 60

    rig.clock.advance(300)
    for row in rig.registry.rows()[1:]:
        await rig.registry.async_set_enabled(row["reg_id"], False)
    rig.tracker().apply_event({"call_id": "c3", "state": "ringing"})
    await _settle(rig)
    assert len(rig.google.fcm_requests) == 61
    assert rig.events.circuit == [True, False]


async def test_per_call_per_registration_at_most_one_of_each(rig):
    phone = await _phone(rig.registry, 1)
    t = rig.tracker()
    t.apply_event({"call_id": "c1", "state": "ringing"})
    for _ in range(5):
        t.apply_event({"call_id": "c1", "state": "ringing"})
        t.apply_event({"call_id": "c1", "state": "answered"})
        t.apply_event({"call_id": "c1", "state": "ringing"})
    t.apply_event({"call_id": "c1", "state": "ended"})
    await _settle(rig)
    assert _kinds(rig.google, reg=phone["registration"]) == [("ring", "c1"), ("answered", "c1")]


async def test_a_notifier_bug_never_breaks_the_live_ring_path(rig, monkeypatch):
    await _phone(rig.registry, 1)

    def _boom(*_a, **_k):
        raise RuntimeError("sender bug")

    monkeypatch.setattr(rig.notifier, "_start_ring", _boom)
    seen = []
    t = rig.tracker()
    t.add_listener(lambda: seen.append(t.state))
    t.apply_event({"call_id": "c1", "state": "ringing"})
    assert (t.state, seen) == ("ringing", ["ringing"])
