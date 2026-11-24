//! The public side of a session: the `Session` handle, its callbacks, the shared state and the counters.

use super::*;

/// Counters the tests (and the log lines) read.
#[derive(Default, Debug)]
pub struct Stats {
    /// datagrams dropped because the session's input queue was full
    pub udp_dropped: AtomicU64,
    pub rtp_rx: AtomicU64,
    /// datagrams from an address `Rtc::accepts` refuses (not the nominated pair, not a remote
    /// candidate, not STUN of this session): dropped before any DTLS / SRTP work (R2-22)
    pub udp_refused: AtomicU64,
    /// `handle_input` errors (only the first one of a session is logged)
    pub input_errors: AtomicU64,
    pub frames_in: AtomicU64,
    pub frames_written: AtomicU64,
    pub write_errors: AtomicU64,
    /// frames the forwarding task dropped because `AudioOut` was full
    pub audio_out_dropped: AtomicU64,
    /// frames delivered to `AudioOut`
    pub audio_out_frames: AtomicU64,
    /// datagrams str0m asked to send that were not sent: the send failed or no local socket
    /// owns the source address
    pub udp_tx_dropped: AtomicU64,
    /// remote candidates handed to str0m
    pub remote_candidates: AtomicU64,
    /// outputs the `EndlessTransmit` fault served (tests only)
    pub injected_outputs: AtomicU64,
    /// the session task reached the end of its teardown (str0m's goodbye, the `closed` line)
    pub teardown: AtomicBool,
    /// lost slots concealed instead of decoded (`UplinkConverter`)
    pub plc_slots: AtomicU64,
    /// decoder restarts after a sequence resynchronisation
    pub decoder_resets: AtomicU64,
    /// the phone's RTP inter-arrival histogram (`report::RTP_GAP_BOUNDS_MS`, 8 bins) and its maximum
    pub gap_bins: [AtomicU64; 8],
    pub gap_max_ms: AtomicU64,
    /// the busiest `AudioOut` depth since the last windowed line was taken
    pub out_depth_window_max: AtomicU64,
    /// the session runtime's loop lag (`report::LAG_BOUNDS_MS`, 9 bins), call maximum, window maximum
    pub lag_bins: [AtomicU64; 9],
    pub lag_max_ms: AtomicU64,
    pub lag_window_max_ms: AtomicU64,
}

pub(super) type Callback = Box<dyn FnOnce() + Send>;

#[derive(Default)]
pub(super) struct CbState {
    pub(super) closed: bool,
    pub(super) connected_fired: bool,
    pub(super) on_connected: Vec<Callback>,
    pub(super) disconnected_fired: bool,
    pub(super) on_disconnected: Vec<Callback>,
}

/// What str0m negotiated for the audio section.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct Negotiated {
    pub pcmu_pt: Option<u8>,
    pub g722_pt: Option<u8>,
    /// The codec sent to the phone (`audioTrack.Bind`): `None` until the section is negotiated
    /// (PCMU is used meanwhile, as in Go).
    pub send: Option<PhoneCodec>,
}

pub(super) struct Shared {
    pub(super) call_id: String,
    pub(super) cb: Mutex<CbState>,
    pub(super) connected: AtomicBool,
    /// an offer was accepted AND answerable (set at the end of `Core::offer`)
    pub(super) offered: AtomicBool,
    pub(super) neg: Mutex<Negotiated>,
    pub(super) stats: Stats,
    /// cancelled when the session is over (the run task ended)
    pub(super) done: CancelToken,
}

impl Shared {
    pub(super) fn fire_connected(&self) {
        let cbs = {
            let mut g = lock(&self.cb);
            if g.connected_fired {
                return;
            }
            g.connected_fired = true;
            std::mem::take(&mut g.on_connected)
        };
        self.run_callbacks("connected", cbs);
    }

    /// Runs every callback, each under its own `catch_unwind`: one that panics is logged and the
    /// rest still run. The ones registered after the first are exactly the ones that hang the
    /// device up (PARITY 81, R1-19).
    pub(super) fn run_callbacks(&self, what: &str, cbs: Vec<Callback>) {
        for cb in cbs {
            if let Err(p) = catch_unwind(AssertUnwindSafe(cb)) {
                crate::panic_hook::out_line(&format!(
                    "portico-transport: sfu call={} an {what} callback panicked: {}",
                    self.call_id,
                    crate::util::panic_message(&*p, "")
                ));
            }
        }
    }

