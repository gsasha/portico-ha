//! The parts of Go's `net/netip` and `net` that `config.go` leans on, with Go's error texts:
//! `netip.ParseAddr`, `netip.ParsePrefix`, `Prefix.Masked/Contains/String`, and a
//! `net.ResolveUDPAddr("udp", s)` reduced to what the loopback rule needs (the IP, if any).

use crate::gostd::net::split_host_port;
use crate::gostd::strconv::{go_atoi, go_quote};

/// A `netip.Addr`: 4 or 16 bytes plus an IPv6 zone.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Addr {
    bytes: [u8; 16],
    is4: bool,
    zone: String,
}

fn addr_err(input: &str, msg: &str, at: &str) -> String {
    if at.is_empty() {
        format!("ParseAddr({}): {msg}", go_quote(input))
    } else {
        format!(
            "ParseAddr({}): {msg} (at {})",
            go_quote(input),
            go_quote(at)
        )
    }
}

/// `parseIPv4Fields(in, off, end, fields)`.
fn parse_ipv4_fields(input: &str, off: usize, end: usize, fields: &mut [u8]) -> Result<(), String> {
    let s = &input.as_bytes()[off..end];
    let (mut val, mut pos, mut dig_len) = (0i32, 0usize, 0usize);
    let at_from = |i: usize| -> String { String::from_utf8_lossy(&s[i..]).into_owned() };
    for (i, &c) in s.iter().enumerate() {
        if c.is_ascii_digit() {
            if dig_len == 1 && val == 0 {
                return Err(addr_err(
                    input,
                    "IPv4 field has octet with leading zero",
                    "",
                ));
            }
            val = val * 10 + i32::from(c - b'0');
            dig_len += 1;
            if val > 255 {
                return Err(addr_err(input, "IPv4 field has value >255", ""));
            }
        } else if c == b'.' {
            if i == 0 || i == s.len() - 1 || s[i - 1] == b'.' {
                return Err(addr_err(
                    input,
                    "IPv4 field must have at least one digit",
                    &at_from(i),
                ));
            }
            if pos == 3 {
                return Err(addr_err(input, "IPv4 address too long", ""));
            }
            fields[pos] = val as u8;
            pos += 1;
            val = 0;
            dig_len = 0;
        } else {
            return Err(addr_err(input, "unexpected character", &at_from(i)));
        }
    }
    if pos < 3 {
        return Err(addr_err(input, "IPv4 address too short", ""));
    }
    fields[3] = val as u8;
    Ok(())
}

