//! One bridge: its two legs (`LegState`), the forwarders, the synchronous and the awaited halves of ending a call (`Bridge::end`, `EndTail`) and the counters line.

use super::manager::{guarded, side_index, Manager};
use super::{
    BoxFut, Event, FramesLeg, HouseLeg, WebRtcLeg, FRAME_BYTES, FRAME_TIME_NS, KIND_WEBRTC,
    REASON_BRIDGE_CLOSED, REASON_ERROR, REASON_LOST, REASON_SILENCE, REASON_WEBRTC_FAILED,
    SIDE_FROM, SIDE_TO, STATE_ATTACHED, STATE_FIRST_FRAME, STATE_LOST, STATE_SDP,
};
use crate::cancel::CancelToken;
use crate::gostd::parse::duration_string;
use std::future::Future;
use std::panic::catch_unwind;
use std::panic::AssertUnwindSafe;
use std::sync::atomic::AtomicBool;
use std::sync::atomic::AtomicI64;
use std::sync::atomic::AtomicU64;
use std::sync::atomic::Ordering;
use std::sync::Arc;
use std::sync::OnceLock;
use std::sync::Weak;
use std::time::Duration;
use tokio::sync::mpsc;
use tokio::sync::oneshot;
use tokio::sync::watch;
use tokio::sync::Notify;

/// How long a RESTARTED `finish` waits for the leg closes of the run it restarts.
const RESTART_CLOSE_WAIT: Duration = Duration::from_secs(2);

pub(super) struct BuiltLeg {
    pub(super) leg: Arc<dyn HouseLeg>,
    pub(super) frames: Option<Arc<dyn FramesLeg>>,
    pub(super) input: mpsc::Sender<Vec<u8>>,
}

pub(super) struct LegState {
    pub(super) side: &'static str,
    pub(super) kind: String,
    pub(super) owner: String,
    pub(super) leg_id: String,
    pub(super) built: OnceLock<BuiltLeg>,
    pub(super) held: AtomicBool,
    pub(super) seen: AtomicBool,
    /// restarts the silence watchdog (`timer.Reset`)
    pub(super) kick: Notify,
    // Counters. rx, bad, concealed, max_gap and last_rx are written by this leg's own pump only;
    // tx and dropped by the peer's pump; all are read once, when the call ends.
    pub(super) rx: AtomicI64,
    pub(super) tx: AtomicI64,
    pub(super) dropped: AtomicI64,
    pub(super) bad: AtomicI64,
    pub(super) concealed: AtomicI64,
    pub(super) max_gap: AtomicI64,
    pub(super) last_rx: AtomicU64,
}

impl LegState {
    pub(super) fn new(side: &str, kind: &str, owner: &str, leg_id: String) -> LegState {
        LegState {
            side: if side_index(side) == Some(1) {
                SIDE_TO
            } else {
                SIDE_FROM
            },
            kind: kind.to_string(),
            owner: owner.to_string(),
            leg_id,
            built: OnceLock::new(),
            held: AtomicBool::new(false),
            seen: AtomicBool::new(false),
            kick: Notify::new(),
            rx: AtomicI64::new(0),
            tx: AtomicI64::new(0),
            dropped: AtomicI64::new(0),
            bad: AtomicI64::new(0),
            concealed: AtomicI64::new(0),
            max_gap: AtomicI64::new(0),
            last_rx: AtomicU64::new(0),
        }
    }

    /// `count`: records one well-sized uplink frame: the frame count and the gap since the last.
    pub(super) fn count(&self, now: u64) {
        self.rx.fetch_add(1, Ordering::SeqCst);
        let last = self.last_rx.swap(now, Ordering::SeqCst);
        if last != 0 {
            let gap = now.saturating_sub(last) as i64;
            if gap > self.max_gap.load(Ordering::SeqCst) {
                self.max_gap.store(gap, Ordering::SeqCst);
            }
            let missed = (gap + FRAME_TIME_NS / 2) / FRAME_TIME_NS - 1;
            if missed > 0 {
                self.concealed.fetch_add(missed, Ordering::SeqCst);
            }
        }
    }
}

/// `Duration.Round` for a positive duration.
pub(super) fn round_ns(d: i64, m: i64) -> i64 {
    if d <= 0 {
        return d;
    }
    let r = d % m;
    if r + r < m {
        d - r
    } else {
        d + m - r
    }
}

// ---- the bridge --------------------------------------------------------------------------------

