use super::*;
use crate::sip::dialog::{build_sdp, dial, SdpInfo};
use std::sync::Arc;
use tokio::net::UdpSocket;
use tokio::sync::Notify;
use tokio::time::timeout;

pub(crate) fn via_line(req: &str) -> String {
    for line in req.split("\r\n") {
        if line.starts_with("Via:") {
            return format!("{line}\r\n");
        }
    }
    String::new()
}

type Reply = Arc<dyn Fn(String) + Send + Sync>;

/// fakePeer: a UDP "device" that hands every datagram to `f` with a reply helper.
pub(crate) async fn fake_peer<F>(f: F) -> Endpoint
where
    F: Fn(String, Reply) + Send + Sync + 'static,
{
    let sock = Arc::new(UdpSocket::bind("127.0.0.1:0").await.unwrap());
    let addr = sock.local_addr().unwrap();
    let s2 = sock.clone();
    let task = tokio::spawn(async move {
        let mut buf = vec![0u8; 4096];
        loop {
            let Ok((n, raddr)) = s2.recv_from(&mut buf).await else {
                return;
            };
            let s3 = s2.clone();
            let reply: Reply = Arc::new(move |s: String| {
                let s3 = s3.clone();
                tokio::spawn(async move {
                    let _ = s3.send_to(s.as_bytes(), raddr).await;
                });
            });
            f(String::from_utf8_lossy(&buf[..n]).into_owned(), reply);
        }
    });
    drop(task); // detached: lives until the runtime of the test ends
    Endpoint::new(&addr.ip().to_string(), i64::from(addr.port()))
        .await
        .unwrap()
}

pub(crate) fn shrink_timers(ep: &Endpoint) {
    ep.set_timers(Timers {
        t1: Duration::from_millis(20),
        t2: Duration::from_millis(80),
        send_timeout: Duration::from_millis(400),
        invite_ring_timeout: Duration::from_millis(300),
        cancel_wait: Duration::from_millis(300),
    });
}

async fn dial_test(ep: &Endpoint) -> Result<crate::sip::Call, Error> {
    dial(
        ep,
        "portico",
        "9901#0",
        SdpInfo {
            host: "127.0.0.1".into(),
            port: 40000,
            payload_type: 0,
        },
        None,
    )
    .await
}

fn ok_with_sdp(via: &str) -> String {
    let sdp = build_sdp("127.0.0.1", 45000);
    format!(
        "SIP/2.0 200 OK\r\n{via}To: <sip:device@127.0.0.1>;tag=devicetag\r\nContent-Type: application/sdp\r\nContent-Length: {}\r\n\r\n{sdp}",
        sdp.len()
    )
}

use std::sync::atomic::AtomicUsize;

// TestSendRequest_RetransmitsInviteUntilResponse
#[tokio::test]
async fn send_request_retransmits_invite_until_response() {
    let seen = Arc::new(AtomicUsize::new(0));
    let s2 = seen.clone();
    let ep = fake_peer(move |req, reply| {
        if !req.starts_with("INVITE ") {
            return;
        }
        if s2.fetch_add(1, Ordering::SeqCst) + 1 >= 3 {
            reply(ok_with_sdp(&via_line(&req))); // drop the first two copies
        }
    })
    .await;
    shrink_timers(&ep);
    dial_test(&ep).await.unwrap();
    assert!(seen.load(Ordering::SeqCst) >= 3);
}

/// R2-15: a peer that never answers, recording the (paused-clock) instant of every datagram.
async fn silent_peer() -> (Endpoint, Arc<std::sync::Mutex<Vec<tokio::time::Instant>>>) {
    let log = Arc::new(std::sync::Mutex::new(Vec::new()));
    let l2 = log.clone();
    let ep = fake_peer(move |_, _| l2.lock().unwrap().push(tokio::time::Instant::now())).await;
    ep.set_timers(Timers {
        t1: Duration::from_millis(500),
        t2: Duration::from_secs(4),
        send_timeout: Duration::from_secs(32),
        invite_ring_timeout: Duration::from_secs(180),
        cancel_wait: Duration::from_secs(2),
    });
    (ep, log)
}

fn gaps_ms(log: &Arc<std::sync::Mutex<Vec<tokio::time::Instant>>>) -> Vec<u128> {
    let v = log.lock().unwrap();
    v.windows(2).map(|w| (w[1] - w[0]).as_millis()).collect()
}

// R2-15. RFC 3261 17.1.1.2 / 17.1.2.2: the INVITE interval doubles WITHOUT a cap (T1, 2 T1, 4 T1
// ... until Timer B), a non-INVITE doubles up to T2 and then stays there. The ports of the Go
// retransmit tests only assert "at least 3 INVITEs" and so cannot tell a doubling from a constant.
#[tokio::test(start_paused = true)]
async fn invite_retransmits_double_without_a_cap() {
    let (ep, log) = silent_peer().await;
    assert!(matches!(dial_test(&ep).await, Err(Error::Timeout)));
    // sent at 0, 0.5, 1.5, 3.5, 7.5, 15.5 and 31.5 s (the next would fall after the 32 s Timer B)
    assert_eq!(gaps_ms(&log), vec![500, 1000, 2000, 4000, 8000, 16000]);
}

