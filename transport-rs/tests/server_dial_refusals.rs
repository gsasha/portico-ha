//! The ports of `dial_test.go` that need no outbound call: the `CanDial` rule and its republish
//! (the registration bookkeeping lives in `server.go`/`dial.go` `noteRegisteredLocked`), and the
//! refusals `DialCall` makes before a single datagram is sent. The tests that place the call
//! itself are in `server_dial.rs`. The Go test name is in a comment.

mod common;

use common::*;
use serde_json::json;
use transport_rs::cancel::CancelToken;
use transport_rs::driver::gatesipserver::GateSipServer;
use transport_rs::driver::{CallDialer, ConnectAnswerer, Driver, MediaTarget, Sentinel};

fn dial_on() -> Opts {
    Opts::cfg(json!({"dial_enabled": true}))
}

/// Go's `Capabilities()` bytes before and after a registration for a `dial_enabled` setting
/// (`driver_caps.json`, `registered`, written by the real Go driver).
fn golden_wire(dial: bool) -> (String, String) {
    let g: serde_json::Value =
        serde_json::from_str(include_str!("golden/driver_caps.json")).unwrap();
    let row = g["registered"]
        .as_array()
        .unwrap()
        .iter()
        .find(|r| r["dial_enabled"].as_bool() == Some(dial))
        .unwrap();
    (
        row["before"].as_str().unwrap().to_string(),
        row["after"].as_str().unwrap().to_string(),
    )
}

/// The wire as it was before DEV-518: the Go golden bytes know nothing of the registration keys,
/// which are the last keys of this driver's reply.
fn legacy(w: &str) -> String {
    match w.find(",\"StationRegistered\"") {
        Some(i) => format!("{}}}", &w[..i]),
        None => w.to_string(),
    }
}

fn caps_seen(h: &Harness) -> Vec<bool> {
    h.sink
        .events()
        .iter()
        .filter_map(|e| e.strip_prefix("caps:"))
        .map(|e| e.ends_with(":true"))
        .collect()
}

async fn wait_for(what: &str, f: impl Fn() -> bool) {
    let end = std::time::Instant::now() + std::time::Duration::from_secs(3);
    while std::time::Instant::now() < end {
        if f() {
            return;
        }
        sleep_ms(10).await;
    }
    panic!("timed out waiting for {what}");
}

/// `dh.register` / `dh.await`: the digest REGISTER, returning as soon as each answer arrives (a
/// registration lifetime of 300 ms leaves no room for fixed 300 ms listening windows).
async fn register_fast(h: &Harness) {
    async fn await_code(h: &Harness, want: &str) -> Msg {
        let end = std::time::Instant::now() + std::time::Duration::from_secs(2);
        while std::time::Instant::now() < end {
            if let Some(m) = find(&h.st.collect(ms(20)).await, want) {
                return m;
            }
        }
        panic!("no {want}");
    }
    h.st.register("").await;
    let m401 = await_code(h, "401").await;
    h.st.register(&digest_auth(&m401, "8001", "s3cretpass"))
        .await;
    await_code(h, "200").await;
}

// TestCanDialNeedsDialEnabledAndAFreshRegistration
#[tokio::test]
async fn can_dial_needs_dial_enabled_and_a_fresh_registration() {
    let off = Harness::with(Opts::cfg(json!({"dial_enabled": false}))).await;
    register_station(&off).await;
    assert!(
        !off.d.capabilities().can_dial,
        "CanDial with dial_enabled false"
    );
    // R2-71: the bytes of the REAL driver after a REAL REGISTER, against Go's
    let (before, after) = golden_wire(false);
    assert_eq!(before, after);
    assert_eq!(legacy(&off.d.capabilities().to_wire()), after);
    assert!(off.d.capabilities().to_wire().contains("\"StationRegistered\":true,\"LastRegisterAt\":"));
    // DEV-518: dial off still republishes, once, for the REGISTER, and CanDial in it stays false
    wait_for("the registration republish", || caps_seen(&off).len() == 1).await;
    assert_eq!(caps_seen(&off), vec![false]);

    let h = Harness::with(dial_on()).await;
    assert!(
        !h.d.capabilities().can_dial,
        "CanDial before the station registered"
    );
    // A REGISTER whose digest is wrong (refused 403) registers nothing.
    h.st.register(
        "Digest username=\"8001\", realm=\"VDP\", nonce=\"x\", uri=\"sip:x\", response=\"00\"",
    )
    .await;
    h.st.collect(ms(200)).await;
    assert!(
        !h.d.capabilities().can_dial,
        "CanDial after a refused REGISTER"
    );
    let (before, after) = golden_wire(true);
    assert_eq!(legacy(&h.d.capabilities().to_wire()), before, "before the REGISTER");
    register_station(&h).await;
    assert!(
        h.d.capabilities().can_dial,
        "no CanDial after a good REGISTER"
    );
    assert_eq!(legacy(&h.d.capabilities().to_wire()), after, "after the REGISTER");
    wait_for("the republish", || caps_seen(&h).len() == 1).await;
    assert_eq!(caps_seen(&h), vec![true]);
    // Stale: the registration is older than the expiry plus grace.
    h.s.backdate_registration(std::time::Duration::from_secs(331));
    assert!(
        !h.d.capabilities().can_dial,
        "CanDial with a stale registration"
    );
}

