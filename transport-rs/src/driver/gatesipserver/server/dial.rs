//! `dial.go` (DEV-518 W4): the server places the "Talk to the gate" call itself. It is a child
//! module of `server` (it reads the server's private state, as Go's `dial.go` shares the
//! package).
//!
//! ⛔ OFF by default (`dial_enabled`). Off, none of this is reachable: CanDial is absent from the
//! configure reply and never republished, `dial_call` refuses without a single datagram, the
//! legacy `dial` is `not_supported` as before, and the inbound path is untouched.
//!
//! The call, as gate-sip's `outbound.go` places it: `INVITE sip:9901@<station ip>` from
//! `sip:8001@<our ip>`, to the address the station REGISTERed FROM (preferred over its Contact),
//! SDP offering PCM/16000 (payload 96) then PCMA/8000 (8); 401/407 answered once with the
//! registration credentials; 200 -> ACK (To tag the station chose) -> media on our own RTP socket
//! under the same strict source rule as an inbound call; BYE on hang-up.
//!
//! # How this differs from the Go code, and why it is the same on the wire
//!
//! * Go blocks a goroutine in `dial` on a `select`; here `dial` is a future. Dropping it is the
//!   analogue of nothing in Go (a goroutine cannot be dropped) and is the one new hazard: a
//!   future owning a ringing call must not leave the station ringing and the slot taken. A
//!   [`DialGuard`] sends the CANCEL, waits for the final answer in a detached task and frees the
//!   slot.
//! * The datagrams are written by hand on the server's own SIP socket, exactly as Go does (the
//!   Via carries the server's SIP port and `rport`); `sip::Dial` opens its own endpoint and
//!   would differ on the wire.

use super::*;
use crate::util::lock;

use tokio::sync::mpsc;

use super::super::media::media_narrow;

use crate::gostd::parse::duration_string_of;
use crate::sip::digest::authorization_for_raw;
use crate::sip::{parse_response, Response};

/// `stationUser`: the extension the station's talk channel answers on.
const STATION_USER: &str = "9901";
/// `defaultDialRing`: gate-sip's `GATE_SIP_CALL_TIMEOUT_SECONDS` default.
pub const DEFAULT_DIAL_RING: Duration = Duration::from_secs(20);
/// `defaultDialCap`: from the 200 OK (`GATE_SIP_PEER_MAX_SECONDS`).
pub const DEFAULT_DIAL_CAP: Duration = Duration::from_secs(300);
/// `defaultDialAttach`: the observer's `legTimings.sinkAttach`.
pub const DEFAULT_DIAL_ATTACH: Duration = Duration::from_secs(15);
/// `defaultCancelWait`: for the final answer after a CANCEL.
pub const DEFAULT_CANCEL_WAIT: Duration = Duration::from_secs(2);

/// What `dial` returns when it refuses or fails: Go's error, which wraps one of the package's
/// sentinels (or none, for `no call id` and `no route`) in text of its own.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DialError {
    kind: Option<ServerError>,
    text: String,
}

impl DialError {
    fn of(kind: ServerError, text: impl Into<String>) -> DialError {
        DialError {
            kind: Some(kind),
            text: text.into(),
        }
    }

    fn plain(text: impl Into<String>) -> DialError {
        DialError {
            kind: None,
            text: text.into(),
        }
    }

    /// `errors.Is(err, kind)` (`ErrDialBusy` also matches the "another call" form).
    pub fn is(&self, kind: ServerError) -> bool {
        match self.kind {
            Some(k) if kind == ServerError::DialBusy => k.is_dial_busy(),
            Some(k) => k == kind,
            None => false,
        }
    }

    /// The sentinel this error wraps.
    pub fn kind(&self) -> Option<ServerError> {
        self.kind
    }
}

impl std::fmt::Display for DialError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.text)
    }
}

impl std::error::Error for DialError {}

impl From<ServerError> for DialError {
    fn from(e: ServerError) -> DialError {
        DialError::of(e, e.to_string())
    }
}

/// The mutable part of the dialog of a call we placed (Go keeps it in `outCall`, under
/// `Server.mu`; this has a lock of its own, a leaf below the state lock).
struct OutMut {
    /// the To tag the station chose (200 OK), as received
    remote_tag: Vec<u8>,
    /// CSeq of the current INVITE
    cseq: i64,
    /// branch of the current INVITE
    branch: String,
    active: bool,
    cap_timer: Option<CancelToken>,
    att_timer: Option<CancelToken>,
}

