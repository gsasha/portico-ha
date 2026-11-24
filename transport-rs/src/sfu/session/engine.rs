//! The str0m core of one session: the `Rtc`, the sockets, the candidate handling, the panic guard and the media in both directions.

use super::*;

pub(super) struct UdpEv {
    pub(super) idx: usize,
    pub(super) src: SocketAddr,
    pub(super) data: Vec<u8>,
}

/// Why `run` ended.
#[derive(Debug)]
pub(super) enum End {
    Stopped,
    PeerClosed,
    Grace,
    ConnectTimeout,
    Panic,
}

pub(super) struct Core {
    pub(super) shared: Arc<Shared>,
    pub(super) cfg: SessionConfig,
    pub(super) rtc: Rtc,
    pub(super) socks: Vec<Arc<UdpSocket>>,
    pub(super) locals: Vec<SocketAddr>,
    pub(super) path: Arc<UplinkPath>,
    pub(super) stop: CancelToken,
    pub(super) have_offer: bool,
    /// a step panicked (the one guard, `Core::guard`): the `Rtc` may be inconsistent
    pub(super) panicked: bool,
    /// the connection completed at least once (`Event::Connected`); `shared.connected` is
    /// cleared while ICE is `Disconnected`, this is not
    pub(super) ever_connected: bool,
    /// remote candidates accepted so far (`MAX_REMOTE_CANDIDATES`)
    pub(super) remote_cands: usize,
    /// outputs left for the `EndlessTransmit` fault
    pub(super) injected_left: u64,
    /// the pass of `drain` stopped at `MAX_OUTPUTS_PER_PASS`: the loop yields before the next
    pub(super) capped: bool,
    pub(super) readers: Vec<JoinHandle<()>>,
    /// the first audio section's mid, where the device audio is written
    pub(super) mid: Option<Mid>,
    pub(super) answer_pts: Vec<u8>,
    pub(super) send: Option<(Pt, PhoneCodec)>,
    pub(super) enc: DownlinkEncoder,
    pub(super) rtp: RtpSendState,
    pub(super) ts_ext: u64,
    pub(super) last_ts: u32,
    pub(super) disc_deadline: Option<Instant>,
    pub(super) connect_deadline: Instant,
    pub(super) first_audio: bool,
    pub(super) first_rtp: bool,
    pub(super) logged_write_err: bool,
    /// the first `handle_input` error was logged (the rest are counted, R2-22)
    pub(super) logged_input_err: bool,
    /// sources a STUN message of THIS session came from (one that passed `Rtc::accepts`, i.e. its
    /// integrity check): the peer's DTLS and RTP
    /// follow its first check from that address, before our own check has made the pair viable
    /// (`Rtc::accepts` alone would drop that first ClientHello and the call would wait a DTLS
    /// retransmit, about a second). At most `MAX_STUN_SOURCES`.
    pub(super) stun_srcs: Vec<SocketAddr>,
    pub(super) t0: Instant,
    /// when the previous RTP packet from the phone arrived (the arrival-gap histogram)
    pub(super) last_rtp: Option<Instant>,
}

const MAX_STUN_SOURCES: usize = 16;

/// Which kind of network an address is on, for a log line (DEV-764): never the address itself.
/// `loopback`, `lan` (private, link-local, unique-local), `tailnet` (100.64.0.0/10, fd7a:115c:a1e0::/48), else `other`.
pub(super) fn addr_class(ip: std::net::IpAddr) -> &'static str {
    use std::net::IpAddr;
    match ip {
        IpAddr::V4(v4) => {
            let o = v4.octets();
            if v4.is_loopback() {
                "loopback"
            } else if o[0] == 100 && (o[1] & 0xC0) == 64 {
                "tailnet"
            } else if v4.is_private() || v4.is_link_local() {
                "lan"
            } else {
                "other"
            }
        }
        IpAddr::V6(v6) => {
            let s = v6.segments();
            if v6.is_loopback() {
                "loopback"
            } else if s[0] == 0xfd7a && s[1] == 0x115c && s[2] == 0xa1e0 {
                "tailnet"
            } else if (s[0] & 0xfe00) == 0xfc00 || (s[0] & 0xffc0) == 0xfe80 {
                "lan"
            } else {
                "other"
            }
        }
    }
}

