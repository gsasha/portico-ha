//! Ports of `parity_test.go` and the server halves of `misc_test.go`: the real SIP server
//! driven over UDP loopback by a fake station written in Rust (E2, design §4.2a, DEV-518). The Go
//! test name is in a comment on each test.

mod common;

use std::net::SocketAddr;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use common::*;
use serde_json::json;
use transport_rs::cancel::CancelToken;
use transport_rs::driver::gatesipserver::media::alaw_encode;
use transport_rs::driver::gatesipserver::{GateSipServer, ServerError};
use transport_rs::driver::{Driver, MediaTarget};

// TestRegisterUnknownNonceIsRechallenged: GO-16, an Authorization carrying a nonce this server
// never issued is re-challenged, not accepted.
#[tokio::test]
async fn register_unknown_nonce_is_rechallenged() {
    let h = Harness::new().await;
    h.st.register("").await;
    let m401 = find(&h.st.collect(ms(300)).await, "401").expect("no 401 challenge");
    let nonce = quoted(&m401.header("WWW-Authenticate"), "nonce");
    let stale = digest_auth(&m401, "8001", "s3cretpass").replace(&nonce, "forgednonce");
    h.st.register(&stale).await;
    let r = h.st.collect(ms(300)).await;
    let m2 = find(&r, "401")
        .unwrap_or_else(|| panic!("unknown nonce was not re-challenged: {:?}", codes(&r)));
    let sn = h.s.snapshot();
    assert!(
        !sn.registered && sn.digest_ok == 0,
        "registered on an unknown nonce: {sn:?}"
    );
    h.st.register(&digest_auth(&m2, "8001", "s3cretpass")).await;
    assert!(
        find(&h.st.collect(ms(300)).await, "200").is_some(),
        "answering the fresh challenge was not accepted"
    );
}

// R1-5 (server half, PARITY 349): the REGISTER's Authorization is verified over the bytes the
// station sent, as Go does. A username that is Latin-1 (0xE9) hashes as that byte; read as
// lossy text it hashed as U+FFFD and a correct station was refused with 403.
#[tokio::test]
async fn a_latin_1_username_in_a_register_digest_verifies_over_its_raw_bytes() {
    let h = Harness::new().await;
    h.st.register("").await;
    let m401 = find(&h.st.collect(ms(300)).await, "401").expect("no 401 challenge");
    let hdr = m401.header("WWW-Authenticate");
    let (realm, nonce) = (quoted(&hdr, "realm"), quoted(&hdr, "nonce"));
    let mut ha1_in = b"u\xe9:".to_vec();
    ha1_in.extend_from_slice(format!("{realm}:s3cretpass").as_bytes());
    let ha1 = md5hex_bytes(&ha1_in);
    let ha2 = md5hex("REGISTER:sip:127.0.0.1");
    let resp = md5hex(&format!("{ha1}:{nonce}:{ha2}"));
    let auth = format!(
        "Authorization: Digest username=\"u@@\", realm=\"{realm}\", nonce=\"{nonce}\", uri=\"sip:127.0.0.1\", response=\"{resp}\", algorithm=MD5\r\n"
    );
    let text = h.st.register_text("").replace(
        "Content-Length: 0\r\n\r\n",
        &format!("{auth}Content-Length: 0\r\n\r\n"),
    );
    let at = text.find("@@").unwrap();
    let mut wire = text.as_bytes()[..at].to_vec();
    wire.push(0xe9);
    wire.extend_from_slice(&text.as_bytes()[at + 2..]);
    h.st.send_bytes(&wire).await;
    let r = h.st.collect(ms(300)).await;
    assert!(
        find(&r, "200").is_some(),
        "a digest over Latin-1 bytes was not accepted: {:?}",
        codes(&r)
    );
    let sn = h.s.snapshot();
    assert!(
        sn.registered && sn.digest_ok == 1 && sn.digest_mismatch == 0,
        "{sn:?}"
    );
}

// TestParity_RegisterDigest (P1-3)
#[tokio::test]
async fn parity_register_digest() {
    let h = Harness::new().await;
    h.st.register("").await;
    let r1 = h.st.collect(ms(300)).await;
    let m401 = find(&r1, "401").unwrap_or_else(|| panic!("no 401 challenge: {:?}", codes(&r1)));
    let w = m401.header("WWW-Authenticate");
    assert!(
        w.contains("realm=\"VDP\"") && w.contains("nonce="),
        "challenge is not a VDP digest challenge: {w:?}"
    );
    assert!(
        !h.s.snapshot().registered,
        "registered before any Authorization arrived"
    );

    h.st.register(&digest_auth(&m401, "8001", "s3cretpass"))
        .await;
    let r2 = h.st.collect(ms(300)).await;
    let m200 =
        find(&r2, "200").unwrap_or_else(|| panic!("good digest not accepted: {:?}", codes(&r2)));
    assert_eq!(
        m200.header("Expires"),
        "300",
        "the station re-registers on its own every ~5 min"
    );
    let sn = h.s.snapshot();
    assert!(
        sn.registered && sn.digest_ok == 1 && sn.digest_mismatch == 0,
        "after a good REGISTER: {sn:?}"
    );

    // A WRONG password is REFUSED (403) and does not set Registered: Alice's decision
    // 2026-10-21 (DEV-518), a deliberate difference from gate-sip, which answers 200.
    let h2 = Harness::new().await;
    h2.st.register("").await;
    let m401b = find(&h2.st.collect(ms(300)).await, "401").unwrap();
    h2.st
        .register(&digest_auth(&m401b, "8001", "wrongpassw"))
        .await;
    let r3 = h2.st.collect(ms(300)).await;
    assert!(
        has(&r3, "403") && !has(&r3, "200"),
        "bad digest not refused with 403: {:?}",
        codes(&r3)
    );
    let sn = h2.s.snapshot();
    assert!(
        sn.digest_mismatch == 1 && !sn.registered && sn.digest_ok == 0,
        "after a bad REGISTER: {sn:?}"
    );
}

// TestParity_InviteRings (P5)
#[tokio::test]
async fn parity_invite_rings() {
    let h = Harness::new().await;
    h.st.invite("call-AAA", 1, &h.st.sdp("narrow")).await;
    let r = h.st.collect(ms(300)).await;
    assert!(
        has(&r, "100") && has(&r, "180"),
        "want 100 and 180, got {:?}",
        codes(&r)
    );
    assert!(!has(&r, "200"), "answered on its own");
    let sn = h.s.snapshot();
    assert!(
        sn.ringing && !sn.in_call && sn.calls == 1,
        "snapshot after INVITE: {sn:?}"
    );
    h.sink
        .wait_events(&[&format!("ringing:call-AAA@{}", h.device_id())])
        .await;
}

// TestParity_SecondCallerBusy (P6)
#[tokio::test]
async fn parity_second_caller_busy() {
    let h = Harness::with(Opts::ring(5000)).await;
    h.st.invite("call-AAA", 1, &h.st.sdp("narrow")).await;
    h.st.collect(ms(200)).await;
    h.st.invite("call-BBB", 1, &h.st.sdp("narrow")).await;
    let r = h.st.collect(ms(300)).await;
    assert!(
        has(&r, "486") && !has(&r, "180"),
        "second caller: {:?}, want 486 and no 180",
        codes(&r)
    );
    assert_eq!(
        h.s.snapshot().calls,
        1,
        "a refused caller was counted as a call"
    );
    // The live dialog is untouched: it is still call-AAA that rings.
    h.sink
        .wait_events(&[&format!("ringing:call-AAA@{}", h.device_id())])
        .await;
}

