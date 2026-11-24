//! Golden vectors produced by the REAL Go `pkg/driver` and `pkg/driver/gatesipserver`
//! (tools/gen_golden, see README.md) replayed against the Rust port: the configure reply of
//! every shipped driver, config validation, codec choice and the SDP answer, A-law and the
//! link-rate converters, the CallAudio frame channels and the driver glue before Start.

use std::path::PathBuf;
use std::sync::{Arc, Mutex};

use serde_json::{Map, Value};
use transport_rs::cancel::CancelToken;
use transport_rs::driver::gatesipserver::audio::{
    apply_gain, from_link_rate, to_link_rate, AudioClient, AudioStats, CallAudioImpl, ClientHost,
};
use transport_rs::driver::gatesipserver::config::parse_config;
use transport_rs::driver::gatesipserver::media::{
    alaw_decode, alaw_encode, choose_media, decode_alaw, encode_alaw, sdp_for, Media,
};
use transport_rs::driver::gatesipserver::GateSipServer;
use transport_rs::driver::{
    registry, AudioClaimer, AudioTopology, CallAudio, CallDialer, CallHangupper, Capabilities,
    ConnectAnswerer, Driver, DriverError, MediaTarget, Sentinel,
};

fn load(name: &str) -> Value {
    let mut p = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    p.push("tests/golden");
    p.push(name);
    let text = std::fs::read_to_string(&p).unwrap_or_else(|e| panic!("{}: {e}", p.display()));
    serde_json::from_str(&text).unwrap()
}

fn unhex(s: &str) -> Vec<u8> {
    assert!(s.len().is_multiple_of(2), "odd hex");
    (0..s.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap())
        .collect()
}

fn hexs(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02x}")).collect()
}

fn s<'a>(v: &'a Value, k: &str) -> &'a str {
    v.get(k).and_then(Value::as_str).unwrap_or("")
}

fn i(v: &Value, k: &str) -> i64 {
    v.get(k).and_then(Value::as_i64).unwrap_or(0)
}

fn b(v: &Value, k: &str) -> bool {
    v.get(k).and_then(Value::as_bool).unwrap_or(false)
}

fn token() -> CancelToken {
    CancelToken::new()
}

// ---- capabilities ---------------------------------------------------------------------------

fn caps_of(v: &Value) -> Capabilities {
    Capabilities {
        inbound_audio: AudioTopology(i(v, "inbound_audio")),
        outbound_audio: AudioTopology(i(v, "outbound_audio")),
        direct_capable: b(v, "direct_capable"),
        has_relay: b(v, "has_relay"),
        rtsp_template: s(v, "rtsp_template").to_string(),
        reports_availability: b(v, "reports_availability"),
        can_dial: b(v, "can_dial"),
        ..Capabilities::default()
    }
}

