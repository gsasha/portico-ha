//! The `unifi_protect` driver's talkback (slice 7b, `talkback.go`): the four `ffmpeg` tests of
//! `unifiprotect_test.go` (the Go name is in a comment on each), run against a fake console and a
//! fake `ffmpeg` shell script, and the behaviours this port adds ("Rust-only"). A real ffmpeg is
//! never needed.

#![allow(unsafe_code)] // a libc call in a test helper (kill / signal); the library has none

mod common;

use std::sync::Arc;
use std::time::Duration;

use common::talk::*;
use common::unifi::*;
use serde_json::json;
use transport_rs::cancel::CancelToken;
use transport_rs::driver::unifiprotect::api::TalkbackSession;
use transport_rs::driver::unifiprotect::talkback::{start_talkback, start_talkback_with};
use transport_rs::driver::unifiprotect::UnifiProtect;
use transport_rs::driver::{CallHangupper, Driver, TalkbackSink};

fn configured(f: &FakeProtect, ffmpeg: &str) -> UnifiProtect {
    let mut d = UnifiProtect::default();
    d.set_scheme("http");
    d.set_ffmpeg_binary(ffmpeg);
    d.configure(
        json!({"nvr_host": f.host(), "nvr_api_key": TEST_KEY, "camera_id": "cam1"})
            .as_object()
            .unwrap(),
    )
    .unwrap();
    d
}

fn sess(url: &str) -> TalkbackSession {
    TalkbackSession {
        url: url.to_string(),
        codec: "opus".into(),
        sampling_rate: 24000,
        bits_per_sample: 16,
    }
}

fn alive(pid: u32) -> bool {
    std::path::Path::new(&format!("/proc/{pid}")).exists()
}

