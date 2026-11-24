"""`ha_dial` (DEV-557): the server's validator must agree with portico-app's
`TailnetTarget.parseHaDial`, because a present `ha_dial` the app refuses
refuses the WHOLE enrollment.

The first three tables are portico-app's own `TailnetTargetTest` cases, copied
verbatim (core/src/test/kotlin/com/meda/portico/core/tailnet/TailnetTargetTest.kt,
origin/main 2026-10-08); keep them in step with that file. The rest are cases
where a Python port could drift from the Kotlin one (Unicode digits, zone ids,
`ipaddress`-style compression) and the property the server relies on: whatever
it emits is a fixed point, so the app reads back the same value.
"""
from __future__ import annotations

import pytest

from custom_components.portico.ha_dial import parse_ha_dial

# --- portico-app TailnetTargetTest, verbatim --------------------------------

APP_ACCEPTED = [
    "10.6.0.5:443", "10.255.255.255:1", "172.16.0.1:8123", "172.31.9.9:65535", "192.168.1.10:8123",
    "100.64.0.1:443", "100.127.255.254:443", "[fd7a:115c:a1e0::5]:443", "[fc00::1]:80",
]

APP_REFUSED = [
    "", " ", "10.6.0.5", "10.6.0.5:", ":443", "10.6.0.5:0", "10.6.0.5:65536", "10.6.0.5:0443", "10.6.0.5:44a",
    # a name needs the DNS that is missing away from home
    "haos.example.org:443", "homeassistant:8123", "ha.local:8123",
    # public, loopback, link-local
    "8.8.8.8:443", "100.128.0.1:443", "172.32.0.1:443", "127.0.0.1:443", "169.254.1.1:443",
    "[::1]:443", "[fe80::1]:443", "[2001:db8::1]:443", "[::ffff:10.6.0.5]:443",
    # non-canonical or ambiguous forms
    "010.6.0.5:443", "10.6.5:443", "0x0a.2.0.5:443", "10.6.0.5:443/path", "http://10.6.0.5:443",
    "user@10.6.0.5:443", "fd7a::1:443", "[fd7a::1]", "[fd7a::1]443", "10.6.0.5:443:1",
]


@pytest.mark.parametrize("value", APP_ACCEPTED)
def test_private_and_tailnet_addresses_with_a_port_are_accepted(value):
    assert parse_ha_dial(value) == value


@pytest.mark.parametrize("value", APP_REFUSED)
def test_anything_else_is_refused(value):
    assert parse_ha_dial(value) is None


def test_ipv6_is_lower_cased_and_the_value_is_trimmed():
    assert parse_ha_dial("  [FD7A:115C:A1E0::5]:443 ") == "[fd7a:115c:a1e0::5]:443"


# --- where a Python port could drift from the Kotlin one --------------------

@pytest.mark.parametrize("value", [
    None, 443, b"10.6.0.5:443",
    # Unicode digits: Python's str.isdigit/int() accept them, Kotlin's '0'..'9' does not.
    "١٠.2.0.5:443", "10.6.0.5:４４３",
    # zone ids: Python's ipaddress accepts them, the app's Ipv6.parse does not.
    "[fc00::1%eth0]:443", "[fe80::1%25eth0]:443",
    # the 172.16/12 and 100.64/10 edges, and the other excluded ranges
    "172.15.255.255:443", "100.63.255.255:443", "0.0.0.0:443", "255.255.255.255:443",
    "224.0.0.1:443", "192.169.0.1:443", "11.0.0.1:443",
    # IPv6 outside fc00::/7, and v4-in-v6 forms of private ranges
    "[fe00::1]:443", "[::]:443", "[::10.6.0.5]:443", "[64:ff9b::10.6.0.5]:443", "[2001:db8::1]:443",
    # malformed IPv6
    "[fc00:::1]:443", "[fc00::1::2]:443", "[fc00:12345::1]:443", "[fc00::g]:443", "[fc00:1:2:3:4:5:6:7:8]:443",
    "[fc00::010.6.0.5]:443", "[]:443", "[fc00::1]:", "[fc00::1]:+443", "[fc00::1]]:443", "[[fc00::1]:443",
    # ports
    "10.6.0.5:+443", "10.6.0.5:-1", "10.6.0.5:99999", "10.6.0.5:100000", "10.6.0.5: 443", "10.6.0.5:443 x",
    # whitespace, control characters and other separators inside
    "10.6.0.5 :443", "10.6.0.5:4\t43", "10.6.0.5:443\x00", "10.6.0.5\\:443", "10.6.0.5:443?", "10.6.0.5:443#",
    "10.6.0.5.:443", ".10.6.0.5:443", "10..0.5:443", "256.2.0.5:443", "10.6.0.5.1:443",
])
def test_cases_a_port_could_get_wrong(value):
    assert parse_ha_dial(value) is None, value


def test_the_64_character_limit_applies_after_the_trim():
    # No valid address is longer than 53 characters ("[" + six groups + a
    # dotted quad + "]:65535"), so the limit only bounds what is parsed; it
    # counts the TRIMMED text, as the app's does.
    longest = "[fd7a:115c:a1e0:0000:0000:0000:255.255.255.255]:65535"
    assert len(longest) == 53
    assert parse_ha_dial(longest) == longest  # a ULA; the tail is just its last 32 bits
    uncompressed = "[fc00:0000:0000:0000:0000:0000:0000:0001]:443"
    assert parse_ha_dial(uncompressed) == uncompressed  # never re-compressed
    assert parse_ha_dial(" " * 40 + longest + " " * 40) == longest
    assert parse_ha_dial("1" * 65) is None
    assert parse_ha_dial("10.6.0.5:443" + "0" * 53) is None


@pytest.mark.parametrize(("value", "expected"), [
    ("[FC00::1]:80", "[fc00::1]:80"),
    ("[fd7a:115c:a1e0::10.6.0.5]:443", "[fd7a:115c:a1e0::10.6.0.5]:443"),  # v4 tail kept as written
    ("[fd7a:0:0::5]:443", "[fd7a:0:0::5]:443"),  # never re-compressed
    ("\t10.6.0.5:443\n", "10.6.0.5:443"),
])
def test_the_canonical_form_is_the_apps(value, expected):
    assert parse_ha_dial(value) == expected


@pytest.mark.parametrize("value", APP_ACCEPTED + [
    "[FD7A:115C:A1E0::5]:443", " 192.168.1.10:8123 ", "[fd7a:115c:a1e0::10.6.0.5]:443",
])
def test_what_the_server_emits_reads_back_unchanged(value):
    """The server only ever puts parse_ha_dial's OUTPUT in the QR; the app
    re-parses it, so the output must be a fixed point with no whitespace."""
    out = parse_ha_dial(value)
    assert out is not None
    assert parse_ha_dial(out) == out
    assert out == out.strip() and not any(c.isspace() for c in out)
