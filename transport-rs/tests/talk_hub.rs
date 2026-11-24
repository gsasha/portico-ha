//! The hub side of the talk family (slice 7b): the claimed-audio bridge, `findTalkbackAnswerer` /
//! `answerViaTalkback` and the Protect and talk drivers through `join_call_to_browser_with`: the
//! Go tests `main_test.go` (`TestFindClaimedAudioMatchesTheCallID`, `TestBridge*`),
//! `two_doors_test.go` (the two claimed-audio tests) and `talkback_test.go` of
//! `cmd/portico-transport` (the Go name is in a comment on each test), and what this port adds
//! ("Rust-only"). The legs, the drivers and the console are fakes on loopback.

mod common;

use std::sync::Arc;
use std::time::Duration;

use common::talk::*;
use common::unifi::*;
use serde_json::json;
use transport_rs::cancel::CancelToken;
use transport_rs::driver::unifiprotect::api::ProtectApi;
use transport_rs::driver::unifiprotect::UnifiProtect;
use transport_rs::driver::{CallAudio, Driver, EventSink, ExternalRinger};
use transport_rs::webrtc::{
    bridge_claimed_audio, find_claimed_audio, find_talkback_answerer, join_call_to_browser_with,
    RelayRegistry,
};

fn reg(ds: Vec<Arc<HubFake>>) -> Vec<Arc<dyn Driver>> {
    ds.into_iter().map(|d| d as Arc<dyn Driver>).collect()
}

fn same(a: &Option<Arc<dyn CallAudio>>, b: &Arc<FakeCallAudio>) -> bool {
    a.as_ref()
        .is_some_and(|x| std::ptr::eq(Arc::as_ptr(x) as *const u8, Arc::as_ptr(b) as *const u8))
}

fn join(drivers: &[Arc<dyn Driver>], relays: &Arc<RelayRegistry>, call: &str, leg: Arc<FakeLeg>) {
    join_call_to_browser_with(&CancelToken::new(), drivers, relays, "127.0.0.1", call, leg);
}

// TestFindClaimedAudioMatchesTheCallID
#[test]
fn find_claimed_audio_matches_the_call_id() {
    let a = FakeCallAudio::new();
    let drivers = reg(vec![HubFake::plain(), HubFake::claimant("c1", a.clone())]);
    assert!(same(&find_claimed_audio(&drivers, "c1"), &a));
    assert!(
        find_claimed_audio(&drivers, "other").is_none(),
        "got one for a call nobody holds"
    );
    assert!(
        find_claimed_audio(&reg(vec![HubFake::plain()]), "c1").is_none(),
        "a driver that holds no audio matched"
    );
}

// TestBridgeJoinsTheTwoLegsOnlyOnceTheBrowserIsConnected
#[tokio::test]
async fn bridge_joins_the_two_legs_only_once_the_browser_is_connected() {
    let (a, leg) = (FakeCallAudio::new(), FakeLeg::new());
    bridge_claimed_audio(a.clone(), leg.clone());
    // nothing flows, and the driver is not told a client leg exists, before that
    a.push_out(vec![1, 2]).await;
    leg.out_tx.send(vec![3, 4]).await.unwrap();
    tokio::time::sleep(ms(100)).await;
    assert_eq!(a.counts().0, 0, "audio moved before the browser connected");
    assert!(leg.in_rx.lock().await.try_recv().is_err() && a.in_rx.lock().await.try_recv().is_err());
    leg.connect();
    let (a2, l2) = (a.clone(), leg.clone());
    let got_down = tokio::time::timeout(Duration::from_secs(2), async move {
        l2.in_rx.lock().await.recv().await
    })
    .await
    .expect("downlink");
    assert_eq!(got_down, Some(vec![1, 2]), "downlink");
    let got_up = tokio::time::timeout(Duration::from_secs(2), async move {
        a2.in_rx.lock().await.recv().await
    })
    .await
    .expect("uplink");
    assert_eq!(got_up, Some(vec![3, 4]), "uplink");
    assert_eq!(a.counts(), (1, 0));
    // both directions keep flowing
    a.push_out(vec![9]).await;
    let l3 = leg.clone();
    let again = tokio::time::timeout(Duration::from_secs(2), async move {
        l3.in_rx.lock().await.recv().await
    })
    .await
    .unwrap();
    assert_eq!(again, Some(vec![9]));
}

// TestBridgeBrowserGoingAwayDetachesTheDriver
#[tokio::test]
async fn bridge_browser_going_away_detaches_the_driver() {
    let (a, leg) = (FakeCallAudio::new(), FakeLeg::new());
    bridge_claimed_audio(a.clone(), leg.clone());
    leg.connect();
    leg.disconnect();
    leg.disconnect(); // a second disconnect must not double-detach into trouble
    assert!(
        a.counts().1 >= 1,
        "the driver was not told the client leg is gone"
    );
}

