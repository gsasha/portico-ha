//! A second `str0m` instance as the OFFERER over loopback: the in-process stand-in for the
//! browser (Go's tests use a pion peer, `newBrowserPeer` / `newPeerWith`). It offers the codecs
//! it is told to, in the m-line order it is told to, can send a tone or raw payloads, and records
//! every RTP packet it receives (payload type, sequence number, timestamp, payload).

// Each test crate that declares `mod common;` compiles every helper here and uses a subset, so an
// item unused in one crate is not dead in the suite (R1-202: a per-consumer split is the alternative).
#![allow(dead_code)]

use std::net::SocketAddr;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use str0m::change::{SdpAnswer, SdpPendingOffer};
use str0m::format::Codec;
use str0m::media::{Direction, Frequency, MediaKind, MediaTime, Mid, Pt};
use str0m::net::{Protocol, Receive};
use str0m::rtp::RawPacket;
use str0m::{Candidate, Event, IceConnectionState, Input, Output, Rtc, RtcConfig};
use tokio::net::UdpSocket;
use tokio::sync::{mpsc, oneshot};
use transport_rs::sfu::{encode_mu_law, G722Encoder};

#[derive(Clone, Debug)]
pub struct PeerOpts {
    pub opus: bool,
    pub g722: bool,
    pub pcmu: bool,
    /// Rewrites the offer text (reorder the m-line, hand-made variations).
    pub rewrite: Option<fn(String) -> String>,
    /// The local address of the peer's UDP socket (default `127.0.0.1:0`).
    pub bind: Option<SocketAddr>,
    /// G.722 and PCMU under these DYNAMIC payload types instead of 9 and 0.
    pub dynamic: Option<(u8, u8)>,
    /// A recvonly VIDEO section after the audio one (H.264; the session under test has no video
    /// codec), as a Safari or an Android offer may carry.
    pub video: bool,
    /// A second recvonly audio section after the first.
    pub second_audio: bool,
}

impl Default for PeerOpts {
    fn default() -> Self {
        PeerOpts {
            opus: false,
            g722: false,
            pcmu: true,
            rewrite: None,
            bind: None,
            dynamic: None,
            video: false,
            second_audio: false,
        }
    }
}

#[derive(Clone, Debug)]
pub struct Rx {
    pub pt: u8,
    pub seq: u16,
    pub ts: u32,
    pub marker: bool,
    pub payload: Vec<u8>,
    pub at: Instant,
    /// the mid in the packet's header extension, when it carries one
    pub mid: Option<String>,
}

enum Cmd {
    Offer(oneshot::Sender<String>),
    Reoffer(oneshot::Sender<String>), // an ICE restart
    Answer(String, oneshot::Sender<Result<(), String>>),
    Candidate(String),
    /// payload of the given payload type, rtp time in 8 kHz ticks
    Send(bool, u64, Vec<u8>),
    Tone {
        wide: bool,
        hz: f64,
        ptime_ms: u64,
        amp: f64,
        seconds: f64,
    },
    /// stop answering, no goodbye (a browser that was killed)
    Kill,
    /// stop answering (true) or answer again (false): a browser that is out of reach for a while
    Stall(bool),
    Close,
}

pub struct Peer {
    pub addr: SocketAddr,
    pub rx: Arc<Mutex<Vec<Rx>>>,
    pub connected: Arc<AtomicBool>,
    pub states: Arc<Mutex<Vec<String>>>,
    pub tx_count: Arc<AtomicU64>,
    cmd: mpsc::UnboundedSender<Cmd>,
    pub task: tokio::task::JoinHandle<()>,
}

