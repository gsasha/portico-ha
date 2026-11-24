//! Ports of the 32 Go tests of `internal/house/house_test.go` (the Go name in a comment on
//! each) and the behaviours the port adds ("Rust-only": the panic guards, the dropped `open`
//! future, the ordered event queue). The legs are fakes with no network, as in Go: a test pushes
//! uplink frames into `out` and reads the downlink from `in`.

mod common;

use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use common::house::*;
use tokio::time::{sleep, timeout, Instant};
use transport_rs::house::{
    BoxFut, Config, EmitFn, Event, FrameError, HouseError, HouseLeg, LegSpec, Manager, Opened,
    DEFAULT_SILENCE, FRAME_BYTES, KIND_FRAMES, KIND_WEBRTC, REASON_BRIDGE_CLOSED, REASON_ERROR,
    REASON_HANGUP, REASON_LOST, REASON_SHUTDOWN, REASON_SILENCE, REASON_WEBRTC_FAILED, SIDE_FROM,
    SIDE_TO, STATE_ATTACHED, STATE_FIRST_FRAME, STATE_LOST, STATE_SDP,
};

fn ms(n: u64) -> Duration {
    Duration::from_millis(n)
}

/// `events`.
#[derive(Default, Clone)]
struct Events(Arc<Mutex<Vec<Event>>>);

impl Events {
    fn emit_fn(&self) -> EmitFn {
        let e = self.0.clone();
        Arc::new(move |x: Event| -> BoxFut {
            e.lock().unwrap().push(x);
            Box::pin(async {})
        })
    }
    fn all(&self) -> Vec<Event> {
        self.0.lock().unwrap().clone()
    }
    fn of(&self, state: &str) -> Vec<Event> {
        self.all()
            .into_iter()
            .filter(|e| e.state == state)
            .collect()
    }
}

async fn eventually(what: &str, mut cond: impl FnMut() -> bool) {
    let end = Instant::now() + Duration::from_secs(3);
    while Instant::now() < end {
        if cond() {
            return;
        }
        sleep(ms(2)).await;
    }
    panic!("timed out waiting for {what}");
}

fn frame(b: u8) -> Vec<u8> {
    vec![b; FRAME_BYTES]
}

fn spec(side: &str, kind: &str, codec: &str, owner: &str) -> LegSpec {
    LegSpec {
        side: side.into(),
        kind: kind.into(),
        codec: codec.into(),
        sdp: String::new(),
        owner: owner.into(),
    }
}

/// `framesSpecs`
fn frames_specs(owners: &[&str]) -> Vec<LegSpec> {
    let o = |i: usize| owners.get(i).copied().unwrap_or("");
    vec![
        spec(SIDE_FROM, KIND_FRAMES, "l16", o(0)),
        spec(SIDE_TO, KIND_FRAMES, "pcmu", o(1)),
    ]
}

fn webrtc_from(sdp: &str) -> Vec<LegSpec> {
    vec![
        LegSpec {
            side: SIDE_FROM.into(),
            kind: KIND_WEBRTC.into(),
            sdp: sdp.into(),
            ..Default::default()
        },
        spec(SIDE_TO, KIND_FRAMES, "l16", ""),
    ]
}

/// `newMgr`
fn new_mgr(cfg: Config) -> (Arc<Manager>, Arc<FakeFactory>, Events) {
    let f = Arc::new(FakeFactory::default());
    let ev = Events::default();
    let m = Manager::new(cfg, Arc::new(Ff(f.clone())), ev.emit_fn());
    (m, f, ev)
}

/// `legsOf`
fn legs_of(f: &FakeFactory, res: &Opened) -> (Arc<FakeLeg>, Arc<FakeLeg>) {
    (
        f.leg(&format!("{}-from", res.session)),
        f.leg(&format!("{}-to", res.session)),
    )
}

fn cfg_silence(d: Duration) -> Config {
    Config {
        silence: d,
        ..Default::default()
    }
}

// ---- internal/house/house_test.go ----------------------------------------------------------------

// TestFramesFlowBothWaysAndInOrder
#[tokio::test(start_paused = true)]
async fn frames_flow_both_ways_and_in_order() {
    let (m, f, _) = new_mgr(Config::default());
    let res = m.open("hso-1", frames_specs(&[])).await.unwrap();
    let (from, to) = legs_of(&f, &res);
    for i in 1..=6u8 {
        from.send_up(frame(i)).await;
    }
    for i in 1..=6u8 {
        let got = to.recv_down().await;
        assert_eq!(got[0], i, "frame {i} arrived as {}", got[0]);
    }
    for i in 10..=12u8 {
        to.send_up(frame(i)).await;
    }
    for i in 10..=12u8 {
        let got = from.recv_down().await;
        assert_eq!(got[0], i, "reverse frame {i} arrived as {}", got[0]);
    }
    m.close_all().await;
}

// TestSessionAndLegIdsHaveTheDesignedShape
#[tokio::test(start_paused = true)]
async fn session_and_leg_ids_have_the_designed_shape() {
    let (m, _, _) = new_mgr(Config::default());
    let res = m.open("hso-1", frames_specs(&[])).await.unwrap();
    let hex = res.session.strip_prefix("house-").expect("prefix");
    assert!(
        hex.len() == 16
            && hex
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b)),
        "session id {:?}",
        res.session
    );
    for l in &res.legs {
        assert_eq!(l.leg_id, format!("{}-{}", res.session, l.side));
        assert_eq!(l.kind, KIND_FRAMES);
    }
    let res2 = m.open("hso-2", frames_specs(&[])).await.unwrap();
    assert_ne!(res2.session, res.session, "two sessions share an id");
    m.close_all().await;
}

// TestASessionIdCollisionIsRetried
#[tokio::test(start_paused = true)]
async fn a_session_id_collision_is_retried() {
    let ids = ["aaaa", "aaaa", "bbbb"];
    let n = Arc::new(AtomicUsize::new(0));
    let n2 = n.clone();
    let (m, _, _) = new_mgr(Config {
        new_id: Some(Arc::new(move || {
            ids[n2.fetch_add(1, Ordering::SeqCst)].to_string()
        })),
        ..Default::default()
    });
    let r1 = m.open("hso-1", frames_specs(&[])).await.unwrap();
    let r2 = m.open("hso-2", frames_specs(&[])).await.unwrap();
    assert_eq!(
        (r1.session.as_str(), r2.session.as_str()),
        ("house-aaaa", "house-bbbb")
    );
    m.close_all().await;
}

// TestWrongSizeFramesAreDroppedNotForwarded
#[tokio::test(start_paused = true)]
async fn wrong_size_frames_are_dropped_not_forwarded() {
    let (m, f, _) = new_mgr(Config::default());
    let res = m.open("hso-1", frames_specs(&[])).await.unwrap();
    let (from, to) = legs_of(&f, &res);
    from.send_up(vec![1, 2, 3]).await;
    from.send_up(frame(7)).await;
    assert_eq!(to.recv_down().await[0], 7);
    if let Some(x) = to.try_down(ms(30)).await {
        panic!("a short frame was forwarded: {} bytes", x.len());
    }
    m.close_all().await;
}