/// `outCall`: the dialog state of a call we placed. Shared (`Arc`) between the call slot and the
/// `dial` future, which keeps sending on it after the slot has been cleared, as Go's goroutine
/// keeps its `*outCall`.
pub struct OutCall {
    call_id: String,
    /// the station's SIP address (where the INVITE goes)
    addr: SocketAddr,
    local_ip: String,
    /// request-URI (`sip:9901@<ip>`)
    target: String,
    /// `<sip:8001@ip>;tag=ours`
    from_hdr: String,
    /// `<target>`, without tag
    to_hdr: String,
    resp: mpsc::Sender<Response>,
    m: Mutex<OutMut>,
}

impl OutCall {
    /// `outCall.stopTimers`.
    pub(super) fn stop_timers(&self) {
        let m = lock(&self.m);
        if let Some(t) = &m.cap_timer {
            t.cancel();
        }
        if let Some(t) = &m.att_timer {
            t.cancel();
        }
    }

    /// `outCall.to`: our To header, with the station's tag once it has chosen one.
    fn to(&self) -> Vec<u8> {
        let m = lock(&self.m);
        let mut t = self.to_hdr.clone().into_bytes();
        if !m.remote_tag.is_empty() {
            t.extend_from_slice(b";tag=");
            t.extend_from_slice(&m.remote_tag);
        }
        t
    }

    /// `oc.active`, `c.out.active = true` on the first media of the call.
    pub(super) fn mark_active(&self) -> bool {
        let mut m = lock(&self.m);
        !std::mem::replace(&mut m.active, true)
    }
}

/// Test seam: the four timings of a dialled call (Go's tests write `s.dialRing` and friends).
#[cfg(feature = "test-seams")]
#[derive(Clone, Copy, Debug, Default)]
pub struct DialTimers {
    pub ring: Option<Duration>,
    pub cancel_wait: Option<Duration>,
    pub cap: Option<Duration>,
    pub attach: Option<Duration>,
}

/// `reToTag` = `;\s*tag=([^;>\s]+)`: the first match's value, `""` when there is none.
pub(super) fn to_tag_of(to: &[u8]) -> Vec<u8> {
    let is_space = |c: u8| matches!(c, b'\t' | b'\n' | 0x0c | b'\r' | b' ');
    let mut i = 0;
    while i < to.len() {
        if to[i] == b';' {
            let mut j = i + 1;
            while j < to.len() && is_space(to[j]) {
                j += 1;
            }
            if to[j..].starts_with(b"tag=") {
                let s = j + 4;
                let e = to[s..]
                    .iter()
                    .position(|&c| c == b';' || c == b'>' || is_space(c))
                    .map_or(to.len(), |k| s + k);
                if e > s {
                    return to[s..e].to_vec();
                }
            }
        }
        i += 1;
    }
    Vec::new()
}

/// `fmt.Sscanf(s, "%d %s", &seq, &method)`: `(seq, method)`, `None` when Go would return an
/// error (no number, a number that overflows an int64, no word after it).
fn scan_cseq(s: &str) -> Option<(i64, String)> {
    let b = s.as_bytes();
    let is_space = |c: u8| matches!(c, b' ' | b'\t' | b'\r' | b'\n' | 0x0b | 0x0c);
    let mut i = 0;
    while i < b.len() && is_space(b[i]) {
        i += 1;
    }
    let st = i;
    if i < b.len() && (b[i] == b'+' || b[i] == b'-') {
        i += 1;
    }
    let ds = i;
    while i < b.len() && b[i].is_ascii_digit() {
        i += 1;
    }
    if i == ds {
        return None;
    }
    let seq: i64 = s[st..i].trim_start_matches('+').parse().ok()?;
    // the format's space matches ONE or more spaces (and never a newline)
    let sp = i;
    while i < b.len() && is_space(b[i]) {
        if b[i] == b'\n' {
            return None;
        }
        i += 1;
    }
    if i == sp {
        return None;
    }
    let ws = i;
    while i < b.len() && !is_space(b[i]) {
        i += 1;
    }
    if i == ws {
        return None;
    }
    Some((seq, s[ws..i].to_string()))
}

