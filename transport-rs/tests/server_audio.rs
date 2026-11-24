//! Ports of the real-server halves of `audio_test.go` (W3, DEV-518): the answer path reaches the
//! server's in-process audio. Loopback only: a fake station on 127.0.0.x, the server on
//! 127.0.0.1. The Go test name is in a comment on each test.

mod common;

use std::sync::Arc;
use std::time::Duration;

use common::*;
use transport_rs::cancel::CancelToken;
use transport_rs::driver::gatesipserver::media::alaw_encode;
use transport_rs::driver::{
    AudioClaimer, CallAudio, CallHangupper, ConnectAnswerer, Driver, MediaTarget, Sentinel,
};

async fn read_rtp(h: &Harness, within: Duration) -> Vec<u8> {
    h.st.read_rtp(within)
        .await
        .map(|(_, p)| p)
        .unwrap_or_default()
}

async fn next_out(a: &Arc<dyn CallAudio>, within: Duration) -> Vec<u8> {
    let out = a.output();
    let mut rx = out.lock().await;
    match tokio::time::timeout(within, rx.recv()).await {
        Ok(Some(f)) => f,
        Ok(None) => panic!("Out closed"),
        Err(_) => panic!("no frame on Out"),
    }
}

async fn out_closed_within(a: &Arc<dyn CallAudio>, within: Duration) -> bool {
    let out = a.output();
    let mut rx = out.lock().await;
    // a frame after the end, or no close in time, is a failure
    matches!(tokio::time::timeout(within, rx.recv()).await, Ok(None))
}

async fn ring_and_answer(h: &Harness, id: &str, sdp: &str) -> Arc<dyn CallAudio> {
    h.st.invite(id, 1, &h.st.sdp(sdp)).await;
    h.st.collect(ms(100)).await;
    h.d.answer_with_audio(&CancelToken::new(), id)
        .await
        .unwrap()
}

// TestW3NothingIsAnsweredUntilAnswerWithAudio
#[tokio::test]
async fn w3_nothing_is_answered_until_answer_with_audio() {
    let h = Harness::with(Opts::ring(5000)).await;
    register_station(&h).await;
    h.st.invite("call-W3A", 1, &h.st.sdp("wide")).await;
    let got = h.st.collect(ms(400)).await;
    assert!(
        !has(&got, "200"),
        "a 200 went out with no answerer: {:?}",
        codes(&got)
    );
    assert!(
        has(&got, "180")
            && ConnectAnswerer::ringing_call(&*h.d, "call-W3A")
            && !ConnectAnswerer::ringing_call(&*h.d, "other"),
        "ring state wrong: {:?}",
        codes(&got)
    );
    assert!(
        h.d.claimed_audio("call-W3A").is_none(),
        "audio claimed on a ringing call"
    );
    let t = CancelToken::new();
    let e =
        h.d.answer_with_audio(&t, "other")
            .await
            .err()
            .expect("wrong id answered");
    assert!(e.is(Sentinel::NoSuchCall), "wrong id: {e}");
    assert!(
        !has(&h.st.collect(ms(100)).await, "200"),
        "a refused answer still sent 200"
    );
    h.sink
        .wait_events(&[
            &format!("caps:{}:false", h.device_id()),
            &format!("ringing:call-W3A@{}", h.device_id()),
        ])
        .await;
    h.d.answer_with_audio(&t, "call-W3A").await.unwrap();
    let r = h.st.collect(ms(300)).await;
    let m = find(&r, "200");
    assert!(
        m.as_ref().is_some_and(|m| m.body().contains("PCM/16000")),
        "no 200 with SDP after the answer: {:?}",
        codes(&r)
    );
    assert!(
        !ConnectAnswerer::ringing_call(&*h.d, "call-W3A"),
        "still ringing after the answer"
    );
}

