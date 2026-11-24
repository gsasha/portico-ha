//! answerer-str0m: the str0m (sans-IO) answerer of the WebRTC library spike (DEV-764 S1).
//! One TCP connection = one session = one thread with its own UDP socket and its own Rtc.
#[path = "../../common/common.rs"]
mod common;

use common::*;
use serde_json::{json, Value};
use std::io::{BufRead, BufReader, Write};
use std::net::{TcpListener, TcpStream, UdpSocket};
use std::sync::mpsc;
use std::time::{Duration, Instant};
use str0m::change::SdpOffer;
use str0m::format::Codec;
use str0m::media::{Frequency, MediaTime, Mid, Pt};
use str0m::net::{Protocol, Receive};
use str0m::{Candidate, Event, IceConnectionState, Input, Output, Rtc, RtcConfig};

fn main() {
    str0m::crypto::from_feature_flags().install_process_default();
    let args = Args::parse();
    let l = TcpListener::bind(&args.listen).expect("listen");
    logj(json!({"ev": "LISTEN", "addr": l.local_addr().unwrap().to_string(), "lib": "str0m 0.25.0"}));
    let mut hs = vec![];
    for i in 0..args.sessions {
        let (s, _) = l.accept().expect("accept");
        let a = args.clone();
        hs.push(std::thread::spawn(move || session(i, s, a)));
    }
    let mut code = 0;
    for h in hs {
        if h.join().is_err() {
            code = 1;
        }
    }
    std::process::exit(code);
}

