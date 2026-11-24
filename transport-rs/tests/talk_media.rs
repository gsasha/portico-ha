//! The media of `unifi_protect_talk` (slice 7b) against a fake console, a fake guard (bell-talk's
//! `/health`) and a fake `ffmpeg` shell script: `talk_media_test.go`, `talk_down_test.go` and
//! `talk_reason_test.go` (the Go name is in a comment on each test), and the behaviours this port
//! adds ("Rust-only"). Nothing here needs a real ffmpeg or reaches anything but 127.0.0.1.

#![allow(unsafe_code)] // a libc call in a test helper (kill / signal); the library has none

mod common;

use std::time::Duration;

use common::talk::*;
use common::unifi::{wait_for, TEST_KEY};
use serde_json::json;
use transport_rs::cancel::CancelToken;
use transport_rs::driver::unifiprotect::talk::{TalkDriver, TalkTiming};
use transport_rs::driver::unifiprotect::talk_media::check_session_guard;
use transport_rs::driver::{
    CallAudio, CallHangupper, ConnectAnswerer, Driver, DriverError, ExternalRinger,
    ReasonedHangupper, Sentinel,
};

const MIN: Duration = Duration::from_secs(60);

async fn answer(d: &TalkDriver, id: &str) -> Result<std::sync::Arc<dyn CallAudio>, DriverError> {
    d.answer_with_audio(&CancelToken::new(), id).await
}

async fn guard(url: &str) -> Result<(), DriverError> {
    check_session_guard(&CancelToken::new(), url, Duration::from_secs(3)).await
}

fn alive(pid: u32) -> bool {
    // /proc/<pid> exists for a zombie as well: a reaped process has none
    std::path::Path::new(&format!("/proc/{pid}")).exists()
}

