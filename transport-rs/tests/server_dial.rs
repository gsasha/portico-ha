//! The ports of the 26 tests of `dial_test.go` that place the call (`TestDialAnswersWithoutA
//! ChallengeAndTalksBothWays` to `TestTheDialAddsNoHTTPOrHomeAssistantPath`), against the Rust
//! server and a fake station on 127.0.0.1, and the tests of what the port adds (the dial future
//! dropped mid-ring, the nonce bound to its host, the dial-only counters). Nothing here reaches
//! anything but 127.0.0.0/8. The Go test name is in a comment on each test.

mod common;

use std::time::Duration;

use common::*;
use serde_json::json;
use tokio::task::JoinHandle;
use transport_rs::cancel::CancelToken;
use transport_rs::driver::gatesipserver::DialTimers;
use transport_rs::driver::{
    AudioClaimer, CallAudio, CallDialer, CallHangupper, ConnectAnswerer, Driver, DriverError,
    MediaTarget, Sentinel,
};
use transport_rs::sip::{verify_digest_authorization, DigestResult, NonceStore};

/// The `dh` of dial_test.go: a harness with `dial_enabled`, the station, and the messages the
/// station has read from its socket and not yet consumed.
struct Dh {
    h: Harness,
    pend: Vec<Msg>,
}

/// A running `DialCall`.
struct Dial {
    jh: Option<JoinHandle<Result<(), DriverError>>>,
    done: Option<Result<(), DriverError>>,
    ctx: CancelToken,
}

impl Dial {
    /// `result`: the outcome of `DialCall`, waiting up to `within`; repeatable.
    async fn result(&mut self, within: Duration) -> Result<(), DriverError> {
        if let Some(r) = &self.done {
            return r.clone();
        }
        let jh = self.jh.as_mut().expect("a dial");
        match tokio::time::timeout(within, jh).await {
            Ok(r) => {
                let r = r.expect("DialCall panicked");
                self.done = Some(r.clone());
                r
            }
            Err(_) => panic!("DialCall did not return"),
        }
    }
}

fn dial_cfg() -> Opts {
    Opts::cfg(json!({"dial_enabled": true}))
}

impl Dh {
    /// `newDialH` with the defaults of the Go helper (`dialRing` 2 s, `cancelW` 400 ms).
    async fn new() -> Dh {
        Dh::with(dial_cfg(), DialTimers::default()).await
    }

    async fn tuned(t: DialTimers) -> Dh {
        Dh::with(dial_cfg(), t).await
    }

    async fn with(o: Opts, t: DialTimers) -> Dh {
        let h = Harness::with(o).await;
        h.s.set_dial_timers(DialTimers {
            ring: Some(t.ring.unwrap_or(ms(2000))),
            cancel_wait: Some(t.cancel_wait.unwrap_or(ms(400))),
            cap: t.cap,
            attach: t.attach,
        });
        Dh { h, pend: vec![] }
    }

    /// `dh.register`
    async fn register(&mut self) {
        self.h.st.register("").await;
        let (m401, _) = self.await_("401", ms(2000)).await;
        let m401 = m401.expect("no challenge");
        self.h
            .st
            .register(&digest_auth(&m401, "8001", "s3cretpass"))
            .await;
        assert!(
            self.await_("200", ms(2000)).await.0.is_some(),
            "register refused"
        );
    }

    /// `dh.await`: the first not-yet-consumed message from the station matching `want` (a method
    /// or a status code), reading its socket for up to `within`. Messages before it stay queued;
    /// the second value is everything read during this call, in order.
    async fn await_(&mut self, want: &str, within: Duration) -> (Option<Msg>, Vec<Msg>) {
        fn pick(pend: &mut Vec<Msg>, want: &str) -> Option<Msg> {
            let i = pend
                .iter()
                .position(|m| m.method == want || (m.code != 0 && m.code.to_string() == want))?;
            Some(pend.remove(i))
        }
        let mut all = Vec::new();
        if let Some(m) = pick(&mut self.pend, want) {
            return (Some(m), all);
        }
        let end = std::time::Instant::now() + within;
        while std::time::Instant::now() < end {
            let ms_ = self.h.st.collect(ms(20)).await;
            all.extend(ms_.clone());
            self.pend.extend(ms_);
            if let Some(m) = pick(&mut self.pend, want) {
                return (Some(m), all);
            }
        }
        (None, all)
    }

    /// `h.dial`
    fn dial(&self, ctx: CancelToken, id: &str) -> Dial {
        let d = self.h.d.clone();
        let (c2, id2) = (ctx.clone(), id.to_string());
        let jh = tokio::spawn(async move { d.dial_call(&c2, &id2, MediaTarget::default()).await });
        Dial {
            jh: Some(jh),
            done: None,
            ctx,
        }
    }

    fn dial_plain(&self, id: &str) -> Dial {
        self.dial(CancelToken::new(), id)
    }

    /// `h.reply`: answers an INVITE-shaped request from the station's side.
    async fn reply(
        &self,
        req: &Msg,
        code: u16,
        reason: &str,
        to_tag: &str,
        extra: &[String],
        body: &str,
    ) {
        let mut to = req.header("To");
        if !to_tag.is_empty() && !to.contains(";tag=") {
            to.push_str(&format!(";tag={to_tag}"));
        }
        let mut l = vec![
            format!("SIP/2.0 {code} {reason}"),
            format!("Via: {}", req.header("Via")),
            format!("From: {}", req.header("From")),
            format!("To: {to}"),
            format!("Call-ID: {}", req.header("Call-ID")),
            format!("CSeq: {}", req.header("CSeq")),
            format!("Contact: <sip:9901@{}>", self.h.st.me),
        ];
        l.extend(extra.iter().cloned());
        if !body.is_empty() {
            l.push("Content-Type: application/sdp".to_string());
        }
        l.push(format!("Content-Length: {}", body.len()));
        self.h
            .st
            .send(&format!("{}\r\n\r\n{body}", l.join("\r\n")))
            .await;
    }

    /// `h.answer`: brings a dial up: INVITE in, 100/180/200 out, ACK in, DialCall returns.
    async fn answer(&mut self, d: &mut Dial, kind: &str) -> Msg {
        let inv = self
            .await_("INVITE", ms(2000))
            .await
            .0
            .expect("the station never got an INVITE");
        self.reply(&inv, 100, "Trying", "", &[], "").await;
        self.reply(&inv, 180, "Ringing", "st1", &[], "").await;
        self.reply(&inv, 200, "OK", "st1", &[], &self.h.st.sdp(kind))
            .await;
        assert!(
            self.await_("ACK", ms(2000)).await.0.is_some(),
            "no ACK for the 200"
        );
        d.result(ms(2000)).await.unwrap();
        inv
    }

    async fn send_rtp(&self, pt: u8, seq: u16, payload: &[u8]) {
        self.h
            .st
            .rtp
            .send_to(
                &rtp_packet(pt, seq, u32::from(seq) * 320, payload),
                self.h.rtp_addr(),
            )
            .await
            .unwrap();
    }

    /// The events of the sink without the capability republishes (`capSink` keeps those apart).
    fn events(&self) -> Vec<String> {
        self.h
            .sink
            .events()
            .into_iter()
            .filter(|e| !e.starts_with("caps:"))
            .collect()
    }

