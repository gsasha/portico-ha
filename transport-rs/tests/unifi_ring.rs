//! The `unifi_protect` driver (slice 7a) against a fake Protect console on loopback: the tests of
//! `unifiprotect_test.go` and `ring_test.go` that need no `ffmpeg` (the Go name is in a comment on
//! each), and the behaviours this port adds. Nothing here reaches anything but 127.0.0.1.

mod common;

use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use common::unifi::*;
use serde_json::{json, Map, Value};
use tokio::time::Instant;
use transport_rs::call::ClientLeg;
use transport_rs::cancel::CancelToken;
use transport_rs::driver::unifiprotect::api::{ProtectApi, TalkbackSession};
use transport_rs::driver::unifiprotect::tls::Tls;
use transport_rs::driver::unifiprotect::{OpenedTalkback, TalkbackOpener, Timing, UnifiProtect};
use transport_rs::driver::{
    BoxFuture, CallHangupper, Driver, DriverError, EventSink, MediaTarget, Sentinel, StreamSourcer,
    TalkbackAnswerer, TalkbackSink,
};
use transport_rs::webrtc::{join_call_to_browser_with, RelayRegistry};

fn ms(n: u64) -> Duration {
    Duration::from_millis(n)
}

fn cfg(host: &str, extra: Value) -> Map<String, Value> {
    let mut m = json!({"nvr_host": host, "nvr_api_key": TEST_KEY, "camera_id": "cam1"})
        .as_object()
        .unwrap()
        .clone();
    if let Some(e) = extra.as_object() {
        for (k, v) in e {
            m.insert(k.clone(), v.clone());
        }
    }
    m
}

/// `newConfiguredHost`: a configured driver against a plain-HTTP console (Go sets `scheme`).
fn configured(host: &str, extra: Value, t: Timing) -> UnifiProtect {
    let mut d = UnifiProtect::default();
    d.set_scheme("http");
    d.set_timing(t);
    d.configure(&cfg(host, extra)).unwrap();
    d
}

/// `started`: a configured driver with `ring_events` on, started against `f`.
async fn started(f: &FakeConsole, extra: Value) -> (Arc<UnifiProtect>, Arc<RecSink>) {
    let mut e = json!({"ring_events": true});
    for (k, v) in extra.as_object().cloned().unwrap_or_default() {
        e[k] = v;
    }
    let d = Arc::new(configured(&f.host(), e, fast()));
    let s = RecSink::new();
    d.start(&CancelToken::new(), s.clone()).await.unwrap();
    (d, s)
}

fn ring_json(typ: &str, id: &str, device: &str, end: &str) -> String {
    let e = if end.is_empty() {
        String::new()
    } else {
        format!(",\"end\":{end}")
    };
    format!(
        r#"{{"type":"{typ}","item":{{"id":"{id}","modelKey":"event","type":"ring","start":1700000000000,"device":"{device}"{e}}}}}"#
    )
}

// ---- unifiprotect_test.go ------------------------------------------------------------------

// TestConfigureValidates
#[test]
fn configure_validates() {
    for c in [
        json!({}),
        json!({"nvr_host": "h", "nvr_api_key": "k"}),
        json!({"nvr_host": "h", "camera_id": "c"}),
        json!({"nvr_api_key": "k", "camera_id": "c"}),
    ] {
        let mut d = UnifiProtect::default();
        assert!(
            d.configure(c.as_object().unwrap()).is_err(),
            "{c} accepted an incomplete config"
        );
    }
    let mut d = UnifiProtect::default();
    d.configure(
        json!({"nvr_host": "h", "nvr_api_key": "k", "camera_id": "c"})
            .as_object()
            .unwrap(),
    )
    .expect("complete config rejected");
}

// The error texts Python shows (added: Go's tests only check that these fail).
#[test]
fn configure_error_texts_and_odd_values() {
    let mut d = UnifiProtect::default();
    let e = d.configure(json!({}).as_object().unwrap()).unwrap_err();
    assert_eq!(
        e.message(),
        "unifi_protect: nvr_host, nvr_api_key and camera_id are required"
    );
    for host in ["h/x", "u@h", "h h", "https://h"] {
        let e = d
            .configure(
                json!({"nvr_host": host, "nvr_api_key": "k", "camera_id": "c"})
                    .as_object()
                    .unwrap(),
            )
            .unwrap_err();
        assert_eq!(
            e.message(),
            "unifi_protect: nvr_host must be a bare host or host:port",
            "{host}"
        );
    }
    // non-string values count as absent
    let e = d
        .configure(
            json!({"nvr_host": 5, "nvr_api_key": "k", "camera_id": "c"})
                .as_object()
                .unwrap(),
        )
        .unwrap_err();
    assert!(e.message().contains("are required"));
    // ring_events: absent false, bool as given, anything else (null included) refused
    for (v, want) in [(json!(true), true), (json!(false), false)] {
        d.configure(&cfg("h", json!({"ring_events": v}))).unwrap();
        assert_eq!(d.capabilities().reports_availability, want);
    }
    for v in [json!(null), json!("yes"), json!(1), json!([])] {
        let e = d
            .configure(&cfg("h", json!({"ring_events": v})))
            .unwrap_err();
        assert_eq!(
            e.message(),
            "unifi_protect: ring_events must be true or false",
            "{v}"
        );
    }
    // a host or key that could end a header line is refused, and the key is never echoed (119)
    for host in [
        "h\tx",
        "h\nx",
        "h\r\nX: y",
        "h?x",
        "h#x",
        "h\\x",
        "h\u{7f}",
        "h\u{1}",
    ] {
        let e = d.configure(&cfg(host, json!({}))).unwrap_err();
        assert!(
            e.message()
                .starts_with("unifi_protect: nvr_host must be a bare host"),
            "{host:?}: {e}"
        );
    }
    for key in ["k\r\nX: y", "k\n", "k\u{0}", "k\u{7f}"] {
        let mut c = cfg("h", json!({}));
        c.insert("nvr_api_key".into(), json!(key));
        let e = d.configure(&c).unwrap_err();
        assert_eq!(
            e.message(),
            "unifi_protect: nvr_api_key must not contain control characters"
        );
        assert!(!e.message().contains("X: y"));
    }
    let mut c = cfg("h", json!({}));
    c.insert("nvr_api_key".into(), json!("a key with spaces"));
    d.configure(&c).unwrap();
    // verify_ssl and stream_quality of the wrong type fall back to their defaults
    d.configure(&cfg("h", json!({"verify_ssl": "no", "stream_quality": 3})))
        .unwrap();
    // a failed validation leaves the previous configuration in place
    d.configure(&cfg("h", json!({"ring_events": true})))
        .unwrap();
    assert!(d.configure(&cfg("h", json!({"ring_events": "x"}))).is_err());
    assert!(
        d.capabilities().reports_availability,
        "the failed configure replaced the config"
    );
}

// TestStartAndStopTouchNoNetwork
#[tokio::test]
async fn start_and_stop_touch_no_network() {
    let f = FakeProtect::new().await;
    let d = configured(&f.host(), json!({}), fast());
    d.start(&CancelToken::new(), RecSink::new()).await.unwrap();
    d.stop().await.unwrap();
    assert!(
        f.reqs().is_empty(),
        "Start/Stop made requests: {:?}",
        f.reqs()
    );
}

// R3B-4, PARITY 462 (R2-102): `configure` of a driver that runs a watcher is refused with the exact
// text, the running watcher is still stopped by `stop`, and once stopped the same driver configures.
// Mutation: the `st.watch.is_some() || ..` refusal in `UnifiProtect::configure` disabled.
#[tokio::test]
async fn configure_is_refused_while_the_driver_runs_a_watcher() {
    let f = FakeProtect::new().await;
    let c = cfg(&f.host(), json!({"ring_events": true}));
    let mut d = configured(&f.host(), json!({"ring_events": true}), fast());
    d.start(&CancelToken::new(), RecSink::new()).await.unwrap();
    let e = d.configure(&c).unwrap_err();
    assert_eq!(
        e.message(),
        "unifi_protect: cannot configure a driver that is running a watcher or a call; stop it first"
    );
    d.stop().await.unwrap();
    d.configure(&c).expect("a stopped driver configures again");
}

// TestDialAndAnswerAreNotSupported
#[tokio::test]
async fn dial_and_answer_are_not_supported() {
    let f = FakeProtect::new().await;
    let d = configured(&f.host(), json!({}), fast());
    let ctx = CancelToken::new();
    let e = d.dial(&ctx, MediaTarget::default()).await.unwrap_err();
    assert!(e.is(Sentinel::NotSupported), "Dial = {e}");
    assert_eq!(
        e.message(),
        "unifi_protect: no SIP dialog to dial (talkback is REST + RTP push): not_supported"
    );
    let e = d.answer(&ctx, MediaTarget::default()).await.unwrap_err();
    assert!(e.is(Sentinel::NotSupported), "Answer = {e}");
    assert_eq!(
        e.message(),
        "unifi_protect: no SIP dialog to answer (talkback is REST + RTP push): not_supported"
    );
}