async fn gone(pid: u32) -> bool {
    for _ in 0..300 {
        if !alive(pid) {
            return true;
        }
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    false
}

// TestOpenRelayStartsFFmpegAndStreamsPCM
#[tokio::test]
async fn open_relay_starts_ffmpeg_and_streams_pcm() {
    let f = FakeProtect::new().await;
    let dir = TmpDir::new("tb");
    let d = configured(&f, &fake_ffmpeg(&dir));
    d.open_relay(&CancelToken::new()).await.expect("OpenRelay");
    let got = f.reqs();
    assert!(
        got.len() == 1 && got[0].ends_with("/cameras/cam1/talkback-session"),
        "requests = {got:?}"
    );
    assert!(
        d.open_relay(&CancelToken::new()).await.is_err(),
        "a second OpenRelay while one is open should fail"
    );
    let input = d
        .talkback_audio()
        .expect("no talkback audio after OpenRelay");
    let pcm = vec![1u8, 2, 3, 4, 5, 6, 7, 8];
    input.send(pcm.clone()).await.unwrap();
    wait_for("the frame reached ffmpeg's stdin", || {
        dir.read("stdin") == pcm
    })
    .await;
    d.stop().await.unwrap();

    let args = String::from_utf8_lossy(&dir.read("args")).to_string();
    let a: Vec<&str> = args.split_whitespace().collect();
    let joined = a.join(" ");
    for want in [
        "-f s16le",
        "-ar 16000",
        "-ac 1",
        "-i pipe:0",
        "-c:a libopus",
        "-ar 24000",
        "-f rtp",
    ] {
        assert!(
            joined.contains(want),
            "ffmpeg args {joined:?} missing {want:?}"
        );
    }
    assert_eq!(*a.last().unwrap(), "rtp://192.168.2.9:7004", "sink");
    assert!(
        !joined.contains(TEST_KEY),
        "API key reached ffmpeg's command line"
    );
    assert!(
        d.talkback_audio().is_none(),
        "talkback still held after Stop"
    );
}

// TestOpenRelayRefusesUnsafeSinkAndCodec
#[tokio::test]
async fn open_relay_refuses_unsafe_sink_and_codec() {
    for (name, body) in [
        (
            "file sink",
            r#"{"url":"file:///etc/passwd","codec":"opus","samplingRate":24000}"#,
        ),
        (
            "http sink",
            r#"{"url":"http://evil.example/x","codec":"opus","samplingRate":24000}"#,
        ),
        (
            "unknown cx",
            r#"{"url":"rtp://192.168.2.9:7004","codec":"vorbis","samplingRate":24000}"#,
        ),
        ("no url", r#"{"codec":"opus","samplingRate":24000}"#),
        // Rust-only rows
        (
            "sink with a newline",
            "{\"url\":\"rtp://192.168.2.9:7004/a\\nb\",\"codec\":\"opus\",\"samplingRate\":24000}",
        ),
        (
            "sink that looks like an option",
            r#"{"url":"-i rtp://x","codec":"opus","samplingRate":24000}"#,
        ),
    ] {
        let f = FakeProtect::new().await;
        f.set_talkback(body);
        let dir = TmpDir::new("tb");
        let d = configured(&f, &fake_ffmpeg(&dir));
        let e = d
            .open_relay(&CancelToken::new())
            .await
            .err()
            .unwrap_or_else(|| panic!("{name}: accepted"));
        assert!(
            !e.message().contains("192.168.2.9")
                && !e.message().contains("evil.example")
                && !e.message().contains(TEST_KEY),
            "{name}: {e}"
        );
        assert!(dir.read("args").is_empty(), "{name}: ffmpeg was started");
        assert!(
            d.talkback_audio().is_none(),
            "{name}: talkback held after a refusal"
        );
    }
}

// TestOpenRelayWithoutFFmpegIsAClearError
#[tokio::test]
async fn open_relay_without_ffmpeg_is_a_clear_error() {
    let f = FakeProtect::new().await;
    let d = configured(&f, "/nonexistent/ffmpeg");
    let e = d.open_relay(&CancelToken::new()).await.unwrap_err();
    assert!(
        e.message().contains("ffmpeg"),
        "err = {e}, want one naming ffmpeg"
    );
    // Rust-only (PARITY.md 152): found missing BEFORE the console is asked for a session
    assert!(f.reqs().is_empty(), "a session was armed: {:?}", f.reqs());
    assert_eq!(
        e.message(),
        "unifi_protect: ffmpeg not found (installer prerequisite on HA Core, §9.9): exec: \"/nonexistent/ffmpeg\": stat /nonexistent/ffmpeg: no such file or directory"
    );
    // and by name on PATH, as the production default is
    let d = configured(&f, "no-such-ffmpeg-xyzzy");
    let e = d.open_relay(&CancelToken::new()).await.unwrap_err();
    assert!(
        e.message()
            .ends_with("exec: \"no-such-ffmpeg-xyzzy\": executable file not found in $PATH"),
        "{e}"
    );
}

// TestTalkbackEndsWhenFFmpegDies
#[tokio::test]
async fn talkback_ends_when_ffmpeg_dies() {
    let f = FakeProtect::new().await;
    let dir = TmpDir::new("tb");
    let p = dir.file("ffmpeg");
    write_script(&p, "#!/bin/sh\nexit 3\n");
    let d = configured(&f, &p.display().to_string());
    d.open_relay(&CancelToken::new()).await.unwrap();
    // A dead ffmpeg frees the slot, so the next OpenRelay may try again.
    wait_for("the slot is free", || d.talkback_audio().is_none()).await;
    d.open_relay(&CancelToken::new())
        .await
        .expect("the next OpenRelay");
    d.stop().await.unwrap();
}

// ---- Rust-only ---------------------------------------------------------------------------------

// The ring path of the hub: AnswerTalkback opens the real talkback, the audio reaches ffmpeg, a
// hang-up reaps it.
#[tokio::test]
async fn a_ring_is_answered_through_the_real_talkback_and_the_hangup_reaps_ffmpeg() {
    let f = FakeProtect::new().await;
    let con = FakeConsole::new(false).await;
    let dir = TmpDir::new("tb");
    let mut d = UnifiProtect::default();
    d.set_scheme("http");
    d.set_timing(fast());
    d.set_ffmpeg_binary(&fake_ffmpeg(&dir));
    d.configure(
        json!({"nvr_host": con.host(), "nvr_api_key": TEST_KEY, "camera_id": "cam1", "ring_events": true})
            .as_object()
            .unwrap(),
    )
    .unwrap();
    d.set_api_for_test(transport_rs::driver::unifiprotect::api::ProtectApi::new(
        "http",
        &f.host(),
        TEST_KEY,
        true,
    ));
    let d = Arc::new(d);
    let sink = RecSink::new();
    d.start(&CancelToken::new(), sink.clone()).await.unwrap();
    con.last_conn().await;
    con.send(r#"{"type":"add","item":{"id":"n1","modelKey":"event","type":"ring","start":1700000000000,"device":"cam1"}}"#).await;
    wait_for("ringing", || sink.has("ringing unifi-n1")).await;
    let ta = Driver::as_talkback_answerer(&*d).expect("TalkbackAnswerer");
    assert!(ta.ringing_call("unifi-n1"));
    let t = ta
        .answer_talkback(&CancelToken::new(), "unifi-n1")
        .await
        .unwrap();
    t.input().send(vec![9, 9, 9, 9]).await.unwrap();
    wait_for("audio at ffmpeg", || dir.read("stdin") == vec![9, 9, 9, 9]).await;
    assert!(sink.has("state unifi-n1 active"));
    d.hangup_call("unifi-n1").unwrap();
    // the call ended and the process is gone: closed() resolves once it is reaped
    tokio::time::timeout(Duration::from_secs(3), t.closed())
        .await
        .expect("closed");
    d.stop().await.unwrap();
}

// TestTalkbackDyingEndsTheCall (ring_test.go:467-486) through a REAL process (R2-75): a ring is
// answered through `FfmpegOpener`, ffmpeg then exits with status 1, and the call ends.
#[tokio::test]
async fn a_ring_answered_through_a_real_ffmpeg_that_dies_ends_the_call() {
    let f = FakeProtect::new().await;
    let con = FakeConsole::new(false).await;
    let dir = TmpDir::new("tb");
    let ff = dir.file("ffmpeg");
    write_script(&ff, "#!/bin/sh\nsleep 0.4\nexit 1\n");
    let mut d = UnifiProtect::default();
    d.set_scheme("http");
    d.set_timing(fast());
    d.set_ffmpeg_binary(&ff.display().to_string());
    d.configure(
        json!({"nvr_host": con.host(), "nvr_api_key": TEST_KEY, "camera_id": "cam1", "ring_events": true})
            .as_object()
            .unwrap(),
    )
    .unwrap();
    d.set_api_for_test(transport_rs::driver::unifiprotect::api::ProtectApi::new(
        "http",
        &f.host(),
        TEST_KEY,
        true,
    ));
    let d = Arc::new(d);
    let sink = RecSink::new();
    d.start(&CancelToken::new(), sink.clone()).await.unwrap();
    con.last_conn().await;
    con.send(r#"{"type":"add","item":{"id":"n2","modelKey":"event","type":"ring","start":1700000000000,"device":"cam1"}}"#).await;
    wait_for("ringing", || sink.has("ringing unifi-n2")).await;
    let ta = Driver::as_talkback_answerer(&*d).expect("TalkbackAnswerer");
    let _t = ta
        .answer_talkback(&CancelToken::new(), "unifi-n2")
        .await
        .unwrap();
    wait_for("the call ends when ffmpeg dies", || {
        sink.has("state unifi-n2 ended")
    })
    .await;
    assert!(!ta.ringing_call("unifi-n2"));
    d.stop().await.unwrap();
}

// Rust-only: close reaps the process (no zombie), is idempotent, and `done` is set
#[tokio::test]
async fn close_reaps_ffmpeg_and_is_idempotent() {
    let dir = TmpDir::new("tb");
    let ff = fake_ffmpeg(&dir);
    let ctx = CancelToken::new();
    let tb = start_talkback(&ctx, &ff, &sess("rtp://192.168.2.9:7004")).unwrap();
    let pid = tb.pid().unwrap();
    assert!(alive(pid));
    assert!(!tb.done().is_cancelled());
    tb.close().await;
    tb.close().await;
    assert!(tb.done().is_cancelled());
    assert!(gone(pid).await, "zombie or running after close");
    assert_eq!(tb.err(), None, "a plain close has no error");
    // closing through the sink trait is the same
    let tb = start_talkback(&ctx, &ff, &sess("udp://192.168.2.9:7004")).unwrap();
    let pid = tb.pid().unwrap();
    let sink: &dyn TalkbackSink = &tb;
    sink.close().unwrap();
    sink.closed().await;
    assert!(gone(pid).await);
}

// Rust-only: the driver's context ending kills ffmpeg (Go: the pump selects on ctx)
#[tokio::test]
async fn the_run_context_ending_kills_ffmpeg() {
    let dir = TmpDir::new("tb");
    let ctx = CancelToken::new();
    let tb = start_talkback(&ctx, &fake_ffmpeg(&dir), &sess("rtp://192.168.2.9:7004")).unwrap();
    let pid = tb.pid().unwrap();
    ctx.cancel();
    tokio::time::timeout(Duration::from_secs(3), tb.done().cancelled())
        .await
        .expect("done");
    assert!(gone(pid).await);
    assert_eq!(tb.err(), None);
}

// Rust-only: ffmpeg dying on its own says how, and is reaped
#[tokio::test]
async fn an_ffmpeg_that_dies_is_explained_and_reaped() {
    let dir = TmpDir::new("tb");
    let p = dir.file("ffmpeg");
    write_script(&p, "#!/bin/sh\nexit 3\n");
    let tb = start_talkback(
        &CancelToken::new(),
        &p.display().to_string(),
        &sess("rtp://h:1"),
    )
    .unwrap();
    let pid = tb.pid().unwrap();
    tokio::time::timeout(Duration::from_secs(3), tb.done().cancelled())
        .await
        .expect("done");
    assert_eq!(tb.err().as_deref(), Some("ffmpeg exited: exit status 3"));
    assert!(gone(pid).await);
    // killed from outside
    write_script(&p, "#!/bin/sh\nexec sleep 30\n");
    let tb = start_talkback(
        &CancelToken::new(),
        &p.display().to_string(),
        &sess("rtp://h:1"),
    )
    .unwrap();
    let pid = tb.pid().unwrap();
    unsafe { libc::kill(pid as i32, libc::SIGKILL) };
    tokio::time::timeout(Duration::from_secs(3), tb.done().cancelled())
        .await
        .expect("done");
    assert_eq!(tb.err().as_deref(), Some("ffmpeg exited: signal: killed"));
    assert!(gone(pid).await);
}

// R2-29 (PARITY 132): a write to an ffmpeg that has closed its stdin fails; its exit status then
// explains it within a 1 s grace (`ffmpeg exited: <nil>` for a clean exit: the exit watcher alone
// says nothing for status 0), and an ffmpeg that stays up is NOT ended by a failed write.
#[tokio::test]
async fn a_write_to_a_closed_stdin_waits_for_the_exit_status() {
    let dir = TmpDir::new("tb");
    let p = dir.file("ffmpeg");
    let ready = dir.file("ready");
    write_script(
        &p,
        &format!(
            "#!/bin/sh\nexec 0<&-\n: > {}\nsleep 0.7\nexit 0\n",
            ready.display()
        ),
    );
    let tb = start_talkback(
        &CancelToken::new(),
        &p.display().to_string(),
        &sess("rtp://h:1"),
    )
    .unwrap();
    let pid = tb.pid().unwrap();
    wait_for("stdin closed", || ready.exists()).await;
    tb.input().send(vec![1u8; 64]).await.unwrap();
    tokio::time::timeout(Duration::from_secs(4), tb.done().cancelled())
        .await
        .expect("done");
    assert_eq!(tb.err().as_deref(), Some("ffmpeg exited: <nil>"));
    assert!(gone(pid).await);

    // stdin closed and the process stays up: the write fails, the grace passes, the session lives
    std::fs::remove_file(&ready).unwrap();
    write_script(
        &p,
        &format!(
            "#!/bin/sh\nexec 0<&-\n: > {}\nexec sleep 30\n",
            ready.display()
        ),
    );
    let tb = start_talkback(
        &CancelToken::new(),
        &p.display().to_string(),
        &sess("rtp://h:1"),
    )
    .unwrap();
    let pid = tb.pid().unwrap();
    wait_for("stdin closed", || ready.exists()).await;
    tb.input().send(vec![1u8; 64]).await.unwrap();
    tokio::time::sleep(Duration::from_millis(1500)).await;
    assert!(
        !tb.done().is_cancelled(),
        "a failed write ended the session"
    );
    assert_eq!(tb.err(), None);
    tb.close().await;
    assert!(gone(pid).await);
}

// Rust-only: an ffmpeg that stops reading its input ends the session after the write timeout (Go
// would block for ever), and the input queue never grows beyond its bound
#[tokio::test]
async fn a_stalled_ffmpeg_ends_the_session_and_the_queue_is_bounded() {
    let dir = TmpDir::new("tb");
    let p = dir.file("ffmpeg");
    write_script(&p, "#!/bin/sh\nexec sleep 30\n");
    let tb = start_talkback_with(
        &CancelToken::new(),
        &p.display().to_string(),
        &sess("rtp://h:1"),
        Duration::from_millis(300),
        TEST_KEY,
    )
    .unwrap();
    let pid = tb.pid().unwrap();
    let tx = tb.input();
    // 128 KiB in the pipe's capacity plus the queue: the sender sees a full queue, never waits
    let mut full = 0;
    let t0 = std::time::Instant::now();
    for _ in 0..400 {
        if tx.try_send(vec![0u8; 8192]).is_err() {
            full += 1;
        }
        tokio::task::yield_now().await;
    }
    assert!(t0.elapsed() < Duration::from_secs(2), "the producer waited");
    assert!(full > 0, "an unread talkback accepted 400 frames of 8 KiB");
    tokio::time::timeout(Duration::from_secs(3), tb.done().cancelled())
        .await
        .expect("done");
    assert_eq!(
        tb.err().as_deref(),
        Some("ffmpeg stopped reading its input")
    );
    assert!(gone(pid).await);
}

// Rust-only: an empty frame is skipped; a frame of any even length passes through intact
#[tokio::test]
async fn empty_frames_are_skipped_and_odd_lengths_pass() {
    let dir = TmpDir::new("tb");
    let tb = start_talkback(&CancelToken::new(), &fake_ffmpeg(&dir), &sess("rtp://h:1")).unwrap();
    let tx = tb.input();
    tx.send(vec![]).await.unwrap();
    tx.send(vec![1, 2, 3]).await.unwrap();
    tx.send(vec![]).await.unwrap();
    tx.send(vec![4]).await.unwrap();
    wait_for("both frames", || dir.read("stdin") == vec![1, 2, 3, 4]).await;
    tb.close().await;
}

// Rust-only: the refusals of start_talkback, in Go's order and words, none echoing the URL
#[tokio::test]
async fn start_talkback_refusals() {
    let dir = TmpDir::new("tb");
    let ff = fake_ffmpeg(&dir);
    let ctx = CancelToken::new();
    let err = |s: TalkbackSession, ff: &str| start_talkback(&ctx, ff, &s).err().unwrap();
    assert_eq!(
        err(sess("file://h/etc/passwd"), &ff),
        "talkback sink scheme \"file\" is not allowed"
    );
    // no host: Go's first check wins
    assert_eq!(
        err(sess("file:///etc/passwd"), &ff),
        "talkback sink is not a usable URL"
    );
    assert_eq!(
        err(sess("rtp://"), &ff),
        "talkback sink is not a usable URL"
    );
    let mut s = sess("rtp://h:1");
    s.codec = "aac".into();
    assert_eq!(
        err(s, &ff),
        "talkback codec \"aac\" is not supported (only opus)"
    );
    assert!(err(sess("rtp://h:1"), "/nonexistent/ffmpeg")
        .starts_with("ffmpeg not found (installer prerequisite"));
    // not executable
    let nx = dir.file("plain");
    dir.write("plain", b"x");
    let e = err(sess("rtp://h:1"), &nx.display().to_string());
    assert!(e.ends_with("permission denied"), "{e}");
    assert!(dir.read("args").is_empty());
    // a rate of zero means 24000
    let mut s = sess("rtp://h:1");
    s.sampling_rate = 0;
    let tb = start_talkback(&ctx, &ff, &s).unwrap();
    tb.input().send(vec![1, 2]).await.unwrap();
    wait_for("args", || !dir.read("args").is_empty()).await;
    assert!(String::from_utf8_lossy(&dir.read("args")).contains("-ar\n24000\n"));
    tb.close().await;
}

// Rust-only (PARITY.md 133): the session never prints its URL
#[test]
fn the_session_debug_text_hides_the_url() {
    let s = sess("rtp://192.168.2.9:7004?token=SECRET");
    let t = format!("{s:?}");
    assert!(!t.contains("192.168.2.9") && !t.contains("SECRET"), "{t}");
    assert!(t.contains("<redacted>") && t.contains("opus"), "{t}");
}

fn procs_with(dir: &TmpDir) -> usize {
    procs_listing(dir).len()
}

fn procs_listing(dir: &TmpDir) -> Vec<String> {
    let needle = dir.path().display().to_string();
    let mut v = Vec::new();
    if let Ok(rd) = std::fs::read_dir("/proc") {
        for e in rd.flatten() {
            if let Ok(b) = std::fs::read(e.path().join("cmdline")) {
                // Only our own children count: the fake script's `cat` is a fork of the shell that
                // has the shell's command line until it execs, and survives a kill of the shell
                // for a moment (found by this test failing 1 run in 200 without the filter)
                let status = std::fs::read_to_string(e.path().join("status")).unwrap_or_default();
                let mine = status.lines().any(|l| {
                    l.strip_prefix("PPid:")
                        .is_some_and(|v| v.trim() == std::process::id().to_string())
                });
                if mine && String::from_utf8_lossy(&b).contains(&needle) {
                    let st = std::fs::read_to_string(e.path().join("status"))
                        .unwrap_or_default()
                        .lines()
                        .filter(|l| l.starts_with("State") || l.starts_with("PPid"))
                        .collect::<Vec<_>>()
                        .join(",");
                    v.push(format!(
                        "{} [{st}]: {}",
                        e.file_name().to_string_lossy(),
                        String::from_utf8_lossy(&b).replace('\0', " ")
                    ));
                }
            }
        }
    }
    v
}

// Rust-only (PARITY.md 129, 132, 149): `stop` returns only after ffmpeg is reaped
#[tokio::test]
async fn stop_returns_after_ffmpeg_is_reaped() {
    let f = FakeProtect::new().await;
    let dir = TmpDir::new("tb");
    let d = configured(&f, &fake_ffmpeg(&dir));
    d.open_relay(&CancelToken::new()).await.unwrap();
    wait_for("ffmpeg running", || procs_with(&dir) > 0).await;
    d.stop().await.unwrap();
    assert_eq!(
        procs_listing(&dir),
        Vec::<String>::new(),
        "Stop returned before ffmpeg was reaped"
    );
}

// Rust-only (PARITY.md 132): a talkback nobody can feed or close any more is not left running
#[tokio::test]
async fn dropping_the_talkback_kills_ffmpeg() {
    let dir = TmpDir::new("tb");
    let tb = start_talkback(&CancelToken::new(), &fake_ffmpeg(&dir), &sess("rtp://h:1")).unwrap();
    let pid = tb.pid().unwrap();
    assert!(alive(pid));
    drop(tb);
    assert!(gone(pid).await, "ffmpeg survived the drop of its session");
}
