use super::*;

// TestEventSink_StateChangedIsForwardedAsCallStateChanged,
// TestEventSink_AvailabilityChangedIsForwardedAsDriverAvailability,
// TestEventSink_ListenStateChangedIsForwardedAsListenState,
// TestEventSink_DriverErrorNamesTheDevice, TestSinkForwarding_CapabilitiesChangedArrivesThroughEveryWrapper
#[tokio::test]
async fn the_sink_forwards_every_event_with_its_params() {
    let h = Hub::detached();
    let a = Fake::new("dev");
    add(&h, &a).await;
    let s = a.sink();
    s.availability_changed("dev", true, "ignored when available");
    s.availability_changed("dev", false, "down");
    s.listen_state_sink()
        .expect("listen state")
        .listen_state_changed("dev", "listening", "");
    s.call_reason_sink()
        .expect("call reason")
        .call_reason("dev", "c1", "ended", "bye");
    let caps = Capabilities {
        can_dial: true,
        ..Capabilities::default()
    };
    s.capabilities_sink()
        .expect("capabilities")
        .capabilities_changed("dev", &caps);
    h.events.driver_error("dev", "auth", "bad key");
    let n = notes(&h);
    assert_eq!(
        n[0],
        (
            "driver_availability".into(),
            json!({"device_id": "dev", "available": true, "reason": ""})
        )
    );
    assert_eq!(n[1].1["reason"], "down");
    assert_eq!(
        n[2],
        (
            "listen_state".into(),
            json!({"device_id": "dev", "state": "listening", "reason": ""})
        )
    );
    assert_eq!(
        n[3],
        (
            "call_reason".into(),
            json!({"device_id": "dev", "call_id": "c1", "kind": "ended", "reason": "bye"})
        )
    );
    assert_eq!(n[4].0, "capabilities_changed");
    assert_eq!(n[4].1["capabilities"]["CanDial"], true);
    assert_eq!(
        n[5],
        (
            "driver_error".into(),
            json!({"device_id": "dev", "code": "auth", "message": "bad key"})
        )
    );
}

// TestEventSink_StateChangedCarriesTheDeviceIdOfItsRing,
// TestEventSink_StateChangedForAnUnannouncedCallHasAnEmptyDeviceId,
// TestTwoDoors_InterleavedRingAnswerEndKeepsEachCallOnItsOwnDevice,
// TestTwoDoors_AnEndedCallIsForgottenButTheOtherDoorsStaysStamped
#[tokio::test]
async fn state_changes_are_stamped_with_the_device_of_their_ring() {
    let h = Hub::detached();
    let (a, b) = (Fake::new("A"), Fake::new("B"));
    add(&h, &a).await;
    add(&h, &b).await;
    a.sink().ringing("ca", "A");
    b.sink().ringing("cb", "B");
    b.sink().state_changed("cb", "answered");
    a.sink().state_changed("ca", "answered");
    a.sink().state_changed("ca", "ended");
    a.sink().state_changed("ca", "ended"); // forgotten: now unannounced
    b.sink().state_changed("cb", "ended");
    a.sink().state_changed("never", "answered");
    let dev: Vec<_> = notes(&h)
        .into_iter()
        .map(|(m, v)| {
            (
                m,
                v["call_id"].as_str().unwrap().to_string(),
                v["device_id"].as_str().unwrap().to_string(),
                v["state"].as_str().unwrap_or("").to_string(),
            )
        })
        .collect();
    // R2-57 (6): the state is checked too, and nothing is left in the call table
    let want = [
        ("ringing", "ca", "A", ""),
        ("ringing", "cb", "B", ""),
        ("call_state_changed", "cb", "B", "answered"),
        ("call_state_changed", "ca", "A", "answered"),
        ("call_state_changed", "ca", "A", "ended"),
        ("call_state_changed", "ca", "", "ended"),
        ("call_state_changed", "cb", "B", "ended"),
        ("call_state_changed", "never", "", "answered"),
    ];
    let want: Vec<_> = want
        .iter()
        .map(|(m, c, d, st)| (m.to_string(), c.to_string(), d.to_string(), st.to_string()))
        .collect();
    assert_eq!(dev, want);
    assert!(h.events.st().calls.is_empty(), "calls left in the table");
}