    pub(super) fn fire_disconnected(&self) {
        let cbs = {
            let mut g = lock(&self.cb);
            if g.disconnected_fired {
                return;
            }
            g.disconnected_fired = true;
            // callbacks registered for a connection that never came are dropped with the session
            g.on_connected.clear();
            std::mem::take(&mut g.on_disconnected)
        };
        self.connected.store(false, Ordering::SeqCst);
        self.run_callbacks("disconnected", cbs);
    }
}

pub(super) enum Cmd {
    Offer(String, oneshot::Sender<Result<String, String>>),
    Answer(String, oneshot::Sender<Result<(), String>>),
    Candidate(String, oneshot::Sender<Result<(), String>>),
}

/// One browser-facing WebRTC call leg.
pub struct Session {
    pub call_id: String,
    pub(super) shared: Arc<Shared>,
    pub(super) cmd_tx: mpsc::Sender<Cmd>,
    pub(super) audio_in: mpsc::Sender<Vec<u8>>,
    pub(super) audio_out: Arc<tokio::sync::Mutex<mpsc::Receiver<Vec<u8>>>>,
    pub(super) jitter: UplinkBuffer,
    pub(super) gate: Arc<UplinkGate>,
    pub(super) stop: CancelToken,
}

pub(super) fn ensure_crypto() {
    static ONCE: Once = Once::new();
    ONCE.call_once(|| {
        str0m::crypto::from_feature_flags().install_process_default();
    });
}

/// G.722 AND PCMU only, never Opus (see the long comment in Go's `newSession`: with Opus in the
/// answer the phone sends Opus that the hub decodes as mu-law). `use_red` is off, comfort noise
/// and telephone-event are not enabled, `clear_codecs` removes the defaults.
pub(super) fn build_rtc(cfg: &SessionConfig, now: Instant) -> Rtc {
    let mut rc = RtcConfig::new()
        .clear_codecs()
        .enable_g722(true, false)
        .enable_pcmu(true, false)
        .enable_raw_packets(true);
    if let Some(n) = cfg.max_stun_retransmits {
        rc.set_max_stun_retransmits(n);
    }
    rc.build(now)
}

impl Session {
    /// `NewSession`. Must run inside a tokio runtime (it spawns the session tasks).
    pub fn new(call_id: &str) -> Result<Arc<Session>, String> {
        Self::with_config(call_id, SessionConfig::default())
    }

    /// `NewSessionWithSettings`: the seam the tests use to pin the interfaces.
    pub fn with_config(call_id: &str, cfg: SessionConfig) -> Result<Arc<Session>, String> {
        let rt = tokio::runtime::Handle::try_current()
            .map_err(|e| format!("sfu: creating peer connection: {e}"))?;
        ensure_crypto();
        let (cmd_tx, cmd_rx) = mpsc::channel(16);
        let (audio_in, audio_in_rx) = mpsc::channel::<Vec<u8>>(8);
        let (out_tx, out_rx) = mpsc::channel::<Vec<u8>>(8);
        let jitter = UplinkBuffer::new();
        let path = Arc::new(UplinkPath::new(jitter.clone()));
        let gate = path.gate();
        let shared = Arc::new(Shared {
            call_id: call_id.to_string(),
            cb: Mutex::new(CbState::default()),
            connected: AtomicBool::new(false),
            offered: AtomicBool::new(false),
            neg: Mutex::new(Negotiated::default()),
            stats: Stats::default(),
            done: CancelToken::new(),
        });
        let stop = CancelToken::new();
        let s = Arc::new(Session {
            call_id: call_id.to_string(),
            shared: shared.clone(),
            cmd_tx,
            audio_in,
            audio_out: Arc::new(tokio::sync::Mutex::new(out_rx)),
            jitter: jitter.clone(),
            gate: gate.clone(),
            stop: stop.clone(),
        });
        // forwardJitterOut
        rt.spawn(forward_jitter_out(
            jitter.clone(),
            gate.clone(),
            out_tx,
            stop.clone(),
            shared.clone(),
        ));
        let core = Core::new(shared.clone(), cfg, path, stop.clone());
        let (udp_tx, udp_rx) = mpsc::channel::<UdpEv>(512);
        let task = rt.spawn(run(core, cmd_rx, udp_rx, udp_tx, audio_in_rx));
        // The supervisor: a panic that escaped the guarded calls ends this session only.
        let (sh, st, jb) = (shared.clone(), stop.clone(), jitter);
        rt.spawn(async move {
            if let Err(e) = task.await {
                if e.is_panic() {
                    crate::panic_hook::out_line(&format!(
                        "portico-transport: sfu call={} session task panicked; the session is closed",
                        sh.call_id
                    ));
                }
            }
            st.cancel();
            jb.close();
            {
                let mut g = lock(&sh.cb);
                g.closed = true;
            }
            sh.fire_disconnected();
            sh.done.cancel();
        });
        Ok(s)
    }