// TestAFullDestinationDropsInsteadOfBlockingAndTheCallStillEnds
#[tokio::test(start_paused = true)]
async fn a_full_destination_drops_instead_of_blocking_and_the_call_still_ends() {
    let (m, f, _) = new_mgr(Config::default());
    let res = m.open("hso-1", frames_specs(&[])).await.unwrap();
    let (from, to) = legs_of(&f, &res);
    // 8 slots in out, 8 in to.in: nobody drains to.in
    let sender = tokio::spawn({
        let from = from.clone();
        async move {
            for _ in 0..200 {
                from.send_up(frame(1)).await;
            }
        }
    });
    timeout(Duration::from_secs(2), sender)
        .await
        .expect("the forwarder blocked on a full destination")
        .unwrap();
    assert_eq!(
        to.down_len().await,
        8,
        "to.in holds {}",
        to.down_len().await
    );
    assert!(
        m.close("hso-1").await && to.is_closed(),
        "close did not end the call"
    );
}

// TestOpenRefusesBadRequestsAndBuildsNothing
#[tokio::test(start_paused = true)]
async fn open_refuses_bad_requests_and_builds_nothing() {
    let long_id = format!("hso-{}", "x".repeat(200));
    let two_from = vec![
        spec("from", "frames", "l16", ""),
        spec("from", "frames", "l16", ""),
    ];
    let cases: Vec<(&str, String, Vec<LegSpec>)> = vec![
        ("door-looking id", "gso-1".into(), frames_specs(&[])),
        ("empty id", String::new(), frames_specs(&[])),
        ("over-long id", long_id, frames_specs(&[])),
        ("one leg", "hso-1".into(), frames_specs(&[])[..1].to_vec()),
        ("three legs", "hso-1".into(), {
            let mut v = frames_specs(&[]);
            v.push(spec("to", "frames", "l16", ""));
            v
        }),
        ("same side twice", "hso-1".into(), two_from),
        (
            "unknown side",
            "hso-1".into(),
            vec![
                spec("from", "frames", "l16", ""),
                spec("left", "frames", "l16", ""),
            ],
        ),
        (
            "unknown kind",
            "hso-1".into(),
            vec![spec("from", "frames", "l16", ""), spec("to", "udp", "", "")],
        ),
        (
            "unknown codec",
            "hso-1".into(),
            vec![
                spec("from", "frames", "l16", ""),
                spec("to", "frames", "opus", ""),
            ],
        ),
        (
            "webrtc no offer",
            "hso-1".into(),
            vec![
                spec("from", "frames", "l16", ""),
                spec("to", "webrtc", "", ""),
            ],
        ),
        (
            "owner too long",
            "hso-1".into(),
            frames_specs(&[&"o".repeat(65)]),
        ),
    ];
    for (name, id, specs) in cases {
        let (m, f, ev) = new_mgr(Config::default());
        let err = m.open(&id, specs).await.unwrap_err();
        assert!(matches!(err, HouseError::BadParams(_)), "{name}: {err:?}");
        sleep(ms(5)).await;
        assert!(
            m.count() == 0 && f.built() == 0 && ev.all().is_empty(),
            "{name}: a refused open left something"
        );
    }
}

// TestTheFifthBridgeIsRefusedAndASlotFreesWhenACallEnds
#[tokio::test(start_paused = true)]
async fn the_fifth_bridge_is_refused_and_a_slot_frees_when_a_call_ends() {
    let (m, f, _) = new_mgr(Config::default());
    for i in 1..=4 {
        m.open(&format!("hso-{i}"), frames_specs(&[]))
            .await
            .unwrap();
    }
    let before = f.built();
    let err = m.open("hso-5", frames_specs(&[])).await.unwrap_err();
    assert!(matches!(err, HouseError::TooMany(_)), "fifth open: {err:?}");
    assert_eq!(f.built(), before, "the refused fifth open built legs");
    m.close("hso-2").await;
    m.open("hso-5", frames_specs(&[])).await.unwrap();
    assert_eq!(m.count(), 4);
    m.close_all().await;
}

// TestConcurrentOpensNeverOvershootTheCap
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn concurrent_opens_never_overshoot_the_cap() {
    let (m, _, _) = new_mgr(Config::default());
    let mut hs = Vec::new();
    for i in 0..40 {
        let m = m.clone();
        hs.push(tokio::spawn(async move {
            m.open(&format!("hso-{i}"), frames_specs(&[])).await
        }));
    }
    let (mut ok, mut refused) = (0, 0);
    for h in hs {
        match h.await.unwrap() {
            Ok(_) => ok += 1,
            Err(HouseError::TooMany(_)) => refused += 1,
            Err(e) => panic!("open: {e:?}"),
        }
    }
    assert_eq!((ok, refused, m.count()), (4, 36, 4));
    m.close_all().await;
}

// TestOnePhoneCannotHoldMoreThanTwoLegs
#[tokio::test(start_paused = true)]
async fn one_phone_cannot_hold_more_than_two_legs() {
    let (m, _, _) = new_mgr(Config::default());
    m.open("hso-1", frames_specs(&["alice", "bob"]))
        .await
        .unwrap();
    m.open("hso-2", frames_specs(&["alice", "carol"]))
        .await
        .unwrap();
    let e = m
        .open("hso-3", frames_specs(&["alice", "dave"]))
        .await
        .unwrap_err();
    assert!(
        matches!(e, HouseError::TooMany(_)),
        "alice's third leg: {e:?}"
    );
    m.open("hso-3", frames_specs(&["", "dave"]))
        .await
        .expect("a call without alice");
    m.close("hso-1").await;
    m.open("hso-4", frames_specs(&["alice", ""]))
        .await
        .expect("alice after a call ended");
    // one call naming the same phone on both legs counts twice
    m.open("hso-5", frames_specs(&["erin", "erin"]))
        .await
        .unwrap();
    let e = m
        .open("hso-6", frames_specs(&["erin", ""]))
        .await
        .unwrap_err();
    assert!(
        matches!(e, HouseError::TooMany(_)),
        "erin over the cap: {e:?}"
    );
    m.close_all().await;
}

// R2-31: the legs THIS call adds count against the cap: a phone holding one leg cannot open a
// call naming it on both sides (it would hold three)
#[tokio::test(start_paused = true)]
async fn a_call_naming_a_phone_twice_counts_both_legs_against_the_cap() {
    let (m, _, _) = new_mgr(Config::default());
    m.open("hso-7", frames_specs(&["zed", ""])).await.unwrap();
    let e = m
        .open("hso-8", frames_specs(&["zed", "zed"]))
        .await
        .unwrap_err();
    assert_eq!(
        e.message(),
        "house: too many house calls: that phone already holds 1 legs"
    );
    m.close_all().await;
}

// TestADuplicateCallIdIsRefused
#[tokio::test(start_paused = true)]
async fn a_duplicate_call_id_is_refused() {
    let (m, f, _) = new_mgr(Config::default());
    m.open("hso-1", frames_specs(&[])).await.unwrap();
    let n = f.built();
    assert_eq!(
        m.open("hso-1", frames_specs(&[])).await.unwrap_err(),
        HouseError::AlreadyOpen
    );
    assert!(
        f.built() == n && m.count() == 1,
        "the duplicate built legs or took a slot"
    );
    m.close_all().await;
}