// TestEventSink_IsSafeForConcurrentDrivers; TestTwoDoors_ConcurrentDoorsNeverCrossStampEachOther
#[test]
fn the_sink_is_safe_for_concurrent_drivers() {
    let sink = IpcEventSink::detached();
    let mut hs = Vec::new();
    for d in 0..8 {
        let s = sink.clone();
        hs.push(std::thread::spawn(move || {
            let dev = format!("door{d}");
            // R1-101: 8 x 40 x 3 = 960 notes fit the queue (1024), so every one is checked
            for i in 0..40 {
                let call = format!("{dev}-{i}");
                s.ringing(&call, &dev);
                s.state_changed(&call, &dev, "answered");
                s.state_changed(&call, &dev, "ended");
            }
        }));
    }
    for t in hs {
        t.join().unwrap();
    }
    let mut n = 0;
    while let Some(note) = sink.pop_note() {
        let Note::Map(_, v) = note else { panic!() };
        let (call, dev) = (
            v["call_id"].as_str().unwrap(),
            v["device_id"].as_str().unwrap(),
        );
        assert!(call.starts_with(dev), "{call} stamped {dev}");
        n += 1;
    }
    assert_eq!(sink.dropped_notes(), 0);
    assert_eq!(n, 8 * 40 * 3, "every note was stamped and checked");
    assert!(
        sink.st().calls.is_empty(),
        "the call table is not empty at the end"
    );
}

// TestTwoDoors_ForgetDeviceKeepsTheOtherDoorsCallsAndDials, TestTwoDoors_CancelDialWithdrawsOnlyTheNamedDoorsDial,
// TestTwoDoors_AnInboundRingOfOneDoorDoesNotConsumeTheOtherDoorsPendingDial,
// TestTwoDoors_AnUnannouncedTransitionIsClaimedOnlyByTheDoorThatDialled,
// TestTwoDoors_ADoorThatDidNotDialClaimsNothingAndLeavesTheOthersDialWaiting,
// TestDialClaim_ADoorsTransitionIsNeverTheOtherDoorsDial, TestDialClaim_InterleavedDialsOfTwoDoorsEachGoToTheirOwnDoor,
// TestDialClaim_TwoDialsOfOneDoorAreClaimedInOrder, TestDialClaim_TheUnscopedSinkClaimsNothing
#[tokio::test]
async fn pending_dials_are_claimed_by_the_dialling_door_only() {
    let h = Hub::detached();
    let sink = h.events.clone();
    let claim = |call: &str, reporter: &str| {
        sink.state_changed(call, reporter, "answered");
        note(&h).unwrap().1["device_id"]
            .as_str()
            .unwrap()
            .to_string()
    };
    sink.expect_dial("A");
    assert_eq!(claim("x1", "B"), "", "B claimed A's dial");
    assert_eq!(claim("x2", ""), "", "the unscoped sink claimed a dial");
    sink.expect_dial("B");
    sink.ringing("in", "A"); // an inbound ring of A does not consume B's dial
    note(&h);
    assert_eq!(claim("y1", "B"), "B");
    assert_eq!(claim("y2", "A"), "A", "A's dial is still waiting");
    // two dials of one door are claimed in order
    sink.expect_dial("A");
    sink.expect_dial("A");
    assert_eq!(claim("z1", "A"), "A");
    assert_eq!(claim("z2", "A"), "A");
    assert_eq!(claim("z3", "A"), "", "a third call has no dial left");
    // cancel withdraws the newest dial of the named door only
    sink.expect_dial("A");
    sink.expect_dial("B");
    sink.cancel_dial("A");
    assert_eq!(claim("w1", "A"), "");
    assert_eq!(claim("w2", "B"), "B");
    // forgetting a device keeps the other door's calls and dials
    sink.expect_dial("B");
    sink.ringing("ka", "A");
    sink.ringing("kb", "B");
    note(&h);
    note(&h);
    sink.forget_device("A");
    assert_eq!(claim("ka", ""), "", "A's call survived forget_device");
    assert_eq!(claim("kb", ""), "B");
    assert_eq!(claim("v1", "B"), "B", "B's dial was forgotten with A");
}

