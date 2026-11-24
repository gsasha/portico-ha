"""Tests for call_state.py's CallStateTracker (§4.5): wire states are
exactly ringing|answered|active|ended, `ended` collapses immediately to
the HA-side-only `idle` with no dwell timer, and listeners fire on every
real state change."""
from __future__ import annotations

from custom_components.portico.door.call_state import CallStateTracker


def test_initial_state_is_idle():
    tracker = CallStateTracker("device-1")
    assert tracker.state == "idle"
    assert tracker.call_id is None


def test_ringing_then_answered_then_active():
    tracker = CallStateTracker("device-1")
    tracker.apply_event({"call_id": "c1", "state": "ringing"})
    assert tracker.state == "ringing"
    assert tracker.call_id == "c1"

    tracker.apply_event({"call_id": "c1", "state": "answered"})
    assert tracker.state == "answered"

    tracker.apply_event({"call_id": "c1", "state": "active"})
    assert tracker.state == "active"


def test_ended_collapses_immediately_to_idle_no_dwell():
    tracker = CallStateTracker("device-1")
    tracker.apply_event({"call_id": "c1", "state": "active"})
    tracker.apply_event({"call_id": "c1", "state": "ended"})
    assert tracker.state == "idle"
    assert tracker.call_id is None


def test_unrecognized_state_is_ignored_not_corrupting():
    tracker = CallStateTracker("device-1")
    tracker.apply_event({"call_id": "c1", "state": "ringing"})
    tracker.apply_event({"call_id": "c1", "state": "some_future_value_this_code_predates"})
    # Still the last known-good state, not silently overwritten with
    # something nothing else in the codebase expects.
    assert tracker.state == "ringing"
    assert tracker.call_id == "c1"


def test_listener_fires_on_every_real_state_change():
    tracker = CallStateTracker("device-1")
    calls = []
    tracker.add_listener(lambda: calls.append(tracker.state))

    tracker.apply_event({"call_id": "c1", "state": "ringing"})
    tracker.apply_event({"call_id": "c1", "state": "answered"})
    tracker.apply_event({"call_id": "c1", "state": "ended"})

    assert calls == ["ringing", "answered", "idle"]


def test_listener_does_not_fire_on_an_ignored_event():
    tracker = CallStateTracker("device-1")
    calls = []
    tracker.add_listener(lambda: calls.append(tracker.state))

    tracker.apply_event({"call_id": "c1", "state": "not_a_real_state"})
    assert calls == []


def test_a_late_event_for_an_ended_call_is_ignored():
    """`ended` is terminal (§4.5). After Python ends a call itself (the
    driver went unavailable, the transport died), the Go side may still
    send that call's own answered/ended once it recovers: those must change
    nothing and raise nothing."""
    tracker = CallStateTracker("device-1")
    calls = []
    tracker.add_listener(lambda: calls.append(tracker.state))

    tracker.apply_event({"call_id": "c1", "state": "ringing"})
    tracker.apply_event({"call_id": "c1", "state": "ended"})
    tracker.apply_event({"call_id": "c1", "state": "answered"})
    tracker.apply_event({"call_id": "c1", "state": "ended"})
    assert tracker.state == "idle"
    assert tracker.call_id is None
    assert calls == ["ringing", "idle"]

    # A new call is unaffected.
    tracker.apply_event({"call_id": "c2", "state": "ringing"})
    assert (tracker.state, tracker.call_id) == ("ringing", "c2")
    # ...and a straggler for the old one does not end it.
    tracker.apply_event({"call_id": "c1", "state": "ended"})
    assert (tracker.state, tracker.call_id) == ("ringing", "c2")


def test_the_ended_call_memory_is_bounded():
    tracker = CallStateTracker("device-1")
    for i in range(1000):
        tracker.apply_event({"call_id": f"c{i}", "state": "ringing"})
        tracker.apply_event({"call_id": f"c{i}", "state": "ended"})
    assert len(tracker._ended_call_ids) <= 64


# --------------------------------------------------------------------------
# Transition listeners (FCM sender design §2.2): the push sender needs the
# transition itself, because on `ended` the tracker clears call_id before its
# argument-less listeners run, so those cannot tell WHICH call ended.
# --------------------------------------------------------------------------

def test_transition_listener_receives_the_ended_call_id():
    tracker = CallStateTracker("device-1", entry_id="entry-1")
    seen = []
    tracker.add_transition_listener(seen.append)

    tracker.apply_event({"call_id": "c1", "state": "ringing"})
    tracker.apply_event({"call_id": "c1", "state": "answered"})
    tracker.apply_event({"call_id": "c1", "state": "ended"})

    assert [(t.call_id, t.prev_state, t.state) for t in seen] == [
        ("c1", "idle", "ringing"),
        ("c1", "ringing", "answered"),
        ("c1", "answered", "ended"),
    ]
    assert {t.device_id for t in seen} == {"device-1"}
    assert {t.entry_id for t in seen} == {"entry-1"}
    assert all(isinstance(t.at_monotonic, float) and t.at_wall > 0 for t in seen)