#[test]
fn golden_capabilities() {
    let g = load("driver_caps.json");
    let mut n = 0;
    // every shipped driver's configure reply: the struct marshals to Go's bytes
    for v in g["drivers"]
        .as_array()
        .unwrap()
        .iter()
        .chain(g["bare"].as_array().unwrap())
    {
        let name = s(v, "name");
        assert_eq!(caps_of(v).to_wire(), s(v, "wire"), "{name}");
        n += 1;
        // R2-71: EVERY row goes through the Rust driver itself (registry + configure +
        // capabilities), not only a Capabilities rebuilt from the golden's own fields (the
        // observer's last case, claims + dial_enabled before any poll, is the one whose CanDial is
        // still false)
        let driver = s(v, "driver");
        if driver.is_empty() {
            continue; // a `bare` row: a Capabilities struct with no driver behind it
        }
        let mut d = registry()[driver]();
        if let Some(cfg) = v.get("config") {
            d.configure(cfg.as_object().unwrap())
                .unwrap_or_else(|e| panic!("{name}: {e}"));
        }
        // DEV-518 added StationRegistered to the end of gate_sip_server's reply; Go has none
        let wire = d.capabilities().to_wire();
        let wire = match wire.find(",\"StationRegistered\"") {
            Some(i) => format!("{}}}", &wire[..i]),
            None => wire,
        };
        assert_eq!(wire, s(v, "wire"), "{name}");
        n += 1;
    }
    // TestServerCanDialAppearsOnTheWireOnlyOnceTheStationIsRegistered
    for v in g["registered"].as_array().unwrap() {
        let name = s(v, "name");
        let dial = v["dial_enabled"].as_bool().unwrap_or(false);
        let cfg = transport_rs::driver::gatesipserver::Config {
            dial_enabled: dial,
            ..Default::default()
        };
        assert_eq!(
            GateSipServer::capabilities_for(&cfg, false).to_wire(),
            s(v, "before"),
            "{name} before"
        );
        assert_eq!(
            GateSipServer::capabilities_for(&cfg, true).to_wire(),
            s(v, "after"),
            "{name} after"
        );
        n += 2;
    }
    // sentinel texts, states, topology numbering and the driver keys we own
    let texts: std::collections::HashMap<&str, &str> = g["texts"]
        .as_array()
        .unwrap()
        .iter()
        .map(|t| (s(t, "name"), s(t, "text")))
        .collect();
    for (name, sent) in [
        ("ErrNotSupported", Sentinel::NotSupported),
        ("ErrAlreadyAnswered", Sentinel::AlreadyAnswered),
        ("ErrClaimsDisabled", Sentinel::ClaimsDisabled),
        ("ErrBusy", Sentinel::Busy),
        ("ErrNoSuchCall", Sentinel::NoSuchCall),
    ] {
        assert_eq!(sent.text(), texts[name], "{name}");
        n += 1;
    }
    assert_eq!(texts["state ringing"], transport_rs::driver::STATE_RINGING);
    assert_eq!(
        texts["state answered"],
        transport_rs::driver::STATE_ANSWERED
    );
    assert_eq!(texts["state active"], transport_rs::driver::STATE_ACTIVE);
    assert_eq!(texts["state ended"], transport_rs::driver::STATE_ENDED);
    let topo: Vec<i64> = g["topology"]
        .as_array()
        .unwrap()
        .iter()
        .map(|x| x.as_i64().unwrap())
        .collect();
    assert_eq!(
        topo,
        [
            AudioTopology::SIP_RTP.0,
            AudioTopology::IN_VIDEO_STREAM.0,
            AudioTopology::TALKBACK_PUSH.0,
            AudioTopology::NO_AUDIO.0
        ]
    );
    let go_keys: Vec<&str> = g["registry_keys"]
        .as_array()
        .unwrap()
        .iter()
        .map(|k| k.as_str().unwrap())
        .collect();
    for k in registry().keys() {
        assert!(go_keys.contains(k), "Rust registers {k}, Go does not");
    }
    assert_eq!(n, 35, "only {n} capability checks ran"); // exact (R2-107): a truncated golden file must fail
}

// ---- config ---------------------------------------------------------------------------------

#[test]
fn golden_config() {
    let vecs = load("gss_config.json");
    let vecs = vecs.as_array().unwrap();
    assert_eq!(vecs.len(), 175, "{} config vectors", vecs.len()); // exact (R2-107): a truncated golden file must fail
    let (mut ok, mut bad) = (0, 0);
    for v in vecs {
        let name = s(v, "name");
        let parsed: Value = serde_json::from_str(s(v, "in")).unwrap();
        let input: Map<String, Value> = match parsed {
            Value::Object(m) => m,
            Value::Null => Map::new(),
            other => panic!("{name}: config {other}"),
        };
        match parse_config(&input) {
            Err(e) => {
                bad += 1;
                assert!(!b(v, "ok"), "{name}: Rust refused, Go accepted: {e}");
                assert_eq!(e, s(v, "err"), "{name}");
            }
            Ok(c) => {
                ok += 1;
                assert!(
                    b(v, "ok"),
                    "{name}: Rust accepted, Go refused with {}",
                    s(v, "err")
                );
                let w = &v["cfg"];
                assert_eq!(c.device_id, s(w, "device_id"), "{name}");
                assert_eq!(c.sip_listen, s(w, "sip_listen"), "{name}");
                assert_eq!(c.rtp_port, i(w, "rtp_port"), "{name}");
                assert_eq!(c.user, s(w, "user"), "{name}");
                assert_eq!(c.realm, s(w, "realm"), "{name}");
                assert_eq!(c.password, s(w, "password"), "{name}");
                assert_eq!(c.ring_limit.as_millis() as i64, i(w, "ring_ms"), "{name}");
                assert_eq!(
                    c.orphan_grace.as_millis() as i64,
                    i(w, "orphan_ms"),
                    "{name}"
                );
                assert_eq!(c.allow_non_loopback, b(w, "allow_non_loopback"), "{name}");
                assert_eq!(c.dial_enabled, b(w, "dial_enabled"), "{name}");
                let got: Vec<String> = c.allow_from.iter().map(|p| p.to_string()).collect();
                let want: Vec<String> = w["allow_from"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|x| x.as_str().unwrap().to_string())
                    .collect();
                assert_eq!(got, want, "{name}: allow_from");
                for p in v["allowed"].as_array().into_iter().flatten() {
                    assert_eq!(
                        c.allowed(&unhex(s(p, "ip_hex"))),
                        b(p, "allowed"),
                        "{name}: allowed({})",
                        s(p, "ip_hex")
                    );
                }
            }
        }
    }
    assert_eq!((ok, bad), (56, 119), "accepted, refused"); // exact (R2-107)
}

