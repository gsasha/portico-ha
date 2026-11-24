use super::*;
use std::sync::atomic::Ordering;

// TestRehearsalRTPSourceRuleWithRealStationAddress (rehearsal_test.go): the strict RTP source
// rule against the station's real address (10.8.0.39, from the journalled 2026-09-09 call:
// SDP c=/m= 10.8.0.39:20002 and the first inbound RTP from 10.8.0.39:20002). Pure predicate:
// nothing is sent anywhere.
#[test]
fn rtp_source_rule_with_the_real_station_address() {
    let ip = |s: &str| s.parse::<IpAddr>().unwrap();
    let c = Call::bare(Some(FarRtp {
        ip: ip("10.8.0.39"),
        port: 20002,
    }));
    let none = Call::bare(None);
    for (name, call, from, want) in [
        (
            "station, the journalled address and port",
            Some(&c),
            "10.8.0.39",
            true,
        ),
        ("another LAN host", Some(&c), "10.8.0.40", false),
        ("the gate-sip host", Some(&c), "10.6.0.7", false),
        ("no live call", None, "10.8.0.39", false),
        (
            "a call with no media address",
            Some(&none),
            "10.8.0.39",
            false,
        ),
        (
            "IPv4-mapped form of the station",
            Some(&c),
            "::ffff:10.8.0.39",
            true,
        ),
    ] {
        assert_eq!(rtp_source_ok(call, ip(from)), want, "{name}");
    }
}

#[test]
fn duration_strings_match_gos() {
    for (ms, want) in [
        (0, "0s"),
        (700, "700ms"),
        (1500, "1.5s"),
        (45_000, "45s"),
        (60_000, "1m0s"),
        (3_600_000, "1h0m0s"),
        (90_500, "1m30.5s"),
    ] {
        assert_eq!(
            duration_string_of(Duration::from_millis(ms)),
            want,
            "{ms} ms"
        );
    }
    assert_eq!(duration_string_of(Duration::from_micros(1500)), "1.5ms");
    assert_eq!(duration_string_of(Duration::from_nanos(7)), "7ns");
}

// The two regexps of onInvite (`reCIP`, `reMPort`) on the shapes the station sends and on the
// ones RE2 treats differently from a naive find.
#[test]
fn sdp_address_and_port_extraction() {
    let sdp = "v=0\r\nc=IN IP4 10.8.0.39\r\nm=audio 20002 RTP/AVP 8\r\n";
    assert_eq!(find_c_ip(sdp), Some("10.8.0.39"));
    assert_eq!(find_m_port(sdp), Some((20002, false)));
    // `^` anchors at a line start only; a c= inside a line does not count
    assert_eq!(find_c_ip("x c=IN IP4 1.2.3.4\r\n"), None);
    // an empty address is no match; the next line can still match
    assert_eq!(
        find_c_ip("c=IN IP4 \r\nc=IN IP4 5.6.7.8\r\n"),
        Some("5.6.7.8")
    );
    assert_eq!(find_m_port("m=audio abc\nm=audio 7\n"), Some((7, false)));
    // more digits than an int64 holds: Sscanf fails and the port stays 0
    assert_eq!(
        find_m_port("m=audio 99999999999999999999\n"),
        Some((0, true))
    );
    assert_eq!(find_m_port("m=audio 007\n"), Some((7, false)));
    assert_eq!(find_m_port("m=video 5\n"), None);
}