fn session(id: usize, stream: TcpStream, a: Args) {
    let sock = UdpSocket::bind("127.0.0.1:0").unwrap();
    let local = sock.local_addr().unwrap();
    let mut rtc: Rtc = RtcConfig::new().clear_codecs().enable_pcmu(true, false).build(Instant::now());
    let host = Candidate::host(local, "udp").unwrap();
    let host_sdp = host.to_sdp_string();
    rtc.add_local_candidate(host);

    let (tx, rx) = mpsc::channel::<Value>();
    let rd = stream.try_clone().unwrap();
    std::thread::spawn(move || {
        for line in BufReader::new(rd).lines() {
            match line {
                Ok(l) => {
                    if let Ok(v) = serde_json::from_str::<Value>(&l) {
                        let _ = tx.send(v);
                    }
                }
                Err(_) => break,
            }
        }
        let _ = tx.send(Value::Null);
    });
    let mut wr = stream;
    let mut send = move |v: Value| {
        let _ = writeln!(wr, "{v}");
    };

    let label = format!("str0m-{id}");
    let mut meter = Meter::new(a.rx_hz);
    let mut tone = Tone::new(a.hz);
    let mut states: Vec<String> = vec![];
    let mut mid: Option<Mid> = None;
    let mut pt: Option<Pt> = None;
    let mut connected = false;
    let mut next_tone = Instant::now();
    let mut rtp_ts: u64 = 0;
    let mut tx_count: u64 = 0;
    let mut tx_err: u64 = 0;
    let start = Instant::now();
    let mut next_snap = start + Duration::from_secs_f64(a.snap);
    let mut done = false;
    let mut buf = vec![0u8; 2000];

    macro_rules! state {
        ($k:expr, $s:expr) => {{
            let s = format!("{} {}={}", now_hms(), $k, $s);
            logj(json!({"id": label, "ev": $k, "state": format!("{}", $s)}));
            states.push(s);
        }};
    }

    while !done {
        while let Ok(m) = rx.try_recv() {
            match m["t"].as_str() {
                Some("offer") => {
                    if std::env::var("DBG").is_ok() { logj(json!({"ev":"offer-sdp","sdp":m["sdp"]})); }
                    let offer = match SdpOffer::from_sdp_string(m["sdp"].as_str().unwrap_or("")) {
                        Ok(o) => o,
                        Err(e) => {
                            logj(json!({"id": label, "error": format!("parse offer: {e}")}));
                            continue;
                        }
                    };
                    match rtc.sdp_api().accept_offer(offer) {
                        Ok(ans) => {
                            let sdp = ans.to_sdp_string(); if std::env::var("DBG").is_ok() { logj(json!({"ev":"answer-sdp","sdp":sdp})); }
                            if a.trickle {
                                let (stripped, _in_sdp) = split_candidates(&sdp);
                                send(json!({"t": "answer", "sdp": stripped}));
                                send(json!({"t": "cand", "c": host_sdp}));
                            } else {
                                send(json!({"t": "answer", "sdp": sdp}));
                            }
                        }
                        Err(e) => logj(json!({"id": label, "error": format!("accept_offer: {e}")})),
                    }
                }
                Some("cand") => {
                    logj(json!({"id": label, "ev": "remote-cand", "c": m["c"]}));
                    match Candidate::from_sdp_string(m["c"].as_str().unwrap_or("")) {
                    Ok(c) => rtc.add_remote_candidate(c),
                    Err(e) => logj(json!({"id": label, "error": format!("bad candidate: {e}")})),
                }},
                Some("bye") | None => done = true,
                _ => {}
            }
        }
        if done {
            break;
        }

        let timeout = loop {
            match rtc.poll_output() {
                Ok(Output::Timeout(t)) => break t,
                Ok(Output::Transmit(t)) => {
                    let _ = sock.send_to(&t.contents, t.destination); if std::env::var("DBG").is_ok() { logj(json!({"ev":"udp-out","to":t.destination.to_string(),"n":t.contents.len()})); }
                }
                Ok(Output::Event(e)) => match e {
                    Event::Connected => {
                        connected = true;
                        state!("pc", "Connected");
                    }
                    Event::IceConnectionStateChange(s) => {
                        state!("ice", format!("{:?}", s));
                        if s == IceConnectionState::Disconnected {
                            state!("pc", "Disconnected(ice)");
                        }
                    }
                    Event::MediaAdded(m) => {
                        mid = Some(m.mid);
                        logj(json!({"id": label, "ev": "media", "mid": m.mid.to_string()}));
                    }
                    Event::MediaData(d) => {
                        let seq = d.seq_range.end().as_u16();
                        meter.packet(seq, d.time.numer() as u32, &d.data, d.network_time);
                    }
                    _ => {}
                },
                Err(e) => {
                    logj(json!({"id": label, "error": format!("poll_output: {e}")}));
                    state!("pc", "Error");
                    done = true;
                    break Instant::now();
                }
            }
        };
        if done {
            break;
        }

        let now = Instant::now();
        if connected {
            if let (Some(m), None) = (mid, pt) {
                if let Some(w) = rtc.writer(m) {
                    pt = w.payload_params().find(|p| p.spec().codec == Codec::PCMU).map(|p| p.pt());
                }
            }
            if let (Some(m), Some(p)) = (mid, pt) {
                if now > next_tone + Duration::from_millis(200) {
                    next_tone = now; // fell behind (suspended?): do not burst
                }
                while Instant::now() >= next_tone {
                    let frame = tone.frame();
                    let t = MediaTime::new(rtp_ts, Frequency::EIGHT_KHZ);
                    match rtc.writer(m).map(|w| w.write(p, Instant::now(), t, frame)) {
                        Some(Ok(())) => tx_count += 1,
                        _ => tx_err += 1,
                    }
                    rtp_ts += 160;
                    next_tone += Duration::from_millis(20);
                }
            }
        }
        if now >= next_snap {
            next_snap += Duration::from_secs_f64(a.snap);
            let mut r = meter.report();
            r["id"] = json!(label);
            r["ev"] = json!("SNAP");
            r["tx"] = json!(tx_count);
            r["elapsed_s"] = json!(start.elapsed().as_secs());
            logj(r);
        }

        let mut wake = timeout.min(Instant::now() + Duration::from_millis(10));
        if connected && pt.is_some() {
            wake = wake.min(next_tone);
        }
        let dur = wake.saturating_duration_since(Instant::now());
        let input = if dur.is_zero() {
            Input::Timeout(Instant::now())
        } else {
            sock.set_read_timeout(Some(dur)).unwrap();
            buf.resize(2000, 0);
            match sock.recv_from(&mut buf) {
                Ok((n, source)) => {
                    if std::env::var("DBG").is_ok() { logj(json!({"ev":"udp-in","from":source.to_string(),"n":n})); }
                    buf.truncate(n);
                    Input::Receive(
                        Instant::now(),
                        Receive {
                            proto: Protocol::Udp,
                            source,
                            destination: local,
                            contents: buf.as_slice().try_into().unwrap(),
                        },
                    )
                }
                Err(e) => { if std::env::var("DBG").is_ok() && e.kind() != std::io::ErrorKind::WouldBlock { logj(json!({"ev":"recv-err","e":e.to_string()})); } Input::Timeout(Instant::now()) }
            }
        };
        if let Err(e) = rtc.handle_input(input) {
            logj(json!({"id": label, "error": format!("handle_input: {e}")}));
        }
    }
    rtc.disconnect();
    let mut r = meter.report();
    r["id"] = json!(label);
    r["ev"] = json!("REPORT");
    r["role"] = json!("answerer-str0m");
    r["tx"] = json!(tx_count);
    r["tx_errors"] = json!(tx_err);
    r["states"] = json!(states);
    logj(r);
}
