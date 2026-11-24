//! The lifecycle and robustness of the str0m session: hostile offers, panics, the end of a call
//! (the peer closing, a browser that vanishes, one that never connects), two sessions at once, an
//! ICE restart and trickled candidates. These have no Go test of the same name (Go has no
//! terminal rule for `Disconnected`, no size cap, no panic seam); PARITY.md lists them as the
//! tests the port adds.

mod common;

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use common::peer::{Peer, PeerOpts};
use tokio::time::sleep;
use transport_rs::sfu::sdp::{MAX_OFFER_BYTES, MAX_OFFER_LINES};
use transport_rs::sfu::session::{Fault, Session, SessionConfig};
use transport_rs::sfu::{decode_l16_frame, encode_l16_frame};

fn cfg() -> SessionConfig {
    SessionConfig::loopback()
}

async fn connected_pair(o: PeerOpts, c: SessionConfig) -> (Arc<Session>, Peer) {
    let peer = Peer::new(o).await;
    let session = Session::with_config("life", c).unwrap();
    let offer = peer.offer().await;
    let answer = session.handle_offer(&offer).await.expect("handle_offer");
    peer.accept_answer(&answer).await.expect("accept_answer");
    assert!(peer.wait_connected(10).await, "never connected");
    let end = Instant::now() + Duration::from_secs(5);
    while !session.connected() && Instant::now() < end {
        sleep(Duration::from_millis(20)).await;
    }
    assert!(session.connected());
    (session, peer)
}

fn counter(s: &Session) -> Arc<AtomicUsize> {
    let n = Arc::new(AtomicUsize::new(0));
    let n2 = n.clone();
    s.on_disconnected(move || {
        n2.fetch_add(1, Ordering::SeqCst);
    });
    n
}

async fn real_offer() -> String {
    Peer::new(PeerOpts {
        opus: true,
        g722: true,
        pcmu: true,
        ..PeerOpts::default()
    })
    .await
    .offer()
    .await
}

// ---- hostile offers: errors, never a dead process ----------------------------------------------

#[tokio::test]
async fn hostile_offers_get_errors_and_the_session_stays_usable() {
    let good = real_offer().await;
    let s = Session::with_config("hostile", cfg()).unwrap();
    // the size cap: bytes and lines
    let big = format!(
        "{good}{}",
        "a=x-pad:".to_string() + &"y".repeat(MAX_OFFER_BYTES) + "\r\n"
    );
    let e = s.handle_offer(&big).await.unwrap_err();
    assert!(
        e.starts_with("sfu: setting remote offer: offer too large"),
        "{e}"
    );
    let many = format!("{good}{}", "a=x-pad:1\r\n".repeat(MAX_OFFER_LINES + 1));
    let e = s.handle_offer(&many).await.unwrap_err();
    assert!(
        e.starts_with("sfu: setting remote offer: offer too large"),
        "{e}"
    );
    // text that is no SDP: pion's own first-line errors
    assert_eq!(
        s.handle_offer("garbage").await.unwrap_err(),
        "sfu: setting remote offer: failed to unmarshal SDP: sdp: syntax error at pos 1: \"a\""
    );
    assert_eq!(
        s.handle_offer("x=1\r\n").await.unwrap_err(),
        "sfu: setting remote offer: failed to unmarshal SDP: sdp: syntax error at pos 1: \"=\""
    );
    // cut in half, binary, NULs, no fingerprint, a format no number, a candidate that is no
    // address: each on a session of its own (str0m ends the session of an offer it refused after
    // binding), each with the exact outcome (R1-129)
    const PARSE: &str =
        "sfu: setting remote offer: SDP parse: Parse error at an offset in the text";
    for (bad, want) in [
        (good[..good.len() / 2].to_string(), Err(PARSE)),
        ("v=0\r\n\0\0\0\u{1}\u{2}".to_string(), Err(PARSE)),
        (
            "v=0\r\no=- 1 2 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 0\r\n"
                .to_string(),
            Err(PARSE),
        ),
        (
            good.replace("a=fingerprint:sha-256 ", "a=fingerprint:sha-256 zz"),
            Err("sfu: setting remote offer: remote sdp: missing a=fingerprint"),
        ),
        (
            good.replace("SAVPF 111 9 0", "SAVPF 111 9 0 99999"),
            Err("sfu: setting remote offer: strconv.ParseUint: parsing \"99999\": value out of range"),
        ),
        // an unparsable address in a candidate line the offer carries is tolerated by str0m
        (
            format!("{good}a=candidate:1 1 udp 1 999.999.0.1 99999 typ host\r\n"),
            Ok(()),
        ),
    ] {
        let s = Session::with_config("hostile-each", cfg()).unwrap();
        let r = tokio::time::timeout(Duration::from_secs(5), s.handle_offer(&bad))
            .await
            .expect("an offer hung the session");
        assert_eq!(r.as_ref().map(|_| ()).map_err(String::as_str), want, "{bad:?}");
    }
    // Opus only: Go's text
    let opus = Peer::new(PeerOpts {
        opus: true,
        pcmu: false,
        ..PeerOpts::default()
    })
    .await
    .offer()
    .await;
    let s2 = Session::with_config("opus-only", cfg()).unwrap();
    assert_eq!(
        s2.handle_offer(&opus).await.unwrap_err(),
        "sfu: creating answer: unable to populate media section, RTPSender created with no codecs"
    );
    s.close();
    s2.close();
    // and a fresh session is answered as ever
    let s3 = Session::with_config("after", cfg()).unwrap();
    assert!(s3.handle_offer(&good).await.is_ok());
    s3.close();
}

