"""The push wire contract `push/v2` (sender design §4.1, §4.2; M6 E3).

Data-only messages, every value a string (the FCM v1 `data` map is
string -> string). No names, URLs, hosts, credentials or media links can be
put in one: the builders take only the door ref, the call id and times. The
door ref is `Door.door_ref`, `d-` and 32 hex characters derived from the door's
stable endpoint id (provider.py), never an entry id and never the URL a door id
may be. `contract/push_v2.json` at the repo root is the fixture shared with
portico-app; a change here is a version bump on both sides (§8.2).

`pv` is "2" (v1's was "1"; an app on `pv: "1"` is refused at registration by
`portico/push/register`, so it is never sent one). The fields are v1's, plus two
optional keys that reserve the house calls (M7, HC-8):

* `ct`, the call type: `door` when absent, `house` for a call between two
  endpoints of the house. The notifier sends neither key until M7; a 0.5.0 app
  that reads `ct: house` ignores the message (`Ignored(UNKNOWN_KIND)`).
* `from_ref`, an opaque reference to the caller of a house call.

v1.1's `kind: "test"` stays: the staged real-device test's quiet notification
(spec §8.3 step 2), which carries only `pv`, `kind` and `t`: no door, call id or
seq, because portico-app treats it as touching no call state (`PushMessage.Test`).
"""
from __future__ import annotations

import json
import math
import re
from typing import Any

PV = "2"
CT_DOOR = "door"
CT_HOUSE = "house"
CALL_TYPES = (CT_DOOR, CT_HOUSE)
KIND_RING = "ring"
KIND_ANSWERED = "answered"
KIND_ENDED = "ended"
KIND_TEST = "test"
SEQ = {KIND_RING: "1", KIND_ANSWERED: "2", KIND_ENDED: "3"}

DISMISS_TTL_S = 60
TEST_TTL_S = DISMISS_TTL_S  # a test is no more urgent than a dismiss
MAX_DATA_BYTES = 4096  # FCM's data-payload limit (§4.1)

# Opaque ids only: a derived door ref (`d-` and 32 hex) and transport call ids
# (`gso-<ns>-<n>-<rand>`) fit; a URL, an e-mail or free text does not.
_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class PayloadError(ValueError):
    """A message this module refuses to build (our bug, never sent)."""


def _check_id(value: Any, what: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise PayloadError(f"invalid {what}")
    return value


_NO_DOOR: Any = object()     # the door_ref of a house message (15.5): absent, never None (a None door_ref stays an error)


def _base(kind: str, door_ref: Any, call_id: str, t_ms: int, ct: str | None = None,
          from_ref: str | None = None) -> dict[str, str]:
    data = {"pv": PV, "kind": kind}
    if door_ref is not _NO_DOOR:
        data["door_ref"] = _check_id(door_ref, "door_ref")
    data["call_id"] = _check_id(call_id, "call_id")
    data["seq"] = SEQ[kind]
    data["t"] = str(int(t_ms))
    if ct is not None:
        if ct not in CALL_TYPES:
            raise PayloadError("invalid ct")
        data["ct"] = ct
    if from_ref is not None:
        data["from_ref"] = _check_id(from_ref, "from_ref")
    return data


def build_ring(door_ref: str, call_id: str, t_ms: int, ring_window_s: int, *, ct: str | None = None,
               from_ref: str | None = None) -> dict[str, str]:
    data = _base(KIND_RING, door_ref, call_id, t_ms, ct, from_ref)
    data["exp"] = str(int(t_ms) + int(ring_window_s) * 1000)
    return data


def build_answered(door_ref: str, call_id: str, t_ms: int, *, ct: str | None = None,
                   from_ref: str | None = None) -> dict[str, str]:
    data = _base(KIND_ANSWERED, door_ref, call_id, t_ms, ct, from_ref)
    # Never a name (§4.1): "answered elsewhere" is enough to stop the ring.
    data["by"] = "other"
    return data


def build_ended(door_ref: str, call_id: str, t_ms: int, *, ct: str | None = None,
                from_ref: str | None = None) -> dict[str, str]:
    return _base(KIND_ENDED, door_ref, call_id, t_ms, ct, from_ref)


def build_house_ring(from_ref: str, call_id: str, t_ms: int, ring_window_s: int) -> dict[str, str]:
    """The push of a house ring for an away phone (M7 3a defines the shape; 3c sends it): `ct: house`, an opaque
    `from_ref` (house.house_from_ref), NO `door_ref` (15.5). App 0.5.0 ignores it by construction; 0.6.0 rings."""
    data = _base(KIND_RING, _NO_DOOR, call_id, t_ms, CT_HOUSE, _check_id(from_ref, "from_ref"))
    data["exp"] = str(int(t_ms) + int(ring_window_s) * 1000)
    return data


def build_house_answered(from_ref: str, call_id: str, t_ms: int) -> dict[str, str]:
    data = _base(KIND_ANSWERED, _NO_DOOR, call_id, t_ms, CT_HOUSE, _check_id(from_ref, "from_ref"))
    data["by"] = "other"
    return data


def build_house_ended(from_ref: str, call_id: str, t_ms: int) -> dict[str, str]:
    return _base(KIND_ENDED, _NO_DOOR, call_id, t_ms, CT_HOUSE, _check_id(from_ref, "from_ref"))


def build_test(t_ms: int) -> dict[str, str]:
    """v1.1: one quiet "the push ring notification works" notification on the phone,
    never a ring (spec §8.3 step 2)."""
    return {"pv": PV, "kind": KIND_TEST, "t": str(int(t_ms))}


def data_size(data: dict[str, str]) -> int:
    return sum(len(k.encode()) + len(v.encode()) for k, v in data.items())


def check_size(data: dict[str, str]) -> None:
    if data_size(data) > MAX_DATA_BYTES:
        raise PayloadError("data payload over 4096 bytes")


def ttl_seconds(data: dict[str, str], now_ms: int, ring_window_s: int) -> int:
    """§4.2: a ring lives until its `exp` (at least 1 s, at most the ring
    window); answered/ended and a test live 60 s."""
    if data.get("kind") == KIND_TEST:
        return TEST_TTL_S
    if data.get("kind") != KIND_RING:
        return DISMISS_TTL_S
    remaining = math.ceil((int(data["exp"]) - int(now_ms)) / 1000)
    return max(1, min(int(ring_window_s), remaining))


def build_message(
    *, kind: str, registration: str, package: str, data: dict[str, str], now_ms: int, ring_window_s: int
) -> dict[str, Any]:
    """The FCM v1 `messages:send` body. `kind` is the registration's kind:
    `fid` sends with `message.fid`, `token` with `message.token` (§0.3).
    HIGH priority, no collapse key (§0.4), no notification block."""
    if kind not in ("fid", "token"):
        raise PayloadError("registration kind must be fid or token")
    if not isinstance(registration, str) or not registration.strip():
        raise PayloadError("blank registration")
    check_size(data)
    return {
        "message": {
            kind: registration,
            "data": dict(data),
            "android": {
                "priority": "HIGH",
                "ttl": f"{ttl_seconds(data, now_ms, ring_window_s)}s",
                "restricted_package_name": package,
                "direct_boot_ok": False,
            },
        }
    }


def message_bytes(message: dict[str, Any]) -> int:
    return len(json.dumps(message, separators=(",", ":")).encode())