// TestBridgeEndsWhenTheDriverClosesTheAudio: Go counts goroutines; here the two copy tasks hold a
// clone of the leg's input and of the leg's output, and release both when they end.
#[tokio::test]
async fn bridge_ends_when_the_driver_closes_the_audio() {
    let (a, leg) = (FakeCallAudio::new(), FakeLeg::new());
    bridge_claimed_audio(a.clone(), leg.clone());
    leg.connect();
    let (in_before, out_before) = (leg.in_tx.strong_count(), Arc::strong_count(&leg.out_arc()));
    tokio::time::sleep(ms(30)).await; // both tasks are running
    assert!(
        leg.in_tx.strong_count() > 1,
        "the downlink copy is not running"
    );
    a.close_out(); // the call ended; the driver closed its side
    let l = leg.clone();
    // both copy tasks must end (the browser's output is never closed, so the uplink one has to
    // notice on its own)
    eventually(move || l.in_tx.strong_count() == 1 && Arc::strong_count(&l.out_arc()) == 2).await;
    let _ = (in_before, out_before);
}

// TestTwoDoors_ClaimedAudioIsFoundByCallIDOnTheRightDoor
#[test]
fn two_doors_claimed_audio_is_found_by_call_id_on_the_right_door() {
    let (ga, ea) = (FakeCallAudio::new(), FakeCallAudio::new());
    let drivers = reg(vec![
        HubFake::claimant("gate-1", ga.clone()),
        HubFake::claimant("door-1", ea.clone()),
    ]);
    for _ in 0..20 {
        assert!(same(&find_claimed_audio(&drivers, "gate-1"), &ga), "gate-1");
        assert!(same(&find_claimed_audio(&drivers, "door-1"), &ea), "door-1");
        assert!(find_claimed_audio(&drivers, "ghost").is_none(), "ghost");
    }
}

// TestTwoDoors_TwoClaimedCallsAreBridgedIndependently
#[tokio::test]
async fn two_doors_two_claimed_calls_are_bridged_independently() {
    let (ga, da) = (FakeCallAudio::new(), FakeCallAudio::new());
    let (gl, dl) = (FakeLeg::new(), FakeLeg::new());
    bridge_claimed_audio(ga.clone(), gl.clone());
    bridge_claimed_audio(da.clone(), dl.clone());
    gl.connect();
    assert_eq!(
        ga.counts().0,
        1,
        "gate attached {} times, want 1",
        ga.counts().0
    );
    assert_eq!(
        da.counts().0,
        0,
        "the entrance's audio attached when only the gate's leg connected"
    );
    dl.connect();
    // audio from each call's device reaches that call's browser leg and no other
    ga.push_out(vec![1]).await;
    da.push_out(vec![2]).await;
    let heard = |l: Arc<FakeLeg>| async move {
        tokio::time::timeout(Duration::from_secs(2), async move {
            l.in_rx.lock().await.recv().await
        })
        .await
        .unwrap()
    };
    assert_eq!(heard(gl.clone()).await, Some(vec![1]), "the gate's browser");
    assert_eq!(
        heard(dl.clone()).await,
        Some(vec![2]),
        "the entrance's browser"
    );
    // the gate's browser leaves: only the gate's driver is detached
    gl.disconnect();
    let g = ga.clone();
    eventually(move || g.counts().1 == 1).await;
    assert_eq!(
        da.counts(),
        (1, 0),
        "entrance attach/detach after the gate's leg left, want 1 and 0"
    );
}

// ---- talkback_test.go (cmd/portico-transport) -------------------------------------------------

// TestFindTalkbackAnswererMatchesOnlyALiveRingByID
#[test]
fn find_talkback_answerer_matches_only_a_live_ring_by_id() {
    let r = HubFake::ringer("unifi-1");
    let drivers = reg(vec![r.clone()]);
    assert!(
        find_talkback_answerer(&drivers, "unifi-1").is_some(),
        "the ringing call was not found"
    );
    assert!(
        find_talkback_answerer(&drivers, "unifi-2").is_none(),
        "another call id matched"
    );
    r.ringing.lock().unwrap().clear();
    assert!(
        find_talkback_answerer(&drivers, "unifi-1").is_none(),
        "an answered call still matched"
    );
}

