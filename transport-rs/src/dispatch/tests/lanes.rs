use super::*;

// TestTwoDoors_LaneKeysOfEveryKeyedCommand, TestTwoDoors_LaneKeysSeparateCallsAndDevicesAndNeverCollide
#[test]
fn lane_keys_of_every_keyed_command() {
    assert_eq!(
        configure_key(r#"{"driver":"gate_sip_server","config":{"device_id":"d1"}}"#),
        "device_id:d1"
    );
    assert_eq!(configure_key(r#"{"config":{"host":"h1"}}"#), "device_id:h1");
    assert_eq!(
        configure_key(r#"{"config":{"nvr_host":"n1"}}"#),
        "device_id:n1"
    );
    assert_eq!(device_id_key(r#"{"device_id":"d1"}"#), "device_id:d1");
    assert_eq!(call_id_key(r#"{"call_id":"c1"}"#), "call_id:c1");
    // a call and a device with the same text never share a lane
    assert_ne!(
        call_id_key(r#"{"call_id":"x"}"#),
        device_id_key(r#"{"device_id":"x"}"#)
    );
    // R2-57 (1): the rest of Go's table (`two_doors_test.go:822-855`)
    assert_eq!(
        configure_key(r#"{"driver":"x","config":{"device_id":"","host":"h"}}"#),
        "device_id:h",
        "an empty device_id falls back to host"
    );
    assert_eq!(
        configure_key(r#"{"driver":"x","config":{"device_id":7,"host":"h"}}"#),
        "device_id:h",
        "a non-string device_id falls back to host"
    );
    for no_lane in [
        r#"{"driver":"x","config":{"port":1}}"#,
        r#"{"driver":"x"}"#,
        r#"{"driver":"x","device_id":"gate"}"#,
    ] {
        assert_eq!(configure_key(no_lane), "", "{no_lane}");
    }
    assert_eq!(
        configure_key(r#"{"config":{"device_id":"gate"}}"#),
        device_id_key(r#"{"device_id":"gate"}"#),
        "a configure and a deconfigure of one door share a lane"
    );
    assert_ne!(
        configure_key(r#"{"config":{"device_id":"gate"}}"#),
        configure_key(r#"{"config":{"device_id":"entrance"}}"#),
        "two doors share a configure lane"
    );
    // R2-55: a lone surrogate escape decodes to U+FFFD (as in Go) and still has a lane, the same
    // one `configure_key` gives
    assert_eq!(
        device_id_key(r#"{"device_id":"a\ud800"}"#),
        "device_id:a\u{fffd}"
    );
    assert_eq!(call_id_key(r#"{"call_id":"a\ud800"}"#), "call_id:a\u{fffd}");
    assert_eq!(
        configure_key(r#"{"config":{"device_id":"a\ud800"}}"#),
        "device_id:a\u{fffd}"
    );
    for k in [
        configure_key("not json"),
        device_id_key("{}"),
        call_id_key("null"),
    ] {
        assert_eq!(k, "", "no lane for a request without an id");
    }
}

// TestTwoDoors_TwoConfiguresOfTheSameDoorEndWithExactlyOneRegisteredDriver: two concurrent
// configures of one device id (real gate_sip_server on port 0): one wins, one is refused,
// and exactly one driver is registered.
#[tokio::test]
async fn two_configures_of_the_same_door_end_with_one_registered_driver() {
    let h = Hub::detached();
    let cfg = r#"{"driver":"gate_sip_server","config":{"device_id":"door","sip_listen":"127.0.0.1:0","rtp_port":0,"password":"12345678"}}"#;
    let (r1, r2) = tokio::join!(h.configure(p(cfg)), h.configure(p(cfg)));
    let codes: Vec<_> = [&r1, &r2]
        .iter()
        .map(|r| r.as_ref().err().map(|e| e.code.clone()))
        .collect();
    assert_eq!(codes.iter().filter(|c| c.is_none()).count(), 1, "{codes:?}");
    assert_eq!(
        codes.iter().flatten().next().map(String::as_str),
        Some("already_configured")
    );
    assert_eq!(h.all().len(), 1);
    h.shutdown().await;
}

// R1-1: `shutdown` is once, and every caller waits for it (Go: `sync.Once`). The second
// caller arrives while the first is still stopping the driver and the registry is drained.
// Mutation: drop the `OnceCell` (the second call then returns at once).
#[tokio::test]
async fn a_second_shutdown_waits_for_the_first_to_stop_every_driver() {
    let h = Hub::detached();
    let f = Fake::new("slow");
    *f.stop_delay.lock().unwrap() = Duration::from_millis(300);
    add(&h, &f).await;
    let first = {
        let h = h.clone();
        tokio::spawn(async move { h.shutdown().await })
    };
    tokio::time::sleep(Duration::from_millis(60)).await; // the first is inside `stop`
    assert!(!f.stopped.load(Ordering::SeqCst));
    h.shutdown().await;
    assert!(
        f.stopped.load(Ordering::SeqCst),
        "the second shutdown returned while the driver was still stopping"
    );
    first.await.unwrap();
    assert_eq!(f.stops.load(Ordering::SeqCst), 1, "stopped once");
}

#[test]
fn keys_match_go() {
    assert_eq!(call_id_key(r#"{"call_id":"c1"}"#), "call_id:c1");
    assert_eq!(call_id_key(r#"{"call_id":""}"#), "");
    assert_eq!(call_id_key(""), "");
    assert_eq!(device_id_key("null"), "");
    assert_eq!(
        configure_key(r#"{"driver":"x","config":{"host":"h"}}"#),
        "device_id:h"
    );
    assert_eq!(
        configure_key(r#"{"config":{"device_id":"d","host":"h"}}"#),
        "device_id:d"
    );
    assert_eq!(configure_key(r#"{"config":"x"}"#), "");
}

// R3A-6 (R2-55): a duplicate member is won by the LAST one, as in a Go map. Mutation: `.rev()`
// removed from `param_key` (the first wins).
#[test]
fn the_last_duplicate_member_names_the_lane() {
    assert_eq!(
        device_id_key(r#"{"device_id":"a","device_id":"b"}"#),
        "device_id:b"
    );
    assert_eq!(
        call_id_key(r#"{"call_id":"x","call_id":""}"#),
        "",
        "an empty last member is no lane, whatever came before"
    );
}

// ---- the lanes over the real registration (R2-1) ------------------------------------------
//
// Go pins the dispatch class of every method twice: five behaviour tests over a mirror of
// `main()` and a regular expression over the source of `main.go`. Here the tests go through
// `register` itself, and the table is read from the live server.

use std::collections::HashMap as Map;
use tokio::sync::Semaphore;

/// `gateTest`: what the gated fake driver records, and the gates it waits at. A gate is a
/// semaphore with no permit; adding permits releases whoever waits (and anyone later).
#[derive(Default)]
struct GateTest {
    starts: Map<String, u32>,
    stopping: Map<String, u32>,
    dials: Map<String, u32>,
    answers: Vec<String>,
    gates: Map<String, Arc<Semaphore>>,
}

type G = Arc<Mutex<GateTest>>;

fn gate(g: &G, name: &str) -> Arc<Semaphore> {
    g.lock()
        .unwrap()
        .gates
        .entry(name.to_string())
        .or_insert_with(|| Arc::new(Semaphore::new(0)))
        .clone()
}

fn count(g: &G, f: impl Fn(&GateTest) -> u32) -> u32 {
    f(&g.lock().unwrap())
}

async fn wait_at(g: &G, name: &str) {
    let sem = g.lock().unwrap().gates.get(name).cloned();
    if let Some(s) = sem {
        s.acquire().await.unwrap().forget();
        s.add_permits(1); // a released gate stays open
    }
}

/// `gatedDriver`: `Start`, `Stop` and `Dial` can be held at a gate named for the host.
struct Gated {
    g: G,
    host: std::sync::OnceLock<String>,
    started: std::sync::atomic::AtomicBool,
    stops: AtomicU32,
    sink: Mutex<Option<Arc<dyn EventSink>>>,
}

impl Gated {
    fn host(&self) -> String {
        self.host.get().cloned().unwrap_or_default()
    }
}

impl Driver for Gated {
    fn configure(&mut self, cfg: &serde_json::Map<String, Value>) -> Result<(), DriverError> {
        let _ = self
            .host
            .set(cfg["host"].as_str().unwrap_or("").to_string());
        Ok(())
    }
    fn capabilities(&self) -> Capabilities {
        Capabilities::default()
    }
    fn start<'a>(
        &'a self,
        _: &'a CancelToken,
        events: Arc<dyn EventSink>,
    ) -> BoxFuture<'a, Result<(), DriverError>> {
        *self.sink.lock().unwrap() = Some(events);
        *self
            .g
            .lock()
            .unwrap()
            .starts
            .entry(self.host())
            .or_default() += 1;
        Box::pin(async move {
            wait_at(&self.g, &self.host()).await;
            self.started.store(true, Ordering::SeqCst);
            Ok(())
        })
    }
    fn stop(&self) -> BoxFuture<'_, Result<(), DriverError>> {
        self.stops.fetch_add(1, Ordering::SeqCst);
        *self
            .g
            .lock()
            .unwrap()
            .stopping
            .entry(self.host())
            .or_default() += 1;
        Box::pin(async move {
            wait_at(&self.g, &format!("stop:{}", self.host())).await;
            Ok(())
        })
    }
    fn dial<'a>(
        &'a self,
        _: &'a CancelToken,
        _: MediaTarget,
    ) -> BoxFuture<'a, Result<(), DriverError>> {
        *self.g.lock().unwrap().dials.entry(self.host()).or_default() += 1;
        Box::pin(async move {
            wait_at(&self.g, &format!("dial:{}", self.host())).await;
            Ok(())
        })
    }
    fn answer<'a>(
        &'a self,
        _: &'a CancelToken,
        _: MediaTarget,
    ) -> BoxFuture<'a, Result<(), DriverError>> {
        self.g.lock().unwrap().answers.push(self.host());
        Box::pin(async { Ok(()) })
    }
    fn open_relay<'a>(&'a self, _: &'a CancelToken) -> BoxFuture<'a, Result<(), DriverError>> {
        Box::pin(async { Ok(()) })
    }
}

/// `twoDoorWire`: the real `register` over a socket, with the gated driver under the key
/// `two_door_test_driver`, and every driver it makes.
struct TwoDoors {
    rig: Rig,
    g: G,
    made: Arc<Mutex<Vec<Arc<Gated>>>>,
}

/// Lets `configure` build drivers that a test can look at afterwards (the hub owns a
/// `Box<dyn Driver>` that it turns into an `Arc`, so the fake is shared through a thin wrapper).
struct Shared(Arc<Gated>);

impl Driver for Shared {
    fn configure(&mut self, cfg: &serde_json::Map<String, Value>) -> Result<(), DriverError> {
        let _ = self
            .0
            .host
            .set(cfg["host"].as_str().unwrap_or("").to_string());
        Ok(())
    }
    fn capabilities(&self) -> Capabilities {
        self.0.capabilities()
    }
    fn start<'a>(
        &'a self,
        c: &'a CancelToken,
        e: Arc<dyn EventSink>,
    ) -> BoxFuture<'a, Result<(), DriverError>> {
        self.0.start(c, e)
    }
    fn stop(&self) -> BoxFuture<'_, Result<(), DriverError>> {
        self.0.stop()
    }
    fn dial<'a>(
        &'a self,
        c: &'a CancelToken,
        t: MediaTarget,
    ) -> BoxFuture<'a, Result<(), DriverError>> {
        self.0.dial(c, t)
    }
    fn answer<'a>(
        &'a self,
        c: &'a CancelToken,
        t: MediaTarget,
    ) -> BoxFuture<'a, Result<(), DriverError>> {
        self.0.answer(c, t)
    }
    fn open_relay<'a>(&'a self, c: &'a CancelToken) -> BoxFuture<'a, Result<(), DriverError>> {
        self.0.open_relay(c)
    }
}

impl TwoDoors {
    async fn new(tag: &str) -> TwoDoors {
        let rig = Rig::new(tag).await;
        let g: G = Arc::default();
        let made: Arc<Mutex<Vec<Arc<Gated>>>> = Arc::default();
        let (g2, m2) = (g.clone(), made.clone());
        rig.hub.add_test_driver(
            "two_door_test_driver",
            Arc::new(move || {
                // the hub configures the box it is given; the test keeps a handle on the same
                // object once it has a host (see `Shared`)
                let d = Arc::new(Gated {
                    g: g2.clone(),
                    host: Default::default(),
                    started: Default::default(),
                    stops: AtomicU32::new(0),
                    sink: Mutex::new(None),
                });
                m2.lock().unwrap().push(d.clone());
                Box::new(Shared(d))
            }),
        );
        TwoDoors { rig, g, made }
    }

    async fn configure(&mut self, id: i64, host: &str) {
        let p = configure_json(host);
        self.rig.send(id, "configure", &p).await;
    }

    async fn configure_both(&mut self) {
        self.configure(10, "gate").await;
        self.configure(11, "entrance").await;
        let (_, by_id, _) = self.rig.replies(2).await;
        assert!(by_id[&10].get("error").is_none() && by_id[&11].get("error").is_none());
    }
}

fn configure_json(host: &str) -> String {
    format!(r#"{{"driver":"two_door_test_driver","config":{{"host":"{host}"}}}}"#)
}

async fn eventually(f: impl Fn() -> bool) {
    for _ in 0..200 {
        if f() {
            return;
        }
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    panic!("the condition never held");
}

fn code_of(v: &Value) -> Option<&str> {
    v["error"]["code"].as_str()
}

// TestTwoDoors_ConfigureOfOneDoorBlockedInStartDoesNotHoldUpTheOther
#[tokio::test]
async fn configure_of_one_door_blocked_in_start_does_not_hold_up_the_other() {
    let mut w = TwoDoors::new("lane-a").await;
    let release = gate(&w.g, "gate");
    w.configure(1, "gate").await;
    w.configure(2, "entrance").await;
    let (ids, by_id, _) = w.rig.replies(1).await;
    assert!(
        ids[0] == 2 && by_id[&2].get("error").is_none(),
        "first reply {ids:?} {by_id:?}"
    );
    release.add_permits(1);
    let (_, by_id, _) = w.rig.replies(1).await;
    assert!(by_id[&1].get("error").is_none(), "{:?}", by_id[&1]);
    assert!(w.rig.hub.driver("gate").is_some() && w.rig.hub.driver("entrance").is_some());
}

// TestTwoDoors_TwoConfiguresOfTheSameDoorEndWithExactlyOneRegisteredDriver, on the wire: the
// lane queues the second configure behind the first, which has registered by then, so the
// second is refused BEFORE it is started (R2-14: "refused before anything is started" is the
// doc comment of `configure`, and the `put_if_absent` net below it would hide a missing check).
#[tokio::test]
async fn a_second_configure_of_a_door_is_refused_before_anything_is_started() {
    let mut w = TwoDoors::new("lane-b").await;
    let release = gate(&w.g, "gate");
    w.configure(1, "gate").await;
    w.configure(2, "gate").await;
    tokio::time::sleep(Duration::from_millis(60)).await;
    release.add_permits(1);
    let (_, by_id, _) = w.rig.replies(2).await;
    let (ok, refused): (Vec<_>, Vec<_>) = by_id.values().partition(|m| m.get("error").is_none());
    assert_eq!((ok.len(), refused.len()), (1, 1), "{by_id:?}");
    assert_eq!(code_of(refused[0]), Some("already_configured"));
    assert_eq!(w.rig.hub.all().len(), 1);
    let made = w.made.lock().unwrap().clone();
    assert_eq!(made.len(), 2, "the refused one was still built");
    assert!(made[0].started.load(Ordering::SeqCst));
    assert!(
        !made[1].started.load(Ordering::SeqCst),
        "the refused configure started its driver"
    );
    assert_eq!(count(&w.g, |g| g.starts["gate"]), 1);
    // the registry entry is the first driver: deconfiguring stops that one
    w.rig
        .send(3, "deconfigure", r#"{"device_id":"gate"}"#)
        .await;
    w.rig.replies(1).await;
    assert_eq!(
        (
            made[0].stops.load(Ordering::SeqCst),
            made[1].stops.load(Ordering::SeqCst)
        ),
        (1, 0)
    );
}

// The `put_if_absent` safety net itself: two configures of one door that RACE (no lane: the
// hub's `configure` called twice at once) both reach `start`; exactly one registers, and the
// loser is stopped, so no orphan keeps running (Go: "every started driver that did not win was
// stopped", `two_doors_test.go:738-757`; R2-14, mutation: no `d.stop()` on the losing branch).
#[tokio::test]
async fn racing_configures_of_one_door_leave_one_driver_and_stop_the_loser() {
    let w = TwoDoors::new("lane-c").await;
    let release = gate(&w.g, "gate");
    let h = w.rig.hub.clone();
    let p = |s: &str| Some(RawValue::from_string(s.to_string()).unwrap());
    let (h1, h2) = (h.clone(), h.clone());
    let a = tokio::spawn(async move { h1.configure(p(&configure_json("gate"))).await });
    let b = tokio::spawn(async move { h2.configure(p(&configure_json("gate"))).await });
    eventually(|| count(&w.g, |g| *g.starts.get("gate").unwrap_or(&0)) == 2).await; // both are inside `start`
    release.add_permits(1);
    let (ra, rb) = (a.await.unwrap(), b.await.unwrap());
    let codes: Vec<_> = [&ra, &rb]
        .iter()
        .map(|r| r.as_ref().err().map(|e| e.code.clone()))
        .collect();
    assert_eq!(codes.iter().filter(|c| c.is_none()).count(), 1, "{codes:?}");
    assert!(codes.contains(&Some("already_configured".to_string())));
    assert_eq!(h.all().len(), 1);
    let made = w.made.lock().unwrap().clone();
    let started = made
        .iter()
        .filter(|d| d.started.load(Ordering::SeqCst))
        .count();
    let stopped: u32 = made.iter().map(|d| d.stops.load(Ordering::SeqCst)).sum();
    assert_eq!(
        (started, stopped),
        (2, 1),
        "the loser of the race must be stopped and the winner not"
    );
}

// TestTwoDoors_ConfigureOfTheSameDoorIsSerialisedPerDeviceID (GO-11)
#[tokio::test]
async fn configure_of_the_same_door_is_serialised_per_device_id() {
    let mut w = TwoDoors::new("lane-d").await;
    let release = gate(&w.g, "gate");
    w.configure(1, "gate").await;
    eventually(|| count(&w.g, |g| *g.starts.get("gate").unwrap_or(&0)) == 1).await;
    w.configure(2, "gate").await;
    tokio::time::sleep(Duration::from_millis(80)).await; // time for a duplicate to reach `start`
    assert_eq!(
        count(&w.g, |g| g.starts["gate"]),
        1,
        "the duplicate was not queued behind the first configure"
    );
    release.add_permits(1);
    let (_, by_id, _) = w.rig.replies(2).await;
    assert!(by_id[&1].get("error").is_none(), "{:?}", by_id[&1]);
    assert_eq!(code_of(&by_id[&2]), Some("already_configured"));
    assert_eq!(count(&w.g, |g| g.starts["gate"]), 1);
}

// TestTwoDoors_ADeconfigureOfADoorQueuesBehindItsConfigure
#[tokio::test]
async fn a_deconfigure_of_a_door_queues_behind_its_configure() {
    let mut w = TwoDoors::new("lane-e").await;
    let release = gate(&w.g, "gate");
    w.configure(1, "gate").await;
    eventually(|| count(&w.g, |g| *g.starts.get("gate").unwrap_or(&0)) == 1).await;
    w.rig
        .send(2, "deconfigure", r#"{"device_id":"gate"}"#)
        .await;
    tokio::time::sleep(Duration::from_millis(60)).await;
    assert_eq!(
        count(&w.g, |g| *g.stopping.get("gate").unwrap_or(&0)),
        0,
        "the deconfigure ran while the configure was still in start"
    );
    release.add_permits(1);
    let (_, by_id, _) = w.rig.replies(2).await;
    assert!(by_id[&1].get("error").is_none() && by_id[&2].get("error").is_none());
}

// TestTwoDoors_DeconfigureLanesAreSerialisedPerDoorAndIndependentAcrossDoors
#[tokio::test]
async fn deconfigure_lanes_are_serialised_per_door_and_independent_across_doors() {
    let mut w = TwoDoors::new("lane-f").await;
    w.configure_both().await;
    let release = gate(&w.g, "stop:gate");
    w.rig
        .send(1, "deconfigure", r#"{"device_id":"gate"}"#)
        .await;
    eventually(|| count(&w.g, |g| *g.stopping.get("gate").unwrap_or(&0)) == 1).await;
    w.rig
        .send(2, "deconfigure", r#"{"device_id":"gate"}"#)
        .await;
    w.rig
        .send(3, "deconfigure", r#"{"device_id":"entrance"}"#)
        .await;
    let (ids, by_id, _) = w.rig.replies(1).await;
    assert!(
        ids[0] == 3 && by_id[&3].get("error").is_none(),
        "while the gate's stop was blocked the first reply was {ids:?}"
    );
    tokio::time::sleep(Duration::from_millis(40)).await;
    assert_eq!(
        count(&w.g, |g| g.stopping["gate"]),
        1,
        "a second deconfigure of the gate entered stop while the first was in flight"
    );
    release.add_permits(1);
    let (_, by_id, _) = w.rig.replies(2).await;
    assert!(by_id[&1].get("error").is_none(), "{:?}", by_id[&1]);
    assert_eq!(code_of(&by_id[&2]), Some("unknown_device"));
}

// TestTwoDoors_ADialInFlightOnOneDoorDoesNotHoldUpAnAnswerOrDialOnTheOther
#[tokio::test]
async fn a_dial_in_flight_on_one_door_does_not_hold_up_the_other() {
    let mut w = TwoDoors::new("lane-g").await;
    w.configure_both().await;
    let release = gate(&w.g, "dial:gate");
    w.rig
        .send(
            1,
            "dial",
            r#"{"device_id":"gate","host":"10.9.9.9","port":4000}"#,
        )
        .await;
    eventually(|| count(&w.g, |g| *g.dials.get("gate").unwrap_or(&0)) == 1).await;
    w.rig.send(2, "answer", r#"{"device_id":"entrance"}"#).await;
    w.rig
        .send(
            3,
            "dial",
            r#"{"device_id":"entrance","host":"10.9.9.8","port":4000}"#,
        )
        .await;
    let (ids, by_id, _) = w.rig.replies(2).await;
    assert!(
        by_id.contains_key(&2) && by_id.contains_key(&3) && !by_id.contains_key(&1),
        "while the gate's dial was blocked the replies were {ids:?}"
    );
    assert!(by_id[&2].get("error").is_none() && by_id[&3].get("error").is_none());
    assert_eq!(w.g.lock().unwrap().answers, ["entrance"]);
    release.add_permits(1);
    let (_, by_id, _) = w.rig.replies(1).await;
    assert!(by_id[&1].get("error").is_none(), "{:?}", by_id[&1]);
}

// TestTwoDoors_MainRegistersTheDispatchLanesTheTestsAssume: Go matches regular expressions over
// `main.go`; here the live server is asked for the class of every method `register` installs
// (18: `auth` belongs to the TCP gate), so a lane dropped or a handler moved onto the read loop
// fails here. The lane key of a keyed method is the one the Python client's params produce.
#[tokio::test]
async fn register_installs_every_method_in_the_dispatch_class_the_lanes_assume() {
    use crate::ipc::DispatchClass::{Inline, Keyed, Plain};
    let r = Rig::new("lane-table").await;
    let class = |m: &str, params: &str| r.srv.dispatch_class(m, params);
    let k = |s: &str| Some(Keyed(s.to_string()));
    let cfg = r#"{"driver":"x","config":{"device_id":"gate"}}"#;
    assert_eq!(class("configure", cfg), k("device_id:gate"));
    assert_eq!(
        class("deconfigure", r#"{"device_id":"gate"}"#),
        k("device_id:gate")
    );
    assert_eq!(
        class("external_ring", r#"{"device_id":"gate"}"#),
        k("device_id:gate")
    );
    for m in ["webrtc_offer", "webrtc_answer", "webrtc_ice"] {
        assert_eq!(class(m, r#"{"call_id":"c1"}"#), k("call_id:c1"), "{m}");
    }
    for m in [
        "dial",
        "answer",
        "open_relay",
        "hangup",
        "stream_url",
        "talk_health",
        "keepalive",
        "house_open",
        "house_close",
    ] {
        assert_eq!(class(m, r#"{"device_id":"gate"}"#), Some(Plain), "{m}");
    }
    for m in ["media_open", "media_frame", "media_close"] {
        assert_eq!(class(m, "{}"), Some(Inline), "{m}");
    }
    assert_eq!(
        class("auth", "{}"),
        None,
        "auth is the TCP gate's, not a handler"
    );
    assert_eq!(class("nope", "{}"), None);
    // and nothing but the media lane is inline
    let n = [
        "configure",
        "deconfigure",
        "external_ring",
        "webrtc_offer",
        "webrtc_answer",
        "webrtc_ice",
        "dial",
        "answer",
        "open_relay",
        "hangup",
        "stream_url",
        "talk_health",
        "keepalive",
        "house_open",
        "house_close",
        "media_open",
        "media_frame",
        "media_close",
    ]
    .iter()
    .filter(|m| class(m, "{}") == Some(Inline))
    .count();
    assert_eq!(n, 3);
}

// R2-108: the sequence does not die with the caller that started it. The first caller is
// dropped 60 ms into a 300 ms stop; the second still waits until the driver has stopped.
// Mutation: run `shutdown_inner` in the caller (an `OnceCell::get_or_init`).
#[tokio::test]
async fn a_dropped_first_shutdown_does_not_cancel_the_sequence() {
    let h = Hub::detached();
    let f = Fake::new("slow");
    *f.stop_delay.lock().unwrap() = Duration::from_millis(300);
    add(&h, &f).await;
    let first = {
        let h = h.clone();
        tokio::spawn(async move { h.shutdown().await })
    };
    tokio::time::sleep(Duration::from_millis(60)).await;
    first.abort();
    let _ = first.await;
    assert!(!f.stopped.load(Ordering::SeqCst));
    h.shutdown().await;
    assert!(
        f.stopped.load(Ordering::SeqCst),
        "the second shutdown returned before the driver had stopped"
    );
    assert_eq!(f.stops.load(Ordering::SeqCst), 1);
}