// ---- media ----------------------------------------------------------------------------------

fn media_of(v: &Value) -> Media {
    Media {
        pt: i(v, "pt"),
        name: s(v, "name").to_string(),
        rate: i(v, "rate") as u32,
        frame_bytes: i(v, "frame_bytes") as usize,
        alaw: b(v, "alaw"),
    }
}

#[test]
fn golden_media_choice_and_sdp_answer() {
    let g = load("gss_media.json");
    let choose = g["choose"].as_array().unwrap();
    assert_eq!(choose.len(), 67, "{} offers", choose.len()); // exact (R2-107): a truncated golden file must fail
    for v in choose {
        let name = s(v, "name");
        let sdp = String::from_utf8(unhex(s(v, "sdp_hex"))).unwrap();
        let (m, note) = choose_media(&sdp);
        assert_eq!(m, media_of(&v["media"]), "{name}");
        assert_eq!(note, s(v, "note"), "{name}");
        assert_eq!(sdp_for("10.1.2.3", 40000, &m), s(v, "answer"), "{name}");
    }
    let sdps = g["sdp"].as_array().unwrap();
    assert_eq!(sdps.len(), 96); // exact (R2-107): a truncated golden file must fail
    for v in sdps {
        assert_eq!(
            sdp_for(s(v, "local_ip"), i(v, "port"), &media_of(&v["media"])),
            s(v, "answer"),
            "{v}"
        );
    }
}

// ---- A-law and the converters ----------------------------------------------------------------

#[test]
fn golden_alaw_and_converters() {
    let g = load("gss_alaw.json");
    let dec = unhex(s(&g, "decode_table_hex"));
    for c in 0..256usize {
        let want = i16::from_le_bytes([dec[2 * c], dec[2 * c + 1]]);
        assert_eq!(decode_alaw(c as u8), want, "decode {c:#x}");
    }
    let enc = unhex(s(&g, "encode_all_int16_hex"));
    assert_eq!(enc.len(), 65536);
    for (idx, want) in enc.iter().enumerate() {
        let v = idx as i32 - 32768;
        assert_eq!(encode_alaw(v as i16), *want, "encode {v}");
    }
    let mut n = 0;
    for v in g["encode"].as_array().unwrap() {
        assert_eq!(
            hexs(&alaw_encode(&unhex(s(v, "in_hex")))),
            s(v, "out_hex"),
            "encode {}",
            s(v, "name")
        );
        n += 1;
    }
    for v in g["decode"].as_array().unwrap() {
        assert_eq!(
            hexs(&alaw_decode(&unhex(s(v, "in_hex")))),
            s(v, "out_hex"),
            "decode {}",
            s(v, "name")
        );
        n += 1;
    }
    for v in g["gain"].as_array().unwrap() {
        let got = apply_gain(&unhex(s(v, "in_hex")), i(v, "arg"));
        assert_eq!(
            hexs(&got),
            s(v, "out_hex"),
            "gain {} x{}",
            s(v, "name"),
            i(v, "arg")
        );
        n += 1;
    }
    for (key, f) in [
        ("to_link", to_link_rate as fn(&[u8], i64) -> Option<Vec<u8>>),
        ("from_link", from_link_rate),
    ] {
        for v in g[key].as_array().unwrap() {
            let got = f(&unhex(s(v, "in_hex")), i(v, "arg"));
            let label = format!("{key} {} @{}", s(v, "name"), i(v, "arg"));
            assert_eq!(got.is_none(), b(v, "nil"), "{label}: nil");
            assert_eq!(hexs(&got.unwrap_or_default()), s(v, "out_hex"), "{label}");
            n += 1;
        }
    }
    assert_eq!(n, 210, "{n} converter vectors"); // exact (R2-107): a truncated golden file must fail
}