// The event queue is bounded: a flood past the cap is dropped (and logged), never grown.
#[tokio::test]
async fn the_event_queue_is_bounded() {
    struct Slow;
    impl EventSink for Slow {
        fn ringing(&self, _: &str, _: &str) {}
        fn state_changed(&self, _: &str, _: &str) {}
        fn availability_changed(&self, _: &str, _: bool, _: &str) {}
    }
    let cfg = super::super::config::parse_config(
        serde_json::json!({"sip_listen": "127.0.0.1:0", "password": "s3cretpass", "rtp_port": 0})
            .as_object()
            .unwrap(),
    )
    .unwrap();
    let srv = Server::start(cfg, Arc::new(Slow)).await.unwrap();
    // no await between the pushes: the dispatcher cannot drain. A stalled consumer with 2000
    // events behind it still gets the ring and the end of the call.
    srv.emit(Event::Ringing {
        call_id: "ring-1".into(),
        device_id: "d".into(),
    });
    for i in 0..2000 {
        srv.emit(Event::State {
            call_id: format!("c{i}"),
            state: STATE_ANSWERED,
        });
    }
    srv.emit(Event::State {
        call_id: "c-end".into(),
        state: STATE_ENDED,
    });
    assert!(srv.evq.len() <= EVENT_QUEUE_MAX, "{}", srv.evq.len());
    assert!(srv.evq.dropped() >= 900);
    let mut kept = Vec::new();
    while let Some(e) = srv.evq.pop() {
        kept.push(e);
    }
    assert!(matches!(&kept[0], Event::Ringing { call_id, .. } if call_id == "ring-1"));
    assert!(matches!(kept.last(), Some(Event::State { call_id, .. }) if call_id == "c-end"));
    srv.shutdown().await;
}

use std::sync::atomic::AtomicUsize;

/// A sink that counts rings and records `driver_error` calls.
#[derive(Default)]
struct Rec {
    rings: AtomicUsize,
    errors: Mutex<Vec<(String, String, String)>>,
}
impl EventSink for Rec {
    fn ringing(&self, _: &str, _: &str) {
        self.rings.fetch_add(1, Ordering::SeqCst);
    }
    fn state_changed(&self, _: &str, _: &str) {}
    fn availability_changed(&self, _: &str, _: bool, _: &str) {}
    fn error_reporter(&self) -> Option<&dyn crate::driver::ErrorReporter> {
        Some(self)
    }
}
impl crate::driver::ErrorReporter for Rec {
    fn driver_error(&self, device_id: &str, code: &str, msg: &str) {
        lock(&self.errors).push((device_id.into(), code.into(), msg.into()));
    }
}

async fn start_rec(extra: serde_json::Value) -> (Arc<Server>, Arc<Rec>) {
    let mut v = serde_json::json!({
        "device_id": "gs", "sip_listen": "127.0.0.1:0", "password": "s3cretpass", "rtp_port": 0
    });
    for (k, x) in extra.as_object().unwrap() {
        v[k] = x.clone();
    }
    let cfg = super::super::config::parse_config(v.as_object().unwrap()).unwrap();
    let rec = Arc::new(Rec::default());
    (Server::start(cfg, rec.clone()).await.unwrap(), rec)
}

fn invite_to(port: u16, station: SocketAddr, id: &str) -> Vec<u8> {
    let sdp = "v=0\r\nc=IN IP4 127.0.0.1\r\nm=audio 20002 RTP/AVP 8\r\na=rtpmap:8 PCMA/8000\r\n";
    format!(
        "INVITE sip:9901@127.0.0.1:{port} SIP/2.0\r\nVia: SIP/2.0/UDP {station};branch=z9hG4bK{id}\r\n\
         From: <sip:8001@station>;tag=t-{id}\r\nTo: <sip:9901@server>\r\nCall-ID: {id}\r\n\
         CSeq: 1 INVITE\r\nContact: <sip:8001@{station}>\r\nContent-Type: application/sdp\r\n\
         Content-Length: {}\r\n\r\n{sdp}",
        sdp.len()
    )
    .into_bytes()
}