// TestParity_RetransmittedInvite (P7)
#[tokio::test]
async fn parity_retransmitted_invite() {
    let h = Harness::with(Opts::ring(5000)).await;
    h.st.invite("call-AAA", 1, &h.st.sdp("narrow")).await;
    h.st.collect(ms(200)).await;
    h.st.invite("call-AAA", 1, &h.st.sdp("narrow")).await;
    let r = h.st.collect(ms(300)).await;
    assert!(
        !has(&r, "486") && has(&r, "180"),
        "retransmission while ringing: {:?}, want 180 and no 486",
        codes(&r)
    );
    assert_eq!(
        h.s.snapshot().calls,
        1,
        "a retransmission started a second call (and a second ring timer)"
    );

    let _cl = h.s.attach().unwrap();
    h.st.collect(ms(200)).await; // the 200 OK
    h.st.invite("call-AAA", 1, &h.st.sdp("narrow")).await;
    let r = h.st.collect(ms(300)).await;
    let m200 = find(&r, "200");
    assert!(
        m200.as_ref().is_some_and(|m| m.body().contains("m=audio")) && !has(&r, "486"),
        "retransmission once answered: {:?}, want 100 + 200 carrying SDP",
        codes(&r)
    );
    h.sink
        .wait_events(&[
            &format!("ringing:call-AAA@{}", h.device_id()),
            "answered:call-AAA",
        ])
        .await;
}

// TestParity_RingLimit480 (P8)
#[tokio::test]
async fn parity_ring_limit_480() {
    let h = Harness::with(Opts::ring(500)).await;
    h.st.invite("call-DDD", 1, &h.st.sdp("narrow")).await;
    // R2-23: a LOWER bound. The Go test (and the old port) collected for 1500 ms and then checked
    // "at least 500 ms have passed", which is true whenever the collection ended: a ring timer
    // armed at 3/4 of the limit passed. The 480 must NOT be in the first 430 ms of a 500 ms limit
    // and must come in the next 600 ms.
    let early = h.st.collect(ms(430)).await;
    assert!(
        has(&early, "180") && !has(&early, "480"),
        "want 180 and no 480 before the ring limit, got {:?}",
        codes(&early)
    );
    let r = h.st.collect(ms(600)).await;
    assert!(
        has(&r, "480"),
        "want the 480 at the limit, got {:?}",
        codes(&r)
    );
    let m480 = find(&r, "480").unwrap();
    assert_eq!(m480.header("Call-ID"), "call-DDD");
    h.sink
        .wait_events(&[
            &format!("ringing:call-DDD@{}", h.device_id()),
            "ended:call-DDD",
        ])
        .await;
    h.st.invite("call-CCC", 1, &h.st.sdp("narrow")).await;
    let r = h.st.collect(ms(200)).await;
    assert!(
        has(&r, "180") && !has(&r, "486"),
        "after the timeout the slot must be free, got {:?}",
        codes(&r)
    );
}

// TestParity_AttachAnswers (P9)
#[tokio::test]
async fn parity_attach_answers() {
    let h = Harness::with(Opts::ring(300)).await;
    h.st.invite("call-EEE", 1, &h.st.sdp("narrow")).await;
    h.st.collect(ms(100)).await;
    let _cl = h.s.attach().unwrap();
    let r = h.st.collect(ms(700)).await; // well past the ring limit
    assert!(has(&r, "200"), "attach did not answer: {:?}", codes(&r));
    assert!(
        !has(&r, "480"),
        "the ring timer fired on an answered call: {:?}",
        codes(&r)
    );
    let sn = h.s.snapshot();
    assert!(
        sn.in_call && sn.app_attached && sn.answered == 1,
        "snapshot: {sn:?}"
    );
}

// TestParity_OrphanGraceThenBye (P10)
#[tokio::test]
async fn parity_orphan_grace_then_bye() {
    let h = Harness::with(Opts::orphan(600)).await;
    h.st.invite("call-EEE", 1, &h.st.sdp("narrow")).await;
    h.st.collect(ms(100)).await;
    let cl = h.s.attach().unwrap();
    h.st.collect(ms(100)).await;
    cl.detach();
    // R2-23: a lower bound that can fail: no BYE in the first 500 ms of a 600 ms grace (a grace
    // armed at 3/4 would BYE at 450 ms), then the BYE in the next 700 ms.
    let early = h.st.collect(ms(500)).await;
    assert!(
        !has(&early, "BYE"),
        "BYE before the grace: {:?}",
        codes(&early)
    );
    let r = h.st.collect(ms(700)).await;
    assert!(has(&r, "BYE"), "no BYE after the grace: {:?}", codes(&r));
    h.sink
        .wait_events(&[
            &format!("ringing:call-EEE@{}", h.device_id()),
            "answered:call-EEE",
            "ended:call-EEE",
        ])
        .await;

    // ...but a client that comes BACK inside the grace keeps the call (the Wi-Fi-blip recovery),
    // and attaching again answers nothing a second time.
    h.st.invite("call-FFF", 1, &h.st.sdp("narrow")).await;
    h.st.collect(ms(100)).await;
    let c1 = h.s.attach().unwrap();
    h.st.collect(ms(100)).await;
    c1.detach();
    sleep_ms(200).await;
    let _c2 = h.s.attach().expect("re-attach inside the grace");
    let r = h.st.collect(ms(1000)).await;
    assert!(
        !has(&r, "BYE"),
        "hung up although the client came back: {:?}",
        codes(&r)
    );
    assert_eq!(
        h.s.snapshot().answered,
        2,
        "re-attaching sent a second 200 OK"
    );
}

// TestParity_GoodbyeEndsAtOnce (P11)
#[tokio::test]
async fn parity_goodbye_ends_at_once() {
    let h = Harness::with(Opts::orphan(5000)).await;
    h.st.invite("call-FFF", 1, &h.st.sdp("narrow")).await;
    h.st.collect(ms(100)).await;
    let cl = h.s.attach().unwrap();
    h.st.collect(ms(100)).await;
    let t0 = std::time::Instant::now();
    cl.goodbye();
    let r = h.st.collect(ms(1000)).await;
    assert!(has(&r, "BYE"), "GOODBYE produced no BYE: {:?}", codes(&r));
    assert!(
        t0.elapsed() < std::time::Duration::from_secs(2),
        "BYE took {:?}",
        t0.elapsed()
    );
}

// TestParity_AttachWithNoCallRefused (P12)
#[tokio::test]
async fn parity_attach_with_no_call_refused() {
    let h = Harness::new().await;
    let err = h.s.attach().err().expect("attached with no call");
    assert_eq!(err, ServerError::NoCall);
    assert!(
        err.to_string().to_lowercase().contains("no call"),
        "refusal text {err:?} does not say why"
    );
}

// TestParity_CodecNegotiation (P13/P14), the socket half: the handshake rate and frame, the
// answer's port and address
#[tokio::test]
async fn parity_codec_negotiation() {
    // (name, sdp kind, wantPT, wantMap, rate, frame)
    let cases = [
        ("wide", "wide", "96", "a=rtpmap:96 PCM/16000", 16000, 640),
        ("menu", "menu", "96", "a=rtpmap:96 PCM/16000", 16000, 640),
        (
            "l16-as-100",
            "l16",
            "100",
            "a=rtpmap:100 L16/16000",
            16000,
            640,
        ),
        ("narrow", "narrow", "8", "a=rtpmap:8 PCMA/8000", 8000, 320),
        (
            "g722-16000-only",
            "g722only",
            "8",
            "a=rtpmap:8 PCMA/8000",
            8000,
            320,
        ),
        (
            "pcmu-only",
            "pcmuonly",
            "8",
            "a=rtpmap:8 PCMA/8000",
            8000,
            320,
        ),
    ];
    for (name, kind, want_pt, want_map, rate, fr) in cases {
        let h = Harness::new().await;
        h.st.invite(&format!("call-{name}"), 1, &h.st.sdp(kind))
            .await;
        h.st.collect(ms(100)).await;
        let cl = h.s.attach().unwrap();
        assert!(
            cl.rate == rate && cl.frame_bytes == fr,
            "{name}: handshake equivalent = {}/{}, want {rate}/{fr} (read the rate off the call, never assume it)",
            cl.rate,
            cl.frame_bytes
        );
        let m200 = find(&h.st.collect(ms(300)).await, "200")
            .unwrap_or_else(|| panic!("{name}: no 200 OK"));
        let body = m200.body();
        let m_line = body
            .lines()
            .find(|l| l.starts_with("m=audio "))
            .unwrap_or_else(|| panic!("{name}: no m= line\n{body}"));
        let parts: Vec<&str> = m_line.split_whitespace().collect();
        assert!(
            parts.len() == 4 && parts[2] == "RTP/AVP" && parts[3] == want_pt,
            "{name}: answer m= line {m_line:?}, want exactly payload {want_pt}\n{body}"
        );
        assert!(
            body.contains(want_map),
            "{name}: answer lacks {want_map:?}:\n{body}"
        );
        assert!(
            !(body.contains("L16/16000") && kind != "l16"),
            "{name}: the answer claims RFC L16 semantics for a little-endian stream:\n{body}"
        );
        assert_eq!(
            parts[1],
            h.s.rtp_port().to_string(),
            "{name}: answer advertises the wrong RTP port"
        );
        assert!(
            body.contains("c=IN IP4 127.0.0.1"),
            "{name}: answer connection address is not the address the station reaches us at:\n{body}"
        );
    }
}