#[tokio::test(start_paused = true)]
async fn non_invite_retransmits_double_up_to_t2() {
    let (ep, log) = silent_peer().await;
    assert!(matches!(probe(&ep).await, Err(Error::Timeout)));
    // sent at 0, 0.5, 1.5, 3.5, 7.5 s and then every T2 = 4 s up to 31.5 s
    assert_eq!(
        gaps_ms(&log),
        vec![500, 1000, 2000, 4000, 4000, 4000, 4000, 4000, 4000, 4000]
    );
}

// TestSendRequest_RingingExtendsWaitAndStopsRetransmit
#[tokio::test]
async fn send_request_ringing_extends_wait_and_stops_retransmit() {
    let invites = Arc::new(AtomicUsize::new(0));
    let i2 = invites.clone();
    let ep = fake_peer(move |req, reply| {
        if !req.starts_with("INVITE ") {
            return;
        }
        i2.fetch_add(1, Ordering::SeqCst);
        let via = via_line(&req);
        reply(format!(
            "SIP/2.0 180 Ringing\r\n{via}Content-Length: 0\r\n\r\n"
        ));
        tokio::spawn(async move {
            // Keep ringing past send_timeout (400ms), answer at 450ms; the 1xx at 250ms
            // restarts Timer C so the 300ms window is never missed.
            sleep(Duration::from_millis(250)).await;
            reply(format!(
                "SIP/2.0 180 Ringing\r\n{via}Content-Length: 0\r\n\r\n"
            ));
            sleep(Duration::from_millis(200)).await;
            reply(ok_with_sdp(&via));
        });
    })
    .await;
    shrink_timers(&ep);
    dial_test(&ep).await.unwrap();
    assert_eq!(
        invites.load(Ordering::SeqCst),
        1,
        "no retransmit after a 1xx"
    );
}

// TestSendRequest_AcksNon2xxFinal
#[tokio::test]
async fn send_request_acks_non_2xx_final() {
    let (tx, mut rx) = mpsc::unbounded_channel::<String>();
    let ep = fake_peer(move |req, reply| {
        if req.starts_with("INVITE ") {
            reply(format!(
                "SIP/2.0 486 Busy Here\r\n{}To: <sip:device@127.0.0.1>;tag=busytag\r\nContent-Length: 0\r\n\r\n",
                via_line(&req)
            ));
        } else if req.starts_with("ACK ") {
            let _ = tx.send(req);
        }
    })
    .await;
    shrink_timers(&ep);
    let err = dial_test(&ep).await.err().expect("486 failure");
    assert!(err.to_string().contains("486"), "{err}");
    let ack = timeout(Duration::from_secs(1), rx.recv())
        .await
        .expect("no ACK for the 486")
        .unwrap();
    assert!(
        ack.contains("tag=busytag") && ack.contains("CSeq: 1 ACK"),
        "bad ACK:\n{ack}"
    );
}

fn cseq_line(req: &str) -> String {
    for line in req.split("\r\n") {
        if line.starts_with("CSeq:") {
            return format!("{line}\r\n");
        }
    }
    String::new()
}

/// A peer that rings, then on CANCEL answers `200 OK (CANCEL)` and, depending on `crossed`,
/// either the 487 for the INVITE or a 200 for the INVITE that crossed the CANCEL. Reports
/// every request it sees on `seen`.
async fn ringing_peer(crossed: bool, seen: mpsc::UnboundedSender<String>) -> Endpoint {
    fake_peer(move |req, reply| {
        let via = via_line(&req);
        let cseq = cseq_line(&req);
        let _ = seen.send(req.clone());
        if req.starts_with("INVITE ") {
            reply(format!("SIP/2.0 180 Ringing\r\n{via}{cseq}Content-Length: 0\r\n\r\n"));
        } else if req.starts_with("CANCEL ") {
            reply(format!("SIP/2.0 200 OK\r\n{via}{cseq}Content-Length: 0\r\n\r\n"));
            let inv_cseq = cseq.replace("CANCEL", "INVITE");
            if crossed {
                reply(ok_with_sdp(&format!("{via}{inv_cseq}")).replace(
                    "tag=devicetag",
                    "tag=t200",
                ));
            } else {
                reply(format!(
                    "SIP/2.0 487 Request Terminated\r\n{via}{inv_cseq}To: <sip:device@127.0.0.1>;tag=t487\r\nContent-Length: 0\r\n\r\n"
                ));
            }
        }
    })
    .await
}

