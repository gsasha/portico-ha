use super::*;
use crate::driver::registry;
use serde_json::json;

fn map(v: Value) -> Map<String, Value> {
    v.as_object().unwrap().clone()
}

fn base() -> Map<String, Value> {
    map(json!({"sip_listen": "127.0.0.1:15060", "password": "12345678"}))
}

fn token() -> CancelToken {
    CancelToken::new()
}

// TestWired_BinaryImportsItButNothingStartsWithoutAConfiguredDevice (misc_test.go): the
// half that holds in Rust. A fresh driver owns no server and Configure binds nothing; the
// check that cmd/.../main.go imports the package has no Rust analogue (the dispatcher
// calls `registry()`, see `src/dispatch/`).
#[test]
fn nothing_starts_without_a_configured_device() {
    let mut d = GateSipServer::default();
    assert!(d.server().is_none(), "a fresh driver already owns a server");
    d.configure(&map(
        json!({"sip_listen": "127.0.0.1:0", "password": "12345678"}),
    ))
    .unwrap();
    assert!(
        d.server().is_none(),
        "Configure started a server: only Start may bind"
    );
}

// TestDeviceIDIsWhatTheHubKeysItBy (misc_test.go)
#[test]
fn device_id_is_what_the_hub_keys_it_by() {
    let mut d = GateSipServer::default();
    assert_eq!(
        d.device_id().as_deref(),
        Some(""),
        "unconfigured DeviceID is empty"
    );
    d.configure(&base()).unwrap();
    assert_eq!(d.device_id().unwrap(), "gate-sip-server:127.0.0.1:15060");
    let mut d2 = GateSipServer::default();
    let mut c = base();
    c.insert("device_id".into(), json!("srv-1"));
    d2.configure(&c).unwrap();
    assert_eq!(d2.device_id().unwrap(), "srv-1");
}

// TestWired_OnlyTheServerModuleNamesTheKeyInPythonDrivers (misc_test.go): reads the Python
// drivers' folder next to the crate. Skips (like Go) when the repository root is not there.
#[test]
fn only_the_server_module_names_the_key_in_python_drivers() {
    let dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../custom_components/portico/door/drivers");
    let Ok(rd) = std::fs::read_dir(&dir) else {
        eprintln!("repo root not mounted: skipping ({})", dir.display());
        return;
    };
    let mut saw_server = false;
    let mut n = 0;
    for e in rd.flatten() {
        n += 1;
        let name = e.file_name().to_string_lossy().into_owned();
        let text = std::fs::read(e.path()).unwrap_or_default();
        let names = String::from_utf8_lossy(&text).contains(KEY);
        match name.as_str() {
            "gate_sip_server.py" => {
                saw_server = true;
                assert!(names, "{name} does not name {KEY:?}");
            }
            "__init__.py" => {}
            _ => assert!(
                !names,
                "{name} names {KEY:?}: another driver could select the SIP-server mode"
            ),
        }
    }
    assert!(
        n > 0,
        "{} is empty: the guard would pass without reading anything",
        dir.display()
    );
    assert!(saw_server, "drivers/gate_sip_server.py is missing");
}

