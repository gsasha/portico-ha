//! Ports of the Go tests of the standalone/container mode (slice 10), the Go name in a comment
//! on each: all 6 of `internal/ipc/tcp_test.go`, all 5 of `cmd/portico-transport/standalone_test.go`
//! (the last one over a real hub and a real `gate_sip_server`) and all 8 of `secretsenv_test.go`;
//! then the tests that have no Go counterpart (the listener's lifetime, the auth window against a
//! shutdown, the accept loop's panic guard, the deterministic error order, the source scan for
//! anything that could print a secret).

use std::collections::BTreeMap;
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde_json::{json, Value};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::tcp::{OwnedReadHalf, OwnedWriteHalf};
use tokio::net::TcpStream;
use transport_rs::flags::Flags;
use transport_rs::ipc::{IpcError, Params, Prefix, Reply, Server, TcpOptions, MIN_TOKEN_LEN};
use transport_rs::secretsenv::{load_secrets_env, with_secrets_env};
use transport_rs::sfu::session::SessionConfig;
use transport_rs::standalone::{
    open_ipc, parse_allow, serve_reconnects, serve_reconnects_with, sip_config_from_env, Getenv,
};
use transport_rs::{dispatch, gojson};

const GOOD_TOK: &str = "0123456789abcdef0123456789abcdef";
const W_TOK: &str = "0123456789abcdef0123456789abcdef";

struct Dir(PathBuf);

impl Dir {
    fn new() -> Dir {
        static N: AtomicU64 = AtomicU64::new(0);
        let p = std::env::temp_dir().join(format!(
            "standalone-{}-{}",
            std::process::id(),
            N.fetch_add(1, Ordering::SeqCst)
        ));
        std::fs::create_dir_all(&p).unwrap();
        Dir(p)
    }
    fn file(&self, name: &str, body: &str) -> String {
        let p = self.0.join(name);
        std::fs::write(&p, body).unwrap();
        p.to_str().unwrap().to_string()
    }
    fn path(&self, name: &str) -> String {
        self.0.join(name).to_str().unwrap().to_string()
    }
}

impl Drop for Dir {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn env_of(pairs: &[(&str, &str)]) -> Arc<Getenv> {
    let m: BTreeMap<String, String> = pairs
        .iter()
        .map(|(k, v)| (k.to_string(), v.to_string()))
        .collect();
    Arc::new(move |k: &str| m.get(k).cloned().unwrap_or_default())
}

fn flags(listen: &str) -> Flags {
    Flags {
        listen: listen.to_string(),
        ..Flags::default()
    }
}

// ---- a TCP client -------------------------------------------------------------------------

struct Client {
    rd: BufReader<OwnedReadHalf>,
    wr: OwnedWriteHalf,
}

impl Client {
    async fn connect(addr: std::net::SocketAddr) -> Client {
        let (rd, wr) = TcpStream::connect(addr).await.unwrap().into_split();
        Client {
            rd: BufReader::new(rd),
            wr,
        }
    }

    /// Go's `line`: sends `send` (and a newline) if it is not empty, then returns the next line
    /// within 3 s; "EOF" when the peer closed, "TIMEOUT" when it stayed open and silent (Go's
    /// helper says "EOF" for both: R2-78).
    async fn line(&mut self, send: &str) -> String {
        if !send.is_empty() {
            let _ = self.wr.write_all(format!("{send}\n").as_bytes()).await;
        }
        let mut s = String::new();
        match tokio::time::timeout(Duration::from_secs(3), self.rd.read_line(&mut s)).await {
            Ok(Ok(n)) if n > 0 => s,
            Err(_) => "TIMEOUT".to_string(),
            _ => "EOF".to_string(),
        }
    }