/// A STUN message (RFC 5389: the two top bits of the first byte are zero, the magic cookie at
/// bytes 4 to 8).
fn looks_like_stun(d: &[u8]) -> bool {
    d.len() >= 20 && d[0] & 0xC0 == 0 && d[4..8] == [0x21, 0x12, 0xA4, 0x42]
}

impl Core {
    pub(super) fn new(
        shared: Arc<Shared>,
        cfg: SessionConfig,
        path: Arc<UplinkPath>,
        stop: CancelToken,
    ) -> Core {
        let now = Instant::now();
        let rtc = build_rtc(&cfg, now);
        let connect_deadline = now + cfg.connect_timeout;
        Core {
            shared,
            cfg,
            rtc,
            socks: Vec::new(),
            locals: Vec::new(),
            path,
            stop,
            have_offer: false,
            panicked: false,
            ever_connected: false,
            remote_cands: 0,
            injected_left: 1_000_000,
            capped: false,
            readers: Vec::new(),
            mid: None,
            answer_pts: Vec::new(),
            send: None,
            enc: DownlinkEncoder::new(),
            // str0m assigns the SSRC and the sequence numbers (PARITY 75): only the timestamp
            // counter of this is used, and nothing here may touch the OS random source
            rtp: RtpSendState::with_ssrc(0),
            ts_ext: 0,
            last_ts: 0,
            disc_deadline: None,
            connect_deadline,
            first_audio: true,
            first_rtp: true,
            logged_write_err: false,
            logged_input_err: false,
            stun_srcs: Vec::new(),
            t0: now,
            last_rtp: None,
        }
    }

    pub(super) fn log(&self, msg: &str) {
        crate::panic_hook::out_line(&format!(
            "portico-transport: sfu call={} {msg}",
            self.shared.call_id
        ));
    }

    /// Milliseconds since this session was created (the first line of a call's session in the log).
    pub(super) fn since_start_ms(&self) -> u128 {
        self.t0.elapsed().as_millis()
    }

    pub(super) fn peer_state(&mut self, s: &str) {
        self.log(&format!("peer connection {s}"));
    }

    /// Binds a socket per allowed local address and adds the host candidates, once.
    pub(super) async fn bind(&mut self, udp_tx: &mpsc::Sender<UdpEv>) -> Result<(), End> {
        let all = ifaces::enumerate();
        let mut want = self.cfg.ifaces.select(&all);
        if let Some(only) = &self.cfg.ifaces.only {
            // the seam may name an address that is not on an interface (it is still bound)
            for ip in only {
                if !want.iter().any(|(_, a)| a == ip) {
                    want.push(("seam".to_string(), *ip));
                }
            }
        }
        for (name, ip) in want {
            match UdpSocket::bind(SocketAddr::new(ip, 0)).await {
                Ok(s) => {
                    let Ok(local) = s.local_addr() else { continue };
                    let inject = self.cfg.fault_is(Fault::PanicInBind);
                    let added = self.guard("adding a host candidate", |c| {
                        if inject {
                            panic!("injected panic while adding a host candidate");
                        }
                        Candidate::host(local, "udp").map(|cand| {
                            c.rtc.add_local_candidate(cand);
                        })
                    })?;
                    if let Err(e) = added {
                        self.log(&format!("host candidate {local} on {name} refused: {e}"));
                        continue;
                    }
                    let s = Arc::new(s);
                    let idx = self.socks.len();
                    self.socks.push(s.clone());
                    self.locals.push(local);
                    let reader = tokio::spawn(
                        Reader {
                            sock: s,
                            idx,
                            tx: udp_tx.clone(),
                            stop: self.stop.clone(),
                            shared: self.shared.clone(),
                            #[cfg(feature = "test-seams")]
                            inject: self.cfg.fault_is(Fault::RecvError),
                            backoff_max: self.cfg.recv_backoff_max,
                            call: self.shared.call_id.clone(),
                        }
                        .run(),
                    );
                    self.readers.push(reader);
                }
                Err(e) => self.log(&format!("binding {ip} on {name}: {e}")),
            }
        }
        Ok(())
    }