/// One house call: two legs, two forwarders.
pub struct Bridge {
    pub(super) m: Arc<Manager>,
    pub(super) call_id: String,
    pub(super) session: String,
    pub(super) legs: [Arc<LegState>; 2],
    pub(super) started: AtomicU64,
    pub(super) ended: AtomicBool,
    pub(super) done: CancelToken,
    pub(super) released: AtomicBool,
    pub(super) rt: tokio::runtime::Handle,
}

/// The awaited half of ending a call (see the module comment). If it is dropped before `run`
/// finished (the task that awaited it was torn down), the remainder runs on the runtime instead:
/// the legs are closed, the counters line is written and the `lost` events go out. Every step is
/// idempotent, so a restart from the top is safe.
#[must_use = "an EndTail that is not run leaves the legs open"]
pub struct EndTail {
    pub(super) inner: Option<Tail>,
}

pub(super) struct Tail {
    pub(super) b: Arc<Bridge>,
    pub(super) cause: String,
    pub(super) reason: String,
    pub(super) notify: bool,
    /// resolves (`true`) once the closes of the FIRST run of `finish` are over. A restarted
    /// `finish` waits on this instead of calling `close` again: a second `close` of a closed leg
    /// returns at once, even while the first one's work (a stalled `media_closed` write) goes on,
    /// and the counters line and the `lost` events would overtake it (R2-103).
    pub(super) closed: OnceLock<watch::Receiver<bool>>,
}

impl EndTail {
    /// Closes both legs (together), writes the counters line and queues the `lost` events.
    pub async fn run(mut self) {
        if let Some(t) = self.inner.as_ref() {
            t.finish().await;
        }
        self.inner = None;
    }
}

impl Drop for EndTail {
    fn drop(&mut self) {
        if let Some(t) = self.inner.take() {
            let rt = t.b.rt.clone();
            rt.spawn(async move { t.finish().await });
        }
    }
}

impl Tail {
    pub(super) async fn finish(&self) {
        let b = &self.b;
        // Together: a leg whose close waits on a slow notification must not hold up the other.
        // Each close is a detached task (a `JoinSet` would abort them when this tail is dropped
        // and restarted, R1-34), and so is the task that waits for them: a dropped `finish`
        // leaves both running, and its restart waits on the same `watch`.
        let first_run = self.closed.get().is_none();
        let mut rx = self
            .closed
            .get_or_init(|| {
                let closes: Vec<BoxFut> = b
                    .legs
                    .iter()
                    .filter_map(|ls| ls.built.get().map(|bl| bl.leg.close(&self.reason)))
                    .collect();
                let handles: Vec<_> = closes.into_iter().map(|c| b.rt.spawn(c)).collect();
                let (tx, rx) = watch::channel(false);
                b.rt.spawn(async move {
                    for h in handles {
                        if h.await.is_err_and(|e| e.is_panic()) {
                            crate::panic_hook::err_line(
                                "portico-transport: house: leg close: a task panicked",
                            );
                        }
                    }
                    let _ = tx.send(true);
                });
                rx
            })
            .clone();
        if first_run {
            let _ = rx.wait_for(|done| *done).await;
        } else {
            // a restart: bounded, so a close that never finishes (a stuck notification write)
            // cannot hold the counters line and the `lost` events back for ever
            let _ = tokio::time::timeout(RESTART_CLOSE_WAIT, rx.wait_for(|done| *done)).await;
        }
        if let Some(logf) = b.m.cfg.logf.clone() {
            let line = b.summary(&self.cause, &self.reason);
            if catch_unwind(AssertUnwindSafe(|| logf(line))).is_err() {
                crate::panic_hook::err_line("portico-transport: house: the log hook panicked");
            }
        }
        if self.notify {
            for ls in &b.legs {
                let r = if ls.side == self.cause {
                    &self.reason
                } else {
                    REASON_BRIDGE_CLOSED
                };
                b.m.emit(Event {
                    call_id: b.call_id.clone(),
                    side: ls.side.to_string(),
                    state: STATE_LOST.to_string(),
                    reason: r.to_string(),
                    sdp: String::new(),
                });
            }
        }
    }
}