// TestW3TwoWayPCMWideband
#[tokio::test]
async fn w3_two_way_pcm_wideband() {
    let h = Harness::new().await;
    register_station(&h).await;
    let a = ring_and_answer(&h, "call-W3B", "wide").await;
    h.st.ack("call-W3B").await;
    a.attach();
    h.sink
        .wait_events(&[
            &format!("caps:{}:false", h.device_id()),
            &format!("ringing:call-W3B@{}", h.device_id()),
            "answered:call-W3B",
            "active:call-W3B",
        ])
        .await;

    // silence to the station until a client sends: 640 zero bytes per 20 ms frame
    let sil = read_rtp(&h, ms(1000)).await;
    assert!(
        sil.len() == 640 && sil.iter().all(|&b| b == 0),
        "not silence: {} bytes",
        sil.len()
    );

    // station -> client: 1280-byte frames of 16 kHz L16, with the observer's x4 gain
    let pcm = tone(320, 16000.0, 440.0, 2000.0); // 640 bytes: one RTP payload
    for i in 0..2u16 {
        // two payloads make one 1280-byte frame
        h.st.rtp
            .send_to(
                &rtp_packet(96, i, u32::from(i) * 320, &pcm[..640]),
                h.rtp_addr(),
            )
            .await
            .unwrap();
    }
    let f = next_out(&a, ms(2000)).await;
    assert_eq!(f.len(), 1280, "frame size");
    assert!(
        goertzel(&f, 16000.0, 440.0) >= 50.0 * goertzel(&f, 16000.0, 1500.0),
        "the downlink is not the 440 Hz tone"
    );

    // client -> station: 1280 bytes in, two 640-byte payloads out, the tone intact
    let up = tone(640, 16000.0, 1000.0, 8000.0);
    a.input().send(up).await.unwrap();
    let mut got = Vec::new();
    let end = std::time::Instant::now() + Duration::from_secs(2);
    while std::time::Instant::now() < end && got.len() < 640 {
        let p = read_rtp(&h, ms(200)).await;
        if p.len() == 640 && p.iter().any(|&b| b != 0) {
            got.extend_from_slice(&p);
        }
    }
    assert!(
        got.len() >= 640
            && goertzel(&got[..640], 16000.0, 1000.0)
                >= 50.0 * goertzel(&got[..640], 16000.0, 440.0),
        "the uplink tone did not reach the station"
    );
}

// TestW3TwoWayPCMNarrowband
#[tokio::test]
async fn w3_two_way_pcm_narrowband() {
    let h = Harness::new().await;
    register_station(&h).await;
    let a = ring_and_answer(&h, "call-W3N", "narrow").await;
    a.attach();
    let sil = read_rtp(&h, ms(1000)).await;
    assert!(
        sil.len() == 160 && sil[0] == 0xD5,
        "A-law silence expected, got {} bytes",
        sil.len()
    );
    // 8 kHz A-law down: 2 x 160 samples -> one 1280-byte 16 kHz frame
    let enc = alaw_encode(&tone(320, 8000.0, 440.0, 2000.0));
    for i in 0..2usize {
        h.st.rtp
            .send_to(
                &rtp_packet(8, i as u16, (i * 160) as u32, &enc[i * 160..(i + 1) * 160]),
                h.rtp_addr(),
            )
            .await
            .unwrap();
    }
    let f = next_out(&a, ms(2000)).await;
    assert!(
        f.len() == 1280 && goertzel(&f, 16000.0, 440.0) >= 20.0 * goertzel(&f, 16000.0, 2500.0),
        "narrowband downlink wrong ({} bytes)",
        f.len()
    );
    a.input()
        .send(tone(640, 16000.0, 1000.0, 8000.0))
        .await
        .unwrap();
    let mut ok = false;
    let end = std::time::Instant::now() + Duration::from_secs(2);
    while std::time::Instant::now() < end && !ok {
        let p = read_rtp(&h, ms(200)).await;
        ok = p.len() == 160 && p[0] != 0xD5;
    }
    assert!(ok, "no A-law uplink payload");
}

