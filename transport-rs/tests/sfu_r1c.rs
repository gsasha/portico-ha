//! Phase R round 1, group C (DEV-764): the session / call / webrtc review comments R1-6 to R1-107
//! that need a real offer/answer pair or a fault injected into a session. The comment id is in
//! the name or the comment of each test.

mod common;

use std::net::UdpSocket as StdUdp;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use common::peer::{Peer, PeerOpts};
use tokio::time::sleep;
use transport_rs::sfu::ifaces::IfacePolicy;
use transport_rs::sfu::session::{Fault, Session, SessionConfig, MAX_REMOTE_CANDIDATES};
use transport_rs::webrtc::SessionRegistry;

fn cfg() -> SessionConfig {
    SessionConfig::loopback()
}

fn fault(f: Fault) -> SessionConfig {
    SessionConfig { fault: f, ..cfg() }
}

async fn connected_pair(o: PeerOpts, c: SessionConfig) -> (Arc<Session>, Peer, String, String) {
    let peer = Peer::new(o).await;
    let session = Session::with_config("r1c", c).unwrap();
    let offer = peer.offer().await;
    let answer = session.handle_offer(&offer).await.expect("handle_offer");
    peer.accept_answer(&answer).await.expect("accept_answer");
    assert!(peer.wait_connected(10).await, "never connected");
    let end = Instant::now() + Duration::from_secs(5);
    while !session.connected() && Instant::now() < end {
        sleep(Duration::from_millis(20)).await;
    }
    assert!(session.connected());
    (session, peer, offer, answer)
}

/// 40 ms of 16 kHz L16 (what the device side hands the session).
fn frame() -> Vec<u8> {
    transport_rs::sfu::encode_l16_frame(&vec![3000i16; 640])
}

async fn feed(session: &Session, frames: usize) {
    for _ in 0..frames {
        let _ = session.audio_in().try_send(frame());
        sleep(Duration::from_millis(40)).await;
    }
}

/// The port of the session's host candidate in an answer.
fn answer_port(answer: &str) -> u16 {
    let l = answer
        .lines()
        .find(|l| l.starts_with("a=candidate:"))
        .expect("a candidate in the answer");
    l.split_whitespace().nth(5).unwrap().parse().unwrap()
}

fn first_mid(sdp: &str) -> String {
    sdp.lines()
        .find_map(|l| l.strip_prefix("a=mid:"))
        .unwrap()
        .trim()
        .to_string()
}

// ---- R1-6: the answer's msid is str0m's, not Go's ----------------------------------------------

// R1-6 (parity, recorded as PARITY 320): Go names the stream `portico-<callID>` and the track
// `audio`; str0m writes random ids. What must hold is that the answer carries ONE consistent
// stream/track pair (the `a=msid` line and the `a=ssrc .. msid` line agree), so a client that
// reads `event.streams[0]` gets a stable id.
#[tokio::test]
async fn the_answer_names_one_stream_and_track_consistently() {
    let peer = Peer::new(PeerOpts::default()).await;
    let session = Session::with_config("portico-call-1", cfg()).unwrap();
    let answer = session.handle_offer(&peer.offer().await).await.unwrap();
    let line = |p: &str| -> Vec<String> {
        answer
            .lines()
            .filter_map(|l| l.strip_prefix(p).map(|r| r.trim().to_string()))
            .collect()
    };
    let msid = line("a=msid:");
    assert_eq!(msid.len(), 1, "{answer}");
    let ssrc_msid: Vec<String> = answer
        .lines()
        .filter_map(|l| l.split_once(" msid:").map(|(_, r)| r.trim().to_string()))
        .collect();
    assert_eq!(ssrc_msid, msid, "{answer}");
    assert_eq!(msid[0].split(' ').count(), 2, "stream id and track id");
}

// ---- R1-18: the device audio goes to the FIRST audio section ----------------------------------

#[tokio::test]
async fn device_audio_goes_to_the_first_audio_section_of_a_multi_section_offer() {
    let (s, peer, offer, _) = connected_pair(
        PeerOpts {
            second_audio: true,
            ..PeerOpts::default()
        },
        cfg(),
    )
    .await;
    let first = first_mid(&offer);
    feed(&s, 15).await;
    assert!(s.stats().frames_written.load(Ordering::Relaxed) >= 10);
    assert_eq!(s.stats().write_errors.load(Ordering::Relaxed), 0);
    let rx = peer.received();
    assert!(!rx.is_empty(), "the phone heard nothing");
    for r in &rx {
        assert_eq!(
            r.mid.as_deref(),
            Some(first.as_str()),
            "audio went to another section"
        );
    }
}