// ---- CallAudio scripts -----------------------------------------------------------------------

/// The slice of Go's `Server` that `AudioClient.release` and `endLocked` touch.
struct Host {
    current: Mutex<Option<Arc<AudioClient>>>,
}

impl ClientHost for Host {
    fn release(&self, client: &AudioClient, deliberate: bool) {
        let mut cur = self.current.lock().unwrap();
        match &*cur {
            Some(c) if std::ptr::eq(Arc::as_ptr(c), client) => {}
            _ => return,
        }
        *cur = None;
        client.close(if deliberate {
            "the client said goodbye"
        } else {
            "the client detached"
        });
    }
}

async fn settle() {
    for _ in 0..200 {
        tokio::task::yield_now().await;
    }
}

#[tokio::test]
async fn golden_call_audio_scripts() {
    // Go's hard-clamp gain and unity uplink, not the production soft limiter and AGC
    transport_rs::driver::gatesipserver::audio::set_legacy_audio(true);
    let g = load("gss_audio.json");
    let scripts = g.as_array().unwrap();
    assert_eq!(scripts.len(), 30, "{} scripts", scripts.len()); // exact (R2-107): a truncated golden file must fail
    let mut steps = 0;
    for sc in scripts {
        let name = s(sc, "name");
        let stats = Arc::new(AudioStats::default());
        let host = Arc::new(Host {
            current: Mutex::new(None),
        });
        let weak: std::sync::Weak<dyn ClientHost> =
            Arc::downgrade(&(host.clone() as Arc<dyn ClientHost>));
        let cl = AudioClient::new(
            i(sc, "rate") as u32,
            i(sc, "frame_bytes") as usize,
            weak,
            stats.clone(),
        );
        *host.current.lock().unwrap() = Some(cl.clone());
        let mut mic = cl.take_microphone().unwrap();
        let audio = CallAudioImpl::new("call-1", cl.clone());
        let driver = GateSipServer::default();
        driver.set_audio(audio.clone());
        let out = audio.output();
        let input = audio.input();

        let ops = sc["ops"].as_array().unwrap();
        let results = sc["results"].as_array().unwrap();
        assert_eq!(ops.len(), results.len(), "{name}");
        for (n, (op, want)) in ops.iter().zip(results).enumerate() {
            let at = format!("{name} / step {n} ({})", s(op, "op"));
            let data = unhex(s(op, "hex"));
            match s(op, "op") {
                "push" => {
                    cl.push_down(data);
                }
                "in" => {
                    let _ = input.try_send(data);
                }
                "attach" => audio.attach(),
                "detach" => audio.detach(),
                "goodbye" => cl.goodbye(),
                "client_detach" => cl.detach(),
                "close_call" => {
                    if let Some(c) = host.current.lock().unwrap().take() {
                        c.close(s(op, "why"));
                    }
                }
                "claim" | "drain" => {}
                other => panic!("{at}: unknown op {other}"),
            }
            settle().await;

            let mut got_out = Vec::new();
            let mut got_sent = Vec::new();
            let mut out_closed = false;
            if !b(op, "no_drain") {
                let mut rx = out.lock().await;
                loop {
                    match rx.try_recv() {
                        Ok(f) => got_out.push(hexs(&f)),
                        Err(tokio::sync::mpsc::error::TryRecvError::Disconnected) => {
                            out_closed = true;
                            break;
                        }
                        Err(_) => break,
                    }
                }
                while let Ok(f) = mic.try_recv() {
                    got_sent.push(hexs(&f));
                }
            }
            let strs = |k: &str| -> Vec<String> {
                want.get(k)
                    .and_then(Value::as_array)
                    .map(|a| a.iter().map(|x| x.as_str().unwrap().to_string()).collect())
                    .unwrap_or_default()
            };
            assert_eq!(got_out, strs("out"), "{at}: Out");
            assert_eq!(got_sent, strs("sent"), "{at}: frames handed to Send");
            assert_eq!(out_closed, b(want, "out_closed"), "{at}: Out closed");
            assert_eq!(cl.done().is_cancelled(), b(want, "done"), "{at}: done");
            assert_eq!(cl.reason(), s(want, "reason"), "{at}: reason");
            assert_eq!(cl.leg(), b(want, "leg"), "{at}: leg");
            assert_eq!(
                driver.claimed_audio("call-1").is_some(),
                b(want, "claimed"),
                "{at}: claimed"
            );
            let (to_app, from_app, dropped_up, dropped_down) = stats.snapshot();
            let w = &want["stats"];
            assert_eq!(to_app, i(w, "frames_to_app"), "{at}: frames_to_app");
            assert_eq!(dropped_down, i(w, "dropped_down"), "{at}: dropped_down");
            assert_eq!(from_app, i(w, "frames_from_app"), "{at}: frames_from_app");
            assert_eq!(dropped_up, i(w, "dropped_up"), "{at}: dropped_up");
            assert_eq!(
                host.current.lock().unwrap().is_some(),
                b(w, "app_attached"),
                "{at}: app_attached"
            );
            steps += 1;
        }
    }
    assert_eq!(steps, 184, "{steps} audio steps"); // exact (R2-107): a truncated golden file must fail
}