async fn collect(rx: &mut mpsc::UnboundedReceiver<String>, ms: u64) -> Vec<String> {
    sleep(Duration::from_millis(ms)).await;
    let mut v = Vec::new();
    while let Ok(m) = rx.try_recv() {
        v.push(m);
    }
    v
}

// TestSendRequest_CancelsAfterTimerCAndAcks487 - fixed: the ACK must carry the 487's To-tag
// and no BYE may follow (Go acks/byes the CANCEL's own 200 instead).
#[tokio::test]
async fn send_request_cancels_after_timer_c_and_acks_487() {
    let (tx, mut rx) = mpsc::unbounded_channel::<String>();
    let ep = ringing_peer(false, tx).await;
    shrink_timers(&ep);
    let err = dial_test(&ep).await.err().expect("timeout");
    assert!(err.is_timeout(), "{err}");
    assert_eq!(err.to_string(), "sip: timed out waiting for a response");
    let seen = collect(&mut rx, 200).await;
    let find = |p: &str| seen.iter().find(|m| m.starts_with(p)).cloned();
    let cancel = find("CANCEL ").expect("peer never saw CANCEL");
    assert!(cancel.contains("CSeq: 1 CANCEL"), "{cancel}");
    let ack = find("ACK ").expect("peer never saw the ACK for the 487");
    assert!(ack.contains("CSeq: 1 ACK"), "{ack}");
    assert!(
        ack.contains("tag=t487"),
        "ACK must carry the 487's To-tag:\n{ack}"
    );
    assert!(find("BYE ").is_none(), "spurious BYE: {seen:?}");
}

// A genuine 200 to the INVITE that crossed the CANCEL is still ACKed and then BYEd.
#[tokio::test]
async fn crossed_200_is_acked_and_torn_down() {
    let (tx, mut rx) = mpsc::unbounded_channel::<String>();
    let ep = ringing_peer(true, tx).await;
    shrink_timers(&ep);
    dial_test(&ep).await.err().expect("timeout");
    let seen = collect(&mut rx, 200).await;
    let find = |p: &str| seen.iter().find(|m| m.starts_with(p)).cloned();
    let ack = find("ACK ").expect("no ACK for the crossed 200");
    assert!(
        ack.contains("tag=t200") && ack.contains("CSeq: 1 ACK"),
        "{ack}"
    );
    let bye = find("BYE ").expect("no BYE for the crossed 200");
    assert!(
        bye.contains("CSeq: 2 BYE") && bye.contains("tag=t200"),
        "{bye}"
    );
}

// Dropping the dial future (hangup mid-ring) CANCELs and cleans up.
#[tokio::test]
async fn dropped_ringing_dial_sends_cancel_and_acks_487() {
    let (tx, mut rx) = mpsc::unbounded_channel::<String>();
    let ep = ringing_peer(false, tx).await;
    shrink_timers(&ep);
    ep.set_timers(Timers {
        send_timeout: Duration::from_secs(30),
        invite_ring_timeout: Duration::from_secs(30),
        ..Timers::default()
    });
    let ep2 = ep.clone();
    let t = tokio::spawn(async move { dial_test(&ep2).await.map(|_| ()) });
    sleep(Duration::from_millis(100)).await; // ringing by now
    t.abort();
    let seen = collect(&mut rx, 300).await;
    let find = |p: &str| seen.iter().find(|m| m.starts_with(p)).cloned();
    assert!(
        find("CANCEL ").is_some(),
        "no CANCEL after the drop: {seen:?}"
    );
    let ack = find("ACK ").expect("487 not acked after the drop");
    assert!(ack.contains("tag=t487"), "{ack}");
    assert!(find("BYE ").is_none());
}

#[tokio::test]
async fn dropped_ringing_dial_byes_a_late_200() {
    let (tx, mut rx) = mpsc::unbounded_channel::<String>();
    let ep = ringing_peer(true, tx).await;
    ep.set_timers(Timers {
        send_timeout: Duration::from_secs(30),
        invite_ring_timeout: Duration::from_secs(30),
        ..Timers::default()
    });
    let ep2 = ep.clone();
    let t = tokio::spawn(async move { dial_test(&ep2).await.map(|_| ()) });
    sleep(Duration::from_millis(100)).await;
    t.abort();
    let seen = collect(&mut rx, 300).await;
    assert!(seen.iter().any(|m| m.starts_with("ACK ")), "{seen:?}");
    assert!(seen.iter().any(|m| m.starts_with("BYE ")), "{seen:?}");
}

// Not ringing yet: dropping must only unregister, no CANCEL.
#[tokio::test]
async fn dropped_unanswered_dial_sends_no_cancel() {
    let (tx, mut rx) = mpsc::unbounded_channel::<String>();
    let ep = fake_peer(move |req, _| {
        let _ = tx.send(req);
    })
    .await;
    let ep2 = ep.clone();
    let t = tokio::spawn(async move { dial_test(&ep2).await.map(|_| ()) });
    sleep(Duration::from_millis(50)).await;
    t.abort();
    let seen = collect(&mut rx, 200).await;
    assert!(!seen.iter().any(|m| m.starts_with("CANCEL ")), "{seen:?}");
    assert!(lock(&ep.inner.state).pending.is_empty());
}

