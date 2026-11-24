//! The `unifi_protect_talk` driver (slice 7b) over a fake media: `talk_test.go` and
//! `talk_dial_test.go` (the Go name is in a comment on each test), and the behaviours this port
//! adds (marked "Rust-only"). The media is the Go tests' `fakeMedia`; no process, no network.

mod common;

use std::sync::Arc;
use std::time::Duration;

use common::talk::*;
use common::unifi::wait_for;
use serde_json::json;
use tokio::time::Instant;
use transport_rs::cancel::CancelToken;
use transport_rs::driver::unifiprotect::talk::{
    NoBackendOpener, TalkDriver, TalkMedia, TalkTiming,
};
use transport_rs::driver::{
    CallAudio, CallDialer, CallHangupper, ConnectAnswerer, Driver, DriverError, EventSink,
    ExternalRinger, MediaTarget, ReasonedHangupper, Sentinel,
};

const KEY: &str = "SECRET-API-KEY";

async fn answer(d: &TalkDriver, id: &str) -> Result<Arc<dyn CallAudio>, DriverError> {
    d.answer_with_audio(&CancelToken::new(), id).await
}

async fn dial(d: &TalkDriver, id: &str) -> Result<(), DriverError> {
    d.dial_call(&CancelToken::new(), id, MediaTarget::default())
        .await
}

fn ring(d: &TalkDriver, id: &str, hold: Duration) -> Result<(), DriverError> {
    d.external_ring(id, hold)
}

const MIN: Duration = Duration::from_secs(60);

// TestTalkConfigureValidates
#[test]
fn talk_configure_validates() {
    let mut bad = vec![json!({}), json!({"device_id": "x"})];
    for (k, v) in [
        ("protect_host", json!("https://h")),
        ("downlink_url", json!("http://x/y")),
        ("guard_url", json!("file:///etc/passwd")),
        ("gain", json!(0.5)),
        ("gain", json!(99)),
        ("verify_ssl", json!("no")),
    ] {
        let mut c = talk_cfg();
        c.insert(k.to_string(), v);
        bad.push(serde_json::Value::Object(c));
    }
    for (i, c) in bad.iter().enumerate() {
        let mut d = TalkDriver::default();
        let e = d
            .configure(c.as_object().unwrap())
            .expect_err(&format!("case {i}: accepted {c}"));
        assert!(
            !e.message().contains(KEY),
            "case {i}: error carries the key: {e}"
        );
    }
    let mut ok = talk_cfg();
    ok.insert("downlink_url".into(), json!("rtsp://10.0.0.1:8554/x"));
    ok.insert("guard_url".into(), json!("http://127.0.0.1:1/health"));
    ok.insert("gain".into(), json!(2.0));
    TalkDriver::default().configure(&ok).unwrap();
}

// Rust-only: the configure texts, the odd values and the control characters (PARITY.md 127).
#[test]
fn talk_configure_texts_and_odd_values() {
    let text = |extra: serde_json::Value| {
        let mut d = TalkDriver::default();
        d.configure(&talk_cfg_with(extra))
            .unwrap_err()
            .message()
            .to_string()
    };
    assert_eq!(
        TalkDriver::default()
            .configure(&serde_json::Map::new())
            .unwrap_err()
            .message(),
        "unifi_protect_talk: device_id, protect_host, protect_api_key and camera_id are required"
    );
    assert_eq!(
        text(json!({"protect_host": "a b"})),
        "unifi_protect_talk: protect_host must be a bare host or host:port"
    );
    assert_eq!(
        text(json!({"downlink_url": "http://x/y"})),
        "unifi_protect_talk: downlink_url must be a rtsp/rtsps:// URL"
    );
    assert_eq!(
        text(json!({"guard_url": "ftp://x/y"})),
        "unifi_protect_talk: guard_url must be a http/https:// URL"
    );
    assert_eq!(
        text(json!({"verify_ssl": 1})),
        "unifi_protect_talk: verify_ssl must be true or false"
    );
    assert_eq!(
        text(json!({"dial_enabled": null})),
        "unifi_protect_talk: dial_enabled must be true or false"
    );
    for g in [
        json!(1.5),
        json!("2"),
        json!(0),
        json!(17),
        json!(true),
        json!(null),
    ] {
        assert_eq!(
            text(json!({"gain": g})),
            "unifi_protect_talk: gain must be a whole number from 1 to 16",
            "{g}"
        );
    }
    // control characters and spaces never reach a process argument or a request line
    for bad in [
        json!({"downlink_url": "rtsp://h/a b"}),
        json!({"downlink_url": "rtsp://h/a\nb"}),
        json!({"guard_url": "http://h/a\r\nHost: x"}),
        json!({"protect_host": "h\nX-Evil: 1"}),
        json!({"protect_host": "h?x"}),
        json!({"protect_api_key": "k\nX: 1"}),
    ] {
        let mut d = TalkDriver::default();
        let e = d.configure(&talk_cfg_with(bad.clone())).unwrap_err();
        // R2-100: neither the injected header nor any supplied value is echoed
        let m = e.message();
        assert!(
            !m.contains("X-Evil") && !m.contains("X: 1") && !m.contains('\n') && !m.contains('\r'),
            "{bad}: {e}"
        );
    }
    // a failed configure leaves the previous one in place; the defaults
    let mut d = TalkDriver::default();
    d.configure(&talk_cfg()).unwrap();
    assert!(d.configure(&talk_cfg_with(json!({"gain": 99}))).is_err());
    assert_eq!(d.device_id().as_deref(), Some("ha_event:front"));
    assert!(d.capabilities().can_dial);
    // verify_ssl defaults to false here (the zero value in Go), gain to 1: seen through the
    // capabilities bytes only, the rest is private; dial_enabled false turns CanDial off
    d.configure(&talk_cfg_with(json!({"dial_enabled": false})))
        .unwrap();
    assert!(!d.capabilities().can_dial);
}

