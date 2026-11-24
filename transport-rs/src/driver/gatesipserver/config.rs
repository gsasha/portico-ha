//! `config.go`: the `configure` parameters of `gate_sip_server`, validated without touching the
//! network (a hostname in `sip_listen` is resolved, as `net.ResolveUDPAddr` does).
//!
//! ⚠️ Safe by default, in layers: the listen address must be loopback unless
//! `allow_non_loopback` is set, and then an `allow_from` list is mandatory. A `/0` entry is
//! refused (it would be "allow everyone" spelt as a restriction).

use std::time::Duration;

use serde_json::{Map, Value};

use super::netaddr::{parse_addr, parse_prefix, resolve_udp_addr, Addr, Prefix};
use crate::gostd::strconv::go_quote;

pub const KEY: &str = "gate_sip_server";

const DEFAULT_USER: &str = "8001"; // SIP.UserID on the station
const DEFAULT_REALM: &str = "VDP"; // SIP.RegisterRealm on the station
const DEFAULT_RTP_PORT: i64 = 40000;
const DEFAULT_RING_SECONDS: u64 = 45;
const DEFAULT_ORPHAN_SECONDS: u64 = 15;
/// Dahua sends NO REGISTER at all below this, silently.
pub const MIN_PASSWORD: usize = 8;
/// ...or above this.
pub const MAX_PASSWORD: usize = 15;

/// Go's unexported `config`.
#[derive(Clone, PartialEq, Eq)]
pub struct Config {
    pub device_id: String,
    /// host:port the station's REGISTER/INVITE arrive at
    pub sip_listen: String,
    /// 0 = kernel-assigned (tests); gate-sip's default is 40000
    pub rtp_port: i64,
    pub user: String,
    pub realm: String,
    pub password: String,
    pub ring_limit: Duration,
    pub orphan_grace: Duration,
    pub allow_from: Vec<Prefix>,
    pub allow_non_loopback: bool,
    /// W4: Talk to the gate may place a call to the registered station
    pub dial_enabled: bool,
    /// DEV-811 P3: count the audio of every answered call and report it at the call's end
    /// (`call_audio_stats`). Absent or anything but `true` = off: no counting, no notification,
    /// the bytes of 0.7.2.
    pub call_stats: bool,
}

/// The SIP secret never prints: a `{cfg:?}` shows `<redacted>` in its place.
impl std::fmt::Debug for Config {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Config")
            .field("device_id", &self.device_id)
            .field("sip_listen", &self.sip_listen)
            .field("rtp_port", &self.rtp_port)
            .field("user", &self.user)
            .field("realm", &self.realm)
            .field("password", &"<redacted>")
            .field("ring_limit", &self.ring_limit)
            .field("orphan_grace", &self.orphan_grace)
            .field("allow_from", &self.allow_from)
            .field("allow_non_loopback", &self.allow_non_loopback)
            .field("dial_enabled", &self.dial_enabled)
            .field("call_stats", &self.call_stats)
            .finish()
    }
}

impl Default for Config {
    /// The values `parseConfig` starts from (and what Go's zero `config` plus defaults is).
    fn default() -> Self {
        Config {
            device_id: String::new(),
            sip_listen: String::new(),
            rtp_port: DEFAULT_RTP_PORT,
            user: DEFAULT_USER.to_string(),
            realm: DEFAULT_REALM.to_string(),
            password: String::new(),
            ring_limit: Duration::from_secs(DEFAULT_RING_SECONDS),
            orphan_grace: Duration::from_secs(DEFAULT_ORPHAN_SECONDS),
            allow_from: Vec::new(),
            allow_non_loopback: false,
            dial_enabled: false,
            call_stats: false,
        }
    }
}

/// `intOf`: a JSON number that is a whole number. (The IPC layer hands numbers over as
/// float64, so `30`, `30.0` and `3e1` are all 30.)
fn int_of(v: &Value) -> Option<i64> {
    let Value::Number(n) = v else { return None };
    let f = n.as_f64()?;
    // Go: `n == float64(int(n))`. Out of range it differs by platform, and every caller
    // rejects an out-of-range value anyway.
    if f == (f as i64) as f64 {
        Some(f as i64)
    } else {
        None
    }
}

/// `strings.TrimSpace(in[k].(string))`; "" when the key is absent or not a string.
fn str_of(m: &Map<String, Value>, k: &str) -> String {
    match m.get(k) {
        Some(Value::String(s)) => s.trim().to_string(),
        _ => String::new(),
    }
}

/// `parseDial` (dial.go): absent, null or false is off, true is on, anything else is an
/// error (never read as on).
pub fn parse_dial(m: &Map<String, Value>) -> Result<bool, String> {
    match m.get("dial_enabled") {
        None | Some(Value::Null) => Ok(false),
        Some(Value::Bool(b)) => Ok(*b),
        Some(_) => Err(format!("{KEY}: dial_enabled must be true or false")),
    }
}

