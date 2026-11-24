//! Golden vectors produced by the REAL Go `internal/ipc/tcp.go`, `cmd/portico-transport/
//! standalone.go` and `secretsenv.go` (tools/gen_golden/standalone_gen.go, see README.md)
//! replayed against the Rust port (slice 10):
//! * 33 `sipConfigFromEnv` environments, 52 `parseAllow` strings, 44 `openIPC` calls (real
//!   listeners on loopback), 59 `.env` files through `loadSecretsEnv` and `withSecretsEnv`, 20
//!   adopt / detach decisions, 5 `preloadSIP` runs;
//! * 85 conversations with a real Go TCP IPC server (the auth handshake over every shape of
//!   first line, the allow-from check, the 5 s window), replayed against the Rust server: what
//!   the client receives, and whether the server closed, must be the same.
//!
//! Every observation must be equal to the recorded one, with these exceptions, all listed in
//! PARITY.md: the text after the quoted entry of an unparsable `-ipc-allow-from` (Rust's own,
//! `netip`'s is not reproduced) and numbers, which are compared as `f64`.
//! Real time: the TCP conversations wait out quiet periods and the 5 s window.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use serde_json::{json, Map, Value};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;
use transport_rs::driver::{AudioTopology, Capabilities};
use transport_rs::flags::Flags;
use transport_rs::ipc::{HandlerResult, IpcError, Params, Reply, Server, TcpOptions};
use transport_rs::secretsenv::{load_secrets_env, with_secrets_env};
use transport_rs::standalone::{
    adopt_configure, detach_deconfigure, open_ipc, parse_allow, preload_sip, sip_config_from_env,
    Getenv, PreloadedSet,
};

fn load() -> Value {
    let mut p = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    p.push("tests/golden/standalone.json");
    let text = std::fs::read_to_string(&p).unwrap_or_else(|e| panic!("{}: {e}", p.display()));
    serde_json::from_str(&text).unwrap()
}

/// A scratch directory, removed on drop.
struct Dir(PathBuf);

impl Dir {
    fn new(tag: &str) -> Dir {
        use std::sync::atomic::{AtomicU64, Ordering};
        static N: AtomicU64 = AtomicU64::new(0);
        let p = std::env::temp_dir().join(format!(
            "golden-standalone-{tag}-{}-{}",
            std::process::id(),
            N.fetch_add(1, Ordering::SeqCst)
        ));
        std::fs::create_dir_all(&p).unwrap();
        Dir(p)
    }
    fn path(&self) -> &Path {
        &self.0
    }
    fn s(&self) -> &str {
        self.0.to_str().unwrap()
    }
}

impl Drop for Dir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn b64(s: &str) -> Vec<u8> {
    transport_rs::util::base64_decode(s).unwrap()
}

/// The recipe of `padGen.bytes()`: prefix, `pad` bytes of `a`, suffix.
fn pad_gen(g: &Value) -> Vec<u8> {
    let mut v = b64(g["prefix_b64"].as_str().unwrap());
    v.extend(std::iter::repeat_n(
        b'a',
        g["pad"].as_u64().unwrap() as usize,
    ));
    v.extend(b64(g["suffix_b64"].as_str().unwrap()));
    v
}

fn env_map(v: &Value, dir: &str) -> BTreeMap<String, String> {
    v.as_object()
        .map(|m| {
            m.iter()
                .map(|(k, v)| (k.clone(), v.as_str().unwrap().replace("{DIR}", dir)))
                .collect()
        })
        .unwrap_or_default()
}

fn getenv_of(m: BTreeMap<String, String>) -> Arc<Getenv> {
    Arc::new(move |k: &str| m.get(k).cloned().unwrap_or_default())
}

