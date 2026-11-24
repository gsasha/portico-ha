//! The RTP half: the far-end address, the strict source rule, the read loop and the per-call sender (`server.go` `rtpLoop`, `handleRTP`, `rtpSender`).

use super::*;

/// The station's media address: an IP and the port its SDP named. Go keeps the port as an
/// `int`, so a port over 65535 is a call whose RTP never leaves (the send fails and is not
/// counted).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct FarRtp {
    pub ip: IpAddr,
    pub port: i64,
}

impl FarRtp {
    /// Go's `*net.UDPAddr` text (`%v`): an IPv6 host is bracketed.
    pub(crate) fn go_text(&self) -> String {
        join_host_port(&self.ip.to_string(), self.port)
    }

    pub(super) fn socket_addr(&self) -> Option<SocketAddr> {
        u16::try_from(self.port)
            .ok()
            .map(|p| SocketAddr::new(self.ip, p))
    }
}

/// `rtpSourceOK`, the strict RTP source rule (D2): during a live call, media is believed only
/// from the host the station's SDP named. The IP is compared, not the port (a station may send
/// from a different source port than it listens on).
pub fn rtp_source_ok(c: Option<&Call>, from: IpAddr) -> bool {
    c.and_then(|c| c.far_rtp)
        .is_some_and(|f| f.ip.to_canonical() == from.to_canonical())
}

impl Server {
    pub(super) async fn rtp_loop(self: Arc<Self>, sock: UdpSocket) {
        let mut buf = vec![0u8; 2048];
        let mut errors = ReadErrors::default();
        loop {
            let r = tokio::select! {
                biased;
                _ = self.stop.cancelled() => return,
                r = sock.recv_from(&mut buf) => r,
            };
            match r {
                Ok((n, from)) => {
                    errors.ok();
                    let from = canon(from);
                    let pkt = &buf[..n];
                    if catch_unwind(AssertUnwindSafe(|| self.handle_rtp(pkt, from))).is_err() {
                        self.logf("gate_sip_server: an RTP handler panicked".to_string());
                    }
                }
                Err(e) => {
                    if errors.failed() {
                        self.give_up("RTP", &e);
                        return;
                    }
                    tokio::time::sleep(READ_RETRY_PAUSE).await;
                }
            }
        }
    }

    pub(super) fn handle_rtp(&self, raw: &[u8], from: SocketAddr) {
        // parse_rtp_packet strips the header (including any CSRC list) and copies the payload:
        // handing the header to the decoder would prepend 24 bytes of noise to every 20 ms
        // frame, a buzz at exactly the frame rate.
        let pkt: RtpPacket = match parse_rtp_packet(raw) {
            Ok(p) if !p.payload.is_empty() => p,
            _ => return,
        };
        let mut st = lock(&self.state);
        // Deliberate difference: gate-sip counts and plays RTP from ANY source. Here it must
        // come from the host the station's SDP named, during a live call.
        if !rtp_source_ok(st.cur.as_ref(), from.ip()) {
            st.st.rtp_rejected += 1;
            let mut first = None;
            if let Some(c) = st.cur.as_mut() {
                c.rtp_rejected += 1;
                if !c.reject_logged {
                    if let Some(f) = c.far_rtp {
                        c.reject_logged = true;
                        // Addresses only, never the payload. The port is the sender's source
                        // port; the expected side is the host the station's SDP named (the rule
                        // compares the IP, not the port).
                        first = Some(format!(
                            "gate_sip_server: RTP rejected for {}: from {from}, expected host {} (first of this call; the rest are only counted)",
                            go_quote(&c.id()),
                            f.ip
                        ));
                    }
                }
            }
            drop(st);
            if let Some(l) = first {
                self.logf(l);
            }
            return;
        }
        let alaw = {
            let Some(c) = st.cur.as_mut() else { return };
            c.rtp_in += 1;
            c.media.alaw
        };
        st.st.rtp_in += 1;
        // dial.go: the first media from the station on a call we placed makes it `active` (the
        // inbound call's `active` is the ACK; ours is the media).
        let first_media = st
            .cur
            .as_ref()
            .filter(|c| c.answered)
            .and_then(|c| c.out.as_ref().filter(|o| o.mark_active()).map(|_| c.id()));
        if let Some(id) = first_media {
            self.emit(Event::State {
                call_id: id,
                state: STATE_ACTIVE,
            });
        }
        if let Some((cl, _)) = &st.client {
            let pcm = if alaw {
                alaw_decode(&pkt.payload)
            } else {
                pkt.payload // 16 kHz: already PCM16 LE, passes straight through
            };
            // never block the RTP read path: a full queue drops and counts
            cl.push_down(pcm);
        }
    }

