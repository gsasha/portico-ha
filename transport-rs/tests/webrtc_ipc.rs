//! The `webrtc_*` handlers over the real IPC socket and the `sessionRegistry`: Go's error codes
//! and messages on every failure path (the texts were measured against the real Go binary,
//! `scripts/ipc_smoke.py` compares them again), two doors, the dispatch lanes, and one end to end
//! call through a configured `gate_sip_server`, a station and a str0m peer as the browser.

mod common;

use std::sync::{Arc, Mutex};
use std::time::Duration;

use common::peer::{Peer, PeerOpts};
use common::*;
use serde_json::{json, Value};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::UnixStream;
use transport_rs::sfu::session::SessionConfig;
use transport_rs::webrtc::SessionRegistry;
use transport_rs::{dispatch, ipc};

struct Client {
    rd: BufReader<tokio::net::unix::OwnedReadHalf>,
    wr: tokio::net::unix::OwnedWriteHalf,
    next: i64,
    pub notes: Vec<Value>,
    srv: ipc::Server,
    pub hub: Arc<dispatch::Hub>,
}

async fn start(tag: &str) -> Client {
    let dir = std::env::temp_dir().join(format!("wrtc-{}-{tag}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let sock = dir.join("t.sock");
    let srv = ipc::Server::listen(&sock).unwrap();
    let hub = dispatch::register(
        &srv,
        dispatch::HubOptions {
            sfu: SessionConfig::loopback(),
            ..dispatch::HubOptions::default()
        },
    );
    let s = srv.clone();
    tokio::spawn(async move { s.serve().await });
    let c = UnixStream::connect(&sock).await.unwrap();
    let (rd, wr) = c.into_split();
    Client {
        rd: BufReader::new(rd),
        wr,
        next: 1,
        notes: Vec::new(),
        srv,
        hub,
    }
}

impl Client {
    /// Sends a request and returns its raw response line (notifications are kept in `notes`).
    async fn raw(&mut self, method: &str, params: Value) -> String {
        let id = self.next;
        self.next += 1;
        let line = format!(
            "{}\n",
            json!({"id": id, "method": method, "params": params})
        );
        self.wr.write_all(line.as_bytes()).await.unwrap();
        loop {
            let mut l = String::new();
            let n = tokio::time::timeout(Duration::from_secs(10), self.rd.read_line(&mut l))
                .await
                .expect("no response")
                .unwrap();
            assert!(n > 0, "connection closed");
            let v: Value = serde_json::from_str(&l).unwrap();
            if v.get("id").and_then(|x| x.as_i64()) == Some(id) {
                return l.trim_end().to_string();
            }
            self.notes.push(v);
        }
    }
    async fn call(&mut self, method: &str, params: Value) -> Value {
        serde_json::from_str(&self.raw(method, params).await).unwrap()
    }
    /// Waits for a notification of `method` (looking at the ones already read first).
    async fn note(&mut self, method: &str, within: Duration) -> Option<Value> {
        let end = tokio::time::Instant::now() + within;
        loop {
            if let Some(i) = self.notes.iter().position(|n| n["method"] == method) {
                return Some(self.notes.remove(i));
            }
            let mut l = String::new();
            match tokio::time::timeout_at(end, self.rd.read_line(&mut l)).await {
                Ok(Ok(n)) if n > 0 => self.notes.push(serde_json::from_str(&l).unwrap()),
                _ => return None,
            }
        }
    }
}

const GOOD: &str = "v=0\r\no=- 1 2 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\n";

// The expected lines are what the real Go binary answered (cmd/portico-transport/main.go).
#[tokio::test]
async fn error_codes_and_messages_are_gos() {
    let mut c = start("errs").await;
    assert_eq!(
        c.raw("webrtc_ice", json!({"call_id": "x", "candidate": "c"}))
            .await,
        r#"{"id":1,"error":{"code":"unknown_session","message":"x"}}"#
    );
    assert_eq!(
        c.raw("webrtc_answer", json!({"call_id": "x", "sdp": "v=0"}))
            .await,
        r#"{"id":2,"error":{"code":"unknown_session","message":"x"}}"#
    );
    assert_eq!(
        c.raw("webrtc_offer", json!({"call_id": "x", "sdp": ""}))
            .await,
        r#"{"id":3,"error":{"code":"bad_params","message":"webrtc_offer needs call_id and sdp"}}"#
    );
    assert_eq!(
        c.raw("webrtc_offer", json!({"call_id": "", "sdp": "v=0"}))
            .await,
        r#"{"id":4,"error":{"code":"bad_params","message":"webrtc_offer needs call_id and sdp"}}"#
    );
    assert_eq!(
        c.raw("webrtc_offer", json!({"call_id": 5, "sdp": "x"}))
            .await,
        r#"{"id":5,"error":{"code":"bad_params","message":"json: cannot unmarshal number into Go struct field webrtcParams.call_id of type string"}}"#
    );
    assert_eq!(
        c.raw("webrtc_offer", json!({"call_id": "g1", "sdp": "garbage"}))
            .await,
        r#"{"id":6,"error":{"code":"webrtc_failed","message":"sfu: setting remote offer: failed to unmarshal SDP: sdp: syntax error at pos 1: \"a\""}}"#
    );
    // a failed first offer drops its session at once (Go keeps it, PARITY.md difference 79): the
    // follow-ups are unknown_session, not "remote description is not set"
    for (i, (m, p)) in [
        ("webrtc_ice", json!({"call_id": "g1", "candidate": "candidate:1 1 udp 2130706431 127.0.0.1 5000 typ host"})),
        ("webrtc_ice", json!({"call_id": "g1", "candidate": ""})),
        ("webrtc_answer", json!({"call_id": "g1", "sdp": "v=0\r\n"})),
        ("webrtc_answer", json!({"call_id": "g1", "sdp": "garbage"})),
    ]
    .into_iter()
    .enumerate()
    {
        assert_eq!(
            c.raw(m, p).await,
            format!(r#"{{"id":{},"error":{{"code":"unknown_session","message":"g1"}}}}"#, 7 + i)
        );
    }
    let r = c
        .call("webrtc_offer", json!({"call_id": "g2", "sdp": GOOD}))
        .await;
    assert_eq!(r["error"]["code"], "webrtc_failed", "{r}");
    c.hub.shutdown().await;
    c.srv.close().await;
}

// TestTwoDoors_BrowserSessionsAreKeptPerCallAndRemovedPerCall + TestSessionRegistryMarksACallJoinedOnce
#[tokio::test]
async fn browser_sessions_are_kept_per_call_and_removed_per_call() {
    let r = SessionRegistry::new(SessionConfig::loopback());
    let a = r.get_or_create("gate-1").unwrap();
    let b = r.get_or_create("door-1").unwrap();
    assert!(!Arc::ptr_eq(&a, &b), "two calls share one browser session");
    assert!(
        Arc::ptr_eq(&r.get_or_create("gate-1").unwrap(), &a),
        "a renegotiating offer must reuse its session"
    );
    // each call is joined once, independently
    assert!(r.mark_joined("gate-1", &a) && r.mark_joined("door-1", &b));
    assert!(
        !r.mark_joined("gate-1", &a) && !r.mark_joined("door-1", &b),
        "a second join of the same call must be refused"
    );
    // the gate call ends: only its slot goes
    r.remove("gate-1", &a);
    assert!(r.get("gate-1").is_none());
    // PARITY 402 (R2-58): Go's last assertion here is "after the session is gone a new join must
    // be allowed" (`talkback_test.go:198-201`); this port refuses it on purpose: a removed
    // session has no leg to join, and accepting it would leave a `joined` entry nothing removes
    assert!(
        !r.mark_joined("gate-1", &a),
        "a removed session was marked joined (Go allows it, PARITY 402)"
    );
    assert!(
        r.get("door-1").is_some_and(|g| Arc::ptr_eq(&g, &b)),
        "door-1's session went with gate-1's"
    );
    assert!(
        !r.mark_joined("door-1", &b),
        "door-1's joined record went with gate-1's"
    );
    // a stale remove (a replaced session) leaves the live one alone
    r.remove("door-1", &a);
    assert!(
        r.get("door-1").is_some(),
        "a stale session removed the live one"
    );
    a.close();
    b.close();
}

// TestASessionThatDisconnectsLeavesTheRegistry
#[tokio::test]
async fn a_session_that_disconnects_leaves_the_registry() {
    let r = SessionRegistry::new(SessionConfig::loopback());
    let s = r.get_or_create("gone").unwrap();
    assert_eq!(r.len(), 1);
    s.close();
    let end = tokio::time::Instant::now() + Duration::from_secs(2);
    while !r.is_empty() && tokio::time::Instant::now() < end {
        sleep_ms(20).await;
    }
    assert!(r.is_empty(), "the session never left the table");
    // and the same call id can start again with a fresh session
    let s2 = r.get_or_create("gone").unwrap();
    assert!(!Arc::ptr_eq(&s, &s2));
    s2.close();
}

// TestTwoDoors_WebrtcRequestsOfOneCallQueueWhileTheOtherCallRuns: the real lane keys with
// recording handlers, a blocked gate offer must not hold the entrance's requests, and one call's
// candidate must not overtake its offer.
#[tokio::test]
async fn webrtc_requests_of_one_call_queue_while_the_other_call_runs() {
    let dir = std::env::temp_dir().join(format!("wrtc-{}-lanes", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let sock = dir.join("t.sock");
    let srv = ipc::Server::listen(&sock).unwrap();
    let order: Arc<Mutex<Vec<String>>> = Arc::new(Mutex::new(Vec::new()));
    let release = Arc::new(tokio::sync::Notify::new());
    for name in ["offer", "ice"] {
        let (order, release) = (order.clone(), release.clone());
        let key: ipc::KeyFn = Arc::new(dispatch::call_id_key);
        srv.handle_keyed(&format!("webrtc_{name}"), key, move |p: ipc::Params| {
            let (order, release) = (order.clone(), release.clone());
            async move {
                let v: Value = serde_json::from_str(ipc::raw_str(&p)).unwrap();
                let id = v["call_id"].as_str().unwrap().to_string();
                if name == "offer" && id == "gate-1" {
                    release.notified().await;
                }
                order.lock().unwrap().push(format!("{name}:{id}"));
                Ok(ipc::Reply::from(json!(true)))
            }
        });
    }
    let s = srv.clone();
    tokio::spawn(async move { s.serve().await });
    let c = UnixStream::connect(&sock).await.unwrap();
    let (rd, mut wr) = c.into_split();
    let mut rd = BufReader::new(rd);
    for l in [
        r#"{"id":1,"method":"webrtc_offer","params":{"call_id":"gate-1","sdp":"x"}}"#,
        r#"{"id":2,"method":"webrtc_ice","params":{"call_id":"gate-1","candidate":"c"}}"#,
        r#"{"id":3,"method":"webrtc_offer","params":{"call_id":"door-1","sdp":"x"}}"#,
        r#"{"id":4,"method":"webrtc_ice","params":{"call_id":"door-1","candidate":"c"}}"#,
    ] {
        wr.write_all(format!("{l}\n").as_bytes()).await.unwrap();
    }
    let mut read = async || {
        let mut l = String::new();
        tokio::time::timeout(Duration::from_secs(3), rd.read_line(&mut l))
            .await
            .unwrap()
            .unwrap();
        serde_json::from_str::<Value>(&l).unwrap()["id"]
            .as_i64()
            .unwrap()
    };
    let mut first = vec![read().await, read().await];
    first.sort();
    assert_eq!(
        first,
        vec![3, 4],
        "the entrance's requests were held up by the gate's offer"
    );
    assert_eq!(*order.lock().unwrap(), vec!["offer:door-1", "ice:door-1"]);
    release.notify_one();
    let mut second = vec![read().await, read().await];
    second.sort();
    assert_eq!(second, vec![1, 2]);
    assert_eq!(
        order.lock().unwrap()[2..],
        ["offer:gate-1", "ice:gate-1"],
        "a call's candidate overtook its offer"
    );
    srv.close().await;
}

fn free_udp() -> u16 {
    let s = std::net::UdpSocket::bind("127.0.0.1:0").unwrap();
    s.local_addr().unwrap().port()
}

/// The whole chain over the IPC: configure a gate_sip_server, a station rings, the browser (a
/// str0m peer) sends its offer through `webrtc_offer`, trickles through `webrtc_ice`, the station
/// is answered only once the browser is connected, audio flows, the browser leaves, the station is
/// hung up, `ended` is notified, and the call id is free again.
#[tokio::test]
async fn a_call_through_the_ipc_from_ring_to_hangup() {
    let mut c = start("e2e").await;
    let (sip, rtp) = (free_udp(), free_udp());
    let cfg = json!({"driver":"gate_sip_server","config":{"device_id":"gate-e2e","sip_listen":format!("127.0.0.1:{sip}"),"rtp_port":rtp,"password":"s3cretpass"}});
    let r = c.call("configure", cfg).await;
    assert!(r.get("result").is_some(), "{r}");
    let st = Station::new(format!("127.0.0.1:{sip}").parse().unwrap()).await;
    st.register("").await;
    let m401 = find(&st.collect(ms(300)).await, "401").expect("no challenge");
    st.register(&digest_auth(&m401, "8001", "s3cretpass")).await;
    assert!(
        find(&st.collect(ms(300)).await, "200").is_some(),
        "register refused"
    );

    st.invite("call-ipc-1", 1, &st.sdp("wide")).await;
    let ring = c
        .note("ringing", ms(2000))
        .await
        .expect("no ringing notification");
    assert_eq!(ring["params"]["call_id"], "call-ipc-1");
    assert_eq!(ring["params"]["device_id"], "gate-e2e");
    assert!(has(&st.collect(ms(200)).await, "180"));

    // the browser: no candidates in the offer, they trickle through webrtc_ice
    fn strip(t: String) -> String {
        t.split_inclusive('\n')
            .filter(|l| !l.starts_with("a=candidate:") && !l.starts_with("a=end-of-candidates"))
            .collect()
    }
    let peer = Peer::new(PeerOpts {
        opus: true,
        g722: true,
        pcmu: true,
        rewrite: Some(strip),
        ..PeerOpts::default()
    })
    .await;
    let offer = peer.offer().await;
    let r = c
        .call(
            "webrtc_offer",
            json!({"call_id": "call-ipc-1", "sdp": offer}),
        )
        .await;
    let answer = r["result"]["sdp"]
        .as_str()
        .unwrap_or_else(|| panic!("{r}"))
        .to_string();
    assert!(answer.contains("a=end-of-candidates"));
    let ptl = transport_rs::sfu::sdp::audio_payload_types(&answer);
    assert_eq!(ptl, vec![9, 0], "G.722 then PCMU only");
    assert!(
        !has(&st.collect(ms(300)).await, "200"),
        "the station was answered before the browser connected"
    );
    let r = c.call("webrtc_ice", json!({"call_id": "call-ipc-1", "candidate": format!("candidate:1 1 udp 2130706431 127.0.0.1 {} typ host", peer.addr.port())})).await;
    assert_eq!(r["result"]["ok"], true, "{r}");
    peer.accept_answer(&answer).await.unwrap();
    assert!(peer.wait_connected(10).await);
    let got = st.collect(ms(1500)).await;
    assert!(
        has(&got, "200"),
        "no 200 once the browser connected: {:?}",
        codes(&got)
    );
    st.ack("call-ipc-1").await;
    let ans = c
        .note("call_state_changed", ms(2000))
        .await
        .expect("no call_state_changed");
    assert_eq!(ans["params"]["call_id"], "call-ipc-1");

    // audio: the browser's G.722 tone reaches the station
    peer.tone(true, 1000.0, 20, 8000.0, 1.5);
    let mut heard: Vec<u8> = Vec::new();
    let end = tokio::time::Instant::now() + ms(2000);
    while tokio::time::Instant::now() < end {
        if let Some((_, pl)) = st.read_rtp(ms(100)).await {
            heard.extend(pl);
        }
    }
    assert!(
        heard.len() > 15000,
        "the station heard {} bytes",
        heard.len()
    );
    let (a, b) = (
        goertzel(&heard[8000..], 16000.0, 1000.0),
        goertzel(&heard[8000..], 16000.0, 400.0),
    );
    assert!(a > 20.0 * b, "no 1 kHz tone at the station: {a} vs {b}");

    // the browser leaves
    peer.close();
    assert!(
        has(&st.collect(ms(3000)).await, "BYE"),
        "the station was not hung up"
    );
    let mut ended = false;
    for _ in 0..10 {
        match c.note("call_state_changed", ms(500)).await {
            Some(n) if n["params"]["state"] == "ended" => {
                ended = true;
                break;
            }
            Some(_) => {}
            None => break,
        }
    }
    assert!(ended, "no ended notification");
    // the session left the table: a candidate for it is an unknown session
    let end = tokio::time::Instant::now() + ms(2000);
    loop {
        let r = c
            .call(
                "webrtc_ice",
                json!({"call_id": "call-ipc-1", "candidate": ""}),
            )
            .await;
        if r["error"]["code"] == "unknown_session" {
            break;
        }
        assert!(
            tokio::time::Instant::now() < end,
            "the ended call's session is still in the table: {r}"
        );
        sleep_ms(50).await;
    }
    c.hub.shutdown().await;
    c.srv.close().await;
}

fn open_fds() -> usize {
    std::fs::read_dir("/proc/self/fd")
        .map(|d| d.count())
        .unwrap_or(0)
}

/// A flood of garbage and of refused offers on distinct call ids neither grows the table nor
/// leaks file descriptors (sockets are bound only for an offer that parsed, and a refused first
/// offer's session is closed at once).
#[tokio::test]
async fn a_flood_of_bad_offers_leaks_neither_sessions_nor_descriptors() {
    let mut c = start("flood").await;
    let opus_only = Peer::new(PeerOpts {
        opus: true,
        pcmu: false,
        ..PeerOpts::default()
    })
    .await
    .offer()
    .await;
    let before = open_fds();
    for i in 0..150 {
        let sdp = match i % 3 {
            0 => "garbage".to_string(),
            1 => opus_only.clone(), // parses, binds, then is refused for lack of a codec
            _ => "v=0\r\n\u{0}\u{0}".to_string(),
        };
        let r = c
            .call(
                "webrtc_offer",
                json!({"call_id": format!("flood-{i}"), "sdp": sdp}),
            )
            .await;
        assert_eq!(r["error"]["code"], "webrtc_failed", "{r}");
    }
    assert!(
        c.hub.sessions().is_empty(),
        "{} sessions linger",
        c.hub.sessions().len()
    );
    // the sockets of the refused offers are released when their tasks end
    let end = tokio::time::Instant::now() + Duration::from_secs(5);
    while open_fds() > before + 4 && tokio::time::Instant::now() < end {
        sleep_ms(50).await;
    }
    let after = open_fds();
    assert!(
        after <= before + 4,
        "descriptors grew from {before} to {after}"
    );
    c.hub.shutdown().await;
    c.srv.close().await;
}

#[tokio::test]
async fn the_session_cap_refuses_a_ninth_call_with_webrtc_failed() {
    let cfg = SessionConfig {
        max_sessions: 8,
        ..SessionConfig::loopback()
    };
    let r = SessionRegistry::new(cfg);
    let mut live = Vec::new();
    for i in 0..8 {
        live.push(r.get_or_create(&format!("cap-{i}")).unwrap());
    }
    let e = r.get_or_create("cap-8").err().expect("a ninth session");
    assert!(
        e.contains("too many concurrent sessions") && e.contains("PORTICO_SFU_MAX_SESSIONS"),
        "{e}"
    );
    // an existing call id is still served, and a closed session frees its slot
    assert!(r.get_or_create("cap-3").is_ok());
    live[0].close();
    let end = tokio::time::Instant::now() + Duration::from_secs(2);
    while r.len() >= 8 && tokio::time::Instant::now() < end {
        sleep_ms(20).await;
    }
    assert!(r.get_or_create("cap-8").is_ok());
    // through the IPC the refusal is webrtc_failed
    let mut c = start("cap").await;
    let good = Peer::new(PeerOpts::default()).await.offer().await;
    for i in 0..8 {
        let r = c
            .call(
                "webrtc_offer",
                json!({"call_id": format!("ok-{i}"), "sdp": good}),
            )
            .await;
        assert!(r.get("result").is_some(), "{r}");
    }
    let r = c
        .call("webrtc_offer", json!({"call_id": "ok-8", "sdp": good}))
        .await;
    assert_eq!(r["error"]["code"], "webrtc_failed");
    assert!(
        r["error"]["message"]
            .as_str()
            .unwrap()
            .contains("too many concurrent sessions"),
        "{r}"
    );
    c.hub.shutdown().await;
    c.srv.close().await;
}

/// The session died while its offer was being answered: `remove` has run, so `mark_joined` must
/// not leave an entry that nothing removes and that pins the session.
#[tokio::test]
async fn a_session_that_died_before_it_was_joined_leaves_no_joined_entry() {
    let r = SessionRegistry::new(SessionConfig::loopback());
    let s = r.get_or_create("race").unwrap();
    s.close();
    let end = tokio::time::Instant::now() + Duration::from_secs(2);
    while !r.is_empty() && tokio::time::Instant::now() < end {
        sleep_ms(20).await;
    }
    assert!(r.is_empty());
    assert!(!r.mark_joined("race", &s), "joined a session that is gone");
    assert_eq!(r.joined_len(), 0);
    // a live one still joins once
    let l = r.get_or_create("live").unwrap();
    assert!(r.mark_joined("live", &l) && !r.mark_joined("live", &l));
    l.close();
}

/// Shutdown waits (bounded) for the sessions to finish, so the close_notify and the hang-up leave.
#[tokio::test]
async fn shutdown_waits_for_the_sessions_to_finish() {
    let r = SessionRegistry::new(SessionConfig::loopback());
    let s = r.get_or_create("bye").unwrap();
    let t0 = std::time::Instant::now();
    r.close_all_wait(Duration::from_secs(2)).await;
    assert!(
        s.is_done(),
        "close_all_wait returned before the session finished"
    );
    assert!(t0.elapsed() < Duration::from_secs(2));
}

// ---- S-T1: the audio of a DIALLED call attaches through the answer path (R2-3) -------------
//
// TestSpikeST1_DialledCallAudioAttachesThroughTheAnswerPath (dial_callid_test.go:409-571), over
// the real socket: `dial` returns a hub-minted `out-` id, `webrtc_offer` for that id joins the
// browser to the driver's claimed audio exactly as for a claimed inbound call, audio flows both
// ways, the browser leaving detaches the driver, and `hangup` ends the call on the sink. The
// driver is a fake CallDialer + AudioClaimer + CallHangupper configured through the real
// `configure` (the hub's test-driver seam); the browser is the str0m peer.

mod st1 {
    use std::collections::HashMap;
    use std::sync::atomic::{AtomicU32, Ordering};
    use std::sync::{Arc, Mutex};
    use tokio::sync::mpsc;
    use transport_rs::cancel::CancelToken;
    use transport_rs::driver::{
        AudioClaimer, BoxFuture, CallAudio, CallDialer, CallHangupper, Capabilities, Driver,
        DriverError, EventSink, MediaTarget, Sentinel,
    };

    /// `fakeAudio`: what the driver's side of a call sees.
    pub struct Audio {
        in_tx: mpsc::Sender<Vec<u8>>,
        out_rx: Arc<tokio::sync::Mutex<mpsc::Receiver<Vec<u8>>>>,
        pub attached: AtomicU32,
        pub detached: AtomicU32,
        /// the driver emits door audio here
        pub out_tx: mpsc::Sender<Vec<u8>>,
        /// what the browser sent to the door
        pub in_rx: tokio::sync::Mutex<mpsc::Receiver<Vec<u8>>>,
    }

    impl CallAudio for Audio {
        fn input(&self) -> mpsc::Sender<Vec<u8>> {
            self.in_tx.clone()
        }
        fn output(&self) -> Arc<tokio::sync::Mutex<mpsc::Receiver<Vec<u8>>>> {
            self.out_rx.clone()
        }
        fn attach(&self) {
            self.attached.fetch_add(1, Ordering::SeqCst);
        }
        fn detach(&self) {
            self.detached.fetch_add(1, Ordering::SeqCst);
        }
    }

    pub type Calls = Arc<Mutex<HashMap<String, Arc<Audio>>>>;

    pub struct Dialer {
        pub calls: Calls,
        pub sink: Mutex<Option<Arc<dyn EventSink>>>,
    }

    impl Driver for Dialer {
        fn configure(
            &mut self,
            _: &serde_json::Map<String, serde_json::Value>,
        ) -> Result<(), DriverError> {
            Ok(())
        }
        fn capabilities(&self) -> Capabilities {
            Capabilities::default()
        }
        fn start<'a>(
            &'a self,
            _: &'a CancelToken,
            events: Arc<dyn EventSink>,
        ) -> BoxFuture<'a, Result<(), DriverError>> {
            *self.sink.lock().unwrap() = Some(events);
            Box::pin(async { Ok(()) })
        }
        fn stop(&self) -> BoxFuture<'_, Result<(), DriverError>> {
            Box::pin(async { Ok(()) })
        }
        fn dial<'a>(
            &'a self,
            _: &'a CancelToken,
            _: MediaTarget,
        ) -> BoxFuture<'a, Result<(), DriverError>> {
            Box::pin(async { Err(DriverError::new("legacy dial")) })
        }
        fn answer<'a>(
            &'a self,
            _: &'a CancelToken,
            _: MediaTarget,
        ) -> BoxFuture<'a, Result<(), DriverError>> {
            Box::pin(async { Ok(()) })
        }
        fn open_relay<'a>(&'a self, _: &'a CancelToken) -> BoxFuture<'a, Result<(), DriverError>> {
            Box::pin(async { Ok(()) })
        }
        fn as_call_dialer(&self) -> Option<&dyn CallDialer> {
            Some(self)
        }
        fn as_audio_claimer(&self) -> Option<&dyn AudioClaimer> {
            Some(self)
        }
        fn as_call_hangupper(&self) -> Option<&dyn CallHangupper> {
            Some(self)
        }
    }

    impl CallDialer for Dialer {
        fn dial_call<'a>(
            &'a self,
            _: &'a CancelToken,
            call_id: &'a str,
            _: MediaTarget,
        ) -> BoxFuture<'a, Result<(), DriverError>> {
            let (in_tx, in_rx) = mpsc::channel(8);
            let (out_tx, out_rx) = mpsc::channel(8);
            let audio = Arc::new(Audio {
                in_tx,
                out_rx: Arc::new(tokio::sync::Mutex::new(out_rx)),
                attached: AtomicU32::new(0),
                detached: AtomicU32::new(0),
                out_tx,
                in_rx: tokio::sync::Mutex::new(in_rx),
            });
            self.calls
                .lock()
                .unwrap()
                .insert(call_id.to_string(), audio);
            let sink = self.sink.lock().unwrap().clone().expect("started");
            sink.state_changed(call_id, "ringing");
            Box::pin(async { Ok(()) })
        }
    }

    impl AudioClaimer for Dialer {
        fn claimed_audio(&self, call_id: &str) -> Option<Arc<dyn CallAudio>> {
            let a = self.calls.lock().unwrap().get(call_id).cloned()?;
            Some(a as Arc<dyn CallAudio>)
        }
    }

    impl CallHangupper for Dialer {
        fn hangup_call(&self, call_id: &str) -> Result<(), DriverError> {
            if self.calls.lock().unwrap().remove(call_id).is_none() {
                return Err(DriverError::wrapping(Sentinel::NoSuchCall, "no such call"));
            }
            let sink = self.sink.lock().unwrap().clone().expect("started");
            sink.state_changed(call_id, "ended");
            Ok(())
        }
    }
}