/// `parseAllowEntry`: one IP or CIDR. A bare address becomes a host prefix (IPv4-mapped
/// IPv6 is unmapped first); a prefix is masked; `/0` is refused.
pub fn parse_allow_entry(s: &str) -> Result<Prefix, String> {
    if s.contains('/') {
        let p = parse_prefix(s)?;
        if p.bits() == 0 {
            return Err("a /0 allows everyone".to_string());
        }
        return Ok(p.masked());
    }
    let a = parse_addr(s)?.unmap();
    let bits = a.bit_len();
    Ok(Prefix::from(&a, bits))
}

/// `parseConfig`. `input` is the `config` object of the `configure` request (an empty map
/// for `null`, as Go's nil map).
pub fn parse_config(input: &Map<String, Value>) -> Result<Config, String> {
    let mut c = Config::default();
    let err = |m: &str| Err(format!("{KEY}: {m}"));

    c.sip_listen = str_of(input, "sip_listen");
    if c.sip_listen.is_empty() {
        return err("sip_listen is required (no default: this mode never binds by accident)");
    }
    let v = str_of(input, "user");
    if !v.is_empty() {
        c.user = v;
    }
    let v = str_of(input, "realm");
    if !v.is_empty() {
        c.realm = v;
    }
    c.password = str_of(input, "password");
    if c.password.is_empty() {
        return err("password is required");
    }
    // ⚠️ gate-sip refuses to START outside 8-15: the station silently sends no REGISTER at
    // all, while its own UI still reports registration "Success".
    let n = c.password.len();
    if !(MIN_PASSWORD..=MAX_PASSWORD).contains(&n) {
        return Err(format!(
            "{KEY}: password is {n} characters; Dahua requires {MIN_PASSWORD}-{MAX_PASSWORD} \
             or the station never sends a REGISTER at all"
        ));
    }
    if let Some(v) = input.get("rtp_port") {
        match int_of(v) {
            Some(n) if (0..=65535).contains(&n) => c.rtp_port = n,
            _ => return err("rtp_port must be 0-65535"),
        }
    }
    if let Some(v) = input.get("ring_seconds") {
        match int_of(v) {
            Some(n) if n > 0 && n <= 300 => c.ring_limit = Duration::from_secs(n as u64),
            _ => return err("ring_seconds must be 1-300"),
        }
    }
    if let Some(v) = input.get("orphan_seconds") {
        match int_of(v) {
            Some(n) if n > 0 && n <= 120 => c.orphan_grace = Duration::from_secs(n as u64),
            _ => return err("orphan_seconds must be 1-120"),
        }
    }
    c.dial_enabled = parse_dial(input)?;
    c.call_stats = matches!(input.get("call_stats"), Some(Value::Bool(true)));
    if let Some(Value::Bool(b)) = input.get("allow_non_loopback") {
        c.allow_non_loopback = *b;
    }
    if let Some(raw) = input.get("allow_from") {
        let items: Vec<String> = match raw {
            Value::Array(l) => {
                let mut items = Vec::new();
                for x in l {
                    match x {
                        Value::String(s) => items.push(s.clone()),
                        _ => return err("allow_from entries must be strings"),
                    }
                }
                items
            }
            Value::String(l) => l.split(',').map(str::to_string).collect(),
            _ => return err("allow_from must be a list of IPs/CIDRs"),
        };
        for it in items {
            let it = it.trim();
            if it.is_empty() {
                continue;
            }
            match parse_allow_entry(it) {
                Ok(p) => c.allow_from.push(p),
                Err(e) => return Err(format!("{KEY}: bad allow_from entry {}: {e}", go_quote(it))),
            }
        }
    }

    let addr = match resolve_udp_addr(&c.sip_listen) {
        Ok(a) => a,
        Err(e) => {
            return Err(format!(
                "{KEY}: bad sip_listen {}: {e}",
                go_quote(&c.sip_listen)
            ))
        }
    };
    if !addr.is_loopback() {
        if !c.allow_non_loopback {
            return Err(format!(
                "{KEY}: refusing sip_listen {}: this mode binds loopback only \
                 until allow_non_loopback is set (design §7 activation gate)",
                go_quote(&c.sip_listen)
            ));
        }
        if c.allow_from.is_empty() {
            return err("a non-loopback sip_listen needs an allow_from list");
        }
    }

    c.device_id = str_of(input, "device_id");
    if c.device_id.is_empty() {
        c.device_id = format!("gate-sip-server:{}", c.sip_listen);
    }
    Ok(c)
}

impl Config {
    /// `config.allowed(ip)`: no list means everyone; otherwise `ip` (4 or 16 bytes, as
    /// `net.IP`; anything else is refused) must fall inside one prefix, IPv4-mapped IPv6
    /// counting as IPv4.
    pub fn allowed(&self, ip: &[u8]) -> bool {
        if self.allow_from.is_empty() {
            return true;
        }
        let Some(a) = Addr::from_slice(ip) else {
            return false;
        };
        let a = a.unmap();
        self.allow_from.iter().any(|p| p.contains(&a))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // R1-110: the SIP secret is not printed by `{:?}`.
    #[test]
    fn debug_does_not_print_the_password() {
        let cfg = Config {
            password: "s3cretpass".to_string(),
            ..Config::default()
        };
        let text = format!("{cfg:?}");
        assert!(!text.contains("s3cretpass"), "{text}");
        assert!(text.contains("<redacted>"), "{text}");
    }
}