// The audio + recvonly video variant of the same comment: str0m rejects the video section (the
// session has no video codec) and emits no MediaAdded for it, so the mid stays the audio one.
#[tokio::test]
async fn an_audio_plus_video_offer_still_sends_the_device_audio() {
    let (s, peer, offer, answer) = connected_pair(
        PeerOpts {
            video: true,
            ..PeerOpts::default()
        },
        cfg(),
    )
    .await;
    assert!(
        answer.contains("m=video 0 "),
        "the video section is rejected"
    );
    let first = first_mid(&offer);
    feed(&s, 15).await;
    assert!(s.stats().frames_written.load(Ordering::Relaxed) >= 10);
    assert_eq!(s.stats().write_errors.load(Ordering::Relaxed), 0);
    let rx = peer.received();
    assert!(!rx.is_empty(), "the phone heard nothing (R2-90b)");
    assert!(rx.iter().all(|r| r.mid.as_deref() == Some(first.as_str())));
}

// ---- R1-19: a panicking callback does not drop the ones after it -------------------------------

#[tokio::test]
async fn a_panicking_disconnected_callback_does_not_stop_the_ones_after_it() {
    let s = Session::with_config("r1-19", cfg()).unwrap();
    let ran = Arc::new(AtomicUsize::new(0));
    s.on_disconnected(|| panic!("the registry's callback panics"));
    let r = ran.clone();
    s.on_disconnected(move || {
        r.fetch_add(1, Ordering::SeqCst);
    });
    s.close(); // unwinds into this test without the fix
    assert_eq!(
        ran.load(Ordering::SeqCst),
        1,
        "the hang-up callback was dropped"
    );
    s.wait_done().await;
}

#[tokio::test]
async fn a_panicking_connected_callback_does_not_stop_the_ones_after_it() {
    let peer = Peer::new(PeerOpts::default()).await;
    let s = Session::with_config("r1-19c", cfg()).unwrap();
    let ran = Arc::new(AtomicUsize::new(0));
    s.on_connected(|| panic!("first connected callback panics"));
    let r = ran.clone();
    s.on_connected(move || {
        r.fetch_add(1, Ordering::SeqCst);
    });
    let answer = s.handle_offer(&peer.offer().await).await.unwrap();
    peer.accept_answer(&answer).await.unwrap();
    assert!(peer.wait_connected(10).await);
    let end = Instant::now() + Duration::from_secs(5);
    while ran.load(Ordering::SeqCst) == 0 && Instant::now() < end {
        sleep(Duration::from_millis(20)).await;
    }
    assert_eq!(ran.load(Ordering::SeqCst), 1);
    // the session survived the panicking callback
    assert!(!s.is_done());
    assert!(!s.stats().teardown.load(Ordering::SeqCst));
}

// ---- R1-20 / R1-127: an Rtc that never stops transmitting cannot pin the task -----------------

#[tokio::test]
async fn an_endless_transmit_source_cannot_pin_the_session_task() {
    let peer = Peer::new(PeerOpts::default()).await;
    let s = Session::with_config("r1-20", fault(Fault::EndlessTransmit)).unwrap();
    let _ = s.handle_offer(&peer.offer().await).await.unwrap();
    s.close();
    tokio::time::timeout(Duration::from_secs(5), s.wait_done())
        .await
        .expect("the session did not end");
    let served = s.stats().injected_outputs.load(Ordering::Relaxed);
    assert!(
        served < 5_000,
        "{served} outputs were taken after the stop (the budget is 1,000,000)"
    );
    // R1-127: none of them has a socket (the source is not ours): every one is counted
    assert_eq!(s.stats().udp_tx_dropped.load(Ordering::Relaxed), served);
    assert!(s.stats().teardown.load(Ordering::SeqCst));
}

// ---- R1-23: every str0m call is under the one guard --------------------------------------------