// TestCloseEndsBothLegsOnceAndSendsNoEvents
#[tokio::test(start_paused = true)]
async fn close_ends_both_legs_once_and_sends_no_events() {
    let (m, f, ev) = new_mgr(Config::default());
    let res = m.open("hso-1", frames_specs(&["alice"])).await.unwrap();
    let (from, to) = legs_of(&f, &res);
    assert!(m.close("hso-1").await, "close said there was no call");
    for l in [&from, &to] {
        assert!(
            l.is_closed() && l.reason() == REASON_HANGUP,
            "leg {} closed={} reason={:?}",
            l.id,
            l.is_closed(),
            l.reason()
        );
    }
    assert!(
        !m.close("hso-1").await,
        "second close claims there was a call"
    );
    sleep(ms(20)).await;
    assert!(
        ev.all().is_empty(),
        "events after a requested close: {:?}",
        ev.all()
    );
    assert_eq!(m.table_entries(), 0, "table entries left");
    assert_eq!(
        m.frame(&format!("{}-from", res.session), &frame(1)),
        Err(FrameError::UnknownLeg)
    );
}

// TestALegGoingAwayEndsTheCallAndTellsBothLegs
#[tokio::test(start_paused = true)]
async fn a_leg_going_away_ends_the_call_and_tells_both_legs() {
    let (m, f, ev) = new_mgr(Config::default());
    let res = m.open("hso-1", frames_specs(&[])).await.unwrap();
    let (from, to) = legs_of(&f, &res);
    // the hub's 5 s idle rule, a hangup frame, a socket drop: the leg is simply gone
    drop(HouseLeg::close(&*to, "idle"));
    eventually("both legs lost", || ev.of(STATE_LOST).len() == 2).await;
    eventually("the other leg closed", || from.is_closed()).await;
    let mut lost = HashMap::new();
    for e in ev.of(STATE_LOST) {
        assert_eq!(e.call_id, "hso-1");
        lost.insert(e.side.clone(), e.reason.clone());
    }
    assert_eq!(lost[SIDE_TO], REASON_LOST);
    assert_eq!(lost[SIDE_FROM], REASON_BRIDGE_CLOSED);
    assert_eq!(m.count(), 0, "the bridge is still counted");
}

// TestSilenceOnOneLegEndsTheCallEvenWhileTheOtherTalks
#[tokio::test(start_paused = true)]
async fn silence_on_one_leg_ends_the_call_even_while_the_other_talks() {
    let (m, f, ev) = new_mgr(cfg_silence(ms(400)));
    let res = m.open("hso-1", frames_specs(&[])).await.unwrap();
    let (from, _) = legs_of(&f, &res);
    let talker = tokio::spawn({
        let from = from.clone();
        async move {
            loop {
                sleep(ms(10)).await;
                from.try_up(frame(1));
            }
        }
    });
    eventually("silence end", || ev.of(STATE_LOST).len() == 2).await;
    talker.abort();
    for e in ev.of(STATE_LOST) {
        match e.side.as_str() {
            SIDE_TO => assert_eq!(e.reason, REASON_SILENCE, "the silent leg's reason"),
            _ => assert_eq!(e.reason, REASON_BRIDGE_CLOSED, "the talking leg's reason"),
        }
    }
    assert_eq!(m.count(), 0, "still counted");
}

/// Lets the tasks that are ready run (the paused clock does not advance while any is).
async fn settle() {
    for _ in 0..30 {
        tokio::task::yield_now().await;
    }
}

// R2-30: the default silence time is 10 s, to the millisecond
#[tokio::test(start_paused = true)]
async fn the_default_silence_time_is_ten_seconds() {
    assert_eq!(DEFAULT_SILENCE, Duration::from_secs(10));
    let (m, _, ev) = new_mgr(Config::default());
    m.open("hso-1", frames_specs(&[])).await.unwrap();
    sleep(ms(9_990)).await;
    settle().await;
    assert!(ev.of(STATE_LOST).is_empty(), "ended before 10 s");
    sleep(ms(30)).await; // t = 10.02 s
    settle().await;
    assert_eq!(ev.of(STATE_LOST).len(), 2, "not ended by 10.02 s");
}

// TestALegThatNeverSendsAFrameHitsTheAudioDeadline
#[tokio::test(start_paused = true)]
async fn a_leg_that_never_sends_a_frame_hits_the_audio_deadline() {
    let (m, _, ev) = new_mgr(cfg_silence(ms(40)));
    m.open("hso-1", frames_specs(&[])).await.unwrap();
    // R2-30: the deadline is the configured silence, to the millisecond (paused clock): nothing
    // 1 ms before it, both `lost` events 20 ms after it
    sleep(ms(39)).await;
    settle().await;
    assert!(
        ev.of(STATE_LOST).is_empty(),
        "ended before the silence time"
    );
    sleep(ms(21)).await; // t = 60 ms
    settle().await;
    assert_eq!(
        ev.of(STATE_LOST).len(),
        2,
        "not ended 20 ms after the silence time"
    );
    assert!(
        ev.of(STATE_FIRST_FRAME).is_empty(),
        "a first_frame without a frame"
    );
}

// TestFramesFromBothLegsKeepTheCallAliveAndFirstFrameIsReportedOnce
#[tokio::test(start_paused = true)]
async fn frames_from_both_legs_keep_the_call_alive_and_first_frame_is_reported_once() {
    let (m, f, ev) = new_mgr(cfg_silence(ms(400)));
    let res = m.open("hso-1", frames_specs(&[])).await.unwrap();
    let (from, to) = legs_of(&f, &res);
    let end = Instant::now() + ms(1200); // three silence windows
    while Instant::now() < end {
        from.send_up(frame(1)).await;
        to.send_up(frame(2)).await;
        from.recv_down().await;
        to.recv_down().await;
        sleep(ms(10)).await;
    }
    assert_eq!(m.count(), 1, "a live call was ended by the watchdog");
    let ff = ev.of(STATE_FIRST_FRAME);
    assert!(
        ff.len() == 2 && ff[0].side != ff[1].side,
        "first_frame events {ff:?}"
    );
    m.close_all().await;
}