    /// THE panic guard: every call into str0m, and every handler that runs on its output, goes
    /// through here. A panic is logged with its message, recorded (`panicked`: the `Rtc` may be
    /// inconsistent, so the loop does not poll it again), ends the session (the stop token) and
    /// comes back as `End::Panic`.
    pub(super) fn guard<T>(
        &mut self,
        what: &str,
        f: impl FnOnce(&mut Self) -> T,
    ) -> Result<T, End> {
        match catch_unwind(AssertUnwindSafe(|| f(self))) {
            Ok(v) => Ok(v),
            Err(p) => {
                self.log(&format!(
                    "{what} panicked: {}",
                    crate::util::panic_message(&*p, "")
                ));
                self.panicked = true;
                self.stop.cancel();
                Err(End::Panic)
            }
        }
    }

    /// `guard` for a step that itself returns `End`.
    pub(super) fn step<T>(
        &mut self,
        what: &str,
        f: impl FnOnce(&mut Self) -> Result<T, End>,
    ) -> Result<T, End> {
        self.guard(what, f)?
    }

    /// `HandleOffer`. Validates and parses first; sockets are bound only for an offer that parsed.
    pub(super) async fn offer(
        &mut self,
        sdp: String,
        udp_tx: &mpsc::Sender<UdpEv>,
    ) -> Result<String, String> {
        const P: &str = "sfu: setting remote offer: ";
        const NO_CODEC: &str = "sfu: creating answer: unable to populate media section, RTPSender created with no codecs";
        let internal = || format!("{P}internal error");
        let pre = self
            .guard(
                "parsing the offer",
                |_| -> Result<(SdpOffer, Vec<u8>), String> {
                    if let Some(e) = sdp::pion_first_line_error(&sdp) {
                        return Err(format!("{P}failed to unmarshal SDP: {e}"));
                    }
                    sdp::check_offer_size(&sdp).map_err(|e| format!("{P}{e}"))?;
                    // pion refuses a format that is no number 0 to 255 (str0m may not)
                    let order =
                        sdp::try_audio_payload_types(&sdp).map_err(|e| format!("{P}{e}"))?;
                    let text = sdp::normalise_rtpmap(&sdp);
                    let offer = SdpOffer::from_sdp_string(&text)
                        .map_err(|e| format!("{P}{}", sdp::stable_error(&e.to_string())))?;
                    Ok((offer, order))
                },
            )
            .map_err(|_| internal())?;
        let (offer, order) = pre?;
        if self.socks.is_empty() {
            self.bind(udp_tx).await.map_err(|_| internal())?;
            if self.socks.is_empty() {
                return Err("sfu: creating peer connection: no usable local address".to_string());
            }
        }
        let inject = self.cfg.fault_is(Fault::PanicInAcceptOffer);
        let ans = self
            .guard("accept_offer", |c| -> Result<String, String> {
                if inject {
                    panic!("injected panic inside accept_offer");
                }
                let ans = c
                    .rtc
                    .sdp_api()
                    .accept_offer(offer)
                    .map_err(|e| format!("{P}{e}"))?;
                Ok(ans.to_sdp_string())
            })
            .map_err(|_| internal())??;
        self.have_offer = true;
        #[cfg(feature = "test-seams")]
        let inject = self.cfg.fault_is(Fault::PanicInAnswerPost);
        let post = self
            .guard("the answer post-processing", |_| {
                #[cfg(feature = "test-seams")]
                if inject {
                    panic!("injected panic in the answer post-processing");
                }
                // the answer names the offerer's preference first (G.722 if it lists G.722 first,
                // PCMU if it lists PCMU first), as pion's does; str0m's own order is G.722 first
                let answer =
                    sdp::order_audio_pts(&sdp::with_end_of_candidates(&ans), &order);
                let pts = sdp::audio_payload_types(&answer);
                // the pion model is the cross-check, not the authority
                let note = policy::answer_codecs(&sdp).ok().and_then(|model| {
                    let model_pts: Vec<u8> = model.iter().map(|c| c.pt).collect();
                    (!model_pts.is_empty() && model_pts != pts).then(|| {
                        format!("str0m answered payload types {pts:?}, the pion model says {model_pts:?}")
                    })
                });
                (answer, pts, note)
            })
            .map_err(|_| internal())?;
        let (answer, pts, note) = post;
        if let Some(n) = note {
            self.log(&n);
        }
        self.answer_pts = pts;
        if sdp::audio_rejected(&answer) || self.answer_pts.is_empty() {
            return Err(NO_CODEC.to_string());
        }
        self.negotiated().map_err(|_| internal())?;
        self.shared.offered.store(true, Ordering::SeqCst);
        Ok(answer)
    }