async fn panics_end_the_session_cleanly(f: Fault, offer_fails: bool) {
    let peer = Peer::new(PeerOpts::default()).await;
    let s = Session::with_config("r1-23", fault(f)).unwrap();
    let r = s.handle_offer(&peer.offer().await).await;
    if offer_fails {
        assert_eq!(r.unwrap_err(), "sfu: setting remote offer: internal error");
    } else {
        peer.accept_answer(&r.unwrap()).await.unwrap();
    }
    tokio::time::timeout(Duration::from_secs(10), s.wait_done())
        .await
        .expect("the session did not end");
    // the teardown (graceful close, the stats line, the `closed` state) ran: the panic was caught
    // by the session's own guard and not by the task supervisor
    assert!(
        s.stats().teardown.load(Ordering::SeqCst),
        "{f:?}: the teardown was skipped"
    );
}

#[tokio::test]
async fn a_panic_parsing_a_datagram_ends_the_session_through_the_same_teardown() {
    panics_end_the_session_cleanly(Fault::PanicInDatagram, false).await;
}

#[tokio::test]
async fn a_panic_in_negotiated_ends_the_session_through_the_same_teardown() {
    panics_end_the_session_cleanly(Fault::PanicInNegotiated, true).await;
}

#[tokio::test]
async fn a_panic_adding_host_candidates_ends_the_session_through_the_same_teardown() {
    panics_end_the_session_cleanly(Fault::PanicInBind, true).await;
}

// ---- R1-24: a callback registered after the end is not kept ------------------------------------

#[tokio::test]
async fn an_on_connected_callback_registered_after_the_end_is_dropped_at_once() {
    let s = Session::with_config("r1-24", cfg()).unwrap();
    s.close();
    s.wait_done().await;
    let held = Arc::new(());
    let h = held.clone();
    s.on_connected(move || drop(h)); // would never run, and would hold `h` for ever
    assert_eq!(Arc::strong_count(&held), 1, "the callback was kept");
    // and a callback registered while the session is alive and may still connect is kept
    let s2 = Session::with_config("r1-24b", cfg()).unwrap();
    let h = held.clone();
    s2.on_connected(move || drop(h));
    assert_eq!(Arc::strong_count(&held), 2);
    s2.close();
    assert_eq!(
        Arc::strong_count(&held),
        1,
        "close drops the pending callbacks"
    );
}

// ---- R1-39 / R1-82: remote candidates ----------------------------------------------------------

fn cand(i: usize) -> String {
    format!(
        "candidate:{i} 1 udp 2130706431 203.0.113.{} {} typ host",
        1 + i % 200,
        10000 + i
    )
}

#[tokio::test]
async fn remote_candidates_are_capped_per_session() {
    let (s, _peer, _, _) = connected_pair(PeerOpts::default(), cfg()).await;
    let mut errs = Vec::new();
    for i in 0..MAX_REMOTE_CANDIDATES + 8 {
        if let Err(e) = s.add_ice_candidate(&cand(i)).await {
            errs.push(e);
        }
    }
    assert_eq!(errs.len(), 8, "{errs:?}");
    assert!(
        errs[0].contains("too many remote candidates"),
        "{}",
        errs[0]
    );
    assert_eq!(
        s.stats().remote_candidates.load(Ordering::Relaxed),
        MAX_REMOTE_CANDIDATES as u64
    );
}

#[tokio::test]
async fn candidates_no_browser_has_are_ignored_not_probed() {
    // loopback is only allowed where the interface policy allows it (the test seam)
    let c = SessionConfig {
        ifaces: IfacePolicy {
            include_loopback: false,
            only: Some(vec!["127.0.0.1".parse().unwrap()]),
            ..IfacePolicy::default()
        },
        ..cfg()
    };
    let (s, _peer, _, _) = connected_pair(PeerOpts::default(), c).await;
    for bad in [
        "candidate:1 1 udp 2130706431 127.0.0.1 9 typ host",
        "candidate:2 1 udp 2130706431 0.0.0.0 9 typ host",
        "candidate:3 1 udp 2130706431 224.0.0.1 9 typ host",
        "candidate:4 1 udp 2130706431 203.0.113.9 0 typ host",
    ] {
        assert_eq!(s.add_ice_candidate(bad).await, Ok(()), "{bad}");
    }
    assert_eq!(s.stats().remote_candidates.load(Ordering::Relaxed), 0);
    // while a loopback session (the seam asks for it) takes a loopback candidate
    let (s2, _p2, _, _) = connected_pair(PeerOpts::default(), cfg()).await;
    s2.add_ice_candidate("candidate:1 1 udp 2130706431 127.0.0.1 9 typ host")
        .await
        .unwrap();
    assert_eq!(s2.stats().remote_candidates.load(Ordering::Relaxed), 1);
}