// TestParity_DoesNotStampHomeAssistant (parity_test.go): the Go test fails if the package
// imports net/http or net/rpc. The Rust analogue has two halves: no HTTP client among the
// crate's dependencies, and none of this package's sources (the part before each file's
// test module) names the crate's own hand-written HTTP client, the other drivers that talk
// to Home Assistant or a TCP stream.
#[test]
fn the_crate_has_no_http_client() {
    let toml = std::fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/Cargo.toml")).unwrap();
    let deps: Vec<&str> = toml
        .split("\n[")
        .filter(|sec| sec.starts_with("dependencies]") || sec.starts_with("dev-dependencies]"))
        .flat_map(|sec| sec.lines().skip(1))
        .filter_map(|l| l.split('=').next().map(str::trim))
        .filter(|n| !n.is_empty() && !n.starts_with('#'))
        .collect();
    assert!(
        deps.contains(&"tokio"),
        "the dependency scan read nothing: {deps:?}"
    );
    for d in deps {
        for bad in [
            "http", "hyper", "reqwest", "ureq", "curl", "isahc", "surf", "tonic", "rpc",
        ] {
            assert!(
                !d.contains(bad),
                "dependency {d} gives the crate a way to talk to Home Assistant"
            );
        }
    }
    let dir = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("src/driver/gatesipserver");
    // the directory and its subdirectories (`server/dial.rs` is a child of `server/mod.rs`)
    fn walk(d: &std::path::Path, out: &mut Vec<std::path::PathBuf>) {
        for e in std::fs::read_dir(d).unwrap() {
            let p = e.unwrap().path();
            if p.is_dir() {
                walk(&p, out);
            } else {
                out.push(p);
            }
        }
    }
    let mut all = Vec::new();
    walk(&dir, &mut all);
    let mut files = 0;
    for p in all {
        // a test-only file (`tests.rs`, `#[cfg(test)] mod tests;`) is the same as the stripped module
        if p.extension().is_none_or(|x| x != "rs") || p.file_name().is_some_and(|n| n == "tests.rs")
        {
            continue;
        }
        files += 1;
        let src = std::fs::read_to_string(&p).unwrap();
        let src = src.split("#[cfg(test)]\nmod tests").next().unwrap();
        for bad in [
            "gatesipobserver",
            "unifiprotect",
            "TcpStream",
            "TcpListener",
            "reqwest",
            "hyper",
            "http://",
            "https://",
            // Go's TestParity_DoesNotStampHomeAssistant also scans its sources for these two
            "input_text.set_value",
            "/api/services",
        ] {
            assert!(!src.contains(bad), "{} mentions {bad:?}", p.display());
        }
    }
    assert!(files >= 7, "the source scan read {files} files");
}

// TestDriverContract (misc_test.go)
#[tokio::test]
async fn driver_contract() {
    assert!(
        registry().contains_key(KEY),
        "not registered in the package's own registry"
    );
    let d = new();
    let t = token();
    let e = d.dial(&t, MediaTarget::default()).await.unwrap_err();
    assert!(e.message().contains("not_supported"), "Dial: {e}");
    let e = d.open_relay(&t).await.unwrap_err();
    assert!(e.message().contains("not_supported"), "OpenRelay: {e}");
    assert!(
        d.answer(&t, MediaTarget::default()).await.is_err(),
        "Answer before Start succeeded"
    );
    let caps = d.capabilities();
    assert!(
        !caps.has_relay
            && !caps.direct_capable
            && !caps.reports_availability
            && caps.inbound_audio == crate::driver::AudioTopology::SIP_RTP,
        "capabilities {caps:?}"
    );
}

// TestConfigRanges (misc_test.go)
#[test]
fn config_ranges() {
    let base = || map(json!({"sip_listen": "127.0.0.1:5060", "password": "s3cretpass"}));
    for (k, v) in [
        ("ring_seconds", json!(0)),
        ("orphan_seconds", json!(121)),
        ("rtp_port", json!(70000)),
    ] {
        let mut c = base();
        c.insert(k.into(), v.clone());
        assert!(
            GateSipServer::default().configure(&c).is_err(),
            "{k}={v} accepted"
        );
    }
    let mut c = base();
    // IPC hands numbers over as float64
    c.insert("ring_seconds".into(), json!(30.0));
    c.insert("orphan_seconds".into(), json!(20.0));
    c.insert("rtp_port".into(), json!(40000.0));
    let mut d = GateSipServer::default();
    d.configure(&c).unwrap();
    let cfg = d.config();
    assert_eq!(cfg.ring_limit, std::time::Duration::from_secs(30));
    assert_eq!(cfg.orphan_grace, std::time::Duration::from_secs(20));
    assert_eq!(
        (cfg.rtp_port, cfg.user.as_str(), cfg.realm.as_str()),
        (40000, "8001", "VDP")
    );
}

// TestParity_PasswordFloor (parity_test.go)
#[test]
fn password_floor() {
    for pw in ["", "1234567", "1234567890123456"] {
        let mut d = GateSipServer::default();
        let c = map(json!({"sip_listen": "127.0.0.1:0", "password": pw}));
        assert!(
            d.configure(&c).is_err(),
            "password {pw:?} ({} chars) accepted",
            pw.len()
        );
    }
    for pw in ["12345678", "123456789012345"] {
        let mut d = GateSipServer::default();
        let c = map(json!({"sip_listen": "127.0.0.1:0", "password": pw}));
        d.configure(&c)
            .unwrap_or_else(|e| panic!("password {pw:?} refused: {e}"));
    }
}