/// `sdpOffer`: what WE propose, wideband first with A-law as the fallback (gate-sip's
/// `sdpOffer`): the station takes 16 kHz when it is on the menu.
fn sdp_offer(local_ip: &str, rtp_port: u16) -> String {
    let (w, n) = (media_wide(), media_narrow());
    format!(
        "v=0\r\no=- 0 0 IN IP4 {local_ip}\r\ns=portico-gate-sip-server\r\nc=IN IP4 {local_ip}\r\nt=0 0\r\n\
         m=audio {rtp_port} RTP/AVP {} {}\r\na=rtpmap:{} {}\r\na=rtpmap:{} {}\r\na=sendrecv\r\n",
        w.pt, n.pt, w.pt, w.name, n.pt, n.name
    )
}

fn line(s: impl AsRef<str>) -> Vec<u8> {
    s.as_ref().as_bytes().to_vec()
}

/// `strings.Join(L, "\r\n") + "\r\n\r\n" + body`.
fn assemble(lines: Vec<Vec<u8>>, body: &[u8]) -> Vec<u8> {
    let mut out = lines.join(&b"\r\n"[..]);
    out.extend_from_slice(b"\r\n\r\n");
    out.extend_from_slice(body);
    out
}

/// Owns a call being placed for as long as `dial` has not returned. Dropped while the INVITE is
/// still out (the caller gave up: a closed connection, a cancelled request), it does what the
/// Go goroutine would have done on a cancelled context: CANCEL, wait a moment for the final
/// answer (a 200 that crosses it is ACKed and BYEd), free the slot. The waiting runs in a
/// detached task, the rest at once.
struct DialGuard {
    srv: Arc<Server>,
    o: Arc<OutCall>,
    serial: u64,
    rx: Option<mpsc::Receiver<Response>>,
    armed: bool,
    cancel_sent: bool,
    /// When the wait for the final response to a CANCEL we already sent ends: a guard dropped
    /// inside that wait carries it on instead of starting a second full wait.
    cancel_deadline: Option<tokio::time::Instant>,
}

impl Drop for DialGuard {
    fn drop(&mut self) {
        if !self.armed {
            return;
        }
        let (srv, o, serial, cancel_sent, deadline) = (
            self.srv.clone(),
            self.o.clone(),
            self.serial,
            self.cancel_sent,
            self.cancel_deadline,
        );
        let rx = self.rx.take();
        srv.logf(if cancel_sent {
            "gate_sip_server: the dial was abandoned while waiting for the answer to our CANCEL"
                .to_string()
        } else {
            "gate_sip_server: the dial was abandoned before the station answered — CANCEL"
                .to_string()
        });
        match tokio::runtime::Handle::try_current() {
            Ok(rt) => {
                rt.spawn(async move {
                    if !cancel_sent {
                        srv.send_cancel(&o);
                    }
                    if let Some(mut rx) = rx {
                        let until = deadline.unwrap_or_else(|| srv.cancel_deadline());
                        srv.await_final(&o, &mut rx, until).await;
                    }
                    srv.finish_dial(serial);
                });
            }
            Err(_) => {
                if !cancel_sent {
                    srv.send_cancel(&o);
                }
                srv.finish_dial(serial);
            }
        }
    }
}

impl Server {
    // ── the dial ─────────────────────────────────────────────────────────────────────────────

    /// Test seam: the four dial timings.
    #[cfg(feature = "test-seams")]
    pub fn set_dial_timers(&self, t: DialTimers) {
        let mut st = lock(&self.state);
        if let Some(d) = t.ring {
            st.dial_ring = d;
        }
        if let Some(d) = t.cancel_wait {
            st.cancel_w = d;
        }
        if let Some(d) = t.cap {
            st.dial_cap = d;
        }
        if let Some(d) = t.attach {
            st.dial_att = d;
        }
    }

    /// `dial(ctx, callID)`: places the call and returns when the station has answered (the audio
    /// client is already attached to the call, waiting for a client leg), or with the reason it
    /// did not. Nothing is emitted for a call that fails.
    pub async fn dial(
        &self,
        ctx: &CancelToken,
        call_id: &str,
    ) -> Result<Arc<AudioClient>, DialError> {
        if call_id.is_empty() {
            return Err(ServerError::NoCallId.into());
        }
        let (o, serial, rx) = self.begin_dial(call_id)?;
        self.logf(format!(
            "gate_sip_server: calling the station — INVITE {} from {}",
            o.target, o.local_ip
        ));
        let mut g = DialGuard {
            srv: self.arc(),
            o,
            serial,
            rx: Some(rx),
            armed: true,
            cancel_sent: false,
            cancel_deadline: None,
        };
        self.send_invite(&g.o, b"");
        let r = self.dial_loop(ctx, &mut g).await;
        g.armed = false;
        r
    }

