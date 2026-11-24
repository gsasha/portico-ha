//! Which local addresses the session offers as host candidates (S1b port condition (d)).
//!
//! pion gathers from every up interface (`SetIncludeLoopbackCandidate` is off unless a test asks)
//! and puts them all into the non-trickle answer. str0m has no gathering at all, so the port owns
//! the list, and a list that is simply "everything" would publish docker bridges and loopback to
//! the browser. The rules, in order:
//!
//! * loopback is skipped unless the test seam asks (`include_loopback`);
//! * unspecified, multicast, IPv4 link-local (169.254/16) and IPv6 link-local (fe80::/10, which
//!   needs a scope id and which browsers do not offer) are always skipped;
//! * an interface whose name starts with one of `exclude_prefixes` is skipped. The default list
//!   names the usual container and virtualisation bridges: `docker`, `br-`, `veth`, `virbr`,
//!   `cni`, `flannel`, `cali`, `lxcbr`, `lxdbr`, `podman`;
//! * **tailnet addresses are kept** (`100.64.0.0/10`, `fd7a:115c:a1e0::/48`), whatever the
//!   interface is called. The Python side and the phones reach the transport over the tailnet
//!   (design doc section 4.8), so a candidate on `tailscale0` is the one that matters most when
//!   the browser is away from home.
//!
//! An interface that is administratively down is NOT skipped (pion skips one without `FlagUp`):
//! `if-addrs` 0.13.4 returns only the address, name and index of an interface, no flags. The
//! list is capped at [`MAX_ADDRESSES`] instead.
//!
//! The patterns are configurable through `PORTICO_SFU_EXCLUDE_IFACES` (comma-separated name
//! prefixes; the empty string means "exclude none").

use std::net::{IpAddr, Ipv4Addr, Ipv6Addr};

/// Name prefixes of interfaces that never get a candidate by default.
pub const DEFAULT_EXCLUDE_PREFIXES: &[&str] = &[
    "docker", "br-", "veth", "virbr", "cni", "flannel", "cali", "lxcbr", "lxdbr", "podman",
];

#[derive(Clone, Debug)]
pub struct IfacePolicy {
    /// Keep loopback addresses (127/8, ::1). Off in production; the test seam.
    pub include_loopback: bool,
    pub exclude_prefixes: Vec<String>,
    /// When set, only these addresses are used (a test seam: pins a session to one address).
    pub only: Option<Vec<IpAddr>>,
}

impl Default for IfacePolicy {
    fn default() -> Self {
        IfacePolicy {
            include_loopback: false,
            exclude_prefixes: DEFAULT_EXCLUDE_PREFIXES
                .iter()
                .map(|s| s.to_string())
                .collect(),
            only: None,
        }
    }
}

/// 100.64.0.0/10 (CGNAT, Tailscale's range) or fd7a:115c:a1e0::/48.
pub fn is_tailnet(ip: IpAddr) -> bool {
    match ip {
        IpAddr::V4(a) => a.octets()[0] == 100 && (a.octets()[1] & 0xc0) == 64,
        IpAddr::V6(a) => a.segments()[..3] == [0xfd7a, 0x115c, 0xa1e0],
    }
}

fn is_link_local(ip: IpAddr) -> bool {
    match ip {
        IpAddr::V4(a) => a.octets()[0] == 169 && a.octets()[1] == 254,
        IpAddr::V6(a) => (a.segments()[0] & 0xffc0) == 0xfe80,
    }
}

fn never(ip: IpAddr) -> bool {
    match ip {
        IpAddr::V4(a) => a == Ipv4Addr::UNSPECIFIED || a.is_multicast() || a.is_broadcast(),
        IpAddr::V6(a) => a == Ipv6Addr::UNSPECIFIED || a.is_multicast(),
    }
}

/// The most local addresses a session binds (a host has a handful; every one is a socket and a
/// reader task for each live session).
pub const MAX_ADDRESSES: usize = 16;

impl IfacePolicy {
    /// The test seam: loopback only, no other interface.
    pub fn loopback_only() -> Self {
        IfacePolicy {
            include_loopback: true,
            exclude_prefixes: Vec::new(),
            only: Some(vec![IpAddr::V4(Ipv4Addr::LOCALHOST)]),
        }
    }