fn parse_ipv6(input: &str) -> Result<Addr, String> {
    let mut s = input;
    let mut zone = "";
    if let Some(i) = s.find('%') {
        zone = &s[i + 1..];
        s = &s[..i];
        if zone.is_empty() {
            return Err(addr_err(input, "zone must be a non-empty string", ""));
        }
    }
    let mk = |ip: [u8; 16]| Addr {
        bytes: ip,
        is4: false,
        zone: zone.to_string(),
    };
    let mut ip = [0u8; 16];
    let mut ellipsis: i32 = -1;
    if s.len() >= 2 && s.starts_with("::") {
        ellipsis = 0;
        s = &s[2..];
        if s.is_empty() {
            return Ok(mk(ip));
        }
    }
    let mut i = 0usize;
    while i < 16 {
        let sb = s.as_bytes();
        let mut off = 0usize;
        let mut acc: u32 = 0;
        while off < sb.len() {
            let c = sb[off];
            if c.is_ascii_digit() {
                acc = (acc << 4) + u32::from(c - b'0');
            } else if (b'a'..=b'f').contains(&c) {
                acc = (acc << 4) + u32::from(c - b'a' + 10);
            } else if (b'A'..=b'F').contains(&c) {
                acc = (acc << 4) + u32::from(c - b'A' + 10);
            } else {
                break;
            }
            if off > 3 {
                return Err(addr_err(input, "each group must have 4 or less digits", s));
            }
            if acc > 0xffff {
                return Err(addr_err(input, "IPv6 field has value >=2^16", s));
            }
            off += 1;
        }
        if off == 0 {
            return Err(addr_err(
                input,
                "each colon-separated field must have at least one digit",
                s,
            ));
        }
        if off < sb.len() && sb[off] == b'.' {
            if ellipsis < 0 && i != 12 {
                return Err(addr_err(
                    input,
                    "embedded IPv4 address must replace the final 2 fields of the address",
                    s,
                ));
            }
            if i + 4 > 16 {
                return Err(addr_err(
                    input,
                    "too many hex fields to fit an embedded IPv4 at the end of the address",
                    s,
                ));
            }
            let mut end = input.len();
            if !zone.is_empty() {
                end -= zone.len() + 1;
            }
            parse_ipv4_fields(input, end - s.len(), end, &mut ip[i..i + 4])?;
            s = "";
            i += 4;
            break;
        }
        ip[i] = (acc >> 8) as u8;
        ip[i + 1] = acc as u8;
        i += 2;
        s = &s[off..];
        if s.is_empty() {
            break;
        }
        if !s.starts_with(':') {
            return Err(addr_err(input, "unexpected character, want colon", s));
        } else if s.len() == 1 {
            return Err(addr_err(
                input,
                "colon must be followed by more characters",
                s,
            ));
        }
        s = &s[1..];
        if s.starts_with(':') {
            if ellipsis >= 0 {
                return Err(addr_err(input, "multiple :: in address", s));
            }
            ellipsis = i as i32;
            s = &s[1..];
            if s.is_empty() {
                break;
            }
        }
    }
    if !s.is_empty() {
        return Err(addr_err(input, "trailing garbage after address", s));
    }
    if i < 16 {
        if ellipsis < 0 {
            return Err(addr_err(input, "address string too short", ""));
        }
        let n = 16 - i;
        let e = ellipsis as usize;
        let mut j = i;
        while j > e {
            j -= 1;
            ip[j + n] = ip[j];
        }
        for b in &mut ip[e..e + n] {
            *b = 0;
        }
    } else if ellipsis >= 0 {
        return Err(addr_err(
            input,
            "the :: must expand to at least one field of zeros",
            "",
        ));
    }
    Ok(mk(ip))
}

/// `netip.ParseAddr`.
pub fn parse_addr(s: &str) -> Result<Addr, String> {
    for c in s.bytes() {
        match c {
            b'.' => {
                let mut f = [0u8; 4];
                parse_ipv4_fields(s, 0, s.len(), &mut f)?;
                let mut bytes = [0u8; 16];
                bytes[..4].copy_from_slice(&f);
                return Ok(Addr {
                    bytes,
                    is4: true,
                    zone: String::new(),
                });
            }
            b':' => return parse_ipv6(s),
            b'%' => return Err(addr_err(s, "missing IPv6 address", "")),
            _ => {}
        }
    }
    Err(addr_err(s, "unable to parse IP", ""))
}

impl Addr {
    /// `Addr.Unmap`: an IPv4-mapped IPv6 address becomes IPv4 (and loses any zone).
    pub fn unmap(&self) -> Addr {
        if !self.is4
            && self.bytes[..10] == [0; 10]
            && self.bytes[10] == 0xff
            && self.bytes[11] == 0xff
        {
            let mut b = [0u8; 16];
            b[..4].copy_from_slice(&self.bytes[12..16]);
            return Addr {
                bytes: b,
                is4: true,
                zone: String::new(),
            };
        }
        self.clone()
    }

    pub fn bit_len(&self) -> u8 {
        if self.is4 {
            32
        } else {
            128
        }
    }

    pub fn is4(&self) -> bool {
        self.is4
    }

    /// The IPv6 zone (`fe80::1%eth0` has `eth0`), empty for none.
    pub fn zone(&self) -> &str {
        &self.zone
    }

    /// The same address as a `std::net::IpAddr` (the zone is dropped).
    pub fn to_ip(&self) -> std::net::IpAddr {
        if self.is4 {
            std::net::IpAddr::V4(std::net::Ipv4Addr::new(
                self.bytes[0],
                self.bytes[1],
                self.bytes[2],
                self.bytes[3],
            ))
        } else {
            std::net::IpAddr::V6(std::net::Ipv6Addr::from(self.bytes))
        }
    }

