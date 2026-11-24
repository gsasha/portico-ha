"""gate_sip_server driver metadata (config-flow schema and guards only; DEV-518 W2).

Portico's OWN SIP-server mode: the registrar/answerer the Dahua station would register to instead of gate-sip (Rust:
transport-rs/src/driver/gatesipserver/, design portico_design.md 4.2a E1). This is the SECOND config entry of the cut-over
(decided 2026-10-21); the cut-over from gate-sip's observer door is done and the observer door is gone (DEV-518 C5).

⛔ INERT. Nothing here contacts the station, gate-sip, RPC2 or anything else. Creating the entry only makes the
transport bind a UDP socket, and the form is built so that cannot happen by accident:

  - it is not in the "Add a door" picker (``hidden``): it has its own menu item, "SIP-server door (advanced)";
  - the form needs a confirmation tick;
  - it defaults to ``127.0.0.1`` and a high port (15060), RTP 45000, so by default it can reach nothing on the LAN;
  - a bind that is not loopback needs BOTH ``allow_non_loopback`` and a non-empty ``allow_from`` (the Go driver's
    own rule, mirrored here so a form that passes is a config Go accepts);
  - the ports gate-sip uses (5060, 40000, its control/bridge/ws ports 8091/8092/8095) and anything below 1024 are
    refused unless that LAN unlock is given.

REMOTE mode (DEV-518 W8b, `normalize_remote` / `REMOTE_SCHEMA`, door/remote_cfg.py, door/remote.py): the SIP server runs in a
`portico-transport` container, not in the process HA spawns. The entry then holds ``remote_host`` / ``remote_port`` /
``remote_token`` (the container's IPC listener and its shared secret) and NO SIP password: the container has its own. Its own
menu item ("SIP-server door, remote container (advanced)"). The host must be an IP literal that is loopback or RFC 1918, the
port may not be gate-sip's 8091/8092/8095, the token is at least 24 characters. Creating the entry opens one TCP connection to
that container (in the background, after setup) and nothing else.

The password is the station's ``SIP.AuthPassword`` (8 to 15 characters; Dahua sends no REGISTER at all outside that
range). It is stored in the entry like the other drivers' credentials and redacted in diagnostics (its key contains
``password``).
"""
from __future__ import annotations

import ipaddress
from typing import Any

import voluptuous as vol
from homeassistant.helpers import selector

from . import DRIVERS, DriverMeta
from ..remote_cfg import CONF_HOST, CONF_PORT, CONF_TOKEN, check_remote, normalize_host

KEY = "gate_sip_server"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_SIP_PORT = 15060
DEFAULT_RTP_PORT = 45000
DEFAULT_USER = "8001"
DEFAULT_REALM = "VDP"
MIN_PASSWORD = 8   # the Go driver's minPassword / maxPassword (gatesipserver/config.go)
MAX_PASSWORD = 15

# Ports a gate-sip on the estate uses: SIP 5060, RTP 40000, control 8091, audio bridge 8092, websocket 8095.
GATE_SIP_PORTS = frozenset({5060, 40000, 8091, 8092, 8095})
LOW_PORT_LIMIT = 1024

CONFIG_SCHEMA = vol.Schema(
    {
        vol.Required("sip_host", default=DEFAULT_HOST): str,
        vol.Required("sip_port", default=DEFAULT_SIP_PORT): vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
        vol.Required("rtp_port", default=DEFAULT_RTP_PORT): vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
        vol.Required("password"): str,
        vol.Optional("user", default=DEFAULT_USER): str,
        vol.Optional("realm", default=DEFAULT_REALM): str,
        vol.Optional("allow_non_loopback", default=False): bool,
        vol.Optional("allow_from", default=""): str,
        # W5: "Talk to the gate from Portico". Off by default. There is no claims leg on this driver (it IS the answerer),
        # so this switch needs nothing else.
        vol.Optional("dial_enabled", default=False): bool,
        # W5: the input_boolean turned ON when Portico answers, which closes every phone's ring as "taken elsewhere".
        vol.Optional("ring_answered_entity"): selector.EntitySelector(selector.EntitySelectorConfig(domain="input_boolean")),
        vol.Required("confirm", default=False): bool,
    }
)


REMOTE_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT): vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
        vol.Required(CONF_TOKEN): selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)),
        # The container's SIP listen address (PORTICO_SIP_LISTEN): it only names the door (`gate-sip-server:<listen>`, the
        # container's default PORTICO_SIP_DEVICE_ID). Nothing in this entry binds it.
        vol.Required("sip_host"): str,
        vol.Required("sip_port", default=5060): vol.All(vol.Coerce(int), vol.Range(min=1, max=65535)),
        vol.Optional("device_id", default=""): str,
        vol.Optional("dial_enabled", default=False): bool,
        vol.Optional("ring_answered_entity"): selector.EntitySelector(selector.EntitySelectorConfig(domain="input_boolean")),
        vol.Required("confirm", default=False): bool,
    }
)


