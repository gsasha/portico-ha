//! Port of `jitter.go`: reorders and paces incoming RTP audio. A fixed playout delay (not
//! adaptive) gives packets time to arrive and be reordered; a packet that never arrives in time
//! becomes silence at its slot, keeping the downstream cadence. Encoding-agnostic: it moves raw
//! payload bytes keyed by RTP sequence number.

use crate::util::lock;
use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use tokio::sync::mpsc;
use tokio::time::{interval_at, sleep_until, Instant, MissedTickBehavior};

use crate::cancel::CancelToken;

/// Floors the window (in frames) so a tiny delay does not make ordinary reordering look like a
/// discontinuity.
const RESYNC_MIN_GAP: i64 = 8;
/// Most distinct frames held before playout starts (the initial delay window is a few frames).
const PRE_PLAYOUT_MAX_FRAMES: usize = 256;
/// How many consecutive, mutually-close packets outside the window it takes to believe the
/// sender restarted its sequence numbering.
const RESYNC_OUTLIERS: i64 = 5;

#[derive(Default)]
pub(crate) struct State {
    pub(crate) buf: HashMap<u16, Vec<u8>>,
    pub(crate) next_seq: u16,
    started: bool,
    /// `next_seq` is valid (the initial buffering window has closed).
    pub(crate) playing: bool,
    outlier_seq: u16,
    outlier_count: i64,
    /// What the buffer did; in the state so one lock covers a tick (R1-153).
    stats: JitterStats,
}

/// Counters of what the buffer did, for diagnosis (Go keeps none).
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
pub struct JitterStats {
    /// slots played as silence: the packet never came, or came too late
    pub silence_slots: u64,
    /// packets that arrived behind `next_seq` and were discarded unplayed
    pub late_discarded: u64,
    /// packets refused by `admit` as outside the window (held back as possible outliers)
    pub outliers_refused: u64,
    /// sequence resynchronisations
    pub resyncs: u64,
    /// the largest lateness of a playout tick seen so far, in ms (how long after its scheduled
    /// instant the tick ran; with `MissedTickBehavior::Skip` a late tick is run once, not made up)
    pub max_tick_lag_ms: u64,
}

/// One playout slot: the payload bytes and whether they are a stand-in for a packet that never
/// arrived in time (`lost`). A consumer that decodes stateful audio (G.722) must not decode a
/// stand-in; it conceals the slot from what it already decoded (`sfu::UplinkConverter`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Slot {
    pub data: Vec<u8>,
    pub lost: bool,
}

struct Inner {
    ptime: Duration,
    delay: Duration,
    frame_len: usize,
    silence_byte: u8,
    state: Mutex<State>,
    out_tx: mpsc::Sender<Slot>,
    out_rx: tokio::sync::Mutex<mpsc::Receiver<Slot>>,
    stop: CancelToken,
}

/// Reorders and paces RTP payloads. Cheap to clone; clones share the buffer.
#[derive(Clone)]
pub struct JitterBuffer {
    inner: Arc<Inner>,
}

/// Whether `a` is earlier than `b` in RTP sequence space (RFC 3550 serial arithmetic).
pub fn seq_before(a: u16, b: u16) -> bool {
    (a.wrapping_sub(b) as i16) < 0
}

impl JitterBuffer {
    /// `NewJitterBuffer`: frames of `frame_len` bytes arriving nominally every `ptime`, played out
    /// `delay` after the first packet is pushed. `silence_byte` fills a lost or too-late slot
    /// (0 for L16, 0xFF for PCMU).
    pub fn new(ptime: Duration, delay: Duration, frame_len: usize, silence_byte: u8) -> Self {
        let (out_tx, out_rx) = mpsc::channel(8);
        JitterBuffer {
            inner: Arc::new(Inner {
                ptime,
                delay,
                frame_len,
                silence_byte,
                state: Mutex::default(),
                out_tx,
                out_rx: tokio::sync::Mutex::new(out_rx),
                stop: CancelToken::new(),
            }),
        }
    }

    /// Adds a received packet's payload at `seq` for eventual, correctly-ordered playout. The
    /// first call starts the playout clock, so it must run inside a tokio runtime.
    pub fn push(&self, seq: u16, payload: Vec<u8>) {
        // Closed: nothing will ever play it, and a push inside the initial delay window would
        // otherwise be admitted for good (R1-103).
        if self.inner.stop.is_cancelled() {
            return;
        }
        let mut st = lock(&self.inner.state);
        if !st.started {
            st.started = true;
            // the playout clock is anchored to THIS push, not to whenever the spawned task first
            // runs (Go's goroutine starts within microseconds; a task on a busy current-thread
            // runtime can start milliseconds later, and the tick phase against the arrival stream
            // decides how many slots the buffer loses, see PARITY.md)
            tokio::spawn(playout_loop(self.inner.clone(), Instant::now()));
        }
        if self.inner.admit(&mut st, seq) {
            st.buf.insert(seq, payload);
        }
    }

    /// The next frame, in sequence order, paced at `ptime`. `None` once the buffer is closed and
    /// its queue is empty (Go's `Out()` channel simply stops delivering).
    pub async fn recv(&self) -> Option<Vec<u8>> {
        self.recv_slot().await.map(|s| s.data)
    }