// TestParity_AllowFromAndLoopbackDefault (parity_test.go): the configuration half. The
// half that sends datagrams from 127.0.0.2 at a running server is in `tests/server_parity.rs`.
#[test]
fn allow_from_and_loopback_default() {
    let cases = [
        (
            "wildcard needs the switch",
            json!({"sip_listen": "0.0.0.0:5060", "password": "s3cretpass"}),
        ),
        (
            "public ip needs the switch",
            json!({"sip_listen": "192.168.2.1:5060", "password": "s3cretpass"}),
        ),
        (
            "switch alone is not enough",
            json!({"sip_listen": "0.0.0.0:5060", "password": "s3cretpass", "allow_non_loopback": true}),
        ),
        (
            "allow_from /0 is everyone",
            json!({"sip_listen": "0.0.0.0:5060", "password": "s3cretpass", "allow_non_loopback": true, "allow_from": ["0.0.0.0/0"]}),
        ),
        (
            "allow_from garbage",
            json!({"sip_listen": "127.0.0.1:5060", "password": "s3cretpass", "allow_from": ["nope"]}),
        ),
        (
            "no sip_listen, no default",
            json!({"password": "s3cretpass"}),
        ),
        ("no password", json!({"sip_listen": "127.0.0.1:5060"})),
    ];
    for (name, cfg) in cases {
        assert!(
            GateSipServer::default().configure(&map(cfg)).is_err(),
            "{name}: accepted"
        );
    }
    GateSipServer::default()
        .configure(&map(
            json!({"sip_listen": "127.0.0.1:5060", "password": "s3cretpass"}),
        ))
        .expect("loopback default refused");
}

// DEV-811 P3: `call_stats` is on only for a literal `true`; absent, false, a string, a number are
// off, and the key never makes a configure fail (an older wheel ignores it the same way).
#[test]
fn call_stats_is_on_only_for_true() {
    let base = map(json!({"sip_listen": "127.0.0.1:0", "password": "s3cretpass"}));
    assert!(!parse_config(&base).unwrap().call_stats);
    for (v, on) in [
        (json!(true), true),
        (json!(false), false),
        (json!("true"), false),
        (json!(1), false),
        (Value::Null, false),
    ] {
        let mut m = base.clone();
        m.insert("call_stats".into(), v.clone());
        assert_eq!(parse_config(&m).unwrap().call_stats, on, "{v}");
    }
    let mut m = base.clone();
    m.insert("some_future_key".into(), json!(1));
    parse_config(&m).expect("unknown keys are ignored");
}

// TestDialEnabledParsesLikeTheObserversAndNeedsNothingElse (dial_test.go)
#[test]
fn dial_enabled_parses_like_the_observers_and_needs_nothing_else() {
    let base = map(json!({"sip_listen": "127.0.0.1:0", "password": "s3cretpass"}));
    let with = |v: Option<Value>| {
        let mut m = base.clone();
        if let Some(v) = v {
            m.insert("dial_enabled".into(), v);
        }
        parse_config(&m)
    };
    let cases: [(&str, Option<Value>, bool, bool); 7] = [
        ("absent", None, false, false),
        ("null", Some(Value::Null), false, false),
        ("false", Some(json!(false)), false, false),
        ("true", Some(json!(true)), true, false),
        ("string true", Some(json!("true")), false, true),
        ("number 1", Some(json!(1)), false, true),
        ("string yes", Some(json!("yes")), false, true),
    ];
    for (name, v, on, bad) in cases {
        match with(v) {
            Ok(c) => assert!(
                !bad && c.dial_enabled == on,
                "{name}: dialEnabled={}",
                c.dial_enabled
            ),
            Err(e) => {
                assert!(bad, "{name}: {e}");
                assert!(
                    e.contains("dial_enabled must be true or false"),
                    "{name}: error text {e:?}"
                );
            }
        }
    }
}