// R1-25 (PARITY 49, second half): an INVITE that reaches `on_invite` once `closed` is set is
// ignored. Fed straight to `handle_sip`, because after `shutdown` the sockets are closed and
// the datagram would never be read.
#[tokio::test]
async fn an_invite_after_closed_starts_no_call() {
    let station = UdpSocket::bind("127.0.0.1:0").await.unwrap();
    let from = station.local_addr().unwrap();
    // the control: the same datagram on an open server is answered 100 and counted
    let (open, rec_open) = start_rec(serde_json::json!({})).await;
    open.set_open_invites(true); // the control INVITE never REGISTERs (PARITY.md 473)
    open.handle_sip(&invite_to(open.sip_addr().port(), from, "ctl"), from);
    let mut buf = [0u8; 1500];
    let n = tokio::time::timeout(Duration::from_secs(2), station.recv(&mut buf))
        .await
        .expect("the control INVITE got no answer")
        .unwrap();
    assert!(buf[..n].starts_with(b"SIP/2.0 100"));
    let n = tokio::time::timeout(Duration::from_secs(2), station.recv(&mut buf))
        .await
        .expect("the control INVITE got no 180")
        .unwrap();
    assert!(buf[..n].starts_with(b"SIP/2.0 180"));
    assert_eq!(open.snapshot().calls, 1);
    tokio::time::sleep(Duration::from_millis(100)).await;
    assert_eq!(rec_open.rings.load(Ordering::SeqCst), 1);
    open.shutdown().await;

    // a second station: the first one still gets the 480 that stopping the control sent
    let station = UdpSocket::bind("127.0.0.1:0").await.unwrap();
    let from = station.local_addr().unwrap();
    let (srv, rec) = start_rec(serde_json::json!({})).await;
    lock(&srv.state).closed = true;
    srv.handle_sip(&invite_to(srv.sip_addr().port(), from, "late"), from);
    let r = tokio::time::timeout(Duration::from_millis(300), station.recv(&mut buf)).await;
    assert!(r.is_err(), "a closed server answered an INVITE");
    assert_eq!(srv.snapshot().calls, 0);
    assert_eq!(rec.rings.load(Ordering::SeqCst), 0);
    lock(&srv.state).closed = false; // a real shutdown would otherwise wait for itself
    srv.shutdown().await;
}

// R1-40: a read loop that gives up reports through the error reporter and stops the server.
#[tokio::test]
async fn a_read_loop_that_gives_up_reports_and_stops() {
    let (srv, rec) = start_rec(serde_json::json!({"dial_enabled": true})).await;
    srv.give_up("SIP", &std::io::Error::other("boom"));
    tokio::time::timeout(Duration::from_secs(3), srv.done.cancelled())
        .await
        .expect("the server was not stopped");
    let errs = lock(&rec.errors).clone();
    assert_eq!(errs.len(), 1, "{errs:?}");
    assert_eq!(errs[0].0, "gs");
    assert_eq!(errs[0].1, "socket_failed");
    assert!(errs[0].2.contains("SIP socket failed for good"), "{errs:?}");
    assert!(!srv.can_dial());
}

// R2-94: the retry policy of the read loops: more than 100 failures IN A ROW give up, a good read
// resets the count (the loops use `ReadErrors` and `READ_RETRY_PAUSE`).
#[test]
fn read_errors_give_up_after_a_hundred_in_a_row() {
    let mut e = ReadErrors::default();
    assert!((0..100).all(|_| !e.failed()), "gave up within 100");
    assert!(e.failed(), "the 101st in a row must give up");
    let mut e = ReadErrors::default();
    for _ in 0..3 {
        assert!((0..100).all(|_| !e.failed()));
        e.ok(); // a good read in between: the count starts again
    }
    assert_eq!(READ_RETRY_PAUSE, Duration::from_millis(10));
}

// R1-87: Go prints a `*net.UDPAddr` with an IPv6 host in brackets.
#[test]
fn the_media_address_prints_as_go_prints_a_udp_addr() {
    let v4 = FarRtp {
        ip: "10.8.0.39".parse().unwrap(),
        port: 20002,
    };
    let v6 = FarRtp {
        ip: "::1".parse().unwrap(),
        port: 20002,
    };
    let big = FarRtp {
        ip: "::1".parse().unwrap(),
        port: 70000,
    };
    assert_eq!(v4.go_text(), "10.8.0.39:20002");
    assert_eq!(v6.go_text(), "[::1]:20002");
    assert_eq!(big.go_text(), "[::1]:70000");
}

// R1-7: the scope id of a link-local peer survives `canon`; an IPv4-mapped one is unmapped.
#[test]
fn canon_keeps_the_scope_id() {
    let ll: SocketAddr = "[fe80::1%2]:5060".parse().unwrap();
    let SocketAddr::V6(v6) = canon(ll) else {
        panic!("a link-local address was not kept as IPv6");
    };
    assert_eq!(v6.scope_id(), 2);
    assert_eq!(v6.port(), 5060);
    assert_eq!(
        canon("[::ffff:10.8.0.39]:5060".parse().unwrap()),
        "10.8.0.39:5060".parse::<SocketAddr>().unwrap()
    );
    assert_eq!(
        canon("10.8.0.39:1".parse().unwrap()),
        "10.8.0.39:1".parse::<SocketAddr>().unwrap()
    );
}