/// A peer on a fixed port that answers every REGISTER with 200; dropping it frees the port.
async fn register_peer(port: u16) -> tokio::task::JoinHandle<()> {
    let sock = UdpSocket::bind(("127.0.0.1", port)).await.unwrap();
    tokio::spawn(async move {
        let mut buf = vec![0u8; 4096];
        while let Ok((n, from)) = sock.recv_from(&mut buf).await {
            let req = String::from_utf8_lossy(&buf[..n]).into_owned();
            let resp = format!(
                "SIP/2.0 200 OK\r\n{}Content-Length: 0\r\n\r\n",
                via_line(&req)
            );
            let _ = sock.send_to(resp.as_bytes(), from).await;
        }
    })
}

async fn probe(ep: &Endpoint) -> Result<Response, Error> {
    let branch = format!("z9hG4bK{}", rand_hex(8));
    let raw = format!(
        "OPTIONS sip:x SIP/2.0\r\nVia: SIP/2.0/UDP 127.0.0.1:1;branch={branch}\r\nCSeq: 1 OPTIONS\r\nContent-Length: 0\r\n\r\n"
    );
    ep.send_request(raw.as_bytes(), &branch, None).await
}

// The station reboots: its port refuses for a while (ICMP), then answers again. The read
// loop must survive the refusal (Go's returns for good).
#[tokio::test]
async fn endpoint_survives_peer_restart() {
    let probe_sock = UdpSocket::bind("127.0.0.1:0").await.unwrap();
    let port = probe_sock.local_addr().unwrap().port();
    drop(probe_sock);
    let peer = register_peer(port).await;
    let ep = Endpoint::new("127.0.0.1", i64::from(port)).await.unwrap();
    probe(&ep).await.expect("first exchange");
    peer.abort();
    let _ = peer.await;
    // provoke ICMP errors on the connected socket
    for _ in 0..3 {
        let _ = ep.write(b"x").await;
        sleep(Duration::from_millis(20)).await;
    }
    let _peer2 = register_peer(port).await;
    probe(&ep)
        .await
        .expect("endpoint deaf after the peer restarted");
}

// A request sent while the station is down keeps retransmitting through refusals and is
// answered once it is back.
#[tokio::test]
async fn retransmits_survive_refusals_until_the_peer_returns() {
    let probe_sock = UdpSocket::bind("127.0.0.1:0").await.unwrap();
    let port = probe_sock.local_addr().unwrap().port();
    drop(probe_sock);
    let ep = Endpoint::new("127.0.0.1", i64::from(port)).await.unwrap();
    ep.set_timers(Timers {
        t1: Duration::from_millis(30),
        t2: Duration::from_millis(60),
        send_timeout: Duration::from_secs(3),
        ..Timers::default()
    });
    let late = tokio::spawn(async move {
        sleep(Duration::from_millis(400)).await;
        let _peer = register_peer(port).await;
        sleep(Duration::from_secs(10)).await;
    });
    probe(&ep)
        .await
        .expect("request aborted by a refused retransmit");
    late.abort();
}

// Echoed headers keep their bytes (Latin-1 in a display name must not become U+FFFD).
#[tokio::test]
async fn echoed_headers_keep_raw_bytes() {
    let peer = UdpSocket::bind("127.0.0.1:0").await.unwrap();
    let pa = peer.local_addr().unwrap();
    let e = Endpoint::new(&pa.ip().to_string(), i64::from(pa.port()))
        .await
        .unwrap();
    e.handle("OPTIONS", |r| async move {
        let _ = r.respond(200, "OK", "tt", "").await;
    });
    let local: std::net::SocketAddr = e.local_addr().parse().unwrap();
    let mut msg = b"OPTIONS sip:x@y SIP/2.0\r\nVia: SIP/2.0/UDP 127.0.0.1:9;branch=z9hG4bKr\r\nFrom: \"Caf\xe9\" <sip:f@h>;tag=ff\r\nTo: <sip:t\xa4@h>\r\nCall-ID: id\xe9\r\nCSeq: 3 OPTIONS\r\n\r\n".to_vec();
    peer.send_to(&msg, local).await.unwrap();
    let mut buf = vec![0u8; 4096];
    let n = timeout(Duration::from_secs(2), peer.recv(&mut buf))
        .await
        .unwrap()
        .unwrap();
    let got = &buf[..n];
    let has = |needle: &[u8]| got.windows(needle.len()).any(|w| w == needle);
    assert!(
        has(b"From: \"Caf\xe9\" <sip:f@h>;tag=ff\r\n"),
        "{:?}",
        String::from_utf8_lossy(got)
    );
    assert!(has(b"To: <sip:t\xa4@h>;tag=tt\r\n"));
    assert!(has(b"Call-ID: id\xe9\r\n"));
    msg.clear();

    // ACK of a non-2xx echoes the response's To and the INVITE's URI/From as received.
    let peer2 = fake_raw_peer(|req| {
        if req.starts_with(b"INVITE ") {
            let via = req
                .split(|&c| c == b'\n')
                .find(|l| l.starts_with(b"Via:"))
                .unwrap()
                .to_vec();
            let mut r = b"SIP/2.0 486 Busy Here\r\n".to_vec();
            r.extend_from_slice(&via);
            r.extend_from_slice(
                b"\nTo: <sip:d\xe9@h>;tag=bz\r\nCSeq: 1 INVITE\r\nContent-Length: 0\r\n\r\n",
            );
            Some(r)
        } else {
            None
        }
    })
    .await;
    let (ep2, mut acks) = peer2;
    let _ = dial_test(&ep2).await;
    let ack = timeout(Duration::from_secs(1), acks.recv())
        .await
        .unwrap()
        .unwrap();
    assert!(
        ack.windows(8).any(|w| w == b"To: <sip") && ack.windows(10).any(|w| w == b"sip:d\xe9@h>;"),
        "{:?}",
        String::from_utf8_lossy(&ack)
    );
}