    /// `HandleOffer`: the final (non-trickle) answer SDP.
    pub async fn handle_offer(&self, offer_sdp: &str) -> Result<String, String> {
        let (tx, rx) = oneshot::channel();
        self.cmd_tx
            .send(Cmd::Offer(offer_sdp.to_string(), tx))
            .await
            .map_err(|_| "sfu: setting remote offer: session is closed".to_string())?;
        rx.await
            .unwrap_or_else(|_| Err("sfu: setting remote offer: session is closed".to_string()))
    }

    /// `HandleAnswer`: this port never creates an offer, so any answer is refused the way pion
    /// refuses one in the stable state.
    pub async fn handle_answer(&self, answer_sdp: &str) -> Result<(), String> {
        let (tx, rx) = oneshot::channel();
        self.cmd_tx
            .send(Cmd::Answer(answer_sdp.to_string(), tx))
            .await
            .map_err(|_| "sfu: setting remote answer: session is closed".to_string())?;
        rx.await
            .unwrap_or_else(|_| Err("sfu: setting remote answer: session is closed".to_string()))
    }

    /// `AddICECandidate`.
    pub async fn add_ice_candidate(&self, candidate: &str) -> Result<(), String> {
        let (tx, rx) = oneshot::channel();
        self.cmd_tx
            .send(Cmd::Candidate(candidate.to_string(), tx))
            .await
            .map_err(|_| "sfu: adding ICE candidate: session is closed".to_string())?;
        rx.await
            .unwrap_or_else(|_| Err("sfu: adding ICE candidate: session is closed".to_string()))
    }

    /// Whether an offer was fully accepted (answered with a usable codec).
    pub fn had_offer(&self) -> bool {
        self.shared.offered.load(Ordering::SeqCst)
    }

    /// `Connected()`.
    pub fn connected(&self) -> bool {
        self.shared.connected.load(Ordering::SeqCst)
    }

    /// `OnConnected`: `cb` runs exactly once when the ICE/DTLS handshake completes, at once if
    /// the session is already connected. Runs on the session's task (or the caller's, when
    /// late): keep it non-blocking, spawn what takes time.
    pub fn on_connected(&self, cb: impl FnOnce() + Send + 'static) {
        let mut g = lock(&self.shared.cb);
        if g.connected_fired {
            drop(g);
            cb();
            return;
        }
        if g.disconnected_fired {
            // the session is gone and never connected: the callback can never run. Keeping it
            // would keep whatever it captured (the leg itself, in every bridge) alive for ever
            // (R1-24)
            return;
        }
        g.on_connected.push(Box::new(cb));
    }

    /// `OnDisconnected`: `cb` runs exactly once when the session is gone for good (the grace
    /// after `Disconnected` elapsed, the connect timeout, the peer closed, `close`), at once if
    /// it already is. A transient `Disconnected` that recovers does not count.
    pub fn on_disconnected(&self, cb: impl FnOnce() + Send + 'static) {
        let mut g = lock(&self.shared.cb);
        if g.disconnected_fired {
            drop(g);
            cb();
            return;
        }
        g.on_disconnected.push(Box::new(cb));
    }