// TestCanDialIsRepublishedWhenTheRegistrationLapsesAndReturns
#[tokio::test]
async fn can_dial_is_republished_when_the_registration_lapses_and_returns() {
    let h = Harness::with(dial_on()).await;
    h.s.set_reg_ttl(ms(300));
    register_fast(&h).await;
    wait_for("true", || caps_seen(&h).len() == 1).await;
    wait_for("false after the TTL", || caps_seen(&h).len() == 2).await;
    assert_eq!(caps_seen(&h), vec![true, false]);
    register_fast(&h).await;
    wait_for("true again", || caps_seen(&h).len() == 3).await;
    // A second REGISTER inside the window republishes nothing (changes only).
    register_fast(&h).await;
    sleep_ms(100).await;
    assert_eq!(
        caps_seen(&h).len(),
        3,
        "a refresh republished: {:?}",
        caps_seen(&h)
    );
}

// TestDialOffRefusesLikeTheLegacyDialAndSendsNothing
#[tokio::test]
async fn dial_off_refuses_like_the_legacy_dial_and_sends_nothing() {
    let h = Harness::with(Opts::cfg(json!({"dial_enabled": false}))).await;
    register_station(&h).await;
    let t = CancelToken::new();
    let e =
        h.d.dial_call(&t, "out-1", MediaTarget::default())
            .await
            .unwrap_err();
    assert!(e.is(Sentinel::NotSupported), "{e}");
    let r = h.st.collect(ms(200)).await;
    assert!(r.is_empty(), "datagrams with dial off: {:?}", codes(&r));
    let e = h.d.dial(&t, MediaTarget::default()).await.unwrap_err();
    assert!(e.is(Sentinel::NotSupported), "legacy Dial: {e}");
}

// TestLegacyDialStaysNotSupportedWithDialOn
#[tokio::test]
async fn legacy_dial_stays_not_supported_with_dial_on() {
    let h = Harness::with(dial_on()).await;
    register_station(&h).await;
    let e =
        h.d.dial(&CancelToken::new(), MediaTarget::default())
            .await
            .unwrap_err();
    assert!(e.is(Sentinel::NotSupported), "{e}");
}

// TestDialBeforeStartIsRefused
#[tokio::test]
async fn dial_before_start_is_refused() {
    let mut d = GateSipServer::default();
    let cfg = json!({"sip_listen": "127.0.0.1:0", "password": "s3cretpass", "dial_enabled": true});
    d.configure(cfg.as_object().unwrap()).unwrap();
    let e = d
        .dial_call(&CancelToken::new(), "out-1", MediaTarget::default())
        .await
        .unwrap_err();
    assert_eq!(e.message(), "gate_sip_server: DialCall: not started");
}

// TestDialRefusedWhenNotRegisteredOrStaleAndNothingIsSent
#[tokio::test]
async fn dial_refused_when_not_registered_or_stale_and_nothing_is_sent() {
    let h = Harness::with(dial_on()).await;
    let t = CancelToken::new();
    let e =
        h.d.dial_call(&t, "out-1", MediaTarget::default())
            .await
            .unwrap_err();
    assert_eq!(
        e.message(),
        "gate_sip_server: DialCall: the station has not registered"
    );
    register_station(&h).await;
    h.s.backdate_registration(std::time::Duration::from_secs(331));
    let e =
        h.d.dial_call(&t, "out-2", MediaTarget::default())
            .await
            .unwrap_err();
    assert_eq!(
        e.message(),
        "gate_sip_server: DialCall: the station's registration has expired"
    );
    let e =
        h.d.dial_call(&t, "", MediaTarget::default())
            .await
            .unwrap_err();
    assert_eq!(
        e.message(),
        "gate_sip_server: DialCall: no call id",
        "an empty call id was accepted"
    );
    let r = h.st.collect(ms(200)).await;
    assert!(
        r.is_empty(),
        "datagrams for a refused dial: {:?}",
        codes(&r)
    );
    // (the events of the earlier register: CanDial went true, then false with the stale check
    // does not republish by itself; only ringing/state events are asserted absent)
    assert!(!h
        .sink
        .events()
        .iter()
        .any(|e| e.starts_with("ringing") || e.contains("out-")));
}