    /// 4 bytes for IPv4, else 16.
    pub fn octets(&self) -> &[u8] {
        if self.is4 {
            &self.bytes[..4]
        } else {
            &self.bytes
        }
    }

    /// `netip.AddrFromSlice`: 4 or 16 bytes.
    pub fn from_slice(b: &[u8]) -> Option<Addr> {
        match b.len() {
            4 => {
                let mut bytes = [0u8; 16];
                bytes[..4].copy_from_slice(b);
                Some(Addr {
                    bytes,
                    is4: true,
                    zone: String::new(),
                })
            }
            16 => {
                let mut bytes = [0u8; 16];
                bytes.copy_from_slice(b);
                Some(Addr {
                    bytes,
                    is4: false,
                    zone: String::new(),
                })
            }
            _ => None,
        }
    }

    fn without_zone(&self) -> Addr {
        Addr {
            zone: String::new(),
            ..self.clone()
        }
    }

    /// `Addr.String()` without the zone (prefixes never carry one).
    fn text(&self) -> String {
        if self.is4 {
            let b = &self.bytes;
            return format!("{}.{}.{}.{}", b[0], b[1], b[2], b[3]);
        }
        let b = &self.bytes;
        let g: Vec<u16> = (0..8)
            .map(|i| u16::from(b[2 * i]) << 8 | u16::from(b[2 * i + 1]))
            .collect();
        // IPv4-mapped: ::ffff:a.b.c.d
        if g[..5] == [0; 5] && g[5] == 0xffff {
            return format!("::ffff:{}.{}.{}.{}", b[12], b[13], b[14], b[15]);
        }
        // the longest run (>= 2) of zero groups, the first one on a tie
        let (mut zi, mut zl) = (usize::MAX, 0usize);
        let mut i = 0;
        while i < 8 {
            if g[i] == 0 {
                let mut j = i;
                while j < 8 && g[j] == 0 {
                    j += 1;
                }
                if j - i > zl && j - i >= 2 {
                    zi = i;
                    zl = j - i;
                }
                i = j;
            } else {
                i += 1;
            }
        }
        let mut s = String::new();
        let mut i = 0;
        while i < 8 {
            if i == zi {
                s.push_str("::");
                i += zl;
                continue;
            }
            if i > 0 && !s.ends_with(':') {
                s.push(':');
            }
            s.push_str(&format!("{:x}", g[i]));
            i += 1;
        }
        s
    }
}

/// A `netip.Prefix`.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Prefix {
    addr: Addr,
    bits: u8,
}

impl Prefix {
    /// `netip.PrefixFrom`: the zone is dropped.
    pub fn from(addr: &Addr, bits: u8) -> Prefix {
        Prefix {
            addr: addr.without_zone(),
            bits,
        }
    }

    pub fn bits(&self) -> u8 {
        self.bits
    }

    /// `Prefix.Masked`.
    pub fn masked(&self) -> Prefix {
        let mut a = self.addr.clone();
        let n = a.octets().len();
        for (i, byte) in a.bytes.iter_mut().enumerate().take(n) {
            let keep = i32::from(self.bits) - 8 * i as i32;
            *byte &= match keep {
                k if k >= 8 => 0xff,
                k if k <= 0 => 0x00,
                k => 0xffu8 << (8 - k),
            };
        }
        Prefix {
            addr: a,
            bits: self.bits,
        }
    }

    /// `Prefix.Contains`: an IPv4 address never matches an IPv6 prefix (mapped included) and
    /// an address with a zone never matches.
    pub fn contains(&self, ip: &Addr) -> bool {
        if !ip.zone.is_empty() || self.addr.is4 != ip.is4 {
            return false;
        }
        let n = usize::from(self.bits);
        let (a, b) = (self.addr.octets(), ip.octets());
        let full = n / 8;
        if a[..full] != b[..full] {
            return false;
        }
        let rem = n % 8;
        if rem == 0 {
            return true;
        }
        let mask = 0xffu8 << (8 - rem);
        (a[full] ^ b[full]) & mask == 0
    }
}

impl std::fmt::Display for Prefix {
    /// `Prefix.String()`.
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}/{}", self.addr.text(), self.bits)
    }
}