// TestTalkCapabilitiesAreTheHonestSetAndNoDoorOpening
#[tokio::test]
async fn talk_capabilities_are_the_honest_set_and_no_door_opening() {
    let mut d = TalkDriver::default();
    d.configure(&talk_cfg()).unwrap();
    const WANT: &str = r#"{"InboundAudio":1,"OutboundAudio":2,"DirectCapable":false,"HasRelay":false,"RTSPTemplate":"","ReportsAvailability":true,"CanDial":true}"#;
    assert_eq!(d.capabilities().to_wire(), WANT);
    let c = CancelToken::new();
    for (name, e) in [
        ("OpenRelay", d.open_relay(&c).await),
        ("Dial", d.dial(&c, MediaTarget::default()).await),
        ("Answer", d.answer(&c, MediaTarget::default()).await),
    ] {
        let e = e.unwrap_err();
        assert!(e.is(Sentinel::NotSupported), "{name}: {e}");
        assert!(
            e.message()
                .starts_with(&format!("unifi_protect_talk: {name}: not_supported")),
            "{e}"
        );
    }
    assert!(d.as_external_ringer().is_some());
    assert!(d.as_connect_answerer().is_some());
    assert!(d.as_call_dialer().is_some());
    assert!(d.as_audio_claimer().is_some());
    assert!(d.as_call_hangupper().is_some());
    assert!(d.as_reasoned_hangupper().is_some());
    assert!(d.as_talk_healther().is_some());
    assert!(d.as_talkback_answerer().is_none());
    assert!(transport_rs::driver::registry().contains_key("unifi_protect_talk"));
}

// TestTalkExternalRingHoldsAnswerableCallAndOpensNothing
#[tokio::test]
async fn talk_external_ring_holds_answerable_call_and_opens_nothing() {
    let (d, s, op, _) = new_talk(TalkTiming::default()).await;
    ring(&d, "c1", MIN).unwrap();
    assert!(d.ringing_call("c1") && !d.ringing_call("other"));
    assert_eq!((s.rings(), op.opens()), (0, 0), "ring reported or opened");
    ring(&d, "c1", MIN).unwrap(); // same call again: no-op
    ring(&d, "c2", MIN).unwrap(); // a new press supersedes the unanswered ring
    assert!(
        !d.ringing_call("c1") && d.ringing_call("c2"),
        "c2 did not take over from c1"
    );
}

// TestTalkNewRingSupersedesRingingCallAndReportsOldEnded
#[tokio::test]
async fn talk_new_ring_supersedes_ringing_call_and_reports_old_ended() {
    let (d, s, _, _) = new_talk(TalkTiming::default()).await;
    ring(&d, "c1", MIN).unwrap();
    ring(&d, "c2", MIN).unwrap();
    assert_eq!(s.got(), "avail:ha_event:front:up,c1:ended");
    assert_eq!(d.last_end_for_test(), "superseded by a new ring");
    let e = answer(&d, "c1").await.err().unwrap();
    assert!(e.is(Sentinel::NoSuchCall), "old call still answerable: {e}");
    answer(&d, "c2").await.expect("new call not answerable");
}

// TestTalkSupersededTimerDoesNotEndTheNewCall
#[tokio::test]
async fn talk_superseded_timer_does_not_end_the_new_call() {
    let (d, s, _, _) = new_talk(short_holds()).await;
    ring(&d, "c1", ms(30)).unwrap();
    ring(&d, "c2", MIN).unwrap();
    tokio::time::sleep(ms(120)).await; // well past c1's original hold
    assert!(d.ringing_call("c2"), "c1's timer ended c2");
    assert_eq!(
        s.got(),
        "avail:ha_event:front:up,c1:ended",
        "c1 must be ended exactly once"
    );
}

// TestTalkNewRingHasAFreshHold
#[tokio::test]
async fn talk_new_ring_has_a_fresh_hold() {
    let (d, _, _, _) = new_talk(TalkTiming::default()).await;
    ring(&d, "c1", Duration::from_secs(5)).unwrap();
    ring(&d, "c2", MIN).unwrap();
    let left = d.deadline_for_test().unwrap() - Instant::now();
    assert!(left >= Duration::from_secs(30), "hold not fresh: {left:?}");
}