    /// The part of `dial` that decides alone: the refusals, in Go's order and with Go's texts,
    /// and the reservation of the call slot ("reserved from here: an inbound INVITE now gets
    /// 486").
    fn begin_dial(
        &self,
        call_id: &str,
    ) -> Result<(Arc<OutCall>, u64, mpsc::Receiver<Response>), DialError> {
        let mut st = lock(&self.state);
        if st.closed {
            return Err(ServerError::ServerStopped.into());
        }
        if st.cur.is_some() {
            return Err(ServerError::DialBusyOtherCall.into());
        }
        let dest = match &st.reg {
            None => return Err(ServerError::NotRegistered.into()),
            Some(r) if r.at.elapsed() > st.reg_ttl => {
                return Err(ServerError::RegistrationStale.into())
            }
            Some(r) => r.addr,
        };
        let lip = local_ip_toward(dest);
        if lip.is_empty() {
            return Err(DialError::plain(format!(
                "no route to the station at {}",
                dest.ip()
            )));
        }
        let our_tag = tok(8);
        let target = format!("sip:{STATION_USER}@{}", dest.ip());
        let (tx, rx) = mpsc::channel(8);
        let o = Arc::new(OutCall {
            call_id: call_id.to_string(),
            addr: dest,
            local_ip: lip.clone(),
            from_hdr: format!("<sip:{}@{lip}>;tag={our_tag}", self.cfg.user),
            to_hdr: format!("<{target}>"),
            target,
            resp: tx,
            m: Mutex::new(OutMut {
                remote_tag: Vec::new(),
                cseq: 1,
                branch: String::new(),
                active: false,
                cap_timer: None,
                att_timer: None,
            }),
        });
        let serial = st.next_serial;
        st.next_serial += 1;
        st.cur = Some(Call {
            out: Some(o.clone()),
            ..Call::new(
                serial,
                Hdrs {
                    call_id: call_id.as_bytes().to_vec(),
                    ..Hdrs::default()
                },
                dest,
                our_tag,
                lip,
            )
        });
        st.st.dials += 1;
        Ok((o, serial, rx))
    }