impl Peer {
    pub async fn new(o: PeerOpts) -> Peer {
        static ONCE: std::sync::Once = std::sync::Once::new();
        ONCE.call_once(|| str0m::crypto::from_feature_flags().install_process_default());
        let bind = o.bind.unwrap_or_else(|| "127.0.0.1:0".parse().unwrap());
        let sock = UdpSocket::bind(bind).await.unwrap();
        let addr = sock.local_addr().unwrap();
        let mut rc = RtcConfig::new().clear_codecs().enable_raw_packets(true);
        if o.opus {
            rc = rc.enable_opus(true, false);
        }
        if let Some((g, u)) = o.dynamic {
            if o.g722 {
                rc.codec_config().add_config(
                    Pt::new_with_value(g),
                    None,
                    Codec::G722,
                    Frequency::SIXTEEN_KHZ,
                    Some(1),
                    Default::default(),
                );
            }
            if o.pcmu {
                rc.codec_config().add_config(
                    Pt::new_with_value(u),
                    None,
                    Codec::PCMU,
                    Frequency::EIGHT_KHZ,
                    Some(1),
                    Default::default(),
                );
            }
        } else {
            if o.g722 {
                rc = rc.enable_g722(true, false);
            }
            if o.pcmu {
                rc = rc.enable_pcmu(true, false);
            }
        }
        if o.video {
            rc = rc.enable_h264(true);
        }
        let mut rtc = rc.build(Instant::now());
        rtc.add_local_candidate(Candidate::host(addr, "udp").unwrap());
        let (cmd, cmd_rx) = mpsc::unbounded_channel();
        let rx = Arc::new(Mutex::new(Vec::new()));
        let connected = Arc::new(AtomicBool::new(false));
        let states = Arc::new(Mutex::new(Vec::new()));
        let tx_count = Arc::new(AtomicU64::new(0));
        let task = tokio::spawn(peer_loop(
            rtc,
            sock,
            o,
            cmd_rx,
            rx.clone(),
            connected.clone(),
            states.clone(),
            tx_count.clone(),
        ));
        Peer {
            addr,
            rx,
            connected,
            states,
            tx_count,
            cmd,
            task,
        }
    }

    pub async fn offer(&self) -> String {
        let (t, r) = oneshot::channel();
        self.cmd.send(Cmd::Offer(t)).unwrap();
        r.await.unwrap()
    }

    /// An ICE-restarting re-offer.
    pub async fn reoffer(&self) -> String {
        let (t, r) = oneshot::channel();
        self.cmd.send(Cmd::Reoffer(t)).unwrap();
        r.await.unwrap()
    }

    pub async fn accept_answer(&self, sdp: &str) -> Result<(), String> {
        let (t, r) = oneshot::channel();
        self.cmd.send(Cmd::Answer(sdp.to_string(), t)).unwrap();
        r.await.unwrap()
    }

    pub fn add_candidate(&self, c: &str) {
        let _ = self.cmd.send(Cmd::Candidate(c.to_string()));
    }

    /// A payload of the wide (G.722) or narrow (PCMU) codec, rtp time in 8 kHz ticks.
    pub fn send(&self, wide: bool, ticks_8k: u64, payload: Vec<u8>) {
        let _ = self.cmd.send(Cmd::Send(wide, ticks_8k, payload));
    }

    /// A tone of `seconds`, one packet per `ptime_ms`, encoded as PCMU (pt 0) or G.722 (pt 9),
    /// paced in real time.
    pub fn tone(&self, wide: bool, hz: f64, ptime_ms: u64, amp: f64, seconds: f64) {
        let _ = self.cmd.send(Cmd::Tone {
            wide,
            hz,
            ptime_ms,
            amp,
            seconds,
        });
    }

    pub fn kill(&self) {
        let _ = self.cmd.send(Cmd::Kill);
    }

    pub fn stall(&self, on: bool) {
        let _ = self.cmd.send(Cmd::Stall(on));
    }

    pub fn close(&self) {
        let _ = self.cmd.send(Cmd::Close);
    }

    pub fn received(&self) -> Vec<Rx> {
        self.rx.lock().unwrap().clone()
    }

