//! Port of `transport/cmd/portico-transport/standalone.go` (DEV-518 W8, slice 10): running this
//! binary as a container, without HA's subprocess supervision. Everything here is OFF unless one
//! of the `-listen` / `-sip-from-env` flags is given; with neither, `main` is what it was.
//!
//! * `-listen host:port` serves the IPC over TCP ([`crate::ipc::Server::listen_tcp`]: token,
//!   loopback by default, two flags for anything else) instead of `-socket`, and keeps running
//!   when the hub disconnects (HA restarts, the link blips): [`serve_reconnects`] calls `serve`
//!   again. SIP registration, the RTP socket and a call in progress belong to this process, not
//!   to the connection.
//! * `-sip-from-env` configures and starts ONE `gate_sip_server` from `PORTICO_SIP_*` variables
//!   at start-up ([`sip_config_from_env`], `Hub::preload_sip`). A later `configure` for that
//!   `device_id` from the hub ADOPTS it (replies with its capabilities, ignores the hub's config,
//!   so the SIP password stays in the container), and `deconfigure` only detaches (an entry reload
//!   in HA must not drop the station's registration): [`PreloadedSet`] and the two wrappers in
//!   `dispatch/`.
//!
//! A ring or call-state change while no hub is connected is not replayed: `notify` has nobody to
//! write to. The house still rings from the doorbell button automation in HA, which does not
//! depend on this link.

use crate::util::lock;
use std::collections::HashSet;
use std::future::Future;
use std::net::IpAddr;
use std::sync::Mutex;
use std::time::Duration;

use serde_json::{json, Map, Value};

use crate::dispatch::{configure_key, device_id_key};
use crate::driver::Capabilities;
use crate::flags::Flags;
use crate::gojson;
use crate::gostd::strconv::parse_bool;
use crate::ipc::{raw_str, HandlerResult, IpcError, Params, Prefix, Reply, Server, TcpOptions};

use crate::driver::gatesipserver::config::KEY as SIP_SERVER_KEY;

/// The process environment as the standalone code reads it (`os.Getenv`: unset is `""`). With
/// `-secrets-env` it is [`crate::secretsenv::with_secrets_env`]'s view.
pub type Getenv = dyn Fn(&str) -> String + Send + Sync;

/// `os.Getenv`. A value that is not UTF-8 is converted lossily (U+FFFD); the token and the
/// password then fail their own checks or carry the same replacement characters Go's JSON
/// round trip would give them (PARITY difference 179).
pub fn process_getenv(k: &str) -> String {
    std::env::var_os(k)
        .map(|v| v.to_string_lossy().into_owned())
        .unwrap_or_default()
}

/// `openIPC`: the IPC server for the chosen transport. The bool reports whether it is the TCP
/// one (so `main` loops over reconnects).
pub fn open_ipc(f: &Flags, getenv: &Getenv) -> Result<(Server, bool), String> {
    if f.listen.is_empty() {
        if f.socket.is_empty() {
            return Err("-socket or -listen is required".into());
        }
        return Ok((Server::listen(&f.socket)?, false));
    }
    if !f.socket.is_empty() {
        return Err("-socket and -listen are mutually exclusive".into());
    }
    let mut tok = getenv("PORTICO_IPC_TOKEN");
    if !f.ipc_token_file.is_empty() {
        tok = read_token_file(&f.ipc_token_file, "-ipc-token-file")
            .map_err(|e| format!("reading -ipc-token-file: {e}"))?;
    }
    let mut allow = Vec::new();
    for it in f.ipc_allow_from.split(',') {
        let it = it.trim();
        if it.is_empty() {
            continue;
        }
        match parse_allow(it) {
            Ok(p) => allow.push(p),
            Err(e) => {
                return Err(format!(
                    "-ipc-allow-from {}: {e}",
                    crate::gostd::strconv::go_quote(it)
                ))
            }
        }
    }
    if tok.contains('\u{fffd}') {
        // Go compares raw bytes; a token that is not valid UTF-8 can never arrive in a JSON
        // line, so it could never authenticate. Refuse it loudly rather than start a listener
        // whose secret has been silently rewritten (PARITY difference 179).
        return Err("ipc: the TCP token must be valid UTF-8 and must not contain U+FFFD".into());
    }
    if tok.chars().any(char::is_whitespace) {
        // Go parity: only the .env reader refuses white space in a token (PARITY 187)
        crate::secretfile::warn(
            "portico-transport: warning: the IPC token contains white space (the -secrets-env reader would refuse it); check how it was written",
        );
    }
    // the token never reaches a panic text (the hook scrubs registered secrets)
    crate::panic_hook::register_secret(&tok);
    let srv = Server::listen_tcp(
        &f.listen,
        TcpOptions {
            token: tok,
            allow_non_loopback: f.ipc_allow_non_loopback,
            allow_from: allow,
        },
    )?;
    Ok((srv, true))
}