// TestTalkDifferentRingStaysBusyWhileAnsweredOrAnswering
#[tokio::test]
async fn talk_different_ring_stays_busy_while_answered_or_answering() {
    let (d, s, _, _) = new_talk(TalkTiming::default()).await;
    ring(&d, "c1", MIN).unwrap();
    answer(&d, "c1").await.unwrap();
    let e = ring(&d, "c2", MIN).unwrap_err();
    assert!(e.is(Sentinel::Busy), "answered: {e}");
    ring(&d, "c1", MIN).unwrap(); // same call: no-op
    assert!(
        !s.got().contains("c1:ended"),
        "answered call was ended: {}",
        s.got()
    );
    d.force_answering_for_test();
    assert!(
        ring(&d, "c2", MIN).unwrap_err().is(Sentinel::Busy),
        "answering"
    );
}

// TestTalkHoldExpiryEndsAnUnansweredRing
#[tokio::test]
async fn talk_hold_expiry_ends_an_unanswered_ring() {
    let (d, s, _, _) = new_talk(short_holds()).await;
    ring(&d, "c1", ms(30)).unwrap();
    let s2 = s.clone();
    wait_for("c1 expired", move || {
        s2.got() == "avail:ha_event:front:up,c1:ended"
    })
    .await;
    assert!(!d.ringing_call("c1"), "still ringing");
    ring(&d, "c2", MIN).unwrap(); // the slot is free again
    assert!(
        s.heard().contains("|c1|ended|the ring expired unanswered"),
        "{}",
        s.heard()
    );
}

// TestTalkHoldIsClamped
#[tokio::test]
async fn talk_hold_is_clamped() {
    let (d, _, _, _) = new_talk(TalkTiming::default()).await;
    ring(&d, "c1", Duration::from_secs(24 * 3600)).unwrap();
    let left = d.deadline_for_test().unwrap() - Instant::now();
    assert!(
        left <= TalkTiming::default().max_hold,
        "hold not clamped: {left:?}"
    );
    // and the floor, and the default for a zero hold
    ring(&d, "c1", Duration::ZERO).unwrap(); // same call: the first hold stays
    let (d, _, _, _) = new_talk(TalkTiming::default()).await;
    ring(&d, "z", Duration::ZERO).unwrap();
    let left = d.deadline_for_test().unwrap() - Instant::now();
    assert!(
        left > Duration::from_secs(29) && left <= Duration::from_secs(30),
        "{left:?}"
    );
    ring(&d, "y", ms(1)).unwrap();
    let left = d.deadline_for_test().unwrap() - Instant::now();
    assert!(left > ms(900) && left <= Duration::from_secs(1), "{left:?}");
}

// TestTalkAnswerOpensOnlyInAnswerWithAudioAndReportsAnsweredActive
#[tokio::test]
async fn talk_answer_opens_only_in_answer_with_audio_and_reports_answered_active() {
    let (d, s, op, m) = new_talk(TalkTiming::default()).await;
    ring(&d, "c1", MIN).unwrap();
    assert_eq!(op.opens(), 0, "opened before the leg connected");
    answer(&d, "c1").await.expect("answer");
    assert!(op.opens() == 1 && !d.ringing_call("c1"));
    assert_eq!(s.got(), "avail:ha_event:front:up,c1:answered,c1:active");
    // a second answerer is refused and the first keeps the call
    let e = answer(&d, "c1").await.err().unwrap();
    assert!(e.is(Sentinel::AlreadyAnswered), "second: {e}");
    assert!(
        op.opens() == 1 && m.closed() == 0,
        "second answerer disturbed the call"
    );
    assert!(answer(&d, "nope")
        .await
        .err()
        .unwrap()
        .is(Sentinel::NoSuchCall));
    // the opener was asked for the call's id
    assert_eq!(*op.asked.lock().unwrap(), vec!["c1".to_string()]);
}

// TestTalkConcurrentAnswersOpenOnce
#[tokio::test]
async fn talk_concurrent_answers_open_once() {
    let (d, _, op, _) = new_talk(TalkTiming::default()).await;
    let gate = Arc::new(tokio::sync::Notify::new());
    *op.gate.lock().unwrap() = Some(gate.clone());
    ring(&d, "c1", MIN).unwrap();
    let d1 = d.clone();
    let first = tokio::spawn(async move { answer(&d1, "c1").await.map(|_| ()) });
    let op2 = op.clone();
    wait_for("one open", move || op2.opens() == 1).await;
    let e = answer(&d, "c1").await.err().unwrap();
    assert!(e.is(Sentinel::AlreadyAnswered), "second: {e}");
    gate.notify_one();
    first.await.unwrap().expect("first");
    assert_eq!(op.opens(), 1);
}

