//! answerer-webrtc: the webrtc-rs 0.21 (async API over the sans-IO `rtc` core) answerer of the
//! WebRTC library spike (DEV-764 S1). One TCP connection = one session = one PeerConnection.
#[path = "../../common/common.rs"]
mod common;

use common::*;
use rtc::media_stream::MediaStreamTrack;
use rtc::peer_connection::configuration::media_engine::{MediaEngine, MIME_TYPE_PCMU};
use rtc::peer_connection::sdp::RTCSessionDescription;
use rtc::rtp::header::Header;
use rtc::rtp::packet::Packet;
use rtc::rtp_transceiver::rtp_sender::{
    RTCRtpCodec, RTCRtpCodecParameters, RTCRtpCodingParameters, RTCRtpEncodingParameters, RtpCodecKind,
};
use serde_json::{json, Value};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::mpsc;
use webrtc::media_stream::track_local::static_rtp::TrackLocalStaticRTP;
use webrtc::media_stream::track_local::TrackLocal;
use webrtc::media_stream::track_remote::{TrackRemote, TrackRemoteEvent};
use webrtc::peer_connection::{
    register_default_interceptors, PeerConnection, PeerConnectionBuilder, PeerConnectionEventHandler,
    RTCConfigurationBuilder, RTCIceCandidateInit, RTCIceConnectionState, RTCPeerConnectionIceEvent,
    RTCPeerConnectionState, Registry,
};

struct Shared {
    label: String,
    meter: Mutex<Meter>,
    states: Mutex<Vec<String>>,
    connected: AtomicBool,
    // candidates are held back until the answer for the current offer is on the wire
    gate: Mutex<(bool, Vec<String>)>,
    out: mpsc::UnboundedSender<Value>,
}
impl Shared {
    fn state(&self, kind: &str, s: String) {
        logj(json!({"id": self.label, "ev": kind, "state": s}));
        self.states.lock().unwrap().push(format!("{} {}={}", now_hms(), kind, s));
    }
}

struct Handler(Arc<Shared>);

#[async_trait::async_trait]
impl PeerConnectionEventHandler for Handler {
    async fn on_ice_candidate(&self, event: RTCPeerConnectionIceEvent) {
        if let Ok(init) = event.candidate.to_json() {
            let mut g = self.0.gate.lock().unwrap();
            if g.0 {
                let _ = self.0.out.send(json!({"t": "cand", "c": init.candidate}));
            } else {
                g.1.push(init.candidate);
            }
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
                    sh.meter.lock().unwrap().packet(
                        p.header.sequence_number,
                        p.header.timestamp,
                        &p.payload,
                        Instant::now(),
                    );
                }
            }
        });
    }
}

#[tokio::main]
async fn main() {
    let args = Args::parse();
    let l = TcpListener::bind(&args.listen).await.expect("listen");
    logj(json!({"ev": "LISTEN", "addr": l.local_addr().unwrap().to_string(), "lib": "webrtc 0.21.1"}));
    let mut hs = vec![];
    for i in 0..args.sessions {
        let (s, _) = l.accept().await.expect("accept");
        let a = args.clone();
        hs.push(tokio::spawn(session(i, s, a)));
    }
    let mut code = 0;
    for h in hs {
        if h.await.is_err() {
            code = 1;
        }
    }
    std::process::exit(code);
}