/// `ipc.ReadTokenFile` with Go's error text (`open P: ...`, `read P: ...`), plus the checks of
/// `secretfile` (a regular file, at most 64 KiB, a warning when others can read it).
fn read_token_file(path: &str, what: &str) -> Result<String, String> {
    let mut file = crate::secretfile::open_secret_file(path, what)?;
    let mut b =
        crate::secretfile::read_capped(&mut file, path, crate::secretfile::MAX_SECRET_FILE)?;
    while matches!(b.last(), Some(b'\n' | b'\r')) {
        b.pop();
    }
    Ok(String::from_utf8_lossy(&b).into_owned())
}

/// `parseAllow`: one `-ipc-allow-from` entry. A prefix is masked (`10.8.7.9/16` is
/// `10.8.0.0/16`); `/0` is refused ("allows everyone"); a plain address is a host prefix, with an
/// IPv4-mapped IPv6 address un-mapped first (a mapped PREFIX is kept as it is and so matches
/// nothing, as in Go). The error text after the quoted entry is Rust's own, not
/// `netip`'s (PARITY difference 180).
pub fn parse_allow(s: &str) -> Result<Prefix, String> {
    if let Some((addr, bits)) = s.split_once('/') {
        let ip = parse_ip(addr, false)?;
        let max = if ip.is_ipv4() { 32 } else { 128 };
        // netip: no sign, no leading zero (but "0" itself is fine)
        if bits.is_empty()
            || (bits.len() > 1 && !(b'1'..=b'9').contains(&bits.as_bytes()[0]))
            || !bits.bytes().all(|b| b.is_ascii_digit())
        {
            return Err(format!(
                "bad bits after slash: {}",
                crate::gostd::strconv::go_quote(bits)
            ));
        }
        let n: u32 = bits.parse().map_err(|_| {
            format!(
                "bad bits after slash: {}",
                crate::gostd::strconv::go_quote(bits)
            )
        })?;
        if n > max {
            return Err(format!("prefix length {n} too large for {max}"));
        }
        if n == 0 {
            return Err("a /0 allows everyone".into());
        }
        return Ok(Prefix::new(ip, n as u8));
    }
    let ip = parse_ip(s, true)?;
    let ip = match ip {
        IpAddr::V6(a) => a.to_ipv4_mapped().map(IpAddr::V4).unwrap_or(ip),
        v4 => v4,
    };
    let bits = if ip.is_ipv4() { 32 } else { 128 };
    Ok(Prefix::new(ip, bits))
}

/// `netip.ParseAddr`'s accepted forms. A zone (`fe80::1%eth0`) is accepted on a plain address
/// and dropped (Go's `PrefixFrom` drops it); it is refused inside a prefix.
fn parse_ip(s: &str, allow_zone: bool) -> Result<IpAddr, String> {
    let (addr, zone) = match s.split_once('%') {
        Some((a, z)) => (a, Some(z)),
        None => (s, None),
    };
    let ip: IpAddr = addr.parse().map_err(|_| {
        format!(
            "ParseAddr({}): unable to parse IP",
            crate::gostd::strconv::go_quote(s)
        )
    })?;
    if let Some(z) = zone {
        if !ip.is_ipv6() {
            return Err(format!(
                "ParseAddr({}): zones are only valid on IPv6",
                crate::gostd::strconv::go_quote(s)
            ));
        }
        if z.is_empty() {
            return Err(format!(
                "ParseAddr({}): zone must be a non-empty string",
                crate::gostd::strconv::go_quote(s)
            ));
        }
        if !allow_zone {
            return Err("IPv6 zones cannot be present in a prefix".into());
        }
    }
    Ok(ip)
}