// TestDialRefusedWhileAnInboundCallRingsOrIsUp
#[tokio::test]
async fn dial_refused_while_an_inbound_call_rings_or_is_up() {
    let h = Harness::with(Opts {
        ring: Some(ms(5000)),
        ..dial_on()
    })
    .await;
    register_station(&h).await;
    h.st.invite("in-1", 1, &h.st.sdp("wide")).await;
    h.st.collect(ms(100)).await;
    let t = CancelToken::new();
    let e =
        h.d.dial_call(&t, "out-1", MediaTarget::default())
            .await
            .unwrap_err();
    assert_eq!(
        e.message(),
        "gate_sip_server: DialCall: the gate is busy: another call is in progress",
        "ringing"
    );
    h.d.answer_with_audio(&t, "in-1").await.unwrap();
    h.st.collect(ms(100)).await;
    let e =
        h.d.dial_call(&t, "out-2", MediaTarget::default())
            .await
            .unwrap_err();
    assert_eq!(
        e.message(),
        "gate_sip_server: DialCall: the gate is busy: another call is in progress",
        "answered"
    );
    // Nothing was sent to the station by either refused dial, and the inbound call is untouched.
    let sn = h.s.snapshot();
    assert!(sn.in_call && sn.dials == 0, "{sn:?}");
    assert!(
        !h.sink.events().iter().any(|e| e.contains("out-")),
        "an event for a refused dial"
    );
    assert!(!ConnectAnswerer::ringing_call(&*h.d, "in-1"));
}

// R2-24 (dial.go:193-201): `s.cur != nil` is checked BEFORE the registration. An inbound INVITE
// needs no REGISTER, so "a ring while the station never registered (or its registration went
// stale), then Talk to the gate" is reachable and must say "busy", not "not registered".
#[tokio::test]
async fn busy_is_reported_before_the_registration_is_looked_at() {
    const BUSY: &str = "gate_sip_server: DialCall: the gate is busy: another call is in progress";
    let t = CancelToken::new();
    // never registered
    let h = Harness::with(Opts {
        ring: Some(ms(5000)),
        ..dial_on()
    })
    .await;
    h.st.invite("in-1", 1, &h.st.sdp("wide")).await;
    h.st.collect(ms(100)).await;
    let e = h.d.dial_call(&t, "out-1", MediaTarget::default()).await;
    assert_eq!(e.unwrap_err().message(), BUSY, "never registered");

    // registered, then the registration goes stale while the call rings
    let h = Harness::with(Opts {
        ring: Some(ms(5000)),
        ..dial_on()
    })
    .await;
    h.s.set_reg_ttl(ms(300));
    register_fast(&h).await;
    h.st.invite("in-2", 1, &h.st.sdp("wide")).await;
    h.st.collect(ms(100)).await;
    sleep_ms(400).await;
    let e = h.d.dial_call(&t, "out-2", MediaTarget::default()).await;
    assert_eq!(e.unwrap_err().message(), BUSY, "stale registration");
}

// R1-86 (PARITY 332; R2-38): a Call-ID with invalid UTF-8 reaches the hub in its lossy form
// (U+FFFD), so the calls the hub makes by that id (`ringing_call`, `answer_with_audio`,
// `hangup_answered`) compare the lossy form too and the call can be answered and hung up. Go
// compares the raw string, which the hub can never produce (the difference is recorded in 332).
#[tokio::test]
async fn a_call_id_with_invalid_utf8_is_matched_by_its_lossy_form() {
    let h = Harness::with(Opts {
        ring: Some(ms(5000)),
        ..dial_on()
    })
    .await;
    register_station(&h).await;
    // every occurrence of the id in the request carries the byte; the Call-ID header is what counts
    let text = h.st.invite_text("in-@@x", 1, &h.st.sdp("wide"));
    assert!(text.contains("@@"));
    let wire = text
        .split("@@")
        .map(|p| p.as_bytes().to_vec())
        .collect::<Vec<_>>()
        .join(&0xe9u8);
    h.st.send_bytes(&wire).await;
    h.st.collect(ms(150)).await;
    let lossy = "in-\u{fffd}x";
    assert!(
        ConnectAnswerer::ringing_call(&*h.d, lossy),
        "the lossy id does not name the ringing call; events {:?}",
        h.sink.events()
    );
    assert!(!ConnectAnswerer::ringing_call(&*h.d, "in-\u{e9}x"));
    let t = CancelToken::new();
    h.d.answer_with_audio(&t, lossy).await.unwrap();
    h.st.collect(ms(100)).await;
    assert!(
        h.s.hangup_answered(lossy),
        "the answered call was not hung up by its lossy id"
    );
}
