//! prod: the production-shaped str0m answerer of spike S1b (DEV-764).
//!
//! Still a spike, but shaped like Portico's Go SFU (`transport/internal/sfu/sfu.go`):
//!  * PCMU only (`clear_codecs().enable_pcmu`), so an Opus-first offer is answered with PCMU alone;
//!  * `PCMU/8000/1` is rewritten to `PCMU/8000` in the offer text (every `a=rtpmap:` line ending in `/1`
//!    loses that suffix) because str0m 0.25 scores a channel count of `Some(1)` differently from `None`;
//!  * the offer is parsed and accepted inside `catch_unwind`; a panic or an error becomes `{"t":"error"}`
//!    and ends that session only;
//!  * the answer is NON-trickle like the Go side: every host candidate of every local interface address is
//!    added to the `Rtc` before `accept_offer`, so the answer SDP carries them all, followed by
//!    `a=end-of-candidates`; remote candidates (trickle, before or after the answer, the empty
//!    end-of-candidates marker, mDNS `.local` names) are accepted at any time;
//!  * one UDP socket per local interface address (own enumeration with `if-addrs`), one session per TCP
//!    connection of the line-JSON signalling protocol of S1, any number of sessions per process.
#[path = "../../common/common.rs"]
mod common;

use common::*;
#[path = "../../common/prod_common.rs"]
mod prod_common;
use prod_common::*;
use serde_json::{json, Value};
use std::collections::HashMap;
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{IpAddr, SocketAddr, TcpListener, TcpStream, UdpSocket};
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc, Arc};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use str0m::change::SdpOffer;
use str0m::format::Codec;
use str0m::media::{Frequency, MediaTime, Mid, Pt};
use str0m::net::{Protocol, Receive};
use str0m::{Candidate, Event, IceConnectionState, Input, Output, Rtc, RtcConfig};

const MAX_LINE: u64 = 8 * 1024 * 1024; // a signalling line longer than this closes the session

#[derive(Clone)]
struct Cfg {
    listen: String,
    sessions: usize, // 0 = unlimited
    hz: f64,
    snap: f64,
    max_stun_retransmits: Option<usize>,
    answer_delay_ms: u64,
    only_loopback: bool,
    sdp_limits: bool,
}

fn parse_args() -> Cfg {
    let mut c = Cfg { listen: "127.0.0.1:0".into(), sessions: 0, hz: 440.0, snap: 10.0, max_stun_retransmits: None, answer_delay_ms: 0, only_loopback: false, sdp_limits: true };
    let v: Vec<String> = std::env::args().collect();
    let mut i = 1;
    while i < v.len() {
        let val = v.get(i + 1).cloned().unwrap_or_default();
        match v[i].as_str() {
            "--listen" => c.listen = val,
            "--sessions" => c.sessions = val.parse().unwrap(),
            "--hz" => c.hz = val.parse().unwrap(),
            "--snap" => c.snap = val.parse().unwrap(),
            "--max-stun-retransmits" => c.max_stun_retransmits = Some(val.parse().unwrap()),
            "--answer-delay-ms" => c.answer_delay_ms = val.parse().unwrap(),
            "--no-sdp-limits" => { c.sdp_limits = false; i -= 1; }
            "--loopback-only" => { c.only_loopback = true; i -= 1; }
            x => panic!("unknown flag {x}"),
        }
        i += 2;
    }
    c
}

enum Ev {
    Sig(Value),
    Udp { idx: usize, at: Instant, src: SocketAddr, data: Vec<u8> },
}

fn main() {
    str0m::crypto::from_feature_flags().install_process_default();
    let cfg = parse_args();
    let l = TcpListener::bind(&cfg.listen).expect("listen");
    let addrs = local_addrs(cfg.only_loopback);
    log(json!({"ev": "LISTEN", "addr": l.local_addr().unwrap().to_string(), "lib": "str0m 0.25.0 prod",
        "interfaces": addrs.iter().map(|(n, a)| format!("{n} {a}")).collect::<Vec<_>>(),
        "max_stun_retransmits": cfg.max_stun_retransmits}));
    let mut n = 0usize;
    for conn in l.incoming() {
        let Ok(s) = conn else { continue };
        let id = n;
        n += 1;
        let c = cfg.clone();
        // A panic in a session thread ends that thread only; the process keeps serving.
        std::thread::spawn(move || {
            let r = catch_unwind(AssertUnwindSafe(|| session(id, s, c)));
            if r.is_err() {
                log(json!({"id": format!("str0m-{id}"), "ev": "SESSION_PANIC"}));
            }
        });
        if cfg.sessions != 0 && n >= cfg.sessions {
            break;
        }
    }
    // with a fixed session count, wait for the sessions' threads to end by staying alive until killed
    loop {
        std::thread::sleep(Duration::from_secs(3600));
    }
}