/// `serveReconnects`: accepts the hub again after each connection ends, until `close`. The
/// accept loop runs in its own task, so a panic in it is reported and survived (a fixed text:
/// the panic hook has already printed the scrubbed payload) instead of ending the container.
pub async fn serve_reconnects(srv: &Server, logf: impl Fn(&str)) {
    serve_reconnects_with(
        || srv.is_closed(),
        || {
            let s = srv.clone();
            async move { s.serve().await }
        },
        || srv.drop_connection(),
        logf,
    )
    .await
}

/// [`serve_reconnects`] with the server abstracted (a test seam: a `serve` that fails or panics
/// on demand).
pub async fn serve_reconnects_with<Fut, Pf>(
    closed: impl Fn() -> bool,
    serve_once: impl Fn() -> Fut,
    after_panic: impl Fn() -> Pf,
    logf: impl Fn(&str),
) where
    Fut: Future<Output = Result<(), String>> + Send + 'static,
    Pf: Future<Output = ()>,
{
    while !closed() {
        match tokio::spawn(serve_once()).await {
            Ok(Ok(())) => {}
            Ok(Err(e)) => {
                if !closed() {
                    logf(&format!("portico-transport: serve: {e} (accepting again)"));
                    tokio::time::sleep(Duration::from_secs(1)).await;
                }
            }
            Err(_) => {
                // The task died holding the hub's connection: its write half is still in the
                // server, so the hub would see an open socket nobody reads. Close it, so the hub
                // reconnects (Go would have crashed the process, loudly).
                after_panic().await;
                logf("portico-transport: serve: the accept loop panicked; the hub connection was closed (accepting again)");
                tokio::time::sleep(Duration::from_secs(1)).await;
            }
        }
    }
}

/// `sipConfigFromEnv`: the `gate_sip_server` config map from `PORTICO_SIP_*`. The password may
/// come from `PORTICO_SIP_PASSWORD_FILE` (a docker secret). The driver's own `parseConfig` does
/// every validation (8-15 character password, loopback unless `allow_non_loopback` +
/// `allow_from`, ...), so this only maps. Numbers are `f64`, as the IPC hands them over. When
/// several numeric (or boolean) variables are malformed Go names a random one (it ranges over a
/// map); this names the first in key order (PARITY difference 176).
pub fn sip_config_from_env(getenv: &Getenv) -> Result<Map<String, Value>, String> {
    let mut cfg = Map::new();
    let mut set = |key: &str, env: &str| {
        let v = getenv(env);
        let v = v.trim();
        if !v.is_empty() {
            cfg.insert(key.to_string(), Value::String(v.to_string()));
        }
    };
    set("sip_listen", "PORTICO_SIP_LISTEN");
    set("user", "PORTICO_SIP_USER");
    set("realm", "PORTICO_SIP_REALM");
    set("allow_from", "PORTICO_SIP_ALLOW_FROM");
    set("device_id", "PORTICO_SIP_DEVICE_ID");
    cfg.insert(
        "password".into(),
        Value::String(getenv("PORTICO_SIP_PASSWORD")),
    );
    let pf = getenv("PORTICO_SIP_PASSWORD_FILE");
    if !pf.is_empty() {
        let p = read_token_file(&pf, "PORTICO_SIP_PASSWORD_FILE")
            .map_err(|e| format!("reading PORTICO_SIP_PASSWORD_FILE: {e}"))?;
        cfg.insert("password".into(), Value::String(p));
    }
    for (key, env) in [
        ("orphan_seconds", "PORTICO_SIP_ORPHAN_SECONDS"),
        ("ring_seconds", "PORTICO_SIP_RING_SECONDS"),
        ("rtp_port", "PORTICO_SIP_RTP_PORT"),
    ] {
        let v = getenv(env);
        let v = v.trim();
        if !v.is_empty() {
            let n: i64 = v.parse().map_err(|_| format!("{env} must be a number"))?;
            cfg.insert(
                key.into(),
                Value::from(serde_json::Number::from_f64(n as f64).expect("finite")),
            );
        }
    }
    for (key, env) in [
        ("allow_non_loopback", "PORTICO_SIP_ALLOW_NON_LOOPBACK"),
        ("dial_enabled", "PORTICO_SIP_DIAL_ENABLED"),
    ] {
        let v = getenv(env);
        let v = v.trim();
        if !v.is_empty() {
            let b = parse_bool(v).ok_or_else(|| format!("{env} must be true or false"))?;
            cfg.insert(key.into(), Value::Bool(b));
        }
    }
    let Some(Value::String(listen)) = cfg.get("sip_listen").cloned() else {
        return Err(
            "PORTICO_SIP_LISTEN is required (no default: this mode never binds by accident)".into(),
        );
    };
    if !cfg.contains_key("device_id") {
        // the driver's own default, made explicit
        cfg.insert(
            "device_id".into(),
            Value::String(format!("gate-sip-server:{listen}")),
        );
    }
    Ok(cfg)
}

