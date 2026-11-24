"""A stand-in for the hub for tests that build a DoorRuntime by hand."""
from __future__ import annotations


class StubHub:
    """The part of the hub a hand-built DoorRuntime touches (a mocked client, no process): `client`, and the
    call-ownership hook the provider calls after a `dial`. The real hub is exercised by test_hub.py."""

    def __init__(self, client=None) -> None:
        self.client = client
        self.noted: list[tuple[str, str]] = []

    def note_call(self, call_id: str, device_id: str) -> None:
        self.noted.append((call_id, device_id))
