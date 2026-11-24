//! `talk_dial.go`: a call OUT to the front door (D-20261111-b558: the Doorbell Talk element must
//! be available at idle so the owner can dial the doorbell from his phone and talk to whoever is
//! at the door).
//!
//! The pieces already exist: the single-session guard, the console talkback-session, the uplink
//! and the downlink (`talk_media.rs`, `talk_down.rs`). A call out is the same media, opened by
//! the PHONE instead of by a ring:
//!
//! * `can_dial` (capabilities) is true while the entry has dial out on (default;
//!   `dial_enabled: false` turns it off).
//! * [`CallDialer::dial_call`]: refused when anything else holds the door (a ring being held or
//!   answered, another dial: `Busy`); otherwise the media opens NOW, guard first (bell-talk
//!   holding the sink refuses, fail closed), then the console session, then both ffmpegs. A
//!   refusal reports nothing and leaves nothing behind. There is no ring leg on a doorbell, so
//!   "dialling" is the time the media takes to open; on success the call is reported ringing,
//!   answered, active in one go (Python maps the two latter to in_call).
//! * [`AudioClaimer::claimed_audio`]: the hub's `webrtc_offer` bridges the browser/app leg to the
//!   media by call id. The phone has `dial_orphan` to connect; after that the session is closed
//!   (the same cap an answer has).
//! * [`CallHangupper`]: the phone's second press; closes both ffmpegs (the RTP stops; the console
//!   session has no teardown endpoint and lapses on its own, see `talk_media.rs`).
//! * Duration cap: `dial_max`, then the call is hung up (the gate's own cap is 300 s).
//!
//! A visitor ring while we are in a call out: Python (`door/ha_event_ring.py`) never sends
//! `external_ring` for it; the bell press is consumed as "hang up my call out"
//! (`hangup_call_why` with the reason `bell_pressed`) and nothing rings (the owner, 2026-11-11, Go
//! commit `fe4dafe`: a comment in Go, behaviour that lives in Python and in the reasoned hang-up
//! this driver implements). Were an `external_ring` to arrive anyway it is still refused `Busy`
//! (one call per door). Door unlock is out of scope: nothing here opens anything.
//!
//! Difference from Go (PARITY.md 143): a `dial_call` future that is dropped while the media
//! opens gives the door back (Go's cancelled context ends the open with an error, which does).

use crate::util::lock;
use std::sync::Arc;

use super::talk::{TalkAudio, TalkCall, TalkCore, TalkDriver};
use crate::cancel::CancelToken;
use crate::driver::{
    AudioClaimer, BoxFuture, CallAudio, CallDialer, DriverError, MediaTarget, Sentinel,
    STATE_ACTIVE, STATE_ANSWERED, STATE_RINGING,
};

/// Frees a door reserved for a dial whose future is dropped.
struct Reserved {
    core: Arc<TalkCore>,
    serial: u64,
    armed: bool,
}

impl Drop for Reserved {
    fn drop(&mut self) {
        if self.armed {
            let mut st = lock(&self.core.st);
            if st.call.as_ref().is_some_and(|c| c.serial == self.serial) {
                st.call = None;
            }
        }
    }
}