// TestTalkFailedOpenLeavesTheRingAnswerableUntilTheHoldRunsOut
#[tokio::test]
async fn talk_failed_open_leaves_the_ring_answerable_until_the_hold_runs_out() {
    let mut d = TalkDriver::default();
    d.set_timing(short_holds());
    d.configure(&talk_cfg()).unwrap();
    d.set_opener(Arc::new(NoBackendOpener));
    let s = TalkSink::new();
    let d = Arc::new(d);
    d.start(&CancelToken::new(), s.clone()).await.unwrap();
    ring(&d, "c1", ms(60)).unwrap();
    let e = answer(&d, "c1").await.err().unwrap();
    assert!(e.is(Sentinel::NotSupported), "stub opener: {e}");
    assert!(d.ringing_call("c1"), "a failed open ended the ring");
    let s2 = s.clone();
    wait_for("hold ran out", move || s2.got().ends_with("c1:ended")).await;
    assert!(!d.ringing_call("c1"), "hold did not end the ring");
}

// TestTalkHangupClosesMediaAndEnds
#[tokio::test]
async fn talk_hangup_closes_media_and_ends() {
    let (d, s, _, m) = new_talk(TalkTiming::default()).await;
    ring(&d, "c1", MIN).unwrap();
    answer(&d, "c1").await.unwrap();
    d.hangup_call("c1").unwrap();
    assert!(
        m.closed() > 0 && s.got().ends_with("c1:active,c1:ended"),
        "closed={} events={}",
        m.closed(),
        s.got()
    );
    assert!(
        d.hangup_call("c1").unwrap_err().is(Sentinel::NoSuchCall),
        "again"
    );
    tokio::time::sleep(ms(50)).await; // the media's done must not end a second time
    assert_eq!(
        s.got().matches("ended").count(),
        1,
        "ended reported more than once"
    );
}

// TestTalkHangupOfARingingCallEndsIt
#[tokio::test]
async fn talk_hangup_of_a_ringing_call_ends_it() {
    let (d, s, _, _) = new_talk(TalkTiming::default()).await;
    ring(&d, "c1", MIN).unwrap();
    d.hangup_call("c1").unwrap();
    assert!(
        !d.ringing_call("c1") && s.got().ends_with("c1:ended"),
        "events={}",
        s.got()
    );
}

// TestTalkDetachHangsUpAndMediaEndingEndsTheCall
#[tokio::test]
async fn talk_detach_hangs_up_and_media_ending_ends_the_call() {
    let (d, s, _, m) = new_talk(TalkTiming::default()).await;
    ring(&d, "c1", MIN).unwrap();
    let a = answer(&d, "c1").await.unwrap();
    a.detach();
    assert!(
        m.detached() == 1 && m.closed() > 0 && s.got().ends_with("c1:ended"),
        "detached={} closed={} events={}",
        m.detached(),
        m.closed(),
        s.got()
    );
    assert!(
        s.heard().contains("|c1|ended|hung up (detach)"),
        "{}",
        s.heard()
    );

    let (d2, s2, _, m2) = new_talk(TalkTiming::default()).await;
    ring(&d2, "c2", MIN).unwrap();
    answer(&d2, "c2").await.unwrap();
    m2.close(); // ffmpeg died, say
    wait_for("c2 ended", move || s2.got().ends_with("c2:ended")).await;
}

// TestTalkStopClosesMediaSilently
#[tokio::test]
async fn talk_stop_closes_media_silently() {
    let (d, s, _, m) = new_talk(TalkTiming::default()).await;
    ring(&d, "c1", MIN).unwrap();
    let _ = answer(&d, "c1").await;
    let before = s.got();
    d.stop().await.unwrap();
    tokio::time::sleep(ms(30)).await;
    assert!(
        m.closed() > 0 && s.got() == before,
        "closed={} events {before:?} -> {:?}",
        m.closed(),
        s.got()
    );
    assert!(ring(&d, "c9", MIN).is_err(), "ring accepted after Stop");
}

// TestTalkSourceHasNoDoorOpeningOrHACalls: the Go test greps talk.go; the same words, over the
// four files of the family, code lines only.
#[test]
fn talk_source_has_no_door_opening_or_ha_calls() {
    for f in [
        "talk.rs",
        "talk_media.rs",
        "talk_down.rs",
        "talk_dial.rs",
        "talkback.rs",
    ] {
        let p = format!("{}/src/driver/unifiprotect/{f}", env!("CARGO_MANIFEST_DIR"));
        let src = std::fs::read_to_string(&p).unwrap().to_lowercase();
        let code: Vec<&str> = src
            .lines()
            .filter(|l| !l.trim_start().starts_with("//"))
            // the unit tests of the file may name the words they check
            .take_while(|l| !l.contains("#[cfg(test)]"))
            .collect();
        for bad in [
            "lock.unlock",
            "unlock_door",
            "\"unlock\"",
            "lock.open",
            "open_door",
            "relay_open",
            "services/",
            "supervisor",
        ] {
            for l in &code {
                assert!(!l.contains(bad), "{f} mentions {bad:?} in code: {l}");
            }
        }
    }
}