/// A peer working on raw bytes; the endpoint's received ACKs come out on the channel.
async fn fake_raw_peer<F>(f: F) -> (Endpoint, mpsc::UnboundedReceiver<Vec<u8>>)
where
    F: Fn(&[u8]) -> Option<Vec<u8>> + Send + 'static,
{
    let sock = UdpSocket::bind("127.0.0.1:0").await.unwrap();
    let addr = sock.local_addr().unwrap();
    let (tx, rx) = mpsc::unbounded_channel();
    tokio::spawn(async move {
        let mut buf = vec![0u8; 4096];
        while let Ok((n, from)) = sock.recv_from(&mut buf).await {
            if buf[..n].starts_with(b"ACK ") {
                let _ = tx.send(buf[..n].to_vec());
            }
            if let Some(r) = f(&buf[..n]) {
                let _ = sock.send_to(&r, from).await;
            }
        }
    });
    let ep = Endpoint::new(&addr.ip().to_string(), i64::from(addr.port()))
        .await
        .unwrap();
    shrink_timers(&ep);
    (ep, rx)
}

// A panic while the handler BUILDS its future must not hit the read loop.
#[tokio::test]
async fn a_handler_that_panics_synchronously_is_contained() {
    let peer = UdpSocket::bind("127.0.0.1:0").await.unwrap();
    let pa = peer.local_addr().unwrap();
    let e = Endpoint::new(&pa.ip().to_string(), i64::from(pa.port()))
        .await
        .unwrap();
    let (tx, mut rx) = mpsc::unbounded_channel::<()>();
    e.handle("INVITE", |_r| {
        if true {
            panic!("boom while building the future");
        }
        async {}
    });
    e.handle("BYE", move |_r| {
        let tx = tx.clone();
        async move {
            let _ = tx.send(());
        }
    });
    let local: std::net::SocketAddr = e.local_addr().parse().unwrap();
    for (m, id) in [("INVITE", "k"), ("BYE", "k"), ("BYE", "other")] {
        let msg = format!("{m} sip:x@y SIP/2.0\r\nCall-ID: {id}\r\nCSeq: 1 {m}\r\n\r\n");
        peer.send_to(msg.as_bytes(), local).await.unwrap();
    }
    for _ in 0..2 {
        timeout(Duration::from_secs(2), rx.recv())
            .await
            .expect("read loop or queue died")
            .unwrap();
    }
}

// TestSendRequest_NoResponseTimesOutWithoutCancel
#[tokio::test]
async fn send_request_no_response_times_out_without_cancel() {
    let (tx, mut rx) = mpsc::unbounded_channel::<()>();
    let ep = fake_peer(move |req, _reply| {
        if req.starts_with("CANCEL ") {
            let _ = tx.send(());
        }
    })
    .await;
    shrink_timers(&ep);
    assert!(
        dial_test(&ep).await.is_err(),
        "Dial succeeded against a silent peer"
    );
    assert!(
        timeout(Duration::from_millis(100), rx.recv())
            .await
            .is_err(),
        "CANCEL sent although no provisional was ever received (9.1)"
    );
}