// ---- panics end that session only -------------------------------------------------------------

#[tokio::test]
async fn a_panic_in_accept_offer_is_an_error_and_ends_that_session_only() {
    let good = real_offer().await;
    let healthy = Session::with_config("healthy", cfg()).unwrap();
    let c = SessionConfig {
        fault: Fault::PanicInAcceptOffer,
        ..cfg()
    };
    let s = Session::with_config("panics", c).unwrap();
    let n = counter(&s);
    let e = s.handle_offer(&good).await.unwrap_err();
    assert_eq!(e, "sfu: setting remote offer: internal error");
    tokio::time::timeout(Duration::from_secs(3), s.wait_done())
        .await
        .expect("the session did not end");
    assert_eq!(n.load(Ordering::SeqCst), 1);
    assert!(
        healthy.handle_offer(&good).await.is_ok(),
        "another session was affected"
    );
    healthy.close();
}

#[tokio::test]
async fn a_panic_inside_a_str0m_poll_ends_that_session_only() {
    let good = real_offer().await;
    let c = SessionConfig {
        fault: Fault::PanicInPoll,
        ..cfg()
    };
    let s = Session::with_config("poll-panic", c).unwrap();
    let n = counter(&s);
    let _ = s.handle_offer(&good).await;
    tokio::time::timeout(Duration::from_secs(3), s.wait_done())
        .await
        .expect("the session did not end");
    assert_eq!(n.load(Ordering::SeqCst), 1);
    let other = Session::with_config("other", cfg()).unwrap();
    assert!(other.handle_offer(&good).await.is_ok());
    other.close();
}

#[tokio::test]
async fn a_panic_in_the_session_task_is_caught_by_the_supervisor() {
    let good = real_offer().await;
    let c = SessionConfig {
        fault: Fault::PanicInTask,
        ..cfg()
    };
    let s = Session::with_config("task-panic", c).unwrap();
    let n = counter(&s);
    let _ = s.handle_offer(&good).await;
    tokio::time::timeout(Duration::from_secs(3), s.wait_done())
        .await
        .expect("the supervisor did not finish the session");
    assert_eq!(
        n.load(Ordering::SeqCst),
        1,
        "OnDisconnected must fire once for a panicked session"
    );
    let other = Session::with_config("other", cfg()).unwrap();
    assert!(other.handle_offer(&good).await.is_ok());
    other.close();
}

#[test]
fn a_session_cannot_start_outside_a_runtime_and_says_so_instead_of_panicking() {
    let e = Session::new("no-runtime").err().expect("an error");
    assert!(e.starts_with("sfu: creating peer connection: "), "{e}");
}

// ---- the end of a call -------------------------------------------------------------------------

#[tokio::test]
async fn the_peer_closing_gracefully_ends_the_session_at_once() {
    let (s, peer) = connected_pair(PeerOpts::default(), cfg()).await;
    let n = counter(&s);
    let t0 = Instant::now();
    peer.close();
    tokio::time::timeout(Duration::from_secs(5), s.wait_done())
        .await
        .expect("a closed peer did not end the session");
    assert_eq!(n.load(Ordering::SeqCst), 1);
    eprintln!(
        "graceful close of the peer ended the session after {:?}",
        t0.elapsed()
    );
}