impl TalkCore {
    /// `DialCall`: places a call to the door. Returns once the media is open.
    pub(crate) async fn dial_call(
        self: &Arc<Self>,
        ctx: &CancelToken,
        call_id: &str,
    ) -> Result<(), DriverError> {
        if !self.cfg.dial_enabled {
            return Err(DriverError::wrapping(
                Sentinel::NotSupported,
                "unifi_protect_talk: DialCall: not_supported (dial out is switched off for this door)",
            ));
        }
        if call_id.is_empty() {
            return Err(DriverError::new("unifi_protect_talk: DialCall: no call id"));
        }
        let (serial, open, env) = {
            let mut st = lock(&self.st);
            if st.stopped || st.events.is_none() {
                return Err(DriverError::wrapping(
                    Sentinel::NotSupported,
                    "unifi_protect_talk: DialCall: the driver is not running: not_supported",
                ));
            }
            if st.call.is_some() {
                return Err(DriverError::wrapping(
                    Sentinel::Busy,
                    "unifi_protect_talk: DialCall: the door is in a call: busy",
                ));
            }
            let serial = self.next_serial();
            let mut c = TalkCall::new(serial, call_id);
            c.answering = true;
            c.outbound = true;
            // reserved from here: a ring or another dial now gets Busy
            st.call = Some(c);
            (serial, st.open.clone(), self.env(&st))
        };
        let mut guard = Reserved {
            core: self.clone(),
            serial,
            armed: true,
        };
        let opened = open.open(ctx, env, call_id).await;
        guard.armed = false;
        let m = match opened {
            Ok(m) => m,
            Err(e) => {
                {
                    let mut st = lock(&self.st);
                    if st.call.as_ref().is_some_and(|c| c.serial == serial) {
                        st.call = None;
                    }
                }
                self.note(call_id, "dial_failed", e.message());
                return Err(e);
            }
        };
        m.set_orphan_grace(self.timing.dial_orphan);

        let reported = {
            let _emit = lock(&self.emit);
            let mut st = lock(&self.st);
            if !st.call.as_ref().is_some_and(|c| c.serial == serial) || st.stopped {
                false
            } else {
                let audio: Arc<dyn CallAudio> = Arc::new(TalkAudio {
                    media: m.clone(),
                    core: self.clone(),
                    call_id: call_id.to_string(),
                });
                let cap = {
                    let (core, id, wait) =
                        (self.clone(), call_id.to_string(), self.timing.dial_max);
                    tokio::spawn(async move {
                        tokio::time::sleep(wait).await;
                        core.guarded("ending a call at its time limit", |c| {
                            // only the call this timer was armed for (PARITY.md 154): a newer
                            // call with the same id is not this one's to end
                            if c.holds_serial(&id, serial) {
                                let _ = c.hangup_call_why(&id, "");
                                c.note(&id, "ended", "the call reached its time limit");
                            }
                        });
                    })
                };
                let sink = {
                    let c = st.call.as_mut().expect("checked live");
                    c.answering = false;
                    c.answered = true;
                    c.media = Some(m.clone());
                    c.audio = Some(audio);
                    c.cap_timer = Some(cap);
                    st.events.clone()
                };
                drop(st);
                if let Some(sink) = sink {
                    sink.state_changed(call_id, STATE_RINGING);
                    sink.state_changed(call_id, STATE_ANSWERED);
                    sink.state_changed(call_id, STATE_ACTIVE);
                }
                true
            }
        };
        if !reported {
            // stopped, or hung up, while the media opened
            self.close_and_wait(&m).await;
            return Err(DriverError::wrapping(
                Sentinel::NoSuchCall,
                format!("unifi_protect_talk: {call_id} ended while dialling: no_such_call"),
            ));
        }
        // the media ending on its own ends the call
        self.watch_media(serial, call_id.to_string(), m);
        Ok(())
    }

    /// `ClaimedAudio`: the audio of a call this driver placed and still holds, by call id. An
    /// inbound call is never claimed here: its audio is handed to the hub by `answer_with_audio`.
    pub(crate) fn claimed_audio(&self, call_id: &str) -> Option<Arc<dyn CallAudio>> {
        let st = lock(&self.st);
        let c = st.call.as_ref()?;
        if !c.outbound || c.id != call_id {
            return None;
        }
        c.audio.clone()
    }
}

impl CallDialer for TalkDriver {
    fn dial_call<'a>(
        &'a self,
        ctx: &'a CancelToken,
        call_id: &'a str,
        _target: MediaTarget,
    ) -> BoxFuture<'a, Result<(), DriverError>> {
        Box::pin(async move { self.core.dial_call(ctx, call_id).await })
    }
}

impl AudioClaimer for TalkDriver {
    fn claimed_audio(&self, call_id: &str) -> Option<Arc<dyn CallAudio>> {
        self.core.claimed_audio(call_id)
    }
}