// R1-108: an IPv4 peer is handed to an IPv6 socket in its mapped form, to an IPv4 one as is;
// a wildcard bind is dual-stack whatever the system default says.
#[tokio::test]
async fn sip_destinations_follow_the_socket_family() {
    let v4: SocketAddr = "10.8.0.39:5060".parse().unwrap();
    assert_eq!(dest_for(false, v4), v4);
    assert_eq!(
        dest_for(true, v4),
        "[::ffff:10.8.0.39]:5060".parse::<SocketAddr>().unwrap()
    );
    let v6: SocketAddr = "[fe80::1%2]:5060".parse().unwrap();
    assert_eq!(dest_for(true, v6), v6);
    match bind_udp("[::]:0".parse().unwrap()) {
        Ok((_rx, tx)) => assert!(!socket2::SockRef::from(&*tx).only_v6().unwrap()),
        Err(e) => eprintln!("no IPv6 here, dual-stack check skipped: {e}"),
    }
}

// R1-83: an empty host in `sip_listen` prints as Go prints a nil IP.
#[test]
fn bind_failure_text_for_an_empty_host_is_go_s() {
    let e = std::io::Error::from(std::io::ErrorKind::AddrInUse);
    let a: SocketAddr = "[::]:5060".parse().unwrap();
    assert_eq!(
        listen_err(&go_listen_text(true, a), &e),
        "listen udp :5060: bind: address already in use"
    );
    assert_eq!(
        listen_err(
            &go_listen_text(false, "127.0.0.1:5060".parse().unwrap()),
            &e
        ),
        "listen udp 127.0.0.1:5060: bind: address already in use"
    );
}

// R1-109: only the Contact header's own parameters carry the expiry.
#[test]
fn the_contact_expiry_is_a_header_parameter() {
    for (c, want) in [
        ("<sip:8001@h>;expires=0", Some(0)),
        ("<sip:8001@h>;Expires=120;q=1", Some(120)),
        ("<sip:8001@h>;q=1;expires=7", Some(7)),
        ("sip:8001@h;expires=30", Some(30)),
        // not the header parameter:
        ("<sip:8001@h;expires=0>", None),
        ("<sip:8001@h>;x-expires=0", None),
        ("<sip:8001@h>;reg-expires=0", None),
        ("\"a;expires=0\" <sip:8001@h>", None),
        ("\"a \\\" ;expires=0\" <sip:8001@h>;expires=5", Some(5)),
        ("sip:8001@h", None),
    ] {
        assert_eq!(contact_expires(c), want, "{c}");
    }
}

// R1-132: an entry older than the quiet minute is dropped, so a full table does not silence
// every new outsider for good.
#[tokio::test]
async fn the_drop_log_table_is_pruned() {
    let (srv, _rec) = start_rec(serde_json::json!({"allow_from": ["127.0.0.0/8"]})).await;
    {
        let mut st = lock(&srv.state);
        // a host whose monotonic clock is under 120 s (a fresh VM) cannot subtract: use what exists
        let Some(old) = Instant::now().checked_sub(Duration::from_secs(120)) else {
            eprintln!("skipped: the monotonic clock is younger than 120 s");
            return;
        };
        for i in 0..256u32 {
            st.last_drop_log
                .insert(IpAddr::from(std::net::Ipv4Addr::from(0x0a09_0000 + i)), old);
        }
    }
    let lines = Arc::new(Mutex::new(Vec::<String>::new()));
    let l2 = lines.clone();
    srv.set_logger(move |l| lock(&l2).push(l.to_string()));
    let outsider: SocketAddr = "10.8.8.8:5060".parse().unwrap();
    assert!(!srv.sip_allowed(outsider));
    assert!(
        lock(&srv.state).last_drop_log.contains_key(&outsider.ip()),
        "a new outsider was not recorded"
    );
    assert!(
        lock(&lines).iter().any(|l| l.contains("10.8.8.8")),
        "{:?}",
        lock(&lines)
    );
    srv.shutdown().await;
}