// TestCapabilitiesBytesWithDialOffAreWhatTheyWere (dial_test.go)
#[test]
fn capabilities_bytes_with_dial_off_are_what_they_were() {
    let mut d = GateSipServer::default();
    d.configure(&map(
        json!({"sip_listen": "127.0.0.1:0", "password": "s3cretpass"}),
    ))
    .unwrap();
    // DEV-518: only StationRegistered (false) is new on the wire; every old byte is as it was
    let want = Capabilities {
        station_registered: Some(false),
        features: vec!["call_stats"],
        ..Capabilities::default()
    };
    assert_eq!(d.capabilities(), want);
    d.configure(&map(
        json!({"sip_listen": "127.0.0.1:0", "password": "s3cretpass", "dial_enabled": true}),
    ))
    .unwrap();
    assert_eq!(d.capabilities(), want, "not started, not registered");
}

// The three gate_sip_server cases of TestConfigureReplyBytesOfEveryShippedDriverAreUnchanged
// and TestServerCanDialAppearsOnTheWireOnlyOnceTheStationIsRegistered, without the
// registration: the CanDial rule is `capabilities_for`. The golden test replays the same
// bytes from the real Go drivers (tests/golden.rs `golden_capabilities`).
#[test]
fn can_dial_is_the_last_key_only_when_dial_is_on_and_the_station_registered() {
    let old = "{\"InboundAudio\":0,\"OutboundAudio\":0,\"DirectCapable\":false,\"HasRelay\":false,\"RTSPTemplate\":\"\",\"ReportsAvailability\":false";
    for (dial, registered, want) in [
        (true, false, format!("{old}}}")),
        (true, true, format!("{old},\"CanDial\":true}}")),
        (false, true, format!("{old}}}")),
        (false, false, format!("{old}}}")),
    ] {
        let cfg = Config {
            dial_enabled: dial,
            ..Config::default()
        };
        assert_eq!(
            GateSipServer::capabilities_for(&cfg, registered).to_wire(),
            want
        );
    }
}

// DEV-518: the registration fields ride the wire whatever dial_enabled says, and only they.
#[test]
fn station_registration_fields_are_on_the_wire_with_dial_off() {
    let off = Config::default();
    assert!(!off.dial_enabled);
    let w = GateSipServer::capabilities_for_station(&off, true, true, 1_800_000_000).to_wire();
    assert!(!w.contains("CanDial"), "{w}");
    assert!(
        w.ends_with(
            ",\"StationRegistered\":true,\"LastRegisterAt\":1800000000,\"Features\":[\"call_stats\"]}"
        ),
        "{w}"
    );
    let w = GateSipServer::capabilities_for_station(&off, false, false, 0).to_wire();
    assert!(
        w.ends_with(",\"StationRegistered\":false,\"Features\":[\"call_stats\"]}"),
        "{w}"
    );
    // the Go-parity shape never lists features
    assert!(!GateSipServer::capabilities_for(&off, true).to_wire().contains("Features"));
}

// --- the driver surface over a fake server (the Go tests that need a real server are in `tests/server_*.rs`) ---

struct FakeServer {
    me: std::sync::Weak<FakeServer>,
    ringing: Mutex<Option<String>>,
    answered: Mutex<bool>,
    client: Mutex<Option<Arc<AudioClient>>>,
    stats: Arc<audio::AudioStats>,
    hangups: Mutex<Vec<String>>,
    registered: bool,
}

impl FakeServer {
    fn new() -> Arc<FakeServer> {
        Arc::new_cyclic(|me| FakeServer {
            me: me.clone(),
            ringing: Mutex::new(None),
            answered: Mutex::new(false),
            client: Mutex::new(None),
            stats: Arc::new(audio::AudioStats::default()),
            hangups: Mutex::new(Vec::new()),
            registered: false,
        })
    }
    fn ring(&self, id: &str) {
        *self.ringing.lock().unwrap() = Some(id.to_string());
    }
}

impl audio::ClientHost for FakeServer {
    fn release(&self, client: &AudioClient, deliberate: bool) {
        let mut cur = self.client.lock().unwrap();
        match &*cur {
            Some(c) if std::ptr::eq(Arc::as_ptr(c), client) => {}
            _ => return,
        }
        *cur = None;
        if deliberate {
            client.close("the client said goodbye");
            self.hangups
                .lock()
                .unwrap()
                .push("the client hung up".into());
        } else {
            client.close("the client detached");
        }
    }
}