// ---- the driver glue before Start -------------------------------------------------------------

#[tokio::test]
async fn golden_driver_glue_before_start() {
    let g = load("gss_driver.json");
    let mut steps = 0;
    for sc in g.as_array().unwrap() {
        let name = s(sc, "name");
        let mut d = GateSipServer::default();
        let t = token();
        let results = sc["results"].as_array().unwrap();
        for (n, (op, want)) in sc["ops"]
            .as_array()
            .unwrap()
            .iter()
            .zip(results)
            .enumerate()
        {
            let at = format!("{name} / step {n} ({})", s(op, "op"));
            let id = s(op, "id");
            let target = MediaTarget {
                host: s(op, "host").to_string(),
                port: i(op, "port"),
            };
            let mut err: Option<DriverError> = None;
            let mut string = String::new();
            let mut boolean: Option<bool> = None;
            let mut caps = String::new();
            match s(op, "op") {
                "configure" => {
                    let cfg: Value = serde_json::from_str(s(op, "json")).unwrap();
                    err = d.configure(cfg.as_object().unwrap()).err();
                }
                "device_id" => string = d.device_id().unwrap_or_default(),
                "caps" => caps = d.capabilities().to_wire(),
                "dial" => err = d.dial(&t, MediaTarget::default()).await.err(),
                "open_relay" => err = d.open_relay(&t).await.err(),
                "answer" => err = d.answer(&t, target).await.err(),
                "dial_call" => err = d.dial_call(&t, id, MediaTarget::default()).await.err(),
                "answer_with_audio" => {
                    let r = d.answer_with_audio(&t, id).await;
                    boolean = Some(r.is_ok());
                    err = r.err();
                }
                "ringing_call" => boolean = Some(ConnectAnswerer::ringing_call(&d, id)),
                "claimed_audio" => boolean = Some(d.claimed_audio(id).is_some()),
                "hangup_call" => err = d.hangup_call(id).err(),
                "stop" => err = d.stop().await.err(),
                other => panic!("{at}: unknown op {other}"),
            }
            assert_eq!(
                err.as_ref().map(DriverError::message).unwrap_or(""),
                s(want, "err"),
                "{at}: err"
            );
            if err.is_some() {
                let e = err.as_ref().unwrap();
                assert_eq!(
                    e.is(Sentinel::NotSupported),
                    b(want, "not_supported"),
                    "{at}"
                );
                assert_eq!(e.is(Sentinel::NoSuchCall), b(want, "no_such_call"), "{at}");
                assert_eq!(
                    e.is(Sentinel::AlreadyAnswered),
                    b(want, "already_answered"),
                    "{at}"
                );
            }
            assert_eq!(string, s(want, "str"), "{at}: string");
            assert_eq!(
                boolean,
                want.get("bool").and_then(Value::as_bool),
                "{at}: bool"
            );
            // the golden file re-indents the reply, so compare the parsed objects, and the
            // field order separately (the golden_capabilities test pins the bytes)
            match want.get("caps") {
                Some(w) => {
                    // DEV-518 keys are new on the wire; Go's golden has none
                    let mut got = serde_json::from_str::<Value>(&caps).unwrap();
                    if let Some(o) = got.as_object_mut() {
                        o.remove("StationRegistered");
                        o.remove("LastRegisterAt");
                        o.remove("Features"); // DEV-811 P3: new on the wire; Go has none
                    }
                    assert_eq!(got, *w, "{at}: caps")
                }
                None => assert!(caps.is_empty(), "{at}: caps"),
            }
            steps += 1;
        }
    }
    assert_eq!(steps, 41, "{steps} glue steps"); // exact (R2-107): a truncated golden file must fail
}