// ---- talk_dial_test.go -----------------------------------------------------------------------

// TestTalkDialIsOffWhenTheEntrySaysSoAndOpensNothing
#[tokio::test]
async fn talk_dial_is_off_when_the_entry_says_so_and_opens_nothing() {
    let mut d = TalkDriver::default();
    d.configure(&talk_cfg_with(json!({"dial_enabled": false})))
        .unwrap();
    assert!(
        !d.capabilities().can_dial,
        "CanDial with dial_enabled false"
    );
    let op = FakeOpener::new();
    d.set_opener(op.clone());
    let d = Arc::new(d);
    d.start(&CancelToken::new(), TalkSink::new()).await.unwrap();
    let e = dial(&d, "out-1").await.unwrap_err();
    assert!(
        e.is(Sentinel::NotSupported) && op.opens() == 0,
        "err {e} opens {}",
        op.opens()
    );
    assert!(
        e.message()
            .contains("dial out is switched off for this door"),
        "{e}"
    );
    let mut bad = TalkDriver::default();
    assert!(
        bad.configure(&talk_cfg_with(json!({"dial_enabled": "yes"})))
            .is_err(),
        "a non-bool dial_enabled was accepted"
    );
}

// TestTalkDialOpensMediaReportsInCallAndTheHubClaimsTheAudio
#[tokio::test]
async fn talk_dial_opens_media_reports_in_call_and_the_hub_claims_the_audio() {
    use transport_rs::driver::AudioClaimer;
    let (d, s, op, m) = new_talk(TalkTiming::default()).await;
    dial(&d, "out-1").await.unwrap();
    assert_eq!(op.opens(), 1);
    assert_eq!(
        s.got(),
        "avail:ha_event:front:up,out-1:ringing,out-1:answered,out-1:active"
    );
    let a = d
        .claimed_audio("out-1")
        .expect("the hub cannot claim the audio of the dialled call");
    assert!(d.claimed_audio("out-2").is_none(), "claimed another id");
    assert!(!d.ringing_call("out-1"), "a dialled call is not a ring");
    // the phone gets the longer grace to connect its audio leg
    assert_eq!(
        *m.grace.lock().unwrap(),
        Some(TalkTiming::default().dial_orphan)
    );
    a.detach(); // the phone's leg going away hangs the call up and closes the media
    assert!(
        m.closed() > 0 && s.got().contains("out-1:ended"),
        "closed {} events {}",
        m.closed(),
        s.got()
    );
    assert!(d.claimed_audio("out-1").is_none(), "claimed after the end");
}

// TestTalkDialRefusedWhileTheDoorIsInACallAndRingRefusedDuringADial
#[tokio::test]
async fn talk_dial_refused_while_the_door_is_in_a_call_and_ring_refused_during_a_dial() {
    let (d, _, op, _) = new_talk(TalkTiming::default()).await;
    ring(&d, "ring-1", MIN).unwrap();
    let e = dial(&d, "out-1").await.unwrap_err();
    assert!(
        e.is(Sentinel::Busy) && op.opens() == 0,
        "dial over a ring: {e} opens {}",
        op.opens()
    );
    d.hangup_call("ring-1").unwrap();
    dial(&d, "out-1").await.unwrap();
    assert!(
        dial(&d, "out-2").await.unwrap_err().is(Sentinel::Busy),
        "second dial"
    );
    assert!(
        ring(&d, "ring-2", MIN).unwrap_err().is(Sentinel::Busy),
        "ring during a dial"
    );
    let e = answer(&d, "out-1").await.err().unwrap();
    assert!(
        e.is(Sentinel::AlreadyAnswered),
        "answer of a dialled call: {e}"
    );
}

// TestTalkDialFailureReportsNothingAndFreesTheDoor
#[tokio::test]
async fn talk_dial_failure_reports_nothing_and_frees_the_door() {
    let (d, s, op, _) = new_talk(TalkTiming::default()).await;
    op.fail_with(Some(DriverError::new(
        "the old intercom holds the talkback session: busy",
    )));
    assert!(dial(&d, "out-1").await.is_err(), "dial succeeded");
    assert_eq!(
        s.got(),
        "avail:ha_event:front:up",
        "events after a failed dial"
    );
    // Rust-only: the reason travels, scrubbed, as a dial_failed note
    assert!(
        s.heard().contains("|out-1|dial_failed|the old intercom"),
        "{}",
        s.heard()
    );
    op.fail_with(None);
    dial(&d, "out-2").await.expect("the door stayed reserved");
}

// TestTalkDialMediaEndingOnItsOwnEndsTheCall
#[tokio::test]
async fn talk_dial_media_ending_on_its_own_ends_the_call() {
    let (d, s, _, m) = new_talk(TalkTiming::default()).await;
    dial(&d, "out-1").await.unwrap();
    m.close(); // ffmpeg died, or the phone never connected within the grace
    let s2 = s.clone();
    wait_for("out-1 ended", move || s2.got().contains("out-1:ended")).await;
    ring(&d, "ring-1", MIN).expect("the door stayed busy");
}