def _bad(code: str, field: str) -> vol.Invalid:
    return vol.Invalid(code, path=[field])


def parse_allow_from(raw: Any) -> list[str]:
    """A comma-separated text (or a list) of IPs/CIDRs, as strings, normalised. A /0 is "allow everyone" spelt as a
    restriction and is refused, like the Go driver does. Raises vol.Invalid on ``allow_from``."""
    if raw is None or raw == "":
        return []
    if isinstance(raw, str):
        items = raw.split(",")
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        raise _bad("invalid_allow_from", "allow_from")
    out: list[str] = []
    for item in items:
        if not isinstance(item, str):
            raise _bad("invalid_allow_from", "allow_from")
        item = item.strip()
        if not item:
            continue
        try:
            net = ipaddress.ip_network(item, strict=False)
        except ValueError as err:
            raise _bad("invalid_allow_from", "allow_from") from err
        if net.prefixlen == 0:
            raise _bad("allow_from_any", "allow_from")
        out.append(str(net.network_address) if net.prefixlen == net.max_prefixlen else str(net))
    return out


def parse_dial(raw: Any) -> bool:
    """The `dial_enabled` switch: a real bool (anything else is refused, as the Go driver's parseDial does)."""
    if raw is None:
        return False
    if not isinstance(raw, bool):
        raise _bad("invalid_dial", "dial_enabled")
    return raw


def parse_ring_entity(raw: Any) -> str | None:
    """The ring-answered helper: empty/None means none, otherwise an ``input_boolean.<object>`` id."""
    if raw is None or raw == "":
        return None
    if not (isinstance(raw, str) and raw.startswith("input_boolean.") and len(raw) > len("input_boolean.")):
        raise _bad("invalid_ring_entity", "ring_answered_entity")
    return raw


def _is_locked_port(port: int) -> bool:
    return port < LOW_PORT_LIMIT or port in GATE_SIP_PORTS


def listen_address(host: str, port: int) -> str:
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def normalize(data: dict[str, Any]) -> dict[str, Any]:
    """Config-flow normalisation (DriverMeta.normalize). Pure: no network, no file, no HA. Returns the entry data the
    transport ``configure`` takes (``sip_listen``, ``rtp_port``, ``password``, ``user``, ``realm``,
    ``allow_non_loopback``, ``allow_from``, ``device_id``) and raises vol.Invalid naming the field."""
    if data.get("confirm") is not True:
        raise _bad("confirm_required", "confirm")

    raw_host = data.get("sip_host", DEFAULT_HOST)
    if not isinstance(raw_host, str):
        raise _bad("invalid_sip_host", "sip_host")
    try:
        ip = ipaddress.ip_address(raw_host.strip().strip("[]"))
    except ValueError as err:  # a name would mean a DNS lookup; literals only
        raise _bad("invalid_sip_host", "sip_host") from err
    host = str(ip)

    ports = {}
    for field, default in (("sip_port", DEFAULT_SIP_PORT), ("rtp_port", DEFAULT_RTP_PORT)):
        value = data.get(field, default)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
            raise _bad("invalid_port", field)
        ports[field] = value
    if ports["sip_port"] == ports["rtp_port"]:
        raise _bad("ports_equal", "rtp_port")

    password = data.get("password")
    if not isinstance(password, str) or not MIN_PASSWORD <= len(password) <= MAX_PASSWORD:
        raise _bad("password_length", "password")

    allow_non_loopback = bool(data.get("allow_non_loopback", False))
    allow_from = parse_allow_from(data.get("allow_from", ""))

    unlocked = allow_non_loopback and bool(allow_from)
    if not ip.is_loopback and not unlocked:
        # the Go driver refuses this too; say which flag is missing
        raise _bad("lan_bind_needs_flags", "allow_non_loopback" if not allow_non_loopback else "allow_from")
    for field in ("sip_port", "rtp_port"):
        if _is_locked_port(ports[field]) and not unlocked:
            raise _bad("standard_port_locked", field)

    user = str(data.get("user") or DEFAULT_USER).strip() or DEFAULT_USER
    realm = str(data.get("realm") or DEFAULT_REALM).strip() or DEFAULT_REALM
    sip_listen = listen_address(host, ports["sip_port"])
    dial = parse_dial(data.get("dial_enabled"))
    ring_entity = parse_ring_entity(data.get("ring_answered_entity"))
    return {
        "sip_host": host,
        "sip_port": ports["sip_port"],
        "sip_listen": sip_listen,
        "rtp_port": ports["rtp_port"],
        "password": password,
        "user": user,
        "realm": realm,
        "allow_non_loopback": allow_non_loopback,
        "allow_from": allow_from,
        "device_id": f"gate-sip-server:{sip_listen}",
        # Stored only when set, so an entry that never uses them is exactly what it was. `dial_enabled` is a transport
        # config key (Go parseDial); the helper is Python's own bookkeeping, kept under the `gate_sip_listen` key
        # (door/gate_sip_listen.py KEY), which `transport_config` strips before `configure`.
        **({"dial_enabled": True} if dial else {}),
        **({"gate_sip_listen": {"ring_answered_entity": ring_entity}} if ring_entity else {}),
    }