// R2-order (PARITY 360): Go's pump writes `first_frame` in its own goroutine before it forwards
// the frame (`touch`, then the send into the other leg), so the frame's `media_frame` can never
// overtake it. Here the emit function is slow (a socket that is busy): the frame must wait.
// Mutation: `touch` hands the event to the queue and the pump forwards at once.
#[tokio::test(start_paused = true)]
async fn the_first_frame_event_is_written_before_the_frame_is_forwarded() {
    let log = Arc::new(Mutex::new(Vec::<&'static str>::new()));
    let l2 = log.clone();
    let emit: EmitFn = Arc::new(move |x: Event| -> BoxFut {
        let l = l2.clone();
        Box::pin(async move {
            if x.state == STATE_FIRST_FRAME {
                sleep(ms(30)).await; // the write takes a while
                l.lock().unwrap().push("first_frame");
            }
        })
    });
    let f = Arc::new(FakeFactory::default());
    let m = Manager::new(Config::default(), Arc::new(Ff(f.clone())), emit);
    let res = m.open("hso-1", frames_specs(&[])).await.unwrap();
    let (from, to) = legs_of(&f, &res);
    from.send_up(frame(7)).await;
    let got = to.recv_down().await;
    log.lock().unwrap().push("frame");
    assert_eq!(got[0], 7);
    assert_eq!(
        *log.lock().unwrap(),
        ["first_frame", "frame"],
        "the frame overtook its first_frame event"
    );
    // the second frame of the same leg does not wait for anything
    let t0 = Instant::now();
    from.send_up(frame(8)).await;
    to.recv_down().await;
    assert!(Instant::now() - t0 < ms(5), "a later frame was held up");
    m.close_all().await;
}

// TestHoldMakesTheHeldUplinkSilenceAndKeepsTheLegAlive
#[tokio::test(start_paused = true)]
async fn hold_makes_the_held_uplink_silence_and_keeps_the_leg_alive() {
    let (m, f, _) = new_mgr(cfg_silence(ms(400)));
    let res = m.open("hso-1", frames_specs(&[])).await.unwrap();
    let (from, to) = legs_of(&f, &res);
    assert!(m.hold("hso-1", SIDE_FROM, true), "hold refused");
    // the held phone keeps sending (its own silent uplink, but here loud so the substitution is visible)
    for _ in 0..4 {
        from.send_up(frame(9)).await;
        let got = to.recv_down().await;
        assert_eq!(
            got,
            vec![0u8; FRAME_BYTES],
            "the peer heard {} on hold",
            got[0]
        );
    }
    // the downlink to the held leg is dropped
    to.send_up(frame(5)).await;
    if let Some(x) = from.try_down(ms(40)).await {
        panic!("the held leg was sent audio: {}", x[0]);
    }
    // the held leg's uplink keeps feeding the watchdog: 3 windows of held frames, call still up
    let end = Instant::now() + ms(1200);
    while Instant::now() < end {
        from.send_up(frame(9)).await;
        to.send_up(frame(5)).await;
        to.recv_down().await;
        sleep(ms(10)).await;
    }
    assert_eq!(
        m.count(),
        1,
        "a held call died of silence although the held leg kept sending"
    );
    m.hold("hso-1", SIDE_FROM, false);
    from.send_up(frame(9)).await;
    assert_eq!(
        to.recv_down().await[0],
        9,
        "audio did not come back after hold"
    );
    to.send_up(frame(5)).await;
    assert_eq!(
        from.recv_down().await[0],
        5,
        "downlink did not come back after hold"
    );
    assert!(
        !m.hold("nope", SIDE_FROM, true) && !m.hold("hso-1", "left", true),
        "hold accepted a bad target"
    );
    m.close_all().await;
}

fn fixed_id(id: &'static str) -> Config {
    Config {
        new_id: Some(Arc::new(move || id.to_string())),
        ..Default::default()
    }
}

// TestAWebRTCOfferIsAnsweredAfterOpenReturnsAndTheSDPArrivesAsAnEvent
#[tokio::test(start_paused = true)]
async fn a_webrtc_offer_is_answered_after_open_returns_and_the_sdp_arrives_as_an_event() {
    let (m, f, ev) = new_mgr(fixed_id("feedfeedfeedfeed"));
    let gate = Gate::new();
    f.st.lock()
        .unwrap()
        .gate
        .insert("house-feedfeedfeedfeed-from".into(), gate.clone());
    let res = m.open("hso-1", webrtc_from("offer-1")).await.unwrap();
    assert!(
        res.legs[0].leg_id.is_empty() && res.legs[0].kind == KIND_WEBRTC,
        "webrtc leg descriptor {:?}",
        res.legs[0]
    );
    // Open returned while the offer is still being answered (ICE gathering): nothing delivered yet.
    sleep(ms(20)).await;
    assert!(ev.of(STATE_SDP).is_empty(), "the sdp event beat the gate");
    gate.open();
    eventually("sdp event", || ev.of(STATE_SDP).len() == 1).await;
    let e = ev.of(STATE_SDP).remove(0);
    assert_eq!(
        (e.call_id.as_str(), e.side.as_str(), e.sdp.as_str()),
        ("hso-1", SIDE_FROM, "answer-for:offer-1")
    );
    let (from, _) = legs_of(&f, &res);
    from.connect();
    eventually("attached", || ev.of(STATE_ATTACHED).len() == 1).await;
    let got = ev.of(STATE_ATTACHED);
    assert_eq!(got[0].side, SIDE_FROM);
    // the events arrived in the order they happened
    let states: Vec<String> = ev.all().into_iter().map(|e| e.state).collect();
    assert_eq!(states, vec![STATE_SDP, STATE_ATTACHED]);
    m.close_all().await;
}

// TestAnOfferThatCannotBeAnsweredEndsTheCall
#[tokio::test(start_paused = true)]
async fn an_offer_that_cannot_be_answered_ends_the_call() {
    let (m, f, ev) = new_mgr(Config::default());
    f.st.lock().unwrap().offer_err = Some("bad sdp".into());
    let res = m.open("hso-1", webrtc_from("garbage")).await.unwrap();
    let (_, to) = legs_of(&f, &res);
    eventually("webrtc_failed", || ev.of(STATE_LOST).len() == 2).await;
    assert!(
        to.is_closed() && m.count() == 0,
        "the call outlived its failed leg"
    );
    for e in ev.of(STATE_LOST) {
        if e.side == SIDE_FROM {
            assert_eq!(e.reason, REASON_WEBRTC_FAILED);
        }
    }
    assert!(
        ev.of(STATE_SDP).is_empty(),
        "an sdp event for a failed offer"
    );
}

// TestACallClosedWhileAnOfferIsPendingGetsNoLateSDP
#[tokio::test(start_paused = true)]
async fn a_call_closed_while_an_offer_is_pending_gets_no_late_sdp() {
    let (m, f, ev) = new_mgr(fixed_id("0000000000000001"));
    let gate = Gate::new();
    f.st.lock()
        .unwrap()
        .gate
        .insert("house-0000000000000001-from".into(), gate.clone());
    m.open("hso-1", webrtc_from("o")).await.unwrap();
    m.close("hso-1").await;
    gate.open();
    sleep(ms(40)).await;
    assert!(ev.all().is_empty(), "events after close: {:?}", ev.all());
}

/// R2-104: the `open` future is dropped while the abort of an already built leg waits on that leg's
/// close; the close (whose work is in the future it returns) still runs to the end.
/// Mutation: `abort` is awaited inline instead of on a task of its own.
#[tokio::test(start_paused = true)]
async fn an_open_dropped_during_the_abort_still_closes_the_built_leg() {
    let done = Arc::new(AtomicUsize::new(0));
    let gate = Gate::new();
    let (m, f, _) = new_mgr(Config::default());
    {
        let mut st = f.st.lock().unwrap();
        st.fail_on = "to".into();
        st.close_gate = Some(gate.clone());
        st.close_done = Some(done.clone());
    }
    let m2 = m.clone();
    let h = tokio::spawn(async move { m2.open("hso-ab", frames_specs(&[])).await });
    sleep(ms(20)).await;
    assert!(
        !h.is_finished(),
        "the abort should be waiting on the leg's close"
    );
    h.abort();
    sleep(ms(20)).await;
    gate.open();
    eventually("the built leg's close to finish", || {
        done.load(Ordering::SeqCst) == 1
    })
    .await;
    eventually("the slot to be released", || m.table_entries() == 0).await;
}

// TestAFactoryFailureLeavesNoSlotNoLegAndNoOwnerCount
#[tokio::test(start_paused = true)]
async fn a_factory_failure_leaves_no_slot_no_leg_and_no_owner_count() {
    let (m, f, _) = new_mgr(Config {
        max_bridges: 1,
        max_per_owner: 1,
        ..Default::default()
    });
    f.st.lock().unwrap().fail_on = "to".into();
    let err = m
        .open("hso-1", frames_specs(&["alice", "bob"]))
        .await
        .expect_err("expected the factory error");
    assert!(matches!(err, HouseError::Failed(_)), "{err:?}");
    assert_eq!(err.message(), "house: building the to leg: factory says no");
    let built = f.all();
    assert!(
        built.len() == 1 && built[0].is_closed(),
        "the leg built before the failure was not closed: {}",
        built.len()
    );
    f.st.lock().unwrap().fail_on = String::new();
    m.open("hso-1", frames_specs(&["alice", "bob"]))
        .await
        .expect("the failed open leaked its slot or owner count");
    m.close_all().await;
}

// TestFrameAndCloseLegOnlyAnswerForLiveFramesLegs
#[tokio::test(start_paused = true)]
async fn frame_and_close_leg_only_answer_for_live_frames_legs() {
    let (m, f, ev) = new_mgr(Config::default());
    let res = m.open("hso-1", webrtc_from("o")).await.unwrap();
    let (from, _) = legs_of(&f, &res);
    assert_eq!(
        m.frame("house-nope-from", &frame(1)),
        Err(FrameError::UnknownLeg)
    );
    assert_eq!(
        m.frame("gso-door-call", &frame(1)),
        Err(FrameError::UnknownLeg)
    );
    assert_eq!(
        m.frame(&format!("{}-from", res.session), &frame(1)),
        Err(FrameError::UnknownLeg),
        "a webrtc leg takes no pushed frames"
    );
    m.frame(&format!("{}-to", res.session), &frame(3)).unwrap();
    // A pushed frame is the "to" phone's uplink: the forwarder drains the "to" leg's Out() and
    // hands the frame to the OTHER leg's In(); the test reads the destination.
    assert_eq!(
        from.recv_down().await[0],
        3,
        "pushed frame did not reach the other leg"
    );
    assert!(
        !m.close_leg("house-nope-to", "x").await,
        "closed a leg that does not exist"
    );
    assert!(
        m.close_leg(&format!("{}-to", res.session), "").await,
        "goodbye refused"
    );
    eventually("lost events", || ev.of(STATE_LOST).len() == 2).await;
    assert_eq!(m.count(), 0, "still counted");
    // the empty reason became "lost"
    let to_lost = ev
        .of(STATE_LOST)
        .into_iter()
        .find(|e| e.side == SIDE_TO)
        .unwrap();
    assert_eq!(to_lost.reason, REASON_LOST);
}

// TestCloseAllEndsEveryBridgeRefusesLaterOpensAndIsQuiet
#[tokio::test(start_paused = true)]
async fn close_all_ends_every_bridge_refuses_later_opens_and_is_quiet() {
    let (m, f, ev) = new_mgr(Config::default());
    for i in 1..=3 {
        let _ = m
            .open(&format!("hso-{i}"), frames_specs(&[&format!("p{i}"), ""]))
            .await;
    }
    m.close_all().await;
    for l in f.all() {
        assert!(
            l.is_closed() && l.reason() == REASON_SHUTDOWN,
            "leg {} closed={} reason={:?}",
            l.id,
            l.is_closed(),
            l.reason()
        );
    }
    assert_eq!(m.count(), 0, "bridges survived");
    assert_eq!(
        m.open("hso-9", frames_specs(&[])).await.unwrap_err(),
        HouseError::Shutdown
    );
    m.close_all().await; // twice is fine
    sleep(ms(20)).await;
    assert!(ev.all().is_empty(), "shutdown sent events: {:?}", ev.all());
}

// TestALegAlreadyGoneAtOpenLeavesNothingBehind
#[tokio::test(start_paused = true)]
async fn a_leg_already_gone_at_open_leaves_nothing_behind() {
    let (m, f, _) = new_mgr(Config::default());
    // a factory whose legs are born closed: on_disconnected runs at once
    f.st.lock().unwrap().born_closed = true;
    m.open("hso-1", frames_specs(&["alice", "bob"]))
        .await
        .unwrap();
    assert_eq!(m.count(), 0, "a dead bridge was published");
    assert_eq!(m.table_entries(), 0, "table entries left");
}

// TestOpensRacingCloseAllNeverLeakABridge
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn opens_racing_close_all_never_leak_a_bridge() {
    for round in 0..30 {
        let (m, f, _) = new_mgr(Config::default());
        let mut hs = Vec::new();
        for i in 0..4 {
            let m = m.clone();
            hs.push(tokio::spawn(async move {
                let _ = m.open(&format!("hso-{i}"), frames_specs(&[])).await;
            }));
        }
        m.close_all().await;
        for h in hs {
            h.await.unwrap();
        }
        assert_eq!(m.count(), 0, "round {round}: bridges survived the shutdown");
        for l in f.all() {
            assert!(l.is_closed(), "round {round}: leg {} left open", l.id);
        }
        assert_eq!(m.table_entries(), 0, "round {round}: slots leaked");
    }
}

// TestManyCallsLeaveNoGoroutinesBehind
#[tokio::test(start_paused = true)]
async fn many_calls_leave_no_tasks_behind() {
    let (m, _, _) = new_mgr(Config::default());
    sleep(ms(20)).await;
    let rt = tokio::runtime::Handle::current();
    let base = rt.metrics().num_alive_tasks();
    for i in 0..60 {
        let id = format!("hso-{i}");
        m.open(&id, frames_specs(&[])).await.unwrap();
        if i % 2 == 0 {
            m.close(&id).await;
        } else {
            let leg = format!("{}-to", m.session_of(&id).unwrap());
            m.close_leg(&leg, "bye").await;
        }
    }
    eventually("tasks to settle", || {
        rt.metrics().num_alive_tasks() <= base + 2
    })
    .await;
}

// TestSlotsAreHeldWhileLegsAreStillBeingBuilt
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn slots_are_held_while_legs_are_still_being_built() {
    let (m, f, _) = new_mgr(Config::default());
    let gate = Gate::new();
    f.st.lock().unwrap().build_gate = Some(gate.clone());
    let mut hs = Vec::new();
    for i in 1..=4 {
        let m = m.clone();
        hs.push(tokio::spawn(async move {
            m.open(&format!("hso-{i}"), frames_specs(&[&format!("p{i}"), ""]))
                .await
        }));
    }
    eventually("four opens to reserve", || m.pending_len() == 4).await;
    let e = m.open("hso-5", frames_specs(&[])).await.unwrap_err();
    assert!(
        matches!(e, HouseError::TooMany(_)),
        "a fifth open while four are mid-build: {e:?}"
    );
    assert_eq!(
        m.open("hso-1", frames_specs(&[])).await.unwrap_err(),
        HouseError::AlreadyOpen,
        "a duplicate of a mid-build call"
    );
    gate.open();
    for h in hs {
        h.await.unwrap().unwrap();
    }
    assert_eq!(m.count(), 4);
    m.close_all().await;
}

// TestACloseAllDuringABuildLeavesNoBridgeAndNoOpenLeg
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn a_close_all_during_a_build_leaves_no_bridge_and_no_open_leg() {
    let (m, f, _) = new_mgr(Config::default());
    let gate = Gate::new();
    f.st.lock().unwrap().build_gate = Some(gate.clone());
    let m2 = m.clone();
    let h = tokio::spawn(async move { m2.open("hso-1", frames_specs(&["alice", "bob"])).await });
    eventually("the open to reserve", || m.pending_len() == 1).await;
    m.close_all().await;
    gate.open();
    assert_eq!(
        h.await.unwrap().unwrap_err(),
        HouseError::Shutdown,
        "open that lost the race with shutdown"
    );
    assert_eq!(m.count(), 0, "a bridge was published after shutdown");
    for l in f.all() {
        assert!(l.is_closed(), "leg {} left open", l.id);
    }
    assert_eq!(m.table_entries(), 0, "slots leaked");
}

// ---- 3g: the per-leg counters and the one line a call writes when it ends --------------------------

/// `fakeClock`: starts at 2026-10-22 12:00:00 UTC in nanoseconds.
#[derive(Clone)]
struct FakeClock(Arc<AtomicU64>);

impl FakeClock {
    fn new() -> FakeClock {
        FakeClock(Arc::new(AtomicU64::new(1_792_670_400_000_000_000)))
    }
    fn now_fn(&self) -> Arc<dyn Fn() -> u64 + Send + Sync> {
        let c = self.0.clone();
        Arc::new(move || c.load(Ordering::SeqCst))
    }
    fn advance(&self, d: Duration) {
        self.0.fetch_add(d.as_nanos() as u64, Ordering::SeqCst);
    }
}

/// `logLines`
#[derive(Default, Clone)]
struct LogLines(Arc<Mutex<Vec<String>>>);

impl LogLines {
    fn logf(&self) -> Arc<dyn Fn(String) + Send + Sync> {
        let l = self.0.clone();
        Arc::new(move |s| l.lock().unwrap().push(s))
    }
    fn all(&self) -> Vec<String> {
        self.0.lock().unwrap().clone()
    }
}

/// `pushAndWait`: sends one frame into the leg and waits until the bridge has counted it.
async fn push_and_wait(m: &Arc<Manager>, call: &str, side: &str, leg: &FakeLeg, b: u8) {
    let want = m.counters(call, side).unwrap()[0] + 1;
    leg.send_up(frame(b)).await;
    eventually("the frame to be counted", || {
        m.counters(call, side).unwrap()[0] >= want
    })
    .await;
}

// TestCountersRecordFramesGapsAndConcealmentPerLeg
#[tokio::test(start_paused = true)]
async fn counters_record_frames_gaps_and_concealment_per_leg() {
    let (clk, lg) = (FakeClock::new(), LogLines::default());
    let (m, f, _) = new_mgr(Config {
        now: Some(clk.now_fn()),
        logf: Some(lg.logf()),
        ..Default::default()
    });
    let res = m.open("hso-c1", frames_specs(&[])).await.unwrap();
    let (from, to) = legs_of(&f, &res);

    push_and_wait(&m, "hso-c1", SIDE_FROM, &from, 1).await;
    clk.advance(ms(40)); // on time: no gap worth counting
    push_and_wait(&m, "hso-c1", SIDE_FROM, &from, 2).await;
    clk.advance(ms(200)); // five frame times: four missing
    push_and_wait(&m, "hso-c1", SIDE_FROM, &from, 3).await;
    clk.advance(ms(60)); // one missing (the threshold)
    push_and_wait(&m, "hso-c1", SIDE_FROM, &from, 4).await;
    clk.advance(ms(50)); // jitter, rounds to one frame: none missing
    push_and_wait(&m, "hso-c1", SIDE_FROM, &from, 5).await;
    push_and_wait(&m, "hso-c1", SIDE_TO, &to, 9).await;
    clk.advance(Duration::from_secs(2));

    assert!(m.close("hso-c1").await, "no call to close");
    let got = lg.all();
    assert_eq!(got.len(), 1, "want exactly one counters line, got {got:?}");
    let want = "house call hso-c1 ended reason=hangup cause=- dur=2.35s \
from[frames rx=5 tx=1 dropped=0 bad=0 concealed=5 maxgap=200ms] \
to[frames rx=1 tx=5 dropped=0 bad=0 concealed=0 maxgap=0s]";
    assert_eq!(got[0], want);
}

// TestCountersCountDropsAndWrongSizeFrames
#[tokio::test(start_paused = true)]
async fn counters_count_drops_and_wrong_size_frames() {
    let lg = LogLines::default();
    let (m, f, _) = new_mgr(Config {
        logf: Some(lg.logf()),
        ..Default::default()
    });
    let res = m.open("hso-c2", frames_specs(&[])).await.unwrap();
    let (from, _to) = legs_of(&f, &res);
    from.send_up(vec![1, 2, 3]).await; // bad
    for _ in 0..8 {
        push_and_wait(&m, "hso-c2", SIDE_FROM, &from, 1).await; // fills to.in (8 slots)
    }
    push_and_wait(&m, "hso-c2", SIDE_FROM, &from, 1).await; // the ninth: dropped
    push_and_wait(&m, "hso-c2", SIDE_FROM, &from, 1).await; // the tenth: dropped
    m.close("hso-c2").await;
    let got = lg.all();
    assert_eq!(got.len(), 1, "lines: {got:?}");
    for w in [
        "from[frames rx=10 tx=0 dropped=0 bad=1 ",
        "to[frames rx=0 tx=8 dropped=2 bad=0 ",
    ] {
        assert!(got[0].contains(w), "missing {w:?} in {:?}", got[0]);
    }
}

// TestACallWritesOneLineWhoeverEndsItAndNamesTheCause
#[tokio::test(start_paused = true)]
async fn a_call_writes_one_line_whoever_ends_it_and_names_the_cause() {
    for case in ["hangup", "goodbye", "leg lost", "shutdown"] {
        let lg = LogLines::default();
        let (m, f, _) = new_mgr(Config {
            logf: Some(lg.logf()),
            ..Default::default()
        });
        let res = m.open("hso-c3", frames_specs(&[])).await.unwrap();
        let want = match case {
            "hangup" => {
                m.close("hso-c3").await;
                "reason=hangup cause=-"
            }
            "goodbye" => {
                m.close_leg(&format!("{}-to", res.session), "goodbye").await;
                "reason=goodbye cause=to"
            }
            "leg lost" => {
                let (from, _) = legs_of(&f, &res);
                drop(HouseLeg::close(&*from, "x"));
                "reason=lost cause=from"
            }
            _ => {
                m.close_all().await;
                "reason=shutdown cause=-"
            }
        };
        eventually("the line", || !lg.all().is_empty()).await;
        m.close("hso-c3").await;
        m.close_all().await; // a second end changes nothing
        sleep(ms(20)).await;
        let got = lg.all();
        assert_eq!(got.len(), 1, "{case}: want one line, got {got:?}");
        assert!(
            got[0].contains(want),
            "{case}: missing {want:?} in {:?}",
            got[0]
        );
    }
}

// TestSilenceEndLineAndAHalfBuiltCallWritesNoLine
#[tokio::test(start_paused = true)]
async fn silence_end_line_and_a_half_built_call_writes_no_line() {
    let lg = LogLines::default();
    let (m, f, _) = new_mgr(Config {
        silence: ms(30),
        logf: Some(lg.logf()),
        ..Default::default()
    });
    m.open("hso-c4", frames_specs(&[])).await.unwrap();
    eventually("the silence line", || lg.all().len() == 1).await;
    let got = lg.all().remove(0);
    assert!(got.contains("reason=silence cause="), "line {got:?}");
    f.st.lock().unwrap().fail_on = "to".into();
    assert!(
        m.open("hso-c5", frames_specs(&[])).await.is_err(),
        "open should fail"
    );
    sleep(ms(20)).await;
    assert_eq!(
        lg.all().len(),
        1,
        "a call that never started wrote a line: {:?}",
        lg.all()
    );
}

// TestCountersAreRaceFreeAndANilLoggerIsQuiet
#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn counters_are_race_free_and_a_nil_logger_is_quiet() {
    let (m, f, _) = new_mgr(Config::default()); // no logf: nothing to call, nothing to panic
    let res = m.open("hso-c6", frames_specs(&[])).await.unwrap();
    let (from, to) = legs_of(&f, &res);
    let mut hs = Vec::new();
    for l in [from, to] {
        hs.push(tokio::spawn(async move {
            for _ in 0..200 {
                l.try_up(frame(1));
                let _ = l.try_down(Duration::from_micros(1)).await;
                tokio::task::yield_now().await;
            }
        }));
    }
    for h in hs {
        h.await.unwrap();
    }
    m.close("hso-c6").await;
}

// ---- Rust-only ---------------------------------------------------------------------------------

/// A leg whose close panics must not stop the other leg from closing, the counters line, or the
/// `lost` events (PARITY.md 166).
#[tokio::test(start_paused = true)]
async fn a_panic_while_closing_a_leg_still_closes_the_other_and_tells_both() {
    let lg = LogLines::default();
    let (m, f, ev) = new_mgr(Config {
        logf: Some(lg.logf()),
        ..Default::default()
    });
    f.st.lock().unwrap().close_panics = true;
    let res = m.open("hso-p1", frames_specs(&[])).await.unwrap();
    let (from, to) = legs_of(&f, &res);
    assert!(m.close_leg(&format!("{}-from", res.session), "bye").await);
    assert!(from.is_closed() && to.is_closed());
    assert_eq!(lg.all().len(), 1);
    eventually("lost events", || ev.of(STATE_LOST).len() == 2).await;
    assert_eq!(m.table_entries(), 0);
}

/// A forwarder task that panics ends the call with reason "error", both legs closed and both told
/// (PARITY.md 166).
#[tokio::test(start_paused = true)]
async fn a_panicking_forwarder_ends_the_call_and_closes_both_legs() {
    let (m, f, ev) = new_mgr(Config::default());
    f.st.lock().unwrap().output_panics = true;
    let res = m.open("hso-p2", frames_specs(&[])).await.unwrap();
    let (from, to) = legs_of(&f, &res);
    eventually("both legs lost", || ev.of(STATE_LOST).len() == 2).await;
    assert!(
        from.is_closed() && to.is_closed(),
        "a leg stayed open after the panic"
    );
    assert!(ev.of(STATE_LOST).iter().any(|e| e.reason == REASON_ERROR));
    assert_eq!(m.count(), 0);
    assert_eq!(m.table_entries(), 0);
    let _ = res;
}

/// An offer whose answering panics is an offer that could not be answered.
#[tokio::test(start_paused = true)]
async fn a_panic_while_answering_an_offer_is_webrtc_failed() {
    let (m, f, ev) = new_mgr(Config::default());
    f.st.lock().unwrap().offer_panics = true;
    let res = m.open("hso-p3", webrtc_from("o")).await.unwrap();
    let (_, to) = legs_of(&f, &res);
    eventually("lost events", || ev.of(STATE_LOST).len() == 2).await;
    assert!(to.is_closed());
    let from_lost = ev
        .of(STATE_LOST)
        .into_iter()
        .find(|e| e.side == SIDE_FROM)
        .unwrap();
    assert_eq!(from_lost.reason, REASON_WEBRTC_FAILED);
}

/// A hook that panics (the counters line) does not stop the `lost` events behind it.
#[tokio::test(start_paused = true)]
async fn a_panicking_log_hook_does_not_swallow_the_lost_events() {
    let (m, _f, ev) = new_mgr(Config {
        logf: Some(Arc::new(|_| panic!("log hook blew up"))),
        ..Default::default()
    });
    let res = m.open("hso-p4", frames_specs(&[])).await.unwrap();
    m.close_leg(&format!("{}-to", res.session), "bye").await;
    eventually("both lost events", || ev.of(STATE_LOST).len() == 2).await;
}

/// One event that panics in the emitter does not stop the events behind it.
#[tokio::test(start_paused = true)]
async fn a_panicking_emitter_does_not_stop_the_event_queue() {
    let f = Arc::new(FakeFactory::default());
    let seen = Arc::new(Mutex::new(Vec::<String>::new()));
    let s2 = seen.clone();
    let emit: EmitFn = Arc::new(move |e: Event| -> BoxFut {
        let s = s2.clone();
        Box::pin(async move {
            if e.side == SIDE_FROM {
                panic!("emit blew up");
            }
            s.lock().unwrap().push(format!("{}:{}", e.side, e.state));
        })
    });
    let m = Manager::new(Config::default(), Arc::new(Ff(f)), emit);
    let res = m.open("hso-p5", frames_specs(&[])).await.unwrap();
    m.close_leg(&format!("{}-to", res.session), "bye").await;
    eventually("the second event", || seen.lock().unwrap().len() == 1).await;
    assert_eq!(seen.lock().unwrap()[0], "to:lost");
}

/// Dropping the `open` future (the request task was cancelled) mid-build releases the slot and the
/// owner counts (PARITY.md 167).
#[tokio::test(start_paused = true)]
async fn a_dropped_open_releases_its_slot_and_its_owner_counts() {
    let (m, f, _) = new_mgr(Config {
        max_bridges: 1,
        max_per_owner: 1,
        ..Default::default()
    });
    let gate = Gate::new();
    f.st.lock().unwrap().build_gate = Some(gate.clone());
    let r = timeout(ms(50), m.open("hso-d1", frames_specs(&["alice", "bob"]))).await;
    assert!(r.is_err(), "the open should still be building");
    eventually("the slot to be released", || m.table_entries() == 0).await;
    gate.open();
    f.st.lock().unwrap().build_gate = None;
    m.open("hso-d1", frames_specs(&["alice", "bob"]))
        .await
        .expect("the dropped open leaked its slot or owner count");
    m.close_all().await;
}

/// Dropping the `open` future after the first leg was built closes that leg.
#[tokio::test(start_paused = true)]
async fn a_dropped_open_after_one_leg_was_built_closes_it() {
    let (m, f, _) = new_mgr(Config::default());
    let gate = Gate::new();
    {
        let mut g = f.st.lock().unwrap();
        g.build_gate = Some(gate.clone());
        g.build_gate_suffix = "to".into();
    }
    let r = timeout(ms(50), m.open("hso-d2", frames_specs(&[]))).await;
    assert!(
        r.is_err(),
        "the open should still be building the second leg"
    );
    assert_eq!(f.built(), 1, "the first leg was not built");
    eventually("the built leg to be closed", || {
        f.all().iter().all(|l| l.is_closed())
    })
    .await;
    assert_eq!(m.table_entries(), 0);
    gate.open();
}

/// A dropped `EndTail` still closes the legs, writes the line and sends the `lost` events
/// (PARITY.md 167): the task that awaited it was torn down in the middle of the close.
#[tokio::test(start_paused = true)]
async fn a_dropped_end_tail_still_finishes_the_call() {
    let lg = LogLines::default();
    let (m, f, ev) = new_mgr(Config {
        logf: Some(lg.logf()),
        ..Default::default()
    });
    // the close of a leg waits on a gate that is never opened (a notification stuck in a write)
    f.st.lock().unwrap().close_gate = Some(Gate::new());
    let res = m.open("hso-t1", frames_specs(&[])).await.unwrap();
    let (from, to) = legs_of(&f, &res);
    let r = timeout(ms(50), m.close_leg(&format!("{}-to", res.session), "bye")).await;
    assert!(r.is_err(), "the close should still be waiting on the gate");
    eventually("the line and the lost events", || {
        lg.all().len() == 1 && ev.of(STATE_LOST).len() == 2
    })
    .await;
    assert!(from.is_closed() && to.is_closed());
    assert_eq!(m.table_entries(), 0);
}

/// `close_all` closes the bridges together: four bridges whose legs take 150 ms to close.
#[tokio::test(start_paused = true)]
async fn close_all_closes_the_bridges_together() {
    let (m, f, _) = new_mgr(Config::default());
    f.st.lock().unwrap().close_delay = ms(150);
    for i in 1..=4 {
        m.open(&format!("hso-{i}"), frames_specs(&[]))
            .await
            .unwrap();
    }
    let t0 = Instant::now();
    m.close_all().await;
    let took = t0.elapsed();
    assert!(
        took < ms(700),
        "close_all took {took:?} (serial would be 1.2 s)"
    );
    assert!(f.all().iter().all(|l| l.is_closed()));
}

/// `close_all` is bounded as a whole: legs whose close never finishes cannot hold it up, and the
/// tails go on in the background (PARITY.md 164).
#[tokio::test(start_paused = true)]
async fn close_all_is_bounded_as_a_whole() {
    let lg = LogLines::default();
    let (m, f, _) = new_mgr(Config {
        close_all_wait: ms(150),
        logf: Some(lg.logf()),
        ..Default::default()
    });
    f.st.lock().unwrap().close_gate = Some(Gate::new());
    for i in 1..=4 {
        m.open(&format!("hso-{i}"), frames_specs(&[]))
            .await
            .unwrap();
    }
    let t0 = Instant::now();
    m.close_all().await;
    let took = t0.elapsed();
    assert!(
        took >= ms(100) && took < ms(1500),
        "close_all took {took:?}"
    );
    assert!(f.all().iter().all(|l| l.is_closed()), "a leg stayed open");
    assert_eq!(m.count(), 0);
    assert_eq!(
        m.open("hso-9", frames_specs(&[])).await.unwrap_err(),
        HouseError::Shutdown
    );
}

// ---- Phase R round 1, group G ------------------------------------------------------------------

/// R1-118: a pump that cannot take its leg's uplink lock ends with its bridge.
#[tokio::test(start_paused = true)]
async fn a_pump_stuck_on_the_uplink_lock_ends_with_its_bridge() {
    let (m, f, _) = new_mgr(Config::default());
    f.st.lock().unwrap().hold_output = true;
    sleep(ms(20)).await;
    let rt = tokio::runtime::Handle::current();
    let base = rt.metrics().num_alive_tasks();
    for i in 0..5 {
        let id = format!("hso-{i}");
        m.open(&id, frames_specs(&[])).await.unwrap();
        m.close(&id).await;
    }
    eventually("the pumps to end", || {
        rt.metrics().num_alive_tasks() <= base + 2
    })
    .await;
}

/// R1-197 (log id; was written R1-24): the counters of a side that is neither `from` nor `to` are `None`, not those of `from`.
#[tokio::test(start_paused = true)]
async fn counters_of_an_unknown_side_are_none() {
    let (m, _, _) = new_mgr(Config::default());
    m.open("hso-1", frames_specs(&[])).await.unwrap();
    assert!(m.counters("hso-1", SIDE_FROM).is_some());
    assert!(m.counters("hso-1", SIDE_TO).is_some());
    assert!(m.counters("hso-1", "sideways").is_none());
}

/// R1-34: a tail torn down while a leg's close is still running does not abort that close; the
/// close finishes on its own task.
#[tokio::test(start_paused = true)]
async fn a_tail_dropped_mid_close_does_not_abort_the_leg_close() {
    let done = Arc::new(AtomicUsize::new(0));
    let (m, f, _) = new_mgr(Config::default());
    {
        let mut st = f.st.lock().unwrap();
        st.close_delay = ms(100);
        st.close_done = Some(done.clone());
    }
    m.open("hso-t", frames_specs(&[])).await.unwrap();
    // `close` awaits the tail; cancel the caller 10 ms into the legs' 100 ms close
    let m2 = m.clone();
    let h = tokio::spawn(async move { m2.close("hso-t").await });
    sleep(ms(10)).await;
    h.abort();
    sleep(ms(300)).await;
    assert_eq!(
        done.load(Ordering::SeqCst),
        2,
        "the two leg closes were aborted with the caller"
    );
}

/// R2-103: a restarted tail (the first one was dropped mid-close) waits for the closes of the first
/// run instead of calling `close` again, which returns at once for a leg that is already closed:
/// the counters line and the `lost` events come after every `media_closed`.
/// Mutation: `finish` closes the legs again on the restart (the pre-fix code).
#[tokio::test(start_paused = true)]
async fn a_restarted_tail_writes_the_lost_events_after_the_first_closes_finished() {
    let done = Arc::new(AtomicUsize::new(0));
    let gate = Gate::new();
    let lg = LogLines::default();
    let (m, f, ev) = new_mgr(Config {
        logf: Some(lg.logf()),
        ..Default::default()
    });
    {
        let mut st = f.st.lock().unwrap();
        st.close_gate = Some(gate.clone());
        st.close_done = Some(done.clone());
    }
    let res = m.open("hso-r", frames_specs(&[])).await.unwrap();
    // the caller of `close_leg` is torn down while both leg closes wait on the gate
    let (m2, to) = (m.clone(), format!("{}-to", res.session));
    let h = tokio::spawn(async move { m2.close_leg(&to, "bye").await });
    sleep(ms(10)).await;
    h.abort();
    sleep(ms(200)).await;
    assert_eq!(done.load(Ordering::SeqCst), 0, "the gate is still shut");
    assert!(
        ev.of(STATE_LOST).is_empty() && lg.all().is_empty(),
        "the restarted tail wrote the line / the lost events before the closes finished"
    );
    gate.open();
    eventually("the line and the lost events", || {
        lg.all().len() == 1 && ev.of(STATE_LOST).len() == 2
    })
    .await;
    assert_eq!(done.load(Ordering::SeqCst), 2);
}

/// R1-99: `route_close` keeps the fields that decoded when another has the wrong type
/// (`_ = json.Unmarshal(raw, &p)` in Go: the reason is `bye`, not `lost`).
#[tokio::test(start_paused = true)]
async fn route_close_keeps_the_reason_when_another_field_has_the_wrong_type() {
    use transport_rs::house::HouseHub;
    use transport_rs::wsmedia::{JoinFn, MediaNotifier, WsMediaHub};
    struct Quiet;
    impl MediaNotifier for Quiet {
        fn notify(&self, _m: &str, _p: serde_json::Value) -> BoxFut {
            Box::pin(async {})
        }
    }
    let (m, f, ev) = new_mgr(Config::default());
    let join: JoinFn = Arc::new(|_, _| {});
    let door = WsMediaHub::new(Arc::new(Quiet), Duration::from_secs(60), join);
    let hub = HouseHub::with_manager(m.clone(), door);
    let res = m.open("hso-1", frames_specs(&[])).await.unwrap();
    let raw = format!(
        r#"{{"call_id":"{}-from","reason":"bye","Reason":5}}"#,
        res.session
    );
    let params = Some(serde_json::value::RawValue::from_string(raw).unwrap());
    hub.route_close(params).await.unwrap();
    eventually("the lost events", || ev.of(STATE_LOST).len() == 2).await;
    let (from, _) = legs_of(&f, &res);
    assert_eq!(from.reason(), "bye", "the decoded reason was dropped");
    let lost = ev.of(STATE_LOST);
    assert!(
        lost.iter()
            .any(|e| e.side == SIDE_FROM && e.reason == "bye"),
        "{lost:?}"
    );
}
