"""A scriptable CallEndpointRegistry for tests: no media, no network, records every placement."""
from __future__ import annotations

from ..call_endpoints import Endpoint, PlaceResult, TargetState


class FakeCallEndpointRegistry:
    def __init__(self) -> None:
        self.states: dict[tuple[str, str], TargetState] = {}
        self.listed: list[Endpoint] = []
        self.result = PlaceResult(ok=True, call_id="hso-fake-1", media={"kind": "fake"})
        self.placed: list[dict] = []
        self.placed_extra: list[dict] = []
        self.raise_on_place: Exception | None = None

    def add(self, target_type: str, target_id: str, label: str, *, available: bool = True,
            reason: str | None = None, listed: bool = True) -> None:
        self.states[(target_type, target_id)] = TargetState(label, available, reason)
        if listed:
            self.listed.append(Endpoint(target_type, target_id, label))

    def endpoints(self, *, caller_user_id: str) -> list[Endpoint]:
        return list(self.listed)

    def target_state(self, target_type: str, target_id: str) -> TargetState | None:
        return self.states.get((target_type, target_id))

    async def place(self, *, caller_user_id: str, caller_phone: str, target_type: str, target_id: str,
                    caller_endpoint: str | None = None, acting_user_id: str | None = None) -> PlaceResult:
        self.placed.append({"caller_user_id": caller_user_id, "caller_phone": caller_phone,
                            "target_type": target_type, "target_id": target_id})
        self.placed_extra.append({"caller_endpoint": caller_endpoint, "acting_user_id": acting_user_id})
        if self.raise_on_place is not None:
            raise self.raise_on_place
        return self.result
