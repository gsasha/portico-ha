"""`ha_dial`: where a phone's tailnet path dials (DEV-557, portico-app #16).

Away from home the app reaches Home Assistant through its own embedded
Tailscale node (tsnet). tsnet resolves a host name through MagicDNS and then
the PUBLIC resolver; it never uses the tailnet's split DNS, so an internal
name like `ha_url`'s cannot be dialled. The enrollment QR may therefore carry
`"ha_dial": "10.6.0.5:443"`, an IP literal and a port.

`parse_ha_dial` is a line-by-line port of portico-app's
`TailnetTarget.parseHaDial` (core/.../tailnet/TailnetTarget.kt) and the
`Ipv4.parseCanonical` / `Ipv6.parse` helpers it uses (core/.../net/UrlPolicy.kt).
The app REFUSES THE WHOLE ENROLLMENT when a present `ha_dial` fails that
function, so the server must never emit a value it would refuse; the table in
tests/test_ha_dial.py is the app's own test table. No `ipaddress` and no
`str.isdigit`: those accept zone ids, Unicode digits and other forms the
Kotlin code refuses.

The app's parseHaDial also runs `UrlPolicy.check("http://" + t)` first. For
any text the checks below accept (ASCII digits, dots, hex, colons, brackets
only), that check always answers "private http", so it refuses nothing more;
the whitespace/control/backslash part of it is kept anyway.
"""
from __future__ import annotations

_MAX_LEN = 64
_DIGITS = frozenset("0123456789")
_HEX = frozenset("0123456789abcdef")
_V6_CHARS = _HEX | {":", "."}


def parse_ha_dial(text) -> str | None:
    """The canonical `host:port` (IPv6 bracketed and lower-case, otherwise as
    written), or None. Accepted: a canonical dotted-quad IPv4 in 10/8,
    172.16/12, 192.168/16 or 100.64/10, or `[v6]` in fc00::/7 (not IPv4-mapped),
    then `:` and a port 1-65535 without a leading zero. Surrounding whitespace
    is trimmed, as the app does; the result has none."""
    if not isinstance(text, str):
        return None
    t = text.strip()
    if not t or len(t) > _MAX_LEN:
        return None
    if any(c.isspace() or not c.isprintable() or c == "\\" for c in t):
        return None
    if t.startswith("["):
        close = t.find("]")
        if close < 0 or close + 1 >= len(t) or t[close + 1] != ":":
            return None
        host = t[1:close].lower()
        port = t[close + 2:]
        b = _parse_ipv6(host)
        if b is None:
            return None
        if (b[0] & 0xFE) != 0xFC:  # fc00::/7 only (excludes ::1, fe80::/10, ::ffff:a.b.c.d)
            return None
    else:
        colon = t.rfind(":")
        if colon <= 0 or t.find(":") != colon:
            return None
        host = t[:colon]
        port = t[colon + 1:]
        ip = _parse_ipv4(host)
        if ip is None or not _ipv4_allowed(ip):
            return None
    if not port or len(port) > 5 or not set(port) <= _DIGITS or port[0] == "0":
        return None
    if not 1 <= int(port) <= 65535:
        return None
    return f"[{host}]:{port}" if t.startswith("[") else f"{host}:{port}"


def _parse_ipv4(text: str) -> int | None:
    """Exactly four decimal parts 0..255 without leading zeros (Ipv4.parseCanonical)."""
    parts = text.split(".")
    if len(parts) != 4:
        return None
    value = 0
    for p in parts:
        if not p or len(p) > 3 or not set(p) <= _DIGITS:
            return None
        if len(p) > 1 and p[0] == "0":
            return None
        n = int(p)
        if n > 255:
            return None
        value = (value << 8) | n
    return value


def _in_net(ip: int, net: int, bits: int) -> bool:
    mask = (0xFFFFFFFF << (32 - bits)) & 0xFFFFFFFF
    return (ip & mask) == (net & mask)


def _ipv4_allowed(ip: int) -> bool:
    """Ipv4.isPrivate minus loopback and link-local: 10/8, 172.16/12,
    192.168/16, 100.64/10."""
    return (
        _in_net(ip, 10 << 24, 8)
        or _in_net(ip, (172 << 24) | (16 << 16), 12)
        or _in_net(ip, (192 << 24) | (168 << 16), 16)
        or _in_net(ip, (100 << 24) | (64 << 16), 10)
    )


def _parse_groups(s: str) -> list[int] | None:
    if not s:
        return []
    groups = []
    for g in s.split(":"):
        if not g or len(g) > 4 or "." in g:
            return None
        groups.append(int(g, 16))
    return groups


def _parse_ipv6(text: str) -> list[int] | None:
    """An RFC 4291 address (no zone id, no brackets, lower-case) as 16 bytes
    (Ipv6.parse). A trailing dotted quad must be canonical."""
    if not text or not set(text) <= _V6_CHARS:
        return None
    body = text
    tail: list[int] | None = None
    if "." in body:
        last_colon = body.rfind(":")
        if last_colon < 0:
            return None
        v4 = _parse_ipv4(body[last_colon + 1:])
        if v4 is None:
            return None
        tail = [(v4 >> 16) & 0xFFFF, v4 & 0xFFFF]
        # Keep the "::" when the dotted quad directly follows it.
        body = body[: last_colon + 1] if body[:last_colon].endswith(":") else body[:last_colon]
    wanted = 8 - (len(tail) if tail else 0)
    dbl = body.find("::")
    if dbl >= 0 and body.find("::", dbl + 1) >= 0:
        return None
    if dbl >= 0:
        head = _parse_groups(body[:dbl])
        back = _parse_groups(body[dbl + 2:])
        if head is None or back is None:
            return None
        zeros = wanted - len(head) - len(back)
        if zeros < 1:
            return None
        groups = head + [0] * zeros + back
    else:
        groups = _parse_groups(body)
        if groups is None or len(groups) != wanted:
            return None
    groups = groups + (tail or [])
    if len(groups) != 8:
        return None
    out = []
    for g in groups:
        out += [g >> 8, g & 0xFF]
    return out