/// Numbers as `f64` (Go decodes every JSON number into a float64 here).
fn norm(v: &Value) -> Value {
    match v {
        Value::Number(n) => json!(n.as_f64().unwrap()),
        Value::Array(a) => Value::Array(a.iter().map(norm).collect()),
        Value::Object(m) => Value::Object(m.iter().map(|(k, v)| (k.clone(), norm(v))).collect()),
        o => o.clone(),
    }
}

// ---- sipConfigFromEnv ---------------------------------------------------------------------

#[test]
fn sip_config_from_env_matches_go() {
    let g = load();
    let scen = g["sip_env"].as_array().unwrap();
    assert_eq!(scen.len(), 33);
    for s in scen {
        let name = s["name"].as_str().unwrap();
        let dir = Dir::new("sip");
        if let Some(files) = s["files"].as_object() {
            for (n, c) in files {
                std::fs::write(dir.path().join(n), c.as_str().unwrap()).unwrap();
            }
        }
        let env = getenv_of(env_map(&s["env"], dir.s()));
        let got = sip_config_from_env(&*env);
        match (got, s["err"].as_str()) {
            (Ok(cfg), None) => {
                let want: Value = serde_json::from_str(s["cfg"].as_str().unwrap()).unwrap();
                assert_eq!(norm(&Value::Object(cfg)), norm(&want), "{name}");
            }
            (Err(e), Some(want)) => {
                assert_eq!(e.replace(dir.s(), "{DIR}"), want, "{name}");
            }
            (Ok(cfg), Some(want)) => panic!("{name}: Go failed with {want:?}, Rust gave {cfg:?}"),
            (Err(e), None) => panic!("{name}: Go succeeded, Rust failed: {e}"),
        }
    }
}

// ---- parseAllow ---------------------------------------------------------------------------

#[test]
fn parse_allow_matches_go() {
    let g = load();
    let v = g["parse_allow"].as_array().unwrap();
    assert_eq!(v.len(), 52);
    for s in v {
        let input = s["in"].as_str().unwrap();
        match (parse_allow(input), s["err"].as_bool().unwrap_or(false)) {
            (Ok(p), false) => assert_eq!(p.to_string(), s["out"].as_str().unwrap(), "{input:?}"),
            (Err(_), true) => {}
            (Ok(p), true) => panic!("{input:?}: Go refused, Rust parsed {p}"),
            (Err(e), false) => panic!("{input:?}: Go parsed {}, Rust refused: {e}", s["out"]),
        }
    }
}

// ---- openIPC ------------------------------------------------------------------------------

#[tokio::test]
async fn open_ipc_matches_go() {
    let g = load();
    let v = g["open_ipc"].as_array().unwrap();
    assert_eq!(v.len(), 44);
    for s in v {
        let name = s["name"].as_str().unwrap();
        let dir = Dir::new("open");
        if let Some(c) = s["token_file_content"].as_str() {
            std::fs::write(dir.path().join("tok"), c).unwrap();
        }
        let f = Flags {
            listen: s["listen"].as_str().unwrap().to_string(),
            ipc_token_file: s["token_file"]
                .as_str()
                .unwrap_or("")
                .replace("{DIR}", dir.s()),
            ipc_allow_non_loopback: s["allow_non_loopback"].as_bool().unwrap_or(false),
            ipc_allow_from: s["allow_from"].as_str().unwrap_or("").to_string(),
            socket: s["socket"].as_str().unwrap_or("").replace("{DIR}", dir.s()),
            ..Flags::default()
        };
        let env = getenv_of(env_map(&s["env"], dir.s()));
        match (open_ipc(&f, &*env), s["err"].as_str()) {
            (Ok((srv, tcp)), None) => {
                assert!(s["ok"].as_bool().unwrap(), "{name}");
                assert_eq!(tcp, s["tcp"].as_bool().unwrap_or(false), "{name}");
                srv.close().await;
            }
            (Err(e), Some(want)) => {
                let e = e.replace(dir.s(), "{DIR}");
                if want.starts_with("-ipc-allow-from ") {
                    // the quoted entry and the colon are compared; the reason is Rust's own
                    let cut = want.find("\": ").unwrap() + 3;
                    assert_eq!(&e[..cut], &want[..cut], "{name}");
                } else {
                    assert_eq!(e, want, "{name}");
                }
            }
            (Ok(_), Some(want)) => panic!("{name}: Go refused ({want}), Rust opened"),
            (Err(e), None) if privileged_bind_refused(&e, s["listen"].as_str().unwrap()) => {
                // the vectors were recorded by a Go run as root, where `127.0.0.1:+80` binds;
                // as an ordinary user the kernel refuses the same bind for both programs
                eprintln!("skipped {name}: {e} (not root, port below ip_unprivileged_port_start)");
            }
            (Err(e), None) => panic!("{name}: Go opened, Rust refused: {e}"),
        }
    }
}