// TestTwoDoors_OutboundCallsOfTwoDoorsHaveOwnIdsAndStayOnTheirDoors
#[tokio::test]
async fn outbound_calls_of_two_doors_stay_on_their_doors() {
    let h = Hub::detached();
    let (a, b) = (Fake::new("A"), Fake::new("B"));
    *a.dialer.lock().unwrap() = Some(Ok(()));
    *b.dialer.lock().unwrap() = Some(Ok(()));
    add(&h, &a).await;
    add(&h, &b).await;
    let ia = val(h.dial(p(r#"{"device_id":"A"}"#)).await)["call_id"]
        .as_str()
        .unwrap()
        .to_string();
    let ib = val(h.dial(p(r#"{"device_id":"B"}"#)).await)["call_id"]
        .as_str()
        .unwrap()
        .to_string();
    assert_ne!(ia, ib);
    b.sink().state_changed(&ib, "answered");
    a.sink().state_changed(&ia, "answered");
    let n = notes(&h);
    assert_eq!(n[0].1["device_id"], "B");
    assert_eq!(n[1].1["device_id"], "A");
    // R2-57 (3): a hang-up by id ends that door's call and leaves the other door's running
    *a.hangup.lock().unwrap() = Some(Ok(()));
    *b.hangup.lock().unwrap() = Some(Ok(()));
    assert_eq!(
        val(h.hangup(p(&format!(r#"{{"call_id":"{ia}"}}"#))).await),
        json!({"ok": true})
    );
    assert_eq!(
        a.hangups.lock().unwrap().as_slice(),
        std::slice::from_ref(&ia)
    );
    assert!(
        b.hangups.lock().unwrap().is_empty(),
        "the other door's call was touched"
    );
    b.sink().state_changed(&ib, "active");
    assert_eq!(
        note(&h).unwrap().1["device_id"],
        "B",
        "B's call lost its stamp"
    );
}

// TestTwoDoors_DeconfigureOfOneDoorDuringTheOthersCallLeavesThatCallStamped
#[tokio::test]
async fn deconfigure_of_one_door_leaves_the_others_call_stamped() {
    let h = Hub::detached();
    let (a, b) = (Fake::new("A"), Fake::new("B"));
    add(&h, &a).await;
    add(&h, &b).await;
    b.sink().ringing("cb", "B");
    h.deconfigure(p(r#"{"device_id":"A"}"#)).await.unwrap();
    b.sink().state_changed("cb", "answered");
    let n = notes(&h);
    assert_eq!(n[1].1["device_id"], "B");
}

// ---- slice 6: the relay-closing wrapper (`relayClosingSink`) ----------------------------

use crate::call::Relay;

/// `endedRecorder`: a sink that records the state changes it is handed.
#[derive(Default)]
struct EndedRecorder {
    got: Mutex<Vec<String>>,
}

impl EventSink for EndedRecorder {
    fn ringing(&self, _: &str, _: &str) {}
    fn state_changed(&self, call_id: &str, state: &str) {
        self.got.lock().unwrap().push(format!("{call_id}:{state}"));
    }
    fn availability_changed(&self, _: &str, _: bool, _: &str) {}
}

async fn is_closed_soon(r: &Arc<Relay>) -> bool {
    let end = tokio::time::Instant::now() + Duration::from_secs(1);
    while !r.is_closed() && tokio::time::Instant::now() < end {
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
    r.is_closed()
}

// TestRelayClosingSink_EndedDropsRelay: an ended event closes and forgets the relay for that
// call, and is still forwarded.
#[tokio::test]
async fn relay_closing_sink_ended_drops_relay() {
    let relays = RelayRegistry::new();
    let (r1, r2) = (Arc::new(Relay::new()), Arc::new(Relay::new()));
    relays.put("c1", r1.clone());
    relays.put("c2", r2.clone());
    let rec = Arc::new(EndedRecorder::default());
    let s = RelayClosingSink::new(rec.clone(), relays.clone());

    s.state_changed("c1", "answered");
    assert!(
        relays.take("c1").is_some(),
        "a non-ended event must leave the relay alone"
    );
    relays.put("c1", r1.clone());
    s.state_changed("c1", "ended");
    assert!(relays.take("c1").is_none(), "ended must remove the relay");
    assert!(is_closed_soon(&r1).await, "ended must close the relay");
    assert!(
        relays.take("c2").is_some(),
        "another call's relay must stay"
    );
    assert!(!r2.is_closed());
    assert_eq!(*rec.got.lock().unwrap(), vec!["c1:answered", "c1:ended"]);
}

// TestTwoDoors_AnEndOnOneDoorClosesOnlyItsRelayAndIsStampedWithIt
#[tokio::test]
async fn an_end_on_one_door_closes_only_its_relay_and_is_stamped_with_it() {
    let h = Hub::detached();
    h.events.ringing("gate-1", "gate");
    h.events.ringing("door-1", "entrance");
    let relays = RelayRegistry::new();
    let (g, d) = (Arc::new(Relay::new()), Arc::new(Relay::new()));
    relays.put("gate-1", g.clone());
    relays.put("door-1", d.clone());
    let s = RelayClosingSink::new(
        Arc::new(DeviceSink {
            s: h.events.clone(),
            device_id: "gate".into(),
        }),
        relays.clone(),
    );
    notes(&h);
    s.state_changed("gate-1", STATE_ENDED);
    assert!(
        relays.take("gate-1").is_none(),
        "gate-1's relay survived its own end"
    );
    assert!(
        relays.take("door-1").is_some(),
        "door-1's relay went with gate-1's end"
    );
    assert!(is_closed_soon(&g).await);
    assert!(!d.is_closed());
    let n = note(&h).unwrap();
    assert_eq!(n.0, "call_state_changed");
    assert_eq!(
        (
            n.1["call_id"].as_str(),
            n.1["device_id"].as_str(),
            n.1["state"].as_str()
        ),
        (Some("gate-1"), Some("gate"), Some("ended"))
    );
}

// TestDialClaim_ThroughTheRelayClosingWrapper: through the production wrapper the claim still
// follows the device, and the wrapper still closes a relay on ended.
#[tokio::test]
async fn dial_claim_through_the_relay_closing_wrapper() {
    let h = Hub::detached();
    let (a, b) = (Fake::new("door-a"), Fake::new("door-b"));
    for f in [&a, &b] {
        let sink: Arc<dyn EventSink> = Arc::new(RelayClosingSink::new(
            Arc::new(DeviceSink {
                s: h.events.clone(),
                device_id: f.id.clone(),
            }),
            h.relays.clone(),
        ));
        f.start(&h.ctx, sink).await.unwrap();
        assert!(h.put_if_absent(&f.id, f.clone()));
    }
    val(h.dial(p(r#"{"device_id":"door-a"}"#)).await);
    b.sink().state_changed("b-1", "active");
    assert_eq!(
        note(&h).unwrap().1["device_id"],
        "",
        "door-b through the wrapper claimed door-a's dial"
    );
    a.sink().state_changed("a-1", "active");
    assert_eq!(note(&h).unwrap().1["device_id"], "door-a");
    // and the wrapper still closes the relay of an ended call
    let r = Arc::new(Relay::new());
    h.relays.put("a-1", r.clone());
    a.sink().state_changed("a-1", "ended");
    assert!(h.relays.take("a-1").is_none() && is_closed_soon(&r).await);
}

/// Every layer of the wiring over one bare sink, as `sinkChains` of sink_forwarding_test.go.
fn sink_chains(h: &Hub) -> Vec<(&'static str, Arc<dyn EventSink>)> {
    let dev = |h: &Hub| -> Arc<dyn EventSink> {
        Arc::new(DeviceSink {
            s: h.events.clone(),
            device_id: "gate".into(),
        })
    };
    let wrap = |inner: Arc<dyn EventSink>| -> Arc<dyn EventSink> {
        Arc::new(RelayClosingSink::new(inner, RelayRegistry::new()))
    };
    vec![
        ("deviceSink", dev(h)),
        (
            "relayClosingSink over deviceSink (what configure hands a driver)",
            wrap(dev(h)),
        ),
        (
            "relayClosingSink around relayClosingSink",
            wrap(wrap(dev(h))),
        ),
    ]
}

// TestSinkForwarding_TheOptionalInterfacesListedAreTheOnesTheDriversAssert: the accessors of
// `EventSink` ARE the list (a driver cannot assert for an extension the trait does not name),
// and the dahua driver asks for `error_reporter`.
//
// TestSinkForwarding_EveryOptionalInterfaceIsReachableThroughEveryWrapperAsOnTheBareSink
#[test]
fn every_optional_extension_is_reachable_through_every_wrapper() {
    let h = Hub::detached();
    for (name, sink) in sink_chains(&h) {
        assert!(
            sink.listen_state_sink().is_some(),
            "{name} hides ListenStateSink"
        );
        assert!(
            sink.capabilities_sink().is_some(),
            "{name} hides CapabilitiesSink"
        );
        assert!(
            sink.call_reason_sink().is_some(),
            "{name} hides CallReasonSink"
        );
        assert!(
            sink.error_reporter().is_some(),
            "{name} hides errorReporter"
        );
    }
}

// TestSinkForwarding_ListenStateAndDriverErrorArriveWithTheRightDeviceIDThroughEveryWrapper,
// TestSinkForwarding_CapabilitiesChangedArrivesThroughEveryWrapper (the CallReason leg too)
#[test]
fn listen_state_driver_error_and_the_rest_arrive_through_every_wrapper() {
    for (name, _) in sink_chains(&Hub::detached()) {
        let h = Hub::detached();
        let sink = sink_chains(&h)
            .into_iter()
            .find(|(n, _)| *n == name)
            .unwrap()
            .1;
        sink.listen_state_sink()
            .unwrap()
            .listen_state_changed("gate", "listening", "");
        sink.listen_state_sink()
            .unwrap()
            .listen_state_changed("entrance", "refused", "forbidden");
        sink.error_reporter()
            .unwrap()
            .driver_error("entrance", "registration_failed", "486 quota");
        sink.call_reason_sink()
            .unwrap()
            .call_reason("gate", "c1", "ended", "bye");
        let caps = Capabilities {
            reports_availability: true,
            can_dial: true,
            ..Capabilities::default()
        };
        sink.capabilities_sink()
            .unwrap()
            .capabilities_changed("gate", &caps);
        let got: Vec<(String, String)> = notes(&h)
            .into_iter()
            .map(|(m, v)| (m, v.to_string()))
            .collect();
        let want = [
            (
                "listen_state",
                r#"{"device_id":"gate","reason":"","state":"listening"}"#,
            ),
            (
                "listen_state",
                r#"{"device_id":"entrance","reason":"forbidden","state":"refused"}"#,
            ),
            (
                "driver_error",
                r#"{"code":"registration_failed","device_id":"entrance","message":"486 quota"}"#,
            ),
            (
                "call_reason",
                r#"{"call_id":"c1","device_id":"gate","kind":"ended","reason":"bye"}"#,
            ),
            (
                "capabilities_changed",
                r#"{"capabilities":{"CanDial":true,"DirectCapable":false,"HasRelay":false,"InboundAudio":0,"OutboundAudio":0,"RTSPTemplate":"","ReportsAvailability":true},"device_id":"gate"}"#,
            ),
        ];
        assert_eq!(got.len(), want.len(), "{name}: {got:?}");
        for (g, w) in got.iter().zip(want) {
            assert_eq!((g.0.as_str(), g.1.as_str()), w, "{name}");
        }
    }
}

// TestSinkForwarding_OrdinaryEventsThroughTheWrapperAreUnchanged
#[test]
fn ordinary_events_through_the_wrapper_are_unchanged() {
    let h = Hub::detached();
    let sink = sink_chains(&h).remove(1).1;
    sink.availability_changed("gate", true, "");
    sink.ringing("c1", "gate");
    sink.state_changed("c1", "answered");
    sink.state_changed("c1", "ended");
    let got: Vec<String> = notes(&h)
        .into_iter()
        .map(|(m, v)| format!("{m} {v}"))
        .collect();
    assert_eq!(
        got,
        vec![
            r#"driver_availability {"available":true,"device_id":"gate","reason":""}"#,
            r#"ringing {"call_id":"c1","device_id":"gate"}"#,
            r#"call_state_changed {"call_id":"c1","device_id":"gate","state":"answered"}"#,
            r#"call_state_changed {"call_id":"c1","device_id":"gate","state":"ended"}"#,
        ]
    );
}

// TestSinkForwarding_AWrapperOverASinkWithoutExtensionsDropsTheReportQuietly
#[test]
fn a_wrapper_over_a_sink_without_extensions_has_none_to_hand_out() {
    let rec = Arc::new(EndedRecorder::default());
    let w = RelayClosingSink::new(rec.clone(), RelayRegistry::new());
    assert!(w.listen_state_sink().is_none() && w.error_reporter().is_none());
    assert!(w.capabilities_sink().is_none() && w.call_reason_sink().is_none());
    w.ringing("c", "gate"); // an ordinary event still reaches the wrapped sink
    w.state_changed("c", "answered");
    assert_eq!(*rec.got.lock().unwrap(), vec!["c:answered"]);
}

// TestCapabilitiesReportsAvailability (the dahua_sip half; the observer's is in 5a)
#[test]
fn dahua_sip_reports_availability_through_the_registry() {
    let d = crate::driver::registry()["dahua_sip"]();
    let m: Value = serde_json::from_str(&d.capabilities().to_wire()).unwrap();
    assert_eq!(m["ReportsAvailability"], true);
}

// Rust only (review of 3b-1): a stalled writer with 2000 queued notifications behind it
// still delivers the ring and the end of the call; drops are counted, never silent.
#[test]
fn a_stalled_writer_still_gets_the_ring_and_the_end() {
    let sink = IpcEventSink::detached(); // no writer: everything queues
    sink.ringing("c1", "dev");
    for i in 0..2000 {
        sink.state_changed(&format!("x{i}"), "dev", "answered");
    }
    sink.state_changed("c1", "dev", "ended");
    assert!(sink.dropped_notes() >= 900, "{}", sink.dropped_notes());
    let mut got = Vec::new();
    while let Some(n) = sink.pop_note() {
        if let Note::Map(m, v) = n {
            got.push((
                m,
                v["call_id"].as_str().unwrap().to_string(),
                v["state"].as_str().map(str::to_string),
            ));
        }
    }
    assert!(got.len() <= NOTE_QUEUE_MAX, "{}", got.len());
    assert_eq!(got.first().unwrap().0, "ringing");
    let last = got.last().unwrap();
    assert_eq!((last.1.as_str(), last.2.as_deref()), ("c1", Some("ended")));
}

// R1-16: `flush` waits for the writer; a panicking write does not stop the writer for good.
#[tokio::test]
async fn flush_waits_for_the_writer_and_a_panic_restarts_it() {
    let seen = Arc::new(Mutex::new(Vec::<String>::new()));
    let first = Arc::new(std::sync::atomic::AtomicBool::new(true));
    let (s2, f2) = (seen.clone(), first.clone());
    let sink = IpcEventSink::with_writer(Arc::new(move |n| {
        let (s, f) = (s2.clone(), f2.clone());
        Box::pin(async move {
            if f.swap(false, Ordering::SeqCst) {
                panic!("writer exploded");
            }
            tokio::time::sleep(Duration::from_millis(40)).await;
            if let Note::Map(m, v) = n {
                s.lock().unwrap().push(format!("{m}{}", v["n"]));
            }
        })
    }));
    for i in 0..4 {
        sink.note(Note::Map("ev", json!({"n": i})));
    }
    sink.flush(Duration::from_secs(5)).await;
    // note 0 was being written when the writer panicked; 1..3 were delivered, in order
    assert_eq!(*seen.lock().unwrap(), ["ev1", "ev2", "ev3"]);
}

// The sink: ringing and call_state_changed notifications carry device_id; the dialled call
// path stamps a minted id. (sink_forwarding_test.go / dial_callid_test.go, the parts that
// need no second driver)
#[tokio::test]
async fn the_sink_stamps_device_ids() {
    let mut r = Rig::new("sink").await;
    r.call("configure", server_cfg(json!({}))).await;
    let sink = r.hub.events.clone();
    sink.ringing("c1", "dev-a");
    sink.state_changed("c1", "dev-a", "answered");
    sink.state_changed("c1", "", "ended");
    sink.state_changed("c1", "", "ended"); // forgotten: empty device id now
    let got = [
        r.line().await,
        r.line().await,
        r.line().await,
        r.line().await,
    ];
    assert_eq!(
        got[0],
        json!({"method": "ringing", "params": {"call_id": "c1", "device_id": "dev-a"}})
    );
    assert_eq!(got[1]["params"]["device_id"], "dev-a");
    assert_eq!(got[2]["params"]["device_id"], "dev-a");
    assert_eq!(got[3]["params"]["device_id"], "");
    // struct-shaped notifications keep Go's field order
    let caps = Capabilities {
        can_dial: true,
        ..Capabilities::default()
    };
    sink.capabilities_changed("dev-a", &caps);
    sink.availability_changed("dev-a", false, "down");
    let mut l = String::new();
    r.rd.read_line(&mut l).await.unwrap();
    assert_eq!(
        l,
        "{\"method\":\"capabilities_changed\",\"params\":{\"device_id\":\"dev-a\",\"capabilities\":{\"InboundAudio\":0,\"OutboundAudio\":0,\"DirectCapable\":false,\"HasRelay\":false,\"RTSPTemplate\":\"\",\"ReportsAvailability\":false,\"CanDial\":true}}}\n"
    );
    l.clear();
    r.rd.read_line(&mut l).await.unwrap();
    assert_eq!(
        l,
        "{\"method\":\"driver_availability\",\"params\":{\"device_id\":\"dev-a\",\"available\":false,\"reason\":\"down\"}}\n"
    );
    // a pending dial is claimed by the reporting device only
    sink.expect_dial("dev-b");
    sink.state_changed("x1", "dev-a", "answered");
    sink.state_changed("x2", "dev-b", "answered");
    l.clear();
    r.rd.read_line(&mut l).await.unwrap();
    assert!(l.contains("\"call_id\":\"x1\",\"device_id\":\"\""), "{l}");
    l.clear();
    r.rd.read_line(&mut l).await.unwrap();
    assert!(
        l.contains("\"call_id\":\"x2\",\"device_id\":\"dev-b\""),
        "{l}"
    );
    let id = sink.new_outbound_call("dev-a");
    assert!(id.starts_with("out-") && id.len() == 20, "{id}");
    r.hub.shutdown().await;
    r.srv.close().await;
}

// TestDialClaim_AnExpiredDialIsNotClaimedAndDoesNotBlockTheFreshOne,
// TestTwoDoors_AStaleDialOfOneDoorIsDroppedAndTheOtherDoorsDialIsStillClaimed (R2-2). The Go
// tests set `sink.dials[0].at` back by the window; so does this one (no clock seam is needed).
// Mutation: no `retain` in `claim_dial_locked`.
#[tokio::test]
async fn a_dial_older_than_the_claim_window_is_dropped_not_claimed() {
    let h = Hub::detached();
    let (a, b) = (Fake::new("door-a"), Fake::new("door-b"));
    add(&h, &a).await;
    add(&h, &b).await;
    for id in ["door-a", "door-b"] {
        assert_eq!(
            val(h.dial(p(&format!(r#"{{"device_id":"{id}"}}"#))).await),
            json!({"ok": true})
        );
    }
    {
        let mut st = h.events.st();
        assert_eq!(st.dials.len(), 2);
        // a monotonic clock younger than the window (a freshly booted VM) has no such instant
        let Some(old) =
            std::time::Instant::now().checked_sub(DIAL_CLAIM_WINDOW + Duration::from_secs(1))
        else {
            eprintln!("skipped: the monotonic clock is younger than the claim window");
            return;
        };
        st.dials[0].at = old;
    }
    a.sink().state_changed("a-1", "active");
    assert_eq!(
        note(&h).unwrap().1["device_id"],
        "",
        "a call claimed an expired dial"
    );
    // the stale entry did not take door-b's fresh one down with it
    b.sink().state_changed("b-1", "active");
    assert_eq!(note(&h).unwrap().1["device_id"], "door-b");
}

// TestDialClaim_P1CallDialerPathIsUnchangedAlongsideALegacyDialOfAnotherDoor (R2-45 b): a
// CallDialer's hub-minted ids stay stamped with their device, never touch the pending-dial list,
// and a legacy dial of another door is still claimed by that door only.
#[tokio::test]
async fn a_call_dialer_path_is_unchanged_alongside_a_legacy_dial_of_another_door() {
    let h = Hub::detached();
    let (a, b) = (Fake::new("door-a"), Fake::new("door-b"));
    *a.dialer.lock().unwrap() = Some(Ok(()));
    *a.emit_in_dial.lock().unwrap() = vec!["ringing"];
    add(&h, &a).await;
    add(&h, &b).await;
    assert!(val(h.dial(p(r#"{"device_id":"door-b"}"#)).await)["call_id"].is_null()); // legacy
    let out = val(h.dial(p(r#"{"device_id":"door-a"}"#)).await)["call_id"]
        .as_str()
        .unwrap()
        .to_string();
    // the CallDialer's state, reported inside the dial, was stamped door-a and did not consume
    // door-b's pending dial
    let n = notes(&h);
    assert_eq!(n.len(), 1, "{n:?}");
    assert_eq!(n[0].0, "call_state_changed");
    assert_eq!(
        n[0].1,
        json!({"call_id": out, "device_id": "door-a", "state": "ringing"})
    );
    // door A's unannounced stray is nobody's; door B's is B's dial
    a.sink().state_changed("stray-a", "active");
    assert_eq!(note(&h).unwrap().1["device_id"], "");
    b.sink().state_changed("b-1", "active");
    assert_eq!(note(&h).unwrap().1["device_id"], "door-b");
}

// R2-56: the struct-ordered notifications travel as text, so their key ORDER is the contract
// (Go: sink_forwarding_test.go:148-150, two_doors_test.go:1133-1135). The other tests parse the
// note and compare values, which sorts the keys; these compare the lines Go writes.
#[test]
fn struct_ordered_notes_keep_go_s_field_order() {
    let sink = IpcEventSink::detached();
    sink.listen_state_changed("d1", "listening", "up");
    sink.call_reason("d1", "c1", "hangup", "busy");
    sink.availability_changed("d1", false, "no route");
    let raw = || match sink.pop_note().unwrap() {
        Note::Raw(m, text) => (m, text),
        Note::Map(m, v) => panic!("{m} is not struct-ordered: {v}"),
    };
    assert_eq!(
        raw(),
        (
            "listen_state",
            r#"{"device_id":"d1","state":"listening","reason":"up"}"#.to_string()
        )
    );
    assert_eq!(
        raw(),
        (
            "call_reason",
            r#"{"device_id":"d1","call_id":"c1","kind":"hangup","reason":"busy"}"#.to_string()
        )
    );
    assert_eq!(
        raw(),
        (
            "driver_availability",
            r#"{"device_id":"d1","available":false,"reason":"no route"}"#.to_string()
        )
    );
}

// R2-82: `deconfigure` forgets the device in the sink (through the handler, not by calling
// `forget_device`): afterwards neither its ringing call nor a call it dialled is stamped.
// Go does the forgetting BEFORE `Stop` (main.go:1166-1168), so the `ended` a driver reports
// while stopping carries `device_id` "" in both binaries (R2-59, PARITY note in ipc_process.md).
// Mutation: no `forget_device` in `deconfigure`.
#[tokio::test]
async fn deconfigure_forgets_the_device_in_the_sink() {
    let h = Hub::detached();
    let (a, b) = (Fake::new("a"), Fake::new("b"));
    *a.dialer.lock().unwrap() = Some(Ok(()));
    add(&h, &a).await;
    add(&h, &b).await;
    a.sink().ringing("ring-a", "a");
    b.sink().ringing("ring-b", "b");
    let out = val(h.dial(p(r#"{"device_id":"a"}"#)).await)["call_id"]
        .as_str()
        .unwrap()
        .to_string();
    notes(&h);
    h.deconfigure(p(r#"{"device_id":"a"}"#)).await.unwrap();
    a.sink().state_changed("ring-a", "answered");
    a.sink().state_changed(&out, "answered");
    b.sink().state_changed("ring-b", "answered");
    let n = notes(&h);
    assert_eq!(
        n[0].1["device_id"], "",
        "the ring of a deconfigured door is still stamped"
    );
    assert_eq!(n[1].1["device_id"], "", "its dialled call is still stamped");
    assert_eq!(n[2].1["device_id"], "b", "the other door lost its stamp");
}

// R2-83 (R1-16): the last thing `shutdown` does is flush what the drivers emitted while stopping
// (the `ended` of a live call), so it is on the socket before the transport closes it. The
// writer is slow, so a shutdown that did not flush would close the socket first and lose it.
// Mutation: no `self.events.flush(..)` at the end of `shutdown_inner`.
#[tokio::test]
async fn shutdown_flushes_the_ended_a_driver_reports_while_stopping() {
    let mut r = Rig::slow_notes("flush", Duration::from_millis(30)).await;
    let f = Fake::new("front");
    add(&r.hub, &f).await;
    f.sink().ringing("c1", "front");
    assert_eq!(r.line().await["method"], "ringing");
    *f.end_on_stop.lock().unwrap() = Some("c1".to_string());
    r.hub.shutdown().await;
    r.srv.close().await;
    let ended = r.line().await;
    assert_eq!(ended["method"], "call_state_changed");
    assert_eq!(ended["params"]["state"], "ended");
    // `shutdown` does not forget devices (only `deconfigure` does), so it carries the id
    assert_eq!(ended["params"]["device_id"], "front");
}