// TestTalkDialIsCappedInDuration
#[tokio::test]
async fn talk_dial_is_capped_in_duration() {
    let (d, s, _, m) = new_talk(TalkTiming {
        dial_max: ms(50),
        ..TalkTiming::default()
    })
    .await;
    dial(&d, "out-1").await.unwrap();
    let s2 = s.clone();
    wait_for("out-1 ended", move || s2.got().contains("out-1:ended")).await;
    assert!(m.closed() > 0, "media not closed at the cap");
    let s3 = s.clone();
    wait_for("the cap's note", move || {
        s3.heard()
            .contains("|out-1|ended|the call reached its time limit")
    })
    .await;
}

// TestTalkHubHangupEndsADialledCall
#[tokio::test]
async fn talk_hub_hangup_ends_a_dialled_call() {
    let (d, s, _, m) = new_talk(TalkTiming::default()).await;
    dial(&d, "out-1").await.unwrap();
    d.hangup_call("out-1").unwrap();
    assert!(
        m.closed() > 0 && s.got().contains("out-1:ended"),
        "closed {} events {}",
        m.closed(),
        s.got()
    );
    assert!(
        d.hangup_call("out-1").unwrap_err().is(Sentinel::NoSuchCall),
        "second hangup"
    );
}

// ---- Rust-only -------------------------------------------------------------------------------

// fe4dafe (Go): a bell press during our own call out is consumed as "hang up my call out", the
// reason `bell_pressed` reaches the end note, and an external_ring that arrives anyway is Busy.
#[tokio::test]
async fn a_bell_press_during_a_call_out_hangs_it_up_with_its_reason() {
    let (d, s, _, m) = new_talk(TalkTiming::default()).await;
    dial(&d, "out-1").await.unwrap();
    // were an external_ring to arrive anyway it is still refused (one call per door)
    assert!(ring(&d, "ring-1", MIN).unwrap_err().is(Sentinel::Busy));
    assert_eq!(s.rings(), 0, "the bell press rang the phones");
    ReasonedHangupper::hangup_call_why(&*d, "out-1", "bell_pressed").unwrap();
    assert!(s.got().ends_with("out-1:active,out-1:ended"), "{}", s.got());
    assert!(m.closed() > 0);
    assert_eq!(d.health().last_end, "hung up (bell_pressed)");
    assert!(
        s.heard().contains("|out-1|ended|hung up (bell_pressed)"),
        "{}",
        s.heard()
    );
    // the door is free for the next ring
    ring(&d, "ring-1", MIN).unwrap();
}

// Rust-only: the reason is trimmed, scrubbed of URLs and capped; control characters stay in the
// note (JSON escapes them on the wire)
#[tokio::test]
async fn the_hangup_reason_is_trimmed_and_scrubbed() {
    let (d, s, _, _) = new_talk(TalkTiming::default()).await;
    ring(&d, "c1", MIN).unwrap();
    d.hangup_call_why("c1", "  see rtsp://u:p@h/x  ").unwrap();
    // the scrub's `\S+` takes the closing parenthesis along with the URL, as Go's does
    assert!(
        s.heard().ends_with("|c1|ended|hung up (see <url>"),
        "{}",
        s.heard()
    );
    ring(&d, "c2", MIN).unwrap();
    d.hangup_call_why("c2", "   ").unwrap();
    assert!(s.heard().ends_with("|c2|ended|hung up"), "{}", s.heard());
    ring(&d, "c3", MIN).unwrap();
    d.hangup_call_why("c3", &"x".repeat(1000)).unwrap();
    assert!(d.health().last_end.len() <= 300);
}

// Rust-only: an answer future that is dropped while the media opens gives the ring back, with
// what is left of its hold (Go: a cancelled context ends the open with an error)
#[tokio::test]
async fn a_dropped_answer_gives_the_ring_back() {
    let (d, s, op, _) = new_talk(short_holds()).await;
    let gate = Arc::new(tokio::sync::Notify::new());
    *op.gate.lock().unwrap() = Some(gate);
    ring(&d, "c1", ms(400)).unwrap();
    let r = tokio::time::timeout(ms(80), answer(&d, "c1")).await;
    assert!(r.is_err(), "the gated open finished");
    assert!(d.ringing_call("c1"), "the dropped answer kept the ring");
    // and its hold still runs: the ring ends on its own
    let s2 = s.clone();
    wait_for("hold", move || s2.got().ends_with("c1:ended")).await;
    // a ring whose hold ran out WHILE the answer was dropped ends at once
    let (d, s, op, _) = new_talk(short_holds()).await;
    *op.gate.lock().unwrap() = Some(Arc::new(tokio::sync::Notify::new()));
    ring(&d, "c1", ms(40)).unwrap();
    let _ = tokio::time::timeout(ms(150), answer(&d, "c1")).await;
    let s2 = s.clone();
    wait_for("overdue ring ended", move || s2.got().ends_with("c1:ended")).await;
}