#[tokio::test]
async fn closing_the_session_tells_the_peer() {
    let (s, peer) = connected_pair(PeerOpts::default(), cfg()).await;
    s.close();
    tokio::time::timeout(Duration::from_secs(3), s.wait_done())
        .await
        .unwrap();
    let end = Instant::now() + Duration::from_secs(3);
    while Instant::now() < end && !peer.task.is_finished() {
        sleep(Duration::from_millis(50)).await;
    }
    assert!(
        peer.task.is_finished(),
        "the peer never saw the DTLS close_notify / RTCP BYE"
    );
}

/// A vanished browser: the terminal rule for `Disconnected`. `max_stun_retransmits` 3 keeps the
/// test short (S1b: 10.6 to 13.4 s to Disconnected); the numbers of the defaults are in
/// INTEROP.md.
#[tokio::test]
async fn a_vanished_peer_ends_after_disconnected_plus_the_grace() {
    let c = SessionConfig {
        max_stun_retransmits: Some(3),
        disconnect_grace: Duration::from_millis(1500),
        ..cfg()
    };
    let (s, peer) = connected_pair(PeerOpts::default(), c).await;
    let n = counter(&s);
    // keep the downlink busy, as a live call does
    let tx = s.audio_in();
    let feeder = tokio::spawn(async move {
        let mut tk = tokio::time::interval(Duration::from_millis(40));
        loop {
            tk.tick().await;
            if tx
                .send(encode_l16_frame(&vec![1000i16; 640]))
                .await
                .is_err()
            {
                return;
            }
        }
    });
    sleep(Duration::from_millis(500)).await;
    let t0 = Instant::now();
    peer.kill();
    // when ICE goes Disconnected (`connected()` turns false), and when the session is over
    let disc_end = t0 + Duration::from_secs(40);
    while s.connected() && Instant::now() < disc_end {
        sleep(Duration::from_millis(10)).await;
    }
    let t_disc = t0.elapsed();
    tokio::time::timeout(Duration::from_secs(40), s.wait_done())
        .await
        .expect("a vanished peer never ended the session");
    let took = t0.elapsed();
    feeder.abort();
    assert_eq!(n.load(Ordering::SeqCst), 1);
    eprintln!(
        "vanished peer (max_stun_retransmits 3, grace 1.5 s): disconnected after {t_disc:?}, over after {took:?}"
    );
    assert!(took < Duration::from_secs(30), "{took:?}");
    // R2-90c: the LENGTH of the grace is pinned to the configured 1.5 s (a grace of zero ends the
    // session at the moment it turns disconnected; the poll above has a 10 ms step)
    let grace = took - t_disc;
    assert!(
        grace >= Duration::from_millis(1400) && grace < Duration::from_millis(4000),
        "the grace lasted {grace:?}, configured 1.5 s"
    );
}

/// PARITY 321 (R1-78): `Connected()` follows the ICE state both ways, as pion's
/// `ConnectionState() == Connected` does: false while the peer is out of reach, true again when it
/// answers, and the session is still alive (a disconnect never re-arms the connect timeout).
#[tokio::test]
async fn connected_is_false_while_disconnected_and_true_again_on_return() {
    let c = SessionConfig {
        max_stun_retransmits: Some(3),
        disconnect_grace: Duration::from_secs(60),
        ..cfg()
    };
    let (s, peer) = connected_pair(PeerOpts::default(), c).await;
    let n = counter(&s);
    assert!(s.connected());
    peer.stall(true);
    let end = Instant::now() + Duration::from_secs(40);
    while s.connected() && Instant::now() < end {
        sleep(Duration::from_millis(100)).await;
    }
    assert!(
        !s.connected(),
        "connected() stayed true with a peer that went silent"
    );
    assert_eq!(
        n.load(Ordering::SeqCst),
        0,
        "the session ended inside the grace"
    );
    peer.stall(false);
    let end = Instant::now() + Duration::from_secs(40);
    while !s.connected() && Instant::now() < end {
        sleep(Duration::from_millis(100)).await;
    }
    assert!(
        s.connected(),
        "connected() stayed false after the peer answered again"
    );
    assert_eq!(n.load(Ordering::SeqCst), 0);
    s.close();
}