impl ServerApi for FakeServer {
    fn ringing_call(&self, id: &str) -> bool {
        self.ringing.lock().unwrap().as_deref() == Some(id) && !*self.answered.lock().unwrap()
    }
    fn attach_for(&self, id: &str) -> Result<Arc<AudioClient>, ServerError> {
        // the order of `Server::attach_for` (R2-47): no call, a different id, answered (for a
        // named call only: an empty id claims whatever is current), a client already attached
        let ringing = self.ringing.lock().unwrap().clone();
        match ringing.as_deref() {
            None => return Err(ServerError::NoCall),
            Some(r) if !id.is_empty() && r != id => return Err(ServerError::NoCall),
            _ => {}
        }
        if !id.is_empty() && *self.answered.lock().unwrap() {
            return Err(ServerError::AlreadyAnswered);
        }
        if self.client.lock().unwrap().is_some() {
            return Err(ServerError::AlreadyAttached);
        }
        *self.answered.lock().unwrap() = true;
        let host: std::sync::Weak<dyn audio::ClientHost> = self.me.clone();
        let cl = AudioClient::new(16000, 640, host, self.stats.clone());
        *self.client.lock().unwrap() = Some(cl.clone());
        Ok(cl)
    }
    fn hangup_answered(&self, id: &str) -> bool {
        let answered = *self.answered.lock().unwrap();
        if self.ringing.lock().unwrap().as_deref() == Some(id) && answered {
            self.hangups.lock().unwrap().push(id.to_string());
            *self.ringing.lock().unwrap() = None;
            return true;
        }
        false
    }
    fn answer_current(&self) -> Result<(), ServerError> {
        if self.ringing.lock().unwrap().is_none() {
            return Err(ServerError::NoCall);
        }
        let mut a = self.answered.lock().unwrap();
        if *a {
            return Err(ServerError::AlreadyAnswered);
        }
        *a = true;
        Ok(())
    }
    fn can_dial(&self) -> bool {
        self.registered
    }
    fn snapshot(&self) -> Stats {
        Stats::default()
    }
    fn dial<'a>(
        &'a self,
        _ctx: &'a CancelToken,
        _call_id: &'a str,
    ) -> BoxFuture<'a, Result<Arc<AudioClient>, DialError>> {
        Box::pin(async { Err(ServerError::NotRegistered.into()) })
    }
    fn shutdown(&self) -> BoxFuture<'_, ()> {
        Box::pin(async move {
            *self.ringing.lock().unwrap() = None;
        })
    }
}

#[tokio::test]
async fn driver_surface_without_a_server_refuses_like_go() {
    let mut d = GateSipServer::default();
    let t = token();
    assert!(!ConnectAnswerer::ringing_call(&d, "c"));
    assert!(d.claimed_audio("c").is_none());
    let e = d.hangup_call("c").unwrap_err();
    assert!(e.is(Sentinel::NoSuchCall) && e.message() == "no_such_call");
    let e = d.answer_with_audio(&t, "c").await.err().unwrap();
    assert!(e.is(Sentinel::NoSuchCall));
    assert_eq!(e.message(), "gate_sip_server: not started: no_such_call");
    let e = d.answer(&t, MediaTarget::default()).await.unwrap_err();
    assert_eq!(e.message(), "gate_sip_server: not started");
    let e = d
        .answer(
            &t,
            MediaTarget {
                host: "127.0.0.1".into(),
                port: 9,
            },
        )
        .await
        .unwrap_err();
    assert!(e.is(Sentinel::NotSupported));
    assert_eq!(
        e.message(),
        "gate_sip_server: Answer with a media target: not_supported (media is bridged in-process)"
    );
    // DialCall: dial off is the legacy Dial, dial on without a server is "not started"
    let e = d
        .dial_call(&t, "out-1", MediaTarget::default())
        .await
        .unwrap_err();
    assert!(e.is(Sentinel::NotSupported));
    assert_eq!(
        e.message(),
        "gate_sip_server: Dial: not_supported (outbound calls are not part of E1)"
    );
    let mut c = base();
    c.insert("dial_enabled".into(), json!(true));
    d.configure(&c).unwrap();
    let e = d
        .dial_call(&t, "out-1", MediaTarget::default())
        .await
        .unwrap_err();
    assert_eq!(e.message(), "gate_sip_server: DialCall: not started");
    // Stop twice
    d.stop().await.unwrap();
    d.stop().await.unwrap();
}