// TestStreamURLMintsWithKeyAndQuality
#[tokio::test]
async fn stream_url_mints_with_key_and_quality() {
    let f = FakeProtect::new().await;
    let d = configured(&f.host(), json!({}), fast());
    let u = d.stream_url(&CancelToken::new()).await.unwrap();
    assert_eq!(u, "rtsps://nvr.example:7441/TOKEN123?enableSrtp");
    assert_eq!(
        f.reqs(),
        vec!["POST /proxy/protect/integration/v1/cameras/cam1/rtsps-stream"]
    );
    assert_eq!(f.keys()[0], TEST_KEY);
    assert!(
        f.bodies()[0].contains(r#""qualities":["high"]"#),
        "body = {}",
        f.bodies()[0]
    );
    // what else the request carried (added): the headers Go sends, the JSON body exactly
    let h = &f.heads()[0];
    assert_eq!(header(h, "Accept"), Some("application/json"));
    assert_eq!(header(h, "Content-Type"), Some("application/json"));
    assert_eq!(header(h, "Content-Length"), Some("22"));
    assert_eq!(f.bodies()[0], r#"{"qualities":["high"]}"#);
    assert_eq!(header(h, "Host"), Some(f.host().as_str()));
}

// stream_quality reaches the request (added)
#[tokio::test]
async fn stream_quality_is_sent_and_looked_up() {
    let f = FakeProtect::new().await;
    f.st.lock().unwrap().rtsps = "rtsps://nvr/T".into();
    let d = configured(&f.host(), json!({"stream_quality": "medium"}), fast());
    // the fake answers {"high": ...}: "medium" is not in it
    let e = d.stream_url(&CancelToken::new()).await.unwrap_err();
    assert_eq!(
        e.message(),
        r#"rtsps-stream: no "medium" stream in the response"#
    );
    assert_eq!(f.bodies()[0], r#"{"qualities":["medium"]}"#);
}

// TestStreamURLRejectsBadResponses
#[tokio::test]
async fn stream_url_rejects_bad_responses() {
    let f = FakeProtect::new().await;
    let d = configured(&f.host(), json!({}), fast());
    f.set_rtsps("http://evil.example/x");
    let e = d.stream_url(&CancelToken::new()).await.unwrap_err();
    assert_eq!(e.message(), "rtsps-stream: unexpected stream scheme");
    f.set_rtsps("");
    let e = d.stream_url(&CancelToken::new()).await.unwrap_err();
    assert_eq!(
        e.message(),
        r#"rtsps-stream: no "high" stream in the response"#
    );
}

// TestErrorsNeverCarryKeyOrBody
#[tokio::test]
async fn errors_never_carry_key_or_body() {
    let f = FakeProtect::new().await;
    f.set_status(401);
    let d = configured(&f.host(), json!({}), fast());
    let e = d.stream_url(&CancelToken::new()).await.unwrap_err();
    assert!(
        !e.message().contains(TEST_KEY) && !e.message().contains("TOKEN-IN-ERROR-BODY"),
        "{e}"
    );
    assert!(
        e.message().contains("401"),
        "error should name the status: {e}"
    );
    assert_eq!(
        e.message(),
        "POST /proxy/protect/integration/v1/cameras/cam1/rtsps-stream: HTTP 401"
    );
}

// TestCapabilitiesUnchanged
#[test]
fn capabilities_unchanged() {
    let c = UnifiProtect::default().capabilities();
    assert_eq!(
        c.inbound_audio,
        transport_rs::driver::AudioTopology::IN_VIDEO_STREAM
    );
    assert_eq!(
        c.outbound_audio,
        transport_rs::driver::AudioTopology::TALKBACK_PUSH
    );
    assert!(!c.direct_capable && c.has_relay && !c.reports_availability && !c.can_dial);
    assert_eq!(c.rtsp_template, "rtsps://{nvr_host}/{camera_id}");
}

// TestImplementsStreamSourcer, TestImplementsCallHangupper (the type assertions of the hub)
#[test]
fn implements_stream_sourcer_and_call_hangupper() {
    let d = UnifiProtect::default();
    assert!(Driver::as_stream_sourcer(&d).is_some());
    assert!(Driver::as_call_hangupper(&d).is_some());
    // not these (Go's driver implements none of them)
    assert!(Driver::as_audio_claimer(&d).is_none());
    assert!(Driver::as_call_dialer(&d).is_none());
    assert!(Driver::as_connect_answerer(&d).is_none());
    assert!(Driver::as_external_ringer(&d).is_none());
    assert!(Driver::as_pending_ringer(&d).is_none());
    // since slice 7b the ffmpeg opener is installed from the start: the hub's accessor is there
    assert!(Driver::as_talkback_answerer(&d).is_some());
    d.clear_talkback_opener();
    assert!(Driver::as_talkback_answerer(&d).is_none());
}

// not configured (added)
#[tokio::test]
async fn an_unconfigured_driver_refuses_the_api_calls() {
    let d = UnifiProtect::default();
    let ctx = CancelToken::new();
    assert_eq!(
        d.stream_url(&ctx).await.unwrap_err().message(),
        "unifi_protect: not configured"
    );
    assert_eq!(
        d.open_relay(&ctx).await.unwrap_err().message(),
        "unifi_protect: not configured"
    );
    d.start(&ctx, RecSink::new()).await.unwrap();
    d.stop().await.unwrap();
}

// ---- ring_test.go --------------------------------------------------------------------------

// TestRingEventsMustBeABool
#[test]
fn ring_events_must_be_a_bool() {
    let mut d = UnifiProtect::default();
    d.set_scheme("http");
    let r = d.configure(
        json!({"nvr_host": "h", "nvr_api_key": "k", "camera_id": "c", "ring_events": "yes"})
            .as_object()
            .unwrap(),
    );
    assert!(r.is_err(), "a non-bool ring_events was accepted");
}

// TestRingWatchingIsOffByDefault
#[tokio::test]
async fn ring_watching_is_off_by_default() {
    let f = FakeConsole::new(false).await;
    let d = configured(&f.host(), json!({}), fast());
    d.start(&CancelToken::new(), RecSink::new()).await.unwrap();
    tokio::time::sleep(ms(150)).await;
    assert_eq!(
        f.attempt_count(),
        0,
        "Start without ring_events made connection attempts"
    );
    assert!(
        !d.capabilities().reports_availability,
        "ReportsAvailability must stay false when nothing is watched"
    );
    d.stop().await.unwrap();
}

// TestStartWithRingEventsConnectsWithTheKeyAndReportsAvailable
#[tokio::test]
async fn start_with_ring_events_connects_with_the_key_and_reports_available() {
    let f = FakeConsole::new(false).await;
    let (d, s) = started(&f, json!({})).await;
    f.last_conn().await;
    wait_for("available", || s.has("available ")).await;
    assert_eq!(f.keys()[0], TEST_KEY);
    assert_eq!(
        f.paths()[0],
        "/proxy/protect/integration/v1/subscribe/events"
    );
    assert!(
        d.capabilities().reports_availability,
        "ReportsAvailability must be true when the ring stream is watched"
    );
    assert_eq!(s.list()[0], format!("available {}", f.host()));
    // the upgrade request, byte for byte but the random key (added)
    let h = &f.heads()[0];
    assert!(h.starts_with("GET /proxy/protect/integration/v1/subscribe/events HTTP/1.1\r\n"));
    let lines: Vec<&str> = h.trim_end().split("\r\n").collect();
    assert_eq!(lines[1], format!("Host: {}", f.host()));
    assert_eq!(&lines[2..4], ["Upgrade: websocket", "Connection: Upgrade"]);
    assert_eq!(lines[4], "Sec-WebSocket-Version: 13");
    assert!(
        lines[5].starts_with("Sec-WebSocket-Key: ")
            && lines[5].len() == "Sec-WebSocket-Key: ".len() + 24
    );
    assert_eq!(lines[6], format!("X-API-KEY: {TEST_KEY}"));
    assert_eq!(lines.len(), 7);
    d.stop().await.unwrap();
}

// TestRingEdgeBecomesRingingForOurCameraOnly
#[tokio::test]
async fn ring_edge_becomes_ringing_for_our_camera_only() {
    let f = FakeConsole::new(false).await;
    let (d, s) = started(&f, json!({})).await;
    f.last_conn().await;
    f.send(&ring_json("add", "e0", "someOtherCam", "")).await;
    f.send(r#"{"type":"add","item":{"id":"m1","modelKey":"event","type":"motion","start":1,"device":"cam1"}}"#).await;
    f.send("not json at all").await;
    f.send(r#"{"type":"add","item":{"id":"","modelKey":"event","type":"ring","start":1,"device":"cam1"}}"#).await;
    f.send(&ring_json("update", "ghost", "cam1", "5")).await;
    f.send(&ring_json("add", "evt1", "cam1", "")).await;
    wait_for("ringing", || s.has("ringing ")).await;
    for e in s.list() {
        if e.starts_with("ringing ") {
            assert_eq!(
                e,
                format!("ringing unifi-evt1 {}", f.host()),
                "all: {:?}",
                s.list()
            );
        }
    }
    assert_eq!(s.count("ringing "), 1, "{:?}", s.list());
    d.stop().await.unwrap();
}

// DEV-796 D10: an event id that is long or has odd characters is ignored.
#[tokio::test]
async fn an_event_id_that_is_long_or_odd_is_ignored() {
    let f = FakeConsole::new(false).await;
    let (d, s) = started(&f, json!({})).await;
    f.last_conn().await;
    f.send(&ring_json("add", &"a".repeat(300), "cam1", ""))
        .await;
    f.send(&ring_json("add", "has space", "cam1", "")).await;
    f.send(&ring_json("add", "a\\u0001b", "cam1", "")).await;
    f.send(&ring_json("add", "ok-id_1", "cam1", "")).await;
    wait_for("ringing", || s.has("ringing ")).await;
    tokio::time::sleep(ms(100)).await;
    assert_eq!(s.count("ringing "), 1, "{:?}", s.list());
    assert!(s.has("ringing unifi-ok-id_1"), "{:?}", s.list());
    d.stop().await.unwrap();
}

// TestASecondRingWhileRingingIsIgnored
#[tokio::test]
async fn a_second_ring_while_ringing_is_ignored() {
    let f = FakeConsole::new(false).await;
    let (d, s) = started(&f, json!({})).await;
    f.last_conn().await;
    f.send(&ring_json("add", "a", "cam1", "")).await;
    f.send(&ring_json("add", "b", "cam1", "")).await;
    wait_for("ringing", || s.has("ringing ")).await;
    tokio::time::sleep(ms(100)).await;
    assert!(
        !s.list().iter().any(|e| e.contains("unifi-b")),
        "second ring reported: {:?}",
        s.list()
    );
    d.stop().await.unwrap();
}

// TestRingEndsAfterTheMinimumHoldOnceTheConsoleSaysItEnded
#[tokio::test]
async fn ring_ends_after_the_minimum_hold_once_the_console_says_it_ended() {
    let f = FakeConsole::new(false).await;
    let (d, s) = started(&f, json!({})).await;
    f.last_conn().await;
    f.send(&ring_json("add", "e1", "cam1", "")).await;
    wait_for("ringing", || s.has("ringing ")).await;
    let t0 = Instant::now();
    f.send(&ring_json("update", "e1", "cam1", "1700000003000"))
        .await;
    wait_for("ended", || s.has("state unifi-e1 ended")).await;
    let el = t0.elapsed();
    assert!(
        el >= fast().ring_min_hold / 2,
        "ended after only {el:?}: the minimum ring hold is not applied"
    );
    d.stop().await.unwrap();
}

// TestRingEndsAfterTheMaximumHoldWithNoEnd
#[tokio::test]
async fn ring_ends_after_the_maximum_hold_with_no_end() {
    let f = FakeConsole::new(false).await;
    let (d, s) = started(&f, json!({})).await;
    f.last_conn().await;
    let t0 = Instant::now();
    f.send(&ring_json("add", "e2", "cam1", "")).await;
    wait_for("ended", || s.has("state unifi-e2 ended")).await;
    let el = t0.elapsed();
    assert!(
        el >= fast().ring_max_hold - ms(100),
        "ended after {el:?}, before the maximum hold"
    );
    // A new ring after the end is a new call.
    f.send(&ring_json("add", "e3", "cam1", "")).await;
    wait_for("ringing e3", || s.has("ringing unifi-e3")).await;
    d.stop().await.unwrap();
}

// TestReconnectsAfterADropAndReportsAvailabilityOnTransitionsOnly
#[tokio::test]
async fn reconnects_after_a_drop_and_reports_availability_on_transitions_only() {
    let f = FakeConsole::new(false).await;
    let (d, s) = started(&f, json!({})).await;
    f.last_conn().await;
    wait_for("available", || s.has("available ")).await;

    // Drop, then refuse: the reconnect attempt fails, so it becomes unavailable (once), and
    // repeated failures add no more reports.
    f.set_status(503);
    f.drop_all();
    wait_for("unavailable", || s.has("unavailable ")).await;
    tokio::time::sleep(ms(300)).await;
    assert_eq!(s.count("unavailable "), 1, "{:?}", s.list());
    assert!(
        s.list().join("|").contains("HTTP 503"),
        "reason lacks the status: {:?}",
        s.list()
    );
    // Recovery: available again, and a ring works on the new connection.
    f.set_status(0);
    f.last_conn().await;
    wait_for("available twice", || s.count("available ") == 2).await;
    f.send(&ring_json("add", "r1", "cam1", "")).await;
    wait_for("ringing r1", || s.has("ringing unifi-r1")).await;
    d.stop().await.unwrap();
}

// TestAQuickDropAndReconnectIsNotReportedUnavailable
#[tokio::test]
async fn a_quick_drop_and_reconnect_is_not_reported_unavailable() {
    let f = FakeConsole::new(false).await;
    let (d, s) = started(&f, json!({})).await;
    f.last_conn().await;
    wait_for("available", || s.has("available ")).await;
    f.drop_all();
    f.last_conn().await; // reconnected
    tokio::time::sleep(ms(100)).await;
    assert!(
        !s.has("unavailable "),
        "a drop that reconnected at once flapped availability: {:?}",
        s.list()
    );
    d.stop().await.unwrap();
}

// TestRefusedKeyIsUnavailableWithoutLeakingSecrets
#[tokio::test]
async fn refused_key_is_unavailable_without_leaking_secrets() {
    let f = FakeConsole::new(false).await;
    f.set_status(401);
    let (d, s) = started(&f, json!({})).await;
    wait_for("unavailable", || s.has("unavailable ")).await;
    let all = s.list().join("|");
    assert!(all.contains("HTTP 401"), "reason {all:?} lacks the status");
    assert!(all.contains("events stream refused (HTTP 401)"));
    for secret in [TEST_KEY, "TOKEN-IN-ERROR-BODY"] {
        assert!(!all.contains(secret), "report leaks {secret:?}: {all}");
    }
    d.stop().await.unwrap();
}

// TestUnreachableConsoleErrorNamesNoSecretAndBacksOffBounded
#[tokio::test]
async fn unreachable_console_error_names_no_secret_and_backs_off_bounded() {
    let f = FakeConsole::new(false).await;
    let host = f.host();
    drop(f); // nothing listens any more
    tokio::time::sleep(ms(50)).await;
    let d = configured(&host, json!({"ring_events": true}), fast());
    let s = RecSink::new();
    d.start(&CancelToken::new(), s.clone()).await.unwrap();
    wait_for("unavailable", || s.has("unavailable ")).await;
    for e in s.list() {
        assert!(!e.contains(TEST_KEY), "report leaks the key: {e:?}");
    }
    assert!(
        s.list()[0].ends_with("console unreachable"),
        "{:?}",
        s.list()
    );
    // Backoff grows and is capped.
    use transport_rs::driver::unifiprotect::ring::next_backoff;
    let t = fast();
    let mut b = t.backoff_min;
    for _ in 0..10 {
        b = next_backoff(b, t.backoff_max);
    }
    assert_eq!(b, t.backoff_max, "backoff settled at {b:?}, want the cap");
    assert!(
        next_backoff(t.backoff_min, t.backoff_max) > t.backoff_min,
        "backoff does not grow"
    );
    d.stop().await.unwrap();
}

// TestVerifySSLIsHonoured
#[tokio::test]
async fn verify_ssl_is_honoured() {
    let f = FakeConsole::new(true).await; // self-signed certificate
    let run = |verify: bool| {
        let mut d = UnifiProtect::default();
        d.set_timing(fast());
        d.configure(&cfg(
            &f.host(),
            json!({"ring_events": true, "verify_ssl": verify}),
        ))
        .unwrap();
        d
    };
    let bad = run(true);
    let bs = RecSink::new();
    bad.start(&CancelToken::new(), bs.clone()).await.unwrap();
    wait_for("unavailable", || bs.has("unavailable ")).await;
    assert!(
        !bs.has("available ") && f.attempt_count() == 0,
        "verify_ssl true reached a self-signed console: {:?} attempts={}",
        bs.list(),
        f.attempt_count()
    );
    assert!(
        bs.list()[0].ends_with("console certificate not trusted (verify_ssl)"),
        "{:?}",
        bs.list()
    );
    bad.stop().await.unwrap();

    let ok = run(false);
    let os = RecSink::new();
    ok.start(&CancelToken::new(), os.clone()).await.unwrap();
    wait_for("available", || os.has("available ")).await;
    assert!(
        f.attempt_count() > 0,
        "verify_ssl false never reached the console"
    );
    ok.stop().await.unwrap();
}

// Verification is real (added): a certificate the pool trusts is accepted, and a name it is
// not valid for is refused even then.
#[tokio::test]
async fn a_trusted_certificate_is_accepted_and_the_name_is_checked() {
    let f = FakeConsole::new(true).await;
    let trust = || {
        let mut roots = rustls::RootCertStore::empty();
        roots.add(f.cert.clone().unwrap()).unwrap();
        Arc::new(Tls::with_roots(roots))
    };
    let mk = |host: String| {
        let mut d = UnifiProtect::default();
        d.set_timing(fast());
        d.configure(&cfg(
            &host,
            json!({"ring_events": true, "verify_ssl": true}),
        ))
        .unwrap();
        d.set_api_for_test(ProtectApi::with_tls("https", &host, TEST_KEY, trust()));
        d
    };
    let good = mk(f.host());
    let gs = RecSink::new();
    good.start(&CancelToken::new(), gs.clone()).await.unwrap();
    wait_for("available", || gs.has("available ")).await;
    good.stop().await.unwrap();

    // the same certificate under a name it does not cover
    let port = f.addr.port();
    let wrong = mk(format!("localhost:{port}"));
    let ws = RecSink::new();
    wrong.start(&CancelToken::new(), ws.clone()).await.unwrap();
    wait_for("unavailable", || ws.has("unavailable ")).await;
    assert!(
        ws.list()[0].ends_with("console certificate not trusted (verify_ssl)"),
        "{:?}",
        ws.list()
    );
    wrong.stop().await.unwrap();
}

// TestClientHandlesFragmentsPingsAndPongs
#[tokio::test]
async fn client_handles_fragments_pings_and_pongs() {
    let f = FakeConsole::new(false).await;
    let (d, s) = started(&f, json!({})).await;
    f.last_conn().await;
    let msg = ring_json("add", "frag", "cam1", "").into_bytes();
    let half = msg.len() / 2;
    f.send_raw(0x01, &msg[..half]).await; // text, not final
    f.send_raw(0x89, b"ping!").await; // a ping in the middle of a message
    f.send_raw(0x80, &msg[half..]).await; // continuation, final
    wait_for("ringing frag", || s.has("ringing unifi-frag")).await;
    // the client also pings us
    wait_for("two pings", || f.pings() >= 2).await;
    // and answered ours with the same payload (added)
    wait_for("pong", || {
        f.client_frames().contains(&(0xA, b"ping!".to_vec()))
    })
    .await;
    d.stop().await.unwrap();
}

// TestASilentConnectionIsDroppedAndReconnected
#[tokio::test]
async fn a_silent_connection_is_dropped_and_reconnected() {
    let f = FakeConsole::new(false).await;
    let (d, _s) = started(&f, json!({})).await;
    f.last_conn().await;
    f.set_no_pong(true); // the console stops answering pings
    assert!(
        f.connected_within(Duration::from_secs(5)).await,
        "a connection that stopped ponging was never replaced"
    );
    d.stop().await.unwrap();
}

// TestOversizeMessageDropsTheConnectionNotTheProcess
#[tokio::test]
async fn oversize_message_drops_the_connection_not_the_process() {
    let f = FakeConsole::new(false).await;
    let (d, _s) = started(&f, json!({})).await;
    f.last_conn().await;
    // the client may reset mid-write
    f.send_bytes_lossy(&frame(0x1, &vec![b'x'; (1 << 20) + 1]))
        .await;
    assert!(
        f.connected_within(Duration::from_secs(5)).await,
        "no reconnect after an oversize message"
    );
    d.stop().await.unwrap();
}

// TestStopEndsTheWatcherAndIsRepeatable
#[tokio::test]
async fn stop_ends_the_watcher_and_is_repeatable() {
    let f = FakeConsole::new(false).await;
    let (d, s) = started(&f, json!({})).await;
    f.last_conn().await;
    wait_for("available", || s.has("available ")).await; // the client has the 101 too
    d.stop().await.unwrap();
    let before = f.attempt_count();
    tokio::time::sleep(ms(200)).await;
    assert_eq!(
        f.attempt_count(),
        before,
        "the watcher kept connecting after Stop"
    );
    d.stop().await.unwrap();
    // the close frame went out (added)
    wait_for("close frame", || {
        f.client_frames().iter().any(|(op, _)| *op == 0x8)
    })
    .await;
}

// ---- the answer path, over the seam slice 7b fills ------------------------------------------
// The Go tests below start a fake ffmpeg; here the same states are driven through a fake
// `TalkbackOpener` (the REST half, `talkback-session`, is the real client against FakeProtect).

struct FakeSink {
    tx: tokio::sync::mpsc::Sender<Vec<u8>>,
    closed: AtomicBool,
    done: CancelToken,
}

impl TalkbackSink for FakeSink {
    fn input(&self) -> tokio::sync::mpsc::Sender<Vec<u8>> {
        self.tx.clone()
    }
    fn close(&self) -> Result<(), DriverError> {
        self.closed.store(true, Ordering::SeqCst);
        self.done.cancel();
        Ok(())
    }
}

#[derive(Default)]
struct FakeOpener {
    started: AtomicUsize,
    fail: AtomicBool,
    /// never finishes opening (a hung start)
    hang: AtomicBool,
    sessions: Mutex<Vec<TalkbackSession>>,
    sinks: Mutex<Vec<Arc<FakeSink>>>,
    rx: Mutex<Vec<tokio::sync::mpsc::Receiver<Vec<u8>>>>,
}

impl TalkbackOpener for FakeOpener {
    fn start<'a>(
        &'a self,
        _ctx: &'a CancelToken,
        session: TalkbackSession,
    ) -> BoxFuture<'a, Result<OpenedTalkback, DriverError>> {
        Box::pin(async move {
            self.started.fetch_add(1, Ordering::SeqCst);
            self.sessions.lock().unwrap().push(session);
            if self.hang.load(Ordering::SeqCst) {
                std::future::pending::<()>().await;
            }
            if self.fail.load(Ordering::SeqCst) {
                return Err(DriverError::new("ffmpeg not found"));
            }
            let (tx, rx) = tokio::sync::mpsc::channel(8);
            let done = CancelToken::new();
            let sink = Arc::new(FakeSink {
                tx,
                closed: AtomicBool::new(false),
                done: done.clone(),
            });
            self.sinks.lock().unwrap().push(sink.clone());
            self.rx.lock().unwrap().push(rx);
            Ok(OpenedTalkback { sink, done })
        })
    }
}

/// `ffmpegDriver`: a started driver whose talkback REST half is `fc` and whose transcode is `op`.
async fn answerable(
    f: &FakeConsole,
    fc: &FakeProtect,
    op: Arc<FakeOpener>,
) -> (Arc<UnifiProtect>, Arc<RecSink>) {
    let (d, s) = started(f, json!({})).await;
    // The REST half lives on the same console host in production; here it is the plain FakeProtect.
    d.set_api_for_test(ProtectApi::new("http", &fc.host(), TEST_KEY, true));
    d.set_talkback_opener(op);
    (d, s)
}

/// A browser leg whose test fires `connected` and `disconnected` (R3A-4).
struct BrowserLeg {
    on_conn: Mutex<Option<Box<dyn FnOnce() + Send>>>,
    on_disc: Mutex<Vec<Box<dyn FnOnce() + Send>>>,
    in_tx: tokio::sync::mpsc::Sender<Vec<u8>>,
    out_rx: Arc<tokio::sync::Mutex<tokio::sync::mpsc::Receiver<Vec<u8>>>>,
    _keep: (
        tokio::sync::mpsc::Receiver<Vec<u8>>,
        tokio::sync::mpsc::Sender<Vec<u8>>,
    ),
}

impl BrowserLeg {
    fn new() -> Arc<BrowserLeg> {
        let (in_tx, in_rx) = tokio::sync::mpsc::channel(8);
        let (out_tx, out_rx) = tokio::sync::mpsc::channel(8);
        Arc::new(BrowserLeg {
            on_conn: Mutex::new(None),
            on_disc: Mutex::new(Vec::new()),
            in_tx,
            out_rx: Arc::new(tokio::sync::Mutex::new(out_rx)),
            _keep: (in_rx, out_tx),
        })
    }
    fn connect(&self) {
        if let Some(cb) = self.on_conn.lock().unwrap().take() {
            cb();
        }
    }
    fn disconnect(&self) {
        let cbs: Vec<_> = self.on_disc.lock().unwrap().drain(..).collect();
        for cb in cbs {
            cb();
        }
    }
}

impl ClientLeg for BrowserLeg {
    fn on_connected(&self, cb: Box<dyn FnOnce() + Send>) {
        *self.on_conn.lock().unwrap() = Some(cb);
    }
    fn on_disconnected(&self, cb: Box<dyn FnOnce() + Send>) {
        self.on_disc.lock().unwrap().push(cb);
    }
    fn input(&self) -> tokio::sync::mpsc::Sender<Vec<u8>> {
        self.in_tx.clone()
    }
    fn output(&self) -> Arc<tokio::sync::Mutex<tokio::sync::mpsc::Receiver<Vec<u8>>>> {
        self.out_rx.clone()
    }
}

// R3A-4, R2-81 (Go's `gone` flag): the browser that connects and leaves at once, while the talkback
// session is still opening (the ring is not answered yet, so the disconnect callback returns early and
// closes nothing), must not leave its relay in the registry once the session has opened. Mutation:
// the `gone.store(true, ..)` of `answer_via_talkback` deleted: the relay is put and nothing takes it out.
#[tokio::test]
async fn a_browser_that_leaves_while_the_talkback_opens_leaves_no_relay_behind() {
    let f = FakeConsole::new(false).await;
    let fc = FakeProtect::new().await;
    let op = Arc::new(FakeOpener::default());
    let (d, s) = answerable(&f, &fc, op.clone()).await;
    f.last_conn().await;
    f.send(&ring_json("add", "t9", "cam1", "")).await;
    wait_for("ringing", || s.has("ringing unifi-t9")).await;
    let relays = RelayRegistry::new();
    let leg = BrowserLeg::new();
    let drivers: Vec<Arc<dyn Driver>> = vec![d.clone()];
    join_call_to_browser_with(
        &CancelToken::new(),
        &drivers,
        &relays,
        "",
        "unifi-t9",
        leg.clone(),
    );
    leg.connect(); // the open starts on a task of its own
    leg.disconnect(); // and the browser is gone before it ends
    wait_for("the session opened", || !fc.reqs().is_empty()).await;
    tokio::time::sleep(ms(300)).await;
    assert!(
        relays.is_empty(),
        "a relay whose browser had left was registered"
    );
}

// TestAnswerTalkbackOpensTheSessionAndMarksTheCallAnswered
#[tokio::test]
async fn answer_talkback_opens_the_session_and_marks_the_call_answered() {
    let f = FakeConsole::new(false).await;
    let fc = FakeProtect::new().await;
    let op = Arc::new(FakeOpener::default());
    let (d, s) = answerable(&f, &fc, op.clone()).await;
    f.last_conn().await;
    f.send(&ring_json("add", "t1", "cam1", "")).await;
    wait_for("ringing", || s.has("ringing unifi-t1")).await;

    let ta = Driver::as_talkback_answerer(&*d).expect("TalkbackAnswerer");
    assert!(
        ta.ringing_call("unifi-t1") && !ta.ringing_call("nope"),
        "RingingCall does not recognise the live ring"
    );
    assert!(
        fc.reqs().is_empty(),
        "a ring alone touched the talkback API: {:?}",
        fc.reqs()
    );
    let ctx = CancelToken::new();
    let sink = ta
        .answer_talkback(&ctx, "unifi-t1")
        .await
        .expect("AnswerTalkback");
    assert_eq!(
        fc.reqs(),
        vec!["POST /proxy/protect/integration/v1/cameras/cam1/talkback-session"]
    );
    // the session the console returned reached the opener
    assert_eq!(
        op.sessions.lock().unwrap()[0],
        TalkbackSession {
            url: "rtp://192.168.2.9:7004".into(),
            codec: "opus".into(),
            sampling_rate: 24000,
            bits_per_sample: 16
        }
    );
    sink.input().send(vec![0u8; 640]).await.unwrap();
    assert!(d.talkback_audio().is_some());
    assert!(
        !ta.ringing_call("unifi-t1"),
        "an answered call still counts as ringing"
    );
    wait_for("answered+active", || {
        s.has("state unifi-t1 answered") && s.has("state unifi-t1 active")
    })
    .await;
    let ev = s.list();
    let a = ev
        .iter()
        .position(|e| e == "state unifi-t1 answered")
        .unwrap();
    assert_eq!(ev[a + 1], "state unifi-t1 active", "{ev:?}");

    // Second answer: someone already has it.
    let e = ta.answer_talkback(&ctx, "unifi-t1").await.err().unwrap();
    assert!(e.is(Sentinel::AlreadyAnswered), "second answer = {e}");
    assert_eq!(
        e.message(),
        "unifi_protect: unifi-t1 is already answered: already_answered"
    );
    let e = ta.answer_talkback(&ctx, "unifi-zzz").await.err().unwrap();
    assert!(e.is(Sentinel::NoSuchCall), "unknown call = {e}");
    assert_eq!(
        e.message(),
        "unifi_protect: unifi-zzz is not a live call: no_such_call"
    );
    // The ring timers must not end an answered call.
    tokio::time::sleep(fast().ring_max_hold + ms(200)).await;
    assert!(
        !s.has("state unifi-t1 ended"),
        "an answered call was ended by the ring timer: {:?}",
        s.list()
    );
    d.hangup_call("unifi-t1").unwrap();
    wait_for("ended", || s.has("state unifi-t1 ended")).await;
    let e = d.hangup_call("unifi-t1").unwrap_err();
    assert!(e.is(Sentinel::NoSuchCall), "second hangup = {e}");
    assert_eq!(e.message(), "unifi_protect: unifi-t1: no_such_call");
    assert!(
        d.talkback_audio().is_none(),
        "the talkback outlived the hangup"
    );
    assert!(op.sinks.lock().unwrap()[0].closed.load(Ordering::SeqCst));
    d.stop().await.unwrap();
}

// TestAFailedTalkbackOpenLeavesTheCallRinging
#[tokio::test]
async fn a_failed_talkback_open_leaves_the_call_ringing() {
    let f = FakeConsole::new(false).await;
    let fc = FakeProtect::new().await;
    fc.set_status(500);
    let op = Arc::new(FakeOpener::default());
    let (d, s) = answerable(&f, &fc, op.clone()).await;
    f.last_conn().await;
    f.send(&ring_json("add", "t2", "cam1", "")).await;
    wait_for("ringing", || s.has("ringing unifi-t2")).await;
    let ta = Driver::as_talkback_answerer(&*d).unwrap();
    let e = ta
        .answer_talkback(&CancelToken::new(), "unifi-t2")
        .await
        .err()
        .expect("the failure was swallowed");
    assert_eq!(
        e.message(),
        "unifi_protect: opening talkback session: POST /proxy/protect/integration/v1/cameras/cam1/talkback-session: HTTP 500"
    );
    assert!(
        ta.ringing_call("unifi-t2"),
        "a failed open consumed the ring; another client could no longer answer"
    );
    assert!(
        !s.has("state unifi-t2 answered"),
        "answered reported though the open failed: {:?}",
        s.list()
    );
    assert_eq!(
        op.started.load(Ordering::SeqCst),
        0,
        "the transcode started without a session"
    );
    // and an opener that fails after the session was granted: the same (added)
    fc.set_status(0);
    op.fail.store(true, Ordering::SeqCst);
    let e = ta
        .answer_talkback(&CancelToken::new(), "unifi-t2")
        .await
        .err()
        .unwrap();
    assert_eq!(e.message(), "unifi_protect: ffmpeg not found");
    assert!(ta.ringing_call("unifi-t2"));
    d.stop().await.unwrap();
}

// TestTalkbackDyingEndsTheCall
#[tokio::test]
async fn talkback_dying_ends_the_call() {
    let f = FakeConsole::new(false).await;
    let fc = FakeProtect::new().await;
    let op = Arc::new(FakeOpener::default());
    let (d, s) = answerable(&f, &fc, op.clone()).await;
    f.last_conn().await;
    f.send(&ring_json("add", "t3", "cam1", "")).await;
    wait_for("ringing", || s.has("ringing unifi-t3")).await;
    let ta = Driver::as_talkback_answerer(&*d).unwrap();
    ta.answer_talkback(&CancelToken::new(), "unifi-t3")
        .await
        .unwrap();
    // the transcode dies on its own
    op.sinks.lock().unwrap()[0].done.cancel();
    wait_for("ended", || s.has("state unifi-t3 ended")).await;
    assert!(
        d.talkback_audio().is_none(),
        "the dead session still holds the slot"
    );
    // the slot is free: a new ring can be answered again
    f.send(&ring_json("add", "t4", "cam1", "")).await;
    wait_for("ringing t4", || s.has("ringing unifi-t4")).await;
    ta.answer_talkback(&CancelToken::new(), "unifi-t4")
        .await
        .unwrap();
    d.stop().await.unwrap();
}

// Without an opener (slice 7a) nothing touches the console (added).
#[tokio::test]
async fn without_an_opener_open_relay_and_answer_touch_nothing() {
    let f = FakeConsole::new(false).await;
    let fc = FakeProtect::new().await;
    let (d, s) = started(&f, json!({})).await;
    d.clear_talkback_opener(); // slice 7a's state: since 7b the default driver has the ffmpeg one
    d.set_api_for_test(ProtectApi::new("http", &fc.host(), TEST_KEY, true));
    f.last_conn().await;
    let e = d.open_relay(&CancelToken::new()).await.unwrap_err();
    assert!(e.message().contains("no talkback opener"), "{e}");
    assert!(fc.reqs().is_empty());
    f.send(&ring_json("add", "n1", "cam1", "")).await;
    wait_for("ringing", || s.has("ringing unifi-n1")).await;
    // the hub-facing accessor is absent, the ring stays answerable by type-erased callers
    assert!(Driver::as_talkback_answerer(&*d).is_none());
    assert!(TalkbackAnswerer::ringing_call(&*d, "unifi-n1"));
    let e = TalkbackAnswerer::answer_talkback(&*d, &CancelToken::new(), "unifi-n1")
        .await
        .err()
        .unwrap();
    assert!(e.message().contains("no talkback opener"), "{e}");
    assert!(
        TalkbackAnswerer::ringing_call(&*d, "unifi-n1"),
        "the refusal consumed the ring"
    );
    assert!(fc.reqs().is_empty());
    d.stop().await.unwrap();
}

// An answer future that is dropped while the session opens gives the ring back (added; Go's
// cancelled context returns an error from the open and clears `answering`).
#[tokio::test]
async fn a_dropped_answer_gives_the_ring_back() {
    let f = FakeConsole::new(false).await;
    let fc = FakeProtect::new().await;
    let op = Arc::new(FakeOpener::default());
    op.hang.store(true, Ordering::SeqCst);
    let (d, s) = answerable(&f, &fc, op.clone()).await;
    f.last_conn().await;
    f.send(&ring_json("add", "d1", "cam1", "")).await;
    wait_for("ringing", || s.has("ringing unifi-d1")).await;
    let d2 = d.clone();
    let h = tokio::spawn(async move {
        let _ = TalkbackAnswerer::answer_talkback(&*d2, &CancelToken::new(), "unifi-d1").await;
    });
    wait_for("opener entered", || op.started.load(Ordering::SeqCst) == 1).await;
    assert!(
        !TalkbackAnswerer::ringing_call(&*d, "unifi-d1"),
        "answering counts as taken"
    );
    h.abort();
    let _ = h.await;
    assert!(
        TalkbackAnswerer::ringing_call(&*d, "unifi-d1"),
        "a dropped answer left the ring taken for good"
    );
    // and the ring's own timers still end it
    wait_for("ended by the max hold", || s.has("state unifi-d1 ended")).await;
    d.stop().await.unwrap();
}

// A hang-up while the session opens: the open's result is closed again and the answer fails.
#[tokio::test]
async fn a_hangup_while_the_session_opens_closes_it_again() {
    let f = FakeConsole::new(false).await;
    let fc = FakeProtect::new().await;
    let gate = Arc::new(tokio::sync::Notify::new());
    struct Gated(Arc<FakeOpener>, Arc<tokio::sync::Notify>);
    impl TalkbackOpener for Gated {
        fn start<'a>(
            &'a self,
            ctx: &'a CancelToken,
            s: TalkbackSession,
        ) -> BoxFuture<'a, Result<OpenedTalkback, DriverError>> {
            Box::pin(async move {
                self.1.notified().await;
                self.0.start(ctx, s).await
            })
        }
    }
    let inner = Arc::new(FakeOpener::default());
    let (d, s) = started(&f, json!({})).await;
    d.set_api_for_test(ProtectApi::new("http", &fc.host(), TEST_KEY, true));
    d.set_talkback_opener(Arc::new(Gated(inner.clone(), gate.clone())));
    f.last_conn().await;
    f.send(&ring_json("add", "h1", "cam1", "")).await;
    wait_for("ringing", || s.has("ringing unifi-h1")).await;
    let d2 = d.clone();
    let h = tokio::spawn(async move {
        TalkbackAnswerer::answer_talkback(&*d2, &CancelToken::new(), "unifi-h1")
            .await
            .err()
            .map(|e| e.to_string())
    });
    wait_for("talkback-session asked", || fc.reqs().len() == 1).await;
    d.hangup_call("unifi-h1").unwrap();
    assert!(s.has("state unifi-h1 ended"));
    gate.notify_one();
    let err = h
        .await
        .unwrap()
        .expect("the answer of an ended call succeeded");
    assert_eq!(
        err,
        "unifi_protect: unifi-h1 ended while answering: no_such_call"
    );
    wait_for("session closed", || {
        inner
            .sinks
            .lock()
            .unwrap()
            .first()
            .is_some_and(|k| k.closed.load(Ordering::SeqCst))
    })
    .await;
    assert!(d.talkback_audio().is_none());
    assert!(!s.has("state unifi-h1 answered"), "{:?}", s.list());
    d.stop().await.unwrap();
}

// DEV-796 D5: an open that finishes after `stop` closes what it opened and stores nothing.
#[tokio::test]
async fn an_open_that_finishes_after_stop_leaves_no_session() {
    let f = FakeConsole::new(false).await;
    let fc = FakeProtect::new().await;
    let gate = Arc::new(tokio::sync::Notify::new());
    let inner = Arc::new(FakeOpener::default());
    let (d, _s) = started(&f, json!({})).await;
    d.set_api_for_test(ProtectApi::new("http", &fc.host(), TEST_KEY, true));
    d.set_talkback_opener(Arc::new(GatedOpener(inner.clone(), gate.clone())));
    f.last_conn().await;
    let d2 = d.clone();
    let h = tokio::spawn(async move { d2.open_relay(&CancelToken::new()).await.err() });
    wait_for("talkback-session asked", || fc.reqs().len() == 1).await;
    d.stop().await.unwrap();
    gate.notify_one();
    assert!(h.await.unwrap().is_some(), "an open after stop succeeded");
    assert!(
        d.talkback_audio().is_none(),
        "a session was stored after stop"
    );
    wait_for("opened sink closed", || {
        inner
            .sinks
            .lock()
            .unwrap()
            .first()
            .is_some_and(|k| k.closed.load(Ordering::SeqCst))
    })
    .await;
}

// DEV-796 D5: an open on a stopped driver touches neither the console nor the opener.
#[tokio::test]
async fn an_open_on_a_stopped_driver_asks_nobody() {
    let f = FakeConsole::new(false).await;
    let fc = FakeProtect::new().await;
    let op = Arc::new(FakeOpener::default());
    let (d, _s) = answerable(&f, &fc, op.clone()).await;
    d.stop().await.unwrap();
    assert!(d.open_relay(&CancelToken::new()).await.is_err());
    assert!(fc.reqs().is_empty(), "{:?}", fc.reqs());
    assert_eq!(op.started.load(Ordering::SeqCst), 0);
}

// DEV-796 D5: a stop followed by a start does not make an open of the earlier run current.
#[tokio::test]
async fn an_open_that_spans_a_stop_and_a_restart_is_stale() {
    let f = FakeConsole::new(false).await;
    let fc = FakeProtect::new().await;
    let gate = Arc::new(tokio::sync::Notify::new());
    let inner = Arc::new(FakeOpener::default());
    let (d, s) = started(&f, json!({})).await;
    d.set_api_for_test(ProtectApi::new("http", &fc.host(), TEST_KEY, true));
    d.set_talkback_opener(Arc::new(GatedOpener(inner.clone(), gate.clone())));
    f.last_conn().await;
    let d2 = d.clone();
    let h = tokio::spawn(async move { d2.open_relay(&CancelToken::new()).await.err() });
    wait_for("talkback-session asked", || fc.reqs().len() == 1).await;
    d.stop().await.unwrap();
    d.start(&CancelToken::new(), s.clone()).await.unwrap();
    gate.notify_one();
    assert!(h.await.unwrap().is_some(), "a stale open was accepted");
    assert!(d.talkback_audio().is_none());
    assert!(inner.sinks.lock().unwrap()[0].closed.load(Ordering::SeqCst));
    d.stop().await.unwrap();
}

// DEV-796 D5: a stop while an answer opens still answers no_such_call, as a hang-up does.
#[tokio::test]
async fn a_stop_while_the_session_opens_answers_no_such_call() {
    let f = FakeConsole::new(false).await;
    let fc = FakeProtect::new().await;
    let gate = Arc::new(tokio::sync::Notify::new());
    let inner = Arc::new(FakeOpener::default());
    let (d, s) = started(&f, json!({})).await;
    d.set_api_for_test(ProtectApi::new("http", &fc.host(), TEST_KEY, true));
    d.set_talkback_opener(Arc::new(GatedOpener(inner.clone(), gate.clone())));
    f.last_conn().await;
    f.send(&ring_json("add", "z1", "cam1", "")).await;
    wait_for("ringing", || s.has("ringing unifi-z1")).await;
    let d2 = d.clone();
    let h = tokio::spawn(async move {
        TalkbackAnswerer::answer_talkback(&*d2, &CancelToken::new(), "unifi-z1")
            .await
            .err()
            .map(|e| e.to_string())
    });
    wait_for("talkback-session asked", || fc.reqs().len() == 1).await;
    d.stop().await.unwrap();
    gate.notify_one();
    assert_eq!(
        h.await.unwrap().as_deref(),
        Some("unifi_protect: unifi-z1 ended while answering: no_such_call")
    );
}

// Stop drops a live call silently and closes the talkback (ring.go endLocked: nothing is
// reported once stopped).
#[tokio::test]
async fn stop_closes_the_talkback_and_reports_nothing_for_the_live_call() {
    let f = FakeConsole::new(false).await;
    let fc = FakeProtect::new().await;
    let op = Arc::new(FakeOpener::default());
    let (d, s) = answerable(&f, &fc, op.clone()).await;
    f.last_conn().await;
    f.send(&ring_json("add", "s1", "cam1", "")).await;
    wait_for("ringing", || s.has("ringing unifi-s1")).await;
    let ta = Driver::as_talkback_answerer(&*d).unwrap();
    ta.answer_talkback(&CancelToken::new(), "unifi-s1")
        .await
        .unwrap();
    d.stop().await.unwrap();
    assert!(op.sinks.lock().unwrap()[0].closed.load(Ordering::SeqCst));
    tokio::time::sleep(ms(100)).await;
    assert!(!s.has("state unifi-s1 ended"), "{:?}", s.list());
    // a ring after Stop is not reported either, and a restart watches again
    assert!(!TalkbackAnswerer::ringing_call(&*d, "unifi-s1"));
    d.start(&CancelToken::new(), s.clone()).await.unwrap();
    f.last_conn().await;
    f.send(&ring_json("add", "s2", "cam1", "")).await;
    wait_for("ringing s2", || s.has("ringing unifi-s2")).await;
    d.stop().await.unwrap();
}

// ---- added: the hub's context, panics, hostile frames ----------------------------------------

// The hub's context ending stops the watcher without Stop (Go: the watcher's context derives
// from Start's).
#[tokio::test]
async fn the_hubs_context_ending_stops_the_watcher() {
    let f = FakeConsole::new(false).await;
    let d = Arc::new(configured(&f.host(), json!({"ring_events": true}), fast()));
    let hub = CancelToken::new();
    let s = RecSink::new();
    d.start(&hub, s.clone()).await.unwrap();
    f.last_conn().await;
    wait_for("available", || s.has("available ")).await;
    hub.cancel();
    wait_for("close frame", || {
        f.client_frames().iter().any(|(op, _)| *op == 0x8)
    })
    .await;
    let before = f.attempt_count();
    tokio::time::sleep(ms(200)).await;
    assert_eq!(f.attempt_count(), before);
    d.stop().await.unwrap();
}

// A sink that panics on a ring does not kill the connection or the watcher; the frames after it
// are handled and the panic is the only thing lost.
#[tokio::test]
async fn a_sink_that_panics_does_not_end_the_connection() {
    struct Bomb {
        inner: Arc<RecSink>,
        armed: AtomicBool,
    }
    impl EventSink for Bomb {
        fn ringing(&self, c: &str, d: &str) {
            if self.armed.swap(false, Ordering::SeqCst) {
                panic!("sink bomb");
            }
            self.inner.ringing(c, d)
        }
        fn state_changed(&self, c: &str, s: &str) {
            self.inner.state_changed(c, s)
        }
        fn availability_changed(&self, d: &str, a: bool, r: &str) {
            self.inner.availability_changed(d, a, r)
        }
    }
    let f = FakeConsole::new(false).await;
    let rec = RecSink::new();
    let bomb = Arc::new(Bomb {
        inner: rec.clone(),
        armed: AtomicBool::new(true),
    });
    let d = Arc::new(configured(&f.host(), json!({"ring_events": true}), fast()));
    d.start(&CancelToken::new(), bomb).await.unwrap();
    f.last_conn().await;
    f.send(&ring_json("add", "p1", "cam1", "")).await; // panics inside the sink
    f.send(&ring_json("add", "p2", "cam1", "")).await; // ignored: p1 holds the camera
                                                       // p1's ring state exists (its max timer ends it); the connection survived
    wait_for("ended p1", || rec.has("state unifi-p1 ended")).await;
    f.send(&ring_json("add", "p3", "cam1", "")).await;
    wait_for("ringing p3", || rec.has("ringing unifi-p3")).await;
    assert_eq!(f.attempt_count(), 1, "the panic cost the connection");
    d.stop().await.unwrap();
}

// A panic in the watcher task is contained: reported unavailable, then the watcher restarts.
#[tokio::test]
async fn a_panic_in_the_watcher_is_reported_and_the_watcher_restarts() {
    let f = FakeConsole::new(false).await;
    let d = Arc::new(configured(&f.host(), json!({"ring_events": true}), fast()));
    d.panic_in_watch_for_test();
    let s = RecSink::new();
    d.start(&CancelToken::new(), s.clone()).await.unwrap();
    wait_for("unavailable internal error", || {
        s.has(&format!("unavailable {} internal error", f.host()))
    })
    .await;
    f.last_conn().await; // restarted
    wait_for("available", || s.has("available ")).await;
    f.send(&ring_json("add", "w1", "cam1", "")).await;
    wait_for("ringing", || s.has("ringing unifi-w1")).await;
    d.stop().await.unwrap();
}

// A console that sends nothing but garbage costs the connection, never the process, and a ring
// on the next connection works (hostile input).
#[tokio::test]
async fn hostile_frames_cost_the_connection_never_the_process() {
    let f = FakeConsole::new(false).await;
    let (d, s) = started(&f, json!({})).await;
    f.last_conn().await;
    // text that is not UTF-8, JSON nested far too deep, a type error in a field we read, an
    // empty frame, a ring with every field null: all ignored, the connection stays up
    f.send_bytes(&frame(0x1, &[0xff, 0xfe, 0x00, 0x80])).await;
    f.send(&format!("{}{}", "[".repeat(5000), "]".repeat(5000)))
        .await;
    f.send(r#"{"type":"add","item":{"id":5,"modelKey":"event","type":"ring","device":"cam1"}}"#)
        .await;
    f.send(r#"{"type":"add","item":"ring"}"#).await;
    f.send(r#"{"type":"add","item":null}"#).await;
    f.send(r#"null"#).await;
    f.send("").await;
    f.send_bytes(&frame(0x2, b"binary is dropped")).await;
    f.send_bytes(&frame(0xA, b"unsolicited pong")).await;
    for i in 0..300 {
        f.send_bytes(&frame(0x9, format!("ping{i}").as_bytes()))
            .await; // a ping flood is answered
    }
    f.send(&ring_json("add", "hf", "cam1", "")).await;
    wait_for("ringing", || s.has("ringing unifi-hf")).await;
    assert_eq!(f.attempt_count(), 1, "hostile frames cost the connection");
    // a protocol violation does cost it: reserved bits
    f.send_raw(0xC1, b"x").await;
    assert!(
        f.connected_within(Duration::from_secs(5)).await,
        "no reconnect after a protocol error"
    );
    d.stop().await.unwrap();
}

// The hold timers: an `end` that arrives with the `add` still waits out the minimum hold; the
// end of another event is ignored; an answered-or-ended call's late `end` does nothing.
#[tokio::test]
async fn end_handling_follows_ring_go() {
    let f = FakeConsole::new(false).await;
    let (d, s) = started(&f, json!({})).await;
    f.last_conn().await;
    let t0 = Instant::now();
    f.send(&ring_json("add", "x1", "cam1", "1700000001000"))
        .await; // add with an end
    wait_for("ringing", || s.has("ringing unifi-x1")).await;
    // (the end of another event is ignored: its own test below, on a ring with no end yet)
    wait_for("ended x1", || s.has("state unifi-x1 ended")).await;
    let el = t0.elapsed();
    assert!(
        el >= fast().ring_min_hold - ms(30) && el < fast().ring_max_hold,
        "ended after {el:?}"
    );
    assert_eq!(s.count("state unifi-x1 ended"), 1);
    // the same end again, after the call is gone: nothing
    f.send(&ring_json("update", "x1", "cam1", "5")).await;
    tokio::time::sleep(ms(300)).await;
    assert_eq!(s.count("state unifi-x1 ended"), 1, "{:?}", s.list());
    // an update that carries `end: null` or no end does not end the call
    f.send(&ring_json("add", "x2", "cam1", "")).await;
    wait_for("ringing x2", || s.has("ringing unifi-x2")).await;
    f.send(&ring_json("update", "x2", "cam1", "null")).await;
    f.send(
        r#"{"type":"update","item":{"id":"x2","modelKey":"event","type":"ring","device":"cam1"}}"#,
    )
    .await;
    tokio::time::sleep(ms(300)).await;
    assert!(!s.has("state unifi-x2 ended"), "{:?}", s.list());
    wait_for("max hold", || s.has("state unifi-x2 ended")).await;
    d.stop().await.unwrap();
}

// R2-99: the end of ANOTHER event is ignored. The ring has no end of its own yet, so nothing but
// the event id check stands between the foreign end and an end timer.
#[tokio::test]
async fn the_end_of_another_event_is_ignored() {
    let f = FakeConsole::new(false).await;
    let (d, s) = started(&f, json!({})).await;
    f.last_conn().await;
    f.send(&ring_json("add", "y1", "cam1", "")).await;
    wait_for("ringing", || s.has("ringing unifi-y1")).await;
    f.send(&ring_json("update", "OTHER", "cam1", "5")).await;
    tokio::time::sleep(fast().ring_min_hold + ms(150)).await;
    assert!(!s.has("state unifi-y1 ended"), "{:?}", s.list());
    // its own end, now, ends it at once (the minimum hold has passed)
    f.send(&ring_json("update", "y1", "cam1", "5")).await;
    wait_for("ended y1", || s.has("state unifi-y1 ended")).await;
    d.stop().await.unwrap();
}

// HangupCall of a ringing call reports it ended once and frees the camera (ring.go HangupCall).
#[tokio::test]
async fn hangup_of_a_ringing_call_ends_it_and_frees_the_camera() {
    let f = FakeConsole::new(false).await;
    let (d, s) = started(&f, json!({})).await;
    f.last_conn().await;
    f.send(&ring_json("add", "g1", "cam1", "")).await;
    wait_for("ringing", || s.has("ringing unifi-g1")).await;
    CallHangupper::hangup_call(&*d, "unifi-g1").unwrap();
    assert_eq!(s.count("state unifi-g1 ended"), 1);
    assert!(!TalkbackAnswerer::ringing_call(&*d, "unifi-g1"));
    f.send(&ring_json("add", "g2", "cam1", "")).await;
    wait_for("ringing g2", || s.has("ringing unifi-g2")).await;
    // the timer of the hung-up call must not end the new one (stale timers are recognised)
    tokio::time::sleep(fast().ring_max_hold - ms(150)).await;
    assert!(!s.has("state unifi-g2 ended"), "{:?}", s.list());
    d.stop().await.unwrap();
}

// A second Start keeps the one watcher (Go: `watchCancel == nil` is the condition).
#[tokio::test]
async fn a_second_start_does_not_start_a_second_watcher() {
    let f = FakeConsole::new(false).await;
    let d = Arc::new(configured(&f.host(), json!({"ring_events": true}), fast()));
    let s = RecSink::new();
    d.start(&CancelToken::new(), s.clone()).await.unwrap();
    d.start(&CancelToken::new(), s.clone()).await.unwrap();
    f.last_conn().await;
    tokio::time::sleep(ms(200)).await;
    assert_eq!(f.attempt_count(), 1);
    d.stop().await.unwrap();
}

// The connection that lived for `stable_after` resets the backoff; one that did not doubles it.
#[tokio::test]
async fn the_backoff_doubles_for_short_connections_and_resets_for_long_ones() {
    let f = FakeConsole::new(false).await;
    let t = Timing {
        stable_after: ms(250),
        backoff_min: ms(40),
        backoff_max: ms(160),
        ..fast()
    };
    let d = Arc::new(configured(&f.host(), json!({"ring_events": true}), t));
    d.start(&CancelToken::new(), RecSink::new()).await.unwrap();
    // short connections: dropped at once, the gaps between reconnects grow 40, 80, 160, 160
    let mut at = vec![];
    for _ in 0..5 {
        f.last_conn().await;
        at.push(Instant::now());
        f.drop_all();
    }
    let gaps: Vec<u128> = at.windows(2).map(|w| (w[1] - w[0]).as_millis()).collect();
    assert!(gaps[0] >= 35 && gaps[0] < 120, "{gaps:?}");
    assert!(gaps[1] >= 75 && gaps[1] < 200, "{gaps:?}");
    assert!(gaps[2] >= 150 && gaps[3] >= 150, "{gaps:?}");
    // a connection that lives longer than stable_after: the next gap is back to the minimum
    f.last_conn().await;
    tokio::time::sleep(ms(300)).await;
    f.drop_all();
    let t1 = Instant::now();
    f.last_conn().await;
    assert!(
        t1.elapsed() < ms(120),
        "the backoff was not reset: {:?}",
        t1.elapsed()
    );
    d.stop().await.unwrap();
}

// Stop gives up on a watcher that is stuck inside the sink instead of hanging the hub's shutdown
// (Go waits for ever).
#[tokio::test]
async fn stop_gives_up_on_a_watcher_stuck_in_the_sink() {
    struct Stuck(Mutex<std::sync::mpsc::Receiver<()>>);
    impl EventSink for Stuck {
        fn ringing(&self, _: &str, _: &str) {}
        fn state_changed(&self, _: &str, _: &str) {}
        fn availability_changed(&self, _: &str, _: bool, _: &str) {
            let _ = self.0.lock().unwrap().recv(); // blocks until released
        }
    }
    let f = FakeConsole::new(false).await;
    let t = Timing {
        stop_wait: ms(300),
        ..fast()
    };
    let d = Arc::new(configured(&f.host(), json!({"ring_events": true}), t));
    let (tx, rx) = std::sync::mpsc::channel::<()>();
    let (ready_tx, ready_rx) = tokio::sync::oneshot::channel();
    // the driver lives on a runtime of its own, whose only thread the sink will block
    let d2 = d.clone();
    let th = std::thread::spawn(move || {
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        rt.block_on(async move {
            d2.start(&CancelToken::new(), Arc::new(Stuck(Mutex::new(rx))))
                .await
                .unwrap();
            let _ = ready_tx.send(());
            tokio::time::sleep(ms(1000)).await;
        });
    });
    ready_rx.await.unwrap();
    f.last_conn().await;
    tokio::time::sleep(ms(200)).await; // the watcher is inside availability_changed now
    let t0 = Instant::now();
    d.stop().await.unwrap();
    let el = t0.elapsed();
    assert!(el >= ms(280) && el < ms(3000), "stop took {el:?}");
    drop(tx); // releases the blocked thread
    th.join().unwrap();
}

// ---- the stuck ring (a Go defect too, PARITY.md) ------------------------------------------------

struct GatedOpener(Arc<FakeOpener>, Arc<tokio::sync::Notify>);
impl TalkbackOpener for GatedOpener {
    fn start<'a>(
        &'a self,
        ctx: &'a CancelToken,
        s: TalkbackSession,
    ) -> BoxFuture<'a, Result<OpenedTalkback, DriverError>> {
        Box::pin(async move {
            self.1.notified().await;
            self.0.start(ctx, s).await
        })
    }
}

/// A ring whose `end` arrives while an answer is opening: the end timer fires and finds the call
/// `answering`. Returns the driver, the sink and the gate.
async fn ring_with_a_spent_end_timer(
    f: &FakeConsole,
    fc: &FakeProtect,
) -> (
    Arc<UnifiProtect>,
    Arc<RecSink>,
    Arc<tokio::sync::Notify>,
    Arc<FakeOpener>,
) {
    let inner = Arc::new(FakeOpener::default());
    inner.fail.store(true, Ordering::SeqCst);
    let gate = Arc::new(tokio::sync::Notify::new());
    let (d, s) = started(f, json!({})).await;
    d.set_api_for_test(ProtectApi::new("http", &fc.host(), TEST_KEY, true));
    d.set_talkback_opener(Arc::new(GatedOpener(inner.clone(), gate.clone())));
    f.last_conn().await;
    f.send(&ring_json("add", "k1", "cam1", "")).await;
    wait_for("ringing", || s.has("ringing unifi-k1")).await;
    (d, s, gate, inner)
}

#[tokio::test]
async fn an_end_that_fires_during_a_failed_answer_still_ends_the_call() {
    let f = FakeConsole::new(false).await;
    let fc = FakeProtect::new().await;
    let (d, s, gate, _inner) = ring_with_a_spent_end_timer(&f, &fc).await;
    let d2 = d.clone();
    let h = tokio::spawn(async move {
        TalkbackAnswerer::answer_talkback(&*d2, &CancelToken::new(), "unifi-k1")
            .await
            .err()
            .map(|e| e.to_string())
    });
    wait_for("talkback-session asked", || fc.reqs().len() == 1).await;
    f.send(&ring_json("update", "k1", "cam1", "5")).await;
    tokio::time::sleep(ms(300)).await; // the end timer (150 ms) has fired and found the call answering
    assert!(!s.has("state unifi-k1 ended"));
    assert!(!TalkbackAnswerer::ringing_call(&*d, "unifi-k1"));
    gate.notify_one(); // the open now fails
    assert_eq!(
        h.await.unwrap().as_deref(),
        Some("unifi_protect: ffmpeg not found")
    );
    // the max hold (600 ms) is still pending here, so the ring is back and a timer will end it;
    // what must not happen is a ring that nothing ends: wait past the max hold
    wait_for("ended", || s.has("state unifi-k1 ended")).await;
    assert!(!TalkbackAnswerer::ringing_call(&*d, "unifi-k1"));
    // and the camera is free for the next ring
    f.send(&ring_json("add", "k2", "cam1", "")).await;
    wait_for("ringing k2", || s.has("ringing unifi-k2")).await;
    d.stop().await.unwrap();
}

#[tokio::test]
async fn both_timers_spent_during_a_failed_or_dropped_answer_end_the_call_at_once() {
    for dropped in [false, true] {
        let f = FakeConsole::new(false).await;
        let fc = FakeProtect::new().await;
        let (d, s, gate, _inner) = ring_with_a_spent_end_timer(&f, &fc).await;
        let d2 = d.clone();
        let h = tokio::spawn(async move {
            let _ = TalkbackAnswerer::answer_talkback(&*d2, &CancelToken::new(), "unifi-k1").await;
        });
        wait_for("talkback-session asked", || fc.reqs().len() == 1).await;
        f.send(&ring_json("update", "k1", "cam1", "5")).await;
        tokio::time::sleep(fast().ring_max_hold + ms(250)).await; // both timers have fired and returned
        assert!(
            !s.has("state unifi-k1 ended"),
            "answering ring was ended: {:?}",
            s.list()
        );
        let t0 = Instant::now();
        if dropped {
            h.abort();
            let _ = h.await;
        } else {
            gate.notify_one();
            let _ = h.await;
        }
        wait_for("ended at once", || s.has("state unifi-k1 ended")).await;
        assert!(
            t0.elapsed() < ms(300),
            "dropped={dropped}: {:?}",
            t0.elapsed()
        );
        assert!(!TalkbackAnswerer::ringing_call(&*d, "unifi-k1"));
        f.send(&ring_json("add", "k2", "cam1", "")).await;
        wait_for("ringing k2", || s.has("ringing unifi-k2")).await;
        d.stop().await.unwrap();
    }
}

// M11, directly: a timer of a call that is gone (a stale serial) ends nothing.
#[tokio::test]
async fn a_stale_ring_serial_ends_nothing() {
    let f = FakeConsole::new(false).await;
    let (d, s) = started(&f, json!({})).await;
    f.last_conn().await;
    f.send(&ring_json("add", "z1", "cam1", "")).await;
    wait_for("ringing", || s.has("ringing unifi-z1")).await;
    let serial = d.ring_serial_for_test().unwrap();
    d.end_ringing_for_test(serial + 1);
    d.end_ringing_for_test(serial.wrapping_sub(1));
    assert!(TalkbackAnswerer::ringing_call(&*d, "unifi-z1"));
    assert!(!s.has("state unifi-z1 ended"));
    d.end_ringing_for_test(serial);
    assert!(s.has("state unifi-z1 ended"));
    assert_eq!(d.ring_serial_for_test(), None);
    d.stop().await.unwrap();
}

// ---- Phase R round 1, group F ---------------------------------------------------------------

/// R1-117: after `stop` gave up on a watcher that did not end, a `start` must not run a second
/// watcher beside it (both would feed one core); once the first has ended a start works again.
#[tokio::test]
async fn a_start_does_not_add_a_watcher_beside_one_that_stop_gave_up_on() {
    struct Stuck(Mutex<std::sync::mpsc::Receiver<()>>);
    impl EventSink for Stuck {
        fn ringing(&self, _: &str, _: &str) {}
        fn state_changed(&self, _: &str, _: &str) {}
        fn availability_changed(&self, _: &str, _: bool, _: &str) {
            let _ = self.0.lock().unwrap().recv(); // blocks until released
        }
    }
    let f = FakeConsole::new(false).await;
    let t = Timing {
        stop_wait: ms(150),
        ..fast()
    };
    let d = Arc::new(configured(&f.host(), json!({"ring_events": true}), t));
    let (tx, rx) = std::sync::mpsc::channel::<()>();
    let (ready_tx, ready_rx) = tokio::sync::oneshot::channel();
    // the driver lives on a runtime of its own, whose only thread the sink will block
    let d2 = d.clone();
    let th = std::thread::spawn(move || {
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        rt.block_on(async move {
            d2.start(&CancelToken::new(), Arc::new(Stuck(Mutex::new(rx))))
                .await
                .unwrap();
            let _ = ready_tx.send(());
            tokio::time::sleep(ms(2500)).await;
        });
    });
    ready_rx.await.unwrap();
    f.last_conn().await;
    tokio::time::sleep(ms(200)).await; // the watcher is inside availability_changed now
    d.stop().await.unwrap(); // gives up after 150 ms
    let e = d
        .start(&CancelToken::new(), RecSink::new())
        .await
        .expect_err("a second watcher was started beside the stuck one");
    assert!(e.message().contains("has not stopped"), "{}", e.message());
    drop(tx); // releases the blocked thread; the watcher ends (cancelled)
    th.join().unwrap();
    // the old watcher is gone with its runtime: a start works again
    d.start(&CancelToken::new(), RecSink::new())
        .await
        .expect("a start after the old watcher ended");
    d.stop().await.unwrap();
}

/// R1-191: an opener installed before `configure` survives it (the `TalkDriver` does the same).
#[tokio::test]
async fn an_opener_set_before_configure_survives_configure() {
    let fc = FakeProtect::new().await;
    let op = Arc::new(FakeOpener::default());
    let mut d = UnifiProtect::default();
    d.set_scheme("http");
    d.set_talkback_opener(op.clone());
    d.configure(&cfg("h", json!({}))).unwrap();
    d.set_api_for_test(ProtectApi::new("http", &fc.host(), TEST_KEY, true));
    d.open_relay(&CancelToken::new())
        .await
        .expect("open_relay through the installed opener");
    assert_eq!(op.started.load(Ordering::SeqCst), 1);
    d.stop().await.unwrap();
}

/// R1-33: the ffmpeg binary of the talkback is looked up once per open (the preflight's result
/// is the one that runs).
#[tokio::test]
async fn the_talkback_looks_the_binary_up_once_per_open() {
    use common::talk::{write_script, TmpDir};
    let dir = TmpDir::new("lookup");
    let ff = dir.file("ffmpeg");
    write_script(&ff, "#!/bin/sh\nexec sleep 30\n");
    let ff = ff.display().to_string();
    let fc = FakeProtect::new().await;
    let mut d = UnifiProtect::default();
    d.set_scheme("http");
    d.set_ffmpeg_binary(&ff);
    d.configure(&cfg("h", json!({}))).unwrap();
    d.set_api_for_test(ProtectApi::new("http", &fc.host(), TEST_KEY, true));
    transport_rs::driver::unifiprotect::ffmpeg::count_lookups_of(&ff);
    d.open_relay(&CancelToken::new()).await.expect("open_relay");
    assert_eq!(
        transport_rs::driver::unifiprotect::ffmpeg::lookups_of(&ff),
        1,
        "the binary was looked up more than once for one open"
    );
    d.stop().await.unwrap();
}

/// R2-98: two `open_relay` that both passed the "already open" check before either session was
/// stored: the second to finish loses, its session is closed (nothing runs for ever beside the
/// first) and it says a session is already open.
#[tokio::test]
async fn the_loser_of_a_concurrent_open_relay_is_closed() {
    let fc = FakeProtect::new().await;
    let inner = Arc::new(FakeOpener::default());
    let gate = Arc::new(tokio::sync::Notify::new());
    let mut d = UnifiProtect::default();
    d.set_scheme("http");
    d.configure(&cfg("h", json!({}))).unwrap();
    d.set_api_for_test(ProtectApi::new("http", &fc.host(), TEST_KEY, true));
    d.set_talkback_opener(Arc::new(GatedOpener(inner.clone(), gate.clone())));
    let d = Arc::new(d);
    let (d1, d2) = (d.clone(), d.clone());
    let a = tokio::spawn(async move { d1.open_relay(&CancelToken::new()).await.map(|_| ()) });
    let b = tokio::spawn(async move { d2.open_relay(&CancelToken::new()).await.map(|_| ()) });
    wait_for("both sessions asked for", || fc.reqs().len() == 2).await;
    gate.notify_one();
    wait_for("one session stored", || d.has_talkback()).await;
    gate.notify_one();
    let (ra, rb) = (a.await.unwrap(), b.await.unwrap());
    assert_eq!(
        (ra.is_ok(), rb.is_ok()),
        (true, false),
        "exactly one open succeeds: {ra:?} {rb:?}"
    );
    assert_eq!(
        rb.unwrap_err().message(),
        "unifi_protect: a talkback session is already open"
    );
    assert!(d.has_talkback());
    {
        let sinks = inner.sinks.lock().unwrap();
        assert_eq!(sinks.len(), 2);
        assert_eq!(
            sinks
                .iter()
                .filter(|k| k.closed.load(Ordering::SeqCst))
                .count(),
            1,
            "the loser's sink is closed, the winner's is not"
        );
    }
    d.stop().await.unwrap();
}

/// R1-135: a watcher that ran for `stable_after` before it panicked starts the backoff afresh.
#[test]
fn the_supervisors_backoff_starts_again_after_a_long_run() {
    use transport_rs::driver::unifiprotect::ring::backoff_after_run;
    let (wait, min, stable) = (ms(40_000), ms(1000), ms(30_000));
    assert_eq!(backoff_after_run(wait, ms(5), stable, min), wait);
    assert_eq!(backoff_after_run(wait, ms(29_999), stable, min), wait);
    assert_eq!(backoff_after_run(wait, stable, stable, min), min);
    assert_eq!(backoff_after_run(wait, ms(600_000), stable, min), min);
}