def test_transition_fires_before_call_id_is_cleared():
    """§2.2, literally: the transition listener runs while the tracker still
    holds the ending call."""
    tracker = CallStateTracker("device-1")
    during = []
    tracker.add_transition_listener(lambda t: during.append((tracker.call_id, t.call_id)))
    tracker.apply_event({"call_id": "c1", "state": "ringing"})
    tracker.apply_event({"call_id": "c1", "state": "ended"})
    assert during[-1] == ("c1", "c1")
    assert tracker.call_id is None


def test_an_ended_with_no_call_id_reports_the_tracked_call():
    """Python-side ends (driver unavailable, transport died) pass the tracked
    id; one without any id still names the call that was being followed."""
    tracker = CallStateTracker("device-1")
    seen = []
    tracker.add_transition_listener(seen.append)
    tracker.apply_event({"call_id": "c1", "state": "ringing"})
    tracker.apply_event({"state": "ended"})
    assert (seen[-1].call_id, seen[-1].state) == ("c1", "ended")


def test_a_dropped_late_event_produces_no_transition():
    tracker = CallStateTracker("device-1")
    seen = []
    tracker.add_transition_listener(seen.append)
    tracker.apply_event({"call_id": "c1", "state": "ringing"})
    tracker.apply_event({"call_id": "c1", "state": "ended"})
    tracker.apply_event({"call_id": "c1", "state": "answered"})
    tracker.apply_event({"call_id": "c1", "state": "ended"})
    tracker.apply_event({"call_id": "c1", "state": "bogus"})
    assert [t.state for t in seen] == ["ringing", "ended"]


def test_parameterless_listeners_are_unchanged_and_removable():
    tracker = CallStateTracker("device-1")
    plain, transitions = [], []
    plain_cb = lambda: plain.append(tracker.state)  # noqa: E731
    tracker.add_listener(plain_cb)
    tracker.add_transition_listener(transitions.append)
    tracker.apply_event({"call_id": "c1", "state": "ringing"})
    tracker.remove_listener(plain_cb)
    tracker.remove_transition_listener(transitions.append)
    tracker.apply_event({"call_id": "c1", "state": "ended"})
    assert plain == ["ringing"]
    assert len(transitions) == 1


def test_a_raising_transition_listener_does_not_break_the_tracker():
    """The push sender must never be able to stop the live ring path
    (§8.1, "the live path is unchanged")."""
    tracker = CallStateTracker("device-1")
    plain = []
    tracker.add_listener(lambda: plain.append(tracker.state))

    def _boom(_t):
        raise RuntimeError("sender bug")

    tracker.add_transition_listener(_boom)
    tracker.apply_event({"call_id": "c1", "state": "ringing"})
    assert (tracker.state, tracker.call_id) == ("ringing", "c1")
    assert plain == ["ringing"]


def test_a_new_ring_replacing_the_tracked_call_names_both_calls():
    """Two quick calls with no `ended` between them: the transition is about
    the new call and names the one it replaced, so the sender can dismiss it."""
    tracker = CallStateTracker("device-1")
    seen = []
    tracker.add_transition_listener(seen.append)
    tracker.apply_event({"call_id": "c1", "state": "ringing"})
    tracker.apply_event({"call_id": "c2", "state": "ringing"})
    assert (seen[-1].call_id, seen[-1].prev_call_id, seen[-1].prev_state) == ("c2", "c1", "ringing")


def test_end_of_a_superseded_call_leaves_the_live_call_alone():
    """A new ring replaces the old one in the tracker; the driver's later `ended` for the OLD id must not end the new
    call, and the old id stays ended."""
    tracker = CallStateTracker("device-1")
    tracker.apply_event({"call_id": "old", "state": "ringing"})
    tracker.apply_event({"call_id": "new", "state": "ringing"})
    tracker.apply_event({"call_id": "old", "state": "ended"})
    assert (tracker.call_id, tracker.state) == ("new", "ringing")
    tracker.apply_event({"call_id": "old", "state": "answered"})     # still dropped: terminal
    assert (tracker.call_id, tracker.state) == ("new", "ringing")
    tracker.apply_event({"call_id": "new", "state": "ended"})
    assert (tracker.call_id, tracker.state) == (None, "idle")


def test_outbound_transitions_log_one_warning_each(caplog):
    # DEV-806: production runs the `portico` logger at WARNING, so the outbound lines must be written at that level
    import logging
    t = CallStateTracker("gate-1")
    with caplog.at_level(logging.WARNING, logger="custom_components.portico.door.call_state"):
        t.apply_outbound_event({"state": "ringing", "call_id": "o1"})
        t.apply_outbound_event({"state": "ended", "call_id": "o1"})
    lines = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(lines) == 2
    assert "outbound call=o1 idle -> dialing" in lines[0] and "outbound call=o1 dialing -> ended" in lines[1]