    /// The `for { select { ... } }` of `dial`.
    async fn dial_loop(
        &self,
        ctx: &CancelToken,
        g: &mut DialGuard,
    ) -> Result<Arc<AudioClient>, DialError> {
        enum Ev {
            Stop,
            Ctx,
            Ring,
            Resp(Option<Response>),
        }
        let o = g.o.clone();
        let (ring_after, serial) = (lock(&self.state).dial_ring, g.serial);
        let ring = tokio::time::sleep(ring_after);
        tokio::pin!(ring);
        let mut auth_tried = false;
        loop {
            let ev = tokio::select! {
                biased;
                _ = self.stop.cancelled() => Ev::Stop,
                _ = ctx.cancelled() => Ev::Ctx,
                _ = &mut ring => Ev::Ring,
                r = g.rx.as_mut().expect("the receiver is owned until the end").recv() => Ev::Resp(r),
            };
            match ev {
                Ev::Stop => {
                    return self
                        .dial_fail(g, ServerError::ServerStopped.into(), false)
                        .await
                }
                Ev::Ctx => {
                    self.logf("gate_sip_server: the dial was cancelled before the station answered — CANCEL".to_string());
                    return self
                        .dial_fail(g, ServerError::DialCancelled.into(), true)
                        .await;
                }
                Ev::Ring => {
                    self.logf(format!(
                        "gate_sip_server: the station did not answer within {} — CANCEL",
                        duration_string_of(ring_after)
                    ));
                    let e = DialError::of(
                        ServerError::NoAnswer,
                        format!(
                            "{} within {}",
                            ServerError::NoAnswer,
                            duration_string_of(ring_after)
                        ),
                    );
                    return self.dial_fail(g, e, true).await;
                }
                Ev::Resp(None) => {
                    // the sender lives in `OutCall`, which this future holds: unreachable
                    return self
                        .dial_fail(g, ServerError::ServerStopped.into(), false)
                        .await;
                }
                Ev::Resp(Some(r)) => {
                    let code = r.status_code;
                    match code {
                        c if c < 200 => continue, // 100 Trying, 180 Ringing, 183
                        401 | 407 => {
                            let (hdr_name, auth_name) = if code == 407 {
                                ("Proxy-Authenticate", "Proxy-Authorization")
                            } else {
                                ("WWW-Authenticate", "Authorization")
                            };
                            // a non-2xx final response is ACKed by the transaction
                            self.send_ack(&o, r.header_raw("To"), true);
                            if auth_tried {
                                let e = DialError::of(
                                    ServerError::StationRefused,
                                    format!(
                                        "{}: it rejected our credentials ({code})",
                                        ServerError::StationRefused
                                    ),
                                );
                                return self.dial_fail(g, e, false).await;
                            }
                            // R1-5: the challenge is read as the bytes the station sent and the
                            // answer is echoed as bytes (a Latin-1 realm survives, as in Go)
                            let auth = match authorization_for_raw(
                                r.header_raw(hdr_name),
                                self.cfg.user.as_bytes(),
                                self.cfg.password.as_bytes(),
                                b"INVITE",
                                o.target.as_bytes(),
                            ) {
                                Ok(a) => a,
                                Err(_) => {
                                    let e = DialError::of(
                                        ServerError::StationRefused,
                                        format!(
                                            "{}: {code} with a challenge we cannot answer",
                                            ServerError::StationRefused
                                        ),
                                    );
                                    return self.dial_fail(g, e, false).await;
                                }
                            };
                            auth_tried = true;
                            lock(&o.m).cseq += 1;
                            self.logf(format!("gate_sip_server: the station challenged our INVITE ({code}) — answering with the registration credentials"));
                            let mut auth_line = format!("{auth_name}: ").into_bytes();
                            auth_line.extend_from_slice(&auth);
                            self.send_invite(&o, &auth_line);
                        }
                        200 => {
                            let r = self.dial_answered(ctx, &o, serial, &r);
                            g.armed = false;
                            return r;
                        }
                        _ => {
                            self.send_ack(&o, r.header_raw("To"), true);
                            self.logf(format!(
                                "gate_sip_server: the station refused our call: {code} {}",
                                r.reason
                            ));
                            let e = DialError::of(
                                ServerError::StationRefused,
                                format!("{}: {code}", ServerError::StationRefused),
                            );
                            return self.dial_fail(g, e, false).await;
                        }
                    }
                }
            }
        }
    }

    /// `fail`: ends the attempt; with `cancel` it first CANCELs the INVITE and waits a moment
    /// for the final answer (a 200 that crosses the CANCEL is ACKed and BYEd).
    async fn dial_fail(
        &self,
        g: &mut DialGuard,
        err: DialError,
        cancel: bool,
    ) -> Result<Arc<AudioClient>, DialError> {
        if cancel {
            g.cancel_sent = true;
            let until = self.cancel_deadline();
            g.cancel_deadline = Some(until);
            self.send_cancel(&g.o);
            let o = g.o.clone();
            if let Some(rx) = g.rx.as_mut() {
                self.await_final(&o, rx, until).await;
            }
        }
        self.finish_dial(g.serial);
        g.armed = false;
        Err(err)
    }

    /// The end of a wait for the final response to a CANCEL that starts now.
    pub(super) fn cancel_deadline(&self) -> tokio::time::Instant {
        tokio::time::Instant::now() + lock(&self.state).cancel_w
    }

    /// The end of `fail`: frees the slot if the call is still the live one.
    pub(super) fn finish_dial(&self, serial: u64) {
        let mut st = lock(&self.state);
        if st.cur.as_ref().map(|c| c.serial) == Some(serial) {
            self.end_cur_locked(&mut st, "the dial failed");
        }
    }

    /// `awaitFinal`: waits briefly for the final response to a CANCELled INVITE: 487 (or any
    /// other final) is ACKed; a 200 that crossed the CANCEL is ACKed and BYEd.
    pub(super) async fn await_final(
        &self,
        o: &OutCall,
        rx: &mut mpsc::Receiver<Response>,
        until: tokio::time::Instant,
    ) {
        let t = tokio::time::sleep_until(until);
        tokio::pin!(t);
        loop {
            tokio::select! {
                biased;
                _ = &mut t => return,
                _ = self.stop.cancelled() => return,
                r = rx.recv() => {
                    let Some(r) = r else { return };
                    match r.status_code {
                        c if c < 200 => {}
                        200 => {
                            lock(&o.m).remote_tag = to_tag_of(r.header_raw("To"));
                            self.send_ack(o, b"", false);
                            self.send_bye_out(o);
                            return;
                        }
                        _ => {
                            self.send_ack(o, r.header_raw("To"), true);
                            return;
                        }
                    }
                }
            }
        }
    }