    /// The negotiated payload types, read off str0m's own table (`codec_config`, which accept_offer
    /// has just rewritten to the offerer's payload types) for the types the answer names: the
    /// uplink gate is built from them and the send codec is the first G.722/PCMU entry of the
    /// answer's `m=audio` line.
    pub(super) fn negotiated(&mut self) -> Result<(), End> {
        let inject = self.cfg.fault_is(Fault::PanicInNegotiated);
        let params: Vec<(Pt, Codec)> = self.guard("reading the negotiated codecs", |c| {
            if inject {
                panic!("injected panic in negotiated()");
            }
            c.rtc
                .codec_config()
                .params()
                .iter()
                .map(|p| (p.pt(), p.spec().codec))
                .collect()
        })?;
        let named: Vec<(Pt, Codec)> = self
            .answer_pts
            .iter()
            .filter_map(|n| params.iter().find(|(p, _)| **p == *n).copied())
            .collect();
        let find = |c: Codec| named.iter().find(|(_, k)| *k == c).map(|(p, _)| *p);
        let (pcmu, g722) = (find(Codec::PCMU), find(Codec::G722));
        self.path
            .gate()
            .set_negotiated(pcmu.map(|p| *p), g722.map(|p| *p));
        let send = named.iter().find_map(|(p, c)| match c {
            Codec::G722 => Some((*p, PhoneCodec::G722)),
            Codec::PCMU => Some((*p, PhoneCodec::Pcmu)),
            _ => None,
        });
        self.send = send;
        *lock(&self.shared.neg) = Negotiated {
            pcmu_pt: pcmu.map(|p| *p),
            g722_pt: g722.map(|p| *p),
            send: send.map(|s| s.1),
        };
        Ok(())
    }

    /// `AddICECandidate`.
    pub(super) fn candidate(&mut self, c: &str) -> Result<(), String> {
        match self.guard("adding a candidate", |s| s.candidate_inner(c)) {
            Ok(r) => r,
            Err(_) => Err("sfu: adding ICE candidate: internal error".to_string()),
        }
    }

    pub(super) fn candidate_inner(&mut self, c: &str) -> Result<(), String> {
        const P: &str = "sfu: adding ICE candidate: ";
        if !self.have_offer {
            return Err(format!(
                "{P}InvalidStateError: remote description is not set"
            ));
        }
        if self.cfg.fault_is(Fault::PanicInCandidate) {
            panic!("injected panic in candidate()");
        }
        let t = c.trim();
        // the end-of-candidates marker is the WHOLE string (R1-82): a candidate whose extension
        // text happens to contain the words is a candidate. `a=` is taken in front of the marker
        // only (PARITY 322); pion trims `candidate:` and nothing else, so `a=candidate:..` is its
        // foundation token with an `=` in it and is refused (R2-67, checked in pion ice v4.0.13)
        if t.is_empty() || t == "end-of-candidates" || t == "a=end-of-candidates" {
            return Ok(());
        }
        if t.starts_with("a=") {
            return Err(format!(
                "{P}failed to parse foundation: invalid ice-char token: = in {t}"
            ));
        }
        // pion trims the `candidate:` prefix itself and takes both forms
        let t = if t.starts_with("candidate:") {
            std::borrow::Cow::Borrowed(t)
        } else {
            std::borrow::Cow::Owned(format!("candidate:{t}"))
        };
        let t = &*t;
        // an mDNS (.local) candidate cannot be resolved here; the peer-reflexive candidate of the
        // browser's own checks connects the pair (S1b), so it is accepted and ignored
        if t.split_whitespace()
            .nth(4)
            .is_some_and(|a| a.to_ascii_lowercase().ends_with(".local"))
        {
            return Ok(());
        }
        let cand = Candidate::from_sdp_string(t).map_err(|e| format!("{P}{e}"))?;
        // Where the transport will send STUN checks is chosen by the remote side: never to an
        // address no browser can have (R1-39). Ignored like an mDNS one, with a log line.
        let a = cand.addr();
        let loopback_ok = self.cfg.ifaces.include_loopback;
        if a.port() == 0
            || a.ip().is_unspecified()
            || a.ip().is_multicast()
            || (a.ip().is_loopback() && !loopback_ok)
        {
            self.log(&format!(
                "remote candidate {a} ignored (not an address a browser has)"
            ));
            return Ok(());
        }
        if self.remote_cands >= MAX_REMOTE_CANDIDATES {
            return Err(format!(
                "{P}too many remote candidates ({MAX_REMOTE_CANDIDATES} is the limit)"
            ));
        }
        self.remote_cands += 1;
        self.log(&format!(
            "remote candidate #{} accepted: class={} +{}ms",
            self.remote_cands,
            addr_class(a.ip()),
            self.since_start_ms()
        ));
        self.shared
            .stats
            .remote_candidates
            .fetch_add(1, Ordering::Relaxed);
        self.rtc.add_remote_candidate(cand);
        Ok(())
    }