fn prefix_err(input: &str, msg: &str) -> String {
    format!("netip.ParsePrefix({}): {msg}", go_quote(input))
}

/// `netip.ParsePrefix`.
pub fn parse_prefix(s: &str) -> Result<Prefix, String> {
    let Some(i) = s.rfind('/') else {
        return Err(prefix_err(s, "no '/'"));
    };
    let ip = parse_addr(&s[..i]).map_err(|e| prefix_err(s, &e))?;
    if !ip.is4 && !ip.zone.is_empty() {
        return Err(prefix_err(s, "IPv6 zones cannot be present in a prefix"));
    }
    let bits_str = &s[i + 1..];
    let bad = || prefix_err(s, &format!("bad bits after slash: {}", go_quote(bits_str)));
    if bits_str.len() > 1 && !matches!(bits_str.as_bytes()[0], b'1'..=b'9') {
        return Err(bad());
    }
    let Ok(bits) = go_atoi(bits_str) else {
        return Err(bad());
    };
    let max = if ip.is4 { 32 } else { 128 };
    if !(0..=max).contains(&bits) {
        return Err(prefix_err(s, "prefix length out of range"));
    }
    Ok(Prefix::from(&ip, bits as u8))
}

/// Go's `parsePort`: `(port, needs_lookup)`; an empty string is port 0; a sign is allowed; a
/// huge number is capped (so it fails the range check later).
fn parse_port(service: &str) -> (i64, bool) {
    if service.is_empty() {
        return (0, false);
    }
    const MAX: u32 = u32::MAX;
    const CUTOFF: u32 = 1 << 30;
    let (neg, digits) = match service.as_bytes()[0] {
        b'+' => (false, &service[1..]),
        b'-' => (true, &service[1..]),
        _ => (false, service),
    };
    let mut n: u32 = 0;
    for d in digits.chars() {
        let Some(d) = d.to_digit(10) else {
            return (0, true);
        };
        if n >= CUTOFF {
            n = MAX;
            break;
        }
        n = n.wrapping_mul(10); // uint32 arithmetic: Go wraps here, then checks `nn < n`
        let nn = n.wrapping_add(d);
        if nn < n {
            n = MAX;
            break;
        }
        n = nn;
    }
    let port = if !neg && n >= CUTOFF {
        i64::from(CUTOFF - 1)
    } else if neg && n > CUTOFF {
        i64::from(CUTOFF)
    } else {
        i64::from(n)
    };
    (if neg { -port } else { port }, false)
}

/// `Resolver.LookupPort("udp", service)`. Names come from Go's built-in table only (`domain`);
/// Go also reads `/etc/services`, which this does not.
fn lookup_port(service: &str) -> Result<i64, String> {
    let (port, needs_lookup) = parse_port(service);
    let port = if needs_lookup {
        match service.to_ascii_lowercase().as_str() {
            "domain" => 53,
            _ => return Err(format!("lookup udp/{service}: unknown port")),
        }
    } else {
        port
    };
    if !(0..=65535).contains(&port) {
        return Err(format!("address {service}: invalid port"));
    }
    Ok(port)
}

/// `isDomainName` (net/dnsclient.go).
fn is_domain_name(s: &str) -> bool {
    if s == "." {
        return true;
    }
    let b = s.as_bytes();
    let l = b.len();
    if l == 0 || l > 254 || (l == 254 && b[l - 1] != b'.') {
        return false;
    }
    let mut last = b'.';
    let mut non_numeric = false;
    let mut partlen = 0;
    for &c in b {
        match c {
            b'a'..=b'z' | b'A'..=b'Z' | b'_' => {
                non_numeric = true;
                partlen += 1;
            }
            b'0'..=b'9' => partlen += 1,
            b'-' => {
                if last == b'.' {
                    return false;
                }
                partlen += 1;
                non_numeric = true;
            }
            b'.' => {
                if last == b'.' || last == b'-' {
                    return false;
                }
                if partlen > 63 || partlen == 0 {
                    return false;
                }
                partlen = 0;
            }
            _ => return false,
        }
        last = c;
    }
    if last == b'-' || partlen > 63 {
        return false;
    }
    non_numeric
}