    /// `answered`: handles the 200 OK to our INVITE: read the station's choice, ACK it, and bring
    /// the call up. Synchronous, so the future has no cancellation point between the ACK and the
    /// call being up.
    fn dial_answered(
        &self,
        ctx: &CancelToken,
        o: &Arc<OutCall>,
        serial: u64,
        r: &Response,
    ) -> Result<Arc<AudioClient>, DialError> {
        lock(&o.m).remote_tag = to_tag_of(r.header_raw("To"));
        // ACK first, whatever follows: the station keeps retransmitting its 200 until it hears one.
        self.send_ack(o, b"", false);
        let bye_fail = |e: DialError| {
            self.send_bye_out(o);
            self.finish_dial(serial);
            Err(e)
        };
        if ctx.is_cancelled() {
            return bye_fail(ServerError::DialCancelled.into());
        }
        let body = go_lossy(&r.body);
        let (md, why) = choose_media(&body);
        if !why.starts_with("wideband") && !why.starts_with("narrowband") {
            return bye_fail(DialError::of(
                ServerError::StationRefused,
                format!(
                    "{}: its answer chose a codec we did not offer ({why})",
                    ServerError::StationRefused
                ),
            ));
        }
        let mut far_ip = o.addr.ip();
        if let Some(m) = find_c_ip(&body) {
            match parse_addr(m) {
                Ok(a) if a.zone().is_empty() && self.cfg.allowed(a.octets()) => {
                    far_ip = a.to_ip().to_canonical();
                }
                _ => self.logf(format!(
                    "gate_sip_server: answer SDP c={} unusable or outside allow_from, using the SIP source",
                    go_quote(m)
                )),
            }
        }
        let far_port = find_m_port(&body).map_or(0, |(p, _)| p);
        if far_port <= 0 || far_port > 65535 {
            return bye_fail(DialError::of(
                ServerError::StationRefused,
                format!(
                    "{}: its answer named no media port",
                    ServerError::StationRefused
                ),
            ));
        }
        let mut st = lock(&self.state);
        if st.cur.as_ref().map(|c| c.serial) != Some(serial) || st.closed {
            return Err(ServerError::ServerStopped.into());
        }
        let (dial_cap, dial_att) = (st.dial_cap, st.dial_att);
        let far = FarRtp {
            ip: far_ip,
            port: far_port,
        };
        let c = st.cur.as_mut().expect("checked above");
        c.media = md.clone();
        c.far_rtp = Some(far);
        c.answered = true;
        let (stop, id) = (c.stop.clone(), c.id());
        st.st.answered += 1;
        let host: Arc<dyn ClientHost> = self.arc();
        let cl = AudioClient::new(
            md.rate,
            md.frame_bytes,
            Arc::downgrade(&host),
            self.audio_stats.clone(),
        );
        if self.cfg.call_stats {
            cl.enable_stats();
        }
        st.client = Some((cl.clone(), serial));
        {
            let mut m = lock(&o.m);
            let cap = CancelToken::new();
            m.cap_timer = Some(cap.clone());
            let id2 = id.clone();
            self.spawn_timer(dial_cap, cap, move |s| {
                if s.hangup_call(serial, "past the call cap") {
                    s.logf(format!(
                        "gate_sip_server: dialled call {id2} hit the {} cap — BYE",
                        duration_string_of(dial_cap)
                    ));
                }
            });
            let att = CancelToken::new();
            m.att_timer = Some(att.clone());
            let cl2 = cl.clone();
            self.spawn_timer(dial_att, att, move |s| {
                let joined = cl2.leg() || cl2.is_closed();
                if !joined && s.hangup_call(serial, "no client leg took the audio in time") {
                    s.logf(format!(
                        "gate_sip_server: nobody took the dialled call's audio within {} — BYE",
                        duration_string_of(dial_att)
                    ));
                }
            });
        }
        let h = tokio::spawn(self.arc().rtp_sender(serial, stop, md, Some(far)));
        {
            let mut senders = lock(&self.senders);
            senders.retain(|j| !j.is_finished());
            senders.push(h);
        }
        self.emit(Event::State {
            call_id: id,
            state: STATE_ANSWERED,
        });
        drop(st);
        self.logf(format!(
            "gate_sip_server: the station ANSWERED our call ({why}), media {}",
            far.go_text()
        ));
        Ok(cl)
    }

