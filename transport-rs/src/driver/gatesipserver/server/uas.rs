//! The UAS half: the SIP read path, INVITE / ACK / BYE / CANCEL handling, the ring and answer timers and the hang-ups (`server.go` `handleSIP`, `onInvite`, `onACK`, `onEnd`, `answer`, `hangup`).

use super::*;

/// The dialog's identifying fields: all a response needs (`hdrs`).
#[derive(Clone, Debug, Default)]
pub(super) struct Hdrs {
    pub(super) vias: Vec<Vec<u8>>,
    pub(super) from: Vec<u8>,
    pub(super) to: Vec<u8>,
    pub(super) call_id: Vec<u8>,
    pub(super) cseq: Vec<u8>,
}

/// The fields are the BYTES received (Go keeps them as strings of the received bytes): a
/// display name in Latin-1 is echoed exactly in every response and in our BYE, never repaired.
pub(super) fn hdrs_of(r: &ParsedRequest) -> Hdrs {
    Hdrs {
        vias: r.headers_raw("via").to_vec(),
        from: r.header_raw("From").to_vec(),
        to: r.header_raw("To").to_vec(),
        call_id: r.header_raw("Call-ID").to_vec(),
        cseq: r.header_raw("CSeq").to_vec(),
    }
}

/// What `answerLocked` returns for the caller to do after releasing the lock.
pub(super) struct Answer {
    pub(super) h: Hdrs,
    pub(super) addr: SocketAddr,
    pub(super) tag: String,
    pub(super) body: String,
    pub(super) far: Option<FarRtp>,
}

impl Server {
    #[allow(clippy::too_many_arguments)]
    pub(super) fn reply_h(
        &self,
        h: &Hdrs,
        addr: SocketAddr,
        code: u16,
        reason: &str,
        to_tag: &str,
        extra: &[String],
        body: &str,
    ) {
        let mut to = h.to.clone();
        if !to.windows(5).any(|w| w == b";tag=") {
            let tag = if to_tag.is_empty() {
                tok(8)
            } else {
                to_tag.to_string()
            };
            to.extend_from_slice(b";tag=");
            to.extend_from_slice(tag.as_bytes());
        }
        let mut out: Vec<u8> = format!("SIP/2.0 {code} {reason}").into_bytes();
        let mut line = |name: &str, v: &[u8]| {
            out.extend_from_slice(b"\r\n");
            out.extend_from_slice(name.as_bytes());
            out.extend_from_slice(b": ");
            out.extend_from_slice(v);
        };
        for v in &h.vias {
            line("Via", v);
        }
        line("From", &h.from);
        line("To", &to);
        line("Call-ID", &h.call_id);
        line("CSeq", &h.cseq);
        let mut rest = vec![
            format!(
                "Contact: <sip:{}@{}>",
                self.cfg.user,
                self.contact_host_port(addr)
            ),
            "User-Agent: portico-gate-sip-server".to_string(),
            "Allow: INVITE, ACK, BYE, CANCEL, OPTIONS".to_string(),
        ];
        rest.extend(extra.iter().cloned());
        if !body.is_empty() {
            rest.push("Content-Type: application/sdp".to_string());
        }
        rest.push(format!("Content-Length: {}", body.len()));
        for l in rest {
            out.extend_from_slice(b"\r\n");
            out.extend_from_slice(l.as_bytes());
        }
        out.extend_from_slice(b"\r\n\r\n");
        out.extend_from_slice(body.as_bytes());
        self.send_bytes(&out, addr);
    }

    /// `sipAllowed`: drops (silently, no reply) any datagram whose UDP source is not on the
    /// allow list, before it can touch a call, a counter or the registrar.
    pub(super) fn sip_allowed(&self, addr: SocketAddr) -> bool {
        let allowed = match addr.ip() {
            IpAddr::V4(a) => self.cfg.allowed(&a.octets()),
            IpAddr::V6(a) => self.cfg.allowed(&a.octets()),
        };
        if allowed {
            return true;
        }
        let mut st = lock(&self.state);
        st.st.dropped += 1;
        let now = Instant::now();
        let key = addr.ip();
        // An entry older than the quiet minute is spent: drop it, so a full table never silences
        // new outsiders for good (PARITY 333).
        st.last_drop_log
            .retain(|_, t| now.duration_since(*t) < Duration::from_secs(60));
        let quiet = if st
            .last_drop_log
            .get(&key)
            .is_some_and(|t| now.duration_since(*t) < Duration::from_secs(60))
        {
            true
        } else if st.last_drop_log.len() < 256 {
            st.last_drop_log.insert(key, now);
            false
        } else {
            true
        };
        drop(st);
        if !quiet {
            self.logf(format!(
                "gate_sip_server: SIP from {addr} dropped — not in allow_from"
            ));
        }
        false
    }