    pub async fn wait_connected(&self, secs: u64) -> bool {
        let end = Instant::now() + Duration::from_secs(secs);
        while Instant::now() < end {
            if self.connected.load(Ordering::SeqCst) {
                return true;
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
        false
    }
}

struct ToneJob {
    wide: bool,
    hz: f64,
    ptime_ms: u64,
    amp: f64,
    left: u64,
    phase: f64,
    next: tokio::time::Instant,
    enc: G722Encoder,
}

#[allow(clippy::too_many_arguments)]
async fn peer_loop(
    mut rtc: Rtc,
    sock: UdpSocket,
    o: PeerOpts,
    mut cmd_rx: mpsc::UnboundedReceiver<Cmd>,
    rxs: Arc<Mutex<Vec<Rx>>>,
    connected: Arc<AtomicBool>,
    states: Arc<Mutex<Vec<String>>>,
    tx_count: Arc<AtomicU64>,
) {
    let local = sock.local_addr().unwrap();
    let (pt_g722, pt_pcmu) = o.dynamic.unwrap_or((9, 0));
    let mut mid: Option<Mid> = None;
    let mut pending: Option<SdpPendingOffer> = None;
    let mut ts: u64 = 0;
    let mut tone: Option<ToneJob> = None;
    let mut buf = vec![0u8; 2048];
    let mut killed = false;
    loop {
        // drain
        let timeout = loop {
            match rtc.poll_output() {
                Ok(Output::Timeout(t)) => break t,
                Ok(Output::Transmit(t)) => {
                    if !killed {
                        let _ = sock.try_send_to(&t.contents, t.destination);
                    }
                }
                Ok(Output::Event(e)) => match e {
                    Event::Connected => {
                        connected.store(true, Ordering::SeqCst);
                        states.lock().unwrap().push("connected".into());
                    }
                    Event::IceConnectionStateChange(s) => {
                        states.lock().unwrap().push(format!("{s:?}"));
                        if s == IceConnectionState::Disconnected {
                            connected.store(false, Ordering::SeqCst);
                        }
                    }
                    Event::MediaAdded(m) => mid = Some(m.mid),
                    Event::RawPacket(p) => {
                        if let RawPacket::RtpRx(h, payload) = *p {
                            rxs.lock().unwrap().push(Rx {
                                pt: *h.payload_type,
                                seq: h.sequence_number,
                                ts: h.timestamp,
                                marker: h.marker,
                                payload,
                                at: Instant::now(),
                                mid: h.ext_vals.mid.map(|m| m.to_string()),
                            });
                        }
                    }
                    _ => {}
                },
                Err(_) => return,
            }
        };
        if !rtc.is_alive() {
            return;
        }
        let tone_at = tone.as_ref().map(|t| t.next);
        let mut wake = tokio::time::Instant::from_std(timeout);
        if let Some(t) = tone_at {
            wake = wake.min(t);
        }
        tokio::select! {
            c = cmd_rx.recv() => {
                let Some(c) = c else { return };
                match c {
                    Cmd::Offer(reply) => {
                        let mut api = rtc.sdp_api();
                        let m = api.add_media(MediaKind::Audio, Direction::SendRecv, None, None, None);
                        if o.video {
                            api.add_media(MediaKind::Video, Direction::RecvOnly, None, None, None);
                        }
                        if o.second_audio {
                            api.add_media(MediaKind::Audio, Direction::RecvOnly, None, None, None);
                        }
                        let (offer, p) = api.apply().unwrap();
                        mid = Some(m);
                        pending = Some(p);
                        let mut text = offer.to_sdp_string();
                        if let Some(f) = o.rewrite { text = f(text); }
                        let _ = reply.send(text);
                    }
                    Cmd::Reoffer(reply) => {
                        let mut api = rtc.sdp_api();
                        api.ice_restart(true);
                        if let Some((offer, p)) = api.apply() {
                            pending = Some(p);
                            let mut text = offer.to_sdp_string();
                            if let Some(f) = o.rewrite { text = f(text); }
                            let _ = reply.send(text);
                        } else {
                            let _ = reply.send(String::new());
                        }
                    }
                    Cmd::Answer(sdp, reply) => {
                        let r = (|| {
                            let ans = SdpAnswer::from_sdp_string(&sdp).map_err(|e| e.to_string())?;
                            let p = pending.take().ok_or("no pending offer")?;
                            rtc.sdp_api().accept_answer(p, ans).map_err(|e| e.to_string())
                        })();
                        let _ = reply.send(r);
                    }
                    Cmd::Candidate(c) => {
                        let t = c.trim().strip_prefix("a=").map(str::to_string).unwrap_or(c);
                        if let Ok(cand) = Candidate::from_sdp_string(&t) { rtc.add_remote_candidate(cand); }
                    }
                    Cmd::Send(wide, ticks, payload) => {
                        let pt = if wide { pt_g722 } else { pt_pcmu };
                        if let Some(m) = mid {
                            let mt = if wide { MediaTime::new(ticks * 2, Frequency::SIXTEEN_KHZ) } else { MediaTime::new(ticks, Frequency::EIGHT_KHZ) };
                            if let Some(Ok(())) = rtc.writer(m).map(|w| w.write(Pt::new_with_value(pt), Instant::now(), mt, payload)) {
                                tx_count.fetch_add(1, Ordering::Relaxed);
                            }
                        }
                    }
                    Cmd::Tone { wide, hz, ptime_ms, amp, seconds } => {
                        tone = Some(ToneJob { wide, hz, ptime_ms, amp, left: (seconds * 1000.0 / ptime_ms as f64) as u64,
                            phase: 0.0, next: tokio::time::Instant::now(), enc: G722Encoder::new() });
                    }
                    Cmd::Kill => { killed = true; tone = None; }
                    Cmd::Stall(on) => { killed = on; }
                    Cmd::Close => { let _ = rtc.close(); }
                }
            }
            r = sock.recv_from(&mut buf), if !killed => {
                if let Ok((n, src)) = r {
                    if let Ok(contents) = (&buf[..n]).try_into() {
                        let _ = rtc.handle_input(Input::Receive(Instant::now(), Receive { proto: Protocol::Udp, source: src, destination: local, contents }));
                    }
                }
            }
            _ = tokio::time::sleep_until(wake) => {
                let now = tokio::time::Instant::now();
                if let Some(t) = tone.as_mut() {
                    if now >= t.next {
                        // one packet of the tone
                        let per8 = (8 * t.ptime_ms) as usize; // samples at 8 kHz
                        let payload: Vec<u8> = if t.wide {
                            let n16 = per8 * 2;
                            let pcm: Vec<i16> = (0..n16).map(|_| { let v = (t.amp * t.phase.sin()) as i16; t.phase += 2.0 * std::f64::consts::PI * t.hz / 16000.0; v }).collect();
                            t.enc.encode(&pcm)
                        } else {
                            (0..per8).map(|_| { let v = (t.amp * t.phase.sin()) as i16; t.phase += 2.0 * std::f64::consts::PI * t.hz / 8000.0; encode_mu_law(v) }).collect()
                        };
                        if let Some(m) = mid {
                            let mt = if t.wide { MediaTime::new(ts * 2, Frequency::SIXTEEN_KHZ) } else { MediaTime::new(ts, Frequency::EIGHT_KHZ) };
                            let pt = if t.wide { pt_g722 } else { pt_pcmu };
                            if let Some(Ok(())) = rtc.writer(m).map(|w| w.write(Pt::new_with_value(pt), Instant::now(), mt, payload)) {
                                tx_count.fetch_add(1, Ordering::Relaxed);
                            }
                        }
                        ts += per8 as u64;
                        t.next += Duration::from_millis(t.ptime_ms);
                        t.left -= 1;
                        if t.left == 0 { tone = None; }
                    }
                }
                let _ = rtc.handle_input(Input::Timeout(Instant::now()));
            }
        }
    }
}