/// True when `err` is the kernel refusing to bind a port below 1024 in `listen` (`host:port`,
/// the port possibly with a sign) to a user that may not: not a difference between the programs.
fn privileged_bind_refused(err: &str, listen: &str) -> bool {
    let port = listen
        .rsplit(':')
        .next()
        .unwrap_or("")
        .trim_start_matches('+');
    err.ends_with("bind: permission denied") && port.parse::<u16>().is_ok_and(|p| p < 1024)
}

// ---- secrets-env --------------------------------------------------------------------------

#[test]
fn secrets_env_matches_go() {
    let g = load();
    let keys: Vec<&str> = g["view_keys"]
        .as_array()
        .unwrap()
        .iter()
        .map(|k| k.as_str().unwrap())
        .collect();
    let scen = g["secrets"].as_array().unwrap();
    assert_eq!(scen.len(), 59);
    for s in scen {
        let name = s["name"].as_str().unwrap();
        let dir = Dir::new("sec");
        let path = if name == "missing file" {
            dir.path().join("absent")
        } else if name == "a directory" {
            dir.path().to_path_buf()
        } else {
            let body = if s.get("content_gen").is_some() {
                pad_gen(&s["content_gen"])
            } else {
                b64(s["content_b64"].as_str().unwrap_or(""))
            };
            let p = dir.path().join(".env");
            std::fs::write(&p, body).unwrap();
            p
        };
        let path = path.to_str().unwrap();
        // loadSecretsEnv
        match (load_secrets_env(path), s["load_err"].as_str()) {
            (Ok(m), None) => {
                let want: BTreeMap<String, String> = s["loaded"]
                    .as_object()
                    .map(|o| {
                        o.iter()
                            .map(|(k, v)| (k.clone(), v.as_str().unwrap().to_string()))
                            .collect()
                    })
                    .unwrap_or_default();
                assert_eq!(m.into_iter().collect::<BTreeMap<_, _>>(), want, "{name}");
            }
            (Err(e), Some(want)) => assert_eq!(e.replace(dir.s(), "{DIR}"), want, "{name}"),
            (r, w) => panic!("{name}: load: Rust {r:?}, Go error {w:?}"),
        }
        // withSecretsEnv
        let process = getenv_of(env_map(&s["env"], ""));
        match (with_secrets_env(path, process), s["view_err"].as_str()) {
            (Ok(view), None) => {
                for k in &keys {
                    assert_eq!(view(k), s["view"][k].as_str().unwrap_or(""), "{name}: {k}");
                }
            }
            (Err(e), Some(want)) => assert_eq!(e.replace(dir.s(), "{DIR}"), want, "{name}"),
            (Ok(_), w) => panic!("{name}: view: Rust opened, Go error {w:?}"),
            (Err(e), None) => panic!("{name}: view: Rust refused: {e}"),
        }
    }
}

// ---- adopt / detach / preload -------------------------------------------------------------

fn params_of(s: &str) -> Option<Params> {
    if s.is_empty() {
        return Some(None);
    }
    serde_json::value::RawValue::from_string(s.to_string())
        .ok()
        .map(Some)
}