// TestTalkbackIsOpenedOnlyOnceTheBrowserConnectsThenAudioFlows
#[tokio::test]
async fn talkback_is_opened_only_once_the_browser_connects_then_audio_flows() {
    let r = HubFake::ringer("unifi-1");
    let leg = FakeLeg::new();
    let relays = RelayRegistry::new();
    join(&reg(vec![r.clone()]), &relays, "unifi-1", leg.clone());
    tokio::time::sleep(ms(100)).await;
    assert_eq!(
        r.counts().0,
        0,
        "the talkback was opened before the browser connected"
    );
    leg.connect();
    let r2 = r.clone();
    eventually(move || r2.counts().0 == 1).await;
    leg.out_tx.send(vec![1, 2, 3, 4]).await.unwrap();
    let r3 = r.clone();
    let got = tokio::time::timeout(Duration::from_secs(2), async move {
        r3.sink.rx.lock().await.recv().await
    })
    .await
    .expect("browser audio never reached the talkback");
    assert_eq!(got, Some(vec![1, 2, 3, 4]));
    let rl = relays.clone();
    eventually(move || rl.len() == 1).await;
    leg.disconnect();
    let r4 = r.clone();
    eventually(move || r4.counts().1 == 1).await;
    // the talkback is closed with the relay (on a task of its own)
    let r5 = r.clone();
    eventually(move || r5.sink.closed.load(std::sync::atomic::Ordering::SeqCst)).await;
    let rl = relays.clone();
    eventually(move || rl.is_empty()).await;
}

// TestABrowserThatNeverConnectedLeavesTheRingAnswerable
#[tokio::test]
async fn a_browser_that_never_connected_leaves_the_ring_answerable() {
    let r = HubFake::ringer("unifi-1");
    let leg = FakeLeg::new();
    join(
        &reg(vec![r.clone()]),
        &RelayRegistry::new(),
        "unifi-1",
        leg.clone(),
    );
    leg.disconnect(); // gave up before connecting
    tokio::time::sleep(ms(100)).await;
    assert_eq!(
        r.counts(),
        (0, 0),
        "answered/hungup for a leg that never connected"
    );
}

// TestAFailedTalkbackOpenIsNotRegisteredAsAnActiveRelay
#[tokio::test]
async fn a_failed_talkback_open_is_not_registered_as_an_active_relay() {
    let r = HubFake::ringer("unifi-1");
    *r.fail_open.lock().unwrap() = Some("console said no".into());
    let leg = FakeLeg::new();
    let relays = RelayRegistry::new();
    join(&reg(vec![r.clone()]), &relays, "unifi-1", leg.clone());
    leg.connect();
    tokio::time::sleep(ms(200)).await;
    assert_eq!(
        relays.len(),
        0,
        "a failed open was registered as an active relay"
    );
}

// TestTheTalkbackIsNotChosenForAnotherCallsOffer
#[tokio::test]
async fn the_talkback_is_not_chosen_for_another_calls_offer() {
    let r = HubFake::ringer("unifi-1");
    let leg = FakeLeg::new();
    join(
        &reg(vec![r.clone()]),
        &RelayRegistry::new(),
        "someone-else",
        leg.clone(),
    );
    leg.connect();
    tokio::time::sleep(ms(100)).await;
    assert_eq!(
        r.counts().0,
        0,
        "an offer for a different call answered this ring"
    );
}

// TestClaimedAudioStillWinsOverATalkbackRing
#[tokio::test]
async fn claimed_audio_still_wins_over_a_talkback_ring() {
    let r = HubFake::ringer("c1");
    let a = FakeCallAudio::new();
    let cl = HubFake::claimant("c1", a.clone());
    let leg = FakeLeg::new();
    join(
        &reg(vec![cl, r.clone()]),
        &RelayRegistry::new(),
        "c1",
        leg.clone(),
    );
    leg.connect();
    let a2 = a.clone();
    eventually(move || a2.counts().0 == 1).await;
    assert_eq!(
        r.counts().0,
        0,
        "the talkback ring was answered although a driver already held the call"
    );
}

// ---- Rust-only ---------------------------------------------------------------------------------

// Rust-only: a browser that leaves WHILE the talkback is opening: the driver hears the hang-up
// (the call is taken, not ringing), and when the open then completes the sink is closed at once
// and no dead relay stays in the registry (Go's `put` after the disconnect would keep it until
// the call's end)
#[tokio::test]
async fn a_browser_that_leaves_during_the_open_leaves_no_dead_relay() {
    let r = HubFake::ringer("unifi-1");
    let gate = Arc::new(tokio::sync::Notify::new());
    *r.gate.lock().unwrap() = Some(gate.clone());
    let leg = FakeLeg::new();
    let relays = RelayRegistry::new();
    join(&reg(vec![r.clone()]), &relays, "unifi-1", leg.clone());
    leg.connect();
    tokio::time::sleep(ms(50)).await; // the open is waiting at its gate
    leg.disconnect();
    let r2 = r.clone();
    eventually(move || r2.counts().1 == 1).await;
    gate.notify_one(); // the open completes after the browser is gone
    let r3 = r.clone();
    eventually(move || r3.counts().0 == 1).await;
    let r4 = r.clone();
    eventually(move || r4.sink.closed.load(std::sync::atomic::Ordering::SeqCst)).await;
    tokio::time::sleep(ms(100)).await;
    assert_eq!(relays.len(), 0, "a dead relay stayed in the registry");
}