    /// One `Transmit` of str0m onto the socket that owns its source address; what cannot be sent
    /// is counted (R1-127).
    pub(super) fn send_transmit(&self, t: &Transmit) {
        let sent = self
            .locals
            .iter()
            .position(|a| *a == t.source)
            .is_some_and(|i| {
                self.socks[i]
                    .try_send_to(&t.contents, t.destination)
                    .is_ok()
            });
        if !sent {
            self.shared
                .stats
                .udp_tx_dropped
                .fetch_add(1, Ordering::Relaxed);
        }
    }

    /// `poll_output`, or what the `EndlessTransmit` fault serves instead.
    pub(super) fn poll_out(&mut self) -> Result<Output, RtcError> {
        if self.cfg.fault_is(Fault::EndlessTransmit) && self.have_offer && self.injected_left > 0 {
            self.injected_left -= 1;
            self.shared
                .stats
                .injected_outputs
                .fetch_add(1, Ordering::Relaxed);
            return Ok(Output::Transmit(Transmit {
                proto: Protocol::Udp,
                source: "192.168.2.1:1".parse().expect("literal"),
                destination: "192.168.2.2:2".parse().expect("literal"),
                contents: vec![0u8].into(),
            }));
        }
        self.rtc.poll_output()
    }

    /// Everything str0m wants to send or tell us, until it asks for a timeout or
    /// `MAX_OUTPUTS_PER_PASS` outputs were taken (then `capped` is set and the loop yields).
    pub(super) fn drain(&mut self) -> Result<Instant, End> {
        self.capped = false;
        for _ in 0..MAX_OUTPUTS_PER_PASS {
            let inject = self.have_offer && self.cfg.fault_is(Fault::PanicInPoll);
            let out = self.guard("poll_output", |c| {
                if inject {
                    panic!("injected panic inside poll_output");
                }
                c.poll_out()
            })?;
            match out {
                Ok(Output::Timeout(t)) => return Ok(t),
                Ok(Output::Transmit(t)) => self.send_transmit(&t),
                // the handlers (the uplink buffer and its locks included) are under the same guard
                Ok(Output::Event(e)) => self.step("an event handler", |c| c.event(e))?,
                Err(e) => {
                    self.log(&format!("poll_output: {e}"));
                    return Err(End::PeerClosed);
                }
            }
        }
        self.capped = true;
        Ok(Instant::now())
    }

