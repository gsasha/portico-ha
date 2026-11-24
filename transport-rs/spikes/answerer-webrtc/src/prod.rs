//! prod: the production-shaped webrtc-rs 0.21.1 answerer of spike S1b (DEV-764), the fallback to compare
//! with `answerer-str0m/src/prod.rs`. Same shape: PCMU only, `/1` rewrite, the offer handled in its own
//! task so a panic is a `JoinError`, a NON-trickle answer (waits for ICE gathering to complete, like
//! `sfu.go`) with a UDP socket per local interface address, remote trickle accepted at any time, the
//! same line-JSON protocol and the same JSON log lines (so `browser/harness.py` drives both).
#[path = "../../common/common.rs"]
mod common;
#[path = "../../common/prod_common.rs"]
mod prod_common;

use common::*;
use prod_common::*;
use rtc::media_stream::MediaStreamTrack;
use rtc::peer_connection::configuration::media_engine::{MediaEngine, MIME_TYPE_PCMU};
use rtc::peer_connection::sdp::RTCSessionDescription;
use rtc::rtp::header::Header;
use rtc::rtp::packet::Packet;
use rtc::rtp_transceiver::rtp_sender::{
    RTCRtpCodec, RTCRtpCodecParameters, RTCRtpCodingParameters, RTCRtpEncodingParameters, RtpCodecKind,
};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tokio::io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::mpsc;
use webrtc::media_stream::track_local::static_rtp::TrackLocalStaticRTP;
use webrtc::media_stream::track_local::TrackLocal;
use webrtc::media_stream::track_remote::{TrackRemote, TrackRemoteEvent};
use webrtc::peer_connection::{
    register_default_interceptors, PeerConnection, PeerConnectionBuilder, PeerConnectionEventHandler,
    RTCConfigurationBuilder, RTCIceCandidateInit, RTCIceConnectionState, RTCIceGatheringState,
    RTCPeerConnectionIceEvent, RTCPeerConnectionState, Registry,
};

const MAX_LINE: u64 = 8 * 1024 * 1024;

struct Shared {
    label: String,
    meter: Mutex<Meter>,
    spec: Mutex<Spectrum>,
    pt_counts: Mutex<HashMap<u8, u64>>,
    states: Mutex<Vec<String>>,
    connected: AtomicBool,
    gather_done: AtomicBool,
    cands_seen: AtomicU64,
    start: Instant,
}
impl Shared {
    fn state(&self, kind: &str, s: String) {
        log(json!({"id": self.label, "ev": "state", "kind": kind, "state": s, "since_start_ms": self.start.elapsed().as_millis() as u64}));
        self.states.lock().unwrap().push(format!("{} {}={}", now_hms(), kind, s));
    }
}

struct Handler(Arc<Shared>);

#[async_trait::async_trait]
impl PeerConnectionEventHandler for Handler {
    async fn on_ice_candidate(&self, _event: RTCPeerConnectionIceEvent) {
        // non-trickle answer: the candidates are in the answer SDP; this only counts them
        self.0.cands_seen.fetch_add(1, Ordering::SeqCst);
    }
    async fn on_ice_gathering_state_change(&self, s: RTCIceGatheringState) {
        if s == RTCIceGatheringState::Complete {
            self.0.gather_done.store(true, Ordering::SeqCst);
        }
    }
    async fn on_ice_connection_state_change(&self, s: RTCIceConnectionState) {
        self.0.state("ice", format!("{s}"));
    }
    async fn on_connection_state_change(&self, s: RTCPeerConnectionState) {
        if s == RTCPeerConnectionState::Connected {
            self.0.connected.store(true, Ordering::SeqCst);
        }
        self.0.state("pc", format!("{s}"));
    }
    async fn on_track(&self, track: Arc<dyn TrackRemote>) {
        let sh = self.0.clone();
        tokio::spawn(async move {
            while let Some(evt) = track.poll().await {
                if let TrackRemoteEvent::OnRtpPacket(p) = evt {
                    *sh.pt_counts.lock().unwrap().entry(p.header.payload_type).or_insert(0) += 1;
                    sh.meter.lock().unwrap().packet(p.header.sequence_number, p.header.timestamp, &p.payload, Instant::now());
                    sh.spec.lock().unwrap().feed(&p.payload);
                }
            }
        });
    }
}

#[derive(Clone)]
struct Cfg {
    listen: String,
    hz: f64,
    snap: f64,
    answer_delay_ms: u64,
    only_loopback: bool,
    sdp_limits: bool,
}

