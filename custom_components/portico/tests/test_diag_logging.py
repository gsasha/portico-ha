"""DEV-764 diagnostic logging: the tracker logs each transition once and records the ring time; the Talk view logs a
change of (available, reason) with its cause and a steady state logs nothing."""
from __future__ import annotations

import logging
import time

from custom_components.portico.door.call_state import CallStateTracker
from custom_components.portico.signaling import _talk_elements


def test_tracker_logs_transitions_and_ring_time(caplog):
    caplog.set_level(logging.INFO, logger="custom_components.portico.door.call_state")
    tracker = CallStateTracker("gate")
    tracker.apply_event({"call_id": "c1", "state": "ringing"})
    assert tracker.ring_mono is not None and tracker.ring_mono <= time.monotonic()
    tracker.apply_event({"call_id": "c1", "state": "answered"})
    tracker.apply_event({"call_id": "c1", "state": "ended"})
    assert tracker.ring_mono is None
    lines = [r.getMessage() for r in caplog.records]
    assert any("call=c1 idle -> ringing" in m for m in lines)
    assert any("call=c1 ringing -> answered" in m for m in lines)
    assert any("call=c1 answered -> ended" in m for m in lines)


def test_late_event_for_ended_call_is_debug_only(caplog):
    tracker = CallStateTracker("gate")
    tracker.apply_event({"call_id": "c1", "state": "ringing"})
    tracker.apply_event({"call_id": "c1", "state": "ended"})
    caplog.set_level(logging.INFO, logger="custom_components.portico.door.call_state")
    caplog.clear()
    tracker.apply_event({"call_id": "c1", "state": "answered"})
    assert not [r for r in caplog.records if r.name.endswith("call_state") and r.levelno >= logging.INFO]


def test_talk_elements_collects_available_and_reason():
    doc = {"pages": [{"blocks": [{"actions": [{"kind": "talk", "id": "t1", "available": False, "reason": "busy"}]}]}]}
    assert _talk_elements(doc) == ["t1:False/busy"]