    pub(super) fn handle_sip(&self, raw: &[u8], addr: SocketAddr) {
        if !self.sip_allowed(addr) {
            return;
        }
        if raw.starts_with(b"SIP/2.0 ") {
            // A response: to our BYE (nothing waits on it) or, since W4, to the INVITE of a
            // call we placed (dial.go).
            self.on_response(raw, addr);
            return;
        }
        let Ok(req) = parse_request(raw) else {
            return;
        };
        let h = hdrs_of(&req);
        match req.method.as_str() {
            "REGISTER" => self.on_register(&req, &h, addr),
            "INVITE" => self.on_invite(&req, h, addr),
            "ACK" => self.on_ack(&h),
            "BYE" | "CANCEL" => self.on_end(&req.method, &h, addr),
            "OPTIONS" => self.reply_h(&h, addr, 200, "OK", "", &[], ""),
            _ => {}
        }
    }

    /// The station's media address from its SDP `c=` line (`farIP` and its text as Go prints it
    /// in the INVITE log line): only believed if it parses AND is inside allow_from (the same
    /// list the SIP source passed); otherwise our RTP would go to a nil or third-party host and
    /// the SIP source is used.
    fn station_media(&self, body: &str, addr: SocketAddr) -> (IpAddr, String) {
        let mut far_ip = addr.ip();
        let mut far_text = far_ip.to_string();
        if let Some(m) = find_c_ip(body) {
            // The station's c= is only believed if it parses AND is inside allow_from (the same
            // list the SIP source passed); otherwise our RTP would go to a nil or third-party
            // host. Fall back to the SIP source.
            match parse_addr(m) {
                Ok(a) if a.zone().is_empty() && self.cfg.allowed(a.octets()) => {
                    far_ip = a.to_ip().to_canonical();
                    far_text = m.to_string();
                }
                _ => self.logf(format!(
                    "gate_sip_server: INVITE from {addr}: SDP c={} unusable or outside allow_from, using the SIP source",
                    go_quote(m)
                )),
            }
        }
        (far_ip, far_text)
    }

    /// A retransmitted INVITE of the call we hold is normal on UDP and is re-answered: 100, then
    /// the 200 if the call is answered, else the 180.
    fn answer_retransmission(
        &self,
        h: &Hdrs,
        addr: SocketAddr,
        answered: bool,
        tag: &str,
        md: &Media,
        lip: &str,
    ) {
        self.logf(format!(
            "gate_sip_server: INVITE retransmission for {}",
            go_quote(&go_lossy(&h.call_id))
        ));
        self.reply_h(h, addr, 100, "Trying", tag, &[], "");
        if answered {
            self.reply_h(
                h,
                addr,
                200,
                "OK",
                tag,
                &[],
                &sdp_for(lip, i64::from(self.rtp_port), md),
            );
        } else {
            self.reply_h(h, addr, 180, "Ringing", tag, &[], "");
        }
    }