    /// `h.sink.waitEvents`
    async fn wait_events(&self, want: &[&str]) {
        if want.is_empty() {
            // "nothing was reported" is true on the first poll, so it would not wait for a late
            // event: settle first (Go's tests add a 100 ms sleep and a second comparison, R2-93)
            sleep_ms(100).await;
        }
        let end = std::time::Instant::now() + Duration::from_secs(3);
        let mut got = Vec::new();
        while std::time::Instant::now() < end {
            got = self.events();
            if got == want {
                return;
            }
            sleep_ms(10).await;
        }
        panic!("events:\n got {got:?}\nwant {want:?}");
    }

    async fn read_dial_rtp(&self, within: Duration) -> Vec<u8> {
        self.h
            .st
            .read_rtp(within)
            .await
            .map(|(_, p)| p)
            .unwrap_or_default()
    }

    fn claimed(&self, id: &str) -> Option<std::sync::Arc<dyn CallAudio>> {
        self.h.d.claimed_audio(id)
    }

    fn station_bye(&self, call: &str, cseq: u32, inv: &Msg) -> String {
        format!(
            "BYE sip:8001@127.0.0.1:{} SIP/2.0\r\nVia: SIP/2.0/UDP {};branch=z9hG4bKbye\r\n\
             From: <sip:9901@127.0.0.1>;tag=st1\r\nTo: {}\r\nCall-ID: {call}\r\nCSeq: {cseq} BYE\r\nContent-Length: 0\r\n\r\n",
            self.h.s.sip_addr().port(),
            self.h.st.me,
            inv.header("From")
        )
    }
}

fn no_ringing(ev: &[String]) {
    assert!(
        !ev.iter().any(|e| e.starts_with("ringing:")),
        "EventSink.Ringing was called for our own call: {ev:?}"
    );
}

async fn wait_for(what: &str, f: impl Fn() -> bool) {
    let end = std::time::Instant::now() + Duration::from_secs(3);
    while std::time::Instant::now() < end {
        if f() {
            return;
        }
        sleep_ms(10).await;
    }
    panic!("timed out waiting for {what}");
}

fn refused(e: &DriverError) -> bool {
    e.message().contains("the station refused the call")
}

/// The wideband tone the station sends, 40 ms of 440 Hz.
fn tone440() -> Vec<u8> {
    tone(640, 16000.0, 440.0, 3000.0)
}

// TestDialAnswersWithoutAChallengeAndTalksBothWays
#[tokio::test]
async fn dial_answers_without_a_challenge_and_talks_both_ways() {
    let mut h = Dh::new().await;
    h.register().await;
    let mut d = h.dial_plain("out-aa11");
    let inv = h.answer(&mut d, "wide").await;

    // The INVITE is gate-sip's shape.
    let raw = &inv.raw;
    assert!(
        raw.starts_with("INVITE sip:9901@127.0.0.1 SIP/2.0\r\n"),
        "request line: {:?}",
        raw.split("\r\n").next()
    );
    assert_eq!(inv.header("Call-ID"), "out-aa11");
    assert!(
        inv.header("From").starts_with("<sip:8001@127.0.0.1>;tag="),
        "From {:?}",
        inv.header("From")
    );
    assert_eq!(inv.header("To"), "<sip:9901@127.0.0.1>");
    assert!(
        raw.lines().any(|l| l.starts_with("CSeq: 1 INVITE"))
            && inv.header("Via").contains("branch=z9hG4bK"),
        "CSeq/Via: {raw:?}"
    );
    let body = inv.body();
    assert!(
        body.contains(&format!("m=audio {} RTP/AVP 96 8", h.h.s.rtp_port()))
            && body.find("a=rtpmap:96 PCM/16000") < body.find("a=rtpmap:8 PCMA/8000")
            && !body.contains("L16")
            && body.contains("c=IN IP4 127.0.0.1"),
        "offer: {body:?}"
    );
    assert!(
        !raw.contains("Authorization"),
        "credentials sent unprompted"
    );

    h.wait_events(&["answered:out-aa11"]).await;
    let a = h
        .claimed("out-aa11")
        .expect("no audio for the dialled call");
    assert!(
        h.claimed("other").is_none(),
        "audio handed out for another id"
    );
    assert!(
        !ConnectAnswerer::ringing_call(&*h.h.d, "out-aa11"),
        "a dialled call reads as a ring to answer"
    );
    a.attach();
    assert!(
        h.claimed("out-aa11").is_none(),
        "the audio was handed out twice"
    );

    // Station -> us: wideband passes through, x4 gain on the way to the browser.
    h.send_rtp(96, 1, &tone440()).await;
    h.wait_events(&["answered:out-aa11", "active:out-aa11"])
        .await;
    let mut got: Vec<u8> = Vec::new();
    let out = a.output();
    let mut rx = out.lock().await;
    while got.len() < 1280 {
        let f = tokio::time::timeout(ms(1000), rx.recv())
            .await
            .expect("no audio out")
            .expect("audio closed");
        got.extend_from_slice(&f);
        h.send_rtp(96, got.len() as u16, &tone440()).await;
    }
    drop(rx);
    assert!(
        goertzel(&got[..1280], 16000.0, 440.0) >= 1e9,
        "440 Hz tone not recovered from the station's audio"
    );
    // Us -> station: a tone from the browser arrives as 16 kHz RTP with payload type 96.
    for _ in 0..2 {
        a.input()
            .send(tone(640, 16000.0, 1000.0, 8000.0))
            .await
            .unwrap();
    }
    let mut heard = false;
    for _ in 0..80 {
        let p = h.read_dial_rtp(ms(100)).await;
        if p.len() == 640 && goertzel(&p, 16000.0, 1000.0) > 1e9 {
            heard = true;
            break;
        }
    }
    assert!(heard, "the browser's tone never reached the station");

    // The station hangs up.
    h.h.st.send(&h.station_bye("out-aa11", 7, &inv)).await;
    assert!(
        h.await_("200", ms(1000)).await.0.is_some(),
        "the station's BYE got no 200"
    );
    h.wait_events(&["answered:out-aa11", "active:out-aa11", "ended:out-aa11"])
        .await;
    let held = h.h.d.held_audio().expect("held audio");
    wait_for("the audio to end with the call", || held.over()).await;
    no_ringing(&h.events());
    let st = h.h.s.snapshot();
    assert!(
        !st.in_call && !st.dialing && !st.ringing,
        "slot not free: {st:?}"
    );
}