    // ── responses ────────────────────────────────────────────────────────────────────────────

    /// `onResponse`: routes a response to the INVITE of the call we are placing. Anything else
    /// (answers to our BYE or CANCEL, strays, a response from another host) is dropped.
    pub(super) fn on_response(&self, raw: &[u8], from: SocketAddr) {
        let Ok(r) = parse_response(raw) else { return };
        let Some((seq, method)) = scan_cseq(r.header("CSeq")) else {
            return;
        };
        if method != "INVITE" {
            return;
        }
        let st = lock(&self.state);
        let Some(c) = st.cur.as_ref() else { return };
        let Some(o) = c.out.clone() else { return };
        if r.header_raw("Call-ID") != c.h.call_id.as_slice()
            || from.ip().to_canonical() != c.addr.ip().to_canonical()
            || seq != lock(&o.m).cseq
        {
            return;
        }
        if c.answered {
            // A retransmitted 200 means our ACK was lost: ACK again.
            let again = r.status_code == 200;
            drop(st);
            if again {
                self.send_ack(&o, b"", false);
            }
            return;
        }
        drop(st);
        // a full channel drops the response (Go: `select { case ch <- r: default: }`)
        let _ = o.resp.try_send(r);
    }

    // ── wire ─────────────────────────────────────────────────────────────────────────────────

    fn via_for(&self, o: &OutCall, branch: &str) -> String {
        format!(
            "SIP/2.0/UDP {};branch={branch};rport",
            self.contact_host_port(o.addr)
        )
    }

    /// `sendInvite`: sends (or, with an auth header, re-sends) the INVITE.
    fn send_invite(&self, o: &OutCall, auth_line: &[u8]) {
        let body = sdp_offer(&o.local_ip, self.rtp_port);
        let (cseq, branch) = {
            let mut m = lock(&o.m);
            m.branch = format!("z9hG4bK{}", tok(12));
            (m.cseq, m.branch.clone())
        };
        let mut l = vec![
            line(format!("INVITE {} SIP/2.0", o.target)),
            line(format!("Via: {}", self.via_for(o, &branch))),
            line("Max-Forwards: 70"),
            line(format!("From: {}", o.from_hdr)),
            line(format!("To: {}", o.to_hdr)),
            line(format!("Call-ID: {}", o.call_id)),
            line(format!("CSeq: {cseq} INVITE")),
            line(format!(
                "Contact: <sip:{}@{}>",
                self.cfg.user,
                self.contact_host_port(o.addr)
            )),
            line("User-Agent: portico-gate-sip-server"),
            line("Allow: INVITE, ACK, BYE, CANCEL, OPTIONS"),
        ];
        if !auth_line.is_empty() {
            l.push(auth_line.to_vec());
        }
        l.push(line("Content-Type: application/sdp"));
        l.push(line(format!("Content-Length: {}", body.len())));
        self.send_bytes(&assemble(l, body.as_bytes()), o.addr);
    }

    /// `sendCancel`: withdraws the INVITE still ringing out: same Request-URI, Call-ID, CSeq
    /// number, From, To and branch as the INVITE (RFC 3261 9.1).
    pub(super) fn send_cancel(&self, o: &OutCall) {
        let (cseq, branch) = {
            let m = lock(&o.m);
            (m.cseq, m.branch.clone())
        };
        let l = vec![
            line(format!("CANCEL {} SIP/2.0", o.target)),
            line(format!("Via: {}", self.via_for(o, &branch))),
            line("Max-Forwards: 70"),
            line(format!("From: {}", o.from_hdr)),
            line(format!("To: {}", o.to_hdr)),
            line(format!("Call-ID: {}", o.call_id)),
            line(format!("CSeq: {cseq} CANCEL")),
            line("Content-Length: 0"),
        ];
        self.send_bytes(&assemble(l, b""), o.addr);
    }