    /// `rtpSender`: keeps the path symmetric while a call is answered: many devices will not
    /// stream until they receive. The client's microphone if it has any ready, silence
    /// otherwise. 20 ms cadence, one frame per tick.
    pub(super) async fn rtp_sender(
        self: Arc<Self>,
        serial: u64,
        stop: CancelToken,
        media: Media,
        far: Option<FarRtp>,
    ) {
        // Two silences: A-law zero is 0xD5, linear PCM16 zero is a zero byte.
        let alaw_silence = vec![0xD5u8; 160];
        let wide_silence = vec![0u8; media_wide().frame_bytes];
        let mut r = [0u8; 6];
        crate::util::fill_random(&mut r);
        let mut seq = u16::from_be_bytes([r[0], r[1]]) % 60000;
        let ssrc = u32::from_be_bytes([r[2], r[3], r[4], r[5]]);
        let mut ts: u32 = 0;
        let mut t = tokio::time::interval_at(
            tokio::time::Instant::now() + Duration::from_millis(20),
            Duration::from_millis(20),
        );
        t.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        // The microphone queue of the client in use; each client's is taken once.
        let mut mic_client: Option<Arc<AudioClient>> = None;
        let mut mic_rx: Option<tokio::sync::mpsc::Receiver<Vec<u8>>> = None;
        loop {
            tokio::select! {
                biased;
                _ = stop.cancelled() => return,
                _ = t.tick() => {}
            }
            let (live, cl) = {
                let st = lock(&self.state);
                let live = st
                    .cur
                    .as_ref()
                    .is_some_and(|c| c.serial == serial && c.answered)
                    && far.is_some();
                (live, st.client.as_ref().map(|(c, _)| c.clone()))
            };
            if !live {
                continue;
            }
            let mut payload = if media.alaw {
                alaw_silence.clone()
            } else {
                wide_silence.clone()
            };
            if let Some(cl) = cl {
                if !mic_client.as_ref().is_some_and(|m| Arc::ptr_eq(m, &cl)) {
                    mic_rx = cl.take_microphone();
                    mic_client = Some(cl);
                }
                if let Some(rx) = mic_rx.as_mut() {
                    if let Ok(pcm) = rx.try_recv() {
                        payload = if media.alaw { alaw_encode(&pcm) } else { pcm };
                    }
                }
            }
            let pkt = RtpPacket {
                payload_type: media.pt as u8,
                sequence_number: seq,
                timestamp: ts,
                ssrc,
                marker: false,
                payload,
            };
            let sent = match (
                far.and_then(|f| f.socket_addr()),
                lock(&self.rtp_send).clone(),
            ) {
                (Some(dst), Some(sock)) => {
                    let dst = dest_for(self.rtp_v6, dst);
                    sock.send_to(&pkt.marshal(), dst).is_ok()
                }
                _ => false,
            };
            if sent {
                let mut st = lock(&self.state);
                st.st.rtp_out += 1;
                if let Some(c) = st.cur.as_mut().filter(|c| c.serial == serial) {
                    c.rtp_out += 1;
                }
            }
            seq = seq.wrapping_add(1);
            // The RTP timestamp counts SAMPLES: 160 per 20 ms at 8 kHz, 320 at 16 kHz.
            ts = ts.wrapping_add(media.rate / 50);
        }
    }
}