fn caps() -> Capabilities {
    Capabilities {
        inbound_audio: AudioTopology(0),
        outbound_audio: AudioTopology(0),
        direct_capable: true,
        has_relay: false,
        rtsp_template: "rtsp://{host}/x".into(),
        reports_availability: true,
        can_dial: true,
        ..Capabilities::default()
    }
}

#[tokio::test]
async fn adopt_and_detach_match_go() {
    let g = load();
    let scen = g["adopt"].as_array().unwrap();
    assert_eq!(scen.len(), 20);
    for s in scen {
        let name = s["name"].as_str().unwrap();
        let set = PreloadedSet::default();
        for k in s["preload"].as_array().into_iter().flatten() {
            set.add(k.as_str().unwrap());
        }
        let registry: Vec<String> = s["registry"]
            .as_array()
            .into_iter()
            .flatten()
            .map(|v| v.as_str().unwrap().to_string())
            .collect();
        // Go hands an unparsable text to the handler as it is; here Params is already valid JSON,
        // so such a line can only be "went on" (the IPC layer drops it before any handler runs).
        let Some(params) = params_of(s["params"].as_str().unwrap()) else {
            assert!(s["passed"].as_bool().unwrap_or(false), "{name}");
            continue;
        };
        let passed = std::sync::atomic::AtomicBool::new(false);
        let next = |_p: Params| async {
            passed.store(true, std::sync::atomic::Ordering::SeqCst);
            Ok::<Reply, IpcError>(Reply::Json(json!({"next": "ran"})))
        };
        let got: HandlerResult = if s["method"] == "configure" {
            adopt_configure(
                &set,
                |id| registry.iter().any(|r| r == id).then(caps),
                params,
                next,
            )
            .await
        } else {
            detach_deconfigure(&set, params, next).await
        };
        assert_eq!(
            passed.load(std::sync::atomic::Ordering::SeqCst),
            s["passed"].as_bool().unwrap_or(false),
            "{name}: went on to the next handler"
        );
        match got {
            Ok(Reply::Raw(t)) => assert_eq!(t, s["result"].as_str().unwrap(), "{name}"),
            Ok(Reply::Json(v)) => {
                assert_eq!(
                    transport_rs::gojson::marshal(&v),
                    s["result"].as_str().unwrap(),
                    "{name}"
                )
            }
            Err(e) => {
                assert_eq!(e.code, s["err_code"].as_str().unwrap(), "{name}");
                assert_eq!(e.message, s["err_msg"].as_str().unwrap(), "{name}");
            }
        }
    }
}

#[tokio::test]
async fn preload_sip_matches_go() {
    let g = load();
    for s in g["preload"].as_array().unwrap() {
        let name = s["name"].as_str().unwrap();
        let cfg: Map<String, Value> = s["cfg"].as_object().unwrap().clone();
        let set = PreloadedSet::default();
        let seen = std::sync::Mutex::new(String::new());
        let fail = s["fail_code"]
            .as_str()
            .map(|c| (c.to_string(), s["fail_msg"].as_str().unwrap().to_string()));
        let got = preload_sip(
            |p: Params| {
                *seen.lock().unwrap() = transport_rs::ipc::raw_str(&p).to_string();
                let fail = fail.clone();
                async move {
                    match fail {
                        Some((c, m)) => Err(IpcError::new(c, m)),
                        None => Ok(Reply::Json(json!({"ok": true}))),
                    }
                }
            },
            &set,
            cfg,
        )
        .await;
        assert_eq!(
            *seen.lock().unwrap(),
            s["raw"].as_str().unwrap(),
            "{name}: what configure was handed"
        );
        assert_eq!(got.err(), s["err"].as_str().map(str::to_string), "{name}");
        let mut want: Vec<String> = s["in_set"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().unwrap().to_string())
            .collect();
        want.retain(|k| !k.is_empty()); // Go's set holds "" for a config naming no device; it never matches
        let mut have = set.keys();
        have.retain(|k| !k.is_empty());
        assert_eq!(have, want, "{name}: the preloaded set");
        assert!(!set.has(""), "{name}");
    }
}