#[tokio::main]
async fn main() {
    let mut c = Cfg { listen: "127.0.0.1:0".into(), hz: 440.0, snap: 10.0, answer_delay_ms: 0, only_loopback: false, sdp_limits: true };
    let v: Vec<String> = std::env::args().collect();
    let mut i = 1;
    while i < v.len() {
        let val = v.get(i + 1).cloned().unwrap_or_default();
        match v[i].as_str() {
            "--listen" => c.listen = val,
            "--hz" => c.hz = val.parse().unwrap(),
            "--snap" => c.snap = val.parse().unwrap(),
            "--answer-delay-ms" => c.answer_delay_ms = val.parse().unwrap(),
            "--max-stun-retransmits" => {} // no such knob in webrtc-rs 0.21.1 (ignored, so the harness flags work for both)
            "--no-sdp-limits" => { c.sdp_limits = false; i -= 1; }
            "--loopback-only" => { c.only_loopback = true; i -= 1; }
            x => panic!("unknown flag {x}"),
        }
        i += 2;
    }
    let l = TcpListener::bind(&c.listen).await.expect("listen");
    let addrs = local_addrs(c.only_loopback);
    log(json!({"ev": "LISTEN", "addr": l.local_addr().unwrap().to_string(), "lib": "webrtc 0.21.1 prod",
        "interfaces": addrs.iter().map(|(n, a)| format!("{n} {a}")).collect::<Vec<_>>()}));
    let mut n = 0usize;
    loop {
        let Ok((s, _)) = l.accept().await else { continue };
        let id = n;
        n += 1;
        let cfg = c.clone();
        tokio::spawn(async move {
            // a panic inside the session task is a JoinError for the supervisor, not a dead process
            let h = tokio::spawn(session(id, s, cfg));
            if let Err(e) = h.await {
                log(json!({"id": format!("webrtc-{id}"), "ev": "SESSION_PANIC", "e": e.to_string()}));
            }
        });
    }
}

async fn build_pc(sh: &Arc<Shared>, cfg: &Cfg) -> Result<(Arc<dyn PeerConnection>, Arc<TrackLocalStaticRTP>, u32, RTCRtpCodec), String> {
    let mut me = MediaEngine::default();
    let codec = RTCRtpCodec { mime_type: MIME_TYPE_PCMU.to_owned(), clock_rate: 8000, channels: 0, sdp_fmtp_line: String::new(), rtcp_feedback: vec![] };
    me.register_codec(RTCRtpCodecParameters { rtp_codec: codec.clone(), payload_type: 0 }, RtpCodecKind::Audio).map_err(|e| e.to_string())?;
    let registry = register_default_interceptors(Registry::new(), &mut me).map_err(|e| e.to_string())?;
    let setting_engine = webrtc::peer_connection::SettingEngineBuilder::default().build();
    let addrs: Vec<String> = local_addrs(cfg.only_loopback)
        .into_iter()
        .map(|(_, ip)| match ip {
            std::net::IpAddr::V4(a) => format!("{a}:0"),
            std::net::IpAddr::V6(a) => format!("[{a}]:0"),
        })
        .collect();
    let pc = PeerConnectionBuilder::new()
        .with_setting_engine(setting_engine)
        .with_configuration(RTCConfigurationBuilder::new().build())
        .with_media_engine(me)
        .with_interceptor_registry(registry)
        .with_handler(Arc::new(Handler(sh.clone())))
        .with_udp_addrs(addrs)
        .build()
        .await
        .map_err(|e| e.to_string())?;
    let ssrc: u32 = 0xa11c_e001;
    let track = Arc::new(TrackLocalStaticRTP::new(MediaStreamTrack::new(
        "spike-stream".into(), "spike-track".into(), "spike-label".into(), RtpCodecKind::Audio,
        vec![RTCRtpEncodingParameters {
            rtp_coding_parameters: RTCRtpCodingParameters { ssrc: Some(ssrc), ..Default::default() },
            codec: codec.clone(),
            ..Default::default()
        }],
    )));
    let pc: Arc<dyn PeerConnection> = Arc::new(pc);
    pc.add_track(track.clone() as Arc<dyn TrackLocal>).await.map_err(|e| e.to_string())?;
    Ok((pc, track, ssrc, codec))
}

