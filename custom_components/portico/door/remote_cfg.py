"""The REMOTE mode of a gate_sip_server entry: pure helpers, no Home Assistant, no I/O (DEV-518 W8b).

A remote entry does not run the SIP server in the transport process HA spawns; it points at a `portico-transport`
container that already runs one (transport-rs/src/process/standalone.rs) and speaks the transport IPC to it over TCP.
The entry carries `remote_host`, `remote_port` and `remote_token` (stored like the other credentials, redacted in
diagnostics). The container holds the SIP password in its own secret, so a remote entry has none.

The IPC stream is NOT encrypted and a connection is full control of that transport, so the Python side refuses what the Go
side would merely hope for: the host is an IP literal on a private network (RFC 1918) or loopback, the port is not one of
gate-sip's, and the token is at least the Go minimum.
"""
from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from typing import Any

SERVER_DRIVER = "gate_sip_server"
CONF_HOST = "remote_host"
CONF_PORT = "remote_port"
CONF_TOKEN = "remote_token"

MIN_TOKEN_LEN = 24     # transport-rs/src/ipc/tcp.rs MIN_TOKEN_LEN
MAX_TOKEN_LEN = 256
GATE_SIP_PORTS = frozenset({8091, 8092, 8095})     # gate-sip's control, audio bridge and websocket ports

_PRIVATE = tuple(ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def is_remote(data: Mapping[str, Any]) -> bool:
    """Is this entry data a remote-mode SIP-server door? Existing (local) entries have no `remote_host` and are unchanged."""
    return data.get("driver") == SERVER_DRIVER and bool(data.get(CONF_HOST))


def host_error(raw: Any) -> str | None:
    """None when `raw` is an IP literal that is loopback or RFC 1918; otherwise the error key."""
    if not isinstance(raw, str):
        return "invalid_remote_host"
    try:
        ip = ipaddress.ip_address(raw.strip().strip("[]"))
    except ValueError:
        return "invalid_remote_host"          # a name would mean a DNS lookup; literals only
    if ip.is_unspecified:
        return "invalid_remote_host"
    if ip.is_loopback or (ip.version == 4 and any(ip in n for n in _PRIVATE)):
        return None
    return "remote_host_not_private"


def port_error(raw: Any) -> str | None:
    if isinstance(raw, bool) or not isinstance(raw, int) or not 1 <= raw <= 65535:
        return "invalid_port"
    if raw in GATE_SIP_PORTS:
        return "remote_port_is_gate_sip"
    return None


def token_error(raw: Any) -> str | None:
    if not isinstance(raw, str) or not MIN_TOKEN_LEN <= len(raw) <= MAX_TOKEN_LEN:
        return "remote_token_length"
    if any(ord(c) <= 32 or ord(c) == 127 for c in raw):
        return "remote_token_chars"
    return None


def check_remote(host: Any, port: Any, token: Any) -> tuple[str, str] | None:
    """(field, error key) of the first problem, or None."""
    for field, err in ((CONF_HOST, host_error(host)), (CONF_PORT, port_error(port)), (CONF_TOKEN, token_error(token))):
        if err:
            return field, err
    return None


def normalize_host(raw: str) -> str:
    return str(ipaddress.ip_address(raw.strip().strip("[]")))
