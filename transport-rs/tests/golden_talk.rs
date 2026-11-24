//! Golden vectors produced by the REAL Go `pkg/pacer` and `pkg/driver/unifiprotect` talk family
//! (tools/gen_golden/{pacer,talk}_gen.go, talk_scen_gen.go, see README.md) replayed against the
//! Rust port: the pacer over 57 scripted clocks, the helpers (`ffmpegArgs`, `validSink`,
//! `downlinkArgs`, `gainPCM`, `scrubLine`, `exec.LookPath`), `TalkDriver.Configure` over a corpus,
//! `checkSessionGuard` against scripted HTTP answers, and 31 whole-driver scenarios against a fake
//! console, a fake guard and a fake ffmpeg script. The replay requires every recorded result to be
//! equal, bar the entries of the `*_DIFFERENCES` tables (each is a numbered difference in
//! PARITY.md; a table entry that stops differing fails the test, so a table cannot go stale).

#![allow(unsafe_code)] // a libc call in a test helper (kill / signal); the library has none

mod common;

use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use common::talk::*;
use common::unifi::TEST_KEY;
use serde_json::{json, Map, Value};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpListener;
use transport_rs::cancel::CancelToken;
use transport_rs::driver::unifiprotect::ffmpeg::{
    downlink_args, ffmpeg_args, gain_pcm, look_path, scrub_line, valid_sink,
};
use transport_rs::driver::unifiprotect::talk::{TalkDriver, TalkTiming};
use transport_rs::driver::unifiprotect::talk_media::check_session_guard;
use transport_rs::driver::{
    CallAudio, CallDialer, CallHangupper, ConnectAnswerer, Driver, ExternalRinger, MediaTarget,
    ReasonedHangupper,
};
use transport_rs::pacer::{Config as PacerConfig, Kind, Pacer};

fn load(name: &str) -> Value {
    let mut p = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    p.push("tests/golden");
    p.push(name);
    let text = std::fs::read_to_string(&p).unwrap_or_else(|e| panic!("{}: {e}", p.display()));
    serde_json::from_str(&text).unwrap()
}

fn s<'a>(v: &'a Value, k: &str) -> &'a str {
    v.get(k).and_then(Value::as_str).unwrap_or("")
}

fn unhx(t: &str) -> Vec<u8> {
    let mut out = Vec::new();
    for it in t.split_whitespace() {
        if let Some((h, n)) = it.split_once('*') {
            let b = u8::from_str_radix(h, 16).unwrap();
            out.extend(std::iter::repeat_n(b, n.parse().unwrap()));
        } else {
            out.extend(
                (0..it.len() / 2).map(|i| u8::from_str_radix(&it[2 * i..2 * i + 2], 16).unwrap()),
            );
        }
    }
    out
}

fn hex(b: &[u8]) -> String {
    b.iter().map(|x| format!("{x:02x}")).collect()
}

fn unhex_plain(t: &str) -> Vec<u8> {
    (0..t.len() / 2)
        .map(|i| u8::from_str_radix(&t[2 * i..2 * i + 2], 16).unwrap())
        .collect()
}

// ---- the pacer -------------------------------------------------------------------------------

fn pacer_stats(p: &Pacer) -> Vec<Value> {
    let s = p.stats();
    vec![
        json!(s.frames_in),
        json!(s.frames_out),
        json!(s.silence_out),
        json!(s.drops_oldest),
        json!(s.drops_drift),
        json!(s.underruns),
        json!(s.max_gap.as_nanos() as i64),
        json!(s.gaps_100),
        json!(s.jitter_ms),
        json!(s.depth),
        json!(s.target.as_nanos() as i64),
    ]
}

/// The counters, with the jitter (index 8) compared as the float it is: Go writes `0` for 0.0.
fn same_stats(got: &[Value], want: &Value, what: &str) {
    let want = want.as_array().unwrap();
    assert_eq!(got.len(), want.len(), "{what}");
    for (i, (g, w)) in got.iter().zip(want).enumerate() {
        if i == 8 {
            assert_eq!(g.as_f64(), w.as_f64(), "{what}: jitter");
        } else {
            assert_eq!(g, w, "{what}: counter {i}");
        }
    }
}