async fn session(id: usize, stream: TcpStream, cfg: Cfg) {
    let label = format!("webrtc-{id}");
    let sh = Arc::new(Shared {
        label: label.clone(),
        meter: Mutex::new(Meter::new(cfg.hz)),
        spec: Mutex::new(Spectrum::new()),
        pt_counts: Mutex::new(HashMap::new()),
        states: Mutex::new(vec![]),
        connected: AtomicBool::new(false),
        gather_done: AtomicBool::new(false),
        cands_seen: AtomicU64::new(0),
        start: Instant::now(),
    });
    let (out_tx, mut out_rx) = mpsc::unbounded_channel::<Value>();
    let (rd, mut wr) = stream.into_split();
    tokio::spawn(async move {
        while let Some(v) = out_rx.recv().await {
            if wr.write_all(format!("{v}\n").as_bytes()).await.is_err() {
                break;
            }
        }
        let _ = wr.shutdown().await;
    });
    log(json!({"id": label, "ev": "session"}));

    let (pc, track, ssrc, _codec) = match build_pc(&sh, &cfg).await {
        Ok(x) => x,
        Err(e) => {
            let _ = out_tx.send(json!({"t": "error", "msg": format!("build: {e}")}));
            return;
        }
    };

    let tx_ok = Arc::new(AtomicU64::new(0));
    let tx_err = Arc::new(AtomicU64::new(0));
    let stop = Arc::new(AtomicBool::new(false));
    {
        let (sh, track, tx_ok, tx_err, stop) = (sh.clone(), track.clone(), tx_ok.clone(), tx_err.clone(), stop.clone());
        let hz = cfg.hz;
        tokio::spawn(async move {
            let mut tone = Tone::new(hz);
            let mut iv = tokio::time::interval(Duration::from_millis(20));
            iv.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
            let (mut seq, mut ts) = (0u16, 0u32);
            while !stop.load(Ordering::Relaxed) {
                iv.tick().await;
                if !sh.connected.load(Ordering::Relaxed) {
                    continue;
                }
                let pkt = Packet {
                    header: Header { version: 2, payload_type: 0, sequence_number: seq, timestamp: ts, ssrc, ..Default::default() },
                    payload: tone.frame().into(),
                };
                match track.write_rtp(pkt).await {
                    Ok(_) => tx_ok.fetch_add(1, Ordering::Relaxed),
                    Err(_) => tx_err.fetch_add(1, Ordering::Relaxed),
                };
                seq = seq.wrapping_add(1);
                ts = ts.wrapping_add(160);
            }
        });
    }

    let mut snap = tokio::time::interval(Duration::from_secs_f64(cfg.snap));
    snap.tick().await;
    let mut rd = BufReader::new(rd);
    let mut pending: Vec<String> = vec![];
    let mut have_offer = false;
    let (mut c_total, mut c_ok, mut c_mdns, mut c_bad) = (0u64, 0u64, 0u64, 0u64);
    loop {
        let mut line = Vec::new();
        let mut lim = (&mut rd).take(MAX_LINE);
        tokio::select! {
            _ = snap.tick() => {
                let mut r = sh.meter.lock().unwrap().report();
                r["id"] = json!(label); r["ev"] = json!("SNAP");
                r["tx"] = json!(tx_ok.load(Ordering::Relaxed));
                r["pt_counts"] = json!(sh.pt_counts.lock().unwrap().iter().map(|(k, v)| (k.to_string(), *v)).collect::<HashMap<_, _>>());
                r["spectrum"] = sh.spec.lock().unwrap().report();
                r["elapsed_s"] = json!(sh.start.elapsed().as_secs());
                log(r);
                continue;
            }
            n = lim.read_until(b'\n', &mut line) => {
                let n = n.unwrap_or(0);
                if n == 0 { break; }
                if line.last() != Some(&b'\n') && n as u64 >= MAX_LINE {
                    let _ = out_tx.send(json!({"t": "error", "msg": "signalling line too long"}));
                    break;
                }
            }
        }
        let m: Value = match serde_json::from_slice::<Value>(&line) {
            Ok(v) if v.is_object() => v,
            _ => {
                let _ = out_tx.send(json!({"t": "error", "msg": "bad json"}));
                break;
            }
        };
        match m["t"].as_str() {
            Some("offer") => {
                let raw = m["sdp"].as_str().unwrap_or("").to_string();
                let sdp = normalise_offer(&raw);
                let rewritten = sdp != raw;
                if cfg.sdp_limits && offer_too_large(&sdp) {
                    let _ = out_tx.send(json!({"t": "error", "msg": format!("offer too large ({} bytes)", sdp.len())}));
                    break;
                }
                let t0 = Instant::now();
                // isolate the library call in its own task: a panic comes back as a JoinError
                let pc2 = pc.clone();
                let sh2 = sh.clone();
                let h = tokio::spawn(async move {
                    sh2.gather_done.store(false, Ordering::SeqCst);
                    let offer = RTCSessionDescription::offer(sdp).map_err(|e| format!("parse offer: {e}"))?;
                    pc2.set_remote_description(offer).await.map_err(|e| format!("set_remote_description: {e}"))?;
                    let ans = pc2.create_answer(None).await.map_err(|e| format!("create_answer: {e}"))?;
                    pc2.set_local_description(ans).await.map_err(|e| format!("set_local_description: {e}"))?;
                    // non-trickle: wait for gathering to complete, like sfu.go (bounded at 3 s)
                    let t = Instant::now();
                    while !sh2.gather_done.load(Ordering::SeqCst) && t.elapsed() < Duration::from_secs(3) {
                        tokio::time::sleep(Duration::from_millis(10)).await;
                    }
                    let gathered = sh2.gather_done.load(Ordering::SeqCst);
                    let sdp = pc2.local_description().await.map(|d| d.sdp).unwrap_or_default();
                    Ok::<(String, bool, u64), String>((sdp, gathered, t.elapsed().as_millis() as u64))
                });
                match h.await {
                    Ok(Ok((mut ans, gathered, gather_ms))) => {
                        have_offer = true;
                        if !ans.contains("a=end-of-candidates") && ans.contains("a=candidate:") {
                            if !ans.ends_with("\r\n") { ans.push_str("\r\n"); }
                            ans.push_str("a=end-of-candidates\r\n");
                        }
                        let ncand = ans.matches("a=candidate:").count();
                        log(json!({"id": label, "ev": "offer-accepted", "rewritten_rtpmap": rewritten, "accept_ms": t0.elapsed().as_millis() as u64,
                            "gather_complete": gathered, "gather_wait_ms": gather_ms, "answer_candidates": ncand, "answer_sdp": ans}));
                        if cfg.answer_delay_ms > 0 {
                            tokio::time::sleep(Duration::from_millis(cfg.answer_delay_ms)).await;
                        }
                        let _ = out_tx.send(json!({"t": "answer", "sdp": ans}));
                        for c in std::mem::take(&mut pending) {
                            add_cand(&pc, &c, &label, &mut (c_total, c_ok, c_mdns, c_bad)).await;
                        }
                    }
                    Ok(Err(e)) => {
                        log(json!({"id": label, "ev": "offer-rejected", "e": e}));
                        let _ = out_tx.send(json!({"t": "error", "msg": e}));
                        break;
                    }
                    Err(p) => {
                        log(json!({"id": label, "ev": "offer-PANIC-CAUGHT", "msg": p.to_string()}));
                        let _ = out_tx.send(json!({"t": "error", "msg": format!("panic: {p}")}));
                        break;
                    }
                }
            }
            Some("cand") => {
                let c = m["c"].as_str().unwrap_or("").to_string();
                if have_offer {
                    let mut st = (c_total, c_ok, c_mdns, c_bad);
                    add_cand(&pc, &c, &label, &mut st).await;
                    (c_total, c_ok, c_mdns, c_bad) = st;
                } else {
                    pending.push(c);
                }
            }
            Some("bye") | None => break,
            _ => {}
        }
    }
    stop.store(true, Ordering::Relaxed);
    let _ = pc.close().await;
    let mut r = sh.meter.lock().unwrap().report();
    r["id"] = json!(label);
    r["ev"] = json!("REPORT");
    r["role"] = json!("prod-webrtc");
    r["tx"] = json!(tx_ok.load(Ordering::Relaxed));
    r["tx_errors"] = json!(tx_err.load(Ordering::Relaxed));
    r["pt_counts"] = json!(sh.pt_counts.lock().unwrap().iter().map(|(k, v)| (k.to_string(), *v)).collect::<HashMap<_, _>>());
    r["spectrum"] = sh.spec.lock().unwrap().report();
    r["remote_cands"] = json!({"total": c_total, "added": c_ok, "mdns_unparsed": c_mdns, "bad": c_bad, "local_cand_events": sh.cands_seen.load(Ordering::SeqCst)});
    r["states"] = json!(sh.states.lock().unwrap().clone());
    log(r);
}

async fn add_cand(pc: &Arc<dyn PeerConnection>, c: &str, label: &str, st: &mut (u64, u64, u64, u64)) {
    st.0 += 1;
    let t = c.trim();
    if t.is_empty() || t.contains("end-of-candidates") {
        log(json!({"id": label, "ev": "end-of-candidates", "c": t}));
        return;
    }
    let t = t.strip_prefix("a=").unwrap_or(t);
    let init = RTCIceCandidateInit { candidate: t.to_string(), ..Default::default() };
    match pc.add_ice_candidate(init).await {
        Ok(_) => {
            st.1 += 1;
            log(json!({"id": label, "ev": "remote-cand", "ok": true, "mdns": t.contains(".local"), "c": t}));
        }
        Err(e) => {
            if t.contains(".local") { st.2 += 1 } else { st.3 += 1 }
            log(json!({"id": label, "ev": "remote-cand", "ok": false, "mdns": t.contains(".local"), "c": t, "e": e.to_string()}));
        }
    }
}