    pub(super) fn on_invite(&self, req: &ParsedRequest, h: Hdrs, addr: SocketAddr) {
        if h.call_id.is_empty()
            || h.vias.is_empty()
            || h.from.is_empty()
            || h.to.is_empty()
            || h.cseq.is_empty()
        {
            self.logf(format!(
                "gate_sip_server: malformed INVITE from {addr} dropped"
            ));
            return;
        }
        // DELIBERATE DIFFERENCE from Go (DEV-796 S1, PARITY.md 473): only the REGISTERED station
        // may ring the house. The datagram must come from the host of the current REGISTER
        // binding (IP only, as `on_end` does: the port may differ), else it is refused 403 and
        // no call, ring or slot is taken. Before this, any host in allow_from could ring the
        // house and hold the one call slot (486 for the real station).
        let from_station = {
            let st = lock(&self.state);
            if st.closed {
                return; // the server is stopping: nothing new may start, not even a refusal
            }
            let from_station = Self::registered_locked(&st)
                && st
                    .reg
                    .as_ref()
                    .is_some_and(|r| r.addr.ip().to_canonical() == addr.ip().to_canonical());
            // A retransmission or re-INVITE of the live inbound call, from the host that
            // started it, is not a new call: it is re-answered even when the registration has
            // lapsed meanwhile (a refusal would end the dialog on the station while the call
            // stays up here).
            let live_dialog = st.cur.as_ref().is_some_and(|c| {
                c.out.is_none()
                    && c.is(&h.call_id)
                    && c.addr.ip().to_canonical() == addr.ip().to_canonical()
            });
            #[cfg(feature = "test-seams")]
            let from_station = from_station || st.open_invites;
            if !from_station && !live_dialog {
                drop(st);
                self.logf(format!(
                    "gate_sip_server: INVITE from {addr} for {} REFUSED 403 — not the registered station",
                    go_quote(&go_lossy(&h.call_id))
                ));
                self.reply_h(&h, addr, 403, "Forbidden", "", &[], "");
                return;
            }
            from_station
        };
        let body = go_lossy(&req.body);
        let (far_ip, far_text) = self.station_media(&body, addr);
        let (far_port, port_overflow) = find_m_port(&body).unwrap_or((0, false));
        let unsendable = far_port > 65535 || port_overflow;
        let far_rtp = (far_port > 0).then_some(FarRtp {
            ip: far_ip,
            port: far_port,
        });

        // NEVER overwrite a live call. A retransmitted INVITE for the SAME Call-ID is normal on
        // UDP and is re-answered; a DIFFERENT one is a second caller and gets 486 Busy Here.
        let mut st = lock(&self.state);
        if st.closed {
            return; // the server is stopping: nothing new may start
        }
        if let Some(existing) = &st.cur {
            // (An outbound call of ours (dial.go) is busy for ANY inbound INVITE, whatever its
            // Call-ID: a retransmission is only ever re-answered for an inbound call.)
            // (a retransmission is answered only to the host that started the call: another
            // host that became the registered station meanwhile finds the slot busy)
            if existing.out.is_some()
                || !existing.is(&h.call_id)
                || existing.addr.ip().to_canonical() != addr.ip().to_canonical()
            {
                let busy_with = existing.id();
                drop(st);
                self.logf(format!(
                    "gate_sip_server: INVITE from {addr} for {} REFUSED — {} is already active",
                    go_quote(&go_lossy(&h.call_id)),
                    go_quote(&busy_with)
                ));
                self.reply_h(&h, addr, 486, "Busy Here", "", &[], "");
                return;
            }
            let (answered, tag, md, lip) = (
                existing.answered,
                existing.tag.clone(),
                existing.media.clone(),
                existing.local_ip.clone(),
            );
            drop(st);
            self.answer_retransmission(&h, addr, answered, &tag, &md, &lip);
            return;
        }
        if !from_station {
            // Admitted only as the live call's retransmission, and that call ended while the
            // lock was down: this is not the registered station and may not start a new one.
            drop(st);
            self.logf(format!(
                "gate_sip_server: INVITE from {addr} for {} REFUSED 403 — not the registered station",
                go_quote(&go_lossy(&h.call_id))
            ));
            self.reply_h(&h, addr, 403, "Forbidden", "", &[], "");
            return;
        }
        let (chosen, why) = choose_media(&body);
        let serial = st.next_serial;
        st.next_serial += 1;
        let tag = tok(8);
        let ring_timer = CancelToken::new();
        let c = Call {
            far_rtp,
            media: chosen,
            ring_timer: Some(ring_timer.clone()),
            ..Call::new(serial, h.clone(), addr, tag.clone(), local_ip_toward(addr))
        };
        st.cur = Some(c);
        st.st.calls += 1;
        if unsendable {
            st.st.unsendable_media += 1;
        }
        self.spawn_timer(self.cfg.ring_limit, ring_timer, move |s| {
            s.ring_expired(serial)
        });
        // E1 must NOT also stamp Home Assistant (input_text.intercom_call_id): the house rings
        // from the button-press automation, and a second stamp rings it twice (design §4.2a,
        // DEV-518). This package has no HTTP client at all.
        self.emit(Event::Ringing {
            call_id: go_lossy(&h.call_id),
            device_id: self.cfg.device_id.clone(),
        });
        drop(st);

        self.logf(format!(
            "gate_sip_server: INVITE from {addr} to {}, station media {far_text}:{far_port}",
            go_quote(&go_lossy(&h.to))
        ));
        self.logf(format!("gate_sip_server: codec {why}"));
        if unsendable {
            // Go answers such a call and then never sends a packet (the send fails, silently).
            self.logf(format!(
                "gate_sip_server: WARNING INVITE from {addr}: the SDP media port is {} (not a UDP port); answering as Go does, but no RTP can ever be sent for {}",
                if port_overflow { "too large to read".to_string() } else { far_port.to_string() },
                go_quote(&go_lossy(&h.call_id))
            ));
        }
        self.reply_h(&h, addr, 100, "Trying", &tag, &[], "");
        // 180 Ringing is what makes the STATION play its own ringback to the visitor.
        self.reply_h(&h, addr, 180, "Ringing", &tag, &[], "");
    }