    async fn auth(&mut self, tok: &str) -> String {
        self.line(&format!(
            r#"{{"id":1,"method":"auth","params":{{"token":"{tok}"}}}}"#
        ))
        .await
    }
}

/// `startTCP`: a real TCP server with a `ping` handler, served in a loop.
fn start_tcp(o: TcpOptions) -> (Server, std::net::SocketAddr) {
    let srv = Server::listen_tcp("127.0.0.1:0", o).unwrap();
    srv.handle("ping", |_p: Params| async {
        Ok::<Reply, IpcError>(Reply::Json(json!({"pong": true})))
    });
    let looper = srv.clone();
    tokio::spawn(async move {
        while !looper.is_closed() {
            if looper.serve().await.is_err() {
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        }
    });
    let addr = srv.tcp_addr().unwrap();
    (srv, addr)
}

fn opts(tok: &str) -> TcpOptions {
    TcpOptions {
        token: tok.to_string(),
        ..TcpOptions::default()
    }
}

// ---- tcp_test.go --------------------------------------------------------------------------

// TestTCPRequiresLongToken
#[tokio::test]
async fn tcp_requires_long_token() {
    let e = Server::listen_tcp("127.0.0.1:0", opts("short")).err();
    assert_eq!(
        e.as_deref(),
        Some("ipc: the TCP listener needs a token of at least 24 characters")
    );
    assert!(Server::listen_tcp("127.0.0.1:0", opts(&GOOD_TOK[..MIN_TOKEN_LEN - 1])).is_err());
    assert!(Server::listen_tcp("127.0.0.1:0", opts(&GOOD_TOK[..MIN_TOKEN_LEN])).is_ok());
}

// TestTCPNonLoopbackNeedsBothFlags
#[tokio::test]
async fn tcp_non_loopback_needs_both_flags() {
    assert!(
        Server::listen_tcp("0.0.0.0:0", opts(GOOD_TOK)).is_err(),
        "non-loopback bind without the flag"
    );
    let e = Server::listen_tcp(
        "0.0.0.0:0",
        TcpOptions {
            token: GOOD_TOK.into(),
            allow_non_loopback: true,
            allow_from: vec![],
        },
    )
    .err();
    assert_eq!(
        e.as_deref(),
        Some("ipc: a non-loopback listen address needs an allow-from list")
    );
    let ok = Server::listen_tcp(
        "0.0.0.0:0",
        TcpOptions {
            token: GOOD_TOK.into(),
            allow_non_loopback: true,
            allow_from: vec![parse_allow("10.8.0.39").unwrap()],
        },
    );
    assert!(ok.is_ok());
    ok.unwrap().close().await;
}

// TestTCPAuthThenServe
#[tokio::test]
async fn tcp_auth_then_serve() {
    let (_srv, addr) = start_tcp(opts(GOOD_TOK));
    let mut c = Client::connect(addr).await;
    let r = c.auth(GOOD_TOK).await;
    assert!(r.contains(r#""ok":true"#), "auth reply {r:?}");
    let r = c.line(r#"{"id":2,"method":"ping"}"#).await;
    assert!(r.contains("pong"), "ping reply {r:?}");
}

// TestTCPBadTokenAndNonAuthFirstLineDropped
#[tokio::test]
async fn tcp_bad_token_and_non_auth_first_line_dropped() {
    let (_srv, addr) = start_tcp(opts(GOOD_TOK));
    for first in [
        r#"{"id":1,"method":"auth","params":{"token":"wrong"}}"#,
        r#"{"id":1,"method":"ping"}"#,
        "garbage",
    ] {
        let mut c = Client::connect(addr).await;
        let r = c.line(first).await;
        assert!(
            !r.contains("pong") && !r.contains(r#""ok":true"#),
            "{first:?} was served: {r:?}"
        );
        // The connection is closed after the (possible) refusal.
        assert_eq!(c.line("").await, "EOF", "{first:?}: connection stayed open");
    }
    // A refused client does not take the slot: a good one still gets in.
    let mut c = Client::connect(addr).await;
    let r = c.auth(GOOD_TOK).await;
    assert!(
        r.contains(r#""ok":true"#),
        "good client after bad ones: {r:?}"
    );
}

// TestTCPSourceOutsideAllowFromClosed
#[tokio::test]
async fn tcp_source_outside_allow_from_closed() {
    let (_srv, addr) = start_tcp(TcpOptions {
        token: GOOD_TOK.into(),
        allow_non_loopback: false,
        allow_from: vec![parse_allow("10.99.0.0/16").unwrap()],
    });
    let mut c = Client::connect(addr).await;
    let r = c.auth(GOOD_TOK).await;
    assert!(
        !r.contains(r#""ok":true"#),
        "a source outside allow_from authenticated: {r:?}"
    );
}

// TestTCPNoNotifyBeforeAuth
#[tokio::test]
async fn tcp_no_notify_before_auth() {
    let (srv, addr) = start_tcp(opts(GOOD_TOK));
    let _c = Client::connect(addr).await;
    tokio::time::sleep(Duration::from_millis(100)).await;
    assert!(
        srv.notify("ringing", &json!({"call_id": "x"}))
            .await
            .is_err(),
        "a notification was written to an unauthenticated connection"
    );
}

// ---- standalone_test.go -------------------------------------------------------------------

// TestSIPConfigFromEnvMapsAndLeavesValidationToTheDriver
#[test]
fn sip_config_from_env_maps_and_leaves_validation_to_the_driver() {
    let cfg = sip_config_from_env(&*env_of(&[
        ("PORTICO_SIP_LISTEN", "127.0.0.1:0"),
        ("PORTICO_SIP_PASSWORD", "s3cretpass"),
        ("PORTICO_SIP_RTP_PORT", "0"),
        ("PORTICO_SIP_ALLOW_FROM", "10.8.0.39"),
        ("PORTICO_SIP_DIAL_ENABLED", "true"),
    ]))
    .unwrap();
    assert_eq!(cfg["sip_listen"], "127.0.0.1:0");
    assert_eq!(cfg["rtp_port"].as_f64(), Some(0.0));
    assert_eq!(cfg["dial_enabled"], true);
    assert_eq!(cfg["allow_from"], "10.8.0.39");
    assert_eq!(cfg["device_id"], "gate-sip-server:127.0.0.1:0");
    assert!(
        sip_config_from_env(&*env_of(&[("PORTICO_SIP_PASSWORD", "s3cretpass")])).is_err(),
        "no PORTICO_SIP_LISTEN must be refused (no default bind)"
    );
    assert!(
        sip_config_from_env(&*env_of(&[
            ("PORTICO_SIP_LISTEN", "127.0.0.1:0"),
            ("PORTICO_SIP_RTP_PORT", "x")
        ]))
        .is_err(),
        "non-numeric port accepted"
    );
}

// TestSIPPasswordFileWins
#[test]
fn sip_password_file_wins() {
    let d = Dir::new();
    let f = d.file("pw", "filepass88\n");
    let cfg = sip_config_from_env(&*env_of(&[
        ("PORTICO_SIP_LISTEN", "127.0.0.1:0"),
        ("PORTICO_SIP_PASSWORD", "envpass888"),
        ("PORTICO_SIP_PASSWORD_FILE", &f),
    ]))
    .unwrap();
    assert_eq!(cfg["password"], "filepass88");
}

fn test_server() -> Server {
    Server::listen_tcp("127.0.0.1:0", opts(W_TOK)).unwrap()
}

// TestEnvConfigStillGoesThroughTheLoopbackGuards
#[tokio::test]
async fn env_config_still_goes_through_the_loopback_guards() {
    let envs: [&[(&str, &str)]; 3] = [
        // no flag
        &[
            ("PORTICO_SIP_LISTEN", "0.0.0.0:5060"),
            ("PORTICO_SIP_PASSWORD", "s3cretpass"),
        ],
        // no allow_from
        &[
            ("PORTICO_SIP_LISTEN", "0.0.0.0:5060"),
            ("PORTICO_SIP_PASSWORD", "s3cretpass"),
            ("PORTICO_SIP_ALLOW_NON_LOOPBACK", "true"),
        ],
        // bad password length
        &[
            ("PORTICO_SIP_LISTEN", "127.0.0.1:0"),
            ("PORTICO_SIP_PASSWORD", "short"),
        ],
    ];
    for env in envs {
        let cfg = sip_config_from_env(&*env_of(env)).unwrap();
        let srv = test_server();
        let hub = dispatch::register(
            &srv,
            dispatch::HubOptions {
                sfu: SessionConfig::loopback(),
                ..dispatch::HubOptions::default()
            },
        );
        let r = hub.preload_sip(cfg).await;
        assert!(r.is_err(), "{env:?} started");
        assert!(
            hub.preloaded().keys().is_empty(),
            "{env:?}: recorded as preloaded"
        );
        hub.shutdown().await;
        srv.close().await;
    }
}

// TestOpenIPCRules
#[tokio::test]
async fn open_ipc_rules() {
    let get = env_of(&[("PORTICO_IPC_TOKEN", W_TOK)]);
    let none = env_of(&[]);
    assert!(
        open_ipc(&Flags::default(), &*get).is_err(),
        "no transport chosen"
    );
    assert!(
        open_ipc(
            &Flags {
                socket: "/tmp/x.sock".into(),
                ..flags("127.0.0.1:0")
            },
            &*get
        )
        .is_err(),
        "-socket with -listen"
    );
    assert!(open_ipc(&flags("127.0.0.1:0"), &*none).is_err(), "no token");
    assert!(
        open_ipc(&flags("0.0.0.0:0"), &*get).is_err(),
        "LAN bind with neither flag"
    );
    let mut f = flags("0.0.0.0:0");
    f.ipc_allow_non_loopback = true;
    assert!(open_ipc(&f, &*get).is_err(), "LAN bind without allow-from");
    let mut f = flags("127.0.0.1:0");
    f.ipc_allow_from = "0.0.0.0/0".into();
    assert!(open_ipc(&f, &*get).is_err(), "/0 allow-from");
    let (srv, tcp) = open_ipc(&flags("127.0.0.1:0"), &*get).unwrap();
    assert!(tcp);
    srv.close().await;
}

fn rpc_line(id: i64, method: &str, params: &str) -> String {
    format!("{{\"id\":{id},\"method\":\"{method}\",\"params\":{params}}}\n")
}

async fn rpc(c: &mut Client, id: i64, method: &str, params: &str) -> Value {
    c.wr.write_all(rpc_line(id, method, params).as_bytes())
        .await
        .unwrap();
    let mut s = String::new();
    tokio::time::timeout(Duration::from_secs(3), c.rd.read_line(&mut s))
        .await
        .expect("reply")
        .unwrap();
    serde_json::from_str(&s).unwrap()
}

// The whole container shape, over a real loopback TCP IPC: preloaded server, hub connects,
// adopts, disconnects, reconnects; the SIP server never stops.
// TestStandaloneAdoptDetachAndReconnect
#[tokio::test]
async fn standalone_adopt_detach_and_reconnect() {
    let (srv, tcp) = open_ipc(
        &flags("127.0.0.1:0"),
        &*env_of(&[("PORTICO_IPC_TOKEN", W_TOK)]),
    )
    .unwrap();
    assert!(tcp);
    let addr = srv.tcp_addr().unwrap();
    let hub = dispatch::register(
        &srv,
        dispatch::HubOptions {
            sfu: SessionConfig::loopback(),
            ..dispatch::HubOptions::default()
        },
    );
    let cfg = sip_config_from_env(&*env_of(&[
        ("PORTICO_SIP_LISTEN", "127.0.0.1:0"),
        ("PORTICO_SIP_PASSWORD", "s3cretpass"),
        ("PORTICO_SIP_RTP_PORT", "0"),
        ("PORTICO_SIP_DEVICE_ID", "gate-sa"),
    ]))
    .unwrap();
    hub.preload_sip(cfg).await.unwrap();
    let s2 = srv.clone();
    let serving = tokio::spawn(async move { serve_reconnects(&s2, |_| {}).await });

    let connect = || async {
        let mut c = Client::connect(addr).await;
        let r = rpc(&mut c, 1, "auth", &format!("{{\"token\":\"{W_TOK}\"}}")).await;
        assert!(r.get("error").is_none(), "auth: {r}");
        c
    };

    let mut c = connect().await;
    let before = hub
        .driver("gate-sa")
        .expect("the preloaded server is registered");
    // Go also compares the bound SIP address across the reconnect (R2-54): the listener is
    // still the same socket, not just the same object
    let addr_before = before.sip_addr().expect("the SIP server is bound");
    assert_ne!(addr_before.port(), 0);
    // The hub's own config (a wrong password, a different bind) is ignored: adopt.
    let r = rpc(
        &mut c,
        2,
        "configure",
        r#"{"driver":"gate_sip_server","config":{"device_id":"gate-sa","sip_listen":"0.0.0.0:5060","password":"x"}}"#,
    )
    .await;
    assert!(r.get("error").is_none(), "adopt failed: {r}");
    assert_eq!(
        r["result"]["DirectCapable"], false,
        "the capabilities of the running server: {r}"
    );
    let r = rpc(&mut c, 3, "deconfigure", r#"{"device_id":"gate-sa"}"#).await;
    assert!(r.get("error").is_none(), "detach: {r}");
    assert!(
        hub.driver("gate-sa").is_some(),
        "the hub's deconfigure stopped the preloaded server"
    );
    drop(c);

    // Reconnect: the server is still the same running driver.
    let mut c2 = connect().await;
    let r = rpc(
        &mut c2,
        2,
        "configure",
        r#"{"driver":"gate_sip_server","config":{"device_id":"gate-sa"}}"#,
    )
    .await;
    assert!(r.get("error").is_none(), "re-adopt: {r}");
    let after = hub.driver("gate-sa").unwrap();
    assert!(
        Arc::ptr_eq(&before, &after),
        "the server changed across the reconnect"
    );
    assert_eq!(
        after.sip_addr(),
        Some(addr_before),
        "the SIP address changed across the reconnect"
    );
    // A device the process does not own still takes the normal path.
    let r = rpc(
        &mut c2,
        3,
        "configure",
        r#"{"driver":"nope","config":{"device_id":"other"}}"#,
    )
    .await;
    assert!(
        gojson::marshal(&r).contains("unknown_driver"),
        "normal configure path: {r}"
    );

    srv.close().await;
    hub.shutdown().await;
    let _ = tokio::time::timeout(Duration::from_secs(3), serving).await;
}

// ---- secretsenv_test.go -------------------------------------------------------------------

const FAKE_PASS: &str = "fakepass1234";
const FAKE_PEER: &str = "peer0123456789abcdef0123456789";
const FAKE_OWNER: &str = "OWNERSECRET-must-never-be-read-0000";

fn good_env() -> String {
    format!(
        "# box env\nGATE_SIP_USER=8001\nGATE_SIP_REALM=VDP\nGATE_SIP_PASSWORD={FAKE_PASS}\nGATE_SIP_PEER_TOKEN={FAKE_PEER}\nGATE_SIP_TOKEN={FAKE_OWNER}\nOTHER=x\n"
    )
}

// TestSecretsEnvMapping
#[test]
fn secrets_env_mapping() {
    let d = Dir::new();
    let g = with_secrets_env(&d.file(".env", &good_env()), env_of(&[])).unwrap();
    assert_eq!(g("PORTICO_SIP_PASSWORD"), FAKE_PASS);
    assert_eq!(g("PORTICO_IPC_TOKEN"), FAKE_PEER);
    assert_eq!(g("PORTICO_SIP_USER"), "8001");
    assert_eq!(g("PORTICO_SIP_REALM"), "VDP");
    // other keys leaked into the environment view?
    assert_eq!(g("PORTICO_SIP_LISTEN"), "");
    assert_eq!(g("GATE_SIP_TOKEN"), "");
    assert_eq!(g("OTHER"), "");
}

// TestSecretsEnvParsingRules
#[test]
fn secrets_env_parsing_rules() {
    let d = Dir::new();
    let body = format!(
        "export GATE_SIP_PASSWORD=\"{FAKE_PASS}\"\n  GATE_SIP_PEER_TOKEN = '{FAKE_PEER}'  \nnoequals\n#GATE_SIP_USER=bad\n"
    );
    let g = with_secrets_env(&d.file(".env", &body), env_of(&[])).unwrap();
    assert_eq!(
        g("PORTICO_SIP_PASSWORD"),
        FAKE_PASS,
        "quotes/export/space handling"
    );
    assert_eq!(g("PORTICO_IPC_TOKEN"), FAKE_PEER);
    assert_eq!(
        g("PORTICO_SIP_USER"),
        "8001",
        "a comment line is ignored, the default used"
    );
}

// TestSecretsEnvDefaultsWhenAbsent
#[test]
fn secrets_env_defaults_when_absent() {
    let d = Dir::new();
    let g = with_secrets_env(
        &d.file(".env", &format!("GATE_SIP_PASSWORD={FAKE_PASS}\n")),
        env_of(&[]),
    )
    .unwrap();
    assert_eq!(g("PORTICO_SIP_USER"), "8001");
    assert_eq!(g("PORTICO_SIP_REALM"), "VDP");
}

// TestSecretsEnvRefusals
#[test]
fn secrets_env_refusals() {
    let d = Dir::new();
    for (name, body) in [
        ("short password", "GATE_SIP_PASSWORD=short\n"),
        ("long password", "GATE_SIP_PASSWORD=0123456789abcdefg\n"),
        ("short token", "GATE_SIP_PEER_TOKEN=tooshort\n"),
        (
            "token space",
            "GATE_SIP_PEER_TOKEN=\"abcdefghij klmnopqrstuvwxyz0123\"\n",
        ),
    ] {
        let e = with_secrets_env(&d.file(".env", body), env_of(&[])).err();
        let e = e.unwrap_or_else(|| panic!("{name} accepted"));
        for secret in ["tooshort", "abcdefghij", "0123456789abcdefg"] {
            assert!(!e.contains(secret), "{name}: value in error: {e}");
        }
    }
    assert!(
        with_secrets_env(&d.path("missing"), env_of(&[])).is_err(),
        "missing file accepted"
    );
}

// TestSecretsEnvOwnerTokenNeverRead
#[test]
fn secrets_env_owner_token_never_read() {
    let d = Dir::new();
    let vals = load_secrets_env(&d.file(".env", &good_env())).unwrap();
    let g = with_secrets_env(&d.file(".env2", &good_env()), env_of(&[])).unwrap();
    let cfg = sip_config_from_env(&move |k: &str| {
        if k == "PORTICO_SIP_LISTEN" {
            "127.0.0.1:0".to_string()
        } else {
            g(k)
        }
    })
    .unwrap();
    // even a bad-valued owner token must neither fail the load nor appear anywhere
    let err_bad =
        load_secrets_env(&d.file(".env3", "GATE_SIP_TOKEN=x y\nGATE_SIP_PASSWORD=short\n"));
    let mut dump = format!("{vals:?}{cfg:?}");
    if let Err(e) = &err_bad {
        dump.push_str(e);
    }
    assert!(!dump.contains(FAKE_OWNER), "owner token leaked");
    assert!(vals.values().all(|v| v != FAKE_OWNER), "owner token mapped");
    assert_eq!(vals.len(), 4, "expected exactly the 4 mapped keys");
    let e = err_bad.expect_err("the bad-password file must fail, for its password");
    assert!(!e.contains("x y"), "{e}");
    assert!(
        !e.contains("GATE_SIP_TOKEN"),
        "owner key even named in an error"
    );
}

// TestSecretsEnvNoSecretsLogged
//
// The Go test captures the `log` package's output. Here the real binary runs with a loose-mode
// `.env` and token file (the warning paths), `-sip-from-env`, a refused TCP admission (wrong
// token, and a first line that carries the real password), and its whole stdout and stderr is
// searched for every secret byte sequence (R1-14: the earlier version grepped the source for
// `println!`, which cannot see the helpers that do print).
#[test]
fn secrets_env_no_secrets_logged() {
    use std::process::{Command, Stdio};
    let d = Dir::new();
    let env_file = d.file(".env", &good_env()); // mode 0644 on purpose: the warning path
    let tok_file = d.file("tok", &format!("{FAKE_PEER}\n"));
    let port = {
        let l = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        l.local_addr().unwrap().port()
    };
    let out_path = d.path("out.log");
    let out = std::fs::File::create(&out_path).unwrap();
    let mut child = Command::new(env!("CARGO_BIN_EXE_portico-transport"))
        .args([
            "-listen",
            &format!("127.0.0.1:{port}"),
            "-ipc-token-file",
            &tok_file,
            "-secrets-env",
            &env_file,
            "-sip-from-env",
        ])
        .env("PORTICO_SIP_LISTEN", "127.0.0.1:0")
        .env("PORTICO_SIP_RTP_PORT", "0")
        .stdin(Stdio::null())
        .stdout(out.try_clone().unwrap())
        .stderr(out)
        .spawn()
        .unwrap();
    let text = |p: &str| std::fs::read_to_string(p).unwrap_or_default();
    let wrong = format!("{}x", &FAKE_PEER[..FAKE_PEER.len() - 1]); // almost the token
    let mut connected = false;
    for _ in 0..100 {
        if let Ok(mut c) = std::net::TcpStream::connect(("127.0.0.1", port)) {
            use std::io::{Read, Write};
            let _ = c.write_all(
                format!("{{\"id\":1,\"method\":\"auth\",\"params\":{{\"token\":\"{wrong}\"}}}}\n")
                    .as_bytes(),
            );
            let mut sink = Vec::new();
            let _ = c.read_to_end(&mut sink);
            connected = true;
            break;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    assert!(connected, "the binary never listened: {}", text(&out_path));
    // a first line that carries the real password and token, in no valid shape
    if let Ok(mut c) = std::net::TcpStream::connect(("127.0.0.1", port)) {
        use std::io::{Read, Write};
        let _ = c.write_all(format!("garbage {FAKE_PASS} {FAKE_PEER}\n").as_bytes());
        let mut sink = Vec::new();
        let _ = c.read_to_end(&mut sink);
    }
    for _ in 0..50 {
        if text(&out_path).contains("refused a TCP connection") {
            break;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    child.kill().unwrap();
    child.wait().unwrap();
    let log = text(&out_path);
    // the log paths were exercised ...
    assert!(log.contains("refused a TCP connection"), "{log}");
    assert!(log.contains("readable by group or others"), "{log}");
    // ... and no secret reached them
    for secret in [FAKE_PASS, FAKE_PEER, FAKE_OWNER, wrong.as_str()] {
        assert!(!log.contains(secret), "a secret reached the log: {log}");
    }
}

// TestSecretsEnvPrecedence
#[tokio::test]
async fn secrets_env_precedence() {
    let d = Dir::new();
    let g = with_secrets_env(
        &d.file(".env", &good_env()),
        env_of(&[
            ("PORTICO_SIP_PASSWORD", "explicitpw1"),
            ("PORTICO_SIP_USER", "9000"),
            ("PORTICO_IPC_TOKEN", "explicit-token-0123456789abcdef"),
        ]),
    )
    .unwrap();
    assert_eq!(
        g("PORTICO_SIP_PASSWORD"),
        "explicitpw1",
        "explicit env must win; file fills the rest"
    );
    assert_eq!(g("PORTICO_SIP_USER"), "9000");
    assert_eq!(g("PORTICO_IPC_TOKEN"), "explicit-token-0123456789abcdef");
    assert_eq!(g("PORTICO_SIP_REALM"), "VDP");
    // -ipc-token-file still wins over the file (open_ipc overrides the env value)
    let mut f = flags("127.0.0.1:0");
    f.ipc_token_file = d.file("tok", &format!("{W_TOK}\n"));
    let (srv, _) = open_ipc(&f, &*g).unwrap();
    // ... and it is the token file's token that the server accepts
    let addr = srv.tcp_addr().unwrap();
    let looper = srv.clone();
    tokio::spawn(async move {
        while !looper.is_closed() {
            let _ = looper.serve().await;
        }
    });
    let mut c = Client::connect(addr).await;
    assert!(
        c.auth(W_TOK).await.contains(r#""ok":true"#),
        "the token file's token"
    );
    srv.close().await;
    // the password file env wins over the file's password
    let pf = d.file("pw", "filepass88\n");
    let g2 = g.clone();
    let cfg = sip_config_from_env(&move |k: &str| match k {
        "PORTICO_SIP_PASSWORD_FILE" => pf.clone(),
        "PORTICO_SIP_LISTEN" => "127.0.0.1:0".into(),
        _ => g2(k),
    })
    .unwrap();
    assert_eq!(cfg["password"], "filepass88", "password file precedence");
}

// TestSecretsEnvFeedsIPCAndSIP
#[tokio::test]
async fn secrets_env_feeds_ipc_and_sip() {
    let d = Dir::new();
    let g = with_secrets_env(&d.file(".env", &good_env()), env_of(&[])).unwrap();
    let (srv, _) = open_ipc(&flags("127.0.0.1:0"), &*g).expect("token from file not used");
    srv.close().await;
    let g2 = g.clone();
    let cfg = sip_config_from_env(&move |k: &str| {
        if k == "PORTICO_SIP_LISTEN" {
            "127.0.0.1:0".to_string()
        } else {
            g2(k)
        }
    })
    .unwrap();
    assert_eq!(cfg["password"], FAKE_PASS);
    assert_eq!(cfg["user"], "8001");
    assert_eq!(cfg["realm"], "VDP");
}

// ---- no Go counterpart --------------------------------------------------------------------

// `listener.Close()` frees the port at once in Go; here the listener is dropped by `close`.
#[tokio::test]
async fn close_frees_the_port_and_ends_serve() {
    let srv = Server::listen_tcp("127.0.0.1:0", opts(GOOD_TOK)).unwrap();
    let addr = srv.tcp_addr().unwrap();
    let s2 = srv.clone();
    let serving = tokio::spawn(async move { s2.serve().await });
    tokio::time::sleep(Duration::from_millis(50)).await;
    srv.close().await;
    let r = tokio::time::timeout(Duration::from_secs(2), serving)
        .await
        .expect("serve still blocked after close")
        .unwrap();
    assert_eq!(r, Ok(()));
    // the port is free again: it can be bound by someone else, and a connect is refused
    assert!(
        TcpStream::connect(addr).await.is_err(),
        "the listener is still accepting"
    );
    let again = Server::listen_tcp(&addr.to_string(), opts(GOOD_TOK));
    assert!(again.is_ok(), "the port is still taken: {:?}", again.err());
    // serve on a closed server returns at once
    assert_eq!(srv.serve().await, Ok(()));
}

// A shutdown during the 5 s auth window does not wait for the window (Go does; PARITY 181).
#[tokio::test]
async fn close_during_the_auth_window_ends_serve_at_once() {
    let (srv, addr) = start_tcp(opts(GOOD_TOK));
    let mut c = Client::connect(addr).await;
    tokio::time::sleep(Duration::from_millis(100)).await; // admitted, waiting for the auth line
    let t0 = std::time::Instant::now();
    srv.close().await;
    assert_eq!(c.line("").await, "EOF", "the silent client was not dropped");
    assert!(t0.elapsed() < Duration::from_secs(2), "{:?}", t0.elapsed());
}

// One connection is served at a time, as in Go (one hub). R1-37 (PARITY 302): Go serves the
// next one only when the first is done; here a second client that passes the token check
// REPLACES the first (a restarted hub must not wait for a half-open predecessor), so there is
// still exactly one live connection, and notifications reach only it.
#[tokio::test]
async fn one_connection_at_a_time() {
    let (srv, addr) = start_tcp(opts(GOOD_TOK));
    let mut first = Client::connect(addr).await;
    assert!(first.auth(GOOD_TOK).await.contains(r#""ok":true"#));
    let mut second = Client::connect(addr).await;
    assert!(second.auth(GOOD_TOK).await.contains(r#""ok":true"#));
    assert_eq!(first.line("").await, "EOF", "two connections were live");
    srv.notify("ringing", &json!({"call_id": "c9"}))
        .await
        .unwrap();
    assert!(second.line("").await.contains("c9"));
}

// A notification reaches the authenticated client, and none is left over for the next one.
#[tokio::test]
async fn notifications_reach_only_an_authenticated_client() {
    let (srv, addr) = start_tcp(opts(GOOD_TOK));
    let mut c = Client::connect(addr).await;
    assert!(c.auth(GOOD_TOK).await.contains(r#""ok":true"#));
    srv.notify("ringing", &json!({"call_id": "c1"}))
        .await
        .unwrap();
    let r = c.line("").await;
    assert_eq!(
        r,
        "{\"method\":\"ringing\",\"params\":{\"call_id\":\"c1\"}}\n"
    );
    drop(c);
    tokio::time::sleep(Duration::from_millis(200)).await;
    // the hub is gone: the write fails (or finds no connection), it never panics or blocks
    let r = tokio::time::timeout(Duration::from_secs(8), srv.notify("ringing", &json!({}))).await;
    // R2-80 (R1-101, third part): not only "did not block": the write finds the dead connection
    assert_eq!(
        r.expect("notify blocked on a closed connection"),
        Err("write tcp: use of closed network connection".to_string())
    );
}

// The accept loop survives an error and a panic, logs a fixed text, and stops when closed.
#[tokio::test(start_paused = true)]
async fn serve_reconnects_survives_errors_and_panics() {
    let closed = Arc::new(AtomicBool::new(false));
    let calls = Arc::new(Mutex::new(0u32));
    let logs = Arc::new(Mutex::new(Vec::<String>::new()));
    let (c2, calls2) = (closed.clone(), calls.clone());
    let logs2 = logs.clone();
    serve_reconnects_with(
        || closed.load(Ordering::SeqCst),
        move || {
            let (c, calls) = (c2.clone(), calls2.clone());
            async move {
                let n = {
                    let mut g = calls.lock().unwrap();
                    *g += 1;
                    *g
                };
                match n {
                    1 => Err("accept boom".to_string()),
                    2 => panic!("serve exploded"),
                    3 => Ok(()),
                    _ => {
                        c.store(true, Ordering::SeqCst);
                        Ok(())
                    }
                }
            }
        },
        || async {},
        move |m| logs2.lock().unwrap().push(m.to_string()),
    )
    .await;
    assert_eq!(*calls.lock().unwrap(), 4);
    let logs = logs.lock().unwrap().clone();
    assert_eq!(
        logs,
        vec![
            "portico-transport: serve: accept boom (accepting again)".to_string(),
            "portico-transport: serve: the accept loop panicked; the hub connection was closed (accepting again)".to_string(),
        ]
    );
}

// What `logf` is given: the error of `serve` and fixed texts, never a token or a request line
// (R1-14: a real server, an authenticated client and an oversized line that starts with a
// secret-looking text; the earlier version counted the substring `logf(` in the source).
#[tokio::test]
async fn serve_reconnects_logs_fixed_texts_only() {
    let (srv, addr) = {
        let srv = Server::listen_tcp("127.0.0.1:0", opts(GOOD_TOK)).unwrap();
        let a = srv.tcp_addr().unwrap();
        (srv, a)
    };
    let logs = Arc::new(Mutex::new(Vec::<String>::new()));
    let l2 = logs.clone();
    let looper = srv.clone();
    let task = tokio::spawn(async move {
        serve_reconnects(&looper, move |m| l2.lock().unwrap().push(m.to_string())).await;
    });
    let mut c = Client::connect(addr).await;
    assert!(c.auth(GOOD_TOK).await.contains(r#""ok":true"#));
    let mut big = b"SECRET-LINE-R1-14 ".to_vec();
    big.resize(big.len() + (1 << 20) + 10, b'a');
    big.push(b'\n');
    let _ = c.wr.write_all(&big).await;
    for _ in 0..50 {
        if !logs.lock().unwrap().is_empty() {
            break;
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
    srv.close().await;
    let _ = tokio::time::timeout(Duration::from_secs(3), task).await;
    let logs = logs.lock().unwrap().clone();
    assert_eq!(
        logs,
        vec!["portico-transport: serve: bufio.Scanner: token too long (accepting again)"],
        "{logs:?}"
    );
    assert!(logs
        .iter()
        .all(|l| !l.contains("SECRET-LINE") && !l.contains(GOOD_TOK)));
}

// Go names a random one of several malformed numeric variables (it ranges over a map); this
// names the first in key order, always (PARITY 176).
#[test]
fn sip_config_error_order_is_deterministic() {
    for _ in 0..50 {
        let e = sip_config_from_env(&*env_of(&[
            ("PORTICO_SIP_LISTEN", "127.0.0.1:0"),
            ("PORTICO_SIP_RTP_PORT", "x"),
            ("PORTICO_SIP_RING_SECONDS", "y"),
            ("PORTICO_SIP_ORPHAN_SECONDS", "z"),
        ]))
        .unwrap_err();
        assert_eq!(e, "PORTICO_SIP_ORPHAN_SECONDS must be a number");
    }
    let e = sip_config_from_env(&*env_of(&[
        ("PORTICO_SIP_LISTEN", "127.0.0.1:0"),
        ("PORTICO_SIP_DIAL_ENABLED", "maybe"),
        ("PORTICO_SIP_ALLOW_NON_LOOPBACK", "perhaps"),
    ]))
    .unwrap_err();
    assert_eq!(e, "PORTICO_SIP_ALLOW_NON_LOOPBACK must be true or false");
}

// A token that is not valid UTF-8 (or holds U+FFFD) is refused at start, from every source.
#[tokio::test]
async fn a_token_that_is_not_utf8_is_refused() {
    let d = Dir::new();
    let bad = std::path::Path::new(&d.path("tok")).to_path_buf();
    let mut bytes = b"0123456789abcdef0123456789".to_vec();
    bytes.extend_from_slice(&[0xff, 0xfe, b'\n']);
    std::fs::write(&bad, bytes).unwrap();
    let mut f = flags("127.0.0.1:0");
    f.ipc_token_file = bad.to_str().unwrap().to_string();
    let e = open_ipc(&f, &*env_of(&[])).err().expect("accepted");
    assert!(e.contains("UTF-8"), "{e}");
    assert!(
        !e.contains("0123456789abcdef"),
        "the token is in the error: {e}"
    );
    let e = open_ipc(
        &flags("127.0.0.1:0"),
        &*env_of(&[("PORTICO_IPC_TOKEN", "0123456789abcdef0123456789\u{fffd}")]),
    )
    .err()
    .expect("accepted");
    assert!(e.contains("UTF-8"), "{e}");
}

// The default bind is loopback-only; the wildcard needs both flags, and then a connection from
// outside the list is closed before anything is read.
#[tokio::test]
async fn the_wildcard_needs_both_flags_and_honours_the_list() {
    let mut f = flags(":0");
    assert!(open_ipc(&f, &*env_of(&[("PORTICO_IPC_TOKEN", GOOD_TOK)])).is_err());
    f.ipc_allow_non_loopback = true;
    assert!(open_ipc(&f, &*env_of(&[("PORTICO_IPC_TOKEN", GOOD_TOK)])).is_err());
    f.ipc_allow_from = "10.99.0.0/16".into();
    let (srv, tcp) = open_ipc(&f, &*env_of(&[("PORTICO_IPC_TOKEN", GOOD_TOK)])).unwrap();
    assert!(tcp);
    let port = srv.tcp_addr().unwrap().port();
    let looper = srv.clone();
    tokio::spawn(async move {
        while !looper.is_closed() {
            let _ = looper.serve().await;
        }
    });
    // reachable through loopback (the wildcard), but 127.0.0.1 is outside the list
    let mut c = Client::connect(std::net::SocketAddr::from(([127, 0, 0, 1], port))).await;
    let r = c.auth(GOOD_TOK).await;
    assert!(
        !r.contains(r#""ok":true"#),
        "a source outside allow-from authenticated: {r:?}"
    );
    srv.close().await;
}

// parse_allow keeps Go's rules for the mapped, the zoned and the /0 forms.
#[test]
fn parse_allow_special_forms() {
    assert_eq!(
        parse_allow("::ffff:10.8.0.39").unwrap().to_string(),
        "10.8.0.39/32"
    );
    assert_eq!(
        parse_allow("10.8.7.9/16").unwrap(),
        parse_allow("10.8.0.0/16").unwrap()
    );
    assert_eq!(
        parse_allow("fe80::1%eth0").unwrap().to_string(),
        "fe80::1/128"
    );
    assert!(parse_allow("fe80::1%eth0/64").is_err());
    for z in ["0.0.0.0/0", "::/0", "1.2.3.4/0"] {
        assert!(parse_allow(z).unwrap_err().contains("/0"), "{z}");
    }
    let _: Prefix = parse_allow("10.0.0.0/8").unwrap();
}

/// The kernel's view of the server side of an accepted connection (Linux `/proc/net/tcp`): the
/// timer column is 2 while a keep-alive timer is armed, which needs SO_KEEPALIVE.
fn server_side_timer(server_port: u16, client_port: u16) -> Option<u32> {
    let text = std::fs::read_to_string("/proc/net/tcp").ok()?;
    for l in text.lines().skip(1) {
        let f: Vec<&str> = l.split_whitespace().collect();
        let port = |a: &str| u16::from_str_radix(a.rsplit(':').next()?, 16).ok();
        if f.len() > 6 && port(f[1]) == Some(server_port) && port(f[2]) == Some(client_port) {
            return u32::from_str_radix(f[5].split(':').next()?, 16).ok();
        }
    }
    None
}

// Go turns TCP keep-alive on for every accepted connection (PARITY 182): the slot must not be
// held forever by a hub host that vanished.
#[tokio::test]
async fn the_served_connection_has_keepalive_armed() {
    let (_srv, addr) = start_tcp(opts(GOOD_TOK));
    let mut c = Client::connect(addr).await;
    assert!(c.auth(GOOD_TOK).await.contains(r#""ok":true"#));
    tokio::time::sleep(Duration::from_millis(100)).await;
    let local = c.wr.local_addr().unwrap().port();
    let timer =
        server_side_timer(addr.port(), local).expect("the connection is not in /proc/net/tcp");
    assert_eq!(
        timer, 2,
        "no keep-alive timer on the accepted connection (timer column {timer})"
    );
}

// The configuration travels as JSON text exactly as Go marshals it (the numbers of the env are
// float64 there and print without a decimal point), so a driver sees the same bytes.
#[tokio::test]
async fn preload_hands_configure_go_s_json_text() {
    let cfg = sip_config_from_env(&*env_of(&[
        ("PORTICO_SIP_LISTEN", "127.0.0.1:0"),
        ("PORTICO_SIP_PASSWORD", "s3cretpass"),
        ("PORTICO_SIP_RTP_PORT", "5004"),
        ("PORTICO_SIP_RING_SECONDS", "30"),
        ("PORTICO_SIP_ALLOW_NON_LOOPBACK", "false"),
    ]))
    .unwrap();
    let seen = Mutex::new(String::new());
    let set = transport_rs::standalone::PreloadedSet::default();
    transport_rs::standalone::preload_sip(
        |p: Params| {
            *seen.lock().unwrap() = transport_rs::ipc::raw_str(&p).to_string();
            async { Ok::<Reply, IpcError>(Reply::Json(json!({"ok": true}))) }
        },
        &set,
        cfg,
    )
    .await
    .unwrap();
    assert_eq!(
        *seen.lock().unwrap(),
        r#"{"driver":"gate_sip_server","config":{"allow_non_loopback":false,"device_id":"gate-sip-server:127.0.0.1:0","password":"s3cretpass","ring_seconds":30,"rtp_port":5004,"sip_listen":"127.0.0.1:0"}}"#
    );
    assert_eq!(
        set.keys(),
        vec!["device_id:gate-sip-server:127.0.0.1:0".to_string()]
    );
}

// R1-13 (PARITY 300): a panic in an inline handler no longer ends the serving task (it used to,
// and the hub's connection was then closed by `serve_reconnects`): it is answered with
// `internal_error` and the connection goes on. The serving task's own panic path stays covered
// by `serve_reconnects_survives_errors_and_panics`.
#[tokio::test]
async fn a_panic_in_an_inline_handler_is_answered_and_the_connection_goes_on() {
    let srv = Server::listen_tcp("127.0.0.1:0", opts(GOOD_TOK)).unwrap();
    srv.handle_inline("boom", |_p: Params| async {
        if true {
            panic!("inline handler exploded");
        }
        Ok::<Reply, IpcError>(Reply::Json(json!({})))
    });
    srv.handle("ping", |_p: Params| async {
        Ok::<Reply, IpcError>(Reply::Json(json!({"pong": true})))
    });
    let addr = srv.tcp_addr().unwrap();
    let logs = Arc::new(Mutex::new(Vec::<String>::new()));
    let (s2, l2) = (srv.clone(), logs.clone());
    let serving = tokio::spawn(async move {
        serve_reconnects(&s2, move |m| l2.lock().unwrap().push(m.to_string())).await
    });
    let mut c = Client::connect(addr).await;
    assert!(c.auth(GOOD_TOK).await.contains(r#""ok":true"#));
    let r = c.line(r#"{"id":2,"method":"boom"}"#).await;
    assert!(r.contains("internal_error"), "{r:?}");
    assert!(!r.contains("exploded"), "{r:?}");
    assert!(c.line(r#"{"id":3,"method":"ping"}"#).await.contains("pong"));
    assert!(
        logs.lock().unwrap().is_empty(),
        "{:?}",
        logs.lock().unwrap()
    );
    srv.close().await;
    let _ = tokio::time::timeout(Duration::from_secs(3), serving).await;
}

// Secret files: a regular file, at most 64 KiB; a loose mode warns; the .env cap is 4 MiB.
#[test]
fn secret_files_are_regular_small_and_warn_when_loose() {
    use std::os::unix::fs::PermissionsExt;
    let d = Dir::new();
    let get = env_of(&[]);
    for dev in ["/dev/zero", "/dev/null"] {
        let mut f = flags("127.0.0.1:0");
        f.ipc_token_file = dev.into();
        let e = open_ipc(&f, &*get).err().expect(dev);
        assert!(e.contains("not a regular file"), "{dev}: {e}");
    }
    // R1-15: a FIFO with no writer used to block `open` for ever; it is refused at once.
    // (Run on a thread: without the fix this hangs, so the test must fail, not wedge.)
    let fifo = d.path("fifo");
    assert!(std::process::Command::new("mkfifo")
        .arg(&fifo)
        .status()
        .unwrap()
        .success());
    let get2 = get.clone();
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let mut f = flags("127.0.0.1:0");
        f.ipc_token_file = fifo;
        let _ = tx.send(open_ipc(&f, &*get2).err());
    });
    let e = rx
        .recv_timeout(Duration::from_secs(5))
        .expect("opening a FIFO blocked")
        .expect("a FIFO was accepted");
    assert!(e.contains("not a regular file"), "fifo: {e}");
    let mut f = flags("127.0.0.1:0");
    f.ipc_token_file = d.file("big", &"a".repeat(64 * 1024 + 1));
    let e = open_ipc(&f, &*get).err().unwrap();
    assert!(
        e.contains("larger than 65536 bytes") && !e.contains("aaaa"),
        "{e}"
    );
    // exactly the cap is read (and then fails only on the token rules? 64 KiB of 'a' is a token)
    let mut f2 = flags("127.0.0.1:0");
    f2.ipc_token_file = d.file("edge", &"a".repeat(64 * 1024));
    let rt = tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap();
    rt.block_on(async {
        let (srv, _) = open_ipc(&f2, &*get).expect("a file of exactly the cap");
        srv.close().await;
    });
    // the password file
    let big = d.file("pwbig", &"p".repeat(64 * 1024 + 1));
    let e = sip_config_from_env(&*env_of(&[
        ("PORTICO_SIP_LISTEN", "127.0.0.1:0"),
        ("PORTICO_SIP_PASSWORD_FILE", &big),
    ]))
    .unwrap_err();
    assert!(e.contains("larger than"), "{e}");
    // the .env
    let big_env = d.file(
        "big.env",
        &"OTHER=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n".repeat(70_000),
    );
    let e = load_secrets_env(&big_env).unwrap_err();
    assert!(e.contains("larger than 4194304 bytes"), "{e}");
    let e = load_secrets_env("/dev/zero").unwrap_err();
    assert!(e.contains("not a regular file"), "{e}");
    // the warning for a loose mode (and none for 0600)
    let tight = d.file("tight", "x\n");
    std::fs::set_permissions(&tight, std::fs::Permissions::from_mode(0o600)).unwrap();
    let loose = d.file("loose.tok", "y\n");
    std::fs::set_permissions(&loose, std::fs::Permissions::from_mode(0o644)).unwrap();
    let _ = sip_config_from_env(&*env_of(&[
        ("PORTICO_SIP_LISTEN", "127.0.0.1:0"),
        ("PORTICO_SIP_PASSWORD_FILE", &tight),
    ]));
    let _ = sip_config_from_env(&*env_of(&[
        ("PORTICO_SIP_LISTEN", "127.0.0.1:0"),
        ("PORTICO_SIP_PASSWORD_FILE", &loose),
    ]));
    let w = transport_rs::secretfile::warnings();
    let mine: Vec<&String> = w
        .iter()
        .filter(|l| l.contains("0644") && l.contains("PORTICO_SIP_PASSWORD_FILE"))
        .collect();
    assert!(!mine.is_empty(), "no warning for a 0644 secret file: {w:?}");
    assert!(
        mine.iter().all(|l| l.contains("PORTICO_SIP_PASSWORD_FILE")
            && !l.contains(&loose)
            && !l.contains(&tight)),
        "{mine:?}"
    );
    assert!(
        w.iter().all(|l| !l.contains("0600")),
        "a 0600 file warned: {w:?}"
    );
    assert!(
        transport_rs::secretfile::loose_mode(0o640) && transport_rs::secretfile::loose_mode(0o604)
    );
    assert!(
        !transport_rs::secretfile::loose_mode(0o700)
            && !transport_rs::secretfile::loose_mode(0o600)
    );
}

// A token with white space from the variable or the token file works (Go parity) and warns.
#[tokio::test]
async fn a_token_with_white_space_warns_once_at_start() {
    let tok = "has space 0123456789abcdefghij";
    let n_before = transport_rs::secretfile::warnings()
        .iter()
        .filter(|l| l.contains("white space"))
        .count();
    let (srv, _) = open_ipc(
        &flags("127.0.0.1:0"),
        &*env_of(&[("PORTICO_IPC_TOKEN", tok)]),
    )
    .expect("accepted");
    srv.close().await;
    let w = transport_rs::secretfile::warnings();
    let ws: Vec<&String> = w.iter().filter(|l| l.contains("white space")).collect();
    assert_eq!(
        ws.len(),
        n_before + 1,
        "exactly one warning at start: {w:?}"
    );
    assert!(
        w.iter()
            .all(|l| !l.contains("has space") && !l.contains("0123456789abcdefghij")),
        "{w:?}"
    );
    // a clean token does not add one
    let n = ws.len();
    let (srv, _) = open_ipc(
        &flags("127.0.0.1:0"),
        &*env_of(&[("PORTICO_IPC_TOKEN", GOOD_TOK)]),
    )
    .unwrap();
    srv.close().await;
    let w = transport_rs::secretfile::warnings();
    assert_eq!(
        w.iter().filter(|l| l.contains("white space")).count(),
        n,
        "a clean token added a warning: {w:?}"
    );
}

// ---- R1-37: admission no longer serial ----------------------------------------------------

// One silent connection used to hold the only admission slot for the 5 s window.
#[tokio::test]
async fn a_silent_connection_does_not_hold_up_an_authentic_one() {
    let (_srv, addr) = start_tcp(opts(GOOD_TOK));
    let _silent = TcpStream::connect(addr).await.unwrap();
    tokio::time::sleep(Duration::from_millis(100)).await;
    let t = std::time::Instant::now();
    let mut c = Client::connect(addr).await;
    let r = c.auth(GOOD_TOK).await;
    assert!(r.contains(r#""ok":true"#), "{r:?}");
    assert!(
        t.elapsed() < Duration::from_secs(2),
        "waited {:?} behind a silent connection",
        t.elapsed()
    );
}

// An authenticated but half-open hub connection used to hold the serving slot (150 s of
// keep-alive); a newcomer that passes the token check now replaces it.
#[tokio::test]
async fn a_newly_authenticated_hub_replaces_a_half_open_one() {
    let (_srv, addr) = start_tcp(opts(GOOD_TOK));
    let mut old = Client::connect(addr).await;
    assert!(old.auth(GOOD_TOK).await.contains(r#""ok":true"#));
    assert!(old
        .line(r#"{"id":2,"method":"ping"}"#)
        .await
        .contains("pong"));
    let mut new = Client::connect(addr).await;
    let r = new.auth(GOOD_TOK).await;
    assert!(
        r.contains(r#""ok":true"#),
        "the newcomer was not read: {r:?}"
    );
    assert!(new
        .line(r#"{"id":3,"method":"ping"}"#)
        .await
        .contains("pong"));
    assert_eq!(old.line("").await, "EOF", "the old connection stayed open");
}

// At most four connections wait to authenticate at the same time. R2-11: the fifth does not
// get closed unread (that let four silent connections lock the hub out); it takes the place of
// the OLDEST one, and is read like any other.
#[tokio::test]
async fn a_fifth_waiting_connection_drops_the_oldest_not_the_newcomer() {
    let (_srv, addr) = start_tcp(opts(GOOD_TOK));
    let mut held = Vec::new();
    for _ in 0..4 {
        held.push(Client::connect(addr).await);
        tokio::time::sleep(Duration::from_millis(30)).await;
    }
    let mut fifth = Client::connect(addr).await;
    let t = std::time::Instant::now();
    let r = fifth.auth(GOOD_TOK).await;
    assert!(
        r.contains(r#""ok":true"#),
        "the newcomer was not read: {r:?}"
    );
    assert!(t.elapsed() < Duration::from_secs(2), "{:?}", t.elapsed());
    // the oldest was dropped; the younger three are still waiting for their window
    assert_eq!(held[0].line("").await, "EOF", "the oldest stayed open");
    let mut younger = held.split_off(1);
    for c in &mut younger {
        let mut s = String::new();
        let r = tokio::time::timeout(Duration::from_millis(200), c.rd.read_line(&mut s)).await;
        assert!(
            r.is_err(),
            "a younger waiting connection was dropped: {s:?}"
        );
    }
}

// R2-11: a flood of silent connections cannot keep the right token out: each newcomer is read,
// and the hub's own connection is never the one refused.
#[tokio::test]
async fn silent_connections_in_a_flood_do_not_lock_the_hub_out() {
    let (_srv, addr) = start_tcp(opts(GOOD_TOK));
    let mut silent = Vec::new();
    for _ in 0..12 {
        silent.push(TcpStream::connect(addr).await.unwrap());
    }
    let mut hub = Client::connect(addr).await;
    let r = hub.auth(GOOD_TOK).await;
    assert!(r.contains(r#""ok":true"#), "{r:?}");
}

// R2-10: a source outside -ipc-allow-from is closed before it takes an admission place. Both
// orders of the burst are tried: the hub connects first and has not sent its line yet when the
// flood arrives (the flood must not push it out of its place), and the hub follows the flood.
// (127.0.0.2 is loopback on Linux, and outside 127.0.0.1/32.) The connects are blocking: the
// kernel completes every handshake before the acceptor task has run once, so the whole burst is
// accepted in one go (an `.await` between them would let the server check each peer before the
// next one arrives, and hide the order of the checks).
async fn burst_and_hub(hub_first: bool) {
    let (_srv, addr) = start_tcp(TcpOptions {
        token: GOOD_TOK.into(),
        allow_non_loopback: false,
        allow_from: vec![parse_allow("127.0.0.1/32").unwrap()],
    });
    let connect_hub = || {
        let sock = std::net::TcpStream::connect(addr).unwrap();
        sock.set_nonblocking(true).unwrap();
        let (rd, wr) = TcpStream::from_std(sock).unwrap().into_split();
        Client {
            rd: BufReader::new(rd),
            wr,
        }
    };
    let mut hub = hub_first.then(connect_hub);
    let mut flood = Vec::new();
    for _ in 0..8 {
        let sock =
            socket2::Socket::new(socket2::Domain::IPV4, socket2::Type::STREAM, None).unwrap();
        sock.bind(
            &"127.0.0.2:0"
                .parse::<std::net::SocketAddr>()
                .unwrap()
                .into(),
        )
        .unwrap();
        sock.connect(&addr.into()).unwrap();
        sock.set_nonblocking(true).unwrap();
        flood.push(TcpStream::from_std(sock.into()).unwrap());
    }
    let mut hub = hub.take().unwrap_or_else(connect_hub);
    let r = hub.auth(GOOD_TOK).await;
    assert!(
        r.contains(r#""ok":true"#),
        "the hub was locked out (hub first: {hub_first}): {r:?}"
    );
    // and every one of them was closed at once, unread
    for mut f in flood {
        let mut b = [0u8; 1];
        let n = tokio::time::timeout(
            Duration::from_secs(1),
            tokio::io::AsyncReadExt::read(&mut f, &mut b),
        )
        .await;
        assert!(matches!(n, Ok(Ok(0)) | Ok(Err(_))), "{n:?}");
    }
}

#[tokio::test]
async fn a_burst_from_a_disallowed_source_does_not_push_out_a_hub_that_came_first() {
    burst_and_hub(true).await;
}

#[tokio::test]
async fn a_burst_from_a_disallowed_source_does_not_lock_the_hub_out() {
    burst_and_hub(false).await;
}

// R3A-1 (PARITY 400): the hub came FIRST, wrote its `auth` line, and four more connections from an
// ALLOWED source were accepted in the same batch (blocking connects: the acceptor has not run
// once before all five are in the backlog). The eviction of the oldest place must not drop a
// connection whose line is already in the socket: the hub is read and answered, the silent
// newcomers are what is dropped.
#[tokio::test]
async fn a_hub_that_came_first_with_its_line_written_is_read_when_four_more_follow() {
    let (_srv, addr) = start_tcp(opts(GOOD_TOK));
    let mut hub_sock = std::net::TcpStream::connect(addr).unwrap();
    std::io::Write::write_all(
        &mut hub_sock,
        format!("{{\"id\":1,\"method\":\"auth\",\"params\":{{\"token\":\"{GOOD_TOK}\"}}}}\n")
            .as_bytes(),
    )
    .unwrap();
    let silent: Vec<std::net::TcpStream> = (0..4)
        .map(|_| std::net::TcpStream::connect(addr).unwrap())
        .collect();
    hub_sock.set_nonblocking(true).unwrap();
    let (rd, wr) = TcpStream::from_std(hub_sock).unwrap().into_split();
    let mut hub = Client {
        rd: BufReader::new(rd),
        wr,
    };
    let mut line = String::new();
    let r = tokio::time::timeout(Duration::from_secs(3), hub.rd.read_line(&mut line)).await;
    assert!(
        matches!(r, Ok(Ok(n)) if n > 0) && line.contains(r#""ok":true"#),
        "the hub that came first was dropped unread: {r:?} {line:?}"
    );
    drop(silent);
}

// R2-11 (PARITY 302, 400): three wrong tokens from one source within 10 s slow that source's
// FAILURES by 5 s (the reply and the close come late); one or two cost nothing (the smoke flow
// sends one wrong token and then the right one). The source is still read, and the right token
// is served at once: on loopback every client has the same address, so anything else would let
// any local process lock the hub out.
#[tokio::test]
async fn wrong_tokens_slow_their_own_reply_and_never_the_right_token() {
    let (_srv, addr) = start_tcp(opts(GOOD_TOK));
    for i in 0..2 {
        let mut bad = Client::connect(addr).await;
        let r = bad.auth("not-the-token").await;
        assert!(r.contains("unauthorized"), "{i}: {r:?}");
    }
    let mut ok = Client::connect(addr).await;
    assert!(
        ok.auth(GOOD_TOK).await.contains(r#""ok":true"#),
        "two wrong tokens must not cost the hub"
    );
    // the right token does not reset the strikes; the third wrong one is answered 5 s late
    let t0 = std::time::Instant::now();
    let mut third = Client::connect(addr).await;
    third
        .wr
        .write_all(
            br#"{"id":1,"method":"auth","params":{"token":"not-the-token"}}
"#,
        )
        .await
        .unwrap();
    // meanwhile the right token, from the same address, is served at once, again and again
    for _ in 0..3 {
        let t = std::time::Instant::now();
        let mut c = Client::connect(addr).await;
        let r = c.auth(GOOD_TOK).await;
        assert!(
            r.contains(r#""ok":true"#),
            "served during the back-off: {r:?}"
        );
        assert!(t.elapsed() < Duration::from_secs(2), "{:?}", t.elapsed());
    }
    let mut s = String::new();
    let early = tokio::time::timeout(Duration::from_millis(500), third.rd.read_line(&mut s)).await;
    assert!(early.is_err(), "the slowed reply came at once: {s:?}");
    let late = tokio::time::timeout(Duration::from_secs(8), third.rd.read_line(&mut s)).await;
    assert!(matches!(late, Ok(Ok(n)) if n > 0), "{late:?}");
    assert!(s.contains("unauthorized"), "{s:?}");
    let waited = t0.elapsed();
    assert!(
        waited >= Duration::from_millis(4500) && waited < Duration::from_secs(8),
        "{waited:?}"
    );
}

// R2-11: the slowed replies are bounded (8 held at once); the rest are closed without a reply,
// and none of them holds an admission place (the hub still gets in).
#[tokio::test]
async fn slowed_replies_are_capped_and_hold_no_admission_place() {
    let (_srv, addr) = start_tcp(opts(GOOD_TOK));
    let mut held = Vec::new();
    for i in 0..11 {
        let mut c = Client::connect(addr).await;
        c.wr.write_all(
            br#"{"id":1,"method":"auth","params":{"token":"not-the-token"}}
"#,
        )
        .await
        .unwrap();
        if i < 2 {
            let mut s = String::new();
            c.rd.read_line(&mut s).await.unwrap();
            assert!(s.contains("unauthorized"), "{i}: {s:?}");
        } else {
            tokio::time::sleep(Duration::from_millis(60)).await;
        }
        held.push(c);
    }
    // 2 answered at once, 8 held back, the 11th was closed without a word
    for (i, c) in held.iter_mut().enumerate().skip(2) {
        let mut s = String::new();
        let r = tokio::time::timeout(Duration::from_millis(300), c.rd.read_line(&mut s)).await;
        if i < 10 {
            assert!(r.is_err(), "{i}: answered early: {s:?}");
        } else {
            assert!(matches!(r, Ok(Ok(0))), "{i}: {r:?} {s:?}");
            assert!(s.is_empty(), "{i}: replied: {s:?}");
        }
    }
    let mut hub = Client::connect(addr).await;
    assert!(hub.auth(GOOD_TOK).await.contains(r#""ok":true"#));
}

// R2-12 (TCP): a request line that never ends is bounded after the handshake too. 1 MiB + 1
// bytes with no newline, the connection kept open: `serve` ends with `token too long`.
#[tokio::test]
async fn an_endless_request_line_ends_the_tcp_connection() {
    let srv = Server::listen_tcp("127.0.0.1:0", opts(GOOD_TOK)).unwrap();
    let addr = srv.tcp_addr().unwrap();
    let s2 = srv.clone();
    let serving = tokio::spawn(async move { s2.serve().await });
    let mut c = Client::connect(addr).await;
    assert!(c.auth(GOOD_TOK).await.contains(r#""ok":true"#));
    let big = vec![b'A'; transport_rs::ipc::MAX_LINE + 1];
    let _ = c.wr.write_all(&big).await; // no newline; the socket stays open
    let r = tokio::time::timeout(Duration::from_secs(1), serving)
        .await
        .expect("serve kept reading an endless line")
        .unwrap();
    assert_eq!(r, Err(transport_rs::ipc::ERR_TOO_LONG.to_string()));
    srv.close().await;
}

// PARITY 401 (R2-53). A final auth line with no newline IS a token when the client finishes
// (EOF), as in Go ...
#[tokio::test]
async fn an_auth_line_without_a_newline_at_eof_is_still_a_token() {
    let (_srv, addr) = start_tcp(opts(GOOD_TOK));
    let mut c = Client::connect(addr).await;
    let line = format!(r#"{{"id":4,"method":"auth","params":{{"token":"{GOOD_TOK}"}}}}"#);
    c.wr.write_all(line.as_bytes()).await.unwrap();
    c.wr.shutdown().await.unwrap();
    let mut s = String::new();
    c.rd.read_line(&mut s).await.unwrap();
    assert!(s.contains(r#""ok":true"#), "{s:?}");
}

// ... but, held open, it is NOT one when the 5 s window runs out. Go's Scanner hands the tail
// out on the deadline error and answers `{"ok":true}`; this server closes without a reply.
#[tokio::test]
async fn an_unterminated_auth_line_held_open_is_closed_unanswered_at_the_window() {
    let (_srv, addr) = start_tcp(opts(GOOD_TOK));
    let mut c = Client::connect(addr).await;
    let line = format!(r#"{{"id":4,"method":"auth","params":{{"token":"{GOOD_TOK}"}}}}"#);
    c.wr.write_all(line.as_bytes()).await.unwrap();
    let t = std::time::Instant::now();
    let mut s = String::new();
    let r = tokio::time::timeout(Duration::from_secs(8), c.rd.read_line(&mut s)).await;
    assert!(
        matches!(r, Ok(Ok(0))),
        "closed without a reply: {r:?} {s:?}"
    );
    assert!(s.is_empty(), "replied: {s:?}");
    assert!(
        t.elapsed() >= Duration::from_secs(4),
        "closed before the window ran out: {:?}",
        t.elapsed()
    );
}
