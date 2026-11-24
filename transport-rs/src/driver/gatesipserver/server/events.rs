//! The event queue: the `Event` kinds, `emit`, `flush_events` and the dispatcher task that hands them to the sink in order.

use super::*;

/// What the dispatcher hands the sink, in order.
#[derive(Debug, Clone)]
pub(super) enum Event {
    Ringing {
        call_id: String,
        device_id: String,
    },
    State {
        call_id: String,
        state: &'static str,
    },
    Caps {
        device_id: String,
        caps: Capabilities,
    },
    /// DEV-811 P3: one answered call's audio counters (only when `call_stats` is on)
    CallStats {
        call_id: String,
        stats: serde_json::Value,
    },
}

impl Event {
    /// A ring and the end of a call are never evicted from a full queue: losing the first hides
    /// a visitor, losing the second leaves a call open for ever on the other side.
    pub(super) fn is_critical(&self) -> bool {
        matches!(self, Event::Ringing { .. })
            || matches!(self, Event::State { state, .. } if *state == STATE_ENDED)
    }
}

impl Server {
    /// Queues an event; the caller holds the state lock so the order of transitions is the order
    /// of events.
    pub(super) fn emit(&self, e: Event) {
        let (d, total) = self.evq.push(e);
        if d != Dropped::Nothing {
            let what = match d {
                Dropped::OldestLowValue => "the oldest low-value event was dropped",
                _ => "the new event was dropped",
            };
            self.logf(format!(
                "gate_sip_server: event queue full, {what} ({total} dropped so far)"
            ));
        }
        self.evsig.notify_one();
    }

    pub(super) fn flush_events(&self) {
        loop {
            let Some(e) = self.evq.pop() else {
                return;
            };
            let r = catch_unwind(AssertUnwindSafe(|| match &e {
                Event::Ringing { call_id, device_id } => self.events.ringing(call_id, device_id),
                // Only a sink that asked for them hears these (driver.CapabilitiesSink).
                Event::Caps { device_id, caps } => {
                    if let Some(cs) = self.events.capabilities_sink() {
                        cs.capabilities_changed(device_id, caps);
                    }
                }
                Event::CallStats { call_id, stats } => {
                    if let Some(cs) = self.events.call_stats_sink() {
                        cs.call_audio_stats(call_id, stats);
                    }
                }
                Event::State { call_id, state } => self.events.state_changed(call_id, state),
            }));
            if r.is_err() {
                self.logf("gate_sip_server: the event sink panicked".to_string());
            }
        }
    }

    pub(super) async fn dispatch_loop(self: Arc<Self>) {
        loop {
            self.flush_events();
            if self.stop.is_cancelled() {
                self.flush_events();
                return;
            }
            tokio::select! {
                _ = self.evsig.notified() => {}
                _ = self.stop.cancelled() => {}
            }
        }
    }
}
