#!/usr/bin/env python3
"""E4 preflight for Portico's SIP-server mode (DEV-518 W6). OFFLINE and read-only.

It reads ONE config (a JSON file of the `gate_sip_server` entry's data, or flags) and prints which E4 gates are closed,
then the list of manual checks for the cut-over window. It opens no socket, binds nothing, resolves no name, contacts
neither the station, gate-sip, HA nor UniFi, and runs none of the commands it prints (a human does, in the window).

  python3 scripts/e4_preflight.py --config entry.json
  python3 scripts/e4_preflight.py --host 10.6.0.5 --sip-port 5060 --rtp-port 45000 --allow-from 10.8.0.39 \
      --allow-non-loopback --media-range 20000-20099 --station-ip 10.8.0.39 --observer-url http://10.6.0.7:8091

Config keys (the entry data, see custom_components/portico/door/drivers/gate_sip_server.py): sip_host, sip_port,
rtp_port, allow_non_loopback, allow_from, password (only its length is looked at, never printed); plus, for this tool:
station_ip (default 10.8.0.39), media_range ("20000-20099"; the station may choose any media port), observer_url
(the observer entry's gate_sip_url), other_entries (a list of other Portico entries' data, for port clashes).

Exit status: 0 no gate closed, 1 at least one FAIL (the entry would be refused or is a mistake), 2 unusable input.
The locked-port rules are mirrored from gate_sip_server.py (W2); tests/test_e4_preflight.py fails if they drift.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import sys
from urllib.parse import urlsplit

GATE_SIP_PORTS = frozenset({5060, 40000, 8091, 8092, 8095})
LOW_PORT_LIMIT = 1024
MIN_PASSWORD, MAX_PASSWORD = 8, 15
STATION_IP = "10.8.0.39"
HA_HOST = "10.6.0.5"
GATE_SIP_HOST = "10.6.0.7"
# Reserved for local Docker on agent-host (CLAUDE.md "Local Docker" rule): never a docker network/gateway address.
RESERVED_ESTATE = ipaddress.ip_network("10.0.0.0/8")
RESERVED_DOCKER_INTERNAL = (ipaddress.ip_network("172.30.32.0/23"), ipaddress.ip_network("172.30.42.0/24"))
MAX_MEDIA_SPAN = 1000

OK, WARN, FAIL = "OK  ", "WARN", "FAIL"


def is_locked_port(port: int) -> bool:
    return port < LOW_PORT_LIMIT or port in GATE_SIP_PORTS


def parse_media_range(raw: str) -> tuple[int, int]:
    lo, _, hi = str(raw).partition("-")
    lo_i, hi_i = int(lo), int(hi or lo)
    if not (1 <= lo_i <= hi_i <= 65535):
        raise ValueError("range out of order or out of 1..65535")
    return lo_i, hi_i


def _port(value, field: str, out: list):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        out.append((FAIL, f"{field} {value!r} is not an integer in 1..65535"))
        return None
    return value


def observer_host(url) -> str | None:
    try:
        return (urlsplit(url or "").hostname or "").lower() or None
    except ValueError:
        return None


def check(cfg: dict) -> list[tuple[str, str]]:
    """Return [(level, message)]. Pure; no I/O."""
    out: list[tuple[str, str]] = []
    # --- bind address
    raw_host = str(cfg.get("sip_host", "127.0.0.1")).strip().strip("[]")
    try:
        ip = ipaddress.ip_address(raw_host)
    except ValueError:
        out.append((FAIL, f"sip_host {raw_host!r} is not an IP literal (a name would need DNS; the form refuses it)"))
        return out
    unlocked = bool(cfg.get("allow_non_loopback")) and bool(cfg.get("allow_from"))
    if ip.is_loopback:
        out.append((WARN, "bound to loopback: the station can never reach this; fine for rehearsal, not for E4"))
    elif not unlocked:
        out.append((FAIL, "LAN bind needs BOTH allow_non_loopback and a non-empty allow_from (the Go driver refuses otherwise)"))
    else:
        out.append((OK, f"LAN bind {ip} unlocked (allow_non_loopback + allow_from)"))
    if ip.is_unspecified:
        out.append((WARN, "wildcard bind: listens on every interface of the host; prefer the single LAN address"))
    for net in RESERVED_DOCKER_INTERNAL:
        if ip in net:
            out.append((FAIL, f"{ip} is inside {net}, an internal Docker/Supervisor range the station cannot route to"))
    if ip in RESERVED_ESTATE and not ip.is_loopback:
        if str(ip) == HA_HOST:
            out.append((OK, f"{ip} is in 10.0.0.0/8 but is HA's own LAN address (VLAN 2), the intended bind"))
        else:
            out.append((WARN, f"{ip} is in 10.0.0.0/8: acceptable only as a real host LAN address; NEVER a docker "
                              "network or gateway address (CLAUDE.md local-Docker rule). Expected HA host: " + HA_HOST))
    # --- ports
    sip = _port(cfg.get("sip_port", 15060), "sip_port", out)
    rtp = _port(cfg.get("rtp_port", 45000), "rtp_port", out)
    if sip and rtp and sip == rtp:
        out.append((FAIL, "sip_port equals rtp_port"))
    for name, port in (("sip_port", sip), ("rtp_port", rtp)):
        if port is None:
            continue
        if is_locked_port(port):
            why = "below 1024" if port < LOW_PORT_LIMIT else "a gate-sip port (5060, 40000, 8091, 8092, 8095)"
            if unlocked:
                out.append((WARN, f"{name} {port} is locked ({why}); allowed only because the LAN bind is unlocked"))
            else:
                out.append((FAIL, f"{name} {port} is locked ({why}) and the LAN bind is not unlocked: standard_port_locked"))
        else:
            out.append((OK, f"{name} {port} is not a locked port"))
    if sip == 5060:
        out.append((OK, "sip_port 5060 is what the station sends to when SIP.SIPServer has no port; it needs the unlock above"))
    elif sip:
        out.append((WARN, f"sip_port {sip} is not 5060: the station must be able to take a server port (unverified, E0)"))
    # --- conflicts with other entries (mirrors entry_conflicts)
    host_s = str(ip)
    obs = observer_host(cfg.get("observer_url"))
    if obs and (obs == host_s or (obs == "localhost" and ip.is_loopback)):
        for name, port in (("sip_port", sip), ("rtp_port", rtp)):
            if port in GATE_SIP_PORTS:
                out.append((FAIL, f"same host as the observer ({obs}) and {name} {port} is a gate-sip port: "
                                  f"the form refuses it outright (port_used_by_gate_sip), no unlock"))
    elif obs:
        out.append((OK, f"observer host {obs} differs from the bind host {host_s}: not refused"))
    for other in cfg.get("other_entries") or []:
        if other.get("driver", "gate_sip_server") != "gate_sip_server":
            continue
        oh = str(other.get("sip_host", ""))
        try:
            overlap = oh == host_s or ip.is_unspecified or ipaddress.ip_address(oh).is_unspecified
        except ValueError:
            overlap = False
        if overlap and {sip, rtp} & {other.get("sip_port"), other.get("rtp_port")}:
            out.append((FAIL, "another server entry uses one of these ports on an overlapping host (port_in_use_by_entry)"))
    # --- allow_from vs the station
    station = str(cfg.get("station_ip") or STATION_IP)
    try:
        st = ipaddress.ip_address(station)
    except ValueError:
        out.append((FAIL, f"station_ip {station!r} is not an IP address"))
        st = None
    if st is not None and st not in ipaddress.ip_network("10.8.0.0/16"):
        out.append((WARN, f"station {st} is not in VLAN 4 (10.8.0.0/16); the expected station is {STATION_IP}"))
    raw_af = cfg.get("allow_from", "")
    items = raw_af if isinstance(raw_af, (list, tuple)) else str(raw_af or "").split(",")
    nets = []
    for item in (str(i).strip() for i in items):
        if not item:
            continue
        try:
            net = ipaddress.ip_network(item, strict=False)
        except ValueError:
            out.append((FAIL, f"allow_from entry {item!r} is not an IP or CIDR"))
            continue
        if net.prefixlen == 0:
            out.append((FAIL, "allow_from contains a /0 (allow everyone): refused"))
        nets.append(net)
    if not nets:
        out.append((WARN if ip.is_loopback else FAIL, "allow_from is empty"))
    elif st is not None:
        exact = [n for n in nets if n.prefixlen == n.max_prefixlen and n.network_address == st]
        if exact and len(nets) == 1:
            out.append((OK, f"allow_from is exactly the station {st}"))
        elif any(st in n for n in nets):
            out.append((WARN, "allow_from admits the station but is wider than the station address alone (plan: equal)"))
        else:
            out.append((FAIL, f"allow_from does not contain the station {st}: its REGISTER would be dropped"))
    # --- password (length only)
    pw = cfg.get("password")
    if pw is None:
        out.append((WARN, "password not in this config: only its length can be checked; it must equal GATE_SIP_PASSWORD"))
    elif not isinstance(pw, str) or not MIN_PASSWORD <= len(pw) <= MAX_PASSWORD:
        out.append((FAIL, f"password length must be {MIN_PASSWORD}-{MAX_PASSWORD} (below 8 the station sends no REGISTER at all)"))
    else:
        out.append((OK, f"password length {len(pw)} is within {MIN_PASSWORD}-{MAX_PASSWORD}"))
    # --- station media range (firewall side)
    mr = cfg.get("media_range")
    if not mr:
        out.append((WARN, "no media_range given: the firewall rule needs the station's media ports "
                          "(20002 seen once; propose 20000-20099 and confirm with the capture)"))
    else:
        try:
            lo, hi = parse_media_range(mr)
        except ValueError as err:
            out.append((FAIL, f"media_range {mr!r}: {err}"))
        else:
            if lo < LOW_PORT_LIMIT:
                out.append((FAIL, "media_range includes ports below 1024"))
            elif hi - lo + 1 > MAX_MEDIA_SPAN:
                out.append((FAIL, f"media_range spans {hi - lo + 1} ports; keep it under {MAX_MEDIA_SPAN}"))
            else:
                out.append((OK, f"media_range {lo}-{hi} ({hi - lo + 1} ports) for the firewall rule"))
            if rtp and lo <= rtp <= hi:
                out.append((WARN, "Portico rtp_port lies inside the station's media range: legal, but confusing in a capture"))
    return out


def manual_checks(cfg: dict) -> list[str]:
    host = str(cfg.get("sip_host", HA_HOST))
    sip = cfg.get("sip_port", 5060)
    rtp = cfg.get("rtp_port", 45000)
    st = str(cfg.get("station_ip") or STATION_IP)
    mr = cfg.get("media_range") or "20000-20099"
    return [
        "MANUAL CHECKS FOR THE WINDOW (nothing below was run; each is for a human, the owner present)",
        "",
        "A. Firewall (a UniFi WRITE: needs the owner's approval each time; reads are free)",
        f"   [ ] the rule in network/firewall_rules_mapping.md section 3 (PROPOSED, NOT APPLIED) is present: {st} -> {HA_HOST} "
        f"UDP {sip},{rtp}; {HA_HOST} -> {st} UDP SIP + {mr}; return paths via 2001.",
        "   [ ] it sits ahead of the VLAN 4 block rules in the first-match chain; read the live rule list back (no reliance on this doc).",
        "   [ ] read the live state first: this estate's mapping doc recorded ZERO loaded rules on 2026-08-12; if still zero the path is open already.",
        "",
        "B. UDP reachability (run FROM the named host; sending a datagram is traffic, so do these only in the window)",
        f"   [ ] HA host ({HA_HOST}) terminal: tcpdump -n -i any 'udp and host {st} and (port {sip} or port {rtp} or portrange {mr})'",
        f"       then on the station UI trigger a re-register (or wait <= 5 min): expect REGISTER {st} -> {host}:{sip}. This is the real station->Portico test.",
        f"   [ ] optional synthetic test from a VLAN 4 host (NOT the station): echo ping | nc -u -w1 {host} {sip} while the tcpdump above runs.",
        f"   [ ] HA host -> station direction: from the HA host, ping -c2 {st} (ICMP only); the INVITE and RTP legs are proven by the first real ring, not before.",
        f"   [ ] Portico side: the entry is up, bound on {host}:{sip} and rtp {rtp}, allow_from {st}; log shows 'SIP on ...'; no REGISTER yet.",
        "   [ ] agent VM (VLAN 7) is NOT a valid vantage for this: it is not on the station's path; do not infer from it.",
        "",
        "C. Station settings (gate-sip/README.md; E0 re-read live first, the owner's go-ahead)",
        f"   SIP.SIPServer and SIP.OutboundProxy : change to {host}        rollback -> {GATE_SIP_HOST} (gate-sip)",
        "   Registar[0].ServerType              : H500, unchanged         SIP.UserType 0 unchanged      SIP.IsMainVTO 1 unchanged",
        "   SIP.AuthPassword                    : 8-15 chars, equal to GATE_SIP_PASSWORD, unchanged",
        "   Local SIP Server                    : must stay OFF           SIP.UserID 8001, realm VDP, dial target 9901 unchanged",
        f"   Pre-SIP originals (rollback beyond gate-sip): ServerType VTO, UserType 2, SIPServer {st}, IsMainVTO 1",
        "   Rollback order: write setting back, reboot the station, reload the Dahua integration, wait for REGISTER at gate-sip, real bell press.",
        "   Decide at the 15-minute mark after the station reboot (portico_e3_rehearsal.md section 5).",
    ]


def load(args) -> dict:
    cfg: dict = {}
    if args.config:
        with open(args.config, encoding="utf-8") as fh:
            cfg = json.load(fh)
        if not isinstance(cfg, dict):
            raise ValueError("config file must hold one JSON object")
    mapping = {"host": "sip_host", "sip_port": "sip_port", "rtp_port": "rtp_port", "allow_from": "allow_from",
               "station_ip": "station_ip", "media_range": "media_range", "observer_url": "observer_url"}
    for flag, key in mapping.items():
        val = getattr(args, flag)
        if val is not None:
            cfg[key] = val
    if args.allow_non_loopback:
        cfg["allow_non_loopback"] = True
    return cfg


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", help="JSON file with the gate_sip_server entry data (never read from HA)")
    ap.add_argument("--host")
    ap.add_argument("--sip-port", type=int, dest="sip_port")
    ap.add_argument("--rtp-port", type=int, dest="rtp_port")
    ap.add_argument("--allow-from", dest="allow_from")
    ap.add_argument("--allow-non-loopback", action="store_true", dest="allow_non_loopback")
    ap.add_argument("--station-ip", dest="station_ip")
    ap.add_argument("--media-range", dest="media_range")
    ap.add_argument("--observer-url", dest="observer_url")
    args = ap.parse_args(argv)
    try:
        cfg = load(args)
    except (OSError, ValueError) as err:
        print(f"unusable input: {err}", file=sys.stderr)
        return 2
    findings = check(cfg)
    print("E4 PREFLIGHT (offline; no socket opened)\n")
    for level, msg in findings:
        print(f"[{level}] {msg}")
    fails = sum(1 for lv, _ in findings if lv == FAIL)
    print(f"\n{fails} gate(s) closed (FAIL)." if fails else "\nNo FAIL: the config would be accepted. WARNs are for a human to read.")
    print()
    print("\n".join(manual_checks(cfg)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