    /// Like `recv`, but says whether the slot is a stand-in for a lost packet.
    pub async fn recv_slot(&self) -> Option<Slot> {
        let mut rx = self.inner.out_rx.lock().await;
        if let Ok(f) = rx.try_recv() {
            return Some(f);
        }
        if self.inner.stop.is_cancelled() {
            return None;
        }
        tokio::select! {
            f = rx.recv() => f,
            _ = self.inner.stop.cancelled() => rx.try_recv().ok(),
        }
    }

    /// A frame if one is queued, without waiting. Test seam (`tests/golden.rs`). Hazard (PARITY
    /// defect 9): returns `None` while a `recv` is waiting, because that call holds the receiver
    /// lock; use `recv` anywhere else.
    #[cfg(feature = "test-seams")]
    pub fn try_recv(&self) -> Option<Vec<u8>> {
        self.inner.out_rx.try_lock().ok()?.try_recv().ok().map(|s| s.data)
    }

    /// Frames held, waiting for their playout slot (a queue depth for the periodic log).
    pub fn depth(&self) -> usize {
        lock(&self.inner.state).buf.len()
    }

    /// What the buffer did so far.
    pub fn stats(&self) -> JitterStats {
        lock(&self.inner.state).stats
    }

    /// Stops the playout task. Idempotent.
    pub fn close(&self) {
        self.inner.stop.cancel();
    }

    #[cfg(test)]
    pub(crate) fn state(&self) -> std::sync::MutexGuard<'_, State> {
        lock(&self.inner.state)
    }

    #[cfg(test)]
    pub(crate) fn admit_for_test(&self, st: &mut State, seq: u16) -> bool {
        self.inner.admit(st, seq)
    }
}

impl Inner {
    /// How far (in frames) a packet's sequence number may sit from `next_seq` and still count as
    /// ordinary jitter or reordering: 3x the playout delay, with a floor.
    fn window(&self) -> i64 {
        let w = (3 * self.delay.as_nanos() / self.ptime.as_nanos()) as i64;
        w.max(RESYNC_MIN_GAP)
    }

    /// Decides whether a packet is stored, and detects a sequence discontinuity. Before playout
    /// starts everything is admitted. Afterwards a packet within `window()` of `next_seq` is
    /// ordinary; one outside it is held back, and `RESYNC_OUTLIERS` consecutive ones near each
    /// other make the buffer jump `next_seq` to the new numbering (dropping the old entries).
    fn admit(&self, st: &mut State, seq: u16) -> bool {
        if !st.playing {
            // DEV-796 S8: bounded before playout too (a replacement of a held seq is not growth).
            return st.buf.len() < PRE_PLAYOUT_MAX_FRAMES || st.buf.contains_key(&seq);
        }
        let w = self.window();
        let d = i64::from(seq.wrapping_sub(st.next_seq) as i16);
        if d >= -w && d <= w {
            st.outlier_count = 0;
            return true;
        }
        if st.outlier_count > 0 {
            let d = i64::from(seq.wrapping_sub(st.outlier_seq) as i16);
            if d < -w || d > w {
                st.outlier_count = 0;
            }
        }
        st.outlier_seq = seq;
        st.outlier_count += 1;
        st.stats.outliers_refused += 1;
        if st.outlier_count < RESYNC_OUTLIERS {
            return false;
        }
        st.stats.resyncs += 1;
        st.outlier_count = 0;
        st.buf = HashMap::new();
        st.next_seq = seq;
        true
    }
}

async fn playout_loop(j: Arc<Inner>, first_push: Instant) {
    tokio::select! {
        _ = sleep_until(first_push + j.delay) => {}
        _ = j.stop.cancelled() => return,
    }

    // next_seq starts at the EARLIEST sequence number seen during the initial buffering window,
    // not whichever packet arrived first (an out-of-order first arrival must not become the
    // reference point).
    {
        let mut st = lock(&j.state);
        let mut start: Option<u16> = None;
        for &seq in st.buf.keys() {
            if start.is_none_or(|s| seq_before(seq, s)) {
                start = Some(seq);
            }
        }
        st.next_seq = start.unwrap_or(0);
        st.playing = true;
    }

    let mut ticker = interval_at(first_push + j.delay + j.ptime, j.ptime);
    ticker.set_missed_tick_behavior(MissedTickBehavior::Skip);
    loop {
        tokio::select! {
            _ = j.stop.cancelled() => return,
            t = ticker.tick() => {
                let lag = Instant::now().saturating_duration_since(t).as_millis() as u64;
                let payload = {
                    let mut st = lock(&j.state);
                    st.stats.max_tick_lag_ms = st.stats.max_tick_lag_ms.max(lag);
                    let cur = st.next_seq;
                    let payload = st.buf.remove(&cur);
                    // Anything still buffered behind next_seq arrived too late to ever play.
                    let before = st.buf.len();
                    st.buf.retain(|&seq, _| !seq_before(seq, cur));
                    st.stats.late_discarded += (before - st.buf.len()) as u64;
                    st.next_seq = cur.wrapping_add(1);
                    if payload.is_none() {
                        st.stats.silence_slots += 1; // lost or too-late packet
                    }
                    payload
                };
                let slot = match payload {
                    Some(data) => Slot { data, lost: false },
                    None => Slot { data: vec![j.silence_byte; j.frame_len], lost: true },
                };
                let _ = j.out_tx.try_send(slot); // full: dropped, never blocks
            }
        }
    }
}

#[cfg(test)]
mod tests;