// TestParity_HangupBothDirections (P15/P16)
#[tokio::test]
async fn parity_hangup_both_directions() {
    let h = Harness::with(Opts::ring(400)).await;
    h.st.invite("call-HUP", 1, &h.st.sdp("narrow")).await;
    h.st.collect(ms(100)).await;
    let cl = h.s.attach().unwrap();
    let mut frames = frames_of(&cl);
    h.st.collect(ms(100)).await;

    h.st.bye("call-STRAY").await;
    let rs = h.st.collect(ms(300)).await;
    assert!(
        has(&rs, "200"),
        "a BYE for an unknown dialog must still be answered 200 (a retransmitting station otherwise keeps retransmitting): {:?}",
        codes(&rs)
    );
    assert!(
        !closed_within(&mut frames, ms(300)).await,
        "a stray BYE closed the LIVE call's client"
    );
    h.st.invite("call-SECOND", 1, &h.st.sdp("narrow")).await;
    let r = h.st.collect(ms(300)).await;
    assert!(
        has(&r, "486") && !has(&r, "180"),
        "the stray BYE cleared the live dialog (2nd caller: {:?})",
        codes(&r)
    );

    h.st.bye("call-HUP").await;
    assert!(
        closed_within(&mut frames, ms(2000)).await,
        "the station's BYE did not tell the client: it would keep a microphone open on a dead dialog"
    );
    assert!(
        !cl.reason().is_empty(),
        "a dropped client carries no reason"
    );
    assert!(
        cl.send(&[0u8; 320]).is_err(),
        "Send succeeded on a client whose call is over"
    );
    h.sink
        .wait_events(&[
            &format!("ringing:call-HUP@{}", h.device_id()),
            "answered:call-HUP",
            "ended:call-HUP",
        ])
        .await;

    // phase 11: the slot is free.
    h.st.collect(ms(100)).await;
    h.st.invite("call-AFTER", 1, &h.st.sdp("narrow")).await;
    let r = h.st.collect(ms(300)).await;
    assert!(
        has(&r, "180") && !has(&r, "486"),
        "after the station's BYE the next caller got {:?}",
        codes(&r)
    );
    h.st.collect(ms(500)).await; // let call-AFTER ring out (480)

    // phase 12: server-side hangup.
    h.st.invite("call-HANGUP", 1, &h.st.sdp("narrow")).await;
    h.st.collect(ms(100)).await;
    let cl2 = h.s.attach().unwrap();
    let mut frames2 = frames_of(&cl2);
    h.st.collect(ms(100)).await;
    assert!(
        h.s.snapshot().app_attached,
        "appAttached false before hangup"
    );
    assert!(h.s.hangup(), "Hangup reported no call");
    assert!(
        closed_within(&mut frames2, ms(1500)).await,
        "Hangup left the client attached to a call that is over"
    );
    let r = h.st.collect(ms(300)).await;
    assert!(
        has(&r, "BYE"),
        "Hangup sent no BYE to the station: {:?}",
        codes(&r)
    );
    let sn = h.s.snapshot();
    assert!(!sn.app_attached && !sn.in_call, "after hangup: {sn:?}");
    assert!(
        !h.s.hangup(),
        "a second Hangup claimed to hang up something"
    );
}

// TestParity_ByeShape (P17)
#[tokio::test]
async fn parity_bye_shape() {
    let h = Harness::new().await;
    h.st.invite("call-BYE", 1, &h.st.sdp("narrow")).await;
    h.st.collect(ms(100)).await;
    let cl = h.s.attach().unwrap();
    let r200 = find(&h.st.collect(ms(200)).await, "200").unwrap();
    let to = r200.header("To");
    let our_tag = to
        .split("tag=")
        .nth(1)
        .unwrap()
        .split_whitespace()
        .next()
        .unwrap()
        .to_string();
    cl.goodbye();
    let bye = find(&h.st.collect(ms(500)).await, "BYE").expect("no BYE");
    let from = bye.header("From");
    assert!(
        from.contains("sip:9901@server") && from.contains(&format!("tag={our_tag}")),
        "BYE From {from:?}, want the station's To with OUR tag {our_tag}"
    );
    let to = bye.header("To");
    assert!(
        to.contains("sip:8001@station") && to.contains("tag=t-call-BYE"),
        "BYE To {to:?}, want the station's From"
    );
    assert_eq!(bye.header("Call-ID"), "call-BYE");
    assert_eq!(bye.header("CSeq"), "2 BYE");
    assert!(
        !bye.header("Via").contains("0.0.0.0"),
        "BYE Via {:?} is a bind string, not a reachable address",
        bye.header("Via")
    );
}

// TestParity_ContactIsReachableNotTheBindString (P18). The ONLY wildcard bind in this suite;
// the station only ever talks to 127.0.0.1.
#[tokio::test]
async fn parity_contact_is_reachable_not_the_bind_string() {
    let h = Harness::with(Opts::cfg(json!({
        "sip_listen": "0.0.0.0:0", "allow_non_loopback": true, "allow_from": ["127.0.0.0/8"]
    })))
    .await;
    h.st.invite("call-CONTACT", 1, &h.st.sdp("narrow")).await;
    let r = h.st.collect(ms(300)).await;
    let m180 = find(&r, "180").unwrap_or_else(|| panic!("no 180: {:?}", codes(&r)));
    let c = m180.header("Contact");
    assert!(
        !c.contains("0.0.0.0") && c.contains("@127.0.0.1:"),
        "Contact {c:?} advertises the bind string"
    );
    assert!(
        c.contains(&format!(":{}>", h.s.sip_addr().port())),
        "Contact {c:?} carries the wrong port (bound {})",
        h.s.sip_addr().port()
    );
}

// TestParity_AckConfirmsDialog (P19)
#[tokio::test]
async fn parity_ack_confirms_dialog() {
    let h = Harness::new().await;
    h.st.invite("call-ACK", 1, &h.st.sdp("narrow")).await;
    h.st.collect(ms(100)).await;
    let _cl = h.s.attach().unwrap();
    h.st.collect(ms(100)).await;
    assert!(!h.s.snapshot().dialog_confirmed, "confirmed before any ACK");
    h.st.ack("call-OTHER").await;
    sleep_ms(100).await;
    assert!(
        !h.s.snapshot().dialog_confirmed,
        "a stray ACK confirmed the live dialog"
    );
    h.st.ack("call-ACK").await;
    h.sink
        .wait_events(&[
            &format!("ringing:call-ACK@{}", h.device_id()),
            "answered:call-ACK",
            "active:call-ACK",
        ])
        .await;
    assert!(
        h.s.snapshot().dialog_confirmed,
        "the ACK did not confirm the dialog"
    );
}

