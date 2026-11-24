use super::*;

// TestRegistryID (main_test.go), the cases that need no other driver: a driver's own
// DeviceID wins, then `host`, then `nvr_host` (the observer case is ported with its driver,
// `tests/observer_ipc.rs`).
#[test]
fn registry_id_prefers_the_drivers_id_then_host_then_nvr_host() {
    let obj = |v: Value| v.as_object().cloned().unwrap();
    let f = Fake::new("gate-sip-server:x");
    assert_eq!(
        registry_id(&*f, &obj(json!({"host": "h"}))),
        "gate-sip-server:x"
    );
    let g = Fake::new("");
    assert_eq!(
        registry_id(&*g, &obj(json!({"host": "10.8.0.39"}))),
        "10.8.0.39"
    );
    assert_eq!(registry_id(&*g, &obj(json!({"nvr_host": "nvr"}))), "nvr");
    assert_eq!(
        registry_id(&*g, &obj(json!({"host": "h", "nvr_host": "nvr"}))),
        "h"
    );
    assert_eq!(registry_id(&*g, &obj(json!({"host": 5}))), "");
}

// TestDeconfigureStopsOneDriverAndKeepsTheOthers
#[tokio::test]
async fn deconfigure_stops_one_driver_and_keeps_the_others() {
    let h = Hub::detached();
    let (a, b) = (Fake::new("a"), Fake::new("b"));
    add(&h, &a).await;
    add(&h, &b).await;
    assert_eq!(
        val(h.deconfigure(p(r#"{"device_id":"a"}"#)).await),
        json!({"ok": true})
    );
    assert_eq!(a.stops.load(Ordering::SeqCst), 1);
    assert_eq!(b.stops.load(Ordering::SeqCst), 0);
    assert!(h.get("a").is_none() && h.get("b").is_some());
    assert_eq!(
        code(h.deconfigure(p(r#"{"device_id":"a"}"#)).await),
        "unknown_device"
    );
    assert_eq!(code(h.deconfigure(p("[1]")).await), "bad_params");
}

// TestHangupGoesToTheDriverThatHoldsTheCall
#[tokio::test]
async fn hangup_goes_to_the_driver_that_holds_the_call() {
    let h = Hub::detached();
    let (a, b) = (Fake::new("a"), Fake::new("b"));
    *a.hangup.lock().unwrap() = Some(Err(DriverError::sentinel(Sentinel::NoSuchCall)));
    *b.hangup.lock().unwrap() = Some(Ok(()));
    add(&h, &a).await;
    add(&h, &b).await;
    assert_eq!(
        val(h.hangup(p(r#"{"call_id":"c1","reason":"x"}"#)).await),
        json!({"ok": true})
    );
    assert_eq!(a.hangups.lock().unwrap().clone(), vec!["c1"]);
    assert_eq!(b.hangups.lock().unwrap().clone(), vec!["c1"]);
}

// TestTwoDoors_HangupGoesOnlyToTheDoorThatHoldsTheCall: a driver that holds the call ends
// it and the later ones are not asked.
#[tokio::test]
async fn hangup_stops_at_the_door_that_holds_the_call() {
    let h = Hub::detached();
    let (a, b) = (Fake::new("a"), Fake::new("b"));
    *a.hangup.lock().unwrap() = Some(Ok(()));
    *b.hangup.lock().unwrap() = Some(Ok(()));
    add(&h, &a).await;
    add(&h, &b).await;
    h.hangup(p(r#"{"call_id":"c1"}"#)).await.unwrap();
    assert_eq!(a.hangups.lock().unwrap().len(), 1);
    assert!(
        b.hangups.lock().unwrap().is_empty(),
        "the second door was asked"
    );
}

// TestHangupFailureIsItsOwnCode; TestTwoDoors_AFailingHangupOfOneDoorDoesNotReachTheOthersCall
#[tokio::test]
async fn hangup_failure_is_its_own_code() {
    let h = Hub::detached();
    let (a, b) = (Fake::new("a"), Fake::new("b"));
    *a.hangup.lock().unwrap() = Some(Err(DriverError::new("BYE refused")));
    *b.hangup.lock().unwrap() = Some(Ok(()));
    add(&h, &a).await;
    add(&h, &b).await;
    let e = h.hangup(p(r#"{"call_id":"c1"}"#)).await.unwrap_err();
    assert_eq!(
        (e.code.as_str(), e.message.as_str()),
        ("hangup_failed", "BYE refused")
    );
    assert!(
        b.hangups.lock().unwrap().is_empty(),
        "a failure reached the other door"
    );
    // R2-57 (4): once the first door no longer holds a call, the other door's hang-up of it
    // still succeeds
    *a.hangup.lock().unwrap() = Some(Err(DriverError::wrapping(Sentinel::NoSuchCall, "gone")));
    assert_eq!(
        val(h.hangup(p(r#"{"call_id":"c2"}"#)).await),
        json!({"ok": true})
    );
    assert_eq!(*b.hangups.lock().unwrap(), ["c2"]);
}

// TestHangupOfAnythingElseIsNotImplementedExactlyAsBefore
#[tokio::test]
async fn hangup_of_anything_else_is_not_implemented_exactly_as_before() {
    let h = Hub::detached();
    let a = Fake::new("a"); // not a CallHangupper
    let b = Fake::new("b");
    *b.hangup.lock().unwrap() = Some(Err(DriverError::sentinel(Sentinel::NoSuchCall)));
    add(&h, &a).await;
    add(&h, &b).await;
    let e = h.hangup(p(r#"{"call_id":"c1"}"#)).await.unwrap_err();
    assert_eq!(
        (e.code.as_str(), e.message.as_str()),
        ("not_implemented", NOT_IMPLEMENTED_MESSAGE)
    );
    assert_eq!(code(h.hangup(p("[1]")).await), "bad_params");
}

// TestDriverError_AlreadyAnsweredIsItsOwnCode, TestDriverError_ClaimsDisabledIsItsOwnCode,
// TestDriverError_NotSupportedIsItsOwnCode / TestNotSupportedIsRelayedOverIPC: through the
// `answer` handler.
#[tokio::test]
async fn driver_errors_keep_their_own_codes_through_answer() {
    let h = Hub::detached();
    let a = Fake::new("a");
    add(&h, &a).await;
    for (e, want) in [
        (
            DriverError::sentinel(Sentinel::AlreadyAnswered),
            "already_answered",
        ),
        (
            DriverError::sentinel(Sentinel::ClaimsDisabled),
            "claims_disabled",
        ),
        (
            DriverError::sentinel(Sentinel::NotSupported),
            "not_supported",
        ),
        (
            DriverError::wrapping(Sentinel::NotSupported, "wrapped: not_supported"),
            "not_supported",
        ),
        (DriverError::new("boom"), "answer_failed"),
    ] {
        *a.answer_err.lock().unwrap() = Some(e);
        assert_eq!(code(h.answer(p(r#"{"device_id":"a"}"#)).await), want);
    }
    *a.answer_err.lock().unwrap() = None;
    assert_eq!(
        val(h.answer(p(r#"{"device_id":"a"}"#)).await),
        json!({"ok": true})
    );
    assert_eq!(
        code(h.answer(p(r#"{"device_id":"zz"}"#)).await),
        "unknown_device"
    );
    assert_eq!(code(h.answer(p("[1]")).await), "bad_params");
}

// TestExternalRingVerb (over a fake ExternalRinger: the Go test uses unifiprotect's talk
// driver, which is not ported): the verb answers ok, announces no ring, adopts the call for
// the device, and maps the errors.
#[tokio::test]
async fn external_ring_verb() {
    let h = Hub::detached();
    let a = Fake::new("ha_event:front");
    *a.ring.lock().unwrap() = Some(Ok(()));
    add(&h, &a).await;
    let r = h
        .external_ring(p(
            r#"{"device_id":"ha_event:front","call_id":"ha-event-1","hold":20}"#,
        ))
        .await;
    assert_eq!(val(r), json!({"ok": true}));
    assert!(note(&h).is_none(), "external_ring announced a ring");
    // the call is adopted: a later transition is stamped with the device
    a.sink().state_changed("ha-event-1", "answered");
    assert_eq!(note(&h).unwrap().1["device_id"], "ha_event:front");
    for (params, want) in [
        (r#"{"device_id":"nope","call_id":"x"}"#, "unknown_device"),
        (r#"{"device_id":"ha_event:front"}"#, "bad_params"),
        (
            r#"{"device_id":"ha_event:front","call_id":"x","hold":-1}"#,
            "bad_params",
        ),
        (r#""not json""#, "bad_params"),
    ] {
        assert_eq!(code(h.external_ring(p(params)).await), want, "{params}");
    }
    for (e, want) in [
        (DriverError::sentinel(Sentinel::Busy), "busy"),
        (
            DriverError::sentinel(Sentinel::NotSupported),
            "not_supported",
        ),
        (DriverError::new("x"), "external_ring_failed"),
    ] {
        *a.ring.lock().unwrap() = Some(Err(e));
        assert_eq!(
            code(
                h.external_ring(p(r#"{"device_id":"ha_event:front","call_id":"y"}"#))
                    .await
            ),
            want
        );
    }
    // a failed ring is forgotten again: no stamp for it
    a.sink().state_changed("y", "answered");
    let n = notes(&h);
    assert_eq!(n.last().unwrap().1["device_id"], "");
}

// TestTalkHealthVerbIsReadOnlyAndKeyed: the error codes (the health block belongs to
// unifi_protect_talk, not ported).
#[tokio::test]
async fn talk_health_error_codes() {
    let h = Hub::detached();
    add(&h, &Fake::new("plain")).await;
    for (params, want) in [
        (r#"{"device_id":"nope"}"#, "unknown_device"),
        ("{}", "bad_params"),
        (r#""x""#, "bad_params"),
        (r#"{"device_id":"plain"}"#, "not_supported"),
    ] {
        assert_eq!(code(h.talk_health(p(params)).await), want, "{params}");
    }
}

// TestStreamURLHandlerReturnsTheMintedURL, TestStreamURLHandlerErrors
#[tokio::test]
async fn stream_url_handler() {
    let h = Hub::detached();
    let (nvr, ok, plain) = (Fake::new("nvr"), Fake::new("ok"), Fake::new("plain"));
    *nvr.stream.lock().unwrap() = Some(Err(DriverError::new("POST x: HTTP 401")));
    *ok.stream.lock().unwrap() = Some(Ok("rtsps://h/x?token=t".into()));
    for f in [&nvr, &ok, &plain] {
        add(&h, f).await;
    }
    assert_eq!(
        val(h.stream_url(p(r#"{"device_id":"ok"}"#)).await),
        json!({"url": "rtsps://h/x?token=t"})
    );
    for (raw, want) in [
        ("[1]", "bad_params"),
        (r#"{"device_id":"nope"}"#, "unknown_device"),
        (r#"{"device_id":"plain"}"#, "not_supported"),
        (r#"{"device_id":"nvr"}"#, "stream_url_failed"),
    ] {
        assert_eq!(code(h.stream_url(p(raw)).await), want, "{raw}");
    }
}

// TestDial_LegacyReplyIsExactlyOK; TestDialRecordsTheDialledDevicesNextCallId;
// TestDialThatFailsDoesNotClaimALaterCall
#[tokio::test]
async fn legacy_dial_claims_the_devices_next_call_and_a_failure_claims_nothing() {
    let h = Hub::detached();
    let a = Fake::new("a");
    add(&h, &a).await;
    let r = h.dial(p(r#"{"device_id":"a"}"#)).await;
    assert!(matches!(&r, Ok(Reply::Json(v)) if *v == json!({"ok": true})));
    a.sink().state_changed("c1", "answered"); // an unannounced call of the dialled device
    assert_eq!(note(&h).unwrap().1["device_id"], "a");
    *a.legacy_dial.lock().unwrap() = Some(DriverError::new("no"));
    assert_eq!(code(h.dial(p(r#"{"device_id":"a"}"#)).await), "dial_failed");
    a.sink().state_changed("c2", "answered");
    assert_eq!(
        note(&h).unwrap().1["device_id"],
        "",
        "a failed dial claimed a call"
    );
    assert_eq!(
        code(h.dial(p(r#"{"device_id":"zz"}"#)).await),
        "unknown_device"
    );
}

// TestCallDialer_DialReplyCarriesACallID, TestCallDialer_CallIDsAreUnique,
// TestCallDialer_TransitionsAreReportedInOrderThroughTheSink,
// TestCallDialer_OutboundEventsNeverLookLikeAnInboundRing,
// TestCallDialer_ARefusingDriverFailsTheDialAndLeavesNoTrace (over a fake CallDialer)
#[tokio::test]
async fn call_dialer_path_mints_ids_and_reports_through_the_sink() {
    let h = Hub::detached();
    let a = Fake::new("a");
    *a.dialer.lock().unwrap() = Some(Ok(()));
    add(&h, &a).await;
    // R2-57 (2): an inbound ring of the same door, interleaved with the outbound calls
    a.sink().ringing("in1", "a");
    let mut ids = std::collections::HashSet::new();
    // R2-57 (7): Go mints 2,000
    for _ in 0..2000 {
        let v = val(h.dial(p(r#"{"device_id":"a"}"#)).await);
        assert_eq!(v["ok"], true);
        let id = v["call_id"].as_str().unwrap().to_string();
        assert!(id.starts_with("out-"), "{id}");
        assert!(ids.insert(id), "a call id repeated");
    }
    assert_eq!(a.dialled.lock().unwrap().len(), 2000);
    assert!(
        h.events.st().dials.is_empty(),
        "a CallDialer dial left a pending dial"
    );
    let id = a.dialled.lock().unwrap()[0].clone();
    for st in ["answered", "active", "ended"] {
        a.sink().state_changed(&id, st);
    }
    let n = notes(&h);
    assert_eq!(n.len(), 4, "{n:?}");
    // exactly one `ringing`, the inbound one, undisturbed and first
    assert_eq!(
        n[0],
        (
            "ringing".to_string(),
            json!({"call_id": "in1", "device_id": "a"})
        )
    );
    assert_eq!(n.iter().filter(|(m, _)| m == "ringing").count(), 1);
    for (i, st) in ["answered", "active", "ended"].iter().enumerate() {
        assert_eq!(n[i + 1].0, "call_state_changed");
        assert_eq!(
            n[i + 1].1,
            json!({"call_id": id, "device_id": "a", "state": st})
        );
    }
    // a plain error of a CallDialer is `dial_failed` (Go: dial_callid_test.go:261-264)
    *a.dialer.lock().unwrap() = Some(Err(DriverError::new("no route")));
    assert_eq!(code(h.dial(p(r#"{"device_id":"a"}"#)).await), "dial_failed");
    notes(&h);
    // a refusal: the error is the reply and the minted id is forgotten
    *a.dialer.lock().unwrap() = Some(Err(DriverError::wrapping(Sentinel::NotSupported, "off")));
    assert_eq!(
        code(h.dial(p(r#"{"device_id":"a"}"#)).await),
        "not_supported"
    );
    let refused = a.dialled.lock().unwrap().last().unwrap().clone();
    a.sink().state_changed(&refused, "answered");
    assert_eq!(
        note(&h).unwrap().1["device_id"],
        "",
        "a refused dial left a trace"
    );
}

// TestOnlyTheGateDriversImplementCallDialer: of the shipped drivers, gate_sip_server
// and (since PR #260, slice 7b here) unifi_protect_talk implement CallDialer.
#[test]
fn only_the_gate_drivers_implement_call_dialer() {
    let reg = crate::driver::registry();
    assert!(!reg.is_empty());
    for (name, mk) in &reg {
        let d = mk();
        assert_eq!(
            d.as_call_dialer().is_some(),
            *name == "gate_sip_server" || *name == "unifi_protect_talk",
            "shipped driver {name:?}"
        );
    }
}

// TestCallDialer_ConcurrentDialAndHangupRace
#[tokio::test]
async fn concurrent_dial_and_hangup_race() {
    /// A fake that places "calls": CallDialer + CallHangupper, reporting through its sink
    /// under its own lock so one call's events are totally ordered.
    #[derive(Default)]
    struct OutDriver {
        id: String,
        calls: Mutex<std::collections::HashMap<String, bool>>,
        sink: Mutex<Option<Arc<dyn EventSink>>>,
    }
    impl OutDriver {
        fn advance(&self, call_id: &str, state: &str) -> bool {
            let mut g = self.calls.lock().unwrap();
            match g.get_mut(call_id) {
                Some(ended) if !*ended => {
                    if state == "ended" {
                        *ended = true;
                    }
                    let sink = self.sink.lock().unwrap().clone().unwrap();
                    sink.state_changed(call_id, state);
                    true
                }
                _ => false,
            }
        }
    }
    impl Driver for OutDriver {
        fn configure(&mut self, _: &serde_json::Map<String, Value>) -> Result<(), DriverError> {
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
            Box::pin(async { Ok(()) })
        }
        fn stop(&self) -> BoxFuture<'_, Result<(), DriverError>> {
            Box::pin(async { Ok(()) })
        }
        fn dial<'a>(
            &'a self,
            _: &'a CancelToken,
            _: MediaTarget,
        ) -> BoxFuture<'a, Result<(), DriverError>> {
            Box::pin(async { Ok(()) })
        }
        fn answer<'a>(
            &'a self,
            _: &'a CancelToken,
            _: MediaTarget,
        ) -> BoxFuture<'a, Result<(), DriverError>> {
            Box::pin(async { Ok(()) })
        }
        fn open_relay<'a>(&'a self, _: &'a CancelToken) -> BoxFuture<'a, Result<(), DriverError>> {
            Box::pin(async { Ok(()) })
        }
        fn device_id(&self) -> Option<String> {
            Some(self.id.clone())
        }
        fn as_call_dialer(&self) -> Option<&dyn CallDialer> {
            Some(self)
        }
        fn as_call_hangupper(&self) -> Option<&dyn CallHangupper> {
            Some(self)
        }
    }
    impl CallDialer for OutDriver {
        fn dial_call<'a>(
            &'a self,
            _: &'a CancelToken,
            call_id: &'a str,
            _: MediaTarget,
        ) -> BoxFuture<'a, Result<(), DriverError>> {
            self.calls
                .lock()
                .unwrap()
                .insert(call_id.to_string(), false);
            // reported BEFORE DialCall returns: the hub must already know the id
            let sink = self.sink.lock().unwrap().clone().unwrap();
            sink.state_changed(call_id, "ringing");
            Box::pin(async { Ok(()) })
        }
    }
    impl CallHangupper for OutDriver {
        fn hangup_call(&self, call_id: &str) -> Result<(), DriverError> {
            if self.advance(call_id, "ended") {
                Ok(())
            } else {
                Err(DriverError::sentinel(Sentinel::NoSuchCall))
            }
        }
    }
    let h = Hub::detached();
    let d = Arc::new(OutDriver {
        id: "door-a".into(),
        ..OutDriver::default()
    });
    let sink = Arc::new(DeviceSink {
        s: h.events.clone(),
        device_id: "door-a".into(),
    });
    d.start(&h.ctx, sink).await.unwrap();
    assert!(h.put_if_absent("door-a", d.clone()));
    let (workers, rounds) = (12, 40);
    let mut joins = Vec::new();
    for _ in 0..workers {
        let (h, d) = (h.clone(), d.clone());
        joins.push(tokio::spawn(async move {
            let mut ids = Vec::new();
            for _ in 0..rounds {
                let v = val(h.dial(p(r#"{"device_id":"door-a"}"#)).await);
                let id = v["call_id"].as_str().unwrap().to_string();
                // two hang-ups and an advance race for the same call
                let body = format!("{{\"call_id\":\"{id}\"}}");
                let (a, b, _) = tokio::join!(h.hangup(p(&body)), h.hangup(p(&body)), async {
                    d.advance(&id, "answered")
                });
                let won = [a.is_ok(), b.is_ok()].iter().filter(|x| **x).count();
                assert_eq!(won, 1, "call {id}: {won} hangups succeeded, want exactly 1");
                ids.push(id);
            }
            ids
        }));
    }
    let mut seen = std::collections::HashSet::new();
    for j in joins {
        for id in j.await.unwrap() {
            assert!(seen.insert(id.clone()), "duplicate id {id}");
        }
    }
    assert_eq!(seen.len(), workers * rounds);
    // per call: ringing first, ended last, nothing after ended
    let mut per: std::collections::HashMap<String, Vec<String>> = Default::default();
    for (m, v) in notes(&h) {
        assert_eq!(m, "call_state_changed");
        assert_eq!(v["device_id"], "door-a");
        per.entry(v["call_id"].as_str().unwrap().to_string())
            .or_default()
            .push(v["state"].as_str().unwrap().to_string());
    }
    for id in &seen {
        let ev = &per[id];
        assert!(
            ev.len() >= 2 && ev[0] == "ringing" && ev[ev.len() - 1] == "ended",
            "call {id} events = {ev:?}"
        );
        assert!(
            !ev[..ev.len() - 1].iter().any(|e| e == "ended"),
            "call {id}: event after ended: {ev:?}"
        );
    }
    // the sink forgot every ended call
    for id in &seen {
        d.sink
            .lock()
            .unwrap()
            .clone()
            .unwrap()
            .state_changed(id, "active");
    }
    assert!(
        notes(&h).iter().all(|(_, v)| v["device_id"] == ""),
        "the sink still remembers ended calls"
    );
}

// R1-3 through the wire: a config serde cannot hold is `bad_params` with Go's text.
#[tokio::test]
async fn configure_with_an_unrepresentable_number_is_bad_params() {
    let h = Hub::detached();
    let e = h
        .configure(p(r#"{"driver":"gate_sip_server","config":{"a":1e999}}"#))
        .await
        .unwrap_err();
    assert_eq!(e.code, "bad_params");
    assert!(e.message.contains("number 1e999"), "{}", e.message);
    // and its lane key is empty, as Go's (Unmarshal fails); a lone surrogate keeps its lane
    assert_eq!(
        configure_key(r#"{"config":{"device_id":"a","x":1e999}}"#),
        ""
    );
    assert_eq!(
        configure_key(r#"{"config":{"device_id":"a\ud800"}}"#),
        "device_id:a\u{fffd}"
    );
}

// R1-120: a secret in any configure is known to the panic hook.
#[tokio::test]
async fn configure_registers_the_secrets_it_carries() {
    let h = Hub::detached();
    let _ = h
        .configure(p(
            r#"{"driver":"nope","config":{"password":"pw-R1-120-aaaa","protect_api_key":"key-R1-120-bbbb","note":"ordinary-R1-120"}}"#,
        ))
        .await;
    assert_eq!(
        crate::panic_hook::scrub("pw-R1-120-aaaa key-R1-120-bbbb ordinary-R1-120"),
        "*** *** ordinary-R1-120"
    );
}

// TestGateSipServer_FreshProcessHasNoDevice, TestGateSipServer_RefusalsLeaveNothingRegistered
// (cmd/portico-transport/gate_sip_server_wiring_test.go)
#[tokio::test]
async fn refusals_leave_nothing_registered() {
    let mut r = Rig::new("refuse").await;
    assert!(r.hub.all().is_empty(), "a fresh process owns a device");
    for (name, cfg) in [
        ("no sip_listen", json!({"password": "12345678"})),
        ("no password", json!({"sip_listen": "127.0.0.1:0"})),
        (
            "short password",
            json!({"sip_listen": "127.0.0.1:0", "password": "short"}),
        ),
        (
            "LAN bind, no flags",
            json!({"sip_listen": "0.0.0.0:5060", "password": "12345678"}),
        ),
        (
            "LAN bind, no allowlist",
            json!({"sip_listen": "0.0.0.0:5060", "password": "12345678", "allow_non_loopback": true}),
        ),
        (
            "allow_from /0",
            json!({"sip_listen": "0.0.0.0:5060", "password": "12345678", "allow_non_loopback": true, "allow_from": ["0.0.0.0/0"]}),
        ),
    ] {
        let (ans, _) = r
            .call(
                "configure",
                json!({"driver": "gate_sip_server", "config": cfg}),
            )
            .await;
        assert!(
            ans.contains("\"code\":\"configure_failed\""),
            "{name}: {ans}"
        );
    }
    assert!(r.hub.all().is_empty(), "devices registered after refusals");
    r.srv.close().await;
}

// TestGateSipServer_ConfiguredOnLoopbackIsKeyedByItsDeviceID, TestGateSipServer_IsKnownToTheShippedBinary,
// TestConfigureOfAnAlreadyConfiguredIdIsRefused, plus the configure reply bytes and deconfigure.
#[tokio::test]
async fn configure_starts_the_server_and_deconfigure_stops_it() {
    let mut r = Rig::new("conf").await;
    let (ans, _) = r.call("configure", server_cfg(json!({}))).await;
    // the reply is the Capabilities struct in Go's field order, not sorted keys
    assert_eq!(
        ans,
        "{\"id\":1,\"result\":{\"InboundAudio\":0,\"OutboundAudio\":0,\"DirectCapable\":false,\"HasRelay\":false,\"RTSPTemplate\":\"\",\"ReportsAvailability\":false,\"StationRegistered\":false,\"Features\":[\"call_stats\"]}}"
    );
    assert!(r.hub.get("gate-sip-server:test").is_some());
    assert!(r.hub.get("").is_none(), "stored under the empty id");
    // a second configure of the same id is refused, it does not rebind
    let (ans, _) = r.call("configure", server_cfg(json!({}))).await;
    assert!(ans.contains("\"code\":\"already_configured\""), "{ans}");
    // unknown device
    let (ans, _) = r.call("deconfigure", json!({"device_id": "nope"})).await;
    assert!(ans.contains("\"unknown_device\""), "{ans}");
    let (ans, _) = r
        .call("deconfigure", json!({"device_id": "gate-sip-server:test"}))
        .await;
    assert_eq!(ans, "{\"id\":4,\"result\":{\"ok\":true}}");
    assert!(r.hub.all().is_empty());
    // configurable again after the deconfigure (the port was released)
    let (ans, _) = r.call("configure", server_cfg(json!({}))).await;
    assert!(ans.contains("\"InboundAudio\":0"), "{ans}");
    r.hub.shutdown().await;
    assert!(r.hub.all().is_empty());
    let (ans, _) = r
        .call("configure", server_cfg(json!({"device_id": "x"})))
        .await;
    // the WHOLE line, message included (R2-40): the text is the contract's `shutting_down` of `configure`
    assert_eq!(
        ans,
        "{\"id\":6,\"error\":{\"code\":\"shutting_down\",\"message\":\"transport is stopping\"}}"
    );
    r.srv.close().await;
}

// dial/answer/open_relay/hangup/external_ring/stream_url/talk_health routed to the driver
// exactly as main.go does for this driver.
#[tokio::test]
async fn device_verbs_route_to_the_driver() {
    let mut r = Rig::new("verbs").await;
    let (ans, _) = r.call("configure", server_cfg(json!({}))).await;
    assert!(ans.contains("InboundAudio"), "{ans}");
    let dev = json!({"device_id": "gate-sip-server:test"});
    // legacy dial: not_supported through driverError
    let (ans, _) = r.call("dial", dev.clone()).await;
    assert!(
        ans.contains("\"code\":\"not_supported\"")
            && ans.contains(
                "gate_sip_server: Dial: not_supported (outbound calls are not part of E1)"
            ),
        "{ans}"
    );
    let (ans, _) = r.call("open_relay", dev.clone()).await;
    assert!(
        ans.contains("\"code\":\"not_supported\"")
            && ans.contains("gate_sip_server: OpenRelay: not_supported"),
        "{ans}"
    );
    // Answer with no call: the package's own ErrNoCall, so answer_failed
    let (ans, _) = r.call("answer", dev.clone()).await;
    assert!(
        ans.contains("\"code\":\"answer_failed\"") && ans.contains("no call to answer"),
        "{ans}"
    );
    let (ans, _) = r
        .call(
            "answer",
            json!({"device_id": "gate-sip-server:test", "host": "1.2.3.4", "port": 5}),
        )
        .await;
    assert!(ans.contains("\"code\":\"not_supported\""), "{ans}");
    let (ans, _) = r.call("answer", json!({"device_id": "nope"})).await;
    assert!(
        ans.contains("\"unknown_device\"") && ans.contains("nope"),
        "{ans}"
    );
    // hangup: no driver holds the call
    let (ans, _) = r
        .call("hangup", json!({"call_id": "c1", "reason": "x"}))
        .await;
    assert!(ans.contains("\"code\":\"not_implemented\""), "{ans}");
    // external_ring / talk_health / stream_url
    let (ans, _) = r
        .call(
            "external_ring",
            json!({"device_id": "gate-sip-server:test", "call_id": "c1", "hold": 5}),
        )
        .await;
    assert!(
        ans.contains("\"code\":\"not_supported\"")
            && ans.contains("gate-sip-server:test does not take external rings"),
        "{ans}"
    );
    let (ans, _) = r.call("talk_health", dev.clone()).await;
    assert!(
        ans.contains("gate-sip-server:test has no talk health block"),
        "{ans}"
    );
    let (ans, _) = r.call("stream_url", dev.clone()).await;
    assert!(
        ans.contains("this driver has no minted stream URL"),
        "{ans}"
    );
    r.hub.shutdown().await;
    r.srv.close().await;
}

// an unknown driver key, and the refusals of two drivers' own configure
#[tokio::test]
async fn unknown_drivers_and_the_driver_refusals() {
    let mut r = Rig::new("unk").await;
    let (ans, _) = r
        .call(
            "configure",
            json!({"driver": "unifi_protect_talk", "config": {}}),
        )
        .await;
    // ported since slice 7b: its own configure answers
    assert!(
        ans.contains("\"code\":\"configure_failed\"")
            && ans.contains("unifi_protect_talk: device_id, protect_host, protect_api_key and camera_id are required"),
        "{ans}"
    );
    let (ans, _) = r
        .call("configure", json!({"driver": "nope", "config": {}}))
        .await;
    assert!(ans.contains("\"unknown_driver\""), "{ans}");
    let (ans, _) = r
        .call(
            "configure",
            json!({"driver": "gate_sip_server", "config": {"device_id": "g1"}}),
        )
        .await;
    assert!(
        ans.contains("gate_sip_server: sip_listen is required (no default: this mode never binds by accident)"),
        "{ans}"
    );
    r.srv.close().await;
}
