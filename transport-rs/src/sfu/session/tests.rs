use super::*;

fn env(k: &'static str, v: &'static str) -> impl Fn(&str) -> Option<String> + Copy {
    move |name| (name == k).then(|| v.to_string())
}

fn core() -> (Core, Arc<Shared>) {
    let jitter = UplinkBuffer::new();
    let path = Arc::new(UplinkPath::new(jitter));
    let shared = Arc::new(Shared {
        call_id: "unit".to_string(),
        cb: Mutex::new(CbState::default()),
        connected: AtomicBool::new(false),
        offered: AtomicBool::new(false),
        neg: Mutex::new(Negotiated::default()),
        stats: Stats::default(),
        done: CancelToken::new(),
    });
    let c = Core::new(
        shared.clone(),
        SessionConfig::loopback(),
        path,
        CancelToken::new(),
    );
    (c, shared)
}

fn ice(c: &mut Core, s: IceConnectionState) {
    c.event(Event::IceConnectionStateChange(s)).unwrap();
}

// R2-114: the 64-bit media time handed to str0m carries on across the wrap of the 32-bit RTP clock
#[test]
fn the_downlink_clock_extends_past_the_32_bit_wrap() {
    let (mut c, _sh) = core();
    // park the counter 400 ticks before the wrap (the call returns the old value, 0)
    c.rtp.next_timestamp(u32::MAX - 399);
    let frame = vec![0u8; 1280]; // 40 ms of 16 kHz L16 -> 320 PCMU bytes -> 320 ticks
    let mut media_times = Vec::new();
    for _ in 0..3 {
        c.write_audio(frame.clone()).unwrap();
        media_times.push(c.ts_ext + u64::from(c.last_ts));
    }
    let start = (1u64 << 32) - 400;
    assert_eq!(
        media_times,
        vec![start, start + 320, start + 640],
        "the media time must go on past 2^32, not fall back to a small value"
    );
    assert_eq!(c.ts_ext, 1 << 32, "extended exactly once");
}

// R1-22: the grace is cancelled by a return to a working pair, not by any other state
#[test]
fn only_a_recovery_cancels_the_disconnected_grace() {
    let (mut c, sh) = core();
    c.event(Event::Connected).unwrap();
    ice(&mut c, IceConnectionState::Disconnected);
    let dl = c.disc_deadline.expect("grace started");
    // a late candidate for the dead peer forms new pairs: Checking, then New
    for s in [IceConnectionState::Checking, IceConnectionState::New] {
        ice(&mut c, s);
        assert_eq!(
            c.disc_deadline,
            Some(dl),
            "{s:?} restarted or cancelled the grace"
        );
    }
    // a second Disconnected does not extend it
    ice(&mut c, IceConnectionState::Disconnected);
    assert_eq!(c.disc_deadline, Some(dl));
    // R1-78: Go's Connected() is false while disconnected, true again on recovery
    assert!(!sh.connected.load(Ordering::SeqCst));
    ice(&mut c, IceConnectionState::Completed);
    assert_eq!(c.disc_deadline, None);
    assert!(sh.connected.load(Ordering::SeqCst));
    // and the connect timeout does not apply to a session that was connected once, even
    // while `Session::connected()` is false
    ice(&mut c, IceConnectionState::Disconnected);
    assert!(!sh.connected.load(Ordering::SeqCst));
    let far = Instant::now() + Duration::from_secs(3600);
    c.connect_deadline = Instant::now() - Duration::from_secs(1);
    assert_ne!(c.next_deadline(far), c.connect_deadline);
    assert!(c.on_timer().is_ok());
}

// R1-22/78: before the first connection ICE `Connected` is not the session's Connected
#[test]
fn ice_connected_before_dtls_does_not_mark_the_session_connected() {
    let (mut c, sh) = core();
    ice(&mut c, IceConnectionState::Connected);
    assert!(!sh.connected.load(Ordering::SeqCst));
    c.event(Event::Connected).unwrap();
    assert!(sh.connected.load(Ordering::SeqCst));
}

#[test]
fn durations_from_the_environment_are_clamped_and_garbage_falls_back() {
    let d = SessionConfig::default();
    for k in [
        "PORTICO_SFU_DISCONNECT_GRACE_SECS",
        "PORTICO_SFU_CONNECT_TIMEOUT_SECS",
    ] {
        for (v, want) in [
            ("0", None),
            ("-5", None),
            ("NaN", None),
            ("inf", None),
            ("-inf", None),
            ("abc", None),
            ("", None),
            (",", None),
            ("1e30", Some(3600.0)),
            ("1e18", Some(3600.0)),
            ("99999", Some(3600.0)),
            ("0.001", Some(1.0)),
            ("2.5", Some(2.5)),
            (" 12 ", Some(12.0)),
        ] {
            let got = parse_secs(v);
            assert_eq!(got.map(|g| g.as_secs_f64()), want, "{k}={v:?}");
            // and through from_env: the field is the default or the clamped value
            let c = SessionConfig::from_env(match k {
                "PORTICO_SFU_DISCONNECT_GRACE_SECS" => env(
                    "PORTICO_SFU_DISCONNECT_GRACE_SECS",
                    Box::leak(v.to_string().into_boxed_str()),
                ),
                _ => env(
                    "PORTICO_SFU_CONNECT_TIMEOUT_SECS",
                    Box::leak(v.to_string().into_boxed_str()),
                ),
            });
            let field = if k.contains("GRACE") {
                c.disconnect_grace
            } else {
                c.connect_timeout
            };
            let dflt = if k.contains("GRACE") {
                d.disconnect_grace
            } else {
                d.connect_timeout
            };
            assert_eq!(
                field,
                want.map_or(dflt, Duration::from_secs_f64),
                "{k}={v:?}"
            );
            // `Instant + Duration` of the biggest accepted value does not overflow
            let _ = Instant::now() + field;
        }
    }
}

// R1-163: every PORTICO_SFU_* value has one place and one validation style
#[test]
fn trace_rtp_and_loopback_come_from_the_one_environment_reader() {
    let c = SessionConfig::from_env(env("PORTICO_SFU_TRACE_RTP", "1"));
    assert!(c.trace_rtp);
    assert!(!SessionConfig::from_env(env("PORTICO_SFU_TRACE_RTP", "yes")).trace_rtp);
    assert!(!SessionConfig::default().trace_rtp);
    assert!(
        SessionConfig::from_env(env("PORTICO_SFU_INCLUDE_LOOPBACK", "1"))
            .ifaces
            .include_loopback
    );
}

#[test]
fn stun_retransmits_and_session_cap_are_validated() {
    let r = |v: &'static str| {
        SessionConfig::from_env(env("PORTICO_SFU_MAX_STUN_RETRANSMITS", v)).max_stun_retransmits
    };
    assert_eq!(r("5"), Some(5));
    assert_eq!(r("1"), Some(1));
    assert_eq!(r("20"), Some(20));
    for bad in [
        "0",
        "21",
        "-1",
        "abc",
        "",
        ",",
        "1e3",
        "99999999999999999999",
    ] {
        assert_eq!(r(bad), None, "{bad:?}");
    }
    let m =
        |v: &'static str| SessionConfig::from_env(env("PORTICO_SFU_MAX_SESSIONS", v)).max_sessions;
    assert_eq!((m("3"), m("0"), m("1000"), m("64")), (3, 1, 64, 64));
    for bad in ["abc", "", ",", "-2", "NaN"] {
        assert_eq!(m(bad), DEFAULT_MAX_SESSIONS, "{bad:?}");
    }
}