// TestParity_OptionsAndCancel (P20/P21)
#[tokio::test]
async fn parity_options_and_cancel() {
    let h = Harness::with(Opts::ring(5000)).await;
    h.st.options().await;
    let r = h.st.collect(ms(200)).await;
    assert!(has(&r, "200"), "OPTIONS: {:?}", codes(&r));

    h.st.invite("call-CAN", 1, &h.st.sdp("narrow")).await;
    h.st.collect(ms(150)).await;
    h.st.cancel("call-NOT-LIVE").await;
    let r = h.st.collect(ms(200)).await;
    assert!(
        has(&r, "200"),
        "a CANCEL for an unknown dialog must still be answered: {:?}",
        codes(&r)
    );
    assert!(
        h.s.snapshot().ringing,
        "a CANCEL for another dialog ended the live call"
    );
    h.st.cancel("call-CAN").await;
    let r = h.st.collect(ms(300)).await;
    assert!(has(&r, "200"), "CANCEL not answered: {:?}", codes(&r));
    // DEVIATION from gate-sip by decision (2026-10-21): the cancelled INVITE gets its final
    // response, 487, carrying the INVITE's own CSeq (not the CANCEL's).
    let m487 = find(&r, "487")
        .unwrap_or_else(|| panic!("no 487 for the cancelled INVITE: {:?}", codes(&r)));
    assert_eq!(m487.header("CSeq"), "1 INVITE");
    assert!(!h.s.snapshot().ringing, "CANCEL did not end the call");
    h.sink
        .wait_events(&[
            &format!("ringing:call-CAN@{}", h.device_id()),
            "ended:call-CAN",
        ])
        .await;
    h.st.invite("call-NEXT", 1, &h.st.sdp("narrow")).await;
    let r = h.st.collect(ms(200)).await;
    assert!(
        has(&r, "180"),
        "CANCEL did not free the slot: {:?}",
        codes(&r)
    );
}

// TestParity_AudioFromStation (P22), A-law
#[tokio::test]
async fn parity_audio_from_station_alaw() {
    let h = Harness::new().await;
    h.st.invite("call-A", 1, &h.st.sdp("narrow")).await;
    h.st.collect(ms(100)).await;
    let cl = h.s.attach().unwrap();
    let mut frames = frames_of(&cl);
    h.st.collect(ms(100)).await;
    let mut got = Vec::new();
    for i in 0..50usize {
        let mut pcm = Vec::with_capacity(320);
        for n in 0..160usize {
            let v = (8000.0
                * (2.0 * std::f64::consts::PI * 440.0 * (i * 160 + n) as f64 / 8000.0).sin())
                as i16;
            pcm.extend_from_slice(&v.to_le_bytes());
        }
        let pkt = rtp_packet(8, i as u16, (i * 160) as u32, &alaw_encode(&pcm));
        h.st.rtp.send_to(&pkt, h.rtp_addr()).await.unwrap();
        let (f, open, ok) = frame_within(&mut frames, std::time::Duration::from_secs(1)).await;
        assert!(ok && open, "frame {i} never arrived");
        let f = f.unwrap();
        assert_eq!(
            f.len(),
            320,
            "frame {i} is {} bytes, want 320 (a 12-byte header left on would make it 344)",
            f.len()
        );
        got.extend_from_slice(&f);
    }
    let (tone, control) = (
        goertzel(&got, 8000.0, 440.0),
        goertzel(&got, 8000.0, 1234.0),
    );
    assert!(
        tone >= control * 1e4,
        "recovered audio is not a 440 Hz tone: tone {tone:.3e} vs control {control:.3e}"
    );
    assert_eq!(h.s.snapshot().rtp_in, 50);
}

// TestParity_AudioFromStation (P22b), wideband little-endian
#[tokio::test]
async fn parity_audio_from_station_wideband() {
    let h = Harness::new().await;
    h.st.invite("call-W", 1, &h.st.sdp("wide")).await;
    h.st.collect(ms(100)).await;
    let cl = h.s.attach().unwrap();
    let mut frames = frames_of(&cl);
    h.st.collect(ms(100)).await;
    let payload: Vec<u8> = (0..1280usize).map(|i| (i * 7) as u8).collect(); // the station's real 40 ms payload
    h.st.rtp
        .send_to(&rtp_packet(96, 1, 0, &payload), h.rtp_addr())
        .await
        .unwrap();
    let (f, open, ok) = frame_within(&mut frames, std::time::Duration::from_secs(1)).await;
    assert!(ok && open, "no frame");
    assert_eq!(
        f.unwrap(),
        payload,
        "the wideband payload was altered or truncated"
    );
}

fn seq_of(h: &[u8]) -> u16 {
    u16::from_be_bytes([h[2], h[3]])
}
fn ts_of(h: &[u8]) -> u32 {
    u32::from_be_bytes([h[4], h[5], h[6], h[7]])
}

// TestParity_AudioToStation (P23), A-law
#[tokio::test]
async fn parity_audio_to_station_alaw() {
    let h = Harness::new().await;
    h.st.invite("call-S", 1, &h.st.sdp("narrow")).await;
    h.st.collect(ms(100)).await;
    let cl = h.s.attach().unwrap();
    let read = || async {
        h.st.read_rtp(std::time::Duration::from_secs(2))
            .await
            .expect("no RTP from the server")
    };
    // silence first: an unbroken cadence is what keeps the station streaming
    let (hdr1, p1) = read().await;
    assert!(hdr1[0] == 0x80 && hdr1[1] == 8, "header {hdr1:02x?}");
    assert!(
        p1.len() == 160 && p1[0] == 0xD5 && p1[159] == 0xD5,
        "silence is not 160 x 0xD5: {} bytes, first {:#x}",
        p1.len(),
        p1[0]
    );
    let (hdr2, _) = read().await;
    assert!(
        seq_of(&hdr2) == seq_of(&hdr1).wrapping_add(1) && ts_of(&hdr2) == ts_of(&hdr1) + 160,
        "seq/ts advance {}->{}, {}->{}, want +1 and +160",
        seq_of(&hdr1),
        seq_of(&hdr2),
        ts_of(&hdr1),
        ts_of(&hdr2)
    );
    // 20 ms cadence: 10 packets take ~200 ms
    let t0 = std::time::Instant::now();
    for _ in 0..10 {
        read().await;
    }
    let d = t0.elapsed();
    assert!(
        d >= ms(150) && d <= ms(600),
        "10 packets took {d:?}, want ~200ms"
    );
    // the mic: PCM16 in, A-law out
    let mut pcm = Vec::new();
    for _ in 0..160 {
        pcm.extend_from_slice(&5000i16.to_le_bytes());
    }
    cl.send(&pcm).unwrap();
    let want = alaw_encode(&pcm);
    for _ in 0..20 {
        let (_, p) = read().await;
        if p.len() == 160 && p[0] != 0xD5 {
            assert_eq!(p, want, "mic frame arrived altered");
            return;
        }
    }
    panic!("the mic frame never reached the station");
}

// TestParity_AudioToStation (P23), wideband
#[tokio::test]
async fn parity_audio_to_station_wideband() {
    let h = Harness::new().await;
    h.st.invite("call-SW", 1, &h.st.sdp("wide")).await;
    h.st.collect(ms(100)).await;
    let cl = h.s.attach().unwrap();
    let read = || async {
        h.st.read_rtp(std::time::Duration::from_secs(2))
            .await
            .expect("no RTP from the server")
    };
    let (hdr1, p1) = read().await;
    assert_eq!(hdr1[1], 96, "payload type");
    assert!(
        p1.len() == 640 && p1.iter().all(|&b| b == 0),
        "wideband silence is not 640 zero bytes ({})",
        p1.len()
    );
    let (hdr2, _) = read().await;
    assert_eq!(
        ts_of(&hdr2).wrapping_sub(ts_of(&hdr1)),
        320,
        "wideband timestamp step (samples, not bytes)"
    );
    let mic: Vec<u8> = (0..640usize).map(|i| (i % 250 + 1) as u8).collect();
    cl.send(&mic).unwrap();
    for _ in 0..20 {
        let (_, p) = read().await;
        if p.len() == 640 && p[0] != 0 {
            assert_eq!(
                p, mic,
                "wideband mic frame was transcoded; it must pass straight through"
            );
            return;
        }
    }
    panic!("the wideband mic frame never reached the station");
}