    pub(super) fn event(&mut self, e: Event) -> Result<(), End> {
        match e {
            Event::Connected => {
                self.disc_deadline = None;
                self.ever_connected = true;
                self.shared.connected.store(true, Ordering::SeqCst);
                self.peer_state("connected");
                self.log(&format!(
                    "connected +{}ms after the session was created ({} remote candidates)",
                    self.since_start_ms(),
                    self.remote_cands
                ));
                self.shared.fire_connected();
            }
            Event::IceConnectionStateChange(s) => {
                self.log(&format!("ICE state {s:?} +{}ms", self.since_start_ms()));
                match s {
                IceConnectionState::Disconnected => {
                    if self.disc_deadline.is_none() {
                        self.disc_deadline = Some(Instant::now() + self.cfg.disconnect_grace);
                    }
                    // Go's `Connected()` is false while the connection is disconnected
                    self.shared.connected.store(false, Ordering::SeqCst);
                    self.peer_state("disconnected");
                }
                // only a return to a working pair is a recovery; `Checking` (a late candidate
                // for a dead peer forms new pairs) is not and must not restart the clock (R1-22)
                IceConnectionState::Connected | IceConnectionState::Completed => {
                    if self.disc_deadline.take().is_some() {
                        self.log(&format!("ICE recovered ({s:?}) within the grace"));
                    }
                    if self.ever_connected {
                        self.shared.connected.store(true, Ordering::SeqCst);
                    }
                }
                _ => {}
                }
            }
            // the device audio goes to the FIRST audio section; another section (video, a second
            // audio line) is not ours to write to (R1-18)
            Event::MediaAdded(m) => {
                if m.kind == MediaKind::Audio && self.mid.is_none() {
                    self.mid = Some(m.mid);
                }
            }
            Event::RawPacket(p) => {
                if let RawPacket::RtpRx(h, payload) = *p {
                    self.shared.stats.rtp_rx.fetch_add(1, Ordering::Relaxed);
                    let now = Instant::now();
                    if let Some(prev) = self.last_rtp.replace(now) {
                        let ms = now.saturating_duration_since(prev).as_millis() as u64;
                        let st = &self.shared.stats;
                        st.gap_bins[report::bin(&report::RTP_GAP_BOUNDS_MS, ms)]
                            .fetch_add(1, Ordering::Relaxed);
                        st.gap_max_ms.fetch_max(ms, Ordering::Relaxed);
                    }
                    let pt = *h.payload_type;
                    if self.cfg.trace_rtp {
                        crate::panic_hook::out_line(&format!(
                            "portico-transport: sfu call={} rtp t={}ms seq={} ts={} ssrc={} pt={} len={}",
                            self.shared.call_id,
                            self.t0.elapsed().as_millis(),
                            h.sequence_number,
                            h.timestamp,
                            *h.ssrc,
                            pt,
                            payload.len()
                        ));
                    }
                    if self.first_rtp {
                        self.first_rtp = false;
                        self.log(&format!(
                            "first RTP from the phone: pt={} {} bytes",
                            pt,
                            payload.len()
                        ));
                        let (ptime, delay) = super::super::uplink::uplink_timing(payload.len());
                        self.log(&report::settings_line(ptime, delay));
                    }
                    if self.cfg.fault_is(Fault::PanicInRtp) {
                        panic!("injected panic while handling an RTP packet");
                    }
                    self.path.on_rtp(pt, h.sequence_number, payload);
                }
            }
            _ => {}
        }
        Ok(())
    }

    pub(super) fn input(&mut self, i: Input) -> Result<(), End> {
        match self.guard("handle_input", |c| c.rtc.handle_input(i))? {
            Ok(()) => Ok(()),
            Err(e) => {
                self.log(&format!("handle_input: {e}"));
                Ok(())
            }
        }
    }

    /// One received datagram: the parse of the raw bytes (str0m's STUN / DTLS / RTP
    /// demultiplexer) and the input are one guarded step.
    pub(super) fn datagram(&mut self, ev: UdpEv) -> Result<(), End> {
        let dest = self.locals[ev.idx];
        let now = Instant::now();
        let inject = self.have_offer && self.cfg.fault_is(Fault::PanicInDatagram);
        let r = self.guard("a datagram", |c| {
            if inject {
                panic!("injected panic while handling a datagram");
            }
            // a datagram str0m cannot parse is dropped
            let Ok(contents) = (&ev.data[..]).try_into() else {
                return Ok(true);
            };
            let input = Input::Receive(
                now,
                Receive {
                    proto: Protocol::Udp,
                    source: ev.src,
                    destination: dest,
                    contents,
                },
            );
            // R2-22: str0m leaves the source check to the caller. pion drops non-STUN traffic from
            // an address that is not a remote candidate; so does this, before any DTLS/SRTP work
            let stun = looks_like_stun(&ev.data);
            let mut ok = c.rtc.accepts(&input);
            if ok && stun && !c.stun_srcs.contains(&ev.src) && c.stun_srcs.len() < MAX_STUN_SOURCES
            {
                c.stun_srcs.push(ev.src);
                // once per new source, at most MAX_STUN_SOURCES per session: never per packet
                c.log(&format!(
                    "first STUN check from a {} address (source #{}) on a {} local address +{}ms",
                    addr_class(ev.src.ip()),
                    c.stun_srcs.len(),
                    addr_class(dest.ip()),
                    c.since_start_ms()
                ));
            }
            if !ok && !stun && c.stun_srcs.contains(&ev.src) {
                ok = true;
            }
            if !ok {
                return Ok(false);
            }
            c.rtc.handle_input(input).map(|()| true)
        })?;
        match r {
            Ok(true) => {}
            Ok(false) => {
                self.shared
                    .stats
                    .udp_refused
                    .fetch_add(1, Ordering::Relaxed);
            }
            Err(e) => {
                // one line per session, the rest are counted: a forged datagram must not make
                // the log grow with the flood (R2-22)
                self.shared
                    .stats
                    .input_errors
                    .fetch_add(1, Ordering::Relaxed);
                if !self.logged_input_err {
                    self.logged_input_err = true;
                    self.log(&format!(
                        "handle_input: {e} (further ones are counted only)"
                    ));
                }
            }
        }
        Ok(())
    }