#[tokio::test]
async fn the_end_of_candidates_marker_is_the_whole_string_and_the_prefix_is_optional() {
    let (s, _peer, _, _) = connected_pair(PeerOpts::default(), cfg()).await;
    let n = || s.stats().remote_candidates.load(Ordering::Relaxed);
    for marker in [
        "",
        "end-of-candidates",
        "a=end-of-candidates",
        "  end-of-candidates ",
    ] {
        assert_eq!(s.add_ice_candidate(marker).await, Ok(()), "{marker:?}");
    }
    assert_eq!(n(), 0);
    // a candidate with the words in an extension is a candidate, not the marker (R1-82)
    let r = s
        .add_ice_candidate("candidate:7 1 udp 1 203.0.113.7 9000 typ host end-of-candidates")
        .await;
    assert!(r.is_err() || n() == 1, "taken for the marker: {r:?}");
    // with and without the prefix; `a=` in front of a candidate is refused, as pion does (R2-67:
    // the `=` is no ice-char of the foundation), and is not counted
    let r = s
        .add_ice_candidate("a=candidate:8 1 udp 1 203.0.113.8 9001 typ host")
        .await;
    assert_eq!(
        r,
        Err("sfu: adding ICE candidate: failed to parse foundation: invalid ice-char token: = in a=candidate:8 1 udp 1 203.0.113.8 9001 typ host".to_string())
    );
    let before = n();
    s.add_ice_candidate("9 1 udp 1 203.0.113.9 9002 typ host")
        .await
        .unwrap();
    assert_eq!(
        n(),
        before + 1,
        "a candidate without the `candidate:` prefix"
    );
}

// ---- R1-104: a re-offer no codec can answer leaves the live Rtc alone --------------------------

fn opus_only(sdp: &str) -> String {
    let mut out = String::new();
    for l in sdp.split_inclusive('\n') {
        let t = l.trim_end();
        if t.starts_with("a=rtpmap:") || t.starts_with("a=fmtp:") || t.starts_with("a=rtcp-fb:") {
            continue;
        }
        if t.starts_with("m=audio ") {
            out.push_str("m=audio 9 UDP/TLS/RTP/SAVPF 111\r\n");
            continue;
        }
        out.push_str(l);
    }
    out.push_str("a=rtpmap:111 opus/48000/2\r\n");
    out
}

#[tokio::test]
async fn an_unanswerable_reoffer_leaves_the_live_call_alone() {
    let (s, peer, _, _) = connected_pair(PeerOpts::default(), cfg()).await;
    feed(&s, 5).await;
    let before = peer.received().len();
    assert!(before > 0);
    let bad = opus_only(&peer.reoffer().await);
    let e = s.handle_offer(&bad).await.unwrap_err();
    assert_eq!(
        e,
        "sfu: creating answer: unable to populate media section, RTPSender created with no codecs"
    );
    // the call is as it was: the device audio still reaches the phone, over the same mid
    feed(&s, 10).await;
    assert!(
        peer.received().len() >= before + 8,
        "the live call stopped after the refused re-offer"
    );
    assert_eq!(s.stats().write_errors.load(Ordering::Relaxed), 0);
}

// ---- R1-107: no session is created once the transport is stopping ------------------------------

#[tokio::test]
async fn no_session_is_created_after_the_shutdown_started() {
    let reg = SessionRegistry::new(cfg());
    let a = reg.get_or_create("a").unwrap();
    reg.close_all_wait(Duration::from_secs(2)).await;
    assert!(a.is_done());
    let e = reg.get_or_create("b").err().expect("a session was created");
    assert!(e.contains("stopping"), "{e}");
    assert_eq!(reg.len(), 0);
    // the table is empty after the shutdown: the closed session is not answered from it (R2-90a)
    assert!(reg.get("a").is_none());
}

// ---- R1-165: wait_done means the sockets are released ------------------------------------------

#[tokio::test]
async fn wait_done_means_the_udp_port_is_free_again() {
    for _ in 0..5 {
        let (s, _peer, _, answer) = connected_pair(PeerOpts::default(), cfg()).await;
        let port = answer_port(&answer);
        s.close();
        s.wait_done().await;
        StdUdp::bind(("127.0.0.1", port)).expect("the session's port is still held");
    }
}