// TestParity_BackpressureDropsAndCounts (P25), the RTP half against the real server
#[tokio::test]
async fn parity_backpressure_drops_and_counts() {
    let h = Harness::new().await;
    h.st.invite("call-BP", 1, &h.st.sdp("narrow")).await;
    h.st.collect(ms(100)).await;
    let cl = h.s.attach().unwrap();
    for i in 0..40u16 {
        // never read the client's frames
        h.st.rtp
            .send_to(
                &rtp_packet(8, i, u32::from(i) * 160, &[0u8; 160]),
                h.rtp_addr(),
            )
            .await
            .unwrap();
    }
    let end = std::time::Instant::now() + std::time::Duration::from_secs(2);
    while std::time::Instant::now() < end && h.s.snapshot().rtp_in < 40 {
        sleep_ms(10).await;
    }
    let sn = h.s.snapshot();
    assert!(
        sn.rtp_in == 40 && sn.frames_to_app == 6 && sn.dropped_down == 34,
        "40 packets, queue of 6: rtpIn {} toApp {} droppedDown {}, want 40/6/34",
        sn.rtp_in,
        sn.frames_to_app,
        sn.dropped_down
    );
    for _ in 0..50 {
        let _ = cl.send(&[0u8; 320]);
    }
    let sn = h.s.snapshot();
    assert!(
        sn.frames_from_app == 50 && sn.dropped_up > 0,
        "uplink: fromApp {} droppedUp {}",
        sn.frames_from_app,
        sn.dropped_up
    );
}

// TestParity_DoesNotStampHomeAssistant (P26), the half that needs the server: one ring is
// exactly one Ringing event (the station's retransmissions don't add one)
#[tokio::test]
async fn parity_one_ring_is_one_ringing_event() {
    let h = Harness::with(Opts::ring(5000)).await;
    h.st.invite("call-ONE", 1, &h.st.sdp("narrow")).await;
    h.st.invite("call-ONE", 1, &h.st.sdp("narrow")).await;
    h.st.collect(ms(300)).await;
    h.sink
        .wait_events(&[&format!("ringing:call-ONE@{}", h.device_id())])
        .await;
}

// TestParity_SecondAttachRefusedNotDisplaced (D1)
#[tokio::test]
async fn parity_second_attach_refused_not_displaced() {
    let h = Harness::new().await;
    h.st.invite("call-D1", 1, &h.st.sdp("narrow")).await;
    h.st.collect(ms(100)).await;
    let first = h.s.attach().unwrap();
    let mut frames = frames_of(&first);
    let err = h.s.attach().err().expect("second attach succeeded");
    assert_eq!(err, ServerError::AlreadyAttached);
    assert!(err.to_string().contains("already answered"), "text {err}");
    assert!(
        !closed_within(&mut frames, ms(200)).await,
        "the first client was displaced"
    );
    assert_eq!(
        h.s.answer_current(),
        Err(ServerError::AlreadyAnswered),
        "AnswerCurrent on an answered call"
    );
}

// TestParity_RTPFromOutsiderRejected (D2)
#[tokio::test]
async fn parity_rtp_from_outsider_rejected() {
    let h = Harness::new().await;
    h.st.invite("call-D2", 1, &h.st.sdp("narrow")).await;
    h.st.collect(ms(100)).await;
    let cl = h.s.attach().unwrap();
    let mut frames = frames_of(&cl);
    let outsider = Station::on(h.s.sip_addr(), "127.0.0.2").await;
    outsider
        .rtp
        .send_to(&rtp_packet(8, 1, 0, &[0u8; 160]), h.rtp_addr())
        .await
        .unwrap();
    let (_, _, got) = frame_within(&mut frames, ms(300)).await;
    assert!(
        !got,
        "audio from an address the station never named reached the client"
    );
    let sn = h.s.snapshot();
    assert!(sn.rtp_rejected == 1 && sn.rtp_in == 0, "snapshot {sn:?}");
    h.st.rtp
        .send_to(&rtp_packet(8, 2, 160, &[0u8; 160]), h.rtp_addr())
        .await
        .unwrap();
    let (_, _, got) = frame_within(&mut frames, std::time::Duration::from_secs(1)).await;
    assert!(got, "the station's own audio was refused");
}

// TestParity_AllowFromAndLoopbackDefault (D3), the datagram half: sources outside allow_from
// are dropped silently
#[tokio::test]
async fn parity_allow_from_drops_outsiders_silently() {
    let h = Harness::with(Opts::cfg(json!({"allow_from": ["127.0.0.1"]}))).await;
    let outsider = Station::on(h.s.sip_addr(), "127.0.0.2").await;
    outsider
        .invite("call-OUT", 1, &outsider.sdp("narrow"))
        .await;
    outsider.register("").await;
    outsider.options().await;
    let r = outsider.collect(ms(300)).await;
    assert!(
        r.is_empty(),
        "an address outside allow_from got an answer: {:?}",
        codes(&r)
    );
    let sn = h.s.snapshot();
    assert!(
        sn.calls == 0 && !sn.registered && sn.dropped == 3,
        "a refused datagram touched state: {sn:?}"
    );
    h.st.options().await;
    let r = h.st.collect(ms(200)).await;
    assert!(
        has(&r, "200"),
        "the allowed station is not served: {:?}",
        codes(&r)
    );
}

// TestParity_StopEndsALiveCall (D4)
#[tokio::test]
async fn parity_stop_ends_a_live_call() {
    let h = Harness::with(Opts::ring(5000)).await;
    h.st.invite("call-STOP", 1, &h.st.sdp("narrow")).await;
    h.st.collect(ms(100)).await;
    let cl = h.s.attach().unwrap();
    let mut frames = frames_of(&cl);
    h.st.collect(ms(100)).await;
    h.d.stop().await.unwrap();
    assert!(
        has(&h.st.collect(ms(500)).await, "BYE"),
        "Stop left the station in a call"
    );
    assert!(
        closed_within(&mut frames, ms(1000)).await,
        "Stop left the client attached"
    );
    h.sink
        .wait_events(&[
            &format!("ringing:call-STOP@{}", h.device_id()),
            "answered:call-STOP",
            "ended:call-STOP",
        ])
        .await;

    let h2 = Harness::with(Opts::ring(5000)).await;
    h2.st.invite("call-STOP2", 1, &h2.st.sdp("narrow")).await;
    h2.st.collect(ms(100)).await;
    h2.d.stop().await.unwrap();
    assert!(
        has(&h2.st.collect(ms(500)).await, "480"),
        "Stop left a ringing call unanswered"
    );
}

// TestParity_MalformedInviteDropped (D5)
#[tokio::test]
async fn parity_malformed_invite_dropped() {
    let h = Harness::new().await;
    h.st.send("INVITE sip:9901@x SIP/2.0\r\nVia: SIP/2.0/UDP 127.0.0.1:9;branch=z9hG4bKx\r\nFrom: <sip:a@b>;tag=1\r\nTo: <sip:c@d>\r\nCSeq: 1 INVITE\r\nContent-Length: 0\r\n\r\n").await;
    let r = h.st.collect(ms(300)).await;
    assert!(r.is_empty(), "answered a malformed INVITE: {:?}", codes(&r));
    assert_eq!(
        h.s.snapshot().calls,
        0,
        "counted a malformed INVITE as a call"
    );
}

// TestParity_SDPAddressOutsideAllowFromFallsBack (D2b)
#[tokio::test]
async fn parity_sdp_address_outside_allow_from_falls_back() {
    for (name, cline) in [("third party", "192.168.2.9"), ("garbage", "999.1.1.1")] {
        let h = Harness::with(Opts::cfg(json!({"allow_from": ["127.0.0.1"]}))).await;
        let sdp = h.st.sdp("narrow").replacen(
            &format!("c=IN IP4 {}", h.st.me.ip()),
            &format!("c=IN IP4 {cline}"),
            1,
        );
        h.st.invite("call-D2b", 1, &sdp).await;
        h.st.collect(ms(100)).await;
        let far =
            h.s.far_rtp()
                .unwrap_or_else(|| panic!("{name}: no far RTP address"));
        assert_eq!(
            far.ip,
            h.st.me.ip(),
            "{name}: far RTP IP, want the SIP source"
        );
    }
}