impl Bridge {
    /// `end`, synchronous half: finishes the call once (the flag, the done token, the slots).
    /// `None` when the call already ended. The caller runs the tail: await it, or spawn it.
    /// `cause` is the leg that ended it (`""` for none).
    pub(super) fn end(
        self: &Arc<Self>,
        cause: &str,
        reason: &str,
        notify: bool,
    ) -> Option<EndTail> {
        if self.ended.swap(true, Ordering::SeqCst) {
            return None;
        }
        self.done.cancel();
        self.m.release(self);
        Some(EndTail {
            inner: Some(Tail {
                b: self.clone(),
                cause: cause.to_string(),
                reason: reason.to_string(),
                notify,
                closed: OnceLock::new(),
            }),
        })
    }

    /// `end` from a synchronous callback: the tail runs on the runtime.
    pub(super) fn end_detached(self: &Arc<Self>, cause: &'static str, reason: &'static str) {
        if let Some(t) = self.end(cause, reason, true) {
            self.rt.spawn(async move { t.run().await });
        }
    }

    /// `abort`: undoes a half-built bridge: nothing was started, the legs that exist are closed.
    pub(super) async fn abort(self: &Arc<Self>) {
        if self.ended.swap(true, Ordering::SeqCst) {
            return;
        }
        self.done.cancel();
        self.m.release(self);
        for ls in &self.legs {
            if let Some(bl) = ls.built.get() {
                guarded("leg abort", bl.leg.close(REASON_LOST)).await;
            }
        }
    }

    /// Answers the offer of the WebRTC leg at `idx` in a task that carries this call: a failed
    /// answer ends the call (`webrtc_failed`), a good one is announced as the `sdp` event.
    pub(super) fn spawn_answer(
        self: &Arc<Self>,
        idx: usize,
        leg: Arc<dyn WebRtcLeg>,
        offer: String,
    ) {
        let (b2, side) = (self.clone(), self.legs[idx].side);
        self.spawn_guarded("answer", side, REASON_WEBRTC_FAILED, async move {
            let r = leg.handle_offer(offer).await;
            if b2.ended.load(Ordering::SeqCst) {
                return;
            }
            match r {
                Err(_) => {
                    if let Some(t) = b2.end(side, REASON_WEBRTC_FAILED, true) {
                        t.run().await;
                    }
                }
                Ok(sdp) => b2.m.emit(Event {
                    call_id: b2.call_id.clone(),
                    side: side.to_string(),
                    state: STATE_SDP.to_string(),
                    reason: String::new(),
                    sdp,
                }),
            }
        });
    }

    /// Spawns a task that carries this call. A panic in it ends the call (both legs closed, both
    /// told) with `reason`.
    pub(super) fn spawn_guarded(
        self: &Arc<Self>,
        what: &'static str,
        side: &'static str,
        reason: &'static str,
        fut: impl Future<Output = ()> + Send + 'static,
    ) {
        let b = self.clone();
        // one task for the work and its guard (R1-47), not a task that only awaits another
        self.rt.spawn(async move {
            if crate::guard::catch(fut).await.is_err() {
                crate::panic_hook::err_line(&format!(
                    "portico-transport: house: {what}: a task panicked; the call is ended"
                ));
                if let Some(t) = b.end(side, reason, true) {
                    t.run().await;
                }
            }
        });
    }

    pub(super) fn start(self: &Arc<Self>) {
        self.started.store(self.m.now(), Ordering::SeqCst);
        // The watchdogs, armed now: a leg that never delivers its first frame hits the 10 s audio
        // deadline too.
        for ls in &self.legs {
            let (b, ls) = (self.clone(), ls.clone());
            let side = ls.side;
            self.spawn_guarded("watchdog", side, REASON_ERROR, async move {
                let silence = b.m.cfg.silence;
                loop {
                    tokio::select! {
                        biased;
                        _ = b.done.cancelled() => return,
                        _ = ls.kick.notified() => {}
                        _ = tokio::time::sleep(silence) => {
                            if let Some(t) = b.end(side, REASON_SILENCE, true) {
                                t.run().await;
                            }
                            return;
                        }
                    }
                }
            });
        }
        // Registered before the forwarders so a leg that is already gone ends the call at once.
        for ls in &self.legs {
            let bl = ls.built.get().expect("legs are built before start");
            let (weak, side) = (Arc::downgrade(self), ls.side);
            bl.leg.on_disconnected(Box::new(move || {
                if let Some(b) = Weak::upgrade(&weak) {
                    b.end_detached(side, REASON_LOST);
                }
            }));
            if ls.kind == KIND_WEBRTC {
                let weak = Arc::downgrade(self);
                bl.leg.on_connected(Box::new(move || {
                    if let Some(b) = Weak::upgrade(&weak) {
                        if !b.ended.load(Ordering::SeqCst) {
                            b.m.emit(Event {
                                call_id: b.call_id.clone(),
                                side: side.to_string(),
                                state: STATE_ATTACHED.to_string(),
                                ..Default::default()
                            });
                        }
                    }
                }));
            }
        }
        for (s, d) in [(0usize, 1usize), (1, 0)] {
            let b = self.clone();
            let side = self.legs[s].side;
            self.spawn_guarded("forwarder", side, REASON_ERROR, async move {
                b.pump(s, d).await
            });
        }
    }