#[test]
fn golden_pacer_scripts() {
    let g = load("talk_pacer.json");
    let scripts = g["scripts"].as_array().unwrap();
    assert_eq!(scripts.len(), 57);
    let mut ticks = 0;
    for sc in scripts {
        let name = s(sc, "name");
        let c = &sc["cfg"];
        let ns = |k: &str| Duration::from_nanos(c[k].as_u64().unwrap_or(0));
        let p = Pacer::new(PacerConfig {
            frame_dur: ns("frame_ns"),
            initial: ns("initial_ns"),
            min: ns("min_ns"),
            max: ns("max_ns"),
            slack: c["slack"].as_i64().unwrap_or(0),
            drift_ticks: c["drift_ticks"].as_i64().unwrap_or(0),
            silence: c["silence_hex"].as_str().map(unhex_plain),
        });
        let mut out = sc["out"].as_array().unwrap().iter();
        let mut stats = sc["stats"].as_array().unwrap().iter();
        for op in sc["ops"].as_array().unwrap() {
            let t = Duration::from_nanos(op["t"].as_u64().unwrap());
            match s(op, "op") {
                "push" => p.push(t, vec![op["id"].as_u64().unwrap_or(0) as u8]),
                "tick" => {
                    let (f, k) = p.tick(t);
                    let want = out.next().unwrap();
                    let kind = match k {
                        Kind::None => 0,
                        Kind::Audio => 1,
                        Kind::Silence => 2,
                    };
                    let (first, len) = f.as_ref().map_or((-1, 0), |f| {
                        (f.first().map_or(-1, |b| *b as i64), f.len() as i64)
                    });
                    assert_eq!(json!([kind, first, len]), *want, "{name}: tick {ticks}");
                    same_stats(
                        &pacer_stats(&p),
                        stats.next().unwrap(),
                        &format!("{name}: stats after tick {ticks}"),
                    );
                    ticks += 1;
                }
                o => panic!("op {o}"),
            }
        }
        same_stats(
            &pacer_stats(&p),
            &sc["final"],
            &format!("{name}: final stats"),
        );
        assert_eq!(
            p.stats().to_string(),
            s(sc, "line"),
            "{name}: the stats line"
        );
    }
    assert!(ticks > 5000, "{ticks}");
}

// ---- the helpers -----------------------------------------------------------------------------

/// Configure outcomes where the port is deliberately stricter than Go (PARITY.md 127, 119): the
/// value is a header, an argument or a request line and carries a control character or a
/// character that can end it. Go accepts all of these.
const CONFIGURE_DIFFERENCES: &[&str] = &[
    "protect_host h\tx",
    "protect_host h?x",
    "protect_host h#x",
    "protect_host h\\x",
    "key k\nX: 1",
    "key k\u{1}",
    "downlink_url rtsp://h/a b",
    "guard_url http://h/a b",
];