    /// Reads `PORTICO_SFU_EXCLUDE_IFACES` (prefix list) and `PORTICO_SFU_INCLUDE_LOOPBACK`
    /// (on: `1`, `true`, `yes`; off: `0`, `false`, `no` or empty; any other text is reported as
    /// "not usable" and the default stays) from the environment; anything unset keeps the default.
    pub fn from_env(getenv: impl Fn(&str) -> Option<String>) -> Self {
        Self::from_env_with(getenv, |k, v| {
            crate::panic_hook::err_line(&format!(
                "portico-transport: {k}={v:?} is not usable, using the default"
            ));
        })
    }

    /// `from_env`, telling `bad` about a value that is not usable (the default stays): the
    /// same validation style as every other `PORTICO_SFU_*` value.
    pub fn from_env_with(
        getenv: impl Fn(&str) -> Option<String>,
        bad: impl Fn(&str, &str),
    ) -> Self {
        let mut p = IfacePolicy::default();
        if let Some(v) = getenv("PORTICO_SFU_EXCLUDE_IFACES") {
            p.exclude_prefixes = v
                .split(',')
                .map(str::trim)
                .filter(|s| !s.is_empty())
                .map(str::to_string)
                .collect();
        }
        if let Some(v) = getenv("PORTICO_SFU_INCLUDE_LOOPBACK") {
            match v.trim() {
                "1" | "true" | "yes" => p.include_loopback = true,
                "0" | "false" | "no" | "" => {}
                _ => bad("PORTICO_SFU_INCLUDE_LOOPBACK", &v),
            }
        }
        p
    }

    /// Whether `ip` on interface `name` gets a host candidate.
    pub fn allows(&self, name: &str, ip: IpAddr) -> bool {
        if let Some(only) = &self.only {
            return only.contains(&ip);
        }
        if never(ip) || is_link_local(ip) {
            return false;
        }
        if ip.is_loopback() {
            return self.include_loopback;
        }
        if is_tailnet(ip) {
            return true;
        }
        !self
            .exclude_prefixes
            .iter()
            .any(|pre| name.starts_with(pre.as_str()))
    }

    /// The allowed (interface, address) pairs out of an enumeration, in input order, without
    /// duplicates.
    pub fn select(&self, all: &[(String, IpAddr)]) -> Vec<(String, IpAddr)> {
        let mut out: Vec<(String, IpAddr)> = Vec::new();
        for (n, ip) in all {
            // one socket and one reader task per address per session: the list is capped
            if out.len() >= MAX_ADDRESSES {
                break;
            }
            if self.allows(n, *ip) && !out.iter().any(|(_, o)| o == ip) {
                out.push((n.clone(), *ip));
            }
        }
        out
    }
}

