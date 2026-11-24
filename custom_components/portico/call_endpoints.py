"""Who a call button can ring: the registry INTERFACE (call buttons, house calls design §14).

A `call` button names a target (`dashboard`, `door` or `phone` plus an opaque endpoint id). What that id
means, whether the endpoint is reachable and how a call to it is placed are not decided here: they belong
to the house-call work (phone and dashboard endpoints) and to gate-sip G2 (the `door` target, "Talk to the
gate"). This module is only the seam they plug into, so the content code (validation, `content/get`,
`content/run`) can be built and tested before any of them exists.

The default registry, `NullCallEndpointRegistry`, knows no endpoint and places no call. On a real install,
until a backend registers a real registry with `set_call_registry`, the builder therefore offers no call
target, an existing call button shows as off, and `content/run` answers a call button with `not_available`.
Nothing here ever dials anything.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from homeassistant.core import HomeAssistant

DATA_CALL_REGISTRY = "call_registry"

TARGET_TYPES = ("dashboard", "door", "phone")
# A roster entry may also name a GROUP ("Everyone", 15.14). A hand-written `call` BUTTON may not, so `normalize_profile`
# (TARGET_TYPES) is unchanged and no stored button can name one: a group is named only by a Talk element's `options.group`
# (which expands to an inline `call` action, group-call design G1/G3) or by a roster key (`r-...`), and only a screen places it.
ROSTER_TARGET_TYPES = (*TARGET_TYPES, "group")
MAX_TARGET_ID = 64

# `reason` codes a registry may give for an endpoint that is not available. The app maps each to its own
# words; an unknown code is shown as "not available". `None` means available.
REASON_NOT_AVAILABLE = "not_available"  # this Home Assistant cannot place calls (the default registry)
REASON_UNKNOWN_TARGET = "unknown_target"  # the endpoint was removed or never existed
REASON_OFFLINE = "offline"
REASON_PUSH_ONLY = "push_only"  # reachable by notification only (house calls 3c)
REASON_DND = "dnd"
REASON_BUSY = "busy"
REASON_RECEIVE_OFF = "receive_off"  # the callee's own "may receive house calls" switch is off
REASON_DISABLED = "disabled"
REASON_PHONE_ONLY = "phone_only"  # the gate (3f): only an enrolled phone can talk to it, not a screen
REASON_NO_PHONES = "no_phones"  # the "Call all phones" group: no phone (panels excluded) is set to receive house calls right now
REASON_NO_PANELS = "no_panels"  # the "Call all panels" group: no other panel can receive a call right now
REASON_NO_RECEIVERS = "no_receivers"  # the "Everyone" key: no phone is set to receive house calls right now (15.14)

# Codes a placement may be refused with (`PlaceResult.code`), sent as the `content/run` error code.
CODE_NOT_AVAILABLE = "not_available"  # the default registry: calls cannot be placed on this install yet


@dataclass(frozen=True)
class Endpoint:
    """One callable thing, as the builder lists it."""

    type: str  # one of TARGET_TYPES
    id: str  # opaque; for `door`, the Portico device_id
    label: str  # what to call it in the picker and the default button label


@dataclass(frozen=True)
class TargetState:
    """What `content/get` says about a call button's target. `label` is the endpoint's current name (a
    renamed phone renames its buttons); `reason` is None when `available`."""

    label: str
    available: bool
    reason: str | None = None


@dataclass(frozen=True)
class PlaceResult:
    """The outcome of placing a call. Success carries the caller's `call_id` and `media` leg descriptor
    (house calls design §8); a refusal carries an error `code` and a plain `message`."""

    ok: bool
    call_id: str | None = None
    media: dict[str, Any] | None = field(default=None)
    code: str | None = None
    message: str | None = None


@runtime_checkable
class CallEndpointRegistry(Protocol):
    def endpoints(self, *, caller_user_id: str) -> list[Endpoint]:
        """The endpoints the builder may offer for this phone's profile: only callable ones (a phone with
        receive switched off is not listed), never the phone itself."""

    def target_state(self, target_type: str, target_id: str) -> TargetState | None:
        """The target's current state, or None when the registry does not know it at all."""

    async def place(self, *, caller_user_id: str, caller_phone: str, target_type: str, target_id: str,
                    caller_endpoint: str | None = None, acting_user_id: str | None = None) -> PlaceResult:
        """Place a call from this phone to the target. Must not raise for an expected refusal (return a
        `PlaceResult` with a code); the caller treats any exception as a failure, never a crash. `caller_endpoint` is the
        id of the endpoint whose grant ran the action (a phone's, or a screen's: a screen's login may serve several) and
        `acting_user_id` the HA user who pressed (an administrator running a phone's button is not the phone)."""


class NullCallEndpointRegistry:
    """The default: knows no endpoint, places nothing."""

    def endpoints(self, *, caller_user_id: str) -> list[Endpoint]:
        return []

    def target_state(self, target_type: str, target_id: str) -> TargetState | None:
        return None

    async def place(self, *, caller_user_id: str, caller_phone: str, target_type: str, target_id: str,
                    caller_endpoint: str | None = None, acting_user_id: str | None = None) -> PlaceResult:
        return PlaceResult(ok=False, code=CODE_NOT_AVAILABLE,
                           message="this Home Assistant cannot place calls yet")


def get_call_registry(hass: HomeAssistant) -> CallEndpointRegistry:
    domain_data = hass.data.setdefault("portico", {})
    registry = domain_data.get(DATA_CALL_REGISTRY)
    if registry is None:
        registry = domain_data[DATA_CALL_REGISTRY] = NullCallEndpointRegistry()
    return registry


def set_call_registry(hass: HomeAssistant, registry: CallEndpointRegistry) -> None:
    """The one hook for a backend: house calls and gate-sip G2 register the real registry here."""
    hass.data.setdefault("portico", {})[DATA_CALL_REGISTRY] = registry
