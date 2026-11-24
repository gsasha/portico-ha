//! `uplink.go`: the phone-to-device jitter buffer, sized by the packets the phone actually sends.
//!
//! The slot size is taken from the FIRST packet (payload bytes are samples at 8 kHz for PCMU,
//! and G.722 sends one byte per 8 kHz clock tick too, so bytes / 8 = milliseconds) and the
//! buffer ticks at that rate; see Go's comment for the 2026-10-22 "buzzing" fault this fixed. A
//! packet whose size later differs is still stored in its own slot.
//!
//! The reordering, pacing and silence filling are `sip::JitterBuffer`'s; this wrapper adds the
//! lazy creation on the first packet, the `Out` channel of depth 8 that drops when full, and the
//! `SetSilence` / `Close` lifecycle.

use crate::util::lock;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use tokio::sync::mpsc;

use crate::cancel::CancelToken;
use crate::sip::jitter::Slot;
use crate::sip::JitterBuffer;

use super::G711_CLOCK_RATE;

pub const MIN_UPLINK_PTIME: Duration = Duration::from_millis(10);
pub const MAX_UPLINK_PTIME: Duration = Duration::from_millis(120);
/// The playout delay floor. Go used 60 ms (three 20 ms slots). A phone on Wi-Fi delivers its
/// microphone in bursts, and the kitchen tablet's 2026-11-21 calls lost 16 % of the playout
/// slots (and discarded 72 packets as late) behind 60 ms. 140 ms is seven 20 ms slots: it
/// absorbs a ~100 ms burst while adding 80 ms to the one-way mouth-to-door delay, which an
/// intercom (half-duplex in practice) tolerates; the resync window (`3 x delay / ptime`, 21
/// frames at 20 ms) follows the delay by construction.
pub const MIN_UPLINK_DELAY: Duration = Duration::from_millis(140);

/// The playout period and delay for a first packet of `first_len` bytes (Go's `Push`): ptime
/// = bytes / 8 ms clamped to `[10, 120]` ms, delay = 2 x ptime but at least
/// [`MIN_UPLINK_DELAY`].
pub fn uplink_timing(first_len: usize) -> (Duration, Duration) {
    uplink_timing_with(first_len, MIN_UPLINK_DELAY)
}

/// The Go delay floor, for the parity replays (`tests/golden_sfu.rs`).
pub const GO_MIN_UPLINK_DELAY: Duration = Duration::from_millis(60);

/// [`uplink_timing`] with the delay floor given.
pub fn uplink_timing_with(first_len: usize, min_delay: Duration) -> (Duration, Duration) {
    // time.Duration(len) * time.Second / g711ClockRate, in integer nanoseconds
    let mut ptime =
        Duration::from_nanos(first_len as u64 * 1_000_000_000 / u64::from(G711_CLOCK_RATE));
    ptime = ptime.clamp(MIN_UPLINK_PTIME, MAX_UPLINK_PTIME);
    let delay = (2 * ptime).max(min_delay);
    (ptime, delay)
}

struct State {
    jb: Option<JitterBuffer>,
    shut: bool,
    /// Fills a lost slot: 0xFF is mu-law zero; G.722 sets its own (`set_silence`).
    silence: u8,
    min_delay: Duration,
}

struct Inner {
    /// frames `forward` dropped because `Out` was full
    dropped: std::sync::atomic::AtomicU64,
    state: Mutex<State>,
    out_tx: mpsc::Sender<Slot>,
    out_rx: tokio::sync::Mutex<mpsc::Receiver<Slot>>,
    stop: CancelToken,
}

/// The phone-to-device jitter buffer. Cheap to clone; clones share the buffer.
#[derive(Clone)]
pub struct UplinkBuffer {
    inner: Arc<Inner>,
}

impl Default for UplinkBuffer {
    fn default() -> Self {
        Self::new()
    }
}

impl UplinkBuffer {
    /// `newUplinkBuffer`.
    pub fn new() -> Self {
        Self::with_min_delay(MIN_UPLINK_DELAY)
    }