    /// `sendAck`: ACKs a final response. For a 2xx (`non_2xx` false) it is its own transaction:
    /// new branch, the To tag the station chose. For a non-2xx it reuses the INVITE's branch and
    /// echoes the response's To.
    pub(super) fn send_ack(&self, o: &OutCall, resp_to: &[u8], non_2xx: bool) {
        let (cseq, mut branch) = {
            let m = lock(&o.m);
            (m.cseq, m.branch.clone())
        };
        let mut to = o.to();
        if non_2xx {
            if !resp_to.is_empty() {
                to = resp_to.to_vec();
            }
        } else {
            branch = format!("z9hG4bK{}", tok(12));
        }
        let mut to_line = b"To: ".to_vec();
        to_line.extend_from_slice(&to);
        let l = vec![
            line(format!("ACK {} SIP/2.0", o.target)),
            line(format!("Via: {}", self.via_for(o, &branch))),
            line("Max-Forwards: 70"),
            line(format!("From: {}", o.from_hdr)),
            to_line,
            line(format!("Call-ID: {}", o.call_id)),
            line(format!("CSeq: {cseq} ACK")),
            line("Content-Length: 0"),
        ];
        self.send_bytes(&assemble(l, b""), o.addr);
    }

    /// `sendByeOut`: hangs up a call we placed: we are the caller, so From is ours (with our tag)
    /// and To the station's (with its tag); CSeq follows the INVITE's.
    pub(super) fn send_bye_out(&self, o: &OutCall) {
        let cseq = lock(&o.m).cseq + 1;
        let mut to_line = b"To: ".to_vec();
        to_line.extend_from_slice(&o.to());
        let l = vec![
            line(format!("BYE {} SIP/2.0", o.target)),
            line(format!(
                "Via: {}",
                self.via_for(o, &format!("z9hG4bK{}", tok(12)))
            )),
            line("Max-Forwards: 70"),
            line(format!("From: {}", o.from_hdr)),
            to_line,
            line(format!("Call-ID: {}", o.call_id)),
            line(format!("CSeq: {cseq} BYE")),
            line("Content-Length: 0"),
        ];
        self.send_bytes(&assemble(l, b""), o.addr);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // `reToTag` (`;\s*tag=([^;>\s]+)`) on the shapes a To header takes.
    #[test]
    fn to_tag_extraction() {
        let t = |s: &str| String::from_utf8(to_tag_of(s.as_bytes())).unwrap();
        assert_eq!(t("<sip:9901@127.0.0.1>;tag=st1"), "st1");
        assert_eq!(t("<sip:9901@127.0.0.1>; tag=abc;x=1"), "abc");
        assert_eq!(t("<sip:9901@127.0.0.1;tag=in>"), "in");
        assert_eq!(t("<sip:9901@127.0.0.1>;tag=a>b"), "a");
        assert_eq!(t("<sip:9901@127.0.0.1>"), "");
        assert_eq!(t("<sip:x>;tag="), "");
        // an empty value at the first match: the regexp goes on to the next one
        assert_eq!(t("<sip:x>;tag=;tag=second"), "second");
        assert_eq!(t("<sip:x>;ttag=no"), "");
    }

    // `fmt.Sscanf(CSeq, "%d %s")`: the number and the method, or an error.
    #[test]
    fn cseq_scan_matches_sscanf() {
        let ok = |s: &str| scan_cseq(s);
        assert_eq!(ok("1 INVITE"), Some((1, "INVITE".to_string())));
        assert_eq!(ok("  12   BYE  "), Some((12, "BYE".to_string())));
        assert_eq!(ok("+7 CANCEL"), Some((7, "CANCEL".to_string())));
        assert_eq!(ok("-3 INVITE"), Some((-3, "INVITE".to_string())));
        assert_eq!(ok("9INVITE"), None); // the format's space needs a space
        assert_eq!(ok("1\nINVITE"), None);
        assert_eq!(ok("1 INVITE extra"), Some((1, "INVITE".to_string())));
        assert_eq!(ok("INVITE"), None);
        assert_eq!(ok("1"), None);
        assert_eq!(ok(""), None);
        assert_eq!(ok("99999999999999999999 INVITE"), None);
    }

    // `sdpOffer`: wideband first, A-law as the fallback, the server's RTP port.
    #[test]
    fn the_offer_is_wideband_then_alaw() {
        assert_eq!(
            sdp_offer("127.0.0.1", 40000),
            "v=0\r\no=- 0 0 IN IP4 127.0.0.1\r\ns=portico-gate-sip-server\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\n\
             m=audio 40000 RTP/AVP 96 8\r\na=rtpmap:96 PCM/16000\r\na=rtpmap:8 PCMA/8000\r\na=sendrecv\r\n"
        );
    }
}