// TestInboundHandlersOffReadLoopOrderedPerCallID
#[tokio::test]
async fn inbound_handlers_off_read_loop_ordered_per_call_id() {
    let peer = UdpSocket::bind("127.0.0.1:0").await.unwrap();
    let pa = peer.local_addr().unwrap();
    let e = Endpoint::new(&pa.ip().to_string(), i64::from(pa.port()))
        .await
        .unwrap();
    let release = Arc::new(Notify::new());
    let order: Arc<Mutex<Vec<String>>> = Arc::default();
    let (done_tx, mut done_rx) = mpsc::unbounded_channel::<()>();
    {
        let (release, order, done) = (release.clone(), order.clone(), done_tx.clone());
        e.handle("INVITE", move |r| {
            let (release, order, done) = (release.clone(), order.clone(), done.clone());
            async move {
                release.notified().await; // a handler stuck in a blocked IPC write
                lock(&order).push(format!("INVITE-{}", r.header("Call-ID")));
                let _ = done.send(());
            }
        });
    }
    for m in ["CANCEL", "BYE"] {
        let (order, done) = (order.clone(), done_tx.clone());
        e.handle(m, move |r| {
            let (order, done) = (order.clone(), done.clone());
            async move {
                lock(&order).push(format!("{m}-{}", r.header("Call-ID")));
                let _ = done.send(());
            }
        });
    }
    let local: std::net::SocketAddr = e.local_addr().parse().unwrap();
    for (m, id) in [("INVITE", "a"), ("CANCEL", "a"), ("BYE", "b")] {
        let msg = format!(
            "{m} sip:x@y SIP/2.0\r\nCall-ID: {id}\r\nCSeq: 1 {m}\r\nContent-Length: 0\r\n\r\n"
        );
        peer.send_to(msg.as_bytes(), local).await.unwrap();
    }
    timeout(Duration::from_secs(2), done_rx.recv())
        .await
        .expect("BYE for another Call-ID blocked behind a stuck INVITE");

    // The read loop is still alive while INVITE a is stuck: a response is delivered to a
    // pending request.
    let (ptx, mut prx) = mpsc::channel(1);
    lock(&e.inner.state)
        .pending
        .insert("z9hG4bKtest".into(), ptx);
    let resp = "SIP/2.0 200 OK\r\nVia: SIP/2.0/UDP 127.0.0.1;branch=z9hG4bKtest\r\nContent-Length: 0\r\n\r\n";
    peer.send_to(resp.as_bytes(), local).await.unwrap();
    timeout(Duration::from_secs(2), prx.recv())
        .await
        .expect("read loop blocked by a stuck handler");

    release.notify_one();
    for _ in 0..2 {
        timeout(Duration::from_secs(2), done_rx.recv())
            .await
            .expect("handlers did not finish");
    }
    let order = lock(&order);
    let pos = |s: &str| order.iter().position(|x| x == s);
    assert!(
        pos("INVITE-a").unwrap() < pos("CANCEL-a").unwrap(),
        "CANCEL ran before INVITE: {order:?}"
    );
    e.close().unwrap();
}

#[tokio::test]
async fn a_panicking_handler_does_not_wedge_the_call_id() {
    let peer = UdpSocket::bind("127.0.0.1:0").await.unwrap();
    let pa = peer.local_addr().unwrap();
    let e = Endpoint::new(&pa.ip().to_string(), i64::from(pa.port()))
        .await
        .unwrap();
    let (tx, mut rx) = mpsc::unbounded_channel::<()>();
    e.handle("INVITE", |_r| async { panic!("boom") });
    e.handle("BYE", move |_r| {
        let tx = tx.clone();
        async move {
            let _ = tx.send(());
        }
    });
    let local: std::net::SocketAddr = e.local_addr().parse().unwrap();
    for m in ["INVITE", "BYE"] {
        let msg = format!("{m} sip:x@y SIP/2.0\r\nCall-ID: k\r\nCSeq: 1 {m}\r\n\r\n");
        peer.send_to(msg.as_bytes(), local).await.unwrap();
    }
    timeout(Duration::from_secs(2), rx.recv())
        .await
        .expect("BYE never ran after the panic")
        .unwrap();
}

#[test]
fn branch_and_cseq_helpers() {
    assert_eq!(
        branch_from_via("SIP/2.0/UDP 1.2.3.4:5;branch=z9hG4bKab ;rport"),
        "z9hG4bKab"
    );
    assert_eq!(branch_from_via("SIP/2.0/UDP x"), "");
}

// R1-76: `n, _ := strconv.Atoi(..)` clamps on a range error.
#[test]
fn cseq_number_clamps_like_go() {
    let req = |c: &str| {
        parse_request(format!("INVITE sip:a@b SIP/2.0\r\nCSeq: {c} INVITE\r\n\r\n").as_bytes())
            .unwrap()
    };
    assert_eq!(cseq_number(&req("7")), 7);
    assert_eq!(cseq_number(&req("x")), 0);
    assert_eq!(cseq_number(&req("99999999999999999999")), i64::MAX);
}