/// The addresses of the interfaces of this host (`getifaddrs`).
pub fn enumerate() -> Vec<(String, IpAddr)> {
    if_addrs::get_if_addrs()
        .unwrap_or_default()
        .into_iter()
        .map(|i| (i.name.clone(), i.ip()))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ip(s: &str) -> IpAddr {
        s.parse().unwrap()
    }

    #[test]
    fn default_policy_skips_loopback_bridges_and_link_local_keeps_lan_and_tailnet() {
        let p = IfacePolicy::default();
        let all = vec![
            ("lo".to_string(), ip("127.0.0.1")),
            ("lo".to_string(), ip("::1")),
            ("eth0".to_string(), ip("10.4.0.2")),
            ("eth0".to_string(), ip("2a0d:6fc0:231c:7b06::1")),
            ("eth0".to_string(), ip("fe80::1")),
            ("eth0".to_string(), ip("169.254.3.4")),
            ("docker0".to_string(), ip("172.28.0.1")),
            ("br-a78a7f8b6c31".to_string(), ip("172.28.1.1")),
            ("veth12ab".to_string(), ip("172.17.0.9")),
            ("tailscale0".to_string(), ip("100.101.102.103")),
            ("tailscale0".to_string(), ip("fd7a:115c:a1e0::1234")),
            ("docker0".to_string(), ip("100.64.0.9")),
            ("eth1".to_string(), ip("0.0.0.0")),
            ("eth1".to_string(), ip("224.0.0.1")),
        ];
        let got: Vec<String> = p.select(&all).iter().map(|(_, a)| a.to_string()).collect();
        assert_eq!(
            got,
            vec![
                "10.4.0.2",
                "2a0d:6fc0:231c:7b06::1",
                "100.101.102.103",
                "fd7a:115c:a1e0::1234",
                "100.64.0.9"
            ]
        );
    }

    // R1-80: the address list is capped
    #[test]
    fn the_list_of_addresses_is_capped() {
        let all: Vec<(String, IpAddr)> = (1..=40)
            .map(|i| ("eth0".to_string(), ip(&format!("192.168.1.{i}"))))
            .collect();
        let got = IfacePolicy::default().select(&all);
        assert_eq!(got.len(), MAX_ADDRESSES);
        assert_eq!(got[0].1, ip("192.168.1.1"));
    }

    // R1-163: an unusable value is named like every other PORTICO_SFU_* one
    #[test]
    fn an_unusable_include_loopback_value_is_reported_and_ignored() {
        use std::cell::RefCell;
        let seen = RefCell::new(Vec::new());
        let env = |v: &'static str| {
            move |k: &str| (k == "PORTICO_SFU_INCLUDE_LOOPBACK").then(|| v.to_string())
        };
        for (v, want, bad) in [
            ("1", true, false),
            ("true", true, false),
            ("0", false, false),
            ("on", false, true),
            ("2", false, true),
        ] {
            seen.borrow_mut().clear();
            let p = IfacePolicy::from_env_with(env(v), |k, val| {
                seen.borrow_mut().push(format!("{k}={val}"))
            });
            assert_eq!(p.include_loopback, want, "{v}");
            assert_eq!(!seen.borrow().is_empty(), bad, "{v}: {:?}", seen.borrow());
        }
    }

    #[test]
    fn tailnet_ranges_are_exact() {
        assert!(is_tailnet(ip("100.64.0.0")));
        assert!(is_tailnet(ip("100.127.255.255")));
        assert!(!is_tailnet(ip("100.63.255.255")));
        assert!(!is_tailnet(ip("100.128.0.0")));
        assert!(is_tailnet(ip("fd7a:115c:a1e0:ab12::1")));
        assert!(!is_tailnet(ip("fd7a:115c:a1e1::1")));
    }

    #[test]
    fn loopback_only_when_the_seam_asks() {
        let all = vec![
            ("lo".to_string(), ip("127.0.0.1")),
            ("eth0".to_string(), ip("10.4.0.2")),
        ];
        assert_eq!(IfacePolicy::default().select(&all).len(), 1);
        let p = IfacePolicy {
            include_loopback: true,
            ..IfacePolicy::default()
        };
        assert_eq!(p.select(&all).len(), 2);
        let l = IfacePolicy::loopback_only();
        let got = l.select(&all);
        assert_eq!(got, vec![("lo".to_string(), ip("127.0.0.1"))]);
    }

    #[test]
    fn env_configures_the_patterns() {
        let env = |k: &str| match k {
            "PORTICO_SFU_EXCLUDE_IFACES" => Some("wg, eth1".to_string()),
            "PORTICO_SFU_INCLUDE_LOOPBACK" => Some("1".to_string()),
            _ => None,
        };
        let p = IfacePolicy::from_env(env);
        assert!(p.include_loopback);
        assert!(!p.allows("wg0", ip("10.9.0.1")));
        assert!(!p.allows("eth1", ip("10.9.0.2")));
        assert!(p.allows("docker0", ip("172.28.0.1"))); // the default list is replaced, not extended
        let none = IfacePolicy::from_env(|k| (k == "PORTICO_SFU_EXCLUDE_IFACES").then(String::new));
        assert!(none.exclude_prefixes.is_empty());
        let d = IfacePolicy::from_env(|_| None);
        assert!(!d.allows("docker0", ip("172.28.0.1")));
    }
}