/// `preloadedSet`: the lane keys (`device_id:<id>`) of the devices this process owns regardless
/// of the hub.
#[derive(Default)]
pub struct PreloadedSet {
    ids: Mutex<HashSet<String>>,
}

impl PreloadedSet {
    pub fn add(&self, id: &str) {
        lock(&self.ids).insert(id.to_string());
    }

    /// An empty key (a config that names no device) is never preloaded, whatever was added.
    pub fn has(&self, id: &str) -> bool {
        !id.is_empty() && lock(&self.ids).contains(id)
    }

    /// The keys, sorted (a test seam).
    pub fn keys(&self) -> Vec<String> {
        let mut v: Vec<String> = lock(&self.ids).iter().cloned().collect();
        v.sort();
        v
    }
}

/// `adoptConfigure`: a hub's `configure` of a preloaded device is answered with that device's
/// capabilities (`caps_of` looks the registry up; the hub's own config is ignored, so the SIP
/// password stays in the container); anything else goes to `next` unchanged.
pub async fn adopt_configure<Fut>(
    set: &PreloadedSet,
    caps_of: impl Fn(&str) -> Option<Capabilities>,
    params: Params,
    next: impl FnOnce(Params) -> Fut,
) -> HandlerResult
where
    Fut: Future<Output = HandlerResult>,
{
    // The set holds lane keys ("device_id:<id>", what configure_key and device_id_key return).
    let key = configure_key(raw_str(&params));
    if set.has(&key) {
        let id = key.strip_prefix("device_id:").unwrap_or(&key);
        return match caps_of(id) {
            Some(c) => Ok(Reply::Raw(c.to_wire())),
            None => Err(IpcError::new("unknown_device", id)),
        };
    }
    next(params).await
}

/// `detachDeconfigure`: a hub's `deconfigure` of a preloaded device does nothing (an entry reload
/// in HA must not drop the station's registration); anything else goes to `next`.
pub async fn detach_deconfigure<Fut>(
    set: &PreloadedSet,
    params: Params,
    next: impl FnOnce(Params) -> Fut,
) -> HandlerResult
where
    Fut: Future<Output = HandlerResult>,
{
    if set.has(&device_id_key(raw_str(&params))) {
        return Ok(json!({"ok": true}).into());
    }
    next(params).await
}

/// `preloadSIP`: starts the env-configured server through `configure` (the real configure path
/// of the hub, so every validation of the driver applies) and records it as preloaded. The error
/// is `<code>: <message>`, as Go prints it.
pub async fn preload_sip<Fut>(
    configure: impl FnOnce(Params) -> Fut,
    set: &PreloadedSet,
    cfg: Map<String, Value>,
) -> Result<(), String>
where
    Fut: Future<Output = HandlerResult>,
{
    if let Some(Value::String(pw)) = cfg.get("password") {
        crate::panic_hook::register_secret(pw);
    }
    // json.Marshal(configureParams{Driver, Config}): struct order, the map sorted
    let mut raw = format!(
        "{{\"driver\":{},\"config\":",
        gojson::marshal(&Value::String(SIP_SERVER_KEY.into()))
    );
    raw.push_str(&gojson::marshal(&Value::Object(cfg)));
    raw.push('}');
    let key = configure_key(&raw);
    let params: Params = Some(
        serde_json::value::RawValue::from_string(raw)
            .map_err(|e| format!("internal_error: {e}"))?,
    );
    configure(params)
        .await
        .map_err(|e| format!("{}: {}", e.code, e.message))?;
    set.add(&key);
    Ok(())
}