// Rust-only: the real unifi_protect driver with a real ring, a real (fake) ffmpeg and the real
// hub join: nothing opens before the browser connects; the browser's audio reaches ffmpeg; the
// browser leaving hangs the call up, reaps the process and empties the registry.
#[tokio::test]
async fn a_protect_ring_through_the_hub_to_a_real_talkback() {
    let con = FakeConsole::new(false).await;
    let api = FakeProtect::new().await;
    let dir = TmpDir::new("hub");
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
    d.set_api_for_test(ProtectApi::new("http", &api.host(), TEST_KEY, true));
    let d = Arc::new(d);
    let sink = RecSink::new();
    d.start(&CancelToken::new(), sink.clone() as Arc<dyn EventSink>)
        .await
        .unwrap();
    con.last_conn().await;
    con.send(r#"{"type":"add","item":{"id":"n1","modelKey":"event","type":"ring","start":1700000000000,"device":"cam1"}}"#).await;
    wait_for("ringing", || sink.has("ringing unifi-n1")).await;

    let drivers: Vec<Arc<dyn Driver>> = vec![d.clone()];
    assert!(find_talkback_answerer(&drivers, "unifi-n1").is_some());
    let leg = FakeLeg::new();
    let relays = RelayRegistry::new();
    join(&drivers, &relays, "unifi-n1", leg.clone());
    tokio::time::sleep(ms(150)).await;
    assert!(
        api.reqs().is_empty(),
        "the console was touched before the browser connected"
    );
    leg.connect();
    wait_for("a relay", || relays.len() == 1).await;
    assert!(sink.has("state unifi-n1 active"));
    leg.out_tx.send(vec![7, 7, 7, 7]).await.unwrap();
    wait_for("audio at ffmpeg", || dir.read("stdin") == vec![7, 7, 7, 7]).await;
    leg.disconnect();
    wait_for("call ended", || sink.has("state unifi-n1 ended")).await;
    wait_for("registry empty", || relays.is_empty()).await;
    wait_for("talkback released", || !d.has_talkback()).await;
    d.stop().await.unwrap();
}

// Rust-only: the talk driver through the hub join (ConnectAnswerer): nothing opens before the
// browser connects, the audio goes both ways, the browser leaving hangs up
#[tokio::test]
async fn a_talk_ring_through_the_hub_joins_the_audio_both_ways() {
    let (d, s, op, m) = new_talk(Default::default()).await;
    d.external_ring("c1", Duration::from_secs(60)).unwrap();
    let drivers: Vec<Arc<dyn Driver>> = vec![d.clone()];
    let leg = FakeLeg::new();
    join_call_to_browser_with(
        &CancelToken::new(),
        &drivers,
        &RelayRegistry::new(),
        "",
        "c1",
        leg.clone(),
    );
    tokio::time::sleep(ms(100)).await;
    assert_eq!(op.opens(), 0, "opened before the browser connected");
    leg.connect();
    let s2 = s.clone();
    eventually(move || s2.got().ends_with("c1:active")).await;
    assert_eq!(m.attached.load(std::sync::atomic::Ordering::SeqCst), 1);
    m.out_tx.send(vec![5, 6]).await.unwrap();
    let l = leg.clone();
    let down = tokio::time::timeout(Duration::from_secs(2), async move {
        l.in_rx.lock().await.recv().await
    })
    .await
    .unwrap();
    assert_eq!(down, Some(vec![5, 6]));
    leg.out_tx.send(vec![8, 8]).await.unwrap();
    let m2 = m.clone();
    let up = tokio::time::timeout(Duration::from_secs(2), async move {
        m2.in_rx.lock().await.recv().await
    })
    .await
    .unwrap();
    assert_eq!(up, Some(vec![8, 8]));
    leg.disconnect();
    let s3 = s.clone();
    eventually(move || s3.got().ends_with("c1:ended")).await;
    assert!(m.closed() > 0);
    assert!(
        s.heard().contains("|c1|ended|hung up (detach)"),
        "{}",
        s.heard()
    );
}
