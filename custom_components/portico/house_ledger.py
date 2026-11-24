"""The ClaimLedger: who is busy with what, for house calls (house calls design 7, 15.3, 15.13; M7 PR 3a).

Pure and synchronous: no Home Assistant import, no I/O, no clock. Every method decides and records in one step with no
`await` inside, so on the event loop two callers racing for the same slot are decided strictly one after the other (the
second sees the first's claim). The registry (house.py) and the `portico/answer` handler call it; nothing else does.

What it holds, and the four rules it enforces:

* **Door side** (keyed by door call id): the door calls that are ringing, the claims a user holds on a door call
  (granted) and the claims being asked for (reserved, while the provider is still deciding). Rule 1, the per-user cap
  (15.13): one user holds at most `DOOR_CAP_PER_USER` (2) door calls at once, one live and one held. The ledger is told
  nothing of hold: a claim is a claim. A third answer by the same user is refused with `too_many_calls`. Keyed by user
  id, not by connection, because a reconnect changes the connection.
* **House side** (keyed by house call id and endpoint id): which house call each endpoint is in. Rule 2 (HC-5): an endpoint
  is in at most one house call. Rule 3: a door outranks a house call on every endpoint (`endpoint_state`). Rule 4: at
  most `HOUSE_CAP` (4) house calls exist at once, the resource guard for the bridge of 3b.

A house call id (`hso-...`) never enters the door side and a door call id never enters the house side (the registry
checks the prefix); the ledger is the only thing the two worlds share, and it shares only counts and states.
"""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

DOOR_CAP_PER_USER = 2     # a phone may hold two door calls, one live and one held (15.13)
HOUSE_CAP = 4             # house calls at once (HC-5): the resource guard for the bridges of 3b
_ENDED_MEMORY = 64

CODE_TOO_MANY_CALLS = "too_many_calls"

# Per-endpoint busy states (design 7).
IDLE = "idle"
DOOR_RINGING = "door_ringing"
DOOR_ACTIVE = "door_active"
HOUSE_RINGING = "house_ringing"
HOUSE_ACTIVE = "house_active"
BUSY_STATES = (IDLE, DOOR_RINGING, DOOR_ACTIVE, HOUSE_RINGING, HOUSE_ACTIVE)


@dataclass(eq=False, slots=True)
class Reservation:
    """A claim being asked for. Identity, not value: releasing one reservation never drops another's."""

    user_id: str
    call_id: str