    /// `encodeLoop`, one frame.
    pub(super) fn write_audio(&mut self, frame: Vec<u8>) -> Result<(), End> {
        self.shared.stats.frames_in.fetch_add(1, Ordering::Relaxed);
        if self.first_audio {
            self.first_audio = false;
            self.log(&format!(
                "first audio to the phone: {} bytes of 16 kHz PCM",
                frame.len()
            ));
        }
        let codec = self.send.map(|s| s.1).unwrap_or(PhoneCodec::Pcmu);
        let Some(payload) = self.enc.encode(codec, &frame) else {
            return Ok(()); // an odd G.722 frame: dropped (PARITY.md difference 55)
        };
        let ts = self.rtp.next_timestamp(payload.len() as u32);
        if ts < self.last_ts {
            self.ts_ext += 1 << 32; // the 32-bit RTP clock wrapped
        }
        self.last_ts = ts;
        let ts64 = self.ts_ext + u64::from(ts);
        if !self.ever_connected {
            return Ok(()); // str0m drops media written before Connected; so does the loop
        }
        let (Some(mid), Some((pt, _))) = (self.mid, self.send) else {
            return Ok(());
        };
        // G.722 samples at 16 kHz but its RTP clock is 8 kHz: str0m's API takes the nominal rate
        let mt = match codec {
            PhoneCodec::Pcmu => MediaTime::new(ts64, Frequency::EIGHT_KHZ),
            PhoneCodec::G722 => MediaTime::new(ts64 * 2, Frequency::SIXTEEN_KHZ),
        };
        let r = self.guard("writing audio", |c| {
            c.rtc
                .writer(mid)
                .map(|w| w.write(pt, Instant::now(), mt, payload))
        })?;
        match r {
            Some(Ok(())) => {
                self.shared
                    .stats
                    .frames_written
                    .fetch_add(1, Ordering::Relaxed);
            }
            Some(Err(e)) => {
                self.shared
                    .stats
                    .write_errors
                    .fetch_add(1, Ordering::Relaxed);
                if !self.logged_write_err {
                    self.logged_write_err = true;
                    self.log(&format!("writing audio to the phone: {e}"));
                }
            }
            None => {}
        }
        Ok(())
    }

    /// The timers other than str0m's.
    pub(super) fn next_deadline(&self, rtc_timeout: Instant) -> Instant {
        let mut t = rtc_timeout;
        if let Some(d) = self.disc_deadline {
            t = t.min(d);
        }
        if !self.ever_connected {
            t = t.min(self.connect_deadline);
        }
        t
    }

    pub(super) fn on_timer(&mut self) -> Result<(), End> {
        let now = Instant::now();
        if self.disc_deadline.is_some_and(|d| now >= d) {
            return Err(End::Grace);
        }
        if !self.ever_connected && now >= self.connect_deadline {
            return Err(End::ConnectTimeout);
        }
        self.input(Input::Timeout(now))
    }

    /// The tail of a stopped session: str0m's graceful close (RTCP BYE, DTLS close_notify) and one
    /// bounded pass of what it queued (`MAX_OUTPUTS_PER_PASS`).
    pub(super) fn goodbye(&mut self) {
        let _ = self.guard("the graceful close", |c| {
            let _ = c.rtc.close();
            for _ in 0..MAX_OUTPUTS_PER_PASS {
                match c.poll_out() {
                    Ok(Output::Transmit(t)) => c.send_transmit(&t),
                    Ok(Output::Event(_)) => {}
                    Ok(Output::Timeout(_)) | Err(_) => break,
                }
            }
        });
    }
}