    /// A buffer whose playout delay is at least `min_delay` (the parity replays use
    /// [`GO_MIN_UPLINK_DELAY`]).
    pub fn with_min_delay(min_delay: Duration) -> Self {
        let (out_tx, out_rx) = mpsc::channel(8);
        UplinkBuffer {
            inner: Arc::new(Inner {
                dropped: std::sync::atomic::AtomicU64::new(0),
                state: Mutex::new(State {
                    jb: None,
                    shut: false,
                    silence: 0xFF,
                    min_delay,
                }),
                out_tx,
                out_rx: tokio::sync::Mutex::new(out_rx),
                stop: CancelToken::new(),
            }),
        }
    }

    /// `SetSilence`: the byte a lost slot is filled with; it must be called before the first
    /// `push` (a later call changes nothing, as in Go).
    pub fn set_silence(&self, b: u8) {
        lock(&self.inner.state).silence = b;
    }

    /// `Push`: adds one received packet; the first one fixes the slot size. Empty payloads and
    /// pushes after `close` are ignored. The first push starts tasks, so it must run inside a
    /// tokio runtime.
    pub fn push(&self, seq: u16, payload: Vec<u8>) {
        if payload.is_empty() {
            return;
        }
        let jb = {
            let mut st = lock(&self.inner.state);
            if st.shut {
                return;
            }
            if st.jb.is_none() {
                let (ptime, delay) = uplink_timing_with(payload.len(), st.min_delay);
                let jb = JitterBuffer::new(ptime, delay, payload.len(), st.silence);
                tokio::spawn(forward(self.inner.clone(), jb.clone()));
                st.jb = Some(jb);
            }
            st.jb.clone().unwrap()
        };
        jb.push(seq, payload);
    }

    /// `Out()`: the next raw payload in sequence order, paced at the sender's own ptime. After
    /// `close` it hands out what is still queued, then `None` (Go's channel would simply stay
    /// silent).
    pub async fn recv(&self) -> Option<Vec<u8>> {
        self.recv_slot().await.map(|s| s.data)
    }

    /// Like `recv`, but says whether the payload is a stand-in for a lost packet.
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

    /// A frame if one is queued, without waiting. A test seam: it returns `None` whenever a
    /// `recv` is parked on the same buffer (it only `try_lock`s the receiver; R2-138).
    pub fn try_recv(&self) -> Option<Vec<u8>> {
        self.inner.out_rx.try_lock().ok()?.try_recv().ok().map(|s| s.data)
    }

    /// Frames held in the jitter buffer plus frames waiting in `Out` (the queue depth the
    /// periodic log reports).
    pub fn depth(&self) -> usize {
        let held = lock(&self.inner.state).jb.as_ref().map_or(0, |j| j.depth());
        held + (self.inner.out_tx.max_capacity() - self.inner.out_tx.capacity())
    }

    /// What the jitter buffer did (zeros before the first packet).
    pub fn jitter_stats(&self) -> crate::sip::jitter::JitterStats {
        let st = lock(&self.inner.state);
        st.jb.as_ref().map(|j| j.stats()).unwrap_or_default()
    }

    /// Frames dropped because `Out` was full (Go drops them silently).
    pub fn dropped(&self) -> u64 {
        self.inner
            .dropped
            .load(std::sync::atomic::Ordering::Relaxed)
    }

    /// `Close`: stops the forwarding task and the jitter buffer. Idempotent.
    pub fn close(&self) {
        let mut st = lock(&self.inner.state);
        if st.shut {
            return;
        }
        st.shut = true;
        self.inner.stop.cancel();
        if let Some(jb) = &st.jb {
            jb.close();
        }
    }
}

/// Go's `forward`: jitter buffer to `Out`, dropping a frame when `Out` is full (never blocks).
async fn forward(u: Arc<Inner>, jb: JitterBuffer) {
    loop {
        tokio::select! {
            biased;
            _ = u.stop.cancelled() => return,
            p = jb.recv_slot() => match p {
                None => return,
                Some(p) => {
                    if u.out_tx.try_send(p).is_err() {
                        u.dropped.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
                    }
                }
            }
        }
    }
}
