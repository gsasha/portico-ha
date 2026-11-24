//! A panic in any task of the talk media (the downlink reader, the uplink writer, the microphone
//! pump, the teardown) costs that call and nothing else: both ffmpegs are killed and reaped, the
//! media's `done` and `closed` are set (by drop guards), the call ends and the door is free for the
//! next. The panic is injected through a process-wide seam (`panic_next_for_test`), so this binary
//! has ONE test that runs the four cases one after the other. Rust-only (PARITY.md 131).

mod common;

use std::time::Duration;

use common::talk::*;
use common::unifi::wait_for;
use transport_rs::cancel::CancelToken;
use transport_rs::driver::unifiprotect::ffmpeg::{
    panic_next_for_test, PANIC_DOWNLINK, PANIC_NEXT, PANIC_PUMP, PANIC_TEARDOWN, PANIC_UPLINK,
};

/// Has the armed injection fired? The seam clears its bit when the place is reached and panics,
/// so a bit that stays set means nothing panicked and the assertions that follow hold for a clean
/// run too (R1-43).
fn fired(bit: u32) -> bool {
    PANIC_NEXT.load(std::sync::atomic::Ordering::SeqCst) & bit == 0
}
use transport_rs::driver::{CallHangupper, ConnectAnswerer, Driver, ExternalRinger};

fn alive(pid: u32) -> bool {
    std::path::Path::new(&format!("/proc/{pid}")).exists()
}

#[tokio::test]
async fn a_panic_in_a_media_task_ends_the_call_and_reaps_both_ffmpegs() {
    for (bit, name) in [
        (PANIC_DOWNLINK, "the downlink reader"),
        (PANIC_UPLINK, "the uplink writer"),
        (PANIC_PUMP, "the microphone pump"),
        (PANIC_TEARDOWN, "the teardown"),
    ] {
        let r = Rig::new().await;
        r.dir.write("down.pcm", &pcm(1280, 1));
        r.d.external_ring("c1", Duration::from_secs(60)).unwrap();
        let a =
            r.d.answer_with_audio(&CancelToken::new(), "c1")
                .await
                .unwrap();
        a.attach();
        let st = r.d.stats_for_test().unwrap();
        let (up, down) = (
            st.up_pid.load(std::sync::atomic::Ordering::SeqCst),
            st.down_pid.load(std::sync::atomic::Ordering::SeqCst),
        );
        panic_next_for_test(bit);
        // feed whatever the armed place needs: a downlink frame, an uplink frame
        r.release();
        a.input().send(pcm(640, 1)).await.unwrap();
        if bit == PANIC_TEARDOWN {
            // the teardown runs on a close
            tokio::time::sleep(Duration::from_millis(100)).await;
            let _ = r.d.hangup_call("c1");
        }
        r.wait_ended("c1").await;
        wait_for(&format!("{name}: the injected panic fired"), move || {
            fired(bit)
        })
        .await;
        let (u2, d2) = (up, down);
        wait_for(&format!("{name}: both ffmpegs reaped"), move || {
            !alive(u2) && !alive(d2)
        })
        .await;
        let s2 = r.sink.clone();
        wait_for(&format!("{name}: the call ended"), move || {
            s2.got().ends_with("c1:ended")
        })
        .await;
        assert!(!r.d.health().session_up, "{name}");
        // the media's `closed` resolved: stop returns promptly, and the door is free again
        tokio::time::timeout(Duration::from_secs(3), r.d.stop())
            .await
            .unwrap_or_else(|_| panic!("{name}: stop hung"))
            .unwrap();
        // the output was closed for the reader of the audio
        let out = a.output();
        let end = tokio::time::timeout(Duration::from_secs(3), async move {
            let mut g = out.lock().await;
            while g.recv().await.is_some() {}
        })
        .await;
        assert!(end.is_ok(), "{name}: the audio output was never closed");
    }
    // the door works after a panic
    let r = Rig::new().await;
    r.d.external_ring("c2", Duration::from_secs(60)).unwrap();
    r.d.answer_with_audio(&CancelToken::new(), "c2")
        .await
        .unwrap();
    r.d.stop().await.unwrap();

    // An ffmpeg that ignores the end of its stdin (so the closed pipe does not stop it) is still
    // killed and reaped when the task that owns it panics: that is `kill_on_drop` (mutation M20a).
    use transport_rs::driver::unifiprotect::api::TalkbackSession;
    use transport_rs::driver::unifiprotect::talkback::start_talkback;
    let dir = TmpDir::new("pk");
    let script = dir.file("ffmpeg");
    write_script(&script, "#!/bin/sh\nexec sleep 30\n");
    let tb = start_talkback(
        &CancelToken::new(),
        &script.display().to_string(),
        &TalkbackSession {
            url: "rtp://h:1".into(),
            codec: "opus".into(),
            sampling_rate: 24000,
            bits_per_sample: 16,
        },
    )
    .unwrap();
    let pid = tb.pid().unwrap();
    panic_next_for_test(PANIC_UPLINK);
    tb.input().send(vec![1, 2]).await.unwrap();
    wait_for("the panicked talkback's ffmpeg is gone", move || {
        !alive(pid)
    })
    .await;
    assert!(fired(PANIC_UPLINK), "the uplink panic never fired");
    assert!(tb.done().is_cancelled());
}