    pub(super) fn on_ack(&self, h: &Hdrs) {
        let mut st = lock(&self.state);
        let mut first = false;
        // Deliberate difference: gate-sip marks the live call confirmed on ANY ACK; here the
        // ACK must name the live call and the call must be answered.
        if let Some(c) = st.cur.as_mut() {
            if c.out.is_none() && c.is(&h.call_id) && c.answered && !c.confirm {
                c.confirm = true;
                first = true;
                let id = c.id();
                self.emit(Event::State {
                    call_id: id,
                    state: STATE_ACTIVE,
                });
            }
        }
        drop(st);
        if first {
            self.logf("gate_sip_server: ACK — dialog confirmed".to_string());
        }
    }

    /// `onEnd`: BYE and CANCEL. Both END A CALL, so both match the dialog before touching
    /// anything. Always answered 200, even for a dialog we do not know: a retransmitting
    /// station that gets no response keeps retransmitting.
    pub(super) fn on_end(&self, method: &str, h: &Hdrs, addr: SocketAddr) {
        self.logf(format!("gate_sip_server: {method} from {addr}"));
        // DELIBERATE DIFFERENCE from Go (server.go onEnd, lines 551 and 562 match on the Call-ID
        // alone): the datagram must also come from the live call's station host (IP only, the
        // port may differ), else it is answered like a request for an unknown dialog and ends
        // nothing. Needs the E3 rehearsal with the real station to confirm that its BYE comes
        // from the address it INVITEd from (PARITY.md).
        let mut ignored_log = None;
        let (tag, ended) = {
            let mut st = lock(&self.state);
            let mut tag = String::new();
            let mut live = false;
            if let Some(c) = st.cur.as_mut().filter(|c| c.is(&h.call_id)) {
                if c.addr.ip().to_canonical() == addr.ip().to_canonical() {
                    tag = c.tag.clone();
                    // A CANCEL cannot end a call WE placed (we are the caller: only a BYE does).
                    live = !(c.out.is_some() && method == "CANCEL");
                } else if !c.end_ignored_logged {
                    c.end_ignored_logged = true;
                    ignored_log = Some(format!(
                        "gate_sip_server: {method} for the live call {} from {addr}, not the station {} — ignored (once per call)",
                        go_quote(&c.id()),
                        c.addr.ip()
                    ));
                }
            }
            (tag, live)
        };
        if let Some(l) = ignored_log {
            self.logf(l);
        }
        self.reply_h(h, addr, 200, "OK", &tag, &[], "");

        // MATCH THE Call-ID: a BYE retransmitted for a FINISHED dialog must not end a LIVE call.
        let ended = if ended {
            let mut st = lock(&self.state);
            // A CANCEL of a call still ringing owes the INVITE its final response
            // (RFC 3261 §9.2): 487 Request Terminated. DELIBERATE DIFFERENCE from gate-sip,
            // which sends none (the owner's decision 2026-10-21, DEV-518). An answered call has no
            // INVITE transaction left to terminate, so none is sent. A CANCEL of an ANSWERED
            // call still ends it, as in Go (gate-sip pins it; the rehearsal depends on it).
            if st.cur.as_ref().is_some_and(|c| c.is(&h.call_id)) {
                self.end_cur_locked(&mut st, "the station ended the call")
            } else {
                None
            }
        } else {
            None
        };
        let Some(c) = ended else {
            self.logf(format!(
                "gate_sip_server: that {method} is not the live call ({}) — nothing ended",
                go_quote(&go_lossy(&h.call_id))
            ));
            return;
        };
        if method == "CANCEL" && !c.answered {
            self.reply_h(&c.h, c.addr, 487, "Request Terminated", &c.tag, &[], "");
        }
    }

