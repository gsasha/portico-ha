//! The audio-client side: `attach`, the orphan grace timer, `release` and the `ClientHost` impl the `AudioClient` calls back into.

use super::*;

impl Server {
    /// `Attach`: claims the audio of the live call. ATTACHING IS ANSWERING: attaching to a
    /// ringing call sends the 200 OK. Re-attaching to a call that is already up (the Wi-Fi-blip
    /// recovery the orphan grace exists for) answers nothing.
    ///
    /// Deliberate difference from gate-sip: a second Attach while a client is attached is
    /// REFUSED (`AlreadyAttached`), not a displacement. gate-sip's displacement is what
    /// reproduced the starved-second-client bug (design §6, "only one audio client per call; a
    /// second late answer must be told already").
    pub fn attach(&self) -> Result<Arc<AudioClient>, ServerError> {
        self.attach_for("")
    }

    /// `attachFor`: Attach for one named call (W3: the hub knows the call id it is joining a
    /// browser to). With a non-empty `call_id` it attaches only to THAT call and only while it
    /// is still ringing: a different or finished call is `NoCall`, an already answered one
    /// `AlreadyAnswered`, both checked under the same lock as the attach so no other call can
    /// slip in between.
    pub fn attach_for(&self, call_id: &str) -> Result<Arc<AudioClient>, ServerError> {
        let mut st = lock(&self.state);
        let Some(c) = &st.cur else {
            return Err(ServerError::NoCall);
        };
        if !call_id.is_empty() && c.id() != call_id {
            return Err(ServerError::NoCall);
        }
        // a call we are still placing is never answered by an attach
        if c.out.is_some() && !c.answered {
            return Err(ServerError::NoCall);
        }
        if !call_id.is_empty() && c.answered {
            return Err(ServerError::AlreadyAnswered);
        }
        if st.client.is_some() {
            return Err(ServerError::AlreadyAttached);
        }
        // Six frames, as gate-sip measured: 120 ms of slack against bursty delivery.
        let host: Arc<dyn ClientHost> = self.arc();
        let (rate, frame_bytes, serial) = (c.media.rate, c.media.frame_bytes, c.serial);
        let cl = AudioClient::new(
            rate,
            frame_bytes,
            Arc::downgrade(&host),
            self.audio_stats.clone(),
        );
        if self.cfg.call_stats {
            cl.enable_stats();
        }
        st.client = Some((cl.clone(), serial));
        let mut answer = None;
        if let Some(c) = st.cur.as_mut() {
            if let Some(t) = c.orphan_timer.take() {
                t.cancel();
            }
            if !c.answered {
                answer = Some(());
            }
        }
        let send = answer.map(|()| self.answer_locked(&mut st));
        drop(st);
        if let Some(a) = send {
            self.logf("gate_sip_server: answered the call because a client attached".to_string());
            self.finish_answer(a);
        }
        Ok(cl)
    }

    /// `orphanExpired`: the client left and nobody came back inside the grace.
    pub(super) fn orphan_expired(&self, serial: u64) {
        // The check and the end of the call are ONE critical section (R2-115): with a gap between
        // them a client that re-attached in the gap had its call hung up under it (Go has the
        // window, server.go:1043-1052; the BYE itself goes out after the lock).
        let ended = {
            let mut st = lock(&self.state);
            let live = st.client.is_none()
                && st
                    .cur
                    .as_ref()
                    .is_some_and(|c| c.serial == serial && c.answered);
            if !live {
                return; // somebody re-attached, or the call is over / a different one
            }
            self.end_cur_locked(&mut st, "nobody attached within the orphan grace")
        };
        self.logf(format!(
            "gate_sip_server: no audio client for {} — hanging up the call",
            duration_string_of(self.cfg.orphan_grace)
        ));
        if let Some(c) = ended {
            self.send_bye(&c);
        }
    }
}

impl ClientHost for Server {
    /// DEV-811 P3: the client's audio ended with its counters on.
    fn call_stats(&self, call_id: &str, stats: serde_json::Value) {
        self.emit(Event::CallStats {
            call_id: call_id.to_string(),
            stats,
        });
    }

    /// `AudioClient.release`.
    fn release(&self, client: &AudioClient, deliberate: bool) {
        let mut st = lock(&self.state);
        let (cl, serial) = match &st.client {
            Some((c, s)) if std::ptr::eq(Arc::as_ptr(c), client) => (c.clone(), *s),
            _ => return,
        };
        st.client = None;
        if deliberate {
            cl.close("the client said goodbye");
            drop(st);
            if self.hangup_call(serial, "the client hung up") {
                self.logf(
                    "gate_sip_server: the client hung up — call ended immediately".to_string(),
                );
            }
            return;
        }
        cl.close("the client detached");
        // The timer is bound to THE CALL that was live when the client left, so a timer left
        // from an abandoned call can never hang up a later one.
        let grace = self.cfg.orphan_grace;
        if let Some(c) = st.cur.as_mut().filter(|c| c.serial == serial && c.answered) {
            if let Some(t) = c.orphan_timer.take() {
                t.cancel();
            }
            let token = CancelToken::new();
            c.orphan_timer = Some(token.clone());
            self.spawn_timer(grace, token, move |s| s.orphan_expired(serial));
        }
    }
}