// TestRTPRejectLoggedOncePerCallAndSummaryAtEnd (D3, DEV-518)
#[tokio::test]
async fn rtp_reject_logged_once_per_call_and_summary_at_end() {
    let h = Harness::new().await;
    let lines = Arc::new(Mutex::new(Vec::<String>::new()));
    let l2 = lines.clone();
    h.s.set_logger(move |l| l2.lock().unwrap().push(l.to_string()));
    h.st.invite("call-rtplog", 1, &h.st.sdp("narrow")).await;
    h.st.collect(ms(100)).await;
    let _cl = h.s.attach().unwrap();
    let outsider = Station::on(h.s.sip_addr(), "127.0.0.2").await;
    let secret = format!("SECRETPAYLOAD{}", "0123456789".repeat(15));
    for i in 0..3u16 {
        outsider
            .rtp
            .send_to(
                &rtp_packet(8, i, u32::from(i) * 160, secret.as_bytes()),
                h.rtp_addr(),
            )
            .await
            .unwrap();
    }
    wait_for("3 rejections", || h.s.snapshot().rtp_rejected == 3).await;
    h.st.rtp
        .send_to(&rtp_packet(8, 9, 0, &[0u8; 160]), h.rtp_addr())
        .await
        .unwrap();
    wait_for("1 accepted", || h.s.snapshot().rtp_in == 1).await;
    assert!(h.s.hangup(), "no call to hang up");
    let lines = lines.lock().unwrap();
    let rejected: Vec<_> = lines
        .iter()
        .filter(|l| l.contains("RTP rejected"))
        .collect();
    let summary: Vec<_> = lines
        .iter()
        .filter(|l| l.contains("rtpRejected="))
        .collect();
    assert!(
        !lines.iter().any(|l| l.contains("SECRETPAYLOAD")),
        "payload in a log line"
    );
    assert!(
        rejected.len() == 1
            && rejected[0].contains("from 127.0.0.2:")
            && rejected[0].contains("expected host 127.0.0.1"),
        "rejection lines {rejected:?}, want exactly one naming source 127.0.0.2:<port> and expected host 127.0.0.1"
    );
    assert!(
        summary.len() == 1
            && summary[0].contains("rtpIn=1")
            && summary[0].contains("rtpRejected=3")
            && summary[0].contains("rtpOut="),
        "summary lines {summary:?}, want one with rtpIn=1 rtpRejected=3"
    );
}

async fn wait_for(what: &str, mut f: impl FnMut() -> bool) {
    let end = std::time::Instant::now() + std::time::Duration::from_secs(2);
    while std::time::Instant::now() < end {
        if f() {
            return;
        }
        sleep_ms(10).await;
    }
    panic!("timed out waiting for {what}");
}

// TestAnswerThroughTheDriver (misc_test.go)
#[tokio::test]
async fn answer_through_the_driver() {
    let h = Harness::with(Opts::ring(5000)).await;
    let t = CancelToken::new();
    let e = h.d.answer(&t, MediaTarget::default()).await.unwrap_err();
    assert_eq!(e.message(), "no call to answer", "Answer with no call");
    h.st.invite("call-DRV", 1, &h.st.sdp("narrow")).await;
    h.st.collect(ms(100)).await;
    let e =
        h.d.answer(
            &t,
            MediaTarget {
                host: "127.0.0.1".into(),
                port: 9,
            },
        )
        .await
        .unwrap_err();
    assert!(
        e.message().contains("not_supported"),
        "Answer with a client media target should be refused: {e}"
    );
    h.d.answer(&t, MediaTarget::default()).await.unwrap();
    assert!(has(&h.st.collect(ms(300)).await, "200"), "no 200 OK");
    let e = h.d.answer(&t, MediaTarget::default()).await.unwrap_err();
    assert_eq!(e.message(), "already answered", "second Answer");
    // An answered call with nobody attached: the grace starts when a client LEAVES. With none
    // ever attached, Hangup is the way out.
    assert!(h.s.hangup(), "no call to hang up");
}

// TestStartTwiceAndContextStop (misc_test.go)
#[tokio::test]
async fn start_twice_and_context_stop() {
    let mut d = GateSipServer::default();
    let cfg = json!({"sip_listen": "127.0.0.1:0", "password": "s3cretpass", "rtp_port": 0});
    d.configure(cfg.as_object().unwrap()).unwrap();
    let ctx = CancelToken::new();
    d.start(&ctx, Arc::new(Sink::default())).await.unwrap();
    let e = d
        .start(&ctx, Arc::new(Sink::default()))
        .await
        .expect_err("second Start succeeded");
    assert_eq!(e.message(), "gate_sip_server: already started");
    let s = d.real_server().unwrap();
    let addr: SocketAddr = s.sip_addr();
    ctx.cancel();
    let end = std::time::Instant::now() + std::time::Duration::from_secs(2);
    while d.real_server().is_some() && std::time::Instant::now() < end {
        sleep_ms(10).await;
    }
    assert!(
        d.real_server().is_none(),
        "context cancel did not stop the server"
    );
    // the port is free again once the server is stopped (shutdown has joined every task); no
    // sleep first, as the Go test (R2-92): a late release is the regression this pins
    drop(
        tokio::net::UdpSocket::bind(addr)
            .await
            .expect("SIP port still held after Stop"),
    );
}

// A concurrent double Start: exactly one wins (the atomic start of the 3a review).
#[tokio::test]
async fn concurrent_start_has_one_winner() {
    let mut d = GateSipServer::default();
    let cfg = json!({"sip_listen": "127.0.0.1:0", "password": "s3cretpass", "rtp_port": 0});
    d.configure(cfg.as_object().unwrap()).unwrap();
    let ctx = CancelToken::new();
    let (a, b) = tokio::join!(
        d.start(&ctx, Arc::new(Sink::default())),
        d.start(&ctx, Arc::new(Sink::default()))
    );
    assert!(a.is_ok() != b.is_ok(), "start results: {a:?} / {b:?}");
    d.stop().await.unwrap();
}

// Binding a taken port is a start error with Go's wording, and leaves nothing running.
#[tokio::test]
async fn start_on_a_taken_port_fails_cleanly() {
    let taken = tokio::net::UdpSocket::bind("127.0.0.1:0").await.unwrap();
    let port = taken.local_addr().unwrap().port();
    let mut d = GateSipServer::default();
    let cfg =
        json!({"sip_listen": format!("127.0.0.1:{port}"), "password": "s3cretpass", "rtp_port": 0});
    d.configure(cfg.as_object().unwrap()).unwrap();
    let e = d
        .start(&CancelToken::new(), Arc::new(Sink::default()))
        .await
        .unwrap_err();
    assert_eq!(
        e.message(),
        format!("gate_sip_server: cannot listen on 127.0.0.1:{port}: listen udp 127.0.0.1:{port}: bind: address already in use")
    );
    assert!(d.real_server().is_none());
}

// ---- the review fixes of 3b-1 (Rust-only tests; the Go behaviour they depart from is in PARITY.md)

// Echoed headers are the bytes received: a Latin-1 display name comes back byte for byte in the
// 200 and in our BYE (slice-2 difference 3; Go keeps the received bytes in its strings).
#[tokio::test]
async fn echoed_headers_are_the_received_bytes() {
    let h = Harness::new().await;
    let sdp = h.st.sdp("narrow");
    let mut msg: Vec<u8> = Vec::new();
    msg.extend_from_slice(
        format!(
            "INVITE sip:9901@127.0.0.1:{} SIP/2.0\r\nVia: SIP/2.0/UDP {};branch=z9hG4bKlat\r\nFrom: \"Jos",
            h.s.sip_addr().port(),
            h.st.me
        )
        .as_bytes(),
    );
    msg.push(0xe9);
    msg.extend_from_slice(
        format!(
            "\" <sip:8001@station>;tag=t-lat\r\nTo: <sip:9901@server>\r\nCall-ID: call-lat\r\nCSeq: 1 INVITE\r\nContent-Type: application/sdp\r\nContent-Length: {}\r\n\r\n{sdp}",
            sdp.len()
        )
        .as_bytes(),
    );
    h.st.send_bytes(&msg).await;
    let r = h.st.collect(ms(200)).await;
    let want: &[u8] = b"From: \"Jos\xe9\" <sip:8001@station>;tag=t-lat";
    let has_from = |m: &Msg| m.bytes.windows(want.len()).any(|w| w == want);
    assert!(
        r.iter().all(has_from) && r.len() == 2,
        "100/180 repair the From: {:?}",
        codes(&r)
    );
    let cl = h.s.attach().unwrap();
    let r = h.st.collect(ms(200)).await;
    assert!(
        find(&r, "200").is_some_and(|m| has_from(&m)),
        "200 repaired the From"
    );
    cl.goodbye();
    let r = h.st.collect(ms(300)).await;
    let bye = find(&r, "BYE").expect("no BYE");
    let to: &[u8] = b"To: \"Jos\xe9\" <sip:8001@station>;tag=t-lat";
    assert!(
        bye.bytes.windows(to.len()).any(|w| w == to),
        "BYE repaired the display name"
    );
}

