"""A provider that also implements the OPTIONAL house-call bridge seam (contract/provider_v1.md section 4b), for the UI
part's tests of the media glue. Plain `FakeProvider` is the version-1 provider WITHOUT it (what the driver part's 0.5.x
wheel is); this adds `house_bridge_supported`, `house_open`, `house_close`, `subscribe_house` and the three `house_media_*`
methods, records every call, and lets a test play the hub: leg states, downlink frames, a leg closing, a failing open.
Like FakeProvider it imports nothing from Portico."""
from __future__ import annotations

import asyncio
from typing import Any, Callable

from .fake_provider import FakeProvider


class HubError(Exception):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class FakeHouseProvider(FakeProvider):
    def __init__(self, domain: str = "bridge", **kw: Any) -> None:
        super().__init__(domain, **kw)
        self.supported = True                      # what house_bridge_supported() answers
        self.supported_raises: BaseException | None = None
        self.opened: list[tuple[str, list[dict]]] = []     # (call_id, legs) of every house_open
        self.closed: list[str] = []                        # call ids of every house_close
        self.uplink: list[tuple[str, str]] = []            # (leg_id, data) of every house_media_frame
        self.media_closed: list[tuple[str, str]] = []      # (leg_id, reason) of every house_media_close
        self.open_error: BaseException | None = None       # house_open raises it
        self.open_gate: asyncio.Event | None = None        # house_open waits for it
        self.legs: dict[str, tuple[Callable[[str], None], Callable[[str], None]]] = {}   # leg_id -> callbacks
        self._house_listeners: list[Callable[[dict], None]] = []
        self._sessions = 0

    # ---- the optional seam -------------------------------------------------------------------------------------

    def house_bridge_supported(self) -> bool:
        if self.supported_raises is not None:
            raise self.supported_raises
        return self.supported

    async def house_open(self, call_id: str, legs: list[dict]) -> dict[str, Any]:
        self.opened.append((call_id, [dict(leg) for leg in legs]))
        if self.open_gate is not None:
            await self.open_gate.wait()
        if self.open_error is not None:
            raise self.open_error
        self._sessions += 1
        session = f"house-{self._sessions:08x}"
        out = []
        for leg in legs:
            desc: dict[str, Any] = {"side": leg["side"], "kind": leg["kind"]}
            if leg["kind"] == "frames":
                codec = leg["codec"]
                desc.update({"state": "attached", "leg_id": f"{session}-{leg['side']}", "codec": codec,
                             "frame_bytes": 1280 if codec == "l16" else 320, "frame_ms": 40})
            else:
                desc["state"] = "pending"
            out.append(desc)
        return {"session": session, "legs": out}

    async def house_close(self, call_id: str) -> dict[str, Any]:
        self.closed.append(call_id)
        return {"ok": True, "closed": True}

    def subscribe_house(self, listener: Callable[[dict], None]) -> Callable[[], None]:
        self._house_listeners.append(listener)

        def _unsub() -> None:
            if listener in self._house_listeners:
                self._house_listeners.remove(listener)

        return _unsub

    def house_media_open(self, leg_id: str, on_frame: Callable[[str], None], on_closed: Callable[[str], None]) -> None:
        self.legs[leg_id] = (on_frame, on_closed)

    def house_media_frame(self, leg_id: str, data: str) -> None:
        self.uplink.append((leg_id, data))

    async def house_media_close(self, leg_id: str, reason: str) -> dict[str, Any]:
        self.media_closed.append((leg_id, reason))
        return {"ok": True}

    # ---- playing the hub ---------------------------------------------------------------------------------------

    def leg_state(self, call_id: str, leg: str, state: str, **extra: Any) -> None:
        """A `house_leg_state` notification as the hub sends it."""
        for listener in list(self._house_listeners):
            listener({"call_id": call_id, "leg": leg, "state": state, **extra})

    def downlink(self, leg_id: str, data: str) -> None:
        self.legs[leg_id][0](data)

    def leg_ended(self, leg_id: str, reason: str = "closed") -> None:
        """`media_closed` for a frame-channel leg."""
        self.legs[leg_id][1](reason)
