"""ha_event driver metadata: a RING-ONLY door whose ring comes from a Home Assistant event entity.

With the `talkback` option OFF (the default, and every entry that exists today) there is no Go driver behind this one
and no network: the Python side (`door/ha_event_ring.py`) listens to the chosen
`event.*` entity (this estate's front door: the UniFi Protect doorbell's event entity, event type `ring`) and starts a
ring on the door's call-state tracker. The rest of the ring pipeline is the ordinary one: `sensor.<door> call state`,
the push notifier, the phones' websocket, the endpoint's camera.

Ring-only by construction (portico_front_door_design.md, options A/B and F1): the Capabilities below state no audio
either way, no relay and no direct path, so a phone gets a ring screen with the door's camera and nothing to talk
through or open with. Nothing in this driver can unlock a door. Its door is also never attached to the transport hub,
so adding it changes nothing for the doors that are. With `talkback` ON the same door ALSO attaches to the hub as the Go
driver `unifi_protect_talk` (door/door.py): the ring source stays here, Go only holds the answer handle and the media.
"""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from . import DRIVERS, DriverMeta

KEY = "ha_event"

DEFAULT_NAME = "front"
DEFAULT_EVENT_TYPE = "ring"
DEFAULT_HOLD = 30        # seconds a ring stays ringing when nobody dismisses it
MIN_HOLD = 5
MAX_HOLD = 120

# Same vocabulary as the Go drivers' Capabilities (driver.go): 3 is NoAudio. ReportsAvailability: the door follows
# its event entity (unavailable entity -> unavailable door).
CAPABILITIES: dict[str, Any] = {
    "InboundAudio": 3,
    "OutboundAudio": 3,
    "DirectCapable": False,
    "HasRelay": False,
    "RTSPTemplate": "",
    "ReportsAvailability": True,
}

# Capabilities of the same door once it can answer and talk (portico_front_door_talk_plan.md, section 3): the visitor's
# voice rides the camera stream (InVideoStream = 1), the phone's voice goes up as a talkback push (TalkbackPush = 2).
# Still no relay and no direct path: nothing here opens or unlocks anything. `signaling._answerable` is therefore true
# for this set and false for CAPABILITIES above, which is how the app learns that Answer may be offered.
TALKBACK_CAPABILITIES: dict[str, Any] = {
    **CAPABILITIES,
    "InboundAudio": 1,
    "OutboundAudio": 2,
}

# The answer/talk backend (Go driver `unifi_protect_talk`, T3-T5 of the plan, wired by T6) exists. The `talkback` option
# is accepted only while this is True; it stays off by default, so no entry changes behaviour until someone turns it on
# (T7, with a Protect key of its own).
TALKBACK_BACKEND_READY = True

# The Go driver behind a talkback entry, and the first wheel that has it. An older wheel makes the door fall back to
# ring-only (door.py) rather than fail its entry.
TALK_DRIVER = "unifi_protect_talk"
TALKBACK_MIN_WHEEL = "0.6.5"

# Defaults of the talkback-only options (plan section 5). Not in CONFIG_SCHEMA as defaults, so an entry without
# talkback stores none of them.
# The options talkback cannot run without (no usable default exists for them).
TALK_REQUIRED = ("protect_api_key", "protect_host", "camera_id", "downlink_url")
TALK_DEFAULTS: dict[str, Any] = {
    # Estate values, so no default (DEV-836): an entry that turns talkback on states its own console host, the doorbell's
    # camera id in Protect and the RTSP URL of the doorbell's audio. Empty means "not configured", and talkback stays
    # off (`talkback_enabled`) until all three are set.
    "protect_host": "",
    "camera_id": "",
    "downlink_url": "",
    # Empty = no external guard: the old bell-talk relay is retired and Portico's own one-call-per-door rule is the only
    # single-session guard. Set a /health URL only while an old relay still runs beside Portico (DEV-835).
    "guard_url": "",
    "verify_ssl": False,        # the console serves a self-signed certificate on its LAN address
    "gain": 1,
}

CONFIG_SCHEMA = vol.Schema(
    {
        vol.Optional("name", default=DEFAULT_NAME): str,
        vol.Required("event_entity"): str,
        vol.Optional("event_type", default=DEFAULT_EVENT_TYPE): str,
        vol.Optional("ring_hold", default=DEFAULT_HOLD): vol.All(vol.Coerce(int), vol.Range(min=MIN_HOLD, max=MAX_HOLD)),
        # Answer and talk through the doorbell's talkback (default off: ring-only). Refused while the backend is not ready.
        vol.Optional("talkback", default=False): bool,
        # Only read when talkback is on (TALK_DEFAULTS above). The key is a full-console admin key, one per consumer:
        # it lives in the entry and nowhere else (diagnostics redact it; no log line and no error carries it).
        vol.Optional("protect_host"): str,
        vol.Optional("protect_api_key"): str,
        vol.Optional("camera_id"): str,
        vol.Optional("downlink_url"): str,
        vol.Optional("guard_url"): str,
        vol.Optional("verify_ssl"): bool,
        vol.Optional("gain"): vol.All(vol.Coerce(int), vol.Range(min=1, max=16)),
    }
)