class ClaimLedger:
    def __init__(self, *, door_cap: int = DOOR_CAP_PER_USER, house_cap: int = HOUSE_CAP) -> None:
        self.door_cap = door_cap
        self.house_cap = house_cap
        self._rings: dict[str, str | None] = {}            # door call id -> door id, for a door call that is ringing
        self._ended: OrderedDict[str, None] = OrderedDict()   # the last door calls that ended: a late claim is dropped
        self._claims: dict[str, tuple[str, str | None]] = {}   # door call id -> (user id, door id), granted
        self._pending: list[Reservation] = []
        self._house: dict[str, str] = {}                   # house call id -> state ("ringing" | "answered" | "active")
        self._members: dict[str, set[str]] = {}            # house call id -> endpoint ids in it
        self._by_endpoint: dict[str, str] = {}             # endpoint id -> house call id

    # ---- door side ---------------------------------------------------------------------------------------------

    def door_ringing(self, call_id: str, door_id: str | None = None) -> None:
        self._rings[call_id] = door_id

    def door_resolved(self, call_id: str) -> None:
        """The door call is no longer ringing (someone answered it, or it ended). A granted claim stays."""
        self._rings.pop(call_id, None)

    def door_ended(self, call_id: str) -> list[str]:
        """The door call is over: its ring, its claim and any reservation go. Returns the user ids whose claim ended."""
        self._rings.pop(call_id, None)
        self._pending = [r for r in self._pending if r.call_id != call_id]
        self._ended[call_id] = None
        while len(self._ended) > _ENDED_MEMORY:
            self._ended.popitem(last=False)
        claim = self._claims.pop(call_id, None)
        return [claim[0]] if claim else []

    def release_claims(self, call_id: str) -> None:
        """The claim on a door call was released (the call went back to ringing): the claim goes, the call lives."""
        self._claims.pop(call_id, None)

    def door_gone(self, door_id: str) -> None:
        """A door left (its entry unloaded): every ring and claim of its calls goes with it."""
        for call_id in [c for c, d in self._rings.items() if d == door_id]:
            self.door_ended(call_id)
        for call_id in [c for c, (_u, d) in self._claims.items() if d == door_id]:
            self.door_ended(call_id)

    def door_rings(self) -> list[str]:
        return list(self._rings)

    def reserve_door(self, user_id: str, call_id: str) -> Reservation | str:
        """Ask for a claim on a door call: a `Reservation` (settle it with `confirm_door` or `release_door`), or the error
        code `too_many_calls` when this user already holds `door_cap` OTHER door calls. A user who already holds, or is
        already asking for, this very call is not counted twice (the provider alone arbitrates the answer itself)."""
        if self._holds(user_id, call_id):
            reservation = Reservation(user_id, call_id)
            self._pending.append(reservation)
            return reservation
        if len(self._calls_of(user_id)) >= self.door_cap:
            return CODE_TOO_MANY_CALLS
        reservation = Reservation(user_id, call_id)
        self._pending.append(reservation)
        return reservation

    def confirm_door(self, reservation: Reservation, door_id: str | None = None) -> None:
        """The provider granted the answer: the reservation becomes a claim."""
        self._drop(reservation)
        if reservation.call_id in self._ended:     # the call ended while the provider was deciding: no claim to keep
            return
        self._claims[reservation.call_id] = (reservation.user_id, door_id)

    def release_door(self, reservation: Reservation) -> None:
        """The provider refused, or failed: the reservation goes; a claim another answer already holds stays."""
        self._drop(reservation)

    def door_sessions(self, user_id: str) -> int:
        return len(self._calls_of(user_id))

    def holds_door(self, user_id: str) -> bool:
        return bool(self._calls_of(user_id))

    def claimant(self, call_id: str) -> str | None:
        claim = self._claims.get(call_id)
        return claim[0] if claim else None

    def _holds(self, user_id: str, call_id: str) -> bool:
        return call_id in self._calls_of(user_id)

    def _calls_of(self, user_id: str) -> set[str]:
        held = {c for c, (u, _d) in self._claims.items() if u == user_id}
        return held | {r.call_id for r in self._pending if r.user_id == user_id}

    def _drop(self, reservation: Reservation) -> None:
        self._pending = [r for r in self._pending if r is not reservation]

    # ---- house side --------------------------------------------------------------------------------------------

    def house_slots_free(self) -> bool:
        return len(self._house) < self.house_cap

    def house_calls(self) -> int:
        return len(self._house)

    def open_house(self, call_id: str, endpoint_ids: set[str]) -> str | None:
        """Register a new house call that puts each of `endpoint_ids` in it. None on success; otherwise the endpoint
        id that is already in a house call (nothing is registered) or `"*"` for the cap."""
        if call_id in self._house:
            return "*"
        if not self.house_slots_free():
            return "*"
        for endpoint_id in endpoint_ids:
            if endpoint_id in self._by_endpoint:
                return endpoint_id
        self._house[call_id] = "ringing"
        self._members[call_id] = set(endpoint_ids)
        for endpoint_id in endpoint_ids:
            self._by_endpoint[endpoint_id] = call_id
        return None

    def set_house_state(self, call_id: str, state: str) -> None:
        if call_id in self._house:
            self._house[call_id] = state

    def leave_house(self, call_id: str, endpoint_id: str) -> None:
        """One endpoint is out of the house call (declined, displaced by a door); the call may go on."""
        if self._by_endpoint.get(endpoint_id) == call_id:
            del self._by_endpoint[endpoint_id]
        self._members.get(call_id, set()).discard(endpoint_id)

    def close_house(self, call_id: str) -> None:
        for endpoint_id in self._members.pop(call_id, set()):
            if self._by_endpoint.get(endpoint_id) == call_id:
                del self._by_endpoint[endpoint_id]
        self._house.pop(call_id, None)

    def house_call_of(self, endpoint_id: str) -> str | None:
        return self._by_endpoint.get(endpoint_id)

    def house_state(self, call_id: str) -> str | None:
        return self._house.get(call_id)

    # ---- the verdict for one endpoint ---------------------------------------------------------------------------

    def endpoint_state(self, endpoint_id: str, user_ids: tuple[str, ...] = (),
                       rung_by: Callable[[str | None], bool] | None = None) -> str:
        """The one busy state of an endpoint, by the precedence of design 7: a door call outranks a house call in every
        state. `user_ids` are the HA users that endpoint is (a phone's one user; a screen's login): a granted door claim
        by any of them is `door_active`; a door ringing rings the endpoint when `rung_by(door_id)` says the door reaches
        it (the default: every door reaches every endpoint, the safe side of "a door outranks a house call")."""
        if any(self.holds_door(u) for u in user_ids):
            return DOOR_ACTIVE
        if any(rung_by is None or rung_by(door) for door in self._rings.values()):
            return DOOR_RINGING
        call_id = self._by_endpoint.get(endpoint_id)
        if call_id is not None:
            return HOUSE_RINGING if self._house.get(call_id) == "ringing" else HOUSE_ACTIVE
        return IDLE