// TestW3SecondAnswererIsRefusedAndFirstKeepsTheCall
#[tokio::test]
async fn w3_second_answerer_is_refused_and_first_keeps_the_call() {
    let h = Harness::new().await;
    register_station(&h).await;
    let a = ring_and_answer(&h, "call-W3C", "wide").await;
    a.attach();
    let e =
        h.d.answer_with_audio(&CancelToken::new(), "call-W3C")
            .await
            .err()
            .expect("second answerer succeeded");
    assert!(e.is(Sentinel::AlreadyAnswered), "second answerer: {e}");
    assert!(
        h.d.claimed_audio("call-W3C").is_none(),
        "the held audio was handed out again"
    );
    let r = h.st.collect(ms(300)).await;
    assert!(
        !has(&r, "BYE") && !has(&r, "486"),
        "the refused answerer disturbed the call: {:?}",
        codes(&r)
    );
    assert_eq!(h.s.snapshot().answered, 1, "answered more than once");
    a.input()
        .send(tone(640, 16000.0, 1000.0, 8000.0))
        .await
        .unwrap(); // the first still owns it
}

// TestW3DetachHangsUpAtOnce
#[tokio::test]
async fn w3_detach_hangs_up_at_once() {
    let h = Harness::with(Opts::orphan(5000)).await;
    register_station(&h).await;
    let a = ring_and_answer(&h, "call-W3D", "wide").await;
    a.attach();
    let t0 = std::time::Instant::now();
    a.detach();
    a.detach(); // idempotent
    let r = h.st.collect(ms(500)).await;
    assert!(
        has(&r, "BYE") && t0.elapsed() <= Duration::from_secs(2),
        "no prompt BYE: {:?}",
        codes(&r)
    );
    h.sink
        .wait_events(&[
            &format!("caps:{}:false", h.device_id()),
            &format!("ringing:call-W3D@{}", h.device_id()),
            "answered:call-W3D",
            "ended:call-W3D",
        ])
        .await;
    assert!(
        out_closed_within(&a, ms(1000)).await,
        "Out not closed after hang-up"
    );
    let e = h.d.hangup_call("call-W3D").unwrap_err();
    assert!(e.is(Sentinel::NoSuchCall), "hangup of a finished call: {e}");
}

// TestW3StationByeClosesOut
#[tokio::test]
async fn w3_station_bye_closes_out() {
    let h = Harness::new().await;
    register_station(&h).await;
    let a = ring_and_answer(&h, "call-W3E", "wide").await;
    a.attach();
    h.st.bye("call-W3E").await;
    assert!(
        out_closed_within(&a, ms(2000)).await,
        "Out not closed by the station's BYE"
    );
}

// TestW3HangupCall
#[tokio::test]
async fn w3_hangup_call() {
    let h = Harness::with(Opts::ring(5000)).await;
    register_station(&h).await;
    h.st.invite("call-W3F", 1, &h.st.sdp("wide")).await;
    h.st.collect(ms(100)).await;
    let e = h.d.hangup_call("call-W3F").unwrap_err();
    assert!(
        e.is(Sentinel::NoSuchCall),
        "a ringing call is not ours to BYE: {e}"
    );
    assert!(
        !has(&h.st.collect(ms(200)).await, "BYE"),
        "BYE sent for a ringing call"
    );
    let a =
        h.d.answer_with_audio(&CancelToken::new(), "call-W3F")
            .await
            .unwrap();
    a.attach();
    let e = h.d.hangup_call("other").unwrap_err();
    assert!(e.is(Sentinel::NoSuchCall), "other id: {e}");
    h.d.hangup_call("call-W3F").unwrap();
    assert!(has(&h.st.collect(ms(500)).await, "BYE"), "no BYE");
}