// A BYE or CANCEL for the live Call-ID from another host is answered 200 (with a tag of its own)
// and ends nothing; the station's own CANCEL of an ANSWERED call still ends it, as in Go.
#[tokio::test]
async fn bye_and_cancel_from_another_host_end_nothing() {
    let h = Harness::new().await;
    let lines = Arc::new(Mutex::new(Vec::<String>::new()));
    let l2 = lines.clone();
    h.s.set_logger(move |l| l2.lock().unwrap().push(l.to_string()));
    h.st.invite("call-F", 1, &h.st.sdp("narrow")).await;
    let r = h.st.collect(ms(150)).await;
    let live_tag = find(&r, "180").unwrap().header("To");
    let _cl = h.s.attach().unwrap();
    h.st.collect(ms(100)).await;
    let outsider = Station::on(h.s.sip_addr(), "127.0.0.2").await;
    outsider.bye("call-F").await;
    outsider.cancel("call-F").await;
    outsider.bye("call-F").await;
    let r = outsider.collect(ms(300)).await;
    assert_eq!(codes(&r), vec!["200", "200", "200"]);
    assert!(
        r.iter().all(|m| !live_tag.contains(
            &m.header("To")
                .split("tag=")
                .last()
                .unwrap_or("?")
                .to_string()
        )),
        "the live call's tag leaked to another host"
    );
    assert!(
        h.s.snapshot().in_call,
        "a BYE from another host ended the call"
    );
    let ignored = lines
        .lock()
        .unwrap()
        .iter()
        .filter(|l| l.contains("ignored (once per call)"))
        .count();
    assert_eq!(ignored, 1, "logged once per call");
    h.st.cancel("call-F").await; // the station's CANCEL of an answered call ends it (Go)
    h.st.collect(ms(200)).await;
    assert!(!h.s.snapshot().in_call);
}

// A REGISTER with expiry 0 (header or Contact parameter) removes the registration and
// republishes CanDial false; Go registers it.
#[tokio::test]
async fn register_with_expiry_zero_clears_the_registration() {
    for (expires, params) in [("0", ""), ("300", ";expires=0")] {
        let h = Harness::with(Opts::cfg(json!({"dial_enabled": true}))).await;
        register_station(&h).await;
        assert!(h.d.capabilities().can_dial);
        h.st.register("").await;
        let m401 = find(&h.st.collect(ms(200)).await, "401").unwrap();
        h.st.register_expiring(&digest_auth(&m401, "8001", "s3cretpass"), expires, params)
            .await;
        let r = h.st.collect(ms(200)).await;
        let m200 = find(&r, "200").unwrap_or_else(|| panic!("{:?}", codes(&r)));
        assert_eq!(m200.header("Expires"), "0", "{expires} {params}");
        let sn = h.s.snapshot();
        assert!(!sn.registered && sn.digest_ok == 2, "{sn:?}");
        assert!(!h.d.capabilities().can_dial);
        h.sink
            .wait_events(&[
                &format!("caps:{}:true", h.device_id()),
                &format!("caps:{}:false", h.device_id()),
            ])
            .await;
    }
}

// A media port above 65535 is answered as in Go (no RTP ever), counted and logged.
#[tokio::test]
async fn unsendable_media_port_is_counted_and_logged() {
    let h = Harness::new().await;
    let lines = Arc::new(Mutex::new(Vec::<String>::new()));
    let l2 = lines.clone();
    h.s.set_logger(move |l| l2.lock().unwrap().push(l.to_string()));
    let sdp =
        h.st.sdp("narrow")
            .replace(&format!("m=audio {} ", h.st.rtp_port()), "m=audio 70000 ");
    h.st.invite("call-70k", 1, &sdp).await;
    h.st.collect(ms(100)).await;
    h.s.answer_current().unwrap();
    assert!(
        h.st.read_rtp(ms(300)).await.is_none(),
        "RTP was sent to a port above 65535"
    );
    assert_eq!(h.s.snapshot().unsendable_media, 1);
    assert!(lines
        .lock()
        .unwrap()
        .iter()
        .any(|l| l.contains("WARNING") && l.contains("70000")));
}

// The contract of `shutdown`: it returns when the SIP and RTP ports are free (no sleep before
// re-binding them), and a second caller waits for the first one's finish instead of returning
// early. (The first caller dropping its future is covered by `a_dropped_shutdown_*`.)
#[tokio::test]
async fn shutdown_returns_with_both_ports_free() {
    let h = Harness::new().await;
    let sip = h.s.sip_addr();
    let rtp = SocketAddr::new(sip.ip(), h.s.rtp_port());
    let (a, b) = tokio::join!(h.s.shutdown(), h.s.shutdown());
    let _ = (a, b);
    drop(
        tokio::net::UdpSocket::bind(sip)
            .await
            .expect("SIP port still held"),
    );
    drop(
        tokio::net::UdpSocket::bind(rtp)
            .await
            .expect("RTP port still held"),
    );
}

// A shutdown whose first caller was dropped before it finished must not strand the next one.
// The state "dropped half way" is real only when the first poll is pre-empted at an await; with
// a zero timeout the first poll runs to the spawn of the finisher, which is exactly the part
// that outlives the dropped future.
#[tokio::test]
async fn a_dropped_shutdown_does_not_strand_the_next_caller() {
    let h = Harness::new().await;
    let sip = h.s.sip_addr();
    let rtp = SocketAddr::new(sip.ip(), h.s.rtp_port());
    let _ = tokio::time::timeout(Duration::ZERO, h.s.shutdown()).await;
    tokio::time::timeout(Duration::from_secs(2), h.s.shutdown())
        .await
        .expect("the second shutdown hung");
    drop(
        tokio::net::UdpSocket::bind(sip)
            .await
            .expect("SIP port still held"),
    );
    drop(
        tokio::net::UdpSocket::bind(rtp)
            .await
            .expect("RTP port still held"),
    );
}

// Past shutdown nothing answers: the sockets are closed, so an INVITE is never even read. (The
// guard in `on_invite` for an INVITE that is read while `closed` is set is tested in
// `server/tests.rs`: `an_invite_after_closed_starts_no_call`.)
#[tokio::test]
async fn a_stopped_server_answers_nothing() {
    let h = Harness::new().await;
    h.s.shutdown().await;
    h.st.invite("late", 1, &h.st.sdp("narrow")).await;
    assert!(h.st.collect(ms(200)).await.is_empty());
    assert_eq!(h.s.snapshot().calls, 0);
}

// ---- DEV-796 S1 (PARITY.md 473): an INVITE only from the registered station ----------------------

fn strict_two_hosts() -> Opts {
    Opts {
        strict_invites: true,
        ..Opts::cfg(json!({"allow_from": ["127.0.0.1", "127.0.0.2"]}))
    }
}

/// With the real rule on: an INVITE before any REGISTER is refused 403, rings nothing and takes
/// no call slot (before the change it answered 100 and 180 and rang the house).
#[tokio::test]
async fn s1_an_invite_before_any_register_is_refused_and_rings_nothing() {
    let h = Harness::with(strict_two_hosts()).await;
    h.st.invite("call-X", 1, &h.st.sdp("narrow")).await;
    let r = h.st.collect(ms(300)).await;
    assert_eq!(codes(&r), vec!["403"], "{r:?}");
    let sn = h.s.snapshot();
    assert!(!sn.ringing && !sn.in_call && sn.calls == 0, "{sn:?}");
    assert!(h.sink.events().is_empty(), "{:?}", h.sink.events());
}