/// One to four dot-separated numbers in decimal, `0x` hex or leading-zero octal: the forms
/// `inet_aton` accepts.
fn is_inet_aton(host: &str) -> bool {
    let labels: Vec<&str> = host.split('.').collect();
    if labels.len() > 4 {
        return false;
    }
    labels.iter().all(|l| {
        if let Some(h) = l.strip_prefix("0x").or_else(|| l.strip_prefix("0X")) {
            h.is_empty() || h.bytes().all(|c| c.is_ascii_hexdigit())
        } else {
            !l.is_empty() && l.bytes().all(|c| c.is_ascii_digit())
        }
    })
}

/// What `ResolveUDPAddr` yields that `parseConfig` looks at.
#[derive(Debug, PartialEq, Eq)]
pub struct UdpAddr {
    /// `None` for an empty host (Go's nil IP).
    pub ip: Option<Addr>,
    /// The port (`0` for an empty string or an empty port).
    pub port: u16,
}

impl UdpAddr {
    /// `addr.IP != nil && addr.IP.IsLoopback()`: IPv4 `127/8`, or `::1`; an IPv4-mapped
    /// IPv6 address counts as the IPv4 it maps.
    pub fn is_loopback(&self) -> bool {
        let Some(ip) = &self.ip else { return false };
        let ip = ip.unmap();
        if ip.is4 {
            return ip.bytes[0] == 127;
        }
        let mut lo = [0u8; 16];
        lo[15] = 1;
        ip.bytes == lo
    }
}

/// `net.ResolveUDPAddr("udp", s)`. A host that is not an IP literal is resolved through the
/// system resolver (blocking: call it off the async runtime); the first IPv4 address wins, else
/// the first one. A name that cannot be resolved reports `lookup NAME: no such host` (Go adds
/// ` on <server>` when it asked a DNS server; not reproduced).
pub fn resolve_udp_addr(s: &str) -> Result<UdpAddr, String> {
    if s.is_empty() {
        return Ok(UdpAddr { ip: None, port: 0 });
    }
    let (host, port) = split_host_port(s)?;
    let port = lookup_port(port)? as u16;
    if host.is_empty() {
        return Ok(UdpAddr { ip: None, port });
    }
    if let Ok(a) = parse_addr(host) {
        return Ok(UdpAddr { ip: Some(a), port });
    }
    let no_host = || format!("lookup {host}: no such host");
    if !is_domain_name(host) {
        return Err(no_host());
    }
    // glibc's getaddrinfo also takes the old inet_aton shapes (`127.1`, `0x7f.1`, `2130706433`,
    // `0177.0.0.1`); Go's resolver does not, so they are refused here, never resolved.
    if is_inet_aton(host) {
        return Err(no_host());
    }
    use std::net::ToSocketAddrs;
    let addrs: Vec<std::net::IpAddr> = (host, 0u16)
        .to_socket_addrs()
        .map_err(|_| no_host())?
        .map(|a| a.ip())
        .collect();
    let pick = addrs
        .iter()
        .find(|a| a.is_ipv4())
        .or_else(|| addrs.first())
        .ok_or_else(no_host)?;
    let bytes = match pick {
        std::net::IpAddr::V4(v) => v.octets().to_vec(),
        std::net::IpAddr::V6(v) => v.octets().to_vec(),
    };
    Ok(UdpAddr {
        ip: Addr::from_slice(&bytes),
        port,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn inet_aton_shapes_are_not_resolved() {
        for h in [
            "0x7f.1",
            "0x7f.0.0.1",
            "127.1",
            "127.0.1",
            "2130706433",
            "0177.0.0.1",
            "0x7f000001",
        ] {
            let e = resolve_udp_addr(&format!("{h}:5060")).unwrap_err();
            assert_eq!(e, format!("lookup {h}: no such host"), "{h}");
        }
        // a real dotted quad and a literal are still literals
        assert!(resolve_udp_addr("127.0.0.1:5060").unwrap().is_loopback());
        assert!(resolve_udp_addr("localhost:5060").unwrap().is_loopback());
    }
}