async fn session(id: usize, stream: TcpStream, a: Args) {
    let label = format!("webrtc-{id}");
    let (out_tx, mut out_rx) = mpsc::unbounded_channel::<Value>();
    let sh = Arc::new(Shared {
        label: label.clone(),
        meter: Mutex::new(Meter::new(a.rx_hz)),
        states: Mutex::new(vec![]),
        connected: AtomicBool::new(false),
        gate: Mutex::new((false, vec![])),
        out: out_tx.clone(),
    });
    let (rd, mut wr) = stream.into_split();
    tokio::spawn(async move {
        while let Some(v) = out_rx.recv().await {
            if wr.write_all(format!("{v}\n").as_bytes()).await.is_err() {
                break;
            }
        }
    });

    // PCMU only, like internal/sfu does.
    let mut me = MediaEngine::default();
    let codec = RTCRtpCodec {
        mime_type: MIME_TYPE_PCMU.to_owned(),
        clock_rate: 8000,
        channels: 0,
        sdp_fmtp_line: String::new(),
        rtcp_feedback: vec![],
    };
    me.register_codec(
        RTCRtpCodecParameters { rtp_codec: codec.clone(), payload_type: 0 },
        RtpCodecKind::Audio,
    )
    .unwrap();
    let registry = register_default_interceptors(Registry::new(), &mut me).unwrap();

    // `ring` is webrtc-rs's default provider; with `--features aws-lc` (and no ring) the provider is chosen here.
    #[cfg(feature = "aws-lc")]
    let setting_engine = webrtc::peer_connection::SettingEngineBuilder::default()
        .with_crypto_provider(Arc::new(webrtc::peer_connection::crypto::providers::AwsLcRsProvider::new()))
        .build();
    #[cfg(not(feature = "aws-lc"))]
    let setting_engine = webrtc::peer_connection::SettingEngineBuilder::default().build();
    let pc = PeerConnectionBuilder::new()
        .with_setting_engine(setting_engine)
        .with_configuration(RTCConfigurationBuilder::new().build())
        .with_media_engine(me)
        .with_interceptor_registry(registry)
        .with_handler(Arc::new(Handler(sh.clone())))
        .with_udp_addrs(vec!["127.0.0.1:0"])
        .build()
        .await
        .expect("build peer connection");

    let ssrc: u32 = 0xa11c_e001 + id as u32;
    let track: Arc<TrackLocalStaticRTP> = Arc::new(TrackLocalStaticRTP::new(MediaStreamTrack::new(
        "spike-stream".into(),
        "spike-track".into(),
        "spike-label".into(),
        RtpCodecKind::Audio,
        vec![RTCRtpEncodingParameters {
            rtp_coding_parameters: RTCRtpCodingParameters { ssrc: Some(ssrc), ..Default::default() },
            codec: codec.clone(),
            ..Default::default()
        }],
    )));
    let _sender = pc.add_track(track.clone() as Arc<dyn TrackLocal>).await.expect("add_track");

    // the tone
    let tx_ok = Arc::new(AtomicU64::new(0));
    let tx_err = Arc::new(AtomicU64::new(0));
    let stop = Arc::new(AtomicBool::new(false));
    {
        let (sh, track, tx_ok, tx_err, stop) = (sh.clone(), track.clone(), tx_ok.clone(), tx_err.clone(), stop.clone());
        let hz = a.hz;
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
                    header: Header {
                        version: 2,
                        payload_type: 0,
                        sequence_number: seq,
                        timestamp: ts,
                        ssrc,
                        ..Default::default()
                    },
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

    let start = Instant::now();
    let mut snap = tokio::time::interval(Duration::from_secs_f64(a.snap));
    snap.tick().await;
    let mut lines = BufReader::new(rd).lines();
    loop {
        tokio::select! {
            _ = snap.tick() => {
                let mut r = sh.meter.lock().unwrap().report();
                r["id"] = json!(label); r["ev"] = json!("SNAP");
                r["tx"] = json!(tx_ok.load(Ordering::Relaxed)); r["elapsed_s"] = json!(start.elapsed().as_secs());
                logj(r);
            }
            line = lines.next_line() => {
                let line = match line { Ok(Some(l)) => l, _ => break };
                let m: Value = match serde_json::from_str(&line) { Ok(v) => v, Err(_) => continue };
                match m["t"].as_str() {
                    Some("offer") => {
                        sh.gate.lock().unwrap().0 = false;
                        let r: Result<String, String> = async {
                            let offer = RTCSessionDescription::offer(m["sdp"].as_str().unwrap_or("").to_string()).map_err(|e| format!("parse offer: {e}"))?;
                            pc.set_remote_description(offer).await.map_err(|e| format!("set_remote_description: {e}"))?;
                            let ans = pc.create_answer(None).await.map_err(|e| format!("create_answer: {e}"))?;
                            pc.set_local_description(ans).await.map_err(|e| format!("set_local_description: {e}"))?;
                            Ok(pc.local_description().await.map(|d| d.sdp).unwrap_or_default())
                        }.await;
                        match r {
                            Ok(sdp) => {
                                let sdp = if a.trickle { split_candidates(&sdp).0 } else { sdp };
                                let _ = sh.out.send(json!({"t": "answer", "sdp": sdp}));
                                let mut g = sh.gate.lock().unwrap();
                                g.0 = true;
                                for c in g.1.drain(..) { let _ = sh.out.send(json!({"t": "cand", "c": c})); }
                            }
                            Err(e) => logj(json!({"id": label, "error": e})),
                        }
                    }
                    Some("cand") => {
                        let init = RTCIceCandidateInit { candidate: m["c"].as_str().unwrap_or("").to_string(), ..Default::default() };
                        if let Err(e) = pc.add_ice_candidate(init).await {
                            logj(json!({"id": label, "error": format!("add_ice_candidate: {e}")}));
                        }
                    }
                    Some("bye") => break,
                    _ => {}
                }
            }
        }
    }
    stop.store(true, Ordering::Relaxed);
    let _ = pc.close().await;
    let mut r = sh.meter.lock().unwrap().report();
    r["id"] = json!(label);
    r["ev"] = json!("REPORT");
    r["role"] = json!("answerer-webrtc");
    r["tx"] = json!(tx_ok.load(Ordering::Relaxed));
    r["tx_errors"] = json!(tx_err.load(Ordering::Relaxed));
    r["states"] = json!(sh.states.lock().unwrap().clone());
    logj(r);
}