def normalize_remote(data: dict[str, Any]) -> dict[str, Any]:
    """Config-flow normalisation of a REMOTE-mode entry (see the module docstring). Pure. Raises vol.Invalid naming the
    field. Returns the entry data: no password, no bind; ``device_id`` is what the container's driver is registered under
    (``PORTICO_SIP_DEVICE_ID``, default ``gate-sip-server:<its SIP listen>``)."""
    if data.get("confirm") is not True:
        raise _bad("confirm_required", "confirm")
    host, port, token = data.get(CONF_HOST), data.get(CONF_PORT), data.get(CONF_TOKEN)
    problem = check_remote(host, port, token)
    if problem is not None:
        raise _bad(problem[1], problem[0])
    try:
        sip_ip = ipaddress.ip_address(str(data.get("sip_host", "")).strip().strip("[]"))
    except ValueError as err:
        raise _bad("invalid_sip_host", "sip_host") from err
    sip_port = data.get("sip_port", 5060)
    if isinstance(sip_port, bool) or not isinstance(sip_port, int) or not 1 <= sip_port <= 65535:
        raise _bad("invalid_port", "sip_port")
    sip_listen = listen_address(str(sip_ip), sip_port)
    device_id = str(data.get("device_id") or "").strip() or f"gate-sip-server:{sip_listen}"
    if len(device_id) > 128 or any(ord(c) < 33 or ord(c) == 127 for c in device_id):
        raise _bad("invalid_device_id", "device_id")
    dial = parse_dial(data.get("dial_enabled"))
    ring_entity = parse_ring_entity(data.get("ring_answered_entity"))
    return {
        CONF_HOST: normalize_host(host),
        CONF_PORT: port,
        CONF_TOKEN: token,
        "sip_host": str(sip_ip),
        "sip_port": sip_port,
        "sip_listen": sip_listen,
        "device_id": device_id,
        **({"dial_enabled": True} if dial else {}),
        **({"gate_sip_listen": {"ring_answered_entity": ring_entity}} if ring_entity else {}),
    }


def remote_conflicts(data: dict[str, Any], entries: list[dict[str, Any]]) -> tuple[str, str] | None:
    """Another remote entry already points at this container's IPC port."""
    for other in entries:
        if other.get("driver") == KEY and other.get(CONF_HOST) and \
                (other.get(CONF_HOST), other.get(CONF_PORT)) == (data[CONF_HOST], data[CONF_PORT]):
            return CONF_PORT, "remote_in_use_by_entry"
    return None


def entry_conflicts(data: dict[str, Any], entries: list[dict[str, Any]]) -> tuple[str, str] | None:
    """The checks that need the other entries (the flow calls this after ``normalize``; ``entries`` is every Portico
    entry's data). Returns ``(field, error key)`` or None.

    - another server entry already uses this SIP or RTP port on an overlapping host: two sockets cannot share it."""
    host = data["sip_host"]
    wildcard = ipaddress.ip_address(host).is_unspecified
    for other in entries:
        if other.get("driver") == KEY:
            oh = other.get("sip_host", "")
            overlap = host == oh or wildcard or (oh and ipaddress.ip_address(oh).is_unspecified)
            if overlap:
                if data["sip_port"] in (other.get("sip_port"), other.get("rtp_port")):
                    return "sip_port", "port_in_use_by_entry"
                if data["rtp_port"] in (other.get("sip_port"), other.get("rtp_port")):
                    return "rtp_port", "port_in_use_by_entry"
    return None


META = DriverMeta(
    key=KEY, config_schema=CONFIG_SCHEMA, normalize=normalize, has_own_video=False, hidden=True
)
DRIVERS[META.key] = META