/// Another allowed host cannot ring the house or hold the slot while the station is registered;
/// the station then rings normally and a second INVITE from the other host is 403, not 486.
#[tokio::test]
async fn s1_another_allowed_host_cannot_ring_or_hold_the_call_slot() {
    let h = Harness::with(strict_two_hosts()).await;
    register_station(&h).await;
    let other = Station::on(h.s.sip_addr(), "127.0.0.2").await;
    other.invite("call-ROGUE", 1, &other.sdp("narrow")).await;
    let r = other.collect(ms(300)).await;
    assert_eq!(codes(&r), vec!["403"], "{r:?}");
    assert_eq!(h.s.snapshot().calls, 0);
    assert!(!h.sink.events().iter().any(|e| e.starts_with("ringing")));

    h.st.invite("call-REAL", 1, &h.st.sdp("narrow")).await;
    let r = h.st.collect(ms(300)).await;
    assert!(has(&r, "100") && has(&r, "180"), "{:?}", codes(&r));
    // DEV-518: the REGISTER itself is republished (StationRegistered; CanDial stays false, dial is off)
    h.sink
        .wait_events(&[
            &format!("caps:{}:false", h.device_id()),
            &format!("ringing:call-REAL@{}", h.device_id()),
        ])
        .await;

    // while the real call rings, the rogue still gets 403 (not 486: no information, no slot)
    other.invite("call-ROGUE2", 1, &other.sdp("narrow")).await;
    assert_eq!(codes(&other.collect(ms(300)).await), vec!["403"]);
    assert_eq!(h.s.snapshot().calls, 1);
    // the registered station's retransmission is still re-answered
    h.st.invite("call-REAL", 1, &h.st.sdp("narrow")).await;
    assert!(has(&h.st.collect(ms(300)).await, "180"));
}

/// A de-registration (and a lapsed registration) takes the right away again.
#[tokio::test]
async fn s1_a_lapsed_registration_no_longer_rings() {
    let h = Harness::with(strict_two_hosts()).await;
    h.s.set_reg_ttl(ms(200));
    register_station(&h).await;
    sleep_ms(300).await;
    h.st.invite("call-LATE", 1, &h.st.sdp("narrow")).await;
    assert_eq!(codes(&h.st.collect(ms(300)).await), vec!["403"]);
}

// ---- DEV-796 follow-up: the other half of the S1 rule ---------------------------------------------

/// A REGISTER that is only challenged, or refused for a wrong digest, registers nothing: the
/// INVITE after it is still 403.
#[tokio::test]
async fn s1_a_failed_or_unfinished_register_does_not_open_the_door() {
    let h = Harness::with(strict_two_hosts()).await;
    h.st.register("").await;
    let m401 = find(&h.st.collect(ms(300)).await, "401").expect("no challenge");
    h.st.invite("c-401", 1, &h.st.sdp("narrow")).await;
    assert_eq!(codes(&h.st.collect(ms(300)).await), vec!["403"]);
    h.st.register(&digest_auth(&m401, "8001", "wrongpassw")).await;
    assert_eq!(codes(&h.st.collect(ms(300)).await), vec!["403"]);
    h.st.invite("c-bad", 1, &h.st.sdp("narrow")).await;
    assert_eq!(codes(&h.st.collect(ms(300)).await), vec!["403"]);
    assert_eq!(h.s.snapshot().calls, 0);
}

/// `Expires: 0` takes the right away at once, with no wait for the TTL.
#[tokio::test]
async fn s1_a_deregistration_takes_the_door_away() {
    let h = Harness::with(strict_two_hosts()).await;
    register_station(&h).await;
    h.st.invite("c-before", 1, &h.st.sdp("narrow")).await;
    assert!(has(&h.st.collect(ms(300)).await, "180"));
    h.st.register("").await;
    let m401 = find(&h.st.collect(ms(300)).await, "401").expect("no challenge");
    h.st.register_expiring(&digest_auth(&m401, "8001", "s3cretpass"), "0", "")
        .await;
    assert!(find(&h.st.collect(ms(300)).await, "200").is_some());
    h.st.invite("c-after", 1, &h.st.sdp("narrow")).await;
    assert_eq!(codes(&h.st.collect(ms(300)).await), vec!["403"]);
}

/// The match is on the IP only: the station may INVITE from another source port than it
/// REGISTERed from.
#[tokio::test]
async fn s1_the_same_ip_from_another_port_rings() {
    let h = Harness::with(strict_two_hosts()).await;
    register_station(&h).await;
    let again = Station::on(h.s.sip_addr(), "127.0.0.1").await;
    assert_ne!(again.me.port(), h.st.me.port());
    again.invite("c-port", 1, &again.sdp("narrow")).await;
    let r = again.collect(ms(300)).await;
    assert!(has(&r, "100") && has(&r, "180"), "{:?}", codes(&r));
}

/// A rogue host that reuses the live call's Call-ID gets a 403 (not the re-answer that carries
/// the call's tag), and the call is untouched.
#[tokio::test]
async fn s1_a_rogue_host_cannot_reuse_the_live_call_id() {
    let h = Harness::with(strict_two_hosts()).await;
    register_station(&h).await;
    h.st.invite("call-LIVE", 1, &h.st.sdp("narrow")).await;
    assert!(has(&h.st.collect(ms(300)).await, "180"));
    let other = Station::on(h.s.sip_addr(), "127.0.0.2").await;
    other.invite("call-LIVE", 2, &other.sdp("narrow")).await;
    assert_eq!(codes(&other.collect(ms(300)).await), vec!["403"]);
    assert_eq!(h.s.snapshot().calls, 1);
}

/// A retransmitted INVITE of the live call is re-answered from its own station even when the
/// registration lapsed while it rang; another host still is not.
#[tokio::test]
async fn s1_a_retransmission_of_the_live_call_survives_a_lapsed_registration() {
    let h = Harness::with(Opts {
        ring: Some(ms(5000)),
        ..strict_two_hosts()
    })
    .await;
    h.s.set_reg_ttl(ms(900));
    register_station(&h).await;
    h.st.invite("call-LONG", 1, &h.st.sdp("narrow")).await;
    assert!(has(&h.st.collect(ms(100)).await, "180"));
    sleep_ms(700).await;
    h.st.invite("call-LONG", 1, &h.st.sdp("narrow")).await;
    let r = h.st.collect(ms(300)).await;
    assert!(has(&r, "100") && has(&r, "180") && !has(&r, "403"), "{:?}", codes(&r));
    // a NEW call from the lapsed station is refused
    h.st.invite("call-NEW", 1, &h.st.sdp("narrow")).await;
    assert_eq!(codes(&h.st.collect(ms(300)).await), vec!["403"]);
}

/// agy #335 finding 1: another host that became the registered station meanwhile cannot take
/// the live call's retransmission (it is answered only to the host that started the call).
#[tokio::test]
async fn s1_a_new_station_cannot_take_over_the_live_call_by_its_call_id() {
    let h = Harness::with(Opts {
        ring: Some(ms(5000)),
        ..strict_two_hosts()
    })
    .await;
    register_station(&h).await;
    h.st.invite("call-A", 1, &h.st.sdp("narrow")).await;
    assert!(has(&h.st.collect(ms(200)).await, "180"));
    // 127.0.0.2 registers (it is now the one binding) and replays station A's Call-ID
    let b = Station::on(h.s.sip_addr(), "127.0.0.2").await;
    b.register("").await;
    let m401 = find(&b.collect(ms(300)).await, "401").expect("no challenge");
    b.register(&digest_auth(&m401, "8001", "s3cretpass")).await;
    assert!(find(&b.collect(ms(300)).await, "200").is_some());
    b.invite("call-A", 2, &b.sdp("narrow")).await;
    assert_eq!(codes(&b.collect(ms(300)).await), vec!["486"]);
}