// Rust-only: Go calls expire when the hold ran out during a FAILED open: the call ends before the
// error is returned
#[tokio::test]
async fn a_hold_that_runs_out_during_a_failed_open_ends_the_ring_at_once() {
    let (d, s, op, _) = new_talk(short_holds()).await;
    let gate = Arc::new(tokio::sync::Notify::new());
    *op.gate.lock().unwrap() = Some(gate.clone());
    op.fail_with(Some(DriverError::new("no")));
    ring(&d, "c1", ms(40)).unwrap();
    let d2 = d.clone();
    let t = tokio::spawn(async move { answer(&d2, "c1").await.map(|_| ()) });
    tokio::time::sleep(ms(120)).await; // the timer was stopped while answering: the call lives
    assert!(!s.got().contains("c1:ended"));
    gate.notify_one();
    assert!(t.await.unwrap().is_err());
    assert!(s.got().ends_with("c1:ended"), "{}", s.got());
    assert!(s.heard().contains("|c1|answer_failed|no"), "{}", s.heard());
}

// Rust-only: a stop or a hang-up while the media opens closes the media and answers NoSuchCall
#[tokio::test]
async fn a_stop_or_a_hangup_while_the_media_opens_closes_it() {
    for stop in [true, false] {
        let (d, s, op, m) = new_talk(TalkTiming::default()).await;
        let gate = Arc::new(tokio::sync::Notify::new());
        *op.gate.lock().unwrap() = Some(gate.clone());
        ring(&d, "c1", MIN).unwrap();
        let d2 = d.clone();
        let t = tokio::spawn(async move { answer(&d2, "c1").await.map(|_| ()) });
        let op2 = op.clone();
        wait_for("opening", move || op2.opens() == 1).await;
        if stop {
            d.stop().await.unwrap();
        } else {
            d.hangup_call("c1").unwrap();
        }
        gate.notify_one();
        let e = t.await.unwrap().unwrap_err();
        assert!(e.is(Sentinel::NoSuchCall), "{e}");
        assert!(e.message().contains("ended while answering"), "{e}");
        assert!(m.closed() > 0, "the late media was left open (stop={stop})");
        assert!(!s.got().contains("answered"), "{}", s.got());
    }
}

// Rust-only: a dial future that is dropped while the media opens gives the door back; a stop
// or hang-up in the window closes the media
#[tokio::test]
async fn a_dropped_dial_frees_the_door_and_a_stop_during_a_dial_closes_the_media() {
    let (d, s, op, m) = new_talk(TalkTiming::default()).await;
    let gate = Arc::new(tokio::sync::Notify::new());
    *op.gate.lock().unwrap() = Some(gate.clone());
    assert!(tokio::time::timeout(ms(60), dial(&d, "out-1"))
        .await
        .is_err());
    ring(&d, "r1", MIN).expect("the door stayed reserved by a dropped dial");
    d.hangup_call("r1").unwrap();
    // a stop while the dial opens
    let d2 = d.clone();
    let t = tokio::spawn(async move { dial(&d2, "out-2").await });
    let op2 = op.clone();
    wait_for("opening", move || op2.opens() == 2).await;
    d.stop().await.unwrap();
    gate.notify_one();
    let e = t.await.unwrap().unwrap_err();
    assert!(
        e.is(Sentinel::NoSuchCall) && e.message().contains("ended while dialling"),
        "{e}"
    );
    assert!(m.closed() > 0);
    assert!(!s.got().contains("out-2:"), "{}", s.got());
    // refusals of a stopped or unstarted driver
    assert!(dial(&d, "out-3")
        .await
        .unwrap_err()
        .is(Sentinel::NotSupported));
    let mut fresh = TalkDriver::default();
    fresh.configure(&talk_cfg()).unwrap();
    assert!(dial(&fresh, "x")
        .await
        .unwrap_err()
        .is(Sentinel::NotSupported));
    assert!(dial(&d, "")
        .await
        .unwrap_err()
        .message()
        .contains("no call id"));
    assert!(ring(&fresh, "x", MIN)
        .unwrap_err()
        .is(Sentinel::NotSupported));
    assert_eq!(
        ring(&d, "", MIN).unwrap_err().message(),
        "unifi_protect_talk: external_ring needs a call_id"
    );
}

// Rust-only: a sink that panics while a timer ends a ring costs neither the process nor the door
#[tokio::test]
async fn a_sink_that_panics_on_an_expiry_is_contained() {
    struct Boom;
    impl EventSink for Boom {
        fn ringing(&self, _: &str, _: &str) {}
        fn state_changed(&self, _: &str, _: &str) {
            panic!("sink boom {}", "rtsp://u:p@h/x");
        }
        fn availability_changed(&self, _: &str, _: bool, _: &str) {}
    }
    let mut d = TalkDriver::default();
    d.set_timing(short_holds());
    d.configure(&talk_cfg()).unwrap();
    let d = Arc::new(d);
    d.start(&CancelToken::new(), Arc::new(Boom)).await.unwrap();
    ring(&d, "c1", ms(20)).unwrap();
    tokio::time::sleep(ms(150)).await;
    assert!(!d.ringing_call("c1"), "the panic left the ring");
    ring(&d, "c2", MIN).expect("the door is usable");
}

