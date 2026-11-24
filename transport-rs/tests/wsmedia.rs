//! Ports of the Go tests of `internal/wsmedia/leg_test.go` (10) and
//! `cmd/portico-transport/wsmedia_test.go` (6), the Go name in a comment on each, and the
//! behaviours the port adds ("Rust-only"). The hub tests inject `notify` and `join` exactly as
//! Go's fixture does.

use std::sync::atomic::{AtomicBool, AtomicI32, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde_json::{json, Value};
use tokio::time::{sleep, Instant};
use transport_rs::call::ClientLeg;
use transport_rs::ipc::{HandlerResult, IpcError, Params, Reply};
use transport_rs::util::{base64_decode, base64_encode};
use transport_rs::wsmedia::{
    JoinFn, Leg, MediaNotifier, PushError, SendFn, WsMediaHub, CODEC_L16, CODEC_PCMU, FRAME_MILLIS,
    L16_FRAME_BYTES, PCMU_FRAME_BYTES,
};

type Fut = std::pin::Pin<Box<dyn std::future::Future<Output = ()> + Send>>;

fn ms(n: u64) -> Duration {
    Duration::from_millis(n)
}

/// `sink`: the frames `send` was called with.
#[derive(Default)]
struct Sink {
    frames: Mutex<Vec<Vec<u8>>>,
}

impl Sink {
    fn send_fn(self: &Arc<Self>) -> SendFn {
        let s = self.clone();
        Arc::new(move |p: Vec<u8>| -> Fut {
            s.frames.lock().unwrap().push(p);
            Box::pin(async {})
        })
    }
    fn got(&self) -> Vec<Vec<u8>> {
        self.frames.lock().unwrap().clone()
    }
}

fn no_send() -> SendFn {
    Arc::new(|_p: Vec<u8>| -> Fut { Box::pin(async {}) })
}

fn l16_frame(v: i16) -> Vec<u8> {
    v.to_le_bytes()
        .iter()
        .copied()
        .cycle()
        .take(L16_FRAME_BYTES)
        .collect()
}

async fn wait_for(what: &str, mut cond: impl FnMut() -> bool) {
    let end = Instant::now() + Duration::from_secs(2);
    while Instant::now() < end {
        if cond() {
            return;
        }
        sleep(ms(5)).await;
    }
    panic!("timed out waiting for {what}");
}

async fn recv(leg: &Arc<Leg>, within: Duration) -> Option<Vec<u8>> {
    let out = ClientLeg::output(&**leg);
    let mut rx = out.lock().await;
    tokio::time::timeout(within, rx.recv()).await.ok().flatten()
}

fn mu_frame_of_silence() -> Vec<u8> {
    // encodeMuLawFrame(make([]int16, PCMUFrameBytes)): mu-law of 0 is 0xFF
    vec![0xFF; PCMU_FRAME_BYTES]
}

// ---- internal/wsmedia/leg_test.go ----------------------------------------------------------------

// TestNewRejectsUnknownCodec
#[tokio::test(start_paused = true)]
async fn new_rejects_unknown_codec() {
    let e = Leg::new("opus", Duration::from_secs(1), no_send())
        .err()
        .expect("want an error for an unsupported codec");
    assert_eq!(e, r#"wsmedia: unsupported codec "opus""#);
}

// TestOnConnectedRunsAtOnceEvenWhenRegisteredLate
#[tokio::test(start_paused = true)]
async fn on_connected_runs_at_once_even_when_registered_late() {
    let l = Leg::new(CODEC_PCMU, Duration::from_secs(60), no_send()).unwrap();
    let n = Arc::new(AtomicI32::new(0));
    let n2 = n.clone();
    ClientLeg::on_connected(
        &*l,
        Box::new(move || {
            n2.fetch_add(1, Ordering::SeqCst);
        }),
    );
    assert_eq!(
        n.load(Ordering::SeqCst),
        1,
        "OnConnected must run immediately for a leg that is already up"
    );
    l.close("test").await;
}

// TestPCMUUplinkBecomesAnL16Frame
#[tokio::test(start_paused = true)]
async fn pcmu_uplink_becomes_an_l16_frame() {
    let l = Leg::new(CODEC_PCMU, Duration::from_secs(60), no_send()).unwrap();
    l.push(&mu_frame_of_silence()).unwrap();
    let f = recv(&l, Duration::from_secs(1))
        .await
        .expect("no frame reached the device leg");
    assert_eq!(f.len(), L16_FRAME_BYTES, "device frame size");
    l.close("test").await;
}

// TestL16UplinkPassesThrough
#[tokio::test(start_paused = true)]
async fn l16_uplink_passes_through() {
    let l = Leg::new(CODEC_L16, Duration::from_secs(60), no_send()).unwrap();
    let inp = l16_frame(1234);
    l.push(&inp).unwrap();
    assert_eq!(
        recv(&l, Duration::from_secs(1)).await.unwrap(),
        inp,
        "l16 must not be altered"
    );
    l.close("test").await;
}

// TestDownlinkFramesAreEncodedForThePhone
#[tokio::test(start_paused = true)]
async fn downlink_frames_are_encoded_for_the_phone() {
    for (codec, size) in [(CODEC_PCMU, PCMU_FRAME_BYTES), (CODEC_L16, L16_FRAME_BYTES)] {
        let s = Arc::new(Sink::default());
        let l = Leg::new(codec, Duration::from_secs(60), s.send_fn()).unwrap();
        ClientLeg::input(&*l).send(l16_frame(500)).await.unwrap();
        wait_for(&format!("{codec} downlink"), || s.got().len() == 1).await;
        assert_eq!(s.got()[0].len(), size, "{codec}: phone frame size");
        l.close("test").await;
    }
}

// TestWrongSizedFrameIsRefusedAndNotForwarded
#[tokio::test(start_paused = true)]
async fn wrong_sized_frame_is_refused_and_not_forwarded() {
    let l = Leg::new(CODEC_PCMU, Duration::from_secs(60), no_send()).unwrap();
    assert_eq!(l.push(&[0u8; 7]), Err(PushError::BadFrame));
    assert!(
        recv(&l, ms(30)).await.is_none(),
        "a bad frame reached the device leg"
    );
    l.close("test").await;
}

// TestFullDeviceChannelDropsInsteadOfBlocking
#[tokio::test]
async fn full_device_channel_drops_instead_of_blocking() {
    let l = Leg::new(CODEC_L16, Duration::from_secs(60), no_send()).unwrap();
    // `push` is synchronous: it returns for all 50 frames or it never returns; the timeout
    // proves it on the async side as Go's goroutine did
    let l2 = l.clone();
    let done = tokio::task::spawn_blocking(move || {
        for _ in 0..50 {
            let _ = l2.push(&l16_frame(1));
        }
    });
    tokio::time::timeout(Duration::from_secs(1), done)
        .await
        .expect("Push blocked on a full channel")
        .unwrap();
    l.close("test").await;
}

// TestIdleWatchdogClosesTheLeg
#[tokio::test(start_paused = true)]
async fn idle_watchdog_closes_the_leg() {
    let l = Leg::new(CODEC_PCMU, ms(60), no_send()).unwrap();
    let reason = Arc::new(Mutex::new(None::<String>));
    let r = reason.clone();
    l.on_closed(move |why| -> Fut {
        *r.lock().unwrap() = Some(why);
        Box::pin(async {})
    })
    .await;
    wait_for("idle close", || reason.lock().unwrap().is_some()).await;
    assert_eq!(reason.lock().unwrap().as_deref(), Some("idle"));
}

// R2-21 (leg.go:103-107): a wrong-sized uplink frame returns `ErrBadFrame` BEFORE `timer.Reset`, so
// it does not restart the idle clock. On a paused clock the end is exact: idle 120 ms, bad frames
// at 70 and 110 ms, closed by 140 ms (with the clock restarted by them it would end at 230 ms).
#[tokio::test(start_paused = true)]
async fn a_wrong_sized_uplink_frame_does_not_restart_the_idle_clock() {
    let l = Leg::new(CODEC_PCMU, ms(120), no_send()).unwrap();
    let reason = Arc::new(Mutex::new(None::<String>));
    let r = reason.clone();
    l.on_closed(move |why| -> Fut {
        *r.lock().unwrap() = Some(why);
        Box::pin(async {})
    })
    .await;
    sleep(ms(70)).await;
    assert_eq!(l.push(&[0u8; 7]), Err(PushError::BadFrame));
    sleep(ms(40)).await;
    assert_eq!(l.push(&[0u8; 7]), Err(PushError::BadFrame));
    sleep(ms(30)).await; // t = 140 ms
    for _ in 0..20 {
        tokio::task::yield_now().await;
    }
    assert_eq!(
        reason.lock().unwrap().as_deref(),
        Some("idle"),
        "the bad frames restarted the idle clock"
    );
}

// TestUplinkKeepsTheWatchdogQuiet
#[tokio::test(start_paused = true)]
async fn uplink_keeps_the_watchdog_quiet() {
    let l = Leg::new(CODEC_L16, ms(80), no_send()).unwrap();
    let fired = Arc::new(AtomicBool::new(false));
    let f = fired.clone();
    l.on_closed(move |_| -> Fut {
        f.store(true, Ordering::SeqCst);
        Box::pin(async {})
    })
    .await;
    for _ in 0..8 {
        let _ = l.push(&l16_frame(1));
        let _ = recv(&l, Duration::from_secs(1)).await;
        sleep(ms(25)).await;
    }
    assert!(
        !fired.load(Ordering::SeqCst),
        "watchdog fired while frames were arriving"
    );
    l.close("test").await;
}

// TestCloseIsIdempotentAndStopsEverything
#[tokio::test(start_paused = true)]
async fn close_is_idempotent_and_stops_everything() {
    let s = Arc::new(Sink::default());
    let l = Leg::new(CODEC_L16, Duration::from_secs(60), s.send_fn()).unwrap();
    let calls = Arc::new(AtomicI32::new(0));
    let c = calls.clone();
    l.on_closed(move |_| -> Fut {
        c.fetch_add(1, Ordering::SeqCst);
        Box::pin(async {})
    })
    .await;
    l.close("a").await;
    l.close("b").await;
    assert_eq!(calls.load(Ordering::SeqCst), 1, "OnDisconnected ran twice");
    assert_eq!(l.push(&l16_frame(1)), Err(PushError::Closed));
    let _ = ClientLeg::input(&*l).try_send(l16_frame(1));
    sleep(ms(30)).await;
    assert!(s.got().is_empty(), "a frame was sent after close");
    let late = Arc::new(AtomicBool::new(false));
    let l2 = late.clone();
    l.on_closed(move |r| -> Fut {
        if r == "a" {
            l2.store(true, Ordering::SeqCst);
        }
        Box::pin(async {})
    })
    .await;
    assert!(
        late.load(Ordering::SeqCst),
        "a late OnDisconnected must run at once with the original reason"
    );
}

// ---- cmd/portico-transport/wsmedia_test.go -----------------------------------------------------------

/// `hubFixture`: the hub with a recording `notify` and `join`.
struct Fixture {
    hub: Arc<WsMediaHub>,
    notes: Arc<Mutex<Vec<(String, Value)>>>,
    joined: Arc<Mutex<Vec<String>>>,
}

struct Notes(Arc<Mutex<Vec<(String, Value)>>>);

impl MediaNotifier for Notes {
    fn notify(&self, method: &str, params: Value) -> Fut {
        self.0.lock().unwrap().push((method.to_string(), params));
        Box::pin(async {})
    }
}

fn new_hub(idle: Duration) -> Fixture {
    let notes = Arc::new(Mutex::new(Vec::new()));
    let joined = Arc::new(Mutex::new(Vec::new()));
    let j = joined.clone();
    let join: JoinFn = Arc::new(move |id: &str, _leg: Arc<dyn ClientLeg>| {
        j.lock().unwrap().push(id.to_string());
    });
    Fixture {
        hub: WsMediaHub::new(Arc::new(Notes(notes.clone())), idle, join),
        notes,
        joined,
    }
}

impl Fixture {
    fn notes_of(&self, method: &str) -> Vec<Value> {
        self.notes
            .lock()
            .unwrap()
            .iter()
            .filter(|(m, _)| m == method)
            .map(|(_, p)| p.clone())
            .collect()
    }
}

fn raw(v: Value) -> Params {
    Some(serde_json::value::RawValue::from_string(v.to_string()).unwrap())
}

fn ok(r: HandlerResult) -> Value {
    match r {
        Ok(Reply::Json(v)) => v,
        other => panic!("{other:?}"),
    }
}

fn err(r: HandlerResult) -> IpcError {
    r.expect_err("an error")
}

// TestMediaOpenJoinsTheLegAndSaysWhatToSend
#[tokio::test(start_paused = true)]
async fn media_open_joins_the_leg_and_says_what_to_send() {
    let f = new_hub(Duration::from_secs(60));
    let m = ok(f
        .hub
        .open(raw(json!({"call_id": "c1", "codec": "pcmu"})))
        .await);
    assert_eq!(m["codec"], "pcmu");
    assert_eq!(m["frame_bytes"], PCMU_FRAME_BYTES);
    assert_eq!(m["frame_ms"], FRAME_MILLIS);
    assert_eq!(*f.joined.lock().unwrap(), ["c1"]);
}

// TestMediaOpenRefusals
#[tokio::test(start_paused = true)]
async fn media_open_refusals() {
    let f = new_hub(Duration::from_secs(60));
    for (p, code) in [
        (json!({"codec": "pcmu"}), "bad_params"),
        (json!({"call_id": "c1", "codec": "opus"}), "bad_params"),
    ] {
        let e = err(f.hub.open(raw(p.clone())).await);
        assert_eq!(e.code, code, "{p}: {e:?}");
    }
    ok(f.hub
        .open(raw(json!({"call_id": "c1", "codec": "pcmu"})))
        .await);
    let e = err(f
        .hub
        .open(raw(json!({"call_id": "c1", "codec": "pcmu"})))
        .await);
    assert_eq!(e.code, "already_open", "second open: {e:?}");
}

// DEV-796 C2: no more than MAX_LEGS legs at once; a freed slot opens again.
#[tokio::test(start_paused = true)]
async fn media_open_is_capped() {
    let f = new_hub(Duration::from_secs(60));
    for i in 0..16 {
        ok(f.hub
            .open(raw(json!({"call_id": format!("c{i}"), "codec": "pcmu"})))
            .await);
    }
    let e = err(f
        .hub
        .open(raw(json!({"call_id": "c16", "codec": "pcmu"})))
        .await);
    assert_eq!(e.code, "too_many", "{e:?}");
    ok(f.hub.close(raw(json!({"call_id": "c0"}))).await);
    ok(f.hub
        .open(raw(json!({"call_id": "c16", "codec": "pcmu"})))
        .await);
}

// DEV-796 C6: an over-long `d` is refused as a bad frame before it is decoded (invalid base64
// of that size used to come back as bad_params).
#[tokio::test(start_paused = true)]
async fn an_overlong_frame_is_refused_before_decoding() {
    let f = new_hub(Duration::from_secs(60));
    ok(f.hub
        .open(raw(json!({"call_id": "c1", "codec": "pcmu"})))
        .await);
    let e = err(f
        .hub
        .frame(raw(json!({"call_id": "c1", "d": "=".repeat(1 << 19)})))
        .await);
    assert_eq!(e.code, "bad_frame", "{e:?}");
}

// TestMediaFrameReachesTheDeviceSideAndAFrameForNobodyIsRefused
#[tokio::test(start_paused = true)]
async fn media_frame_reaches_the_device_side_and_a_frame_for_nobody_is_refused() {
    let f = new_hub(Duration::from_secs(60));
    ok(f.hub
        .open(raw(json!({"call_id": "c1", "codec": "l16"})))
        .await);
    let payload = vec![0u8; L16_FRAME_BYTES];
    ok(f.hub
        .frame(raw(json!({"call_id": "c1", "d": base64_encode(&payload)})))
        .await);
    let got = recv(&f.hub.leg("c1").unwrap(), Duration::from_secs(1))
        .await
        .expect("no frame");
    assert_eq!(got.len(), L16_FRAME_BYTES, "size");
    let e = err(f
        .hub
        .frame(raw(json!({"call_id": "nope", "d": "AAAA"})))
        .await);
    assert_eq!(e.code, "unknown_session", "{e:?}");
    let e = err(f.hub.frame(raw(json!({"call_id": "c1", "d": "***"}))).await);
    assert_eq!(e.code, "bad_params", "{e:?}");
    let e = err(f
        .hub
        .frame(raw(json!({"call_id": "c1", "d": "AAAA"})))
        .await);
    assert_eq!(e.code, "bad_frame", "{e:?}");
}

// TestDeviceAudioIsSentToPythonAsMediaFrameNotifications
#[tokio::test(start_paused = true)]
async fn device_audio_is_sent_to_python_as_media_frame_notifications() {
    let f = new_hub(Duration::from_secs(60));
    ok(f.hub
        .open(raw(json!({"call_id": "c1", "codec": "pcmu"})))
        .await);
    ClientLeg::input(&*f.hub.leg("c1").unwrap())
        .send(vec![0u8; L16_FRAME_BYTES])
        .await
        .unwrap();
    wait_for("media_frame", || !f.notes_of("media_frame").is_empty()).await;
    let ns = f.notes_of("media_frame");
    assert!(ns.len() == 1 && ns[0]["call_id"] == "c1", "notes {ns:?}");
    let b = base64_decode(ns[0]["d"].as_str().unwrap()).unwrap();
    assert_eq!(b.len(), PCMU_FRAME_BYTES, "payload size");
}

// TestMediaCloseTellsPythonOnceAndForgetsTheLeg
#[tokio::test(start_paused = true)]
async fn media_close_tells_python_once_and_forgets_the_leg() {
    let f = new_hub(Duration::from_secs(60));
    ok(f.hub
        .open(raw(json!({"call_id": "c1", "codec": "pcmu"})))
        .await);
    ok(f.hub
        .close(raw(json!({"call_id": "c1", "reason": "user"})))
        .await);
    assert_eq!(
        f.notes_of("media_closed").len(),
        1,
        "{:?}",
        f.notes.lock().unwrap()
    );
    assert!(f.hub.leg("c1").is_none(), "leg kept");
    // closing again, or a frame after, is harmless
    ok(f.hub.close(raw(json!({"call_id": "c1"}))).await);
    ok(f.hub
        .open(raw(json!({"call_id": "c1", "codec": "pcmu"})))
        .await);
}

// TestAnIdleLegTellsPythonItClosed
#[tokio::test(start_paused = true)]
async fn an_idle_leg_tells_python_it_closed() {
    let f = new_hub(ms(40));
    ok(f.hub
        .open(raw(json!({"call_id": "c1", "codec": "pcmu"})))
        .await);
    wait_for("media_closed", || !f.notes_of("media_closed").is_empty()).await;
    let ns = f.notes_of("media_closed");
    assert!(ns.len() == 1 && ns[0]["reason"] == "idle", "notes {ns:?}");
}

// ---- Rust-only --------------------------------------------------------------------------------------

// A callback that panics does not stop the callbacks after it, nor the close.
#[tokio::test(start_paused = true)]
async fn a_panicking_close_callback_does_not_stop_the_others() {
    let l = Leg::new(CODEC_L16, Duration::from_secs(60), no_send()).unwrap();
    let ran = Arc::new(Mutex::new(Vec::new()));
    let r = ran.clone();
    l.on_closed(move |_| -> Fut {
        r.lock().unwrap().push("first");
        Box::pin(async {})
    })
    .await;
    l.on_closed(|_| -> Fut { Box::pin(async { panic!("a callback bug") }) })
        .await;
    ClientLeg::on_disconnected(&*l, Box::new(|| panic!("a sync callback bug")));
    let r = ran.clone();
    ClientLeg::on_disconnected(
        &*l,
        Box::new(move || {
            r.lock().unwrap().push("last");
        }),
    );
    l.close("x").await;
    assert_eq!(*ran.lock().unwrap(), ["first", "last"]);
    assert!(l.is_closed());
    // registering a panicking callback on a closed leg is survivable as well
    ClientLeg::on_disconnected(&*l, Box::new(|| panic!("late bug")));
    l.on_closed(|_| -> Fut { Box::pin(async { panic!("late async bug") }) })
        .await;
}

// The two tasks of a leg end when it closes: nothing keeps the leg alive.
#[tokio::test(start_paused = true)]
async fn closing_a_leg_ends_its_tasks() {
    let l = Leg::new(CODEC_L16, Duration::from_secs(60), no_send()).unwrap();
    let weak = Arc::downgrade(&l);
    l.close("x").await;
    drop(l);
    wait_for("the leg to be released", || weak.upgrade().is_none()).await;
}

// A leg is not made outside a runtime: a caller bug, a panic, never a `bad_params` (R1-199).
#[test]
#[should_panic(expected = "no reactor running")]
fn a_leg_outside_a_runtime_is_a_caller_bug_and_panics() {
    let _ = Leg::new(CODEC_L16, ms(10), no_send());
}

// A join that panics closes the leg and tells the caller; the id is free again.
#[tokio::test(start_paused = true)]
async fn a_panicking_join_closes_the_leg_and_frees_the_id() {
    let notes = Arc::new(Mutex::new(Vec::new()));
    let boom = Arc::new(AtomicBool::new(true));
    let b = boom.clone();
    let join: JoinFn = Arc::new(move |_id: &str, _l: Arc<dyn ClientLeg>| {
        if b.load(Ordering::SeqCst) {
            panic!("join bug");
        }
    });
    let hub = WsMediaHub::new(
        Arc::new(Notes(notes.clone())),
        Duration::from_secs(60),
        join,
    );
    let e = err(hub
        .open(raw(json!({"call_id": "c1", "codec": "l16"})))
        .await);
    assert_eq!(e.code, "internal_error", "{e:?}");
    assert!(hub.leg("c1").is_none(), "the failed leg was kept");
    assert_eq!(notes.lock().unwrap().len(), 1, "media_closed was not sent");
    assert_eq!(notes.lock().unwrap()[0].1["reason"], "error");
    boom.store(false, Ordering::SeqCst);
    ok(hub
        .open(raw(json!({"call_id": "c1", "codec": "l16"})))
        .await);
}

// Hostile media_frame input: oversized, malformed and binary-ish payloads are refused, never panic.
#[tokio::test(start_paused = true)]
async fn hostile_frames_are_refused_without_a_panic() {
    let f = new_hub(Duration::from_secs(60));
    ok(f.hub
        .open(raw(json!({"call_id": "c1", "codec": "pcmu"})))
        .await);
    let huge = "A".repeat(4 << 20);
    let e = err(f.hub.frame(raw(json!({"call_id": "c1", "d": huge}))).await);
    assert_eq!(e.code, "bad_frame");
    for (d, code) in [
        ("=", "bad_params"),
        ("====", "bad_params"),
        ("AA==AA==", "bad_params"),
        ("\u{0}", "bad_params"),
        ("é", "bad_params"),
        ("A\nA\nA\nA", "bad_frame"),
        ("\u{2028}", "bad_params"),
    ] {
        let e = err(f.hub.frame(raw(json!({"call_id": "c1", "d": d}))).await);
        assert_eq!(e.code, code, "{d:?}: {e:?}");
    }
    // a flood of good frames neither blocks nor grows without bound: the device channel holds 8
    let good = base64_encode(&mu_frame_of_silence());
    for _ in 0..1000 {
        ok(f.hub.frame(raw(json!({"call_id": "c1", "d": good}))).await);
    }
    let leg = f.hub.leg("c1").unwrap();
    let mut n = 0;
    while recv(&leg, ms(5)).await.is_some() {
        n += 1;
    }
    assert_eq!(n, 8, "the device channel is Go's 8");
}

// The downlink is ordered and a slow notification does not reorder or lose the queued frames
// (up to the channel's 8).
#[tokio::test(start_paused = true)]
async fn downlink_frames_keep_their_order_through_a_slow_send() {
    let seen = Arc::new(Mutex::new(Vec::<u8>::new()));
    let s = seen.clone();
    let send: SendFn = Arc::new(move |p: Vec<u8>| -> Fut {
        let s = s.clone();
        Box::pin(async move {
            sleep(ms(5)).await;
            s.lock().unwrap().push(p[0]);
        })
    });
    let l = Leg::new(CODEC_L16, Duration::from_secs(60), send).unwrap();
    let input = ClientLeg::input(&*l);
    for i in 0..6u8 {
        input.send(vec![i; L16_FRAME_BYTES]).await.unwrap();
    }
    wait_for("six frames", || seen.lock().unwrap().len() == 6).await;
    assert_eq!(*seen.lock().unwrap(), [0, 1, 2, 3, 4, 5]);
    l.close("t").await;
}

// TestTwoDoors_AwayMediaLegsAreKeptPerCall (two_doors_test.go)
#[tokio::test(start_paused = true)]
async fn two_doors_away_media_legs_are_kept_per_call() {
    let f = new_hub(Duration::from_secs(60));
    for c in ["gate-1", "door-1"] {
        ok(f.hub.open(raw(json!({"call_id": c, "codec": "l16"}))).await);
    }
    assert_eq!(f.joined.lock().unwrap().len(), 2, "joined {:?}", f.joined);
    let e = err(f
        .hub
        .open(raw(json!({"call_id": "gate-1", "codec": "l16"})))
        .await);
    assert_eq!(e.code, "already_open", "a second open of gate-1 = {e:?}");
    // an uplink frame for the gate reaches the gate's leg only
    let payload = vec![0u8; L16_FRAME_BYTES];
    let frame = || raw(json!({"call_id": "gate-1", "d": base64_encode(&payload)}));
    ok(f.hub.frame(frame()).await);
    assert!(
        recv(&f.hub.leg("gate-1").unwrap(), Duration::from_secs(1))
            .await
            .is_some(),
        "the gate's frame never arrived"
    );
    assert!(
        recv(&f.hub.leg("door-1").unwrap(), ms(30)).await.is_none(),
        "the gate's frame arrived on the entrance's leg"
    );
    // closing the gate's leg tells Python once, about the gate, and leaves the entrance's open
    ok(f.hub
        .close(raw(json!({"call_id": "gate-1", "reason": "user"})))
        .await);
    let ns = f.notes_of("media_closed");
    assert!(
        ns.len() == 1 && ns[0]["call_id"] == "gate-1",
        "media_closed notes {ns:?}"
    );
    assert!(
        f.hub.leg("door-1").is_some(),
        "the entrance's leg closed with the gate's"
    );
    ok(f.hub
        .frame(raw(
            json!({"call_id": "door-1", "d": base64_encode(&payload)}),
        ))
        .await);
    let e = err(f.hub.frame(frame()).await);
    assert_eq!(
        e.code, "unknown_session",
        "a frame for the closed gate call = {e:?}"
    );
}

// Rust-only (PARITY.md 160): a panic in `send` closes the leg with reason `error` instead of
// killing the downlink task silently while the uplink keeps the idle clock quiet.
#[tokio::test(start_paused = true)]
async fn a_panic_in_send_closes_the_leg_with_reason_error() {
    let send: SendFn = Arc::new(|_p: Vec<u8>| -> Fut { panic!("a send bug") });
    let l = Leg::new(CODEC_L16, Duration::from_secs(60), send).unwrap();
    let reason = Arc::new(Mutex::new(None::<String>));
    let r = reason.clone();
    l.on_closed(move |why| -> Fut {
        *r.lock().unwrap() = Some(why);
        Box::pin(async {})
    })
    .await;
    ClientLeg::input(&*l).send(l16_frame(1)).await.unwrap();
    wait_for("the error close", || reason.lock().unwrap().is_some()).await;
    assert_eq!(reason.lock().unwrap().as_deref(), Some("error"));
    assert_eq!(l.push(&l16_frame(1)), Err(PushError::Closed));
    // and a send whose future panics (not the call) is the same
    let send: SendFn = Arc::new(|_p: Vec<u8>| -> Fut { Box::pin(async { panic!("late bug") }) });
    let l = Leg::new(CODEC_L16, Duration::from_secs(60), send).unwrap();
    ClientLeg::input(&*l).send(l16_frame(1)).await.unwrap();
    wait_for("the second error close", || l.is_closed()).await;
}

// ---- Phase R round 1, group G ------------------------------------------------------------------

/// R1-34: a `close` whose caller is dropped in the middle still runs the callbacks it had not
/// reached (a second `close` returns at once, so nothing else would).
#[tokio::test(start_paused = true)]
async fn a_close_torn_down_mid_callbacks_still_runs_the_rest() {
    let l = Leg::new(CODEC_L16, Duration::from_secs(60), no_send()).unwrap();
    let ran = Arc::new(Mutex::new(Vec::<&'static str>::new()));
    for name in ["first", "second", "third"] {
        let r = ran.clone();
        l.on_closed(move |_| -> Fut {
            Box::pin(async move {
                if name == "first" {
                    sleep(ms(100)).await;
                }
                r.lock().unwrap().push(name);
            })
        })
        .await;
    }
    let l2 = l.clone();
    let h = tokio::spawn(async move { l2.close("x").await });
    sleep(ms(10)).await;
    h.abort(); // the caller is gone while "first" is still sleeping
    sleep(ms(300)).await;
    assert_eq!(*ran.lock().unwrap(), ["first", "second", "third"]);
}

/// Audit (group G): a downlink frame of the wrong size is dropped, not encoded and sent; the
/// next good frame still goes (mutation: dropping the size check let the short one through).
#[tokio::test(start_paused = true)]
async fn a_downlink_frame_of_the_wrong_size_is_dropped() {
    let s = Arc::new(Sink::default());
    let l = Leg::new(CODEC_L16, Duration::from_secs(60), s.send_fn()).unwrap();
    let tx = ClientLeg::input(&*l);
    tx.send(vec![1u8; 7]).await.unwrap();
    tx.send(l16_frame(500)).await.unwrap();
    wait_for("the good frame", || !s.got().is_empty()).await;
    sleep(ms(50)).await;
    assert_eq!(s.got(), vec![l16_frame(500)], "the short frame was sent");
    l.close("test").await;
}

// ---- DEV-811 P3: the per-leg counters, behind `stats: true` -------------------------------------------

// off by default: no snapshot, and the leg behaves as before
#[tokio::test(start_paused = true)]
async fn leg_counters_are_off_until_enabled() {
    let l = Leg::new(CODEC_L16, Duration::from_secs(60), no_send()).unwrap();
    l.push(&l16_frame(1)).unwrap();
    assert!(l.stats_snapshot().is_none());
    l.close("test").await;
}

// the counters: frames, a bad frame, full-channel drops, and the gap numbers (virtual clock)
#[tokio::test(start_paused = true)]
async fn leg_counters_count_frames_bad_frames_drops_and_gaps() {
    let l = Leg::new(CODEC_L16, Duration::from_secs(60), no_send()).unwrap();
    l.enable_stats();
    // 20 frames 40 ms apart, one 300 ms stall, one wrong-sized frame
    for _ in 0..20 {
        l.push(&l16_frame(1)).unwrap();
        sleep(ms(40)).await;
    }
    sleep(ms(260)).await; // the stall: the next gap is 300 ms
    l.push(&l16_frame(1)).unwrap();
    assert_eq!(l.push(&[0u8; 7]), Err(PushError::BadFrame));
    let s = l.stats_snapshot().expect("counting is on");
    assert_eq!(s["uplink_frames"], 21);
    assert_eq!(s["uplink_bad_frame"], 1);
    // nobody reads the device channel (8 slots): 21 - 8 frames were dropped
    assert_eq!(s["uplink_full_drops"], 13);
    assert_eq!(s["uplink_gap_ms_max"], 300);
    // 20 gaps: 19 of 40 ms and one of 300 ms: p95 is the 19th..20th of 20 -> need ceil(19.0)=19 -> the 40 ms bin's bound 45, capped by the max
    assert_eq!(s["uplink_gap_ms_p95"], 45);
    assert_eq!(s["downlink_frames"], 0);
    l.close("test").await;
}

// the p95 sees a stall that is more than 5 % of the gaps
#[tokio::test(start_paused = true)]
async fn the_gap_p95_follows_a_stall_that_is_more_than_five_percent() {
    let l = Leg::new(CODEC_L16, Duration::from_secs(60), no_send()).unwrap();
    l.enable_stats();
    for i in 0..10 {
        l.push(&l16_frame(1)).unwrap();
        sleep(ms(if i % 5 == 4 { 150 } else { 40 })).await; // two gaps of 150 ms in 10
    }
    l.push(&l16_frame(1)).unwrap();
    let s = l.stats_snapshot().unwrap();
    assert_eq!(s["uplink_gap_ms_max"], 150);
    assert_eq!(s["uplink_gap_ms_p95"], 150, "{s}");
    l.close("test").await;
}

// the downlink counts frames sent to the phone and the slowest send
#[tokio::test(start_paused = true)]
async fn the_downlink_counts_frames_and_the_slowest_send() {
    let sink = Arc::new(Sink::default());
    let l = Leg::new(CODEC_L16, Duration::from_secs(60), sink.send_fn()).unwrap();
    l.enable_stats();
    let tx = ClientLeg::input(&*l);
    for _ in 0..3 {
        tx.send(l16_frame(2)).await.unwrap();
    }
    wait_for("three frames sent", || sink.got().len() == 3).await;
    let s = l.stats_snapshot().unwrap();
    assert_eq!(s["downlink_frames"], 3);
    assert_eq!(s["downlink_send_ms_max"], 0);
    l.close("test").await;
}

// the hub: `stats: true` puts the counters in media_closed; without it the notification is the old one
#[tokio::test(start_paused = true)]
async fn media_closed_carries_stats_only_when_the_open_asked() {
    let f = new_hub(Duration::from_secs(60));
    ok(f.hub
        .open(raw(json!({"call_id": "s1", "codec": "l16", "stats": true})))
        .await);
    ok(f.hub
        .open(raw(json!({"call_id": "s2", "codec": "l16", "stats": "yes"})))
        .await);
    ok(f.hub
        .open(raw(json!({"call_id": "s3", "codec": "l16"})))
        .await);
    ok(f.hub
        .frame(raw(json!({"call_id": "s1", "d": base64_encode(&vec![0u8; L16_FRAME_BYTES])})))
        .await);
    for id in ["s1", "s2", "s3"] {
        ok(f.hub.close(raw(json!({"call_id": id, "reason": "bye"}))).await);
    }
    let closed = f.notes_of("media_closed");
    assert_eq!(closed.len(), 3);
    assert_eq!(closed[0]["stats"]["uplink_frames"], 1, "{}", closed[0]);
    assert_eq!(closed[1], json!({"call_id": "s2", "reason": "bye"}));
    assert_eq!(closed[2], json!({"call_id": "s3", "reason": "bye"}));
}