// ---- the TCP conversations ----------------------------------------------------------------

/// Plays one recorded conversation against a fresh Rust TCP IPC server; every phase must see the
/// recorded bytes and the recorded close.
async fn play(s: Value) {
    let name = s["name"].as_str().unwrap().to_string();
    let token = s["token"]
        .as_str()
        .filter(|t| !t.is_empty())
        .unwrap_or("0123456789abcdef0123456789abcdef");
    let allow_from = s["allow_from"]
        .as_array()
        .into_iter()
        .flatten()
        .map(|p| parse_allow(p.as_str().unwrap()).unwrap())
        .collect();
    let srv = Server::listen_tcp(
        "127.0.0.1:0",
        TcpOptions {
            token: token.to_string(),
            allow_non_loopback: false,
            allow_from,
        },
    )
    .unwrap();
    srv.handle("ping", |_p: Params| async {
        Ok::<Reply, IpcError>(Reply::Json(json!({"pong": true})))
    });
    let looper = srv.clone();
    let serving = tokio::spawn(async move {
        while !looper.is_closed() {
            if looper.serve().await.is_err() {
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        }
    });
    let mut c = TcpStream::connect(srv.tcp_addr().unwrap()).await.unwrap();
    let mut closed = false;
    for (i, p) in s["phases"].as_array().unwrap().iter().enumerate() {
        let want_closed = p["closed"].as_bool().unwrap();
        let want_recv = b64(p["recv_b64"].as_str().unwrap_or(""));
        if closed {
            assert!(want_closed, "{name} phase {i}: Rust closed earlier than Go");
            continue;
        }
        if let Some(ms) = p["wait_before_ms"].as_u64() {
            tokio::time::sleep(Duration::from_millis(ms)).await;
        }
        let data = if p.get("send_gen").is_some() {
            Some(pad_gen(&p["send_gen"]))
        } else {
            p["send_b64"].as_str().filter(|d| !d.is_empty()).map(b64)
        };
        if let Some(d) = data {
            if c.write_all(&d).await.is_err() {
                closed = true;
            }
        }
        if p["close_write"].as_bool().unwrap_or(false) {
            let _ = c.shutdown().await;
        }
        let mut got = Vec::new();
        let mut buf = vec![0u8; 4096];
        loop {
            match tokio::time::timeout(Duration::from_millis(400), c.read(&mut buf)).await {
                Err(_) => break, // quiet
                Ok(Ok(0)) | Ok(Err(_)) => {
                    closed = true;
                    break;
                }
                Ok(Ok(n)) => got.extend_from_slice(&buf[..n]),
            }
        }
        assert_eq!(
            String::from_utf8_lossy(&got),
            String::from_utf8_lossy(&want_recv),
            "{name} phase {i}: what the client received"
        );
        assert_eq!(closed, want_closed, "{name} phase {i}: closed");
    }
    srv.close().await;
    let _ = tokio::time::timeout(Duration::from_secs(3), serving).await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn tcp_conversations_match_go() {
    let g = load();
    let scen = g["tcp"].as_array().unwrap();
    assert_eq!(scen.len(), 85);
    let mut set = tokio::task::JoinSet::new();
    for s in scen {
        set.spawn(play(s.clone()));
    }
    let mut failures = Vec::new();
    while let Some(r) = set.join_next().await {
        if let Err(e) = r {
            failures.push(
                e.try_into_panic()
                    .ok()
                    .and_then(|p| p.downcast_ref::<String>().cloned())
                    .unwrap_or_else(|| "panicked".into()),
            );
        }
    }
    assert!(
        failures.is_empty(),
        "{} conversations differ:\n{}",
        failures.len(),
        failures.join("\n")
    );
}