// TestW3CancelStillGets487AndLeavesNothingToAnswer
#[tokio::test]
async fn w3_cancel_still_gets_487_and_leaves_nothing_to_answer() {
    let h = Harness::with(Opts::ring(5000)).await;
    register_station(&h).await;
    h.st.invite("call-W3G", 1, &h.st.sdp("wide")).await;
    h.st.collect(ms(100)).await;
    h.st.cancel("call-W3G").await;
    let r = h.st.collect(ms(400)).await;
    assert!(
        has(&r, "487") && has(&r, "200"),
        "CANCEL: want 200 + 487, got {:?}",
        codes(&r)
    );
    assert!(
        !ConnectAnswerer::ringing_call(&*h.d, "call-W3G"),
        "cancelled call still ringing"
    );
    let e =
        h.d.answer_with_audio(&CancelToken::new(), "call-W3G")
            .await
            .err()
            .expect("answered a cancelled call");
    assert!(e.is(Sentinel::NoSuchCall), "answered a cancelled call: {e}");
    assert!(
        !has(&h.st.collect(ms(100)).await, "200"),
        "a 200 INVITE reply after the CANCEL"
    );
}

// TestW3AnswerOfALegacyAnsweredCallIsRefused, the real-server half
#[tokio::test]
async fn w3_answer_of_a_legacy_answered_call_is_refused() {
    let h = Harness::new().await;
    h.st.invite("call-W3H", 1, &h.st.sdp("wide")).await;
    h.st.collect(ms(100)).await;
    let t = CancelToken::new();
    h.d.answer(&t, MediaTarget::default()).await.unwrap();
    let e =
        h.d.answer_with_audio(&t, "call-W3H")
            .await
            .err()
            .expect("answered twice");
    assert!(e.is(Sentinel::AlreadyAnswered), "got {e}");
}

// TestW3StrictRTPSourceRuleUnchanged
#[tokio::test]
async fn w3_strict_rtp_source_rule_unchanged() {
    let h = Harness::new().await;
    register_station(&h).await;
    let a = ring_and_answer(&h, "call-W3I", "wide").await;
    a.attach();
    let other = Station::on(h.s.sip_addr(), "127.0.0.2").await; // not the host the SDP named
    for i in 0..2u16 {
        other
            .rtp
            .send_to(
                &rtp_packet(
                    96,
                    i,
                    u32::from(i) * 320,
                    &tone(320, 16000.0, 440.0, 2000.0),
                ),
                h.rtp_addr(),
            )
            .await
            .unwrap();
    }
    sleep_ms(200).await;
    let out = a.output();
    assert!(
        out.lock().await.try_recv().is_err(),
        "audio from a foreign source reached the client"
    );
    let sn = h.s.snapshot();
    assert!(sn.rtp_rejected >= 2, "foreign RTP not counted: {sn:?}");
}

// TestW3ClaimedAudioHandsOutAnUnheldAudioOnce, the real-server half
#[tokio::test]
async fn w3_claimed_audio_hands_out_an_unheld_audio_once() {
    let h = Harness::new().await;
    register_station(&h).await;
    let a = ring_and_answer(&h, "call-W3J", "wide").await;
    let held =
        h.d.held_audio()
            .expect("the driver keeps the audio it handed out");
    held.clear_attached(); // as if no leg had taken it yet (W4's dialled calls)
    let got =
        h.d.claimed_audio("call-W3J")
            .expect("unheld audio not handed out");
    assert!(
        std::ptr::addr_eq(Arc::as_ptr(&got), Arc::as_ptr(&a)),
        "a different audio was handed out"
    );
    assert!(
        h.d.claimed_audio("other").is_none(),
        "matched another call id"
    );
    got.attach();
    assert!(
        h.d.claimed_audio("call-W3J").is_none(),
        "handed out after Attach"
    );
    h.d.stop().await.unwrap();
    assert!(
        h.d.claimed_audio("call-W3J").is_none(),
        "handed out after Stop"
    );
}