#[tokio::test]
async fn a_browser_that_never_connects_ends_after_the_connect_timeout() {
    let c = SessionConfig {
        connect_timeout: Duration::from_millis(1500),
        ..cfg()
    };
    let peer = Peer::new(PeerOpts::default()).await;
    let s = Session::with_config("never", c).unwrap();
    let n = counter(&s);
    let _ = s.handle_offer(&peer.offer().await).await.unwrap();
    let t0 = Instant::now();
    // the answer is never applied: the peer never starts its checks
    tokio::time::timeout(Duration::from_secs(5), s.wait_done())
        .await
        .expect("the connect timeout did not end the session");
    assert_eq!(n.load(Ordering::SeqCst), 1);
    // R2-90d: not before the configured 1.5 s (a timeout of zero ends it at once)
    let took = t0.elapsed();
    assert!(
        took >= Duration::from_millis(1400),
        "ended after {took:?}, configured 1.5 s"
    );
}

// ---- two calls at once, ICE restart, trickle ---------------------------------------------------

#[tokio::test]
async fn two_sessions_at_once_do_not_hear_each_other() {
    let (a, pa) = connected_pair(PeerOpts::default(), cfg()).await;
    let (b, pb) = connected_pair(PeerOpts::default(), cfg()).await;
    pa.tone(false, 500.0, 20, 8000.0, 2.0);
    pb.tone(false, 1500.0, 20, 8000.0, 2.0);
    let (oa, ob) = (a.audio_out(), b.audio_out());
    let (mut ga, mut gb): (Vec<i16>, Vec<i16>) = (Vec::new(), Vec::new());
    let start = Instant::now();
    while start.elapsed() < Duration::from_millis(2400) {
        if let Ok(Some(f)) = tokio::time::timeout(Duration::from_millis(20), async {
            oa.lock().await.recv().await
        })
        .await
        {
            ga.extend(decode_l16_frame(&f));
        }
        if let Ok(Some(f)) = tokio::time::timeout(Duration::from_millis(20), async {
            ob.lock().await.recv().await
        })
        .await
        {
            gb.extend(decode_l16_frame(&f));
        }
    }
    let freq = |s: &[i16]| {
        let s = &s[6000..s.len() - 4000];
        let (mut first, mut last, mut n) = (None, 0usize, 0u32);
        for i in 1..s.len() {
            if s[i - 1] < 0 && s[i] >= 0 {
                first.get_or_insert(i);
                last = i;
                n += 1;
            }
        }
        f64::from(n - 1) * 16000.0 / (last - first.unwrap()) as f64
    };
    assert!(
        (freq(&ga) - 500.0).abs() < 15.0,
        "session A heard {:.0} Hz",
        freq(&ga)
    );
    assert!(
        (freq(&gb) - 1500.0).abs() < 40.0,
        "session B heard {:.0} Hz",
        freq(&gb)
    );
    // one ends: the other carries on
    a.close();
    a.wait_done().await;
    assert!(b.connected());
    b.close();
}

#[tokio::test]
async fn an_ice_restart_keeps_the_call_up_without_a_gap() {
    let (s, peer) = connected_pair(PeerOpts::default(), cfg()).await;
    let n = counter(&s);
    let tx = s.audio_in();
    let feeder = tokio::spawn(async move {
        let mut tk = tokio::time::interval(Duration::from_millis(40));
        loop {
            tk.tick().await;
            if tx
                .send(encode_l16_frame(&vec![2000i16; 640]))
                .await
                .is_err()
            {
                return;
            }
        }
    });
    sleep(Duration::from_secs(1)).await;
    let offer2 = peer.reoffer().await;
    assert!(!offer2.is_empty(), "the peer made no restart offer");
    let answer2 = s.handle_offer(&offer2).await.expect("the restart offer");
    peer.accept_answer(&answer2).await.unwrap();
    sleep(Duration::from_secs(2)).await;
    feeder.abort();
    let rx = peer.received();
    let max_gap = rx
        .windows(2)
        .map(|w| w[1].at.duration_since(w[0].at))
        .max()
        .unwrap();
    eprintln!(
        "ICE restart: {} packets, max inter-arrival gap {:?}",
        rx.len(),
        max_gap
    );
    assert!(max_gap < Duration::from_millis(400), "gap {max_gap:?}");
    assert!(s.connected() && n.load(Ordering::SeqCst) == 0);
    s.close();
}