    /// `pump`: forwards the uplink of leg `s` to leg `d` until the bridge ends.
    pub(super) async fn pump(self: Arc<Self>, s: usize, d: usize) {
        let (src, dst) = (&self.legs[s], &self.legs[d]);
        let out = src.built.get().expect("built").leg.output();
        // Not `out.lock().await` bare: were the lock ever held elsewhere, the pump would outlive
        // its bridge (R1-118).
        let mut rx = tokio::select! {
            biased;
            _ = self.done.cancelled() => return,
            g = out.lock() => g,
        };
        let to = &dst.built.get().expect("built").input;
        loop {
            let f = tokio::select! {
                biased;
                _ = self.done.cancelled() => return,
                f = rx.recv() => f,
            };
            let Some(mut f) = f else {
                if let Some(t) = self.end(src.side, REASON_LOST, true) {
                    t.run().await;
                }
                return;
            };
            if let Some(delivered) = self.touch(src) {
                // the first frame of the leg: `first_frame` is written before the frame goes on
                // (Go: `touch` writes it in this goroutine, then the frame is forwarded); a call
                // that ends meanwhile stops waiting
                tokio::select! {
                    biased;
                    _ = self.done.cancelled() => return,
                    _ = delivered => {}
                }
            }
            if f.len() != FRAME_BYTES {
                src.bad.fetch_add(1, Ordering::SeqCst);
                continue;
            }
            src.count(self.m.now());
            if dst.held.load(Ordering::SeqCst) {
                continue;
            }
            if src.held.load(Ordering::SeqCst) {
                f = vec![0u8; FRAME_BYTES]; // L16 silence
            }
            match to.try_send(f) {
                Ok(()) => {
                    dst.tx.fetch_add(1, Ordering::SeqCst);
                }
                Err(_) => {
                    dst.dropped.fetch_add(1, Ordering::SeqCst);
                }
            }
        }
    }

    /// `touch`: records an uplink frame: feeds the watchdog and reports the first one. For the
    /// first one it returns what resolves when `first_frame` has been written.
    pub(super) fn touch(&self, ls: &LegState) -> Option<oneshot::Receiver<()>> {
        let first = (!ls.seen.swap(true, Ordering::SeqCst)).then(|| {
            self.m.emit_delivered(Event {
                call_id: self.call_id.clone(),
                side: ls.side.to_string(),
                state: STATE_FIRST_FRAME.to_string(),
                ..Default::default()
            })
        });
        ls.kick.notify_one();
        first
    }

    /// The one line a call writes when it ends: the reason, the leg that caused it, the duration
    /// and each leg's counters. No owner, no address: a call id and numbers.
    pub(super) fn summary(&self, cause: &str, reason: &str) -> String {
        let cause = if cause.is_empty() { "-" } else { cause };
        let dur = round_ns(
            self.m
                .now()
                .saturating_sub(self.started.load(Ordering::SeqCst)) as i64,
            10_000_000,
        );
        let mut s = format!(
            "house call {} ended reason={reason} cause={cause} dur={}",
            self.call_id,
            duration_string(dur)
        );
        for ls in &self.legs {
            s.push_str(&format!(
                " {}[{} rx={} tx={} dropped={} bad={} concealed={} maxgap={}]",
                ls.side,
                ls.kind,
                ls.rx.load(Ordering::SeqCst),
                ls.tx.load(Ordering::SeqCst),
                ls.dropped.load(Ordering::SeqCst),
                ls.bad.load(Ordering::SeqCst),
                ls.concealed.load(Ordering::SeqCst),
                duration_string(round_ns(ls.max_gap.load(Ordering::SeqCst), 1_000_000)),
            ));
        }
        s
    }
}