async fn all_gone(pids: &[u32]) -> bool {
    for _ in 0..300 {
        if pids.iter().all(|p| !alive(*p)) {
            return true;
        }
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    false
}

fn pids(d: &TalkDriver) -> (u32, u32) {
    let st = d.stats_for_test().expect("stats");
    (
        st.up_pid.load(std::sync::atomic::Ordering::SeqCst),
        st.down_pid.load(std::sync::atomic::Ordering::SeqCst),
    )
}

fn kill9(pid: u32) {
    unsafe {
        libc::kill(pid as i32, libc::SIGKILL);
    }
}

async fn next_frame(a: &std::sync::Arc<dyn CallAudio>, secs: u64) -> Option<Vec<u8>> {
    let out = a.output();
    let mut g = out.lock().await;
    tokio::time::timeout(Duration::from_secs(secs), g.recv())
        .await
        .ok()
        .flatten()
}

// ---- the guard -------------------------------------------------------------------------------

// TestGuardTruthTable
#[tokio::test]
async fn guard_truth_table() {
    for (name, body, code, want) in [
        ("idle", r#"{"sessionUp":false}"#, 200u16, "ok"),
        (
            "idle with other fields",
            r#"{"ok":true,"sessionUp":false,"session":{"codec":"opus"}}"#,
            200,
            "ok",
        ),
        ("busy", r#"{"sessionUp":true}"#, 200, "busy"),
        ("missing field", r#"{"ok":true}"#, 200, "refuse"),
        ("string false", r#"{"sessionUp":"false"}"#, 200, "refuse"),
        ("zero", r#"{"sessionUp":0}"#, 200, "refuse"),
        ("null", r#"{"sessionUp":null}"#, 200, "refuse"),
        ("malformed", r#"{"sessionUp":"#, 200, "refuse"),
        ("not an object", "[false]", 200, "refuse"),
        ("empty", "", 200, "refuse"),
        (
            "idle body, error status",
            r#"{"sessionUp":false}"#,
            503,
            "refuse",
        ),
        ("redirect", r#"{"sessionUp":false}"#, 302, "refuse"),
        // Rust-only rows: the shapes json.Unmarshal into a map reads
        ("whitespace", "  {\"sessionUp\" :\n false }\n", 200, "ok"),
        (
            "last duplicate wins (busy)",
            r#"{"sessionUp":false,"sessionUp":true}"#,
            200,
            "busy",
        ),
        (
            "last duplicate wins (idle)",
            r#"{"sessionUp":true,"sessionUp":false}"#,
            200,
            "ok",
        ),
        ("key case matters", r#"{"SessionUp":false}"#, 200, "refuse"),
        ("literal null body", "null", 200, "refuse"),
        // R2-8: json.Unmarshal skips space, tab, CR and LF only
        (
            "form feed before",
            "\u{c}{\"sessionUp\":false}",
            200,
            "refuse",
        ),
        (
            "no-break space before",
            "\u{a0}{\"sessionUp\":false}",
            200,
            "refuse",
        ),
        (
            "vertical tab after",
            "{\"sessionUp\":false}\u{b}",
            200,
            "refuse",
        ),
        (
            "line separator and NEL",
            "\u{2028}{\"sessionUp\":false}\u{85}",
            200,
            "refuse",
        ),
        (
            "trailing garbage",
            r#"{"sessionUp":false} x"#,
            200,
            "refuse",
        ),
    ] {
        let g = FakeGuard::new(body).await;
        g.set_code(code);
        let r = guard(&g.url()).await;
        match want {
            "ok" => assert!(r.is_ok(), "{name}: {r:?}"),
            "busy" => assert!(
                r.as_ref().is_err_and(|e| e.is(Sentinel::Busy)),
                "{name}: {r:?}"
            ),
            _ => assert!(
                r.as_ref().is_err_and(|e| !e.is(Sentinel::Busy)),
                "{name}: want a plain refusal, got {r:?}"
            ),
        }
    }
}

// R2-74 (R1-95): Go checks the status BEFORE it reads the body, so a non-200 answer whose body
// is cut off is "answered HTTP N", and a 200 whose body is cut off is the cut-off text.
#[tokio::test]
async fn a_cut_off_guard_answer_is_judged_by_its_status_first() {
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    for (code, want) in [
        (
            503,
            "unifi_protect_talk: the session guard answered HTTP 503, refusing to open a session",
        ),
        (
            200,
            "unifi_protect_talk: the session guard reply was cut off, refusing to open a session",
        ),
    ] {
        let ln = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let url = format!("http://{}/health", ln.local_addr().unwrap());
        tokio::spawn(async move {
            let (mut s, _) = ln.accept().await.unwrap();
            let mut b = [0u8; 2048];
            let _ = s.read(&mut b).await;
            let r = format!("HTTP/1.1 {code} X\r\nContent-Length: 100\r\n\r\n{{\"sess");
            let _ = s.write_all(r.as_bytes()).await;
            // closes with 95 bytes missing
        });
        let e = guard(&url).await.unwrap_err();
        assert_eq!(e.message(), want, "{code}");
    }
}

// Rust-only: Go's refusal texts, one per way to refuse, and none of them echo the URL
#[tokio::test]
async fn guard_refusal_texts() {
    let t = |r: Result<(), DriverError>| r.unwrap_err().message().to_string();
    let g = FakeGuard::new(r#"{"sessionUp":false}"#).await;
    g.set_code(503);
    assert_eq!(
        t(guard(&g.url()).await),
        "unifi_protect_talk: the session guard answered HTTP 503, refusing to open a session"
    );
    g.set_code(200);
    g.set_body("nope");
    assert_eq!(
        t(guard(&g.url()).await),
        "unifi_protect_talk: the session guard reply is not JSON, refusing to open a session"
    );
    g.set_body(r#"{"a":1}"#);
    assert_eq!(
        t(guard(&g.url()).await),
        "unifi_protect_talk: the session guard reply has no boolean sessionUp, refusing to open a session"
    );
    g.set_body(r#"{"sessionUp":true}"#);
    assert_eq!(
        t(guard(&g.url()).await),
        "unifi_protect_talk: the old intercom holds the talkback session: busy"
    );
    let url = g.url();
    g.close();
    tokio::time::sleep(Duration::from_millis(50)).await;
    let e = t(guard(&url).await);
    assert_eq!(
        e,
        "unifi_protect_talk: the session guard is unreachable, refusing to open a session"
    );
    assert!(!e.contains("127.0.0.1"), "{e}");
    // an https guard is spoken over TLS (R2-117); one nobody answers is unreachable
    assert_eq!(
        t(guard("https://127.0.0.1:1/health").await),
        "unifi_protect_talk: the session guard is unreachable, refusing to open a session"
    );
    // a URL Go's http.NewRequest refuses
    assert_eq!(
        t(guard("http://127.0.0.1:notaport/health").await),
        "unifi_protect_talk: guard_url is not usable"
    );
    // a body over 4 KiB is read only that far: a long idle answer is still not a yes
    let g = FakeGuard::new(&format!(
        r#"{{"pad":"{}","sessionUp":false}}"#,
        "x".repeat(8000)
    ))
    .await;
    assert!(
        guard(&g.url()).await.is_err(),
        "a body over the cap was believed"
    );
}

// TestGuardRefusesWhenUnreachableSlowOrUnset
#[tokio::test]
async fn guard_refuses_when_unreachable_slow_or_unset() {
    let g = FakeGuard::new(r#"{"sessionUp":false}"#).await;
    let url = g.url();
    g.close();
    tokio::time::sleep(Duration::from_millis(50)).await;
    assert!(
        guard(&url).await.is_err(),
        "unreachable guard let the answer go on"
    );
    // PARITY.md 476: an empty guard_url is "no external guard", not a refusal
    assert!(guard("").await.is_ok(), "an empty guard_url refused");
    let slow = FakeGuard::new(r#"{"sessionUp":false}"#).await;
    *slow.delay.lock().unwrap() = Duration::from_secs(2);
    let start = std::time::Instant::now();
    let r = check_session_guard(&CancelToken::new(), &slow.url(), Duration::from_millis(50)).await;
    assert!(
        r.is_err() && start.elapsed() < Duration::from_secs(1),
        "slow guard: {r:?} after {:?}",
        start.elapsed()
    );
    // a cancelled request context ends the wait at once
    let cancel = CancelToken::new();
    let c2 = cancel.clone();
    tokio::spawn(async move {
        tokio::time::sleep(Duration::from_millis(50)).await;
        c2.cancel();
    });
    let start = std::time::Instant::now();
    let r = check_session_guard(&cancel, &slow.url(), Duration::from_secs(3)).await;
    assert!(
        r.is_err() && start.elapsed() < Duration::from_secs(1),
        "{r:?}"
    );
}

// ---- the uplink, the session, the refusals -------------------------------------------------------

// TestTalkAnswerOpensSessionAndPushesTheMicrophoneThroughFFmpeg
#[tokio::test]
async fn talk_answer_opens_session_and_pushes_the_microphone_through_ffmpeg() {
    let r = Rig::new().await;
    r.ring(MIN);
    assert!(
        r.posts() == 0 && r.guard.hits() == 0,
        "something was opened by the ring alone"
    );
    let a = answer(&r.d, "c1").await.unwrap();
    a.attach();
    assert!(
        r.guard.hits() == 1 && r.posts() == 1,
        "guard hits {}, POSTs {}",
        r.guard.hits(),
        r.posts()
    );
    assert_eq!(
        r.con.reqs()[0],
        "POST /proxy/protect/integration/v1/cameras/cam1/talkback-session"
    );
    assert_eq!(r.con.keys()[0], TEST_KEY);
    assert_eq!(r.con.bodies()[0].trim(), "{}");
    let head = r.con.heads()[0].to_lowercase();
    assert!(head.contains("content-type: application/json"), "{head}");
    let frame = b"0123456789ABCDEF".to_vec();
    a.input().send(frame.clone()).await.unwrap();
    // The pacer follows the frame with silence once it has played it; the fake ffmpeg copies all
    // of it. The frame comes first.
    let dir = &r.dir;
    wait_for("the frame reached ffmpeg's stdin", || {
        dir.read("stdin").starts_with(&frame)
    })
    .await;
    let args = String::from_utf8_lossy(&r.dir.read("args")).to_string();
    assert!(
        args.contains("libopus") && args.contains("rtp://192.168.2.9:7004"),
        "ffmpeg args: {args}"
    );
    assert!(
        !args.contains(TEST_KEY),
        "the API key reached ffmpeg's command line"
    );
    assert!(
        r.sink.got().ends_with("c1:answered,c1:active"),
        "events {}",
        r.sink.got()
    );
    r.d.stop().await.unwrap();
}

// TestTalkHangupStopsFFmpegAndEndsTheCall
#[tokio::test]
async fn talk_hangup_stops_ffmpeg_and_ends_the_call() {
    let r = Rig::new().await;
    r.ring(MIN);
    let a = answer(&r.d, "c1").await.unwrap();
    a.attach();
    let (up, down) = pids(&r.d);
    assert!(up != 0 && down != 0 && alive(up));
    r.d.hangup_call("c1").unwrap();
    assert!(
        all_gone(&[up, down]).await,
        "ffmpeg still running (or a zombie) after hangup"
    );
    assert!(next_frame(&a, 3).await.is_none(), "Out not closed");
    assert!(
        r.sink.got().ends_with("c1:ended"),
        "events {}",
        r.sink.got()
    );
    // the console was asked for nothing else (no teardown endpoint is invented)
    assert_eq!(r.con.reqs().len(), 1, "requests: {:?}", r.con.reqs());
}

// TestTalkDetachOfTheClientLegHangsUp
#[tokio::test]
async fn talk_detach_of_the_client_leg_hangs_up() {
    let r = Rig::new().await;
    r.ring(MIN);
    let a = answer(&r.d, "c1").await.unwrap();
    a.attach();
    a.detach();
    r.wait_ended("c1").await;
}

// TestTalkRefusalsLeaveTheRingAnswerable
#[tokio::test]
async fn talk_refusals_leave_the_ring_answerable() {
    type Setup = Box<dyn Fn(&Rig)>;
    let talkback =
        |body: &'static str| -> Setup { Box::new(move |r: &Rig| r.con.set_talkback(body)) };
    let cases: Vec<(&str, Setup, bool, usize, RigOpts)> = vec![
        (
            "guard busy",
            Box::new(|r| r.guard.set_body(r#"{"sessionUp":true}"#)),
            true,
            0,
            RigOpts::default(),
        ),
        (
            "guard unreadable",
            Box::new(|r| r.guard.set_body("nope")),
            false,
            0,
            RigOpts::default(),
        ),
        (
            "guard down",
            Box::new(|r| r.guard.close()),
            false,
            0,
            RigOpts::default(),
        ),
        (
            "session POST fails",
            Box::new(|r| r.con.set_status(500)),
            false,
            1,
            RigOpts::default(),
        ),
        (
            "codec is not opus",
            talkback(r#"{"url":"rtp://192.168.2.9:7004","codec":"aac","samplingRate":24000}"#),
            false,
            1,
            RigOpts::default(),
        ),
        (
            "no sampling rate",
            talkback(r#"{"url":"rtp://192.168.2.9:7004","codec":"opus"}"#),
            false,
            1,
            RigOpts::default(),
        ),
        (
            "sink is not rtp",
            talkback(r#"{"url":"file:///etc/passwd","codec":"opus","samplingRate":24000}"#),
            false,
            1,
            RigOpts::default(),
        ),
        (
            "ffmpeg missing",
            Box::new(|_| {}),
            false,
            0,
            RigOpts {
                ffmpeg: Some("/nonexistent/ffmpeg".into()),
                ..RigOpts::default()
            },
        ),
        (
            "no downlink_url",
            Box::new(|_| {}),
            false,
            0,
            RigOpts {
                no_downlink: true,
                ..RigOpts::default()
            },
        ),
        // Rust-only rows
        (
            "no url in the session",
            talkback(r#"{"codec":"opus","samplingRate":24000}"#),
            false,
            1,
            RigOpts::default(),
        ),
        (
            "session is not JSON",
            talkback("<html>"),
            false,
            1,
            RigOpts::default(),
        ),
        (
            "sink with a space",
            talkback(r#"{"url":"rtp://192.168.2.9:7004/a b","codec":"opus","samplingRate":24000}"#),
            false,
            1,
            RigOpts::default(),
        ),
        (
            "udp sink in upper case codec is fine, http sink is not",
            talkback(r#"{"url":"http://evil.example/x","codec":"OPUS","samplingRate":24000}"#),
            false,
            1,
            RigOpts::default(),
        ),
    ];
    for (name, setup, busy, posts, opts) in cases {
        let r = Rig::with(opts).await;
        setup(&r);
        r.ring(MIN);
        let e = answer(&r.d, "c1")
            .await
            .err()
            .unwrap_or_else(|| panic!("{name}: answer succeeded"));
        assert_eq!(e.is(Sentinel::Busy), busy, "{name}: busy={busy}: {e}");
        let text = e.message();
        assert!(
            !text.contains(TEST_KEY)
                && !text.contains("TOKEN-IN-ERROR-BODY")
                && !text.contains("192.168.2.9"),
            "{name}: secret in error: {text}"
        );
        assert_eq!(r.posts(), posts, "{name}: POSTs");
        assert!(
            r.d.ringing_call("c1") && !r.sink.got().contains("answered"),
            "{name}: ring lost or answered: {}",
            r.sink.got()
        );
        // nothing was started
        assert!(
            r.dir.read("args").is_empty() && r.dir.read("args_down").is_empty(),
            "{name}: ffmpeg was started"
        );
        assert!(
            !r.sink.heard().contains(TEST_KEY),
            "{name}: {}",
            r.sink.heard()
        );
        r.d.stop().await.unwrap();
    }
}

// TestTalkSecondAnswererNeverTouchesTheConsole
#[tokio::test]
async fn talk_second_answerer_never_touches_the_console() {
    let r = Rig::new().await;
    r.ring(MIN);
    answer(&r.d, "c1").await.unwrap();
    let e = answer(&r.d, "c1").await.err().unwrap();
    assert!(e.is(Sentinel::AlreadyAnswered), "{e}");
    assert!(
        r.guard.hits() == 1 && r.posts() == 1,
        "guard {} POSTs {}",
        r.guard.hits(),
        r.posts()
    );
    r.d.stop().await.unwrap();
}

// TestTalkUnattachedSessionIsClosedAfterTheOrphanCap
#[tokio::test]
async fn talk_unattached_session_is_closed_after_the_orphan_cap() {
    let t = TalkTiming {
        orphan: Duration::from_millis(60),
        ..TalkTiming::default()
    };
    let r = Rig::with(RigOpts {
        timing: t,
        ..RigOpts::default()
    })
    .await;
    r.ring(MIN);
    answer(&r.d, "c1").await.unwrap();
    r.wait_ended("c1").await;
    assert!(
        r.sink
            .heard()
            .contains("|c1|ended|no client attached within the grace period"),
        "{}",
        r.sink.heard()
    );
    r.d.stop().await.unwrap();

    // an attached one is not
    let r2 = Rig::with(RigOpts {
        timing: t,
        ..RigOpts::default()
    })
    .await;
    r2.ring(MIN);
    let a = answer(&r2.d, "c1").await.unwrap();
    a.attach();
    tokio::time::sleep(Duration::from_millis(200)).await;
    assert!(
        !r2.sink.got().ends_with("c1:ended"),
        "attached session was closed by the orphan cap"
    );
    r2.d.stop().await.unwrap();
}

// TestTalkFFmpegDyingEndsTheCall
#[tokio::test]
async fn talk_ffmpeg_dying_ends_the_call() {
    let r = Rig::new().await;
    r.ring(MIN);
    let a = answer(&r.d, "c1").await.unwrap();
    a.attach();
    kill9(pids(&r.d).0);
    r.wait_ended("c1").await;
    r.d.stop().await.unwrap();
}

// ---- talk_down_test.go -------------------------------------------------------------------------

// TestTalkDownlinkDeliversExact1280ByteFramesAndNoPartialOne
#[tokio::test]
async fn talk_downlink_delivers_exact_1280_byte_frames_and_no_partial_one() {
    let r = Rig::new().await;
    let want = pcm(3 * 1280 + 500, 1); // three whole frames and a stub that must not be sent
    r.dir.write("down.pcm", &want);
    r.ring(MIN);
    let a = answer(&r.d, "c1").await.unwrap();
    a.attach();
    r.release();
    let mut got = Vec::new();
    for i in 0..3 {
        let f = next_frame(&a, 3)
            .await
            .unwrap_or_else(|| panic!("frame {i} never came"));
        assert_eq!(f.len(), 1280, "frame {i} is {} bytes", f.len());
        got.extend_from_slice(&f);
    }
    assert!(
        got == want[..3 * 1280],
        "frames are not the stream, in order"
    );
    // After the three real frames the pacer keeps the stream continuous with SILENCE frames
    // (whole, all zero), never the 500-byte stub.
    let f = next_frame(&a, 1)
        .await
        .expect("no silence frame after the audio ended");
    assert!(
        f.len() == 1280 && f.iter().all(|b| *b == 0),
        "after the audio came a non-silence frame ({} bytes)",
        f.len()
    );
    let h = r.d.health();
    assert!(
        h.session_up && h.frames_from_camera == 3 && h.drops_down == 0,
        "health {h:?}"
    );
    let args = String::from_utf8_lossy(&r.dir.read("args_down")).to_string();
    for w in [
        "-rtsp_transport\ntcp",
        "rtsp://127.0.0.1:1/doorbell",
        "-ar\n16000",
        "-ac\n1",
        "s16le",
        "pipe:1",
    ] {
        assert!(args.contains(w), "downlink args lack {w:?}: {args}");
    }
    r.d.stop().await.unwrap();
}

// TestTalkDownlinkCountsDropsWhenNobodyReadsAndDiscardsBeforeAttach
#[tokio::test]
async fn talk_downlink_counts_drops_when_nobody_reads_and_discards_before_attach() {
    let r = Rig::new().await;
    r.dir.write("down.pcm", &pcm(40 * 1280, 1));
    r.ring(MIN);
    let a = answer(&r.d, "c1").await.unwrap();
    r.release(); // frames flow BEFORE attach: discarded, not queued, not drops
    let d = r.d.clone();
    wait_for("40 frames from the camera", move || {
        d.health().frames_from_camera == 40
    })
    .await;
    let h = r.d.health();
    let queued = a.output().lock().await.try_recv().is_ok();
    assert!(
        h.drops_down == 0 && !queued,
        "pre-attach frames were kept or counted as drops: {h:?} queued={queued}"
    );
    r.d.stop().await.unwrap();
}

// TestTalkDownlinkBurstKeepsTheNewestFramesAndDropsTheOldest
#[tokio::test]
async fn talk_downlink_burst_keeps_the_newest_frames_and_drops_the_oldest() {
    let r = Rig::new().await;
    let all = pcm(40 * 1280, 1);
    r.dir.write("down.pcm", &all);
    r.ring(MIN);
    let a = answer(&r.d, "c1").await.unwrap();
    a.attach();
    r.release(); // 40 frames at once: the 200 ms cap (5 frames) keeps the newest
    let d = r.d.clone();
    wait_for("40 frames from the camera", move || {
        d.health().frames_from_camera == 40
    })
    .await;
    let first = next_frame(&a, 3).await.unwrap();
    assert!(
        first == all[35 * 1280..36 * 1280],
        "the burst did not keep the newest frames"
    );
    let st = r.d.stats_for_test().unwrap().down_pacer().unwrap().stats();
    assert!(st.drops_oldest == 35 && st.frames_in == 40, "{st:?}");
    r.d.stop().await.unwrap();
}

// TestTalkDownlinkAppliesGainWithSaturation
#[tokio::test]
async fn talk_downlink_applies_gain_with_saturation() {
    let r = Rig::with(RigOpts {
        gain: Some(4),
        ..RigOpts::default()
    })
    .await;
    let mut frame = vec![0u8; 1280];
    frame[0] = 100; // 100 -> 400
    frame[1] = 0;
    frame[2] = 0xFF; // 32767 -> saturates
    frame[3] = 0x7F;
    frame[4] = 0x00; // -32768 -> saturates
    frame[5] = 0x80;
    r.dir.write("down.pcm", &frame);
    r.ring(MIN);
    let a = answer(&r.d, "c1").await.unwrap();
    a.attach();
    r.release();
    let f = next_frame(&a, 3).await.unwrap();
    assert_eq!(
        &f[..6],
        &[0x90, 0x01, 0xFF, 0x7F, 0x00, 0x80],
        "gain x4: {:02x?}",
        &f[..6]
    );
    r.d.stop().await.unwrap();
}

// TestTalkUplinkIsCountedAndTheDownlinkDyingEndsTheCall
#[tokio::test]
async fn talk_uplink_is_counted_and_the_downlink_dying_ends_the_call() {
    let r = Rig::new().await;
    r.ring(MIN);
    let a = answer(&r.d, "c1").await.unwrap();
    a.attach();
    a.input().send(pcm(640, 1)).await.unwrap();
    let d = r.d.clone();
    wait_for("640 bytes to the camera", move || {
        d.health().bytes_to_camera == 640
    })
    .await;
    kill9(pids(&r.d).1);
    r.wait_ended("c1").await;
    let h = r.d.health();
    assert!(!h.session_up, "session_up after the call ended: {h:?}");
    assert!(next_frame(&a, 3).await.is_none(), "Out not closed");
    r.d.stop().await.unwrap();
}

// TestTalkDownlinkFailureShowsAScrubbedLastLineAndEndsTheCall
#[tokio::test]
async fn talk_downlink_failure_shows_a_scrubbed_last_line_and_ends_the_call() {
    let r = Rig::new().await;
    r.dir.write("down_fail", b"");
    r.ring(MIN);
    answer(&r.d, "c1").await.unwrap();
    r.wait_ended("c1").await;
    let h = r.d.health();
    assert!(
        h.last_ffmpeg.contains("Connection to")
            && !h.last_ffmpeg.contains("s3cret")
            && !h.last_ffmpeg.contains("10.6.0.6"),
        "last ffmpeg line {:?}",
        h.last_ffmpeg
    );
    // the reason names the path and carries the scrubbed line, never the URL
    let heard = r.sink.heard();
    assert!(
        heard
            .contains("|c1|ended|the visitor audio (downlink ffmpeg) stopped: Connection to <url>"),
        "{heard}"
    );
    assert!(!heard.contains("s3cret"), "{heard}");
    r.d.stop().await.unwrap();
}

// TestTalkHangupStopsBothFFmpegs
#[tokio::test]
async fn talk_hangup_stops_both_ffmpegs() {
    let r = Rig::new().await;
    r.ring(MIN);
    let a = answer(&r.d, "c1").await.unwrap();
    a.attach();
    let (up, down) = pids(&r.d);
    r.d.hangup_call("c1").unwrap();
    assert!(
        all_gone(&[up]).await,
        "uplink ffmpeg still running after hangup"
    );
    assert!(
        all_gone(&[down]).await,
        "downlink ffmpeg still running after hangup"
    );
}

// ---- talk_reason_test.go -----------------------------------------------------------------------

// TestTalkRefusedAnswerIsReportedWithItsReasonAndNoCredential
#[tokio::test]
async fn talk_refused_answer_is_reported_with_its_reason_and_no_credential() {
    let r = Rig::new().await;
    r.guard.set_body(r#"{"sessionUp":true}"#);
    r.ring(MIN);
    assert!(
        answer(&r.d, "c1").await.is_err(),
        "the busy guard did not refuse"
    );
    let got = r.sink.heard();
    assert!(
        got.contains("ha_event:front|c1|answer_failed|") && got.contains("old intercom"),
        "sink heard {got:?}"
    );
    assert!(
        r.d.health().last_end.contains("old intercom"),
        "health.last_end = {:?}",
        r.d.health().last_end
    );
    assert!(
        !got.contains(TEST_KEY) && !got.contains("http://"),
        "reason leaks: {got:?}"
    );
    assert!(r.d.ringing_call("c1"), "the ring must stay answerable");
}

// TestTalkFFmpegDyingIsReportedAsWhichPathStopped
#[tokio::test]
async fn talk_ffmpeg_dying_is_reported_as_which_path_stopped() {
    let r = Rig::new().await;
    r.ring(MIN);
    let a = answer(&r.d, "c1").await.unwrap();
    a.attach();
    kill9(pids(&r.d).0);
    r.wait_ended("c1").await;
    let got = r.sink.heard();
    assert!(
        got.contains("|c1|ended|") && got.contains("stopped"),
        "sink heard {got:?}"
    );
    assert!(
        got.contains("the microphone path (uplink ffmpeg) stopped"),
        "{got}"
    );
    assert!(
        !r.d.health().last_end.contains("hung up"),
        "a dead ffmpeg read as a hangup: {:?}",
        r.d.health().last_end
    );
    r.d.stop().await.unwrap();
}

// TestTalkNormalHangupAndExpiryAreNamed
#[tokio::test]
async fn talk_normal_hangup_and_expiry_are_named() {
    let r = Rig::new().await;
    r.ring(MIN);
    let a = answer(&r.d, "c1").await.unwrap();
    a.attach();
    r.d.hangup_call("c1").unwrap();
    tokio::time::sleep(Duration::from_millis(100)).await; // let the dying ffmpegs finish; they must not rewrite the reason
    assert_eq!(r.d.health().last_end, "hung up", "after a hangup");

    let (d, s, _, _) = new_talk(short_holds()).await;
    d.external_ring("c2", Duration::from_millis(30)).unwrap();
    let s2 = s.clone();
    wait_for("c2 ended", move || s2.got().ends_with("c2:ended")).await;
    assert!(
        s.heard().contains("|c2|ended|the ring expired unanswered"),
        "sink heard {:?}",
        s.heard()
    );
}

// TestTalkHangupCallWhyNamesWhoHungUp
#[tokio::test]
async fn talk_hangup_call_why_names_who_hung_up() {
    let r = Rig::new().await;
    r.ring(MIN);
    let a = answer(&r.d, "c1").await.unwrap();
    a.attach();
    r.d.hangup_call_why("c1", "cancelled").unwrap();
    tokio::time::sleep(Duration::from_millis(100)).await;
    assert_eq!(r.d.health().last_end, "hung up (cancelled)");
    assert!(
        r.sink.heard().contains("|c1|ended|hung up (cancelled)"),
        "sink heard {:?}",
        r.sink.heard()
    );
    let e = r.d.hangup_call_why("nope", "x").unwrap_err();
    assert!(e.is(Sentinel::NoSuchCall), "an unknown call: {e}");
}

// ---- Rust-only ---------------------------------------------------------------------------------

// Rust-only: a call out through the real media (the guard first, the session, both ffmpegs, the
// claimed audio, the longer grace, the hang-up)
#[tokio::test]
async fn a_call_out_through_the_real_media() {
    use transport_rs::driver::{AudioClaimer, CallDialer, MediaTarget};
    let r = Rig::new().await;
    r.d.dial_call(&CancelToken::new(), "out-1", MediaTarget::default())
        .await
        .unwrap();
    assert!(r.guard.hits() == 1 && r.posts() == 1);
    assert_eq!(
        r.sink.got(),
        "avail:ha_event:front:up,out-1:ringing,out-1:answered,out-1:active"
    );
    let a = r.d.claimed_audio("out-1").expect("claimed");
    a.attach();
    let (up, down) = pids(&r.d);
    ReasonedHangupper::hangup_call_why(&*r.d, "out-1", "bell_pressed").unwrap();
    assert!(all_gone(&[up, down]).await);
    assert_eq!(r.d.health().last_end, "hung up (bell_pressed)");
    // refused while the guard says busy: nothing reported, nothing left behind
    r.guard.set_body(r#"{"sessionUp":true}"#);
    assert!(r
        .d
        .dial_call(&CancelToken::new(), "out-2", MediaTarget::default())
        .await
        .is_err());
    assert!(!r.sink.got().contains("out-2"), "{}", r.sink.got());
    assert!(
        r.sink.heard().contains("|out-2|dial_failed|"),
        "{}",
        r.sink.heard()
    );
    r.d.external_ring("r1", MIN)
        .expect("the door stayed reserved");
}

// Rust-only: a dialled session whose phone never connects is closed after the dial grace
#[tokio::test]
async fn a_dialled_session_nobody_attaches_to_is_closed_after_the_dial_grace() {
    use transport_rs::driver::{CallDialer, MediaTarget};
    let t = TalkTiming {
        dial_orphan: Duration::from_millis(80),
        ..TalkTiming::default()
    };
    let r = Rig::with(RigOpts {
        timing: t,
        ..RigOpts::default()
    })
    .await;
    r.d.dial_call(&CancelToken::new(), "out-1", MediaTarget::default())
        .await
        .unwrap();
    r.wait_ended("out-1").await;
    assert!(
        r.sink
            .heard()
            .contains("no client attached within the grace period"),
        "{}",
        r.sink.heard()
    );
    // one that attached in time is kept
    let r2 = Rig::with(RigOpts {
        timing: t,
        ..RigOpts::default()
    })
    .await;
    r2.d.dial_call(&CancelToken::new(), "out-1", MediaTarget::default())
        .await
        .unwrap();
    use transport_rs::driver::AudioClaimer;
    r2.d.claimed_audio("out-1").unwrap().attach();
    tokio::time::sleep(Duration::from_millis(250)).await;
    assert!(!r2.sink.got().contains("ended"), "{}", r2.sink.got());
    r2.d.stop().await.unwrap();
    r.d.stop().await.unwrap();
}

// Rust-only: Stop with a live call reaps both ffmpegs before it returns (no zombie), reports
// nothing, and a hub shutdown (the run context) kills them too
#[tokio::test]
async fn stop_and_the_hubs_context_leave_no_ffmpeg_behind() {
    let r = Rig::new().await;
    r.ring(MIN);
    let a = answer(&r.d, "c1").await.unwrap();
    a.attach();
    let (up, down) = pids(&r.d);
    let before = r.sink.got();
    r.d.stop().await.unwrap();
    assert!(
        !alive(up) && !alive(down),
        "Stop returned before ffmpeg was reaped"
    );
    assert_eq!(r.sink.got(), before, "Stop reported");

    // the run context ending kills them without a Stop
    let con = common::unifi::FakeProtect::new().await;
    let guard = FakeGuard::new(r#"{"sessionUp":false}"#).await;
    let dir = TmpDir::new("ctx");
    let mut d = TalkDriver::default();
    d.set_scheme("http");
    d.set_ffmpeg_binary(&fake_ffmpeg_both(&dir));
    d.configure(&talk_cfg_with(json!({
        "protect_host": con.host(), "guard_url": guard.url(), "downlink_url": "rtsp://127.0.0.1:1/x"
    })))
    .unwrap();
    let d = std::sync::Arc::new(d);
    let ctx = CancelToken::new();
    d.start(&ctx, TalkSink::new()).await.unwrap();
    d.external_ring("c1", MIN).unwrap();
    d.answer_with_audio(&CancelToken::new(), "c1")
        .await
        .unwrap();
    let (up, down) = pids(&d);
    ctx.cancel();
    assert!(
        all_gone(&[up, down]).await,
        "the hub's context ending left ffmpeg running"
    );
}

// Rust-only: the health block over a call: bytes, frames, session_up, and the wire form
#[tokio::test]
async fn health_follows_the_call() {
    let r = Rig::new().await;
    assert_eq!(r.d.health().to_wire(), "{\"session_up\":false,\"bytes_to_camera\":0,\"frames_from_camera\":0,\"drops_up\":0,\"drops_down\":0,\"last_ffmpeg\":\"\",\"last_end\":\"\"}");
    r.ring(MIN);
    let a = answer(&r.d, "c1").await.unwrap();
    a.attach();
    assert!(r.d.health().session_up);
    r.d.hangup_call("c1").unwrap();
    let d = r.d.clone();
    wait_for("session down", move || !d.health().session_up).await;
    assert_eq!(r.d.health().last_end, "hung up");
}

// Rust-only: the camera id reaches the console path escaped, the key never leaves the header
#[tokio::test]
async fn the_camera_id_is_escaped_in_the_session_request() {
    let r = Rig::with(RigOpts {
        extra: json!({"camera_id": "cam/1 x"}),
        ..RigOpts::default()
    })
    .await;
    r.ring(MIN);
    let _ = answer(&r.d, "c1").await;
    assert_eq!(
        r.con.reqs()[0],
        "POST /proxy/protect/integration/v1/cameras/cam%2F1%20x/talkback-session"
    );
    r.d.stop().await.unwrap();
}

// Rust-only: an answer is a ConnectAnswerer call whatever the ctx: a cancelled request context
// while the console is silent gives the ring back
#[tokio::test]
async fn a_cancelled_answer_context_gives_the_ring_back() {
    let r = Rig::new().await;
    r.con.set_silent(true);
    r.ring(MIN);
    let ctx = CancelToken::new();
    let c2 = ctx.clone();
    tokio::spawn(async move {
        tokio::time::sleep(Duration::from_millis(150)).await;
        c2.cancel();
    });
    let e = r.d.answer_with_audio(&ctx, "c1").await.err().unwrap();
    assert!(e.message().contains("context canceled"), "{e}");
    assert!(r.d.ringing_call("c1"), "the cancelled answer kept the ring");
    assert!(!e.message().contains(TEST_KEY));
    assert!(r.dir.read("args").is_empty(), "ffmpeg started");
    r.d.stop().await.unwrap();
}

// Rust-only (PARITY.md 135): a grandchild that inherited ffmpeg's stderr and stdout (a shell
// pipeline's other half) must not hold the end of the call: the reader of the pipe is given 2 s
// after the process is reaped, then abandoned. Go's `<-errDone` would wait for ever.
#[tokio::test]
async fn a_grandchild_holding_the_pipes_does_not_hold_the_call() {
    let r = Rig::new().await;
    let dir = &r.dir;
    // the downlink script leaves a background sleep that holds both pipes after the kill
    let p = dir.file("ffmpeg");
    let d = dir.path().display();
    write_script(
        &p,
        &format!(
            "#!/bin/sh\ncase \"$*\" in\n*pipe:1*) sleep 6 & exec sleep 30;;\n*) cat > {d}/stdin;;\nesac\n"
        ),
    );
    r.ring(MIN);
    let a = answer(&r.d, "c1").await.unwrap();
    a.attach();
    tokio::time::sleep(Duration::from_millis(150)).await;
    let t0 = std::time::Instant::now();
    r.d.hangup_call("c1").unwrap();
    // the audio output closes once the downlink task is over
    let out = a.output();
    let end = tokio::time::timeout(Duration::from_secs(4), async move {
        let mut g = out.lock().await;
        while g.recv().await.is_some() {}
    })
    .await;
    assert!(end.is_ok(), "the held pipe held the call for more than 4 s");
    let took = t0.elapsed();
    assert!(
        took >= Duration::from_millis(1500) && took < Duration::from_millis(3500),
        "the grace is 2 s: {took:?}"
    );
    r.d.stop().await.unwrap();
}

// ---- Phase R round 1, group F ---------------------------------------------------------------

/// R1-11: the error of the talkback session goes out as Go returns it: not cut at 300 bytes and
/// with its URL, the closing quote and colon intact (Go scrubs only the `note()` of the stats).
#[tokio::test]
async fn a_failed_session_request_is_reported_as_go_words_it() {
    let r = Rig::with(RigOpts {
        extra: json!({"protect_host": "127.0.0.1:1"}),
        ..RigOpts::default()
    })
    .await;
    r.ring(MIN);
    let e = answer(&r.d, "c1").await.err().expect("a refused console");
    assert_eq!(
        e.message(),
        "unifi_protect_talk: opening the talkback session: POST /proxy/protect/integration/v1/cameras/cam1/talkback-session: Post \"http://127.0.0.1:1/proxy/protect/integration/v1/cameras/cam1/talkback-session\": dial tcp 127.0.0.1:1: connect: connection refused"
    );
    assert!(!e.message().contains(TEST_KEY));
    assert!(!r.sink.heard().contains(TEST_KEY), "{}", r.sink.heard());
    r.d.stop().await.unwrap();
}

/// R1-33: the ffmpeg binary is looked up once per answer (it was three times: the check, the
/// uplink and the downlink), and the one that was found is the one that runs.
#[tokio::test]
async fn the_binary_is_looked_up_once_per_answer() {
    let r = Rig::new().await;
    let ff = r.dir.file("ffmpeg").display().to_string();
    transport_rs::driver::unifiprotect::ffmpeg::count_lookups_of(&ff);
    r.ring(MIN);
    let a = answer(&r.d, "c1").await.unwrap();
    a.attach();
    assert_eq!(
        transport_rs::driver::unifiprotect::ffmpeg::lookups_of(&ff),
        1,
        "the binary was looked up more than once for one answer"
    );
    r.d.stop().await.unwrap();
}
