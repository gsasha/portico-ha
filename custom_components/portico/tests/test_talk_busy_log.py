"""DEV-806: the Talk reason going to or from `busy` is logged at WARNING (production's level), once per edge, with its cause."""
from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import patch

from custom_components.portico.content import ContentStore


def _store():
    s = object.__new__(ContentStore)
    s._talk_last, s._busy_since, s._talk_cause = {}, {}, None
    s._talk_keyed = lambda endpoint: endpoint
    return s


def _view(s, reason, cause=None):
    def raw(*a, **k):
        s._talk_cause = cause
        return {"available": reason is None, "reason": reason}
    with patch.object(s, "_talk_view_raw", raw):
        return s._talk_view("gate", SimpleNamespace(id="alice"))


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


def test_busy_edges_log_once_each_with_cause_and_duration(caplog):
    s = _store()
    with caplog.at_level(logging.WARNING, logger="custom_components.portico.content"):
        _view(s, None)
        assert _warnings(caplog) == []                       # ready: nothing
        _view(s, "busy", "busy because state=ringing dialing=False")
        _view(s, "busy", "busy because state=ringing dialing=False")    # steady: nothing more
        lines = _warnings(caplog)
        assert len(lines) == 1 and "door=gate" in lines[0] and "reason=busy began" in lines[0] and "state=ringing" in lines[0]
        _view(s, None)
        _view(s, None)
    lines = _warnings(caplog)
    assert len(lines) == 2 and "reason=busy cleared after" in lines[1] and "door=gate" in lines[1]


def test_other_reason_changes_are_not_warnings(caplog):
    s = _store()
    with caplog.at_level(logging.WARNING, logger="custom_components.portico.content"):
        _view(s, None)
        _view(s, "not_connected")
        _view(s, "connecting")
    assert _warnings(caplog) == []