fn strip_candidates(text: String) -> String {
    text.split_inclusive('\n')
        .filter(|l| !l.starts_with("a=candidate:") && !l.starts_with("a=end-of-candidates"))
        .collect()
}

#[tokio::test]
async fn candidates_trickled_after_the_answer_connect_the_pair() {
    let o = PeerOpts {
        rewrite: Some(strip_candidates),
        ..PeerOpts::default()
    };
    let peer = Peer::new(o).await;
    let s = Session::with_config("trickle", cfg()).unwrap();
    let offer = peer.offer().await;
    assert!(!offer.contains("a=candidate:"));
    let answer = s.handle_offer(&offer).await.unwrap();
    assert!(
        answer.contains("a=candidate:") && answer.contains("a=end-of-candidates"),
        "{answer}"
    );
    // the candidate arrives after the answer was produced, before the peer even has it
    s.add_ice_candidate(&format!(
        "candidate:1 1 udp 2130706431 127.0.0.1 {} typ host",
        peer.addr.port()
    ))
    .await
    .unwrap();
    s.add_ice_candidate("").await.unwrap(); // end of candidates
    peer.accept_answer(&answer).await.unwrap();
    assert!(peer.wait_connected(10).await);
    s.close();
}

// ---- the new panic sites: every one ends THAT session, fires OnDisconnected once ---------------

#[tokio::test]
async fn a_panic_in_the_candidate_path_ends_that_session_only() {
    let c = SessionConfig {
        fault: Fault::PanicInCandidate,
        ..cfg()
    };
    let peer = Peer::new(PeerOpts::default()).await;
    let s = Session::with_config("cand-panic", c).unwrap();
    let n = counter(&s);
    s.handle_offer(&peer.offer().await).await.unwrap();
    let e = s
        .add_ice_candidate("candidate:1 1 udp 2130706431 127.0.0.1 5000 typ host")
        .await
        .unwrap_err();
    assert_eq!(e, "sfu: adding ICE candidate: internal error");
    tokio::time::timeout(Duration::from_secs(3), s.wait_done())
        .await
        .expect("did not end");
    assert_eq!(n.load(Ordering::SeqCst), 1);
}

#[tokio::test]
async fn a_panic_in_the_answer_post_processing_ends_that_session_only() {
    let c = SessionConfig {
        fault: Fault::PanicInAnswerPost,
        ..cfg()
    };
    let s = Session::with_config("post-panic", c).unwrap();
    let n = counter(&s);
    let e = s.handle_offer(&real_offer().await).await.unwrap_err();
    assert_eq!(e, "sfu: setting remote offer: internal error");
    tokio::time::timeout(Duration::from_secs(3), s.wait_done())
        .await
        .expect("did not end");
    assert_eq!(n.load(Ordering::SeqCst), 1);
    assert!(!s.had_offer());
}

#[tokio::test]
async fn a_panic_while_handling_rtp_ends_that_session_only() {
    let c = SessionConfig {
        fault: Fault::PanicInRtp,
        ..cfg()
    };
    let (s, peer) = connected_pair(PeerOpts::default(), c).await;
    let n = counter(&s);
    peer.send(false, 0, vec![0xFF; 160]);
    tokio::time::timeout(Duration::from_secs(5), s.wait_done())
        .await
        .expect("did not end");
    assert_eq!(n.load(Ordering::SeqCst), 1);
    let other = Session::with_config("other", cfg()).unwrap();
    assert!(other.handle_offer(&real_offer().await).await.is_ok());
    other.close();
}

// ---- a persistent UDP receive error backs off and ends the session -----------------------------

#[tokio::test]
async fn a_persistent_receive_error_backs_off_and_ends_the_session() {
    let c = SessionConfig {
        fault: Fault::RecvError,
        recv_backoff_max: Duration::from_millis(2),
        ..cfg()
    };
    let s = Session::with_config("recv-err", c).unwrap();
    let n = counter(&s);
    let t0 = Instant::now();
    // the offer binds the sockets, whose readers fail at once
    let _ = s.handle_offer(&real_offer().await).await;
    tokio::time::timeout(Duration::from_secs(10), s.wait_done())
        .await
        .expect("a session whose sockets only fail never ended");
    let took = t0.elapsed();
    assert_eq!(n.load(Ordering::SeqCst), 1);
    // 50 failures: 10+20+40+... capped at 2 ms is at least 100 ms of sleeping, so no hot loop
    assert!(took > Duration::from_millis(80), "{took:?}: no backoff");
}