#[derive(Default)]
struct CandStats {
    total: u64,
    ok: u64,
    mdns: u64,
    bad: u64,
}

fn add_cand(rtc: &mut Rtc, cs: &mut CandStats, c: &str, label: &str) {
    cs.total += 1;
    let t = c.trim();
    if t.is_empty() || t.contains("end-of-candidates") {
        log(json!({"id": label, "ev": "end-of-candidates", "c": t}));
        return;
    }
    let t = t.strip_prefix("a=").unwrap_or(t);
    match Candidate::from_sdp_string(t) {
        Ok(cand) => {
            cs.ok += 1;
            log(json!({"id": label, "ev": "remote-cand", "ok": true, "c": t}));
            rtc.add_remote_candidate(cand);
        }
        Err(e) => {
            if t.contains(".local") {
                cs.mdns += 1;
            } else {
                cs.bad += 1;
            }
            log(json!({"id": label, "ev": "remote-cand", "ok": false, "mdns": t.contains(".local"), "c": t, "e": e.to_string()}));
        }
    }
}

fn session(id: usize, stream: TcpStream, cfg: Cfg) {
    let label = format!("str0m-{id}");
    let (tx, rx) = mpsc::channel::<Ev>();
    let stop = Arc::new(AtomicBool::new(false));

    // sockets: one per local interface address
    let mut socks: Vec<UdpSocket> = vec![];
    let mut locals: Vec<SocketAddr> = vec![];
    let mut bound_names = vec![];
    for (name, ip) in local_addrs(cfg.only_loopback) {
        match UdpSocket::bind(SocketAddr::new(ip, 0)) {
            Ok(s) => {
                locals.push(s.local_addr().unwrap());
                bound_names.push(format!("{name} {}", s.local_addr().unwrap()));
                socks.push(s);
            }
            Err(e) => log(json!({"id": label, "ev": "bind-failed", "if": name, "ip": ip.to_string(), "e": e.to_string()})),
        }
    }
    for (idx, s) in socks.iter().enumerate() {
        let s = s.try_clone().unwrap();
        s.set_read_timeout(Some(Duration::from_millis(200))).unwrap();
        let tx = tx.clone();
        let stop = stop.clone();
        std::thread::spawn(move || {
            let mut buf = vec![0u8; 2048];
            while !stop.load(Ordering::Relaxed) {
                if let Ok((n, src)) = s.recv_from(&mut buf) {
                    if tx.send(Ev::Udp { idx, at: Instant::now(), src, data: buf[..n].to_vec() }).is_err() {
                        break;
                    }
                }
            }
        });
    }

    // signalling reader (line JSON, lines capped)
    {
        let rd = stream.try_clone().unwrap();
        let tx = tx.clone();
        std::thread::spawn(move || {
            let mut r = BufReader::new(rd);
            loop {
                let mut line = Vec::new();
                let n = (&mut r).take(MAX_LINE).read_until(b'\n', &mut line).unwrap_or(0);
                if n == 0 {
                    break;
                }
                if line.last() != Some(&b'\n') && n as u64 >= MAX_LINE {
                    let _ = tx.send(Ev::Sig(json!({"t": "__oversize"})));
                    break;
                }
                match serde_json::from_slice::<Value>(&line) {
                    Ok(v) if v.is_object() => {
                        let _ = tx.send(Ev::Sig(v));
                    }
                    _ => {
                        let _ = tx.send(Ev::Sig(json!({"t": "__badjson"})));
                    }
                }
            }
            let _ = tx.send(Ev::Sig(Value::Null));
        });
    }
    let ctl = stream.try_clone().unwrap();
    let mut wr = stream;
    let mut send = move |v: Value| {
        let _ = writeln!(wr, "{v}");
    };

    let mut rcfg = RtcConfig::new().clear_codecs().enable_pcmu(true, false);
    if let Some(n) = cfg.max_stun_retransmits {
        rcfg.set_max_stun_retransmits(n);
    }
    let mut rtc: Rtc = rcfg.build(Instant::now());
    let mut host_sdps = vec![];
    for a in &locals {
        match Candidate::host(*a, "udp") {
            Ok(c) => {
                host_sdps.push(c.to_sdp_string());
                rtc.add_local_candidate(c);
            }
            Err(e) => log(json!({"id": label, "ev": "host-cand-failed", "a": a.to_string(), "e": e.to_string()})),
        }
    }
    log(json!({"id": label, "ev": "session", "sockets": bound_names}));

    let mut meter = Meter::new(cfg.hz);
    let mut spec = Spectrum::new();
    let mut tone = Tone::new(cfg.hz);
    let mut pt_counts: HashMap<u8, u64> = HashMap::new();
    let mut states: Vec<String> = vec![];
    let mut mid: Option<Mid> = None;
    let mut pt: Option<Pt> = None;
    let mut connected = false;
    let mut next_tone = Instant::now();
    let mut rtp_ts: u64 = 0;
    let (mut tx_count, mut tx_err) = (0u64, 0u64);
    let mut cs = CandStats::default();
    let mut pending_cands: Vec<String> = vec![];
    let mut have_offer = false;
    let mut dead = false; // session over after an error
    let start = Instant::now();
    let mut next_snap = start + Duration::from_secs_f64(cfg.snap);
    let mut answer_at: Option<(Instant, String)> = None;
    let mut done = false;
    let mut last_src: HashMap<SocketAddr, u64> = HashMap::new();

    macro_rules! state {
        ($k:expr, $s:expr) => {{
            let s = format!("{}", $s);
            log(json!({"id": label, "ev": "state", "kind": $k, "state": s, "since_start_ms": start.elapsed().as_millis() as u64}));
            states.push(format!("{} {}={}", now_hms(), $k, s));
        }};
    }

    while !done {
        // --- poll the Rtc
        let timeout = loop {
            match rtc.poll_output() {
                Ok(Output::Timeout(t)) => break t,
                Ok(Output::Transmit(t)) => {
                    let idx = locals.iter().position(|a| *a == t.source).unwrap_or(0);
                    if let Some(s) = socks.get(idx) {
                        let _ = s.send_to(&t.contents, t.destination);
                    }
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
                        log(json!({"id": label, "ev": "media", "mid": m.mid.to_string()}));
                    }
                    Event::MediaData(d) => {
                        *pt_counts.entry(*d.pt).or_insert(0) += 1;
                        let seq = d.seq_range.end().as_u16();
                        meter.packet(seq, d.time.numer() as u32, &d.data, d.network_time);
                        spec.feed(&d.data);
                    }
                    _ => {}
                },
                Err(e) => {
                    log(json!({"id": label, "error": format!("poll_output: {e}")}));
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
        // delayed answer (test knob: lets trickle candidates overtake the answer)
        if let Some((at, _)) = &answer_at {
            if now >= *at {
                let (_, sdp) = answer_at.take().unwrap();
                send(json!({"t": "answer", "sdp": sdp}));
                log(json!({"id": label, "ev": "answer-sent"}));
            }
        }
        if connected {
            if let (Some(m), None) = (mid, pt) {
                if let Some(w) = rtc.writer(m) {
                    pt = w.payload_params().find(|p| p.spec().codec == Codec::PCMU).map(|p| p.pt());
                }
            }
            if let (Some(m), Some(p)) = (mid, pt) {
                if now > next_tone + Duration::from_millis(200) {
                    next_tone = now;
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
            next_snap += Duration::from_secs_f64(cfg.snap);
            let mut r = meter.report();
            r["id"] = json!(label);
            r["ev"] = json!("SNAP");
            r["tx"] = json!(tx_count);
            r["pt_counts"] = json!(pt_counts.iter().map(|(k, v)| (k.to_string(), *v)).collect::<HashMap<_, _>>());
            r["spectrum"] = spec.report();
            r["elapsed_s"] = json!(start.elapsed().as_secs());
            log(r);
        }

        // --- wait for the next event
        let mut wake = timeout.min(Instant::now() + Duration::from_millis(10));
        if connected && pt.is_some() {
            wake = wake.min(next_tone);
        }
        if let Some((at, _)) = &answer_at {
            wake = wake.min(*at);
        }
        let dur = wake.saturating_duration_since(Instant::now());
        let ev = if dur.is_zero() { None } else { rx.recv_timeout(dur).ok() };
        let mut input: Option<Input> = None;
        let mut udp_data: Vec<u8> = vec![];
        match ev {
            Some(Ev::Udp { idx, at, src, data }) => {
                *last_src.entry(src).or_insert(0) += 1;
                udp_data = data;
                let _ = at;
                let dest = locals[idx];
                // `Receive` borrows udp_data; built below
                let rcv = Receive {
                    proto: Protocol::Udp,
                    source: src,
                    destination: dest,
                    contents: match udp_data.as_slice().try_into() {
                        Ok(c) => c,
                        Err(_) => {
                            continue;
                        }
                    },
                };
                if let Err(e) = rtc.handle_input(Input::Receive(Instant::now(), rcv)) {
                    log(json!({"id": label, "error": format!("handle_input: {e}")}));
                }
            }
            Some(Ev::Sig(m)) => {
                match m["t"].as_str() {
                    Some("offer") => {
                        let raw = m["sdp"].as_str().unwrap_or("").to_string();
                        let sdp = normalise_offer(&raw);
                        let rewritten = sdp != raw;
                        let t0 = Instant::now();
                        let res = catch_unwind(AssertUnwindSafe(|| -> Result<String, String> {
                            if cfg.sdp_limits && offer_too_large(&sdp) {
                                return Err(format!("offer too large ({} bytes)", sdp.len()));
                            }
                            if std::env::var("S1B_INJECT_PANIC").is_ok() && sdp.contains("x-s1b-panic-now") {
                                panic!("injected panic inside accept_offer's catch_unwind");
                            }
                            let offer = SdpOffer::from_sdp_string(&sdp).map_err(|e| format!("parse offer: {e}"))?;
                            let ans = rtc.sdp_api().accept_offer(offer).map_err(|e| format!("accept_offer: {e}"))?;
                            Ok(ans.to_sdp_string())
                        }));
                        let took = t0.elapsed().as_millis() as u64;
                        match res {
                            Ok(Ok(mut ans)) => {
                                have_offer = true;
                                if !ans.contains("a=end-of-candidates") && ans.contains("a=candidate:") {
                                    // non-trickle answer: all host candidates are in; say so (pion does the same)
                                    if !ans.ends_with("\r\n") {
                                        ans.push_str("\r\n");
                                    }
                                    ans.push_str("a=end-of-candidates\r\n");
                                }
                                let ncand = ans.matches("a=candidate:").count();
                                log(json!({"id": label, "ev": "offer-accepted", "rewritten_rtpmap": rewritten,
                                    "accept_ms": took, "answer_candidates": ncand, "answer_sdp": ans}));
                                if cfg.answer_delay_ms == 0 {
                                    send(json!({"t": "answer", "sdp": ans}));
                                } else {
                                    answer_at = Some((Instant::now() + Duration::from_millis(cfg.answer_delay_ms), ans));
                                }
                                for c in std::mem::take(&mut pending_cands) {
                                    add_cand(&mut rtc, &mut cs, &c, &label);
                                }
                            }
                            Ok(Err(e)) => {
                                log(json!({"id": label, "ev": "offer-rejected", "e": e, "accept_ms": took}));
                                send(json!({"t": "error", "msg": e}));
                                dead = true;
                            }
                            Err(p) => {
                                let msg = p.downcast_ref::<String>().cloned().or_else(|| p.downcast_ref::<&str>().map(|s| s.to_string())).unwrap_or_default();
                                log(json!({"id": label, "ev": "offer-PANIC-CAUGHT", "msg": msg, "accept_ms": took}));
                                send(json!({"t": "error", "msg": format!("panic: {msg}")}));
                                dead = true; // the Rtc may be inconsistent: drop the session
                            }
                        }
                    }
                    Some("cand") => {
                        let c = m["c"].as_str().unwrap_or("").to_string();
                        if have_offer {
                            add_cand(&mut rtc, &mut cs, &c, &label);
                        } else {
                            pending_cands.push(c); // candidate ahead of the offer: held until the offer is accepted
                        }
                    }
                    Some("__oversize") => {
                        send(json!({"t": "error", "msg": "signalling line too long"}));
                        dead = true;
                    }
                    Some("__badjson") => {
                        send(json!({"t": "error", "msg": "bad json"}));
                        dead = true;
                    }
                    Some("bye") | None => done = true,
                    _ => {}
                }
                if dead {
                    done = true;
                }
            }
            None => {
                input = Some(Input::Timeout(Instant::now()));
            }
        }
        if let Some(i) = input {
            if let Err(e) = rtc.handle_input(i) {
                log(json!({"id": label, "error": format!("handle_input: {e}")}));
            }
        }
        let _ = &udp_data;
    }
    stop.store(true, Ordering::Relaxed);
    let _ = ctl.shutdown(std::net::Shutdown::Both);
    rtc.disconnect();
    let mut r = meter.report();
    r["id"] = json!(label);
    r["ev"] = json!("REPORT");
    r["role"] = json!("prod-str0m");
    r["tx"] = json!(tx_count);
    r["tx_errors"] = json!(tx_err);
    r["pt_counts"] = json!(pt_counts.iter().map(|(k, v)| (k.to_string(), *v)).collect::<HashMap<_, _>>());
    r["spectrum"] = spec.report();
    r["remote_cands"] = json!({"total": cs.total, "added": cs.ok, "mdns_unparsed": cs.mdns, "bad": cs.bad});
    r["rx_sources"] = json!(last_src.iter().map(|(k, v)| (k.to_string(), *v)).collect::<HashMap<_, _>>());
    r["states"] = json!(states);
    log(r);
}