async fn wait_port_free(port: u16) -> bool {
    let end = tokio::time::Instant::now() + Duration::from_secs(2);
    loop {
        if UdpSocket::bind(("0.0.0.0", port)).await.is_ok() {
            return true;
        }
        if tokio::time::Instant::now() > end {
            return false;
        }
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
}

// R1-17: close releases the socket even while a clone of the endpoint is alive and a handler
// closure holds an IncomingRequest (a strong Endpoint): the cycle the dahua ring state makes.
#[tokio::test]
async fn close_releases_the_socket_and_breaks_the_handler_cycle() {
    let peer = UdpSocket::bind("127.0.0.1:0").await.unwrap();
    let pa = peer.local_addr().unwrap();
    let e = Endpoint::new(&pa.ip().to_string(), i64::from(pa.port()))
        .await
        .unwrap();
    let held: Arc<std::sync::Mutex<Option<IncomingRequest>>> = Arc::default();
    let h2 = held.clone();
    e.handle("INVITE", move |r| {
        let h2 = h2.clone();
        async move {
            *lock(&h2) = Some(r);
        }
    });
    let port: u16 = e.local_addr().rsplit(':').next().unwrap().parse().unwrap();
    let local: std::net::SocketAddr = e.local_addr().parse().unwrap();
    peer.send_to(
        b"INVITE sip:x@y SIP/2.0\r\nVia: SIP/2.0/UDP 127.0.0.1:9;branch=z9hG4bKq\r\nCall-ID: c1\r\nCSeq: 1 INVITE\r\n\r\n",
        local,
    )
    .await
    .unwrap();
    let end = tokio::time::Instant::now() + Duration::from_secs(2);
    while lock(&held).is_none() {
        assert!(tokio::time::Instant::now() < end, "handler never ran");
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
    assert!(
        UdpSocket::bind(("0.0.0.0", port)).await.is_err(),
        "bound while open"
    );
    e.close().unwrap();
    e.close().unwrap(); // idempotent
                        // `e` and the request held by the handler's state are both still alive here
    assert!(
        wait_port_free(port).await,
        "the port stayed bound after close()"
    );
    assert!(e.write(b"x").await.unwrap_err().is_closed());
    drop(held);
}

// R1-121: close ends a request that is waiting out its deadline after a 1xx.
// R2-85 (declined): a request STARTED on a closed endpoint says `sip: writing request: use of
// closed network connection`, which is Go's text (`dahua_driver.rs` pins it for a dial after
// stop); only a request that was already waiting reports `Error::Closed`.
#[tokio::test]
async fn a_request_started_on_a_closed_endpoint_has_gos_writing_request_text() {
    let ep = fake_peer(|_, _| {}).await;
    ep.close().unwrap();
    let e = probe(&ep).await.unwrap_err();
    assert_eq!(
        e.to_string(),
        "sip: writing request: use of closed network connection"
    );
}

#[tokio::test]
async fn close_ends_a_ringing_request_at_once() {
    let ep = fake_peer(|req, reply| {
        if req.starts_with("INVITE ") {
            reply(format!(
                "SIP/2.0 180 Ringing\r\n{}Content-Length: 0\r\n\r\n",
                via_line(&req)
            ));
        }
    })
    .await;
    let (tx, mut rx) = mpsc::unbounded_channel::<()>();
    let ep2 = ep.clone();
    let task = tokio::spawn(async move {
        let mut ring = move || {
            let _ = tx.send(());
        };
        let mut cb: Option<&mut (dyn FnMut() + Send)> = Some(&mut ring);
        let r = crate::sip::dial(
            &ep2,
            "portico",
            "9901#0",
            SdpInfo {
                host: "127.0.0.1".into(),
                port: 40000,
                payload_type: 0,
            },
            cb.take(),
        )
        .await;
        r.err()
    });
    timeout(Duration::from_secs(2), rx.recv())
        .await
        .unwrap()
        .unwrap(); // ringing
    ep.close().unwrap();
    let err = timeout(Duration::from_secs(2), task)
        .await
        .expect("dial kept waiting")
        .unwrap();
    assert!(err.expect("dial succeeded").is_closed());
}

// R2-84 / R3A-7: `close` ends the wait for the CANCEL's final response (difference 313). The peer
// rings and then ignores the CANCEL, so the dial sits in `cancel_wait` (60 s here, paused clock);
// closing the endpoint there must end it at once, not after the wait.
#[tokio::test(start_paused = true)]
async fn close_during_the_cancel_wait_ends_the_dial_before_the_wait() {
    let ep = fake_peer(|req, reply| {
        if req.starts_with("INVITE ") {
            reply(format!(
                "SIP/2.0 180 Ringing\r\n{}Content-Length: 0\r\n\r\n",
                via_line(&req)
            ));
        }
    })
    .await;
    ep.set_timers(Timers {
        t1: Duration::from_millis(500),
        t2: Duration::from_secs(4),
        send_timeout: Duration::from_secs(32),
        invite_ring_timeout: Duration::from_secs(1),
        cancel_wait: Duration::from_secs(60),
    });
    let ep2 = ep.clone();
    let task = tokio::spawn(async move { dial_test(&ep2).await.err() });
    // the ring timeout (1 s) has fired and the CANCEL is out: the dial is now in `cancel_wait`
    sleep(Duration::from_secs(2)).await;
    assert!(!task.is_finished(), "the dial ended before the CANCEL wait");
    let closed_at = tokio::time::Instant::now();
    ep.close().unwrap();
    let _ = timeout(Duration::from_secs(5), task)
        .await
        .expect("the dial kept waiting after close")
        .unwrap();
    assert!(
        closed_at.elapsed() < Duration::from_secs(5),
        "close did not end the CANCEL wait: {:?}",
        closed_at.elapsed()
    );
}

// R1-4: a UAS-side BYE echoes the peer's Call-ID, tag and user as the bytes received.
#[tokio::test]
async fn uas_bye_keeps_raw_peer_bytes() {
    let peer = UdpSocket::bind("127.0.0.1:0").await.unwrap();
    let pa = peer.local_addr().unwrap();
    let e = Endpoint::new(&pa.ip().to_string(), i64::from(pa.port()))
        .await
        .unwrap();
    let (tx, mut rx) = mpsc::unbounded_channel::<IncomingRequest>();
    e.handle("INVITE", move |r| {
        let tx = tx.clone();
        async move {
            let _ = tx.send(r);
        }
    });
    let offer = build_sdp("127.0.0.1", 41000);
    let mut invite = format!(
        "INVITE sip:portico@x SIP/2.0\r\nVia: SIP/2.0/UDP 127.0.0.1:{};branch=z9hG4bKb\r\n",
        pa.port()
    )
    .into_bytes();
    invite.extend_from_slice(
        b"From: <sip:9\xe9\xff1@h>;tag=t\xe9g\r\nTo: <sip:po\xa4rtico@h>\r\nCall-ID: id\xe9\xff@h\r\nCSeq: 1 INVITE\r\nContent-Type: application/sdp\r\n",
    );
    invite.extend_from_slice(format!("Content-Length: {}\r\n\r\n{offer}", offer.len()).as_bytes());
    peer.send_to(
        &invite,
        e.local_addr().parse::<std::net::SocketAddr>().unwrap(),
    )
    .await
    .unwrap();
    let req = timeout(Duration::from_secs(2), rx.recv())
        .await
        .unwrap()
        .unwrap();
    let call = crate::sip::answer_incoming(
        &req,
        SdpInfo {
            host: "127.0.0.1".into(),
            port: 42000,
            payload_type: 0,
        },
    )
    .await
    .unwrap();
    let mut buf = vec![0u8; 4096];
    timeout(Duration::from_secs(2), peer.recv(&mut buf))
        .await
        .unwrap()
        .unwrap(); // the 200
    let bye = tokio::spawn(async move {
        let _ = call.bye().await;
    });
    let n = timeout(Duration::from_secs(2), peer.recv(&mut buf))
        .await
        .unwrap()
        .unwrap();
    bye.abort();
    let got = &buf[..n];
    let has = |needle: &[u8]| got.windows(needle.len()).any(|w| w == needle);
    assert!(
        has(b"Call-ID: id\xe9\xff@h\r\n"),
        "{:?}",
        String::from_utf8_lossy(got)
    );
    assert!(
        has(b"To: <sip:9\xe9\xff1@127.0.0.1>;tag=t\xe9g\r\n"),
        "{:?}",
        String::from_utf8_lossy(got)
    );
    assert!(
        has(b"From: <sip:po\xa4rtico@"),
        "{:?}",
        String::from_utf8_lossy(got)
    );
    e.close().unwrap();
}

// DEV-796 S2: the dispatch map must not grow without bound for a peer that sends distinct
// Call-IDs while handlers are blocked, nor a single key's queue for one repeated Call-ID.
#[tokio::test]
async fn dispatch_map_is_capped() {
    let ep = fake_peer(|_, _| {}).await;
    let gate = Arc::new(Notify::new());
    for i in 0..1000 {
        let g = gate.clone();
        ep.dispatch(
            format!("call-{i}"),
            "INVITE".into(),
            Box::new(move || Box::pin(async move { g.notified().await })),
        );
    }
    let keys = lock(&ep.inner.dq).len();
    assert!(keys <= 256, "dq grew to {keys} keys");
    for _ in 0..1000 {
        let g = gate.clone();
        ep.dispatch(
            "call-0".into(),
            "INVITE".into(),
            Box::new(move || Box::pin(async move { g.notified().await })),
        );
    }
    let depth = lock(&ep.inner.dq).get("call-0").map_or(0, |q| q.len());
    assert!(depth <= 32, "per-key queue grew to {depth}");
    gate.notify_waiters();
    ep.close().unwrap();
}