# The talk fields the Configure form (door/flow.py) edits. They live in the entry's OPTIONS (the add-a-door form put them
# in `data`); `merged_talk_data` is the one place that lays options over data, and every reader of them goes through it.
TALK_OPTION_KEYS = ("talkback", "protect_api_key", *TALK_DEFAULTS)


def merged_talk_data(data: Any, options: Any) -> dict[str, Any]:
    """The entry's data with the talk fields of its options laid over it. An option present wins even when empty (an
    explicitly cleared key is "" and hides a key left in `data`); every other option (away_media) is not merged."""
    merged = dict(data)
    for key in TALK_OPTION_KEYS:
        if key in options:
            merged[key] = options[key]
    return merged


def validate_talk_options(merged: dict[str, Any]) -> tuple[str, str] | None:
    """The talk fields of an already-merged entry through the driver's own `normalize`: None when fine, else
    (field, error code) with the add-a-door form's codes. Carries no value, so no error can echo the key."""
    try:
        normalize(merged)
    except vol.Invalid as err:
        return (str(err.path[0]) if err.path else "base", err.error_message)
    return None


def capabilities_for(data: dict[str, Any]) -> dict[str, Any]:
    """The door's capabilities for this entry: ring-only unless the entry has `talkback` on, the backend is ready AND a
    key is stored. Every condition, so a hand-edited entry cannot make the door claim audio the code cannot carry. (The
    running door states Go's own capabilities once the Go driver has attached; these are the ones before that.)"""
    if talkback_enabled(data):
        return dict(TALKBACK_CAPABILITIES)
    return dict(CAPABILITIES)


def talkback_enabled(data: dict[str, Any]) -> bool:
    """Whether this entry runs the answer/talk backend: the option is on AND the backend is ready AND the key, the console
    host, the camera id and the audio URL are all stored (there is no default for any of them)."""
    return bool(data.get("talkback") is True and TALKBACK_BACKEND_READY and all(data.get(k) for k in TALK_REQUIRED))


def talk_transport_config(data: dict[str, Any], device_id: str) -> dict[str, Any]:
    """The `configure` payload of the Go `unifi_protect_talk` driver for this entry: the talkback options over their
    defaults. Contains the Protect key, so it goes to the transport and nowhere else (never logged)."""
    config: dict[str, Any] = {"device_id": device_id, **TALK_DEFAULTS}
    for key in (*TALK_DEFAULTS, "protect_api_key"):
        value = data.get(key)
        if value not in (None, ""):
            config[key] = value
    return config


def normalize(data: dict[str, Any]) -> dict[str, Any]:
    """Config-flow normalisation: `event_entity` must be an `event.` entity id. `device_id` (the id both sides know the
    door by, and the entry's unique-id field) is derived from it. Pure: reads no state."""
    entity = data.get("event_entity")
    if not isinstance(entity, str) or not entity.strip().lower().startswith("event.") or " " in entity.strip():
        raise vol.Invalid("invalid_event_entity", path=["event_entity"])
    entity = entity.strip().lower()
    name = str(data.get("name") or DEFAULT_NAME).strip() or DEFAULT_NAME
    hold = int(data.get("ring_hold", DEFAULT_HOLD))
    if not MIN_HOLD <= hold <= MAX_HOLD:
        raise vol.Invalid("invalid_ring_hold", path=["ring_hold"])
    talkback = data.get("talkback", False)
    if not isinstance(talkback, bool):
        raise vol.Invalid("invalid_talkback", path=["talkback"])
    if talkback and not TALKBACK_BACKEND_READY:
        raise vol.Invalid("talkback_not_ready", path=["talkback"])
    if talkback and not (isinstance(data.get("protect_api_key"), str) and data["protect_api_key"].strip()):
        raise vol.Invalid("protect_api_key_required", path=["protect_api_key"])
    if talkback:
        for key in ("protect_host", "camera_id", "downlink_url"):
            if not (isinstance(data.get(key), str) and data[key].strip()):
                raise vol.Invalid("invalid_talkback_option", path=[key])
    for key in ("protect_host", "protect_api_key", "camera_id", "downlink_url", "guard_url"):
        if data.get(key) is not None and not isinstance(data[key], str):
            raise vol.Invalid("invalid_talkback_option", path=[key])
    if talkback and "guard_url" not in data:
        data = {**data, "guard_url": ""}        # a NEW install states "no external guard"; legacy entries lack the key
    return {
        **data,
        "talkback": talkback,
        "name": name,
        "event_entity": entity,
        "event_type": str(data.get("event_type") or DEFAULT_EVENT_TYPE),
        "ring_hold": hold,
        "device_id": f"ha_event:{entity}",
    }


META = DriverMeta(
    key=KEY,
    config_schema=CONFIG_SCHEMA,
    normalize=normalize,
    # The door's only picture is an existing HA camera entity (the doorbell's own): required in the flow.
    has_own_video=False,
    local_capabilities=CAPABILITIES,
    local_capabilities_for=capabilities_for,
)
DRIVERS[META.key] = META