    /// `AudioIn` (the producer side): L16 frames from the device, to be sent to the phone.
    pub fn audio_in(&self) -> mpsc::Sender<Vec<u8>> {
        self.audio_in.clone()
    }

    /// `AudioOut`: 16 kHz L16 frames from the phone.
    pub fn audio_out(&self) -> Arc<tokio::sync::Mutex<mpsc::Receiver<Vec<u8>>>> {
        self.audio_out.clone()
    }

    /// `Session.jitter`: the test seam of `TestBrowserLegJitterBuffer_ReordersAndFillsGaps`.
    pub fn jitter(&self) -> &UplinkBuffer {
        &self.jitter
    }

    /// What str0m negotiated (empty until the offer was accepted).
    pub fn negotiated(&self) -> Negotiated {
        *lock(&self.shared.neg)
    }

    /// The phone's codec as seen on the first packet.
    pub fn uplink_codec(&self) -> Option<PhoneCodec> {
        self.gate.codec()
    }

    pub fn stats(&self) -> &Stats {
        &self.shared.stats
    }

    /// Whether the session is over (its task ended).
    pub fn is_done(&self) -> bool {
        self.shared.done.is_cancelled()
    }

    /// Resolves when the session is over and its sockets are released.
    pub async fn wait_done(&self) {
        self.shared.done.cancelled().await
    }

    /// `Close`: idempotent, fires `OnDisconnected` once. The task sends the DTLS close_notify and
    /// the RTCP BYE (str0m's graceful close) and ends; `wait_done` waits for that.
    pub fn close(&self) {
        {
            let mut g = lock(&self.shared.cb);
            if g.closed {
                return;
            }
            g.closed = true;
        }
        self.stop.cancel();
        self.jitter.close();
        self.shared.fire_disconnected();
    }
}

impl Drop for Session {
    fn drop(&mut self) {
        self.close();
    }
}

impl crate::call::ClientLeg for Session {
    fn on_connected(&self, cb: Box<dyn FnOnce() + Send>) {
        Session::on_connected(self, cb);
    }
    fn on_disconnected(&self, cb: Box<dyn FnOnce() + Send>) {
        Session::on_disconnected(self, cb);
    }
    fn input(&self) -> mpsc::Sender<Vec<u8>> {
        self.audio_in()
    }
    fn output(&self) -> Arc<tokio::sync::Mutex<mpsc::Receiver<Vec<u8>>>> {
        self.audio_out()
    }
}

/// `forwardJitterOut`: the jitter buffer's ordered, paced payloads to L16 frames on `AudioOut`;
/// a full `AudioOut` drops the new frame.
pub(super) async fn forward_jitter_out(
    jitter: UplinkBuffer,
    gate: Arc<UplinkGate>,
    out: mpsc::Sender<Vec<u8>>,
    stop: CancelToken,
    shared: Arc<Shared>,
) {
    let mut conv = UplinkConverter::new(gate);
    let mut resyncs = 0;
    loop {
        tokio::select! {
            biased;
            _ = stop.cancelled() => return,
            p = jitter.recv_slot() => match p {
                None => return,
                Some(slot) => {
                    // a resynchronised sequence is a restarted sender: its encoder state is new
                    let r = jitter.jitter_stats().resyncs;
                    if r != resyncs {
                        resyncs = r;
                        conv.reset_decoder();
                    }
                    let frame = conv.convert_slot(&slot.data, slot.lost);
                    shared.stats.plc_slots.store(conv.concealed(), Ordering::Relaxed);
                    shared.stats.decoder_resets.store(conv.decoder_resets(), Ordering::Relaxed);
                    match out.try_send(frame) {
                        Ok(()) => {
                            shared.stats.audio_out_frames.fetch_add(1, Ordering::Relaxed);
                            let depth = (out.max_capacity() - out.capacity()) as u64;
                            shared.stats.out_depth_window_max.fetch_max(depth, Ordering::Relaxed);
                        }
                        Err(_) => {
                            shared.stats.audio_out_dropped.fetch_add(1, Ordering::Relaxed);
                        }
                    }
                }
            }
        }
    }
}