// TestW3AnswerOfALegacyAnsweredCallIsRefused (audio_test.go), the mapping half: the
// server's own ErrAlreadyAnswered/ErrAlreadyAttached become driver.ErrAlreadyAnswered, an
// unknown call driver.ErrNoSuchCall; AnswerCurrent's errors keep their own text.
#[tokio::test]
async fn server_errors_map_to_driver_errors() {
    let d = GateSipServer::default();
    let t = token();
    let s = FakeServer::new();
    d.set_server(s.clone());
    s.ring("call-1");
    let e = d.answer_with_audio(&t, "other").await.err().unwrap();
    assert!(e.is(Sentinel::NoSuchCall));
    assert_eq!(
        e.message(),
        "gate_sip_server: other is not a live call: no_such_call"
    );
    assert!(
        ConnectAnswerer::ringing_call(&d, "call-1") && !ConnectAnswerer::ringing_call(&d, "other")
    );
    d.answer(&t, MediaTarget::default()).await.unwrap();
    assert!(!ConnectAnswerer::ringing_call(&d, "call-1"));
    let e = d.answer(&t, MediaTarget::default()).await.unwrap_err();
    assert!(
        !e.is(Sentinel::AlreadyAnswered),
        "the package's own error, not the driver sentinel"
    );
    assert_eq!(e.message(), "already answered");
    let e = d.answer_with_audio(&t, "call-1").await.err().unwrap();
    assert!(e.is(Sentinel::AlreadyAnswered));
    assert_eq!(e.message(), "gate_sip_server: call-1: already_answered");
    // a ringing call is not ours to BYE; an answered one is
    assert!(d.hangup_call("call-1").is_ok());
    assert_eq!(
        s.hangups.lock().unwrap().clone(),
        vec!["call-1".to_string()]
    );
    assert!(d
        .hangup_call("call-1")
        .unwrap_err()
        .is(Sentinel::NoSuchCall));
    d.stop().await.unwrap();
    assert!(d.server().is_none());
}

// TestW3ClaimedAudioHandsOutAnUnheldAudioOnce (audio_test.go)
#[tokio::test]
async fn claimed_audio_hands_out_an_unheld_audio_once() {
    let d = GateSipServer::default();
    let host = FakeClientHost::arc();
    let (cl, a) = host.audio("call-W3J");
    let _ = cl;
    d.inner.lock().unwrap().audio = Some(a.clone());
    a.clear_attached(); // as if no leg had taken it yet (W4's dialled calls)
    let got = d
        .claimed_audio("call-W3J")
        .expect("unheld audio not handed out");
    assert!(std::ptr::addr_eq(Arc::as_ptr(&got), Arc::as_ptr(&a)));
    assert!(
        d.claimed_audio("other").is_none(),
        "matched another call id"
    );
    got.attach();
    assert!(
        d.claimed_audio("call-W3J").is_none(),
        "handed out after Attach"
    );
    a.clear_attached();
    assert!(d.claimed_audio("call-W3J").is_some());
    d.stop().await.unwrap();
    assert!(
        d.claimed_audio("call-W3J").is_none(),
        "handed out after Stop"
    );
}

struct FakeClientHost;
impl FakeClientHost {
    fn arc() -> Arc<dyn audio::ClientHost> {
        Arc::new(FakeClientHost)
    }
}
impl audio::ClientHost for FakeClientHost {
    fn release(&self, client: &AudioClient, deliberate: bool) {
        client.close(if deliberate {
            "the client said goodbye"
        } else {
            "the client detached"
        });
    }
}
trait AudioFor {
    fn audio(&self, id: &str) -> (Arc<AudioClient>, Arc<CallAudioImpl>);
}
impl AudioFor for Arc<dyn audio::ClientHost> {
    fn audio(&self, id: &str) -> (Arc<AudioClient>, Arc<CallAudioImpl>) {
        let cl = AudioClient::new(
            16000,
            640,
            Arc::downgrade(self),
            Arc::new(audio::AudioStats::default()),
        );
        let a = CallAudioImpl::new(id, cl.clone());
        (cl, a)
    }
}