#[tokio::test]
async fn a_dialled_calls_audio_attaches_through_the_answer_path() {
    let mut c = start("st1").await;
    let calls: st1::Calls = Arc::default();
    let k2 = calls.clone();
    c.hub.add_test_driver(
        "st1_dialer",
        Arc::new(move || {
            Box::new(st1::Dialer {
                calls: k2.clone(),
                sink: Mutex::new(None),
            })
        }),
    );
    let r = c
        .call(
            "configure",
            json!({"driver": "st1_dialer", "config": {"host": "door-a"}}),
        )
        .await;
    assert!(r.get("result").is_some(), "{r}");
    let r = c.call("dial", json!({"device_id": "door-a"})).await;
    assert_eq!(r["result"]["ok"], true, "{r}");
    let call_id = r["result"]["call_id"].as_str().unwrap().to_string();
    assert!(call_id.starts_with("out-"), "{call_id}");
    let audio = calls
        .lock()
        .unwrap()
        .get(&call_id)
        .cloned()
        .expect("the driver placed the call");

    // nothing is attached to the dialled call's audio before the browser lands
    assert_eq!(audio.attached.load(std::sync::atomic::Ordering::SeqCst), 0);
    let peer = Peer::new(PeerOpts::default()).await;
    let r = c
        .call(
            "webrtc_offer",
            json!({"call_id": call_id, "sdp": peer.offer().await}),
        )
        .await;
    let answer = r["result"]["sdp"]
        .as_str()
        .unwrap_or_else(|| panic!("webrtc_offer for the dialled id: {r}"))
        .to_string();
    peer.accept_answer(&answer).await.unwrap();
    assert!(peer.wait_connected(10).await, "the browser never connected");
    // the driver's audio was attached by the existing bridge
    for _ in 0..100 {
        if audio.attached.load(std::sync::atomic::Ordering::SeqCst) == 1 {
            break;
        }
        sleep_ms(50).await;
    }
    assert_eq!(audio.attached.load(std::sync::atomic::Ordering::SeqCst), 1);

    // door -> browser: L16 frames the driver emits arrive as PCMU RTP
    let mut frame = vec![0u8; 1280];
    for i in (0..frame.len()).step_by(2) {
        frame[i] = 0x70;
        frame[i + 1] = 0x17; // 6000, little-endian
    }
    for _ in 0..5 {
        audio.out_tx.send(frame.clone()).await.unwrap();
    }
    for _ in 0..100 {
        if !peer.received().is_empty() {
            break;
        }
        sleep_ms(50).await;
    }
    let heard = peer.received();
    assert!(!heard.is_empty(), "the browser never heard the door");
    assert!(heard.iter().all(|p| p.pt == 0), "PCMU was offered");

    // browser -> door: the mic's RTP arrives on the driver's audio in
    let mut got = None;
    for seq in 0..200u64 {
        peer.send(false, seq * 160, vec![0xFF; 160]);
        let mut rx = audio.in_rx.lock().await;
        if let Ok(Some(f)) = tokio::time::timeout(ms(20), rx.recv()).await {
            got = Some(f);
            break;
        }
    }
    let f = got.expect("the door never heard the browser");
    assert!(!f.is_empty() && f.len() % 2 == 0, "{} bytes", f.len());

    // the browser leaving detaches the driver; hanging up through the hub ends the call
    peer.close();
    for _ in 0..100 {
        if audio.detached.load(std::sync::atomic::Ordering::SeqCst) == 1 {
            break;
        }
        sleep_ms(50).await;
    }
    assert_eq!(audio.detached.load(std::sync::atomic::Ordering::SeqCst), 1);
    let r = c.call("hangup", json!({"call_id": call_id})).await;
    assert_eq!(r["result"]["ok"], true, "hangup of the dialled id: {r}");
    let mut last = None;
    while let Some(n) = c.note("call_state_changed", ms(500)).await {
        if n["params"]["call_id"] == call_id.as_str() {
            last = Some(n);
        }
    }
    let last = last.expect("no state for the dialled call");
    assert_eq!(last["params"]["state"], "ended");
    assert_eq!(last["params"]["device_id"], "door-a");
    c.hub.shutdown().await;
    c.srv.close().await;
}