    /// `endLocked`: ends the live call, which clears it, stops its timers and sender, tells any
    /// attached client (its frames channel is closed, the analogue of gate-sip closing the app's
    /// socket) and queues `ended`. Returns the call so the caller can send what it owes the
    /// station. Caller holds the state lock.
    pub(super) fn end_cur_locked(&self, st: &mut State, why: &str) -> Option<Call> {
        let c = st.cur.take()?;
        if let Some(t) = &c.ring_timer {
            t.cancel();
        }
        if let Some(t) = &c.orphan_timer {
            t.cancel();
        }
        if let Some(o) = &c.out {
            o.stop_timers();
        }
        c.stop.cancel();
        self.logf(format!(
            "gate_sip_server: call {} over ({why}): rtpIn={} rtpOut={} rtpRejected={}",
            go_quote(&c.id()),
            c.rtp_in,
            c.rtp_out,
            c.rtp_rejected
        ));
        if st.client.as_ref().is_some_and(|(_, s)| *s == c.serial) {
            if let Some((cl, _)) = st.client.take() {
                cl.close(why);
            }
        }
        // An outbound call that never came up has no id for anyone to follow: its failure is
        // `dial`'s error, not an `ended` (driver.CallDialer).
        if c.out.is_none() || c.answered {
            self.emit(Event::State {
                call_id: c.id(),
                state: STATE_ENDED,
            });
        }
        Some(c)
    }

    pub(super) fn spawn_timer(
        &self,
        d: Duration,
        token: CancelToken,
        f: impl FnOnce(Arc<Server>) + Send + 'static,
    ) {
        let me = self.arc();
        tokio::spawn(async move {
            tokio::select! {
                biased;
                _ = token.cancelled() => return,
                _ = tokio::time::sleep(d) => {}
            }
            let r = catch_unwind(AssertUnwindSafe(|| f(me.clone())));
            if r.is_err() {
                me.logf("gate_sip_server: a timer callback panicked".to_string());
            }
        });
    }

    /// `ringExpired`: ends a call nobody takes with an honest 480 (not silence): the extension
    /// exists, nobody picked up. It is what makes the station announce "not answered" and
    /// return to idle.
    pub(super) fn ring_expired(&self, serial: u64) {
        let c = {
            let mut st = lock(&self.state);
            let release = st
                .cur
                .as_ref()
                .is_some_and(|c| c.serial == serial && !c.answered);
            if release {
                self.end_cur_locked(&mut st, "nobody answered the call")
            } else {
                None
            }
        };
        let Some(c) = c else { return };
        self.logf(format!(
            "gate_sip_server: nobody answered within {} — 480 to the station",
            duration_string_of(self.cfg.ring_limit)
        ));
        self.reply_h(
            &c.h,
            c.addr,
            480,
            "Temporarily Unavailable",
            &c.tag,
            &[],
            "",
        );
    }

    /// `AnswerCurrent`: sends the 200 OK for the ringing call. Errors: `NoCall`,
    /// `AlreadyAnswered`.
    pub fn answer_current(&self) -> Result<(), ServerError> {
        let mut st = lock(&self.state);
        let Some(c) = &st.cur else {
            return Err(ServerError::NoCall);
        };
        // a call we are placing is not ours to answer
        if c.out.is_some() && !c.answered {
            return Err(ServerError::NoCall);
        }
        if c.answered {
            return Err(ServerError::AlreadyAnswered);
        }
        let a = self.answer_locked(&mut st);
        drop(st);
        self.finish_answer(a);
        Ok(())
    }