// Rust-only: a second start keeps one availability report per start and `stop` is repeatable
#[tokio::test]
async fn start_reports_available_and_stop_is_repeatable() {
    let (d, s, _, _) = new_talk(TalkTiming::default()).await;
    assert_eq!(s.got(), "avail:ha_event:front:up");
    d.stop().await.unwrap();
    d.stop().await.unwrap();
    let s2 = TalkSink::new();
    d.start(&CancelToken::new(), s2.clone()).await.unwrap();
    assert_eq!(s2.got(), "avail:ha_event:front:up");
    ring(&d, "c1", MIN).expect("restarted driver takes rings");
}

// R3B-4, PARITY 462 (R2-102): `configure` of a talk driver that holds a call is refused with the
// exact text; once the call is hung up the same driver configures. Mutation: the
// `call.is_some()` refusal in `TalkDriver::configure` disabled.
#[tokio::test]
async fn talk_configure_is_refused_while_the_driver_holds_a_call() {
    let mut d = TalkDriver::default();
    d.set_timing(TalkTiming::default());
    d.configure(&talk_cfg()).unwrap();
    d.set_opener(FakeOpener::new());
    d.start(&CancelToken::new(), TalkSink::new()).await.unwrap();
    ring(&d, "c1", MIN).unwrap();
    let _audio = answer(&d, "c1").await.unwrap();
    let e = d.configure(&talk_cfg()).unwrap_err();
    assert_eq!(
        e.message(),
        "unifi_protect_talk: cannot configure a driver that is in a call; stop it first"
    );
    d.hangup_call("c1").unwrap();
    d.configure(&talk_cfg())
        .expect("a driver without a call configures again");
}

// Rust-only: the concurrent hammer: rings, answers, hang-ups and dials from many tasks leave the
// door consistent (no panic, one call at most, ends reported once per call)
#[tokio::test]
async fn concurrent_rings_answers_and_hangups_keep_one_call_per_door() {
    let (d, s, _, _) = new_talk(short_holds()).await;
    let mut tasks = Vec::new();
    for i in 0..8 {
        let d = d.clone();
        tasks.push(tokio::spawn(async move {
            for j in 0..30 {
                let id = format!("c{}", (i + j) % 5);
                let _ = ring(&d, &id, ms(20));
                let _ = answer(&d, &id).await;
                let _ = d.hangup_call(&id);
                let _ = dial(&d, &format!("o{i}-{j}")).await;
                let _ = d.hangup_call(&format!("o{i}-{j}"));
                tokio::task::yield_now().await;
            }
        }));
    }
    for t in tasks {
        t.await.unwrap();
    }
    tokio::time::sleep(ms(100)).await;
    // per call id: an `active` is always followed by its `ended` before the next `active`
    let got = s.got();
    let mut open: std::collections::HashMap<&str, bool> = Default::default();
    for e in got.split(',') {
        match e.rsplit_once(':') {
            Some((id, "active")) => {
                assert!(
                    !open.insert(id, true).unwrap_or(false),
                    "{id} went active twice"
                );
            }
            Some((id, "ended")) => {
                open.insert(id, false);
            }
            _ => {}
        }
    }
    assert!(
        open.values().all(|o| !o),
        "a call never ended ({} events)",
        got.split(',').count()
    );
    assert!(!d.health().session_up);
}

// Rust-only (PARITY.md 154): the cap timer of a call out ends only the call it was armed for. A
// call out hung up and placed again with the same id (the phone's second press) keeps its own
// full duration.
#[tokio::test]
async fn the_cap_of_an_earlier_call_out_does_not_end_a_later_one_with_the_same_id() {
    let (d, s, op, _) = new_talk(TalkTiming {
        dial_max: ms(200),
        ..TalkTiming::default()
    })
    .await;
    dial(&d, "out-1").await.unwrap();
    tokio::time::sleep(ms(80)).await;
    d.hangup_call("out-1").unwrap();
    *op.media.lock().unwrap() = FakeMedia::new(); // the first one is closed for good
    dial(&d, "out-1").await.unwrap(); // its own cap is at about t=280 ms
    tokio::time::sleep(ms(170)).await; // t=250: the first cap (t=200) has passed
    assert_eq!(s.got().matches("out-1:ended").count(), 1, "{}", s.got());
    assert!(
        d.claimed_audio_for_test("out-1"),
        "the second call was ended by the first one's cap"
    );
    let s2 = s.clone();
    wait_for("the second cap", move || {
        s2.got().matches("out-1:ended").count() == 2
    })
    .await;
}