// TestDialAnswersAnINVITEChallengeWithTheRegistrationCredentials
#[tokio::test]
async fn dial_answers_an_invite_challenge_with_the_registration_credentials() {
    for (code, challenge, reply) in [
        (401u16, "WWW-Authenticate", "Authorization"),
        (407, "Proxy-Authenticate", "Proxy-Authorization"),
    ] {
        let mut h = Dh::new().await;
        h.register().await;
        let mut d = h.dial_plain("out-c1");
        let inv1 = h.await_("INVITE", ms(2000)).await.0.expect("no INVITE");
        let ns = NonceStore::new();
        let nonce = ns.issue();
        h.reply(
            &inv1,
            code,
            "Unauthorized",
            "st1",
            &[format!(
                r#"{challenge}: Digest realm="VDP", nonce="{nonce}", algorithm=MD5"#
            )],
            "",
        )
        .await;
        // The 4xx is ACKed (same CSeq), then the INVITE comes again with credentials.
        let (ack, all) = h.await_("ACK", ms(2000)).await;
        let ack = ack
            .unwrap_or_else(|| panic!("the challenge was not ACKed properly: {:?}", codes(&all)));
        assert!(
            ack.raw.contains("CSeq: 1 ACK") && ack.header("To").contains("tag=st1"),
            "the challenge was not ACKed properly: {:?}",
            ack.raw
        );
        let inv2 = h
            .await_("INVITE", ms(2000))
            .await
            .0
            .expect("no second INVITE");
        assert!(
            inv2.header("Call-ID") == "out-c1" && !inv2.header("From").contains("st1"),
            "not the same dialog"
        );
        assert!(
            inv2.raw.contains("CSeq: 2 INVITE") && inv2.header("Via") != inv1.header("Via"),
            "CSeq/branch: {:?}",
            inv2.raw
        );
        let auth = inv2.header(reply);
        assert!(!auth.is_empty(), "no {reply} header: {:?}", inv2.raw);
        assert_eq!(
            verify_digest_authorization(&ns, &auth, "INVITE", "VDP", "s3cretpass"),
            DigestResult::Ok,
            "digest for {auth:?}"
        );
        assert!(
            auth.contains(r#"username="8001""#)
                && auth.contains(r#"uri="sip:9901@127.0.0.1""#)
                && !auth.contains("s3cretpass"),
            "auth header: {auth:?}"
        );
        h.reply(&inv2, 200, "OK", "st1", &[], &h.h.st.sdp("wide"))
            .await;
        assert!(h.await_("ACK", ms(2000)).await.0.is_some(), "no ACK");
        d.result(ms(2000)).await.unwrap();
        h.wait_events(&["answered:out-c1"]).await;
    }
}

// R1-5 (server half, PARITY 349): the challenge is read as the bytes the station sent and the
// answer echoes them. A Latin-1 realm (0xE9, not UTF-8) must come back as that same byte inside
// `realm="..."`, and the response must be the digest over those bytes (as Go hashes them), not
// over U+FFFD.
#[tokio::test]
async fn a_latin_1_realm_in_an_invite_challenge_is_answered_over_its_raw_bytes() {
    let mut h = Dh::new().await;
    h.register().await;
    let mut d = h.dial_plain("out-l1");
    let inv1 = h.await_("INVITE", ms(2000)).await.0.expect("no INVITE");
    let ns = NonceStore::new();
    let nonce = ns.issue();
    let text = format!(
        "SIP/2.0 401 Unauthorized\r\nVia: {}\r\nFrom: {}\r\nTo: {};tag=st1\r\nCall-ID: {}\r\nCSeq: {}\r\nWWW-Authenticate: Digest realm=\"VDP@@\", nonce=\"{nonce}\", algorithm=MD5\r\nContent-Length: 0\r\n\r\n",
        inv1.header("Via"),
        inv1.header("From"),
        inv1.header("To"),
        inv1.header("Call-ID"),
        inv1.header("CSeq"),
    );
    let at = text.find("@@").unwrap();
    let mut wire = text.as_bytes()[..at].to_vec();
    wire.push(0xe9); // Latin-1 e-acute
    wire.extend_from_slice(&text.as_bytes()[at + 2..]);
    h.h.st.send_bytes(&wire).await;
    assert!(h.await_("ACK", ms(2000)).await.0.is_some(), "no ACK");
    let inv2 = h
        .await_("INVITE", ms(2000))
        .await
        .0
        .expect("no second INVITE");
    assert!(
        inv2.bytes.windows(12).any(|w| w == b"realm=\"VDP\xe9\""),
        "the realm was not echoed as the raw byte: {:?}",
        inv2.raw
    );
    let ha1 = md5hex_bytes(b"8001:VDP\xe9:s3cretpass");
    let ha2 = md5hex("INVITE:sip:9901@127.0.0.1");
    let want = md5hex(&format!("{ha1}:{nonce}:{ha2}"));
    let auth = inv2.header("Authorization");
    assert!(
        auth.contains(&format!("response=\"{want}\"")),
        "the digest was not computed over the raw realm: {auth:?}"
    );
    h.reply(&inv2, 200, "OK", "st1", &[], &h.h.st.sdp("wide"))
        .await;
    assert!(h.await_("ACK", ms(2000)).await.0.is_some(), "no ACK");
    d.result(ms(2000)).await.unwrap();
}

// TestDialGivesUpWhenTheChallengeIsRepeated
#[tokio::test]
async fn dial_gives_up_when_the_challenge_is_repeated() {
    let mut h = Dh::new().await;
    h.register().await;
    let mut d = h.dial_plain("out-c2");
    for i in 0..2 {
        let inv = h
            .await_("INVITE", ms(2000))
            .await
            .0
            .unwrap_or_else(|| panic!("INVITE {} missing", i + 1));
        h.reply(
            &inv,
            401,
            "Unauthorized",
            "st1",
            &[format!(
                r#"WWW-Authenticate: Digest realm="VDP", nonce="n{i}""#
            )],
            "",
        )
        .await;
    }
    let e = d.result(ms(2000)).await.unwrap_err();
    assert!(refused(&e) && e.message().contains("credentials"), "{e}");
    assert!(
        !e.message().contains("s3cretpass"),
        "the password is in the error"
    );
    h.wait_events(&[]).await;
    assert!(
        h.await_("INVITE", ms(300)).await.0.is_none(),
        "a third INVITE (a loop)"
    );
}

// TestDialUnanswerableChallengeIsARefusal
#[tokio::test]
async fn dial_unanswerable_challenge_is_a_refusal() {
    let mut h = Dh::new().await;
    h.register().await;
    let mut d = h.dial_plain("out-c3");
    let inv = h.await_("INVITE", ms(2000)).await.0.unwrap();
    h.reply(
        &inv,
        401,
        "Unauthorized",
        "st1",
        &[r#"WWW-Authenticate: Digest realm="VDP", nonce="n", algorithm=SHA-256"#.to_string()],
        "",
    )
    .await;
    let e = d.result(ms(2000)).await.unwrap_err();
    assert!(refused(&e), "{e}");
}

// TestStationRefusalsAreErrorsWithNoEventsAndFreeTheSlot
#[tokio::test]
async fn station_refusals_are_errors_with_no_events_and_free_the_slot() {
    for code in [403u16, 486, 480, 603, 500] {
        let mut h = Dh::new().await;
        h.register().await;
        let mut d = h.dial_plain("out-r");
        let inv = h.await_("INVITE", ms(2000)).await.0.unwrap();
        h.reply(&inv, 180, "Ringing", "st1", &[], "").await;
        h.reply(&inv, code, "No", "st1", &[], "").await;
        let e = d.result(ms(2000)).await.unwrap_err();
        assert!(
            refused(&e) && e.message().contains(&code.to_string()),
            "{code}: {e}"
        );
        let ack = h.await_("ACK", ms(1000)).await.0;
        assert!(
            ack.is_some_and(|a| a.header("To").contains("tag=st1")),
            "{code}: the refusal was not ACKed"
        );
        h.wait_events(&[]).await; // nothing: a call that never came up has no id to follow
                                  // The slot is free again: an inbound ring is accepted.
        h.h.st.invite("in-1", 1, &h.h.st.sdp("wide")).await;
        let want = format!("ringing:in-1@{}", h.h.device_id());
        h.wait_events(&[&want]).await;
    }
}

fn branch_of(m: &Msg) -> String {
    let v = m.header("Via");
    let i = v.find("branch=z9hG4bK").unwrap() + 7;
    v[i..]
        .split(|c: char| c == ';' || c.is_whitespace())
        .next()
        .unwrap()
        .to_string()
}

// TestNoAnswerCancelsAndThe487IsAcked
#[tokio::test]
async fn no_answer_cancels_and_the_487_is_acked() {
    let mut h = Dh::tuned(DialTimers {
        ring: Some(ms(300)),
        ..Default::default()
    })
    .await;
    h.register().await;
    let t0 = std::time::Instant::now();
    let mut d = h.dial_plain("out-n");
    let inv = h.await_("INVITE", ms(2000)).await.0.unwrap();
    h.reply(&inv, 180, "Ringing", "st1", &[], "").await;
    let can = h
        .await_("CANCEL", ms(2000))
        .await
        .0
        .expect("no CANCEL after the ring timeout");
    // R2-23: the text "within 300ms" is not the time; the CANCEL must not come early either
    // (a timer armed at 3/4 of the ring time would cancel at 225 ms)
    assert!(t0.elapsed() >= ms(270), "CANCEL after {:?}", t0.elapsed());
    assert!(
        can.header("Call-ID") == "out-n"
            && can.raw.contains("CSeq: 1 CANCEL")
            && !can.header("Via").is_empty()
            && can.raw.contains(&format!("branch={}", branch_of(&inv))),
        "CANCEL: {:?}",
        can.raw
    );
    h.reply(&can, 200, "OK", "", &[], "").await;
    h.reply(&inv, 487, "Request Terminated", "st1", &[], "")
        .await;
    let e = d.result(ms(2000)).await.unwrap_err();
    assert!(
        e.message()
            .contains("the station did not answer within 300ms"),
        "{e}"
    );
    assert!(
        h.await_("ACK", ms(1000)).await.0.is_some(),
        "the 487 was not ACKed"
    );
    h.wait_events(&[]).await;
}

// TestSilenceFromTheStationAtAllIsANoAnswer
#[tokio::test]
async fn silence_from_the_station_at_all_is_a_no_answer() {
    let mut h = Dh::tuned(DialTimers {
        ring: Some(ms(200)),
        cancel_wait: Some(ms(100)),
        ..Default::default()
    })
    .await;
    h.register().await;
    let mut d = h.dial_plain("out-s");
    let e = d.result(ms(2000)).await.unwrap_err();
    assert!(e.message().contains("the station did not answer"), "{e}");
    assert!(h.await_("CANCEL", ms(1000)).await.0.is_some(), "no CANCEL");
    h.wait_events(&[]).await;
    let st = h.h.s.snapshot();
    assert!(!st.dialing && !st.in_call, "{st:?}");
}

// TestCancelBeforeAnswerSendsCancelAndReportsNothing
#[tokio::test]
async fn cancel_before_answer_sends_cancel_and_reports_nothing() {
    let mut h = Dh::new().await;
    h.register().await;
    let mut d = h.dial(CancelToken::new(), "out-x");
    let inv = h.await_("INVITE", ms(2000)).await.0.unwrap();
    h.reply(&inv, 180, "Ringing", "st1", &[], "").await;
    d.ctx.cancel();
    assert!(h.await_("CANCEL", ms(2000)).await.0.is_some(), "no CANCEL");
    h.reply(&inv, 487, "Request Terminated", "st1", &[], "")
        .await;
    let e = d.result(ms(2000)).await.unwrap_err();
    assert!(e.message().contains("the dial was cancelled"), "{e}");
    h.wait_events(&[]).await;
}

// TestAnAnswerThatCrossesTheCancelIsAckedAndHungUp
#[tokio::test]
async fn an_answer_that_crosses_the_cancel_is_acked_and_hung_up() {
    let mut h = Dh::new().await;
    h.register().await;
    let mut d = h.dial(CancelToken::new(), "out-y");
    let inv = h.await_("INVITE", ms(2000)).await.0.unwrap();
    d.ctx.cancel();
    assert!(h.await_("CANCEL", ms(2000)).await.0.is_some(), "no CANCEL");
    h.reply(&inv, 200, "OK", "st1", &[], &h.h.st.sdp("wide"))
        .await; // the 200 won the race
    let (bye, all) = h.await_("BYE", ms(2000)).await;
    assert!(
        bye.is_some() && has(&all, "ACK"),
        "expected ACK then BYE, got {:?}",
        codes(&all)
    );
    let e = d.result(ms(2000)).await.unwrap_err();
    assert!(e.message().contains("the dial was cancelled"), "{e}");
    h.wait_events(&[]).await; // never reported as a call
    assert!(!h.h.s.snapshot().in_call);
}

// TestAnAnswerWithNoUsableMediaIsByedAndRefused
#[tokio::test]
async fn an_answer_with_no_usable_media_is_byed_and_refused() {
    type Sdp = fn(&Station) -> String;
    let cases: [(&str, Sdp); 2] = [
        ("a codec we did not offer", |st| st.sdp("pcmuonly")),
        ("no media port", |st| {
            st.sdp("wide")
                .replacen(&format!("m=audio {}", st.rtp_port()), "m=audio 0", 1)
        }),
    ];
    for (name, sdp) in cases {
        let mut h = Dh::new().await;
        h.register().await;
        let mut d = h.dial_plain("out-m");
        let inv = h.await_("INVITE", ms(2000)).await.0.unwrap();
        h.reply(&inv, 200, "OK", "st1", &[], &sdp(&h.h.st)).await;
        let (bye, all) = h.await_("BYE", ms(2000)).await;
        assert!(
            bye.is_some() && has(&all, "ACK"),
            "{name}: expected ACK and BYE, got {:?}",
            codes(&all)
        );
        let e = d.result(ms(2000)).await.unwrap_err();
        assert!(refused(&e), "{name}: {e}");
        h.wait_events(&[]).await;
    }
}

// TestANarrowbandAnswerIsTranscoded
#[tokio::test]
async fn a_narrowband_answer_is_transcoded() {
    let mut h = Dh::new().await;
    h.register().await;
    let mut d = h.dial_plain("out-nb");
    h.answer(&mut d, "narrow").await;
    let a = h.claimed("out-nb").unwrap();
    a.attach();
    h.send_rtp(8, 1, &[0xD5; 160]).await;
    h.wait_events(&["answered:out-nb", "active:out-nb"]).await;
    for _ in 0..2 {
        a.input()
            .send(tone(640, 16000.0, 1000.0, 8000.0))
            .await
            .unwrap();
    }
    let mut ok = false;
    for _ in 0..80 {
        let p = h.read_dial_rtp(ms(100)).await;
        if p.len() == 160 && p[0] != 0xD5 {
            ok = true; // A-law frames, not silence
            break;
        }
    }
    assert!(ok, "no A-law audio reached the station");
}

// TestInboundInviteWhileDiallingOrTalkingGets486AndNeverRings
#[tokio::test]
async fn inbound_invite_while_dialling_or_talking_gets_486_and_never_rings() {
    let mut h = Dh::new().await;
    h.register().await;
    let mut d = h.dial_plain("out-b");
    let inv = h.await_("INVITE", ms(2000)).await.0.unwrap();
    h.reply(&inv, 180, "Ringing", "st1", &[], "").await;
    // While ringing out.
    h.h.st.invite("in-1", 1, &h.h.st.sdp("wide")).await;
    assert!(
        h.await_("486", ms(1000)).await.0.is_some(),
        "no 486 while dialling"
    );
    // Same Call-ID as ours is not a retransmission to re-answer either.
    h.h.st.invite("out-b", 1, &h.h.st.sdp("wide")).await;
    assert!(
        h.await_("486", ms(1000)).await.0.is_some(),
        "no 486 for an INVITE reusing our Call-ID"
    );
    h.reply(&inv, 200, "OK", "st1", &[], &h.h.st.sdp("wide"))
        .await;
    h.await_("ACK", ms(1000)).await;
    d.result(ms(2000)).await.unwrap();
    // While talking.
    h.h.st.invite("in-2", 1, &h.h.st.sdp("wide")).await;
    assert!(
        h.await_("486", ms(1000)).await.0.is_some(),
        "no 486 while talking"
    );
    h.wait_events(&["answered:out-b"]).await;
    no_ringing(&h.events());
    let st = h.h.s.snapshot();
    assert_eq!(
        st.calls, 0,
        "a refused INVITE was counted as a call: {st:?}"
    );
    // Then it ends and an inbound call rings normally.
    h.h.d.hangup_call("out-b").unwrap();
    h.wait_events(&["answered:out-b", "ended:out-b"]).await;
    h.h.st.invite("in-3", 1, &h.h.st.sdp("wide")).await;
    let ring = format!("ringing:in-3@{}", h.h.device_id());
    h.wait_events(&["answered:out-b", "ended:out-b", &ring])
        .await;
}

// TestHangupCallSendsTheByeOfTheCallerAndEndsOnce
#[tokio::test]
async fn hangup_call_sends_the_bye_of_the_caller_and_ends_once() {
    let mut h = Dh::new().await;
    h.register().await;
    let mut d = h.dial_plain("out-h");
    let inv = h.answer(&mut d, "wide").await;
    let e = h.h.d.hangup_call("nope").unwrap_err();
    assert!(e.is(Sentinel::NoSuchCall), "{e}");
    h.h.d.hangup_call("out-h").unwrap();
    let bye = h.await_("BYE", ms(2000)).await.0.expect("no BYE");
    assert!(
        bye.header("From") == inv.header("From")
            && bye.header("To") == format!("{};tag=st1", inv.header("To"))
            && bye.header("Call-ID") == "out-h"
            && bye.raw.contains("CSeq: 2 BYE")
            && bye.raw.starts_with("BYE sip:9901@127.0.0.1 SIP/2.0"),
        "BYE shape: {:?}",
        bye.raw
    );
    h.wait_events(&["answered:out-h", "ended:out-h"]).await;
    let e = h.h.d.hangup_call("out-h").unwrap_err();
    assert!(e.is(Sentinel::NoSuchCall), "second hang-up: {e}");
    sleep_ms(100).await;
    h.wait_events(&["answered:out-h", "ended:out-h"]).await;
}

// TestTheClientLegLeavingHangsUpAtOnce
#[tokio::test]
async fn the_client_leg_leaving_hangs_up_at_once() {
    let mut h = Dh::new().await;
    h.register().await;
    let mut d = h.dial_plain("out-d");
    h.answer(&mut d, "wide").await;
    let a = h.claimed("out-d").unwrap();
    a.attach();
    a.detach();
    assert!(
        h.await_("BYE", ms(1000)).await.0.is_some(),
        "no BYE on Detach"
    );
    h.wait_events(&["answered:out-d", "ended:out-d"]).await;
}

// TestNobodyTakingTheAudioHangsTheCallUp
#[tokio::test]
async fn nobody_taking_the_audio_hangs_the_call_up() {
    let mut h = Dh::tuned(DialTimers {
        attach: Some(ms(250)),
        ..Default::default()
    })
    .await;
    h.register().await;
    let mut d = h.dial_plain("out-t");
    h.answer(&mut d, "wide").await;
    assert!(
        h.await_("BYE", ms(2000)).await.0.is_some(),
        "a call nobody joined was kept"
    );
    h.wait_events(&["answered:out-t", "ended:out-t"]).await;
}

// TestAClientLegThatAttachedKeepsTheCallPastTheAttachWindow
#[tokio::test]
async fn a_client_leg_that_attached_keeps_the_call_past_the_attach_window() {
    let mut h = Dh::tuned(DialTimers {
        attach: Some(ms(250)),
        ..Default::default()
    })
    .await;
    h.register().await;
    let mut d = h.dial_plain("out-k");
    h.answer(&mut d, "wide").await;
    let a = h.claimed("out-k").unwrap();
    a.attach();
    assert!(
        h.await_("BYE", ms(600)).await.0.is_none(),
        "a joined call was hung up"
    );
    assert!(h.h.s.snapshot().in_call, "call gone");
}

// TestTheCallIsCappedFromTheAnswer
#[tokio::test]
async fn the_call_is_capped_from_the_answer() {
    let mut h = Dh::tuned(DialTimers {
        cap: Some(ms(300)),
        ..Default::default()
    })
    .await;
    h.register().await;
    let mut d = h.dial_plain("out-cap");
    h.answer(&mut d, "wide").await;
    let a = h.claimed("out-cap").unwrap();
    a.attach();
    assert!(
        h.await_("BYE", ms(2000)).await.0.is_some(),
        "no BYE at the cap"
    );
    h.wait_events(&["answered:out-cap", "ended:out-cap"]).await;
    let held = h.h.d.held_audio().unwrap();
    wait_for("the audio to end with the cap", || held.over()).await;
}

// TestStrictRTPSourceRuleHoldsForADialledCall
#[tokio::test]
async fn strict_rtp_source_rule_holds_for_a_dialled_call() {
    let mut h = Dh::new().await;
    h.register().await;
    let mut d = h.dial_plain("out-rtp");
    let inv = h.await_("INVITE", ms(2000)).await.0.unwrap();
    // The answer names 127.0.0.2 as the media host; the packets come from 127.0.0.1.
    let sdp =
        h.h.st
            .sdp("wide")
            .replacen("c=IN IP4 127.0.0.1", "c=IN IP4 127.0.0.2", 1);
    h.reply(&inv, 200, "OK", "st1", &[], &sdp).await;
    h.await_("ACK", ms(1000)).await;
    d.result(ms(2000)).await.unwrap();
    h.send_rtp(96, 1, &tone440()).await;
    wait_for("rejection", || h.h.s.snapshot().rtp_rejected == 1).await;
    let st = h.h.s.snapshot();
    assert_eq!(st.rtp_in, 0, "{st:?}");
    h.wait_events(&["answered:out-rtp"]).await; // no `active` from a stranger's media
}

// TestRTPBeforeTheAnswerIsNotBelieved
#[tokio::test]
async fn rtp_before_the_answer_is_not_believed() {
    let mut h = Dh::new().await;
    h.register().await;
    let mut d = h.dial_plain("out-early");
    let inv = h.await_("INVITE", ms(2000)).await.0.unwrap();
    h.send_rtp(96, 1, &tone440()).await;
    wait_for("rejection", || h.h.s.snapshot().rtp_rejected == 1).await;
    h.reply(&inv, 486, "Busy Here", "st1", &[], "").await;
    let _ = d.result(ms(2000)).await;
    h.wait_events(&[]).await;
}

// TestRetransmitted200IsAckedAgain
#[tokio::test]
async fn retransmitted_200_is_acked_again() {
    let mut h = Dh::new().await;
    h.register().await;
    let mut d = h.dial_plain("out-re");
    let inv = h.answer(&mut d, "wide").await;
    h.reply(&inv, 200, "OK", "st1", &[], &h.h.st.sdp("wide"))
        .await;
    let ack = h.await_("ACK", ms(1000)).await.0;
    assert!(
        ack.is_some_and(|a| a.header("To").contains("tag=st1")),
        "a retransmitted 200 was not ACKed again"
    );
    h.wait_events(&["answered:out-re"]).await; // and answered is not repeated
}

fn stray(inv: &Msg, from: &str, to: &str) -> Msg {
    Msg {
        code: 0,
        method: String::new(),
        raw: inv.raw.replacen(from, to, 1),
        bytes: vec![],
    }
}

// TestStraySIPDoesNotTouchTheDial
#[tokio::test]
async fn stray_sip_does_not_touch_the_dial() {
    let mut h = Dh::new().await;
    h.register().await;
    let mut d = h.dial_plain("out-st");
    let inv = h.await_("INVITE", ms(2000)).await.0.unwrap();
    // A 200 for another Call-ID, one for another CSeq, a CANCEL and a BYE naming other calls.
    let wide = h.h.st.sdp("wide");
    h.reply(
        &stray(&inv, "Call-ID: out-st", "Call-ID: other"),
        200,
        "OK",
        "x",
        &[],
        &wide,
    )
    .await;
    h.reply(
        &stray(&inv, "CSeq: 1 INVITE", "CSeq: 9 INVITE"),
        200,
        "OK",
        "x",
        &[],
        &wide,
    )
    .await;
    h.reply(
        &stray(&inv, "CSeq: 1 INVITE", "CSeq: 1 BYE"),
        200,
        "OK",
        "x",
        &[],
        "",
    )
    .await;
    h.h.st.cancel("out-st").await;
    h.h.st.bye("other").await;
    h.h.st.collect(ms(200)).await;
    assert!(h.h.s.snapshot().dialing, "a stray message ended the dial");
    h.reply(&inv, 200, "OK", "st1", &[], &wide).await;
    h.await_("ACK", ms(1000)).await;
    d.result(ms(2000)).await.unwrap();
    // A CANCEL from the station cannot end a call WE placed; a BYE does.
    h.h.st.cancel("out-st").await;
    h.h.st.collect(ms(100)).await;
    assert!(h.h.s.snapshot().in_call, "a CANCEL ended the outbound call");
}

// TestResponsesFromAnotherHostAreIgnored
#[tokio::test]
async fn responses_from_another_host_are_ignored() {
    let mut h = Dh::new().await;
    h.register().await;
    let mut d = h.dial_plain("out-ip");
    let inv = h.await_("INVITE", ms(2000)).await.0.unwrap();
    let imp = Station::on(h.h.s.sip_addr(), "127.0.0.2").await;
    imp.send(&format!(
        "{}\r\n\r\n",
        [
            "SIP/2.0 200 OK".to_string(),
            format!("Via: {}", inv.header("Via")),
            format!("From: {}", inv.header("From")),
            format!("To: {};tag=evil", inv.header("To")),
            "Call-ID: out-ip".to_string(),
            "CSeq: 1 INVITE".to_string(),
            "Content-Length: 0".to_string(),
        ]
        .join("\r\n")
    ))
    .await;
    sleep_ms(150).await;
    assert!(
        !h.h.s.snapshot().in_call,
        "a 200 from another host brought the call up"
    );
    h.reply(&inv, 486, "Busy Here", "st1", &[], "").await;
    let _ = d.result(ms(2000)).await;
}

// TestOnlyOneDialAtATime
#[tokio::test]
async fn only_one_dial_at_a_time() {
    let mut h = Dh::new().await;
    h.register().await;
    let mut d = h.dial_plain("out-1");
    h.await_("INVITE", ms(2000)).await;
    let e =
        h.h.d
            .dial_call(&CancelToken::new(), "out-2", MediaTarget::default())
            .await
            .unwrap_err();
    assert!(e.message().contains("the gate is busy"), "{e}");
    let ms_ = h.h.st.collect(ms(150)).await;
    assert!(!has(&ms_, "INVITE"), "a second INVITE");
    h.h.d.stop().await.unwrap();
    let e = d.result(ms(2000)).await.unwrap_err();
    assert!(e.message().contains("the server is stopping"), "{e}");
}

// TestStopDuringRingOutCancelsAndStopWhileTalkingByes
#[tokio::test]
async fn stop_during_ring_out_cancels_and_stop_while_talking_byes() {
    let mut h = Dh::new().await;
    h.register().await;
    let mut d = h.dial_plain("out-sd");
    h.await_("INVITE", ms(2000)).await;
    h.h.d.stop().await.unwrap();
    assert!(
        h.await_("CANCEL", ms(1000)).await.0.is_some(),
        "no CANCEL when the driver stopped mid-ring"
    );
    assert!(
        d.result(ms(2000)).await.is_err(),
        "DialCall succeeded on a stopped server"
    );
    h.wait_events(&[]).await;

    let mut h2 = Dh::new().await;
    h2.register().await;
    let mut d2 = h2.dial_plain("out-sd2");
    h2.answer(&mut d2, "wide").await;
    h2.h.d.stop().await.unwrap();
    assert!(
        h2.await_("BYE", ms(1000)).await.0.is_some(),
        "no BYE when the driver stopped mid-call"
    );
}

// TestCallDialerContractAcrossOutcomes
#[tokio::test]
async fn call_dialer_contract_across_outcomes() {
    let id = "out-k";
    // (name, events, error expected)
    let outcomes: [(&str, Vec<String>, bool); 4] = [
        (
            "hung up by us",
            vec![
                format!("answered:{id}"),
                format!("active:{id}"),
                format!("ended:{id}"),
            ],
            false,
        ),
        (
            "hung up by the station before any media",
            vec![format!("answered:{id}"), format!("ended:{id}")],
            false,
        ),
        ("refused", vec![], true),
        ("rings out", vec![], true),
    ];
    for (name, want, want_err) in outcomes {
        let mut h = Dh::tuned(DialTimers {
            ring: Some(ms(400)),
            cancel_wait: Some(ms(50)),
            ..Default::default()
        })
        .await;
        h.register().await;
        let mut d = h.dial_plain(id);
        match name {
            "hung up by us" => {
                h.answer(&mut d, "wide").await;
                h.send_rtp(96, 1, &[0u8; 640]).await;
                h.wait_events(&[&format!("answered:{id}"), &format!("active:{id}")])
                    .await;
                let _ = h.h.d.hangup_call(id);
            }
            "hung up by the station before any media" => {
                let inv = h.answer(&mut d, "wide").await;
                h.h.st.send(&h.station_bye(id, 3, &inv)).await;
            }
            "refused" => {
                let inv = h.await_("INVITE", ms(2000)).await.0.unwrap();
                h.reply(&inv, 486, "Busy Here", "st1", &[], "").await;
            }
            "rings out" => {
                h.await_("INVITE", ms(2000)).await;
            }
            _ => unreachable!(),
        }
        let r = d.result(ms(3000)).await;
        assert_eq!(r.is_err(), want_err, "{name}: err = {r:?}");
        let w: Vec<&str> = want.iter().map(String::as_str).collect();
        h.wait_events(&w).await;
        sleep_ms(100).await;
        let ev = h.events();
        no_ringing(&ev);
        assert_eq!(ev, want, "{name}: events");
        // `ended`, when there is one, is last and exactly once.
        let n = ev.iter().filter(|e| e.starts_with("ended:")).count();
        assert!(n <= 1, "{name}: ended {n} times");
        if n == 1 {
            assert!(ev.last().unwrap().starts_with("ended:"), "{name}: {ev:?}");
        }
    }
}

// TestTheDialAddsNoHTTPOrHomeAssistantPath
#[test]
fn the_dial_adds_no_http_or_home_assistant_path() {
    // misc_test.go pins the imports of the whole package; this pins that dial.rs itself opens
    // nothing but the server's own sockets (the Rust spellings of the Go list).
    let src = std::fs::read_to_string(
        std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("src/driver/gatesipserver/server/dial.rs"),
    )
    .unwrap();
    for bad in [
        "reqwest",
        "hyper",
        "http://",
        "https://",
        "TcpStream",
        "TcpListener",
        "UdpSocket::bind",
        "10.8.0.39",
        "10.6.0.7",
        "rpc2",
        "RPC2",
    ] {
        assert!(!src.contains(bad), "dial.rs mentions {bad:?}");
    }
}

// ── what the port adds (not in Go) ─────────────────────────────────────────────────────────

/// A `DialCall` future dropped while the INVITE rings (the IPC request died): the station is
/// CANCELled, the final answer is ACKed, the slot is free again, and no `ended` or `answered` is
/// reported.
#[tokio::test]
async fn a_dropped_dial_future_cancels_the_ringing_call_and_frees_the_slot() {
    let mut h = Dh::new().await;
    h.register().await;
    let mut d = h.dial_plain("out-drop");
    let inv = h.await_("INVITE", ms(2000)).await.0.unwrap();
    h.reply(&inv, 180, "Ringing", "st1", &[], "").await;
    d.jh.take().unwrap().abort();
    assert!(
        h.await_("CANCEL", ms(2000)).await.0.is_some(),
        "no CANCEL for a dropped dial"
    );
    h.reply(&inv, 487, "Request Terminated", "st1", &[], "")
        .await;
    assert!(
        h.await_("ACK", ms(1000)).await.0.is_some(),
        "the 487 was not ACKed"
    );
    wait_for("the slot", || !h.h.s.snapshot().dialing).await;
    h.wait_events(&[]).await;
    // free: an inbound ring is accepted
    h.h.st.invite("in-1", 1, &h.h.st.sdp("wide")).await;
    let want = format!("ringing:in-1@{}", h.h.device_id());
    h.wait_events(&[&want]).await;
}

/// R1-112: a dial dropped while it waits for the answer to the CANCEL it already sent does not
/// start a second full wait: the slot is free when the FIRST wait ends, and the log says what
/// happened (no CANCEL is sent now).
#[tokio::test]
async fn a_dial_dropped_in_the_cancel_wait_keeps_the_first_deadline() {
    let mut h = Dh::tuned(DialTimers {
        ring: Some(ms(150)),
        cancel_wait: Some(ms(1000)),
        ..Default::default()
    })
    .await;
    let lines = std::sync::Arc::new(std::sync::Mutex::new(Vec::<String>::new()));
    let l2 = lines.clone();
    h.h.s
        .set_logger(move |l| l2.lock().unwrap().push(l.to_string()));
    h.register().await;
    let mut d = h.dial_plain("out-cw");
    let inv = h.await_("INVITE", ms(2000)).await.0.unwrap();
    h.reply(&inv, 180, "Ringing", "st1", &[], "").await;
    assert!(h.await_("CANCEL", ms(2000)).await.0.is_some(), "no CANCEL");
    let t0 = std::time::Instant::now();
    sleep_ms(500).await;
    d.jh.take().unwrap().abort();
    wait_for("the slot", || !h.h.s.snapshot().dialing).await;
    let freed = t0.elapsed();
    assert!(
        freed < ms(1250),
        "the slot stayed busy {freed:?} after the CANCEL: a second cancel wait was started"
    );
    let all = lines.lock().unwrap().join("\n");
    assert!(
        all.contains("abandoned while waiting for the answer to our CANCEL"),
        "{all}"
    );
    assert!(
        !all.contains("abandoned before the station answered"),
        "{all}"
    );
}

/// The same drop, with the 200 crossing the CANCEL: ACKed and BYEd, nothing reported.
#[tokio::test]
async fn a_dropped_dial_future_hangs_up_a_200_that_crosses_the_cancel() {
    let mut h = Dh::new().await;
    h.register().await;
    let mut d = h.dial_plain("out-drop2");
    let inv = h.await_("INVITE", ms(2000)).await.0.unwrap();
    d.jh.take().unwrap().abort();
    assert!(h.await_("CANCEL", ms(2000)).await.0.is_some(), "no CANCEL");
    h.reply(&inv, 200, "OK", "st1", &[], &h.h.st.sdp("wide"))
        .await;
    let (bye, all) = h.await_("BYE", ms(2000)).await;
    assert!(bye.is_some() && has(&all, "ACK"), "{:?}", codes(&all));
    wait_for("the slot", || !h.h.s.snapshot().dialing).await;
    h.wait_events(&[]).await;
    assert!(!h.h.s.snapshot().in_call);
}

/// `Hangup()` on the server while the INVITE rings: a BYE (Go's oddity), the slot is cleared and
/// nothing is reported; the dial later fails with its own error and does not touch a newer call.
#[tokio::test]
async fn hangup_of_a_ringing_out_call_reports_nothing() {
    let mut h = Dh::new().await;
    h.register().await;
    let mut d = h.dial_plain("out-hu");
    let inv = h.await_("INVITE", ms(2000)).await.0.unwrap();
    assert!(h.h.s.hangup());
    assert!(h.await_("BYE", ms(1000)).await.0.is_some(), "no BYE");
    h.reply(&inv, 486, "Busy Here", "st1", &[], "").await;
    let _ = d.result(ms(3000)).await;
    h.wait_events(&[]).await;
    assert!(!h.h.s.snapshot().dialing);
}

/// The counters of a dial: `Dials` counts INVITE attempts that passed the refusals, `Dialing` is
/// true only while the call rings out.
#[tokio::test]
async fn dials_and_dialing_are_counted() {
    let mut h = Dh::new().await;
    h.register().await;
    assert_eq!(h.h.s.snapshot().dials, 0);
    let mut d = h.dial_plain("out-cnt");
    let inv = h.await_("INVITE", ms(2000)).await.0.unwrap();
    let st = h.h.s.snapshot();
    assert!(
        st.dials == 1 && st.dialing && !st.ringing && !st.in_call,
        "{st:?}"
    );
    h.reply(&inv, 200, "OK", "st1", &[], &h.h.st.sdp("wide"))
        .await;
    d.result(ms(2000)).await.unwrap();
    let st = h.h.s.snapshot();
    assert!(
        st.dials == 1 && !st.dialing && st.in_call && st.answered == 1,
        "{st:?}"
    );
}

/// A dial towards an INVITE target that is the address the station REGISTERed FROM (not its
/// Contact): the Contact names a port nobody listens on.
#[tokio::test]
async fn the_invite_goes_to_the_registers_source_not_its_contact() {
    let mut h = Dh::new().await;
    h.h.st.send(&h.h.st.register_text("")).await;
    let (m401, _) = h.await_("401", ms(2000)).await;
    let auth = digest_auth(&m401.unwrap(), "8001", "s3cretpass");
    let text =
        h.h.st
            .register_text(&auth)
            .replace(&format!("sip:8001@{}", h.h.st.me), "sip:8001@127.0.0.1:9");
    h.h.st.send(&text).await;
    assert!(h.await_("200", ms(2000)).await.0.is_some());
    let mut d = h.dial_plain("out-ct");
    let inv = h
        .await_("INVITE", ms(2000))
        .await
        .0
        .expect("the INVITE did not reach the REGISTER's source");
    assert!(inv.raw.starts_with("INVITE sip:9901@127.0.0.1 SIP/2.0"));
    h.reply(&inv, 486, "Busy Here", "st1", &[], "").await;
    let _ = d.result(ms(2000)).await;
}

/// Difference 44 for a call WE placed: a BYE naming the live Call-ID from a host other than the
/// station's is answered 200 and ends nothing (Go ends the call on the Call-ID alone).
#[tokio::test]
async fn a_bye_from_another_host_does_not_end_a_dialled_call() {
    let mut h = Dh::with(
        Opts::cfg(json!({"dial_enabled": true, "allow_from": ["127.0.0.1", "127.0.0.2"]})),
        DialTimers::default(),
    )
    .await;
    h.register().await;
    let mut d = h.dial_plain("out-44");
    let inv = h.answer(&mut d, "wide").await;
    let imp = Station::on(h.h.s.sip_addr(), "127.0.0.2").await;
    imp.send(&h.station_bye("out-44", 9, &inv)).await;
    let got = imp.collect(ms(300)).await;
    assert_eq!(codes(&got), vec!["200"], "{got:?}");
    assert!(h.h.s.snapshot().in_call, "a stranger's BYE ended the call");
    h.wait_events(&["answered:out-44"]).await;
    // the station's own BYE still does
    h.h.st.send(&h.station_bye("out-44", 10, &inv)).await;
    h.wait_events(&["answered:out-44", "ended:out-44"]).await;
}

// ── difference 50: a nonce is good only for the host it was issued to ──────────────────────

fn two_hosts() -> Opts {
    Opts::cfg(json!({"dial_enabled": true, "allow_from": ["127.0.0.1", "127.0.0.2"]}))
}

/// A captured Authorization replayed from another ALLOWED host inside the nonce's five minutes
/// is re-challenged (a fresh nonce for that host) and does not move the registration, so it
/// cannot redirect the outbound INVITE. The legitimate host reusing its nonce still gets 200 as
/// in Go.
#[tokio::test]
async fn a_replayed_authorization_from_another_host_does_not_move_the_registration() {
    let mut h = Dh::with(two_hosts(), DialTimers::default()).await;
    h.register().await;
    let reg = h.h.s.registration_addr().unwrap();
    assert_eq!(reg, h.h.st.me);
    // the nonce the station used (its REGISTER was answered once)
    let nonce = {
        // challenge once more from the real station to learn a live nonce
        h.h.st.register("").await;
        let (m401, _) = h.await_("401", ms(2000)).await;
        quoted(&m401.unwrap().header("WWW-Authenticate"), "nonce")
    };
    let auth = digest_for("8001", "s3cretpass", "VDP", &nonce);
    let imp = Station::on(h.h.s.sip_addr(), "127.0.0.2").await;
    // the replay: a correct digest on a nonce issued to 127.0.0.1, sent from 127.0.0.2
    imp.register(&auth).await;
    let got = imp.collect(ms(300)).await;
    assert_eq!(codes(&got), vec!["401"], "{got:?}");
    assert_ne!(
        quoted(&got[0].header("WWW-Authenticate"), "nonce"),
        nonce,
        "a fresh nonce"
    );
    assert_eq!(h.h.s.registration_addr(), Some(reg), "reg.addr moved");
    assert_eq!(
        h.h.s.snapshot().digest_ok,
        1,
        "the replay was counted as a good digest"
    );
    // a wrong password from the other host is still judged first: 403, as in Go
    let bad = digest_for("8001", "wrongpassw", "VDP", &nonce);
    imp.register(&bad).await;
    assert_eq!(codes(&imp.collect(ms(300)).await), vec!["403"]);
    // the dial still goes to the real station
    let mut d = h.dial_plain("out-nr");
    let inv = h
        .await_("INVITE", ms(2000))
        .await
        .0
        .expect("the INVITE went elsewhere");
    h.reply(&inv, 486, "Busy Here", "st1", &[], "").await;
    let _ = d.result(ms(2000)).await;
    // and the real station reusing its own nonce is answered 200 exactly as in Go
    h.h.st.register(&auth).await;
    let (ok, _) = h.await_("200", ms(1000)).await;
    assert!(
        ok.is_some(),
        "the station's own reuse of its nonce was refused"
    );
}

/// The other half: a registration that really moves (the station's address changes, it
/// REGISTERs through a fresh challenge from the new address) is accepted and the next dial
/// follows it.
#[tokio::test]
async fn a_station_that_moves_registers_from_its_new_address_and_is_dialled_there() {
    let mut h = Dh::with(two_hosts(), DialTimers::default()).await;
    h.register().await;
    let moved = Station::on(h.h.s.sip_addr(), "127.0.0.2").await;
    moved.register("").await;
    let m401 = find(&moved.collect(ms(300)).await, "401").expect("no challenge");
    moved
        .register(&digest_auth(&m401, "8001", "s3cretpass"))
        .await;
    assert!(find(&moved.collect(ms(300)).await, "200").is_some());
    assert_eq!(h.h.s.registration_addr(), Some(moved.me));
    let mut d = h.dial_plain("out-mv");
    let ms_ = moved.collect(ms(500)).await;
    let inv = find(&ms_, "INVITE").expect("the INVITE did not follow the station");
    assert!(
        inv.raw.starts_with("INVITE sip:9901@127.0.0.2 SIP/2.0"),
        "{:?}",
        inv.raw
    );
    d.ctx.cancel();
    let _ = d.result(ms(3000)).await;
}