#[test]
fn golden_helpers() {
    let g = load("talk_helpers.json");

    for v in g["ffmpeg_args"].as_array().unwrap() {
        let r = ffmpeg_args(s(v, "sink"), s(v, "codec"), v["rate"].as_i64().unwrap());
        match r {
            Ok(a) => {
                assert_eq!(s(v, "err"), "", "{v}");
                let want: Vec<&str> = v["args"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .map(|x| x.as_str().unwrap())
                    .collect();
                assert_eq!(a, want, "{v}");
            }
            Err(e) => assert_eq!(e, s(v, "err"), "{v}"),
        }
    }

    // validSink: Go checks the scheme and the host; the port also refuses a space or a control
    // character in the URL (PARITY.md 127)
    for v in g["valid_sink"].as_array().unwrap() {
        let input = s(v, "in");
        let got = valid_sink(input).err().unwrap_or_default();
        if s(v, "err").is_empty() && got == "talkback sink host is not allowed" {
            // PARITY.md 474 (DEV-796 D2): Go accepts a loopback, unspecified or multicast host
            assert!(
                matches!(input, "rtp://[::1]:7004" | "udp://0.0.0.0:0"),
                "{input:?}: refused for no listed reason"
            );
            continue;
        }
        if s(v, "err").is_empty() && !got.is_empty() {
            assert!(
                input.bytes().any(|b| b <= 0x20 || b == 0x7f)
                    && got == "talkback sink is not a usable URL",
                "{input:?}: Go accepted, Rust: {got}"
            );
            continue;
        }
        assert_eq!(got, s(v, "err"), "{input:?}");
    }

    for v in g["downlink_args"].as_array().unwrap() {
        let want: Vec<&str> = v["args"]
            .as_array()
            .unwrap()
            .iter()
            .map(|x| x.as_str().unwrap())
            .collect();
        assert_eq!(downlink_args(s(v, "in")), want, "{v}");
    }

    let mut gains = 0;
    for v in g["gain"].as_array().unwrap() {
        let input = unhex_plain(s(v, "in_hex"));
        let gain = v["gain"].as_i64().unwrap();
        let out = gain_pcm(&input, gain);
        assert_eq!(hex(&out), s(v, "out_hex"), "{v}");
        assert_eq!(
            matches!(out, std::borrow::Cow::Borrowed(_)) && !input.is_empty(),
            v["same"].as_bool().unwrap(),
            "{v}: copy or not"
        );
        gains += 1;
    }
    assert!(gains > 50);

    for v in g["scrub_line"].as_array().unwrap() {
        assert_eq!(scrub_line(s(v, "in")), s(v, "out"), "{:?}", s(v, "in"));
    }

    // exec.LookPath's texts, with a temporary directory standing in for DIR
    let tmp = TmpDir::new("lp");
    tmp.write("plain", b"x");
    write_script(&tmp.file("prog"), "#!/bin/sh\n");
    std::fs::create_dir(tmp.file("adir")).unwrap();
    let d = tmp.path().display().to_string();
    let old = std::env::var_os("PATH");
    for v in g["look_path"].as_array().unwrap() {
        let name = s(v, "name").replace("DIR", &d);
        // PATH is process-wide: this is the only test of the binary that touches it
        std::env::set_var("PATH", s(v, "path").replace("DIR", &d));
        let r = look_path(&name);
        match (r, v["ok"].as_bool().unwrap_or(false)) {
            (Ok(_), true) => {}
            (Err(e), false) => assert_eq!(e.replace(&d, "DIR"), s(v, "err"), "{v}"),
            (r, ok) => panic!("{v}: Rust {r:?}, Go ok={ok}"),
        }
    }
    if let Some(p) = old {
        std::env::set_var("PATH", p);
    }

    // Configure over the corpus
    let mut n = 0;
    let mut differing = 0;
    for v in g["configure"].as_array().unwrap() {
        let name = s(v, "name");
        let cfg: Map<String, Value> = serde_json::from_str(s(v, "cfg")).unwrap();
        let mut d = TalkDriver::default();
        let r = d.configure(&cfg);
        if CONFIGURE_DIFFERENCES.contains(&name) {
            assert_eq!(s(v, "err"), "", "{name}: the table says Go accepts it");
            assert!(r.is_err(), "{name}: the table says Rust refuses it");
            differing += 1;
            continue;
        }
        match r {
            Ok(()) => {
                assert_eq!(s(v, "err"), "", "{name}: Go refused, Rust accepted");
                assert_eq!(d.capabilities().to_wire(), s(v, "caps"), "{name}");
            }
            Err(e) => assert_eq!(e.message(), s(v, "err"), "{name}"),
        }
        n += 1;
    }
    assert!(n >= 80, "{n}");
    assert_eq!(differing, CONFIGURE_DIFFERENCES.len());
    assert_eq!(
        TalkDriver::default().capabilities().to_wire(),
        s(&g, "unconfigured_caps")
    );
}

// ---- the guard -------------------------------------------------------------------------------

/// Where the port's guard differs from Go's `net/http` client (PARITY.md 140): a URL with a
/// user:password (Go sends Authorization: Basic; the port never sends credentials and refuses),
/// which is the safe side. (A head with bare LF line ends used to differ too; the observer's
/// client, which the guard reuses, accepts it since R1-90.)
const GUARD_DIFFERENCES: &[&str] = &["url http://u:p@HOST/health"];

#[tokio::test]
async fn golden_guard_answers() {
    let g = load("talk_guard.json");
    let cases = g["cases"].as_array().unwrap();
    assert_eq!(cases.len(), 74);
    let mut differing = 0;
    let mut bad: Vec<String> = Vec::new();
    for c in cases {
        let name = s(c, "name");
        let l = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let addr = l.local_addr().unwrap().to_string();
        let (reply, no_send, slow) = (
            unhex_plain(s(c, "reply_hex")),
            c["no_send"].as_bool().unwrap_or(false),
            c["slow_ms"].as_u64().unwrap_or(0),
        );
        let req = Arc::new(std::sync::Mutex::new(String::new()));
        let req2 = req.clone();
        let server = tokio::spawn(async move {
            loop {
                let Ok((mut sk, _)) = l.accept().await else {
                    return;
                };
                let (reply, req) = (reply.clone(), req2.clone());
                tokio::spawn(async move {
                    if no_send {
                        return;
                    }
                    let mut head = Vec::new();
                    let mut b = [0u8; 1];
                    while !head.ends_with(b"\r\n\r\n") {
                        match tokio::time::timeout(Duration::from_secs(2), sk.read(&mut b)).await {
                            Ok(Ok(1)) => head.push(b[0]),
                            _ => break,
                        }
                    }
                    *req.lock().unwrap() = String::from_utf8_lossy(&head).to_string();
                    if slow > 0 {
                        tokio::time::sleep(Duration::from_millis(slow)).await;
                    }
                    let _ = sk.write_all(&reply).await;
                    let _ = sk.shutdown().await;
                });
            }
        });
        let url = if !s(c, "url").is_empty() {
            s(c, "url").replace("HOST", &addr)
        } else if !s(c, "path").is_empty() {
            format!("http://{addr}{}", s(c, "path"))
        } else {
            format!("http://{addr}/health")
        };
        let timeout = if slow > 0 {
            Duration::from_millis(100)
        } else {
            Duration::from_millis(400)
        };
        let got = match check_session_guard(&CancelToken::new(), &url, timeout).await {
            Ok(()) => "ok".to_string(),
            Err(e) if e.is(transport_rs::driver::Sentinel::Busy) => "busy".to_string(),
            Err(e) => e.message().replace(&addr, "HOST"),
        };
        tokio::time::sleep(Duration::from_millis(60)).await; // the server task records the head
        server.abort();
        if GUARD_DIFFERENCES.contains(&name) {
            assert_ne!(got, s(c, "result"), "{name}: the table says it differs");
            assert!(got.contains("unreachable"), "{name}: {got}");
            differing += 1;
            continue;
        }
        if got != s(c, "result") {
            bad.push(format!("{name}: Rust {got:?}, Go {:?}", s(c, "result")));
            continue;
        }
        // the request: line and Host header (the other headers differ by client)
        let r = req.lock().unwrap().clone();
        let mut lines = r.trim_end().split("\r\n");
        let first = lines.next().unwrap_or("").to_string();
        let host = lines
            .find_map(|l| {
                l.strip_prefix("Host: ")
                    .or_else(|| l.strip_prefix("host: "))
            })
            .map(|h| h.replace(&addr, "HOST"))
            .unwrap_or_default();
        let want = s(c, "req");
        if !want.is_empty() {
            let mut w = want.split('|');
            assert_eq!(w.next().unwrap(), first, "{name}: request line");
            assert_eq!(
                w.next().unwrap_or(""),
                format!("host:{host}"),
                "{name}: host"
            );
            assert!(
                w.next().is_none(),
                "{name}: Go sent extra recorded headers: {want}"
            );
        } else {
            // (an `https` guard sends a TLS hello, as Go's does: not an HTTP request line)
            assert!(
                !first.starts_with("GET "),
                "{name}: Rust sent a request Go did not: {first}"
            );
        }
    }
    assert!(
        bad.is_empty(),
        "{} guard answers differ:\n{}",
        bad.len(),
        bad.join("\n")
    );
    assert_eq!(differing, GUARD_DIFFERENCES.len());
}

// ---- the scenarios ---------------------------------------------------------------------------

fn mask(text: &str, con: &str, guard: &str) -> String {
    let t = text.replace(con, "HOST").replace(guard, "GUARD");
    // 127.0.0.1:<digits> -> ADDR
    let b = t.as_bytes();
    let mut out = String::new();
    let mut i = 0;
    while i < b.len() {
        if t[i..].starts_with("127.0.0.1:") {
            let mut j = i + 10;
            while j < b.len() && b[j].is_ascii_digit() {
                j += 1;
            }
            if j > i + 10 {
                out.push_str("ADDR");
                i = j;
                continue;
            }
        }
        let ch = t[i..].chars().next().unwrap();
        out.push(ch);
        i += ch.len_utf8();
    }
    out
}

fn kill9(pid: u32) -> bool {
    pid != 0 && unsafe { libc::kill(pid as i32, libc::SIGKILL) } == 0
}

async fn run_scenario(sc: &Value) -> Vec<Value> {
    let con = common::unifi::FakeProtect::new().await;
    let guard = FakeGuard::new(r#"{"sessionUp":false}"#).await;
    let dir = TmpDir::new("gs");
    let (conh, guardh) = (con.host(), guard.addr.to_string());
    let t = &sc["timing"];
    let ms =
        |k: &str, def: u64| Duration::from_millis(t[k].as_u64().filter(|n| *n > 0).unwrap_or(def));
    let timing = TalkTiming {
        min_hold: ms("min_hold_ms", 1000),
        orphan: ms("orphan_ms", 10000),
        dial_orphan: ms("dial_orphan_ms", 20000),
        dial_max: ms("dial_max_ms", 300000),
        guard_timeout: ms("guard_timeout_ms", 3000),
        ..TalkTiming::default()
    };
    let mut d = TalkDriver::default();
    d.set_scheme("http");
    d.set_timing(timing);
    let ff = fake_ffmpeg_both(&dir);
    d.set_ffmpeg_binary(if sc["ffmpeg_missing"].as_bool().unwrap_or(false) {
        "/nonexistent/ffmpeg"
    } else {
        &ff
    });
    let mut cfg = Map::new();
    for (k, v) in sc["cfg"].as_object().cloned().unwrap_or_default() {
        let v = match v {
            Value::String(x) => {
                Value::String(x.replace("$CON", &conh).replace("$GUARD", &guard.url()))
            }
            o => o,
        };
        cfg.insert(k, v);
    }
    d.configure(&cfg).unwrap();
    let sink = TalkSink::new();
    let d = Arc::new(d);
    d.start(&CancelToken::new(), sink.clone()).await.unwrap();

    let mut audio: std::collections::HashMap<String, Arc<dyn CallAudio>> = Default::default();
    let mut res = Vec::new();
    let err = |e: Option<transport_rs::driver::DriverError>| json!({"err": e.map(|e| mask(e.message(), &conh, &guardh)).unwrap_or_default()});
    use transport_rs::driver::AudioClaimer;
    for op in sc["ops"].as_array().unwrap() {
        let id = s(op, "id");
        let name = s(op, "name");
        let n = op["n"].as_u64().unwrap_or(0);
        let r = match s(op, "op") {
            "ring" => err(d
                .external_ring(id, Duration::from_millis(op["ms"].as_u64().unwrap_or(0)))
                .err()),
            "answer" => match d.answer_with_audio(&CancelToken::new(), id).await {
                Ok(a) => {
                    audio.insert(name.to_string(), a);
                    err(None)
                }
                Err(e) => err(Some(e)),
            },
            "dial" => err(d
                .dial_call(&CancelToken::new(), id, MediaTarget::default())
                .await
                .err()),
            "claim" => match d.claimed_audio(id) {
                Some(a) => {
                    audio.insert(name.to_string(), a);
                    json!({"found": true})
                }
                None => json!({"found": false}),
            },
            "ringing" => json!({"ringing": ConnectAnswerer::ringing_call(&*d, id)}),
            "attach" => {
                audio[name].attach();
                Value::Null
            }
            "detach" => {
                audio[name].detach();
                Value::Null
            }
            "in" => {
                audio[name]
                    .input()
                    .send(unhex_plain(s(op, "hex")))
                    .await
                    .unwrap();
                Value::Null
            }
            "out" => {
                let mut frames = Vec::new();
                let mut closed = false;
                let rx = audio[name].output();
                for _ in 0..n {
                    let f = tokio::time::timeout(
                        Duration::from_millis(op["ms"].as_u64().unwrap_or(0)),
                        async { rx.lock().await.recv().await },
                    )
                    .await;
                    match f {
                        Ok(Some(fr)) => {
                            frames.push(format!("{}:{}", fr.len(), hex(&fr[..fr.len().min(6)])))
                        }
                        Ok(None) => {
                            closed = true;
                            break;
                        }
                        Err(_) => break,
                    }
                }
                json!({"frames": frames, "closed": closed})
            }
            "hangup" => {
                let why = s(op, "why");
                let r = if why.is_empty() {
                    d.hangup_call(id)
                } else {
                    ReasonedHangupper::hangup_call_why(&*d, id, why)
                };
                err(r.err())
            }
            "stop" => err(d.stop().await.err()),
            "sleep" => {
                tokio::time::sleep(Duration::from_millis(op["ms"].as_u64().unwrap_or(0))).await;
                Value::Null
            }
            "guard" => {
                guard.set_body(s(op, "body"));
                guard.set_code(op["code"].as_u64().filter(|c| *c > 0).unwrap_or(200) as u16);
                Value::Null
            }
            "talkback" => {
                con.set_talkback(s(op, "body"));
                Value::Null
            }
            "console_status" => {
                con.set_status(op["code"].as_u64().unwrap_or(0) as u16);
                Value::Null
            }
            "file" => {
                dir.write(name, &unhx(s(op, "hex")));
                Value::Null
            }
            "release" => {
                dir.write("go", b"");
                Value::Null
            }
            "kill" => {
                let st = d.stats_for_test();
                let pid = st.map_or(0, |st| {
                    if s(op, "which") == "up" {
                        st.up_pid.load(std::sync::atomic::Ordering::SeqCst)
                    } else {
                        st.down_pid.load(std::sync::atomic::Ordering::SeqCst)
                    }
                });
                json!({"killed": kill9(pid)})
            }
            "wait_ended" => {
                let (sk, pat) = (sink.clone(), format!("{id}:ended"));
                let mut ok = false;
                for _ in 0..500 {
                    if sk.got().contains(&pat) {
                        ok = true;
                        break;
                    }
                    tokio::time::sleep(Duration::from_millis(10)).await;
                }
                json!({"ended": ok})
            }
            w @ ("wait_frames" | "wait_bytes") => {
                let mut ok = false;
                for _ in 0..500 {
                    let h = d.health();
                    let v = if w == "wait_frames" {
                        h.frames_from_camera
                    } else {
                        h.bytes_to_camera
                    };
                    if v == n {
                        ok = true;
                        break;
                    }
                    tokio::time::sleep(Duration::from_millis(10)).await;
                }
                json!({"reached": ok})
            }
            "snap" => {
                let mut m = Map::new();
                m.insert("events".into(), json!(sink.got()));
                m.insert("heard".into(), json!(mask(&sink.heard(), &conh, &guardh)));
                m.insert("rings".into(), json!(sink.rings()));
                if op["health"].as_bool().unwrap_or(false) {
                    let h = d.health();
                    m.insert(
                        "health".into(),
                        json!({"session_up": h.session_up, "bytes_to_camera": h.bytes_to_camera,
                            "frames_from_camera": h.frames_from_camera, "drops_up": h.drops_up,
                            "drops_down": h.drops_down, "last_ffmpeg": h.last_ffmpeg,
                            "last_end": mask(&h.last_end, &conh, &guardh)}),
                    );
                    let reqs = con.reqs();
                    m.insert("posts".into(), json!(reqs.len()));
                    m.insert("requests".into(), json!(reqs));
                    m.insert(
                        "keys_ok".into(),
                        json!(con.keys().iter().all(|k| k == TEST_KEY)),
                    );
                    m.insert(
                        "bodies".into(),
                        json!(con
                            .bodies()
                            .iter()
                            .map(|b| b.chars().take(64).collect::<String>())
                            .collect::<Vec<_>>()),
                    );
                    m.insert("guard_hits".into(), json!(guard.hits()));
                }
                Value::Object(m)
            }
            o => panic!("unknown op {o}"),
        };
        res.push(r);
    }
    let _ = d.stop().await;
    res
}

/// Scenarios (by name) and steps (by index) where the port differs, with the PARITY.md number.
/// The one: Go refuses an unset `guard_url` ("no guard_url"), the port treats it as "no external
/// guard" and goes on (PARITY.md 476). Steps 1 (the answer) and 2 (the snapshot) differ.
const SCEN_DIFFERENCES: &[(&str, usize)] = &[("no guard_url", 1), ("no guard_url", 2)];

#[tokio::test]
async fn golden_scenarios_first_half() {
    replay(0, 16).await;
}

#[tokio::test]
async fn golden_scenarios_second_half() {
    replay(16, 100).await;
}

async fn replay(from: usize, to: usize) {
    let g = load("talk_scen.json");
    let scs = g["scenarios"].as_array().unwrap();
    assert_eq!(scs.len(), 31);
    for (i, sc) in scs.iter().enumerate() {
        if i < from || i >= to {
            continue;
        }
        let name = s(sc, "name");
        let got = run_scenario(sc).await;
        let want = sc["results"].as_array().unwrap();
        assert_eq!(got.len(), want.len(), "{name}");
        for (k, (g, w)) in got.iter().zip(want).enumerate() {
            if SCEN_DIFFERENCES.contains(&(name, k)) {
                assert_ne!(g, w, "{name}: step {k} (the table says it differs)");
                continue;
            }
            assert_eq!(g, w, "{name}: step {k} {}", sc["ops"][k]);
        }
    }
}