    /// `answerLocked`: marks the live call answered and returns the I/O to do once the lock is
    /// released. Caller holds the state lock and has checked the call is live and unanswered.
    pub(super) fn answer_locked(&self, st: &mut State) -> Answer {
        let c = st.cur.as_mut().expect("answer_locked needs a live call");
        c.answered = true;
        if let Some(t) = &c.ring_timer {
            t.cancel();
        }
        let a = Answer {
            h: c.h.clone(),
            addr: c.addr,
            tag: c.tag.clone(),
            body: sdp_for(&c.local_ip, i64::from(self.rtp_port), &c.media),
            far: c.far_rtp,
        };
        let (serial, id, stop, media, far) =
            (c.serial, c.id(), c.stop.clone(), c.media.clone(), c.far_rtp);
        st.st.answered += 1;
        self.emit(Event::State {
            call_id: id,
            state: STATE_ANSWERED,
        });
        let h = tokio::spawn(self.arc().rtp_sender(serial, stop, media, far));
        let mut senders = lock(&self.senders);
        senders.retain(|j| !j.is_finished());
        senders.push(h);
        a
    }

    pub(super) fn finish_answer(&self, a: Answer) {
        self.reply_h(&a.h, a.addr, 200, "OK", &a.tag, &[], &a.body);
        let far = a.far.map_or_else(|| "<nil>".to_string(), |f| f.go_text());
        self.logf(format!(
            "gate_sip_server: ANSWERED — 200 OK sent, media to {far}"
        ));
    }

    /// `Hangup`: BYE for the live call, which is cleared. False when there is none.
    pub fn hangup(&self) -> bool {
        let c = {
            let mut st = lock(&self.state);
            self.end_cur_locked(&mut st, "the call was hung up")
        };
        let Some(c) = c else { return false };
        self.send_bye(&c);
        self.logf("gate_sip_server: hung up".to_string());
        true
    }

    /// `hangupAnswered`: ends the live call `call_id` with a BYE, but only if it is the live call
    /// AND it was answered; false otherwise (a ringing call is not ours to BYE, and a finished or
    /// different one is not this call).
    pub fn hangup_answered(&self, call_id: &str) -> bool {
        let c = {
            let mut st = lock(&self.state);
            if !st
                .cur
                .as_ref()
                .is_some_and(|c| c.id() == call_id && c.answered)
            {
                return false;
            }
            self.end_cur_locked(&mut st, "the call was hung up")
        };
        let Some(c) = c else { return false };
        self.send_bye(&c);
        self.logf(format!("gate_sip_server: hung up {call_id}"));
        true
    }

    /// `ringingCall`: whether `call_id` is the live call and nobody has answered it.
    pub fn ringing_call(&self, call_id: &str) -> bool {
        lock(&self.state)
            .cur
            .as_ref()
            .is_some_and(|c| c.out.is_none() && c.id() == call_id && !c.answered)
    }

    /// `hangupCall(c, why)`: ends the call with this serial if it is still the live one.
    pub(super) fn hangup_call(&self, serial: u64, why: &str) -> bool {
        let c = {
            let mut st = lock(&self.state);
            if st.cur.as_ref().map(|c| c.serial) != Some(serial) {
                return false;
            }
            self.end_cur_locked(&mut st, why)
        };
        let Some(c) = c else { return false };
        self.send_bye(&c);
        true
    }

    /// `sendBye`: an INBOUND call, so we are the callee: From is the To the station sent, WITH
    /// our tag; To is the station's From. Sending any other shape gets the BYE discarded and the
    /// call stays up on the station.
    pub(super) fn send_bye(&self, c: &Call) {
        // a call we placed hangs up with sendByeOut
        if let Some(o) = &c.out {
            self.send_bye_out(o);
            return;
        }
        let mut out: Vec<u8> = format!(
            "BYE sip:{}@{} SIP/2.0\r\nVia: SIP/2.0/UDP {};branch=z9hG4bK{}\r\nFrom: ",
            self.cfg.user,
            c.addr,
            self.contact_host_port(c.addr),
            tok(12)
        )
        .into_bytes();
        out.extend_from_slice(&c.h.to);
        out.extend_from_slice(format!(";tag={}\r\nTo: ", c.tag).as_bytes());
        out.extend_from_slice(&c.h.from);
        out.extend_from_slice(b"\r\nCall-ID: ");
        out.extend_from_slice(&c.h.call_id);
        out.extend_from_slice(b"\r\nCSeq: 2 BYE\r\nContent-Length: 0\r\n\r\n");
        self.send_bytes(&out, c.addr);
    }
}
